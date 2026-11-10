# qoder2api-public 设计文档

> 版本：v1.1（决策已确认）· 2026-11-10 · 分支 `feat/public-gateway-nps`
>
> 本文是**动手前的总设计**。文档里每个「决策」都给了备选与理由；
> 标 **[待确认]** 的地方需要你拍板，确认后再进入实现。

## 0. 一句话方案

在 qoder2api 前面加一层**独立的公网网关**（Python，零依赖，新文件，不改
`qoder_proxy.py`），由 nps 内网穿透把它暴露到公网。公网访客只能带着
**网关签发的 API Key** 访问白名单推理端点，密钥按人管理（配额、限速、模型
白名单、有效期、审计）。qoder2api 本体、面板、nps 管理台全部留在 127.0.0.1。

```
  users 1..5
     │   https://<API_URL>/v1   +   Authorization: Bearer qdp-xxxx
     ▼
┌──────────────── 公网 VPS ────────────────┐
│  nps 服务端                              │
│    :8443  API 入口（TCP 隧道 / 域名）      │
│    :8024  bridge（npc 连入，加密）         │
│    :8080  管理台（仅 127.0.0.1）           │
└──────────────────┬───────────────────────┘
                   │  加密隧道（vkey + crypt）
                   ▼
┌──────────────── 内网主机（Linux / Windows）────────────────┐
│  npc ──▶ 网关 :8791 ──▶ qoder2api :8790                    │
│          （鉴权 / 配额 / 限流 / 计量 / 审计）  （上游反代 + 账号池 + 面板） │
│  网关管理面 :8792（仅 127.0.0.1，或按需开放）                 │
└────────────────────────────────────────────────────────────┘
```

## 1. 目标与验收口径

| # | 需求 | 设计对应 |
|---|---|---|
| 1 | Linux + Windows 都能跑 | 代码纯标准库、路径全部 `pathlib`；两条启动脚本链（`.bat/.ps1` + `.sh`）；nps/npc 二进制按平台下载 |
| 2 | 公网 API_URL 访问内网服务 | nps TCP/域名两种隧道路由；npc 断线自动重连；网关对上游做超时与 502 包装 |
| 3 | 只有持 API Key 的用户可用；不同 Key 分别管理 | 网关白名单端点 + 每 Key 独立策略（启用/禁用、RPM、并发、日/月 token 配额、模型白名单、有效期、IP 白名单、审计） |
| 4 | 目标用户 < 5 | 单进程内存态限流 + JSONL 流水足够；不做多租户数据库 |
| 5 | 基于 qoder2api，另开分支，人工合并 | 仓库保留上游全部历史，开发只在 `feat/public-gateway-nps` 分支；新增文件为主，`qoder_proxy.py` 零改动 |

## 2. 现状与约束（已核实）

- qoder2api（本仓库基线）：Python 3.9+、**零第三方依赖**；入口 `qoder_proxy.py`，默认 `127.0.0.1:8790`；参数 `--host/--port/--lan/--api-key/--panel-password/--pid-file/--accounts-dir/--usage-dir`；支持 OpenAI Chat/Responses + Anthropic Messages + SSE 流式；自带面板与多 Key 管理（`accounts/settings.json` 的 `api_keys`）。
- 上游版本基线：v1.9.5（`qoder_proxy.py:74`），本仓库 HEAD `9ab1dd0`。
- nps：Go 编写、GPL-3.0，最新发布 v0.26.10（2021 年后停更但功能稳定）；服务端/客户端二进制都有 windows/linux amd64/arm64；支持配置文件模式（客户端声明隧道）、TCP 端口转发、域名代理、TLS 透传（`https_just_proxy=true`）。本机没有 Go 工具链 → **不编译、直接下载官方 release 二进制**。
- 本机 Python 3.14，可直接跑 qoder2api 与网关；公网/VPS 由你提供。

## 3. 关键设计决策

### D1 网关是**独立进程**，不改 `qoder_proxy.py`  [建议]

- 备选 A：在 `qoder_proxy.py` 里加鉴权与隧道逻辑 → 与 69 万字节单文件强耦合，上游每次更新都可能冲突，且面板/管理端点天然暴露在公网入口。
- 备选 B（采用）：新增 `qoder_public_*.py`，默认只监听 127.0.0.1；qoder2api 用 `--host 127.0.0.1 --api-key <内部Key>` 启动，只信任网关。
- 收益：上游 `git merge upstream/main` 几乎零冲突；网关可以独立测试、独立重启；安全边界清晰（公网面=网关，内部面=qoder2api）。
- 代价：多一个进程；由启动脚本统一管理（pid 文件、日志、stop）。

### D2 双 Key 模型：用户 Key ≠ 上游 Key

