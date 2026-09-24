# 桌面端客户端规划（一键接入 + 本地 Agent 自动监测）

> 日期：2026-09-16
>
> 状态：`PLAN`（待拍板事项见 §9，均已给默认值）
>
> 上游：`docs/DEMO_1_0_IMPLEMENTATION_PLAN.md`（Demo 1.0 已完成）、`docs/CODEX_EXECUTOR.md`（Codex 作为执行体）
>
> **分工**：本文档回答「为什么这么选」（选型、架构、协议设计）；**怎么做与做到什么算完成**见 `docs/DESKTOP_CLIENT_IMPLEMENTATION_PLAN.md`（DP-0…DP-6 阶段、工作项、验收命令、禁区）。两者冲突时以后者为准，选型理由以本文档为准。
>
> 目标形态：用户下载一个安装包 → 输入平台地址 → 一键配对 → 此后**开机自动连、断线自动重连、本机可用 Agent 自动上报、有任务自动执行**

---

## 1. 现状盘点：仓库里已经有多少

结论先说：**大半个桌面端已经在仓库里**，缺的是"外壳、打包、配对深链、以及把任务循环接进常驻服务"。

### 1.1 可直接复用的资产

| 能力 | 位置 | 现状 |
| --- | --- | --- |
| 连接监督（断线重连 + 退避） | `apps/agent/machine_service.py`（`MachineAgentService`，退避 1s→60s） | 已实现，含连接状态与重试计数 |
| 本地安全边界（紧急停止） | 同上（`is_emergency_stopped` / `service-emergency-stop`） | 已实现 |
| 心跳协议 | `packages/agent_protocol.AgentHeartbeat` | **已含 `adapter_versions` / `capabilities` / `running_run_ids` / `local_queue_length` / `user_session_state` / `resource_summary`** |
| 设备身份与配对 | `packages/device_identity`（Ed25519 挑战签名）+ 平台 `/api/devices/pairings` | 已实现，一次性配对码 + 只存哈希 |
| 凭据安全存储 | `apps/agent/credential_store.py`（Windows Credential Manager） | 已实现（设备 Token / 项目 Token 两个目标） |
| 本地持久化与补传 | `apps/agent/local_state.py`（SQLite + durable queue） | 已实现（序号断档补传） |
| Windows 服务安装与控制 | `apps/agent/windows_service_adapter.py`（`WindowsServiceInstaller` + 故障恢复策略） | 已实现（需管理员） |
| 用户会话 Worker + 伪终端 | `apps/agent/session_runtime.py`、`conpty_runner.py`、`named_pipe_transport.py` | 已实现（Session 0 适配器） |
| 访问观测（文件/网络） | `apps/agent/windows_etw_observer.py` | 已实现（ETW 开发版） |
| 外部 CLI 探测（Codex/Claude） | `apps/agent/cli_adapters.py` + `codex_executor.py` | 已实现；Codex 已作为执行体实测通过 |
| 任务执行循环 | `apps/agent/agentd.py` 的 `worker-run` | 已实现，但**是独立前台命令** |
| 平台侧接入向导 | `apps/web/app/devices/page.tsx`（配对 + 一次性 Token + 命令） | 已实现（Web 页） |
| 一键接入脚本 | `scripts/connect-agent.ps1`（含 `-Codex`） | 已实现（需要 Python 与仓库） |

### 1.2 缺口（本规划要补的）

