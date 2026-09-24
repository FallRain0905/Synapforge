# P3-11 Windows Named Pipe/ACL/身份认证传输交接文档

> 日期：2026-09-13
>
> 状态：开发版完成；生产级 Windows Service 和跨账户验收尚未完成
>
> 对应计划：`docs/PROJECT_EXECUTION_PLAN.md` 第 10.14 节

## 1. 本轮目标

将 P3-10 的传输无关 `SessionWorkerBroker` 接入 Windows 原生本机通信，完成：

- 有边界的请求/响应帧编码。
- 双工 Named Pipe 连接。
- Pipe ACL 和拒绝远程客户端。
- 对端 PID、Token SID 和 Windows Session ID 认证。
- 认证结果转换为 `SessionPeer` 并交给 Broker。

本轮不实现 Windows Service 安装、Session 0 中启动用户 Worker、ConPTY 或桌面控制。

## 2. 已完成

实现文件：`apps/agent/named_pipe_transport.py`

### 2.1 帧协议

`JsonFrameCodec` 使用：

```text
4-byte little-endian body length
UTF-8 JSON object
```

已实现：

- 默认最大帧大小 1 MiB。
- 编码时拒绝不可序列化对象和超限正文。
- 解码时拒绝空帧、超限帧、截断头、长度不一致、非法 UTF-8、非法 JSON 和非对象载荷。
- `NamedPipeConnection` 使用 `_read_exact`/`_write_all` 处理部分读写。

### 2.2 Windows 传输

- 使用 Windows 原生 `ctypes`，当前不依赖 `pywin32`。
- `NamedPipeServer`/`NamedPipeClient` 使用双工 byte-mode Named Pipe。
- 同一个连接可以连续处理多个 Session 请求，客户端断开后服务端释放句柄。
- 连接名统一为 `\\.\pipe\<name>`，拒绝包含额外路径片段或超长名称。
- 连接策略保持 blocking API，异步服务应通过线程或后续异步适配器调用。

### 2.3 ACL 和身份认证

`PipePeerPolicy` 包含：

```text
peer_id
peer_kind
allowed_sid / allowed_sids
allowed_session_ids
max_frame_bytes
```

服务端：

1. 使用 SDDL 创建 DACL，授予 SYSTEM、Administrators 和配置的受信 SID 访问权限。
2. 设置 `PIPE_REJECT_REMOTE_CLIENTS`，明确拒绝远程客户端。
3. 使用 `GetNamedPipeClientProcessId` 获取实际连接进程 PID。
4. 使用客户端 Token 查询实际 SID。
5. 使用 `ProcessIdToSessionId` 查询实际 Windows Session ID。
6. 只有 SID 和 Session ID 同时满足策略，才生成认证后的 `SessionPeer`。

`SessionPeer` 新增 `windows_session_id` 字段。Broker 还会校验请求里的 PID 和 Windows Session ID
与认证层结果一致，防止请求 JSON 自报身份。

Machine Service 的服务账户 SID 与交互用户 SID 分离。服务身份必须由 Pipe ACL 和配置策略认证，
不再要求 Machine Service 请求携带用户 SID。

## 3. 测试证据

新增：`apps/agent/test_named_pipe_transport.py`

覆盖：

- JSON 帧正常往返。
- 超限、截断、长度错误和非法 JSON 拒绝。
- Windows 实机同机 Named Pipe 连接。
- 同一连接上的多请求收发。
- 客户端 PID、SID、Windows Session ID 认证。
- 与 `SessionWorkerBroker` 的真实传输到授权链路串接。
- 不在允许 Session ID 范围的客户端被拒绝且不能到达业务 Handler。
- 多 SID、多 Session ID 策略。
- 非 Windows 环境显式拒绝 Named Pipe 使用。

当前 Windows 开发机验证结果：

```text
apps/agent/test_named_pipe_transport.py: 6 passed, 1 skipped
```

全量验证：

```text
apps/agent: 47 passed, 1 skipped
apps/api: 68 passed, 2 skipped
compileall apps/api apps/agent packages: passed
JSON Schema parse: domain/domain catalog/task state/gateway/session passed
API import: 68 routes / 51 OpenAPI paths
```

