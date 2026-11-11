#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Local CLI. Online key management always goes through the loopback admin API."""

import argparse
import copy
import getpass
import http.cookiejar
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

import qoder_public_config as config_module


ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(ROOT, config_module.DEFAULT_DATA_DIR,
                              config_module.DEFAULT_CONFIG_NAME)
KEY_FIELDS = ("name", "owner", "note", "expires_at", "rpm", "max_concurrent",
              "daily_tokens", "monthly_tokens", "allowed_models", "allowed_ips", "realm")


def config_path(args):
    return os.path.abspath(getattr(args, "config", None)
                           or os.environ.get("QDP_CONFIG") or DEFAULT_CONFIG)


def load_config(args):
    return config_module.load(config_path(args))


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def init_command(args):
    path = config_path(args)
    if os.path.exists(path):
        raise ValueError("配置已存在，拒绝覆盖: %s" % path)
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    raw = copy.deepcopy(config_module.DEFAULTS)
    password = config_module.new_secret(24)
    raw["upstream"]["api_key"] = "qdp-internal-" + config_module.new_secret(32)
    raw["upstream"]["panel_password"] = config_module.new_secret(24)
    raw["admin"]["password_hash"] = config_module.hash_password(password)
    raw["nps"]["vkey"] = config_module.new_secret(32)
    config_module.Config(copy.deepcopy(raw), path=path)
    # Exclusive creation prevents overwriting an operator's file on a race.
    created = False
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        created = True
        config_module.write_config(path, raw)
    except Exception:
        if created and os.path.isfile(path) and os.path.getsize(path) == 0:
            os.unlink(path)
        raise
    print("已生成配置: %s" % path)
    print("管理密码（仅本次显示）: %s" % password)
    print("内部 Key 与 nps vkey 已随机生成并仅写入配置；请妥善保存管理密码。")
    return 0


