# 阶段 3 P3-20 设备 Token 轮换交接

## 1. 本轮目标和任务 ID

- 任务 ID：`P3-20`
- 目标：在已有 challenge/signature 和 Windows Credential Manager 开发版基础上，建立设备 Token
  的服务端轮换契约。
- 轮换语义：管理员触发后，旧 Token 立即失效；新 Token 只返回一次；设备已有 Gateway 连接立即
  撤销；数据库不保存任何明文 Token。
- 本轮非目标：双 Token 过渡窗口、设备私钥轮换、Agent 自动写回系统凭据、正式 OIDC、真实
  PostgreSQL 并发验收和 Windows Service 跨账户验收。

## 2. 实际完成内容

### 2.1 领域契约

- `Device` 增加 `token_version` 和 `token_rotated_at`。
- 新增 `DeviceTokenRotateRequest`，只接受长度为 1 至 500 的轮换原因。
- `DeviceCredential` 和设备相关字段同步到 `packages/contracts/domain.schema.json`。

### 2.2 SQLite 开发 Store

- 新增 `Store.rotate_device_token()`。
- 使用 `BEGIN IMMEDIATE` 将管理员授权、设备 active 检查、Token 哈希替换、版本递增、活动连接
  撤销和轮换审计记录放在同一事务中。
- 设备 Token 仍只保存 SHA-256 哈希；新 Token 使用高熵随机值生成。
- 轮换记录写入 `device_token_rotations`，只包含设备、组织、操作者、原因、版本和时间。
- 已撤销设备不能再次轮换；无权限成员不能轮换其他组织设备。

### 2.3 PostgreSQL Repository

- 新增 `PostgresRepository.rotate_device_token()`。
- 使用 `SELECT ... FOR UPDATE` 锁定设备行，保持与 SQLite 相同的轮换语义。
- 新增增量迁移 `009_device_token_rotation.sql`，加入设备字段、审计表、索引和组织范围 RLS。
- 保留 `005_agent_devices.sql` 历史迁移不变，已执行过旧迁移的数据库通过 `009` 升级。

### 2.4 HTTP 接口

新增：

```text
POST /api/devices/{device_id}/rotate-token
```

请求体：

```json
{
  "reason": "credential rollover"
}
```

响应仍使用 `DeviceCredential`。调用者必须是设备所属组织的管理员；开发认证模式沿用现有
`member-001`，生产模式仍要求人类 Session。

## 3. 修改文件清单

- `apps/api/app/contracts.py`
- `apps/api/app/repository.py`
- `apps/api/app/store.py`
- `apps/api/app/postgres_repository.py`
- `apps/api/app/main.py`
- `apps/api/migrations/009_device_token_rotation.sql`
- `apps/api/test_devices.py`
- `apps/api/test_http_contracts.py`
- `apps/api/test_platform_contracts.py`
- `packages/contracts/domain.schema.json`
- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/AGENT_DEVICE_CONNECTION_DESIGN.md`

## 4. 数据库和协议变化

### SQLite

`devices` 增加：

- `token_version INTEGER NOT NULL DEFAULT 1`
- `token_rotated_at TEXT`

新增表 `device_token_rotations`：

- `id`
- `device_id`
- `organization_id`
- `rotated_by`
- `reason`
- `token_version`
- `created_at`

### PostgreSQL

通过 `apps/api/migrations/009_device_token_rotation.sql` 增加相同字段和审计表。审计表启用
`organization_id = app.current_organization_id()` 的 RLS 策略。

### 轮换后的生命周期

```text
active/token_version=N
    -> 管理员原子轮换
active/token_version=N+1
    -> 旧 Token 无效，旧 Gateway 连接 REVOKED
