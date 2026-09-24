# PostgreSQL 迁移边界

当前 API 默认仍使用 SQLite 开发存储。生产数据库的第一版结构已经以版本化 SQL 固化在：

- `apps/api/migrations/001_initial.sql`：组织、项目、任务、成果物、运行、审核、事件、事务 Event Outbox、租约和幂等记录。
- `apps/api/migrations/002_rls.sql`：基于 `app.organization_id` 会话变量的 Row-Level Security 基线。
- `apps/api/migrations/003_idempotency_request_hash.sql`：为幂等记录补充请求指纹。
- `apps/api/migrations/004_event_outbox.sql`：为已经应用旧迁移的数据库补建 Event Outbox、历史事件待投递记录和租户策略。
- `apps/api/migrations/005_agent_devices.sql`：设备、配对、项目能力授权和 AgentConnection，以及对应租户 RLS。
- `apps/api/migrations/006_gateway_command_results.sql`：Gateway 命令结果持久化、ACK 丢失恢复索引和连接租户 RLS。
- `apps/api/migrations/007_run_execution_profile.sql`：为历史 `runs` 增加结构化执行配置，并从旧网络策略迁移默认值。
- `apps/api/migrations/008_device_registration_challenge.sql`：为设备注册增加一次性 Ed25519 challenge 哈希字段。
- `apps/api/migrations/009_device_token_rotation.sql`：为设备 Token 增加版本/轮换时间、轮换审计表和租户 RLS。
- `apps/api/migrations/010_gateway_command_request_hash.sql`：为 Gateway 命令结果增加不含项目能力 Token 的请求指纹字段。
- `apps/api/migrations/011_workflow_review_relations.sql`：为 Handoff 增加汇总输入、返工修订和拒绝决定字段；为 Review/Gate 增加 Evidence、Review 关系和风险摘要字段。
- `apps/api/migrations/012_p4_02_receipts_risks_gate_snapshots.sql`：新增 Fanout 逐接收方收据、独立风险登记表、Gate 输入快照/失效字段及对应租户 RLS；为旧 Relay 交接提供收据回填。
- `apps/api/migrations/013_p4_04_force_rls_and_event_idempotency.sql`：对全部租户表启用 `FORCE ROW LEVEL SECURITY`，补齐 agents、sessions、idempotency_records 的租户策略，为幂等记录增加组织范围，并为事件幂等键增加项目级唯一索引。
- `apps/api/migrations/014_runtime_role_and_grants.sql`：建立集群级 `app_runtime` 运行时角色（`NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`），授予 schema USAGE、应用函数 EXECUTE、全表 DML、序列使用和后续对象默认权限，并撤回 schema public 的 CREATE。密码不写入迁移，由部署侧通过 `scripts/local-infra.ps1 provision` 设置；迁移对非超级用户迁移身份在 `insufficient_privilege` 时记录 NOTICE 而不中断。
- `apps/api/app/migrations.py`：只在显式执行时导入 `psycopg` 的迁移入口。

当前迁移集合为 `001_initial.sql` 至 `014_runtime_role_and_grants.sql`。集成测试会从迁移目录读取期望集合，执行两次迁移，并确认第二次没有新增迁移；这只证明迁移脚本在真实 PostgreSQL 上具备幂等入口，不替代生产备份、回滚和并发演练。

执行示例：

```powershell
$env:PYTHONPATH = "$(Resolve-Path '.');$(Resolve-Path 'apps/api')"
python -m app.migrations "postgresql://platform:password@localhost:5432/math_agent_platform"
# 然后设置运行时角色密码（临时 trust 窗口，随后恢复 scram-sha-256）
.\scripts\local-infra.ps1 provision
```

阶段 2/3 真实基础设施入口：

```powershell
$env:STAGE2_POSTGRES_DSN = "postgresql://platform:password@localhost:5432/math_agent_platform"
$env:STAGE2_MINIO_ENDPOINT = "http://localhost:9000"
.\scripts\test-stage2-integration.ps1
```

阶段 4 PostgreSQL/MinIO 并发和隔离验收入口：

```powershell
$env:STAGE4_POSTGRES_DSN = "postgresql://platform:password@localhost:5432/math_agent_platform"
$env:STAGE4_MINIO_ENDPOINT = "http://localhost:9000"
.\scripts\test-stage4-integration.ps1
```

P4-04 入口覆盖强制 RLS 和跨租户读取、Fanout 收据并发确认、风险更新幂等、Gate 输入变化传播、
Event Outbox 的 `SKIP LOCKED` 并发领取，以及 MinIO 跨实例 Multipart 和哈希完整性。所有测试使用唯一
测试租户，正常结束会清理；清理失败时保留现场供诊断。

PostgreSQL 条件集成测试会验证迁移集合和二次执行幂等性、核心 RLS 开启状态、P3-18/P3-20 新字段与轮换审计表，并在真实数据库中创建隔离测试租户，验证设备注册、Token 轮换的事务结果、旧 Token 失效、连接撤销、审计记录和两个并发轮换请求的版本串行化。测试结束会删除该测试租户；若初始化或清理失败，应保留数据库现场供人工检查，不能将失败解释为通过。

迁移 runner 会在 `schema_migrations` 中记录已执行文件，并在单个迁移事务中执行 SQL。应用层切换 PostgreSQL 前仍需完成完整 Repository 的 SQL 查询实现、连接池、业务事务边界、Event Outbox 并发领取、租户上下文注入和 RLS 集成测试；本文件不能被视为生产切换已完成。
