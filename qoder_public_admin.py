#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Loopback-only administration for a running qoder2api-public gateway."""

import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import qoder_public_keys as public_keys


COOKIE_NAME = "qdp_admin"
KEY_FIELDS = (
    "id", "name", "owner", "prefix", "enabled", "created_at",
    "expires_at", "rpm", "max_concurrent", "daily_tokens",
    "monthly_tokens", "allowed_models", "allowed_ips", "realm", "note",
    "last_used_at", "totals", "deleted_at",
)
EDIT_FIELDS = (
    "name", "owner", "note", "expires_at", "rpm", "max_concurrent",
    "daily_tokens", "monthly_tokens", "allowed_models", "allowed_ips", "realm",
)
ID_RE = re.compile(r"^k_[A-Za-z0-9_-]{1,80}$")


def public_key(record):
    """Never expose the hash, even to the local browser."""
    return {field: record.get(field) for field in KEY_FIELDS if field in record}


def loopback(host):
    if str(host).lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


class Sessions(object):
    def __init__(self, ttl):
        self.ttl = max(300, min(int(ttl), 7 * 86400))
        self._items = {}
        self._lock = threading.Lock()

    def create(self):
        item = {"csrf": secrets.token_urlsafe(24),
                "expires": time.time() + self.ttl}
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._items[token] = item
            if len(self._items) > 100:
                now = time.time()
                self._items = {key: value for key, value in self._items.items()
                               if value["expires"] > now}
        return token, dict(item)

    def get(self, token):
        with self._lock:
            item = self._items.get(token)
            if not item:
                return None
            if item["expires"] <= time.time():
                self._items.pop(token, None)
                return None
            return dict(item)

    def revoke(self, token):
        with self._lock:
            self._items.pop(token, None)


class LoginGuard(object):
    def __init__(self):
        self._items = {}
        self._lock = threading.Lock()

    def failure(self, ip):
        now = time.time()
        with self._lock:
            failures, since, blocked = self._items.get(ip, (0, now, 0))
            if now < blocked:
                return blocked
            if now - since > 300:
                failures, since = 0, now
            failures += 1
            blocked = now + 300 if failures >= 10 else 0
            self._items[ip] = (failures, since, blocked)
            return blocked

    def blocked_until(self, ip):
        with self._lock:
            return self._items.get(ip, (0, 0, 0))[2]

    def success(self, ip):
        with self._lock:
            self._items.pop(ip, None)


def upstream_json(config, path, timeout=8):
    """Allow only the two fixed, loopback qoder2api operational endpoints."""
    base = urlsplit(config.upstream_url)
    if base.scheme not in ("http", "https") or not loopback(base.hostname):
        raise ValueError("上游必须监听本机，拒绝管理面探测远程地址")
    if path not in ("/health", "/v1/models"):
        raise ValueError("不支持的上游探测路径")
    request = urllib.request.Request(
        config.upstream_url + path,
        headers={"Authorization": "Bearer " + config.upstream_api_key,
                 "Accept": "application/json"})
    context = None
    if base.scheme == "https":
        context = ssl.create_default_context()
        if not config.upstream_verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
        return json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))


def tunnel_status(state, probe=False):
    cfg = state.config
    nps = dict(cfg.nps)
    nps["vkey"] = "已配置" if nps.get("vkey") else "未配置"
    result = {"mode": nps.get("mode"), "server_addr": nps.get("server_addr"),
              "server_port": nps.get("server_port"), "domain": nps.get("domain"),
              "crypt": nps.get("crypt", True), "compress": nps.get("compress", True),
              "vkey": nps["vkey"], "probe": None}
    if probe:
        address = str(nps.get("server_addr") or "")
        try:
            host, port = address.rsplit(":", 1)
            with socket.create_connection((host.strip("[]"), int(port)), timeout=3):
                pass
            result["probe"] = {"reachable": True,
                                 "message": "nps bridge TCP 可达；不代表 npc 已连接"}
        except (ValueError, OSError) as exc:
            result["probe"] = {"reachable": False, "message": str(exc)}
    return result


class AdminServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, state):
        if not loopback(address[0]):
            raise ValueError("管理面只允许绑定本机回环地址")
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        self.state = state
        self.sessions = Sessions(state.config.admin_session_ttl)
        self.login_guard = LoginGuard()
        super(AdminServer, self).__init__(address, AdminHandler)


class AdminHandler(BaseHTTPRequestHandler):
    server_version = "qoder-public-admin"
    sys_version = ""

    def log_message(self, fmt, *args):
        self.server.state.log("admin: " + fmt % args, tag="admin")

    def _send(self, status, data=None, content_type="application/json; charset=utf-8",
              cookie=None, nonce=""):
        if status >= 400:
            # 早退路径（CSRF/鉴权/参数错误）多半没读请求体；不排空就关闭
            # 连接会被协议栈 RST，客户端表现为 10053/Connection reset
            self._drain_body()
        body = (data if isinstance(data, bytes) else
                json.dumps(data, ensure_ascii=False).encode("utf-8"))
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'nonce-%s'; style-src 'self' 'unsafe-inline'; "
                         "connect-src 'self'; img-src 'self'; base-uri 'none'; "
                         "form-action 'self'; frame-ancestors 'none'" % nonce)
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status, message, code=""):
        self._send(status, {"error": {"code": code or "request_error",
                                      "message": message}})

    def _body(self):
        if "application/json" not in (self.headers.get("Content-Type") or "").lower():
            raise ValueError("请求体必须使用 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("请求体大小不合法")
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("JSON 请求体必须是对象")
            self._body_consumed = True
            return value
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("JSON 请求体无效: %s" % exc)

    def _drain_body(self, limit=65536):
        """错误响应前排空未读的请求体，避免关闭连接触发 RST。"""
        if getattr(self, "_body_consumed", False):
            return
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            return
        if length <= 0:
            self._body_consumed = True
            return
        if length > limit:
            self.close_connection = True
            return
        remaining = length
        while remaining > 0:
            piece = self.rfile.read(min(65536, remaining))
            if not piece:
                break
            remaining -= len(piece)
        self._body_consumed = True

    def _origin(self):
        origin = self.headers.get("Origin")
        if not origin or self.headers.get("X-Requested-With") != "qdp-admin":
            return False
        try:
            parsed = urlsplit(origin)
            return (parsed.scheme in ("http", "https") and
                    loopback(parsed.hostname) and
                    parsed.port == self.server.server_port and
                    not parsed.username and not parsed.password and
                    not parsed.path.strip("/") and not parsed.query and not parsed.fragment)
        except ValueError:
            return False

    def _origin_ok(self):
        """只要带了 Origin 头（浏览器跨源行为），就必须是本源；读写一致。"""
        if not self.headers.get("Origin"):
            return True
        return self._origin()

    def _auth(self, write=False):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(COOKIE_NAME)
        if not morsel:
            return None
        token = morsel.value
        item = self.server.sessions.get(token)
        if not item:
            return None
        if write and (not self._origin_ok() or not hmac.compare_digest(
                self.headers.get("X-QDP-CSRF", ""), item["csrf"])):
            return None
        item["token"] = token
        return item

    def _dispatch(self):
        if not loopback(self.client_address[0]):
            return self._error(403, "仅允许本机管理", "local_only")
        try:
            host = urlsplit("//" + self.headers.get("Host", ""))
            if not loopback(host.hostname) or host.port != self.server.server_port:
                return self._error(403, "Host 不受信任", "bad_host")
        except ValueError:
            return self._error(403, "Host 不受信任", "bad_host")
        path = urlsplit(self.path).path
        query = parse_qs(urlsplit(self.path).query)
        if not self._origin_ok():
            return self._error(403, "Origin 不受信任", "bad_origin")
        if path == "/" and self.command in ("GET", "HEAD"):
            try:
                filename = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        "qoder_public_dashboard.html")
                with open(filename, "rb") as handle:
                    page = handle.read()
                nonce = secrets.token_urlsafe(24)
                page = page.replace(b"__CSP_NONCE__", nonce.encode("ascii"))
                return self._send(200, page, "text/html; charset=utf-8", nonce=nonce)
            except OSError:
                return self._error(503, "管理页面不可用", "dashboard_unavailable")
        if path == "/api/login" and self.command == "POST":
            # login 本身是敏感写操作：必须显式带匹配的 Origin + 请求标记
            if not self._origin():
                return self._error(403, "Origin 或请求标记无效", "csrf_failed")
            ip = self.client_address[0]
            until = self.server.login_guard.blocked_until(ip)
            if until > time.time():
                return self._error(429, "登录失败次数过多，请稍后重试", "login_limited")
            try:
                body = self._body()
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_json")
            if not self.server.state.config.verify_admin_password(body.get("password", "")):
                until = self.server.login_guard.failure(ip)
                if until > time.time():
                    return self._error(429, "登录失败次数过多，请稍后重试", "login_limited")
                return self._error(401, "管理密码错误", "unauthorized")
            self.server.login_guard.success(ip)
            token, item = self.server.sessions.create()
            cookie = ("%s=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=%d"
                      % (COOKIE_NAME, token, self.server.sessions.ttl))
            return self._send(200, {"csrf": item["csrf"],
                                    "expires_at": datetime.fromtimestamp(
                                        item["expires"], timezone.utc).isoformat()},
                              cookie=cookie)
        write = self.command in ("POST", "PATCH", "DELETE")
        session = self._auth(write)
        if not session:
            return self._error(403 if write else 401,
                               "CSRF/Origin 校验失败" if write else "请先登录",
                               "csrf_failed" if write else "unauthorized")
        if path == "/api/session" and self.command == "GET":
            return self._send(200, {"csrf": session["csrf"],
                                    "expires_at": datetime.fromtimestamp(
                                        session["expires"], timezone.utc).isoformat()})
        if path == "/api/logout" and self.command == "POST":
            self.server.sessions.revoke(session["token"])
            return self._send(200, {"ok": True},
                              cookie=COOKIE_NAME + "=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0")
        try:
            return self._api(path, query)
        except public_keys.KeyStoreError as exc:
            status = 404 if "找不到" in str(exc) else 400
            return self._error(status, str(exc), "invalid_key")
        except (ValueError, TypeError) as exc:
            return self._error(400, str(exc), "invalid_request")
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            return self._error(503, "本机上游不可用: %s" % exc, "upstream_unavailable")

    def _api(self, path, query):
        state = self.server.state
        store, usage = state.store, state.usage
        if path == "/api/status" and self.command == "GET":
            keys = store.list()
            totals = usage.summary(days=1, names={key["id"]: key["name"] for key in keys})
            upstream = {"ok": False}
            try:
                health = upstream_json(state.config, "/health")
                upstream = {"ok": True, "status": health.get("status", "ok")}
            except Exception as exc:
                upstream["message"] = str(exc)
            result = {"version": "1.0.0", "keys": len(keys),
                      "enabled_keys": sum(1 for key in keys if key["enabled"]),
                      "today_requests": sum(row["requests"] for row in totals),
                      "today_errors": sum(row["errors"] for row in totals),
                      "upstream": upstream, "gateway": "%s:%d" % (
                          state.config.server_host, state.config.server_port)}
            snapshot = getattr(state, "runtime_snapshot", None)
            if callable(snapshot):
                result["runtime"] = snapshot()
            else:
                result["runtime"] = {"started_at": datetime.fromtimestamp(
                    state.started_at, timezone.utc).isoformat()}
            return self._send(200, result)
        if path == "/api/keys" and self.command == "GET":
            return self._send(200, {"keys": [public_key(row) for row in store.list()]})
        if path == "/api/keys" and self.command == "POST":
            data = self._body()
            allowed = set(EDIT_FIELDS) | {"enabled"}
            if set(data) - allowed:
                raise ValueError("包含不支持的 Key 字段")
            enabled = data.pop("enabled", True)
            if not isinstance(enabled, bool):
                raise ValueError("enabled 必须是布尔值")
            record, secret = store.create(**data)
            enabled_record = None
            if not enabled:
                enabled_record = store.set_enabled(record["id"], False)
            return self._send(201, {"key": public_key(enabled_record or record),
                                    "secret": secret})
        match = re.match(r"^/api/keys/([^/]+)(?:/(enable|disable|rotate))?$", path)
        if match:
            key_id, action = match.groups()
            if not ID_RE.match(key_id):
                return self._error(404, "资源不存在", "not_found")
            if self.command == "GET" and not action:
                record = store.get(key_id, include_deleted=False)
                if not record:
                    return self._error(404, "Key 不存在", "not_found")
                return self._send(200, {"key": public_key(record)})
            if self.command == "PATCH" and not action:
                data = self._body()
                if set(data) - set(EDIT_FIELDS) - {"enabled"}:
                    raise ValueError("包含不支持的 Key 字段")
                if "enabled" in data and not isinstance(data["enabled"], bool):
                    raise ValueError("enabled 必须是布尔值")
                record = store.update(key_id, **{field: value for field, value in data.items()
                                               if field in EDIT_FIELDS})
                if "enabled" in data:
                    record = store.set_enabled(key_id, data["enabled"])
                return self._send(200, {"key": public_key(record)})
            if self.command == "POST" and action in ("enable", "disable"):
                return self._send(200, {"key": public_key(store.set_enabled(
                    key_id, action == "enable"))})
            if self.command == "POST" and action == "rotate":
                record, secret = store.rotate(key_id)
                return self._send(200, {"key": public_key(record), "secret": secret})
            if self.command == "DELETE" and not action:
                return self._send(200, {"key": public_key(store.delete(key_id))})
        if path == "/api/usage" and self.command == "GET":
            days = int(query.get("days", ["7"])[0])
            if not 1 <= days <= 70:
                raise ValueError("days 必须在 1 到 70 之间")
            key_id = query.get("key_id", [None])[0]
            keys = store.list()
            quotas = {}
            for key in keys:
                if key_id and key["id"] != key_id:
                    continue
                used = usage.totals(key["id"])
                used["daily_remaining"] = (max(0, key["daily_tokens"] - used["day_tokens"])
                                             if key["daily_tokens"] else None)
                used["monthly_remaining"] = (max(0, key["monthly_tokens"] - used["month_tokens"])
                                               if key["monthly_tokens"] else None)
                quotas[key["id"]] = used
            return self._send(200, {"days": days,
                                    "summary": usage.summary(days=days, names={
                                        key["id"]: key["name"] for key in keys}),
                                    "daily": usage.daily(days=days, key_id=key_id),
                                    "quotas": quotas})
        if path == "/api/audit" and self.command == "GET":
            limit = int(query.get("limit", ["100"])[0])
            if not 1 <= limit <= 500:
                raise ValueError("limit 必须在 1 到 500 之间")
            return self._send(200, {"rows": usage.rows(
                days=7, key_id=query.get("key_id", [None])[0], limit=limit)})
        if path == "/api/models" and self.command == "GET":
            data = upstream_json(state.config, "/v1/models")
            fields = ("id", "object", "owned_by", "context_length", "realm")
            return self._send(200, {"models": [
                {field: row.get(field) for field in fields if field in row}
                for row in data.get("data", []) if isinstance(row, dict)]})
        if path == "/api/tunnel" and self.command == "GET":
            return self._send(200, tunnel_status(
                state, query.get("probe", ["0"])[0] == "1"))
        return self._error(404, "资源不存在", "not_found")

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def do_PATCH(self):
        self._dispatch()

    def do_DELETE(self):
        self._dispatch()

    def do_OPTIONS(self):
        self._error(403, "不允许跨源访问", "cors_denied")

    def do_PUT(self):
        self._error(405, "方法不允许", "method_not_allowed")

    def do_TRACE(self):
        self._error(405, "方法不允许", "method_not_allowed")


def serve_in_thread(state):
    server = AdminServer((state.config.admin_host, state.config.admin_port), state)
    thread = threading.Thread(target=server.serve_forever,
                              kwargs={"poll_interval": 0.5},
                              name="qdp-admin", daemon=True)
    server.thread = thread
    thread.start()
    return server
