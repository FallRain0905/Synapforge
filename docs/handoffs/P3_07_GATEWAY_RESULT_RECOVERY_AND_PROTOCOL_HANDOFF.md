# 阶段 3 P3-07 Gateway 结果恢复与跨端协议包交接

> 交接日期：2026-09-13
>
> 对应计划：`docs/PROJECT_EXECUTION_PLAN.md` 2.3
>
> 本轮状态：开发版完成，生产验收未完成

## 1. 本轮目标

本轮承接 P3-06，处理两个已经确认的协议差距：

1. Gateway 首次执行命令后，如果 ACK 丢失，重复消息不能只返回空的重复 ACK，必须恢复原业务结果或原错误。
2. `apps/agent` 不应直接依赖 `apps/api.app.contracts` 作为跨进程协议边界，需要独立的共享协议包。

本轮不包含真实 PostgreSQL、TLS 反向代理、自动重连服务、Artifact/Handoff/审批 Gateway 命令，也没有切换默认 SQLite 运行时。

## 2. 已完成

### 2.1 Gateway 命令结果持久化

新增 `GatewayCommandResult`，字段包括：

```text
id
connection_id
message_id
idempotency_key
sequence
message_type
status: SUCCEEDED | FAILED
response_type: gateway.ack | gateway.error
result
error_code
created_at
```

结果按 `connection_id` 隔离，并对以下组合建立唯一约束：

```text
(connection_id, message_id)
(connection_id, idempotency_key)
```

Gateway 行为现在是：

- 首次成功命令：执行后保存结果，再随 `gateway.ack.command_result` 返回。
- 首次失败命令：保存错误码，再返回 `gateway.error`。
- 相同序号和相同 `message_id` 重复投递：按消息 ID 恢复原结果。
- 新消息 ID 复用同一幂等键：按幂等键恢复原结果，不再次执行命令。
- 失败结果重复投递：恢复原错误码，不再次执行命令。
- 命令返回 `None` 时也会显式携带 `command_result.result: null`，不会丢失“命令已处理”的事实。

### 2.2 SQLite 开发实现

`apps/api/app/store.py` 新增：

- `gateway_command_results` 表。
- `get_gateway_command_result()`：按消息 ID、幂等键或二者联合查询。
- `save_gateway_command_result()`：校验连接存在、识别重复记录和身份冲突。
- 结果 JSON 的序列化与领域模型还原。

SQLite 表由 Store 初始化自动创建，现有开发数据库可以直接启动。

### 2.3 PostgreSQL 迁移和 Repository

新增：

- `apps/api/migrations/006_gateway_command_results.sql`
- `PostgresRepository.get_gateway_command_result()`
- `PostgresRepository.save_gateway_command_result()`
- `PostgresRepository._gateway_command_result()`

迁移包含：

- 结果表、状态约束、响应类型约束。
- 连接/消息 ID 和连接/幂等键索引。
- 通过 `agent_connections -> devices -> organization_id` 继承的租户 RLS 策略。

当前只完成 SQL 和 Repository 代码，未在真实 PostgreSQL 执行迁移。

### 2.4 独立 Agent Gateway 协议包

新增：

```text
packages/__init__.py
packages/agent_protocol/__init__.py
packages/agent_protocol/gateway.schema.json
```

共享包提供：

- `GATEWAY_COMMAND_TYPES`
- `GatewayEnvelope`
- `AgentHeartbeat`
- `GatewayEventAck`
- `GatewayReplayRequest`
- `GatewayCommandResult`

`apps/api/app/contracts.py` 重新导出共享协议模型，`apps/agent/gateway_client.py` 直接使用
`packages.agent_protocol`。这样 API 和 Agent 仍可以使用同一组 Pydantic 字段约束，但 Agent
不再导入 API 内部业务契约。

独立 JSON Schema 为 `gateway.schema.json`，领域总 Schema 和 `domain.json` 也加入了
`GatewayCommandResult` 及其状态目录。

## 3. 修改文件