- 用户拿到的是网关签发的 `qdp-...`；网关校验通过后，用**内部 Key** 转发给 qoder2api。
- 好处：用户 Key 与 qoder2api 的 `api_keys` 解耦，qoder2api 面板里的 Key 只给网关用；用户 Key 的配额/模型白名单由网关执行；吊销用户 Key 不需要动 qoder2api。
- 网关转发时额外带 `X-QDP-Key-Id`（仅内部可见）与 `X-Forwarded-For`，便于排障。
- 备选：把用户 Key 直接注册进 qoder2api 的 `api_keys`，复用它的 `key_id` 用量统计。否决理由：需要网关持有面板会话去写 qoder 设置，耦合深；且 qoder 的 Key 是明文存储、无日配额，仍要另做一套策略。

### D3 隧道路由：域名代理（模式 B）为默认

| 模式 | 公网入口 | TLS 终止点 | 需要域名 | 说明 |
|---|---|---|---|---|
| **A. TCP**（默认） | `https://<VPS>:8443/v1` | 网关（自带证书） | 否（IP 证书/自签亦可） | nps 只做字节搬运；端到端加密，VPS 看不到 Key 明文 |
| **B. 域名** | `https://api.example.com/v1` | nps 服务端（`https_default_cert_file`） | 是 | 客户端只连 443；VPS 上需要证书（certbot 等） |
| C. 域名 + TLS 透传 | `https://api.example.com/v1` | 网关 | 是 | `https_just_proxy=true` + 网关证书；端到端加密 + 443 端口 |

**已确认**：走 **B**（域名 + VPS 终止 TLS）。A/C 作为可选能力保留在配置里，不改代码即可切换。
**绝不允许**明文 HTTP 直接暴露在公网（Key 会在链路上裸奔）。

### D4 管理面：独立端口 + CLI + 单页 UI，只绑 127.0.0.1

- `:8792` 管理面（默认 `127.0.0.1`），密码 PBKDF2 加盐存储，登录发 HMAC 会话 cookie；写操作要 `X-Requested-With` 头（CSRF）。
- )同时提供 `qoder_public_cli.py`：`key add/list/disable/enable/rotate/rm`、`usage`、`doctor`、`tunnel-init`。人在服务器上、不想开浏览器时用 CLI。
- 备选：复用 qoder2api 的 558KB `dashboard.html` 与面板会话 → 侵入大、合并冲突风险高。否决。
- 管理面要不要允许远程访问由你决定（`admin.allow_remote` + 证书），默认只本机。

### D5 Key 存储：只存 SHA-256 摘要 + 前缀

- 签发时**只显示一次**明文；之后列表只显示 `qdp-abcd12…`。密钥本身 32 字节 urlsafe 随机。
- 比对用 `hmac.compare_digest`（摘要），防时序攻击；日志只记前缀与 key_id。
- 备选：明文存储（qoder2api 现状，方便「再看一眼」）→ 泄漏面更大。否决；确实要「找回」就用 `rotate` 换新。
- 加一把 `master key`（`public-data/master.key`）只用于给配置里的内部 Key 加密？**不做**：内部 Key 本来就在同一台机器的 `accounts/settings.json` 里，收益为零。

### D6 计量口径：以响应里的 `usage` 为准，缺了才估算

- OpenAI 非流式：`usage.prompt_tokens/completion_tokens`。
- OpenAI 流式：默认在请求里补 `stream_options.include_usage=true`（可关闭），从最后一个 `data:` 帧的 `usage` 取数；客户端若已显式设置则尊重原值。
- Anthropic：`message_start.message.usage.input_tokens` + `message_delta.usage.output_tokens`。
- 都没有（上游截断/异常）且开启 `estimate_when_missing`：按 `字符数/4` 粗估输出、按请求体长度粗估输入，并在流水里标 `estimated=true`。
- 配额按「UTC 自然日 / 自然月」累计，落 `public-data/usage/usage.jsonl`（每请求一行），内存里维护当日计数器，重启时回放最近 2 天。
- 为什么不直接用 qoder2api 的 `usage/by-key`：所有公网请求在它看来都是同一个内部 Key，无法按用户拆分。自己的流水还能记 IP、UA、耗时、拒绝原因。

### D7 幂等与重试：不做自动重试

LLM 请求非幂等（一次重试 = 双倍扣费）。网关只在**连接失败**时返回 502/504 并带明确错误体，把「重试」留给客户端。

## 4. 组件与目录结构

