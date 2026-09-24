# PostgreSQL Repository 第一批说明

> 状态：开发版契约、P4-04 并发收紧和 P4-04-RUN-PROD 运行时角色/连接池硬化完成，真实数据库集成已在本机用户态 PostgreSQL 验收；生产形态（TLS、跨实例、密码轮换）待验收
>
> 对应交接：`docs/handoffs/P2_04_POSTGRES_HTTP_BOUNDARY_HANDOFF.md`

## 1. 目的

`apps/api/app/postgres_repository.py` 是平台从 SQLite 开发 Store 迁移到 PostgreSQL 的第一条生产数据库链路。它不复制整个 SQLite Store，也不隐式降级到 SQLite，而是明确提供已稳定对象的数据库查询和事务边界。

## 2. 初始化

安装生产依赖：

```powershell
pip install -r apps/api/requirements-prod.txt
```

先执行迁移：

```powershell
python -m app.migrations "$env:DATABASE_URL"
```

示例：

```python
from uuid import UUID

from app.postgres_repository import PostgresRepository

repository = PostgresRepository(
    dsn="postgresql://platform:password@localhost:5432/math_agent_platform",
    organization_id=UUID("00000000-0000-4000-8000-000000000001"),
)
try:
    projects = repository.list_projects()
finally:
    repository.close()
```

## 2.1 运行时角色与连接池（P4-04-RUN-PROD）

生产部署不应使用 schema owner 连接。迁移 `014_runtime_role_and_grants.sql` 创建集群级
`app_runtime` 角色（仅 DML、不可绕过 RLS、不可 DDL），密码由部署侧设置：

```powershell
# 设置 platform（管理员）与 app_runtime（运行时）密码并恢复 scram-sha-256
.\scripts\local-infra.ps1 provision
```

应用运行时使用非 owner DSN：

```python
repository = PostgresRepository(
    dsn="postgresql://app_runtime:<password>@db.internal:5432/math_agent_platform",
    organization_id=organization_id,
    min_size=2,
    max_size=20,
    timeout=5.0,          # 等待空闲连接的秒数
    max_idle=300.0,       # 空闲连接回收
    max_lifetime=3600.0,  # 连接最大寿命，避免长事务僵死
)
```

构造参数非法时抛出带稳定错误码的 `ValueError`：`postgres_pool_min_size_invalid`、
`postgres_pool_size_range_invalid`、`postgres_pool_timeout_invalid`、
`postgres_pool_max_idle_invalid`、`postgres_pool_max_lifetime_invalid`、
`postgres_pool_max_idle_exceeds_lifetime`。

连接池耗尽或后端不可用时，`transaction()` 抛出 `PostgresRepositoryError`，其 `code` 为
`postgres_pool_timeout` 或 `postgres_connection_unavailable`；调用方自身语句抛出的数据库异常
保持原类型不变。容量和等待指标可通过 `repository.pool_stats()` 读取（不含 DSN 等敏感值）。

## 3. 已覆盖的数据库切片

- 连接池创建和关闭。
- 每次操作的事务上下文。
- 有组织作用域时，在事务内设置 `app.organization_id`，供 PostgreSQL RLS 使用。
- Project、Task 的查询和创建。
- Artifact 的查询和创建，以及内容哈希去重边界。
- Run、Handoff、Review、Gate、Evidence 的列表/单项查询。
- Event 列表和带项目行锁的顺序写入；每条新事件在同一事务中创建 Event Outbox 记录。
- Event Outbox 按项目/状态查询、待投递查询、成功标记和失败重试状态写入。
- Git Repository 和 Git file index 查询。
- 带请求指纹的幂等记录读写。
- 设备配对、challenge/signature 注册、设备 Token 解析与轮换、项目能力授权、AgentConnection 和 Gateway 序号/心跳方法。
- Gateway 命令结果的连接范围持久化、ACK 丢失恢复查询和非秘密请求指纹。
- P4-04 项目级事务锁、冲突安全 Gate 创建、锁内 Gate 失效复检、事件序号串行和 keyed event 幂等检查。
- 组织范围幂等记录：Repository 使用组织命名空间后的 key，并写入 `organization_id`。

## 4. 尚未覆盖

以下内容仍由 SQLite Store 或后续阶段负责：

- 完整 `PlatformRepository` 协议的所有方法。
- PostgreSQL 版任务租约、领取、进度和结果回传。
- PostgreSQL 版对象存储 Multipart 生命周期。
- Review、Gate、Task、Artifact 的完整 PostgreSQL 事务编排（SQLite 高风险写入已先收紧）和 outbox 业务集成。
- Outbox Dispatcher 的并发领取/租约、实际消息发布和消费者幂等。
- 真实连接池、RLS、并发序号和跨租户越权测试；P4-04 已提供 `test_stage4_integration.py`，但当前环境仍未执行。
- 真实设备 Token 轮换的回滚、并发轮换竞态和撤销连接集成测试。
- 运行时按环境自动切换 Repository。

因此当前不能仅因为该类可以实例化，就把生产运行时切换到 PostgreSQL。完成上述差距并通过真实数据库验收后，才允许接入统一 Repository 工厂。`test_stage2_integration.py` 应覆盖历史阶段迁移和设备/Gateway 事实，`test_stage4_integration.py` 负责迁移 001 至 013、强制 RLS、工作流并发和 MinIO 条件验收；未配置真实 DSN 时这些测试必须保持 skipped。

## 5. RLS 使用规则

- `organization_id` 不能在生产调用中省略。
- 每个请求应从认证主体解析组织，再创建带组织作用域的 Repository 或事务上下文。
- 不允许从请求参数直接信任组织 ID。
- 迁移脚本 `002_rls.sql` 的策略只提供数据库兜底，应用层仍必须执行项目成员和角色授权。

## 6. 验收命令

开发环境可运行：

```powershell
$env:PYTHONPATH = "$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\path\to\python.exe -m unittest discover -s apps/api -p 'test_*.py' -v
```

真实环境需额外设置：

```powershell
$env:STAGE2_POSTGRES_DSN = "postgresql://..."
$env:STAGE2_MINIO_ENDPOINT = "http://localhost:9000"
.\scripts\test-stage2-integration.ps1
```

阶段 4 并发和隔离验收：

```powershell
$env:STAGE4_POSTGRES_DSN = "postgresql://..."
$env:STAGE4_MINIO_ENDPOINT = "http://localhost:9000"
.\scripts\test-stage4-integration.ps1
```

真实服务未提供时，迁移、RLS 和 MinIO 测试必须保持 skipped，不能改成无条件通过。
