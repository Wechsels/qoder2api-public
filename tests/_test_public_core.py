#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qoder2api-public 核心模块自测（config / keys / meter）。

与上游 tests/ 的其它脚本一样是「直接运行」的独立脚本：

    python tests/_test_public_core.py

覆盖：配置校验与 fail-closed、Key 签发/摘要/启停/轮换/软删/计量、
流式与非流式 usage 解析、估算回退、用量账本与配额累计。
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import qoder_public_config as C
import qoder_public_keys as K
import qoder_public_meter as M

FAILS = []


def check(name, cond, extra=""):
    if cond:
        print("PASS %s" % name)
    else:
        print("FAIL %s | %s" % (name, extra))
        FAILS.append(name)


def make_config(tmp, **overrides):
    data = os.path.join(tmp, "public-data")
    os.makedirs(data, exist_ok=True)
    raw = json.loads(json.dumps(C.DEFAULTS))
    raw["upstream"]["api_key"] = "qdp-internal-test"
    raw["upstream"]["panel_password"] = "panel-pw"
    raw["admin"]["password_hash"] = C.hash_password("secret-admin")
    for dotted, value in overrides.items():
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node[part]
        node[parts[-1]] = value
    path = os.path.join(data, "config.json")
    C.write_config(path, raw)
    return path


def test_config():
    tmp = tempfile.mkdtemp()
    path = make_config(tmp)
    cfg = C.load(path)
    check("config: 加载成功", cfg.upstream_api_key == "qdp-internal-test")
    check("config: 口令校验", cfg.verify_admin_password("secret-admin")
          and not cfg.verify_admin_password("wrong"))
    check("config: 数据目录", cfg.keys_path.endswith("keys.json"))

    # fail closed：缺内部 Key 必须拒绝启动
    bad = os.path.join(tmp, "bad.json")
    raw = json.loads(json.dumps(C.DEFAULTS))
    raw["upstream"]["api_key"] = ""
    C.write_config(bad, raw)
    try:
        C.load(bad)
        check("config: 缺内部 Key 应拒绝", False)
    except C.ConfigError:
        check("config: 缺内部 Key 应拒绝", True)

    # 环境变量覆盖
    cfg2 = C.load(path, env={"QDP_UPSTREAM_KEY": "from-env",
                             "QDP_PORT": "9999",
                             "QDP_DATA_DIR": os.path.join(tmp, "public-data")})
    check("config: 环境变量覆盖", cfg2.upstream_api_key == "from-env"
          and cfg2.server_port == 9999)

    # 非法端口
    try:
        C.load(make_config(tempfile.mkdtemp(), **{"server.port": 0}))
        check("config: 端口 0 应拒绝", False)
    except C.ConfigError:
        check("config: 端口 0 应拒绝", True)

    # 管理面非本机但未开 allow_remote
    try:
        C.load(make_config(tempfile.mkdtemp(), **{"admin.host": "0.0.0.0"}))
        check("config: 远程管理面需显式允许", False)
    except C.ConfigError:
        check("config: 远程管理面需显式允许", True)


def test_keys():
    tmp = tempfile.mkdtemp()
    cfg = C.load(make_config(tmp))
    store = K.KeyStore(cfg.keys_path, defaults=cfg.defaults)
    record, secret = store.create("alice-laptop", owner="alice", rpm=30,
                                  allowed_models=["Qwen*"])
    check("keys: 前缀与明文一致", secret.startswith("qdp-")
          and record["prefix"] == secret[:12])
    found, reason = store.lookup(secret)
    check("keys: 明文可校验", reason == "ok" and found["id"] == record["id"])
    check("keys: 未知 Key", store.lookup("qdp-nope")[1] == "unknown")
    check("keys: 模型白名单", store.model_allowed(found, "qwen3.8-max")
          and not store.model_allowed(found, "glm-5"))
    check("keys: 文件里没有明文", secret not in open(cfg.keys_path, encoding="utf-8").read())

    store.set_enabled(record["id"], False)
    check("keys: 禁用生效", store.lookup(secret)[1] == "disabled")
    store.set_enabled(record["id"], True)

    _rotated, secret2 = store.rotate(record["id"])
    check("keys: 轮换后旧 Key 失效", store.lookup(secret)[0] is None
          and store.lookup(secret2)[1] == "ok")

    store.update(record["id"], note="hello", daily_tokens=1000)
    check("keys: 更新字段", store.get(record["id"])["note"] == "hello")

    try:
        store.create("alice-laptop")
        check("keys: 重名应拒绝", False)
    except K.KeyStoreError:
        check("keys: 重名应拒绝", True)

    try:
        store.update(record["id"], hash="sha256:evil")
        check("keys: 不可改 hash", False)
    except K.KeyStoreError:
        check("keys: 不可改 hash", True)

    store.add_totals(record["id"], requests=1, prompt=10, completion=5)
    store.flush(force=True)
    reloaded = K.KeyStore(cfg.keys_path, defaults=cfg.defaults)
    check("keys: 统计可持久化", reloaded.get(record["id"])["totals"]["requests"] == 1)

    reloaded.delete(record["id"])
    check("keys: 软删后失效", reloaded.lookup(secret2)[1] == "unknown")
    check("keys: 软删后列表隐藏", all(k["id"] != record["id"] for k in reloaded.list()))

    # 过期
    expired, secret3 = reloaded.create("expired", expires_at="2000-01-01T00:00:00+08:00")
    check("keys: 过期 Key 拒绝", reloaded.lookup(secret3)[1] == "expired")

    # IP 白名单
    ipk, _s = reloaded.create("ipkey", allowed_ips=["10.0.0.0/8"])
    check("keys: IP 白名单", reloaded.ip_allowed(ipk, "10.1.2.3")
          and not reloaded.ip_allowed(ipk, "192.168.1.1"))


