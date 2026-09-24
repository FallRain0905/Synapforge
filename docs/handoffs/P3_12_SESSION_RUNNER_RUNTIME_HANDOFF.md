# P3-12 User Session Worker 与 Runner 事件编排交接文档

> 日期：2026-09-13
>
> 状态：开发版完成；ConPTY、桌面执行和云端结果闭环尚未完成
>
> 对应计划：`docs/PROJECT_EXECUTION_PLAN.md` 第 10.15 节

## 1. 本轮目标

将 P3-10 的会话授权、P3-11 的 Windows Named Pipe 和 P3-09 的本地 Runner 组成一条可执行
的 `USER_SESSION` 链路，覆盖启动、stdin、输出事件、停止和本地离线队列。

本轮实现的是标准 stdin/stdout/stderr 管道。它支持用户会话进程，但不宣称实现 Windows
ConPTY 或 `INTERACTIVE_DESKTOP`。

## 2. 已完成

### 2.1 Runner 生命周期

`apps/agent/machine_service.py` 和 `apps/agent/runner.py` 已支持：

- `ProcessSpec.stdin_enabled`，只有用户会话模式启用 stdin。
- `LocalProcessSupervisor.request_stop`，可以请求终止而不重复消费完成结果。
- `LocalProcessSupervisor.write_stdin`，只向启用 stdin 的活动进程写入 UTF-8 数据。
- `LocalProcessSupervisor` 的 stdout/stderr 回调。
- `LocalRunner.start`、`wait`、`request_stop`、`write_stdin` 和 `active_run_ids`。
- 原有 `LocalRunner.run` 保留为启动后等待的兼容入口。
- `ProcessSpec` 项目、Agent、Device 和 Workspace 归属字段恢复为真正必填。
- stdout/stderr 共用 `max_output_bytes` 总预算；超出部分继续排空管道但不保存或发出事件。

### 2.2 Session Worker Runtime

新增 `apps/agent/session_runtime.py`：

- `SessionRuntimeConfig` 配置 Worker、用户会话、用户 SID、能力和执行模式。
- `SessionWorkerRuntime` 复用 `SessionWorkerBroker`，所有请求先经权限和状态校验。
- `session.run.start` 载荷由 `SessionRunStartPayload` 验证，然后转换为 `RunnerRequest`。
- Runtime 使用独立 asyncio loop 监督进程，避免阻塞同步 Named Pipe Handler。
- `session.run.stop` 请求终止活动 Runner，并等待其结果；并发结束时不误报清理失败。
- `session.terminal.input` 受 Broker 门禁后转发给活动用户会话进程。
- `INTERACTIVE_DESKTOP` 当前明确返回 `session_interactive_desktop_runtime_unavailable`。
- 桌面控制当前明确返回 `session_desktop_control_runtime_unavailable`。

### 2.3 事件与离线队列

Runtime 产生：

```text
process.started
process.stdout
process.stderr
process.exited
run.completed
run.failed
```

事件包含 Worker、Run、序号、时间和载荷，先进入 Runtime 内存队列。`agentd session-worker-run`
同时将事件写入 `LocalAgentState` 的 Gateway outbox：

```text
message_type: agent.event
payload: {"event": <SessionIpcEvent JSON>}
idempotency_key: session-event:<worker_id>:<sequence>
message_id: event_id
```

因此断线时事件可以沿用现有 Gateway 本地队列；云端正式事件映射、游标和 Run 状态闭环仍待后续。

### 2.4 CLI 入口

`apps/agent/agentd.py session-worker-run` 已提供开发版入口，可配置：

- Pipe 名称。
- Worker ID、用户 Session ID 和用户 SID。
- 允许的对端 ID、Peer 类型、SID 和 Windows Session ID。
- 工作区、Python 解释器、Adapter ID。
- 能力和环境变量白名单。
- 进程停止超时。

入口建立 `NamedPipeServer -> SessionWorkerRuntime -> SessionWorkerBroker -> LocalRunner` 链路。

## 3. 测试证据

更新/新增：

- `apps/agent/test_machine_service.py`
- `apps/agent/test_runner.py`
- `apps/agent/test_session_runtime.py`

覆盖：

