# Math Agent Platform 项目执行计划

> 文档版本：3.44
>
> 冻结日期：2026-09-12
>
> 当前阶段：阶段 3 开发版已完成（P3-23）；阶段 4 P4-01 至 P4-04 已完成开发切片，P4-04-RUN 已在用户态真实 PostgreSQL/MinIO 完成集成验收，P4-04-RUN-PROD 已完成非 owner 运行时角色、scram-sha-256 密码认证、连接池硬化与故障演练验收，P4-05 浏览器验收准备已完成；阶段 5 P5-01 至 P5-06 已完成开发版契约或接入切片，P5-06-REAL 已完成 Windows ETW 开发版适配、观察器诊断和运行时依赖分类；阶段 6 CUMCM 数学建模模板 pack（manifest/升级规则/四问 DAG/题面事实与数据画像 schema/9 模板/物化器/校验器）已完成端到端验收，阶段 6 剩余项（项目模板导出、信息边界审计 Gate、机器 Review、C 题交接包导入）与阶段 7 仍待执行；真实管理员权限 ETW 采集（UAC 阻塞，已取证）、容器观察、网络阻断和浏览器联调仍待执行
>
> 文档状态：执行基线

## 1. 文档职责

本文档是 Math Agent Platform 的工程执行事实来源，用于回答：

- 当前已经完成了什么。
- 当前实现与目标架构之间还差什么。
- 下一轮应该执行哪些任务。
- 每个阶段依赖什么、如何验收、何时允许进入下一阶段。
- 每轮完成后需要留下哪些交接材料。

文档优先级如下：

1. `PRODUCT_AND_SAAS_DECISIONS.md`：产品定位和 SaaS 架构决策基线。
2. `PROJECT_EXECUTION_PLAN.md`：工程阶段、任务编号、依赖和验收基线。
3. `ARCHITECTURE.md`：当前技术架构及实现边界。
4. `IMPLEMENTATION_STATUS.md`：当前代码状态的简要快照。
5. `docs/handoffs/`：每轮实际执行结果和后续交接记录。

如产品方向发生变化，先更新产品决策文档，再更新本文档。任何代码完成但没有更新本文档和交接记录的任务，不能视为正式完成。

## 2. 项目目标

建设一个面向复杂知识工作的多 Agent 协作平台。数学建模竞赛是第一套领域模板，平台内核不绑定单一赛事。

平台核心不是多 Agent 自由聊天，而是：

```text
可拆解任务
+ 结构化交接
+ 版本化成果物
+ 可复现运行
+ 证据关系
+ 审核门禁
+ 可复用领域模板
= 可追踪、可恢复、可交付的知识生产流程
```

平台采用云端控制平面和本地执行平面：

- 云端保存组织、项目、任务、成果物索引、运行记录、审核、门禁和审计事件。
- 队员个人电脑上的 Agent 负责模型调用、代码执行和本地文件访问。
- Agent 主动外连平台，不要求个人电脑开放公网入站端口。
- Git 管理代码和 LaTeX，平台管理任务、成果物、证据和审批关系。

## 3. 冻结的产品与架构决策

以下决定从本文档 1.0 起视为冻结基线。变更时必须记录原因、影响和迁移方案。

| 决策领域 | 冻结结论 |
| --- | --- |
| 首批用户 | 以 3 人数学建模竞赛队伍为主要设计基准，领域命名保持通用 |
| 首个领域 | CUMCM 工作流包 |
| 首个验证场景 | 接力式协作、分发式协作和独立复核必须同时被契约覆盖 |
| 产品形态 | 多组织、多队伍、多项目 SaaS，后续支持自托管 |
| 核心事实 | Task、Handoff、Artifact、Run、Evidence、Review、Gate、Event 由平台掌握 |
| Agent 连接 | 平台自有协议加 Gateway、agentd、Adapter 和 MCP |
| 执行位置 | 本地 Runner 优先，云端隔离 Runner 为可选扩展 |
| 数据默认值 | 项目默认私有；数据外发和未来数据访问默认拒绝 |
| 版本边界 | Git 管源文件历史；平台管正式成果物、引用、审核和证据关系 |
| 审批边界 | Agent 可以建议批准，不能模拟或替代真实成员审批 |
| 技术基础 | Next.js、FastAPI、PostgreSQL、S3、数据库状态机、NATS 扩展、Docker/Podman Runner |
| 建设方式 | 分阶段交付，但每个阶段都服务于完整目标架构，不建设一次性推倒重来的临时产品 |
| 商业化 | 计费、公开市场和大规模生态延后，先验证可靠协作闭环 |

## 4. 当前代码基线

工程根目录：`math-agent-platform/`

### 4.1 已完成

| 模块 | 已有实现 | 状态 |
| --- | --- | --- |
| 工程结构 | `apps/api`、`apps/web`、`apps/agent`、`packages`、`infra`、`docs`、`scripts` | DONE |
| API 控制平面 | FastAPI 健康检查、项目、任务、交接、成果物、Agent、审核、事件、运行、上传、Git 和 Bundle 接口 | PARTIAL |
| 开发存储 | SQLite 表结构、种子项目、对象存储绑定、Multipart 记录和 Repository 协议边界 | PARTIAL |
| 任务执行协议 | Agent 注册、心跳、任务领取、租约、续租、进度和结果回传 | PARTIAL |
| 幂等处理 | 任务领取、任务进度、结果回传和 Run 创建具备开发版幂等记录；上传、Multipart、Git 和 Bundle 恢复接入 HTTP `Idempotency-Key` 与请求指纹 | PARTIAL |
| 成果物 | 类型、版本、内容哈希、来源路径、任务和运行关联、对象存储、Multipart、批准不可变和归档 | PARTIAL |
| CUMCM 导入 | 识别现有分析、代码、图表、结果、论文和附件目录，登记路径、内容、对象 key 与 SHA-256 | PARTIAL |
| Run Manifest | 代码提交、输入、环境、参数、随机种子、工具、输出和基础信息边界 | PARTIAL |
| 信息边界 | 对未来数据策略和未声明输入文件进行基础检查 | PARTIAL |
| Web 工作台 | 项目总览、任务流、交接、成果物、审核、时间线、Agent 和运行展示 | PARTIAL |
| 实时事件 | 项目 WebSocket 进程内广播 | PARTIAL |
| Agent CLI | `register`、`heartbeat`、`claim`、`progress`、`complete`、`session-worker-run` 和结果接入参数 | PARTIAL |
| 外部 CLI Adapter | Codex `exec --json` 版本/命令/JSONL 事件适配；Claude 明确不可用状态 | PARTIAL |
| 基础设施 | PostgreSQL、NATS 和 MinIO 的 Docker Compose 占位配置 | PARTIAL |
| 项目授权 | HTTP 开发/生产模式、Bearer Session 解析、项目/对象路径授权和主体记账覆盖 | PARTIAL |
| PostgreSQL Repository | 第一批项目、任务、成果物、运行、交接、审核索引、事件读写和连接池事务边界 | PARTIAL |
| Event Outbox | 事件与 outbox 同事务、原子领取、锁过期、Dispatcher 开发版和失败退避 | PARTIAL |
| Gateway 命令结果 | 连接范围结果持久化、ACK 丢失恢复和共享协议模型 | DONE（开发版） |
| 测试 | Store、租约、权限、Artifact、Git、Bundle 恢复、HTTP 幂等/授权、Gateway/Agent 事件与结果接入、设备 Token 轮换、PostgreSQL 映射测试、Python 编译检查、前端生产构建 | PARTIAL |

### 4.2 已验证结果

- 后端当前有 78 个 unittest 通过，另有 2 个真实基础设施测试因 MinIO/PostgreSQL 未提供而跳过；本地 Agent 当前有 114 个 unittest 通过，另有 1 个非 Windows 测试跳过。
- 前端 `npm run build` 通过。
- API 和 Web 开发服务可以启动。
- CUMCM 导入器对现有 C 题工作区 dry-run 可识别 77 个文件且无警告；开发导入会把内容写入 ObjectStore。
- 任务租约、续租、进度和重复结果提交完成过开发环境验证。

### 4.3 当前工作树核验

以上历史基线问题已在 P0-01 交接中修复。本计划随后记录的是当前工作树的实际状态；阶段 0/1 的“完成”均指开发版契约，不代表生产身份、PostgreSQL 和 RLS 已退出验收。

### 4.3 当前实现性质

当前代码是完整目标架构的第一条可运行纵向链路，不是生产版本，也不是可以直接对外提供服务的 SaaS。

以下内容目前仍属于开发实现：

- 无 OIDC 登录和长期设备凭证；HTTP 已支持开发模式默认主体和 Bearer Session，生产模式可强制认证。
- 组织、队伍和项目授权已有开发版模型；HTTP 项目和对象路径已接入开发版 RBAC，尚未完成正式身份服务和 Agent token。
- SQLite 不是生产事实库。
- PostgreSQL Repository 已完成第一批查询和事务封装，但尚未作为默认运行时切换。
- 文件已可写入 LocalObjectStore，也有 S3 适配器，但生产对象存储尚未验收。
- WebSocket 事件不是持久消息。
- Agent 已有项目授权表，但没有设备配对、长期凭证和正式 API 鉴权。
- Run 只登记元数据，没有真实隔离执行器。
- 信息边界检查不是操作系统级文件访问审计。

## 5. 当前协议与实现差距

### 5.1 Task

本轮已经在开发版 SQLite Store 中补齐并执行：

- 父任务、依赖任务、验收标准、能力、deadline、信息边界和资源策略字段。
- 服务端统一状态转换校验。
- 直接写入 `APPROVED` 的门禁阻断。
- 依赖任务和批准成果物未满足时禁止领取。

仍缺少正式的 assignee 类型/身份、任务取消重试协议、租约并发冲突处理、依赖环检测和生产级权限校验。

### 5.2 Handoff

当前开发版已补齐结构化 `receiver`、`schema_version`、接收确认、交接类型、交接幂等键和输入输出成果物的项目归属校验；Gateway 已支持创建和接收交接，并用接力测试验证结构化交接可被下游消费。SQLite 与 PostgreSQL 的交接创建均保存请求指纹，重复请求返回原交接。

P4-01/P4-02 已补齐拒绝、返工修订链、汇总输入交接、逐接收人 Fanout 收据和下游 Gate 门禁；HTTP Agent 接收已接入开发版项目能力 Token，正式设备会话认证和跨实例幂等竞争仍未完成。

### 5.3 Artifact

本轮已经补齐输入成果物、Git commit、文档快照、创建主体类型、批准主体/时间、对象存储元数据和
`downstream_allowed` 字段，并要求成果物批准通过 Review，批准成果物才能作为任务正式输入。

仍缺少正式 Evidence 图谱查询、生产对象存储验收、完整不可变批准版本协议和归档恢复运维策略。

### 5.4 Run

当前已有开发版本地 Runner、exit code、输出事件、Run Manifest、本地恢复队列、输出哈希和
Artifact/Run 绑定。仍缺少：

- 真实隔离 Runner。
- CPU、内存、网络和系统级时间/资源限制。
- 环境镜像和 dependency lock 的实际可复现验证。
- 系统调用级文件访问跟踪。
- 重新执行与重新上传的生产级区分和恢复编排。
- Run 与正式结果门禁的生产级强制绑定。

### 5.5 Review、Gate 和 Evidence

Review 已驱动 Task、Artifact 和 Handoff 状态，且 `APPROVED` 强制要求 `member` 审批；SQLite 与 PostgreSQL 开发版均将 Review、Gate 状态更新、事件/outbox 和幂等记录纳入一次业务事务。Gateway 已允许 Agent 提交复核意见，但不允许 Agent 批准。未批准成果物不能通过任务领取检查。

P4-01/P4-02 已加入统一风险规范化、独立风险登记与关闭/重新打开、Review/Gate/Evidence 关系、审核中心后端聚合和 Gate 输入快照；前端完整数据驱动化、跨实例事务和生产 RLS 验收仍未完成。

### 5.6 Event 和可靠性

当前事件已具备项目序号、类型、actor、对象、幂等键、Schema 版本和事务 outbox；P3-13 已补齐
Agent Runtime 事件接入和 Run 终态映射。仍缺少：

- organization/team scope。
- 并发安全的序号生成。
- NATS 投递和消费游标。
- Agent 离线补传和冲突解决。

### 5.7 SaaS、权限和安全

当前已有开发版实现，仍尚未完成生产化：

- Organization、Team、HumanMember、开发版邀请/Session、项目 RBAC、设备 Token 和项目范围能力 Token。
- PostgreSQL RLS 的真实数据库验收。
- 密钥托管和 BYOK。
- 数据外发策略强制执行。
- 威胁模型和安全测试。

## 6. 阶段与依赖总览

```text
P0 契约冻结
 ├─> P1 多租户、身份和权限
 ├─> P2 成果物、对象存储、导入导出和 Git 索引
 └─> P3 Agent Gateway 和本地 Agent

P1 + P2 + P3
 └─> P4 任务编排、交接、审核和门禁

P2 + P3 + P4
 └─> P5 Runner、可复现性和信息边界审计

P2 + P4 + P5
 └─> P6 CUMCM 正式领域包

P1 + P2 + P4
 └─> P7 Git、协同文档和证据图谱

P5 + P6 + P7
 └─> P8 论文、PPT、报告和最终交付

P1 至 P8 稳定
 └─> P9 稳定性、部署、配额和 SaaS 化
```

阶段可以并行准备，但不得绕过入口条件和验收门禁。

## 7. 阶段 0：领域契约与架构冻结

### 7.1 阶段目标

在引入 PostgreSQL、身份系统、对象存储和更多前端功能前，先冻结平台核心对象、状态机、权限边界、事件语义和首批端到端样例。

### 7.2 入口条件

- 第一轮纵向链路可运行。
- 产品与 SaaS 决策文档完成。
- 当前实现状态已盘点。
- CUMCM 样例项目可用于契约验证。

### 7.3 工作项

| ID | 工作项 | 主要输出 | 状态 |
| --- | --- | --- | --- |
| P0-01 | 冻结产品和架构决策 | 产品决策基线和本执行计划 | DONE |
| P0-02 | 建立领域术语表 | `DOMAIN_GLOSSARY.md` | READY |
| P0-03 | 完成正式领域 Schema | Task、Handoff、Artifact、Run、Evidence、Review、Gate、Event JSON Schema | DONE |
| P0-04 | 冻结任务状态机 | 转换矩阵、操作主体、错误语义和服务端校验 | PARTIAL |
| P0-05 | 冻结成果物生命周期 | 草稿、提交、批准、拒绝、归档和下游引用规则 | READY |
| P0-06 | 冻结交接协议 | receiver、schema version、接收确认、返工和汇总规则 | PARTIAL |
| P0-07 | 冻结 Review、Gate 和 Evidence | 审查类型、风险级别、人工批准和自动阻断规则 | READY |
| P0-08 | 冻结事件信封 | actor、object、scope、idempotency key、payload version | READY |
| P0-09 | 冻结信息边界 Schema | 时间范围、可访问时间、观测/预测/计划/真实标签和读取清单 | PARTIAL |
| P0-10 | 建立接力式契约测试 | A Agent -> B Agent -> Reviewer 的纯后端流程 | DONE |
| P0-11 | 建立分发式契约测试 | 并行子任务、汇总依赖、冲突和审核流程 | DONE |
| P0-12 | 建立状态机性质测试 | 非法跳转、重复提交、过期租约和门禁绕过 | PARTIAL |
| P0-13 | 权限矩阵初稿 | 成员、负责人、贡献者、审核人、Agent 和观察者 | READY |
| P0-14 | 威胁模型初稿 | 路径越权、数据外发、伪造审批、重放和越租户访问 | READY |
| P0-15 | 导出恢复协议 | manifest、对象文件、Git 引用和校验清单 | READY |
| P0-16 | 阶段 0 交接与评审 | `docs/handoffs/P0_*_HANDOFF.md` 和阶段验收记录 | READY |

### 7.4 第一执行批次

本轮第一执行批次已完成：

1. `P0-03`：完成正式领域 Schema。
2. `P0-04`：收紧服务端状态机和人工门禁。
3. `P0-10`、`P0-11`：建立接力式与分发式纯后端契约测试。

本批次没有引入 PostgreSQL、OIDC、NATS、Temporal 或 Yjs。下一批次应先完成 P0-02、P0-05 至 P0-09、P0-12 至 P0-16，再评估阶段 0 是否具备退出条件。

### 7.5 阶段 0 验收标准

- 正式 Schema 可以独立描述一次完整协作，不依赖具体数据库和前端。
- 所有任务状态跳转由服务端统一校验。
- Agent 不能批准需要人工确认的任务和成果物。
- 依赖任务未批准时，下游任务不能被领取。
- fatal review 自动阻断下游。
- 已批准成果物才能作为正式下游输入。
- 接力测试覆盖任务、成果物、交接、审核和事件。
- 分发测试覆盖并行任务、汇总依赖、重复提交和冲突。
- Schema、Pydantic 模型和测试样例之间没有字段冲突。
- 阶段交接文档完整记录已做、未做、测试和下一步。

