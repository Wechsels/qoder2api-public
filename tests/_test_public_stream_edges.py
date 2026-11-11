#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Streaming metering and end-of-stream audit edges for the public gateway."""
import copy
import http.client
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

sys.path.insert(0, ROOT)
import qoder_public_config as C
import qoder_public_gateway as G
import qoder_public_meter as M


declare_suite(__file__, ["qoder_public_gateway.py", "qoder_public_meter.py"])
FIRST_SSE = b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
RESPONSES_SSE = (b'data: {"type":"response.output_text.delta","delta":"abcdefgh"}\n\n'
                 b"data: [DONE]\n\n")


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(size)
        is_disconnect = self.path == "/v1/messages"
        payload = RESPONSES_SSE if self.path == "/v1/responses" else FIRST_SSE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.write(("%x\r\n" % len(payload)).encode() + payload + b"\r\n")
        self.wfile.flush()
        if is_disconnect:
            # 保持流活跃：网关必须还在转发第二块，才能观测到客户端 RST
            time.sleep(0.3)
            self.wfile.write(("%x\r\n" % len(payload)).encode() + payload + b"\r\n")
            self.wfile.flush()
            return
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()


def audit_for(state, path):
    return [row for row in state.usage.rows(days=1) if row.get("path") == path]


def wait_for_row(state, path, count):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        rows = audit_for(state, path)
        if len(rows) >= count:
            return rows
        time.sleep(0.02)
    return audit_for(state, path)


def main():
    temp = tempfile.TemporaryDirectory(prefix="qdp-stream-")
    raw = copy.deepcopy(C.DEFAULTS)
    raw["upstream"]["api_key"] = "internal-test-key"
    raw["logging"].update({"dir": os.path.join(temp.name, "logs"), "level": "error"})
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    raw["upstream"]["url"] = "http://127.0.0.1:%d" % upstream.server_port
    config_path = os.path.join(temp.name, "config.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle)
    config = C.load(config_path, env={"QDP_ADMIN_PASSWORD": "test-admin-password"})
    state = G.build_state(config, G.GatewayLog(config.log_dir, level="error"))
    _record, secret = state.store.create("stream")
    gateway = G.GatewayServer(("127.0.0.1", 0), G.GatewayHandler, state)
    threads = [threading.Thread(target=server.serve_forever, daemon=True)
               for server in (upstream, gateway)]
    for thread in threads:
        thread.start()

    try:
        for path in ("/v1/chat/completions", "/v1/responses"):
            body = b'{"model":"qwen3.8-max","stream":true,"messages":[]}'
            connection = http.client.HTTPConnection("127.0.0.1", gateway.server_port, timeout=5)
            connection.request("POST", path, body=body, headers={
                "Authorization": "Bearer " + secret, "Content-Type": "application/json",
                "X-Forwarded-For": "198.51.100.1"})
            response = connection.getresponse()
            payload = response.read()
            connection.close()
            rows = wait_for_row(state, path, 1)
            check("%s normal SSE has exactly one successful audit" % path,
                  response.status == 200 and len(rows) == 1
                  and rows[0].get("status") == 200 and rows[0].get("outcome") == "ok"
                  and not rows[0].get("error_code") and not rows[0].get("err"),
                  {"status": response.status, "rows": rows})
            if path == "/v1/chat/completions":
                # 修复前首块双计："hello" 被算成 10 字符 -> 3 tokens；单次计量为 5 字符
                check("first complete SSE block is metered only once",
                      len(rows) == 1 and rows[0].get("estimated") is True
                      and rows[0].get("completion_tokens") == M.estimate_tokens(len("hello"))
                      and payload == FIRST_SSE, rows)
            else:
                check("Responses output_text.delta contributes to estimate",
                      len(rows) == 1 and rows[0].get("estimated") is True
                      and rows[0].get("completion_tokens") == M.estimate_tokens(8), rows)

        body = b'{"model":"qwen3.8-max","stream":true,"messages":[]}'
        request = (b"POST /v1/messages HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                   + ("Authorization: Bearer %s\r\n" % secret).encode()
                   + b"X-Forwarded-For: 198.51.100.1\r\n"
                   + ("Content-Length: %d\r\nConnection: close\r\n\r\n" % len(body)).encode() + body)
        with socket.create_connection(("127.0.0.1", gateway.server_port), timeout=5) as connection:
            connection.settimeout(5)
            connection.sendall(request)
            received = b""
            while FIRST_SSE not in received:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                received += chunk
            check("client receives first stream block before disconnect", FIRST_SSE in received, received[:200])
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                  __import__("struct").pack("ii", 1, 0))
        rows = wait_for_row(state, "/v1/messages", 1)
        check("client disconnect has exactly one distinct audit outcome",
              len(rows) == 1 and rows[0].get("outcome") == "client_disconnect"
              and rows[0].get("status") == 499 and rows[0].get("error_code") == "client_disconnect", rows)
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
