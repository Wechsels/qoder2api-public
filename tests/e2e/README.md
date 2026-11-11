# 独立真实 nps 回环 e2e

`public_nps_local.py` 验证真实二进制链路：

```text
client -> [optional Caddy] -> nps HTTP domain proxy -> npc -> gateway -> fake upstream
```

它使用真实 nps、npc 和 gateway 进程，以及本脚本内的假上游。不调用真实 Qoder
账号，不读取或修改项目 `public-data/`，不改 hosts/DNS，不下载二进制，不启动公网服务。
测试 Host 默认为 `qoder-e2e.invalid`，客户端直接连接环回端口并发送该 Host。

此脚本不属于上游 `tests/run_all.py` 的 `_test_*.py` 套件，需显式运行。

## 准备

- Python 3.9+；使用项目现有的标准库 public gateway。
- nps 与 npc **相同兼容版本**；当前配置按 nps `v0.26.10` 编写。
- nps 和 npc 二进制必须与当前 OS/CPU 匹配。
- nps 0.26.10 会优先读取 `C:\Program Files\nps` 或 `/etc/nps`。检测到其中任一
  已存在的安装目录时，脚本退出 2，以免误用真实配置。请在隔离 VM/容器中运行，
  或先为 nps 增加显式配置目录参数；不要删除真实安装来解决此问题。

**未提供二进制、路径无效或不满足隔离条件，都退出 2，绝不记为通过。**

先校验参数而不启动任何进程：

```powershell
# Windows PowerShell，从项目根目录
python tests/e2e/public_nps_local.py --check-config `
  --nps-path C:\tools\nps\nps.exe --npc-path C:\tools\nps\npc.exe

# 完整真实回环测试
python tests/e2e/public_nps_local.py `
  --nps-path C:\tools\nps\nps.exe --npc-path C:\tools\nps\npc.exe

# 可选：把 Caddy 加入客户端与 nps 之间
python tests/e2e/public_nps_local.py `
  --nps-path C:\tools\nps\nps.exe --npc-path C:\tools\nps\npc.exe `
  --caddy-path C:\tools\caddy\caddy.exe --keep-temp
```

```bash
# Linux，从项目根目录
python3 tests/e2e/public_nps_local.py --check-config \
  --nps-path /opt/nps-test/nps --npc-path /opt/nps-test/npc

python3 tests/e2e/public_nps_local.py \
  --nps-path /opt/nps-test/nps --npc-path /opt/nps-test/npc

# 可选 Caddy
python3 tests/e2e/public_nps_local.py \
  --nps-path /opt/nps-test/nps --npc-path /opt/nps-test/npc \
  --caddy-path /opt/caddy-test/caddy --keep-temp
```

也可设置 `QDP_E2E_NPS_PATH`、`QDP_E2E_NPC_PATH`、可选 `QDP_E2E_CADDY_PATH`。
命令行参数优先于环境变量。`--keep-temp` 保留临时配置和日志；默认退出时清理。
`--work-dir <目录>` 用显式目录（须为空）代替系统临时目录且从不自动清理，
便于在临时目录被外部清理策略影响的机器上保留诊断日志。

下载工具是**可选的用户前置步骤**，e2e 本身不联网：

```bash
python tests/e2e/fetch-nps.py --version v0.26.10 --dest /path/to/empty/tool-dir \
  --sha256-nps <对应发布资产的SHA256> --sha256-npc <对应发布资产的SHA256>
```

它支持 `windows_amd64`、`linux_amd64`、`linux_arm64`，只下载官方 GitHub release，
要求显式提供两个 SHA-256，不下载 Caddy。校验值应人工从对应 release 的校验文件取得。
运行后使用输出的 nps/npc 路径调用 e2e。不要将下载物放入仓库。

## 断言与隔离

- 无 Key 返回 401 且不触达上游；合法 Key 成功；禁用 Key 返回 401。
- 非流式和 SSE 完整穿过隧道，包含 `[DONE]`。
- 假上游收到内部 API Key，以及由 nps `HeaderChange` 写入的 `X-Nps-E2E-Marker`
  标记；这证明请求经过隧道，而非客户端绕到 gateway。标记头名刻意避开 `x-qdp-`
  前缀——网关会剥离一切 `x-qdp-*` 请求头（防伪造内部标记）。
- 停止 npc 后请求失败且不触达上游；重新启动 npc 后恢复。
- 所有配置、密钥、nps 私有 client/host JSON、日志均在系统临时目录；nps 是
  临时目录中的专用副本。所有监听均为 `127.0.0.1`，管理端口不会暴露到公网。
- 每个进程记录实际 PID 和独立日志，退出时逆序精确清理。Windows 按 PID 清树，
  Linux 使用独立进程组，先 TERM 后 KILL；绝不按程序名杀进程。
- 动态端口采用“绑定环回端口、取得端口号、关闭后立即启动”的常规测试方式，
  仍有不可避免的端口竞争窗口；端口被抢用时应失败并重跑，而不是换测真实服务。

## 验证边界

通过只证明本机真实协议链路和进程恢复，不证明公网可达。没有 VPS 时，以下均未验证：
公网 DNS、TLS 证书签发与信任、VPS 防火墙/安全组、NAT 出口、外部客户端、长期断线重连、
真实 Qoder 上游账号与配额。Caddy 未提供时，也不包含 Caddy 兼容性验证。

最终仍需在受控 VPS 上执行一次人工公网验收。此脚本不得指向公网地址，也不得作为
部署或启动生产服务的工具。
