# Agent 与用户设备连接补充设计

> 文档版本：1.4
>
> 编写日期：2026-09-12
>
> 文档状态：架构补充；P3-08 至 P3-20 已完成开发版 Machine Agent Service、Runner/Adapter、User Session Worker、Windows Named Pipe、ConPTY、Windows Service/Session 0、Codex CLI JSONL、设备签名证明、系统凭据存储和 Token 轮换接入切片；生产运行时、真实 CLI 任务和 Claude 语义适配仍未完成
>
> 适用范围：本地电脑 Agent、CLI Agent、桌面程序自动化、手机控制和远程任务管理

## 1. 文档目的

本文件补充 `PROJECT_EXECUTION_PLAN.md` 中关于“平台如何连接、启动、控制和恢复用户设备 Agent”的部署设计。

主计划已经冻结了以下方向：

- 云端控制平面与本地执行平面分离。
- 用户设备上的 Agent 主动外连平台。
- 不要求用户电脑开放公网入站端口。
- 平台使用自有协议、Gateway、`agentd`、Adapter 和 MCP。
- 本地 Runner 优先，云端隔离 Runner 作为扩展。

本文件进一步明确本地运行时的进程结构、Windows 会话处理、CLI 控制、设备连接、断线恢复、手机控制和权限边界。

本文件目前是架构补充，不表示所有能力已经实现。只有在对应阶段完成代码、契约测试和交接文档后，相关能力才能标记为已完成。

## 2. 核心原则

1. 桌面 UI 不是任务执行的必要条件。
2. `agentd` 是设备运行时，不是聊天窗口。
3. 云端通过主动外连连接设备，不直接访问用户电脑。
4. 正式纳入平台管理的 CLI 默认由平台 Adapter 启动。
5. 任务控制、终端控制和桌面控制必须分级授权。
6. 人工审批是独立的一等对象，不能隐藏在终端文本中。
7. 本地 Agent 必须支持离线缓存、事件补传和幂等恢复。
8. 设备权限不能自动继承成员的全部权限。
9. 远程桌面控制不能作为普通任务执行的默认能力。
10. 外部 CLI 的私有内部状态不属于平台稳定接口。
11. 任务结果、进程事件和人工操作都必须可以追溯到设备、Agent、成员和 Run。
12. 用户可以在本地紧急停止 Agent，云端不能绕过本地安全边界。

## 3. 总体部署结构

```text
┌────────────────────────────────────────────────────────────┐
│ 云端控制平面                                               │
│ API / Gateway / Task / Run / Artifact / Approval / Event   │
└──────────────────────────────┬─────────────────────────────┘
                               │ TLS 主动外连
                               │ WebSocket，HTTPS 降级
┌──────────────────────────────▼─────────────────────────────┐
│ 用户设备                                                     │
│                                                              │
│  Machine Agent Service / agentd-core                         │
│  - 设备身份、云端连接、任务队列、断线缓存                    │
│  - 安全策略、任务生命周期、进程监督                          │
│                    │ 本机 Named Pipe / 本机 RPC              │
│  User Session Worker / agentd-session                        │
│  - 用户会话中的 CLI、ConPTY、Office、浏览器和桌面自动化     │
│  - 本地确认、屏幕控制和用户会话相关权限                      │
│                    │ 本机 IPC                               │
│  Desktop UI / Tray App                                       │
│  - 配对、配置、状态、日志、审批和紧急停止                    │
│                                                              │
│  Runner / Adapter                                            │
│  - Codex、Claude Code、Python、Office、Browser、Shell        │
└──────────────────────────────────────────────────────────────┘
```

### 3.1 组件职责

#### Machine Agent Service

机器级 Agent 负责长期运行和设备级控制：

- 保存设备公钥身份和连接凭据。
- 与云端 Gateway 建立主动连接。
- 接收任务和控制请求。
- 保存本地任务队列、事件日志和待上传文件。
- 管理 Run 生命周期和进程树。
- 检查项目、路径、命令和网络策略。
- 启动或唤醒用户会话 Worker。
- 处理断线重连和云端事件确认。

它不应默认直接操作用户桌面。

#### User Session Worker

用户会话 Worker 负责必须在交互式用户会话中执行的任务：

- Windows ConPTY 终端。
- 交互式 Codex、Claude Code 或其他 CLI。
- PowerPoint、Word 和其他 Office 程序。
- 浏览器和 UI Automation。
- 屏幕读取、鼠标键盘控制。
- 本地审批窗口。

Windows Service 运行在 Session 0，不能假设它可以直接操作当前用户桌面。因此桌面级能力必须经过 User Session Worker。

#### Desktop UI / Tray App

桌面 UI 是配置和授权界面，不是运行时核心。它负责：

- 设备配对和撤销。
- 查看 Agent、Device、Run 和 Session 状态。
- 配置工作区、目录和网络策略。
- 显示日志和审批请求。
- 暂停、恢复、取消和紧急终止任务。
- 启动本地诊断和连接测试。

桌面 UI 关闭后，`HEADLESS` 和部分 `USER_SESSION` 任务仍应可以继续执行。

## 4. 执行配置

每个 Run 必须声明 `execution_profile`：

| 配置 | 典型任务 | UI 关闭后 | 用户注销后 | 电脑睡眠后 |
| --- | --- | --- | --- | --- |
| `HEADLESS` | Python、数据处理、非交互 CLI | 继续 | 可继续 | 挂起 |
| `USER_SESSION` | 交互式 CLI、需要用户会话的程序 | 通常继续 | 默认暂停 | 挂起 |
| `INTERACTIVE_DESKTOP` | PowerPoint、浏览器、鼠标键盘控制 | 可继续但不能接收 UI 操作 | 暂停 | 挂起 |

