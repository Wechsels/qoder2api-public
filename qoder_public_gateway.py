"""Public data-plane gateway for qoder2api (qoder2api-public).

This process is the **only** thing exposed to the internet:

    internet ──nps tunnel──▶ gateway :8791 ──▶ qoder2api :8790 (127.0.0.1)

It enforces per-user API keys, endpoint whitelisting, rate/concurrency limits,
daily/monthly token budgets, model allow-lists and writes an audit/usage trail.
qoder2api itself keeps running with `--host 127.0.0.1 --api-key <internal>`.

Design: docs/design-public-gateway.md §7.  Zero third-party dependencies.
"""

import argparse
import errno
import json
import os
import secrets
import signal
import socket
import ssl
import sys
import threading
import time
from collections import deque
from http.client import HTTPConnection, HTTPSConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit

import qoder_public_config as public_config
import qoder_public_keys as public_keys
import qoder_public_meter as public_meter

VERSION = "1.0.0"
SERVER_NAME = "qoder2api-public"

# 绝不能透传的逐跳头部（请求与响应共用）
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "host",
    "content-length", "expect", "accept-encoding",
}
CLIENT_AUTH_HEADERS = ("authorization", "x-api-key", "api-key")

INFERENCE_PATHS = ("/v1/chat/completions", "/v1/responses", "/v1/messages")
PROXY_GET_PATHS = ("/v1/models",)
LOCAL_PATHS = ("/ping", "/healthz")
RESPONSE_BUFFER_LIMIT = 8 * 1024 * 1024


