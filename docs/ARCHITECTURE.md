# 平台架构说明

## 目标

平台的权威对象不是聊天消息，而是任务状态、成果物版本、证据关系和审核决定。聊天只作为讨论入口，正式结论必须通过结构化交接和成果物提交进入项目时间线。

## 控制平面与执行平面

云端控制平面负责项目、任务、权限、交接、成果物索引、审计和实时事件。队员电脑上的本地 Agent 负责代码、模型、数据处理和本地文件访问。Agent 只通过主动外连会话连接平台，平台不要求队员开放入站端口。

## 领域对象

`Organization`、`Team`、`Project`、`CompetitionPack`、`HumanMember`、`Agent`、`Task`、`Handoff`、`Artifact`、`Run`、`Evidence`、`Review`、`Gate` 和 `Event` 是稳定领域对象。开发存储使用 SQLite 验证契约，生产存储替换为 PostgreSQL 和对象存储。

## 数据和信息边界

每个 Artifact 和 Run 都可以声明数据的时间范围、来源类型和访问策略。未来信息检查在运行提交时保存实际读取的输入清单，并由赛事模板的规则验证。

## 可靠性约定

- Agent 任务和 Run 写操作在请求体中携带幂等键；下一步统一收敛为 HTTP `Idempotency-Key`。
- 事件采用单调序号，客户端断线后按序号续传。
- 任务认领使用 lease，超时后可回收。
- 文件以 SHA-256 做内容寻址和去重。
- 未通过门禁的结果不能被标记为正式结果。

## 当前可运行协议

### CUMCM 工作区导入

`POST /api/projects/{project_id}/imports/cumcm` 接收本地目录路径。适配器识别
`PROBLEM_ANALYSIS.md`、`PROBLEM_FACTS.json`、`DATA_PROFILE.json`、
`MODELING_REPORT.md`、审计报告、`code/`、`figures/`、`output/`、`paper/` 和
`user_data/`，将它们登记为 Artifact，并将读取到的内容写入当前 ObjectStore。导入不会复制、删除或改写原工作区文件；原始路径、相对路径、
哈希、对象存储元数据和未来信息策略都写入成果物元数据；重复导入通过项目、文件名和哈希去重。

### Artifact 内容接口

- `POST /api/artifacts/{artifact_id}/content`：单文件上传，可用 `X-Content-SHA256` 校验。
- `GET /api/artifacts/{artifact_id}/content`：下载对象并返回哈希响应头。
- `POST/PUT/POST/DELETE /api/artifacts/{artifact_id}/multipart...`：初始化、分块、完成和取消可恢复上传。

Multipart 上传 ID 同时登记在数据库和 ObjectStore 中，并且始终绑定 Artifact，完成时重新计算最终 SHA-256。

### Git 索引接口

`/api/projects/{project_id}/git` 提供本地仓库注册、HEAD 查询、commit 文件索引和索引读取。平台保存 Git commit 与文件哈希，不把 Git 工作树内容替代为 Artifact；需要上传的代码和论文文件仍通过 Artifact 接口归档。

### 项目 Bundle

项目可通过 `/api/projects/{project_id}/export` 导出，并通过 `/api/projects/restore` 恢复。恢复先校验 manifest 和对象，再在事务中写入项目关系；详情见 `docs/EXPORT_RECOVERY.md`。

### Agent 任务协议

Agent 领取任务后获得 `lease_token`。只有持有有效租约的 Agent 才能回传进度和结果；
租约可以续期，超时后任务可重新领取。结果提交会根据任务的 `requires_review` 自动进入
`WAITING_REVIEW` 或 `APPROVED`，重复请求通过幂等键返回原结果，不重复创建事件或执行结果。

### Run Manifest 与信息边界

Run 在开始时登记 source commit、输入成果物哈希、环境摘要、依赖锁、参数、随机种子、模型、
工具版本和网络策略。完成时登记实际读取文件、标准输出/错误和输出成果物。若输入成果物的
`future_data` 策略违反任务边界，或运行读取了未声明文件，Run 会被记录为 `BLOCKED`，而不是
伪装成成功结果。

## 生产替换点

- SQLite -> PostgreSQL/RLS（已提供初始迁移 SQL，Repository 查询实现仍待完成）
- 本地文件存储 -> S3 兼容对象存储（已提供适配器，生产配置和集成测试仍待完成）
- 进程内事件广播 -> NATS JetStream
- 简化任务执行 -> Temporal 工作流
- 开发 JWT -> OIDC/Keycloak 或托管身份服务