执行配置决定：

- 任务需要哪个 Worker。
- 是否必须存在登录用户。
- 是否允许自动重试。
- 是否允许远程终端输入。
- 是否允许屏幕和桌面控制。
- 设备离线、锁屏、注销或睡眠时的状态转换。

建议状态包括：

```text
QUEUED
STARTING
RUNNING
WAITING_APPROVAL
PAUSED
OFFLINE_PENDING
SUSPENDED
SUCCEEDED
FAILED
CANCELLED
ABANDONED
```

## 5. 设备连接协议

### 5.1 连接方向

设备只建立向云端的出站连接：

```text
agentd → Gateway
```

推荐协议顺序：

1. WebSocket over TLS：默认长连接。
2. HTTPS 长轮询：网络限制环境的降级方案。
3. 后续可以增加 gRPC streaming 或 QUIC，但不能改变领域事件契约。

平台不要求用户电脑开放公网入站端口，也不依赖端口转发或动态公网 IP。

### 5.2 设备配对

设备第一次接入时：

1. Desktop UI 或 CLI 生成设备密钥对。
2. 用户登录平台并获取一次性配对码。
3. `agentd` 使用配对码完成设备注册。
4. 云端保存设备公钥、设备名称、所有者和能力摘要。
5. 后续连接使用设备密钥和短期会话凭证。
6. 用户可以从平台撤销设备，撤销后设备不能继续领取新任务。

设备密钥不能使用成员密码代替，也不能因为成员加入项目就自动拥有项目访问权。

### 5.3 连接消息

连接消息至少包含：

```text
device_id
agent_id
session_id
connection_id
sequence
message_id
idempotency_key
message_type
schema_version
sent_at
payload
```

平台需要确认已接收的事件序号。设备必须保留未确认事件，直到收到云端确认。

对于任务和 Run 命令，Gateway 在首次执行后必须持久化
`GatewayCommandResult(connection_id, message_id, idempotency_key, sequence, message_type, status, response_type, result/error_code)`。
重复序号按 `message_id` 恢复，重试按 `idempotency_key` 恢复；ACK 丢失不能导致业务命令再次执行。
当前这是 SQLite/PostgreSQL 开发版契约，业务副作用与结果写入的同事务并发保证仍待真实 PostgreSQL 验收。

### 5.4 心跳和在线状态

心跳至少包含：

- 设备时间和 Agent 时间。
- Agent 版本和 Adapter 版本。
- 当前连接 ID。
- 能力摘要。
- 运行中的 Run 列表。
- 本地队列长度。
- 当前用户会话状态。
- 磁盘和资源告警摘要。

在线状态不能仅依赖最后一次 HTTP 请求，而应由 Gateway 根据心跳、连接状态和事件确认共同判断。

### 5.5 设备身份与项目能力分层

设备级认证和项目级授权必须分开：

| 层级 | 凭证 | 作用 |
| --- | --- | --- |
| 设备 | Device Token | 证明设备已登记并允许建立 Gateway 连接 |
| 项目 | `X-Project-Capability-Token` | 证明该设备代表的 Agent 在指定项目具备某项操作能力 |

HTTP Agent 任务和 Run 写请求，以及 Gateway 任务/Run 命令，都必须携带项目能力 Token。
服务端校验项目、Agent、Token 状态和操作能力；人类 Session 或设备 Token 不能替代这个
项目授权。P3-22 的 Gateway Handoff 创建/接收和 Agent Review 命令，以及 P3-23 的
`agent.artifact.create` 元数据命令，已经复用这套项目能力语义；Artifact 文件内容继续通过
HTTP/Multipart 上传，完整人工审批端到端协调仍待后续实现，不应重新设计另一套权限规则。

## 6. CLI Agent 控制设计

### 6.1 默认启动策略

正式托管 Run 默认由平台 Adapter 启动：

```text
创建 Run
→ 检查权限和执行配置
→ 准备工作区
→ 下载输入成果物
→ Adapter 构造命令
→ Runner 启动进程
→ 采集事件和文件变更
→ 上传输出和运行记录
→ 提交审核或交接
```

每个进程必须绑定：

```text
project_id
task_id
run_id
agent_id
device_id
workspace_id
```

### 6.2 已打开 CLI 的接入

平台不应默认尝试发现并接管任意用户手动打开的 Codex 或 Claude Code 进程，因为无法可靠确认：

- stdin 是否可用。
- 当前工作目录。
- 原始任务。
- 当前会话状态。
- 是否可以安全终止。
- 输出是否完整。

后续可以支持两种受控方式：

```text
platform run <adapter>
platform attach --session <session_id>
```

只有通过平台启动包装器、带有明确 Session ID 的进程，才能进入正式审计链路。无法完整识别的外部进程应标记为：

```text
ATTACHED_BEST_EFFORT
```

其输出可以参考，但不能自动成为正式结果。

### 6.3 输入输出方式

按稳定性排序：

1. CLI 官方机器可读模式、事件流或 hooks。
2. 普通 stdin/stdout/stderr 管道。
3. Windows ConPTY。
4. 桌面 UI 自动化。

平台内部统一为以下事件：

```text
process.started
process.stdout
process.stderr
terminal.prompt
approval.requested
tool.started
tool.completed
file.changed
artifact.created
process.exited
run.completed
run.failed
```

人类可读的终端文本不能作为唯一审计依据。

## 7. Adapter 契约

每种外部 CLI 或本地执行器通过 Adapter 接入，不将具体工具写死在平台核心。