### 7.6 阶段 0 退出条件

只有满足以下条件才进入阶段 1：

- P0-03 至 P0-14 完成或有书面延期决定。
- 接力和分发测试全部通过。
- 非法状态跳转和 Agent 越权审批测试通过。
- 产品负责人确认核心 Schema 不再发生破坏式变更。
- 阶段 0 交接文档完成。

## 8. 阶段 1：多租户、身份和权限基础

### 8.1 依赖

- 阶段 0 核心 Schema、权限矩阵和事件信封冻结。

### 8.2 工作包

- Organization、Team、HumanMember 和 Membership。
- PostgreSQL Repository 和迁移框架。
- 邮箱/邀请登录，结构兼容 OIDC。
- 项目角色和 Agent 项目授权。
- RBAC，预留 ABAC 条件。
- 项目默认私有和数据外发策略。
- 人类会话和 Agent 设备凭证分离。
- 审计 actor 绑定真实主体。

### 8.3 验收

- 两个组织和两个队伍数据互不可见。
- Contributor、Reviewer、Project Lead 和 Observer 权限不同。
- Agent 不能继承所有者的完整人类权限。
- Agent 无法伪造人工批准。
- PostgreSQL 中所有项目对象带租户作用域。

## 9. 阶段 2：成果物、对象存储、Git 与导入导出

### 9.1 依赖

- 阶段 0 Artifact 和 Event 契约。
- 阶段 1 项目权限。

### 9.2 工作包

- S3 兼容对象存储。
- 分块上传、断点续传、哈希和 MIME 校验。
- Artifact Version 和不可变批准版本。
- 输入输出、引用和被引用关系。
- Git provider adapter 和 commit 索引。
- CUMCM 导入适配器升级为对象存储导入。
- 项目导出、校验和恢复。
- 存储生命周期和归档策略。

### 9.3 验收

- 文件重复上传不会生成错误重复版本。
- 已批准成果物不能被静默覆盖。
- 一个结果可以追溯到输入文件和 Git commit。
- 项目可导出并在干净部署中恢复。

### 9.4 当前退出评审

阶段 2 的开发版工作包已完成：LocalObjectStore、S3 适配器、Multipart、哈希/MIME/大小校验、Artifact 版本和批准不可变、输入输出引用、Git commit 索引、CUMCM 内容导入、Bundle 导出恢复以及归档/过期上传清理均已有实现和契约测试。本轮又完成 PostgreSQL Repository 第一批事务查询、HTTP `Idempotency-Key` 请求指纹、开发版项目路径授权，以及 SQLite 高风险写入的事务 Event Outbox。

四项阶段 2 验收场景均已有测试证据：重复文件不会产生错误重复版本；批准成果物不能覆盖；结果可通过输入成果物和 Git commit 追溯；Bundle 可在干净 Store 恢复。

阶段 2 仍不能标记为生产完成，退出前置条件为：真实 MinIO/S3 集成测试、完整 PostgreSQL Repository 与 RLS 集成测试、生产身份和正式权限依赖、上传配额/流式限制、outbox Dispatcher/并发重试、以及生产迁移演练。当前状态保持 `PASS_WITH_ASSUMPTIONS`，由 `docs/handoffs/P2_01_OBJECT_STORAGE_GIT_EXPORT_HANDOFF.md`、`docs/handoffs/P2_02_INTEGRITY_AND_LIFECYCLE_HANDOFF.md`、`docs/handoffs/P2_03_RETRY_CONFIG_INTEGRATION_HANDOFF.md`、`docs/handoffs/P2_04_POSTGRES_HTTP_BOUNDARY_HANDOFF.md` 和 `docs/handoffs/P2_05_EVENT_OUTBOX_TRANSACTION_HANDOFF.md` 共同记录。

## 10. 阶段 3：Agent Gateway、本地 Agent 与 Adapter

### 10.1 依赖

- 阶段 0 Agent、Task、Run 和 Event 契约。
- 阶段 1 身份和项目授权。
- 阶段 2 成果物上传接口。

### 10.2 工作包

- 设备配对和长期设备密钥。
- 项目范围和能力范围 token。
- Gateway 长连接、心跳和在线状态。
- 任务拉取、租约续期、取消和冲突处理。
- Agent 本地输出缓存和断线恢复。
- 工作区路径白名单。
- 危险命令和外部网络本地确认。
- MCP Server/Client。
- Codex、Claude Code 和 Headless Agent Adapter 接口。
- 兼容性测试矩阵。

### 10.3 验收

- 两台不同电脑上的 Agent 可以加入同一项目。
- 至少一个 Agent 通过 MCP/Adapter，另一个通过 agentd。
- 断线、重复回传和租约过期不会产生重复正式结果。
- Agent 无法读取未授权项目和路径。

### 10.4 P3-01 当前执行切片

本切片先冻结设备接入的后端领域契约，不提前宣称 Gateway 或本地运行时已经完成：

| 内容 | 当前状态 | 说明 |
| --- | --- | --- |
| `Device`、`DevicePairing`、`DeviceProjectGrant` | DONE（开发版） | SQLite Store、Pydantic 和 JSON Schema 已对齐 |
| 一次性配对码 | DONE（开发版） | 只保存 SHA-256，消费后不可重放 |
| 设备 Token | DONE（开发版） | 只保存哈希，提供失效检查 |
| 项目范围能力 Token | DONE（开发版） | 绑定 device、agent、project、capabilities 和过期时间 |
| 设备撤销 | DONE（开发版） | 撤销设备、项目令牌和已有连接 |
| `AgentConnection` 元数据 | PARTIAL | 已有 SQLite/迁移和打开连接方法，尚无 Gateway 长连接 |
| PostgreSQL Repository | PARTIAL（开发版） | 设备、项目令牌、连接、序号和心跳方法已加入，真实数据库尚未验收 |
| 公钥签名证明 | DONE（开发版，P3-18） | Ed25519 challenge/signature 持有证明、规范化指纹和 pairing 一次性消费已接入 |

P3-01 的后端验收证据为 `apps/api/test_devices.py` 和
`docs/handoffs/P3_01_DEVICE_IDENTITY_HANDOFF.md`。本切片允许阶段 2 的真实基础设施
退出工作继续作为并行依赖，但不得把开发版 SQLite 设备链路当作阶段 3 生产验收。

### 10.5 P3-02 当前执行切片

| 内容 | 当前状态 | 说明 |
| --- | --- | --- |
| Gateway Token 握手 | DONE（开发版） | `/ws/agents/{device_id}` 使用设备 Token，并校验路径身份 |
| 连接生命周期 | DONE（开发版） | 建立、连接元数据、正常断开和撤销连接关闭 |
| `GatewayEnvelope` 校验 | DONE（开发版） | 设备、Agent、Session、Connection ID 必须与连接上下文一致 |
| 心跳处理 | DONE（开发版） | 更新 Agent、Device 和 AgentConnection 的在线时间 |
| 入站序号 | DONE（开发版） | 连续帧接受、重复帧幂等确认、缺口返回补传请求 |
| 出站序号 | DONE（开发版） | 每条 Gateway 响应使用持久化连接序号 |
| 任务/桌面命令处理 | PARTIAL | 任务、Run、Handoff、Review 命令已接入开发版 Gateway；Artifact 命令和桌面控制仍待实现 |
| 公钥签名挑战 | DONE（开发版，P3-18） | 注册前必须证明 Ed25519 私钥持有；挑战只保存哈希，旧无挑战 pairing 不可注册 |
| 本地离线队列 | TODO | 进入 P3-03 `agentd` 持久化和断线补传 |

P3-02 的后端验收证据为 `apps/api/app/gateway.py`、
`apps/api/app/main.py` 中的 Agent WebSocket 路由和 `apps/api/test_gateway.py`。

### 10.6 P3-03 当前执行切片

| 内容 | 当前状态 | 说明 |
| --- | --- | --- |
| 本地事件持久化 | DONE（开发版） | `apps/agent/local_state.py` 使用 SQLite 保存序号、消息、幂等键和状态 |
| 未确认事件恢复 | DONE（开发版） | `SENT` 事件重启后回到 `PENDING`，保留原序号和消息 ID |
| ACK/补传处理 | DONE（开发版） | 客户端按连续序号确认，并按 `after_sequence` 重新排队 |
| Run/上传/审批本地状态 | DONE（开发版） | 建立恢复所需的本地状态表 |
| Gateway 客户端封套 | DONE（开发版） | `DurableGatewayClient` 与平台 `GatewayEnvelope` 共用字段约束 |
| 实际 WebSocket 客户端 | PARTIAL | `run_once()` 已提供 websockets 适配，P3-08 已补充监督器、退避和心跳调度；真实长连接仍待验收 |
| 安全凭据存储 | DONE（开发版，P3-19/P3-20） | Windows Credential Manager 适配器、目标名规范、Gateway/Service 默认读取和服务端 Token 轮换已接入；真实跨登录会话持久化和自动写回仍待实机验收 |
| Machine Agent Service | DONE（开发版，P3-08） | 已有连接监督、心跳、进程监督和本地紧急停止；Windows Service、User Session Worker 和本地 IPC 仍未完成 |

P3-03 的验收证据为 `apps/agent/local_state.py`、
`apps/agent/gateway_client.py`、`apps/agent/agentd.py` 以及
`apps/agent/test_local_state.py`、`apps/agent/test_gateway_client.py`。

### 10.7 P3-04 当前执行切片

| 内容 | 当前状态 | 说明 |
| --- | --- | --- |
| PostgreSQL Device/Pairing 映射 | DONE（开发版） | 设备登记、配对消费、设备 Token 哈希和撤销方法已实现 |
| PostgreSQL Project Token | DONE（开发版） | 项目/组织/能力/过期/撤销检查已实现 |
| PostgreSQL AgentConnection | DONE（开发版） | 连接建立、读取、关闭和设备撤销联动已实现 |
| PostgreSQL Gateway 序号 | DONE（开发版） | `FOR UPDATE` 锁下连续序号、重复和缺口语义已对齐 SQLite |
| PostgreSQL 心跳 | DONE（开发版） | 校验四元身份并更新 Device/Agent/Connection 状态 |
| PostgreSQL 真实迁移/RLS | TODO（集成验收） | 当前环境没有 PostgreSQL，尚未执行 001 至 009 |
| 跨实例并发 Gateway | TODO（集成验收） | 尚未做连接池、RLS 和并发序号压力测试 |

P3-04 的验收证据为 `apps/api/app/postgres_repository.py`、
`apps/api/test_postgres_repository.py` 和 `apps/api/test_platform_contracts.py`，
生产验收仍必须依赖真实 PostgreSQL。

### 10.8 P3-05 当前执行切片

本切片把设备级认证和项目级授权接到旧 HTTP Agent 任务/Run 写路径。设备 Token 只证明
这是已登记设备，项目能力 Token 才证明该设备代表的 Agent 可以在指定项目执行某项操作。
该切片仍使用开发版 SQLite Repository，不代表生产身份系统或 Gateway 全部完成。

| 内容 | 当前状态 | 说明 |
| --- | --- | --- |
| 项目能力 Token 请求头 | DONE（开发版） | 机器写请求使用 `X-Project-Capability-Token`，不接受人类 Session 代替 |
| 任务领取 | DONE（开发版） | `task.claim` 校验项目、Agent 和 Token 能力 |
| 租约续期 | DONE（开发版） | `task.lease` 校验项目归属，防止跨项目复用 lease token |
| 任务进度/结果 | DONE（开发版） | 分别要求 `task.progress`、`task.result` |
| Run 创建/完成 | DONE（开发版） | 分别要求 `run.create`、`run.complete` |
| agentd CLI | DONE（开发版） | claim/progress/complete/lease-heartbeat 附带项目 Token |
| 跨项目和错误 Agent 阻断 | DONE（开发版） | 纯后端契约覆盖 Token、项目、Agent、能力和租约绑定 |
| Gateway 内任务命令 | DONE（开发版，P3-06） | Gateway 任务/租约/进度/结果和 Run 命令已接入项目能力 Token |
| PostgreSQL 任务/Run 运行时 | TODO（集成验收） | PostgreSQL Repository 尚未覆盖完整任务租约和 Run 编排 |

固定 HTTP 约定：

```text
X-Project-Capability-Token: prj_...
```

能力名为 `task.claim`、`task.lease`、`task.progress`、`task.result`、`run.create` 和
`run.complete`。领取任务时必须在请求体提供 `project_id`；租约心跳也必须提供
`project_id`，服务端会同时核对 Token 和 lease 的项目归属。无效、撤销或过期 Token 返回
401；能力不足、Agent 不匹配或租约项目不一致返回 403。

P3-05 的验收证据为 `apps/api/app/main.py`、`apps/api/app/store.py`、
`apps/agent/agentd.py` 和 `apps/api/test_agent_capability.py`，交接记录为
`docs/handoffs/P3_05_PROJECT_CAPABILITY_ENFORCEMENT_HANDOFF.md`。

P3-06 的 Gateway 命令验收证据为 `apps/api/app/gateway.py`、
`apps/api/test_gateway.py`、`apps/agent/gateway_client.py` 和
`apps/agent/test_gateway_client.py`。该切片仍属于开发版：真实 PostgreSQL 任务/Run
Repository、跨实例连接、Token 轮换和长时间断线恢复尚未验收。

### 10.9 P3-06 当前执行切片

P3-06 将项目能力 Token 从 HTTP 写路径延伸到 Gateway 消息路径。命令消息沿用
`GatewayEnvelope` 的设备/Agent/Session/Connection 身份，并在 payload 中携带项目 ID 和
项目能力 Token。成功命令使用 `gateway.ack` 返回结果，同时确认入站消息序号，以便本地
持久化队列完成幂等清理。

支持的消息类型：

```text
agent.task.claim
agent.task.lease.heartbeat
agent.task.progress
agent.task.result
agent.run.create
agent.run.complete
```

当前完成：

- Gateway 在执行命令前校验项目 Token、设备、Agent 和能力。
- 任务和 Run 资源再次核对项目归属，避免仅凭 payload 中的项目 ID 放行。
- 命令成功结果嵌入 ACK 的 `command_result` 字段。
- 命令错误返回 `gateway.error`，入站序号已经被记录，不会因为重复投递再次执行。
- `DurableGatewayClient` 可以读取 ACK 中的 `command_result`。

P3-06 已完成开发版验收，交接记录为 `docs/handoffs/P3_06_GATEWAY_COMMANDS_HANDOFF.md`。以下内容转入 P3-07：

- 真实 Gateway 长连接下的命令执行和结果回传。
- PostgreSQL 任务/租约/Run Repository 和跨实例并发验证。
- Gateway 中的 Artifact 上传、Handoff 交接和审批命令。

### 10.10 P3-07 Gateway 结果恢复与跨端协议包

P3-07 处理 ACK 丢失后“消息已执行但客户端没有业务结果”的恢复问题，并把 Agent 与 API
之间的 Gateway 传输模型移出 `apps/api` 内部模块。结果以连接范围的
`GatewayCommandResult` 持久化，分别按 `message_id` 和 `idempotency_key` 查询。

当前完成：

- SQLite 增加 `gateway_command_results` 表、唯一约束和查询/保存仓储接口。
- PostgreSQL 增加 `006_gateway_command_results.sql`，包含租户 RLS 策略和索引。
- Gateway 首次执行成功或失败命令后保存结果。
- 重复序号或同一幂等键重试会恢复原 `command_result` 或原错误码。
- 新增 `packages/agent_protocol`，API 和本地 Agent 共同使用 Gateway 封套、心跳、ACK、补传和结果模型。
- 独立协议 JSON Schema 已加入 `packages/agent_protocol/gateway.schema.json`，领域 Schema 和目录同步更新。

当前未完成：

- 结果保存与业务副作用尚未在同一 PostgreSQL 事务中完成，跨实例并发下仍需防止“重复执行后才发生唯一键冲突”。
- 真实 PostgreSQL/RLS/连接池迁移尚未执行，ACK 丢失恢复尚未经过真实 TLS 反向代理长连接测试。
- Artifact Gateway 命令、结果副作用同事务、真实 PostgreSQL/RLS 和长连接生产验收仍未完成；Machine Agent Service 已转入 P3-08 并完成开发版。

P3-07 交接记录为 `docs/handoffs/P3_07_GATEWAY_RESULT_RECOVERY_AND_PROTOCOL_HANDOFF.md`。

### 10.11 P3-08 Machine Agent Service 与断线监督

P3-08 把此前的单次 `gateway-run` 适配提升为机器级服务原语，负责连接监督、心跳调度、
本地紧急停止和受控子进程生命周期。该切片是开发版本地运行时，不等同于已完成 Windows
Service 安装、用户会话 Worker 或系统级沙箱。

