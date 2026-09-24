# P4-04-RUN-PROD 生产形态运行时角色与连接池交接文档

> 日期：2026-09-15
>
> 状态：`PASS`（本机用户态 PostgreSQL 实测通过；生产形态仍待 TLS/跨实例/密钥托管验收）
>
> 任务：把 P4-04-RUN 的开发形态验收推进到生产形态——独立非 owner 运行时角色、密码认证、连接池上限与故障演练

## 1. 本轮结论

本轮完成 P4-04-RUN-PROD 的三条交付：

```text
迁移 014 app_runtime 运行时角色（最小权限）
  -> local-infra.ps1 provision 切换 scram-sha-256 密码认证
  -> PostgresRepository 连接池硬化（校验/超时/稳定错误码/指标）
  -> 6 项运行时角色验收 + API 101 项 / Agent 145 项全量回归全绿
```

此前 P4-04-RUN 的验收以 schema owner（超级用户）身份连接，既不能证明 RLS 隔离，也不能证明权限边界。本轮以独立非 owner 角色 + 密码认证重新验收，补齐了这部分证据。

## 2. 已完成

### 2.1 运行时角色（`apps/api/migrations/014_runtime_role_and_grants.sql`）

- 创建集群级 `app_runtime`：`LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`。
- 通过 `ALTER ROLE` 再次断言安全属性，防止手工创建的超级用户角色被沿用。
- 授权：schema `public`/`app` 的 USAGE、`app` 架构函数的 EXECUTE、全部表的
  `SELECT/INSERT/UPDATE/DELETE`、全部序列的 `USAGE/SELECT`，以及后续对象的默认权限。
- 撤回：schema `public` 的 `CREATE`（运行时角色不得建表）。
- **密码不写入迁移**；由部署侧设置。
- 迁移对非超级用户迁移身份容错：`CREATE ROLE`/`ALTER ROLE`/`GRANT`/`REVOKE` 遇到
  `insufficient_privilege` 时记录 NOTICE 并继续，因此租户级隔离数据库（例如验收用的
  一次性 owner 数据库）也能应用迁移而不中断。

### 2.2 密码认证（`scripts/local-infra.ps1 provision`）

- 在 `pg_hba.conf` 中临时插入 127.0.0.1 的 `trust` 行（必须位于 scram 规则之前，first match wins），
  `pg_ctl reload` 后设置 `platform` 与 `app_runtime` 密码，再移除信任行并重新加载，恢复
  scram-sha-256。
- 实测输出：`platform password login: True`、`app_runtime password login: True`。
- psql 调用统一使用 `-w`，避免缺少密码时交互式提示把脚本挂死。

### 2.3 最小权限实测

```text
(rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin) = (False, False, False, False, True)
CREATE TABLE                 -> InsufficientPrivilege: permission denied for schema public
SELECT * FROM pg_authid      -> InsufficientPrivilege
CREATE ROLE                  -> InsufficientPrivilege
未设置 app.organization_id 时 -> projects / organizations 可见 0 行
```

### 2.4 连接池硬化（`apps/api/app/postgres_repository.py`）

- `PostgresRepositoryError` 携带稳定 `code`：`postgres_pool_timeout`、`postgres_connection_unavailable`。
- 构造参数校验（稳定 `ValueError` 码）：`postgres_dsn_required`、`postgres_pool_min_size_invalid`、
  `postgres_pool_size_range_invalid`、`postgres_pool_timeout_invalid`、`postgres_pool_max_idle_invalid`、
  `postgres_pool_max_lifetime_invalid`、`postgres_pool_max_idle_exceeds_lifetime`。
- 新增 `timeout` / `max_idle` / `max_lifetime` 参数并透传给 `ConnectionPool`。
- `pool_stats()` 暴露容量与等待指标（不含 DSN）；兼容 psycopg_pool 返回 dict 或命名元组的差异。
- `transaction()` 只翻译**取连接**阶段的池/连接错误；调用方语句自身的数据库异常保持原类型，
  现有调用方语义不变。连接归还统一走 `context.__exit__(*sys.exc_info())`。

### 2.5 验收测试（`apps/api/test_postgres_runtime_role.py`，由 `STAGE4_RUNTIME_DSN` 控制）

```text
test_runtime_role_is_least_privilege_and_cannot_bypass_rls   ok
test_runtime_role_sees_no_rows_without_organization_scope    ok
test_runtime_role_isolates_tenants_under_rls                 ok
test_runtime_role_completes_workflow_through_repository      ok
test_pool_exhaustion_returns_stable_timeout_code             ok
test_lost_backend_surfaces_as_unavailable_and_recovers       ok
Ran 6 tests / OK
```