Adapter 至少声明：

```text
adapter_id
agent_name
agent_version
adapter_version
supported_os
supported_execution_profiles
supported_capabilities
launch_command
input_mode
output_mode
approval_mode
interrupt_mode
artifact_detection_mode
```

初始 Adapter 类型包括：

```text
CodexAdapter
ClaudeCodeAdapter
PythonRunner
ShellRunner
OfficeRunner
BrowserRunner
MCPAdapter
```

Adapter 必须：

- 优先使用官方接口。
- 不读取未公开的内部数据库或进程内存。
- 提供版本和能力检测。
- 为关键版本维护兼容性测试。
- 在协议不兼容时明确失败，不静默伪造成功。

## 8. 审批与远程控制

### 8.1 审批对象

以下行为必须转换成正式 `ApprovalRequest`：

- 执行危险命令。
- 访问工作区之外的路径。
- 访问外部网络。
- 使用敏感凭据。
- 删除或覆盖文件。
- 启动桌面控制。
- 操作浏览器或 Office。
- 发送外部消息。

审批状态：

```text
PENDING
APPROVED
DENIED
EXPIRED
CANCELLED
```

审批超时默认暂停任务，不默认放行。

### 8.2 控制等级

| 等级 | 内容 | 默认策略 |
| --- | --- | --- |
| 任务控制 | 启动、暂停、恢复、取消、重试 | 项目权限控制 |
| 终端控制 | 读取输出、发送输入、响应 CLI 确认 | 任务和 Run 权限控制 |
| 桌面控制 | 屏幕、鼠标、键盘、Office、浏览器 | 单独授权，默认关闭 |

设备所有者必须始终保留本地紧急停止权。云端控制不能绕过本地拒绝。

### 8.3 手机控制

手机端不直接连接用户电脑，采用：

```text
Mobile App → Cloud API → Agent Gateway → Device Agent
```

首期手机能力：

- 查看设备和 Run 状态。
- 查看日志摘要。
- 审批或拒绝请求。
- 暂停、恢复和终止任务。
- 接收设备离线和任务失败通知。
- 查看成果物和交接包。

完整远程桌面能力应单独设计、单独授权并记录完整操作日志。

## 9. 断线、崩溃和恢复

本地 Agent 必须拥有持久化运行日志，不能只使用内存队列。可以使用本地 SQLite 保存：

- Run 状态。
- 事件序号。
- 未确认事件。
- 待上传文件。
- 审批状态。
- 进程退出信息。
- 重试次数和原因。

恢复流程：

```text
本地产生事件
→ 写入本地日志
→ 上传事件
→ 云端确认序号
→ 删除已确认缓存
```

进程失败时：

- 幂等任务可以按策略自动重试。
- 非幂等任务必须暂停并请求确认。
- 破坏性操作不得自动重试。
- 已产生但未完成的输出保留为实验性成果。
- 旧 Run 不能覆盖新 Run 的正式结果。

网络恢复时，设备必须按序补传事件和输出，重复上传由 `idempotency_key` 和内容哈希消解。

### 9.1 P3-08 开发版实现状态

当前 `apps/agent/machine_service.py` 已提供机器级服务原语：

- `MachineAgentService` 负责一次 Gateway 会话的监督、Token Provider 调用、指数退避重连、
  心跳调度和共享 SQLite 紧急停止检查。
- `LocalProcessSupervisor` 使用 `asyncio.create_subprocess_exec` 启动直接子进程，不经过 shell，
  并记录启动、输出、退出码、超时、取消和启动异常。
- 服务启动时会将本地恢复库中上一个实例遗留的 `STARTING/RUNNING` 记录标记为 `ABANDONED`。
- `agentd service-run` 启动监督服务；`service-emergency-stop` 和
  `service-clear-emergency-stop` 可以由另一个本地进程设置或清除停止标志。

该实现已经通过本地 17 项 Agent 测试，但仍属于开发版：

- `service-run` 默认从 `WindowsCredentialManager` 按设备目标读取 Token；显式 `--device-token` 仅为开发兼容路径。
  当前环境对 `CRED_PERSIST_LOCAL_MACHINE` 实测返回 `1312`，跨登录会话持久化、ACL 和服务身份访问仍待验收。
- 尚未实现 Windows Service 安装与升级、Session 0、User Session Worker、Named Pipe/本机 RPC。
- 子进程监督尚未提供进程树终止、工作区/可执行文件白名单、系统级网络隔离和资源沙箱。
- 真实 TLS、反向代理、跨进程故障恢复和 PostgreSQL 运行时尚未完成集成验收。

### 9.2 P3-09 Runner/Adapter 开发版实现状态

`apps/agent/runner.py` 已把执行请求统一为包含 `project_id`、`task_id`、`run_id`、`agent_id`、
`device_id`、`workspace_id` 和 `workspace_path` 的 `RunnerRequest`。`AdapterDescriptor` 声明
外部 Agent、Adapter 版本、操作系统、执行模式和能力；`LocalRunner` 在调用
`LocalProcessSupervisor` 前执行所有策略检查。

当前开发版策略包括：

- `HEADLESS`、`USER_SESSION`、`INTERACTIVE_DESKTOP` 模式及其用户会话/桌面权限一致性校验。
- 工作区、输入路径和输出路径的规范化绝对路径校验，并拒绝工作区外访问。
- 可执行文件和环境变量白名单；底层 `ProcessSpec` 默认不继承宿主机环境，Adapter 不能重新打开继承。
- `allow-listed` 或 `unrestricted` 网络在没有显式 `NetworkEnforcer` 时直接拒绝。
- `CommandAdapter` 和 `PythonAdapter` 复用非 shell 进程监督；进程记录包含完整项目、任务、Run、
  Agent、Device 和 Workspace 归属。
