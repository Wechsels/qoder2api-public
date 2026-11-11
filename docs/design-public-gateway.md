# qoder2api-public 设计文档

> 版本：v1.2（已定架构与实施约束）· 2026-11-11 · 分支 `feat/public-gateway-nps`
>
> v1.0/v1.1 的已替代决策保留在 §10。未交付组件在本文明确标为“待实现”。

## 0. 方案与固定请求链

保留 qoder2api 作为本机上游，在其前增加独立的 Python 公网网关。Caddy 在
VPS 管理 HTTPS，nps 只提供 HTTP 域名隧道；每个用户持有独立的 `qdp-...` Key。

```text
用户（少于 5 人）
  │ https://<API_DOMAIN>/v1 + qdp-...
  ▼
Linux VPS
  Caddy :443 → nps 127.0.0.1:18080
  nps bridge :8024；管理台 127.0.0.1:8080（SSH 转发）
  │ 私有 client + 预建 host，client.Crypt=true
  ▼
Windows / Linux 内网机
  npc → gateway 127.0.0.1:8791 → qoder2api 127.0.0.1:8790
  gateway 管理页面和 CLI → 127.0.0.1:8792（仅本机）
```

固定约束：

- VPS 只公开 `80/tcp`（ACME/跳转）、`443/tcp` 和 `8024/tcp`；`18080`、
  `8080` 仅回环。SSH 按现有运维策略保护。
- nps **不直接终止 API 的 TLS**，不启用 `https_proxy_port`，不公开 TCP 隧道端口。
- 不设置 `public_vkey`；用离线脚本预建一个私有 client 和一个固定 host。
  npc 只能使用分配的 vkey，不能任意新增隧道、端口或目标。
- `8790/8791/8792` 均仅回环；npc 是内网机唯一对外主动连接。
- 公网 Key 鉴权在 gateway，不在 nps。上游 qoder2api 使用独立的内部 Key。
- 公网白名单只有 `/v1/models`、`/v1/chat/completions`、`/v1/responses`、
  `/v1/messages`、`/v1/messages/count_tokens`。不包括 `/v1/models/refresh`、
  原面板、`/settings`、nps 管理端点或通用转发。
- 保持 Python 3.9+ 和标准库；v1.2 不引入数据库、多节点、计费或自助注册。

## 1. 目标、边界与验收

| 目标 | 验收 |
|---|---|
| Windows/Linux 内网部署 | 同一 `qoder_public_runtime.py`（待实现）统一启停；系统只提供薄入口 |
| 公网 URL 访问内网模型 | 外部经真实域名通过 Caddy→nps→npc 调用；本机 e2e 不冒充公网验收 |
| 按用户独立管理 | 两个 Key 分别验证启停、轮换、过期、RPM、并发、配额、模型/IP 策略及用量 |
| 默认拒绝 | 无/错 Key、非白名单路径、非法区域、伪造转发头均不触达上游 |
| 小团队可维护 | 单内网实例、单 VPS、JSON/JSONL 数据、日志轮转、可备份恢复 |

安全接受的是“小团队 + 受控 VPS”的边界，不是军事级、零信任或端到端
加密承诺。nps v0.26.10 的隧道加密与旧 TLS 风险见 §5 和安全文档。

## 2. 组件与交付状态

| 组件 | 职责 | 状态（本分支已交付） |
|---|---|---|
| `qoder_public_config.py` | 校验配置、秘密和环境覆盖 | 已交付；含 fail-closed 校验 |
| `qoder_public_gateway.py` | 鉴权、限流、配额、转发、审计 | 已交付；含每 Key 并发租约与 499 审计 |
| `qoder_public_keys.py` / `qoder_public_meter.py` | Key 存储、usage 解析和持久化 | 已交付；轮转文件也参与重启回放 |
| `qoder_public_tunnel.py` + `scripts/` 模板 | 预建私有 nps 资源、渲染配置、下载校验 | 已交付 |
| `qoder_public_admin.py` + dashboard | 仅本机的 Key/用量/审计管理 | 已交付；含 CSRF/Origin/Host 边界 |
| `qoder_public_cli.py` | 初始化及本机管理 HTTP 的薄客户端 | 已交付；`init` 拒绝覆盖已有配置 |
| `qoder_public_runtime.py` + `start/stop-qoder-public.*` | 单实例锁、原子 instance JSON、有界退避重启、逆序停止 | 已交付 |
| `tests/e2e/public_nps_local.py` | 真 nps/npc 本机回环链路（可选 Caddy） | 已交付；不纳入普通测试，需用户提供二进制 |

