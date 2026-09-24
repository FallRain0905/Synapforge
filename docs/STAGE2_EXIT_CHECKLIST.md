# 阶段 2 退出清单

本清单对应 `PROJECT_EXECUTION_PLAN.md` 第 9 节，区分开发契约证据和生产基础设施证据。

| 要求 | 开发版证据 | 当前结论 |
| --- | --- | --- |
| S3 兼容对象存储 | `app/object_store.py` 的 `S3ObjectStore`、S3 fake-client 测试、生产配置工厂 | 开发完成；真实 MinIO 待验收 |
| Multipart/断点续传 | Local/S3 分块、持久化 key、缺块拒绝、完成重试和过期清理测试 | 通过 |
| SHA-256、大小、MIME | Artifact 写入、读取、导出和恢复的重新计算/校验 | 通过 |
| Artifact Version | 版本号、父版本、同哈希明确新版本测试 | 通过 |
| 批准版本不可变 | 人工 Review 后 immutable，覆盖写拒绝测试 | 通过 |
| 输入/输出/引用关系 | Artifact input IDs、Run input/output IDs、Handoff/Evidence 引用和恢复校验 | 通过 |
| Git provider/index | LocalGitProvider HEAD、commit 存在性、文件索引和 SHA-256 | 开发完成；远端同步待后续 |
| CUMCM 对象导入 | 导入路径、内容、对象 key、hash、策略元数据和回读测试 | 通过 |
| Bundle 导出 | manifest 1.1、对象文件、校验清单、Git/审核/事件关系 | 通过 |
| Bundle 恢复 | 干净 Store 恢复、UUID/引用/对象篡改拒绝和回滚测试 | 通过 |
| 存储生命周期 | DRAFT 可更新、APPROVED 不可变、ARCHIVED 幂等、ACTIVE Multipart 过期清理 | 通过 |
| HTTP 接口 | 上传、下载、Multipart、Git、版本、归档、导出和恢复路径进入 OpenAPI；写入接口使用 `Idempotency-Key` 和请求指纹 | 开发完成；正式身份/配额待验收 |
| HTTP 项目授权 | Bearer Session、项目/对象路径 RBAC、成员过滤和主体记账 | 开发完成；OIDC/Agent token 待验收 |
| PostgreSQL Repository | `postgres_repository.py` 第一批项目、任务、成果物、运行、交接、审核索引、设备/Gateway 读写和连接池事务边界；真实集成入口覆盖 P3-20 轮换 | 开发完成；完整协议和真实 DB/RLS 待验收 |
| PostgreSQL 迁移边界 | `001_initial.sql` 至 `010_gateway_command_request_hash.sql`、`app.migrations`、迁移集合/二次执行幂等性/新设备字段/RLS/Gateway 请求指纹集成契约 | 开发契约完成；真实 DB 待验收 |
| 事务 Event Outbox | SQLite 业务状态、事件和 outbox 同事务；旧事件补建、恢复补建、原子领取/锁过期、Dispatcher 成功/失败退避和回滚测试；PostgreSQL 事件写入同步 outbox | 开发完成；真实 DB、NATS/消费者和生产故障演练待验收 |

## 当前测试证据

```text
常规测试：以 `IMPLEMENTATION_STATUS.md` 的最新回归记录为准
MinIO/PostgreSQL 条件集成测试：2 skipped（本机没有外部服务和生产依赖）
Python compileall：通过
API/OpenAPI 导入：通过
```

## 生产退出前置条件

1. 安装 `apps/api/requirements-prod.txt`。
2. 提供真实 MinIO/S3 和 PostgreSQL 服务。
3. 设置 `STAGE2_MINIO_ENDPOINT`、`STAGE2_MINIO_BUCKET` 和 `STAGE2_POSTGRES_DSN`。
4. 执行 `scripts/test-stage2-integration.ps1`，两个条件测试必须从 skipped 变为 passed。
5. 在 PostgreSQL 中完成 `001` 至 `010` 迁移、二次执行幂等性、Repository、连接池、租户上下文、RLS 和 Event Outbox 集成验证；包含 P3-20 设备轮换事务与 P3-23 Gateway 请求指纹测试。
6. 在真实 PostgreSQL 中完成设备 Token 轮换回滚、并发轮换、连接撤销和跨租户拒绝验收。
7. 在真实 PostgreSQL 中完成 outbox 并发领取、锁过期、重试和消费者幂等验收后，才可宣称事件已可靠投递。

因此当前阶段 2 是“开发契约完成，生产集成待验收”，不能把条件跳过解释为生产完成。
