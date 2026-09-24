# 阶段 3 P3-01 设备身份与项目授权交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-13
>
> 对应任务：P3-01 设备配对、设备凭证、项目范围 Agent Grant 与连接元数据

## 1. 本轮目标

在实现 WebSocket Gateway 和本地 Agent 之前，先冻结设备接入所依赖的后端契约和安全边界：

1. 定义 `Device`、`DevicePairing`、`DeviceProjectGrant` 和 `AgentConnection`。
2. 定义 Gateway 信封、心跳、执行配置和事件确认/补传请求。
3. 实现一次性配对码、设备 Token 和项目范围能力 Token 的开发版存储边界。
4. 实现设备撤销后的令牌和连接失效。
5. 用纯后端测试验证跨组织、跨项目和跨能力的拒绝行为。

## 2. 已完成

### 2.1 领域契约

`apps/api/app/contracts.py` 新增：

- `DeviceStatus`：`active`、`revoked`。
- `DevicePairingStatus`：`PENDING`、`CONSUMED`、`EXPIRED`、`REVOKED`。
- `AgentConnectionStatus`：`CONNECTING`、`CONNECTED`、`DISCONNECTED`、`REVOKED`。
- `ExecutionMode`：`HEADLESS`、`USER_SESSION`、`INTERACTIVE_DESKTOP`。
- `DevicePairingCreate`、`DevicePairing`、`DeviceRegisterRequest`、`Device`、`DeviceCredential`。
- `DeviceProjectGrantCreate`、`DeviceProjectGrant`、`DeviceProjectCredential`。
- `AgentConnection`、`ExecutionProfile`、`GatewayEnvelope`、`AgentHeartbeat`、`GatewayEventAck`、`GatewayReplayRequest`。

跨语言 JSON Schema 已同步到：

- `packages/contracts/domain.schema.json`
- `packages/contracts/domain.json`

### 2.2 SQLite Store

新增表：

- `devices`
- `device_pairings`
- `device_project_grants`
- `agent_connections`

安全边界：

- 配对码只保存 SHA-256，创建响应才返回原始码。
- 设备 Token 只保存 SHA-256，设备登记响应才返回原始 Token。
- 配对码只能消费一次；过期码会变为 `EXPIRED`。
- 设备只能登记到配对成员所拥有的 Agent。
- 项目授权同时绑定设备所属组织、Agent、项目和 capabilities。
- 设备撤销会将设备令牌失效、项目令牌标记撤销、现有连接标记为 `REVOKED`。
- 项目令牌解析检查设备状态、令牌状态、过期时间、项目 ID 和可选 capability。

为兼容现有的旧 Agent 任务领取接口，创建项目范围设备授权时也会更新旧的
`agent_project_grants`。这只是迁移期间的兼容路径，不代表旧接口已经完成设备 Token 强制鉴权。

### 2.3 API

新增接口：

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| POST | `/api/devices/pairings` | 成员创建一次性配对码 |
| POST | `/api/devices/register` | Agent 使用配对码登记设备并领取设备 Token |
| GET | `/api/devices` | 查看当前成员组织内的设备 |
| POST | `/api/devices/{device_id}/revoke` | 成员撤销设备 |
| POST | `/api/projects/{project_id}/device-grants` | 为设备签发项目范围能力 Token |
| GET | `/api/projects/{project_id}/device-grants` | 查看项目授权元数据，不返回 Token |
| POST | `/api/device-grants/{grant_id}/revoke` | 撤销项目范围 Token |

### 2.4 PostgreSQL 基线

新增 `apps/api/migrations/005_agent_devices.sql`，包括四张表、索引、约束和 RLS：

- `devices`、`device_pairings` 按 `organization_id` 隔离。
- `device_project_grants` 按关联项目所属组织隔离。
- `agent_connections` 按关联设备所属组织隔离。

该迁移已经过文本契约测试，但当前环境没有真实 PostgreSQL，尚未完成数据库执行验收。

## 3. 测试与验证

新增 `apps/api/test_devices.py`，覆盖：

1. 配对码一次性消费、设备/配对凭证不以明文入库。
2. Agent 所有者不匹配时拒绝设备注册，失败注册不会错误消费配对码。
3. 项目 Token 的项目范围和 capability 范围检查。
4. 跨组织设备授权和跨项目 Token 解析拒绝。
5. 设备撤销使设备 Token、项目 Token 和已有连接同时失效。
6. OpenAPI 路由和 PostgreSQL 迁移/RLS 契约存在性。

本轮验证结果：

```text
全量 unittest：51 passed，2 skipped
compileall：通过
domain.schema.json / domain.json：解析通过
```

跳过项仍为真实 MinIO/S3 和真实 PostgreSQL 集成测试，原因是当前环境没有对应服务。

## 4. 尚未完成与明确风险

本轮不能标记为阶段 3 生产完成，原因如下：

- `PostgresRepository` 尚未实现设备、配对、项目授权和连接方法；目前只有迁移/RLS 基线。
- 设备登记保存了公钥及其指纹，但尚未执行 challenge/signature 公钥持有证明。
- 设备 Token 和项目 Token 尚未接入正式 OIDC、短期会话轮换或硬件密钥保护。
- 旧 `/api/agents/...` 任务接口仍可以通过兼容 `agent_id` 和旧 Grant 工作，尚未强制项目 Token 与 capability。
- 尚未实现 Agent Gateway WebSocket、HTTPS 长轮询、心跳处理、连接序号确认和重放。
- 尚未实现 agentd 本地 SQLite 事件缓存、待确认事件、断线补传和本地紧急停止。
- 尚未做设备登记/令牌签发请求的完整幂等恢复；响应丢失时不能安全重新显示原始 Token。
- 目前仍没有真实 PostgreSQL RLS、并发领取和设备撤销竞态测试。

## 5. 下一步

1. 将 P3-01 的新领域对象加入 PostgreSQL Repository，保持与 SQLite Store 相同的查询和权限语义。
2. 在真实 PostgreSQL 中执行 `001` 至 `005` 迁移，验证 RLS 和设备撤销竞态。
3. 为设备登记加入 challenge/signature 证明，并设计设备 Token 轮换/恢复策略。
4. 实现 Gateway WebSocket：设备 Token 握手、`GatewayEnvelope` 校验、连接生命周期、心跳和序号确认。
5. 将项目 Token 与任务领取、成果物上传和 Run 创建统一绑定，逐步下线旧的无设备鉴权路径。
6. 在 Gateway 契约稳定后实现 agentd 本地 SQLite 队列、事件确认和断线补传。

## 6. 复现命令

```powershell
$env:PYTHONPATH = "$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s apps/api -p 'test_*.py' -v
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall -q apps/api
```

不要把集成测试的 `skipped` 解读为 PostgreSQL、MinIO 或生产设备连接已经验收。
