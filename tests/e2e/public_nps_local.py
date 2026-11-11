#!/usr/bin/env python3
"""Real, loopback-only nps e2e for qoder2api-public.

This is deliberately separate from tests/run_all.py. It requires user-supplied
nps/npc executables; it never downloads them and never starts a public server.

    python tests/e2e/public_nps_local.py --nps-path /path/nps --npc-path /path/npc
"""

import argparse
import copy
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import faulthandler
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qoder_public_config as public_config  # noqa: E402
import qoder_public_keys as public_keys  # noqa: E402


LOOPBACK = "127.0.0.1"
TEST_HOST = "qoder-e2e.invalid"
# Marker header added via nps HeaderChange. Deliberately NOT x-qdp-*:
# the gateway strips every x-qdp- header (anti-forgery), which is
# exactly the kind of interaction this e2e should document.
TUNNEL_HEADER = "X-Nps-E2E-Marker"
TUNNEL_VALUE = "nps-host-route"


class MissingDependency(Exception):
    pass


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _fmt, *_args):
        return

    def do_POST(self):  # noqa: N802 - stdlib HTTP handler API
        length = int(self.headers.get("Content-Length") or "0")
        request = json.loads(self.rfile.read(length) or b"{}")
        with self.server.lock:
            self.server.seen.append({
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "tunnel": self.headers.get(TUNNEL_HEADER),
            })
        if self.path != "/v1/chat/completions":
            self._json(404, {"error": "not found"})
            return
        if request.get("stream"):
            self._stream()
            return
        self._json(200, {
            "id": "e2e-non-stream",
            "choices": [{"message": {"role": "assistant", "content": "tunnel-ok"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        })

    def _json(self, status, value):
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        chunks = [
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n',
            b"data: [DONE]\n\n",
        ]
        for chunk in chunks:
            self.wfile.write(("%x\r\n" % len(chunk)).encode("ascii") + chunk + b"\r\n")
            self.wfile.flush()
            time.sleep(0.05)
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


class Child:
    """A precisely owned child process, with logs in the isolated workspace."""

    def __init__(self, name, argv, cwd, logs, env):
        self.name = name
        self.argv = [str(part) for part in argv]
        self.cwd = str(cwd)
        self.path = logs / (name + ".log")
        self.handle = None
        self.process = None
        self._stream = None
        # Debugging identifiers are safe; secrets are never written to argv.
        (logs / (name + ".argv.json")).write_text(
            json.dumps(self.argv, indent=2), encoding="utf-8")
        self.handle = self.path.open("a+b")
        kwargs = {
            "cwd": self.cwd,
            "env": env,
            "stdin": subprocess.DEVNULL,
            "stdout": self.handle,
            "stderr": subprocess.STDOUT,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW)
        else:
            kwargs["start_new_session"] = True
        self.process = subprocess.Popen(self.argv, **kwargs)
        (logs / (name + ".pid")).write_text(str(self.process.pid), encoding="ascii")
        print("START %-8s pid=%d log=%s" % (name, self.process.pid, self.path))

    @property
    def pid(self):
        return self.process.pid if self.process else None

    def alive(self):
        return self.process is not None and self.process.poll() is None

    def require_alive(self):
        if not self.alive():
            raise AssertionError("%s exited early (rc=%s); see %s" % (
                self.name, self.process.poll(), self.path))

    def stop(self, timeout=8):
        if not self.process or self.process.poll() is not None:
            self.close()
            return
        pid = self.process.pid
        print("STOP  %-8s pid=%d" % (self.name, pid))
        if os.name == "nt":
            try:
                self.process.send_signal(signal.CTRL_BREAK_EVENT)
                self.process.wait(timeout=2)
                self.close()
                return
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:
            if os.name == "nt":
                # This exact PID, not an image-name kill. The tree is ours.
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=12, check=False)
            else:
                os.killpg(pid, signal.SIGTERM)
            self.process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=12, check=False)
                else:
                    os.killpg(pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.process.poll() is None:
            print("WARNING: unable to reap owned PID %d" % pid, file=sys.stderr)
        self.close()

    def close(self):
        if self.handle:
            self.handle.close()
            self.handle = None
        self._stream = None


class E2E:
    def __init__(self, args):
        self.args = args
        self.temp = None
        self.work = None
        self.children = []
        self.upstream = None
        self.upstream_thread = None
        self.secret = None
        self.record = None
        self.npc = None
        self.checks = 0
        self.failures = []
        self.host = args.test_host
        self.fake_client_ip = "198.51.100.7"

    def check(self, name, condition, detail=""):
        self.checks += 1
        if condition:
            print("PASS %s" % name)
        else:
            self.failures.append(name)
            print("FAIL %s | %s" % (name, detail))

    def request(self, port, path="/v1/chat/completions", key=None,
                stream=False, method="POST", host=None, client_ip=True):
        payload = json.dumps({
            "model": "qwen3.8-max",
            "messages": [{"role": "user", "content": "e2e"}],
            "stream": stream,
        }).encode("utf-8")
        headers = {"Host": host or self.host, "Content-Type": "application/json"}
        if client_ip:
            # In production Caddy overwrites X-Forwarded-For with the real
            # client IP before nps; this e2e has no Caddy, so the entry client
            # plays that role. Without any XFF the gateway must fail closed.
            headers["X-Forwarded-For"] = self.fake_client_ip
        if key is not None:
            headers["Authorization"] = "Bearer " + key
        conn = http.client.HTTPConnection(LOOPBACK, port, timeout=12)
        try:
            conn.request(method, path, body=payload if method == "POST" else None,
                         headers=headers)
            response = conn.getresponse()
            first = response.readline() if stream else b""
            body = first + response.read()
            return response.status, dict(response.getheaders()), body, first
        except (OSError, http.client.HTTPException) as exc:
            # A dead tunnel resets connections; report "no status" rather
            # than leaking the reset as a harness failure.
            return 0, {}, repr(exc).encode("utf-8"), b""
        finally:
            conn.close()

    def wait_request(self, port, predicate, label, timeout=25):
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            try:
                result = self.request(port, "/ping", method="GET")
                if predicate(result):
                    return result
                last = result
            except (OSError, http.client.HTTPException) as exc:
                last = repr(exc)
            for child in self.children:
                if child.name != "npc":
                    child.require_alive()
            time.sleep(0.2)
        raise AssertionError("timeout waiting for %s; last=%r" % (label, last))

    def start(self, name, argv, cwd=None):
        env = dict(os.environ)
        for key in list(env):
            if key.lower() in ("http_proxy", "https_proxy", "all_proxy", "no_proxy"):
                env.pop(key)
        env.update({"QDP_E2E": "1", "NO_PROXY": LOOPBACK, "no_proxy": LOOPBACK})
        child = Child(name, argv, cwd or self.work, self.work / "logs", env)
        self.children.append(child)
        return child

    def prepare_gateway(self):
        data = self.work / "data"
        data.mkdir()
        raw = copy.deepcopy(public_config.DEFAULTS)
        raw["server"].update({"host": LOOPBACK, "port": self.gateway_port})
        raw["admin"].update({"host": LOOPBACK, "port": self.admin_port})
        raw["upstream"].update({
            "url": "http://%s:%d" % (LOOPBACK, self.upstream.server_port),
            "api_key": "qdp-internal-e2e",
            "panel_password": "e2e-panel-not-a-real-secret",
        })
        raw["admin"]["password_hash"] = public_config.hash_password("e2e-admin")
        raw["defaults"].update({"rpm": 0, "max_concurrent": 8})
        raw["metering"]["estimate_when_missing"] = False
        raw["logging"]["level"] = "debug"
        public_config.write_config(str(data / "config.json"), raw)
        store = public_keys.KeyStore(
            str(data / "keys.json"), defaults=raw["defaults"])
        self.record, self.secret = store.create("e2e-user", rpm=0)
        self.key_path = data / "keys.json"
        # admin (8792-equivalent, random port) is needed for the
        # disable/enable key round-trip later in the suite.
        self.start("gateway", [
            sys.executable, str(ROOT / "qoder_public_gateway.py"),
            "--config", str(data / "config.json"),
        ])
        self.wait_request(self.gateway_port, lambda result: result[0] == 200, "gateway")

    def prepare_nps(self):
        runtime = self.work / "nps-runtime"
        runtime.mkdir()
        self.nps_binary = copy_binary(self.args.nps_path, runtime, "nps")
        self.npc_path = copy_binary(self.args.npc_path, self.work / "npc-runtime", "npc")
        conf = runtime / "conf"
        conf.mkdir()
        # nps 0.26.10 loads its JSON DB beside the executable. These are
        # private one-line records, separated by its literal *#* delimiter.
        client = {
            "Cnf": {"U": "", "P": "", "Compress": False, "Crypt": True},
            "Id": 1, "VerifyKey": "e2e-private-vkey", "Status": True,
            "NoStore": False, "NoDisplay": False,
            # nps 0.26.10 dereferences Rate/Flow during forwarding;
            # a minimal record makes it panic on the first request.
            "IsConnect": True, "Addr": LOOPBACK,
            "Rate": {"Limit": 100000, "Curve": 100000},
            "RateLimit": 0,
            "Flow": {"FlowLimit": 0, "ExportFlow": 0, "InletFlow": 0},
            "MaxConn": 0, "NowConn": 0, "Version": "",
            "ConfigConnAllow": True, "MaxTunnelNum": 0,
            "WebUserName": "", "WebPassword": "",
        }
        # The client must pre-exist (private vkey auth). The host record is
        # also pre-created with HeaderChange: in host mode npc is a raw pipe,
        # nps owns routing and header rewriting. npc.conf therefore carries
        # NO host task -- registering it would be rejected as "occupied".
        (conf / "clients.json").write_text(json.dumps(client) + "\n*#*",
                                          encoding="utf-8")
        host = {
            "Id": 1, "Host": self.host, "HeaderChange": "%s:%s" % (
                TUNNEL_HEADER, TUNNEL_VALUE), "HostChange": "",
            "Location": "/", "Remark": "isolated-e2e", "Scheme": "http",
            "CertFilePath": "", "KeyFilePath": "", "NoStore": False,
            "IsClose": False,
            "Flow": {"FlowLimit": 0, "ExportFlow": 0, "InletFlow": 0},
            "Client": {"Id": 1},
            "Target": {"TargetStr": "%s:%d" % (LOOPBACK, self.gateway_port),
                       "LocalProxy": False},
        }
        # Include the field separator. A single JSON line without it makes
        # nps 0.26.10 append a second, empty record and fail to decode.
        (conf / "hosts.json").write_text(json.dumps(host) + "\n*#*",
                                          encoding="utf-8")
        (conf / "tasks.json").write_text("", encoding="utf-8")
        # npc only authenticates and pipes; no host task here (see above).
        (self.work / "npc.conf").write_text(
            "[common]\nserver_addr=%s:%d\nconn_type=tcp\nvkey=e2e-private-vkey\n"
            "crypt=true\ncompress=false\nauto_reconnection=true\ndisconnect_timeout=5\n"
            % (LOOPBACK, self.bridge_port), encoding="utf-8")
        (conf / "nps.conf").write_text(
            "appname=nps\nrunmode=dev\nhttp_proxy_ip=%s\nhttp_proxy_port=%d\n"
            "https_proxy_port=\nhttps_just_proxy=false\nbridge_type=tcp\n"
            "bridge_ip=%s\nbridge_port=%d\ndisconnect_timeout=5\n"
            "web_ip=%s\nweb_port=%d\nweb_host=%s\nweb_username=e2e-admin\n"
            "web_password=e2e-private-panel\nweb_open_ssl=false\n"
            "auth_crypt_key=1234567812345678\nlog_level=7\nlog_path=%s\n"
            "public_vkey=e2e-private-vkey\nallow_local_proxy=false\n"
            % (LOOPBACK, self.nps_port, LOOPBACK, self.bridge_port,
               LOOPBACK, self.web_port, self.host, self.nps_log), encoding="utf-8")
        # Minimal web assets avoid Beego global template failures if its
        # management router is initialized even with a loopback-only port.
        web = runtime / "web" / "views"
        web.mkdir(parents=True)
        for name in ("index.html", "login.html", "error.html"):
            (web / name).write_text("<!doctype html><title>e2e</title>", encoding="utf-8")
        for name in ("layout.html",):
            (web / "public").mkdir(exist_ok=True)
            (web / "public" / name).write_text("{{.LayoutContent}}", encoding="utf-8")

    def start_npc(self, name="npc"):
        # Only flags npc 0.26.10 knows: -config / -log_level. Anything else
        # (e.g. -debug, -pprof_addr) makes npc print usage and exit.
        child = self.start(name, [
            self.npc_path, "-config=" + str(self.work / "npc.conf"),
            "-log_level=7",
        ], cwd=self.npc_path.parent)
        return child

    def start_caddy(self):
        caddy = copy_binary(self.args.caddy_path, self.work / "caddy-runtime", "caddy")
        config = self.work / "Caddyfile"
        config.write_text(
            "{\n  auto_https off\n  admin off\n}\n"
            "http://%s:%d {\n  reverse_proxy http://%s:%d\n}\n"
            % (self.host, self.caddy_port, LOOPBACK, self.nps_port), encoding="utf-8")
        child = self.start("caddy", [
            caddy, "run", "--config", str(config), "--adapter", "caddyfile",
        ], cwd=config.parent)
        return child

    def run(self):
        self.prepare_gateway()
        self.prepare_nps()
        self.start("nps", [self.nps_binary], cwd=self.nps_binary.parent)
        # An HTTP listener is not sufficient: wait for the npc bridge.
        self.wait_tcp(self.bridge_port, "nps bridge")
        self.wait_request(self.nps_port, lambda result: result[0] in (404, 502), "nps HTTP")
        self.npc = self.start_npc()
        self.wait_request(self.nps_port, lambda result: result[0] == 200, "npc route")
        self.entry_port = self.nps_port
        if self.args.caddy_path:
            self.start_caddy()
            self.entry_port = self.caddy_port
            self.wait_request(self.entry_port, lambda result: result[0] == 200, "Caddy route")

        seen_before = len(self.upstream.seen)
        status, _, _, _ = self.request(self.entry_port)
        self.check("missing API key -> 401", status == 401, status)
        self.check("unauthenticated request did not reach upstream",
                   len(self.upstream.seen) == seen_before)

        status, _, body, _ = self.request(self.entry_port, key=self.secret)
        self.check("valid API key -> 200", status == 200, (status, body[:200]))
        self.check("non-stream response", b"tunnel-ok" in body, body[:200])
        self.check("request traversed nps + npc + gateway",
                   len(self.upstream.seen) > seen_before)
        seen = self.upstream.seen[-1]
        self.check("upstream receives only internal key",
                   seen.get("authorization") == "Bearer qdp-internal-e2e", seen)
        self.check("npc tunnel marker reached upstream",
                   seen.get("tunnel") == TUNNEL_VALUE, seen)

        status, headers, body, first = self.request(self.entry_port, key=self.secret, stream=True)
        self.check("SSE through tunnel -> 200", status == 200, (status, body[:200]))
        self.check("SSE complete and not truncated",
                   b"hello" in body and b"data: [DONE]" in body, body[:500])
        self.check("SSE first event is forwarded before the stream ends",
                   b"hello" in first, first[:200])
        self.check("SSE content type", "text/event-stream" in headers.get("Content-Type", ""),
                   headers)

        self.admin_key_action("disable")
        status, _, _, _ = self.request(self.entry_port, key=self.secret)
        self.check("disabled API key -> 401", status == 401, status)
        self.admin_key_action("enable")
        status, _, _, _ = self.request(self.entry_port, key=self.secret)
        self.check("re-enabled API key -> 200", status == 200, status)

        before_restart = len(self.upstream.seen)
        self.npc.stop()
        self.children.remove(self.npc)
        self.wait_request(self.entry_port, lambda result: result[0] != 200,
                          "route failure after npc stop", timeout=20)
        status, _, _, _ = self.request(self.entry_port, key=self.secret)
        self.check("npc stopped -> request fails", status != 200, status)
        self.check("failed tunnel did not reach upstream",
                   len(self.upstream.seen) == before_restart)

        self.npc = self.start_npc("npc-restarted")
        self.wait_request(self.entry_port, lambda result: result[0] == 200,
                          "npc restart recovery", timeout=35)
        status, _, body, _ = self.request(self.entry_port, key=self.secret)
        self.check("npc restarted -> valid key succeeds", status == 200,
                   (status, body[:200]))
        self.check("recovery uses real tunnel",
                   len(self.upstream.seen) > before_restart
                   and self.upstream.seen[-1]["tunnel"] == TUNNEL_VALUE)

    def wait_tcp(self, port, label, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket() as sock:
                sock.settimeout(0.5)
                if sock.connect_ex((LOOPBACK, port)) == 0:
                    return
            time.sleep(0.1)
        raise AssertionError("timeout waiting for " + label)

    def admin_key_action(self, action):
        host = "%s:%d" % (LOOPBACK, self.admin_port)
        origin = "http://" + host
        common = {"Origin": origin, "X-Requested-With": "qdp-admin"}
        login, cookie = self.local_json(host, "/api/login", {"password": "e2e-admin"},
                                         common)
        headers = dict(common, **{"X-QDP-CSRF": login["csrf"],
                                  "Cookie": cookie.split(";")[0]})
        self.local_json(host, "/api/keys/%s/%s" % (self.record["id"], action),
                        {}, headers)

    def local_json(self, host, path, payload, headers):
        conn = http.client.HTTPConnection(LOOPBACK, self.admin_port, timeout=10)
        try:
            conn.request("POST", path, body=json.dumps(payload).encode("utf-8"),
                         headers=dict(headers, **{"Host": host,
                                                 "Content-Type": "application/json"}))
            response = conn.getresponse()
            data = json.loads(response.read() or b"{}")
            if response.status != 200:
                raise AssertionError("admin %s returned %d: %r" % (path, response.status, data))
            return data, response.getheader("Set-Cookie") or ""
        finally:
            conn.close()

    def close(self):
        for child in reversed(self.children):
            child.stop()
        self.children = []
        if self.upstream:
            self.upstream.shutdown()
            self.upstream.server_close()
        if self.upstream_thread:
            self.upstream_thread.join(timeout=5)
        if self.temp:
            if self.args.keep_temp:
                print("KEPT isolated workspace: %s" % self.work)
            else:
                self.temp.cleanup()


def copy_binary(value, directory, stem):
    source = Path(value).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (stem + source.suffix)
    shutil.copy2(str(source), str(target))
    if os.name != "nt":
        target.chmod(target.stat().st_mode | 0o111)
    return target


def free_ports(count):
    sockets = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind((LOOPBACK, 0))
            sockets.append(sock)
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def binary_argument(args, option, env_name, executable):
    value = getattr(args, option) or os.environ.get(env_name)
    if not value:
        raise MissingDependency(
            "missing --%s or %s; provide an executable (exit 2, not a pass)" % (
                option.replace("_", "-"), env_name))
    path = Path(value).expanduser()
    if not path.is_file() or not os.access(str(path), os.X_OK):
        raise MissingDependency("%s is not an executable file: %s" % (executable, path))
    return str(path)


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nps-path", help="nps executable; or QDP_E2E_NPS_PATH")
    parser.add_argument("--npc-path", help="npc executable; or QDP_E2E_NPC_PATH")
    parser.add_argument("--caddy-path", help="optional Caddy executable; or QDP_E2E_CADDY_PATH")
    parser.add_argument("--timeout", type=float, default=180,
                        help="total e2e deadline in seconds (default: 180)")
    parser.add_argument("--keep-temp", action="store_true",
                        help="keep the isolated temp directory for diagnosis")
    parser.add_argument("--work-dir",
                        help="explicit isolated workspace (must be empty or nonexistent)")
    parser.add_argument("--test-host", default=TEST_HOST,
                        help="Host header for nps; never requires hosts/DNS changes")
    parser.add_argument("--check-config", action="store_true",
                        help="only validate supplied binaries and isolation preconditions")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        args.nps_path = binary_argument(args, "nps_path", "QDP_E2E_NPS_PATH", "nps")
        args.npc_path = binary_argument(args, "npc_path", "QDP_E2E_NPC_PATH", "npc")
        if args.caddy_path or os.environ.get("QDP_E2E_CADDY_PATH"):
            args.caddy_path = binary_argument(
                args, "caddy_path", "QDP_E2E_CADDY_PATH", "caddy")
        # nps 0.26.10 prefers an installed configuration over its own dir.
        # Refuse rather than ever touching a real nps installation.
        installed = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nps" \
            if os.name == "nt" else Path("/etc/nps")
        if installed.exists():
            raise MissingDependency(
                "nps install directory exists (%s); refusing to risk using real config. "
                "Use an isolated VM/container for this nps 0.26.10 test." % installed)
        if Path.cwd().resolve() == (ROOT / "public-data").resolve():
            raise MissingDependency("refusing to run from the real public-data directory")
        print("DEPENDENCIES OK; no downloads or public listeners requested")
    except MissingDependency as exc:
        print("E2E ERROR: %s" % exc, file=sys.stderr)
        return 2
    if args.check_config:
        return 0

    suite = E2E(args)
    def expire():
        faulthandler.dump_traceback()
        for child in reversed(suite.children):
            child.stop()
        os._exit(124)

    timer = threading.Timer(args.timeout, expire)
    timer.daemon = True
    timer.start()
    try:
        if args.work_dir:
            work = Path(args.work_dir).expanduser().resolve()
            if work.exists():
                if not work.is_dir() or any(work.iterdir()):
                    raise AssertionError("--work-dir must be an empty directory: %s" % work)
            else:
                work.mkdir(parents=True)
            (work / "logs").mkdir()
            suite.work = work
            suite.temp = None          # caller owns cleanup
            print("EXPLICIT isolated workspace: %s (never auto-cleaned)" % work)
        else:
            suite.temp = tempfile.TemporaryDirectory(prefix="qoder-public-nps-e2e-")
            suite.work = Path(suite.temp.name).resolve()
            (suite.work / "logs").mkdir()
        (suite.gateway_port, suite.admin_port, suite.nps_port, suite.bridge_port,
         suite.web_port, suite.caddy_port) = free_ports(6)
        suite.nps_log = str(suite.work / "logs" / "nps-internal.log")
        suite.upstream = ThreadingHTTPServer((LOOPBACK, 0), FakeUpstreamHandler)
        suite.upstream.daemon_threads = True
        suite.upstream.seen = []
        suite.upstream.lock = threading.Lock()
        suite.upstream_thread = threading.Thread(target=suite.upstream.serve_forever, daemon=True)
        suite.upstream_thread.start()
        suite.run()
    except Exception as exc:
        suite.failures.append("harness: %s" % exc)
        print("E2E ERROR: %s" % exc, file=sys.stderr)
        if os.environ.get("QDP_E2E_TRACEBACK") == "1":
            traceback.print_exc()
        if suite.work:
            print("Logs: %s" % (suite.work / "logs"), file=sys.stderr)
    finally:
        timer.cancel()
        suite.close()
    print("SUMMARY: TOTAL %d checks, %d failed, %d harness failures" % (
        suite.checks, len(suite.failures),
        sum(name.startswith("harness: ") for name in suite.failures)))
    if suite.failures:
        print("RESULT: RED (exit 1)")
        return 1
    print("RESULT: GREEN; loopback tunnel only, no VPS/public-network validation")
    return 0


if __name__ == "__main__":
    sys.exit(main())
