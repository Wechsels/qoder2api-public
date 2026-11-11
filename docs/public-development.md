# qoder2api-public 开发指南

## 1. 分支与文件边界

开发分支为 `feat/public-gateway-nps`，完成后由用户手动合并 main。保留
qoder2api 的历史、协议和模块风格；public 功能放在独立 `qoder_public_*`
模块，不复制一套上游面板或改写上游路由。

当前分层：

```text
qoder_public_config.py   配置、默认值、环境覆盖、秘密散列
qoder_public_keys.py     原子 KeyStore、生命周期、策略与累计
qoder_public_meter.py    OpenAI/Anthropic usage、JSONL 与聚合
qoder_public_gateway.py  数据面、管理线程宿主、HTTP 转发与审计
qoder_public_admin.py    仅回环管理 API；dashboard 为静态本地资源
qoder_public_cli.py      离线 init + 本机管理 API 客户端
qoder_public_tunnel.py   nps 私有 client/host 离线准备与模板渲染
qoder_public_runtime.py  统一启停/探活/看护（随运行器任务交付）
scripts/                 固定版本下载、VPS 初始化、Caddy/nps/npc 模板
tunnel/                  systemd 模板，不含秘密或二进制
```

`public-data/`、生成的 Caddy/nps/npc 配置、`run/`、下载目录和证书均不提交。
不要把开发机当前目录、API Key、管理密码或 VPS vkey 写进示例。

## 2. 不可破坏的约定

- Python 3.9+、标准库、Linux/Windows；路径用 `pathlib`/安全路径 API。
- 请求链固定 Caddy:443 → nps 回环:18080 → npc → gateway:8791 →
  qoder2api:8790；bridge 8024，nps admin 回环:8080，管理面回环:8792。
- 不启用 nps 公网 TLS，不配置 public_vkey，不允许 npc 任意声明隧道。
- 路由白名单不含 `/v1/models/refresh`。配置可启用的路由必须来自唯一
  `PUBLIC_ROUTES` 表，避免“配置成功但运行时 404”。
- 用户 Key 与内部 Key 分离。在线管理只能通过 8792 共享状态；不得让 CLI
  与 gateway 同时写 `keys.json`。
- 源 IP 只信 Caddy 覆盖且经 nps 到达的头；不信任任意 XFF。
- 不自动重试推理；流式逐块转发，usage 只计一次，客户端断连释放并发。

## 3. 本地开发

使用隔离数据目录，避免读取真实账号和 public 配置：

```sh
python qoder_public_cli.py --config <临时目录>/config.json init
python tests/run_all.py --jobs 4 --logs <临时日志目录>
```

普通测试用动态端口、临时目录和假上游，不需要真实 Qoder 账号、VPS、
Caddy/nps/npc 或网络。不要改系统 hosts/代理，不要向仓库写测试数据。

隧道模板使用 `scripts/` 中的无秘密样例。实际 `render` 必须输出到新的
私密目录；已有文件默认拒绝覆盖。`prepare-server` 是 nps v0.26.10 JSON
格式操作，要求服务停止、备份、已有 JSON 文件和 `--yes-stopped`。

## 4. 修改时的检查点

### 数据面

检查每个拒绝分支是否恰好一次鉴权失败计数、恰好一条审计，且不触达上游。
请求体读取要覆盖 Content-Length/chunked、跨 TCP 分片、TE+CL 和超限；连接
槽必须从头读取前覆盖至连接结束。共享信号量的 lease 要幂等释放。

修改 `qoder_public_meter.py` 时验证首个 SSE 块、CRLF、跨包事件、无 usage、
截断 JSON、Responses `response.output_text.delta` 和 Anthropic usage。

### 管理面/CLI

检查 loopback peer/Host、Origin、CSRF、会话过期/注销、登录限速、无 CORS、
页面 CSP 和 `textContent`。秘密只在 create/rotate 的响应出现一次；递归扫描
API/页面/CLI/日志，确保无 hash、内部 Key、完整 vkey 或提示词。

### 运行器/隧道

启动失败要清理本轮服务；异常退出按关键程度恢复；停止中禁止重启。
Linux 用进程组，Windows 用精确 PID + Job Object，绝不按名称全机查杀。
`QDP_PUBLIC_MANAGED=1` 下上游不得自重启脱离看护。

## 5. 提交前

1. `git diff --check`，并只暂存本任务允许的文件。
2. 跑 public 套件和完整 `tests/run_all.py`；记录总数与失败。
3. 实际修改运行器时分别做 Windows/Linux 进程生命周期测试。
4. 只有真实 e2e 脚本存在且需要运行时，才显式运行它；普通测试不下载。
5. 核对部署/API/排障命令与 argparse/模板一致；本机 e2e 不写成公网已验证。

文档/CI 修改不需要擅自 Git 提交。合并、上传和真实 VPS 变更由用户决定。