当前完成：

- `apps/agent/machine_service.py` 提供 `MachineServiceConfig`、`MachineAgentService`、
  `LocalProcessSupervisor`、`ProcessSpec` 和 `ProcessResult`。
- Gateway 断开、连接失败或 Token 解析失败后，服务按配置的指数退避自动重连；退避上下限
  由 `reconnect_base_seconds` 和 `reconnect_max_seconds` 控制。
- 心跳由独立调度循环写入本地 Gateway 持久化队列，发送仍由 `DurableGatewayClient` 负责。
- 共享 SQLite `service_control` 标志提供 fail-closed 的本地紧急停止；服务外部进程可以设置
  或清除该标志，服务检测到后取消连接并停止受管进程。
- 子进程使用 `asyncio.create_subprocess_exec`，不经过 shell；记录 stdout、stderr、退出码、
  超时、取消、启动失败和 Run 状态。
- 服务启动时将上一个服务实例遗留的 `STARTING/RUNNING` 进程记录标记为 `ABANDONED`，避免
  把无法确认的旧进程继续当作活动运行。
- `agentd` 新增 `service-run`、`service-emergency-stop` 和 `service-clear-emergency-stop`。
- `apps/agent/test_machine_service.py` 覆盖退避、同步/异步 Token、自动重连、心跳、外部紧急
  停止、解除停止、子进程输出、非零退出、超时、取消、批量停止和孤儿恢复。

开发版验收：

```text
apps/agent: 17 passed
compileall apps/api apps/agent packages: passed
```

当前未完成：

- P3-19/P3-20 已接入 Windows Credential Manager 开发版和服务端 Token 轮换；当前开发机的长期
  `CRED_PERSIST_LOCAL_MACHINE` 写入仍可能因登录会话条件返回 `1312`，目标部署身份下的持久化、ACL、
  轮换后自动写回和服务访问不能据此视为已验收。
- 尚未实现 Windows Service 安装/升级、Session 0 与用户桌面切换、User Session Worker、
  Named Pipe/本机 RPC 和桌面控制。
- 子进程目前是直接子进程监督，尚未实现跨平台进程树终止、工作区/可执行文件白名单、操作系统
  级网络隔离和资源 cgroup/Sandbox。
- 真实 TLS/反向代理长连接、跨进程恢复、PostgreSQL 运行时和业务副作用与结果写入的同事务
  保证仍未验收。

P3-08 交接记录为 `docs/handoffs/P3_08_MACHINE_AGENT_SERVICE_HANDOFF.md`。

### 10.12 P3-09 Runner/Adapter 基础契约与工作区策略

P3-09 将机器服务提供的“可启动进程”收紧为可审计的 Runner 请求。Runner 在创建本地
进程前校验项目、任务、Run、Agent、Device、Workspace 的完整绑定，并通过 Adapter 将统一
请求转换为不经 shell 的 `ProcessSpec`。本切片提供一个普通 Python Adapter 作为第一个可执行
实现，同时保留后续 CLI、容器和用户会话 Adapter 的扩展边界。

当前完成：

- `apps/agent/runner.py` 新增 `RunnerRequest`、`ExecutionProfile`、`AdapterDescriptor`、
  `RunnerAdapter`、`CommandAdapter`、`PythonAdapter`、`WorkspacePolicy` 和 `LocalRunner`。
- Runner 请求必须携带 `project_id`、`task_id`（可为空）、`run_id`、`agent_id`、`device_id`、
  `workspace_id`、`workspace_path` 和命令；Adapter 生成的进程必须保持同一上下文绑定。
- 支持 `HEADLESS`、`USER_SESSION`、`INTERACTIVE_DESKTOP` 执行模式的基础契约校验；当前
  只有 HEADLESS 直接执行，用户会话和桌面模式必须等待对应 Worker/IPC。
- `WorkspacePolicy` 校验工作区、输入路径、输出路径、可执行文件和环境变量白名单；路径使用
  规范化绝对路径并拒绝工作区外路径。
- 未具备操作系统网络隔离器时，`allow-listed` 和 `unrestricted` 网络策略直接拒绝；只有显式
  `NetworkEnforcer` 才能进入非默认网络路径。
- `ProcessSpec` 和 `managed_processes` 记录项目、任务、Agent、设备、工作区上下文，Run 状态
  与进程生命周期保持关联；Adapter 不能伪造归属或重新启用宿主机环境继承。
- `PythonAdapter` 使用配置解释器和现有 `LocalProcessSupervisor` 执行 Python 命令，stdout、
  stderr、退出码和本地 Run 状态继续沿用 P3-08 记录路径。

开发版验收：

```text
apps/agent: 26 passed
compileall apps/api apps/agent packages: passed
```

当前未完成：

- `USER_SESSION`、`INTERACTIVE_DESKTOP` 尚未连接 User Session Worker、ConPTY 或桌面控制。
- 网络 Enforcer 目前只是接口契约，尚未提供 Windows 防火墙/容器/沙箱级实际网络隔离。
- 尚未实现进程树终止、操作系统资源限制、工作区挂载隔离、文件访问审计和输出文件自动发现。
- `PythonAdapter` 当前通过调用方给出的 Python argv 执行，尚未从平台 Run 命令自动创建完整
  Runner Manifest、输入下载和输出 Artifact 上传流程。
- Codex、Claude Code、MCP、Office 和 Browser Adapter 尚未实现。

P3-09 交接记录为 `docs/handoffs/P3_09_RUNNER_ADAPTER_POLICY_HANDOFF.md`。

### 10.13 P3-10 User Session Worker/本机 IPC 传输无关契约

P3-10 先冻结 Machine Service、User Session Worker 和 Desktop UI 之间的授权与生命周期
语义，再接入 Windows Named Pipe 等具体传输。当前切片不打开端口、不创建 Named Pipe，
因此不能视为真实跨进程 IPC 或 Windows Service 实现。

当前完成：

- `packages/agent_protocol` 新增 `SessionPeer`、`SessionRunContext`、`SessionIpcRequest`、
  `SessionIpcResponse`、`SessionIpcEvent` 和 `SESSION_IPC_MESSAGE_TYPES`。
- 新增 `packages/agent_protocol/session.schema.json`，覆盖请求、响应、事件、Peer 和运行上下文。
- `apps/agent/session_worker.py` 新增 `SessionWorkerBroker`、Worker 注册、请求幂等、Peer/SID
  校验、执行模式校验、能力校验、用户会话状态和桌面审批策略。
- Machine Service 使用独立的服务身份，不要求携带交互用户 SID；User Session Worker 和 Desktop UI
  仍需匹配用户 SID，真实服务身份认证留给 Named Pipe/ACL 传输层。
- Machine Service 只能启动/停止 User Session Run；Worker 只能上报 hello/status；Desktop UI
  只能回应审批；终端输入和桌面控制均通过明确能力、上下文和审批门禁。
- Run 启动要求 `session.run` 能力；停止允许在注销后清理已存在 Run，但不能停止不存在的 Run；
  活动 Run 期间状态上报不会错误地覆盖为闲置状态；停止后的 Worker 拒绝新的普通请求。
- 同一 Worker 和幂等键重复请求返回 `DUPLICATE`，同幂等键不同内容返回冲突；错误响应始终带稳定错误码。
- 新增 `apps/agent/test_session_worker.py` 和 `apps/agent/test_protocol_schemas.py` 的协议、权限、
  生命周期、幂等和 Schema 一致性测试。

开发版验收：

```text
apps/agent: 39 passed
apps/api: 68 passed, 2 skipped
compileall apps/api apps/agent packages: passed
JSON Schema parse: domain/domain catalog/task state/gateway/session passed
API import: 56 OpenAPI paths（含 P3-13 Agent Artifact 路径）
```

当前未完成：

- Windows Named Pipe、Windows ACL、真实进程身份认证、Session 0/用户会话切换和 Windows Service。
- ConPTY、桌面控制实现、进程树终止和 OS 级网络/资源隔离。
- Worker 与 Machine Service 的真实跨进程事件流、断线恢复和本机操作审计持久化。

P3-10 交接记录为 `docs/handoffs/P3_10_USER_SESSION_WORKER_IPC_HANDOFF.md`。

### 10.14 P3-11 Windows Named Pipe/ACL/真实进程身份认证传输

P3-11 将 P3-10 的传输无关 Broker 接入 Windows 原生 Named Pipe。传输层负责字节帧、连接、
ACL 和 OS 对端身份发现，授权规则仍由 `SessionWorkerBroker` 负责。当前完成的是可在现有
Windows 开发环境验证的传输适配，不等同于已安装 Windows Service 或完成所有生产硬化。

当前完成：

- 新增 `apps/agent/named_pipe_transport.py`，使用 Windows 原生 `ctypes` API，不依赖 `pywin32`。
- `JsonFrameCodec` 使用小端 4 字节长度前缀和 UTF-8 JSON，限制最大帧大小，拒绝截断、长度不一致、
  非 JSON 对象和超限消息。
- `NamedPipeServer`/`NamedPipeClient` 支持双工 byte-mode Named Pipe；同一连接可连续处理多个请求。
- Pipe ACL 使用 SDDL 显式授予 SYSTEM、Administrators 和配置的受信 SID，启用拒绝远程客户端标志。
- 服务端通过 `GetNamedPipeClientProcessId` 获取对端 PID，再通过进程 Token 获取 SID，通过
  `ProcessIdToSessionId` 获取 Windows Session ID。
- `PipePeerPolicy` 支持多个受信 SID 和显式允许的 Windows Session ID；认证后的 PID、SID、Session ID
  作为 `SessionPeer` 交给 Broker，Machine Service 不要求携带交互用户 SID。
- 服务端拒绝不在允许 SID 或 Windows Session ID 范围内的客户端；传输层不绕过 Broker 的请求身份校验。
- 新增 Named Pipe 帧、同机多请求、ACL 策略、Session ID 拒绝和非 Windows fail-closed 测试。

开发版验收：

```text
apps/agent: 47 passed, 1 skipped
apps/api: 68 passed, 2 skipped
compileall apps/api apps/agent packages: passed
Named Pipe same-host round trip and peer rejection: passed on Windows
JSON Schema parse: domain/domain catalog/task state/gateway/session passed
API import: 56 OpenAPI paths（含 P3-13 Agent Artifact 路径）
```

当前未完成：

- Windows Service 安装、升级、恢复、服务账户部署和 Session 0 到用户会话 Worker 的真实启动链路。
- 跨账户/跨 Session 的实机矩阵、服务账户 ACL 演练、Pipe 欺骗/重放/压力和长时间断线验收。
- 真实跨进程事件持久化、Worker 注册生命周期、ConPTY、桌面控制和 Runner 编排。
- 本机操作审计持久化、进程树终止、OS 级网络/资源隔离和生产密钥管理。

P3-11 交接记录为 `docs/handoffs/P3_11_NAMED_PIPE_TRANSPORT_HANDOFF.md`。

### 10.15 P3-12 User Session Worker 与 Runner/标准管道事件编排

P3-12 将 P3-11 的 Named Pipe、P3-10 的 Broker 和 P3-09 的 Runner 组成真实的本地执行链。
当前实现使用标准 stdin/stdout/stderr 管道支持 `USER_SESSION`，并保留对 `INTERACTIVE_DESKTOP`
和 ConPTY 的显式 fail-closed 行为；普通管道不能被描述为 ConPTY。

当前完成：

- `LocalProcessSupervisor` 支持 `stdin_enabled`、受控 stdin 写入和 stdout/stderr 实时回调。
- `LocalRunner` 增加 `start`、`wait`、`request_stop`、`write_stdin` 和活动 Run 查询，保留原有
  `run` 兼容入口；`ProcessSpec` 的执行归属字段恢复为真正必填。
- 新增 `SessionRunStartPayload`，将 Adapter、命令、环境、输入输出路径、超时、输出上限和网络
  策略纳入 Session 启动载荷 Schema。
- 新增 `apps/agent/session_runtime.py` 的 `SessionWorkerRuntime`：授权请求先进入 Broker，
  再构造 `RunnerRequest`，在后台 asyncio loop 监督用户会话进程，并处理启动、停止和终端输入。
- Runtime 生成有序 `process.started`、实时 `process.stdout/stderr`、`process.exited` 和
  `run.completed/failed` 事件，事件进入内存队列，并可通过 `LocalAgentState` 持久化到 Gateway 队列。
- `INTERACTIVE_DESKTOP` 没有桌面执行器时明确返回失败；不降级执行、不伪造 ConPTY。
- `agentd session-worker-run` 提供可运行的 Windows User Session Worker Named Pipe 入口，配置
  Worker 身份、Pipe 受信 SID/Session、工作区、Python Adapter 和环境白名单。
- 新增 Runner stdin/停止、Runtime 事件、Named Pipe 到 Runtime 到 Runner 纵向链路测试。
- stdout/stderr 使用同一 Run 总输出预算，超额内容继续排空但不进入结果或事件。

开发版验收：

```text
apps/agent: 58 passed, 1 skipped
apps/api: 68 passed, 2 skipped
compileall apps/api apps/agent packages: passed
session-worker-run CLI help: passed
Named Pipe -> SessionWorkerRuntime -> Broker -> Runner: passed on Windows
JSON Schema parse: domain/domain catalog/task state/gateway/session passed
API import: 68 routes / 51 OpenAPI paths
```

当前未完成：

- ConPTY、交互式终端真实 PTY 语义、INTERACTIVE_DESKTOP 桌面控制和屏幕传输。
- Worker 作为 Windows Service/Session 0 的启动、用户会话发现切换、注销监听和跨账户部署。
- P3-13 已完成开发版 Session 事件上传、输出文件发现/哈希、Artifact 上传和 Run Manifest；仍需
  生产级断线游标、跨服务事务和正式结果门禁绑定。
- 进程树终止、OS 级网络/资源隔离、本机审计持久化和生产密钥管理。

P3-12 交接记录为 `docs/handoffs/P3_12_SESSION_RUNNER_RUNTIME_HANDOFF.md`。

### 10.16 P3-13 CLI Adapter、事件上传和 Artifact/Run 结果接入

P3-13 的本轮结果接入子切片将 P3-12 的本地运行结果接入平台控制平面，完成以下开发版纵向链路：

- `AgentEventPayload` 以 `project_id`、项目能力令牌和 `SessionIpcEvent` 组成正式 Gateway
  事件载荷。Gateway 验证令牌、设备/Agent、项目和 Run 归属后，将安全事件写入 Event；令牌
  不写入平台 Event payload。
- `agent.event` 使用现有 Gateway 入站序号、ACK 和本地 outbox 机制。相同幂等键不会重复写入
  Event；`run.completed`/`run.failed` 事件会完成已存在的同 Agent Run。
- Agent 提供 `RuntimeEventUploader`，把带项目绑定的本地事件放入 `DurableGatewayClient`；
  `execute_command` 可在持久队列基础上等待 Gateway ACK 业务结果。
- `OutputDiscovery` 只扫描 Runner 显式声明、且位于工作区白名单内的文件，计算 SHA-256、大小、
  MIME 和相对路径；`RunManifestBuilder` 生成包含命令、执行配置、环境变量名、输入输出和流摘要
  哈希的 `run_manifest`。
- `ResultUploader` 将输出和 Manifest 写入本地 `pending_uploads` 恢复表，使用确定性幂等键调用
  Agent Artifact 接口。小文件走单文件上传，大文件走 Multipart；文件在排队后发生变化会被拒绝。
- 新增 Agent 专用 Artifact HTTP 路径，所有创建、内容上传、Multipart 操作均要求
  `X-Project-Capability-Token`、`X-Agent-Id` 和请求幂等键；Artifact 必须绑定当前 Agent 的 Run。
- Runtime 的完成事件携带输出 Artifact ID、stdout/stderr、退出码和输入文件清单，从而把本地结果
  映射到平台 Run。Artifact 仍需后续人工 Review 才能成为正式下游输入。

开发版验收证据：

```text
apps/api: 71 passed, 2 skipped
apps/agent: 63 passed, 1 skipped
compileall apps/api apps/agent packages: passed
Gateway runtime event and Agent Artifact contract tests: passed
```

P3-13 仍未完成：真实 CLI（Codex/Claude Code）语义适配和 Prompt/取消协议、ConPTY、桌面执行、Windows Service、
设备密钥环、真实 PostgreSQL/MinIO、NATS 投递、OS 级网络/资源隔离、文件访问系统审计，以及正式
Run/Artifact Review/Gate 的生产级跨服务事务。当前结果接入是开发版纵向链路，不能视为生产 SaaS
上传服务。

P3-13 交接记录为 `docs/handoffs/P3_13_RESULT_INGESTION_HANDOFF.md`。

### 10.17 P3-14 会话生命周期与终端控制契约

