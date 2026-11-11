# Caddy + nps initialization

No template contains a usable credential. Replace `<API_DOMAIN>` and `<VPS_IP>`; never use literal placeholder text as a password.

## 1. Verified release files

Obtain SHA-256 independently from the release artifact, then pass it explicitly. The upstream v0.26.10 release has no checksum manifest, so this tool does not invent or auto-trust a digest. One invocation fetches both the server and the client archive for a platform and verifies both digests before extracting:

```sh
python3 scripts/fetch-nps.py --version v0.26.10 --platform linux_amd64 \
  --sha256-nps <VERIFIED_LINUX_SERVER_SHA256> \
  --sha256-npc <VERIFIED_LINUX_CLIENT_SHA256> --dest ./downloads/linux
python3 scripts/fetch-nps.py --version v0.26.10 --platform windows_amd64 \
  --sha256-nps <VERIFIED_WINDOWS_SERVER_SHA256> \
  --sha256-npc <VERIFIED_WINDOWS_CLIENT_SHA256> --dest ./downloads/windows
```

`--dest` must be an empty directory outside the repository. Extraction occurs only after the digest matches. The server package requires its `web/views` and `web/static` resources. The client requires only `npc`/`npc.exe`.

## 2. Initialize the Linux VPS

Ensure nps is stopped. In a fresh installation, run `sudo sh scripts/setup-nps-server.sh ./downloads/server <API_DOMAIN>`. It prepares files only; it does not start a service. Set a random administrator password in `/etc/nps/conf/nps.conf`.

Back up `/etc/nps/conf`, then precreate the private client and host while nps is stopped:

```sh
sudo python3 qoder_public_tunnel.py prepare-server --nps-root /etc/nps \
  --domain <API_DOMAIN> --vkey-output /root/npc-vkey.txt --yes-stopped
```

Transfer `/root/npc-vkey.txt` privately, restrict its ACL, and remove the server copy after initializing npc. For an existing installation, choose unused numeric `--client-id` and `--host-id`. This is offline nps v0.26.10 JSON generation, not an authenticated management API call.

Install `tunnel/qoder-nps.service.example` as `/etc/systemd/system/qoder-nps.service`, then explicitly run `systemctl daemon-reload` and `systemctl enable --now qoder-nps`. Check `systemctl status qoder-nps` and `journalctl -u qoder-nps`.

## 3. Initialize the internal host

```sh
python3 qoder_public_tunnel.py render --domain <API_DOMAIN> --server <VPS_IP> \
  --vkey-file /secure/path/npc-vkey.txt --output-dir ./generated
```

Omit `--vkey-file` and use `--vkey-stdin` for hidden interactive input. Keep `generated/npc.conf` private.

The supervisor on the internal host owns startup: `python3 qoder_public_runtime.py start` (or `./start-qoder-public.sh start`) launches loopback qoder2api, the public gateway with its admin plane, and npc whenever `nps.server_addr` is configured. Put the npc binary at `public-data/tools/npc` (or point `QDP_NPC_BIN` at it) — never inside the repository tree.

Linux service: install `tunnel/qoder-public.service.example` as `/etc/systemd/system/qoder-public.service` (it runs `qoder_public_runtime.py run`). For a standalone npc unit instead of supervisor-managed npc, install the private config as `/etc/qoder2api-public/npc.conf` (root:root, 0600), put `npc` at `/opt/qoder2api-public/npc/npc`, and install `tunnel/qoder-npc.service.example`; then run `systemctl daemon-reload` and `systemctl enable --now qoder-npc`. Windows: run `start-qoder-public.bat start`; the supervisor starts `npc.exe` itself from the rendered config. No nps admin credential is needed.

## 4. Caddy

Install the rendered `Caddyfile` using the normal Caddy deployment procedure, validate with `caddy validate --config /path/Caddyfile`, then reload Caddy. Only the five API paths are forwarded. Caddy terminates TLS on 443 and proxies to nps on `127.0.0.1:18080`.

The gateway must trust only its nps loopback peer and normalize the forwarded chain. Caddy cannot remove headers added by nps afterward. Publicly expose only 80/443 and the firewall-restricted bridge 8024; management remains `127.0.0.1:8080`.