```
qoder2api-public/
├── qoder_proxy.py ...            # 上游文件，零改动（基线 v1.9.5）
├── qoder_public_config.py        # [新] 配置加载/校验/默认值/环境变量覆盖
├── qoder_public_keys.py          # [新] Key 存储：签发/吊销/轮换/查询/配额校验
├── qoder_public_meter.py         # [新] 流式+非流式 usage 解析、日/月累计、估算
├── qoder_public_gateway.py       # [新] 数据面：鉴权、白名单、限流、转发、审计
├── qoder_public_admin.py         # [新] 管理面 HTTP API + 静态页
├── qoder_public_cli.py           # [新] 命令行：key/usage/doctor/tunnel-init
├── qoder_public_dashboard.html   # [新] 管理单页（无第三方资源）
├── public.example.json           # [新] 配置样例（无秘密）
├── public-data/                  # [新] 运行时（gitignore）：config.json、keys.json、
│                                 #      usage/、logs/、admin-secret、certs/
├── tunnel/
│   ├── nps/nps.conf.example      # [新] 服务端配置模板
│   ├── npc/npc.conf.example      # [新] 客户端配置模板
│   └── bin/                      # [新] 下载的 nps/npc（gitignore）
├── scripts/
│   ├── fetch-nps.ps1 / .sh       # [新] 按平台下载 nps/npc + SHA256 校验
│   ├── setup-nps-server.ps1/.sh  # [新] 生成服务端配置/随机口令/防火墙提示
│   └── tunnel-init.ps1/.sh       # [新] 生成 npc.conf（或退回 CLI 子命令）
├── start-qoder-public.bat/.sh    # [新] 起 qoder2api + 网关 + npc（后台、pid/日志）
├── stop-qoder-public.bat/.sh     # [新]
├── tests/_test_public_*.py       # [新] 单元 + 本地端到端（假上游）
└── docs/
    ├── design-public-gateway.md  # 本文
    ├── public-deployment.md      # [新] 公网+内网部署（Win/Linux）
    ├── public-security.md        # [新] 威胁模型与加固清单
    ├── public-api.md             # [新] 对外 API 与错误码
    └── public-troubleshooting.md # [新] 隧道排障
```

命名遵循上游习惯（根目录 `qoder_*.py` 扁平模块、`tests/_test_*.py`、`.sh/.bat` 成对）。

## 5. 数据模型

### 5.1 `public-data/keys.json`（0600）

```json
{
  "version": 1,
  "keys": [
    {
      "id": "k_7f3a91c2",
      "name": "alice-laptop",
      "owner": "alice",
      "prefix": "qdp-3f2a91",
      "hash": "sha256:9b1f...",
      "enabled": true,
      "created_at": "2026-11-10T20:30:00+08:00",
      "expires_at": null,
      "rpm": 60,
      "max_concurrent": 4,
      "daily_tokens": 200000,
      "monthly_tokens": 0,
      "allowed_models": ["qwen*", "glm*"],
      "allowed_ips": [],
      "realm": "",
      "note": "Alice 的 Cursor",
      "last_used_at": "2026-11-10T21:02:13+08:00",
      "totals": {"requests": 128, "prompt_tokens": 90210, "completion_tokens": 33120, "errors": 2}
    }
  ]
}
```

- 原子写：临时文件 + `os.replace`，进程内 `threading.Lock`；文件权限尽量 600/ACL。
- 校验：`allowed_models` 用 fnmatch、大小写不敏感（与 qoder2api 的 `key_allows_model` 同语义）；空 = 不限。
- 未知字段保留（向前兼容）；坏记录跳过并告警，绝不 fail-open 到「无限额」。

### 5.2 `public-data/usage/usage.jsonl`（每请求一行）

```json
{"ts":"2026-11-10T21:02:13.412+08:00","key_id":"k_7f3a91c2","prefix":"qdp-3f2a91",
 "ip":"203.0.113.7","method":"POST","path":"/v1/chat/completions","status":200,
 "model":"Qwen3.8-Flash","stream":true,"prompt_tokens":812,"completion_tokens":305,
 "estimated":false,"ms":4210,"ua":"OpenAI/Python 1.30.0","err":""}
```

- 单文件按 `rotate_mb`（默认 20MB）轮转到 `.1`…`keep` 个，读接口只回最近 N 天。
- `public-data/usage/daily.json`：当日/当月聚合缓存（可按 jsonl 重建，丢了不致命）。

### 5.3 `public-data/config.json`（0600，含秘密，不入库）

示例见 §6。秘密项（`upstream.api_key`、`admin.password_hash`、`nps.vkey`）只存在这里。

## 6. 配置样例 `public.example.json`

```json
{
  "version": 1,
  "server": {"host": "127.0.0.1", "port": 8791},
  "tls": {"enabled": false, "cert_file": "", "key_file": ""},
  "upstream": {
    "url": "http://127.0.0.1:8790",
    "api_key": "",
    "connect_timeout": 10,
    "read_timeout": 600,
    "max_body_bytes": 33554432
  },
  "admin": {
    "host": "127.0.0.1",
    "port": 8792,
    "password_hash": "",
    "session_ttl": 43200,
    "allow_remote": false
  },
  "defaults": {
    "rpm": 60,
    "max_concurrent": 4,
    "daily_tokens": 0,
    "monthly_tokens": 0,
    "allowed_models": [],
    "realm": "",
    "allow_client_realm": false
  },
  "endpoints": {
    "allowed_paths": ["/v1/models", "/v1/chat/completions", "/v1/responses",
      "/v1/messages", "/v1/messages/count_tokens", "/v1/models/refresh"],
    "auth_fail_limit": 20,
    "auth_fail_window": 60
  },
  "metering": {"inject_stream_usage": true, "estimate_when_missing": true},
  "logging": {"dir": "public-data/logs", "level": "info", "rotate_mb": 20, "keep": 5},
  "nps": {
    "server_addr": "1.2.3.4:8024",
    "vkey": "",
    "mode": "tcp",
    "server_port": 8443,
    "domain": "",
    "crypt": true,
    "compress": true
  }
}
```

