# 阶段 2 第五执行批次交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-13
>
> 对应任务：P2-05 事务 Event Outbox 与高风险写入原子性

## 1. 本轮目标

在 P2-04 的 PostgreSQL/HTTP 边界基础上，解决“业务状态已提交但事件未提交”的审计窗口，并为后续消息总线保留可靠的事实来源：

1. 为事件增加正式 `EventOutbox` 领域契约和数据库表。
2. 让高风险 SQLite 写入在同一事务中完成业务状态、事件和 outbox。
3. 提供 outbox 查询、成功标记、失败记录和重试时间接口。
4. 为旧数据迁移和 Bundle 恢复补建 outbox 记录。
5. 用回滚测试证明事务失败不会留下半套 Review/Artifact 数据。

## 2. 实际完成内容

### 2.1 领域契约

- `apps/api/app/contracts.py` 新增 `EventOutbox`。
- `packages/contracts/domain.schema.json` 新增 `EventOutbox` 定义并加入联合根对象。
- `packages/contracts/domain.json` 增加 `event_outbox_statuses`。
- 状态固定为 `PENDING`、`PROCESSING`、`DELIVERED`、`FAILED`。

### 2.2 SQLite 事务边界

- `Store._insert_event()` 负责在当前事务中同时插入 `events` 和 `event_outbox`。
- `add_event()` 保留原有公共接口，但现在一次提交事件和 outbox。
- 以下路径已收紧为业务状态、事件和 outbox 同一次数据库提交：
  - `create_artifact`
  - `store_artifact_content`
  - `complete_artifact_multipart`
  - `create_artifact_version`
  - `archive_artifact`
  - `create_evidence`
  - `create_review`，包含 Gate、目标状态和 Review
- `_ensure_gate()` 不再自行提交，避免 Review 事务被提前切断。
- 关键写入失败会执行 SQLite rollback；已用注入事件失败测试覆盖 Artifact 和 Review 两条路径。

### 2.3 Outbox 生命周期

- `list_event_outbox()` 支持按项目和状态查询。
- `list_pending_event_outbox()` 返回到达 `available_at` 的 `PENDING`/`FAILED` 记录，以及锁已过期的 `PROCESSING` 记录。
- `claim_event_outbox()` 以 SQLite `BEGIN IMMEDIATE` 和 PostgreSQL `FOR UPDATE SKIP LOCKED` 实现开发版原子领取，并写入锁过期时间。
- `mark_event_outbox_delivered()` 幂等，重复确认不会增加副作用。
- `mark_event_outbox_failed()` 记录截断后的错误信息，递增 `attempts`，清空锁定时间并设置重试时间。
- `apps/api/app/outbox.py` 提供与消息传输无关的 `EventOutboxDispatcher`，实现领取、发布、指数退避和成功确认。
- SQLite 启动时会为旧 `events` 补建缺失 outbox；Bundle 恢复导入的事件也会创建 `PENDING` outbox。

### 2.4 PostgreSQL 基线

- `001_initial.sql` 和 `002_rls.sql` 纳入完整 outbox 表及租户策略。
- 新增 `004_event_outbox.sql`，兼容已经应用 `001-003` 的数据库，包含表、索引、历史事件补建和幂等策略创建。
- `PostgresRepository._insert_event()` 在同一事务内插入 Event 和 Event Outbox。
- PostgreSQL Repository 增加 outbox 查询、成功标记和失败重试方法。
- PostgreSQL 业务对象的完整 Review/Artifact/Bundle 事务编排仍未完成，不能据此切换默认 API 运行时。

## 3. 修改文件

- `apps/api/app/contracts.py`
- `apps/api/app/repository.py`
- `apps/api/app/store.py`
- `apps/api/app/postgres_repository.py`
- `apps/api/app/outbox.py`
- `apps/api/migrations/001_initial.sql`
- `apps/api/migrations/002_rls.sql`
- `apps/api/migrations/004_event_outbox.sql`
- `apps/api/test_store.py`
- `apps/api/test_workflows.py`
- `apps/api/test_platform_contracts.py`
- `apps/api/test_postgres_repository.py`
- `packages/contracts/domain.schema.json`
- `packages/contracts/domain.json`
- `docs/EVENT_ENVELOPE.md`
- `docs/POSTGRES_REPOSITORY.md`
- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/STAGE2_EXIT_CHECKLIST.md`

## 4. 测试与验证

使用工作区 Python，并设置 `PYTHONPATH=apps/api;apps/api/vendor`：

```text
全量 unittest：46 passed, 2 skipped
compileall：通过
JSON Schema/contract JSON 解析：通过
API/OpenAPI 导入：通过，60 routes / 45 OpenAPI paths
```

跳过项仍为：

- 真实 MinIO 对象和跨实例 Multipart。
- 真实 PostgreSQL migration/RLS/Repository。

本轮新增的主要证明：

- 事件插入失败时，Artifact 不会残留，事件和 outbox 数量不增加。
- Review 事件插入失败时，Task 状态、Gate 和 Review 一起回滚。
- outbox 失败重试会增加 `attempts`，到期后重新进入待投递查询，成功标记可重复执行。

## 5. 没有完成的内容

- 没有启动真实 PostgreSQL/MinIO，也没有把 2 个条件集成测试从 skipped 变为 passed。
- 没有将 PostgreSQL Repository 扩展到完整 `PlatformRepository`。
- 没有把 PostgreSQL 的 Review、Artifact 内容、Multipart、Bundle 恢复业务编排全部收敛到同一事务方法。
- 没有接入 NATS JetStream 或真实消费者幂等；Dispatcher 目前是同步、传输无关的开发版实现。
- 没有完成 OIDC、Agent 设备 token、上传配额和恶意文件扫描。
- 对象存储写入仍是数据库外部副作用；数据库 rollback 不能自动撤销已完成的 S3/LocalObjectStore 写入，后续需要补偿删除或可恢复写入任务。

## 6. 下一步

1. 在可用环境安装 `apps/api/requirements-prod.txt`，启动 PostgreSQL/MinIO，执行 `scripts/test-stage2-integration.ps1`。
2. 在真实 PostgreSQL 中验证 `004_event_outbox.sql` 的历史事件补建、RLS 和并发序号。
3. 在真实 PostgreSQL 中验证 `FOR UPDATE SKIP LOCKED` 并发领取、锁过期和连接池行为。
4. 接入实际发布器和消费者幂等键，再进行故障注入、退避和恢复测试。
5. 扩展 PostgreSQL Repository 完整协议，并将 HTTP 运行时切换建立在统一 Repository 工厂之上。

## 7. 复现命令

```powershell
$env:PYTHONPATH = "$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s apps/api -p 'test_*.py' -v
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m compileall -q apps/api
```

不要把条件集成测试的 `skipped` 解读为生产基础设施已经验收。
