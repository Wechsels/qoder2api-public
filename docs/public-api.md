# qoder2api-public API 与本机管理 CLI

## 1. 公网数据面

Base URL 为 `https://<API_DOMAIN>`，不要在域名后重复写 `/v1` 再拼接下表
完整路径。OpenAI 客户端通常填 `https://<API_DOMAIN>/v1`，Anthropic 客户端
按其配置填域名根路径。

仅以下路径可从 Caddy→nps 进入 gateway：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 上游模型，按 Key 的模型策略过滤 |
| POST | `/v1/chat/completions` | OpenAI Chat，支持 SSE |
| POST | `/v1/responses` | OpenAI Responses |
| POST | `/v1/messages` | Anthropic Messages，支持 SSE |
| POST | `/v1/messages/count_tokens` | Anthropic token 计数入口 |

不包括 `/v1/models/refresh`。`/settings`、`/accounts`、原 qoder2api 面板、
nps 管理端点和其他路径均不开放。Caddy 对其他路径返回 404。

认证使用任一请求头：

```http
Authorization: Bearer qdp-<secret>
X-API-Key: qdp-<secret>
```

同时提供两种身份且冲突时拒绝。不要用 `?key=`、Cookie 或 URL 携带 Key。
请求体的 `model` 仍按 qoder2api 实际支持的模型填写；公网 Key 可另行限制
模型、IP、区域、并发、RPM、UTC 日/月 token。

示例：

```sh
curl https://api.example.com/v1/models \
  -H 'Authorization: Bearer <USER_QDP_KEY>'

curl https://api.example.com/v1/chat/completions \
  -H 'Authorization: Bearer <USER_QDP_KEY>' \
  -H 'Content-Type: application/json' \
  -d '{"model":"<MODEL>","messages":[{"role":"user","content":"hello"}]}'

curl -N https://api.example.com/v1/messages \
  -H 'X-API-Key: <USER_QDP_KEY>' \
  -H 'anthropic-version: 2023-06-01' \
  -H 'Content-Type: application/json' \
  -d '{"model":"<MODEL>","max_tokens":64,"messages":[{"role":"user","content":"hello"}],"stream":true}'
```

流式响应使用 SSE，网关逐块转发，不将完整响应攒到结束。客户端中途断开、
上游中途断开或缺少 usage 时，审计可能有 `estimated=true` 或断连结果；
已开始返回的 200 流不能再被改写成新的 HTTP 错误状态。

常见状态：

| 状态 | 含义 |
|---|---|
| 400 | 路径、JSON、chunked、请求参数或区域不合法 |
| 401 | 缺失、错误、禁用、过期或已轮换的 Key |
| 403 | 模型、IP 或其他策略不允许 |
| 404 | 非白名单路径 |
| 405 | 路由不允许此方法 |
| 413 | 请求体超过上限（默认 32 MiB） |
| 429 | Key/全局并发、RPM、日/月配额或鉴权失败限速 |
| 502/504 | 上游不可达或超时；不会由网关自动重试推理 |

错误体按协议使用 OpenAI/Anthropic 错误形状，包含机器可读 `code`；可能带
`x-qdp-request-id` 和 `Retry-After`。不要把响应中的 Key ID 当作用户秘密。

## 2. 仅本机管理 API

地址 `http://127.0.0.1:8792`，与 gateway 同进程。远程通过 SSH 转发访问，
不是公网接口。每个连接的 peer 和 Host 必须为回环；不信任 XFF 决定管理权限。

| 方法 | 路径 | 行为 |
|---|---|---|
| GET/HEAD | `/` | 无 CDN 的管理页面 |
| POST | `/api/login` | `{"password":"..."}`，创建会话并返回 CSRF/过期时间 |
| GET | `/api/session` | 当前会话 |
| POST | `/api/logout` | 注销当前会话 |
| GET | `/api/status` | 版本、Key 数、当日请求/失败、上游探测 |
| GET | `/api/keys`；POST 同路径 | 脱敏列表；创建 Key，201 且仅此返回 `secret` |
| GET/PATCH/DELETE | `/api/keys/{id}` | 详情；策略编辑；软删除 |
| POST | `/api/keys/{id}/enable`、`disable`、`rotate` | 启停；轮换立即作废旧 Key并仅此次返回 `secret` |
| GET | `/api/usage?days=7&key_id=` | 汇总、逐日数据、配额余量；1–70 天 |
| GET | `/api/audit?key_id=&limit=100` | 最近流水，1–500 条，无提示词/正文 |
| GET | `/api/models` | 用内部 Key 获取并脱敏上游模型字段 |
| GET | `/api/tunnel?probe=1` | 脱敏 nps 配置及可选 TCP 探测，不代表 npc 在线 |

登录要求 `Origin` 指向相同回环端口和 `X-Requested-With: qdp-admin`。
登录后使用 `qdp_admin` HttpOnly/SameSite=Strict Cookie。写操作还要求
`X-QDP-CSRF`；无 CORS，Origin/Host 伪造不能获得权限。

管理响应不返回用户 Key 的 hash、内部上游 Key、管理散列或完整 vkey。
CLI 和浏览器都通过此 API 修改 Key，不能直接编辑运行中的 `keys.json`。

## 3. 本机 CLI

命令均从项目根目录执行；每次在线命令会提示管理密码，也可从受保护的
`QDP_ADMIN_PASSWORD` 环境变量读取。`--config` 可指定隔离配置。

```sh
# 仅一次：生成配置和管理密码，不覆盖已有配置
python qoder_public_cli.py init

# Key；create 也可用 add，update 的参数可组合
python qoder_public_cli.py key create alice --owner alice --rpm 30 --max-concurrent 2
python qoder_public_cli.py key list
python qoder_public_cli.py key show k_xxx
python qoder_public_cli.py key update k_xxx --daily-tokens 1000000 \
  --allowed-models 'qwen*,glm*' --allowed-ips '203.0.113.0/24'
python qoder_public_cli.py key enable k_xxx
python qoder_public_cli.py key disable k_xxx
python qoder_public_cli.py key rotate k_xxx
python qoder_public_cli.py key delete k_xxx --yes

# 状态与用量
python qoder_public_cli.py status
python qoder_public_cli.py models
python qoder_public_cli.py usage --days 7 --key-id k_xxx
python qoder_public_cli.py audit --limit 100 --key-id k_xxx
python qoder_public_cli.py tunnel show --probe
python qoder_public_cli.py doctor
```

`allowed-models`/`allowed-ips` 可传逗号分隔列表或 JSON 数组。`realm` 留空
继承默认，或设为 `cn`/`intl`。配额 0 表示不限。删除是软删除，需要 `--yes`。

退出码：0 成功；1 连接、API 或运行错误；2 参数错误。`doctor` 逐项报告
配置、数据目录、上游、端口、npc 文件和 bridge TCP，**不证明公网 DNS、证书、
npc 在线或外部用户已成功访问**。
