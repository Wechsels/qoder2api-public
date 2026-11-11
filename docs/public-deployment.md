# qoder2api-public 部署手册

本手册按 v1.2 请求链部署：**Linux VPS 运行 Caddy+nps；Windows 或 Linux
内网机运行 qoder2api、公网 gateway 和 npc**。只支持一个内网实例，用户数少于 5。

> 并行开发说明：`qoder_public_runtime.py` 统一运行入口交付后，使用 §4 的
> 启停命令。隧道下载、初始化和配置渲染脚本已有；部署前先按开发文档
> 运行测试，不要把开发中的工作树直接当生产版本。

## 1. 准备

| 项 | 要求 |
|---|---|
| 域名 | 例如 `api.example.com`，A/AAAA 记录指向 VPS；AAAA 必须也确实可用 |
| VPS | Linux amd64/arm64、Python 3.9+、Caddy 2、nps v0.26.10 |
| 内网机 | Windows/Linux、Python 3.9+、可访问 Qoder 的 qoder2api 账号 |
| 端口 | VPS 入站 80、443、8024；SSH 按管理员策略；无其他 public 端口 |
| 发布校验 | 管理员独立取得的 nps 资产 SHA-256；Caddy 固定版本及官方校验值 |

不要复用示例中的密码、Key、域名或摘要。nps v0.26.10 官方 release 没有
SHA-256 清单，下载脚本故意要求管理员提供 `--sha256`。若无法独立核验，
停止部署，不要用“跳过校验”代替。

VPS 防火墙只开放上述端口；能限制 8024 到内网出口 IP 时应限制。
`127.0.0.1:18080`、`127.0.0.1:8080`、内网的 8790/8791/8792 都不能对公网开放。

## 2. Linux VPS：nps 与私有隧道

以下假设完整分支 checkout 在 `/opt/qoder2api-public/src`。也可以安全地复制
同一版本的脚本、`scripts/` 和 `tunnel/` 到该位置；不要复制本机
`public-data/`。

```sh
cd /opt/qoder2api-public/src
DOMAIN=api.example.com
VPS_IP=203.0.113.10

# 目录必须不存在或是本用途的新目录；摘要是所选 server 资产的值。
sudo python3 scripts/fetch-nps.py \
  --platform linux --arch amd64 --role server \
  --sha256 '<ADMIN_VERIFIED_SERVER_ASSET_SHA256>' \
  --dest /opt/qoder2api-public/nps-dist

# 只离线准备文件；已存在 /etc/nps 时拒绝覆盖，先按 §6 迁移。
sudo scripts/setup-nps-server.sh /opt/qoder2api-public/nps-dist "$DOMAIN"
```

编辑 `/etc/nps/conf/nps.conf`：把 `<VPS_IP>` 替换为 VPS 实际地址，设置随机
管理用户名和强口令。保留 `http_proxy_ip=127.0.0.1`、
`http_proxy_port=18080`、`https_proxy_port=`、`web_ip=127.0.0.1`、
`web_port=8080`。不要设置 `public_vkey`、`auth_key` 或 `auth_crypt_key`。

nps 尚未启动；已有安装则先 `systemctl stop qoder-nps`。备份后显式离线初始化：

```sh
sudo install -d -m 0700 /root/nps-backup
sudo cp -a /etc/nps/conf /root/nps-backup/conf-before-qoder

sudo python3 qoder_public_tunnel.py prepare-server \
  --nps-root /etc/nps --domain "$DOMAIN" \
  --client-id 1 --host-id 1 \
  --vkey-output /root/npc-vkey.txt --yes-stopped

sudo cp tunnel/qoder-nps.service.example /etc/systemd/system/qoder-nps.service
sudo systemctl daemon-reload
sudo systemctl enable --now qoder-nps
sudo systemctl status qoder-nps --no-pager
sudo journalctl -u qoder-nps -n 50 --no-pager
```

`prepare-server` 生成一个 `Crypt=true` 的私有 client 和一个固定 host，
host 目标名为 `api`，与 npc 配置段一致。它**不是管理 API 调用**，所以 nps
必须停止。已有 ID/域名冲突时，检查现有配置并选未使用 ID，不要覆盖。

将 `/root/npc-vkey.txt` 通过 `scp`/密码管理器私密传到内网管理员，权限
0600；确认内网保存后删除 VPS 副本。它不是给用户使用的 API Key。

## 3. Linux VPS：Caddy

从 Caddy 官方固定版本 release 获取二进制/软件包并按官方校验值验证。
不要执行未固定版本的 `curl | sh`。使用私密 vkey 渲染三份配置：

```sh
cd /opt/qoder2api-public/src
sudo python3 qoder_public_tunnel.py render \
  --domain "$DOMAIN" --server "$VPS_IP" \
  --vkey-file /root/npc-vkey.txt --output-dir /root/qoder-render

sudo cp /root/qoder-render/Caddyfile /etc/caddy/Caddyfile
sudo caddy fmt --overwrite /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl status caddy --no-pager
```

非 systemd 部署使用 `caddy run --config /etc/caddy/Caddyfile`，保持该前台
进程受服务管理器看护。首次签发需要公网 DNS 和 80/443 可达；Caddy 自动
续期，不需要把证书复制给 nps 或内网机。检查：

```sh
sudo ss -lntp | grep -E ':(80|443|8024|18080|8080)\b'
curl -fsS -o /dev/null -w '%{http_code}\n' "https://$DOMAIN/"   # 预期 404
```

Caddy 仅转发五条公网 API 路径，覆盖源 IP 头；nps 不终止 TLS。