- 环境变量覆盖（便于容器/systemd）：`QDP_PORT`、`QDP_ADMIN_PASSWORD`、`QDP_UPSTREAM_KEY`、`QDP_CONFIG`。
- `public-data/config.json` 由 `cli init` 生成：随机内部 Key、随机管理密码（打印一次）、随机 nps vkey。


## 7. 数据面（公网入口）设计

### 7.1 端点白名单（其余一律 404，不暴露存在性）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/chat/completions` | OpenAI Chat，支持流式 |
| POST | `/v1/responses` | OpenAI Responses（Codex） |
| POST | `/v1/messages` | Anthropic Messages（Claude Code），支持流式 |
| POST | `/v1/messages/count_tokens` | 本地估算，不转发也可（按 qoder2api 能力转发） |
| POST | `/v1/models/refresh` | 刷新模型清单（可选，按需开放） |
| GET | `/v1/models` | 模型清单，按 Key 白名单过滤 |
| GET | `/ping`、`/healthz` | 网关自身存活（不检查上游、不返回敏感信息） |

- 路径先做百分号解码与规范化，拒绝 `..`、`//`、`%2f`、非白名单路径。
- `/settings`、`/accounts`、`/panel/*`、`/agents/*`、`/update/*`、`/logs` 等**永不出现在公网面**。

### 7.2 鉴权

- 支持 `Authorization: Bearer qdp-...` 与 `x-api-key: qdp-...`（大小写不敏感头部名由 `http.server` 处理）。
- **不接受** `?key=`（会进代理日志/浏览器历史）；出现即 401，并记一条审计。
- 校验顺序：格式与前缀 → 摘要比对（`hmac.compare_digest`）→ 启用/有效期 → IP 白名单 → 模型白名单（解析请求体后）→ RPM/并发 → 日/月配额。
- 失败计数：同一 IP 在 `auth_fail_window` 内失败 `auth_fail_limit` 次后，该 IP 直接 429（内存滑动窗口），防爆破与无效重试。
- 401 响应体按协议分形状（见 §7.4），并带 `WWW-Authenticate: Bearer`。

### 7.3 请求处理流水线

```
accept
 → 路径白名单/方法校验                    ── 404/405
 → 方法体大小上限（max_body_bytes）        ── 413
 → 读完整请求体（支持 chunked 客户端）     ── 400/408
 → 鉴权（Key → key 记录）                  ── 401/403/429
 → 解析 JSON 取 model（推理端点必需）      ── 400
 → 模型白名单 / realm 绑定 / IP 白名单     ── 403
 → RPM + 并发闸门（每 Key + 全局）         ── 429 + Retry-After
 → 日/月 token 配额预检                    ── 429（附剩余额度）
 → 请求改写（Authorization→内部Key、去 Hop-by-Hop、
   去客户端 X-Realm/?realm、加 X-Forwarded-For/X-QDP-Key-Id、
   流式请求补 stream_options.include_usage）
 → 转发上游（http.client，流式读）
 → 响应改写（状态码/头部透传、SSE 逐块 flush、
   同时旁路解析 usage 计量）
 → 落 usage.jsonl + 更新内存计数 + 审计
```

- 并发闸门用 `threading.Semaphore`（每 Key）+ 全局计数；超限立即 429，不排队。
- 排队不做：用户少，抢不到就是 429，语义清晰。
- 客户端断开（`BrokenPipeError`/`ConnectionResetError`）：关闭上游连接、记 `client_aborted`，不重试。
- 上游连不上：502（`upstream_unreachable`）；上游超时：504（`upstream_timeout`）；上游 4xx/5xx 原样透传（body 原样，头部过滤）。

### 7.4 错误体形状

OpenAI 风格（`/v1/chat/completions`、`/v1/responses`、`/v1/models`、`/ping`）：

```json
{"error": {"message": "invalid API key", "type": "invalid_request_error", "code": "invalid_api_key"}}
```

Anthropic 风格（`/v1/messages*`）：

```json
{"type": "error", "error": {"type": "authentication_error", "message": "invalid API key"}}
```

状态码约定：401 未认证/Key 无效、403 无权限（模型/IP/禁用）、404 端点不存在、413 请求过大、429 限流/超额、502/504 上游异常。

### 7.5 转发细节