| 缺口 | 说明 | 影响 |
| --- | --- | --- |
| **任务循环与连接监督是两套进程** | `service-run` 只做连接监督；`worker-run` 是独立前台循环，`machine_service.py` 里没有任何 claim/task 逻辑 | 桌面端必须把它们合成一个常驻体，否则"自动干活"要用户手动开两个窗口 |
| **本地 Agent 清单不落库** | `record_agent_heartbeat()` 只更新 `last_seen`/`status`，把 `adapter_versions`、`capabilities`、`running_run_ids`、`local_queue_length` 全丢掉（`apps/api/app/store.py:1376`） | 平台看不到"这台机器能用 Codex 0.154"，无法据此派活 |
| **设备能力是注册时快照** | `devices.capabilities` 只在注册时写一次，之后不随本机 CLI 变化更新 | 装了新 CLI 也不会被识别 |
| **没有配对深链** | 平台网页给出的是命令行，需要仓库 + Python | 与"下载即用"矛盾 |
| **没有安装包与自动更新** | 目前只有脚本 | 分发依赖用户会用 PowerShell |
| **Agent 侧不知道自己的项目授权** | 现在靠 `--grant` 显式传入；设备无法主动查询"我被授权到哪些项目、用什么执行体偏好" | 桌面端配置无法集中管理 |

---

## 2. 目标与非目标

### 2.1 目标（可验收）

1. **下载即用**：只装一个安装包，不需要仓库、不需要 Python、不需要 PowerShell。
2. **一键配对**：在平台网页点「接入这台电脑」→ 系统调起桌面端 → 自动完成配对，用户只输平台地址。
3. **自动连接**：开机自启、断线自动重连（复用现有退避）、离线由平台侧心跳超时判定（UX-3 已实现）。
4. **自动监测**：定期探测本机可用 Agent（Codex/Claude/自研 adapter）与版本、会话状态、队列长度，随心跳上报；平台 `/devices` 与 `/runs` 可见。
5. **自动干活**：常驻任务循环——有任务自动领取执行、无任务退避轮询；托盘可见"正在执行 N 个任务"并可一键紧急停止。

### 2.2 非目标

- 不做跨平台首版（Windows 优先；macOS/Linux 见 D5）；
- 不自建账号体系（沿用现有设备配对与项目授权）；
- 不把平台 Web 整体搬进桌面端（只内嵌必要的本地状态与日志页）；
- 不做云端 Runner（沿用"本地执行优先"的既有决策）。

---

## 3. 目标用户旅程

```text
① 平台网页 /devices → 「接入这台电脑」
        │  生成一次性配对串并给出深链  map://pair?blob=<base64url>
        ▼
② 系统调起桌面端（未安装则跳下载页）
        │  自动填入平台地址与配对串
        ▼
③ 桌面端内部完成：keygen → device-register → Token 入凭据管理器 → 建本地状态库
        │  托盘图标由灰变绿
        ▼
④（可选）平台网页为该设备「授权到项目」→ 桌面端随后自动拉到授权（新接口）
        ▼
⑤ 常驻：心跳 + 本地 Agent 清单上报 + 任务循环
        │  有任务 → 调用本机 Codex/命令执行 → 上报结果与 Run 台账
        ▼
⑥ 托盘右键：打开本地状态页 / 紧急停止 / 查看日志 / 退出
```

关键体验点：**全程没有命令行**，且第 ③–⑤ 步之后用户不需要再管它。

---

## 4. 技术选型

### 4.1 决策表

