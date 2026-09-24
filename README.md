# Math Agent Platform

面向数学建模竞赛的多 Agent 协作工作台。

当前工程以“任务、交接、成果物、证据和审核门禁”为核心领域对象，提供：

- 多队伍/项目空间模型
- CUMCM 工作流模板和现有目录导入约定
- FastAPI 控制平面
- 本地 Agent 注册、心跳和任务领取接口
- 成果物哈希、运行记录和事件时间线
- 面向项目负责人的协作工作台

## 项目结构

```text
apps/api/       FastAPI 控制平面和 SQLite 开发存储
apps/web/       Next.js/React 协作工作台
apps/agent/     本地 Agent CLI/守护进程原型
packages/       跨端领域契约和赛事模板
infra/          Docker Compose 与部署配置
docs/           架构、协议和实施说明
```

## 关于"下载即用"的桌面端

桌面端是"壳（Electron）+ 内核（Python 常驻体）"两个进程，**只通过回环 HTTP 契约通信**。
现状（2026-09-16）：

- **DP-0 / DP-1 / DP-2 已退出**：契约冻结（`docs/SIDECAR_CONTRACT.md`）、NSIS 安装包（未签名，137MB，`apps/desktop/dist/`）、
  常驻任务循环与本地 Agent 监测（装/配后不改配置，平台建 `worker_executor=codex` 任务即自动领取执行，
  实测 3–10 秒领取、30–60 秒完成）。
- **还没做**：本地诊断页与诊断包（DP-3）、代码签名/自动更新/开机自启/下载页（DP-4）、跨平台（DP-5，按需）。
- 路线与验收口径：`docs/DESKTOP_CLIENT_PLAN.md`（选型）、`docs/DESKTOP_CLIENT_IMPLEMENTATION_PLAN.md`（分阶段）与各阶段交接 `docs/handoffs/DP_*_HANDOFF.md`。

未装桌面端时，下面这条"仓库 + Python"的路径**依然有效**，且是排障时的第一选择。

## 最快上手（推荐）

```powershell
# 终端 1：基础设施（PostgreSQL + MinIO）+ API + Web 三件一起起
.\scripts\start-demo.ps1
```

启动后打开 <http://127.0.0.1:3000>。首次运行会自动构建前端（约 40 秒），之后几秒即可用。

**上手后的前 15 分钟**（这也是 Demo 1.0 的验收路径）：

1. **设置页**填 LLM / Embedding / MinerU 凭据（有 DeepSeek、SiliconFlow、通义千问、OpenAI 预设；可点「测试连接」逐项验证）；
2. **总览页**「新建项目」→ 选竞赛模板包与题号；
3. **建模模板包页**「一键应用」→ 生成四问任务与模板成果物（会先弹确认，告诉你将新建多少条）；
4. **设备与接入页**「生成配对」→ 复制命令到要接入的机器执行 → 「授权到项目」→ 复制 worker 命令；
5. 在**任务页**建一个任务并填入执行命令，粘上一步的 worker 命令 → Agent 自动领取并执行；
6. 去**审核门禁页**人工批准，在**交接中心**接受交接，在**文档版本页**编辑并「保存草稿」。

> 一键自检：`python -m uvicorn scripts.acceptance_empty_api:app --port 8010` 起一个空数据库实例后，
> `\scripts\demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove` 会跑完整链路并逐项断言（详见 `docs/DEMO_1_0_ACCEPTANCE.md`）。

**检索服务是独立进程**：知识库索引与超图检索需要另外启动 `hyper-rag-service`（端口 8100），
平台通过 HTTP 代理访问它，不改动其代码。不启动它时，除知识库索引/检索外的功能都可用。

## 本地启动（分步）


### API

```powershell
.\scripts\start-api.ps1
```

脚本会优先使用项目虚拟环境，其次使用系统 Python，最后使用 Codex 工作区 Python。也可以手动使用工作区自带 Python：

