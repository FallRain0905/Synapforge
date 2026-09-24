# P4-04 真实基础设施验收准备交接

> 日期：2026-09-14
>
> 状态：PASS_WITH_ASSUMPTIONS
>
> 本轮性质：完成 PostgreSQL/MinIO 真实验收入口和数据库并发边界修复；未宣称真实基础设施通过
>
> 下一入口：`P4-04-RUN`，随后进入 `P4-05` 浏览器端到端验收

## 1. 交接目的

本轮承接 P4-03 的审核中心和 Gate 影响图传播，实现阶段 4 真实基础设施验收前的数据库正确性收紧，并建立一套独立、可重复执行的 PostgreSQL/MinIO 条件集成入口。

本轮的判断原则是：

- SQLite 回归只能证明开发存储行为，不替代 PostgreSQL/RLS 验收。
- 迁移脚本静态检查只能证明入口存在，不替代真实数据库执行。
- 没有 PostgreSQL/MinIO 时保留 skipped，不将环境缺失解释为通过。

## 2. 已完成

### 2.1 RLS 和租户隔离

新增 `apps/api/migrations/013_p4_04_force_rls_and_event_idempotency.sql`：

- 对全部租户表执行 `FORCE ROW LEVEL SECURITY`。
- 补齐旧迁移遗漏的 agents 和 sessions 租户策略。
- 为 `idempotency_records` 增加 `organization_id` 和租户策略。
- 旧的无组织范围幂等记录保留为 `NULL`，在强制 RLS 下不可被组织作用域 Repository 读取；不能自动猜测其所属租户。
- 增加事件 `(project_id, idempotency_key)` 的部分唯一索引，防止同一项目重复写入 keyed event。

特别注意：PostgreSQL 表所有者默认可以绕过普通 RLS。此前只有 `ENABLE ROW LEVEL SECURITY`，如果开发数据库使用表所有者运行 API，隔离测试不充分。本轮用 `FORCE` 把该边界变成可验收行为。未来若切换到专用非 owner 运行角色，仍应保留并重新验证该约束。

### 2.2 PostgreSQL Repository 并发语义

修改 `apps/api/app/postgres_repository.py`：

- 新增项目行锁边界，项目内的 Handoff、Risk、Review、Gate 和 Event 副作用统一按项目串行。
- Handoff 创建、Risk 更新和 Review 创建在幂等查询前锁定项目，避免两个实例同时看到幂等记录不存在。
- Handoff 接收/拒绝先确定项目并锁项目，再锁 Handoff 和对应 Receipt，避免与 Review 事务形成反向锁顺序。
- Gate 使用 `INSERT ... ON CONFLICT DO NOTHING` 后重新读取并加行锁，避免并发创建同一 Gate 时唯一约束竞态。
- Gate 失效刷新重新读取 `FOR UPDATE` 行并复核当前状态，避免多个实例重复失效同一 Gate。
- Event 写入自身锁项目，并在 keyed event 写入前检查已有事件；事件序号的 `MAX(sequence)+1` 只在项目锁内使用。
- PostgreSQL 幂等记录使用组织命名空间后的 key，并写入 `organization_id`，避免全局主键导致跨租户冲突。

### 2.3 真实验收入口

新增：

- `apps/api/test_stage4_integration.py`
- `scripts/test-stage4-integration.ps1`

PostgreSQL 测试覆盖：

1. 迁移集合执行两次，第二次无新增迁移。
2. 全部租户表存在且启用 `relforcerowsecurity`。
3. 表所有者连接切换组织上下文后只能读取当前组织项目。
4. Fanout 两个接收方并发确认，最终两个 Receipt 均为 `ACCEPTED`，整体 Handoff 才完成。
5. 两个实例使用同一幂等键并发更新 Risk，只产生一个有效业务事件。
6. 根 Task 输入改变后，Gate 在锁内失效，依赖 Task 和其已通过 Gate 被传播为返工/失效。
7. 两个实例并发领取 Event Outbox 时，`FOR UPDATE SKIP LOCKED` 不返回重复 Outbox。

MinIO 测试覆盖：

- 对象 SHA-256 和元数据。
- 一个实例初始化 Multipart，另一个实例按乱序分块上传并完成。

执行：

```powershell
$env:STAGE4_POSTGRES_DSN = "postgresql://platform:password@localhost:5432/math_agent_platform"
$env:STAGE4_MINIO_ENDPOINT = "http://localhost:9000"
.\scripts\test-stage4-integration.ps1
```