| 层 | 推荐 | 备选 | 理由 / 否决原因 |
| --- | --- | --- | --- |
| 桌面壳 | **Electron（TypeScript）** | Tauri 2（Rust） | 见 §4.2 对比 |
| 本地 Agent 运行时 | **复用现有 Python（agentd + machine_service）打包为 sidecar** | 用 TS/Rust 重写 | 服务安装、ConPTY、ETW、凭据管理器、补传队列都已实现且测试覆盖（Agent 178 项）；重写等于把最难的部分再做一遍 |
| sidecar 打包 | **PyInstaller ONEDIR**（onedir 启动快、便于排障） | Nuitka / onefile | onefile 每次启动解包到临时目录，慢且易被杀软误报 |
| 进程间通信 | **本地回环 HTTP（127.0.0.1 随机端口）+ 一次性启动令牌** | stdio JSON-RPC / Named Pipe | 壳要读状态、发指令；回环 HTTP 最好调试，随机端口 + 令牌避免被其他本地进程调用。Named Pipe 已有代码（`named_pipe_transport.py`）可复用为后续加固 |
| 与平台通信 | **现有 Gateway WebSocket + HTTP 能力 Token** | gRPC / 自建长连接 | 已有序号校验、幂等、补传与结果持久化；无需引入新协议 |
| 凭据存储 | **Windows Credential Manager（已有）** | DPAPI + 本地文件 | 已实现且 fail-closed |
| 本地 UI | **平台 Web 内嵌（WebView2/Electron 窗口）+ 托盘菜单** | 另写一套本地 UI | 复用现有 Next.js 页面（设备页、运行页），不重复造 UI |
| 配对深链 | **自定义协议 `map://`（Electron `setAsDefaultProtocolClient`）+ 网页兜底复制粘贴** | 二维码（扫码在网页侧仍是复制粘贴） | 深链是"一键"的关键；兜底保留剪贴板路径 |
| 安装包 | **electron-builder → NSIS（可选 MSI）** | WiX / Inno Setup | electron-builder 顺带产出自动更新所需的元数据 |
| 自动更新 | **electron-updater + 平台托管更新源**（`latest.yml` + 安装包，平台已有对象存储/MinIO） | 手动下载覆盖 | 自托管部署下用平台自己的存储最省事；也符合"数据不出内网" |
| 开机自启 | **Windows 服务（已有 `service-install`）为默认**；托盘常驻为可选 | 计划任务 / Run 键 | 服务能在无人登录时跑；但 ConPTY/桌面控制需要用户会话 → 用"服务 + 用户会话 Worker"的组合（已有 Session 0 适配器） |

### 4.2 壳技术：Electron vs Tauri

| 维度 | Electron | Tauri 2 |
| --- | --- | --- |
| 本机工具链 | **node 24 / npm 11 已就绪** | 需要 Rust 工具链（本机未安装，约 1GB 依赖） |
| 团队熟悉度 | 平台前端就是 Next.js/TS，直接复用 | 需学 Rust 写原生部分（托盘/服务调用/更新器） |
| 安装包体积 | ≈ 230MB（含 Python sidecar ≈ 80MB） | ≈ 90MB |
| 内存占用 | 较高（多进程模型） | 低（系统 WebView2） |
| 原生能力 | 主进程 Node，可直接调 Windows API（或调 sidecar） | 插件生态完善，但部分需自己写 |
| 自动更新 | electron-updater 成熟 | tauri-plugin-updater 成熟 |
| 迁移成本 | 壳是可替换的：**sidecar 契约不变**，日后换 Tauri 只重写外壳 | — |

**推荐 Electron，并把 sidecar 的接口做成协议（§5），以便日后换壳不换芯。**
理由：本机/团队工具链已在 TS 侧，Rust 未安装；这个产品的难点在 Python sidecar（已存在），壳的选择应优先"最快到可用"。若后续对安装包体积/内存有硬要求，再迁移 Tauri——因为 sidecar 契约独立，迁移是受控的。

### 4.3 双进程架构（推荐）

```text
┌─────────────── 桌面端（Electron）───────────────┐
│ 主进程：托盘、窗口、深链(map://)、自启、更新器   │
│  ─ 管理 sidecar 生命周期（启动/健康检查/重启）  │
│ 渲染进程：平台 Web 内嵌 + 本地状态页             │
└───────────────┬─────────────────────────────────┘
                │  127.0.0.1:<随机端口> + 启动令牌
┌───────────────▼─────────────────────────────────┐
│ sidecar：Python（现有 agentd/machine_service）   │
│  ├─ 连接监督（退避重连）+ 心跳（含本地 Agent 清单）│
│  ├─ 任务循环（claim→执行→上报；Codex/命令执行体） │
│  ├─ 凭据（Credential Manager）与本地状态（SQLite）│
│  ─ 服务模式（Windows Service）/ 用户会话 Worker  │
└───────────────┬─────────────────────────────────┘
                │  WebSocket（Bearer 设备 Token）+ HTTP（项目能力 Token）
        ┌───────▼────────┐
        │  平台控制平面   │  FastAPI：设备/授权/任务/Run/门禁
        └────────────────┘
```