不直接编辑 `public-data/` 中的 `keys.json`。CLI 和页面都只通过 `8792` 的
管理 API 写入；该 API 与 gateway 共享同一个进程、KeyStore 和 UsageLog。

## 3. 配置、秘密与生命周期

### 3.1 配置优先级

```text
显式运维 CLI 参数 > QDP_* 环境变量 > public.json / public-data/config.json > 默认值
```

`QDP_CONFIG`、`QDP_DATA_DIR` 指定隔离配置/数据目录；`QDP_UPSTREAM_KEY`、
`QDP_ADMIN_PASSWORD` 提供秘密覆盖。运行器还需支持 `QDP_UPSTREAM_PORT`、
`QDP_ADMIN_PORT`、`QDP_NPS_SERVER_ADDR`、`QDP_NPS_VKEY`、`QDP_NPC_PATH`、
`QDP_START_TIMEOUT`。公共模式强制回环绑定，不能通过 `QDP_HOST` 暴露端口。

当前隧道下载/渲染接口：

```sh
python scripts/fetch-nps.py --platform linux --arch amd64 --role server \
  --sha256 <管理员独立核验的SHA256> --dest /opt/qoder2api-public/nps
python qoder_public_tunnel.py prepare-server --nps-root /etc/nps \
  --domain <API_DOMAIN> --client-id 1 --host-id 1 --vkey-output npc-vkey.txt
python qoder_public_tunnel.py render --domain <API_DOMAIN> --server <VPS_IP> \
  --output-dir <安全输出目录> --vkey-stdin
```

官方 nps release 没有 SHA-256 清单，脚本**必须**要求 `--sha256`。管理员从独立
可信渠道核验所选资产的摘要；匹配不代表 release 本身安全。nps/npc 固定
v0.26.10，二进制不提交 Git。

### 3.2 数据目录与 Key

`public-data/` 包含配置、Key 摘要、会话密钥、usage 和日志，整体不入库。

- 内部 qoder2api Key、管理口令散列、nps vkey 不写入 Git、状态输出或审计。
- 用户 Key 只存 SHA-256 摘要和可识别前缀；仅在创建/轮换时返回一次明文。
- 轮换立即吊销旧 Key；禁用、过期、软删除均拒绝后续请求，软删除保留用量。
- 默认 `rpm=60`、`max_concurrent=4`，日/月 token 为 0 表示不限；模型空列表不限。
- 默认固定区域（可由管理员指定 `cn` 或 `intl`），客户端不得用 query/header
  改区。`allow_client_realm=false` 时无绑区 Key 也不能自选区域。
- 配额按 UTC 自然日/月。推理不自动重试，避免重复计费；usage 缺失时估算并标记。

配置/Key 写入采用同目录临时文件、原子替换和锁；UNIX 尽量 `0600`，Windows
限制为当前服务账号/Administrators。备份必须整体保护，备份不等于脱敏。

## 4. 数据面与管理面

### 4.1 请求流水线

```text
连接槽 + 请求头/请求体超时
  → 规范化路径 + 路由/方法白名单
  → 可信代理解析真实源 IP
  → 有界 Content-Length/chunked 读取（TE+CL 拒绝）
  → qdp Key、启停、过期、IP 策略
  → 模型/区域、RPM、并发、日/月配额
  → 替换内部 Authorization，删除 hop-by-hop 和伪造身份头
  → 上游，不自动重试；SSE 逐块转发、usage 计量、一次性审计
```

