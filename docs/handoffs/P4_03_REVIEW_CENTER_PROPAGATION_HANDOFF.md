# P4-03 审核中心与 Gate 失效传播交接

> 任务编号：P4-03
>
> 完成日期：2026-09-14
>
> 状态：PASS_WITH_ASSUMPTIONS（开发版完成，生产验收延期）
>
> 上游：`P4_02_FANOUT_RISK_GATE_HANDOFF.md`
>
> 下一步：P4-04 真实 PostgreSQL/MinIO、RLS、并发和多实例验收；P4-05 浏览器端到端验收

## 1. 本轮目标

本轮把 P4-02 已完成的审核中心后端聚合能力接入前端，并补齐 Gate 失效后的显式下游影响传播。
重点是让用户能看见真实审核状态、逐接收方交接收据和风险责任链，同时避免上游证据或风险变化后旧的下游任务继续看起来可执行或已通过。

## 2. 已完成

### 2.1 审核中心数据契约

- `ReviewCenter` 增加 `handoffs` 字段，返回项目内 Handoff 及其 `receipts`。
- 前端 API 类型补齐 `Gate`、`Review`、`Evidence`、`RiskRegistryEntry`、`HandoffReceipt` 和审核中心响应。
- 新增 `getReviewCenter(projectId)` 和 `updateRisk(projectId, riskId, payload)`。
- 风险请求由前端生成新的幂等键，成功后重新读取 Dashboard 和 Review Center。

### 2.2 前端审核工作台

修改：

```text
apps/web/lib/api.ts
apps/web/components/dashboard-shell.tsx
apps/web/app/globals.css
```

前端现在：

- 根据真实 Gate 数据展示 `OPEN`、`PASSED`、`FAILED`、`BLOCKED`、`INVALIDATED`。
- 显示 Gate 的目标对象、最近 Review 摘要和失效原因。
- 显示未解决风险数量、严重级别、目标、责任人和关闭状态。
- 支持风险 `ASSIGN`、带项目内 Evidence 的 `RESOLVE`、`REOPEN`。
- 显示 Fanout Handoff 的整体收据状态和每个接收方的 `PENDING`、`ACCEPTED`、`REJECTED` 状态。
- 页面不再使用此前写死的“问题三/问题四”审核演示条目。

风险操作使用既有服务端门禁；前端没有直接修改 Gate 或 Review 状态的旁路。

### 2.3 Gate 失效影响图

修改：

```text
apps/api/app/store.py
apps/api/app/postgres_repository.py
```

SQLite 和 PostgreSQL Repository 均增加影响图遍历。遍历边包括：

```text
task -> dependency_task_ids
task -> task-owned handoffs
artifact -> input_artifact_ids / task.input_artifacts
artifact -> handoff input/output artifacts
handoff -> input_handoff_ids / task.input_handoff_ids
```

从失效 Gate 的目标开始，使用已访问集合防止循环引用，找到目标任务及其下游任务。

当 Gate 因输入快照变化而自动失效，或 fatal/major 风险被重新打开导致 Gate 阻断时：

- `READY`、`CLAIMED`、`RUNNING` 等未完成任务标记为 `BLOCKED`。
- `APPROVED`、`WAITING_REVIEW`、`NEEDS_REVISION` 任务标记为 `NEEDS_REVISION`。
- `FAILED`、`CANCELLED` 任务不被重新激活。
- 已通过的下游 Task Gate 标记为 `INVALIDATED`，清除 `approved_by` 和 `approved_at`。
- 写入 `task.upstream_gate_invalidated` 和下游 `gate.invalidated` 事件。
- 原 Gate 的失效事件仍保留，传播事件带有 `source_gate_id` 或原 Gate 标识。

这保证了下游领取路径继续只接受有效输入，同时把影响结果显式呈现在任务与审核中心中。

## 3. 关键语义说明

1. `INVALIDATED` 表示 Gate 曾经通过，但其目标、Evidence 或输入来源已发生变化；它不是新的 Review 结论。
2. Gate 失效不会自动批准新的 Review，也不会自动关闭风险；用户仍必须通过重新审核和项目门禁恢复流程。
3. 风险 `RESOLVE` 必须提交项目内 Evidence 和关闭理由；`REOPEN` 会重新阻断相关 Gate。
4. 传播只处理当前项目内引用，不能跨项目建立影响边。
5. 任务传播是服务端状态变化，不能依赖前端刷新才能成立；前端刷新只是显示已持久化结果。
6. 本轮保留 SQLite 开发运行时为默认实现，PostgreSQL 代码与 SQLite 语义同步但未连接真实数据库验收。

## 4. 验证结果

### 已通过

```text
P4-03 目标模块 py_compile                                  PASS
API unittest 全量                                           91 passed, 3 skipped
Next.js 15 production build                                 PASS
```

API 测试命令的当前环境适配方式：

```text
使用工作区 bundled Python；先加载 bundled _cffi_backend，
再将 apps/api/vendor 和 apps/api 放入 sys.path，执行 unittest discover。
```

### 明确未完成

- 真实 PostgreSQL 执行 `001` 至 `012` 迁移。
- PostgreSQL RLS、连接池、事务回滚、收据/风险/Gate 传播并发验收。
- 跨实例传播消费者、NATS JetStream 和 Gateway 多实例统一事务。
- 浏览器自动化、移动端响应式实机检查和真实 API 联调截图。
- 生产 OIDC、正式成员身份记账和设备会话认证。
- 传播策略的长链、循环图、并发 Gate 失效和恢复重放专项测试。
- 项目独立 Python 运行环境安装；本轮 pip 包源等待无响应后中止，未将其记录为成功安装。

## 5. 交接给下一轮的入口

1. 阅读 `docs/IMPLEMENTATION_STATUS.md`、`docs/PROJECT_EXECUTION_PLAN.md` 和本文件。
2. 启动 API 与 Web，创建或使用一个包含 Task、Artifact、Handoff、Review、Evidence 的开发项目。
3. 通过 `/api/projects/{project_id}/review-center` 检查 `handoffs` 和 `receipts` 是否完整返回。
4. 构造一个已通过的 Gate，修改其 Evidence 源或重新打开 fatal/major 风险，确认受影响任务和下游 Task Gate 状态变化。
5. 在真实 PostgreSQL 条件环境执行 P4-04；不要把本轮 SQLite 回归结果写成生产通过。
6. 进行浏览器端到端验收后，再更新阶段 4 开发版退出评审。

## 6. 变更文件

```text
apps/api/app/contracts.py
apps/api/app/main.py
apps/api/app/store.py
apps/api/app/postgres_repository.py
apps/web/lib/api.ts
apps/web/components/dashboard-shell.tsx
apps/web/app/globals.css
docs/IMPLEMENTATION_STATUS.md
docs/PROJECT_EXECUTION_PLAN.md
docs/handoffs/P4_03_REVIEW_CENTER_PROPAGATION_HANDOFF.md
```

## 7. 风险与回滚边界

- 传播会改变已批准下游 Task 的状态为 `NEEDS_REVISION`，这是为了避免旧结果在上游证据失效后继续作为正式结果；回滚前必须先确认是否已有新的人工 Review。
- 旧数据库由 SQLite `_ensure_columns()` 兼容读取，生产环境仍必须使用显式迁移，不应依赖该兼容逻辑。
- PostgreSQL 传播 SQL 尚未在真实数据库执行，不能在生产切换前跳过 P4-04。
