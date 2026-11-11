#!/bin/sh
# Offline initialization only: never starts, stops, or reloads a service.
set -eu
[ "$(id -u)" -eq 0 ] || { echo "Run as root." >&2; exit 1; }
[ "$#" -eq 2 ] || { echo "Usage: $0 <verified-server-extract-dir> <API_DOMAIN>" >&2; exit 2; }
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SRC=$(CDPATH= cd -- "$1" && pwd)
DOMAIN=$2
[ -f "$SRC/nps" ] && [ -d "$SRC/web/views" ] && [ -d "$SRC/web/static" ] || {
    echo "Expected verified nps server files in $SRC." >&2; exit 1; }
[ ! -e /etc/nps ] || { echo "Refusing to overwrite existing /etc/nps; back it up and initialize manually." >&2; exit 1; }
getent passwd nps >/dev/null || useradd --system --home-dir /nonexistent --shell /usr/sbin/nologin nps
install -d -m 0755 /opt/qoder2api-public/nps /etc/nps/conf
install -m 0755 "$SRC/nps" /opt/qoder2api-public/nps/nps
cp -R "$SRC/web" /etc/nps/web
echo "1. Set the real VPS_IP and administrator credentials in /etc/nps/conf/nps.conf."
cp "$ROOT/scripts/nps.conf.example" /etc/nps/conf/nps.conf
for file in clients.json hosts.json tasks.json; do printf '{}\n' >"/etc/nps/conf/$file"; done
chmod 0600 /etc/nps/conf/*.json /etc/nps/conf/nps.conf
chown -R nps:nps /etc/nps /opt/qoder2api-public/nps
echo "2. With nps stopped, run:"
echo "   python3 $ROOT/qoder_public_tunnel.py prepare-server --nps-root /etc/nps --domain $DOMAIN --vkey-output /root/npc-vkey.txt --yes-stopped"
echo "3. Copy tunnel/qoder-nps.service.example to /etc/systemd/system/qoder-nps.service."
echo "4. systemctl daemon-reload && systemctl enable --now qoder-nps"
echo "5. systemctl status qoder-nps; journalctl -u qoder-nps -n 50"