- API `RunCreate/Run` 和数据库运行记录正式保存 `execution_profile`；PostgreSQL 增量迁移为
  `007_run_execution_profile.sql`。

P3-09 已通过 `apps/agent/test_runner.py` 和全量本地 Agent 回归。该切片仍不是 OS 级安全边界：
网络 Enforcer、进程树隔离、工作区挂载、文件访问审计、资源限制、User Session Worker 和
Artifact 上传尚未完成。

### 9.3 P3-10 User Session Worker/本机 IPC 传输无关契约状态

P3-10 在真正选择 Windows Named Pipe 传输之前，先将 User Session Worker 的身份、消息类型、
运行上下文和授权语义固定下来：

- `packages/agent_protocol/session.schema.json` 定义 `SessionPeer`、`SessionRunContext`、
  `SessionIpcRequest`、`SessionIpcResponse` 和 `SessionIpcEvent`。
- `apps/agent/session_worker.py` 的 `SessionWorkerBroker` 负责 Worker 注册、请求幂等、Peer/用户
  SID 匹配、执行模式和能力白名单、用户会话状态、桌面审批以及 Worker 生命周期。Machine Service
  使用独立的服务身份，不要求携带交互用户 SID；其身份认证由具体本机 IPC 传输层负责。
- Machine Service 负责 `session.run.start/stop`；User Session Worker 负责 `session.hello/status`；
  Desktop UI 负责 `session.approval.respond`。终端输入和桌面控制分别受能力、上下文和审批约束。
- `session.run` 能力是启动和停止 Run 的必要条件；用户注销后仍可停止已有 Run 做清理，但不能停止
  不存在的 Run；活动 Run 的状态上报不会错误覆盖运行状态。

P3-10 已通过 `apps/agent/test_session_worker.py` 和 `apps/agent/test_protocol_schemas.py`，本地
Agent 全量测试达到 39 项。该实现是传输无关的内存 Broker，明确不代表：

- Windows Named Pipe、Windows ACL 或真实进程身份认证；
- Windows Service、Session 0 到用户会话切换或真实跨进程故障恢复；
- ConPTY、桌面控制执行、本机操作审计或 OS 级网络/资源隔离。

### 9.4 P3-11 Windows Named Pipe/ACL/身份传输状态

`apps/agent/named_pipe_transport.py` 已把 P3-10 Broker 接入 Windows 原生 Named Pipe：

- 采用 byte-mode 双工 Pipe 和 4 字节小端长度前缀 UTF-8 JSON 帧，限制最大帧大小并拒绝畸形帧。
- `NamedPipeServer` 支持同一客户端连接连续收发多个请求；断开后清理连接句柄。
- 使用 SDDL 创建显式 DACL，允许 SYSTEM、Administrators 和配置的受信 SID，并设置拒绝远程客户端。
- 服务端通过 `GetNamedPipeClientProcessId` 获取 PID，再查询客户端进程 Token SID 和
  `ProcessIdToSessionId`；只有满足 `PipePeerPolicy` 的 SID/Windows Session ID 才交给 Broker。
- `SessionPeer` 增加 `windows_session_id`，Broker 会校验认证层给出的 PID 和 Windows Session ID
  与请求声明一致；Machine Service 身份与交互用户 SID 分离。
- 传输层只负责连接、帧和 OS 身份，业务消息授权仍统一进入 `SessionWorkerBroker`。

P3-11 已在当前 Windows 开发机完成同机多请求往返和不允许 Session 拒绝测试，Agent 全量测试达到 47 项。该结果不代表：

- Windows Service、Session 0 服务账户部署和跨用户/跨 Session 实机矩阵已完成；
- Pipe 断线恢复、服务/Worker 重启、重放保护、压力和长时间运行验收已完成；
- ConPTY、真实 User Session Worker 启动、桌面控制、Runner 事件和 Artifact 链路已完成。

### 9.5 P3-12 User Session Worker 与标准管道 Runner 编排状态

P3-12 已将 Named Pipe、Session Broker 和本地 Runner 接成可执行的 `USER_SESSION` 链路：

- `LocalProcessSupervisor` 支持受控 stdin、stdout/stderr 回调和一个 Run 共享的输出预算；超额输出
  仍被排空，防止子进程阻塞，但不进入结果或事件。
- `LocalRunner` 提供 `start`、`wait`、`request_stop`、`write_stdin` 和活动 Run 查询；旧的 `run`
  接口继续可用。
- `SessionRunStartPayload` 约束 Adapter、命令、环境、路径、超时、输出上限和网络策略。
- `SessionWorkerRuntime` 让请求先通过 `SessionWorkerBroker`，再构造 `RunnerRequest`，并在独立
  asyncio loop 中监督进程。
- Runtime 产生有序 `process.started`、实时 `process.stdout/stderr`、`process.exited` 和
  `run.completed/failed` 事件；`agentd session-worker-run` 可将这些事件写入本地 Gateway outbox。
- `session.terminal.input` 经过 Broker 的能力和活动 Run 校验后写入用户会话进程。
- `session.terminal.resize` 经过同样的 Run/能力校验后，只能调用显式 PTY resize 后端；标准管道
  Runner 没有该后端时必须返回 `terminal_resize_runtime_unavailable`。
- `INTERACTIVE_DESKTOP` 和桌面控制在没有真实执行器时显式失败，不降级成后台进程。

