# P3-10 User Session Worker/本机 IPC 契约交接文档

> 日期：2026-09-13
>
> 状态：开发版完成；真实 Windows 本机 IPC 尚未完成
>
> 对应计划：`docs/PROJECT_EXECUTION_PLAN.md` 第 10.13 节

## 1. 本轮目标

在实现 Windows Named Pipe 之前，冻结 Machine Service、User Session Worker 和 Desktop UI
之间的消息、身份、权限、审批、幂等和生命周期语义。本轮只实现传输无关的内存 Broker，
不把它当作跨进程通信或 Windows Service。

## 2. 已完成

### 2.1 共享协议

新增 `packages/agent_protocol` 会话协议模型：

- `SessionPeer`：调用方 ID、调用方类型、进程 ID 和用户 SID。
- `SessionRunContext`：项目、任务、Run、Agent、Device、Workspace 和执行模式绑定。
- `SessionIpcRequest`：请求 ID、幂等键、消息类型、Peer、Worker、用户会话、上下文、审批和载荷。
- `SessionIpcResponse`：请求关联、状态、错误码、响应时间和结果载荷。
- `SessionIpcEvent`：事件 ID、Worker、序号、Run、时间和事件载荷。
- `SESSION_IPC_MESSAGE_TYPES`：会话消息类型白名单。

独立 Schema：`packages/agent_protocol/session.schema.json`。Schema 与 Pydantic 模型保持同一组
消息类型、字段边界和错误响应规则。

### 2.2 Broker 与权限边界

实现文件：`apps/agent/session_worker.py`。

`SessionWorkerBroker` 当前负责：

- 注册 Worker 及其用户会话、用户 SID、支持的执行模式和能力。
- 校验请求 Peer 与已认证 Peer 的 ID、类型；User Session Worker 和 Desktop UI 还必须匹配用户 SID。
- Machine Service 不要求携带交互用户 SID，其服务身份必须由未来的 Named Pipe/ACL 传输层认证并传给 Broker。
- 校验 User Session Worker 的 Peer ID 必须等于注册的 Worker ID。
- 校验 Worker、用户会话和执行上下文绑定关系。
- 按消息类型限制调用方：Machine Service、User Session Worker、Desktop UI 各自只能调用授权操作。
- 校验 `HEADLESS`、`USER_SESSION`、`INTERACTIVE_DESKTOP` 与 Worker 能力和上下文声明的一致性。
- 对终端输入要求 `terminal.input` 能力和 `allow_remote_terminal=true`。
- 对桌面控制要求 `desktop.control` 能力、`INTERACTIVE_DESKTOP`、未锁定会话和有效人工审批。
- 对 Run 启动/停止要求 `session.run` 能力。
- 对重复幂等键返回 `DUPLICATE`，同键不同内容返回 `session_idempotency_conflict`。
- Worker 注销后拒绝普通请求；shutdown 会清理活动 Run。

### 2.3 Run 状态语义

- `session.run.start` 只能由 Machine Service 发起，已存在的活动 Run 被拒绝。
- `session.run.stop` 只能由 Machine Service 发起，不存在的 Run 被拒绝。
- 用户注销或会话不可用后，仍允许停止已有 Run，以便清理本地进程和状态。
- Activity Run 期间收到 `session.status` 不会把 Worker 错误设置为闲置；注销时进入 `PAUSED`。
- `INTERACTIVE_DESKTOP` 仅允许在 `logged_in` 的未锁定会话中启动或执行桌面控制。
- `session.worker.shutdown` 会进入 `STOPPED` 并清空活动 Run。

## 3. 测试证据

新增和更新：

- `apps/agent/test_session_worker.py`
- `apps/agent/test_protocol_schemas.py`

覆盖范围：

