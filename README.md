<h1 align="center">Qoder2API-Hub</h1>

<p align="center">
  把 Qoder 国内版（qoder.com.cn）与国际版（qoder.com）封装成 OpenAI / Anthropic 兼容接口的多账号网关
</p>

<p align="center">
  <a href="https://github.com/shuishuipingan/qoder2api-hub/releases"><img src="https://img.shields.io/github/v/release/shuishuipingan/qoder2api-hub?style=flat-square" alt="Release"></a>
  <a href="https://github.com/shuishuipingan/qoder2api-hub/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/shuishuipingan/qoder2api-hub/tests.yml?branch=main&style=flat-square&label=tests" alt="Tests"></a>
  <img src="https://img.shields.io/badge/Python-3.9+-blue?style=flat-square" alt="Python 3.9+">
  <img src="https://img.shields.io/badge/dependencies-zero-ff69b4?style=flat-square" alt="Zero dependencies">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-green?style=flat-square" alt="MIT"></a>
</p>

![网关总览](docs/img/dashboard.png)

---

## 两种用法

- **只在自己电脑上用**：把 Qoder 账号变成本机的 `http://127.0.0.1:8790/v1`，给自己的客户端用。跳到 [只想本机用](#只想本机用不公网)。
- **发布到公网给别人用**（本 README 重点）：变成 `https://你的域名/v1`，发给朋友（少于 5 人），**每人一把独立的 API Key**，你可以随时停用、轮换、限量、看用量。

公网版叫 **qoder2api-public**，在 `feat/public-gateway-nps` 分支上。它不改动反代本体，是在前面加了一层独立网关 + 内网穿透。下面从零开始，照着做即可。

## 公网部署教程（小白版）

### 先看懂这张图

```text
朋友的客户端
   │  https://你的域名/v1  +  API Key
   ▼
你的 VPS（一台有公网 IP 的 Linux 服务器）
   Caddy:443 ──▶ nps:18080 ──┐
   （自动 HTTPS 证书）        │ 加密隧道 :8024
                             ▼
你的内网机（家里/公司电脑，运行 Qoder 账号）
   npc ──▶ 公网网关:8791 ──▶ qoder2api:8790
                管理面板:8792（只有本机能开）
```

一句话：**VPS 只负责收公网请求并通过隧道转回你家；真正干活、存账号的是你的内网机。** 管理面板和 nps 后台永远不暴露到公网。

### 你需要准备

| 东西 | 说明 |
|---|---|
| 一个域名 | 比如 `api.mydomain.com`，**你自己能改 DNS 解析**。没有就买一个，或用 DuckDNS 等免费子域名 |
| 一台 VPS | Linux（amd64/arm64），有公网 IP，装好 Python 3.9+、Caddy 2 |
| 一台内网机 | Windows 或 Linux，Python 3.9+，能正常登录 Qoder |
| Qoder 账号 | 在内网机上用，先保证本机版能跑通 |
| 放行端口 | VPS 安全组/防火墙只开 **80、443、8024**（8024 能限制到你家出口 IP 更好） |

> 教程里所有 `api.example.com` 都是**占位符**，下面第 2 步会告诉你换成自己的。所有 `<...>` 尖括号内容都要替换成你自己的值，别原样照抄。

### 第 1 步：内网机先跑通本机版

公网版是叠加层，先确保本机能用。在内网机仓库根目录：

```bash
git clone https://github.com/shuishuipingan/qoder2api-hub.git
cd qoder2api-hub
git checkout feat/public-gateway-nps
```

按 [只想本机用](#只想本机用不公网) 启动并添加 Qoder 账号，浏览器开 `http://127.0.0.1:8790/` 能对话，就 OK。然后停掉它（公网版会自己拉起）。

### 第 2 步：定域名 + 加 DNS 解析（这就是"改 URL"的地方）

你的 API 地址长这样：`https://<你的域名>/v1`。**域名完全由你定**，整条链路没有任何写死的域名，只需要在 4 个地方保持一致：

1. **DNS**：到域名服务商后台，加一条 **A 记录**：`api.mydomain.com → 你的VPS公网IP`。等几分钟生效。
2. **VPS 的 nps**：第 3 步命令里的 `--domain`。
3. **VPS 的 Caddy**：第 4 步渲染时自动带入。
4. **内网机配置**：第 5 步 `config.json` 里的 `nps.domain`。

以后想换域名，改这 4 处 + DNS 即可，**代码一行都不用动**。

> 域名必须是你真实可控的（能加解析记录）。随便编一个 `example.example` 不会指向你的服务器，HTTPS 证书也签发不下来。

### 第 3 步：VPS 装 nps（隧道服务端）

把同一分支的代码放到 VPS，比如 `/opt/qoder2api-public/src`，然后：

```sh
cd /opt/qoder2api-public/src
DOMAIN=api.mydomain.com          # ← 换成你的域名
VPS_IP=203.0.113.10              # ← 换成你的 VPS 公网 IP

# 下载 nps 服务端（强制 SHA-256 校验，防投毒）
sudo python3 scripts/fetch-nps.py --platform linux --arch amd64 --role server \
  --sha256 '<你核验过的SERVER摘要>' --dest /opt/qoder2api-public/nps-dist

# 离线铺好文件（不会启动服务；已有 /etc/nps 会拒绝覆盖）
sudo sh scripts/setup-nps-server.sh /opt/qoder2api-public/nps-dist "$DOMAIN"
```

> **关于 SHA-256**：nps 官方没给校验清单，所以下载脚本要求你提供**自己独立核验过**的摘要，核验方法见 [tunnel/README.md](tunnel/README.md)。没有核验过的摘要就停下，不要跳过校验。

编辑 `/etc/nps/conf/nps.conf`：把 `<VPS_IP>` 换成真实 IP，设一个随机管理用户名和强口令。其余保持默认（`http_proxy_ip=127.0.0.1`、`web_ip=127.0.0.1` 等不要改成 0.0.0.0）。

确认 nps 没在运行，然后生成私有隧道凭证并启动：

```sh
sudo python3 qoder_public_tunnel.py prepare-server --nps-root /etc/nps \
  --domain "$DOMAIN" --vkey-output /root/npc-vkey.txt --yes-stopped

sudo cp tunnel/qoder-nps.service.example /etc/systemd/system/qoder-nps.service
sudo systemctl daemon-reload
sudo systemctl enable --now qoder-nps
sudo systemctl status qoder-nps --no-pager   # 看到 active (running) 就成功
```

`/root/npc-vkey.txt` 是隧道钥匙（**不是**给用户的 API Key）。用 scp 或密码管理器**私密**传到内网机，传完删掉 VPS 上的副本。

### 第 4 步：VPS 装 Caddy（HTTPS 入口）

从 Caddy 官方固定版本安装并校验（不要跑未固定版本的 `curl | sh`）。然后渲染配置：

```sh
sudo python3 qoder_public_tunnel.py render --domain "$DOMAIN" --server "$VPS_IP" \
  --vkey-file /root/npc-vkey.txt --output-dir /root/qoder-render

sudo cp /root/qoder-render/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile   # 输出 Valid configuration
sudo systemctl reload caddy
```

Caddy 会自动向 Let's Encrypt 申请并续期证书（需要 DNS 已生效、80/443 可达）。验证：

```sh
curl -fsS -o /dev/null -w '%{http_code}\n' "https://$DOMAIN/"   # 预期 404
```

**返回 404 就是链路通了**（根路径本来就不该有内容）。证书报错先看 DNS 是否生效、80/443 是否放行。

### 第 5 步：内网机接上隧道

回到内网机仓库根目录：

```bash
python qoder_public_cli.py init
```

**管理密码只显示一次，立刻存进密码管理器。** 然后编辑 `public-data/config.json`，找到 `nps` 节，把下面几个字段改成你的值（其余字段保持原样）：

```jsonc
"nps": {
  "server_addr": "203.0.113.10:8024",   // ← VPS IP:8024
  "domain": "api.mydomain.com",         // ← 你的域名
  "vkey": "第3步私密传来的vkey",
  "mode": "domain",
  "crypt": true
}
```

> 上面的 `//` 只是给你看的说明。`config.json` 是标准 JSON，**实际文件里不要保留任何 `//` 注释**，否则启动会报错。

下载 nps 客户端 npc（同样要核验摘要），把可执行文件放到 `public-data/tools/`：

```bash
# Windows：--platform windows --arch amd64 --role client，解压出 npc.exe
# Linux：  --platform linux   --arch amd64 --role client，解压出 npc
python scripts/fetch-nps.py --platform windows --arch amd64 --role client \
  --sha256 '<你核验过的CLIENT摘要>' --dest 某个临时目录
# 把 npc.exe（或 npc）放进 public-data/tools/
```

启动整套服务（**npc.conf 由运行器自动生成，不用手写**）：

```bash
# Windows
start-qoder-public.bat start
start-qoder-public.bat status

# Linux / macOS
./start-qoder-public.sh start
./start-qoder-public.sh status
```

启动顺序固定：qoder2api:8790 → 网关:8791 + 管理:8792 → npc。`status` 里三个进程都活着就成功。Linux 想开机常驻，用 `tunnel/qoder-public.service.example`（systemd）。

### 第 6 步：给朋友签发 API Key

```bash
python qoder_public_cli.py key add alice --rpm 30 --daily-tokens 1000000
```

输出里的 `qdp-...` 就是给 alice 的 Key，**只显示一次**，发给她。也可以开浏览器管理面板 **http://127.0.0.1:8792/**（只有内网机能开）点点鼠标完成。远程管理用 SSH 转发：`ssh -N -L 8792:127.0.0.1:8792 内网机`。

常用管理命令：

```bash
python qoder_public_cli.py key list             # 看所有 Key（用 id 操作）
python qoder_public_cli.py key disable <id>     # 停用；enable 恢复
python qoder_public_cli.py key rotate <id>      # 轮换，旧 Key 立即失效
python qoder_public_cli.py key revoke <id> --yes  # 删除
python qoder_public_cli.py usage --days 7       # 用量
python qoder_public_cli.py audit --limit 50     # 请求流水
```

### 第 7 步：客户端接入

把下面两样发给朋友：

- **Base URL**：`https://api.mydomain.com/v1`（换成你的域名）
- **API Key**：第 6 步签发的 `qdp-...`

| 客户端 | Base URL | 备注 |
|---|---|---|
| OpenAI 兼容（Cherry Studio、Chatbox、NextChat…） | `https://你的域名/v1` | 协议选 Chat Completions |
| Codex CLI | `https://你的域名/v1` | Responses API |
| Claude Code | `https://你的域名`（不带 `/v1`） | 用 `x-api-key` 头 |

自己先验一遍（在**不是内网机**的设备上）：

```bash
curl -i https://api.mydomain.com/v1/models
# 不带 Key → 401（对了，说明鉴权生效）

curl https://api.mydomain.com/v1/chat/completions \
  -H "Authorization: Bearer qdp-你的Key" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen3.8-Max","messages":[{"role":"user","content":"你好"}]}'
# 带 Key → 正常回复
```

模型名填 `/v1/models` 返回的 `id`。到这一步就全部完成了。

### 日常运维

```bash
start-qoder-public.bat status     # 看状态（Windows）
start-qoder-public.bat restart    # 重启
stop-qoder-public.bat             # 逆序停止 npc → 网关 → qoder2api
```

运行器有单实例锁、就绪检查（校验进程 PID）、崩溃有界退避重启。日志在 `public-data/` 下。

### 出问题看这里

| 现象 | 多半是 |
|---|---|
| `https://域名/` 不是 404 | DNS 没生效 / 80、443 没放行 / Caddy 没 reload |
| 证书签发失败 | 域名解析没指向 VPS，或 80 端口被占 |
| 带 Key 也 401 | Key 抄错、被停用，或内网机网关没起来 |
| 403 | Key 的模型白名单/IP 限制，或可信代理头不对 |
| 502 / 连不上 | npc 掉了，看 `status` 和 npc 日志 |

分层排查详见 [排障手册](docs/public-troubleshooting.md)。

> **安全边界**：nps 隧道开了 `crypt=true`，但 v0.26.10 的旧 TLS 不验证服务端证书身份，所以**不把它叫端到端加密**，VPS 仍属可信边界。公网只暴露 5 条 API 路径，面板、`/settings`、nps 后台全留内网。详见 [安全文档](docs/public-security.md)。

## 只想本机用（不公网）

需要 Python 3.9+ 或 Docker：

```bash
git clone https://github.com/shuishuipingan/qoder2api-hub.git
cd qoder2api-hub
```

| 平台 | 启动 | 停止 |
|---|---|---|
| Windows | 双击 `start-qoder-proxy.bat`（后台，日志 `qoder-proxy.log`） | `stop-qoder-proxy.bat` |
| Linux / macOS | `chmod +x *.sh && ./start-qoder-proxy.sh`（前台） | `Ctrl+C`，常驻用 `./stop-qoder-proxy.sh` |
| Docker | `docker compose up -d` | `docker compose down` |

启动后：API `http://127.0.0.1:8790/v1`，看板 `http://127.0.0.1:8790/`（默认密码 `admin`，首次登录请改）。在看板「账号」页 **+ 添加账号 (OAuth)** 完成授权即可。

客户端 Base URL 填 `http://127.0.0.1:8790/v1`（Claude Code 填 `http://127.0.0.1:8790`，不带 `/v1`），API Key 在看板「设置」生成；本机无 Key 时可留空。看板「智能体配置」覆盖 31 个客户端、13 类支持一键配置，详见 [客户端接入](docs/clients.md)。

> Docker 以局域网模式启动会强制 API Key，用 `docker compose logs qoder-proxy | grep -i "api key"` 查看。

## 特性

- **三种协议**：OpenAI Chat Completions、Responses API（Codex）、Anthropic Messages（Claude Code）
- **双区多账号**：国内/国际独立账号池，按模型自动选区；轮询、会话亲和、故障冷却换号
- **免客户端登录**：OAuth 设备授权、PAT 导入，或导入本机官方客户端凭证
- **模型与官方同源**：动态清单，含峰谷价、上下文窗口、思考档位
- **签到与保活**：自动领每日 Credits，定时刷新 Token
- **防风控**：出站身份对齐官方客户端，可选并发闸门与节奏控制
- **公网版加料**：按人分发 Key、启停/轮换/吊销、RPM/日配额/模型白名单、用量与审计、管理面板
- **零依赖**：只用 Python 标准库，另提供 amd64/arm64 Docker 镜像

## 文档

**公网版（qoder2api-public）**

| 文档 | 内容 |
|---|---|
| [部署手册](docs/public-deployment.md) | 本教程的详细版：VPS + 内网机从零到可用、升级回滚 |
| [设计](docs/design-public-gateway.md) | 架构、Key 模型、威胁模型、验收标准 |
| [安全](docs/public-security.md) | 加固清单、密钥生命周期、吊销演练 |
| [对外 API](docs/public-api.md) | 端点、鉴权、错误码、客户端接入 |
| [排障](docs/public-troubleshooting.md) | 分层排查与常见坑 |
| [开发](docs/public-development.md) / [测试](docs/public-testing.md) | 文件边界、测试层级、CI 与验收 |

**本机版**

| 文档 | 内容 |
|---|---|
| [部署](docs/deployment.md) | 本机 / 局域网 / Docker、防火墙、出口代理、升级 |
| [配置参考](docs/configuration.md) | 命令行、环境变量、API Key、模型闸门、限额 |
| [账号与签到](docs/accounts.md) | 添加账号、凭证生命周期、签到规则 |
| [客户端接入](docs/clients.md) | 各协议接入、智能体一键配置 |
| [模型](docs/models.md) | 来源、命名、双区路由、峰谷价、思考档位 |
| [接口参考](docs/api.md) | HTTP 端点与鉴权 |
| [故障排查](docs/troubleshooting.md) | 常见问题、诊断脚本、已知限制 |
| [风控与排队](docs/risk-control-and-queueing.md) / [处置手册](docs/risk-control-playbook.md) | 风控维度、处置等级、落地方案 |
| [实现原理](docs/internals.md) | 请求链路、设备指纹、容错重试 |
| [开发与测试](docs/development.md) | 模块结构、测试、维护脚本 |
| [更新日志](CHANGELOG.md) | 各版本变更 |

**测试**（不需要真实账号或公网）：`python tests/run_all.py public` 跑 7 套件 226 项检查；真实 nps 回环 e2e 是独立入口，见 `tests/e2e/README.md`。

## 🔗 相关：多平台合集面板

如果你同时在用别的 AI 编程平台账号，可以看看这块合集面板：

**[WorkBuddy2API Panel](https://github.com/xu20060420fan-cell/workbuddy2api-trae-qoder-minimax-panel)**

同样支持 **Qoder**（每日额度按账号轮转领取、凭证自动续期），另外还统管 **WorkBuddy / CodeBuddy / Trae / MiniMax / 豆包 Doubao / 华为云 CodeArts 码道**，全部账号共用一个 OpenAI 兼容网关。

## 赞助商

> 目前还没有赞助商。想出现在这里，联系 **zhang8586201314@gmail.com**

---

## 致谢

- [mmqz/cpa-multi-plugins](https://github.com/mmqz/cpa-multi-plugins)：双区常量表、OAuth 设备授权与 PAT 交换、COSY 签名与自定义 Base64、签到与保活排程
- [Liki4/qodercli2api](https://github.com/Liki4/qodercli2api)：OAuth 与推理协议的逆向记录（PKCE、deviceToken / jobToken、SSE 信封）
- [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)：设备指纹派生（`derive_id`）、整点排程、DeepSeek 思维链回填
- WorkBuddy2API-Hub：本项目架构、看板交互与功能矩阵的蓝本

## 免责声明

本项目是非官方的自托管网关，仅供技术研究，以及个人在私有环境中使用自己合法授权的账号。本项目不提供任何账号或额度。请遵守 Qoder 官方服务条款，不得用于商业转售、恶意并发等滥用行为。

[MIT License](LICENSE)