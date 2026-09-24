# 阶段 2 第四执行批次交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-13
>
> 对应任务：P2-04 PostgreSQL Repository 第一批、HTTP 幂等边界和项目授权边界

## 1. 本轮目标

在阶段 2 的对象存储、Git 和 Bundle 开发契约基础上，继续完成：

1. PostgreSQL Repository 第一批实际查询、连接池和事务边界。
2. 上传、Multipart、Git 和 Bundle 恢复接口的 HTTP `Idempotency-Key` 契约。
3. 项目及对象路径的 HTTP 授权入口和主体记账。
4. 用纯后端测试固定重复请求、请求变更、跨项目访问和 PostgreSQL 行映射行为。

## 2. 实际完成内容

### 2.1 PostgreSQL Repository

- 新增 `apps/api/app/postgres_repository.py`。
- 使用 `psycopg_pool.ConnectionPool`，生产依赖加入 `psycopg-pool`。
- 每个 Repository 操作通过事务上下文借用连接。
- 设置 `organization_id` 时使用事务级 `set_config`，配合 RLS。
- 完成 Project、Task、Artifact、Run、Handoff、Review、Gate、Evidence、Event 和 Git index 的第一批查询/写入。
- Event 写入在项目行上加锁后再生成项目内序号，避免简单 `MAX(sequence)+1` 的并发漏洞。
- 明确未实现的完整 Store 方法，没有把半成品作为默认运行时。

### 2.2 HTTP 幂等

- SQLite `idempotency_records` 增加 `request_hash`；PostgreSQL 初始迁移和 `003_idempotency_request_hash.sql` 同步。
- 相同幂等键和相同请求指纹会返回第一次响应。
- 相同幂等键复用于不同操作或不同请求时拒绝。
- 覆盖：单文件 Artifact 上传、Multipart 初始化/分块/完成/取消、Git 注册/索引、项目 Bundle 恢复。
- 文件请求指纹包含对象身份、内容 SHA-256、大小、MIME/期望哈希等关键字段。

### 2.3 HTTP 授权和主体记账

- 增加 `PLATFORM_AUTH_MODE=development|required`。
- Bearer Session 会解析到真实 `HumanMember`；生产模式没有凭证时返回 401。
- 项目路径和任务/交接/成果物/运行对象路径统一执行项目成员 RBAC。
- 项目列表改为按当前成员过滤。
- 项目创建、Artifact、Evidence、Review、导入和 Agent grant 的主体字段由会话成员覆盖，不信任请求体中的伪造主体。
- `project.view`、`project.write`、`project.admin`、`review.submit` 和 `project.export` 使用不同权限边界。

## 3. 修改文件

- `apps/api/app/store.py`
- `apps/api/app/repository.py`
- `apps/api/app/main.py`
- `apps/api/app/postgres_repository.py`
- `apps/api/requirements-prod.txt`
- `apps/api/migrations/001_initial.sql`
- `apps/api/migrations/003_idempotency_request_hash.sql`
- `apps/api/test_platform_contracts.py`
- `apps/api/test_stage2_integration.py`
- `apps/api/test_http_contracts.py`
- `apps/api/test_postgres_repository.py`
- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/POSTGRES_REPOSITORY.md`
- `docs/STAGE2_EXIT_CHECKLIST.md`

## 4. 测试与验证

使用 Codex 工作区 Python，并设置 `PYTHONPATH=apps/api;apps/api/vendor`：

```text
compileall：通过
API/OpenAPI import：通过，60 routes / 45 OpenAPI paths
全量 unittest：39 passed, 2 skipped
```

跳过项：

- 真实 MinIO 对象和跨实例 Multipart。
- 真实 PostgreSQL migration/RLS。

跳过原因是当前机器没有可用的 MinIO/PostgreSQL 服务和生产依赖。该结果只证明开发契约，不证明生产基础设施验收。

## 5. 没有完成的内容

- 没有安装或启动 MinIO、PostgreSQL。
- 没有完成真实 PostgreSQL Repository、连接池和 RLS 集成测试。
- 没有把 PostgreSQL Repository 接入默认 API 运行时。
- 没有完成 OIDC、Agent 长期设备密钥和项目范围 token。
- 没有完成上传配额、流式大小限制、病毒扫描和恢复任务队列。
- 没有把 Review/Artifact/Bundle 写入统一事务 outbox。
- 没有完成完整 `PlatformRepository` 的 PostgreSQL 实现。

## 6. 已知风险和注意事项

1. 当前开发认证默认使用 `member-001`，生产必须设置 `PLATFORM_AUTH_MODE=required` 并接入正式身份服务。
2. HTTP 幂等响应记录是在业务副作用之后保存的；如果保存响应本身失败，生产仍需用同一事务/outbox 或业务幂等约束进一步加固。
3. `idempotency_records` 目前是全局键表，请求指纹包含项目/对象身份以阻止跨对象复用；后续可增加组织作用域列。
4. PostgreSQL Repository 的事件序号已锁项目行，但尚未在真实并发数据库中验证死锁、重试和池耗尽行为。
5. Bundle 恢复在识别项目归属前仍是全局入口，生产需要在解析 manifest 后执行组织和项目恢复授权。

## 7. 下一轮建议

1. 安装 `requirements-prod.txt`，启动真实 PostgreSQL/MinIO，运行阶段 2 集成脚本。
2. 增加 PostgreSQL Repository 的租约、Artifact 内容和 Multipart 查询/写入，并补 RLS 越权测试。
3. 将 HTTP 幂等响应保存、Artifact 元数据更新和事件写入收敛到事务/outbox。
4. 完成 Bundle 恢复的项目归属授权、大小限制和异步执行边界。
5. 阶段 2 生产退出评审通过后，进入阶段 3 Agent Gateway 设备配对和离线恢复。

## 8. 复现与回滚

- 开发验证：按第 4 节设置 Python 路径后运行全量 unittest。
- 真实验证：先执行迁移，再设置 MinIO/PostgreSQL 环境变量，运行 `scripts/test-stage2-integration.ps1`。
- 回滚代码：撤销本批次的 `postgres_repository.py`、HTTP Header 接入和 `003` 迁移；已存在的 `request_hash` 列可保留，不影响旧幂等记录读取。
- 不要使用 `git reset --hard` 或删除用户未提交的工作树内容。