```powershell
$env:PYTHONPATH = "$(Resolve-Path 'apps/api');$(Resolve-Path 'apps/api/vendor')"
& "C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -c "import sys; sys.argv=['uvicorn','app.main:app','--reload','--port','8000']; from uvicorn.main import main; main()"
```

### Web

```powershell
.\scripts\start-web.ps1
```

访问 `http://localhost:3000`。前端默认访问 `http://localhost:8000`。

> **改完前端必须重建并重启 Web 进程。** `next start` 在启动时就把构建清单和预渲染路由缓存进内存，之后改代码只重建 `.next` 而不重启进程，页面会一直发旧的预渲染 HTML（引用旧 chunk 名），表现为「改了但页面没变化」，且往往连浏览器缓存都排除了还是老样子。正确做法：`cd apps\web; npm run build`，然后 Ctrl+C 停掉正在跑的 Web 再重新启动。`scripts\start-demo.ps1` 只在 `.next` 缺失时才自动构建，不会替你重建。

### 部署到服务器（多机使用）

关键约定：**工作台（Web）构建时把 API 地址内联进前端**。所以"别人机器上的浏览器 / 桌面端怎么找到你的服务器"由这一个变量决定。

```powershell
# 服务器上（假设服务器 IP 是 10.0.0.5）
$env:NEXT_PUBLIC_API_URL   = "http://10.0.0.5:8000"          # 前端与深链都用它
$env:PLATFORM_CORS_ORIGINS = "http://10.0.0.5:3000"          # 允许的工作台来源（逗号分隔可多写）

# API：要监听 0.0.0.0，否则只有本机能连
cd apps/api
python -X utf8 -m uvicorn app.main:app --host 0.0.0.0 --port 8000

# Web：同样要监听 0.0.0.0；改了 NEXT_PUBLIC_API_URL 必须**重新构建**（它被内联）
cd apps/web
npm run build
npx next start -H 0.0.0.0 -p 3000
```

Docker 方式等价（`infra/docker-compose.yml`，API 镜像已监听 `0.0.0.0`）：

```bash
NEXT_PUBLIC_API_URL=http://10.0.0.5:8000 \
PLATFORM_CORS_ORIGINS=http://10.0.0.5:3000 \
docker compose -f infra/docker-compose.yml up -d --build
```

接入另一台机器时：

1. 浏览器打开 `http://10.0.0.5:3000` →「设备与接入」→「生成配对」；
2. 装了桌面端就点「接入这台电脑」（深链里的平台地址就是 `NEXT_PUBLIC_API_URL`，内核会记进 `platform.json`）；
   没装桌面端就在那台机器上跑 `.\scripts\connect-agent.ps1 -Url http://10.0.0.5:8000 -AgentName "工作站" -Pairing <配对串>`；
3. Agent 连平台用的是 WebSocket：http → `ws://`、https → `wss://`，由平台地址自动推导，不需要另配；
4. 记得放行端口：`8000`（API/WS）、`3000`（工作台）；HTTPS 建议在前面放反向代理，并把 `NEXT_PUBLIC_API_URL` 改成 `https://…`。

> 症状对照：远程访问时页面能打开但数据全是空的、控制台报 `Failed to fetch`，基本就是
> `PLATFORM_CORS_ORIGINS` 没带上工作台的来源（默认只允许本机 `localhost:3000` / `127.0.0.1:3000`）。

### 常驻内核：装完就待着，有任务自动执行（DP-2）

桌面端内核的常驻形态是 `daemon-run`：本地契约服务 + 平台连接监督 + 任务循环 + 状态镜像，同一个进程。

```powershell
$env:PYTHONPATH = "$(Resolve-Path '.')";$(Resolve-Path 'apps/agent')
# 未配对也能启动：先提供本地契约服务，网页点「接入这台电脑」后自动开始连接（不必重启）
python -u -X utf8 apps/agent/agentd.py daemon-run --url http://127.0.0.1:8000 --workspace <仓库路径>
```