只允许支持的 OpenAI/Anthropic 路由。`GET/HEAD /v1/models` 可用；推理端点
为 POST。`/healthz` 仅提供本机 gateway 的存活信息，不由 Caddy 公网转发，
不返回上游、Key、配置或进程信息。

Key 接受 `Authorization: Bearer qdp-...` 或 `X-API-Key`；两者冲突、query/cookie
携带 Key、未知/过期/禁用 Key 均失败。一次鉴权失败只计一次；达到窗口阈值
返回 429 和 `Retry-After`。错误形状按请求协议返回，不泄漏策略细节。

### 4.2 源 IP 信任链

仅 gateway 配置的 `server.trusted_proxies`（默认 `127.0.0.1/::1`）直接对端可
提供转发头。Caddy 用直接客户端地址覆盖 `X-Forwarded-For` 和 `X-Real-IP`，
不得信任用户提交的 `Forwarded`、CF/True-Client 等头。nps 追加的回环节点
不是用户 IP；gateway 必须按实际 Caddy→nps 链验证并归一化，不能盲取首段。

不能解析出合法、唯一且与 `X-Real-IP` 一致的客户端 IP 时，默认拒绝；不得
把 `127.0.0.1` 当作用户地址。IP 白名单、失败限速和审计共用该结果。

### 4.3 管理面接口（待实现）

页面和 CLI 均只访问 `http://127.0.0.1:8792`。即使配置存在
`admin.allow_remote`，也不能开放远程管理；远程运维走 SSH：

```sh
ssh -N -L 8792:127.0.0.1:8792 <内网机SSH>
ssh -N -L 8080:127.0.0.1:8080 <VPS_SSH>
```

除登录外 `/api/*` 需要 HttpOnly、SameSite=Strict 会话。登录口令用 PBKDF2，
会话由持久化随机 HMAC 密钥签名；写操作要求本机 Origin、`X-Requested-With`
和 CSRF。登录失败 10 次锁 5 分钟，无 CORS，不返回散列、内部 Key 或 vkey。
当前管理实现使用进程内随机短时令牌，重启后需重新登录；持久化 HMAC 会话
不是现有能力，除非后续实现并通过过期/篡改测试，不得据此承诺重启保持会话。

| 接口 | 用途 |
|---|---|
| `POST /api/login`、`GET /api/session`、`POST /api/logout` | 会话 |
| `GET /api/status`、`/api/models`、`/api/tunnel?probe=1` | 脱敏状态、上游模型、nps TCP 可达性 |
| `GET/POST /api/keys`、`PATCH /api/keys/{id}`、`DELETE /api/keys/{id}` | Key 列表、创建、策略修改、软删除 |
| `POST /api/keys/{id}/enable`、`disable`、`rotate` | 启停、立即轮换 |
| `GET /api/usage?days=7&key_id=`、`/api/audit?key_id=&limit=100` | 1–70 天汇总、最近 500 条内流水 |

创建/轮换仅在本次响应包含明文。隧道 TCP 探测只说明服务器可达，不代表
npc 在线或公网可用。CLI 支持 `key list/create/update/enable/disable/rotate/delete`、
`usage`、`audit`、`status`、`models`、`tunnel`、`doctor`；秘密用交互/环境输入，
不提供 `--password`，会话仅存进程内存。

## 5. 隧道、TLS 与信任边界

### 5.1 VPS 配置

```ini
# /etc/nps/conf/nps.conf
http_proxy_ip=127.0.0.1
http_proxy_port=18080
https_proxy_port=
bridge_type=tcp
bridge_ip=0.0.0.0
# 0.0.0.0 让 VPS 所有接口可达 bridge；防火墙必须把 TCP/8024 限制到
# 内网机的出口 IP，并保证 npc 可达（与 scripts/nps.conf.example 一致）。
bridge_port=8024
web_ip=127.0.0.1
web_host=127.0.0.1
web_port=8080
allow_user_login=false
allow_user_register=false
allow_local_proxy=false
# 无 public_vkey / auth_key / auth_crypt_key
```