---

## 5. sidecar 契约（壳与内核之间，需冻结）

| 端点 | 方法 | 用途 |
| --- | --- | --- |
| `/health` | GET | 存活与版本（壳用于健康检查与重启判定） |
| `/status` | GET | 连接状态、当前任务、队列长度、本地 Agent 清单、最后错误 |
| `/pair` | POST | 传入配对串（深链或手输），完成 keygen+注册+凭据入库 |
| `/agents/rescan` | POST | 立即重扫本机 CLI（安装新 CLI 后不必等周期） |
| `/tasks/pause` `/tasks/resume` | POST | 暂停/恢复领取任务（托盘菜单） |
| `/emergency-stop` `/clear-emergency-stop` | POST | 复用现有本地紧急停止 |
| `/grant` | POST | 交付项目授权串（DP-2-08 追加）：内核写入凭据并**运行期**开始领取该项目任务 |
| `/logs?tail=N` | GET | 最近日志（托盘"查看日志"） |
| `/shutdown` | POST | 优雅退出 |

要求：随机端口 + 每次启动生成的 Bearer 令牌（写在壳可读的临时文件里，权限限当前用户）；仅监听 `127.0.0.1`。所有写操作幂等或可重入。

> **v1 演进记录**：DP-1 冻结 v1；DP-2 追加 `POST /grant`、状态目录 `platform.json`、`connection.state` 新取值与
> `identity.project_id` / `queue.local_queue_length` 字段（全部为追加，无既有字段语义变化）。
> 细节与错误码以 `docs/SIDECAR_CONTRACT.md` 为准。

---

## 6. 平台侧需补的接口与字段（后端工作量）

| # | 改动 | 说明 |
| --- | --- | --- |
| B1 | 心跳落库新增字段 | `agent_connections` 或新表 `agent_runtime_state`：`adapter_versions`、`capabilities`、`running_run_ids`、`local_queue_length`、`user_session_state`、`resource_summary`（**当前被丢弃**，见 `store.py:1376`） |
| B2 | 设备能力刷新 | 允许心跳更新 `devices.capabilities`（注册时的快照改为"最近一次上报"） |
| B3 | `GET /api/agent/me` | 设备 Token 换自己的配置：`agent_id`、已授权项目与能力、执行体偏好、心跳间隔、允许的 sandbox 上限、最近任务 |
| B4 | `GET /api/devices` 扩展 | 返回"最近能力上报 + 本机 CLI 清单 + 队列长度"，供 `/devices` 页与派活匹配使用 |
| B5 | 派活匹配（可选） | `claim_next_task` 增加"只领我能执行的任务"过滤（按 `required_capabilities` / 执行体）——当前**不做**（决策 D5 保持"任何有权限的 Agent 都能领"），仅记录为演进项 |
| B6 | 更新源 | 平台静态托管 `latest.yml` + 安装包（MinIO 桶或 `apps/api` 静态目录），提供 `GET /api/client/latest` 返回版本与下载地址、最低兼容版本 |

---

## 7. 分阶段计划

### D1 桌面端最小可用（MVP）

**目标**：下载安装包 → 输入平台地址 → 网页点「接入这台电脑」→ 托盘变绿 → 平台 `/devices` 出现该设备。

工作项：