P3-12 已通过 `apps/agent/test_session_runtime.py` 的 Runtime、stdin、事件、停止和 Named Pipe
纵向测试；本地 Agent 全量测试达到 58 项。当前仍是标准管道执行，不代表 ConPTY、桌面控制、
Windows Service 或云端 Run/Artifact 闭环已完成。

### 9.6 P3-13 事件、Run 和 Artifact 接入状态

P3-13 已完成开发版结果接入：

- `AgentEventPayload` 将项目、项目能力令牌和本地 `SessionIpcEvent` 组成 Gateway 事件；事件
  通过 `agent.event` 入站序号、ACK 和本地 outbox 可靠传输。
- Gateway 重新验证设备、Agent、项目能力和 Run 所属关系，再写入平台 Event。令牌只用于授权，
  不进入事件内容；重复幂等键不会重复写入。
- `run.completed` 和 `run.failed` 事件可以完成同 Agent 的平台 Run，并附带退出码、stdout/stderr、
  输入文件和输出 Artifact ID。
- `OutputDiscovery` 只接受工作区内显式声明的输出，生成 SHA-256、大小、MIME、相对路径；
  `RunManifestBuilder` 生成可归档的运行 Manifest。
- `ResultUploader` 在本地 `pending_uploads` 中保存重试状态，使用 Agent 专用 Artifact 接口完成
  元数据、内容和 Multipart 上传。上传前检测文件是否被修改。
- Agent Artifact 接口使用项目能力 Token、`X-Agent-Id` 和请求幂等键；输出 Artifact 绑定到同一
  Agent 的 Run，仍然必须经过 Review 才能成为正式下游输入。

证据：`apps/api/app/gateway.py`、`apps/api/app/main.py`、`apps/agent/result_uploader.py`、
`apps/agent/gateway_client.py`、`apps/api/test_gateway.py`、`apps/api/test_agent_capability.py`
和 `apps/agent/test_result_uploader.py`。

以下能力仍明确未完成：真实 Codex/Claude CLI Adapter、桌面执行器、Windows Service/
Session 0 生命周期、设备系统密钥环、生产 PostgreSQL/MinIO、NATS、OS 级隔离、跨服务事务和完整
文件访问审计。

### 9.7 P3-14 会话生命周期与终端控制契约

P3-14 已完成开发版传输无关切片：

- `TerminalSize` 约束列/行范围和可选像素尺寸；`session.terminal.resize` 已同步到共享 JSON Schema。
- `MachineServiceLifecycle` 将服务启动、会话发现、Worker 就绪、锁屏/解锁、注销、Worker 退出、
  显式恢复、服务停止和紧急停止转换成稳定动作；注销生成停止 Run 和 Worker 的清理动作。
- 锁屏不强制清除普通 `USER_SESSION` Run，但禁止新的 `INTERACTIVE_DESKTOP` Run；桌面操作仍
  需要独立审批。
- `SessionLifecycleTransition` 与本地 `lifecycle_transitions` 表记录转移、活跃 Run、清理 Run、原因和
  时间，并按 transition ID 幂等保存。

本轮不宣称真实 ConPTY、Windows Service/Session 0、跨账户会话发现或桌面控制已经完成。

### 9.8 P3-15 ConPTY Adapter 开发版状态

P3-15 已将真实 Windows ConPTY 接入现有 User Session Worker Runtime：

- `apps/agent/conpty_runner.py` 使用 `pywinpty>=2.0,<3` 的 ConPTY backend，提供能力探测、stdin、合并
  stdout、初始尺寸、运行时 resize、取消、超时、退出状态和输出资源清理。
- `HybridProcessSupervisor` 按 `ProcessSpec.terminal_backend` 进行 PIPE/CONPTY 路由；显式选择 CONPTY
  时，宿主不支持或依赖缺失会失败，不会静默退回普通管道。
- `ConPtyAdapter` 只接受 `terminal_backend=CONPTY`；`SessionRunStartPayload`、`RunnerRequest`、
  `ProcessSpec`、Run Manifest 和本地 Run 状态会保留终端后端与尺寸。
- pywinpty 使用 blocking 模式，避免非阻塞包装器在子进程仍存活时误报 EOF；创建过程使用锁保护临时环境变量，
  子进程只收到显式环境和最小 `PATH`。

当前 Windows 开发机能力与测试：

```text
Windows 11 build 26200 / pywinpty 2.0.15 / ConPTY available=true
ConPTY tests: 8 passed, ResourceWarning-as-error passed
apps/agent full suite (截至 P3-16): 93 passed, 1 skipped
compileall: passed; JSON Schema: passed
```

该切片仍属于开发版。尚未完成真实 Codex/Claude CLI 语义适配、进程树/网络/资源隔离、Windows Service
安装升级、Session 0 到用户 Session 的真实 Worker 启动，以及多账户、锁屏、注销、睡眠唤醒的实机矩阵。
P3-15 交接记录为 `docs/handoffs/P3_15_CONPTY_ADAPTER_HANDOFF.md`。

### 9.9 P3-16 Windows Service/Session 0 Adapter

P3-16 将把已有 `MachineServiceLifecycle`、Named Pipe、Session Worker Runtime 和 ConPTY 组合到真实
Windows 服务宿主边界，目标包括：

- Windows Service 的安装、卸载、启动、停止、升级和失败恢复配置；
- 服务运行在 Session 0 时发现活动用户 Session，并使用受控用户 Token 启动或回收对应 Worker；
- 将服务控制管理器事件、Session 登录/注销/锁屏/解锁和 Worker 退出转换为生命周期信号；
- 对 Worker 启动参数、Pipe 名称、用户 SID、Windows Session ID 和工作区执行审计绑定；
- 通过 mockable OS Adapter 完成纯后端契约测试，再以单账户、多账户、锁屏/注销/睡眠唤醒实机测试作为退出门禁。