`prepare-server` 在 nps 停止时更新 `conf/clients.json`、`conf/hosts.json`：
私有 client `Crypt=true`，host 为 `http://<API_DOMAIN>`，目标固定
`127.0.0.1:8791`。服务端不替客户端接受任意隧道声明。

### 5.2 Caddy 和内网 npc

`scripts/Caddyfile.example` 仅转发白名单，其余 404；将 Host 固定为 API 域名、
覆盖源 IP 头、`flush_interval -1`，由 Caddy 自动签发/续期证书。

```ini
[common]
server_addr=<VPS_IP>:8024
conn_type=tcp
vkey=<私有client vkey>
auto_reconnection=true
crypt=true
compress=true
disconnect_timeout=60

[api]
host=<API_DOMAIN>
target_addr=127.0.0.1:8791
```

nps v0.26.10 的 `crypt=true` 隧道使用旧 TLS 封装；客户端代码设置
`InsecureSkipVerify: true`，服务端动态生成自签证书，缺少证书身份认证。
因此它降低明文暴露风险，但**不能单独抵御主动中间人，也不是经认证的
E2E 加密**。Caddy 只保护用户到 VPS 的 HTTPS；VPS 能看到 HTTP 明文和内部
转发头，必须视为可信边界。ssh/防火墙/系统账户保护仍是必要条件。

不在 v1.2 宣称 nps 自身的证书轮换、完美前向保密或防主动攻击能力；如需
更高安全等级，应另行评审并在隧道内增加经过身份认证的 TLS/mTLS，而非
继续启用 nps 旧 HTTPS 入口。

## 6. 统一运行器（待实现）

`qoder_public_runtime.py start|stop|restart|status|run` 统一管理 qoder2api、
gateway 和 npc。薄 `.bat/.sh` 只定位 Python 并透传，不再各自维护 PID。

- 启动顺序：qoder2api → `/health` 探活 → gateway（默认 `--with-admin`）→
  `/healthz` 探活并确认 8791/8792 → npc。任一失败逆序清理本轮进程并非零退出。
- 独占锁在项目 `run/`，原子实例 JSON 记录真实项目路径、随机 instance_id、
  PID、启动时间、重启次数；拒绝陈旧 PID/不同令牌的所有权操作。
- qoder2api/gateway 异常退出是关键故障：先隔离 npc，再按顺序恢复；npc
  可独立重启。退避 `1,2,5,10,30,60` 秒，稳定 5 分钟清零，连续 5 次进入 failed。
- 停止顺序 npc → gateway（含管理线程）→ qoder2api。Linux 先 SIGTERM、
  10 秒后对所属进程组 SIGKILL；Windows 用精确 PID + Job Object 清理，禁止按
  `python.exe`/`npc.exe` 名称全机查杀。
- `QDP_PUBLIC_MANAGED=1` 禁用上游自更新自重启及旧 console-close 强退，
  由运行器统一重建；关闭启动终端不等于停止服务。
- `status` 支持人类可读与 JSON，区分 starting/running/degraded/stopped/failed/stale；
  进程存活不能冒充“公网已连通”。
- 保留 qoder2api 和 gateway 自己的日志轮转；运行器仅记录 `runtime.log` 和
  `npc.log`，避免重复 Tee。日志不得记录秘密或提示词/响应正文。

## 7. 测试与验收

普通 `python tests/run_all.py` 不下载二进制、不访问公网、不启动 nps。
保留上游测试约定，public 测试至少包括：

