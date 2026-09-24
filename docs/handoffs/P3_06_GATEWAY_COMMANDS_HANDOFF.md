# 阶段 3 P3-06 Gateway 任务与 Run 命令交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-13
>
> 对应任务：P3-06 将项目能力 Token 从 HTTP Agent 写路径延伸到 Gateway 消息路径

## 1. 本轮目标

P3-05 已经收紧 HTTP 任务和 Run 写接口，但 Gateway 仍只处理心跳和通用事件。这样本地
Agent 如果从 WebSocket 工作，仍无法使用正式任务协议。本轮把首批任务执行操作放入
`GatewayEnvelope`，并复用 P3-05 的项目能力 Token 语义。

## 2. 已完成内容

### 2.1 支持的消息类型

```text
agent.task.claim
agent.task.lease.heartbeat
agent.task.progress
agent.task.result
agent.run.create
agent.run.complete
```

每个命令 payload 都必须包含：

```json
{
  "project_id": "...",
  "project_token": "prj_..."
}
```

其余字段按命令使用：

| 命令 | 额外字段 | 返回结果 |
| --- | --- | --- |
| `agent.task.claim` | `lease_seconds`、`stages` | TaskAssignment 或 null |
| `agent.task.lease.heartbeat` | `lease_token`、`extend_seconds` | TaskLease |
| `agent.task.progress` | `task_id`、`lease_token`、`status`、`message` | 更新后的 Task |
| `agent.task.result` | `task_id`、`lease_token`、`success`、输出成果物和 Handoff | TaskResult |
| `agent.run.create` | RunCreate 除 `agent_id`/`idempotency_key` 外的字段 | Run |
| `agent.run.complete` | `run_id` 和 RunComplete 字段 | Run |

### 2.2 鉴权与资源归属

Gateway 先验证连接封套内的设备、Agent、Session 和 Connection ID，再验证项目能力 Token：

1. Token 必须存在并且未失效。
2. Token 的项目必须等于 payload 的 `project_id`。
3. Token 的 device 和 agent 必须等于当前 Gateway 连接上下文。
4. Token 必须包含当前命令所需能力。
5. Task 或 Run 资源的真实 `project_id` 必须等于 payload 的项目。

命令所需能力与 HTTP 层保持一致：

```text
task.claim
task.lease
task.progress
task.result
run.create
run.complete
```

### 2.3 ACK 和幂等

成功的命令返回 `gateway.ack`，并在 ACK payload 中加入：

```json
{
  "command_result": {
    "message_type": "agent.task.claim",
    "result": {}
  }
}
```

同一消息的入站 sequence 仍由 Gateway Repository 记录，因此重复投递不会再次执行任务或
Run。`DurableGatewayClient` 会照常根据 ACK 确认本地队列，并将 `command_result` 返回给
上层调用者。

命令错误返回 `gateway.error`。错误命令的入站 sequence 已经被消费，避免客户端无限重放
同一条已判定的请求；如果客户端随后重发同一 sequence，服务端只返回重复 ACK。

## 3. 修改文件

- `apps/api/app/gateway.py`
  - 增加 Gateway 命令类型集合。
  - 增加项目 Token、设备和 Agent 绑定检查。
  - 增加 Task/Lease/Run 命令分发。
  - 增加命令结果 ACK。
- `apps/agent/gateway_client.py`
  - 在处理 `gateway.ack` 时透传 `command_result`。
- `packages/contracts/domain.schema.json`、`packages/contracts/domain.json`
  - 登记已支持的 Gateway 命令类型，保留 `agent.*` 扩展消息的协议空间。
- `apps/api/test_gateway.py`
  - 增加任务命令成功/Token 错误和 Run 创建/完成测试。
- `apps/agent/test_gateway_client.py`
  - 增加客户端读取命令结果的测试。
- `docs/PROJECT_EXECUTION_PLAN.md`
  - 增加 P3-06 执行切片、消息类型、验收和下一轮任务。
- `docs/IMPLEMENTATION_STATUS.md`
  - 更新 Gateway 当前能力和剩余边界。

## 4. 测试与验证

### 4.1 Gateway 测试

```text
apps/api/test_gateway.py：7 passed
```

覆盖：

- 任务命令使用项目 Token 并返回结果 ACK。
- 错误 Token 返回 Gateway error，且 sequence 正确消费。
- Run 创建和完成使用项目 Token。
- 既有设备握手、心跳、重复帧、缺口补传和撤销连接行为不回归。

### 4.2 Agent 客户端测试

```text
apps/agent/test_gateway_client.py：4 passed
```

覆盖客户端的队列、ACK、补传、服务端身份和 `command_result` 读取。

### 4.3 全量基线

P3-06 完成后后端全量测试为 63 项通过、2 项真实基础设施测试跳过；本地 Agent 测试为
7 项通过。契约 JSON 文件解析通过。2 项跳过项仍是未提供服务时的真实 PostgreSQL/RLS/连接池
和 MinIO/S3 集成测试。

## 5. 尚未完成与风险

1. 目前使用 Fake WebSocket/开发版 SQLite；真实 WebSocket、TLS、反向代理和跨进程连接
   尚未验证。
2. PostgreSQL Repository 尚未覆盖完整 Task、Lease 和 Run 编排，无法宣称跨实例 Gateway
   已生产验收。
3. 如果成功 ACK 丢失，重复消息当前只返回重复 ACK，不重新携带业务结果；需要结果缓存或
   可查询的 command operation 记录解决。
4. Gateway 尚未支持 Artifact 上传、Handoff、审批和终端/桌面命令。
5. 项目 Token 仍以内嵌 payload 的形式传递，后续需要结合协议加密、日志脱敏和凭据轮换。
6. 设备公钥持有证明、系统密钥环和 Machine Agent Service 仍未实现。

## 6. 下一步

1. 抽取独立 `packages/agent-protocol`，提供 JSON Schema、TypeScript 类型和 Python 类型，
   去除本地 Agent 对 `apps/api` Python 包的直接依赖。
2. 为 Gateway 命令增加服务端 operation/result 持久化，使丢 ACK 后可以按 message ID 或
   idempotency key 查询原结果。
3. 把 Artifact 上传、Handoff 交接和审批请求纳入命令白名单，并逐项定义门禁。
4. 实现 Machine Agent Service：自动重连、退避、心跳调度、进程监督和本地紧急停止。
5. 提供 PostgreSQL/MinIO，执行真实迁移、RLS、并发序号、撤销竞态和长连接恢复测试。

## 7. 复现命令

```powershell
$env:PYTHONPATH = "$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest apps.api.test_gateway -v

$env:PYTHONPATH = "$(Resolve-Path apps/agent);$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest apps.agent.test_gateway_client -v
```

本交接只证明开发版协议和纯后端行为，不证明生产部署、安全密钥管理或真实跨实例运行。
