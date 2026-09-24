# 阶段 2 第一执行批次交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-12
>
> 对应任务：P2-01 对象存储、Artifact 内容接口、Git 索引、项目 Bundle 恢复

## 1. 本轮目标

沿用 SQLite 开发 Store 和既有阶段 0/1 契约，补齐阶段 2 的成果物内容存储、断点 Multipart、Git 索引、项目导出恢复和首批 PostgreSQL 迁移边界。默认运行时没有切换为 PostgreSQL、S3、OIDC 或生产鉴权。

## 2. 已完成

### 2.1 项目 Bundle

- 导出 manifest 升级为 `schema_version=1.1`。
- 导出组织、队伍、项目成员、团队成员关系、项目 Agent 和授权。
- 导出任务、交接、成果物、运行、审核、Gate、Evidence、Event、Git 仓库和 Git 文件索引。
- 恢复前校验项目作用域、重复 ID、任务/交接/运行/成果物/审核/证据引用和 Git 作用域。
- 恢复前读取所有对象并校验 SHA-256、大小和 `projects/{project_id}/` key 前缀。
- 使用 SQLite 事务恢复结构化数据；失败时删除本批次已写入的本地对象。
- 增加篡改对象、悬空引用和完整关系恢复测试。

### 2.2 Artifact 和 ObjectStore

- 新增 `get_artifact()`。
- 新增 Artifact 单文件 API 使用的内容读写边界。
- 新增 `artifact_multipart_uploads` 表和 Store Multipart API。
- Multipart 上传 ID 必须绑定 Artifact，完成时重新写入存储元数据。
- 带显式内容哈希的同名非归档 Artifact 创建会去重。
- JSON Schema 补齐 `ARCHIVED`、storage key、大小、MIME、不可变、父版本和归档时间。
- S3 单文件写入及 Multipart 完成后的 SHA-256 metadata 逻辑已补齐。

### 2.3 Git、API 与迁移基线

- 新增 Git 仓库查询、注册、索引和索引读取 API。
- 新增 Artifact 上传、下载、Multipart 初始化/分块/完成/取消 API。
- 新增项目 Bundle 导出和恢复 API。
- 新增 PostgreSQL 初始表结构、RLS 基线和显式迁移 runner。

## 3. 未完成

- PostgreSQL Repository 的实际查询实现、连接池和 RLS 集成测试。
- 真实 S3/MinIO 集成测试、跨进程 Multipart 完成和对象生命周期任务。
- HTTP 层正式身份认证、项目级成员/Agent 授权、上传配额和流式限制。
- Event outbox、跨服务投递、Bundle 异步恢复和生产审计。
- Git 远端 provider 的 API 同步；当前 Git adapter 只读取本地仓库元数据。

## 4. 测试与结果

执行：

```powershell
$env:PYTHONPATH = "$(Resolve-Path 'apps\\api');$(Resolve-Path 'apps\\api\\vendor')"
& 'C:\Users\19855\Documents\ChatGPT\数学建模\C题工作区\.venv\Scripts\python.exe' -m unittest discover -s apps\\api -p 'test_*.py' -v
```

结果：24 项通过。另有 `compileall` 和 API 导入检查通过。

测试覆盖：

- 单文件 Artifact 哈希、大小、MIME、批准不可变和归档。
- Local Multipart 顺序上传、Artifact 绑定和完成后元数据。
- 内容哈希去重。
- Git commit 索引。
- 完整项目导出恢复、对象篡改拒绝和悬空引用拒绝。
- 原有接力、分发、状态机、租约、信息边界和多组织开发版契约。

## 5. 风险与假设

- S3 Multipart 的上传 ID 当前仍由 S3 适配器的进程内映射辅助解析；生产重启恢复必须改为持久化 provider upload metadata。
- Bundle 恢复会保留原始本地 Git path，但不会复制 Git 工作树；跨机器恢复时路径需要重新绑定。
- 默认 API 仍可被未接入认证的客户端调用，不能作为公网 SaaS 使用。
- `ARCHIVED` 成果物内容仍可读取，但不允许作为新的正式下游输入；这是当前生命周期决策。

## 6. 下一步

1. 建立 PostgreSQL Repository 第一批查询和事务测试，保留 SQLite 契约测试作为双后端基线。
2. 加入 API 授权依赖和 HTTP 幂等头。
3. 为 MinIO 真实服务增加对象、Multipart、哈希和断点恢复测试。
4. 完成阶段 2 退出评审，再开始阶段 3 Agent Gateway、设备配对和离线缓存。