P3-16 不在本切片内实现桌面自动化，也不把服务账户自身伪装成交互用户；桌面能力继续必须由用户 Session Worker
和独立审批提供。

P3-16 纯后端开发版已实现 `apps/agent/windows_service_adapter.py`：

- `CtypesServiceControlBackend` 封装 SCM 服务安装、更新、卸载、启停、状态和失败重启配置；
  `WindowsServiceInstaller` 负责安装或更新后的恢复策略重应用。
- `WindowsSessionProcessBackend` 封装 WTS 会话枚举、用户 SID 解析、用户 Token 获取、
  `CreateProcessAsUserW` Worker 启动和 Worker 终止/存活检查。
- `SessionWorkerCoordinator` 负责会话巡检、Worker 启动/退出/注销/恢复和生命周期转移回调；
  `WorkerLaunchAudit` 固化 Worker 与用户 Session、SID、Pipe、命令和工作目录绑定。
- Worker 启动后由服务通过同一 Pipe 发送带 `readiness_probe=true` 的 `session.hello`；只有 Pipe OS 对端
  身份、机器服务 Peer ID、Windows Session 0、Worker ID 和用户 Session 全部匹配，才进入 `worker.ready`。
- `CtypesServiceDispatcherBackend` 封装 `StartServiceCtrlDispatcherW`、停止/关机和
  `SERVICE_CONTROL_SESSIONCHANGE`；`WindowsServiceHost` 在服务运行期间持续巡检。
- `agentd.py service-install` 和 `service-control` 是显式管理员操作入口；自动化测试只使用 Fake SCM、
  Session 和 Dispatcher，不会触碰开发机服务注册表。

P3-16 开发版测试为适配器 13 项、Agent 全量 94 项通过（1 项非 Windows 分支跳过）；API 71 项通过、2 项
真实服务依赖跳过。当前仍必须完成管理员权限服务安装/升级回滚、Worker Pipe readiness 握手、进程句柄和
进程树生命周期、跨账户/跨 Session、锁屏/注销/睡眠唤醒及 Gateway 长时间恢复组合实机验收。

### 9.10 P3-17 外部 CLI Adapter 与 JSONL 事件

P3-17 将 CLI 接入从“可启动任意命令”收紧为带版本和协议边界的 Adapter：

- `apps/agent/cli_adapters.py` 提供 `CliCapabilityStatus`、无 Shell `probe_cli()`、版本解析和
  `CodexCompatibilityRule`。当前兼容性矩阵只把已验证的 `codex-cli 0.153.x` 标为可用；版本族之外
  返回 `UNSUPPORTED`，可执行文件不存在返回 `NOT_INSTALLED`，探测异常返回 `ERROR`。
- `CodexAdapter` 只允许官方 `codex exec --json` 入口，统一加入机器可读输出、无颜色和可选临时会话参数；
  不允许自动批准、危险沙箱、`--cd`/`--add-dir`/`--image` 工作区逃逸或未受控配置覆盖。输出 Schema
  路径必须位于 Runner 工作区内。
- `CodexJsonlEventParser` 只接受已登记的顶层事件和 Item 类型，能够处理跨 stdout chunk 的 JSONL，映射
  `run.started`、`run.completed`、`run.failed`、`tool.started`、`tool.completed`、`approval.requested`、
  `file.changed` 和 `agent.message`。未知事件、未知 Item、非法 JSON 或不完整结构均抛出协议错误。
- Session Runtime 为所选 Adapter 建立独立解析器；原始 stdout 仍保存，语义事件额外进入统一事件队列。
  CLI 协议错误会使 Run 失败并停止等待中的进程，避免未知格式被误判为成功。
- `ClaudeCodeAdapter` 当前仅用于报告能力状态：未安装报告 `NOT_INSTALLED`，已安装但未完成稳定机器协议
  适配报告 `UNSUPPORTED`，不能启动或伪造 Claude 结果。
- `agentd cli-capability --adapter codex|claude` 提供本机能力诊断。P3-17 原始切片测试为 CLI Adapter
  11 项、Agent 105 项通过；P3-20 完成时的历史全量记录为 Agent 114 项、API 78 项通过（2 项真实服务依赖跳过）。
  P3-22 收尾复核后的当前记录为 API 82 项通过、3 项真实 PostgreSQL/MinIO 测试跳过，Agent 114 项通过、9 项条件测试跳过。

P3-17 仍未完成真实模型调用、Codex 多版本/多操作系统兼容性矩阵、审批响应回传、Claude Code 语义协议、
进程树和资源/网络隔离。适配器完成不等于阶段 3 生产退出。交接记录为
`docs/handoffs/P3_17_CLI_ADAPTER_HANDOFF.md`。

### 9.11 P3-18 设备注册 challenge/signature

设备 pairing 创建后同时生成 pairing code 和一次性 challenge。服务端只保存两者的 SHA-256，
原始值只通过创建响应交给发起方。注册端必须携带 `pairing_id`、pairing code、challenge、明确的
`device_id`、Ed25519 公钥和 Base64URL 签名。

签名消息固定绑定以下字段：

```text
math-agent-platform/device-pairing/v1
pairing_id
challenge
agent_id
device_id
public_key_fingerprint
```

字段采用长度分隔编码，公钥先规范化为 DER `SubjectPublicKeyInfo` 再计算 SHA-256 指纹。API 和
PostgreSQL Repository 在创建设备前都执行 pairing 状态、过期时间、code、challenge 和签名检查；
失败不会消费 pairing。历史迁移中没有 challenge 的 pairing 保持不可注册，必须重新创建。

