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

## 🌐 公网部署：qoder2api-public（nps 内网穿透 + 按人分发的 API Key）

本仓库的 `feat/public-gateway-nps` 分支开发 **qoder2api-public**：在 qoder2api
前加独立网关，用 Caddy + [nps](https://github.com/ehang-io/nps) 将内网服务发布到
公网域名。按用户签发 Key，分别管理启停、轮换、用量、限速、模型白名单和有效期
（少于 5 人的小团队）。

- **不改动反代本体**：网关是独立进程（`qoder_public_*.py`），`qoder_proxy.py` 保持上游原样，便于跟进上游更新。
- **公网只暴露白名单端点**：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/messages`、`/v1/messages/count_tokens`；不含 `/v1/models/refresh`，面板、`/settings`、nps 管理台全部留内网。
- **两种 Key**：用户拿到的是 `qdp-...`（只存 SHA-256 摘要，签发时显示一次），网关再用内部 Key 访问本机 qoder2api。
- **管理仅本机**：页面和 CLI 通过 `127.0.0.1:8792` 管理；远程运维走 SSH 转发。

```text
users ──https──▶ VPS Caddy:443 ─▶ nps 127.0.0.1:18080
                     ▲                         │ nps bridge :8024
                     └──── 私有 client+host ───┘
内网机：npc ─▶ 网关:8791 ─▶ qoder2api:8790；管理:8792（仅回环）
```

nps 隧道启用 `crypt=true`，但 v0.26.10 的旧 TLS 不验证服务端证书身份；
不将其称为端到端加密，VPS 仍属可信边界。

| 文档 | 内容 |
|---|---|
| [设计](docs/design-public-gateway.md) | 架构、Key 模型、威胁模型、验收标准 |
| [部署](docs/public-deployment.md) | VPS（Linux）+ 内网机（Windows/Linux）从零到可用 |
| [安全](docs/public-security.md) | 加固清单、密钥生命周期、吊销演练 |
| [对外 API](docs/public-api.md) | 端点、鉴权、错误码、客户端接入 |
| [排障](docs/public-troubleshooting.md) | 分层排查与常见坑 |
| [开发](docs/public-development.md) / [测试](docs/public-testing.md) | 文件边界、测试层级、CI 与验收 |

初始化与运行（在内网机仓库根目录）：

```bash
python qoder_public_cli.py init          # 生成 public-data/config.json（随机秘密，只打印一次）
./start-qoder-public.sh start            # 后台拉起 qoder2api + 网关（+npc，若已配置）
./start-qoder-public.sh status           # 实例状态与各进程活性
./start-qoder-public.sh restart          # 逆序停止后重新拉起
./stop-qoder-public.sh                   # 逆序停止 npc -> 网关 -> qoder2api
```

Windows 用 `start-qoder-public.bat` / `stop-qoder-public.bat`；Linux 常驻可用
`tunnel/qoder-public.service.example`（systemd）。逻辑都在 `qoder_public_runtime.py`
里：单实例锁、就绪检查（校验 /healthz 返回的 PID）、有界退避重启。前台调试用
`./start-qoder-public.sh run`，Ctrl+C 停止。

Key 管理与观测（全部经本机管理 API，需 `init` 时打印的管理密码）：

```bash
python qoder_public_cli.py key add alice --rpm 60 --daily-tokens 2000000
python qoder_public_cli.py key list
python qoder_public_cli.py key disable <id>   # 停用；enable 恢复
python qoder_public_cli.py key rotate <id>    # 轮换，新 Key 只显示一次
python qoder_public_cli.py key revoke <id> --yes   # 软删除
python qoder_public_cli.py usage --days 7     # 按 Key 汇总用量
python qoder_public_cli.py audit --limit 50   # 请求审计流水
python qoder_public_cli.py doctor             # 本机配置/端口/隧道前置检查，不代表公网验收
```

也可以直接用浏览器打开 **http://127.0.0.1:8792/** 管理面板（仅回环）：登录、
Key 增删启停/轮换、用量与审计图表；远程运维走 SSH 端口转发。

测试不需要真实账号或公网：

```bash
python tests/run_all.py public    # 7 套件 226 项检查（假上游 + 真网关，全本机回环）
```

真实 nps/npc 回环 e2e 是独立显式入口（17 项检查），需自备核验过 SHA-256 的
v0.26.10 二进制，见 `tests/e2e/README.md`；CI 仅在手动触发并输入核验摘要时运行。

完整启动、VPS 初始化和跨平台部署按上述部署手册执行。

> 仍然可以直接按下面的「快速开始」使用原有的本机反代，公网部署是可选的叠加层。

## 特性

- **三种协议**：OpenAI Chat Completions、Responses API（Codex）、Anthropic Messages（Claude Code）
- **双区多账号**：国内版 / 国际版独立账号池与出口，按模型自动选区；账号轮询、会话亲和、故障冷却换号
- **免客户端登录**：OAuth 设备授权、PAT 导入，或直接导入本机官方客户端已登录的凭证
- **模型与官方同源**：动态拉取官方清单，含峰谷价、上下文窗口、思考档位和禁用原因
- **签到与保活**：自动领取每日 Credits 活动，定时刷新 Token
- **防风控**：出站身份形态对齐官方客户端，可选的单号并发闸门与请求节奏控制，`/health` 提供排队 / 限流 / 拒绝信号遥测
- **Web 看板**：账号、用量与性能指标、请求流水、限额护栏、智能体一键配置
- **零依赖**：只用 Python 标准库（COSY 签名、AES / RSA 都是纯 Python 实现），另提供 amd64 / arm64 Docker 镜像

## 快速开始

需要 Python 3.9+ 或 Docker。先获取代码：

```bash
git clone https://github.com/shuishuipingan/qoder2api-hub.git
cd qoder2api-hub
```

然后选一种方式启动：

| 平台 | 启动 | 停止 |
|---|---|---|
| Windows | 双击 `start-qoder-proxy.bat`（后台运行，日志写入 `qoder-proxy.log`） | `stop-qoder-proxy.bat` |
| Linux / macOS | `chmod +x *.sh && ./start-qoder-proxy.sh`（前台运行；会写 `qoder-proxy.pid`） | `Ctrl+C`，常驻时用 `./stop-qoder-proxy.sh` |
| Docker | `docker compose up -d` | `docker compose down` |

启动后：

- **API**：`http://127.0.0.1:8790/v1`
- **看板**：`http://127.0.0.1:8790/`，默认密码 `admin`，首次登录后请到「设置」修改

在看板「账号」页点 **+ 添加账号 (OAuth)**，到浏览器里完成授权，账号就加好了。

> Docker 以局域网模式启动，会强制使用 API Key。首次启动时自动生成，用 `docker compose logs qoder-proxy | grep -i "api key"` 查看。
> 局域网共享、改端口、出口代理见[部署](docs/deployment.md)。

## 接入客户端

| 客户端 | Base URL | 协议 |
|---|---|---|
| OpenAI 兼容客户端（Cherry Studio、Chatbox、NextChat 等） | `http://127.0.0.1:8790/v1` | Chat Completions |
| Codex CLI | `http://127.0.0.1:8790/v1` | Responses API |
| Claude Code | `http://127.0.0.1:8790`（不带 `/v1`） | Anthropic Messages |

- **模型名**：填 `/v1/models` 返回的 `id`，也就是官方模型名，例如 `Qwen3.8-Max`
- **API Key**：在看板「设置」中生成。本机运行且没有配置任何 Key 时，可以留空

```bash
curl http://127.0.0.1:8790/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "Qwen3.8-Flash", "messages": [{"role": "user", "content": "你好"}]}'
```

看板「智能体配置」覆盖 **31 个热门客户端 / Harness**，其中 **13 类支持自动配置与还原**：Claude Code、Codex CLI、OpenCode、DSH、Crush、Aider、Continue、Pi、Factory Droid、GitHub Copilot CLI、OpenHands CLI、Kilo CLI、Mistral Vibe。其他客户端按实际能力提供安装检测和手动接入指引；API 示例和客户端模板只使用 `YOUR_API_KEY`。详见[客户端接入](docs/clients.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [部署](docs/deployment.md) | 本机 / 局域网 / Docker 部署、防火墙、出口代理、升级 |
| [配置参考](docs/configuration.md) | 命令行参数、环境变量、API Key、模型闸门、限额护栏 |
| [账号与签到](docs/accounts.md) | 添加账号、凭证生命周期、签到与活动规则、定时调度 |
| [客户端接入](docs/clients.md) | 各协议的接入方式、智能体一键配置 |
| [模型](docs/models.md) | 模型来源、命名、双区路由、峰谷价、思考档位 |
| [接口参考](docs/api.md) | HTTP 端点与鉴权方式 |
| [故障排查](docs/troubleshooting.md) | 常见问题、诊断脚本、已知限制 |
| [风控与排队](docs/risk-control-and-queueing.md) | 上游的风控维度与处置等级、多账号为什么仍会排队、官方客户端逆向结论 |
| [风控处置手册](docs/risk-control-playbook.md) | 出口 / 身份 / 调度 / 请求 / 观测各层的处置办法，容器与数据中心的落地方案 |
| [实现原理](docs/internals.md) | 请求链路、设备指纹、容错重试、工具调用回读 |
| [开发与测试](docs/development.md) | 模块结构、测试、维护脚本 |
| [更新日志](CHANGELOG.md) | 各版本变更 |

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
