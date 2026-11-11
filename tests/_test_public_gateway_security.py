#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Security boundaries of the public gateway, using a real loopback server."""
import copy
import http.client
import os
import socket
import sys
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

sys.path.insert(0, ROOT)
import qoder_public_config as C
import qoder_public_gateway as G


declare_suite(__file__, ["qoder_public_gateway.py", "qoder_public_config.py"])
SEEN = []


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(size)
        SEEN.append({"path": self.path, "body": body,
                     "headers": {key.lower(): value for key, value in self.headers.items()}})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")


def request(port, path="/v1/chat/completions", secret=None, headers=None,
            body=b"{}", method="POST", caddy_ip=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = dict(headers or {})
    if caddy_ip is not None:
        headers["X-Forwarded-For"] = caddy_ip
    if secret is not None:
        headers["Authorization"] = "Bearer " + secret
    try:
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    except (http.client.HTTPException, OSError) as exc:
        return 0, {}, repr(exc).encode("utf-8")
    finally:
        connection.close()


def raw_request(port, headers, payload):
    lines = ["POST /v1/chat/completions HTTP/1.1", "Host: 127.0.0.1",
             "Connection: close"] + list(headers) + ["", ""]
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        connection.settimeout(5)
        connection.sendall("\r\n".join(lines).encode("ascii") + payload)
        chunks = []
        while True:
            try:
                chunk = connection.recv(65536)
            except (ConnectionResetError, socket.timeout):
                break
            if not chunk:
                break
            chunks.append(chunk)
    response = b"".join(chunks)
    try:
        return int(response.split(b" ", 2)[1]), response
    except (IndexError, ValueError):
        return 0, response


def rows_matching(state, **fields):
    time.sleep(0.05)
    rows = state.usage.rows(days=1)
    return [row for row in rows if all(row.get(key) == value for key, value in fields.items())]


@contextmanager
def untrusted_gateway(temp, upstream_port):
    raw = copy.deepcopy(C.DEFAULTS)
    raw["upstream"].update({"url": "http://127.0.0.1:%d" % upstream_port,
                              "api_key": "internal-test-key"})
    raw["logging"].update({"dir": os.path.join(temp, "untrusted-logs"), "level": "error"})
    raw["server"]["trusted_proxies"] = ["192.0.2.1"]
    path = os.path.join(temp, "untrusted-config.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle)
    config = C.load(path, env={"QDP_ADMIN_PASSWORD": "test-admin-password"})
    state = G.build_state(config, G.GatewayLog(config.log_dir, level="error"))
    record, secret = state.store.create("untrusted", allowed_ips=["203.0.113.9"])
    _open_record, open_secret = state.store.create("untrusted-open")
    gateway = G.GatewayServer(("127.0.0.1", 0), G.GatewayHandler, state)
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        yield gateway.server_port, {"open": open_secret, "restricted": secret}
    finally:
        gateway.shutdown()
        gateway.server_close()
        state.store.stop_autosave()
        thread.join(timeout=3)


def main():
    temp = tempfile.TemporaryDirectory(prefix="qdp-security-")
    raw = copy.deepcopy(C.DEFAULTS)
    raw["upstream"].update({"api_key": "internal-test-key", "max_body_bytes": 2048})
    raw["logging"].update({"dir": os.path.join(temp.name, "logs"), "level": "error"})
    raw["endpoints"].update({"auth_fail_limit": 4, "auth_fail_window": 60})
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    raw["upstream"]["url"] = "http://127.0.0.1:%d" % upstream.server_port
    config_path = os.path.join(temp.name, "config.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle)
    config = C.load(config_path, env={"QDP_ADMIN_PASSWORD": "test-admin-password"})
    log = G.GatewayLog(config.log_dir, level="error")
    state = G.build_state(config, log)
    _record, secret = state.store.create("security")
    _bound, bound_secret = state.store.create("bound", allowed_ips=["203.0.113.8"])
    _realm, realm_secret = state.store.create("realm", realm="cn")
    _denied, denied_secret = state.store.create("denied", allowed_ips=["198.51.100.9"])
    gateway = G.GatewayServer(("127.0.0.1", 0), G.GatewayHandler, state)
    threads = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in (upstream, gateway)]
    for thread in threads:
        thread.start()
    port = gateway.server_port
    auth = {"Authorization": "Bearer " + secret}
    body = b'{"model":"qwen3.8-max","messages":[]}'

    try:
        with untrusted_gateway(temp.name, upstream.server_port) as (untrusted_port, secrets):
            before = len(SEEN)
            status, _, _ = request(untrusted_port, secret=secrets["open"],
                                  headers={"X-Forwarded-For": "203.0.113.8"}, body=body)
            forwarded = SEEN[-1]["headers"].get("x-forwarded-for", "") if len(SEEN) > before else ""
            check("untrusted XFF cannot spoof client IP",
                  status == 200 and forwarded.split(",")[0].strip() == "127.0.0.1",
                  {"status": status, "forwarded": forwarded})
            status, _, _ = request(untrusted_port, secret=secrets["restricted"],
                                  headers={"X-Forwarded-For": "203.0.113.9"}, body=body)
            check("untrusted XFF cannot bypass IP allow-list", status == 403, status)

        status, _, _ = request(port, secret=denied_secret,
                              headers={"X-Forwarded-For": "198.51.100.9",
                                       "X-Real-IP": "198.51.100.9"}, body=body)
        check("trusted nps client IP honors IP allow-list", status == 200, status)
        status, _, _ = request(port, secret=bound_secret,
                              headers={"X-Forwarded-For": "203.0.113.8, 127.0.0.1"}, body=body)
        check("Caddy client IP reaches upstream from XFF",
              status == 200 and SEEN[-1]["headers"].get("x-forwarded-for", "").split(",")[0].strip() == "203.0.113.8",
              SEEN[-1] if SEEN else status)
        for path in ("/v1/models/refresh", "/settings"):
            before = len(SEEN)
            status, _, _ = request(port, path, secret=secret, method="GET", body=None)
            check("%s is rejected before upstream" % path,
                  status == 404 and len(SEEN) == before, status)

        before = len(SEEN)
        status, _, _ = request(port, headers={"Authorization": "Bearer " + secret,
                                              "X-API-Key": "different-secret"}, body=body)
        check("conflicting authentication headers are rejected",
              status == 401 and len(SEEN) == before, status)

        before = len(SEEN)
        status, _, _ = request(port, "/v1/chat/completions?realm=intl", secret=secret,
                               headers={"X-Realm": "intl"}, body=body,
                               caddy_ip="198.51.100.1")
        sent = SEEN[-1] if len(SEEN) > before else {}
        check("default removes client realm header and query",
              status == 200 and sent and "realm=" not in sent.get("path", "")
              and sent.get("headers", {}).get("x-realm") in (None, ""), sent)
        before = len(SEEN)
        status, _, _ = request(port, "/v1/chat/completions?realm=intl", secret=realm_secret,
                              headers={"X-Realm": "intl"},
                              body=b'{"model":"qwen3.8-max","messages":[]}', caddy_ip="198.51.100.1")
        check("bound realm cannot be overridden",
              status == 403 and len(SEEN) == before, status)
        before = len(SEEN)
        status, _, _ = request(port, "/v1/chat/completions?realm=cn", secret=realm_secret,
                              headers={"X-Realm": "cn"},
                              body=b'{"model":"qwen3.8-max","messages":[]}', caddy_ip="198.51.100.1")
        sent = SEEN[-1] if len(SEEN) > before else {}
        check("same realm is forwarded for bound key",
              status == 200 and sent.get("headers", {}).get("x-realm") == "cn"
              and "realm=" not in sent.get("path", ""), {"status": status, "sent": sent})
        config.defaults["allow_client_realm"] = True
        status, _, _ = request(port, secret=realm_secret,
                              headers={"X-Realm": "intl"},
                              body=b'{"model":"qwen3.8-max","messages":[]}', caddy_ip="198.51.100.1")
        check("bound realm stays fixed when client realm is allowed",
              status == 403, status)
        status, _, _ = request(port, secret=realm_secret,
                              headers={"X-Realm": "cn"},
                              body=b'{"model":"qwen3.8-max","messages":[]}', caddy_ip="198.51.100.1")
        check("same realm header accepted when client realm is allowed",
              status == 200 and SEEN[-1]["headers"].get("x-realm") == "cn", status)
        status, _, _ = request(port, "/v1/chat/completions?realm=intl", secret=secret,
                              body=body, caddy_ip="198.51.100.1")
        check("explicitly allowed realm query is forwarded",
              status == 200 and "realm=intl" in SEEN[-1]["path"], SEEN[-1])
        config.defaults["allow_client_realm"] = False

        cases = [
            ("Content-Length over limit", ["Content-Length: 2049"], b"x" * 2049, (413,)),
            ("chunked body over limit", ["Transfer-Encoding: chunked"],
             ("%x\r\n" % 2049).encode() + b"x" * 2049 + b"\r\n0\r\n\r\n", (413,)),
            ("TE and CL ambiguity", ["Transfer-Encoding: chunked", "Content-Length: 2"],
             b"2\r\n{}\r\n0\r\n\r\n", (400,)),
            ("malformed chunk", ["Transfer-Encoding: chunked"], b"Z\r\n{}\r\n0\r\n\r\n", (400,)),
            ("chunk missing CRLF", ["Transfer-Encoding: chunked"], b"2\r\n{}XX0\r\n\r\n", (400,)),
        ]
        for label, extra_headers, payload, expected in cases:
            before = len(SEEN)
            status, response = raw_request(
                port, ["Authorization: " + auth["Authorization"],
                       "X-Forwarded-For: 198.51.100.1"] + extra_headers, payload)
            check(label + " is rejected without forwarding",
                  status in expected and len(SEEN) == before, {"status": status, "response": response[:160]})

        before_seen = len(SEEN)
        before_rows = len(state.usage.rows(days=1))
        status, response_headers, _ = request(port, secret="invalid-secret",
                                               body=body, caddy_ip="198.51.100.1")
        request_id = response_headers.get("x-qdp-request-id")
        matching = rows_matching(state, path="/v1/chat/completions", status=401,
                                 outcome="rejected", request_id=request_id)
        check("authentication failure is audited exactly once",
              status == 401 and len(state.usage.rows(days=1)) == before_rows + 1
              and len(matching) == 1 and matching[0].get("request_id")
              and len(matching[0].get("error_code", "")) > 0
              and len(SEEN) == before_seen, matching)
        counts = []
        for _ in range(3):
            request(port, secret="invalid-secret", body=body, caddy_ip="198.51.100.1")
            with state.auth_failures._lock:
                counts.append(len(state.auth_failures._events.get("198.51.100.1", ())))
        check("each failed authentication increments throttle once", counts == [2, 3, 4], counts)
        before = len(rows_matching(state, path="/settings"))
        status, _, _ = request(port, "/settings", secret=secret, method="GET", body=None)
        matching = rows_matching(state, path="/settings", status=404, outcome="rejected")
        check("early route rejection has one audit event",
              status == 404 and len(matching) == before + 1, matching)
    finally:
        gateway.shutdown()
        upstream.shutdown()
        gateway.server_close()
        upstream.server_close()
        state.store.stop_autosave()
        for thread in threads:
            thread.join(timeout=3)
        temp.cleanup()
    finish()


if __name__ == "__main__":
    main()