P3-18 的实现证据为 `packages/device_identity/__init__.py`、`apps/api/app/store.py`、
`apps/api/app/postgres_repository.py`、`apps/api/migrations/008_device_registration_challenge.sql`、
`apps/api/test_devices.py` 和 `apps/agent/agentd.py`。开发版不包含系统密钥环、正式 OIDC、Token
轮换和真实 PostgreSQL 并发验收。

### 9.12 P3-19 系统凭据存储

`apps/agent/credential_store.py` 提供统一 `CredentialStore` 边界和 Windows Credential Manager 实现。
设备 Token 默认保存到 `MathAgentPlatform/device-token/<device_id>`，`agentd credential-save` 写入，
`credential-delete` 删除；`gateway-run` 和 `service-run` 在没有显式 Token 时读取该目标。适配器使用
`CRED_PERSIST_LOCAL_MACHINE`，长期持久化失败会返回明确错误，不会降级为 Session Credential。

开发版证据为 `apps/agent/credential_store.py`、`apps/agent/test_credential_store.py` 和
`apps/agent/agentd.py`。当前开发机的真实 Windows API 写入返回 `ERROR_NO_SUCH_LOGON_SESSION (1312)`，
因此仍需在目标用户、服务账户和用户配置文件条件下验证重启/注销恢复、ACL、轮换和服务访问。

### 9.13 P3-20 设备 Token 轮换

设备 Token 轮换由控制平面负责，不由本地 Agent 自行生成或修改服务端身份。管理员调用
`POST /api/devices/{device_id}/rotate-token` 后，服务端在单个事务中：

1. 锁定并检查设备仍为 active，且调用者具备组织管理员权限。
2. 生成新 Token，只保存其 SHA-256 哈希并递增 `token_version`。
3. 设置 `token_rotated_at`，撤销设备所有非终态 Gateway 连接。
4. 写入 `device_token_rotations` 审计记录，记录设备、组织、操作者、原因、版本和时间，不记录明文。
5. 仅在该次响应的 `DeviceCredential.device_token` 字段返回新 Token。

旧 Token 在哈希替换提交后立即无效，不设置双 Token 过渡窗口。调用方必须将新 Token 写入已经完成
ACL 验收的 Credential Manager 目标；若本地写回失败，不应继续使用旧 Token，应该把设备标记为需要
重新取证/人工恢复。现阶段 P3-20 只完成服务端轮换和开发版存储契约，尚未实现 Agent 自动写回、
双 Token graceful rotation、私钥轮换或目标身份下的注销/重启恢复。

## 10. 本机 IPC 和安全边界

建议采用：

```text
Machine Service ↔ User Session Worker：Windows Named Pipe
User Session Worker ↔ Desktop UI：Named Pipe 或受保护的本机 RPC
```

不建议使用未认证的本机 HTTP 端口作为唯一安全边界。

本机 IPC 至少需要：

- Windows 用户和服务 ACL。
- 消息类型白名单。
- 请求 ID 和幂等键。
- 调用方身份校验。
- 高风险操作二次确认。
- 本机操作审计。

云端发来的任务还必须经过本地策略检查：

- 项目目录白名单。
- 命令白名单。
- 网络访问策略。
- 环境变量和凭据策略。
- 资源限制。
- 运行时间限制。

## 11. 外部 Agent 版本管理

设备心跳和 Run Manifest 必须记录：

```text
agent_name
agent_version
adapter_version
operating_system
os_version
capability_set
tool_versions
```

平台需要维护兼容性矩阵：

```text
外部 Agent 版本
操作系统版本
Adapter 版本
支持能力
已知限制
测试状态
```

发生版本不兼容时：

1. 阻止进入正式执行，或明确降级为实验性 Run。
2. 提示用户升级、降级或选择其他 Adapter。
3. 保留失败诊断信息。
4. 不允许静默生成看似成功的结果。

## 12. 与项目执行阶段的关系

本设计主要补充以下阶段：

### 阶段 3：Agent Gateway、本地 Agent 与 Adapter

必须覆盖：

- 设备注册和配对。
- Gateway 出站连接。
- `agentd` 基础运行时。
- User Session Worker 的最小实现。
- 心跳、任务拉取和事件确认。
- 本地缓存和断线补传。
- 一个普通 Runner 和一个 CLI Adapter。