| 层 | 必测 |
|---|---|
| 数据面 | Key 生命周期、真实路由矩阵、伪造 XFF、可信代理、IP 策略、并发释放、模型/区域、配额、配置 fail-closed |
| HTTP 边界 | Content-Length/chunked 边界、TE+CL、超限/错误分块、慢头/慢体、连接槽、SSE 分片/断连、仅一次审计 |
| 管理/CLI | 登录、过期/注销、CSRF/Origin、非回环拒绝、秘密仅一次出现、两个用户策略互不影响 |
| 运行器/隧道 | 模板渲染、私有资源、SHA-256 缺失/错误、路径含空格、重复 start/stop、PID 复用、异常恢复 |
| 独立本机 e2e | 真 Caddy→nps→npc→gateway→假上游，401/200、非流式/SSE、禁用/恢复、精确进程清理 |

`tests/e2e/public_nps_local.py` 存在后才加入独立 Linux CI job；可选测试失败
不得被记成通过。使用临时目录、动态端口和固定版本，不改 hosts、不用系统
代理，不读取真实 `public-data/`。Windows 本机实测无 console 停止、进程树
清理和回环边界；Linux 实测进程组/SIGINT/SIGTERM。

最终公网验收必须人工验证 DNS、真实 ACME 证书、VPS 防火墙、NAT 出口、
外部客户端、两个 Key 的非流式/SSE、禁用/轮换、断线恢复和备份恢复。

## 8. 分支、升级与交付

- 保留 `feat/public-gateway-nps`，开发完成后由用户手动合并 main；文档编辑
  不擅自提交，不修改他人并行开发文件。
- 仓库沿用 qoder2api 历史，public 为独立文件层；上游更新先审查再合并，
  运行器负责重启，不直接启用 qoder2api 自重启。
- `public-data/` 与 `run/` 不属于代码升级内容；升级前停止服务并备份，
  回滚代码后使用兼容的数据目录。
- 不把 nps 管理 API 暴露到公网，不代理原 qoder2api 面板，不新增公网
  `/api/*`、通用转发、refresh、WebSocket、多节点或数据库。

## 9. 实施顺序

1. 完成数据面安全回归：可信代理、请求体/连接边界、区域、计量和审计。
2. 完成仅本机管理 API/CLI，网关管理初始化失败 fail-closed。
3. 完成隧道模板和统一运行器；文档中的待实现入口随实际 CLI 同步。
4. 跑 public 与上游全量测试；落地独立 e2e 脚本后才启用其 CI job。
5. 在 Windows/Linux 内网和真实 Linux VPS 验收，记录未验证边界后人工合并。

## 10. 决策历史

| 版本/日期 | 决策 | 当前状态 |
|---|---|---|
| v1.0 / 2026-11-10 | 独立 Python gateway；按 Key 管理；Linux VPS、Windows/Linux 内网；<5 用户；feat 分支人工合并 | 保留 |
| v1.1 / 2026-11-10 | 域名 HTTPS、VPS 终止 TLS；Key 只显示一次；管理面仅本机；默认模型不限 | 保留，但 TLS 改为 Caddy |
| v1.1 / 2026-11-10 | nps `https_proxy_port=443`、`https_just_proxy=false`、nps 管理证书 | **废止**：旧 nps TLS 不作为公网入口 |
| v1.1 / 2026-11-10 | npc 自由声明 TCP/域名隧道，靠任意 vkey 保护 | **废止**：预建私有 client+host，禁用 `public_vkey` |
| v1.2 / 2026-11-11 | Caddy:443 → nps 回环:18080 → npc → gateway:8791 → 上游:8790；bridge:8024；nps admin 回环:8080 | 固定 |
| v1.2 / 2026-11-11 | 公网路由排除 refresh；只信 Caddy 覆盖的源 IP 链；管理写仅本机 API；统一 runtime；下载必须管理员 SHA-256 | 固定 |
| v1.2 / 2026-11-11 | nps 隧道开启 crypt，但承认旧 TLS 无证书身份验证；不声称军事级或 E2E | 固定风险边界 |

详细操作见 [部署](public-deployment.md)、[安全](public-security.md)、
[API](public-api.md)、[开发](public-development.md)、[测试](public-testing.md)、
[排障](public-troubleshooting.md)。
