# 阶段 3 P3-21 真实基础设施验收准备交接

## 1. 本轮目标和任务 ID

- 任务 ID：`P3-21`
- 目标：把当前 PostgreSQL/MinIO 真实验收入口与已经实现的 `001` 至 `009` 迁移、P3-18 设备注册和 P3-20 Token 轮换事实对齐，并为真实服务可用后的快速回归准备测试。
- 交接状态：`PASS_WITH_ASSUMPTIONS`
- 本轮性质：验收准备完成；真实 PostgreSQL/MinIO 未启动，因此不宣称生产基础设施验收通过。

## 2. 实际完成内容

### 2.1 集成测试契约

更新 `apps/api/test_stage2_integration.py`：

- 从 `app.migrations.migration_files()` 动态读取迁移期望集合，不再硬编码到 `006`。
- 第一次执行迁移后再次执行，要求第二次返回空列表。
- 检查 `schema_migrations` 记录与当前 `001` 至 `009` 文件集合完全一致。
- 检查核心表、Outbox、Gateway 结果表和五张设备相关表的 RLS 开启状态。
- 检查 `devices.token_version`、`devices.token_rotated_at`、`device_pairings.challenge_hash` 和 `device_token_rotations` 字段。
- 增加真实 PostgreSQL 设备注册与轮换集成入口：创建隔离组织、成员、项目和 Agent，完成 Ed25519 challenge 注册，验证旧 Token 失效、活动连接撤销、轮换审计和版本递增。
- 使用两个独立 `PostgresRepository` 连接并发轮换同一设备，要求版本从 `2` 串行递增为 `3`、`4`。
- 测试清理使用唯一测试组织；若初始化或清理失败，应保留现场给人工排查。

### 2.2 文档同步

- `docs/POSTGRESQL_MIGRATIONS.md`：列出 `007`、`008`、`009`，补充真实验收命令和测试覆盖。
- `docs/POSTGRES_REPOSITORY.md`：补充设备/Gateway/Token 轮换覆盖，并说明真实并发和 RLS 仍待验证。
- `docs/STAGE2_EXIT_CHECKLIST.md`：迁移边界更新为 `001` 至 `009`，增加轮换事务验收前置条件。
- `docs/PROJECT_EXECUTION_PLAN.md`：版本更新为 `3.8`，记录 P3-21 范围、结果和未完成项。
- `docs/IMPLEMENTATION_STATUS.md`：记录 P3-21 和当前环境限制。

## 3. 修改文件清单

- `apps/api/test_stage2_integration.py`
- `docs/POSTGRESQL_MIGRATIONS.md`
- `docs/POSTGRES_REPOSITORY.md`
- `docs/STAGE2_EXIT_CHECKLIST.md`
- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/handoffs/P3_21_REAL_INFRASTRUCTURE_ACCEPTANCE_PREP_HANDOFF.md`

## 4. 已运行验证

使用 bundled Python：

```text
apps/api/test_stage2_integration.py：3 skipped（无 STAGE2_POSTGRES_DSN/STAGE2_MINIO_ENDPOINT）
apps/api 全量：78 passed, 2 skipped
apps/agent 全量：114 passed, 1 skipped
compileall apps/api/app apps/agent packages：passed
domain/gateway/session JSON Schema：passed
OpenAPI paths：57
```

环境探测结果：

```text
docker：未找到
podman：未找到
postgres/psql/initdb：未找到
minio：未找到
WSL：存在入口，但没有可用发行版
```

已提交 WSL Ubuntu 安装和 web-download 安装尝试；当前系统未完成发行版安装，web-download 申请未获授权。因此真实服务测试保持条件跳过。

## 5. 没有完成的内容

- 未执行真实 PostgreSQL `001` 至 `009` 迁移。
- 未执行真实 PostgreSQL RLS、连接池、事务回滚、跨租户越权和并发序号测试。
- 未执行真实 PostgreSQL 设备 Token 轮换测试。
- 未执行真实 MinIO 对象写入、跨实例 Multipart、失败恢复和清理测试。
- 未接入 NATS、真实消费者幂等或生产部署。
- 未修改默认 SQLite 运行时，也未将 PostgreSQL Repository 宣称为生产 Store。

## 6. 关键假设和风险

- 测试假设 PostgreSQL 用户能够执行迁移并看到当前 schema；真实部署应使用专用数据库用户和明确的 migration owner。
- RLS 开启检查不等于非表 owner 数据库角色下的拒绝行为验收；后续必须使用受限角色设置 `app.organization_id` 做跨租户读写测试。
- 并发版本测试只验证 Repository 的 `FOR UPDATE` 串行化和最终版本，不覆盖多实例 Gateway 业务副作用事务。
- 测试清理依赖组织外键级联；出现失败时不能强制删除现场。

## 7. 下一步建议

优先级顺序：

1. 在有 Docker/Podman 或已安装 PostgreSQL/MinIO 的验收环境执行 `scripts/test-stage2-integration.ps1`。
2. 若真实集成通过，再补受限 PostgreSQL 角色下的 RLS 越权快速测试。
3. 完成 P3-20 轮换后 Credential Manager 写回和丢响应恢复协议。
4. 将 Gateway 业务命令结果写入与副作用执行纳入同一事务/幂等协调。
5. 继续 P3-16 Windows Service/Session 0 实机、真实 Codex 任务和多账户验收。

## 8. 复现命令

```powershell
$env:STAGE2_PYTHON = "C:\\path\\to\\python.exe"
$env:STAGE2_POSTGRES_DSN = "postgresql://platform:password@localhost:5432/math_agent_platform"
$env:STAGE2_MINIO_ENDPOINT = "http://localhost:9000"
.\scripts\test-stage2-integration.ps1
```

未提供真实服务时，预期输出为 `3 skipped`；这表示验收入口可收集，不表示基础设施已完成。