- 头部：删 `connection/keep-alive/proxy-*/te/trailer/transfer-encoding/upgrade/host/content-length`（按实际体长重算），保留 `content-type`、`accept` 等业务头；响应侧同理，保留 `content-type`、`cache-control` 与 `x-qoder-*`（便于排障），删 `set-cookie`（不把内部会话带出去）。
- SSE：上游无 `content-length` 时用 `Transfer-Encoding: chunked` 逐块 flush；`Cache-Control: no-cache`、`X-Accel-Buffering: no` 保证 nps/反代不缓冲。
- 计量用「旁路 tee」：把读到的字节喂给解析器（不改变发给客户端的字节），内存只保留最后 64KB 的尾部窗口 + 首个 SSE 事件窗口。
- `/v1/models`：缓存上游清单 60s；Key 有 `allowed_models` 时过滤 `data[].id`；响应带 `X-QDP-Filtered: true`。
- 请求体篡改最小化：只在需要时（流式 usage 注入）改 JSON，其余原样转字节；若 JSON 解析失败但端点要求 model，则 400 失败关闭。

### 7.6 计量与配额

- 每请求一行 `usage.jsonl`（见 §5.2）；内存维护 `{key_id: {date: {requests, prompt, completion}}}`，启动回放最近 2 天文件。
- 配额检查在转发前（拿当前值 + 预留本次预估输入 token），转发后按实际 usage 修正。
- `daily_tokens=0` 表示不限；超限 429，错误体带 `{"remaining_tokens": 0, "reset_at": "..."}`（放在 `error.message` 之外的自定义字段 `x-qdp-quota`，避免破坏客户端解析？→ 保守做法：只放在 `error.message` 文本里）。
- UTC 00:00 重置日累计、每月 1 日重置月累计；切换时写日志。

## 8. 管理面设计（`:8792`，默认仅本机）

### 8.1 认证

- `POST /api/login {password}` → 校验 PBKDF2-SHA256（迭代 210000，16B 盐），成功下发 `qdp_admin` cookie（HttpOnly、SameSite=Strict、Path=/，可选 Secure）。
- 会话令牌 = `base64url(payload).hmac_sha256(server_secret)`，payload 含 `exp`、`nonce`；服务端只认签名与过期，登出把 nonce 加入内存黑名单。
- 所有写操作要求 `X-Requested-With: qdp-admin`，并校验 `Origin` 为本机地址（防 CSRF）。
- 连续 10 次密码错误锁 5 分钟（内存）。默认密码在 `cli init` 时随机生成并打印一次，禁止 `admin` 这类弱口令（启动时检查并 WARN/拒绝）。

### 8.2 REST

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 版本、上游健康、npc 是否在线（可选探测）、Key 数、今日请求/失败 |
| GET | `/api/keys` | Key 列表（脱敏） |
| POST | `/api/keys` | 创建，返回一次性明文 |
| PATCH | `/api/keys/{id}` | 修改 name/owner/rpm/并发/配额/模型/IP/有效期/note |
| POST | `/api/keys/{id}/rotate` | 轮换，返回一次性明文 |
| POST | `/api/keys/{id}/disable`、`/enable` | 启停 |
| DELETE | `/api/keys/{id}` | 删除（软删：`deleted_at`，保留流水可追溯） |
| GET | `/api/usage?days=7` | 按 Key 汇总 + 逐日曲线 |
| GET | `/api/audit?limit=100` | 最近请求流水（可 `?key_id=`） |
| GET | `/api/models` | 上游模型清单（给管理页做白名单勾选） |
| GET | `/api/tunnel` | 当前 nps 配置（**vkey 打码**）与连通性提示 |

### 8.3 页面（单文件 HTML，无 CDN）

- 登录 → 「Keys」表（新建/禁用/轮换/删除/一次性明文弹窗）→ 「用量」（按 Key 的今日/7 天表，纯 CSS 柱状图）→ 「流水」（最近请求，可按 Key 过滤）→ 「状态」（上游/隧道/版本/自检结果）。
- 不引入任何外链 JS/CSS（公网机可能没有外网，也避免供应链风险）。

## 9. 安全设计

### 9.1 威胁模型

| 威胁 | 缓解 |
|---|---|
| 公网爆破/猜测 Key | 32B 随机；摘要比对；失败计数按 IP 封 60s；401 不区分「不存在/已禁用」 |
| Key 泄漏（用户笔记本） | 每 Key 独立吊销/轮换、有效期、RPM/配额封顶、IP 白名单（可选）、审计可查 |
| 内网服务被打穿 | qoder2api 只听 127.0.0.1 且要求内部 Key；网关白名单端点；管理面只听 127.0.0.1 |
| nps 管理台被入侵 | 只绑 127.0.0.1、强口令、错误次数限制（nps 自带）、`allow_user_login=false` |
| vkey 泄漏 → 别人白用隧道 | 长随机 vkey + `crypt=true`；公网只开 8443 与 8024；文档给出「按出口 IP 限制 8024」的 iptables/nft 方案 |
| VPS 被入侵看到明文 | 模式 A/C：TLS 终止在网关，VPS 只见密文；模式 B 明确告知风险 |
| 明文链路上泄漏 | 禁止纯 HTTP 公网入口；启动自检发现 `tls.enabled=false` 且 `server.host` 非本机时 WARN |
| 日志泄漏 Key | 全链路只记 `prefix`/`key_id`；`doctor` 会扫日志找明文 Key 并告警 |
| CSRF/XSS（管理面） | SameSite=Strict cookie + 自定义头 + Origin 校验；页面 `textContent` 渲染，不用 `innerHTML` 拼数据 |
| 配额被绕过 | 配额检查与计量在同一进程内串行更新（per-key 锁） |