要点：

- **平台地址与设备标识自己记**：配对成功后写 `%LOCALAPPDATA%\MathAgentPlatform\platform.json`（只有地址与标识，**没有 Token**），
  下次开机不带参数也能连回同一个平台；
- **授权可以运行期交付**：网页「设备与接入 → 授权到项目」后点「把授权交给本机桌面端」（`map://grant`），
  内核立即开始领该项目任务，不需要重启，也不用手工复制 Token；
- **契约端点**（`docs/SIDECAR_CONTRACT.md`）：`/health` `/status` `/pair` `/grant` `/agents/rescan` `/tasks/pause|resume` `/emergency-stop|clear` `/logs` `/shutdown`；
- **暂停**只停"领取"，正在跑的任务不受影响；**紧急停止**会停领取并断开平台连接（平台侧随即看到掉线），
  但内核**不退出**——所以托盘能「解除紧急停止」，解除后自动重连恢复；
- 只要没有项目授权，内核照常连接并上报本机清单，只是不领取任务（不会假装"在干活"）。

`worker-run`（前台、单轮/循环、打印到终端）保留为调试入口，与常驻体共用同一套任务循环实现。
只想要本地契约服务（不连平台）用 `sidecar-run`。

### 本地 Agent

**主路径：用接入向导，三条输入完成接入。**

1. 在 Web 打开「设备与接入」（`/devices`），点「生成配对」，填一个 Agent 名称；
2. 复制向导给出的命令，到**要接入的那台机器**上执行（需要本仓库，或把 `scripts/connect-agent.ps1` 拷过去）；
3. 按脚本最后打印的命令启动 Gateway 连接（保持前台运行）。

脚本内部依次完成：登记 Agent（幂等）→ 生成 Ed25519 密钥 → 用私钥签名完成设备注册 → 把设备 Token 写入 Windows 凭据管理器 → 打印可直接粘贴的 `gateway-run` 命令。**不需要手工编造 `session_id` / `connection_id`，也不需要自己生成密钥。**

要点与边界：

- **一个公钥只能注册一台设备**（服务端校验指纹唯一），密钥名默认跟随设备标识；同一台机器接入第二台设备请给 `-DeviceId`。
- **设备已存在**时脚本会提前报错并给出出路：只想重启连接就直接跑 `gateway-run`；要重装或换机器，先在「设备与接入」页撤销旧设备，再用新配对重新接入。
- 设备 Token 只在注册响应里出现一次，平台只存 SHA-256，**无法回显**；`credential-save` 是唯一的持久化路径。
- 配对码默认 15 分钟有效且只能用一次，过期后在向导页重新生成。
- 连接断开后平台在 **90 秒**内把该 Agent 标记为离线（3 × 30s 心跳周期）；租约过期的任务按「未开工 → READY / 已开工 → NEEDS_REVISION」回收。
- **接入只解决身份与连接**：Agent 要出现在项目看板并领取任务，还需要项目范围授权（项目能力 Token 或 `agent_project_grants`），属 Agent 任务闭环（UX-5）范围。

手工生成密钥（脚本已包含，一般不需要）：

```powershell
python -X utf8 apps/agent/agentd.py keygen --directory "$HOME\.math-agent-platform\keys" --name <设备标识>
# 输出私钥路径、公钥路径与公钥指纹（DER SPKI 的 SHA-256，与服务端一致）
```

### 用 Codex 作为执行体（可选）

装了 ChatGPT 桌面端就自带 Codex CLI（在 `%LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe`，
注意**不要**直接从 `Program Files\WindowsApps` 运行，那里被 ACL 拒绝）。把任务交给 Codex 只需两步：

```powershell
# 1) 接入时加 -Codex：自动探测 CLI 并打印版本
.\scripts\connect-agent.ps1 -Url http://127.0.0.1:8000 -AgentName "我的工作站" -Pairing <配对串> -Codex
```