跳过项分别是跨平台非 Windows 分支和未提供真实 PostgreSQL/MinIO 服务的集成测试，不代表生产验收完成。

## 4. 重要使用约束

### 4.1 Pipe 策略必须显式配置 Session

`PipePeerPolicy.allowed_session_ids` 为必填语义。部署方必须根据连接方向显式配置：

- 服务到用户 Worker：允许服务所在 Windows Session，通常需要包含 Session 0。
- 用户 Worker 到 Desktop UI：允许目标交互用户 Session。
- 测试环境：使用测试进程实际 Session ID。

不要用空 Session 列表作为生产默认值，也不要把用户 Session ID 当作用户 SID。

### 4.2 传输层不是授权层

Named Pipe 只证明 OS 层连接进程的 PID/SID/Session，并把结果交给 Broker。调用方仍必须：

- 使用配置的 `peer_id` 和 `peer_kind`。
- 通过 `SessionIpcRequest` 的 Pydantic 校验。
- 进入 `SessionWorkerBroker.handle`。

不得在新传输适配器中复制 Run、终端、桌面或审批授权逻辑。

### 4.3 当前 API 形态

服务端：

```python
server = NamedPipeServer(
    "MathAgentPlatform-worker",
    PipePeerPolicy(
        peer_id="machine-service-001",
        peer_kind="machine_service",
        allowed_sids=(service_sid,),
        allowed_session_ids=(0,),
    ),
)
server.serve_forever(broker.handle, stop_event=stop_event)
```

客户端：

```python
client = NamedPipeClient("MathAgentPlatform-worker")
response = client.request(request)
```

`server.serve_forever` 的 Handler 签名为：

```text
(SessionIpcRequest, SessionPeer) -> SessionIpcResponse
```

## 5. 未完成和不能宣称的能力

- 未实现 Windows Service 安装、升级、服务 SID 配置、恢复和 Session 0 启动链路。
- 未完成服务账户与普通用户账户之间的真实跨 Session/跨账户测试矩阵。
- 未完成 Pipe 重放保护、连接级认证握手、消息序列持久化和断线恢复。
- 未实现 ConPTY、终端输入执行、桌面控制执行和屏幕传输。
- 未实现本机操作审计落盘、资源限制、进程树终止和 OS 级网络隔离。
- `NamedPipeServer` 当前是阻塞式单客户端循环，需要后续并发/异步调度设计。
- 当前传输不接入 `agentd` 启动命令，也未将 Worker 生命周期接到 Machine Agent Service。

## 6. 下一步

### P3-12：User Session Worker 与 Runner/ConPTY 编排

1. 在用户 Session 中启动并监督真实 Worker 进程。
2. 将 Machine Service 的 `session.run.start/stop` 接入 Named Pipe。
3. 将 Worker 的 `session.hello/status` 接入连接生命周期。
4. 接入 `USER_SESSION` 与 `INTERACTIVE_DESKTOP` Runner。
5. 对接 ConPTY、stdin/stdout/stderr 和统一事件协议。
6. 让断线、注销、锁屏、Worker 崩溃和服务重启进入可恢复状态机。

### 并行基础设施

- 设备 Token 接入 Windows Credential Manager。
- PostgreSQL/MinIO 真实集成与阶段 2/3 生产退出。
- 本机审计事件持久化和日志轮转。

## 7. 接收检查清单

- [x] 长度前缀 JSON 帧和边界检查。
- [x] Named Pipe 双工客户端/服务端。
- [x] SDDL ACL 和拒绝远程客户端。
- [x] 对端 PID、SID、Windows Session ID 获取。
- [x] 多 SID/Session 策略和不匹配拒绝。
- [x] 传输到 SessionWorkerBroker 的串接测试。
- [x] 主计划、实施状态和设备连接设计已同步。
- [ ] Windows Service 和 Session 0 实机部署。
- [ ] 跨账户/跨 Session、重启恢复和压力验收。
- [ ] ConPTY、Runner、事件和 Artifact 集成。