覆盖：密码登录身份断言、DDL/pg_authid/CREATE ROLE 拒绝、无作用域不可见、跨租户隐藏、
以运行时角色完成 Task/Handoff/收据/outbox 业务流、**同一连接池**耗尽返回
`postgres_pool_timeout` 且 `requests_errors >= 1`、后端被 `pg_terminate_backend` 后返回稳定错误并
自动恢复（期间池会打印 `discarding closed connection` 属预期行为）。

### 2.6 迁移 014 引入的回归修复

- `test_platform_contracts.test_postgres_migrations_cover_core_tables_and_rls`：期望迁移列表补充
  `014_runtime_role_and_grants.sql`。
- `test_stage4_integration.test_rls_hides_other_tenants_from_table_owner_connection`：
  - 一次性 owner 角色改为**带密码**创建并以密码连接（pg_hba 现强制 scram-sha-256）；
  - **DDL 中不使用绑定参数**：PostgreSQL 不接受 `CREATE ROLE ... PASSWORD $1`，改为字面量插值
    （密码由 `uuid4().hex` 本地生成，含不含引号）；
  - `DROP DATABASE`/`DROP ROLE` 改为仅在对象存在时执行，避免清理错误掩盖 try 块内的真实失败；
  - fixture 改为返回 `(organization_id, project_id)`，去掉跨用例累积的类级列表。

## 3. 验证

```text
API 全量回归（含 STAGE2/STAGE4 集成与 STAGE4_RUNTIME_DSN） -> 101 passed, 0 skipped
Agent 全量回归                                            -> 145 passed, 9 skipped
运行时角色验收                                            -> 6 passed
阶段 2 集成（真实 PG + MinIO）                            -> 3 passed
阶段 4 集成（真实 PG + MinIO）                            -> 4 passed
```

运行命令：

```powershell
$env:STAGE2_POSTGRES_DSN = "postgresql://platform:<pw>@127.0.0.1:54329/postgres"
$env:STAGE2_MINIO_ENDPOINT = "http://127.0.0.1:9100"
$env:STAGE4_POSTGRES_DSN = $env:STAGE2_POSTGRES_DSN
$env:STAGE4_MINIO_ENDPOINT = $env:STAGE2_MINIO_ENDPOINT
$env:STAGE4_RUNTIME_DSN = "postgresql://app_runtime:<pw>@127.0.0.1:54329/postgres"
$env:PYTHONPATH = "<repo>\apps\api"
python -X utf8 -m unittest discover -s apps/api -p 'test_*.py'
```

## 4. 未完成与已知风险

- 未验证 TLS/反向代理、跨实例部署、连接池上限压测和多实例并发下的池行为。
- 运行时角色密码为部署期明文环境默认值，尚无轮换流程与系统密钥托管；`provision` 只做本机开发引导。
- 未做备份/回滚演练；迁移 014 在已应用的数据库中不会重跑，脚本增强只对新库生效。
- `apps/api/vendor` 仍有约 41 个沙箱历史会话造成的不可读条目；当前 API 测试通过
  `PYTHONPATH=apps/api`（不含 vendor）从 site-packages 导入，需后续重建该目录。
- 真实 ETW 管理员权限采集仍被 UAC 阻塞（需交互式提升会话）。

## 5. 关键文件

- 迁移：`apps/api/migrations/014_runtime_role_and_grants.sql`
- 仓储：`apps/api/app/postgres_repository.py`
- 验收：`apps/api/test_postgres_runtime_role.py`、`apps/api/test_stage4_integration.py`、`apps/api/test_platform_contracts.py`
- 基础设施：`scripts/local-infra.ps1`（`start` / `provision` / `status` / `stop`）
- 文档：`docs/POSTGRES_REPOSITORY.md`、`docs/POSTGRESQL_MIGRATIONS.md`、`docs/IMPLEMENTATION_STATUS.md`、`docs/PROJECT_EXECUTION_PLAN.md`（3.27）
- 前置交接：`docs/handoffs/P5_06_REAL_ADMIN_AND_P4_04_RUN_HANDOFF.md`

## 6. 接收方行动

1. 生产部署必须使用 `app_runtime`（或同等最小权限角色）连接，禁止使用 schema owner。
2. 设置角色密码后确认 `pg_hba.conf` 已恢复 scram-sha-256；`provision` 会自行恢复，但人工改动后需复核。
3. 修改 `transaction()` 的错误翻译时，保持"只翻译取连接阶段"的边界，避免改变业务异常类型。
4. 后续推进生产收尾：TLS、跨实例、池上限压测、密码轮换与密钥托管、备份回滚演练。
