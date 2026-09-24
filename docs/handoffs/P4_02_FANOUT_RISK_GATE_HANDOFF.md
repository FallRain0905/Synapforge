# P4-02 Fanout、风险与 Gate 快照开发交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务编号：`P4-02`
>
> 完成日期：2026-09-14
>
> 下一主线：阶段 4 `P4-03` 审核中心前端、下游失效传播和真实 PostgreSQL/RLS 验收

## 1. 本轮目标

在 P4-01 已有的单一 Handoff 收据、Review 风险派生和 Gate 关系基础上，完成开发版的三项关键闭环：

1. `FANOUT` 交接拆分为逐接收方独立收据，并让汇总/下游流程等待所有必需接收方。
2. 将风险登记从 Review 文本中独立出来，提供责任人、关闭证据、关闭/重新打开操作。
3. 为 Gate 保存目标、Review、Evidence 和输入成果物快照，输入变化后自动失效。

同时补上 HTTP Agent 交接接收/拒绝的开发版项目能力认证，并同步 SQLite、PostgreSQL、JSON Schema、Bundle 和文档。

## 2. 已完成

### 2.1 Fanout 收据

新增 `HandoffReceipt` 领域对象和 `handoff_receipts` 表。每个 Fanout 接收方有独立记录：

```text
PENDING -> ACCEPTED
PENDING -> REJECTED
```

Handoff 原有 `receipt_status` 保留为兼容字段，并由收据聚合得到：

- 任一收据 `REJECTED`，整体为 `REJECTED`，Handoff 进入 `NEEDS_REVISION`。
- 尚有收据未决定，整体为 `PENDING`。
- 全部收据 `ACCEPTED`，整体为 `ACCEPTED`。

因此单个接收方接受不会提前放行下游。Gateway 和 HTTP Agent 接收/拒绝均依据服务端收据匹配当前 Agent，不接受载荷伪造的接收主体。

兼容处理：

- SQLite 启动时为没有收据的旧 Handoff 进行兼容回填。
- PostgreSQL 迁移 012 为旧 Relay Handoff 提供回填；新 Fanout 在 Repository 事务内展开收据。
- Bundle 导出保存 `Handoff.receipts`，恢复时恢复收据行；旧 Bundle 没有该字段时可由启动回填补齐。

### 2.2 独立风险登记

新增 `risks` 表和 `RiskRegistryEntry`。Review 创建时将 findings 固化为独立风险记录，Review 文本不再是风险关闭事实。

新增操作：

```text
PATCH /api/projects/{project_id}/risks/{risk_id}
action = ASSIGN | RESOLVE | REOPEN
```

规则：

- `ASSIGN` 必须提供责任人。
- `RESOLVE` 必须提供关闭理由和项目内 Evidence ID，并保存关闭人、关闭时间和关闭证据。
- `REOPEN` 清除关闭信息；fatal/major 风险重新打开时，已通过的 Gate 标记为 `BLOCKED`。
- Review finding 不允许通过 `resolved=true` 直接关闭；必须调用风险操作。
- 存在未解决 fatal/major 风险时，即使提交一条没有 finding 的新 Review，也不能 `APPROVED`。

### 2.3 Gate 输入快照

Gate 新增：

```text
input_snapshot
invalidated_at
invalidation_reason
```

快照包含：

- Gate target 的状态/版本/内容哈希。
- target 关联的输入成果物、输入交接和依赖信息。
- Gate 引用的 Review 指纹。
- Gate 引用的 Evidence 及其 Artifact/Run 源指纹。

已通过 Gate 被读取或列出时会检查快照；发现变化后转为 `INVALIDATED`，清空批准主体和时间，并产生 `gate.invalidated` 系统事件。下游判断只接受 `PASSED`，所以失效 Gate 不会继续放行。

### 2.4 PostgreSQL 与契约同步

新增：

```text
apps/api/migrations/012_p4_02_receipts_risks_gate_snapshots.sql
```

迁移包含收据表、风险表、Gate 快照列、旧 Relay 收据回填和两张新表的租户 RLS 策略。PostgreSQL Repository 已同步收据创建/确认/拒绝、风险登记/更新、Gate 快照读取和失效逻辑。

## 3. 主要修改文件

- `apps/api/app/contracts.py`
- `apps/api/app/store.py`
- `apps/api/app/postgres_repository.py`
- `apps/api/app/repository.py`
- `apps/api/app/main.py`
- `apps/api/app/gateway.py`
- `packages/contracts/domain.schema.json`
- `packages/contracts/domain.json`
- `apps/api/migrations/012_p4_02_receipts_risks_gate_snapshots.sql`
- `apps/api/test_workflows.py`
- `apps/api/test_platform_contracts.py`
- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/POSTGRESQL_MIGRATIONS.md`

## 4. 已执行验证

本轮执行：

```text
API 全量：90 passed，3 个真实 PostgreSQL/MinIO 条件测试 skipped
工作流专项：13 passed
PostgreSQL Repository 映射专项：4 passed
Gateway 专项：15 passed
Bundle 导出恢复专项：5 passed
Schema/OpenAPI/迁移契约：通过
目标 Python 模块 py_compile：通过
```

新增工作流测试覆盖：

- Fanout 两个接收方分别确认，单方确认时下游仍等待。
- 风险 ASSIGN、带 Evidence 的 RESOLVE、重新审核和只改 Review 不能清风险。
- Evidence 源内容变化后 Gate 自动 `INVALIDATED`。

## 5. 尚未完成

以下事项明确保留到后续统一测试和生产化轮次：

1. 真实 PostgreSQL 执行迁移 001 至 012、RLS、收据并发确认、风险并发更新和事务回滚。
2. PostgreSQL Repository 尚未覆盖完整 `PlatformRepository`，Gate 快照失效尚未做跨实例长时间验证。
3. HTTP Agent 路由已使用设备项目能力 Token，但正式 OIDC、设备会话、Token 生命周期和生产密钥托管未完成。
4. Gateway 命令结果与业务副作用仍未实现跨实例统一事务；Fanout 重试/租约/重复投递的生产竞争测试未完成。
5. 风险关闭后不会自动生成新的 Review 或自动把 Gate 改为 `PASSED`，必须重新提交审核，这是当前有意保留的人工门禁。
6. Gate 失效目前主要在读取、领取和汇总检查路径触发；尚未建立独立的全局失效消费者和向所有下游任务的批量传播作业。
7. 前端审核中心尚未接入新的 receipts、risks、input_snapshot 和 `INVALIDATED` 状态。
8. 真实 MinIO、NATS、Temporal、OIDC、TLS、Windows Service、Docker/Podman Runner 和大规模恢复演练仍未完成。

## 6. 下一步交接动作

下一轮进入 `P4-03`：

1. 将审核中心前端改为读取 Review Center API，展示逐接收方收据、风险责任人/关闭证据和 Gate 失效原因。
2. 把 Gate 失效结果传播到受影响下游 Task，形成明确的 `NEEDS_REVISION` 或重新审核待办。
3. 在真实 PostgreSQL/MinIO 条件环境执行 001 至 012 迁移和 RLS/并发测试。
4. 设计 Gateway 业务命令执行租约，使命令副作用和命令结果具备跨实例一致性。

下一轮开始仍需先阅读：

- `docs/IMPLEMENTATION_STATUS.md`
- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/AGENT_DEVICE_CONNECTION_DESIGN.md`
- 本交接文档