P3-01 至 P3-23 已完成开发版协议和本地运行时切片。P3-08 的实现证据为
`apps/agent/machine_service.py`、`apps/agent/agentd.py` 和
`apps/agent/test_machine_service.py`；P3-09 的实现证据为 `apps/agent/runner.py` 和
`apps/agent/test_runner.py`；P3-10 的实现证据为 `apps/agent/session_worker.py`、
`apps/agent/test_session_worker.py` 和 `packages/agent_protocol/session.schema.json`；P3-11 的实现证据为
`apps/agent/named_pipe_transport.py` 和 `apps/agent/test_named_pipe_transport.py`。阶段 3 的
生产退出仍依赖真实基础设施、设备密钥存储、User Session Worker、本机 IPC、实际网络/进程隔离和
Artifact/Runner 集成。P3-12 的实现证据为 `apps/agent/session_runtime.py`、
`apps/agent/test_session_runtime.py` 和 `apps/agent/agentd.py`；P3-13 的实现证据为
`apps/agent/result_uploader.py`、`apps/agent/gateway_client.py`、`apps/api/app/gateway.py`、
`apps/api/app/main.py` 及对应契约测试。阶段 3 的生产退出仍依赖真实基础设施、设备密钥存储、
本机 IPC、实际网络/进程隔离和跨服务 Artifact/Runner 集成。
P3-14 的实现证据为 `packages/agent_protocol/__init__.py`、
`apps/agent/service_lifecycle.py`、`apps/agent/session_runtime.py`、`apps/agent/local_state.py` 和
对应的 `test_service_lifecycle.py`、`test_session_runtime.py`、`test_local_state.py`；P3-15 的实现证据为
`apps/agent/conpty_runner.py`、`apps/agent/test_conpty_runner.py` 和 `apps/agent/requirements.txt`；
P3-16 的实现证据为 `apps/agent/windows_service_adapter.py`、
`apps/agent/test_windows_service_adapter.py` 和 `apps/agent/agentd.py`。真实
Windows Service/Session 0 安装、Worker readiness、跨账户和系统事件验收仍未完成。P3-17 的实现证据为
`apps/agent/cli_adapters.py`、`apps/agent/session_runtime.py`、`apps/agent/test_cli_adapters.py` 和
`apps/agent/test_session_runtime.py`；真实 Codex 任务和 Claude 语义适配仍未完成。
P3-18 的实现证据为 `packages/device_identity/__init__.py`、`apps/api/app/device_identity.py`、
`apps/api/app/store.py`、`apps/api/app/postgres_repository.py`、`apps/api/test_devices.py` 和
`apps/agent/agentd.py`；P3-19 的实现证据为 `apps/agent/credential_store.py` 和
`apps/agent/test_credential_store.py`；P3-20 的实现证据为 `apps/api/app/contracts.py`、
`apps/api/app/store.py`、`apps/api/app/postgres_repository.py`、`apps/api/migrations/009_device_token_rotation.sql`、
`apps/api/test_devices.py` 和 `apps/api/test_http_contracts.py`；P3-22/P3-23 的 Gateway Handoff/Review、
Artifact 元数据和请求指纹证据为 `apps/api/app/gateway.py`、`apps/api/migrations/010_gateway_command_request_hash.sql`
及对应主计划交接文档。系统密钥环目标身份验收、真实 PostgreSQL 并发、自动凭据写回和正式设备会话认证仍未完成。

### 阶段 5：Runner、可复现性和信息边界审计

必须覆盖：

- 真实进程启动。
- 工作区隔离。
- 进程树终止。
- 输入输出哈希。
- 文件访问记录。
- 资源和网络限制。
- Run 与正式成果门禁绑定。

### 后续扩展阶段

- Office 和浏览器 Runner。
- 手机控制端。
- 桌面级远程控制。
- 第三方 Agent 和更多 MCP Adapter。
- 云端隔离 Runner。

## 13. 首批技术验收标准

### CONN-01：设备注册

- 一台 Windows 设备可以通过一次性配对码注册。
- 云端可以看到设备在线状态和能力摘要。
- 撤销设备后不能继续领取新任务。

### CONN-02：后台运行

- 关闭 Desktop UI 后，`HEADLESS` Run 可以继续。
- 云端可以查看 Run 状态和事件。
- 本地重新打开 UI 后可以恢复展示。

### CONN-03：CLI 执行

- 平台可以通过 Adapter 启动一个 CLI。
- stdout、stderr、退出码和输出文件可以回传。
- Run 与 Task、Agent、Device 和工作区正确关联。

### CONN-04：断线恢复

- 运行过程中断开网络。
- 本地事件和输出进入持久化队列。
- 网络恢复后按序补传。
- 重复补传不会生成重复正式结果。

### CONN-05：审批门禁

- CLI 请求危险操作时生成 `ApprovalRequest`。
- 未批准前任务保持暂停。
- Web 或手机批准后继续执行。
- 拒绝或超时后任务不会继续执行。

### CONN-06：权限隔离

- Agent 不能访问未授权项目目录。
- 普通任务不能自动获得桌面控制权限。
- 本地用户可以紧急停止云端任务。
- 云端事件和本地控制事件可以审计。

### CONN-07：版本兼容

- Adapter 能报告外部 CLI 版本。
- 不兼容版本会被阻止或明确降级。
- 失败原因可定位到 Adapter、工具版本或操作系统。

## 14. 当前不冻结的事项

以下内容先保留为后续设计，不在本文件中提前绑定具体实现：

- Tauri、Electron 或原生 Windows UI 的最终选择。
- WebSocket、gRPC streaming 或 QUIC 的长期协议选择。
- Windows Service 的安装方式和更新机制。
- 是否使用 Docker、Podman 或 Windows Sandbox。
- Codex 和 Claude Code 的具体命令行参数。
- 完整远程桌面协议和屏幕传输方式。
- 手机端采用原生应用、跨平台框架或 PWA。
- 多设备任务迁移和跨设备热切换。

这些事项需要结合实际 CLI 版本、Windows 测试结果、安全评估和部署成本逐项冻结。

## 15. 与主计划的关系

`PROJECT_EXECUTION_PLAN.md` 仍然是阶段、依赖、验收和交接的执行事实来源。本文件只补充设备连接和本地运行时细节。

如果本文件与主计划发生冲突，应先记录冲突，再更新主计划中的冻结决策、阶段任务和验收标准，不能仅修改本文件绕过主计划。

本文件对应的实现完成后，必须新增阶段交接文档，至少说明：

- 已实现的本地组件和协议。
- 已支持的执行配置。
- 已验证的 CLI 和操作系统版本。
- 未实现的桌面控制和手机能力。
- 断线、审批和权限测试结果。
- 下一阶段的 Adapter 或 Runner 工作。