class AdminClient(object):
    def __init__(self, args):
        cfg = load_config(args)
        self.url = getattr(args, "url", None) or "http://%s:%d" % (
            "[::1]" if ":" in cfg.admin_host else cfg.admin_host, cfg.admin_port)
        self.origin = self.url
        self.csrf = ""
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        password = os.environ.get("QDP_ADMIN_PASSWORD")
        if password is None:
            password = getpass.getpass("管理密码: ")
        self.request("POST", "/api/login", {"password": password}, login=True)

    def request(self, method, path, body=None, login=False):
        headers = {"Accept": "application/json", "Origin": self.origin,
                   "X-Requested-With": "qdp-admin"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if not login and method != "GET":
            headers["X-QDP-CSRF"] = self.csrf
        request = urllib.request.Request(self.url + path, data=data,
                                        headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode("utf-8"))["error"]["message"]
            except Exception:
                message = str(exc)
            raise ValueError("管理 API 错误 (%d): %s" % (exc.code, message))
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            raise ValueError("无法连接本机管理面: %s" % exc)
        if login:
            self.csrf = result["csrf"]
        return result


def key_payload(args):
    payload = {}
    for field in KEY_FIELDS:
        value = getattr(args, field, None)
        if value is not None:
            payload[field] = value
    if getattr(args, "enabled", None) is not None:
        payload["enabled"] = args.enabled == "true"
    for field in ("allowed_models", "allowed_ips"):
        if field in payload:
            text = payload[field].strip()
            if text.startswith("["):
                value = json.loads(text)
                if not isinstance(value, list):
                    raise ValueError(field + " 必须是列表")
                payload[field] = value
            else:
                payload[field] = [part.strip() for part in text.split(",") if part.strip()]
    return payload


def emit(result, args):
    print_json(result)
    if result.get("secret"):
        print("\nAPI Key（仅本次显示，请立即保存）: %s" % result["secret"])


def tcp_check(host, port, timeout=2):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, "监听中"
    except (OSError, ValueError) as exc:
        return False, str(exc)


def upstream_probe(cfg, path):
    from urllib.parse import urlsplit
    parsed = urlsplit(cfg.upstream_url)
    if parsed.hostname not in ("127.0.0.1", "::1", "localhost"):
        return False, "上游地址不是本机，已跳过探测"
    request = urllib.request.Request(
        cfg.upstream_url + path,
        headers={"Authorization": "Bearer " + cfg.upstream_api_key})
    context = None
    if parsed.scheme == "https":
        context = ssl.create_default_context()
        if not cfg.upstream_verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
    try:
        with urllib.request.urlopen(request, timeout=8, context=context) as response:
            body = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
            return True, ({"status": body.get("status", "ok")} if path == "/health"
                          else {"models": len(body.get("data", []))})
    except Exception as exc:
        return False, str(exc)


def doctor(args):
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    try:
        cfg = load_config(args)
        add("config", True, config_path(args))
    except Exception as exc:
        add("config", False, str(exc))
        print_json({"checks": checks, "ok": False})
        return 1
    for label, path, directory in (
            ("data_dir", cfg.data_dir, True),
            ("usage_dir", cfg.usage_dir, True),
            ("log_dir", cfg.log_dir, True),
            ("keys_file", cfg.keys_path, False)):
        exists = os.path.isdir(path) if directory else os.path.isfile(path)
        add(label, exists, path)
        if exists and directory:
            add(label + "_writable", os.access(path, os.W_OK), "可写" if os.access(path, os.W_OK) else "不可写")
        elif directory:
            parent = os.path.dirname(path)
            while parent and not os.path.isdir(parent):
                parent = os.path.dirname(parent)
            add(label + "_writable", os.access(parent, os.W_OK),
                "尚不存在；运行时自动创建" if os.access(parent, os.W_OK) else "父目录不可写")
    for label, path in (("upstream_health", "/health"), ("upstream_models", "/v1/models")):
        ok, detail = upstream_probe(cfg, path)
        add(label, ok, detail)
    host = "127.0.0.1" if cfg.admin_host in ("0.0.0.0", "::", "") else cfg.admin_host
    for label, port in (("gateway_port", cfg.server_port), ("admin_port", cfg.admin_port)):
        ok, detail = tcp_check(host if label == "admin_port" else cfg.server_host, port)
        add(label, ok, detail)
    npc_names = ("npc.exe", "npc") if os.name == "nt" else ("npc", "npc.exe")
    npc_candidates = []
    explicit = os.environ.get("QDP_NPC_BIN")
    if explicit:
        npc_candidates.append(explicit)
    # 只在数据目录找二进制（0600 私密、被 .gitignore 覆盖），不诱导放进仓库
    npc_candidates += [os.path.join(cfg.data_dir, "tools", name) for name in npc_names]
    npc = next((path for path in npc_candidates if os.path.isfile(path)), None)
    add("npc_binary", bool(npc),
        npc or "public-data/tools/npc[.exe] 不存在（或设 QDP_NPC_BIN）")
    npc_config = os.path.join(cfg.data_dir, "npc.conf")
    add("npc_config", os.path.isfile(npc_config), npc_config)
    nps = cfg.nps
    address = str(nps.get("server_addr") or "")
    try:
        server_host, server_port = address.rsplit(":", 1)
        ok, detail = tcp_check(server_host.strip("[]"), server_port)
    except ValueError:
        ok, detail = False, "nps.server_addr 未配置或格式无效"
    add("nps_bridge_tcp", ok, detail)
    print_json({"checks": checks, "ok": all(row["ok"] for row in checks)})
    return 0 if all(row["ok"] for row in checks) else 1


def tunnel_render(args):
    cfg = load_config(args)
    if not cfg.nps.get("server_addr") or not cfg.nps.get("vkey"):
        raise ValueError("请先在配置中设置 nps.server_addr 与 nps.vkey")
    # 输出到数据目录（0600、被 .gitignore 覆盖）；仓库目录绝不含 vkey
    path = os.path.join(cfg.data_dir, "npc.conf")
    if os.path.exists(path) and not args.force:
        raise ValueError("npc 配置已存在，拒绝覆盖: %s（确认后可使用 --force）" % path)
    if args.print_path:
        print("输出路径: %s" % path)
        return 0
    if cfg.nps.get("mode", "domain") == "domain":
        if not cfg.nps.get("domain"):
            raise ValueError("domain 模式必须设置 nps.domain")
        tunnel = ("host = %s\ntarget_addr = 127.0.0.1:%d"
                  % (cfg.nps["domain"], cfg.server_port))
    else:
        tunnel = ("mode = tcp\nserver_port = %s\ntarget_addr = 127.0.0.1:%d"
                  % (cfg.nps.get("server_port", 8443), cfg.server_port))
    text = ("[common]\nserver_addr = %s\nconn_type = tcp\nvkey = %s\n"
            "auto_reconnection = true\ncrypt = %s\ncompress = %s\n"
            "disconnect_timeout = 60\n\n[api]\n%s\n"
            % (cfg.nps["server_addr"], cfg.nps["vkey"],
               str(bool(cfg.nps.get("crypt", True))).lower(),
               str(bool(cfg.nps.get("compress", True))).lower(), tunnel))
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    # Atomic text write; never print the vkey.
    temporary = path + ".tmp.%d" % os.getpid()
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    print("已生成 npc 配置: %s（vkey 未打印）" % path)
    return 0


def parser():
    root = argparse.ArgumentParser(description="qoder2api-public 本机管理 CLI")
    root.add_argument("--config", help="配置文件路径")
    root.add_argument("--url", help="本机管理 API URL")
    root.add_argument("--json", action="store_true", help="JSON 输出（当前默认格式）")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="离线生成随机配置，不覆盖已有文件")
    key = commands.add_parser("key").add_subparsers(dest="action", required=True)
    for name in ("add", "create"):
        item = key.add_parser(name)
        item.add_argument("name")
        for field in KEY_FIELDS:
            if field != "name":
                item.add_argument("--" + field.replace("_", "-"))
        item.add_argument("--disabled", action="store_true")
    for name in ("list",):
        key.add_parser(name)
    for name in ("show", "enable", "disable", "rotate", "revoke", "delete"):
        item = key.add_parser(name)
        item.add_argument("id")
        if name in ("revoke", "delete"):
            item.add_argument("--yes", action="store_true", help="确认软删除")
    item = key.add_parser("update")
    item.add_argument("id")
    for field in KEY_FIELDS:
        item.add_argument("--" + field.replace("_", "-"))
    item.add_argument("--enabled", choices=("true", "false"))
    usage = commands.add_parser("usage")
    usage.add_argument("--days", type=int, default=7)
    usage.add_argument("--key-id")
    audit = commands.add_parser("audit")
    audit.add_argument("--limit", type=int, default=100)
    audit.add_argument("--key-id")
    commands.add_parser("status")
    commands.add_parser("models")
    commands.add_parser("doctor")
    tunnel = commands.add_parser("tunnel").add_subparsers(dest="tunnel_action", required=True)
    show = tunnel.add_parser("show")
    show.add_argument("--probe", action="store_true")
    render = tunnel.add_parser("render")
    render.add_argument("--print-path", action="store_true",
                        help="仅显示输出路径，不写入")
    render.add_argument("--force", action="store_true", help="确认覆盖已有 npc.conf")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "init":
            return init_command(args)
        if args.command == "doctor":
            return doctor(args)
        if args.command == "tunnel" and args.tunnel_action == "render":
            return tunnel_render(args)
        client = AdminClient(args)
        if args.command == "tunnel":
            emit(client.request("GET", "/api/tunnel?probe=%d" % int(args.probe)), args)
        elif args.command in ("status", "models"):
            emit(client.request("GET", "/api/" + args.command), args)
        elif args.command == "usage":
            emit(client.request("GET", "/api/usage?days=%d&key_id=%s" % (
                args.days, urllib.parse.quote(args.key_id or ""))), args)
        elif args.command == "audit":
            emit(client.request("GET", "/api/audit?limit=%d&key_id=%s" % (
                args.limit, urllib.parse.quote(args.key_id or ""))), args)
        elif args.action in ("add", "create"):
            payload = key_payload(args)
            if args.disabled:
                payload["enabled"] = False
            emit(client.request("POST", "/api/keys", payload), args)
        elif args.action == "list":
            emit(client.request("GET", "/api/keys"), args)
        elif args.action == "show":
            emit(client.request("GET", "/api/keys/" + urllib.parse.quote(args.id)), args)
        elif args.action == "update":
            payload = key_payload(args)
            if not payload:
                raise ValueError("update 至少需要一个待修改字段")
            emit(client.request("PATCH", "/api/keys/" + args.id, payload), args)
        elif args.action in ("enable", "disable", "rotate"):
            emit(client.request("POST", "/api/keys/%s/%s" % (args.id, args.action)), args)
        elif args.action in ("revoke", "delete"):
            if not args.yes:
                raise ValueError("软删除需要 --yes 确认")
            emit(client.request("DELETE", "/api/keys/" + args.id), args)
        return 0
    except (ValueError, OSError, config_module.ConfigError) as exc:
        print("错误: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
