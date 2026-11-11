# qoder2api-public 测试

## 1. 测试层级

| 层 | 入口 | 依赖与边界 |
|---|---|---|
| 上游与 public 套件 | `python tests/run_all.py` | 假上游、临时目录/端口；不下载、不访问公网、不启动 nps |
| 单项 public 测试 | `python tests/_test_public_core.py` 等 | 快速定位配置、Key、转发、管理或安全回归 |
| 真实本机隧道 | `python tests/e2e/public_nps_local.py ...` | 显式提供固定版本二进制；不改 hosts/DNS，不读真实数据 |
| 手工公网验收 | 部署文档 §5 | 真实 VPS/DNS/ACME/NAT/外部客户端，不能被本机测试代替 |

普通 `run_all.py` 不引用 `tests/e2e/`。CI 的 e2e 是独立、手动触发的
Linux job；没有提供二进制时不能跳过并宣称通过。

## 2. 常规与专项

```sh
python tests/run_all.py --jobs 4 --logs suite-logs
python tests/run_all.py public --jobs 4 --logs public-suite-logs
python tests/_test_public_core.py
python tests/_test_public_gateway.py
```

保留仓库 `_suite_head.py`、`declare_suite()` 和 `SUMMARY` 约定，不为了
public 引入 pytest 或全局测试依赖。改动共享测试头部前先 `rg -l
"_suite_head" tests/`，改后检查总套件数、总断言数和 without-SUMMARY。

数据面安全回归必须覆盖：

- 无/错/空/冲突 Key、query/cookie Key、禁用/过期/轮换/软删除均不触达上游。
- 所有路径和方法、编码路径/重复斜杠、refresh/面板/管理端点拒绝。
- 伪造 XFF、可信 Caddy 链、nps 追加回环、非法/冲突转发头、IP 白名单。
- Content-Length/chunked 边界、TE+CL、错误分块、慢头/慢体、全局连接槽。
- 默认禁止客户端改区；模型/IP/区域、RPM、并发、日/月配额两用户互不干扰。
- SSE 分片、CRLF、注释、空 data、最终 usage、Responses delta、首块不重复
  计量；上游/客户端断连不 hang、不伪造完整响应、只审计一次。

管理/CLI 测试覆盖登录、过期/注销、CSRF/Origin、非回环拒绝、创建/编辑/
启停/轮换/删除、用量过滤、审计无正文，以及管理初始化失败时数据面也
不能无认证降级。

运行器测试覆盖并发 start、陈旧/部分实例 JSON、PID 复用令牌、重复 stop、
启动失败清理、退避、异常恢复、路径含空格。Windows 另测无 console 停止和
Job 进程树；Linux 另测 SIGINT/SIGTERM 和进程组。

## 3. 真实 nps 本机 e2e

详细参数见 `tests/e2e/README.md`。使用 nps/npc v0.26.10，二进制不提交仓库。
该脚本拒绝已有 `/etc/nps` 或 `C:\Program Files\nps` 的环境，避免误用
真实配置；请在隔离 VM/容器中运行。

```sh
# 管理员先核验两个发布资产；下载不属于测试本身。
python tests/e2e/fetch-nps.py --version v0.26.10 --dest /secure/new/e2e-tools \
  --sha256-nps '<VERIFIED_NPS_ASSET_SHA256>' \
  --sha256-npc '<VERIFIED_NPC_ASSET_SHA256>'

# 先只校验参数
python tests/e2e/public_nps_local.py --check-config \
  --nps-path /secure/e2e-tools/nps --npc-path /secure/e2e-tools/npc

# 真 nps/npc + gateway + 假上游；有 Caddy 时加 --caddy-path
python tests/e2e/public_nps_local.py \
  --nps-path /secure/e2e-tools/nps --npc-path /secure/e2e-tools/npc \
  --caddy-path /secure/caddy-test/caddy
```

Windows 使用 `python` 和 `.exe` 路径；Linux 使用 `python3`。也可用
`QDP_E2E_NPS_PATH`、`QDP_E2E_NPC_PATH`、`QDP_E2E_CADDY_PATH`。
缺二进制、路径错误或隔离条件不满足退出 2，绝不记为通过。

当前测试夹具的 Caddy 配置与生产白名单/源 IP 覆盖、私有 client+host
仍需逐项对齐；未对齐前只能证明局部回环隧道，不可把“包含 Caddy”
写成生产请求链验收。生产 `scripts/Caddyfile.example` 才是部署依据。

测试使用临时配置、动态端口、假上游和标记头证明请求经过隧道。验证无/错
Key 401、两个有效 Key 独立成功、禁用失败、非流式/SSE、npc 停止/恢复。
`finally` 精确逆序清理 Caddy → npc → nps → gateway → 假上游，等待句柄和
端口释放，默认删除临时目录；`--keep-temp` 仅用于诊断。

## 4. CI 与公网验收

现有矩阵在 Ubuntu Python 3.9/3.12 和 Windows Python 3.12 运行完整
`run_all.py`，随后检查 checkout 清洁。e2e 仅在 `workflow_dispatch` 的
`run_public_e2e=true` 时执行；手动运行者需提供已核验的 release 资产摘要。

普通 push/PR 不下载二进制。e2e job 有总超时、串行执行和独立日志；不得
用 `continue-on-error` 将其变绿，也不得向仓库安装真实 nps 配置。

最终仍需在真实 VPS 验证 DNS/证书、80/443/8024 防火墙、18080/8080 回环、
内网仅回环 8790–8792、两个 Key 的非流式/SSE、禁用/轮换、npc 断网恢复、
日志脱敏和备份恢复。没有完成这些步骤时，只能报告“普通测试/本机协议
链路通过”，不能报告“公网已验证”。
