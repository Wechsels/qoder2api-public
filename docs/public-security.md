# qoder2api-public 安全手册

## 1. 信任边界

本方案适合少于 5 名已知用户的小团队，不提供军事级、零信任或经认证的
端到端加密保证。信任关系如下：

| 边界 | 保护 | 仍须信任/防护 |
|---|---|---|
| 用户 → VPS | Caddy HTTPS、有效域名证书、自动续期 | DNS、CA、VPS 系统和 Caddy 配置 |
| VPS → 内网 | 私有 nps client、`crypt=true`、固定 host、8024 防火墙 | nps v0.26.10 旧 TLS 无证书身份验证，VPS 可见 HTTP 明文 |
| 内网 npc → gateway → qoder2api | 仅回环、用户 Key 换内部 Key、端点白名单 | 内网机账户、Python 服务、配置与账号文件 |
| 管理员 → 8792/8080 | 仅回环 + SSH 转发；管理会话和 CSRF | 管理员本机、SSH、口令和备份 |

**nps 旧 TLS 风险**：其 v0.26.10 客户端隧道代码使用
`InsecureSkipVerify: true`，服务端使用动态自签证书。`crypt=true` 应避免
明文直接暴露，但不提供可靠的服务端证书身份认证，不能单独抵御主动中间人。
这不是“HTTPS 贯穿到内网”，也不是有身份认证的 E2E。Caddy 只保护用户到
VPS；VPS 和回环上的 nps 能读取 HTTP 请求/响应及内部转发头。

不要把 nps 的 `https_just_proxy=false` + 默认证书重新作为入口；该模式让
nps 终止 TLS，扩大旧组件暴露面。需要更强保护时，应评审并增加隧道内的
认证 TLS/mTLS，而不是宣称现有 `crypt` 已解决此问题。

## 2. 网络与系统基线

- VPS 公网仅开放 Caddy 的 80/443 和 nps bridge 8024；8024 尽可能限制到
  内网出口 IP。SSH 使用密钥、非默认弱口令和适当防火墙。
- nps `http_proxy_ip=127.0.0.1`、`http_proxy_port=18080`；
  `https_proxy_port=`；`web_ip=127.0.0.1`、`web_port=8080`。
- 不配置 `public_vkey`，不开放管理 API 认证豁免。关闭用户注册、公共登录
  和本地代理。预建私有 client 的 `Crypt=true`、固定 host/target。
- 内网 8790/8791/8792 仅回环，Windows 防火墙/云安全组不能例外放行。
  npc 只需出站访问 VPS:8024，不需要入站规则。
- nps 使用无登录系统用户，配置 0600；Caddy/nps/npc 二进制目录仅管理员
  可写。运行时账号不得写代码目录，升级由管理员完成。
- 下载 nps 必须管理员提供独立核验的 `--sha256`；官方无 SHA 清单。固定
  v0.26.10，不下载 latest。校验只证明资产字节匹配，不证明上游无漏洞。

## 3. API Key 与源 IP

- 用户只获得 `qdp-...`，创建/轮换只显示一次；服务端存 SHA-256 摘要和前缀。
  内部 qoder2api Key、nps vkey、管理口令是不同秘密，不能复用或发给用户。
- 禁用、过期、软删除和轮换均 fail-closed。人员离开时禁用/软删除对应 Key；
  怀疑泄露立即轮换。明文丢失不能找回，只能轮换。
- 默认 RPM 60、并发 4，按用户实际用途收紧；日/月 token 为 0 表示不限，
  不等于已配置预算。模型默认不限，敏感部署按 Key 设置白名单。
- 公网路由不包含 `/v1/models/refresh`，也不包含面板、账号、设置、日志、
  管理 API 和 nps 管理端点。Caddy 和 gateway 两层都拒绝，而非只隐藏链接。
- 区域策略在 gateway 决定。禁止客户端用 `X-Realm` 或 `?realm=` 改区；
  如未来显式允许多区，也只能接受 `cn/intl` 并经过策略校验。

**只信任 Caddy 覆盖的源 IP 头**：Caddy 用直接 peer 覆盖
`X-Forwarded-For` 和 `X-Real-IP`，删除其他客户端 IP 头。gateway 只在直接
对端属于 `server.trusted_proxies`（默认回环）时解析该链，且按 nps 追加的
回环节点归一化；非法、重复、冲突或缺失时拒绝。不得盲取 XFF 第一段，
不得信任 `Forwarded`、CF-Connecting-IP 或 X-Client-IP。

IP 白名单、鉴权失败限速和审计必须共用同一解析结果。伪造头不能提权；
共享 NAT 下 IP 不是用户身份，用户身份仍以独立 Key 为准。

## 4. 管理面与秘密存储

- 页面与 CLI 仅访问 `127.0.0.1:8792`；远程使用 SSH `-L`。
  `allow_remote` 不得被解释为开放公网管理。
- 管理口令使用 PBKDF2；登录需要本机 Origin 和请求标记，写操作另需 CSRF。
  会话 HttpOnly、SameSite=Strict、过期和注销；登录失败限速。禁止 CORS。
- CLI 通过本机 HTTP 写 Key，不直接修改 `keys.json`；不实现 `--password`。
  使用交互输入或受保护的 `QDP_ADMIN_PASSWORD`，及时清理不必要的 shell
  环境/历史。浏览器明文 Key 只在创建/轮换模态框出现，不写 URL、存储或日志。
- 配置、npc.conf、`public-data/`、备份在 UNIX 为 0600/0700，Windows 用
  icacls 限制到服务账户和 Administrators。vkey 由私密渠道传输并删除 VPS
  暂存文件。直接传 npc argv 的 vkey可能在本机进程列表短暂可见，优先用
  0600 配置/LoadCredential。
- 审计只记录 Key ID/前缀、IP、路径、状态、用量、耗时和错误码，不记录
  Authorization、Cookie、提示词、模型响应正文、口令散列或完整 vkey。
- 日志轮转且限制保留；错误响应不返回内部 URL、上游凭证、配置或堆栈。
  备份按秘密保护、加密保存并定期恢复演练。

## 5. 漏洞响应与吊销演练

1. **用户 Key 泄露**：CLI `key rotate k_xxx` 或 `key disable k_xxx`；验证
   旧 Key 失败，检查该 Key ID 的审计和配额，不删除审计证据。
2. **npc vkey 泄露**：停止内网 npc 和 VPS nps，备份 `/etc/nps/conf`；在
   受控离线流程中替换私有 client vkey，重新渲染/部署 npc.conf，启动并验证。
   不能用创建公共 vkey 的方式“临时恢复”。
3. **内部 Key/管理口令泄露**：停止统一运行器，替换配置并更新上游内部
   Key；重新启动，作废旧管理会话。检查本机账户和日志。
4. **VPS 疑似失陷**：关闭公网入口、吊销相关秘密、隔离 VPS，从可信备份
   重建。仅更换 API Key 不足以修复 VPS 被控，因为 VPS 可看到明文流量。
5. **异常流量**：按 Key/IP/路径检查审计、429/5xx、nps 和 Caddy 日志；
   收紧 Key 配额、禁用 Key、限制 8024。不要以关闭日志作为止损手段。

每次演练至少记录时间、操作者、影响范围、恢复结果和剩余风险。真实 VPS
失陷或上游凭证泄露应遵循账号提供方的吊销流程，本项目不能代替该流程。