### 9.2 加固清单（写进 `docs/public-security.md`）

1. `public-data/` 权限 700/600；Windows 用 ACL 只给运行账户。
2. qoder2api：`--host 127.0.0.1`、`--api-key <内部Key>`、`--panel-password <强口令>`；面板不外泄。
3. nps 服务端：`web_ip=127.0.0.1`、`web_password` 强口令、`allow_user_login=false`、`public_vkey` 32+ 随机、`runmode=pro`、`log_level=4`（别开 7 把请求细节写日志）。
4. 防火墙：只放行 8443/443（API）、8024（bridge，若出口 IP 不固定则限制来源为内网出口段）、22（管理）。
5. 关闭 qoder2api 一键更新：`QD_UPDATE_DISABLE=1`（公网暴露面最小化）。
6. 证书轮换：`cli doctor` 检查证书剩余天数 < 14 时告警。
7. 定期 `cli usage --days 30` 复核异常 Key；`cli doctor` 输出全链路自检。

### 9.3 密钥生命周期

`签发（一次性展示）→ 使用 → 到期/吊销 → 轮换 → 删除（软删，流水保留）`。

## 10. nps 集成

### 10.1 服务端（公网 VPS）配置要点

```ini
appname = nps
runmode = pro
bridge_type = tcp
bridge_port = 8024
bridge_ip = 0.0.0.0
public_vkey = <32+ 随机>
http_proxy_port = 80          ; 只有模式 B（域名）用
https_proxy_port = 443        ; 只有模式 B 用
https_just_proxy = false      ; B 用 false（nps 终止 TLS）；C 用 true（透传）
https_default_cert_file = conf/server.pem
https_default_key_file  = conf/server.key
web_ip = 127.0.0.1
web_port = 8080
web_username = <随机>
web_password = <强口令>
web_host = nps.example.com
allow_user_login = false
allow_user_register = false
allow_flow_limit = false
log_level = 4
disconnect_timeout = 60
```

- 服务端**不需要**预先注册隧道：npc 用配置文件模式声明 `[api] mode=tcp server_port=8443` 即可（nps v0.26.10 已无 `allow_ports` 限制，端口自由，靠 vkey 保护 → 更要保护 vkey）。
- 若用域名模式：`[api] host=api.example.com target_addr=127.0.0.1:8791`，DNS A 记录指向 VPS，`nps` 用 443 终止 TLS（证书放 `conf/server.pem/key`，certbot 续期后重启 nps 或软链）。
- systemd 单元与 Windows 服务（`nps install/start`）由 `scripts/setup-nps-server.*` 生成。
- nps 为 GPL-3.0：**仓库不提交二进制**，`scripts/fetch-nps.*` 运行时从官方 release 下载并校验 SHA256；分发提示写入 `docs/public-deployment.md`。

### 10.2 客户端（内网机）配置要点

```ini
[common]
server_addr = 1.2.3.4:8024
conn_type = tcp
vkey = <与服务端一致>
crypt = true
compress = true
auto_reconnection = true
max_conn = 64
disconnect_timeout = 60

[api]
mode = tcp
target_addr = 127.0.0.1:8791
server_port = 8443
```

- 内网机**只连出**，不需要公网 IP、不需要开入站端口。
- 断线：nps 自带 `auto_reconnection`；网关和 qoder2api 不重启。启动脚本再叠一层「npc 进程看护」（每分钟检查 pid/连接，挂了重拉并记日志）。
- 域名模式下把 `mode=tcp` 换成 `host=api.example.com`。

### 10.3 公网 API_URL 形态

| 模式 | Base URL 示例（OpenAI 客户端填 `.../v1`） |
|---|---|
| A TCP | `https://203.0.113.10:8443/v1` |
| B 域名 | `https://api.example.com/v1`（OpenAI/Responses） / `https://api.example.com`（Claude Code 的 Anthropic 入口） |

## 11. 部署形态