- Machine Service 启动/停止 User Session Run。
- Worker status/hello 的调用方限制和 SID/Worker 身份绑定。
- `session.run`、`terminal.input`、`desktop.control` 能力门禁。
- 用户登录、锁定、注销和活动 Run 状态转换。
- 桌面审批有效期、批准和拒绝。
- 幂等重复与同键冲突。
- 停止不存在 Run、停止已存在 Run 和 shutdown 清理。
- Session、Gateway 和领域 Schema 的 JSON 解析及消息类型对齐。

验证结果：

```text
apps/agent: 39 passed
apps/api: 68 passed, 2 skipped
compileall apps/api apps/agent packages: passed
JSON Schema parse: domain/domain catalog/task state/gateway/session passed
API import: 68 routes / 51 OpenAPI paths
```

两个跳过项是未提供真实 PostgreSQL/MinIO 服务的集成测试，不代表生产数据库验收通过。

## 4. 明确未完成

本轮不能宣称以下能力已经实现：

- Windows Named Pipe 或其他真实本机 IPC 传输。
- Windows 用户/服务 ACL 和真实进程身份认证。
- Windows Service 安装、升级、恢复和 Session 0 处理。
- 用户会话发现、切换、注销监听和真实 Worker 进程管理。
- ConPTY、终端输入执行、桌面控制执行和屏幕状态传输。
- 进程树终止、OS 级网络隔离、资源限制和文件访问审计。
- 跨进程消息持久化、事件序号恢复和本机审计日志落盘。

当前 `_results`、Worker 记录和审批记录均为进程内内存状态，只适合开发版契约测试。

## 5. 关键设计决策

1. 授权规则留在传输无关 Broker，Named Pipe 适配器只负责连接、认证、解码、调用和编码。
2. 真实传输必须在 Broker 之前提供已认证 Peer，不能仅相信请求 JSON 内的身份字段。
3. `session.run.stop` 是清理动作，不能依赖用户仍处于可启动状态；但仍必须验证 Run 当前存在。
4. 所有高风险桌面操作需要独立审批 ID，审批必须有过期时间并绑定后续传输身份和运行上下文。
5. 生产实现必须保持当前错误码和幂等语义，若破坏性修改需增加 Schema 版本和迁移说明。

## 6. 下一步交接

### P3-11：Windows Named Pipe/ACL/进程认证传输适配

- 选择并记录服务端与 User Session Worker 的 Pipe 命名和生命周期。
- 建立 Windows Service SID、用户 SID、Pipe ACL 和最小权限设置。
- 让传输层提供不可伪造或可验证的 Peer 身份给 Broker。
- 增加连接关闭、半包、超时、最大消息长度、版本协商和重放保护。
- 验证服务重启、Worker 重启、用户注销、Pipe 断开和重复请求恢复。
- 保持 `SessionWorkerBroker` 作为唯一授权入口，不在 Windows 适配器中复制业务规则。

### P3-12：真实 User Session Runner 编排

- 将 `USER_SESSION`/`INTERACTIVE_DESKTOP` Runner 请求接入 Worker。
- 接入 ConPTY 或受控终端执行器。
- 将 Run Manifest、输入输出文件、事件和 Artifact 上传接入现有平台协议。
- 继续保留本轮开发版策略的 fail-closed 行为，并增加 Windows 实机验收。

### 并行基础设施验收

- 真实 PostgreSQL/MinIO、RLS、设备撤销竞态和连接池验收。
- 设备 challenge/signature 持有证明和 Windows Credential Manager 接入。

## 7. 接收检查清单

- [x] 会话消息类型、请求/响应/事件模型已冻结。
- [x] Session JSON Schema 已建立并有解析/对齐测试。
- [x] Worker、Machine Service、Desktop UI 的调用方权限已编码。
- [x] 用户 SID、Worker ID、执行模式、能力和审批门禁已测试。
- [x] Run 启停、注销清理、活动状态和 shutdown 语义已测试。
- [x] 幂等重复与冲突语义已测试。
- [ ] Named Pipe 和 Windows ACL。
- [ ] 真实进程身份认证和跨进程恢复。
- [ ] Windows Service、ConPTY 和桌面控制执行。
- [ ] 生产级本机审计和 OS 隔离。
