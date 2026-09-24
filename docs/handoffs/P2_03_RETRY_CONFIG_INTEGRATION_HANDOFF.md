# 阶段 2 第三执行批次交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-12
>
> 对应任务：P2-03 Multipart 重试、S3 配置、生命周期清理和真实集成验收入口

## 1. 本轮目标

继续收紧阶段 2 的对象存储边界，使 Multipart 在完成请求重试、进程重启和缺块情况下具有明确语义；提供 Local/S3 配置工厂、过期上传清理和可执行的 MinIO/PostgreSQL 集成测试入口。

## 2. 已完成

- Multipart 数据库记录增加 `ACTIVE`、`COMPLETED`、`ABORTED` 生命周期和完成时间。
- 完成请求重复提交时返回原 Artifact，不重复写入对象或事件。
- 过期 Multipart 清理只处理 ACTIVE 记录，并调用 provider abort 后保留审计状态。
- S3 分块、完成和取消均可使用持久化 storage key，不依赖当前进程内映射。
- S3 `list_parts` 支持分页并按 PartNumber 排序。
- 增加 `create_object_store()`，通过 `OBJECT_STORE_BACKEND`、`S3_BUCKET`、`S3_ENDPOINT_URL` 和 `S3_REGION` 配置后端。
- 增加生产依赖入口 `apps/api/requirements-prod.txt`。
- 增加条件式真实 MinIO 对象/Multipart 测试和 PostgreSQL migration/RLS 测试。
- 增加 `scripts/test-stage2-integration.ps1`，自动选择可用 Python 并运行集成测试。
- 增加 Artifact version、archive API 和 Git commit 存在性校验。

## 3. 未完成

- 当前机器没有 Docker、MinIO、PostgreSQL、boto3 或 psycopg，因此真实服务测试本轮按条件跳过。
- PostgreSQL Repository 查询实现、连接池和 RLS 上下文注入仍未完成。
- 生产认证、项目授权、上传配额、异步生命周期任务和 Event outbox 不属于本批次完成内容。

## 4. 测试与结果

```text
常规 unittest：31 passed
真实 MinIO/PostgreSQL 条件测试：2 skipped（外部服务和生产依赖不可用）
compileall：通过
API/OpenAPI 导入：通过
```

本轮覆盖了 S3 fake-client 跨实例分块、Multipart 重试、缺块拒绝、过期清理、对象完整性、Bundle 校验、Artifact 版本和后端工厂。

## 5. 下一步

1. 在安装 Docker 和 `requirements-prod.txt` 后运行 `scripts/test-stage2-integration.ps1`。
2. 真实集成通过后，完成阶段 2 生产退出评审。
3. 若 PostgreSQL 仍作为阶段 1 的后续任务处理，则保留本交接的 `PASS_WITH_ASSUMPTIONS`，不得把迁移 SQL 当作 Repository 已完成。