| 位置 | 组件 | Windows | Linux |
|---|---|---|---|
| 公网 VPS | nps 服务端 | `nps.exe -config=nps.conf`（或 `nps install/start` 注册服务） | `systemd` 单元（脚本生成） |
| 内网机 | qoder2api + 网关 + npc | `start-qoder-public.bat`（后台、pid/日志） | `start-qoder-public.sh`（前台或 `nohup`/systemd） |
| 内网机（可选） | Docker | 复用上游 `Dockerfile`（qoder2api）+ 网关进程/镜像；MVP 不提供网络容器化，文档给指引 | 同左 |

- 首次部署流程：`cli init` 生成秘密 → 起 qoder2api → 起网关 → `tunnel-init` 生成 npc.conf → 起 npc → `doctor` 自检 → 浏览器/curl 验收。
- 升级：`git pull`（上游）→ 重启脚本；网关数据在 `public-data/`，不受覆盖影响。

## 12. 测试计划

### 12.1 单元测试（`tests/_test_public_*.py`，纯 unittest，无网络）

- `keys`：签发/摘要/一次性明文/重复 name/过期/禁用/轮换/软删/原子写/坏记录容错。
- `config`：默认值、环境变量覆盖、非法值拒绝、秘密缺失时拒绝启动（fail-closed）。
- `auth`：Bearer/x-api-key/?key= 拒绝、大小写、畸形头、失败计数封禁。
- `policy`：模型白名单 fnmatch、空列表不限、realm 绑定、IP 白名单。
- `meter`：OpenAI JSON / OpenAI SSE（含 include_usage 注入）/ Anthropic 事件流 / 估算回退 / 截断流不崩。
- `quota`：日/月边界、并发、RPM 窗口、429 头与文案。
- `forward`：假上游（http.server）验证头部过滤、SSE 逐块、4xx/5xx 透传、超时、客户端断开。
- `admin`：登录、会话过期、CSRF 头、越权、脱敏（响应里绝不出现明文/哈希）。
- `cli`：init 幂等、doctor 退出码、tunnel-init 生成配置。

### 12.2 本地端到端（真 nps，本机回环）

`nps 服务端(8024) + npc(→ 127.0.0.1:8791) + 网关(8791) + 假上游(18790)`：
- 无 Key → 401；错误 Key → 401；正确 Key 非流式 → 200 且计量落盘；
- 流式（SSE）经隧道逐块到达（`curl -N` 观察时间戳）；
- 杀掉 npc 再拉起 → 隧道自愈；杀掉网关 → 502 且审计有记录；
- 域名模式（本机 hosts + `host=`）走同样断言。

### 12.3 手工验收清单（见 §14）

## 13. 里程碑（每步都可独立验收）

| 阶段 | 交付物 | 验收 |
|---|---|---|
| **M0 设计**（本次） | 本文 + git 基线/分支 | 你确认 §16 的问题 |
| M1 骨架 | `public.example.json`、`qoder_public_config.py`、`qoder_public_cli.py init` 生成 `public-data/config.json`（随机秘密） | `python qoder_public_cli.py init` 可重复、幂等、权限正确 |
| M2 数据面 | `qoder_public_gateway.py` + `keys` + `meter` + 转发；白名单/鉴权/限流/计量 | 单元测试全绿；假上游 e2e 通过 |
| M3 管理面 | `admin` + 单页 UI + CLI 全部子命令 | 创建/禁用/轮换/删除/用量/审计可用；响应脱敏 |
| M4 隧道 | `tunnel/` 模板、`fetch-nps.*`、`setup-nps-server.*`、`start/stop-qoder-public.*` | 本机真 nps 回环 e2e 通过（TCP + 域名两种） |
| M5 加固与文档 | 部署/安全/API/排障文档 + `doctor` 自检 + README 入口 | `doctor` 全绿；文档按步骤可复现 |
| M6 联调 | 你在 VPS 上部署，我远程/按清单配合 | §14 全部勾选 |

## 14. 验收标准（Definition of Done）

**功能**
1. 在 Windows 与 Linux 上，`start-qoder-public.*` 能一键拉起 qoder2api + 网关 + npc，`stop-*` 能干净停掉（只杀自己的 pid）。
2. 公网 `https://<API_URL>/v1/models` 带正确 Key 返回模型列表；不带 Key / 错 Key / 已禁用 Key / 过期 Key → 401/403，且响应体符合 §7.4。
3. `/v1/chat/completions`、`/v1/messages` 的**流式**非流式都能经隧道工作（Claude Code、OpenAI SDK、curl 各验一次）。
4. 每个 Key 的 `rpm`、并发、日/月 token 配额、模型白名单、IP 白名单、有效期**各自独立生效**，互不影响。
5. 管理面能创建 Key（一次性显示）、禁用、轮换、删除、看用量与流水；CLI 与 UI 结果一致。
6. `/v1/models` 对受限 Key 只返回白名单内模型。
7. 上游 qoder2api 挂掉时，网关 502 且错误体明确；恢复后无需重启网关。
8. npc 被杀后自动重连（或看护脚本 60s 内拉起），公网入口自动恢复。
9. `doctor` 报出：配置完整性、上游健康、隧道进程、证书剩余天数、日志里是否有明文 Key、公开端口是否可达（可选）。