```text
packages/__init__.py
packages/agent_protocol/__init__.py
packages/agent_protocol/gateway.schema.json
packages/contracts/domain.schema.json
packages/contracts/domain.json
apps/api/app/contracts.py
apps/api/app/gateway.py
apps/api/app/repository.py
apps/api/app/store.py
apps/api/app/postgres_repository.py
apps/api/migrations/006_gateway_command_results.sql
apps/agent/gateway_client.py
apps/agent/test_gateway_client.py
scripts/start-api.ps1
apps/api/test_gateway.py
apps/api/test_postgres_repository.py
apps/api/test_platform_contracts.py
apps/api/test_stage2_integration.py
docs/PROJECT_EXECUTION_PLAN.md
docs/IMPLEMENTATION_STATUS.md
docs/POSTGRESQL_MIGRATIONS.md
docs/AGENT_DEVICE_CONNECTION_DESIGN.md
README.md
```

## 4. 验证结果

使用工作区 Python：

```text
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
```

验证结果：

```text
apps/api unittest       67 passed, 2 skipped
apps/agent unittest      7 passed
compileall              passed
JSON parse              domain.schema.json/domain.json/gateway.schema.json passed
API import               68 routes / 51 OpenAPI paths
```

跳过的 2 项仍然是既有真实 PostgreSQL 和 MinIO 集成测试，因为当前环境没有对应服务。

新增 Gateway 行为测试：

- `test_duplicate_command_recovers_persisted_business_result_after_ack_loss`
- `test_same_idempotency_key_with_new_message_id_reuses_command_result`
- `test_duplicate_failed_command_recovers_persisted_error`

## 5. 当前协议语义

结果查找顺序由 Gateway 按以下逻辑执行：

```text
1. 入站身份和消息类型校验
2. 记录入站连续序号
3. 若序号重复，按 message_id 查结果，再按 idempotency_key 查结果
4. 若序号新但幂等键已存在，直接恢复已保存结果
5. 否则执行命令
6. 保存成功结果或失败错误
7. 生成 ACK 或 gateway.error
```

`gateway.ack` 的 `command_result` 仍保留 P3-06 的外部格式：

```json
{
  "message_type": "agent.task.claim",
  "result": {}
}
```

本轮没有改变 Agent 本地出站队列的序号、PENDING/SENT/ACKED 状态和补传协议。

## 6. 未完成和风险

### 6.1 业务副作用与结果写入不是同一事务

当前流程是：

```text
record_gateway_receive
-> 执行任务或 Run 业务操作
-> save_gateway_command_result
-> 返回 ACK
```

在跨实例并发场景下，两个 Gateway 可能同时看到幂等记录不存在并同时执行业务副作用，最后才由
结果表唯一约束收敛。这不能作为生产级 exactly-once 保证。后续需要把幂等占位、业务写入和结果
提交放进同一个数据库事务，或明确采用可恢复的 `PENDING` 命令状态和业务操作租约。

### 6.2 崩溃窗口

如果服务在“入站序号已确认、业务执行尚未保存结果”之间崩溃，当前恢复路径无法重建未知结果。
后续应增加命令状态记录或事务化命令执行协调器，不能只依赖重复消息。

### 6.3 生产基础设施

- `006_gateway_command_results.sql` 尚未在真实 PostgreSQL 应用。
- PostgreSQL RLS、连接池、撤销竞态和并发序号尚未集成验收。
- WebSocket 仍未经过真实 TLS、反向代理和跨进程长连接测试。
- Agent Token 仍由调用方提供，尚未进入 Windows Credential Manager。

### 6.4 功能范围

- Machine Agent Service 和自动重连监督器未实现。
- Artifact 上传、Handoff 交接、Review/Approval Gateway 命令未实现。
- 当前协议包提供 Python 类型和 JSON Schema，尚未提供 TypeScript 类型生成/发布流程。

## 7. 下一步

下一轮建议编号为 P3-08，优先顺序如下：

1. 设计 Machine Agent Service 的自动重连、指数退避、心跳调度、停止信号和进程监督。
2. 为 P3-07 增加真实 PostgreSQL 验收环境，执行 001 至 006 迁移并验证 RLS。
3. 设计幂等占位和业务副作用同事务方案，关闭并发重复执行窗口。
4. 将设备 Token 接入 Windows Credential Manager，避免命令行参数成为长期凭据方案。
5. 再扩展 Artifact、Handoff 和审批命令，沿用同一项目能力和结果恢复语义。

## 8. 交接结论

P3-07 已完成开发版目标：命令结果有持久化记录，ACK 丢失后的重复命令可以恢复原业务结果，
API 与 Agent 已使用独立跨端协议包，相关 Schema 和测试已同步。阶段 3 仍处于 `PARTIAL`，
不能据此宣称生产级 exactly-once、真实 PostgreSQL Gateway 或自动重连服务已经完成。