本轮先完成 P3-14 的传输无关契约切片，冻结后续 ConPTY、Windows Service 和 User Session Worker
适配层需要遵守的状态与错误语义：

- `session.terminal.resize` 加入共享 Pydantic/JSON Schema；尺寸限制为 1 至 1000 列/行，像素尺寸可选。
- `TerminalResizeEnforcer` 成为 Runner 的显式能力；没有 PTY 后端时返回
  `terminal_resize_runtime_unavailable`，不能把 resize 伪装成 stdin 或声称支持 ConPTY。
- `SessionLifecycleTransition` 记录 service、worker、用户会话和 Run 的状态变化、清理 Run、原因和时间。
- `MachineServiceLifecycle` 冻结服务启动、会话发现、Worker 启动/就绪、锁屏、解锁、注销、Worker
  退出、恢复、停止和本地紧急停止的动作与门禁。
- 用户注销会生成停止活动 Run 和停止 Worker 的动作；锁屏允许普通用户会话 Run 继续，但禁止新的
  `INTERACTIVE_DESKTOP` Run；桌面能力仍需单独审批。
- 本地恢复库新增幂等 `lifecycle_transitions` 表，`agentd session-worker-run` 会保存 Worker Broker
  产生的生命周期转移。

开发版验收：

```text
apps/api: 71 passed, 2 skipped
apps/agent: 73 passed, 1 skipped
compileall apps/api apps/agent packages: passed
所有 packages/*.schema.json 解析通过
terminal resize 在无 PTY 后端时 fail-closed 测试通过
Machine Service 生命周期、注销清理、故障恢复和紧急停止测试通过
```

P3-14 本轮仍未完成：真实 ConPTY、PTY 输入/输出语义和 resize 调用，Windows Service/Session 0
安装与用户会话发现，跨账户/锁屏/注销实机矩阵，桌面执行器、窗口范围和屏幕/输入审计。生产版仍
需要设备密钥环、真实 PostgreSQL/MinIO、Gateway 事务和 OS 级隔离。

P3-14 交接记录为 `docs/handoffs/P3_14_SESSION_LIFECYCLE_HANDOFF.md`。

### 10.18 P3-15 ConPTY Adapter

P3-15 已完成真实 Windows ConPTY 的开发版纵向链路：

- 安装并锁定可选依赖 `pywinpty>=2.0,<3`（仅 Windows）；`conpty-capability` CLI 报告 Windows Build、
  pywinpty 版本和可用性。
- `SessionRunStartPayload` 增加 `terminal_backend`（`PIPE`/`CONPTY`）和初始 `terminal_size`；
  `RunnerRequest`、`ProcessSpec`、Run Manifest 和本地 Run 状态保存这些字段。
- `ConPtyProcessSupervisor` 使用 pywinpty 的 ConPTY backend，支持合并 stdout、stdin、退出状态、
  取消和 `setwinsize`；`HybridProcessSupervisor` 按 Run 后端路由，禁止 CONPTY 静默降级为 PIPE。
- `ConPtyAdapter` 作为显式 Adapter 标记；无 Windows ConPTY、版本过低或依赖缺失时 fail-closed。
- `agentd session-worker-run` 同时注册 PIPE 和 ConPTY Adapter；默认 Adapter 不变，ConPTY 通过明确的
  Adapter ID 和 `terminal_backend=CONPTY` 选择。

开发版验收：

```text
ConPTY capability: Windows 11 build 26200 / pywinpty 2.0.15 / available=true
ConPTY tests: 8 passed, ResourceWarning-as-error passed
apps/agent full suite (截至 P3-16): 93 passed, 1 skipped
apps/api full suite: 71 passed, 2 skipped
compileall: passed
```

P3-15 仍未完成：真实 Codex/Claude CLI 兼容性矩阵、Prompt/工具事件、进程树资源隔离、Windows Service
Session 0 启动、桌面控制、ConPTY 在多账户/锁屏/注销/睡眠唤醒下的实机验收。生产环境仍需固定依赖
哈希/供应链策略、设备密钥环和 OS 级网络/资源隔离。

P3-15 交接记录为 `docs/handoffs/P3_15_CONPTY_ADAPTER_HANDOFF.md`。

### 10.19 P3-16 Windows Service/Session 0 Adapter

P3-16 已完成纯后端开发版适配切片，将服务控制、用户会话和 Worker 生命周期接入现有协议边界：

- `CtypesServiceControlBackend` 使用 Windows SCM API 实现服务安装、更新、卸载、启动、停止、状态查询和
  失败重启策略；`agentd service-install` 与 `service-control` 提供显式管理入口。
- `WindowsSessionProcessBackend` 使用 WTS 枚举会话和 `WTSQueryUserToken` 获取用户令牌，使用
  `CreateProcessAsUserW` 在目标用户 Session 启动 Worker；不把 Session 0 服务账户当成交互用户。
- Worker 创建后，服务通过受 Named Pipe OS 身份保护的 `session.hello` readiness probe 验证 Worker ID、
  用户 Session 和服务调用方；只有握手成功才记录 `worker.ready`。
- `SessionWorkerCoordinator` 把 WTS 会话状态、Worker 启动/退出、注销回收和显式恢复接入
  `MachineServiceLifecycle`，并记录 Worker PID、用户 SID、Windows Session ID、Pipe、命令和工作目录绑定。
- `CtypesServiceDispatcherBackend` 提供 `StartServiceCtrlDispatcherW`、停止/关机和
  `SERVICE_CONTROL_SESSIONCHANGE` 事件边界；`WindowsServiceHost` 持续巡检会话和 Worker 退出。
- `agentd service-host` 将服务 Dispatcher、WTS 会话后端、按会话绑定的 Worker 命令和 User Session Worker
  入口串联起来；Worker 命令支持解释器加脚本的完整启动前缀。
- 适配器使用 Fake 后端完成纯后端契约测试，不在自动化测试中安装系统服务。

开发版验收：

```text
P3-16 adapter tests: 13 passed
apps/agent full suite: 94 passed, 1 skipped
apps/api full suite: 71 passed, 2 skipped
compileall: passed; JSON Schema: passed
```

P3-16 仍未完成真实管理员权限服务安装/升级和回滚演练、Session 0 服务账户部署、Worker Pipe readiness
握手的真实进程验证、进程句柄/进程树生命周期、跨账户/跨 Session、锁屏/注销/睡眠唤醒实机矩阵，以及与真实 Gateway
长时间断线恢复的组合验收。完成这些项目之前，阶段 3 不能标记为生产退出。

P3-16 交接记录为 `docs/handoffs/P3_16_WINDOWS_SERVICE_SESSION0_HANDOFF.md`。

### 10.20 P3-17 外部 CLI Adapter 与 JSONL 事件接入

P3-17 完成了真实 Codex CLI 官方 `exec --json` 接口的开发版 Adapter 契约，并把语义事件接入
User Session Runtime 的统一事件队列：

- `apps/agent/cli_adapters.py` 提供无 Shell 的版本探测、兼容性矩阵、能力状态、Codex 命令构造和
  `CodexJsonlEventParser`；当前明确验证的 Codex 版本族为 `0.153.x`，本机实测 `codex-cli 0.153.4`。
- Codex Adapter 强制使用 `exec --json` 机器可读输出，默认加入 `--color never` 和 `--ephemeral`；
  拒绝自动批准、危险沙箱、工作区逃逸参数和未受控配置覆盖。
- 已支持 `thread.started`、`turn.started`、`turn.completed`、`turn.failed`、`error`、审批事件以及
  已登记的 `item.*` 类型，并映射为 `run.started`、`run.completed`、`run.failed`、`tool.*`、
  `approval.requested`、`file.changed` 和 `agent.message`。
- 未登记的顶层事件、Item 类型、非法 JSONL 和不完整结构都会 fail-closed；运行时会把该 Run 判为失败，
  不把未知输出当作普通成功文本。
- `ClaudeCodeAdapter` 只报告 `NOT_INSTALLED` 或 `UNSUPPORTED`，在其稳定机器协议尚未冻结前不伪造可执行支持。
- `agentd cli-capability --adapter codex|claude` 提供本地能力诊断；`LocalRunner` 在实际 Adapter 执行前
  要求通过兼容性探测。

开发版验收：

```text
CLI Adapter 契约测试：11 passed
Session Runtime JSONL 纵向测试：passed
apps/agent full suite：105 passed, 1 skipped
apps/api full suite：71 passed, 2 skipped
compileall：passed；Session JSON Schema：passed
```

P3-17 仍不等同于生产 CLI 交付：尚未在真实任务/认证环境执行完整 Codex 模型调用，尚未完成不同 Codex
版本族、操作系统和反向代理场景矩阵；审批事件的云端批准回传、进程树终止、资源/网络隔离、真实
Windows Service 组合验收和 Claude Code 语义适配仍未完成。交接记录为
`docs/handoffs/P3_17_CLI_ADAPTER_HANDOFF.md`。

### 10.21 P3-18 设备公钥 challenge/signature 持有证明

P3-18 将设备注册从“提交公钥并登记指纹”收紧为“证明设备实际持有对应 Ed25519 私钥”：

- `packages/device_identity` 统一定义注册上下文、长度分隔的签名消息、PEM/OpenSSH 公钥解析、
  DER 规范化公钥指纹、Base64URL 签名编解码和验签逻辑；API 与测试工具共用同一套规则。
- 创建设备 pairing 时生成一次性高熵 `challenge`。响应仅在创建结果中返回原始 challenge，数据库只保存
  `challenge_hash`；原始 pairing code 仍只保存 `code_hash`。
- 注册请求必须携带 `pairing_id`、`pairing_code`、`challenge`、`challenge_signature`、明确的
  `device_id` 和 Ed25519 公钥。签名绑定 pairing、challenge、agent、device 和规范化公钥指纹。
- SQLite Store 与 PostgreSQL Repository 都按 pairing ID 锁定/检查 pairing、校验 pairing code 和 challenge，
  通过验签后才创建设备；错误签名、错误 challenge、公钥替换、设备 ID 替换和重放均拒绝。
- `agentd device-register` 支持直接提交签名，或在本地读取 PEM 私钥临时签名；私钥不上传、不写入平台数据库。
  系统密钥环接入仍属于后续生产工作。
- PostgreSQL 增量迁移为 `008_device_registration_challenge.sql`；历史没有 challenge 的 pairing 不会被自动
  补造为可用 pairing，必须重新创建，避免把未知 challenge 误当成已验证身份。

开发版验收：

```text
设备身份专项测试：8 passed
apps/api full suite：75 passed, 2 skipped
apps/agent full suite：106 passed, 1 skipped
compileall（应用与共享包）：passed；领域 Schema：passed
```

P3-18 不等同于生产设备身份完成：其后的 P3-19 已提供 Windows Credential Manager 开发版适配，
但设备私钥/Token 的目标部署身份持久化、配对端点正式 OIDC/成员会话策略、PostgreSQL 真实迁移、RLS、
并发注册和设备 Token 轮换仍待验收。
交接记录为 `docs/handoffs/P3_18_DEVICE_CHALLENGE_HANDOFF.md`。

### 10.22 P3-19 系统凭据存储与 Agent 默认取证

P3-19 将设备 Token 的长期保存从命令行参数迁移到操作系统凭据边界：

- `apps/agent/credential_store.py` 定义 `CredentialStore`、稳定错误类型、设备 Token 目标名和严格的
  目标/秘密长度校验；`InMemoryCredentialStore` 仅用于测试或明确的开发注入。
- `WindowsCredentialManager` 使用 Windows `Advapi32 CredWriteW/CredReadW/CredDeleteW`，以 Generic
  Credential 和 `CRED_PERSIST_LOCAL_MACHINE` 保存 Token；读取后校验 UTF-8 和大小，删除不存在项返回
  `False`，权限、后端不可用和持久化失败均保留明确错误。
- `agentd credential-save` 支持隐藏输入或 `--token-stdin`；`credential-delete` 支持删除。目标默认由
  `MathAgentPlatform/device-token/<device_id>` 生成，也可以显式指定目标。
- `gateway-run` 和 `service-run` 在没有显式 `--device-token` 时按 `device_id` 从 Windows Credential
  Manager 读取；显式 Token 仅保留为开发兼容路径，读取的值不进入本地 SQLite 状态库。
- 持久化写入不会自动降级为会话级 Credential。当前开发机实测 `CRED_PERSIST_LOCAL_MACHINE` 返回
  `ERROR_NO_SUCH_LOGON_SESSION (1312)`，因此该环境不能宣称完成跨登录会话持久化；需要在目标部署身份和
  用户配置文件条件下重新验收。

开发版验收：

```text
凭据存储专项测试：8 passed
apps/agent full suite：114 passed, 1 skipped
```

P3-19 交接记录为 `docs/handoffs/P3_19_CREDENTIAL_STORE_HANDOFF.md`。生产退出仍依赖真实用户身份、
Machine Service 身份、Credential Manager ACL、重启/注销恢复、凭据轮换和丢失设备处置测试。

### 10.23 P3-20 设备 Token 轮换与连接撤销

P3-20 完成设备 Token 轮换的开发版服务端契约。轮换是一个管理员授权的原子操作：服务端锁定设备记录，
生成新的高熵 Token，替换数据库中的 Token 哈希和版本，撤销该设备的活动 Gateway 连接，并写入不含
明文 Token 的轮换审计记录。旧 Token 不进入过渡窗口，轮换提交后立即失效；新 Token 只通过本次响应
返回一次，调用方负责写入已验证的系统凭据存储。

实现范围：

- `Device` 增加 `token_version` 和 `token_rotated_at`；新增 `DeviceTokenRotateRequest`。
- SQLite `Store.rotate_device_token()` 使用 `BEGIN IMMEDIATE`，管理员权限、设备活动状态、哈希替换、连接撤销和审计记录在同一事务内完成。
- PostgreSQL `PostgresRepository.rotate_device_token()` 使用 `FOR UPDATE`，并通过 `009_device_token_rotation.sql` 增加字段、审计表和组织范围 RLS。
- 新增 `POST /api/devices/{device_id}/rotate-token`；接口不接受或回显旧 Token，不把新 Token 写入事件或普通日志。
- 轮换后的新 Token 可以正常 Gateway 认证；旧 Token、旧连接和已撤销设备不能继续工作。

开发版验收：

```text
apps/api：78 passed, 2 skipped
apps/agent：114 passed, 1 skipped
compileall apps/api/app apps/agent packages：passed
domain/gateway/session JSON Schema：passed
```

P3-20 不包含双 Token 过渡窗口、设备私钥轮换、自动写回 Windows Credential Manager、正式 OIDC、真实
PostgreSQL 并发和跨账户 Service 读取。轮换后的 Token 需要在目标 Machine Service/User Session Worker
身份下执行系统凭据更新和恢复演练后，才能进入生产退出评审。

交接记录为 `docs/handoffs/P3_20_DEVICE_TOKEN_ROTATION_HANDOFF.md`。

### 10.24 P3-22 Gateway 接力、接收与 Agent 复核

P3-22 补齐 Gateway 与结构化协作协议之间的开发版缺口，使 Agent 可以通过平台正式创建交接、接收指向自身的交接包，并提交不能替代真人批准的复核意见。

实现范围：

- `packages/agent_protocol`、领域 Schema 和默认项目能力 Token 新增 `agent.handoff.create`、`agent.handoff.accept` 和 `agent.review.submit`。
- `GatewayService` 校验项目能力、项目资源归属、交接接收人身份和 Agent 复核身份；Agent 提交 `APPROVED` 时稳定返回 `human_approval_required`。
- `PostgresRepository` 新增 `create_handoff()`、`accept_handoff()` 和 `create_review()`；Review 会在同一事务中更新目标状态、Gate、Review、Event、EventOutbox 和幂等记录。
- SQLite `Store` 的 Handoff 和 Review 幂等记录保存请求指纹；重复请求返回原对象，同一幂等键提交不同内容会被拒绝。
- Handoff 创建、接收和 Review 写入口均保持 `PlatformRepository` 协议可替换，后端不依赖具体 Gateway 实例。

开发版验收：

```text
apps/api：82 passed，3 skipped
apps/agent：114 passed，9 skipped
compileall apps/api/app apps/agent packages：passed
```

当前未完成：

- 未执行真实 PostgreSQL 连接池、RLS、并发幂等和事务回滚验收；相关入口并入 P3-21/后续基础设施回归。
- Handoff 的拒绝、返工、汇总和跨任务分发语义仍属于阶段 4；HTTP 接收接口仍需进一步收紧主体校验。
- SQLite Handoff 创建/接收目前先提交业务行，再单独写入事件；尚未像 Review 一样保证业务状态、事件和幂等记录的统一事务边界。
- Gateway 业务副作用与 Gateway 结果表在跨实例场景的统一事务协调仍需后续切片处理；本轮只完成 Handoff/Review Repository 写入口的事务边界。
- Artifact Gateway 命令、桌面控制、真实 NATS 投递和生产认证仍未实现。