**安全**
10. qoder2api 面板、网关管理面、nps 管理台**都无法从公网访问**（验收时从外网 curl 三个端口均应失败）。
11. 公网链路为 HTTPS；`usage.jsonl`/日志中不出现任何完整 Key。
12. `public-data/` 与 `accounts/` 不在 git 里（`git status` 干净）。

**工程**
13. `qoder_proxy.py` 与上游基线**零 diff**；所有新增代码集中在独立文件；`git diff upstream/main --stat` 一眼能看懂。
14. 新增测试全部通过；上游原有测试不被破坏（`python tests/run_all.py` 可选跑）。
15. 文档齐备：部署（Win/Linux）、安全加固、API、排障、上游同步。

## 15. 与上游 qoder2api 的同步策略

- 本仓库保留上游完整历史（基线 = `shuishuipingan/qoder2api-hub@9ab1dd0`，v1.9.5），已加 `upstream` remote。
- 开发只在 `feat/public-gateway-nps`；**新文件为主**，`qoder_proxy.py` 不碰。
- 需要改上游文件时（`.gitignore`、`.gitattributes`、`README.md` 加入口），单独一个 commit，commit message 前缀 `public:`，方便 `git log --grep '^public:'` 审计。
- 跟进上游：`git fetch upstream && git merge upstream/main`（在分支上），冲突面应该只有上述三个文件。
- 你合并到主分支：`git checkout main && git merge --no-ff feat/public-gateway-nps`（或按你习惯 PR 合并）。

## 16. 已确认的决策（2026-11-10）

| 项 | 结论 |
|---|---|
| 公网入口 | **模式 B**：域名 + VPS 终止 TLS（`https://<域名>/v1`，证书在 VPS） |
| 公网服务器 | **Linux amd64**（nps `linux_amd64_server` + systemd） |
| 管理面 | **仅本机 127.0.0.1**（远程管理走 SSH 端口转发或 CLI） |
| Key 明文 | **只显示一次**：只存 SHA-256 摘要 + 前缀，丢失就轮换 |
| 默认模型白名单 | **不限制**（空列表 = 全部可用，按 Key 可选限制） |

由此固定的实现细节：
- nps 服务端 `https_proxy_port=443`、`https_just_proxy=false`、`https_default_cert_file` 指向 VPS 上的域名证书；`http_proxy_port` 只用于 ACME `http-01` 或 80→443 跳转，可留空。
- npc 用域名模式：`[api] host=<域名> target_addr=127.0.0.1:8791`。
- 网关 `server.host=127.0.0.1`、`tls.enabled=false`（TLS 在 VPS 终止；VPS→内网经 nps 加密隧道，属可接受风险，已在 `docs/public-security.md` 写明；若日后要端到端加密，开 `tls.enabled` + 改 `https_just_proxy=true` 即可，不改代码）。
- `admin.allow_remote=false`；管理面只绑 127.0.0.1。
- `defaults.allowed_models=[]`（不限制）。

## 17. 工作量与协作计划（我方内部）

| 包 | 内容 | 预估 |
|---|---|---|
| P1 | config + keystore + meter + 数据面转发（关键路径，Lead 主写） | ~900 行 |
| P2 | 管理面 + 单页 UI + CLI（可并行，接口先定） | ~700 行 + HTML |
| P3 | 隧道脚本 + nps 模板 + 启动/看护脚本（可并行） | ~400 行脚本 |
| P4 | 测试（单测 + 本机真 nps e2e）+ 部署/安全/排障文档 | ~700 行 |

并行方式：P1 由我先定接口（`qoder_public_config.py` 的 `Config`、`keys` 的 `KeyStore`、`meter` 的 `UsageMeter` 签名），P2/P3/P4 按接口分头写，最终由我统一 review + 跑全量测试 + 真 nps 回环验收。写文件范围互不重叠：
`qoder_public_config.py / qoder_public_gateway.py / qoder_public_meter.py`（P1）、`qoder_public_keys.py / qoder_public_admin.py / qoder_public_dashboard.html / qoder_public_cli.py`（P2）、`tunnel/ scripts/ start-* stop-*`（P3）、`tests/_test_public_* docs/public-*`（P4）。

---

### 附：本设计刻意不做的事（避免过度工程，对应目标用户 < 5）

- 不做用户注册/找回密码/自助改配额；Key 由管理员签发。
- 不做数据库；JSON + JSONL 足够，备份 = 拷 `public-data/`。
- 不做多节点/负载均衡/高可用；单机内网服务 + 单 VPS。
- 不做计费结算（只有 token 用量与配额，不涉及钱）。
- 不改 qoder2api 的请求协议与路由逻辑（那是上游的活）。
- 不做 WebSocket 端点（当前协议只有 SSE）。
