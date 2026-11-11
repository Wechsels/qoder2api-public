#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qoder2api-public 数据面黑盒测试（假上游 + 真网关，全本机回环）。

    python tests/_test_public_gateway.py

不需要 qoder2api，也不需要真实上游账号：脚本内置一个假上游，验证
鉴权、端点白名单、模型白名单、RPM/并发/配额、SSE 逐块转发与计量、
错误体形状、健康检查、上游错误透传。
"""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import qoder_public_config as C
import qoder_public_gateway as G

SEEN = []

class FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/v1/models"):
            SEEN.append({"path": self.path, "auth": self.headers.get("Authorization"),
                         "method": "GET"})
            body = json.dumps({"data": [{"id": "qwen3.8-max"}, {"id": "glm-5"},
                                        {"id": "deepseek-v4"}]}).encode()
            self.send_response(200); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        self.send_response(404); self.send_header("Content-Length","0"); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        payload = json.loads(raw or b"{}")
        SEEN.append({"path": self.path, "auth": self.headers.get("Authorization"),
                     "keyid": self.headers.get("X-QDP-Key-Id"),
                     "stream_options": payload.get("stream_options"),
                     "model": payload.get("model")})
        if payload.get("model") == "boom":
            body = json.dumps({"error": {"message": "upstream exploded"}}).encode()
            self.send_response(500); self.send_header("Content-Type","application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.send_header("Cache-Control","no-cache")
            self.send_header("Transfer-Encoding","chunked")
            self.end_headers()
            def chunk(obj):
                data = ("data: " + json.dumps(obj) + "\n\n").encode()
                self.wfile.write(("%x\r\n" % len(data)).encode() + data + b"\r\n"); self.wfile.flush()
            chunk({"choices":[{"delta":{"content":"he"}}]})
            time.sleep(0.05)
            chunk({"choices":[{"delta":{"content":"llo"}}]})
            chunk({"choices":[],"usage":{"prompt_tokens":17,"completion_tokens":9,"total_tokens":26}})
            self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
            return
        body = json.dumps({"choices":[{"message":{"content":"hi"}}],
                           "usage":{"prompt_tokens":11,"completion_tokens":4,"total_tokens":15}}).encode()
        self.send_response(200); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

tmp = tempfile.mkdtemp()
data = os.path.join(tmp, "public-data"); os.makedirs(data, exist_ok=True)
raw = json.loads(json.dumps(C.DEFAULTS))
raw["server"]["port"] = 18791          # 仅占位，真正监听用 0（临时端口）
raw["admin"]["port"] = 18792          # 本测试不启管理面
raw["upstream"]["url"] = "http://127.0.0.1:18790"   # 启动假上游后改写
raw["upstream"]["api_key"] = "qdp-internal-SECRET"
raw["admin"]["password_hash"] = C.hash_password("adminpw")
cfg_path = os.path.join(data, "config.json")
C.write_config(cfg_path, raw)
cfg = C.load(cfg_path)
log = G.GatewayLog(os.path.join(tmp, "logs"))
state = G.build_state(cfg, log)

store = state.store
rec, secret = store.create("alice", rpm=3, allowed_models=["qwen*"])
rec_tiny, secret_tiny = store.create("tiny", daily_tokens=5)
rec_glm, secret_glm = store.create("glm", max_concurrent=1)

up = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstream); up.daemon_threads = True
threading.Thread(target=up.serve_forever, daemon=True).start()
cfg.raw["upstream"]["url"] = "http://127.0.0.1:%d" % up.server_address[1]
cfg.raw["upstream"]["api_key"] = "qdp-internal-SECRET"
srv = G.GatewayServer(("127.0.0.1", 0), G.GatewayHandler, state)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(0.3)
BASE = "http://127.0.0.1:%d" % srv.server_address[1]

def call(path, key=None, body=None, method=None, query=""):
    url = BASE + path + query
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("X-Forwarded-For", "198.51.100.1")
    req.add_header("X-Real-IP", "198.51.100.1")
    if key: req.add_header("Authorization", "Bearer " + key)
    if data: req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()

fails = []
passes = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ((" | " + str(extra)) if extra and not cond else ""))
    if not cond: fails.append(name)
    else: passes.append(name)

s, h, b = call("/v1/models")
check("no key -> 401", s == 401, s)
check("401 WWW-Authenticate", h.get("WWW-Authenticate","").startswith("Bearer"), h.get("WWW-Authenticate"))
check("401 openai shape", "invalid_api_key" in b.decode(), b[:200])
s, h, b = call("/v1/models", query="?key=" + secret)
check("?key= rejected", s == 401 and "query parameter" in b.decode(), (s, b[:120]))
s, h, b = call("/v1/models", key="qdp-wrong")
check("wrong key -> 401", s == 401, s)
s, h, b = call("/v1/models", key=secret)
check("models 200 + filtered", s == 200 and [m["id"] for m in json.loads(b)["data"]] == ["qwen3.8-max"], b[:200])
check("internal key forwarded", SEEN and SEEN[-1]["auth"] == "Bearer qdp-internal-SECRET", SEEN[-1:])
check("client key not leaked upstream", all(x["auth"] != "Bearer " + secret for x in SEEN))

s, h, b = call("/v1/chat/completions", key=secret, body={"model":"qwen3.8-max","messages":[{"role":"user","content":"hi"}]})
check("chat 200", s == 200, (s, b[:200]))
time.sleep(0.3)
rows = [r for r in state.usage.rows(days=1) if r["path"] == "/v1/chat/completions"]
check("usage recorded", rows and rows[0]["prompt_tokens"] == 11 and rows[0]["completion_tokens"] == 4, rows[:1])

s, h, b = call("/v1/chat/completions", key=secret_glm, body={"model":"glm-5","messages":[]})
check("non-whitelisted key can use glm", s == 200, s)
s, h, b = call("/v1/chat/completions", key=secret, body={"model":"glm-5","messages":[]})
check("model not allowed -> 403", s == 403 and "not allowed" in b.decode(), (s, b[:150]))

req = urllib.request.Request(BASE + "/v1/chat/completions",
    data=json.dumps({"model":"qwen3.8-max","stream":True,"messages":[]}).encode(),
    method="POST")
req.add_header("Authorization", "Bearer " + secret_tiny)
req.add_header("Content-Type", "application/json")
req.add_header("X-Forwarded-For", "198.51.100.1")
req.add_header("X-Real-IP", "198.51.100.1")
with urllib.request.urlopen(req, timeout=10) as resp:
    chunks = []
    while True:
        piece = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
        if not piece: break
        chunks.append(piece)
stream_body = b"".join(chunks)
check("stream relayed", b"hello" in stream_body.replace(b"\r\n", b"") or b"he" in stream_body, stream_body[:200])
check("include_usage injected", SEEN[-1]["stream_options"] == {"include_usage": True}, SEEN[-1])
time.sleep(0.2)
rows = state.usage.rows(days=1, key_id=rec_tiny["id"])
check("stream usage metered", rows and rows[0]["prompt_tokens"] == 17 and rows[0]["completion_tokens"] == 9, rows[:1])

s, h, b = call("/v1/chat/completions", key=secret_tiny, body={"model":"qwen3.8-max","messages":[]})
check("quota -> 429", s == 429 and h.get("Retry-After"), (s, dict(h)))
check("quota headers", h.get("x-qdp-quota-remaining") == "0", dict(h))

codes = []
for _ in range(4):
    s2, h2, _b2 = call("/v1/chat/completions", key=secret, body={"model":"qwen3.8-max","messages":[]})
    codes.append(s2)
check("rpm -> 429", 429 in codes and h2.get("Retry-After"), codes)

s, h, b = call("/settings")
check("/settings -> 404", s == 404, s)
s, h, b = call("/panel/login", body={})
check("/panel/login -> 404", s == 404, s)
s, h, b = call("/v1/models/refresh")
check("/v1/models/refresh -> 404 (not whitelisted)", s == 404, s)

s, h, b = call("/v1/messages", key="bad", body={"model":"qwen","messages":[]})
check("anthropic error shape", s == 401 and json.loads(b).get("type") == "error", b[:200])

s, h, b = call("/ping")
check("ping text", s == 200 and b == b"pong", (s, b))
s, h, b = call("/healthz")
check("healthz json", s == 200 and json.loads(b)["status"] == "ok", b[:120])

s, h, b = call("/v1/chat/completions", key=secret_glm, body={"model":"boom","messages":[]})
check("upstream 500 passthrough", s == 500 and "upstream exploded" in b.decode(), (s, b[:120]))

# RuntimeState：每 Key 活动计数、租约实例绑定、配置更新后的新信号量
# 先等最后一个 HTTP 请求的 handler 线程完全释放（瞬时并发会让 acquire 拿不到租约）
deadline = time.time() + 5
while time.time() < deadline:
    if all(entry["active"] == 0 for entry in state.runtime_snapshot()["keys"].values()):
        break
    time.sleep(0.05)
lease = state.acquire(rec_glm)
check("runtime acquire grants a lease", bool(lease))
check("runtime active counted",
      state.runtime_snapshot()["keys"].get(rec_glm["id"], {}).get("active") == 1,
      state.runtime_snapshot())
check("second acquire on exhausted key rejected", state.acquire(rec_glm) is False)
state.release(lease)
check("runtime active decremented once",
      state.runtime_snapshot()["keys"].get(rec_glm["id"], {}).get("active") == 0,
      state.runtime_snapshot())
store.update(rec_glm["id"], max_concurrent=2)
glm_updated = store.get(rec_glm["id"])
lease3 = state.acquire(glm_updated)
lease4 = state.acquire(glm_updated)
check("re-acquire under new limit works", bool(lease3) and bool(lease4))
state.release(lease)   # 旧租约只能释放旧的 1 槽信号量，不得影响新信号量
check("release targets the acquired semaphore instance",
      state.runtime_snapshot()["keys"][rec_glm["id"]]["active"] == 1,
      state.runtime_snapshot())
state.release(lease3)
state.release(lease4)
check("active returns to zero after releases",
      state.runtime_snapshot()["keys"].get(rec_glm["id"], {}).get("active") == 0,
      state.runtime_snapshot())

srv.shutdown(); up.shutdown()
print()
print("SUMMARY: TOTAL %d checks, %d failed" % (len(passes) + len(fails), len(fails)))
print("FAILURES:", fails if fails else "none")
print("audit rows:", len(state.usage.rows(days=1)))
print("sample row:", json.dumps(state.usage.rows(days=1)[0], ensure_ascii=False))
sys.exit(1 if fails else 0)
