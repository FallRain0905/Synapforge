# 项目导出与恢复协议

## Bundle 内容

当前开发版 Bundle 的 `manifest.json` 为 `schema_version=1.1`，包含：

- 项目及其组织、队伍、项目成员和团队成员关系。
- 项目授权的 Agent 及 `agent_project_grants`。
- Task、Handoff、Artifact、Run、Review、Gate、Evidence 和 Event。
- Git 仓库元数据、HEAD 快照和 commit 文件索引。
- Artifact 的对象存储 key、大小、MIME、SHA-256、版本父子关系和批准状态。
- `objects/<storage_key>` 中的实际文件内容。

旧版 `schema_version=1.0` Bundle 仍可恢复，但缺失的身份关系会使用开发兼容回退值；新导出不再省略这些关系。

## 恢复顺序

1. 读取并解析 manifest，不创建任何数据库记录。
2. 校验项目作用域、ID 唯一性、任务/交接/运行/成果物/审核/证据之间的引用关系。
3. 校验每个对象文件的 SHA-256 和大小，并限制对象 key 在当前项目命名空间内。
4. 在单个 SQLite 事务中写入身份关系和所有项目记录，同时写入对象存储。
5. 恢复 Git 仓库与 commit 索引元数据。

哈希不一致、文件缺失、项目外对象 key、重复 ID、身份关系冲突或悬空引用都会使恢复失败；不会调用普通业务事件创建方法，也不会静默生成“恢复成功”事件覆盖原始时间线。

## 当前接口

- `GET /api/projects/{project_id}/export`：生成 ZIP Bundle。
- `POST /api/projects/restore`：上传 ZIP 并恢复项目。

这两个接口当前仍使用开发 Store。生产版本需要增加流式上传、Bundle 大小限制、权限检查、对象存储临时 key、审计 outbox 和恢复任务队列。