```json
// 2) 任务声明执行体（界面建任务暂未暴露该字段，可用 API）
{ "title": "让 Codex 读 README 并给结论",
  "resource_policy": { "worker_executor": "codex", "worker_prompt": "读取 README.md 的第一行标题并只回复该行" } }
```

worker 会自动探测 Codex 路径（显式参数 → `CODEX_CLI_PATH` → 桌面端解包目录 → PATH），首次跑完记进 `worker.json`。
写盘类任务需要放宽沙箱：`worker-run ... --codex-sandbox workspace-write`（默认 `read-only`）。

平台强制的安全约束：拒绝 `danger-full-access`、拒绝 `--dangerously-bypass-*`、拒绝路径逃逸与配置覆盖类参数；
环境变量按显式白名单传递（**禁止继承父环境**——缺 `USERPROFILE` 时 Codex 会找不到 `~/.codex`，表现为"连不上服务商"）。

完整说明、实测记录与排障表见 `docs/CODEX_EXECUTOR.md`。

### 开发调试：手工命令与老轨 HTTP

以下命令用于调试与协议验证，不是用户接入路径。老轨（`register` / `heartbeat` / `claim` 系列）不发放设备凭证，`agent_id` 由调用方自带，仅建议本机开发使用。

```powershell
$env:PYTHONPATH = "$(Resolve-Path 'apps/agent');$(Resolve-Path 'apps/api');$(Resolve-Path 'apps/api/vendor')"
python apps/agent/agentd.py --url http://localhost:8000 register --agent-id agent-mira --display-name "Mira Agent"
python apps/agent/agentd.py --url http://localhost:8000 heartbeat --agent-id agent-mira
python apps/agent/agentd.py --url http://localhost:8000 claim --agent-id agent-mira --project-id <PROJECT_ID> --project-token <PROJECT_TOKEN>
python apps/agent/agentd.py --url http://localhost:8000 progress --agent-id agent-mira --task-id <TASK_ID> --lease-token <LEASE_TOKEN> --project-token <PROJECT_TOKEN> --status RUNNING
python apps/agent/agentd.py --url http://localhost:8000 complete --agent-id agent-mira --task-id <TASK_ID> --lease-token <LEASE_TOKEN> --project-token <PROJECT_TOKEN> --summary "运行完成"
```

任务与运行协议的幂等键可用 `--key` 显式指定：断线后用同一个键重试会返回原结果，不会重复领取或重复提交。机器写请求必须携带项目范围能力 Token（`X-Project-Capability-Token`），服务端按项目、Agent 与具体能力校验。

设备配对与 Gateway 的原始调用（`connect-agent.ps1` 已封装，这里用于排查协议问题）：

```powershell
# 配对注册：服务端一次性返回 pairing_code 与 challenge，用私钥签名后提交。
# 注意用 --opt=value 形式：challenge 是 base64url 令牌，可能以 - 或 _ 开头，空格分隔会被当成选项。
python apps/agent/agentd.py --url http://localhost:8000 device-register --pairing-code=<CODE> --pairing-id=<ID> --challenge=<CHALLENGE> --agent-id <AGENT_ID> --device-id <DEVICE_ID> --device-name <NAME> --private-key <私钥路径>

python apps/agent/agentd.py gateway-queue --device-id <DEVICE_ID> --agent-id agent-mira --session-id <SESSION_ID> --connection-id <CONNECTION_ID> --message-type agent.event --payload '{"run_id":"<RUN_ID>","status":"RUNNING"}' --idempotency-key event-001
python apps/agent/agentd.py gateway-recover --state-path "$HOME\.math-agent-platform\agentd.db"
python apps/agent/agentd.py credential-save --device-id <DEVICE_ID> --token-stdin
python apps/agent/agentd.py gateway-run --uri "ws://localhost:8000/ws/agents/<DEVICE_ID>?session_id=<SESSION_ID>&connection_id=<CONNECTION_ID>" --device-id <DEVICE_ID> --agent-id agent-mira --session-id <SESSION_ID> --connection-id <CONNECTION_ID>
```