交接记录为 `docs/handoffs/P3_22_GATEWAY_HANDOFF_REVIEW_HANDOFF.md`。

### 10.25 P3-23 阶段 3 开发版收尾

P3-23 完成阶段 3 开发版的最后一条 Gateway 纵向能力，并明确 Gateway 命令结果的幂等边界：

- 新增 `agent.artifact.create`，通过 Gateway 创建 Artifact 元数据；创建请求强制绑定当前 Agent、项目能力 Token、任务/Run/输入 Artifact 的项目归属。
- Artifact 内容不通过 JSON Gateway 传输，继续使用已有 Agent HTTP 单文件或 Multipart 上传通道，避免把大文件和断点续传塞进控制协议。
- `GatewayCommandResult` 增加不含项目能力 Token 的 `request_hash`；同一连接范围内复用幂等键但改变命令内容时返回 `gateway_command_request_mismatch`。
- PostgreSQL 新增迁移 `010_gateway_command_request_hash.sql`；SQLite 通过兼容列补齐同名字段。
- 阶段 3 开发版退出评审记录在 `docs/STAGE3_EXIT_REVIEW.md`，本轮交接记录在 `docs/handoffs/P3_23_STAGE3_DEV_EXIT_HANDOFF.md`。

开发版结论：`PASS_WITH_ASSUMPTIONS`。阶段 3 的开发协议、Gateway、本地 Agent、Runner/Adapter 和 Artifact 元数据边界可以交给阶段 4 继续开发；真实 PostgreSQL/MinIO、多实例事务、Windows Service、真实 CLI、NATS、OIDC 和 OS 级隔离仍不能标记为生产完成。

本轮执行了目标模块编译、Schema 文件解析和最小 Artifact/幂等冒烟；完整 API/Agent 回归、真实数据库和多实例并发测试按当前项目决策延期，并在交接文档中列明。

## 11. 阶段 4：任务编排、交接、审核与门禁

### 11.1 依赖

- 阶段 0 状态机和 Gate 契约。
- 阶段 1 权限。
- 阶段 3 Agent 执行协议。

### 11.2 工作包

- 任务父子关系和 DAG。
- 任务认领、重试、取消和租约冲突。
- 接力式、分发式和汇总式任务。
- Handoff 接收、拒绝、返工和归档。
- Review、Gate 和 Risk Registry。
- 机器审计、Agent 复核和人工批准。
- fatal、major、minor 规则。
- 项目阶段门禁。
- 数据驱动的前端任务、交接和审核中心。

### 11.3 验收

- 任务不能绕过前置依赖。
- 未接受交接不能推动接力流程。
- fatal 问题自动阻断下游。
- 修复并由真人批准后才能继续。
- 接力和分发两种完整流程均可运行。

### 11.4 P4-01 开发切片结果

P4-01 已完成开发版的核心后端契约：

- Handoff 增加 `REJECTED` 收据、拒绝原因、决定 findings、输入交接列表、修订来源和修订编号。
- 接收方拒绝后，原交接进入 `REJECTED + NEEDS_REVISION`，不能直接再次接受；新交接必须通过 `revision_of_handoff_id` 建立返工链。
- `AGGREGATE` Handoff 必须声明输入交接，所有输入必须已接受且在需要时通过 Handoff Gate。
- 下游 Task 领取除检查输入交接接受状态外，还检查 Handoff Gate 为 `PASSED`；因此人工门禁不能被接收动作绕过。
- Review findings 统一规范化为 `fatal`、`major`、`minor`；未解决 `fatal` 或 `major` 禁止 `APPROVED`，Gate 保存阻断 findings 和风险摘要。
- Review 保存 Evidence 引用，Gate 保存 Review/Evidence 引用；新增 `GET /api/projects/{project_id}/review-center` 聚合审核中心所需数据和风险登记项。
- SQLite、PostgreSQL Schema/Repository、Gateway `agent.handoff.reject` 和 Agent Client 均已同步；PostgreSQL 结构通过迁移 `011_workflow_review_relations.sql` 扩展。

P4-01 仍属于开发版：真实 PostgreSQL/MinIO、多实例竞争、正式 OIDC 主体认证和前端审核中心验收延期。

### 11.5 P4-02 开发切片结果

P4-02 已完成开发版的工作流完整性补齐：

- `FANOUT` Handoff 会为每一个接收方创建独立 `HandoffReceipt`；单个接收方确认不会把整体交接提前标记为 `ACCEPTED`，只有全部收据接受后才可推动下游；任一接收方拒绝会保留独立拒绝原因并令整体进入返工状态。
- 新增 `handoff_receipts` 表、`Handoff.receipts` 契约和迁移 `012_p4_02_receipts_risks_gate_snapshots.sql`；旧单收据数据在 SQLite 启动和 PostgreSQL 迁移中提供兼容回填。
- Review finding 会产生独立 Risk Registry 记录。风险必须通过 `ASSIGN`、带理由和 Evidence 的 `RESOLVE`、或 `REOPEN` 操作变更；仅提交一条没有 finding 的新 Review 不能清除未解决的 fatal/major 风险。
- Gate 保存目标、Review、Evidence 和输入成果物的快照；发现目标输入或 Evidence 源发生变化时，已通过 Gate 自动转为 `INVALIDATED`，清除批准主体并留下系统事件。
- HTTP Agent Handoff 接收/拒绝路由已使用 `X-Agent-Id` 和项目能力 Token，且服务端从收据而不是请求体决定接收方身份。
- SQLite、PostgreSQL Repository、领域 Schema、Bundle 导出恢复、OpenAPI 和纯后端工作流测试已同步；当前 API 全量回归为 90 项通过、3 项真实基础设施条件测试跳过。

P4-02 仍属于开发版：真实 PostgreSQL/RLS/并发收据与风险事务、正式设备会话/OIDC、NATS/多实例 Gateway 事务、前端审核中心和大规模恢复演练延期到后续验收批次。

### 11.6 P4-03 审核中心数据化与 Gate 失效传播

P4-03 完成阶段 4 的开发版可视化与失效传播切片：

- `ReviewCenter` 响应增加项目 Handoff 集合，前端审核中心从接口读取 Gate、Review、Evidence、Risk 和 Fanout 收据，不再使用写死的审核条目。
- 审核中心展示 Gate 的 `OPEN`、`PASSED`、`FAILED`、`BLOCKED`、`INVALIDATED` 状态及失效原因，并提供风险责任人分配、带项目 Evidence 的关闭和重新打开操作。
- SQLite/PostgreSQL Repository 增加 Gate 影响图遍历，沿任务依赖、成果物派生、交接汇总和任务产出的交接寻找受影响任务。
- Gate 因输入快照变化或 fatal/major 风险重新打开而失效时，受影响的 `READY`、`CLAIMED`、`RUNNING` 任务进入 `BLOCKED`；已通过或等待复核的任务进入 `NEEDS_REVISION`，并写入 `task.upstream_gate_invalidated` 事件。
- 已通过的下游 Task Gate 同步转为 `INVALIDATED`，清除批准主体，避免旧任务结果继续被误认为正式有效。

开发版结论：`PASS_WITH_ASSUMPTIONS`。本轮已完成前端构建和 SQLite 回归；真实 PostgreSQL/RLS、跨实例并发、多实例失效消费者、浏览器视觉回归和生产认证仍未完成。

### 11.7 P4-04 真实基础设施验收准备与并发收紧

P4-04 已完成可执行的真实 PostgreSQL/MinIO 验收入口和验收前必须修复的数据库边界：

- 新增迁移 `013_p4_04_force_rls_and_event_idempotency.sql`，对全部租户表执行 `FORCE ROW LEVEL SECURITY`，避免开发部署中表所有者绕过 RLS。
- 为历史遗漏的 agents、sessions、idempotency_records 补齐租户策略；幂等记录增加组织范围并在 Repository 中使用组织命名空间，避免跨租户幂等键冲突和响应泄漏。
- PostgreSQL 工作流写入统一先锁项目，再执行幂等检查、Handoff 收据、Risk、Review、Gate 和 Event/Outbox 副作用；事件序号和 keyed event 在多实例间按项目串行。
- Gate 创建改为冲突安全插入，Gate 失效刷新在行锁下重新检查；审核中心读取先锁项目，避免两个实例同时失效同一 Gate 或产生重复传播事件。
- 新增 `test_stage4_integration.py` 和 `scripts/test-stage4-integration.ps1`，覆盖迁移幂等、强制 RLS、跨租户读取、Fanout 并发确认、Risk 幂等、Gate 传播、Outbox `SKIP LOCKED` 领取和 MinIO 跨实例 Multipart。

当前开发回归为 API 95 项通过、7 项条件测试跳过，其中新增 P4-04 条件测试 4 项。当前机器没有 Docker、Podman、PostgreSQL、psql 或 MinIO，因此本轮结论仍为 `PASS_WITH_ASSUMPTIONS`：验收代码与入口完成，但真实数据库和对象存储结果尚未取得，P4-04 不能标记为生产通过。

### 11.8 P4-05 浏览器端到端验收准备与审核中心联调

P4-05 本轮完成浏览器验收所需的前端数据驱动和稳定定位准备：

- 侧栏的项目、任务、交接和在线 Agent 数量改为从当前项目和审核中心数据派生，不再使用写死的示例数字。
- 审核门禁的待审核状态、任务关注项和交接关注项与实际 API 返回保持同一数据源；没有待处理项时不显示误导性的数量徽标。
- 项目总览、任务流、任务列表、审核中心、风险登记、交接收据和导航入口增加稳定 `data-testid`，为后续浏览器自动化和人工验收提供定位契约。
- 移动端导航的所有锚点点击都会关闭抽屉，遮罩仍可关闭导航；Agent 在线数量和最近心跳时间来自项目 Agent 数据。
- `apps/web` 的 Next.js 生产构建通过，审核中心真实 API 的类型和渲染链路保持可编译。

本轮完成浏览器验收准备、本地 Web 页面加载和定位冒烟；当前可访问的开发 API 返回空项目列表，因此只验证了空状态，尚未完成有数据的真实 API 联调、真实数据库环境下的浏览器验收、移动端视觉回归和 Playwright 自动化。开发版结论：`PASS_WITH_ASSUMPTIONS`。

## 12. 阶段 5：Runner、可复现性与信息边界审计

### 12.1 依赖

- 阶段 2 成果物存储。
- 阶段 3 本地 Agent。
- 阶段 4 正式门禁。

### 12.2 工作包

- Python Runner 接口。
- Docker/Podman 隔离后端。
- CPU、内存、网络、路径和超时限制。
- 环境镜像、dependency lock、参数和随机种子。
- stdout、stderr、exit code 和输出归档。
- 实际读取文件和网络外发记录。
- 未来信息、单位、约束和结果审计。
- 可复现重跑。
- 审计结果自动创建 Review 和 Gate finding。

### 12.3 验收

- 相同输入、环境和随机种子可以重现结果。
- 未声明或未来数据读取被记录并阻断。
- 失败运行不能成为正式成果物来源。
- Run、代码、数据和结果之间可完整追溯。

### 12.4 P5-01 可复现执行上下文与 Manifest

P5-01 完成阶段 5 的第一条开发切片：

- `RunnerRequest` 增加 `source_commit`、`environment_image_digest`、`dependency_lock`、`parameters`、`random_seed`、模型信息、工具版本、数据访问策略和已观察输入文件字段，并在创建请求时做基础类型校验。
- `SessionRunStartPayload` 与共享 `session.schema.json` 同步这些执行上下文，`SessionWorkerRuntime` 将它们传入本地 Runner，避免会话层执行和平台 Run 记录使用两套未关联的元数据。
- `RunManifestBuilder` 升级为 `schema_version=1.1`，记录复现上下文、声明/观察/未声明输入集合、信息边界状态、stdout/stderr 字节数和 Manifest 自身 SHA-256。
- 输入存在声明但没有观察记录时，Manifest 明确记录 `input_observation_not_captured`，不把“未观测”静默当成“已审计”。

本轮只完成执行上下文和 Manifest 的开发版固化，没有实现 Docker/Podman 隔离、系统调用级文件访问跟踪、CPU/内存/网络硬限制或真实环境重跑。开发版结论：`PASS_WITH_ASSUMPTIONS`。

### 12.5 P5-02 Docker/Podman 容器执行边界

P5-02 新增独立的 `apps/agent/container_runner.py` 开发版后端：

- `ContainerRuntime` 支持 Docker/Podman 可执行文件解析；运行时不存在时返回明确错误，不回退到宿主机 Runner。
- `ContainerPolicy` 校验镜像 digest、HEADLESS 执行模式、默认拒绝网络、工作区根目录和输入/输出路径范围。
- `ContainerCommandBuilder` 生成无 Shell 的 `run` 参数，默认使用 `--network none`、`--read-only`、`--cap-drop=ALL`、`no-new-privileges`、CPU、内存、PIDs 和工作区 bind mount。
- `ContainerInvocation` 可将运行时、镜像、网络模式、只读策略和资源限制序列化到后续 Run Manifest。
- `ContainerRunner` 复用本地进程监督和 Run/Task/Agent/Device/Workspace 归属，不改变现有 `LocalRunner` 的宿主执行语义。

当前只完成容器后端契约和命令构造，尚未在 Docker/Podman 中实跑，尚未把 Session Worker 默认路由切换到容器，也未完成 Windows 容器、挂载权限、进程树和网络限制的真实验收。开发版结论：`PASS_WITH_ASSUMPTIONS`。

### 12.6 P5-03 容器路由、分层挂载与 Manifest 接入

P5-03 将 P5-02 的容器后端接入 Session Worker，但不改变默认宿主机执行：

- `SessionRunStartPayload` 和 `RunnerRequest` 增加 `execution_backend`、`container_runtime`、`container_image` 和 `execution_details`；宿主机请求携带容器字段会被拒绝，容器请求必须明确运行时和镜像。
- `SessionWorkerRuntime` 根据执行后端选择宿主机 `LocalRunner` 或已配置的 Docker/Podman `ContainerRunner`。容器运行时未配置、镜像不满足 digest 策略或运行模式不支持时 fail-closed，不回退到宿主机。
- `ContainerPolicy.plan_mounts()` 固定将工作区挂载为只读，并仅将输出文件所在的工作区子目录以 `rw` 挂载；工作区根目录输出和输入/可写输出目录重叠均拒绝。
- `ContainerInvocation.as_manifest()` 接入运行上下文。Manifest 记录运行时、镜像、工作负载命令、资源限制、挂载目标和环境变量名，但不记录环境变量值；`RunManifestBuilder` 将其放入 `execution` 字段。
- `agentd session-worker-run --container-runtime docker|podman` 可显式启用容器后端，并为该 Worker 增加 HEADLESS 执行能力；不传该参数时维持原有 User Session 宿主机能力。
- 新增容器挂载、环境脱敏、输出目录门禁和 Session 后端选择契约测试；宿主机 Session、ConPTY、Named Pipe、CLI、结果上传等回归保持通过。

P5-03 的开发版结论为 `PASS_WITH_ASSUMPTIONS`。当前仍未完成真实 Docker/Podman 启动、Windows Docker Desktop/Podman rootless 挂载验证、容器内系统调用级文件访问观察、真实容器输出 Artifact 上传组合验收和平台 Run 表对执行后端的结构化登记。

### 12.7 P5-04 信息边界审计内核

P5-04 建立与操作系统追踪器解耦的信息边界审计内核：

- `FileObservation` 统一描述路径、读写方向、观测来源、数据可用时间和数据时间范围。
- `InformationBoundaryAudit` 检查声明输入与实际读取是否一致、读取是否越出工作区、数据是否晚于决策时间或超过数据截止时间，并生成 `fatal`/`major` finding。
- `observation_mode=system` 时，没有读取记录、观测来源不是系统追踪器或缺失必要时间元数据均不能通过；未配置操作系统观察器不会被当作“无违规”。
- `FailClosedObservationController` 提供后续 Windows ETW、Linux eBPF 或容器审计适配器的统一接入口；观察器不存在、启动失败或没有返回记录时返回 `not_captured`。
- `RunManifestBuilder` 使用该审计内核生成 `information_boundary`，并从 `data_access_policy.observed_files` 读取可用时间/数据截止元数据；审计结果随 Run Manifest 固化。

P5-04 的开发版结论为 `PASS_WITH_ASSUMPTIONS`。本轮完成的是确定性审计内核和 fail-closed 观察器接口，不是操作系统级文件访问追踪器；真实未来数据阻断、系统调用记录、网络外发观察和 Review/Gate 自动写入仍待后续切片。

### 12.8 P5-05 可复现重跑入口

P5-05 已完成开发版重跑规划与比较切片，目标是从已通过信息边界审计的 Run Manifest 创建一个新的、可审计的 Run 请求，而不是复用原 Run 身份或隐式使用当前工作区状态。