# --------------------------------------------------------------------- logging
class GatewayLog(object):
    """极简分级日志：stdout + 按大小轮转的文件（与上游 qoder_logpaths 同思路）。"""

    LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40}

    def __init__(self, directory, level="info", rotate_mb=20, keep=5):
        self.directory = directory
        self.threshold = self.LEVELS.get(str(level).lower(), 20)
        self.rotate_bytes = max(1, int(rotate_mb)) * 1024 * 1024
        self.keep = max(1, min(20, int(keep)))
        self.path = os.path.join(directory, "gateway.log")
        self._lock = threading.Lock()
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError:
            pass

    def __call__(self, message, level="info", tag="gateway", **extra):
        if self.LEVELS.get(str(level).lower(), 20) < self.threshold:
            return
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "%s %-5s [%s] %s" % (stamp, str(level).upper(), tag, message)
        with self._lock:
            try:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()
            except Exception:
                pass
            try:
                self._rotate_if_needed()
                with open(self.path, "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def _rotate_if_needed(self):
        try:
            if os.path.getsize(self.path) < self.rotate_bytes:
                return
        except OSError:
            return
        try:
            for index in range(self.keep - 1, 0, -1):
                src = "%s.%d" % (self.path, index)
                if os.path.isfile(src):
                    os.replace(src, "%s.%d" % (self.path, index + 1))
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass


# ------------------------------------------------------------------- helpers
def _now():
    return time.time()


class RateWindow(object):
    """每 Key 的滑窗 RPM（内存态；重启清零可接受）。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._events = {}

    def allow(self, key_id, rpm, now=None):
        if not rpm or rpm <= 0:
            return True
        moment = now if now is not None else _now()
        cutoff = moment - 60.0
        with self._lock:
            window = self._events.setdefault(key_id, deque())
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= int(rpm):
                return False
            window.append(moment)
            return True

    def count(self, key_id, now=None):
        moment = now if now is not None else _now()
        cutoff = moment - 60.0
        with self._lock:
            window = self._events.get(key_id)
            if not window:
                return 0
            while window and window[0] < cutoff:
                window.popleft()
            return len(window)


class AuthFailureTracker(object):
    """同一 IP 的鉴权失败计数（防爆破），窗口外自动清理。"""

    def __init__(self, limit=20, window=60):
        self.limit = max(1, int(limit))
        self.window = max(5, int(window))
        self._lock = threading.Lock()
        self._events = {}

    def is_blocked(self, ip):
        cutoff = _now() - self.window
        with self._lock:
            window = self._events.get(ip)
            if not window:
                return False
            while window and window[0] < cutoff:
                window.popleft()
            return len(window) >= self.limit

    def note(self, ip):
        now = _now()
        cutoff = now - self.window
        with self._lock:
            window = self._events.setdefault(ip, deque())
            while window and window[0] < cutoff:
                window.popleft()
            window.append(now)
            if len(self._events) > 4096:
                for stale in [k for k, v in self._events.items() if not v]:
                    self._events.pop(stale, None)


class RuntimeState(object):
    """进程内共享状态：Key 存储、用量账本、限流器、模型缓存。"""

    def __init__(self, config, log):
        self.config = config
        self.log = log
        self.store = public_keys.KeyStore(
            config.keys_path, log=log, defaults=config.defaults)
        self.store.start_autosave()
        logging_cfg = config.logging
        self.usage = public_meter.UsageLog(
            config.usage_log_path, log=log,
            rotate_mb=logging_cfg.get("rotate_mb", 20),
            keep=logging_cfg.get("keep", 5))
        self.rate = RateWindow()
        self.auth_failures = AuthFailureTracker(
            (config.endpoints or {}).get("auth_fail_limit", 20),
            (config.endpoints or {}).get("auth_fail_window", 60))
        self._sem_lock = threading.Lock()
        self._semaphores = {}
        self._global = threading.Semaphore(config.server_max_concurrent)
        self._models_lock = threading.Lock()
        self._models_cache = (0.0, None)
        self.started_at = time.time()

    # -- concurrency -----------------------------------------------------
    def semaphore_for(self, record):
        key_id = record["id"]
        limit = max(1, int(record.get("max_concurrent") or 1))
        with self._sem_lock:
            entry = self._semaphores.get(key_id)
            if entry is None or entry[0] != limit:
                entry = (limit, threading.Semaphore(limit))
                self._semaphores[key_id] = entry
            return entry[1]

    def acquire(self, record):
        semaphore = self.semaphore_for(record)
        if not semaphore.acquire(blocking=False):
            return False
        if not self._global.acquire(blocking=False):
            semaphore.release()
            return False
        return True

    def release(self, record):
        with self._sem_lock:
            entry = self._semaphores.get(record["id"])
        if entry is not None:
            try:
                entry[1].release()
            except ValueError:
                pass
        try:
            self._global.release()
        except ValueError:
            pass

    # -- model catalogue (缓存 60s，供 /v1/models 过滤) --------------------
    def upstream_models(self, force=False):
        with self._models_lock:
            stamp, cached = self._models_cache
            if not force and cached is not None and _now() - stamp < 60:
                return cached
        payload = fetch_upstream_models(self.config, self.log, self.store)
        with self._models_lock:
            self._models_cache = (_now(), payload)
        return payload


def client_ip(handler):
    """优先取 nps/反代写入的 X-Forwarded-For 首段，其次 X-Real-IP。"""
    for header in ("X-Forwarded-For", "X-Real-IP"):
        value = handler.headers.get(header)
        if value:
            first = value.split(",")[0].strip()
            if first:
                return first
    try:
        return handler.connection.getpeername()[0]
    except Exception:
        return ""


def extract_api_key(handler):
    """Bearer / x-api-key；返回 (secret, error_reason)。"""
    query = dict(parse_qsl(urlsplit(handler.path).query, keep_blank_values=True))
    if query.get("key"):
        return "", "query_key_not_allowed"
    auth = handler.headers.get("Authorization", "")
    if auth:
        parts = auth.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip(), ""
        return "", "malformed_authorization"
    for header in ("x-api-key", "api-key"):
        value = handler.headers.get(header)
        if value:
            return value.strip(), ""
    return "", "missing"


def protocol_for(path):
    return public_meter.protocol_for_path(path)


def error_body(protocol, message, code="", kind="invalid_request_error"):
    if protocol == public_meter.PROTO_ANTHROPIC:
        return {"type": "error",
                "error": {"type": kind or "invalid_request_error",
                          "message": message}}
    payload = {"error": {"message": message, "type": kind or "invalid_request_error"}}
    if code:
        payload["error"]["code"] = code
    return payload


def mask_key(secret):
    return public_config.mask(secret, keep=4)

# ------------------------------------------------------------------ upstream
def _connection_for(config, timeout):
    parts = urlsplit(config.upstream_url)
    if parts.scheme == "https":
        context = ssl.create_default_context()
        if not config.upstream_verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return HTTPSConnection(parts.hostname, parts.port or 443,
                               timeout=timeout, context=context), parts.path
    return HTTPConnection(parts.hostname, parts.port or 80,
                          timeout=timeout), parts.path


def _upstream_headers(config, extra=None):
    headers = {"Authorization": "Bearer " + config.upstream_api_key,
               "Accept-Encoding": "identity"}
    if extra:
        headers.update(extra)
    return headers


def fetch_upstream_models(config, log, store=None):
    """GET /v1/models（带内部 Key）；失败返回 None，由调用方降级。"""
    try:
        connection, base_path = _connection_for(config, config.upstream_connect_timeout)
        try:
            connection.request("GET", base_path.rstrip("/") + "/v1/models",
                               headers=_upstream_headers(config))
            response = connection.getresponse()
            body = response.read(RESPONSE_BUFFER_LIMIT)
            if response.status != 200:
                log("models: 上游返回 %d" % response.status, level="WARN", tag="models")
                return None
            return json.loads(body.decode("utf-8", "replace"))
        finally:
            connection.close()
    except Exception as exc:
        log("models: 拉取失败 %s" % exc, level="WARN", tag="models")
        return None


def filter_models_payload(payload, record, store):
    """按 Key 的模型白名单过滤 /v1/models 的 data[]。"""
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if not isinstance(data, list):
        return payload
    if not (record or {}).get("allowed_models"):
        return payload
    kept = [item for item in data
            if isinstance(item, dict) and store.model_allowed(record, item.get("id"))]
    out = dict(payload)
    out["data"] = kept
    return out


def inject_stream_usage(body_bytes, path):
    """给 OpenAI Chat 流式请求补 stream_options.include_usage=true。

    只在客户端没有显式配置 stream_options 时注入；任何异常都原样返回，
    绝不因为计量而破坏请求。
    """
    if path != "/v1/chat/completions":
        return body_bytes, False
    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        return body_bytes, False
    if not isinstance(payload, dict) or not payload.get("stream"):
        return body_bytes, False
    if isinstance(payload.get("stream_options"), dict):
        return body_bytes, False
    payload["stream_options"] = {"include_usage": True}
    try:
        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), True
    except Exception:
        return body_bytes, False


# ------------------------------------------------------------------- handler
class GatewayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVER_NAME + "/" + VERSION
    sys_version = ""

    # -- 基础设施 ---------------------------------------------------------
    @property
    def state(self):
        return self.server.state

    @property
    def config(self):
        return self.server.state.config

    @property
    def log(self):
        return self.server.state.log

    def log_message(self, fmt, *args):
        """覆盖默认实现：不写 stderr（访问日志我们自己记）。"""
        return

    def _request_id(self):
        return "req_" + secrets.token_hex(6)

    def _send_json(self, status, payload, extra_headers=None, request_id=""):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if request_id:
            self.send_header("x-qdp-request-id", request_id)
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        return status

    def _drain_body(self, limit=1048576):
        """拒绝请求前把未读的请求体吃掉，否则 keep-alive 连接会被客户端 abort。"""
        if getattr(self, "_body_consumed", False):
            return
        transfer = (self.headers.get("Transfer-Encoding") or "").lower()
        length_header = self.headers.get("Content-Length")
        if "chunked" in transfer or length_header is None:
            self.close_connection = True
            return
        try:
            length = int(length_header)
        except ValueError:
            self.close_connection = True
            return
        if length <= 0:
            self._body_consumed = True
            return
        if length > limit:
            self.close_connection = True
            return
        try:
            remaining = length
            while remaining > 0:
                piece = self.rfile.read(min(65536, remaining))
                if not piece:
                    break
                remaining -= len(piece)
            self._body_consumed = True
        except (OSError, ValueError):
            self.close_connection = True

    def _send_error_json(self, status, message, code="", headers=None,
                         request_id="", kind="invalid_request_error"):
        if status >= 400:
            self._drain_body()
        protocol = protocol_for(urlsplit(self.path).path)
        extra = dict(headers or {})
        if status == 401:
            extra.setdefault("WWW-Authenticate", 'Bearer realm="qoder2api-public"')
        return self._send_json(status, error_body(protocol, message, code, kind),
                               extra_headers=extra, request_id=request_id)

    def _send_text(self, status, text, content_type="text/plain; charset=utf-8",
                   request_id=""):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if request_id:
            self.send_header("x-qdp-request-id", request_id)
        self.end_headers()
        self.wfile.write(body)

    # -- 解析请求 ---------------------------------------------------------
    def _normalized_path(self):
        raw = urlsplit(self.path).path or "/"
        try:
            from urllib.parse import unquote
            path = unquote(raw)
        except Exception:
            path = raw
        if "\\" in path or "\\0" in path:
            return ""
        if ".." in path or "//" in path:
            return ""
        if len(path) > 256:
            return ""
        return path.rstrip("/") or "/"

    def _read_body(self, limit):
        """读取完整请求体；支持 Content-Length 与 chunked。返回 (bytes, error)。"""
        transfer = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in transfer:
            return self._read_chunked(limit)
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            return b"", ""
        try:
            length = int(raw_length)
        except ValueError:
            return b"", "invalid_content_length"
        if length < 0:
            return b"", "invalid_content_length"
        if length > limit:
            return b"", "body_too_large"
        if length == 0:
            return b"", ""
        remaining = length
        chunks = []
        while remaining > 0:
            try:
                piece = self.rfile.read(min(65536, remaining))
            except (OSError, ValueError):
                return b"", "body_read_failed"
            if not piece:
                return b"", "body_read_failed"
            chunks.append(piece)
            remaining -= len(piece)
        return b"".join(chunks), ""

    def _read_chunked(self, limit):
        total = 0
        chunks = []
        while True:
            line = self.rfile.readline(1024)
            if not line:
                return b"", "body_read_failed"
            try:
                size = int(line.split(b";")[0].strip() or b"0", 16)
            except ValueError:
                return b"", "bad_chunk"
            if size == 0:
                while True:
                    trailer = self.rfile.readline(1024)
                    if not trailer or trailer in (b"\r\n", b"\n"):
                        break
                break
            total += size
            if total > limit:
                return b"", "body_too_large"
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                return b"", "body_read_failed"
            chunks.append(chunk)
            self.rfile.read(2)
        return b"".join(chunks), ""

    # -- 主入口 -----------------------------------------------------------
    def do_OPTIONS(self):
        self._reject(404, "not found", "not_found", allow="")

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self._handle(send_body=True)

    def do_POST(self):
        self._handle(send_body=True)

    def do_PUT(self):
        self._reject(405, "method not allowed", "method_not_allowed")

    do_DELETE = do_PATCH = do_PUT

    def _reject(self, status, message, code, allow=None):
        headers = {"Allow": allow} if allow else None
        self._send_error_json(status, message, code, headers=headers)

    def _handle(self, send_body=True):
        started = _now()
        request_id = self._request_id()
        self._body_consumed = False
        path = self._normalized_path()
        if not path:
            return self._send_error_json(400, "bad request path", "bad_path",
                                         request_id=request_id)
        if path in LOCAL_PATHS and self.command in ("GET", "HEAD"):
            if path == "/healthz":
                return self._send_json(200, {
                    "status": "ok", "service": SERVER_NAME, "version": VERSION,
                    "uptime_s": round(_now() - self.state.started_at, 1),
                }, request_id=request_id)
            return self._send_text(200, "pong", request_id=request_id)

        allowed = path in self.config.allowed_paths
        if not allowed:
            if path.startswith("/v1/"):
                return self._send_error_json(404, "unknown endpoint", "not_found",
                                             request_id=request_id)
            return self._send_error_json(404, "not found", "not_found",
                                         request_id=request_id)

        expected = "POST" if path in INFERENCE_PATHS or path.endswith(
            "/count_tokens") else "GET"
        if path == "/v1/models":
            expected = "GET"
        if self.command not in (expected, "HEAD"):
            return self._reject(405, "method not allowed", "method_not_allowed",
                                allow=expected)
        return self._authorized_flow(path, request_id, started, send_body)

    # -- 鉴权与策略 -------------------------------------------------------
    def _authorized_flow(self, path, request_id, started, send_body):
        ip = client_ip(self)
        if self.state.auth_failures.is_blocked(ip):
            return self._send_error_json(
                429, "too many failed authentication attempts, retry later",
                "auth_blocked",
                headers={"Retry-After": str(self.config.endpoints.get(
                    "auth_fail_window", 60))},
                request_id=request_id)

        secret, reason = extract_api_key(self)
        if reason:
            self.state.auth_failures.note(ip)
            message = ("query parameter ?key= is not accepted; use the "
                       "Authorization header") if reason == "query_key_not_allowed" \
                else "missing or malformed API key"
            return self._send_error_json(401, message, "invalid_api_key",
                                         request_id=request_id)
        record, verdict = self.state.store.lookup(secret)
        if record is None or verdict != "ok":
            self.state.auth_failures.note(ip)
            self.log("auth: 拒绝 %s ip=%s key=%s reason=%s"
                     % (path, ip, mask_key(secret), verdict), level="WARN",
                     tag="auth")
            return self._send_error_json(401, "invalid API key",
                                         "invalid_api_key", request_id=request_id)

        if not self.state.store.ip_allowed(record, ip):
            self.log("policy: IP 不在白名单 key=%s ip=%s" % (record["id"], ip),
                     level="WARN", tag="auth")
            return self._send_error_json(403, "client IP is not allowed for this key",
                                         "ip_not_allowed", request_id=request_id)

        limit = self.config.upstream_max_body
        body, error = self._read_body(limit)
        self._body_consumed = not error
        if error == "body_too_large":
            return self._send_error_json(
                413, "request body exceeds %d bytes" % limit, "body_too_large",
                request_id=request_id)
        if error:
            return self._send_error_json(400, "cannot read request body", error,
                                         request_id=request_id)

        model = ""
        if path in INFERENCE_PATHS:
            try:
                payload = json.loads(body.decode("utf-8"))
            except Exception:
                return self._send_error_json(400, "request body must be JSON",
                                             "invalid_json", request_id=request_id)
            if not isinstance(payload, dict):
                return self._send_error_json(400, "request body must be an object",
                                             "invalid_json", request_id=request_id)
            model = str(payload.get("model") or "").strip()
            if not model:
                return self._send_error_json(400, "model is required",
                                             "missing_model", request_id=request_id)
            if not model_allowed_for(record, model, self.state.store):
                self.log("policy: 模型不在白名单 key=%s model=%s"
                         % (record["id"], model), level="WARN", tag="policy")
                return self._send_error_json(
                    403, "model %r is not allowed for this key" % model,
                    "model_not_allowed", request_id=request_id)
        elif path == "/v1/models/refresh":
            if self.command != "POST" and self.command != "GET":
                return self._reject(405, "method not allowed",
                                    "method_not_allowed", allow="POST")

        # RPM / 并发
        if not self.state.rate.allow(record["id"], record.get("rpm")):
            retry = 60
            return self._send_error_json(
                429, "rate limit exceeded (%s requests/minute); retry in %ds"
                     % (record.get("rpm"), retry), "rate_limited",
                headers={"Retry-After": str(retry)}, request_id=request_id)
        totals = self.state.usage.totals(record["id"])
        quota_headers = {}
        exceeded = quota_exceeded(record, totals)
        if exceeded:
            retry = max(1, int(_seconds_until(totals["reset_at"])))
            return self._send_error_json(
                429, "%s quota exhausted (%d/%d tokens); resets at %s"
                     % (exceeded, totals["day_tokens"] if exceeded == "daily"
                        else totals["month_tokens"],
                        record.get("daily_tokens") if exceeded == "daily"
                        else record.get("monthly_tokens"),
                        totals["reset_at"]), "quota_exceeded",
                headers={"Retry-After": str(retry),
                         "x-qdp-quota-remaining": "0",
                         "x-qdp-quota-reset": totals["reset_at"]},
                request_id=request_id)

        if not self.state.acquire(record):
            return self._send_error_json(
                429, "too many concurrent requests for this key (max %d)"
                     % int(record.get("max_concurrent") or 1),
                "concurrency_limited", headers={"Retry-After": "2"},
                request_id=request_id)
        try:
            return self._forward(path, body, record, ip, request_id, started,
                                 model, send_body)
        finally:
            self.state.release(record)

    # -- 转发 -------------------------------------------------------------
    def _forward(self, path, body, record, ip, request_id, started, model,
                 send_body):
        config = self.config
        parts = urlsplit(config.upstream_url)
        upstream_path = (parts.path or "").rstrip("/") + path
        query = urlsplit(self.path).query
        if query and path in PROXY_GET_PATHS:
            upstream_path += "?" + query

        metering = config.metering or {}
        if (path == "/v1/chat/completions"
                and metering.get("inject_stream_usage", True)):
            body, _injected = inject_stream_usage(body, path)

        headers = {}
        for name, value in self.headers.items():
            lowered = name.lower()
            if lowered in HOP_BY_HOP or lowered in CLIENT_AUTH_HEADERS:
                continue
            if lowered.startswith("x-qdp-"):
                continue
            headers[name] = value
        headers["Host"] = parts.netloc or parts.hostname or ""
        headers["Authorization"] = "Bearer " + config.upstream_api_key
        headers["Accept-Encoding"] = "identity"
        headers["X-Forwarded-For"] = ip
        headers["X-Forwarded-Proto"] = "https"
        headers["X-QDP-Key-Id"] = record["id"]
        headers["X-QDP-Request-Id"] = request_id
        realm = record.get("realm") or ""
        if realm and not config.defaults.get("allow_client_realm", False):
            headers["X-Realm"] = realm

        started_ms = _now()
        meter = None
        if path in INFERENCE_PATHS:
            meter = public_meter.StreamMeter(path, request_body_bytes=len(body))
        status = 0
        streamed = False
        error_text = ""
        connection = None
        try:
            connection, _base = _connection_for(
                config, max(config.upstream_connect_timeout,
                            min(config.upstream_read_timeout, 60)))
            connection.timeout = config.upstream_read_timeout
            connection.request(self.command if self.command != "HEAD" else "GET",
                               upstream_path, body=body or None, headers=headers)
            upstream = connection.getresponse()
            status = upstream.status
            response_headers = upstream.getheaders()
            content_type = ""
            for name, value in response_headers:
                if name.lower() == "content-type":
                    content_type = value
                    break
            is_sse = "text/event-stream" in content_type.lower()
            if status >= 400:
                payload = upstream.read(RESPONSE_BUFFER_LIMIT)
                error_text = payload[:500].decode("utf-8", "replace")
                return self._relay_buffered(upstream, payload, request_id,
                                            record, ip, request_id_log=request_id,
                                            started=started, model=model,
                                            streamed=False, meter=None,
                                            error_text=error_text, body_sent=True)
            if is_sse:
                streamed = True
                return self._relay_stream(upstream, request_id, record, ip,
                                          started, model, meter)
            payload = upstream.read(RESPONSE_BUFFER_LIMIT)
            if path == "/v1/models":
                payload = self._filter_models_bytes(payload, record)
            return self._relay_buffered(upstream, payload, request_id, record, ip,
                                        request_id_log=request_id, started=started,
                                        model=model, streamed=False, meter=meter,
                                        error_text="", body_sent=send_body)
        except (socket.timeout, TimeoutError):
            error_text = "upstream timeout"
            self.log("forward: 上游超时 %s" % path, level="ERROR", tag="forward")
            self._write_audit(record, ip, path, 504, model, False,
                              public_meter.Usage(0, 0, 0, True), started,
                              error_text)
            return self._send_error_json(
                504, "upstream timed out after %ss" % config.upstream_read_timeout,
                "upstream_timeout", request_id=request_id)
        except Exception as exc:
            error_text = str(exc)
            self.log("forward: 上游不可达 %s (%s)" % (path, exc), level="ERROR",
                     tag="forward")
            self._write_audit(record, ip, path, 502, model, False,
                              public_meter.Usage(0, 0, 0, True), started,
                              error_text)
            return self._send_error_json(
                502, "upstream unreachable: %s" % exc, "upstream_unreachable",
                request_id=request_id)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    def _filter_models_bytes(self, payload, record):
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except Exception:
            return payload
        filtered = filter_models_payload(parsed, record, self.state.store)
        try:
            return json.dumps(filtered, ensure_ascii=False).encode("utf-8")
        except Exception:
            return payload

    def _client_headers(self, upstream_headers):
        out = []
        for name, value in upstream_headers:
            lowered = name.lower()
            if lowered in HOP_BY_HOP or lowered in ("set-cookie", "server",
                                                    "x-powered-by", "date"):
                continue
            if lowered in ("content-type", "cache-control"):
                out.append((name, value))
                continue
            if lowered.startswith("x-qoder-") or lowered.startswith("x-ratelimit-"):
                out.append((name, value))
        return out

    def _relay_buffered(self, upstream, payload, request_id, record, ip,
                        request_id_log, started, model, streamed, meter,
                        error_text, body_sent):
        usage = public_meter.Usage(0, 0, 0, False)
        if meter is not None:
            try:
                meter.feed(payload)
            except Exception:
                pass
            try:
                usage = meter.finish()
            except Exception:
                usage = public_meter.Usage(0, 0, 0, True)
        self.send_response(upstream.status)
        for name, value in self._client_headers(upstream.getheaders()):
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("x-qdp-request-id", request_id)
        self.end_headers()
        if body_sent and payload:
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass
        self._write_audit(record, ip, urlsplit(self.path).path, upstream.status,
                          model, streamed, usage, started, error_text)
        return upstream.status

    def _relay_stream(self, upstream, request_id, record, ip, started, model,
                      meter):
        self.send_response(upstream.status)
        sent_type = False
        for name, value in self._client_headers(upstream.getheaders()):
            if name.lower() == "content-type":
                sent_type = True
            self.send_header(name, value)
        if not sent_type:
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("x-qdp-request-id", request_id)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        error_text = ""
        try:
            while True:
                try:
                    piece = upstream.read1(65536) if hasattr(upstream, "read1") \
                        else upstream.read(65536)
                except (socket.timeout, TimeoutError):
                    error_text = "upstream timeout during stream"
                    break
                if not piece:
                    break
                try:
                    meter.feed(piece)
                except Exception:
                    pass
                self._write_chunk(piece)
            self._write_chunk(b"")
        except (BrokenPipeError, ConnectionResetError):
            error_text = "client aborted"
        except Exception as exc:
            error_text = "stream error: %s" % exc
        finally:
            try:
                usage = meter.finish()
            except Exception:
                usage = public_meter.Usage(0, 0, 0, True)
            self._write_audit(record, ip, urlsplit(self.path).path,
                              upstream.status, model, True, usage, started,
                              error_text)
        return upstream.status

    def _write_chunk(self, data):
        if data:
            chunk = ("%x\r\n" % len(data)).encode("ascii") + data + b"\r\n"
        else:
            chunk = b"0\r\n\r\n"
        self.wfile.write(chunk)
        self.wfile.flush()

    # -- 审计 -------------------------------------------------------------
    def _write_audit(self, record, ip, path, status, model, streamed, usage,
                     started, error_text):
        elapsed_ms = int((_now() - started) * 1000)
        row = {
            "ts": _iso_now(),
            "key_id": record["id"],
            "prefix": record.get("prefix", ""),
            "ip": ip,
            "method": self.command,
            "path": path,
            "status": int(status or 0),
            "model": model,
            "stream": bool(streamed),
            "prompt_tokens": usage.prompt,
            "completion_tokens": usage.completion,
            "estimated": bool(usage.estimated),
            "ms": elapsed_ms,
            "ua": (self.headers.get("User-Agent") or "")[:160],
            "err": (error_text or "")[:200],
        }
        self.state.usage.append(row)
        try:
            self.state.store.add_totals(
                record["id"], requests=1, prompt=usage.prompt,
                completion=usage.completion,
                errors=1 if int(status or 0) >= 400 else 0)
        except Exception:
            pass
        if int(status or 0) >= 400:
            self.log("req: %s %s -> %s key=%s ip=%s %dms %s"
                     % (self.command, path, status, record["id"], ip,
                        elapsed_ms, (error_text or "")[:80]),
                     level="WARN", tag="req")


# ------------------------------------------------------------------- policy
def model_allowed_for(record, model, store):
    try:
        return store.model_allowed(record, model)
    except Exception:
        return False


def quota_exceeded(record, totals):
    daily = int(record.get("daily_tokens") or 0)
    monthly = int(record.get("monthly_tokens") or 0)
    if daily and totals.get("day_tokens", 0) >= daily:
        return "daily"
    if monthly and totals.get("month_tokens", 0) >= monthly:
        return "monthly"
    return ""


def _seconds_until(iso_text):
    from datetime import datetime, timezone
    try:
        text = str(iso_text).replace("Z", "+00:00")
        moment = datetime.fromisoformat(text)
        if moment.tzinfo is None:
            moment = moment.astimezone()
        return max(1.0, (moment - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        return 60.0


def _iso_now():
    from datetime import datetime
    return datetime.now().astimezone().replace(microsecond=0).isoformat()

# -------------------------------------------------------------------- server
class GatewayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, state, use_tls=False, cert_file="",
                 key_file=""):
        self.state = state
        super(GatewayServer, self).__init__(address, handler)
        if use_tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert_file, key_file or None)
            self.socket = context.wrap_socket(self.socket, server_side=True)


def build_state(config, log):
    return RuntimeState(config, log)


def _port_in_use(host, port):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.6)
    try:
        return probe.connect_ex((host, port)) == 0
    except Exception:
        return False
    finally:
        probe.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="qoder2api-public gateway (auth + quota in front of qoder2api)")
    parser.add_argument("--config", default=None,
                        help="配置文件路径（默认 QDP_CONFIG 或 ./public-data/config.json）")
    parser.add_argument("--port", type=int, default=None,
                        help="覆盖 server.port（容器部署用）")
    parser.add_argument("--host", default=None, help="覆盖 server.host")
    parser.add_argument("--with-admin", dest="with_admin",
                        action="store_true", default=True,
                        help="同进程在后台线程启动管理面（默认开）")
    parser.add_argument("--no-admin", dest="with_admin", action="store_false",
                        help="不启动管理面（单独跑 qoder_public_cli.py admin）")
    parser.add_argument("--print-config", action="store_true",
                        help="打印脱敏配置后退出（排障用）")
    args = parser.parse_args(argv)

    try:
        config = public_config.load(args.config)
    except public_config.ConfigError as exc:
        sys.stderr.write("[ERROR] %s\n" % exc)
        return 2
    if args.port:
        config.raw["server"]["port"] = int(args.port)
    if args.host:
        config.raw["server"]["host"] = args.host
    if args.print_config:
        print(json.dumps(config.describe(), ensure_ascii=False, indent=2))
        return 0

    logging_cfg = config.logging
    log = GatewayLog(config.log_dir, logging_cfg.get("level", "info"),
                     logging_cfg.get("rotate_mb", 20), logging_cfg.get("keep", 5))
    log("%s v%s 启动" % (SERVER_NAME, VERSION), tag="system")
    log("config: %s" % json.dumps(config.describe(), ensure_ascii=False),
        tag="system")
    if config.upstream_panel_password in ("", "admin"):
        log("SECURITY: qoder2api 面板口令为空或仍是默认 admin，"
            "面板虽只听 127.0.0.1，仍建议在 config 里设置 upstream.panel_password",
            level="WARN", tag="system")
    if config.admin_host not in ("127.0.0.1", "::1", "localhost"):
        log("SECURITY: 管理面监听 %s（非本机）；确认已用防火墙限制访问"
            % config.admin_host, level="WARN", tag="system")

    state = build_state(config, log)

    if _port_in_use("127.0.0.1" if config.server_host in ("0.0.0.0", "")
                    else config.server_host, config.server_port):
        sys.stderr.write(
            "[ERROR] 端口 %d 已被占用：可能已有一份网关在运行。\n"
            "        先运行 stop-qoder-public 脚本，或用 --port 换端口。\n"
            % config.server_port)
        return 3

    try:
        server = GatewayServer((config.server_host, config.server_port),
                               GatewayHandler, state,
                               use_tls=config.tls_enabled,
                               cert_file=config.tls_cert_file,
                               key_file=config.tls_key_file)
    except OSError as exc:
        sys.stderr.write("[ERROR] 无法监听 %s:%d (%s)\n"
                         % (config.server_host, config.server_port, exc))
        return 3

    admin_server = None
    if args.with_admin:
        try:
            import qoder_public_admin as public_admin
            admin_server = public_admin.serve_in_thread(state)
            log("admin: 管理面已启动 http://%s:%d"
                % (config.admin_host, config.admin_port), tag="admin")
        except ImportError:
            log("admin: qoder_public_admin.py 不存在，管理面未启动（只能用 CLI）",
                level="WARN", tag="admin")
        except Exception as exc:
            log("admin: 管理面启动失败 %s" % exc, level="ERROR", tag="admin")

    stopping = threading.Event()

    def _shutdown(signum, _frame):
        if stopping.is_set():
            return
        stopping.set()
        log("收到信号 %s，正在停止..." % signum, tag="system")
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), _shutdown)
            except (ValueError, OSError):
                pass

    log("gateway: 监听 http://%s:%d%s" % (
        config.server_host, config.server_port,
        " (TLS)" if config.tls_enabled else ""), tag="system")
    log("gateway: 上游 %s，白名单 %d 个端点"
        % (config.upstream_url, len(config.allowed_paths)), tag="system")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            state.store.stop_autosave()
        except Exception:
            pass
        if admin_server is not None:
            try:
                admin_server.shutdown()
                admin_server.server_close()
            except Exception:
                pass
        try:
            server.server_close()
        except Exception:
            pass
        log("gateway: 已停止", tag="system")
    return 0


if __name__ == "__main__":
    sys.exit(main())