def test_meter():
    meter = M.StreamMeter("/v1/chat/completions", request_body_bytes=100)
    meter.feed(b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n')
    meter.feed(b'data: {"choices":[],"usage":{"prompt_tokens":12,'
               b'"completion_tokens":7,"total_tokens":19}}\n\n')
    meter.feed(b'data: [DONE]\n\n')
    usage = meter.finish()
    check("meter: OpenAI SSE usage", (usage.prompt, usage.completion,
                                      usage.estimated) == (12, 7, False), usage)

    meter = M.StreamMeter("/v1/messages", request_body_bytes=200)
    meter.feed(b'event: message_start\ndata: {"type":"message_start","message":'
               b'{"usage":{"input_tokens":9,"cache_read_input_tokens":3}}}\n\n')
    meter.feed(b'event: content_block_delta\ndata: {"type":"content_block_delta",'
               b'"delta":{"text":"abcdefgh"}}\n\n')
    meter.feed(b'event: message_delta\ndata: {"type":"message_delta",'
               b'"usage":{"output_tokens":4}}\n\n')
    usage = meter.finish()
    check("meter: Anthropic usage（含 cache 计入输入）",
          (usage.prompt, usage.completion) == (12, 4), usage)

    meter = M.StreamMeter("/v1/chat/completions", request_body_bytes=50)
    meter.feed(b'{"choices":[{"message":{"content":"hi"}}],'
               b'"usage":{"prompt_tokens":3,"completion_tokens":2}}')
    usage = meter.finish()
    check("meter: OpenAI JSON usage",
          (usage.prompt, usage.completion, usage.estimated) == (3, 2, False), usage)

    meter = M.StreamMeter("/v1/chat/completions", request_body_bytes=40)
    meter.feed(b'{"choices":[{"message":{"content":"hello world"}}]}')
    usage = meter.finish()
    check("meter: 缺 usage 时估算并标记", usage.estimated and usage.prompt > 0, usage)

    meter = M.StreamMeter("/v1/chat/completions", request_body_bytes=10)
    meter.feed(b'data: {"choices":')  # 截断的流
    usage = meter.finish()
    check("meter: 截断流不崩", usage is not None)


def test_usage_log():
    tmp = tempfile.mkdtemp()
    log = M.UsageLog(os.path.join(tmp, "usage.jsonl"))
    now = M.datetime.now(M.timezone.utc).isoformat()
    log.append({"ts": now, "key_id": "k1", "status": 200,
                "prompt_tokens": 12, "completion_tokens": 7})
    log.append({"ts": now, "key_id": "k1", "status": 500,
                "prompt_tokens": 1, "completion_tokens": 0})
    log.append({"ts": now, "key_id": "k2", "status": 200,
                "prompt_tokens": 5, "completion_tokens": 5})
    summary = {row["key_id"]: row for row in log.summary(days=7)}
    check("usage: 按 Key 汇总", summary["k1"]["requests"] == 2
          and summary["k1"]["errors"] == 1 and summary["k1"]["prompt_tokens"] == 13)
    totals = log.totals("k1")
    check("usage: 日/月累计", totals["day_tokens"] == 20
          and totals["month_tokens"] == 20, totals)
    check("usage: 重置时间在将来", totals["reset_at"] > now[:10])
    check("usage: 概览行数与明细一致", len(log.rows(days=7)) == 3)

    reloaded = M.UsageLog(os.path.join(tmp, "usage.jsonl"))
    check("usage: 重启后回放", reloaded.totals("k1")["day_tokens"] == 20)


def main():
    test_config()
    test_keys()
    test_meter()
    test_usage_log()
    print()
    if FAILS:
        print("FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("ALL PUBLIC-CORE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