`credential-save` 把设备 Token 存入 Windows 凭据管理器（目标 `MathAgentPlatform/device-token/<DEVICE_ID>`），`gateway-run` / `service-run` 默认按设备 ID 读取；`--device-token` 仅用于开发兼容。设备 Token 不写入本地 SQLite 状态库。如果当前 Windows 登录环境不允许 `CRED_PERSIST_LOCAL_MACHINE` 持久化，命令会明确失败，不会悄悄改存为仅本次登录有效的会话凭据。

Gateway 任务命令使用 `agent.task.claim`、`agent.task.lease.heartbeat`、`agent.task.progress`、`agent.task.result`、`agent.run.create` 和 `agent.run.complete`；命令 payload 需要 `project_id` 与 `project_token`，成功结果通过 ACK 的 `command_result` 返回；结果按连接和幂等键持久化。跨端 Gateway 类型位于 `packages/agent_protocol`。

Windows 侧的 Session Worker、ConPTY、Machine Service 安装与运行属于生产验收步骤（`service-install` / `service-control` / `session-worker-run` 需要管理员权限，自动化测试不执行）：`python apps/agent/agentd.py --help` 可查看全部子命令，`docs/AGENT_DEVICE_CONNECTION_DESIGN.md` 记录了设计约束。

## 当前实现边界

本仓库先实现完整目标架构的核心契约和可运行纵向链路：项目、任务、交接、成果物、Agent、事件、审核、CUMCM 工作区导入、任务租约和 Run Manifest。生产环境切换 PostgreSQL、对象存储、NATS、Temporal 和正式身份服务时，API 契约保持不变。

现有 `C题工作区`、`C题四问完整交接包` 和 `competition-workflow` 不会被修改；后续通过导入适配器接入。

## 改动须知（Demo 1.0 期间）

当前主线见 `docs/DEMO_1_0_IMPLEMENTATION_PLAN.md`（Demo 1.0，已收尾）与 `docs/DESKTOP_CLIENT_IMPLEMENTATION_PLAN.md`（桌面端，进行中：DP-2 已退出）。
开工前先读对应文档的 §0、§3、§4；桌面端另见 §6.5b 的决策追加（DE14–DE17）。

**禁区**（本阶段禁止改动，理由见计划书 §6.4）：`apps/api/app/collaboration.py` 的中继语义、`apps/api/app/gateway.py` 的逐帧应答模型、`packages/agent_protocol/` 的信封与序号语义、`packages/competition_packs/` 的内置包内容、`apps/api/app/kb_gateway.py` 的错误码映射、`infra/docker-compose.yml`、**既有** PostgreSQL 迁移脚本（新增迁移文件不受此限，但必须同步 SQLite 侧表结构与契约测试，并更新 `apps/api/test_platform_contracts.py` 的迁移清单）。

**基线**（改动后必须仍然成立）：Agent `python -X utf8 -m unittest discover -s . -p "test_*.py"` ≥ 247（9 skipped）；后端同命令 ≥ 305（14 skipped）；前端 `npm run build` 通过且路由数不少于 19；`scripts/demo-1.0.ps1 -AutoApprove` 17 项全过。

**前端改动后必须重建并重启 Web 进程**（见上文「Web」小节）：`next start` 会缓存构建清单，只重建文件不重启会持续发旧 HTML。

**改桌面端壳或内核前**：`docs/SIDECAR_CONTRACT.md` 是冻结契约（只能追加）；改内核记得同时更新契约文档与 `apps/agent/test_sidecar_api.py`。

**前端改动后必须重建并重启 Web 进程**（见上文「Web」小节）：`next start` 会缓存构建清单，只重建文件不重启会持续发旧 HTML。