实现证据：

- `apps/agent/reproducible_rerun.py`：`ReproducibleReplayPlanner`、`ReplayComparator`、`ReplayExecutor`。
- `apps/agent/result_uploader.py`：稳定复现哈希和工作区路径规范化。
- `apps/agent/test_reproducible_rerun.py`：8 项 P5-05 契约测试。

本轮已固化的规则：

- 源 Manifest 的 `information_boundary.allowed` 必须为 `true`，否则拒绝生成重跑计划。
- 源 Manifest 必须包含与内容一致的稳定复现哈希；内容被修改但哈希未同步时，重跑计划直接拒绝。
- 新 Run 必须使用调用方提供的全新 `run_id`；代码提交、环境镜像 digest、dependency lock、参数、随机种子、模型和工具版本、数据访问策略均从源 Manifest 还原。
- 环境变量只在调用方显式提供且名称集合与源 Manifest 完全一致时允许重跑；Manifest 不保存环境变量值。
- 输入、输出、观测文件、数据访问元数据、命令中的工作区绝对路径和容器工作负载命令会在工作区迁移时重新绑定；工作区外的绝对路径不会被静默放行。
- 稳定复现哈希排除 Run ID、工作区绝对路径、进程 ID/状态/输出流动态摘要、自身哈希和运行时命令字段，同时把工作区内路径规范化为相对路径，避免仅因换工作区而误报不一致。
- 比较器同时检查输出文件哈希、稳定复现哈希和重跑的信息边界状态；输出缺失、意外、内容变化或边界未通过时，比较结果不匹配，只能进入待复核流程。
- 容器重跑保留 `host/container`、Docker/Podman 和镜像 digest 等执行上下文，不会因容器运行时不可用而回退宿主机。

验证结果：

```text
Agent P5-05 contract tests       -> 8 passed
Agent full unittest discover     -> 134 passed, 1 conditional skipped
py_compile target modules        -> passed
```

本轮仍未完成：平台 API 创建新 Run 和正式 Artifact 快照绑定、真实宿主机/容器重跑、Docker/Podman 实跑、Windows/Linux 系统级文件与网络观察器、自动创建 Review/Gate，以及哈希不一致后的平台状态写入。因此 P5-05 结论为 `PASS_WITH_ASSUMPTIONS`，不能标记为生产可复现执行完成。

### 12.9 P5-06 文件/网络访问观察契约与传递链

P5-06 本轮完成开发版观察契约和数据传递链，暂不宣称已经完成 Windows/Linux/容器系统调用级追踪。

实现证据：

- `apps/agent/access_observer.py`：统一 JSONL 观察事件格式、文件/网络事件归一化、线程安全记录器和外部观察桥接入口。
- `apps/agent/information_boundary.py`：`NetworkObservation`、组合观察捕获、fail-closed 捕获控制和网络策略审计。
- `apps/agent/runner.py`：`RunnerRequest.observed_network_connections`。
- `packages/agent_protocol/__init__.py`、`packages/agent_protocol/session.schema.json`：Session 启动载荷中的网络观测字段。
- `apps/agent/session_runtime.py`、`apps/agent/result_uploader.py`、`apps/agent/reproducible_rerun.py`：网络观测从 Session/Runner 进入 Manifest，并在重跑计划中恢复。

本轮冻结的事件形态：

```json
{
  "type": "file.read",
  "path": "input/data.csv",
  "process_id": "...",
  "observation_source": "system"
}
```

```json
{
  "type": "network.connect",
  "host": "api.example.com",
  "port": 443,
  "protocol": "tcp",
  "direction": "egress",
  "process_id": "...",
  "observation_source": "system"
}
```

网络审计规则：

- `deny-by-default` 下出现出站连接生成 `fatal / network_egress_not_allowed`。
- `allow-listed` 下连接目标不在允许主机集合中生成 `fatal / network_host_not_allowlisted`。
- 观察来源不是 `system` 时生成 `major / system_network_observation_incomplete`。
- 要求系统网络观察但没有记录时生成 `major / network_observation_not_captured`。
- 观察器缺失、异常、返回非法事件或没有事件时保持 fail-closed，不把“没有记录”解释为“没有访问”。

本轮未完成：真实 Windows ETW/minifilter、Linux eBPF/LSM、容器内系统调用采集、进程树自动绑定、真实网络阻断、容器运行时事件桥接和平台 Review/Gate 自动写入。本节记录的是 P5-06 契约切片；P5-06-REAL 的 Windows 开发版结果见下一节。

### 12.10 P5-06-REAL Windows ETW 开发版观察器

P5-06-REAL 完成了当前 Windows 开发机可实现的第一条真实 OS 观察链路，但结论仍为开发版 `PASS_WITH_ASSUMPTIONS`：

- 新增 `apps/agent/windows_etw_observer.py`，使用 Windows 原生 `logman` 分别创建 Kernel File 和 Kernel Network 两个单 Provider ETW 会话，启用
  `Microsoft-Windows-Kernel-File` 和 `Microsoft-Windows-Kernel-Network`，进程结束后使用
  `tracerpt` 转换 CSV，再归一化为 `FileObservation` 和 `NetworkObservation`。
- `LocalProcessSupervisor.operating_system_pid()` 暴露实际子进程 PID；`LocalRunner` 在创建真实进程前启动观察器，创建后绑定实际 PID，结束后停止采集并把观察结果放入 `ProcessResult`。
- `WindowsProcessTree` 使用 Toolhelp32Snapshot 枚举父子进程；绑定后通过后台轮询累积运行期间出现的子进程 PID，解析阶段只接受该进程树内的事件。
- `SessionWorkerRuntime` 将 `ProcessResult` 中的实际观察文件和网络记录回填到有效 `RunnerRequest`，因此 `RunManifestBuilder` 会使用系统观察结果生成信息边界和网络审计，同时记录采集状态/原因。
- ETW 会话删除、未绑定进程拒绝、工具缺失、启动/停止/解析失败和 CSV 缺失均保持 fail-closed；没有自动回退到声明式“安全”。
- `agentd session-worker-run --access-observer windows-etw` 提供显式启用入口，`--etw-output-directory` 可保留 ETL/CSV 诊断；默认 `none`，不会改变既有运行行为。
- 新增 `apps/agent/test_windows_etw_observer.py`，覆盖 ETW CSV 解析、进程树 PID 过滤、未绑定拒绝、平台不可用和 Runner 实际 PID 绑定。

验证结果：

```text
Windows ETW observer tests       -> 5 passed
Agent full unittest discover    -> 139 passed, 1 skipped
target modules py_compile        -> passed
Windows inbox capability probe   -> logman/tracerpt available; kernel providers queryable
```

本轮真实冒烟已确认当前普通执行身份调用单 Provider `logman` 返回 `Access is denied`；因此不能宣称以下能力已完成：管理员权限下的真实 ETW 实机采集结果、不同 Windows build 的事件字段稳定性、对已退出子进程的完整历史发现、文件系统 minifilter 级别保证、容器 PID namespace/挂载路径映射、真实网络阻断、跨进程长时间恢复以及自动创建 Review/Gate。`tracerpt` CSV 解析是版本相关的开发版适配，正式生产退出必须建立事件样本和权限矩阵。

## 13. 阶段 6：CUMCM 正式领域工作流包

### 13.1 依赖

- 阶段 2 导入导出。
- 阶段 4 任务和门禁。
- 阶段 5 Runner 和审计。

### 13.2 工作包

- CUMCM pack manifest 和版本升级规则。
- 题面事实、数据画像、四问任务 DAG。
- 建模、代码、实验、复核和论文模板。
- 现有 `competition-workflow` 脚本适配。
- 结果表和官方模板校验。
- 信息边界审计 Gate。
- 现有 C 题交接包无损或可解释导入。
- 项目模板导出。

### 13.3 验收

- 现有 C 题项目可以导入、继续执行和导出。
- 四问成果物、运行和审核关系完整。
- 数模审计脚本可以生成机器 Review。
- 模板特有字段不侵入平台通用内核。

## 14. 阶段 7：Git、协同文档与证据图谱

### 14.1 依赖

- 阶段 1 权限。
- 阶段 2 Git 和 Artifact。
- 阶段 4 Review 和 Gate。

### 14.2 工作包

- Markdown/Tiptap 编辑器。
- Yjs/Hocuspocus 实时协作。
- 评论、建议和文档快照。
- Git 分支、提交、Diff 和合并确认。
- 草稿、提交和批准三层版本。
- Evidence 对象和证据链查询。
- 结论、图表、运行和文档段落关系。
- 数据驱动证据图谱界面。

### 14.3 验收

- 多人可以同时编辑分析报告。
- 正式版本可以追溯到成员、Agent、任务和 Git commit。
- 修改结果后能够定位受影响的图表和段落。
- 实时草稿不能绕过正式版本门禁。

## 15. 阶段 8：报告、论文、PPT 与最终交付

### 15.1 依赖

- 阶段 5 正式运行结果。
- 阶段 6 领域模板。
- 阶段 7 文档和 Evidence。

### 15.2 工作包

- 批准成果物驱动的大纲和内容装配。
- LaTeX/Word 编译。
- Marp/Slidev，后续 PptxGenJS。
- 图表清单、引用和公式检查。
- 页数、匿名、格式和附件检查。
- 最终提交包生成和冻结。
- 交付版本签名和校验清单。
- 跨部署恢复验证。

### 15.3 验收

- 未批准结果无法进入正式论文或 PPT。
- 每个关键结论均可追溯到数据、代码、运行和审核。
- 文档可以从批准素材重新生成。
- 最终提交包可在另一套部署恢复。

## 16. 阶段 9：稳定性、部署、配额与 SaaS 化

### 16.1 依赖

- 阶段 1 至阶段 8 的核心模型稳定。

### 16.2 工作包

- PostgreSQL RLS 加固。
- 事务 outbox 和 NATS JetStream。
- 长流程成熟后引入 Temporal。
- OpenTelemetry、Prometheus、Grafana 和模型追踪。
- 备份、恢复和灾难演练。
- 组织、队伍和项目配额。
- Token、运行、存储和成本统计。
- Docker Compose 私有部署。
- Helm/Kubernetes 部署边界。
- 更多赛事模板和企业身份。
- 真实使用数据出现后再设计计费。

### 16.3 验收

- 备份恢复和灾难演练通过。
- 多队伍并发时不存在数据越权。
- 消息重复投递不会重复确认业务操作。
- 平台可自托管并可观察关键运行状态。

## 17. 首批端到端验收场景

### E2E-01：接力式协作

```text
成员创建任务 A
-> Agent A 领取并提交分析成果物
-> Agent A 创建 PASS_WITH_ASSUMPTIONS 交接
-> 人员或 Reviewer 确认交接
-> Agent B 使用批准输入完成代码和 Run
-> Agent C 独立复核
-> 人员通过最终门禁
```

通过条件：

- B 不需要读取 A 的完整聊天记录。
- 所有输入、结论、假设和风险来自正式交接与成果物。
- Agent C 能定位原始运行、代码和输入。
- Agent 不能代替人员批准最终结果。

### E2E-02：分发式协作

```text
主任务拆为 B、C 两个并行子任务
-> 两个 Agent 分别领取
-> 分别提交成果物和 Run
-> 汇总任务等待 B、C 均批准
-> 汇总 Agent 比较结果并生成冲突清单
-> Reviewer 复核
```

通过条件：

- 子任务可以并行执行。
- 任一前置任务未批准时，汇总任务不可领取。
- 重复提交不会生成重复成果物或重复批准。
- 冲突不会被静默覆盖。

### E2E-03：门禁阻断与修复

- 预置一个 fatal 问题。
- Agent Review 发现问题并阻断下游。
- 原 Agent 提交修订版本。
- 独立复核确认问题已修复。
- 人员完成批准。

通过条件：未修复前，论文任务和正式导出都不能引用该结果。

### E2E-04：信息边界违规

- 任务声明只允许使用某时点前可获得的预测数据。
- Runner 读取未来真实数据。
- 审计记录实际读取文件。
- Run 被标记为 BLOCKED。

通过条件：违规运行不能生成可被下游正式引用的结果。

### E2E-05：断线、租约和幂等

- Agent 领取任务后断线。
- 租约到期后另一 Agent 领取。
- 原 Agent 恢复并尝试回传旧结果。
- 客户端重复发送相同幂等键。

通过条件：平台不丢失记录、不重复确认，也不会让旧结果覆盖新执行。

### E2E-06：项目导出与恢复

- 导出项目 manifest、对象、Git 引用和校验清单。
- 在干净数据库和对象存储中导入。
- 校验任务、成果物、运行、审核和事件。

通过条件：恢复后的正式关系和哈希与原项目一致。

## 18. 全局测试要求

- Pydantic 与 JSON Schema 一致性测试。
- API 和数据库迁移测试。
- 状态机性质测试。
- 多租户越权测试。
- Agent 重放、断线、租约和重复提交测试。
- 文件哈希和版本一致性测试。
- Artifact 下游引用门禁测试。
- Run 可复现测试。
- 未来数据和未声明输入测试。
- 路径越权和网络外发测试。
- 并发编辑和文档版本测试。
- 导出恢复测试。
- 论文和提交包完整性测试。
- 压力、备份和灾难恢复测试。

## 19. 每轮任务执行规则

每一轮开发只处理一个明确工作包或一组强相关任务。执行顺序固定为：

1. 阅读本文档和上一轮交接。
2. 明确本轮任务 ID、目标、范围和非目标。
3. 检查当前代码和已有未提交变化。
4. 实施代码、迁移、测试和文档。
5. 运行与风险相匹配的验证。
6. 更新本计划中的任务状态。
7. 更新 `IMPLEMENTATION_STATUS.md`。
8. 写入本轮交接文档。

任何阶段不得因为“页面看起来完成”而跳过契约、权限、测试或交接。

## 20. 强制交接文档规范

交接目录：

```text
docs/handoffs/
```

文件名：

```text
P{阶段编号}_{轮次编号}_{主题}_HANDOFF.md
```

示例：

```text
P0_01_SCHEMA_AND_GATE_HANDOFF.md
P1_02_TENANT_AUTH_HANDOFF.md
```

每份交接文档必须包含：

1. 本轮目标和任务 ID。
2. 实际完成内容。
3. 修改文件清单。
4. 数据库或协议变化。
5. 关键决策和理由。
6. 已运行测试和结果。
7. 没有完成的内容。
8. 已知问题和风险。
9. 下一轮建议任务。
10. 启动、迁移、复现或回滚注意事项。

交接状态使用：

```text
PASS
PASS_WITH_ASSUMPTIONS
NEEDS_REVISION
BLOCKED
```

没有交接文档的开发轮次不能标记为完成。

## 21. 计划状态维护规则

任务状态统一使用：

```text
DONE
PARTIAL
READY
IN_PROGRESS
BLOCKED
DEFERRED
```

状态解释：

| 状态 | 含义 |
| --- | --- |
| DONE | 输出、测试、文档和交接均完成 |
| PARTIAL | 已有实现，但未满足正式验收 |
| READY | 依赖已满足，可以进入开发 |
| IN_PROGRESS | 当前轮次正在执行 |
| BLOCKED | 存在明确阻塞条件，交接中必须记录 |
| DEFERRED | 主动延期，已记录原因和恢复条件 |

更新规则：

- 每轮结束时更新任务状态和验证结果。
- 范围变化必须记录在版本记录中。
- 阶段验收失败时不得将后续阶段标记为正式开始。
- 可以提前做技术验证，但只能标记为 PARTIAL。
- 任何破坏性 Schema 变化必须附迁移方案。

## 22. 当前任务队列

### 当前里程碑

阶段 5：Runner、可复现性与信息边界审计开发；P3-05 至 P3-23、P4-01 至 P4-05、P5-01 至 P5-06
已完成开发版切片或验收准备。真实 Windows Service/Session 0、真实 CLI 任务、PostgreSQL/RLS、多实例事务、
真实容器、OS 级文件/网络观察器和生产基础设施仍作为延期验收项，不得将开发版结果标记为生产退出。

### 本轮已完成

