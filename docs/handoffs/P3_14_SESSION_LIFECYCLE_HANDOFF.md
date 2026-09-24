# P3-14 会话生命周期与终端控制契约交接文档

> 日期：2026-09-13
>
> 状态：PASS_WITH_ASSUMPTIONS
>
> 对应计划：`docs/PROJECT_EXECUTION_PLAN.md` 第 10.17 节

## 1. 本轮目标

在接入真实 ConPTY 和 Windows Service 之前，冻结本地执行面的两个基础契约：

1. 交互式终端 resize 必须是显式 PTY 能力，标准管道没有能力时 fail-closed。
2. Machine Service、User Session Worker、用户会话和 Run 的启动、锁屏、注销、故障恢复与紧急停止
   必须具有确定的状态机和可审计记录。

本轮没有把开发机标准管道包装成 ConPTY，也没有宣称 Windows Service/Session 0 已经生产可用。

## 2. 已完成

### 2.1 共享协议

- `SESSION_IPC_MESSAGE_TYPES` 新增 `session.terminal.resize`。
- `TerminalSize` 定义 `columns`、`rows` 和可选的 `pixel_width`、`pixel_height`；列/行范围为 1 至
  1000，像素尺寸范围为 0 至 100000。
- `SessionIpcRequest` 对 resize 强制要求会话上下文、远程终端许可和合法尺寸。
- `SessionIpcEvent` 新增 `terminal.resized`。
- `SessionLifecycleTransition` 定义服务/Worker/会话/Run 的转移、活动 Run、清理 Run、原因、时间和
  元数据。
- Worker 注册、Worker 就绪和 Worker 退出使用独立事件类型，避免把“已启动”与“可接单”混为一谈。
- `packages/agent_protocol/session.schema.json` 与 Pydantic 模型同步，独立 Schema 解析通过。

### 2.2 Broker 与 Runner

- `SessionWorkerBroker` 对 resize 执行机器服务、活动 Run 和 `terminal.resize` 能力检查。
- `LocalRunner.resize_terminal()` 只调用显式 `TerminalResizeEnforcer`；没有 PTY 后端时抛出稳定错误
  `terminal_resize_runtime_unavailable`。
- `SessionWorkerRuntime` 接收 resize 请求，失败时保存幂等失败响应，成功时产生 `terminal.resized` 事件。
- Runtime 的默认开发版 Worker 能力包含 `terminal.resize`，但实际标准管道仍不会伪造 PTY。

### 2.3 生命周期状态机

新增 `apps/agent/service_lifecycle.py` 的 `MachineServiceLifecycle`，覆盖：

```text
STOPPED
→ STARTING / WAITING_FOR_SESSION
→ STARTING_WORKER
→ WORKER_READY
→ DEGRADED / FAILED / STOPPING / EMERGENCY_STOPPED
```

已冻结的关键语义：

- 服务启动时若没有可用会话，只产生 `discover_session` 动作。
- 会话发现或登录后产生 `start_worker` 动作，Worker 明确报告 ready 后才允许 Run。
- 锁屏不清理普通 `USER_SESSION` Run，但 `INTERACTIVE_DESKTOP` 不可启动。
- 注销产生停止活动 Run 和停止 Worker 的清理动作；清理中的 Run 不再计入新的活动 Run。
- Worker 异常退出进入 `DEGRADED` 或 `FAILED`，必须显式 `recovery.requested` 才能重新启动。
- 紧急停止进入 `EMERGENCY_STOPPED`，不能通过普通 `service.start` 绕过本地停止状态。

### 2.4 本地审计

- `LocalAgentState` 新增 `lifecycle_transitions` 表。
- `save_lifecycle_transition()` 使用 `INSERT OR IGNORE`，重复 transition ID 不会产生重复记录。
- `list_lifecycle_transitions()` 返回结构化活动 Run、清理 Run 和元数据。
- `SessionWorkerRuntime` 可以将 Broker 转移交给回调；`agentd session-worker-run` 已接入本地恢复库。

## 3. 测试证据

```text
apps/agent: 73 passed, 1 skipped
apps/api: 71 passed, 2 skipped
python -m compileall -q apps/api apps/agent packages: passed
所有 packages/*.schema.json 解析通过
```

新增/重点测试：

- `apps/agent/test_service_lifecycle.py`
  - 启动、会话发现、Worker 就绪
  - 锁屏/解锁和注销清理
  - Worker 故障与显式恢复
  - 紧急停止和非法转移不变更状态
- `apps/agent/test_session_runtime.py`
  - 无 PTY 后端时 resize fail-closed
  - resize 失败响应幂等
- `apps/agent/test_local_state.py`
  - 生命周期转移持久化和重复写入去重
- `apps/agent/test_protocol_schemas.py`、`test_session_worker.py`
  - 共享 Schema 定义和消息枚举同步

## 4. 未完成

- 真实 Windows ConPTY：Pseudo Console 创建、输入输出管道、PTY resize、Ctrl-C/取消和进程树回收。
- Codex/Claude Code 真实 CLI Adapter、Prompt 解析、tool/approval 事件和兼容性矩阵。
- Windows Service 安装/升级/恢复，Session 0 服务到交互用户 Session 的真实 Worker 启动。
- 锁屏、解锁、注销和用户 Session 变化的系统通知监听。
- 跨账户、跨 Session、睡眠/唤醒、服务重启和 Worker 崩溃的实机测试。
- 桌面执行器、窗口范围、屏幕/鼠标/键盘审计和人工审批链路。
- OS 级网络、资源、进程树和文件访问隔离。
- 真实 PostgreSQL/MinIO、NATS、设备 challenge/signature 和系统密钥环。

## 5. 下一步交接

下一轮建议拆成两个可独立验收的子切片：

1. **P3-15 ConPTY Adapter**：在不改变现有 IPC/Run/Event 协议的前提下，实现 Windows 版本探测、
   Pseudo Console 生命周期、输入/输出、resize、取消和回收；没有满足版本或权限条件时继续
   fail-closed。
2. **P3-16 Windows Service Adapter**：把本交接中的生命周期动作接入 Windows Service、Session 0、
   会话发现和 User Session Worker 启动；先完成服务重启/锁屏/注销矩阵，再做桌面执行器。

真实基础设施验收可并行推进，但不得用 SQLite 开发版结果替代 PostgreSQL/RLS/MinIO 生产验收。

## 6. 复审入口

```text
packages/agent_protocol/__init__.py
packages/agent_protocol/session.schema.json
apps/agent/service_lifecycle.py
apps/agent/session_worker.py
apps/agent/session_runtime.py
apps/agent/runner.py
apps/agent/local_state.py
apps/agent/agentd.py
apps/agent/test_service_lifecycle.py
apps/agent/test_session_runtime.py
apps/agent/test_local_state.py
```
