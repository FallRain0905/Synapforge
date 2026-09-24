# 阶段 2 第二执行批次交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-12
>
> 对应任务：P2-02 完整性校验、Multipart 生命周期、版本边界和阶段 2 开发退出评审

## 1. 本轮目标

针对 P2-01 之后的真实协议差距，补齐对象读取/导出完整性校验、S3 Multipart 跨实例续传、缺块拒绝、显式版本语义、导入哈希一致性、过期上传清理和对应 HTTP 入口。

## 2. 已完成

- `get_artifact_content()` 读取时重新计算 SHA-256 和大小，底层对象损坏会被拒绝。
- Bundle 导出前校验所有对象，并写入 `object_checksums` 独立清单；恢复时同时校验 Artifact 元数据和清单。
- Local Multipart 要求 part number 从 1 连续到最大编号，缺块不会被静默拼接。
- S3 Multipart 的分块、完成和取消均支持调用方传入持久化 storage key；新 S3 adapter 实例可以继续已有上传。
- Artifact 内容哈希去重与显式 Artifact version 解耦，相同内容的明确新版本仍生成新版本号和父版本关系。
- CUMCM 导入使用扫描时的 hash 作为写入期 `expected_hash`，文件扫描后发生变化会留下警告而不伪造完整对象。
- 增加过期 Multipart 清理方法 `cleanup_stale_artifact_multipart_uploads()`。
- 增加 Artifact version、archive、上传下载和 Git 校验相关 API/契约。
- 新增 S3 fake-client、缺块、版本、清理和损坏对象测试。

## 3. 未完成

- 未连接真实 MinIO/S3 服务，尚未完成网络、凭证、分页和故障恢复集成测试。
- PostgreSQL Repository、连接池、事务上下文和 RLS 集成仍未完成。
- HTTP 层仍未接入 OIDC/RBAC、项目授权和上传配额。
- Event outbox、异步清理任务和对象存储生命周期策略尚未部署化。

## 4. 测试与结果

```powershell
$env:PYTHONPATH = "$(Resolve-Path 'apps\\api');$(Resolve-Path 'apps\\api\\vendor')"
& 'C:\Users\19855\Documents\ChatGPT\数学建模\C题工作区\.venv\Scripts\python.exe' -m unittest discover -s apps\\api -p 'test_*.py' -v
```

结果：28 项常规测试通过；集成测试在未配置 MinIO/PostgreSQL 时按条件跳过；`compileall`、API 导入和 OpenAPI 生成通过。

本轮新增/加强测试：

- 底层对象损坏检测和 Bundle 导出阻断。
- Local Multipart 缺块拒绝和过期清理。
- 同哈希显式新版本不会被去重吞掉。
- S3 hash metadata、分块排序和跨实例继续上传。
- OpenAPI 阶段 2 路径和 PostgreSQL 迁移/RLS 静态契约。

## 5. 阶段 2 退出判断

阶段 2 的开发版工作包和计划中的四项功能验收均已有实现与测试证据：重复文件、批准不可变、输入/Git 追溯、Bundle 干净环境恢复。当前仍保留 `PASS_WITH_ASSUMPTIONS`，因为真实 S3、PostgreSQL 和生产权限集成尚未验收。

## 6. 下一步

1. 在隔离环境启动 MinIO，执行真实对象、Multipart、哈希和恢复集成测试。
2. 完成 PostgreSQL Repository 第一批查询和双后端契约测试。
3. 接入 HTTP 身份/项目授权和 `Idempotency-Key`。
4. 生产条件满足后，正式关闭阶段 2 并开始阶段 3 Agent Gateway。