```text
P2-04 PostgreSQL Repository 第一批读写、连接池和事务边界（开发版，PARTIAL）
P2-04 HTTP `Idempotency-Key`、请求指纹和项目/对象路径授权（开发版，PARTIAL）
P2-04 写接口主体记账和越权测试
P2-05 Event Outbox 领域契约、事务收紧、原子领取、Dispatcher 和失败退避（开发版，PARTIAL）
P2-05 事务回滚、锁过期、恢复补建和 Dispatcher 测试
此前阶段基线：46 项常规 unittest、compileall、API/OpenAPI 导入
P3-05 项目能力 Token 强制接入任务领取、租约、进度/结果和 Run 写路径
P3-05 跨项目、错误 Agent、能力不足和租约项目绑定纯后端契约测试
P3-06 基线：63 项后端 unittest 通过，7 项本地 Agent unittest 通过，2 项真实 PostgreSQL/MinIO 集成测试跳过
P3-06 Gateway 任务/租约/进度/结果和 Run 命令，成功结果随 ACK 回传
P3-06 Gateway 命令鉴权、资源归属和本地客户端 command_result 契约测试
P3-07 Gateway 命令结果持久化、重复命令结果恢复和错误结果恢复
P3-07 packages/agent_protocol 跨端协议包、独立 JSON Schema 和 API/Agent 接入
P3-08 Machine Agent Service、自动重连、心跳调度、子进程监督和本地紧急停止
P3-08 agentd service-run、service-emergency-stop、service-clear-emergency-stop
P3-09 Runner/Adapter 基础契约、PythonAdapter、工作区/命令/环境/网络策略校验
P3-10 User Session Worker/本机 IPC 传输无关 Broker、会话权限和生命周期契约
P3-11 Windows Named Pipe、ACL、PID/SID/Windows Session 身份认证和多请求传输
P3-12 User Session Worker Runtime、标准管道 Runner、stdin 和本地事件编排
P3-13 Agent 事件上传、输出文件哈希、Run Manifest、Artifact/Multipart 和 Run 结果接入
P3-14 `session.terminal.resize`、TerminalSize、SessionLifecycleTransition 和 MachineServiceLifecycle
P3-14 本地生命周期审计持久化、注销/锁屏/恢复/紧急停止/无 PTY fail-closed 测试
P3-15 ConPTY 监督器、Hybrid 路由、ConPTY Adapter、能力探测和 Runtime resize 纵向测试
P3-16 Windows Service/Session 0 的 SCM、WTS、用户 Token、Worker Coordinator 和 Dispatcher 开发版适配
P3-17 Codex/Claude CLI Adapter 能力探测、Codex JSONL 解析、Runtime 事件映射和 CLI 诊断入口
P3-18 设备 pairing challenge、Ed25519 私钥持有证明、规范化指纹和注册 CLI 参数
P3-19 Windows Credential Manager、凭据保存/删除 CLI 和默认取证
P3-20 设备 Token 轮换、旧 Token 失效、连接撤销和无秘密值审计记录
P3-22 Gateway Handoff 创建/接收、Agent Review 提交、PostgreSQL 写入口和跨后端幂等语义
P3-23 Artifact 元数据 Gateway 命令、Gateway 请求指纹和阶段 3 开发版退出评审
P3-22 基线：API 82 项通过、3 项真实基础设施跳过；Agent 114 项通过、9 项条件测试跳过
P3-23 目标模块 `py_compile`、迁移/Schema 文件解析：通过
P4-03 审核中心真实数据接入、风险操作、Fanout 收据展示和 Gate 失效影响图传播
P4-03 API 回归：91 项通过、3 项真实基础设施跳过；Next.js 生产构建：通过
P4-04 API 回归：95 项通过、7 项条件测试跳过；Stage 4 集成入口：4 项因无真实服务跳过
P5-03 容器执行后端协议、Session 路由、只读工作区/独立输出挂载和 Manifest 脱敏接入；Agent 回归：119 项通过、1 项条件跳过
P5-04 信息边界审计内核、未来数据/未声明输入规则、观察器 fail-closed 接口和 Manifest 接入；专项及 Manifest 集成测试 6 项通过
P5-05 可复现重跑规划、工作区路径迁移、稳定复现哈希自校验、输出差异和信息边界门禁；Agent 回归 134 项通过、1 项条件跳过
P5-06 文件/网络观察契约、JSONL 事件归一化、fail-closed 捕获和 Runner/Session/Manifest 传递链；P5-06-REAL Windows ETW 开发版已接入，真实管理员权限/事件矩阵待验收
```

### 下一轮正式任务

```text
P5-06-REAL-ADMIN-ETW 管理员权限真实采集：需一次交互式 UAC 提升会话（当前过滤令牌已取证为硬阻塞），完成后冻结真实 ETL/CSV 样本、事件字段矩阵并增加固定样本回归；随后推进 Linux eBPF/LSM、容器观测桥接和网络阻断
阶段 6 数学建模模板接入平台：pack 列表/详情/应用/导出/校验 HTTP 端点与 Web 模板面板，随后补项目模板导出、信息边界审计 Gate、机器 Review 与现有 C 题交接包导入
阶段 7 文档模板三层版本（草稿/提交/批准）与 Evidence 关系
P4-04-RUN-PROD-PROD 生产收尾：TLS/反向代理、跨实例部署、连接池上限压测、运行时角色密码轮换与密钥托管、备份与回滚演练（非 owner 运行时角色、scram-sha-256 密码认证、连接池故障演练已完成）
P4-05 浏览器端到端验收、真实 API 联调和阶段 4 开发版退出评审
设备系统密钥环目标身份验收、轮换后的凭据写回/恢复和正式设备会话认证
阶段 3 P3-16 Windows Service/Session 0 实机验证、真实 Codex 任务矩阵、Claude 语义适配和多账户实机矩阵
继续阶段 4：浏览器端到端验收和多实例 Gateway 事务；P5-05 可复现重跑的平台 Run 接入
```

### P5-05 可复现重跑入口（本轮完成开发版）

依赖 P5-01 至 P5-04。本轮已完成从通过审计的 Run Manifest 生成新 Run 请求、恢复代码/环境/参数/随机种子和数据策略、工作区路径迁移，以及输出哈希、稳定复现哈希和信息边界比较。哈希不一致、输入快照变化或审计未通过时，结果只能进入待复核状态；平台 Run 创建、正式 Artifact 快照绑定和真实执行仍待实现。

### 本轮非目标

- OIDC 登录页面和长期 Agent 设备凭证。
- 真实 MinIO/PostgreSQL 服务验收。
- NATS、Temporal、Yjs 和 Docker Runner。
- Gateway 业务结果与全部副作用的跨实例统一事务协调。
- 新的营销页面或视觉扩展。

### P3-21 当前轮次结果

本轮完成真实基础设施验收准备，而非宣称生产验收完成：

- Stage 2 集成测试从硬编码的 `001` 至 `006` 修正为动态覆盖迁移目录中的 `001` 至 `009`。
- 集成测试增加迁移二次执行幂等性、`schema_migrations` 完整记录、P3-18/P3-20 字段、轮换审计表和相关 RLS 检查。
- 增加真实 PostgreSQL 设备注册、Token 轮换、旧 Token 失效、活动连接撤销、轮换审计和并发版本串行化测试入口。
- 同步 `STAGE2_EXIT_CHECKLIST.md`、`POSTGRESQL_MIGRATIONS.md`、`POSTGRES_REPOSITORY.md`、`IMPLEMENTATION_STATUS.md` 的事实口径。
- 当前环境没有 Docker、Podman、PostgreSQL、psql、MinIO 或安装器，MinIO/PostgreSQL 条件测试仍为 skipped；阶段 2/3 生产退出不变。

交接文档：`docs/handoffs/P3_21_REAL_INFRASTRUCTURE_ACCEPTANCE_PREP_HANDOFF.md`。

### P3-22 当前轮次结果

本轮完成 Gateway 接力与复核开发版切片，不宣称阶段 3 生产退出：

- 新增 `agent.handoff.create`、`agent.handoff.accept`、`agent.review.submit` 三类 Gateway 命令，并同步协议包、领域 Schema、默认能力 Token、客户端和 Gateway 测试。
- PostgreSQL Repository 已补齐 Handoff 创建/接收和 Review 写入口；Review 的目标状态、Gate、Review、Event、EventOutbox 和幂等记录在同一事务内完成。
- SQLite Handoff/Review 写入增加请求指纹幂等语义；重复请求不重复产生 Review 或事件，同键不同请求返回冲突。
- Agent Review 只能提出 `NEEDS_REVISION`/`BLOCKED` 等复核意见，不能以 Agent 身份提交 `APPROVED`。
- API 全量测试通过 82 项，3 项真实 PostgreSQL/MinIO 基础设施测试因服务未提供跳过；本地 Agent 全量测试通过 114 项，9 项 ConPTY/平台条件测试跳过。
- 本轮额外确认：目标 API 模块 `py_compile` 通过；Agent 跳过项主要因为当前测试虚拟环境未安装 `pywinpty`，不能据此宣称真实 ConPTY 已验收。

交接文档：`docs/handoffs/P3_22_GATEWAY_HANDOFF_REVIEW_HANDOFF.md`。

### P4-03 当前轮次结果

本轮完成审核中心与失效传播开发版切片，不宣称真实生产验收完成：

- `apps/web/lib/api.ts` 新增审核中心领域类型、`getReviewCenter()` 和 `updateRisk()`；`apps/web/components/dashboard-shell.tsx` 将 Gate、Review、Risk、Evidence、Handoff 收据接入可操作工作台。
- 审核门禁列表根据真实 Gate 状态渲染；风险登记支持责任人分配、带 Evidence 关闭和重新打开；Fanout 交接显示整体收据状态及各接收方状态。
- `ReviewCenter` 增加 `handoffs` 字段，保留既有 API 兼容性并让前端获取完整交接审计上下文。
- SQLite 和 PostgreSQL 失效传播均遍历任务依赖、成果物派生、交接汇总和任务产出的交接；受影响任务分别进入 `BLOCKED` 或 `NEEDS_REVISION`，下游已通过 Task Gate 进入 `INVALIDATED`。
- 传播动作写入 `task.upstream_gate_invalidated` 与 `gate.invalidated` 事件，重新打开 fatal/major 风险也会触发同一传播逻辑。
- 验证：API 91 项通过、3 项真实基础设施测试跳过；P4-03 目标模块 `py_compile` 通过；Next.js 15 生产构建通过。
- 环境说明：系统 Python 不在 PATH，项目 `.venv` 缺少解释器；本轮使用工作区 bundled Python，并预加载其兼容 `_cffi_backend` 后通过 vendor 依赖运行回归。项目内独立依赖安装因包源无响应中止，未把该过程当作成功安装。

交接文档：`docs/handoffs/P4_03_REVIEW_CENTER_PROPAGATION_HANDOFF.md`。

### P4-04 当前轮次结果

本轮完成真实基础设施验收准备和 PostgreSQL 多实例正确性修复，不宣称真实服务验收完成：

- 新增迁移 013，强制租户 RLS，补齐 agents、sessions、idempotency_records 策略和组织范围幂等记录。
- PostgreSQL Repository 增加项目级事务锁、并发安全 Gate 创建、锁内 Gate 失效复检、事件幂等查询和项目级序号串行。
- 新增阶段 4 条件集成测试，覆盖强制 RLS、租户隔离、Fanout 收据并发、Risk 幂等、Gate 传播、Outbox 并发领取和 MinIO 跨实例 Multipart。
- 当前 API 回归 95 项通过、7 项条件测试跳过；P4-04 独立脚本可执行，4 项真实基础设施测试因服务未提供跳过。
- 本机未发现 Docker、Podman、PostgreSQL、psql 或 MinIO，真实运行仍保留为 `P4-04-RUN`。

交接文档：`docs/handoffs/P4_04_REAL_INFRASTRUCTURE_ACCEPTANCE_PREP_HANDOFF.md`。

## 23. 时间预估

以下时间以个人或两三人业余开发为基准，阶段可以有限并行：

| 阶段 | 预估 |
| --- | --- |
| 阶段 0 | 2 至 3 周 |
| 阶段 1 | 4 至 6 周 |
| 阶段 2 | 4 至 6 周 |
| 阶段 3 | 5 至 7 周 |
| 阶段 4 | 5 至 7 周 |
| 阶段 5 | 6 至 8 周 |
| 阶段 6 | 4 至 6 周 |
| 阶段 7 | 6 至 8 周 |
| 阶段 8 | 5 至 7 周 |
| 阶段 9 | 8 周以上并持续进行 |

完整建设周期按约 10 至 14 个月评估。该估算不以缩减目标架构为前提，而是通过阶段门禁控制风险。

## 24. 版本记录

