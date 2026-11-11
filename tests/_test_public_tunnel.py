#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隧道模板与渲染链路测试：nps/npc/Caddy 模板与架构一致性、
qoder_public_tunnel.py render/prepare-server 的不覆盖与秘密处理、
CLI tunnel render 落在 public-data。全部在临时目录完成，不碰真实服务。"""
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import qoder_public_config as C

FAILS = []
PASS_COUNT = [0]


def check(name, cond, extra=""):
    if cond:
        PASS_COUNT[0] += 1
        print("PASS %s" % name)
    else:
        print("FAIL %s | %s" % (name, extra))
        FAILS.append(name)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


PUBLIC_PATHS = ("/v1/models", "/v1/chat/completions", "/v1/responses",
                "/v1/messages", "/v1/messages/count_tokens")


def test_templates():
    nps = read(os.path.join(ROOT, "scripts", "nps.conf.example"))
    active = "\n".join(line for line in nps.splitlines()
                      if line.strip() and not line.strip().startswith("#"))
    check("nps.conf: HTTP domain listener 只听回环 18080",
          "http_proxy_ip=127.0.0.1" in active and "http_proxy_port=18080" in active, nps)
    check("nps.conf: 加密桥接 8024", "bridge_type=tcp" in active
          and "bridge_port=8024" in active, nps)
    check("nps.conf: 配置行不含 public_vkey / auth_key / auth_crypt_key",
          "public_vkey" not in active and "auth_key" not in active
          and "auth_crypt_key" not in active, active)
    check("nps.conf: 管理面只听回环", "web_ip=127.0.0.1" in active
          and "web_port=8080" in active, nps)
    npc = read(os.path.join(ROOT, "scripts", "npc.conf.example"))
    check("npc.conf: 指向 VPS bridge 8024", "server_addr=<VPS_IP>:8024" in npc, npc)
    check("npc.conf: crypt=true / compress=true",
          "crypt=true" in npc and "compress=true" in npc, npc)
    check("npc.conf: target 是本机网关 8791", "target_addr=127.0.0.1:8791" in npc, npc)
    check("npc.conf: 私有 vkey 占位符待注入", "vkey=<NPS_CLIENT_VKEY>" in npc, npc)
    caddy = read(os.path.join(ROOT, "scripts", "Caddyfile.example"))
    check("Caddyfile: 五个公网白名单路径",
          all(path in caddy for path in PUBLIC_PATHS), caddy)
    check("Caddyfile: 反代指向 nps 回环 18080", "127.0.0.1:18080" in caddy, caddy)
    check("Caddyfile: 覆盖客户端转发头",
          "header_up X-Forwarded-For {http.request.remote.host}" in caddy
          and "header_up X-Real-IP {http.request.remote.host}" in caddy, caddy)
    check("Caddyfile: 删除可伪造的头",
          "-CF-Connecting-IP" in caddy and "-True-Client-IP" in caddy
          and "-X-Client-IP" in caddy and "-Forwarded" in caddy, caddy)
    check("Caddyfile: 非白名单路径 404", "respond 404" in caddy, caddy)


def run_tool(args, stdin=None, timeout=60):
    return subprocess.run([sys.executable, os.path.join(ROOT, "qoder_public_tunnel.py")]
                          + args, input=stdin, capture_output=True, text=True,
                          timeout=timeout)


def test_render(tmp):
    vkey = "v" * 32
    out = os.path.join(tmp, "render-out")
    proc = run_tool(["render", "--domain", "api.example-test.org", "--server",
                     "198.51.100.7", "--output-dir", out, "--vkey-stdin"],
                    stdin=vkey + "\n")
    files = {name: os.path.join(out, name) for name in
             ("Caddyfile", "nps.conf", "npc.conf")}
    check("render: 生成三件套", proc.returncode == 0
          and all(os.path.isfile(path) for path in files.values()),
          (proc.returncode, proc.stderr[:200]))
    check("render: vkey 不进 stdout/stderr", vkey not in proc.stdout + proc.stderr,
          (proc.stdout[:120], proc.stderr[:120]))
    rendered = read(files["npc.conf"])
    check("render: npc 注入 vkey / VPS / 域名",
          "vkey=" + vkey in rendered and "server_addr=198.51.100.7:8024" in rendered
          and "host=api.example-test.org" in rendered, rendered)
    check("render: 保留 crypt=true / target 8791",
          "crypt=true" in rendered and "target_addr=127.0.0.1:8791" in rendered,
          rendered)
    check("render: Caddyfile 注入域名",
          "api.example-test.org" in read(files["Caddyfile"]))
    check("render: nps.conf 占位符被替换或保留服务端语义",
          "<NPS_CLIENT_VKEY>" not in read(files["nps.conf"])
          and "<VPS_IP>" not in read(files["nps.conf"]))
    again = run_tool(["render", "--domain", "api.example-test.org", "--server",
                      "198.51.100.7", "--output-dir", out, "--vkey-stdin"],
                     stdin=vkey + "\n")
    check("render: 拒绝覆盖已有输出", again.returncode == 1
          and "refusing to overwrite" in again.stderr, again.stderr[:200])
    forced = run_tool(["render", "--domain", "api.example-test.org", "--server",
                       "198.51.100.7", "--output-dir", out, "--vkey-stdin",
                       "--force"], stdin="w" * 32 + "\n")
    check("render: --force 显式覆盖", forced.returncode == 0, forced.stderr[:200])
    check("render: 恶意域名被拒", run_tool(
        ["render", "--domain", "http://evil.example/x", "--server",
         "198.51.100.7", "--output-dir", os.path.join(tmp, "x"), "--vkey-stdin"],
        stdin=vkey + "\n").returncode == 2)


def test_prepare_server(tmp):
    root = os.path.join(tmp, "nps-root")
    conf = os.path.join(root, "conf")
    os.makedirs(conf)
    with open(os.path.join(conf, "clients.json"), "w", encoding="utf-8") as handle:
        handle.write("{}")
    with open(os.path.join(conf, "hosts.json"), "w", encoding="utf-8") as handle:
        handle.write("{}")
    domain = "api.example-test.org"
    vkey_path = os.path.join(tmp, "npc-vkey.txt")
    proc = run_tool(["prepare-server", "--nps-root", root, "--domain", domain,
                     "--client-id", "1", "--host-id", "1",
                     "--vkey-output", vkey_path, "--yes-stopped"])
    check("prepare: 离线写入 client/host", proc.returncode == 0, proc.stderr[:300])
    clients = json.load(open(os.path.join(conf, "clients.json"), encoding="utf-8"))
    hosts = json.load(open(os.path.join(conf, "hosts.json"), encoding="utf-8"))
    check("prepare: client crypt=true 且记录 vkey",
          clients["1"]["Cnf"]["Crypt"] is True and clients["1"]["VerifyKey"], clients)
    check("prepare: host 指向网关 target",
          hosts["1"]["Host"] == domain
          and hosts["1"]["Target"]["TargetStr"] == "127.0.0.1:8791", hosts)
    check("prepare: vkey 落盘到指定私密文件",
          os.path.isfile(vkey_path) and len(read(vkey_path).strip()) >= 16)
    duplicate = run_tool(["prepare-server", "--nps-root", root, "--domain", domain,
                          "--client-id", "2", "--host-id", "2",
                          "--vkey-output", os.path.join(tmp, "v2.txt"),
                          "--yes-stopped"])
    check("prepare: 同域名重复注册被拒", duplicate.returncode == 1
          and "already has an nps host record" in duplicate.stderr,
          duplicate.stderr[:200])
    unstopped = run_tool(["prepare-server", "--nps-root", root, "--domain", domain,
                          "--client-id", "3", "--host-id", "3",
                          "--vkey-output", os.path.join(tmp, "v3.txt")])
    check("prepare: 不加 --yes-stopped 直接拒绝", unstopped.returncode == 1
          and "stop nps" in unstopped.stderr, unstopped.stderr[:200])


def test_cli_tunnel_render(tmp):
    data = os.path.join(tmp, "public-data")
    os.makedirs(data)
    raw = json.loads(json.dumps(C.DEFAULTS))
    raw["upstream"]["api_key"] = "qdp-internal-test"
    raw["admin"]["password_hash"] = C.hash_password("pw")
    raw["server"]["port"] = 38791
    raw["admin"]["port"] = 38792
    raw["nps"].update({"server_addr": "198.51.100.7:8024", "vkey": "c" * 32,
                       "mode": "domain", "domain": "api.example-test.org"})
    config_path = os.path.join(data, "config.json")
    C.write_config(config_path, raw)
    cli = os.path.join(ROOT, "qoder_public_cli.py")
    common = [sys.executable, cli, "--config", config_path]
    proc = subprocess.run(common + ["tunnel", "render", "--print-path"],
                          capture_output=True, text=True, timeout=60)
    check("cli render: --print-path 只打印不写入",
          proc.returncode == 0 and proc.stdout.strip().endswith("npc.conf")
          and not os.path.exists(os.path.join(data, "npc.conf")), proc.stdout)
    proc = subprocess.run(common + ["tunnel", "render"], capture_output=True,
                          text=True, timeout=60)
    npc_conf = os.path.join(data, "npc.conf")
    check("cli render: 写入 public-data（不在仓库）",
          proc.returncode == 0 and os.path.isfile(npc_conf), proc.stderr[:200])
    text = read(npc_conf)
    check("cli render: domain 模式指向网关端口",
          "host = api.example-test.org" in text
          and "target_addr = 127.0.0.1:%d" % C.load(config_path).server_port in text,
          text)
    check("cli render: vkey 不外泄到 stdout",
          "c" * 32 not in proc.stdout, proc.stdout[:120])
    proc = subprocess.run(common + ["tunnel", "render"], capture_output=True,
                          text=True, timeout=60)
    check("cli render: 已存在时拒绝覆盖", proc.returncode == 1
          and "拒绝覆盖" in proc.stderr, proc.stderr[:200])
    proc = subprocess.run(common + ["tunnel", "render", "--force"],
                          capture_output=True, text=True, timeout=60)
    check("cli render: --force 确认覆盖", proc.returncode == 0, proc.stderr[:200])


def main():
    tmp = tempfile.mkdtemp(prefix="qdp-tunnel-")
    try:
        test_templates()
        test_render(tmp)
        test_prepare_server(tmp)
        test_cli_tunnel_render(tmp)
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    print()
    print("SUMMARY: TOTAL %d checks" % (PASS_COUNT[0] + len(FAILS)))
    if FAILS:
        print("FAILED (%d): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("ALL PUBLIC-TUNNEL TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
