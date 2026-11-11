# qoder2api-public 排障

按“客户端 → Caddy → nps → npc → gateway → qoder2api”顺序定位。记录时间和
`x-qdp-request-id`，不要把完整 Key、配置或含提示词的请求贴到 issue。

## 1. 先收集只读状态

```sh
# 内网机（完整分支统一运行入口）
python qoder_public_runtime.py status
python qoder_public_cli.py doctor
python qoder_public_cli.py status

# VPS
systemctl status caddy qoder-nps --no-pager
journalctl -u caddy -u qoder-nps -n 100 --no-pager
ss -lntp | grep -E ':(80|443|8024|18080|8080)\b'

# 本机端口可用性；不要把它们暴露到非回环地址
curl http://127.0.0.1:8790/health
curl http://127.0.0.1:8791/healthz
curl http://127.0.0.1:8792/
```

查看 `public-data/logs/`、运行器的 `runtime.log` 和 `npc.log`。
进程存在不代表公网已连通；`tunnel show --probe` 的 TCP 成功也不证明
npc 已登录、host 已匹配或证书有效。

## 2. 按症状处理

### 公网无响应/502，本机 gateway 正常

1. 在 VPS 检查 `https://<API_DOMAIN>/` 是否返回预期 404。证书失败时
   核对 DNS A/AAAA、80/443 防火墙和 Caddy journal；无效的 AAAA 也要修。
2. `curl -v -H 'Host: <API_DOMAIN>' http://127.0.0.1:18080/v1/models`。
   无响应说明 nps 未监听或配置不是回环 18080。
3. 检查 npc 是否连接 VPS:8024、vkey 是否属于预建私有 client、host 段名
   是否为 `[api]`，以及预建 host 的 target 是否为 `api`。
4. 检查 nps host 的域名与 Caddy `header_up Host` 一致；不要改用 IP 直连
   期望命中域名 host。
5. 检查 VPS:8024 安全组、出口 NAT、内网机到 VPS 的 TCP。

### 公网 401，但本机使用用户 Key 成功

期望行为是 Caddy/nps 不鉴权、gateway 鉴权。核对用户用的是 `qdp-...` 而非
内部 Key，Key 是否禁用/过期/轮换；Authorization 或 X-API-Key 是否被客户端
覆盖。`?key=` 一律不接受。轮换后只保存新明文。

### 403 `ip_not_allowed`，或所有审计都是 127.0.0.1

源 IP 链配置有误。Caddy 必须覆盖而非追加用户提供的 XFF/X-Real-IP；
gateway 仅信任 `server.trusted_proxies` 中的直接 nps peer，并解析其追加的
回环地址。不能通过放开所有 XFF、把用户 IP 白名单写成 127.0.0.1 来修复。

直接绕过 Caddy 请求 nps 也必须失败/不可达；检查 `http_proxy_ip` 和防火墙。

### 404，包括模型刷新

确认 URL 没有重复 `/v1/v1`。公网只开放五条 API 路径；
`/v1/models/refresh`、原面板、`/settings`、管理 API 都是有意不开放。
在本机管理端获取模型，不开放 refresh 来“临时修复”。

### 413、400、chunked 或客户端断开

默认请求体上限 32 MiB，超限不会转发。TE+CL、非法分块、重复斜杠、编码
路径穿越等应按 400 拒绝。慢客户端可能触发请求头/请求体超时。SSE 已开始
后断连不能改 HTTP 状态，应检查审计 `outcome`/错误码，而非只看 200。

### 429 或请求一直排队

使用 CLI `usage` 和 `audit` 判断是 RPM、Key 并发、全局并发、日/月配额还是
鉴权失败限速；遵守 `Retry-After`。客户端断连后仍占槽、运行器重复启动、
多个请求并发是不同问题，先 `status` 排除重复实例。不要简单把全部限额设 0。

### 管理页面或 CLI 无法登录

1. 只能从 `127.0.0.1:8792` 或 SSH `-L 8792:127.0.0.1:8792` 访问。
2. Host、Origin、端口必须一致，不能经不受信公网反代访问管理面。
3. 会话过期则重新登录；失败过多会临时锁定。密码仅在 init 输出一次，
   无法从 hash 反推出；按受控运维流程重置配置，不能清除 hash 获得免登录。
4. CLI 必须连接已启动的 gateway 管理线程，不直接修改 Key 文件。

### 初始化、启动或停止失败

- `prepare-server` 要求 nps 已停止、已备份、JSON 文件已存在、ID/域名未占用，
  且显式 `--yes-stopped`。临时文件冲突要先检查，不要直接覆盖。
- 下载脚本必须提供正确 `--sha256`、目标平台/arch/role；官方没有 SHA 清单。
  校验失败应停止，不要重试成跳过校验。
- 运行器启动失败按日志检查端口占用、npc 缺失、配置或上游自检；不能把
  8790/8791/8792 改成非回环来解决冲突。
- 重复 `stop` 应安全；陈旧 PID/不同 instance token 不能停止别人的进程。
  不要按 `python.exe`/`npc.exe` 名字或端口全机查杀。

## 3. 恢复与升级注意

断网恢复只启动 npc/运行器，不应改动 Key。nps 自带 `auto_reconnection=true`，
恢复后仍需从外部验证一次真实 HTTPS。更新 qoder2api 由运行器统一重启，
避免上游自己拉起的进程脱离 PID/Job Object 管理。

修改 `/etc/nps/conf/clients.json` 或 `hosts.json` 前停止 nps 并备份；Caddy
修改后 `caddy validate` 再 reload。nps 和 npc 保持相同兼容版本，重新校验
新资产的 SHA-256。恢复备份前明确其包含哪些 Key/vkey，备份文件同样敏感。

## 4. 测试不能覆盖什么

普通测试与回环 e2e 不能证明公网 DNS、ACME、VPS 防火墙、运营商 NAT、
真实客户端网络、长期断线恢复或主动中间人抵抗。必须保留一次真实 VPS
外部验收，并记录日期、版本和未通过项。
