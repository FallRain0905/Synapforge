# 阶段 3 P3-03 本地 Agent 状态与断线恢复交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-13
>
> 对应任务：P3-03 本地 `agentd` 持久化队列、Gateway 客户端和恢复边界

## 1. 本轮目标

在 P3-02 Gateway 协议基础上建立本地恢复事实，使 Agent 进程重启或网络断开后可以继续同步：

1. 将未确认 Gateway 事件持久化到本地 SQLite。
2. 为每个本地出站事件分配稳定序号、消息 ID 和幂等键。
3. 处理服务端 ACK、重复确认和序号补传请求。
4. 保存 Run、待上传文件和审批状态的最小恢复信息。
5. 给 `agentd` 增加设备登记、本地排队、状态恢复和单次 Gateway 会话入口。

## 2. 实际完成内容

### 2.1 本地 SQLite 状态库

新增 `apps/agent/local_state.py` 和 `LocalAgentState`：

- `agent_metadata`：保存本地 Gateway 出站序号游标。
- `gateway_outbox`：保存序号、消息 ID、幂等键、消息类型、载荷、发送状态、尝试次数和 ACK 时间。
- `run_states`：保存 Run 状态和最小上下文。
- `pending_uploads`：保存待上传路径、Artifact、哈希和重试状态。
- `approval_states`：保存本地审批状态和上下文。

事件状态固定为：

```text
PENDING -> SENT -> ACKED
```

进程重启时，`SENT` 且未确认的事件可以通过 `recover_unacked()` 回到 `PENDING`，原序号和消息 ID 不变。

### 2.2 持久化 Gateway 客户端

新增 `apps/agent/gateway_client.py` 和 `DurableGatewayClient`：

- `queue_event()`：幂等地写入本地出站队列。
- `pending_envelopes()`：把本地事件转换为平台 `GatewayEnvelope`，并在发送前标记 `SENT`。
- `handle_server_message()`：处理 `gateway.connected`、`gateway.ack`、`gateway.replay_required` 和 `gateway.error`。
- `recover_after_disconnect()`：恢复未确认事件。
- `run_once()`：使用可选 `websockets` 依赖运行一次主动 WebSocket 会话。

设备 Token 只作为 `run_once()` 的调用参数传入，不写入本地 SQLite。后续应由桌面端从 Windows Credential Manager 或等价系统密钥环提供。

### 2.3 `agentd` CLI

`apps/agent/agentd.py` 增加：

```text
device-register
gateway-queue
gateway-recover
gateway-run
```

旧的 HTTP 注册、领取、进度和结果回传命令保留，便于迁移期兼容；新 Gateway 命令不自动把旧 Agent HTTP 路径伪装成已完成的设备鉴权。

## 3. 恢复语义

### 正常发送

```text
queue_event
-> PENDING(sequence=N)
-> pending_envelopes
-> SENT(sequence=N)
-> gateway.ack(highest_contiguous_sequence=N)
-> ACKED(sequence=N)
```

### 进程/网络断开

```text
SENT 未收到 ACK
-> 进程重启或显式 recover
-> PENDING(sequence=N, message_id 不变)
-> 重连后重新发送
```

### 服务端发现缺口

```text
gateway.replay_required(after_sequence=K)
-> 本地 sequence > K 的未确认事件重新进入 PENDING
-> 按原序号顺序发送
```

重复 ACK 只改变尚未确认的记录；已经 `ACKED` 的事件不会被重复计数或重新发送。

## 4. 测试与验证

新增：

- `apps/agent/test_local_state.py`
- `apps/agent/test_gateway_client.py`

覆盖：

1. 序号和幂等键在重新打开 SQLite 后保持稳定。
2. `SENT` 未确认事件可以恢复。
3. ACK 只推进未确认前缀，重复 ACK 无副作用。
4. 补传请求重新排队缺口后的事件。
5. Run、上传和审批状态可重新打开读取。
6. 客户端封套生成、服务端 ACK/补传处理和身份不匹配拒绝。

本轮验证结果：

```text
本地 Agent 测试：6 passed
后端 Gateway/平台全量测试：56 passed，2 skipped
Python compileall：通过
```

跳过项仍为真实 MinIO/S3 和真实 PostgreSQL 集成测试。

## 5. 尚未完成与边界

- `run_once()` 是单次会话适配，不是 Machine Agent Service；没有自动重连退避、心跳调度、进程监督或 Windows 服务生命周期。
- 本地 SQLite 状态库未加密，没有系统密钥环、日志轮转、大小配额和损坏修复策略。
- 未实现 Windows Service 与用户会话 Worker 的 Named Pipe/本机 RPC。
- 未实现 ConPTY、CLI Adapter、Python Runner、Office/Browser 控制和本地紧急停止。
- Gateway 的 `agent.event` 尚未写入平台 Event/Run/Artifact，任务与租约命令还未接入项目 Token/capability。
- 客户端当前依赖 `app.contracts` 作为共享 Python 契约；后续应抽取独立的跨端协议包，避免本地 Agent 依赖 API 应用目录。
- 设备 Token 仍通过命令行参数提供，不能作为生产凭据保存方案。

## 6. 下一步

1. 补齐 PostgreSQL Repository 的设备连接、序号、心跳和项目 Token 查询实现。
2. 将项目 Token/capability 强制接入 Gateway 的任务领取、租约、Run 和 Artifact 操作。
3. 抽取独立 Python/TypeScript 协议包，固定跨端版本兼容矩阵。
4. 实现 Machine Agent Service：自动重连、指数退避、心跳、进程监督和本地紧急停止。
5. 实现 User Session Worker、IPC、ConPTY 和第一个 Headless Python Adapter。

## 7. 复现命令

```powershell
$env:PYTHONPATH = "$(Resolve-Path apps/agent);$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s apps/agent -p 'test_*.py' -v
$env:PYTHONPATH = "$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s apps/api -p 'test_*.py' -v
```

不要把本地恢复单元测试解释为真实多设备、TLS、Windows 服务或生产断线演练已经通过。