```

不提供旧 Token 的过渡有效期。新 Token 仅出现在当前 `DeviceCredential` 响应中，不进入事件、审计表、
本地状态库或普通日志。

## 5. 关键决策和理由

1. **先实现单 Token 原子替换。** 当前系统还没有完成 Service 身份和 Credential Manager ACL 验收，
   直接实现双 Token 会掩盖凭据读取身份问题；单 Token 更容易验证失效边界。
2. **轮换同时撤销现有连接。** 即使旧 Token 已经失效，已建立的连接仍可能拥有有效传输上下文；
   立即撤销连接可以避免旧会话继续发送 Gateway 消息。
3. **审计表不记录 Token。** 审计必须支持追责和恢复判断，但不能把秘密复制到数据库或项目事件。
4. **新增 009 增量迁移。** 不修改历史迁移文件，避免新旧部署因重放历史脚本而产生不可解释差异。
5. **服务端负责身份事实，本地负责凭据落盘。** 本轮接口只返回新凭据；Windows Credential Manager
   的自动写回必须等 Machine Service 与 User Session Worker 的身份边界完成实机验收后再实现。

## 6. 已运行测试和结果

使用工作区 Python 运行时，测试按串行顺序执行：

```text
apps/api：78 passed, 2 skipped
apps/agent：114 passed, 1 skipped
compileall apps/api/app apps/agent packages：passed
domain.schema.json、domain.json、gateway.schema.json、session.schema.json：JSON parse passed
```

新增测试覆盖：

- 新 Token 可解析，旧 Token 立即失效。
- Token 版本从 1 递增到 2。
- 活动 Gateway 连接在轮换后变为 `REVOKED`。
- 轮换审计记录不包含明文 Token。
- 非组织成员不能轮换；已撤销设备不能轮换。
- HTTP 轮换入口返回新的 `DeviceCredential`。
- 迁移目录契约包含 `009_device_token_rotation.sql`。

说明：并发启动 API 与 Agent 测试时，Windows ConPTY 和异步 Runtime 测试曾出现既有间歇性竞态；
三次串行 Agent 回归均通过，最终验收采用串行命令，不把并发环境下的噪声记为 P3-20 失败。

## 7. 没有完成的内容

- Agent 尚未在收到轮换响应后自动更新 Windows Credential Manager。
- 未实现双 Token 过渡窗口、优雅连接迁移和本地旧凭据删除确认。
- 当前开发机 `CRED_PERSIST_LOCAL_MACHINE` 写入实测返回 `1312`，长期跨登录会话持久化未验收。
- Machine Service、User Session Worker 和服务账户访问 Credential Manager 的 ACL/身份矩阵未验收。
- 设备私钥轮换、公钥替换和正式 OIDC/设备会话认证未完成。
- PostgreSQL 009 迁移、RLS、连接池、并发轮换和回滚尚未在真实数据库执行。

## 8. 已知问题和风险

- 轮换响应丢失时，服务端已经使旧 Token 失效；当前没有自动恢复或再次安全取回新 Token 的流程，
  需要管理员重新轮换或重新配对。
- 如果轮换后调用方尚未成功写入系统凭据，Agent 会暂时无法建立新 Gateway 连接；这是故意的
  fail-closed 行为，但需要后续提供人工恢复指引和可观测状态。
- 轮换撤销的是已有连接，不会撤销项目授权记录；项目 Token 的独立撤销和过期策略仍按原协议执行。
- SQLite `Store` 仍是开发实现；生产切换前必须验证 PostgreSQL 事务、RLS 和多实例并发。
- 当前测试没有覆盖真实 Windows Service、注销/重启、睡眠唤醒和多账户组合。

## 9. 下一轮建议任务

1. 启动真实 PostgreSQL/MinIO，执行 `001` 至 `009` 迁移并验证设备轮换 RLS、回滚和并发竞态。
2. 冻结 Machine Service 与 User Session Worker 的凭据读取身份，设计轮换响应的安全写回和丢失恢复流程。
3. 在目标 Windows 用户、LocalSystem/服务账户和用户配置文件组合下验收 Credential Manager ACL、
   注销/重启恢复和轮换后重新连接。
4. 将 Gateway 业务命令及其结果持久化纳入同事务/幂等协调，减少跨实例副作用窗口。
5. 完成正式 OIDC/设备会话认证后，再评估双 Token graceful rotation 是否必要。

## 10. 启动、迁移、复现和回滚注意事项

### 开发启动

SQLite 会在 `Store` 初始化时创建 P3-20 表和字段；无需手动迁移。API 仍可按现有开发方式启动。

### PostgreSQL 迁移

在配置 `psycopg` 和 DSN 后执行既有迁移入口：

```text
python -m app.migrations --dsn <POSTGRES_DSN>
```

确认 `009_device_token_rotation.sql` 已应用，并检查 `devices.token_version`、
`devices.token_rotated_at`、`device_token_rotations` 和 RLS policy。

### 复现轮换

1. 创建 pairing 并注册设备。
2. 打开一个 Agent Gateway connection。
3. 由组织管理员调用 `POST /api/devices/{device_id}/rotate-token`。
4. 验证旧 Token 返回 `device_token_invalid`，新 Token 可以认证，旧 connection 返回
   `device_connection_revoked`。
5. 将新 Token 写入已经通过 ACL 验收的凭据目标；当前不应假定开发机可以使用
   `CRED_PERSIST_LOCAL_MACHINE`。

### 回滚

代码回滚不能恢复旧 Token，因为轮换提交后旧 Token 按设计永久失效。若新版本存在问题，应修复或
再次执行轮换，而不是从日志中寻找旧 Token。数据库迁移不提供自动降级脚本；生产回滚前必须保留
`009` schema，旧代码只能在确认不会读取未知字段且不会覆盖新 Token 的情况下运行。

## 11. 交接状态

`PASS_WITH_ASSUMPTIONS`

开发版服务端轮换契约、SQLite/PostgreSQL 代码边界和测试已完成；生产凭据身份、真实 PostgreSQL、
自动写回和跨账户恢复仍是明确的后续验收条件。