脚本会优先使用 `STAGE4_PYTHON`、项目 `.venv` 或 Codex bundled Python，并在 Windows 下先加载 `_cffi_backend` 再加入 vendor 路径，规避当前 bundled runtime 的 DLL 冲突。

## 3. 验证结果

### 3.1 已执行并通过

```text
API unittest: 95 passed, 7 skipped
P4-04 integration entry: 4 skipped because PostgreSQL/MinIO were not configured
Python py_compile: passed
```

API 的 7 个 skipped 包含既有真实基础设施条件测试和本轮新增的 4 个阶段 4 条件测试。当前机器检查结果：

```text
docker: missing
podman: missing
psql: missing
PostgreSQL service: not available for this workspace
MinIO service: not available for this workspace
```

因此本轮没有取得真实 PostgreSQL、RLS、连接池、事务回滚或 MinIO 结果。

### 3.2 尚未完成

- 尚未在真实 PostgreSQL 执行迁移 001 至 013。
- 尚未验证表 owner、专用 API role 和 `BYPASSRLS` 角色差异。
- 尚未完成真实连接池下的跨实例 Handoff/Review/Risk/Gate 事务回滚演练。
- 尚未完成真实设备注册、Token 轮换、旧 Token 失效和 Gateway 序号并发验收。
- 尚未接入 NATS，也未验证 Outbox 消费者幂等和消息重投。
- 尚未完成真实 MinIO 凭据、分块失败恢复、过期上传清理和对象权限验收。
- 默认 API 运行时仍为 SQLite，PostgreSQL Repository 尚未覆盖完整 `PlatformRepository` 协议。
- 尚未进行浏览器自动化、审核中心视觉回归和真实 API 联调。

## 4. 下一轮执行顺序

### P4-04-RUN：真实服务执行

1. 准备 PostgreSQL 16 和 MinIO，或使用等价兼容服务；确认 API 连接用户不是超级用户，记录 role 的 `rolsuper`、`rolbypassrls` 和表 owner 状态。
2. 安装 `apps/api/requirements-prod.txt`，设置 `STAGE4_POSTGRES_DSN`、`STAGE4_MINIO_ENDPOINT` 和测试凭据。
3. 运行 `scripts/test-stage4-integration.ps1`。
4. 运行 `scripts/test-stage2-integration.ps1`，确认 P3-21 的设备 Token/RLS/迁移验收未被 013 破坏。
5. 若失败，保留测试租户和数据库日志，先区分迁移失败、RLS 拒绝、连接池死锁、事务冲突、对象存储错误和测试清理失败。
6. 只有 PostgreSQL/MinIO 条件测试全部通过，才可以把 P4-04 标为真实基础设施通过。

### P4-05：浏览器和开发版退出

P4-04-RUN 通过后再执行：

- 启动 API 和 Web，使用真实 review-center 数据完成审核中心浏览器验收。
- 验证 Fanout 收据、风险分配/关闭/重新打开、Gate 失效状态和时间线展示。
- 进行浏览器端错误态、刷新、重复提交和权限拒绝测试。
- 更新阶段 4 开发版退出评审；不能因为 P4-04 只通过 SQLite 就提前退出。

## 5. 回滚和注意事项

- 若迁移 013 在已有数据库因历史重复 keyed event 创建唯一索引失败，应先导出并审查重复事件，不能直接删除审计记录；修复策略必须另开迁移并留下决策记录。
- 若已有旧幂等记录需要恢复，必须由人工确认所属组织后回填 `organization_id`，不能用默认组织批量回填。
- 项目级锁当前是正确性优先的开发/验收实现，可能降低同一项目高并发吞吐；在引入更细粒度锁或事件序列表前，不要移除该锁。
- SQLite Store 未同步组织范围幂等字段，因为 SQLite 仍是开发后端；跨后端行为差异必须继续通过统一契约测试记录。
- 迁移已经写入 schema 版本表后，不要手工删除版本记录来“重跑”生产迁移；应使用新的前向迁移。

## 6. 交接结论

本轮完成了 P4-04 的代码准备、关键并发竞态修复、强制 RLS 迁移和真实基础设施测试入口。开发版回归通过，但真实 PostgreSQL/MinIO 尚不可用，因此交接状态为 `PASS_WITH_ASSUMPTIONS`。下一位执行者应从 `P4-04-RUN` 开始，先取得真实数据库和对象存储结果，再进入 P4-05 浏览器验收。