## 4. 内网机：服务与 Key

### 4.1 初始化和 npc

先按上游文档准备 qoder2api 账号，再从分支根目录运行：

```sh
python qoder_public_cli.py init
```

Windows PowerShell 用 `python`；Linux 可用 `python3`。管理密码只显示一次，
立即存入密码管理器。`public-data/config.json` 包含内部 Key 和管理散列，
不得提交或发给用户。编辑其中 `nps` 节：`server_addr=<VPS_IP>:8024`、
`domain=<API_DOMAIN>`、私有 client 的 `vkey`、`mode=domain`、`crypt=true`。

用刚才私密保存的 vkey 在内网机渲染：

```sh
python qoder_public_tunnel.py render --domain <API_DOMAIN> --server <VPS_IP> \
  --vkey-stdin --output-dir <新建私密目录>
```

`--vkey-stdin` 隐藏输入；也可使用 `--vkey-file <私密路径>`。只取生成的
`npc.conf`，不要把这份含秘密的文件提交到仓库。

Linux 下载并安装 client：

```sh
python3 scripts/fetch-nps.py --platform linux --arch amd64 --role client \
  --sha256 '<ADMIN_VERIFIED_CLIENT_ASSET_SHA256>' \
  --dest /opt/qoder2api-public/npc-dist
sudo install -D -m 0755 /opt/qoder2api-public/npc-dist/npc /opt/qoder2api-public/npc/npc
sudo install -D -m 0600 <新建私密目录>/npc.conf /etc/qoder2api-public/npc.conf
```

统一运行器交付后由它启动 npc；也可使用随附的
`tunnel/qoder-npc.service.example` 独立看护，**两者不要同时管理同一个 npc**。

Windows 下载 `--platform windows --arch amd64 --role client`，将解压出的
`npc.exe` 放到仅管理员可写目录。使用
`npc.exe -config=C:\绝对\私密路径\npc.conf -log=stdout -log_level=4`。
用 `icacls` 仅授予服务账户读取和管理员完全控制。完整分支由统一运行器
启动，不携带 nps 管理台口令。

### 4.2 统一运行入口

`qoder_public_runtime.py` 交付后，标准操作是：

```sh
# Windows PowerShell
python qoder_public_runtime.py start
python qoder_public_runtime.py status
python qoder_public_runtime.py stop

# Linux 将上面的 python 替换为 python3。restart 用于统一受控重启。
```

启动顺序固定为 qoder2api:8790 → gateway:8791 + 管理:8792 → npc。
启动失败应非零退出并报告 `runtime.log`/`npc.log`；不要把“进程已创建”
当成就绪。管理页面仅 `http://127.0.0.1:8792`。

手工诊断上游时，使用 `public-data/config.json` 中的内部 Key，而不是用户 Key：

```sh
python qoder_proxy.py --host 127.0.0.1 --port 8790 --api-key '<INTERNAL_KEY>'
curl http://127.0.0.1:8790/health
```

正常生产操作不要同时手工启动上游和运行器，避免重复实例。

### 4.3 创建用户和远程管理

在本机或 SSH 端口转发后创建两个以内的测试用户（实际总数少于 5）：

```sh
python qoder_public_cli.py key create alice --owner alice --rpm 30 --max-concurrent 2
python qoder_public_cli.py key list
python qoder_public_cli.py key update k_xxx --daily-tokens 1000000 --allowed-models 'qwen*'
python qoder_public_cli.py key disable k_xxx
python qoder_public_cli.py key enable k_xxx
python qoder_public_cli.py key rotate k_xxx
python qoder_public_cli.py key delete k_xxx --yes
```

明文只在 create/rotate 输出一次。列表使用 `id`，不是 Key 前缀。远程管理：

```sh
ssh -N -L 8792:127.0.0.1:8792 <内网机SSH>
ssh -N -L 8080:127.0.0.1:8080 <VPS_SSH>
```

不要把 8792 或 8080 改成 `0.0.0.0`，也不要给 CLI 增加命令行明文密码。

## 5. 公网验收

使用一台**不在内网机上**的客户端和真实公网 DNS：

```sh
API_URL=https://api.example.com
curl -i "$API_URL/v1/models"
# 无 Key：401，而不是上游数据或 200。

curl -i "$API_URL/v1/models" -H 'Authorization: Bearer <USER_QDP_KEY>'
curl -N "$API_URL/v1/chat/completions" \
  -H 'Authorization: Bearer <USER_QDP_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{"model":"<已配置模型>","messages":[{"role":"user","content":"hello"}],"stream":true}'
```

分别验证第二个 Key、禁用后 401/403、轮换后旧 Key 失效、非白名单路径和
`/v1/models/refresh` 均不可达。停止 npc 应失败；恢复后应自动重连。检查
管理用量、失败审计和 VPS/内网日志，不记录完整 Key 或提示词。

## 6. 升级、备份、回滚

1. `stop` 内网统一运行器；备份 `public-data/` 与私密 npc.conf。
2. VPS 停止 nps 后备份 `/etc/nps/conf/`、`/etc/caddy/Caddyfile`。
3. 拉取经过审查的代码，跑普通测试；更新固定二进制时重新核验新资产摘要。
4. 修改 nps 离线 JSON 前必须停止 nps 并备份；不要并行运行初始化工具。
5. 启动、检查状态和外部 API；回滚代码/配置后重启。不要恢复旧 Key 明文，
   丢失时轮换。

仓库只包含代码和无秘密模板；`public-data/`、`run/`、下载二进制、证书和
生成的配置均不入库。
