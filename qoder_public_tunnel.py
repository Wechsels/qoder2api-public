"""Generate qoder2api-public tunnel files without nps management API access."""

import argparse
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import sys


ROOT = Path(__file__).resolve().parent
TEMPLATES = ROOT / "scripts"
DOMAIN = re.compile(r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def domain(value):
    value = value.strip().rstrip(".").encode("idna").decode("ascii").lower()
    if not DOMAIN.fullmatch(value):
        raise argparse.ArgumentTypeError("domain must be a DNS name without scheme, port, or path")
    return value


def secret(value):
    if not 16 <= len(value) <= 256 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("vkey must be 16-256 ASCII letters, digits, '_' or '-'")
    return value


def write(path, text, overwrite=False, mode=0o600):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("refusing non-regular output path: %s" % path)
    if path.exists() and not overwrite:
        raise ValueError("refusing to overwrite %s (use --force)" % path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if overwrite:
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(path), flags, mode)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    if os.name != "nt":
        path.chmod(mode)


def load_json(path):
    try:
        with path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict) or any(not key.isdigit() for key in value):
        raise ValueError("%s must be an nps JSON object keyed by numeric IDs" % path)
    return value


def render(args):
    if args.vkey_file:
        vkey = secret(Path(args.vkey_file).read_text(encoding="utf-8").strip())
    elif not sys.stdin.isatty():
        vkey = secret(sys.stdin.readline().strip())
    else:
        vkey = secret(getpass.getpass("Private nps client vkey (input hidden): "))
    values = {
        "API_DOMAIN": args.domain,
        "VPS_IP": args.server,
        "NPS_CLIENT_VKEY": vkey,
    }
    outputs = [Path(args.output_dir) / name for name in ("Caddyfile", "nps.conf", "npc.conf")]
    if not args.force:
        conflicts = [str(path) for path in outputs if path.exists()]
        if conflicts:
            raise ValueError("refusing to overwrite: %s" % ", ".join(conflicts))
    for name, output in zip(("Caddyfile", "nps.conf", "npc.conf"), outputs):
        text = (TEMPLATES / (name + ".example")).read_text(encoding="utf-8")
        for key, value in values.items():
            text = text.replace("<%s>" % key, value)
        write(output, text, args.force)
    print("Wrote Caddyfile, nps.conf, and npc.conf to %s" % Path(args.output_dir).resolve())
    print("Windows ACL: restrict npc.conf to the service account; do not commit these files.")


def prepare_server(args):
    root = Path(args.nps_root)
    clients_path, hosts_path = root / "conf/clients.json", root / "conf/hosts.json"
    clients, hosts = load_json(clients_path), load_json(hosts_path)
    client_id, host_id = str(args.client_id), str(args.host_id)
    if client_id in clients or host_id in hosts:
        raise ValueError("client or host ID already exists; choose unused IDs")
    if any(item.get("Host") == args.domain for item in hosts.values()):
        raise ValueError("domain already has an nps host record")
    if not clients_path.is_file() or not hosts_path.is_file():
        raise ValueError("nps conf/clients.json and conf/hosts.json must already exist")
    vkey = secret(args.vkey_file and Path(args.vkey_file).read_text(encoding="utf-8").strip()
                  or secrets.token_urlsafe(32))
    if not args.print_vkey:
        write(Path(args.vkey_output), vkey + "\n")
    # These field names and JSON object layout come from nps v0.26.10 lib/file.
    clients[client_id] = {
        "Cnf": {"U": "", "P": "", "Compress": True, "Crypt": True},
        "Id": args.client_id, "VerifyKey": vkey, "Addr": "",
        "Remark": "qoder2api-public", "Status": True, "IsConnect": False,
        # nps 0.26.10 dereferences Rate/Flow during forwarding; null values
        # panic on the first proxied request.
        "Rate": {"Limit": 100000, "Curve": 100000},
        "RateLimit": 0,
        "Flow": {"FlowLimit": 0, "ExportFlow": 0, "InletFlow": 0},
        "NowConn": 0, "MaxConn": 0, "WebUserName": "", "WebPassword": "",
        "ConfigConnAllow": True, "MaxTunnelNum": 0, "Version": "",
    }
    hosts[host_id] = {
        "Id": args.host_id, "Host": args.domain, "HeaderChange": "", "HostChange": "",
        "Location": "/", "Remark": "qoder2api-public", "Scheme": "http",
        "CertFilePath": "", "KeyFilePath": "", "NoStore": False, "IsClose": False,
        "Flow": {"FlowLimit": 0, "ExportFlow": 0, "InletFlow": 0},
        "Client": {"Id": args.client_id},
        # TargetStr 是 host:port（与 e2e 实测一致）；路由经 npc 的 [api] host 隧道
        "Target": {"TargetStr": "127.0.0.1:8791", "LocalProxy": False},
    }
    pending = [(path, value, path.with_name(path.name + ".qoder-tmp"))
               for path, value in ((clients_path, clients), (hosts_path, hosts))]
    if any(temporary.exists() for _, _, temporary in pending):
        raise ValueError("an nps JSON temporary file already exists; inspect it before retrying")
    for path, value, temporary in pending:
        write(temporary, json.dumps(value, ensure_ascii=True, indent=2) + "\n")
    for path, _, temporary in pending:
        os.replace(str(temporary), str(path))
    print("Prepared private client %s and host %s. Stop nps before this operation." % (client_id, host_id))
    if args.print_vkey:
        print(vkey)
    else:
        print("Vkey written to %s; transfer it privately and then delete the server copy." % args.vkey_output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    render_parser = commands.add_parser("render", help="render Caddy, nps, and npc configuration")
    render_parser.add_argument("--domain", required=True, type=domain)
    render_parser.add_argument("--server", required=True, help="VPS IPv4 address or DNS name; bridge port is 8024")
    render_parser.add_argument("--output-dir", required=True)
    source = render_parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--vkey-stdin", action="store_true")
    source.add_argument("--vkey-file")
    render_parser.add_argument("--force", action="store_true")
    render_parser.set_defaults(func=render)

    prepare = commands.add_parser("prepare-server", help="offline: precreate a private nps client and host")
    prepare.add_argument("--nps-root", required=True, help="nps runtime root containing conf/")
    prepare.add_argument("--domain", required=True, type=domain)
    prepare.add_argument("--client-id", type=int, default=1)
    prepare.add_argument("--host-id", type=int, default=1)
    prepare.add_argument("--vkey-file", help="optional existing private vkey; otherwise generate one")
    prepare.add_argument("--vkey-output", default="npc-vkey.txt")
    prepare.add_argument("--print-vkey", action="store_true", help="explicitly print the vkey once")
    prepare.add_argument("--force", action="store_true")
    prepare.add_argument("--yes-stopped", action="store_true",
                         help="confirm nps is stopped before offline JSON changes")
    prepare.set_defaults(func=prepare_server)
    args = parser.parse_args(argv)
    try:
        if getattr(args, "client_id", 1) < 1 or getattr(args, "host_id", 1) < 1:
            raise ValueError("client and host IDs must be positive")
        if args.command == "prepare-server":
            if not args.yes_stopped:
                raise ValueError("stop nps, back up conf/, then repeat with --yes-stopped")
            if args.print_vkey and args.vkey_file:
                raise ValueError("do not print an existing vkey; omit --print-vkey")
        args.func(args)
    except (OSError, ValueError) as exc:
        parser.exit(1, "error: %s\n" % exc)


if __name__ == "__main__":
    main()