| 版本 | 日期 | 内容 |
| --- | --- | --- |
| 1.0 | 2026-09-12 | 冻结产品决策、第一轮实现基线、阶段 0 至阶段 9、首批 E2E 验收和交接规范 |
| 1.1 | 2026-09-12 | 完成基线修复、正式领域 Schema、开发版状态机门禁、接力/分发契约测试；记录当前测试和剩余阶段 0 差距 |
| 1.2 | 2026-09-12 | 进入阶段 2 开发实现；完成 Artifact 内容与 Multipart、Git 索引、Bundle 1.1 导出恢复、PostgreSQL 迁移和 RLS 基线 |
| 1.3 | 2026-09-12 | 完成对象完整性、S3 跨实例 Multipart、缺块拒绝、显式版本、过期清理和阶段 2 开发退出评审记录 |
| 1.4 | 2026-09-12 | 完成 Multipart 生命周期重试、S3 配置工厂、真实集成测试入口、Artifact 版本/归档 API 和阶段 2 最终待验收项记录 |
| 1.5 | 2026-09-12 | 完成 S3 跨实例分块续传、上传过期清理、Bundle UUID/校验清单审计和集成脚本可执行性修复；常规测试达到 31 项 |
| 1.6 | 2026-09-13 | 完成 PostgreSQL Repository 第一批事务查询、HTTP 幂等请求指纹、开发/生产认证模式、项目与对象路径授权及主体记账；常规测试达到 39 项 |
| 1.7 | 2026-09-13 | 完成 EventOutbox 领域契约、SQLite 高风险写入事务收紧、PostgreSQL outbox 迁移/读写边界、恢复补建和回滚测试；常规测试达到 44 项 |
| 1.8 | 2026-09-13 | 完成 EventOutbox 原子领取、锁过期、通用 Dispatcher 和失败退避测试；常规测试达到 45 项 |
| 1.9 | 2026-09-13 | 补充 EventOutbox 锁过期回收测试；常规测试达到 46 项 |
| 2.1 | 2026-09-13 | 完成 P3-05 项目能力 Token 对任务领取、租约、进度/结果和 Run 写路径的开发版强制接入；新增跨项目/能力/Agent/租约契约测试；后端达到 61 项通过 |
| 2.2 | 2026-09-13 | 完成 P3-06 Gateway 任务/租约/进度/结果和 Run 命令的开发版处理；项目能力 Token 复用到 Gateway；命令结果随 ACK 返回；后端达到 63 项、本地 Agent 达到 7 项通过 |
| 2.3 | 2026-09-13 | 完成 P3-07 Gateway 命令结果持久化、重复/同幂等键恢复、失败结果恢复和跨端 `packages/agent_protocol` 抽取；后端达到 67 项、本地 Agent 7 项通过 |
| 2.4 | 2026-09-13 | 完成 P3-08 Machine Agent Service 开发版：自动重连退避、心跳调度、本地紧急停止、子进程监督、Run 状态恢复和 agentd 服务控制命令；后端 67 项、本地 Agent 17 项通过 |
| 2.5 | 2026-09-13 | 完成 P3-09 Runner/Adapter 基础契约、PythonAdapter、完整进程归属上下文和工作区/命令/环境/网络策略 fail-closed 校验；本地 Agent 达到 26 项通过 |
| 2.6 | 2026-09-13 | 完成 P3-10 User Session Worker/本机 IPC 传输无关 Broker 契约、会话权限与生命周期测试；本地 Agent 达到 39 项通过；明确真实 Named Pipe/ACL 仍未实现 |
| 2.7 | 2026-09-13 | 完成 P3-11 Windows Named Pipe/ACL/PID-SID-Windows Session 身份认证传输开发版及同机拒绝测试；本地 Agent 达到 47 项通过；明确 Windows Service/跨账户生产验收仍未完成 |
| 2.8 | 2026-09-13 | 完成 P3-12 User Session Worker Runtime、标准管道 Runner、stdin/停止和本地事件编排；Windows Named Pipe 到 Runner 纵向测试通过；本地 Agent 达到 58 项通过；明确 ConPTY/桌面/Artifact 云端闭环仍未完成 |
| 2.9 | 2026-09-13 | 完成 P3-13 Agent 事件、Artifact/Multipart、Run Manifest 和 Run 结果接入开发版；后端达到 71 项、本地 Agent 达到 63 项通过；建立 P3-13 交接文档 |
| 3.0 | 2026-09-13 | 完成 P3-14 会话生命周期与终端 resize 契约切片；新增本地生命周期审计持久化、锁屏/注销/恢复/紧急停止状态机和无 PTY fail-closed 测试；本地 Agent 达到 73 项通过 |
| 3.1 | 2026-09-13 | 完成 P3-15 开发版 ConPTY Adapter、Hybrid 路由、stdin/输出/resize/取消和能力探测；安装 pywinpty 2.0.15；本地 Agent 达到 77 项通过 |
| 3.2 | 2026-09-13 | 校正 P3-15 实际验收数字为 ConPTY 7 项、本地 Agent 80 项；补充开发版与生产验收边界；开始 P3-16 Windows Service/Session 0 Adapter |
| 3.3 | 2026-09-13 | 完成 P3-15 恢复兜底回归和 P3-16 Windows Service/Session 0 纯后端适配切片；ConPTY 8 项、本地 Agent 93 项通过；进入真实服务验收准备 |
| 3.4 | 2026-09-13 | 完成 P3-17 Codex `exec --json` 版本探测、命令构造、JSONL 事件解析、Runtime 归一化、危险参数阻断和 Claude 明确不可用状态；本地 Agent 105 项通过；新增 CLI Adapter 交接文档 |
| 3.5 | 2026-09-13 | 完成 P3-18 设备 pairing challenge、Ed25519 私钥持有证明、规范化公钥指纹、SQLite/PostgreSQL 注册校验、迁移和注册 CLI；API 75 项、本地 Agent 106 项通过；新增设备 challenge 交接文档 |
| 3.6 | 2026-09-13 | 完成 P3-19 Windows Credential Manager 适配器、目标名规范、凭据保存/删除 CLI 和 Gateway/Service 默认取证；凭据专项 8 项通过；记录当前环境长期持久化返回 1312；新增凭据存储交接文档 |
| 3.7 | 2026-09-13 | 完成 P3-20 设备 Token 原子轮换、旧 Token 失效、活动连接撤销、SQLite/PostgreSQL 契约和无秘密值审计；API 78 项、本地 Agent 114 项通过；新增设备 Token 轮换交接文档 |
| 3.8 | 2026-09-13 | P3-21 完成真实 PostgreSQL/MinIO 验收准备：迁移测试覆盖 001 至 009、二次执行幂等性、新字段/RLS 检查，增加设备轮换事务与并发集成入口；当前无真实服务，仍为待验收 |
| 3.9 | 2026-09-14 | 完成 P3-22 Gateway Handoff 创建/接收、Agent Review 提交、PostgreSQL 写入口和 SQLite/PG 幂等语义；API 81 项通过、真实基础设施 3 项跳过；阶段 3 仍未生产退出 |
| 3.10 | 2026-09-14 | 完成 P3-22 收尾复核和交接文档；校正当前测试事实为 API 82 项通过/3 项基础设施跳过、Agent 114 项通过/9 项条件测试跳过；记录 SQLite Handoff 事件非统一事务、HTTP 接收主体校验和后续 P3-23 边界 |
| 3.11 | 2026-09-14 | 完成 P3-23 阶段 3 开发版收尾：新增 Artifact 元数据 Gateway 命令、Gateway 请求指纹和迁移 010；形成阶段 3 开发版退出评审与交接文档；完整回归和真实基础设施验收延期 |
| 3.12 | 2026-09-14 | 完成 P4-01 开发切片：Handoff 拒绝/返工/汇总、下游 Gate 门禁、统一风险规则、Review/Gate/Evidence 关系、审核中心接口、Gateway reject 命令和迁移 011；API 86 项通过（3 项真实基础设施跳过）、Agent 114 项通过（9 项条件测试跳过） |
| 3.13 | 2026-09-14 | 完成 P4-02 开发切片：Fanout 独立收据、独立风险登记/关闭/重新打开、Gate 输入快照与自动失效、HTTP Agent Handoff 接收/拒绝和迁移 012；API 90 项通过（3 项真实基础设施跳过） |
| 3.14 | 2026-09-14 | 完成 P4-03 审核中心数据化、风险操作、Fanout 收据展示和 Gate 失效影响图传播；API 91 项通过（3 项真实基础设施跳过），Next.js 生产构建通过；真实 PG/RLS、浏览器和多实例验收延期 |
| 3.15 | 2026-09-14 | 完成 P4-04 验收准备：新增迁移 013 强制 RLS 和租户幂等范围，收紧 PostgreSQL 项目级事务、Gate/Event 并发语义，新增阶段 4 PostgreSQL/MinIO 条件集成入口；API 95 项通过、7 项条件测试跳过，真实服务仍待执行 |
| 3.16 | 2026-09-14 | 完成 P4-05 浏览器验收准备：前端侧栏计数改为真实数据派生，补充稳定 `data-testid` 和移动端导航收起行为；Next.js 生产构建通过，真实浏览器/API 联调仍待执行 |
| 3.17 | 2026-09-14 | 完成 P5-01 可复现执行上下文开发切片：Runner/Session 协议补齐复现元数据，Run Manifest 增加输入访问摘要、信息边界状态和自身哈希；容器隔离、系统级审计和真实重跑仍待执行 |
| 3.18 | 2026-09-14 | 完成 P5-02 Docker/Podman 容器执行边界开发切片：运行时解析、digest/路径/模式策略、默认无网络和资源限制命令构造、监督器接入；真实容器运行和 Session 路由仍待执行 |
| 3.19 | 2026-09-14 | 完成 P5-03 容器执行后端显式协议、Session Worker 路由、工作区只读/输出目录可写分层挂载、Manifest 脱敏接入和容器契约测试；真实 runtime、系统级文件审计和容器组合验收仍待执行 |
| 3.20 | 2026-09-14 | 完成 P5-04 信息边界审计内核、声明/实际读取/工作区外/未来数据分类、观察器 fail-closed 接口和 Manifest 接入；真实 OS 级观察器、网络观察与 Review/Gate 自动化仍待执行 |
| 3.21 | 2026-09-15 | 补充 P5-04 的 Run Manifest 集成回归：声明输入但无观测时 Manifest 明确阻断；Agent 回归基线更新为 126 项通过、1 项跳过 |
| 3.22 | 2026-09-15 | 完成 P5-05 可复现重跑规划、路径迁移、稳定复现哈希自校验、输出/信息边界比较和容器上下文保留；新增 8 项契约测试，Agent 回归为 134 项通过、1 项跳过；真实重跑、平台 Run 接入和 OS 级观察器仍待执行 |
| 3.23 | 2026-09-15 | 完成 P5-06 文件/网络观察契约、JSONL 外部桥接、网络边界规则和 Runner/Session/Manifest/重跑传递链开发版切片；真实 ETW/eBPF/容器观察器、进程树绑定和网络阻断进入 P5-06-REAL |
| 3.24 | 2026-09-15 | 完成 P5-06-REAL Windows ETW 开发版适配：logman/tracerpt 会话、实际 PID 与进程树轮询、CSV 归一化、Runner/Session/Manifest 传递和 4 项专项测试；真实管理员权限采集、事件矩阵、容器观察和网络阻断仍待验收 |
| 3.25 | 2026-09-15 | 修正 ETW 兼容策略为两个单 Provider 会话，补充真实 PID 入口、写事件排除、tracerpt Description 路径解析和 CLI 显式开关；专项测试 5 项、Agent 全量 139 项通过，真实管理员权限采集仍待验收 |
| 3.26 | 2026-09-15 | 完成 P5-06-REAL-ADMIN 免管理员切片（观察器诊断 Manifest、runtime_dependency_roots 分类、异常清理测试，专项 10 项/Agent 全量 145 项通过）与 P4-04-RUN 真实基础设施验收（用户态 PostgreSQL 16/MinIO，阶段 2+4 集成 7 项全通过，含 FORCE RLS 非超级用户隔离、并发轮换、Fanout/Gate 传播、SKIP LOCKED）；ETW 管理员采集确认为 UAC 硬阻塞并取证 |
| 3.27 | 2026-09-15 | 完成 P4-04-RUN-PROD：迁移 014 建立非 owner `app_runtime` 运行时角色（最小权限、对非超级用户迁移身份容错），`local-infra.ps1 provision` 实现 scram-sha-256 密码认证切换，`PostgresRepository` 增加连接池参数校验/超时/稳定错误码/`pool_stats()`，新增 6 项运行时角色验收（密码登录、DDL 与 pg_authid 拒绝、RLS 隔离、业务流、池耗尽与后端丢失故障演练）全通过；修复迁移 014 引入的两处回归，API 全量 101 项、Agent 全量 145 项全绿 |
| 3.28 | 2026-09-15 | 完成阶段 6 数学建模模板 pack：`packages/competition_packs`（loader/materializer/validation）+ `cumcm` pack（manifest、四问 DAG、题面事实与数据画像 schema、9 个模板），四问 DAG 可物化为平台任务（含 dependency_task_ids）、模板渲染为成果物骨架且全流程幂等；校验器实现四问覆盖（中/阿拉伯数字标记）、章节字数、官方结果表命名（pack 管理 major / 遗留 minor、模板骨架豁免）与信息边界规则，合规项目 PASS/findings=0；新增 `apps/api/test_competition_packs.py` 15 项契约测试全通过 |
| 3.29 | 2026-09-15 | 竞赛 pack 接入 HTTP：`/api/competition-packs` 列表与详情、骨架导出、`/api/projects/{id}/competition-pack` 物化进度、`apply`（幂等键必填、同键重放）与 `validate`（四问覆盖/官方结果表/信息边界），以及按项目上下文渲染的模板导出；物化语义收紧为"空骨架不授予覆盖、填写后按交付物校验"（`data_policy.template_hash`）；新增 15 项路由契约测试，API 全量 131 项通过 |
| 3.30 | 2026-09-15 | 完成阶段 6 收尾两项：Web 工作台 pack 面板（pack 信息/物化进度/问题范围/模板预览导出/一键应用/校验报告，含 `data-testid`，生产构建与页面渲染标记验证通过）与数模审计生成机器 Review + 信息边界 Gate（`fatal→BLOCKED`/`major→NEEDS_REVISION`，干净结果不自动批准，保留人工批准门禁，`scope=information_boundary` 只报边界发现）；新增 7 项审查契约测试，API 全量 138 项通过；剩余：C 题交接包导入与阶段 7 |
| 3.31 | 2026-09-15 | 完成阶段 6 最后一项：现有 C 题交接包可解释无损导入（前缀识别交接包布局、目录→成果物类型映射、未分类文件可解释回退登记、lossless 判定与哈希核对）；真实交接包实测 declared=imported=1964、lossless=True、重复导入幂等、导入后可继续执行 pack 校验与机器 Review；新增 7 项契约测试，API 全量 145 项通过。阶段 6 数学建模模板全部收口，下一步进入阶段 7 |
| 3.32 | 2026-09-15 | 开始阶段 7：完成文档三层版本（草稿/提交/批准）与证据链最小切片——文档模板纳入成果物版本链与 Review/Gate，提交必须带证据（草稿无法绕过门禁）、批准仅限人工 Review、修订只新增草稿版本并保留历史；版本时间线可追溯到成员/Agent/任务/Git commit，新增证据链查询与 4 个端点；14 项契约测试，API 全量 159 项通过，真实服务链路验证通过 |
| 3.33 | 2026-09-15 | 阶段 7 文档三层版本接入 Web 工作台：文档面板（层级徽标、三层版本时间线、追溯信息、证据链、提交/派生新草稿动作、`nav-documents` 入口），并与真实 API 完成数据链路联调（dashboard 成果物→文档过滤→timeline→证据链）；Next.js 生产构建与页面标记验证通过 |
| 3.34 | 2026-09-15 | 阶段 7 补齐版本 Diff/合并确认与协作要素：版本间 unified diff（含统计与 Git/任务/作者变化）、合并确认（派生新草稿、批准版本不可为合并目标、可选 Git 提交校验）、评论与建议（可锚定段落）、文档快照（revision+内容哈希检查点）、结论-图表-运行-段落关系与影响面定位端点；全部基于事件流实现无需迁移；新增 17 项契约测试，API 全量 176 项通过 |
| 3.35 | 2026-09-15 | 阶段 7 收口：Web 文档面板接入 Diff/合并/评论/快照/关系五标签页；实现 Yjs CRDT 多人同时编辑（前端 Y.Doc 增量 + 平台 WS 校验中继，不回声、按项目隔离），真实服务验证中继送达与非法帧拒绝、真实 yjs 验证两端并发编辑收敛且互不覆盖；新增协作中继 7 项契约测试，API 全量 183 项通过，Next.js 生产构建通过 |
| 3.36 | 2026-09-15 | 阶段 6 收尾：把 pack 的 information_boundary 规则接入平台 Gate/Runner 审计（审计端点 + 只读预览，违规 BLOCKED、观察缺失 NEEDS_REVISION、干净不自动批准）；落地 competition-workflow 插件适配（技能→阶段/产出、模板→pack 模板、检查脚本→审计入口，未识别资产显式登记），真实插件实测 11/12 技能与 8/9 模板映射；新增 16 项契约测试，API 全量 199 项通过 |
| 3.37 | 2026-09-15 | 阶段 8 交付链路开发版：批准素材装配（未批准一律排除并列出）、Marp 答辩提纲、交付检查（图表/引用/公式/匿名/附件/页数）、提交包生成与冻结（提交待审→人工批准后不可变）、跨部署恢复验证（第二套 Store 哈希比对）；真实服务端到端 Demo 验证通过；新增第一版 Demo 一键启动脚本 scripts/start-demo.ps1 并实测端点全绿；新增 12 项契约测试，API 全量 211 项通过。阶段 9 与 LaTeX/PPT 真实渲染仍待推进 |
| 3.38 | 2026-09-15 | 阶段 9 最小切片：健康/可观测端点（health/metrics/quota/项目 usage，含存储、事件、能力 Token 与标注估算的成本）、配额判定接入运行与成果物写路径（超限 429 + 稳定错误码，限额与单价可由环境变量覆盖）、自托管部署补全（api/web Dockerfile + docker-compose 五服务 + infra/README 部署说明与生产必做项）；真实服务端点验证通过，新增 11 项契约测试，API 全量 222 项通过。剩余：Helm/K8s、真实跨机恢复、备份演练、OTel 导出与 LaTeX/PPT 真实渲染 |
| 3.39 | 2026-09-15 | 后续统一测试第一批：浏览器交互验收（pack 一键应用使物化进度 4%→100%、文档三层与五标签页、协作在线状态、评论提交全链路，截图留存）与 LaTeX 真实编译（安装 MiKTeX 25.12 用户级，新增 latex_compile 模块与交付编译端点，交付检查页数项改为真实页数，中文论文实测编译 2 页 PDF 并通过检查）；新增 13 项契约测试，API 全量 235 项通过 |
| 3.40 | 2026-09-15 | 工作台重构为多页面应用：设计系统重写（令牌/组件类/两级响应式）、共享壳层与工作区上下文、11 个路由（总览/任务/模板包/文档/审核/交付/成果物/交接/运行/时间线/设置）；旧单页 DashboardShell 移除；构建 11 路由全部静态预渲染，浏览器漫游 11 路由均 200 且关键定位器唯一命中，总览与模板包页截图留存 |
| 3.41 | 2026-09-15 | 新增黑夜/白天主题（令牌覆盖 + localStorage 持久化 + 顶栏切换，刷新后保持）与个人云盘（成员隔离 200MB 配额、内容寻址去重、压缩包识别、一键导入项目空间并按类型登记成果物、引用保护禁止删除；前端 /drive 页含上传/用量/导入确认弹窗）；总览页精简为摘要+链接卡；新增 10 项云盘契约测试与真实上传导入链路验证，API 全量 245 项通过，前端 13 路由构建通过 |
| 3.42 | 2026-09-15 | Hyper-RAG 集成 Phase A 后端：集成决策固化（检索不进门禁、MinerU 只调 API、可视化含向量库管理、服务独立 8100、KB 双形态），hyper-rag-service 零改动独立运行；新增知识库双形态存储（项目库/个人库+分享）、成员 AI 凭据、检索/索引/超图 Gateway 代理（稳定错误码映射）、多会话与消息；14 个端点 + 12 项契约测试 + 真实代理链路验证，API 全量 257 项通过。下一阶段：MinerU 转换队列、/kb 与 /ask 前端页、/graph 超图可视化 |
| 3.43 | 2026-09-15 | Hyper-RAG 集成 Phase B：MinerU 转换队列（入队即返回 + 并发上限 2 + 指数退避重试 + done 后写入知识库）与前端三页——/kb 知识库管理（文档+索引触发+状态展示）、/ask 多会话 AI 问答（普通+检索双模式、绑定知识库、来源 chips）、/settings 升级为 AI 凭据配置中心（LLM/Embedding/MinerU 三组密钥 + 四家厂商预设 + 脱敏回显）；新增 5 个转换端点 + 9 项测试，API 全量 266 项通过，前端 15 路由构建通过 |
| 3.44 | 2026-09-15 | Hyper-RAG 集成补齐：/graph 超图可视化页（三标签页、邻居子图 SVG 渲染、实体/关系管理）、内置 AI 普通对话直连用户配置 LLM（ai_chat.py + POST /api/ai/chat，支持 SSE 流式）、云盘→MinerU 转换→知识库链路打通（/drive 页加转换入知识库按钮）；@antv/g6+graphin 已安装；16 路由构建通过，API 全量 266 项回归通过 |