- stdin 写入和 Runner 终止。
- stdout/stderr 实时事件和总输出预算。
- 活动 Run 状态、停止竞态和失败幂等结果。
- 非法启动载荷和桌面模式 fail-closed。
- 标准管道模式启动、stdin、退出事件。
- Named Pipe 请求到 Runtime、Broker、Runner 的 Windows 同机纵向链路。

当前 Windows 开发环境验证：

```text
apps/agent: 58 passed, 1 skipped
apps/api: 68 passed, 2 skipped
compileall apps/api apps/agent packages: passed
agentd session-worker-run --help: passed
Named Pipe -> SessionWorkerRuntime -> Broker -> Runner: passed
JSON Schema parse: domain/domain catalog/task state/gateway/session passed
API import: 68 routes / 51 OpenAPI paths
```

后续加入输出预算回归后，Agent 全量测试实际达到 `58` 项；其中 1 项为非 Windows 分支跳过。

## 4. 关键语义和约束

1. Broker 是唯一会话授权入口；Runtime 不得绕过 Broker 直接启动进程。
2. `session.run.start` 先写入 Broker 的活动 Run，再验证执行载荷；执行失败会把同一幂等键缓存结果替换为 `FAILED`。
3. `session.run.stop` 先由 Broker 校验活动 Run；物理终止失败会恢复活动状态并返回失败，避免假成功。
4. 用户注销后仍可清理已有 Run；停止最后一个 Run 后 Worker 保持 `PAUSED`，不会错误变成 `READY`。
5. `USER_SESSION` 只代表用户会话中的标准管道进程，不等于可操作桌面。
6. `INTERACTIVE_DESKTOP` 没有真实桌面执行器时必须失败，不得自动降级到后台模式。
7. 实时输出和最终结果都受同一 Run 总输出预算约束。
8. Runtime 事件回调或持久化失败不能改变子进程本身的成功/失败结果，但必须由后续审计暴露。

## 5. 未完成和不能宣称的能力

- 未实现 Windows ConPTY、PTY resize、终端 prompt 解析和完整交互式 CLI 语义。
- 未实现桌面控制、Office、浏览器、屏幕读取和 UI Automation。
- 未实现 Windows Service、Session 0 用户 Worker 启动、用户会话发现和注销监听。
- 未实现 Runtime 事件上传 Gateway 的正式命令/事件协议、确认游标和云端 Run 状态闭环。
- 未实现输出文件发现、内容哈希、Artifact 上传和正式结果门禁绑定。
- 未实现进程树终止、系统资源限制、OS 网络隔离、本机审计落盘和生产密钥管理。
- `NamedPipeServer` 仍是阻塞式单客户端循环，尚未完成多客户端并发和异步 Pipe 适配。
- 当前 CLI 入口是开发版前台进程，不是已注册、可升级、可恢复的 Windows Service。

## 6. 下一步

### P3-13：CLI Adapter、事件上传和 Artifact/Run 结果接入

1. 抽象 CLI Adapter 的版本、命令、stdin、退出和取消语义。
2. 将 Runtime 事件映射到 Gateway 事件/输出协议并持久化确认游标。
3. 发现输出文件、计算哈希、创建 Run Manifest 并上传 Artifact。
4. 将执行结果与平台 Run 状态、Review/Gate 和正式结果绑定。
5. 增加 Codex/Claude Code 等 CLI 的隔离适配器，不允许任意命令绕过白名单。

### P3-14：ConPTY、桌面执行和 Service

1. 设计 ConPTY 的 Windows 版本兼容和伪终端资源回收。
2. 在用户 Session 启动/监督真实 Worker，处理 Session 0 到交互 Session 的切换。
3. 对桌面控制增加人工审批、窗口范围和屏幕/输入审计。
4. 实现 Windows Service 安装、升级、恢复和服务账户 ACL 演练。

## 7. 接收检查清单

- [x] Runner start/wait/stop/stdin 生命周期。
- [x] USER_SESSION 标准管道执行。
- [x] stdout/stderr 实时事件和总预算。
- [x] Runtime 到 Broker/Runner 的授权执行链。
- [x] Named Pipe 到 Runtime 到 Runner 的 Windows 纵向测试。
- [x] 本地 Gateway outbox 事件持久化入口。
- [x] 主计划、实施状态和设备连接设计已同步。
- [ ] ConPTY 和完整交互式 CLI。
- [ ] Windows Service/Session 0/用户会话生命周期。
- [ ] Gateway 事件上传、Artifact 和正式 Run 结果门禁。