| 编号 | 工作项 |
| --- | --- |
| D1-01 | Electron 壳骨架：主进程 + 托盘（状态灯）+ 窗口（内嵌平台 Web）+ 单实例锁 |
| D1-02 | Python sidecar 打包（PyInstaller ONEDIR）+ 壳的启动/健康检查/异常重启 |
| D1-03 | sidecar 契约实现（§5 的 `/health`、`/pair`、`/status`、`/shutdown`） |
| D1-04 | `map://` 深链注册 + 平台网页生成深链（含兜底：复制配对串手输） |
| D1-05 | 凭据与状态路径固定（`%LOCALAPPDATA%\MathAgentPlatform\`），不污染用户家目录 |
| D1-06 | electron-builder 产出 NSIS 安装包（未签名，标注"内测"） |

**验收**：在干净 Windows 机器上装包 → 走完 §3 的 ①–③，平台 `/devices` 可见且状态 active；杀掉 sidecar 后壳能自动拉起。

**Do NOT**：不做跨平台；不做自动更新；不把 worker 循环接进来（D2 做）。

### D2 常驻任务循环（把 worker 接进机器服务）

**目标**：安装后无需任何操作，有任务自动执行。

| 编号 | 工作项 |
| --- | --- |
| D2-01 | `machine_service` 增加任务循环：连接就绪后 claim→执行→上报，断线暂停、重连恢复 |
| D2-02 | 与连接监督合并为一个常驻体（`service-run --with-tasks` 或新子命令 `daemon-run`），保持 `worker-run` 前台模式用于调试 |
| D2-03 | 本地 Agent 清单探测与上报（复用 `detect_codex_cli` + `cli_adapters` 探针，周期 + 手动 rescan） |
| D2-04 | 托盘菜单：暂停/恢复、紧急停止、查看日志、打开本地状态页 |
| D2-05 | 平台侧 B1/B2（心跳字段落库 + 能力刷新）与 `/devices` 展示 |

**验收**：装包后不改任何配置，在平台建一个 `worker_executor=codex` 任务 → 分钟内自动执行完成，`/runs` 摘要含 Codex 回复；`/devices` 显示"Codex 0.154 可用"；拔网线 30 秒后恢复自动重连。

**Do NOT**：不引入多任务并发调度（保持单并发）；不做"平台主动推送任务"（沿用拉取，决策 D5）。

### D3 本地状态页与可观测

| 编号 | 工作项 |
| --- | --- |
| D3-01 | 本地状态页（壳内渲染）：连接、当前任务、队列、最近 50 条日志、本地 CLI 清单 |
| D3-02 | 一键导出诊断包（含 `agentd.db` 摘要、日志、版本清单、脱敏后的配置） |
| D3-03 | 失败可视化：把 `parse_codex_jsonl` 的 `error_events` / `unparsed` 呈现在界面上 |

**验收**：任务失败时用户能在本地页面看到原因（而不是去翻 JSONL）。

### D4 分发与运维

| 编号 | 工作项 |
| --- | --- |
| D4-01 | 代码签名（EV/OV 证书）+ SmartScreen 说明 |
| D4-02 | 自动更新（electron-updater + 平台 B6 更新源）+ 最低兼容版本校验（与协议 `schema_version` 对齐） |
| D4-03 | 开机自启策略：默认装 Windows 服务（已有 `service-install`），可选"仅登录后运行" |
| D4-04 | 静默安装参数（企业内网批量部署）与卸载清理（凭据、服务、本地库） |
| D4-05 | 平台侧下载页（`/devices` 或 `/settings` 提供安装包与版本说明） |

**验收**：老版本客户端在平台发布新版本后能自更新；卸载后无残留服务与凭据。

### D5 跨平台（可选，按需）

| 编号 | 工作项 |
| --- | --- |
| D5-01 | 凭据存储抽象：macOS Keychain / Linux Secret Service（现有实现是 Windows 专属且 fail-closed） |
| D5-02 | 服务化替代：launchd（macOS）/ systemd user service（Linux） |
| D5-03 | 伪终端与观测：非 Windows 无 ConPTY/ETW，需明确降级（PIPE + 无观测）并在界面标注 |
| D5-04 | 打包：dmg / AppImage / deb |

---

## 8. 风险与工作量

| 风险 | 影响 | 应对 |
| --- | --- | --- |
| Python sidecar 体积与杀软误报 | 首次安装体验差 | ONEDIR + 签名 + 白名单说明；必要时把 CLI 探测等轻逻辑前移到壳 |
| 深链在未安装时无响应 | 用户困惑 | 网页在无响应时给出"下载 + 手动粘贴配对串"兜底路径 |
| 服务模式与用户会话的权限边界 | ConPTY/桌面控制在服务进程不可用 | 沿用"服务 + 用户会话 Worker"组合（已有 Session 0 适配器与 Named Pipe） |
| 自动更新与协议版本错配 | 老客户端连不上 | 平台返回最低兼容版本；客户端拒绝降级并提示升级 |
| 代码签名成本 | SmartScreen 拦截 | D1 内测用未签名 + 说明；D4 补证书 |
| 心跳洪泛 | 平台侧压力 | 心跳间隔可下发（B3），默认 30s，且 `resource_summary` 限大小 |

工作量估算（人日，含自测与文档）：

| 阶段 | 估算 |
| --- | --- |
| D1 MVP | 8 |
| D2 常驻任务循环 | 6 |
| D3 本地可观测 | 4 |
| D4 分发与运维 | 6 |
| D5 跨平台 | 10（按需） |
| 合计（D1–D4） | **24** |

最小可用（D1+D2）≈ **14 人日**，即可交付"下载→配对→自动连→自动干活"。

---

## 9. 待拍板事项（均给默认值，按默认即可开工）

| # | 事项 | 默认值 | 影响 |
| --- | --- | --- | --- |
| P-1 | 壳技术：Electron 还是 Tauri | **Electron**（本机无 Rust 工具链，团队栈在 TS） | 换 Tauri 需 +3 人日与 Rust 环境；sidecar 契约不变故可后换 |
| P-2 | 首版是否跨平台 | **仅 Windows** | 跨平台 +10 人日，且 ConPTY/ETW 需降级方案 |
| P-3 | 是否要求代码签名 | **D1 不签、D4 补** | 不签则用户需手动"仍要运行"，企业分发受限 |
| P-4 | 自动更新源放哪 | **平台托管（MinIO/静态目录）** | 若走外部 CDN，需额外网络与权限规划 |
| P-5 | 服务模式 vs 托盘常驻哪个为默认 | **服务为默认 + 托盘可选** | 影响是否需要管理员权限安装 |
| P-6 | 是否允许"平台主动派活" | **不允许**（沿用拉取，决策 D5） | 允许则要改 Gateway 为服务端推送，属协议级变更 |

---

## 附录 A：现状证据索引

| 结论 | 证据 |
| --- | --- |
| 连接监督与退避已实现 | `apps/agent/machine_service.py:418`（`MachineAgentService`）、`:48-49`（退避 1s→60s） |
| 紧急停止已实现 | `machine_service.py:165`、`agentd.py` 的 `service-emergency-stop` |
| 心跳字段已具备但被丢弃 | `packages/agent_protocol/__init__.py` 的 `AgentHeartbeat`；`apps/api/app/store.py:1376-1394`（只更新 last_seen/status） |
| 设备能力是注册快照 | `apps/api/app/store.py` 的 `devices` 表（`capabilities TEXT NOT NULL`，仅注册时写入） |
| 机器服务无任务循环 | `machine_service.py` 中无 `claim`/task 相关定义；任务循环在 `agentd.py` 的 `worker-run`（前台命令） |
| Windows 服务安装已实现 | `apps/agent/windows_service_adapter.py:246`（`WindowsServiceInstaller`）、README 的 `service-install` 说明 |
| 用户会话 Worker 已实现 | `apps/agent/session_runtime.py`、`session_worker.py`、`named_pipe_transport.py` |
| 配对与凭据已实现 | `packages/device_identity`、`apps/agent/credential_store.py` |
| Codex 执行体已打通 | `docs/CODEX_EXECUTOR.md`（实测两条：纯对话与工具调用） |
| 平台接入向导已存在 | `apps/web/app/devices/page.tsx`；`scripts/connect-agent.ps1` |
| 本机工具链 | node v24.19.0 / npm 11.17.0 已装；**cargo/rustc 未安装**；pyinstaller 未安装（可 pip 安装） |