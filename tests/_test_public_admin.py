#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""管理面 + CLI 集成测试（全本机回环、临时目录）。

覆盖：登录/登出、CSRF/Origin/Host 边界、Key CRUD/rotate/revoke、
明文 secret 只出现在 create/rotate 响应、session 过期、CLI 别名与
init 不覆盖、dashboard 无外部 CDN。
"""
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _suite_head import *  # noqa: F401,F403

sys.path.insert(0, ROOT)
import qoder_public_config as C
import qoder_public_gateway as G
import qoder_public_admin as A
import qoder_public_keys as K

declare_suite(__file__, ["qoder_public_admin.py", "qoder_public_cli.py",
                         "qoder_public_dashboard.html"])
ADMIN_PASSWORD = "test-admin-password"


def admin_request(port, method, path, body=None, cookie=None, csrf=None,
                  origin=None, host=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Origin": origin or ("http://127.0.0.1:%d" % port),
               "X-Requested-With": "qdp-admin"}
    if cookie:
        headers["Cookie"] = cookie
    if csrf:
        headers["X-QDP-CSRF"] = csrf
    if host:
        headers["Host"] = host
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return (response.status, response.read(),
                response.getheader("Set-Cookie"))
    finally:
        connection.close()


def main():
    temp = tempfile.TemporaryDirectory(prefix="qdp-admin-")
    raw = json.loads(json.dumps(C.DEFAULTS))
    raw["upstream"]["api_key"] = "qdp-internal-test"
    raw["admin"]["password_hash"] = C.hash_password(ADMIN_PASSWORD)
    raw["logging"].update({"dir": os.path.join(temp.name, "logs"),
                           "level": "error"})
    raw["server"]["port"] = 38791
    raw["admin"]["port"] = 38792
    config_path = os.path.join(temp.name, "public-data", "config.json")
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(raw, handle)
    config = C.load(config_path)
    state = G.build_state(config, G.GatewayLog(config.log_dir, level="error"))
    server = A.AdminServer(("127.0.0.1", 0), state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        # -- 登录边界 ---------------------------------------------------
        status, body, _cookie = admin_request(port, "POST", "/api/login",
                                              {"password": "wrong-password"})
        check("login: 错误密码 401", status == 401, (status, body[:200]))
        status, body, _cookie = admin_request(port, "POST", "/api/login", {})
        check("login: 空 body 401", status == 401, (status, body[:200]))
        status, body, cookie = admin_request(
            port, "POST", "/api/login", {"password": ADMIN_PASSWORD})
        parsed = json.loads(body)
        check("login: 正确密码 200 + csrf",
              status == 200 and parsed.get("csrf") and cookie
              and cookie.startswith("qdp_admin="), (status, parsed))
        csrf = parsed["csrf"]
        cookie_value = cookie.split(";")[0]

        status, _body, _c = admin_request(port, "GET", "/api/keys")
        check("session: 无 cookie 的读请求 401", status == 401, status)
        status, body, _c = admin_request(port, "GET", "/api/keys",
                                         cookie=cookie_value)
        keys = json.loads(body).get("keys")
        check("session: 带 cookie 列出 Key", status == 200 and keys == [], (status, body[:200]))

        status, _body, _c = admin_request(port, "POST", "/api/keys",
                                          {"name": "no-csrf"},
                                          cookie=cookie_value)
        check("csrf: 写请求缺 X-QDP-CSRF 被拒", status == 403, status)
        status, _body, _c = admin_request(port, "POST", "/api/keys",
                                          {"name": "bad-origin"},
                                          cookie=cookie_value, csrf=csrf,
                                          origin="http://evil.example")
        check("csrf: 跨源 Origin 被拒", status == 403, status)
        status, _body, _c = admin_request(port, "GET", "/api/keys",
                                         cookie=cookie_value, host="evil:1234")
        check("host: 不受信任的 Host 被拒", status == 403, status)
        status, _body, _c = admin_request(port, "OPTIONS", "/api/keys")
        check("cors: OPTIONS 一律拒绝", status == 403, status)
        status, body, _c = admin_request(port, "GET", "/api/keys",
                                         cookie=cookie_value,
                                         origin="http://127.0.0.1:%d" % (port + 1))
        check("origin: 端口不匹配被拒", status == 403, status)

        # -- Key CRUD ---------------------------------------------------
        status, body, _c = admin_request(
            port, "POST", "/api/keys",
            {"name": "alice", "owner": "ops", "rpm": 30,
             "allowed_models": ["qwen*"], "daily_tokens": 1000},
            cookie=cookie_value, csrf=csrf)
        payload = json.loads(body)
        secret = payload.get("secret") or ""
        record = payload.get("key") or {}
        check("keys: create 201 返回一次明文",
              status == 201 and secret.startswith("qdp-")
              and record.get("name") == "alice", (status, payload))
        with open(state.store.path, encoding="utf-8") as handle:
            stored = handle.read()
        check("keys: keys.json 不含明文 secret", secret not in stored)
        check("keys: keys.json 不含 hash 之外的口令字段", "hash" in stored)
        found, reason = state.store.lookup(secret)
        check("keys: 管理面创建的 Key 可被网关校验", reason == "ok"
              and found["id"] == record["id"], reason)

        status, body, _c = admin_request(port, "GET", "/api/keys",
                                         cookie=cookie_value)
        listed = json.loads(body)["keys"]
        check("keys: 列表不带 hash/明文",
              all("hash" not in row and "secret" not in row for row in listed), listed)

        status, body, _c = admin_request(
            port, "PATCH", "/api/keys/" + record["id"], {"rpm": 5},
            cookie=cookie_value, csrf=csrf)
        check("keys: PATCH 更新字段",
              status == 200 and json.loads(body)["key"]["rpm"] == 5, (status, body[:200]))

        status, body, _c = admin_request(
            port, "POST", "/api/keys/%s/disable" % record["id"],
            cookie=cookie_value, csrf=csrf)
        check("keys: disable 生效",
              status == 200 and json.loads(body)["key"]["enabled"] is False,
              (status, body[:200]))
        _found, reason = state.store.lookup(secret)
        check("keys: 禁用后网关拒绝", reason == "disabled", reason)

        status, body, _c = admin_request(
            port, "POST", "/api/keys/%s/enable" % record["id"],
            cookie=cookie_value, csrf=csrf)
        check("keys: enable 恢复", status == 200
              and json.loads(body)["key"]["enabled"] is True, status)

        status, body, _c = admin_request(
            port, "POST", "/api/keys/%s/rotate" % record["id"],
            cookie=cookie_value, csrf=csrf)
        rotated = json.loads(body)
        new_secret = rotated.get("secret") or ""
        check("keys: rotate 返回新明文一次", status == 200
              and new_secret.startswith("qdp-") and new_secret != secret, rotated)
        _f, old_reason = state.store.lookup(secret)
        _f2, new_reason = state.store.lookup(new_secret)
        check("keys: 轮换后旧 Key 失效、新 Key 可用",
              old_reason == "unknown" and new_reason == "ok",
              (old_reason, new_reason))

        status, body, _c = admin_request(
            port, "POST", "/api/keys",
            {"name": "bob", "enabled": False}, cookie=cookie_value, csrf=csrf)
        bob = json.loads(body)
        bob_id = bob["key"]["id"]
        check("keys: create 可直接生成禁用 Key",
              status == 201 and bob["key"]["enabled"] is False, bob)

        status, body, _c = admin_request(
            port, "DELETE", "/api/keys/" + bob_id,
            cookie=cookie_value, csrf=csrf)
        check("keys: revoke 软删 200", status == 200, (status, body[:200]))
        status, body, _c = admin_request(port, "GET", "/api/keys/" + bob_id,
                                         cookie=cookie_value)
        check("keys: 软删后单查 404", status == 404, status)
        status, body, _c = admin_request(port, "GET", "/api/keys",
                                         cookie=cookie_value)
        check("keys: 软删后列表隐藏",
              all(row["id"] != bob_id for row in json.loads(body)["keys"]))

        status, body, _c = admin_request(
            port, "POST", "/api/keys", {"name": "bad", "hash": "sha256:evil"},
            cookie=cookie_value, csrf=csrf)
        check("keys: 不支持的字段被拒", status == 400, (status, body[:200]))
        status, body, _c = admin_request(
            port, "POST", "/api/keys", {"name": "bad-realm", "realm": "xx"},
            cookie=cookie_value, csrf=csrf)
        check("keys: 非法 realm 被拒", status == 400, (status, body[:200]))

        # -- 状态/审计/用量 ---------------------------------------------
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        state.usage.append({"ts": now_iso, "key_id": record["id"],
                            "path": "/v1/chat/completions",
                            "status": 200, "prompt_tokens": 3,
                            "completion_tokens": 4, "request_id": "req_x1"})
        for label, api in (("status", "/api/status"), ("usage", "/api/usage"),
                           ("audit", "/api/audit")):
            status, body, _c = admin_request(port, "GET", api,
                                             cookie=cookie_value)
            check("api: %s 200" % label, status == 200, (status, body[:120]))
        status, body, _c = admin_request(port, "GET", "/api/status",
                                         cookie=cookie_value)
        report = json.loads(body)
        check("api: status 带 runtime 快照", "runtime" in report
              and report["runtime"].get("keys") is not None, report.get("runtime"))
        status, body, _c = admin_request(port, "GET", "/api/audit?limit=2",
                                         cookie=cookie_value)
        check("api: audit limit 生效",
              status == 200 and len(json.loads(body)["rows"]) == 1, body[:200])

        # -- 登出与 session 过期 ----------------------------------------
        status, _body, set_cookie = admin_request(
            port, "POST", "/api/logout", {}, cookie=cookie_value, csrf=csrf)
        check("logout: 200 且 cookie 失效", status == 200
              and set_cookie is not None and "Max-Age=0" in set_cookie, set_cookie)
        status, _body, _c = admin_request(port, "GET", "/api/keys",
                                         cookie=cookie_value)
        check("logout: 旧 session 不可用", status == 401, status)

        status, body, cookie = admin_request(
            port, "POST", "/api/login", {"password": ADMIN_PASSWORD})
        csrf = json.loads(body)["csrf"]
        cookie_value = cookie.split(";")[0]
        _token = cookie_value.split("=", 1)[1]
        with server.sessions._lock:
            for item in server.sessions._items.values():
                item["expires"] = time.time() - 1
        status, _body, _c = admin_request(port, "GET", "/api/keys",
                                         cookie=cookie_value)
        check("session: 过期后拒绝", status == 401, status)

        # -- dashboard 无外部资源 ---------------------------------------
        with open(os.path.join(ROOT, "qoder_public_dashboard.html"),
                  encoding="utf-8") as handle:
            html = handle.read()
        check("dashboard: 无外部 CDN/外链",
              not re.search(r"(?:src|href|action)\s*=\s*[\"']https?://", html)
              and "http://" not in html, re.findall(r"https?://[^\s\"']+", html)[:5])
        status, body, _c = admin_request(port, "GET", "/")
        check("dashboard: 首页 200 返回 HTML",
              status == 200 and body.startswith(b"<!doctype html>"), (status, body[:60]))
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            connection.request("GET", "/")
            response = connection.getresponse()
            response.read()
            csp = response.getheader("Content-Security-Policy") or ""
        finally:
            connection.close()
        check("dashboard: CSP nonce 注入（占位符被替换）",
              "script-src 'nonce-" in csp and "__CSP_NONCE__" not in csp
              and "style-src 'self'" in csp, csp)

        # -- CLI --------------------------------------------------------
        cli = os.path.join(ROOT, "qoder_public_cli.py")
        before_mtime = os.path.getmtime(config_path)
        before = open(config_path, encoding="utf-8").read()
        proc = subprocess.run([sys.executable, cli, "--config", config_path, "init"],
                              capture_output=True, text=True, timeout=60)
        check("cli: init 拒绝覆盖已有配置",
              proc.returncode == 1 and "拒绝覆盖" in proc.stderr
              and open(config_path, encoding="utf-8").read() == before,
              (proc.returncode, proc.stderr[:200]))

        fresh = os.path.join(temp.name, "fresh", "config.json")
        proc = subprocess.run([sys.executable, cli, "--config", fresh, "init"],
                              capture_output=True, text=True, timeout=60)
        fresh_raw = json.load(open(fresh, encoding="utf-8")) if os.path.isfile(fresh) else {}
        check("cli: init 生成随机秘密配置",
              proc.returncode == 0 and os.path.isfile(fresh)
              and fresh_raw.get("admin", {}).get("password_hash", "").startswith("pbkdf2_sha256$")
              and fresh_raw.get("upstream", {}).get("api_key", "").startswith("qdp-internal-")
              and fresh_raw.get("nps", {}).get("vkey"), proc.stderr[:200])
        proc = subprocess.run([sys.executable, cli, "--config", fresh, "init"],
                              capture_output=True, text=True, timeout=60)
        check("cli: init 二次运行拒绝", proc.returncode == 1, proc.returncode)

        env = dict(os.environ)
        env["QDP_ADMIN_PASSWORD"] = ADMIN_PASSWORD
        base_url = "http://127.0.0.1:%d" % port
        common = [sys.executable, cli, "--config", config_path, "--url", base_url]
        proc = subprocess.run(common + ["key", "add", "cli-bob", "--rpm", "30"],
                              capture_output=True, text=True, env=env, timeout=60)
        check("cli: key add 经管理面创建", proc.returncode == 0
              and "qdp-" in proc.stdout and "仅本次显示" in proc.stdout,
              (proc.returncode, proc.stdout[:200], proc.stderr[:200]))
        proc = subprocess.run(common + ["key", "create", "cli-alias"],
                              capture_output=True, text=True, env=env, timeout=60)
        check("cli: key create 是 add 的别名", proc.returncode == 0
              and "qdp-" in proc.stdout, (proc.returncode, proc.stderr[:200]))
        proc = subprocess.run(common + ["key", "list"], capture_output=True,
                              text=True, env=env, timeout=60)
        listed = json.loads(proc.stdout).get("keys", [])
        check("cli: key list 返回 JSON 且无明文",
              proc.returncode == 0
              and {row["name"] for row in listed} >= {"cli-bob", "cli-alias"}
              and all("secret" not in row and "hash" not in row for row in listed),
              proc.stdout[:200])
        proc = subprocess.run(common + ["audit", "--limit", "5"],
                              capture_output=True, text=True, env=env, timeout=60)
        check("cli: audit 输出流水", proc.returncode == 0
              and json.loads(proc.stdout).get("rows") is not None,
              proc.stdout[:200])
        proc = subprocess.run(common + ["status"], capture_output=True,
                              text=True, env=env, timeout=60)
        check("cli: status 输出在线状态", proc.returncode == 0
              and "runtime" in json.loads(proc.stdout), proc.stdout[:200])
        bob_rows = [row for row in state.store.list() if row["name"] == "cli-bob"]
        proc = subprocess.run(common + ["key", "rotate", bob_rows[0]["id"]],
                              capture_output=True, text=True, env=env, timeout=60)
        check("cli: rotate 打印新明文一次", proc.returncode == 0
              and "qdp-" in proc.stdout and "仅本次显示" in proc.stdout,
              proc.stdout[:200])
        proc = subprocess.run(common + ["key", "revoke", bob_rows[0]["id"]],
                              capture_output=True, text=True, env=env, timeout=60)
        check("cli: revoke 需要 --yes", proc.returncode == 1
              and "--yes" in proc.stderr, proc.stderr[:200])
        proc = subprocess.run(common + ["key", "revoke", bob_rows[0]["id"], "--yes"],
                              capture_output=True, text=True, env=env, timeout=60)
        check("cli: revoke --yes 软删", proc.returncode == 0, proc.stderr[:200])
    finally:
        server.shutdown()
        server.server_close()
        state.store.stop_autosave()
        thread.join(timeout=3)
        temp.cleanup()
    finish()


if __name__ == "__main__":
    main()
