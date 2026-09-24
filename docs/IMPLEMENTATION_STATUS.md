# 实施状态

> 更新日期：2026-09-15
>
> 当前阶段：阶段 3 开发版已完成（P3-23），阶段 4 P4-01 至 P4-04 已完成开发切片，P4-04-RUN 已在用户态真实 PostgreSQL/MinIO 完成集成验收，P4-04-RUN-PROD 已完成非 owner 运行时角色、scram-sha-256 密码认证、连接池硬化与故障演练验收，P4-05 浏览器验收准备已完成；阶段 5 P5-01 至 P5-06 已完成开发版契约或接入切片，P5-06-REAL 已完成 Windows ETW 开发版适配、观察器诊断和运行时依赖分类；阶段 6 CUMCM 领域包（数学建模模板）已完成 pack manifest/版本升级规则/四问 DAG/题面事实与数据画像 schema/9 个模板/物化器/校验器，并通过端到端验收、HTTP 端点接入、Web 工作台面板接入、机器 Review/信息边界 Gate 接入、现有 C 题交接包无损导入，以及阶段 7 文档三层版本（草稿/提交/批准）、证据链、版本 Diff/合并确认、评论与快照、结论-图表-段落关系与影响面定位（含 Web 文档版本面板 + Yjs 多人协作编辑），把 pack 的信息边界规则接入平台 Gate/Runner 审计、完成 competition-workflow 插件适配，完成阶段 8 交付链路（批准素材装配、交付检查、提交包生成与冻结、跨部署恢复验证）、第一版 Demo 启动脚本，阶段 9 最小切片（健康/可观测端点、配额与成本统计、自托管 Docker Compose 与部署说明），完成第一批后续统一测试（浏览器交互验收 + MiKTeX 真实 PDF 编译），把工作台重构为多页面应用（13 个路由 + 统一设计系统 + 黑夜/白天主题 + 个人云盘），完成 Hyper-RAG 集成 Phase A（知识库双形态 + AI 凭据 + 检索代理 + 多会话）与 Phase B（MinerU 转换队列 + /kb /ask /settings 前端页 + /graph 超图可视化 + /ask 普通对话直连 LLM + 云盘转换入知识库）。管理员权限 ETW 实机采集、容器观察、网络阻断、阶段 6 剩余项（项目模板导出、信息边界审计 Gate、机器 Review、C 题交接包导入）、阶段 7 与浏览器联调仍未完成。
>
> 状态口径：DONE 表示开发版链路已实现并有测试；PARTIAL 表示存在明确生产差距；TODO 表示尚未实现。
>
> **当前主线**：`docs/DEMO_1_0_IMPLEMENTATION_PLAN.md`（Demo 1.0 用户体验主线，阶段 UX-0 至 UX-7）。新工作优先按该计划推进；本文件的「已完成」列表按阶段归档，不承担计划职责。
>
> **仓库托管（2026-09-24）**：源码在 **github.com/FallRain0905/Synapforge**（**公开仓库**，默认分支 `master`，受管文件 480 个 / 6.1 MB）。
> 上传前做了三件事：① `.gitignore` 补全（`.infra/` 本地 PostgreSQL/MinIO 有 1GB、`dist-sidecar/`、`apps/desktop/dist/`、
> `apps/api/data/` 运行数据、`.venv/` 等）——构建产物与运行数据都不进仓库；② **凭据脱敏**：docs 里 IP 入口的 nginx Basic
> 口令及其旧口令共 11 处已替换为 `<Basic 口令已脱敏>`；③ 清掉根目录一个 0 字节的误建文件。
> 口径：**仓库里不放任何口令/密钥**——服务器 root 口令、平台账号口令、设备/项目令牌、模型 API key 只留在 `~/.ssh/*.secret`、
> 平台库与执行体的 0600 配置文件里。文档中仍有平台/执行体的 IP（不含凭据）。

## 已完成

- FastAPI 控制平面、SQLite 开发 Store、Next.js 工作台和本地 Agent CLI。
- Organization、Team、HumanMember、Membership、Project Membership 和 Agent Project Grant 的开发版模型。
- Task、Handoff、Artifact、Run、Review、Gate、Evidence、Event、EventOutbox 的领域模型和 JSON Schema。
- Task 状态转换、任务依赖、批准成果物门禁和 Agent 不得人工审批规则。
- 接力式、分发式和状态机门禁纯后端契约测试。
- LocalObjectStore 和可选 S3ObjectStore 适配器。
- Artifact 单文件写入、读取、内容哈希、MIME、大小、版本、父版本、批准不可变和归档。
- Artifact Multipart 初始化、分块上传、完成、取消和数据库绑定。
- CUMCM 导入器将识别文件内容写入对象存储，同时保留源路径、相对路径和策略元数据。
- Local Git HEAD、commit 存在性、commit 文件索引和文件 SHA-256。
- 项目 Bundle 导出和恢复，包含所有项目关系、对象文件、Git 索引和审核时间线。
- Bundle 恢复前引用校验、对象哈希/大小校验、项目范围 key 校验和事务回滚测试；恢复事件会重新进入待投递 outbox。
- Artifact、Multipart、Git、导出恢复和事务 outbox/Dispatcher 测试，并有后端契约测试覆盖。
- S3 元数据、跨实例 Multipart、缺块拒绝、版本去重边界和过期上传清理测试。
- PostgreSQL `001_initial.sql` 至 `013_p4_04_force_rls_and_event_idempotency.sql` 和显式迁移入口已建立，未切换默认运行时。
- PostgreSQL Repository 第一批已实现：连接池、事务上下文、事务内 `app.organization_id` RLS 设置，以及项目、任务、成果物、运行、交接、Review/Gate/Evidence/Event 和 Git 索引的读写/查询切片。
- HTTP `Idempotency-Key` 已覆盖单文件上传、Multipart 初始化/分块/完成/取消、Git 注册/索引和 Bundle 恢复，并保存请求指纹以拒绝同键不同请求。
- HTTP 已有开发/生产认证模式：Bearer Session 解析、项目和对象路径 RBAC、项目列表按成员过滤、创建/审核/授权主体由会话成员记账。
- 阶段 2 逐项退出清单已固化在 `docs/STAGE2_EXIT_CHECKLIST.md`。
- Event Outbox 已加入 SQLite 和 PostgreSQL 迁移；SQLite 高风险 Artifact、Review、Evidence 写入实现业务状态、事件和 outbox 同事务，提供原子领取、锁过期、成功标记、失败退避和幂等查询接口，并有通用 Dispatcher。
- P3-01 已加入 `Device`、`DevicePairing`、`DeviceProjectGrant`、`AgentConnection` 等设备接入契约，以及 `ExecutionProfile`、`GatewayEnvelope`、`AgentHeartbeat`、事件确认/补传请求模型。
- SQLite 已支持一次性设备配对、设备 Token 哈希存储、设备 Token 解析、设备撤销、项目范围能力 Token、Token 撤销和基础连接元数据；新增 FastAPI 设备配对、注册、列表、撤销和项目授权接口。
- PostgreSQL `005_agent_devices.sql` 已加入设备、配对、项目授权和连接表，并为四张表建立组织/项目范围 RLS 策略。
- `apps/api/test_devices.py` 覆盖配对一次性、密钥不明文存储、Agent 所有者匹配、项目/能力范围、设备撤销级联和连接状态；P3-01 当时后端常规测试为 51 项通过。
- P3-02 新增 `apps/api/app/gateway.py` 和 `/ws/agents/{device_id}` WebSocket Gateway：使用设备 Token 握手，绑定 device/agent/session/connection 身份，处理连接生命周期、心跳、入站连续序号、重复帧确认和序号缺口补传。
- `apps/api/test_gateway.py` 覆盖 Gateway 服务和 FastAPI 路由级协议：握手身份、心跳 ACK、重复消息、序号缺口、非法载荷、撤销连接和断开清理，并已扩展任务/Run 命令、结果恢复和失败结果恢复；当前后端总计为 68 项通过。
- `apps/agent/local_state.py` 新增本地 SQLite 恢复日志：Gateway 出站序号、消息幂等键、PENDING/SENT/ACKED 状态、Run 状态、待上传文件和审批状态均可持久化。
- `apps/agent/gateway_client.py` 新增 `DurableGatewayClient`：构造统一 Gateway 封套，发送前持久化为 SENT，处理服务器 ACK/补传请求，并在断线后恢复未确认消息；设备 Token 不写入本地状态库。
- `apps/agent/agentd.py` 增加 `device-register`、`gateway-queue`、`gateway-recover` 和 `gateway-run` 命令；本地 Agent 测试新增 6 项通过。
- `PostgresRepository` 已加入 P3-01/P3-02 设备运行时方法：Device/Pairing/Project Token/AgentConnection 查询与写入、设备撤销联动、`FOR UPDATE` 序号推进和心跳身份校验；新增映射及方法存在性测试。
- HTTP Agent 任务/Run 写路径已强制使用项目能力 Token：任务领取、租约心跳、进度、结果和 Run 创建/完成分别校验项目、Agent 与能力范围；`agentd` CLI 已支持传递该 Token。
- 新增 `apps/api/test_agent_capability.py`，覆盖缺少 Token、跨项目 Token、错误 Agent、能力不足和租约项目重绑定；P3-05 后端常规测试达到 61 项通过。
- Gateway 已支持 `agent.task.claim`、`agent.task.lease.heartbeat`、`agent.task.progress`、`agent.task.result`、`agent.run.create` 和 `agent.run.complete`，并将业务结果随 `gateway.ack.command_result` 返回。
- `DurableGatewayClient` 已透传 ACK 中的 `command_result`；Gateway 命令测试覆盖项目 Token、资源归属、Run 生命周期和错误序号消费。
- Gateway 命令结果已写入连接范围的 `gateway_command_results`；重复序号和同幂等键重试可以恢复原业务结果或原错误码。
- 新增 `packages/agent_protocol`，API 和本地 Agent 共同使用 Gateway Envelope、Heartbeat、ACK、Replay 和 `GatewayCommandResult`。
- 新增 `packages/agent_protocol/gateway.schema.json`，并将 `GatewayCommandResult` 同步加入领域 Schema 与契约目录。
- P3-08 新增 `apps/agent/machine_service.py`：Machine Agent Service、指数退避自动重连、心跳调度、
  本地紧急停止和直接子进程监督；进程 stdout/stderr、退出码、超时、取消、启动失败和 Run 状态
  均写入本地 SQLite 恢复库。
- `apps/agent/agentd.py` 新增 `service-run`、`service-emergency-stop` 和
  `service-clear-emergency-stop`，允许服务进程与独立控制进程共享本地紧急停止标志。
- 新增 `apps/agent/test_machine_service.py`，覆盖 P3-08 的重连、心跳、fail-closed 停止、清除停止、
  子进程生命周期和孤儿记录恢复；本地 Agent 常规测试达到 17 项通过。
- P3-09 新增 `apps/agent/runner.py`：统一 `RunnerRequest`、`ExecutionProfile`、`AdapterDescriptor`、
  `LocalRunner`、`WorkspacePolicy`、`CommandAdapter` 和 `PythonAdapter` 契约。
- P3-09 将项目、任务、Run、Agent、Device、Workspace 上下文写入 `ProcessSpec` 和本地
  `managed_processes` 恢复记录；Adapter 不能修改运行归属、工作区或重新启用宿主机环境继承。
- P3-09 将 `execution_profile` 正式加入 API `RunCreate/Run`、SQLite 运行表、PostgreSQL 迁移
  `007_run_execution_profile.sql`、项目 Bundle 恢复和领域 JSON Schema，并兼容历史 Run 响应。
- 新增 `apps/agent/test_runner.py`，覆盖 Python 执行、路径/命令/环境变量白名单、执行模式、网络
  fail-closed 和 Adapter 上下文防伪；本地 Agent 常规测试达到 26 项通过。
- P3-10 新增 `packages/agent_protocol/session.schema.json` 和会话协议模型：`SessionPeer`、
  `SessionRunContext`、`SessionIpcRequest`、`SessionIpcResponse`、`SessionIpcEvent`。
- P3-10 新增 `apps/agent/session_worker.py` 的传输无关 `SessionWorkerBroker`，实现 Worker 注册、
  Peer/SID 绑定、执行模式和能力白名单、用户会话状态、桌面审批、请求幂等和 Worker 生命周期门禁。
- P3-10 收紧 Run 生命周期：启动/停止均要求 `session.run` 能力；注销后允许停止已有 Run 进行清理，
  不允许停止不存在的 Run；运行中状态上报不会覆盖活动状态；停止 Worker 拒绝新的普通请求。
- 新增会话权限、生命周期、幂等和共享 Schema 测试；本地 Agent 常规测试达到 39 项通过。
- P3-11 新增 `apps/agent/named_pipe_transport.py`，使用 Windows 原生 `ctypes` 实现双工 byte-mode
  Named Pipe、4 字节长度前缀 JSON 帧、最大帧限制、连接循环和显式关闭。
- P3-11 使用 SDDL 创建 Pipe ACL，拒绝远程客户端；通过 Named Pipe client PID、进程 Token SID 和
  `ProcessIdToSessionId` 认证对端，并将认证结果转换为 `SessionPeer`。
- `PipePeerPolicy` 支持多个受信 SID 和显式允许的 Windows Session ID；Machine Service 的服务身份与
  交互用户 SID 分离；服务端在调用 Broker 前拒绝 SID/Session 不匹配客户端。
- 新增 Named Pipe 帧编解码、同机多请求、同机身份认证、Session ID 拒绝和跨平台保护测试；
  本地 Agent 常规测试达到 47 项通过、1 项非 Windows 分支跳过。
- P3-12 扩展 `LocalProcessSupervisor` 和 `LocalRunner`，支持 USER_SESSION 的 stdin 写入、可停止
  Runner 生命周期、实时 stdout/stderr 回调和共享 Run 总输出预算；清理 `ProcessSpec` 重复字段。
- 新增 `apps/agent/session_runtime.py`，将 Session Broker、Runner 和本地事件队列串接；授权的
  `session.run.start` 会启动用户会话模式进程，终端输入可转发，退出会生成有序 process/run 事件。
- 新增 `SessionRunStartPayload` 及其 Session Schema 定义；`INTERACTIVE_DESKTOP` 没有桌面执行器时
  fail-closed，不伪造 ConPTY 或降级后台执行。
- `agentd.py session-worker-run` 提供 Windows Named Pipe User Session Worker 入口，可配置工作区、
  Python Adapter、受信 SID/Session 和环境白名单；Runtime 事件可写入既有本地 Gateway 队列。
- 新增 Runner、Runtime、Named Pipe 到 Runtime 到 Runner 纵向链路测试；本地 Agent 常规测试达到
  58 项通过、1 项非 Windows 分支跳过。
- P3-13 新增 `AgentEventPayload` 和 Gateway `agent.event` 正式事件接入：项目能力、项目/Run/Agent
  归属会在服务端再次校验，事件幂等落库，运行终态事件会完成平台 Run。
- P3-13 新增 `RuntimeEventUploader`、`OutputDiscovery`、`RunManifestBuilder` 和 `ResultUploader`：
  显式工作区输出会计算 SHA-256/大小/MIME，写入本地 `pending_uploads`，并通过 Agent 专用 Artifact
  接口完成单文件或 Multipart 上传。
- P3-13 Agent Artifact 接口要求项目能力 Token、`X-Agent-Id` 和请求幂等键；输出 Artifact 必须绑定
  同 Agent Run，仍需人工 Review 才能作为正式下游输入。
- P3-14 共享会话协议新增 `session.terminal.resize`、`TerminalSize` 和
  `SessionLifecycleTransition`；JSON Schema 与 Pydantic 模型同步。
- P3-14 `LocalRunner` 只通过显式 `TerminalResizeEnforcer` 执行 PTY resize；当前标准管道没有该后端时
  返回稳定错误，不伪造 ConPTY 能力。
- P3-14 新增 `MachineServiceLifecycle`，覆盖服务启动、会话发现、Worker 就绪、锁屏/解锁、注销、
  Worker 故障、显式恢复、服务停止和本地紧急停止动作；新增生命周期状态机测试。
- P3-14 本地 SQLite 新增 `lifecycle_transitions` 恢复/审计表，`agentd session-worker-run` 可
  幂等保存 Broker 生命周期转移。
- P3-15 已接入真实 Windows ConPTY 开发版：`pywinpty>=2.0,<3`、能力探测、PIPE/CONPTY 显式路由、
  stdin、合并输出、resize、取消、超时和本地 Run 状态记录；ConPTY 不可用时 fail-closed。
- P3-16 已新增 `apps/agent/windows_service_adapter.py`：SCM 安装/更新/卸载/启停/状态/失败恢复配置、
  WTS 会话发现、`WTSQueryUserToken` + `CreateProcessAsUserW` Worker 启动、Worker 退出恢复、WTS 会话
  事件映射和 `StartServiceCtrlDispatcherW` 宿主边界。服务账户不会伪装成交互用户，Worker 启动绑定
  Windows Session ID、SID、Pipe、命令和工作目录并可回调本地审计；服务启动后通过受保护的
  `session.hello` readiness probe 才进入 `worker.ready`；`agentd service-host` 已串联按 Session 生成
  Worker 命令的服务宿主入口。
- P3-17 已新增 `apps/agent/cli_adapters.py`：Codex `exec --json` 版本探测、`0.153.x` 兼容性矩阵、
  无 Shell 命令构造、危险参数和工作区路径阻断、增量 JSONL 事件解析，以及 Claude Code 的
  `NOT_INSTALLED`/`UNSUPPORTED` 明确能力状态。
- P3-17 已将 Codex 语义事件接入 `SessionWorkerRuntime`，保留原始 stdout，并归一化为
  `run.started`、`run.completed`、`run.failed`、`tool.*`、`approval.requested`、`file.changed` 和
  `agent.message`；未知协议事件会 fail-closed。`LocalRunner` 执行具备能力探测的 Adapter 前要求
  兼容性检查通过；新增 `agentd cli-capability --adapter codex|claude` 诊断入口。
- P3-18 已将设备注册收紧为 Ed25519 私钥持有证明：pairing 返回一次性 challenge，数据库只保存挑战哈希；
  注册签名绑定 pairing、challenge、agent、明确的 device ID 和规范化公钥指纹；SQLite 与 PostgreSQL
  Repository、迁移、CLI 和错误/重放专项测试已同步。
- P3-19 已新增 Windows Credential Manager 凭据适配器、设备 Token 目标名、保存/读取/删除 CLI；
  `gateway-run` 和 `service-run` 在未提供显式 Token 时默认从系统凭据存储读取，并在长期持久化失败时
  fail-closed，不回退到 Session 凭据。
- P3-20 已新增设备 Token 轮换契约：管理员可原子替换 Token，旧 Token 立即失效，现有 Gateway 连接被撤销，
  新 Token 只在轮换响应中返回；SQLite/PostgreSQL 均保存 Token 版本、轮换时间和不含秘密值的轮换审计记录。
- P3-21 已将 PostgreSQL/MinIO 条件集成入口更新为当前 `001` 至 `009` 迁移集合，加入二次执行幂等性、
  P3-18/P3-20 字段、RLS、设备 Token 轮换事务、旧 Token 失效、连接撤销和并发版本串行化检查；
  当前环境未提供真实 PostgreSQL/MinIO，故仅完成验收准备，未宣称生产通过。
- P3-22 已新增 Gateway `agent.handoff.create`、`agent.handoff.accept` 和 `agent.review.submit`；
  Gateway 会校验项目能力、资源归属、接收 Agent 身份，并禁止 Agent 提交人工 `APPROVED`。
- P3-22 已补齐 `PostgresRepository.create_handoff()`、`accept_handoff()` 和 `create_review()`；
  PostgreSQL Review 会在同一事务中更新目标、Gate、Review、Event、EventOutbox 和幂等记录。
- SQLite Handoff/Review 幂等记录已保存请求指纹；重复请求返回原对象，同一键提交不同请求会返回冲突。
- P3-22 交接文档已完成：`docs/handoffs/P3_22_GATEWAY_HANDOFF_REVIEW_HANDOFF.md`，状态为 `PASS_WITH_ASSUMPTIONS`。
- P3-23 已新增 `agent.artifact.create`、Gateway 命令请求指纹和迁移 `010_gateway_command_request_hash.sql`；阶段 3 开发版退出评审和交接文档已完成。
- P4-01 已新增 Handoff `REJECTED` 收据、拒绝原因/风险、返工修订链、`AGGREGATE` 输入交接和下游 Handoff Gate 门禁；新增迁移 `011_workflow_review_relations.sql`。
- P4-01 已统一 `fatal`/`major`/`minor` finding 规则；未解决 `fatal` 或 `major` 禁止 Review `APPROVED`，Review 保存 Evidence 引用，Gate 保存 Review/Evidence 引用和风险摘要。
- P4-01 已新增 `agent.handoff.reject`、HTTP Handoff 拒绝接口、`GET /api/projects/{project_id}/review-center` 和接力/汇总/风险链/Gateway 拒绝契约测试。
- P4-02 已新增 `HandoffReceipt` 逐接收方收据、`handoff_receipts` 表和 Fanout 全量确认门禁；新增 `RiskRegistryEntry` 独立风险记录、责任人/关闭理由/关闭证据/重新打开操作，以及 `PATCH /api/projects/{project_id}/risks/{risk_id}`。
- P4-02 已新增 Gate `input_snapshot`、`INVALIDATED` 状态和输入变化自动失效事件；HTTP Agent Handoff 接收/拒绝路由已使用 `X-Agent-Id` 与项目能力 Token；Bundle 导出恢复已包含 receipts、risks 和 Gate 快照。
- P4-03 已将审核中心接入真实 `review-center` 数据，展示 Gate 状态/失效原因、风险登记操作、Evidence 关联和 Fanout 逐接收方收据；风险分配、关闭、重新打开可从前端提交并在成功后刷新项目状态。
- P4-03 已在 SQLite/PostgreSQL 开发版 Repository 增加 Gate 失效影响图传播：遍历任务依赖、成果物派生、交接汇总和任务产出的交接，将受影响任务置为 `BLOCKED` 或 `NEEDS_REVISION`，并失效已通过的下游 Task Gate、写入审计事件。
- P4-05 已将前端侧栏项目、任务、交接和 Agent 数量改为真实数据派生，补充审核中心、风险登记、交接收据、任务流和导航的稳定 `data-testid`；移动端导航锚点点击后会自动收起菜单。
- P5-01 已为 `RunnerRequest`、`SessionRunStartPayload` 和共享 Session Schema 补齐代码/环境/参数/工具/数据访问上下文；`RunManifestBuilder` 增加声明与观察输入摘要、信息边界状态、流输出字节数和 Manifest SHA-256。
- P5-02 已新增 `apps/agent/container_runner.py`，提供 Docker/Podman 运行时解析、镜像 digest/路径/执行模式策略、默认无网络/只读根文件系统/丢弃能力/资源限制命令构造和本地监督器接入；真实容器运行与 Session 默认路由仍未完成。
- P5-03 已将 `host/container` 执行后端显式加入 Session/Runner 协议；Session Worker 可按配置路由到 Docker/Podman Runner，容器运行时未配置时 fail-closed；工作区固定只读，输出目录独立可写，输入与可写输出重叠时拒绝；容器调用以脱敏后的 `execution` 字段进入 Run Manifest；新增容器边界和 Session 路由契约测试。
- P5-04 已新增 `apps/agent/information_boundary.py`：统一文件访问观察、声明输入/实际读取、工作区外路径、决策时间、数据截止时间和未来数据审计；观察器缺失、失败或无记录时 fail-closed；`RunManifestBuilder` 已接入该审计结果；新增信息边界契约测试。
- P5-05 已新增 `apps/agent/reproducible_rerun.py`：从通过信息边界审计且稳定哈希自校验通过的 Manifest 生成新 Run 请求，恢复代码/环境/参数/种子/数据访问策略，迁移工作区路径，比较输出哈希、稳定复现哈希和重跑信息边界；容器运行时和镜像上下文会被保留，环境变量名称集合不一致时拒绝重跑。
- P5-06 已新增 `apps/agent/access_observer.py` 和网络观测契约：统一 JSONL 文件/网络事件、线程安全记录、fail-closed 捕获控制、网络策略审计，并将网络观测字段接入 Runner、Session、Manifest 和重跑规划；Linux eBPF/容器观察器及 Windows ETW 生产验收仍未完成。
- P5-06-REAL 已新增 `apps/agent/windows_etw_observer.py`：使用 Windows 原生 `logman`/`tracerpt` 采集并解析 Kernel File/Network ETW，按真实 Runner PID 和轮询得到的进程树过滤事件；`LocalProcessSupervisor`、`LocalRunner`、`SessionWorkerRuntime`、`RunManifestBuilder` 已接收并记录观察结果。该适配为开发版，真实管理员权限采集、事件字段矩阵、容器观察和网络阻断仍未完成。
- P5-06-REAL 新增 `apps/agent/test_windows_etw_observer.py`，专项 5 项通过；Agent 全量回归为 139 项通过、1 项跳过。
- P5-06-REAL 已为 `agentd session-worker-run` 增加显式 `--access-observer windows-etw` 和 `--etw-output-directory`，并为 PIPE/CONPTY/Hybrid 监督器补齐真实 OS PID 查询；默认观察器保持关闭。
- P5-06-REAL-ADMIN 免管理员部分已完成：`AccessObservationCapture`、`ProcessResult`、`RunnerRequest` 和 Run Manifest 已贯通观察器诊断链（session 名、provider 配置、ETL/CSV SHA-256、启动/停止/转换返回码、绑定 PID 集合、清理状态）；诊断不进入 `reproducibility_hash`，不会破坏可复现重跑比较。
- P5-06-REAL-ADMIN 信息边界审计已支持 `runtime_dependency_roots` 策略分类：解释器/依赖库等工具运行时读取记录为 `runtime_dependency` 类别且不触发未声明输入 violation，显式声明的输入仍按任务输入判定；观察记录带 `category` 字段。
- P4-04-RUN 已完成真实基础设施集成验收：用户态 PostgreSQL 16.9（127.0.0.1:54329）和 MinIO（127.0.0.1:9100）由 `scripts/local-infra.ps1` 管理，无需管理员权限；阶段 2 集成 3 项、阶段 4 集成 4 项全部通过，包括迁移 001-013 二次幂等、全部租户表 FORCE RLS、非超级用户 owner 连接的跨租户隔离、设备 Token 并发轮换串行化、Fanout 收据/风险/Gate 失效传播多实例并发和 outbox `SKIP LOCKED` 互斥、MinIO 对象哈希与跨实例 Multipart。
- 集成测试修复了三处真实服务才暴露的缺陷：S3 分块除最后一块外需 ≥5MiB、PG 清理顺序需满足外键依赖、RLS 验收需非超级用户角色（超级用户天然绕过 RLS，`FORCE ROW LEVEL SECURITY` 亦不生效）。
- P4-04-RUN-PROD 已新增迁移 `014_runtime_role_and_grants.sql`：建立集群级 `app_runtime` 运行时角色（`NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`，仅 DML，撤回 schema public 的 CREATE），并授予 schema USAGE、应用函数 EXECUTE、全表 SELECT/INSERT/UPDATE/DELETE、序列 USAGE/SELECT 和后续对象默认权限。迁移对非超级用户迁移身份容错：`CREATE ROLE`/`ALTER ROLE`/`GRANT`/`REVOKE` 在 `insufficient_privilege` 时记录 NOTICE 而不中断，使租户级隔离数据库也能应用迁移。
- P4-04-RUN-PROD 已把 PostgreSQL 认证切换为 scram-sha-256：`scripts/local-infra.ps1` 新增 `provision` 动作，通过临时 trust 窗口设置 `platform`/`app_runtime` 密码后恢复 scram 规则；实测 `platform password login: True`、`app_runtime password login: True`。
- P4-04-RUN-PROD 已实测 `app_runtime` 最小权限：`(rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin) = (False, False, False, False, True)`，`CREATE TABLE`、`SELECT * FROM pg_authid`、`CREATE ROLE` 均返回 `InsufficientPrivilege`，未设置组织作用域时全部租户表可见 0 行。
- P4-04-RUN-PROD 已硬化 `PostgresRepository`：新增 `PostgresRepositoryError` 稳定错误码（`postgres_pool_timeout`、`postgres_connection_unavailable`）、连接池参数校验（`postgres_pool_min_size_invalid`、`postgres_pool_size_range_invalid`、`postgres_pool_timeout_invalid`、`postgres_pool_max_idle_invalid`、`postgres_pool_max_lifetime_invalid`、`postgres_pool_max_idle_exceeds_lifetime`）、`timeout`/`max_idle`/`max_lifetime` 参数和 `pool_stats()` 容量/等待指标。
- P4-04-RUN-PROD 新增 `apps/api/test_postgres_runtime_role.py`，6 项运行时角色验收全部通过：密码认证登录、DDL 与 pg_authid 拒绝、无作用域不可见、跨租户 RLS 隔离、以运行时角色完成 Task/Handoff/收据/outbox 业务流、单池耗尽返回稳定 `postgres_pool_timeout`、后端被终止后返回稳定错误并自动恢复。
- 修复迁移 014 引入的两处回归：平台契约测试补充 014 迁移名；stage4 RLS 验收改用带密码的一次性 owner 角色（DDL 中不使用绑定参数），并让 `DROP DATABASE`/`DROP ROLE` 只在对象存在时执行，避免清理错误掩盖真实失败。
- 阶段 6 数学建模模板已完成 pack 骨架：新增 `packages/competition_packs/`（`loader.py`、`materializer.py`、`validation.py`、`__init__.py`）与 `packages/competition_packs/cumcm/`（`manifest.json`、`four_question_dag.json`、`schemas/problem_facts_schema.json`、`schemas/data_profile_schema.json`、`templates/` 9 个模板）。
- CUMCM pack manifest 声明 9 个模板（赛题分析、题面事实、数据画像、建模报告、代码说明、实验计划、官方结果表、独立复核、论文结构）、9 项必需成果物、按成果物类型的校验规则（必需章节/最少字数/必需与禁止 JSON 字段/官方文件名校验/哈希要求）、四问覆盖规则（期望问题 [1,2,3,4]，接受中文数字与阿拉伯数字标记）以及信息边界规则（deny 未来数据、要求声明输入、观察模式 system、观察缺失 fail-closed）。
- pack 版本升级规则已实现并验证：`plan_upgrade('1.0.0')` 返回 1.1.0 兼容升级，含新增成果物 `capability_checklist`/`cross_problem_ledger` 与重命名 `review.md → COMP_REVIEW.md`；降级被拒绝，环依赖与非平台成果物类型在加载/自检时 fail-closed。
- 四问任务 DAG 已落地为平台任务：`PackMaterializer.apply()` 按拓扑顺序创建 17 个任务并写入 `dependency_task_ids`（例如「问题一代码与计算」依赖「问题一建模」），可选择问题子集（`questions=[1]` 只物化对应分支），并把 9 个渲染后的模板骨架写入工作区；重复执行完全幂等（第二次创建 0 个任务、0 个成果物），已存在同名成果物时仍补齐缺失的工作区骨架文件。
- 校验器 `PackValidator.validate()` 已可区分真实缺陷与误报：合规项目返回 `PASS`（0 findings、覆盖 [1,2,3,4]）；缺少 `result_table`/`paper_source`/`review_report` 为 fatal（BLOCKED），缺少其他必需成果物为 major（NEEDS_REVISION）；模板骨架按 `data_policy.template_id` 识别并豁免官方文件名校验，pack 管理的正式结果表要求 `result1..4.xlsx` 命名（major），导入的遗留结果表仅提示 minor；文本不可读时不判定问题覆盖，避免把可读性问题伪装成内容缺失。
- 竞赛 pack 已接入 HTTP（5 个端点）：`GET /api/competition-packs`（pack 概要）、`GET /api/competition-packs/{pack_id}`（模板/DAG/规则详情，含占位符清单）、`GET /api/competition-packs/{pack_id}/templates/{template_id}`（骨架导出）、`GET /api/projects/{project_id}/competition-pack`（项目视角 pack + 物化进度）、`POST /api/projects/{project_id}/competition-pack/apply`（物化，`Idempotency-Key` 必填且同键重放返回原响应）、`POST /api/projects/{project_id}/competition-pack/validate`（按 pack 规则校验并返回可喂给 Review 的报告）、`GET /api/projects/{project_id}/competition-pack/templates/{template_id}`（按项目上下文渲染导出，支持 `questions=1,2`）。
- pack 物化语义已收紧：空骨架不授予四问覆盖，但团队填写内容后哈希改变即按真实交付物参与校验（`data_policy.template_hash` 精确区分"未填写骨架"与"已填写交付物"），避免写完的报告被永久当成空模板。
- Web 工作台已接入建模模板包面板：pack 名称/版本/问题数、物化进度（任务与成果物齐备度、待补齐清单）、问题范围筛选（问题一~四，默认全部）、9 个模板清单与"预览导出"、一键应用、运行模板校验，以及校验报告（状态、覆盖面、发现列表按 fatal/major/minor 着色）；模板预览弹窗支持复制与下载渲染后的骨架。侧边栏新增 `nav-pack` 入口并在未完成物化时显示提醒点；面板关键节点均带 `data-testid`（pack-panel/pack-progress/pack-template-list/pack-apply/pack-validate/pack-validation-report/pack-preview-modal）。
- `apps/web/lib/api.ts` 新增 pack 客户端（8 个类型 + 5 个函数），应用模板包时同时发送 `Idempotency-Key` 请求头与请求体幂等键。
- 数模审计脚本已可生成机器 Review（`POST /api/projects/{id}/competition-pack/review`）：把 pack 校验结果落成平台 Review + Gate，`fatal → BLOCKED`、`major → NEEDS_REVISION`；严重度按平台口径映射（pack 的 `info` 降级为 `minor`）；Review 挂载目标优先落在**已填写的交付物**（跳过未改动骨架），其次骨架槽位，最后「独立复核与一致性检查」任务；目标缺失返回 404 `machine_review_target_missing`。
- 机器审计遵守人工批准门禁：干净结果（PASS / PASS_WITH_ASSUMPTIONS）**不会自动创建 Review**，而是返回 `created=false` 与 `machine_review_clean_requires_human_approval`，正式批准仍必须由人工提交（平台 `human_approval_required` 规则）。
- 信息边界审计 Gate：`scope="information_boundary"` 只把信息边界类发现（`future_data_allowed`、`information_boundary_missing`）写入 Review/Gate；同一幂等键重放返回同一 Review。
- 新增 7 项机器 Review 契约测试（major→NEEDS_REVISION 且 Gate FAILED、幂等重放、干净结果不自动批准、边界范围只报边界码、非法 scope 双层拦截、显式成果物目标、空项目 404）；API 全量回归 138 项通过。
- 现有 C 题交接包已支持可解释导入（`POST /api/projects/{id}/imports/cumcm-handoff`）：识别 `00_复审入口` / `01_项目工作区` / `02_外部参考*` / `03_工作流插件` 布局（**前缀匹配**，兼容带后缀的真实目录名），按目录映射成果物类型，返回可解释报告（布局识别、声明/导入/跳过计数、按类型分类计数、未识别文件清单、哈希不符清单、lossless 判定）。
- 未分类文件走**可解释回退**登记为 `problem_source`，绝不静默丢弃；`lossless` 仅在所有参与文件入库且哈希一致时为真。
- 真实交接包实测：`declared 1964 = imported 1964`、`skipped 0`、`lossless True`、哈希不符 0、未分类回退 57 个（清单可查）；分类结果为 paper_source 943、problem_source 454、code 321、result_table 176、figure 16、compiled_pdf 13、审计/复核各 10、题面事实/数据画像/建模/题面分析各 5-6；重复导入幂等（0 新建 / 1964 跳过）；导入后可直接继续执行 pack 校验（报真实缺口 `experiment_plan`）。
- 新增 `apps/api/test_cumcm_handoff_import.py` 7 项契约测试（dry-run 不写盘、无损且可解释、源路径与哈希保留、重复导入幂等、5 个阶段任务只建一次、导入后可继续校验、源目录缺失拒绝）；API 全量回归 145 项通过。
- 阶段 7 已落地文档三层版本最小切片：`apps/api/app/document_api.py` 把 pack 文档模板（论文/建模报告/复核报告）纳入平台既有成果物版本链、Review/Gate 与 Evidence 原语，映射为 **draft（DRAFT，可编辑）/ submitted（PENDING_REVIEW，冻结待审）/ approved（APPROVED，不可变且允许下游）**，`REJECTED` 与再次起草都回到 draft 层。
- 三层门禁规则已固化：**提交必须带证据**（`store.submit_artifact_for_review`，无证据或证据不属于该成果物时返回 `document_evidence_required`/`document_evidence_not_linked_to_artifact`，草稿因此无法绕过正式版本门禁）；**批准只能由人工 Review 产生**（agent 提交 `APPROVED` 仍被 `human_approval_required` 拒绝）；**修订只新增草稿版本**（`store.revise_artifact` 复制出 DRAFT 新版本并经 `parent_artifact_id` 串链，已批准版本保持不可变）。
- 新增 4 个端点：`GET /api/documents/layers`（三层定义与门禁规则）、`GET /api/projects/{id}/documents/{artifact_id}/timeline`（版本链与成员/Agent/任务/Git/审批追溯）、`GET .../documents/{artifact_id}/evidence`（证据链查询）、`POST .../documents/{artifact_id}/submit`、`POST .../documents/{artifact_id}/revise`。
- 新增 `apps/api/test_document_layers.py` 14 项契约测试（三层定义、新建即草稿、无证据提交被拒、提交冻结、提交幂等、拒绝未关联证据、agent 不能批准、人工批准解锁下游、修订新增草稿且保留历史、已批准版本不被改写、可追溯到成员/类型/Git commit、证据链查询、未知文档 404）；API 全量回归 159 项通过。
- 真实服务验证通过：新建文档 draft → 无证据提交 400(`document_evidence_required`) → 带证据提交 submitted → 人工 Review 后 approved（downstream_allowed=True、immutable=True、approved_by=member-001）→ 修订派生新 draft（链为 `['approved','draft']`，旧版本未变）→ 证据链查询返回 claim。
- Web 工作台已接入文档版本面板（`data-testid="document-panel"`）：左侧列出文档类成果物（paper_source/model_spec/problem_analysis/audit_report/review_report/submission_bundle/experiment_plan）并按草稿/提交/批准显示层级徽标；选中后展示三层版本时间线（每版内容哈希、创建者与成员/Agent 类型、Git commit、批准人、是否允许下游、证据数）与证据链列表；提供「提交待审」「派生新草稿」动作，提交缺证据时前端提示"提交需要先关联证据"。侧边栏新增 `nav-documents` 入口。
- 文档面板数据链路已与真实 API 联调验证：dashboard 返回带状态的成果物 → 前端按文档类型过滤 → timeline 返回 `draft` 层与 `member/member-001` 追溯 → pack 面板同时拿到物化进度；Next.js 生产构建通过，SSR HTML 中 `document-panel`/`document-list`/`nav-documents`/`pack-panel` 标记齐备。
- 阶段 7 已补齐**版本 Diff 与合并确认**：`GET /api/projects/{id}/documents/{artifact_id}/diff`（支持 `from_revision`/`to_revision`，输出 unified diff、增删行数与 hunk 统计，以及层级/Git commit/任务/运行/作者/批准人的逐项变化），两侧内容必须已落库否则 `document_content_unavailable` fail-closed；`POST .../merge` 选定一方版本内容派生**新的草稿版本**并留痕（`document.merged` 事件），已批准版本不可作为合并目标（`document_approved_is_immutable`），项目登记本地 Git 仓库时会校验 `git_commit` 真实存在（`git_commit_not_found`）。
- 阶段 7 已补齐**评论/建议、文档快照与关系图谱**（全部落在平台事件流上，无需新增表）：`POST/GET .../comments`（`kind=comment|suggestion`，可锚定段落）、`POST/GET .../snapshots`（记录当前版本的 revision 与 content_hash 检查点）、`POST/GET .../relations`（`artifact|figure|result_table|run|task ↔ 文档段落`，跨项目目标返回 404）、`GET /api/projects/{id}/impact?target_type=&target_id=`（**影响面定位**：某结果/图表/运行变化后受影响的文档与段落清单）。
- 新增 `apps/api/test_document_diff_merge.py` 8 项与 `apps/api/test_document_activity.py` 9 项契约测试；API 全量回归 176 项通过。
- 阶段 7 前端已接入 Diff/合并/评论/快照/关系：文档面板新增五个标签页（版本/差异/评论/快照/关系，`document-tab-*`），差异页展示 unified diff、增删行统计与 Git/作者变化并提供「按第 N 版合并」（`document-merge`）；评论页支持评论/建议与段落锚点（`document-comment-input`/`document-comment-submit`）；快照页创建检查点（`document-snapshot`）；关系页可关联图表/结果表/成果物/运行到文档段落并一键「查影响面」（`document-relation-link`/`document-impact`/`document-impact-result`）。
- 阶段 7 已实现**多人同时编辑（Yjs CRDT）**：Web 端新增 `apps/web/lib/collab.ts`（Y.Doc + Y.Text + 增量更新 base64 编解码 + 最小插入/删除换算，保留并发合并语义），通过平台既有项目 WebSocket 中继；服务端新增 `apps/api/app/collaboration.py`（帧校验：类型白名单、文档 id 必填、base64 校验、256KB 上限、fail-closed 错误码）与 `ConnectionManager.relay()`（同项目转发、**不回声给发送者**、按项目隔离、自动剔除断连）。文档面板新增协作编辑区（`document-collab`/`document-collab-editor`/`document-collab-status`/`document-collab-presence`），并明确协作内容是内存 CRDT、定稿仍需「提交待审」写入正式版本。
- 协作验证证据：`apps/web/scripts/verify-collab.mjs` 用真实 yjs 13.6.32 验证两端并发编辑收敛一致、双方编辑均保留、同段落并发插入不互相覆盖（`COLLAB_CONVERGENCE_OK`）；`scripts/verify-collab-relay.py` 在真实 WebSocket 服务上验证中继送达、无回声、非法帧返回 `collaboration.error`、presence 转发（`RELAY_E2E_OK`）；新增 `apps/api/test_collaboration.py` 7 项契约测试；API 全量回归 183 项通过。
- 阶段 6 的**信息边界审计 Gate** 已接入平台审计路径：新增 `apps/api/app/boundary_gate.py`，把 pack 的 `information_boundary` 规则（deny 未来数据、要求声明输入、要求 `system` 观察、观察缺失 fail-closed）与平台运行事实（`Run.information_boundary` 的 allowed/violations、`data_access_policy`、`observed_input_files`、任务级 `allow_future_data`）逐条比对，产出 `POST /api/projects/{id}/competition-pack/boundary-gate`（落库 Review + Gate）与 `GET .../boundary-audit`（只读预览）。违规 → `BLOCKED`、观察缺失/模式不符/任务级未来数据冲突 → `NEEDS_REVISION`，干净结论不自动批准。目标优先取任务，其次运行产出成果物，最后项目结果表槽位；缺目标返回 404 `boundary_gate_target_missing`。
- 阶段 6 的 **competition-workflow 脚本适配**已落地：新增 `apps/api/app/competition_workflow_adapter.py` 与 CLI `scripts/adapt-competition-workflow.py`，把外部插件的技能映射到 pack 阶段与产出成果物类型、模板映射到 pack 模板 id、检查脚本映射到平台审计入口，未识别资产显式列出（不静默丢弃）。对真实插件实测：12 个技能（11 个映射，`feishu-notify` 属通知类未映射）、9 个模板（8 个映射，README 未映射）、4 个检查脚本（能力清单/AI 披露/文献真实性）映射完成；适配清单已落盘 `docs/competition_workflow_adapter.json`。
- 新增 `apps/api/test_boundary_gate.py` 10 项与 `apps/api/test_competition_workflow_adapter.py` 6 项契约测试；真实服务验证信息边界审计端点（无运行记录时给出 `boundary_audit_no_run` 且 `created=false`）；API 全量回归 199 项通过。
- 阶段 8 交付链路已完成开发版：新增 `apps/api/app/delivery.py`，实现「批准素材装配」（只读取 `APPROVED` 成果物，按 pack 论文结构装配章节并保留来源哈希/批准人/任务/Git 追溯，未批准素材显式列入 `excluded_unapproved`）、「答辩提纲」（Marp 兼容 Markdown，每页标注来源与批准人）、「交付检查」（图表引用、引用文献闭合、公式标签、匿名、附件齐备、页数）、「提交包生成与冻结」（装配+检查+清单写入 `submission_bundle` 成果物并提交待审，人工批准后成为不可变冻结版本）、「跨部署恢复验证」（按 artifact_id 解析来源哈希，并可在第二套 Store 上恢复比对）。
- 阶段 8 新增 5 个端点：`GET /api/projects/{id}/delivery/assembly`、`GET .../delivery/slides`、`POST .../delivery/checklist`、`POST .../delivery/submission-bundle`（幂等键必填）、`GET .../delivery/submission-bundle/{artifact_id}/verify?restore_check=`。
- 真实服务端到端 Demo 验证通过：装配 3 章节/3 来源（含哈希与批准人）→ Marp 提纲 4 页 → 交付检查 3 项 pass + 2 项 fail（缺编译 PDF 与页数，本机无 LaTeX）→ 提交包 `submitted`（3 来源/3 章节）→ 校验 `verified=true` 且**跨部署恢复成功**（隔离 Store 恢复 4 个成果物、哈希一致）。
- 第一版 Demo 一键启动：新增 `scripts/start-demo.ps1`（基础设施 → API → Web，首次运行自动生产构建，打印工作台/API 文档/模板包/文档三层/MinIO 入口并在 Ctrl+C 时清理）。实测：PG/MinIO 健康、`/health` ok、Web 200、`/api/competition-packs` 200、`/api/documents/layers` 200。脚本与 `local-infra.ps1` 已加 UTF-8 BOM（Windows PowerShell 5.1 按 ANSI 读取会导致中文乱码破坏语法），并改用父进程环境变量传递（5.1 的 `Start-Process` 无 `-Environment`）。
- 新增 `apps/api/test_delivery.py` 12 项契约测试；API 全量回归 211 项通过。
- 阶段 9 最小切片已完成：新增 `apps/api/app/observability.py` 与 4 个端点——`GET /api/platform/health`（探活：状态/版本/关键计数/outbox 待投递）、`GET /api/platform/metrics`（平台事实汇总：项目/任务按状态/运行按状态/成果物/已批准/事件/Review/Gate/设备/存储字节）、`GET /api/platform/quota`（限额与成本单价）、`GET /api/projects/{id}/usage`（单项目用量：任务/运行/成果物/事件/能力 Token、存储分解、**标注为估算**的成本与配额状态）。
- 配额判定已接入写路径：`enforce_quota` 在创建运行与登记成果物时检查 `max_runs_per_project` / `max_artifacts_per_project` / `max_storage_bytes_per_project`，超限返回 **429** 与稳定错误码（`quota_exceeded_runs` 等）；限额与成本单价可由 `PLATFORM_QUOTA_*` / `PLATFORM_COST_*` 环境变量覆盖（非法值回落默认）；`quota_status` 与 `enforce_quota` 采用同一口径（`used >= limit` 即无余量）。
- 自托管部署已补全：新增 `apps/api/Dockerfile`（依赖分层缓存、`/api/platform/health` 作为容器 HEALTHCHECK）、`apps/web/Dockerfile`（`NEXT_PUBLIC_API_URL` 作为构建参数，因 Next.js 在构建期内联）、重写 `infra/docker-compose.yml`（postgres/minio/nats/api/web 五服务，含健康检查、依赖顺序、配额与成本环境变量透传、端口可覆盖）与 `infra/README.md`（本机 Demo 与 Compose 两条路径、生产三件必做：改默认口令、用非 owner 的 `app_runtime` 角色、配置配额与单价；并列出 Helm/K8s、真实跨机恢复、备份演练、OTel 导出等待办）。
- 真实服务验证：健康返回 `ok`（9 项目/22 任务/21 成果物）、指标含存储 24396 字节与 outbox 74 条待投递、配额与单价正确下发、项目用量返回 4 成果物（3 已批准）/17 事件/3 个能力 Token/3655 字节存储与估算成本；compose 结构解析通过（5 服务齐全）。
- 新增 `apps/api/test_observability.py` 11 项契约测试；API 全量回归 222 项通过。
- 后续统一测试第一批已完成①**浏览器交互验收**（真实 Chrome/IAB）：工作台加载正常（项目总览、指标、审核门禁）；建模模板包面板显示 v1.1.0·4 问、9 个模板清单与问题范围筛选，**点击「一键应用模板包」后物化进度由 1/26(4%) 变为 26/26(100%)、任务 17/17、成果物 9/9**；文档版本面板显示三层状态（4 草稿 / 1 提交 / 2 批准），打开文档后时间线（草稿、第 1 版、哈希、批准人）与五个标签页（版本/差异/评论/快照/关系）齐备，**协作编辑区显示「已连接 · 在线 1 人」**；在浏览器内提交评论成功并即时显示（UI→API→事件流全链路）；截图已留存。
- 后续统一测试第一批已完成②**LaTeX 真实编译**：安装 MiKTeX 25.12（用户级、免管理员），新增 `apps/api/app/latex_compile.py`（引擎解析 PATH + MiKTeX 用户目录、二次编译交叉引用、`pypdf` 优先解析页数并回退正则启发式、引擎缺失/编译失败 fail-closed）与 `POST /api/projects/{id}/delivery/compile`（编译最新已批准 `paper_source` 或显式源码并登记 `compiled_pdf`）；交付检查的 `page_count` 由占位改为**真实页数**。实测：中文 ctexart 论文编译为 **2 页 PDF（47,065 字节）**，`page_count` 与 `attachments` 检查双双 `pass`，PDF 可下载且以 `%PDF-1.5` 开头。
- 新增 `apps/api/test_latex_compile.py` 13 项契约测试（页数解析三态、引擎解析、缺引擎 fail-closed、空源码拒绝、stub 编译产物、非零退出诊断、交付编译需已批准源码、登记 compiled_pdf 与页数、内联源码编译、**真实引擎编译中文论文**）；示例论文留存 `apps/api/fixtures/sample-paper.tex`。
- 工作台已重构为多页面应用（此前是单页 DashboardShell）：新增设计系统（`app/globals.css` 重写：颜色/间距/圆角/阴影令牌、卡片/指标/徽标/表格/表单/弹窗/空状态组件类、1024/760 两级响应式与移动抽屉），共享壳层 `components/shell.tsx`（侧边栏分组导航 + 顶栏项目切换/连接状态/刷新 + 页面标题），共享上下文 `lib/workspace.tsx`（项目选择、看板/审核/模板包数据、WebSocket 联动刷新、提示条），通用组件 `components/ui.tsx`（Panel/Metric/StatusPill/Progress/Modal/FindingList）。
- 11 个页面路由全部静态预渲染并通过真实浏览器访问（均 200）：`/` 总览、`/tasks` 任务看板（阶段分组 + 新建弹窗 + 状态推进/返工/阻塞）、`/pack` 建模模板包（物化进度、问题范围、模板预览导出、一键应用、模板校验、信息边界审计预览/落库）、`/documents` 文档与版本（三层时间线、差异/评论/快照/关系五标签、协作编辑在线状态）、`/review` 审核门禁（门禁、复核意见、风险分配/关闭/重开、交接收据）、`/delivery` 论文交付（装配、交付检查、真实编译、提交包与跨部署校验）、`/artifacts`、`/handoffs`、`/runs`、`/timeline`（可搜索）、`/settings`（平台指标与配额）。
- 前端验证：`npm run build` 通过（11 路由全部静态预渲染）；浏览器漫游确认侧栏 `nav-overview/nav-pack/nav-documents/nav-delivery/nav-reviews` 与 pack 页 `pack-apply/pack-validate/pack-template-list/pack-planned-count` 定位器均唯一命中；总览与模板包页截图已留存。
- **黑夜/白天主题**已上线：`app/globals.css` 增加 `[data-theme="dark"]` 令牌覆盖（表面/边框/文字/四色语义色/阴影全套），`lib/theme.tsx` 提供上下文并把选择持久化到 localStorage；顶栏新增切换按钮（`data-testid="theme-toggle"`，月亮/太阳图标）。实测：切换后 `data-theme=dark` 生效，**刷新后仍保持 dark**（localStorage 记录），暗色云盘页截图留存。
- **个人云盘**已上线（成员级文件暂存 + 项目导入）：后端新增 `apps/api/app/personal_drive.py` 与 4 个端点（`GET /api/drive`、`POST /api/drive/upload`、`DELETE /api/drive/{file_id}`、`POST /api/projects/{id}/drive/import`）。语义：成员隔离（他人不可见/不可删）、**200MB 配额**（超限 413）、**内容寻址**（同内容去重不重复占用）、压缩包识别（zip/tar/gz/7z/rar → `is_archive`）、导入项目登记为成果物并按扩展名映射类型（csv/xlsx→result_table、pdf→compiled_pdf、tex/md→paper_source、png/jpg→figure 等，默认 problem_source）、被项目引用的文件不可删除（409）。前端新增 `/drive` 页（`nav-drive` 导航入口）：上传（多文件）、用量进度条、文件列表（类型/大小/导入状态）、删除、**加入项目空间**（确认弹窗）。真实链路实测：上传 sample-paper.tex（915 字节）→ 云盘列出 → 导入项目得 `paper_source` 成果物且哈希一致。
- 总览页已精简为"指标 + 进度 + 快速链接卡 + 摘要面板"，不再堆长列表；新增云盘入口卡。
- 新增 `apps/api/test_personal_drive.py` 10 项契约测试（上传/列表/去重/配额/删除/引用保护/导入/隔离/压缩包，隔离用真实 Session 认证路径）；API 全量回归 245 项通过；前端构建 13 路由全部静态预渲染。
- **Hyper-RAG 集成 Phase A 后端已完成**：集成决策固化在 `docs/INTEGRATION_HYPER_RAG_PLAN.md`（第二轮已拍板：检索不进门禁、MinerU 只调 API、可视化含向量库管理、hyper-rag-service 保持独立 + 8100 端口、KB 双形态）。上游 `hyper-rag-service` 已在本机 8100 端口独立运行（`hyperrag_available: true`，凭据随请求传入，服务零改动）。
- 新增 `apps/api/app/knowledge_base.py`（存储层）：**双形态知识库**（project_id 非空即项目库复用项目授权，为空即个人库可经 `kb_shares` 分享给其他成员；KB 只存文档，与个人云盘语义分离）、`kb_documents`（Markdown + content_hash + index_status + 来源 artifact/drive 引用）、`ai_settings`（成员级 LLM/Embedding/MinerU 凭据）、`kb_conversations/kb_messages`（多会话与消息，RAG 会话绑定 KB 并复用访问控制）。
- 新增 `apps/api/app/kb_gateway.py`（HTTP 代理）：对独立 hyper-rag-service 转发 `sync-batch`（索引并回写文档状态）、`query`（5 种检索模式）、`entities/relationships/entity-names/vertex-neighbor`（超图数据）、`status/sync-progress`；服务不可达映射为稳定错误码 `hyper_rag_unavailable`（路由层 502），上游 404 等映射 `hyper_rag_upstream_*`。凭据从 `ai_settings` 读取随请求传入（沿用 question-bank 协议，调用方持有模型凭据）。
- 新增 14 个端点：`GET/PUT /api/settings/ai`（凭据脱敏读取/保存）、`GET /api/projects/{id}/kb`、`GET/POST /api/kb`、`POST /api/kb/{id}/share`、`GET/POST /api/kb/{id}/documents`、`POST /api/kb/{id}/index`、`POST /api/kb/{id}/query`、`GET /api/kb/{id}/graph/entities|relationships|entity-names|vertex-neighbor/{vertex}`、`GET/POST /api/ai/conversations`、`DELETE /api/ai/conversations/{id}`、`POST /api/ai/conversations/{id}/messages`。
- 新增 `apps/api/test_knowledge_base.py` 12 项契约测试（双形态、分享权限、文档访问控制、凭据脱敏、多会话生命周期、RAG 会话越权、查询缺凭据/代理/服务不可达 502、索引状态回写、超图端点访问控制、真实服务健康检查）。**真实 Gateway 链路验证通过**：health ok / kb_status 正常 / 未索引实体查询正确映射 `hyper_rag_upstream_404`。API 全量回归 257 项通过。
- **MinerU 转换队列（Phase B 后端）已完成**：新增 `apps/api/app/convert_queue.py`——入队即返回 job_id（202 Accepted，不挂长请求）、`MineruClient`（创建任务 + 轮询 + Markdown 提取，多字段回退兼容）、`process_pending_jobs` 工作循环（并发上限 2、指数退避重试 ≤3 次、done/failed 状态回写）、`collect_to_kb`（把 done 任务的 Markdown 写入知识库文档，source_type=convert）。新增 5 个端点：`POST /api/convert`（入队）、`GET /api/convert`（列表）、`GET /api/convert/{job_id}`（查询）、`POST /api/convert/{job_id}/to-kb`（写入知识库）、`POST /api/convert/process`（手动触发一轮处理）。
- 新增 `apps/api/test_convert_queue.py` 9 项契约测试（入队/查询/not_found、工作循环完成/失败/缺凭据、重试成功、写入知识库、并发上限）；API 全量回归 266 项通过。
- **/graph 超图可视化页已完成**：新增 `app/graph/page.tsx`（三个标签页：超图探索 / 实体管理 / 关系管理）——实体搜索列表、选中实体后拉取一阶邻居子图并渲染（超边用椭圆气泡圈近似 bubble-sets，按 entity_type 着色，力导向环形布局 SVG 实现，不依赖 G6 客户端重量库）、实体/关系分页管理可跳转子图。侧边栏新增 `nav-graph` 入口。@antv/g6 + @antv/graphin 已安装（未来可升级为完整 G6 渲染）。
- **内置 AI 普通对话已接通**：新增 `apps/api/app/ai_chat.py`——代理直连成员配置的 OpenAI 兼容 LLM（凭据从 `ai_settings` 读取），支持非流式（完整 JSON 响应）与 **SSE 流式**（逐块 `data: {"delta":"..."}`）两种模式；`POST /api/ai/chat` 端点（stream=false 返回 JSON，stream=true 返回 SSE）。/ask 页普通对话已直连此端点：配置 LLM 凭据后即可使用，检索问答照旧走 Hyper-RAG。
- **云盘 → MinerU 转换 → 知识库链路已打通**：/drive 页每个文件新增「转换入知识库」按钮——点击即入队 MinerU 转换任务（POST /api/convert, source_type=drive_file），完成后可在知识库页用 `/api/convert/{job_id}/to-kb` 写入文档。缺 MinerU Token 时提示"请先在设置中配置"。API 全量回归 266 项通过，前端 16 路由构建通过。
- **前端新增三个页面**：`/kb`（知识库管理：新建/切换/文档列表/添加文档/触发索引/索引状态展示）、`/ask`（多会话 AI 问答：普通对话 + 检索问答双模式、绑定知识库、会话切换/删除、消息气泡 + 来源 chips、回车发送）、`/settings` **大幅升级为 AI 凭据配置中心**（LLM/Embedding/MinerU 三组密钥配置，密钥/显示切换，DeepSeek/SiliconFlow/通义/OpenAI 四家预设一键填入，已配置状态绿勾，保存后密钥脱敏回显"configured"）；15 个路由全部静态预渲染。
- **侧边栏补齐「知识库与 AI」导航分组**：此前只加了 `PAGE_TITLES` 的 `/graph` 文案，`NAV_GROUPS` 未同步，导致 `/kb`、`/graph`、`/ask` 三个页面无侧栏入口（只能手敲 URL）。现新增独立分组（位于「工作区」之后）：`nav-kb` 知识库、`nav-graph` 超图可视化、`nav-ask` AI 问答，并补全 `/kb`、`/ask` 的 `PAGE_TITLES`；`/settings` 副标题改为「平台 API 凭据（LLM / Embedding / MinerU）与空间配额」。验证方式：`next start` 后抓 `/kb` 服务端渲染 HTML，15 个 `data-testid="nav-*"` 全部出现、分组标签为 `['工作区','知识库与 AI','协作与交付','运行']`，且当前页 `nav-kb` 带 `nav-item-active` 高亮。
- 已用真实服务验证：`/api/competition-packs` 返回 cumcm 1.1.0；对新建项目 `apply` 创建 17 个任务与 9 个模板成果物（无告警）；`validate` 返回 `NEEDS_REVISION` 且仅报告真实缺口（`audit_report` 需由实验任务产出，不属模板骨架）；模板导出按项目上下文渲染正确（UTF-8 中文项目名往返无误）。
- 新增 `apps/api/test_competition_packs_api.py`（15 项路由契约测试：pack 目录/详情/404、骨架导出、物化进度、幂等重放与必填幂等键、问题子集、校验状态与覆盖面、渲染导出与非法参数）；API 全量回归 131 项通过。
- **UX-1 前端地基与项目起点已完成**（Demo 1.0 主线首个阶段，交接见 `docs/handoffs/UX_1_FRONTEND_FOUNDATION_AND_PROJECT_START_HANDOFF.md`）：`lib/api.ts` 新增 `ApiError{status, code, detail}` + `apiError()` + `errorMessage()`，54 处错误抛出统一（含 FastAPI 422 的字段级 `字段名：原因` 映射）；`components/ui.tsx` 新增 `LoadingSkeleton` 与 `ConfirmDialog`；新增 `components/project-create.tsx` 新建项目弹窗（模板包/题号下拉，真实拉到 CUMCM v1.1.0）；总览页新增空项目引导（替换"等待项目数据"死状态）；项目选择持久化到 `localStorage["map.selectedProjectId"]` 并在候选失效时回退提示；项目切换器改为常驻、侧栏与顶栏各加新建入口。**修复**：`refresh` 以闭包旧 `projectId` 覆盖显式新建的选择（表现为"已有项目时新建后被切回旧项目"），改为 `refresh(preferredProjectId?)` 首选优先。新增 `scripts/acceptance_empty_api.py` 作为"空项目起点"验收夹具。后端回归 266 项通过（13 skipped，本阶段未改后端），前端 16 路由构建通过。
- **UX-2 任务与模板闭环已完成**（交接见 `docs/handoffs/UX_2_TASK_AND_PACK_CLOSURE_HANDOFF.md`）：`/tasks` 新增任务详情弹窗（依赖任务、关联成果物分"产出/输入"、交接、最近事件、完成标准、阻塞原因）；负责人下拉改为由 `dashboard.agents` 派生真实 `agent_id`（删除硬编码的 Mira/Aster/Nova/Reviewer，并保留历史值选项），创建后可在详情改派；新增 `TASK_TRANSITIONS` 与后端 `_task_transition_allowed` 对齐，按钮带目标状态与后果、破坏性流转走确认弹窗，**修掉两个必然失败的按钮**（`WAITING_REVIEW→APPROVED` 实测 403 `task_approval_requires_review`、`RUNNING→NEEDS_REVISION` 为非法迁移 409），`WAITING_REVIEW` 行改为提供「去审核」入口。/pack 接线 `getCompetitionPacks()`（原死代码）新增「可用模板包」面板（版本/模板数/DAG 任务数/题号），页头新增题号下拉并随 apply 提交，apply 前用确认弹窗预告"将补建 N 个任务、M 个成果物"、应用后报实际数量，项目包不可解析时显示 `pack-unbound` 面板并给出创建入口。顺带补全前端 `Task` 类型缺失字段。真实前后端验收通过（预告 17/9 = 实际 17/17、9/9；改派刷新保持；详情依赖与成果物渲染正确）。后端回归 266 项通过（未改后端），前端 16 路由构建通过。
- **UX-3 Agent 状态真实性已完成**（交接见 `docs/handoffs/UX_3_AGENT_LIVENESS_HANDOFF.md`）：新增心跳超时扫描（`AGENT_HEARTBEAT_TIMEOUT_SECONDS=90`，固定值不做环境变量）把超时 Agent 置 offline 并按项目授权写 `agent.offline` 事件；补全离线写入点（Gateway 最后一个连接断开、设备撤销）；新增租约回收 `recycle_expired_leases`——按决策 D4 分级：CLAIMED→READY 可重领、RUNNING→NEEDS_REVISION 并走 `create_review(reviewer_kind=SYSTEM)` 登记 `lease_expired_during_execution` 风险（幂等键避免重复复核）；新增维护循环（10s）并在事件循环线程调用 `broadcast_event`（此前是死代码），界面前端无需手动刷新即可反映掉线与回收。**验收中发现并修掉两个缺陷**：广播初版跑在 `asyncio.to_thread` 工作线程里必然抛 RuntimeError 被静默吞掉；维护线程与 FastAPI 线程池共用同一个 sqlite 连接触发 `InterfaceError: bad parameter or other API misuse`（表现为请求 404/500 与 WebSocket 建连即断），现以 `_SerializedConnection`/`_SerializedCursor` 串行化全部连接操作。/runs 增加「最近心跳 N 秒前，是否超过判定阈值」列。新增 `apps/api/test_agent_health.py` 16 项契约测试（含 2 项并发/线程回归）；后端回归 282 项通过（266 基线 + 16），前端 16 路由构建通过。端到端实测：改库把心跳置 5 分钟前后，界面在 24 秒内**未刷新**自动显示 offline 与「308 秒前，已超过判定阈值」。
- **UX-4 接入向导与设备管理已完成**（交接见 `docs/handoffs/UX_4_ONBOARDING_AND_DEVICES_HANDOFF.md`）：新增 `agentd keygen`（未加密 PKCS8 私钥 + SPKI 公钥 + `.fingerprint`，指纹与平台一致，已存在默认拒绝覆盖）；新增 `/devices` 页（配对向导：配对码/倒计时/可复制的接入命令；设备列表：指纹、最近心跳、撤销、Token 轮换与一次性 Token 展示）；新增 `scripts/connect-agent.ps1`（UTF-8 BOM）——三条输入完成 登记 Agent → keygen → device-register → Token 入 Windows 凭据管理器 → 打印可粘贴的 gateway-run 命令；README「本地 Agent」改为向导主路径，老轨手工命令移入开发调试附录。**验收中修掉两个真缺陷**：challenge 以 `-`/`_` 开头时被 argparse 当成选项（改用 `--opt=value`，约 3% 配对会踩到）；`agentd.request()` 只抛 HTTPError 导致接入失败看不到服务端原因（改为 `http_<code>:<detail>` + `main()` 一行错误输出）。另修验收夹具 `scripts/acceptance_empty_api.py`：`main.gateway` 等模块级单例仍绑旧 store，导致 WebSocket 网关查真实开发库（设备恒 403）。端到端实测：keygen 产物完成真实设备注册（指纹一致）、复制命令执行完成全流程并写入凭据管理器、启动 Gateway 后 `agent_connections` 出现 CONNECTED 且 Agent 在线。新增 `apps/agent/test_agent_keygen.py` 7 项；Agent 侧 152 项（9 skipped）、后端 282 项（13 skipped）全通过，前端 19 个静态页。
- **UX-5 Agent 任务闭环已完成**（交接见 `docs/handoffs/UX_5_AGENT_TASK_LOOP_HANDOFF.md`）：新增前置「接入即授权」——`/devices` 设备行可「授权到项目」（选项目 + 13 项能力），复用既有 `POST /api/projects/{id}/device-grants`（同时写设备级与 Agent 级授权），一次性展示 `prj_` 项目 Token 并给出 worker 启动命令；凭据目标新增 `MathAgentPlatform/project-token/<project_id>`。`agentd` 新增 `worker-run`：首次 `--grant <授权串>` 写入凭据管理器与 `worker.json`（token 不落文件），之后无参启动；循环为 claim → progress → 执行 → result，空队列指数退避；执行走既有 `LocalRunner` + `CommandAdapter`（命令由任务 `resource_policy.worker_command` 声明，未声明则显式失败 `executor_not_configured`，不假装成功）；执行前后调 `POST /api/projects/{id}/runs` 与 `/api/runs/{id}/complete`，因此 `/runs` 可见执行者、状态与 stdout。**验收中修掉两个真缺陷**：`worker-run` 缺 device_id 会被 LocalRunner 拒绝（`runner_device_id_required`），现从授权串带入并在缺失时提前报错；向导命令的 `--url` 必须在子命令之前（agentd 顶层参数），初版顺序错误会导致 `unrecognized arguments`。端到端实测：界面建任务 → `worker-run --once` 自动领取并执行 → 任务 APPROVED、assignee=agent-worker、Run 记录 `SUCCEEDED`（含真实 stdout）。新增 `apps/agent/test_agent_worker.py` 12 项；Agent 侧 164 项（9 skipped）、后端 282 项（13 skipped）全通过，前端 19 个静态页。
- **UX-6 协作正确性已完成**（交接见 `docs/handoffs/UX_6_COLLABORATION_CORRECTNESS_HANDOFF.md`）：**协作编辑真落库**——`lib/api.ts` 新增 `saveArtifactText()`（multipart + 幂等键），文档页加「保存草稿」按钮与「有未保存的修改 / 已保存 HH:MM」状态，并纠正了原先误导的文案（旧文案让人以为「提交待审」会保存未保存的编辑）；浏览器实测：输入 → 保存 → 刷新后内容仍在（1869 → 1886 字节），此前刷新即丢。**门禁人工批准接线**——新增 `submitReview()`（门禁批准的唯一致入口，映射 `review_blocked_by_open_risks` 等五类守卫原因），`/review` 门禁卡片改为显示目标标题而非 UUID、提供「批准」「退回修订」（批准带后果确认、退回必填原因），项目级门禁显式标注「由程序规则派生，不能通过复核直接批准」；实测 FAILED → PASSED、待确认 1 → 0。**交接收据可操作**——新增 `acceptHandoff()/rejectHandoff()`（映射接收方不匹配等错误码），`/handoffs` 按 pending 收据逐条给接受/拒绝（拒绝必填原因），实测 PENDING → ACCEPTED 且指标同步。**项目知识库**——`/kb` 新建时可选个人库/项目库并说明授权差异。**四处危险操作接入确认**：云盘删除、删除会话、落库门禁、生成提交包。后端回归 282 项（13 skipped，本阶段未改后端）、Agent 164 项全通过，前端 17 个页面构建通过。
- **UX-7 打磨与 Demo 1.0 验收已完成（Demo 1.0 主线收尾）**（交接见 `docs/handoffs/UX_7_POLISH_AND_DEMO_ACCEPTANCE_HANDOFF.md`，验收清单见 `docs/DEMO_1_0_ACCEPTANCE.md`）：6 个页面补加载骨架（不再把「数据未到」显示成「暂无」）；死代码处置——删除 `importCumcmWorkspace`/`getDocumentLayers`/`getDocumentEvidence`，`getAiSettings`/`saveAiSettings` 接线进设置页（消除重复裸 fetch）；新增「测试连接」能力（`apps/api/app/ai_probe.py` + `POST /api/settings/ai/test`，逐组件探活、Embedding 回报实际维度并对不一致告警、MinerU 明说不假装测过）；答辩提纲可下载；`/graph` 的「实体管理/关系管理」改名为「实体浏览/关系浏览」并标注只读。新增 `scripts/demo-1.0.ps1` 端到端演示脚本（自动断言 + 界面检查点 + `-AutoApprove` 无人值守自检），独立临时库实测 **17/17 项通过**。**验收中修掉三个真缺陷**：新增探针端点时插错位置导致 `PUT /api/settings/ai` 被覆盖（保存凭据静默失效）；同一端点的响应明文回显 API Key（现 GET/PUT 共用 `_mask_ai_settings` 脱敏，并改掉了把明文回显当预期的旧测试）；演示脚本自身三处问题（PS 5.1 管道断言不稳定、catch-all 掩盖报错、`-InFile` 打 multipart 端点 422）。后端 291 项（13 skipped）、Agent 164 项（9 skipped）、前端 17 个页面构建全通过，README 新增「最快上手」六步。
- **桌面端客户端规划已产出**（`docs/DESKTOP_CLIENT_PLAN.md`，状态 PLAN）：目标是「下载安装包 → 输入平台地址 → 网页一键配对 → 开机自动连、自动监测本机 Agent、有任务自动执行」。盘点结论：连接监督与退避、紧急停止、Windows 服务安装、用户会话 Worker/ConPTY、凭据管理器、本地补传队列、外部 CLI 探测与 Codex 执行体均已在仓库内可复用；缺口是任务循环与连接监督分属两个进程、心跳里的 `adapter_versions`/`capabilities`/`local_queue_length` 被平台丢弃（`store.py:1376`）、无配对深链、无安装包与自动更新、Agent 侧无法查询自身项目授权。选型建议：Electron 壳 + 现有 Python sidecar（PyInstaller ONEDIR）+ 回环 HTTP 契约 + electron-updater，平台侧补 `GET /api/agent/me`、心跳字段落库与更新源。**尚未开始实现**（D1–D4 约 24 人日，最小可用 14）。
- **桌面端客户端实施计划已产出**（`docs/DESKTOP_CLIENT_IMPLEMENTATION_PLAN.md`，状态 PLAN）：把 `DESKTOP_CLIENT_PLAN.md` 的选型与架构展开为可执行阶段——`DP-0` 基线/契约冻结、`DP-1` 壳与内核骨架（MVP 配对）、`DP-2` 常驻任务循环 + 本地 Agent 监测（含后端 B1/B2/B3/B4）、`DP-3` 本地可观测与诊断、`DP-4` 分发与运维、`DP-5` 跨平台（按需）、`DP-6` 交付验收。含 13 条冻结决策（DE1–DE13）、7 条不变式、6 条禁区、交接模板要求与防偏移机制。最小可用（DP-0…DP-2）约 16.5 人日，全量（含 DP-4/DP-6）约 28.5 人日。**尚未开始实现**。
- **桌面端 DP-0 已退出，DP-1 进行中**（交接见 `docs/handoffs/DP_0_BASELINE_AND_CONTRACT_HANDOFF.md`）：基线冻结（后端 291 / Agent 178 / 前端 17 页 / `demo-1.0.ps1` 17 项）；sidecar 契约 **v1 冻结**（`docs/SIDECAR_CONTRACT.md`：仅回环 + 启动令牌、8 个端点、错误码表、只能追加的演进规则）；工具链自检脚本 `scripts/desktop-toolchain-check.ps1`（实测 5/8，PyInstaller 曾缺）。**DP-1 已完成的部分**：契约实现 `apps/agent/sidecar_api.py` + `agentd sidecar-run`（30 项契约测试，含只监听回环、配对串解码、设备标识冲突自动换标识重试）；内核 **PyInstaller ONEDIR 打包并通过烟测**（87MB，不依赖系统 Python 与仓库）；Electron 壳 `apps/desktop/`（单实例锁、托盘四态、拉起/健康检查/异常重启内核、状态页、`map://` 深链、暂停/紧急停止/日志）。**实测两条端到端链路**：① 内核 `/pair` 对真平台完成接入（凭据入管理器 + Codex 路径探测）；② `map://pair?…` 深链 → 壳转交内核 → 撞 `device_id_already_registered` → **自动换标识重试成功**（平台出现 `device-fallrain-9ac1`）。**DP-1 剩余**：`/devices` 页深链入口、NSIS 安装包（electron-builder，需把 `dist-sidecar` 作为 extraResources）。回归：Agent 208 项（9 skipped，+30）、API 291 项（13 skipped）、前端类型检查通过。
- **桌面端 DP-1 已退出（壳与内核骨架 / 最小可用配对）**（交接见 `docs/handoffs/DP_1_SHELL_AND_KERNEL_HANDOFF.md`）：Electron 壳 `apps/desktop/`（单实例、托盘四态、内核生命周期与异常重启、本机状态页、`map://` 深链）；内核打包链路 `scripts/build-desktop.ps1`（PyInstaller ONEDIR 84.9MB → 打进安装包 `resources/sidecar/`）；NSIS 安装包 **137MB（未签名）**；平台 `/devices` 配对弹窗新增「接入这台电脑」深链入口（保留命令行兜底）。**安装包验收实测**（`scripts/verify-desktop-install.ps1`）：静默安装退出码 0、含随包内核、安装版启动后内核被拉起且 `/health=ok`、静默卸载后目录与注册表残留 0（凭据残留 28 条属 DP-4-04 范围）。**深链端到端实测**：`map://pair?…` → 壳经 second-instance 转交 → 内核配对成功（撞已有 device_id 时自动换标识 `device-fallrain-9ac1`）。**验收中修掉五个真缺陷**：`/pair` 误用项目授权串解码器、`capabilities` 类型错致 422、POST 不读请求体导致 Windows RST、设备标识冲突无重试（重装后永远接不进）、PS 5.1 EAP=Stop 会中止原生构建程序；另按平台机制把 Codex CLI `0.154.` 登记为已测版本族（此前被判 UNSUPPORTED）。回归：Agent 208 项（9 skipped）、API 291 项（13 skipped）、前端 19 页构建通过。**DP-1 交付物留在仓库内需注意**：`dist-sidecar/`(87MB) 与 `apps/desktop/dist/`(138MB) 是构建产物，纳管 git 前必须先忽略。
- **桌面端 DP-2 已退出（常驻任务循环 + 本地 Agent 监测，含后端 B1/B2/B3/B4）**（交接见 `docs/handoffs/DP_2_RESIDENT_TASK_LOOP_HANDOFF.md`）：任务循环 `apps/agent/task_loop.py`（claim→执行→上报，退避/暂停/断线闸门）注入连接监督 `machine_service`，常驻体 `agentd daemon-run`（契约 + 连接 + 循环 + 状态镜像，未配对时先起契约、配对后自动连接，不必重启）；本机清单 `apps/agent/agent_inventory.py`（TTL 缓存 + 强制重扫，只上报可执行者）随心跳上报 `adapter_versions`；后端 B1/B2 心跳运行态落库（新表 `device_runtime_state` + 设备能力/版本跟随心跳，PG 迁移 015 同语义）、B3 `GET /api/agent/me`（设备 Token 换自身配置与运行参数）、B4 `GET /api/devices` 带 `runtime` 且 `/devices` 显示「执行体 · 队列 · 会话 · 上报时间」；契约追加 `POST /grant` + `map://grant` 授权深链（DP-2-08），内核授权后运行期换循环、不必重启。**实机验收全过**：装/配后不改配置 → 平台建 `worker_executor=codex` 任务 → 内核 3–10 秒领取、30–60 秒完成、任务进 `WAITING_REVIEW`、Run `SUCCEEDED` 且摘要含 Codex 回复；暂停期间任务保持 READY、恢复立即领取；紧急停止后 Agent 掉线且不领取、解除后自动恢复；B3 返回 13 项能力与 `runtime_policy` 且不含 Token 明文。**验收中修掉四处真缺陷（已追加 DE14–DE17）**：任务循环同步完成时饿死事件循环（每轮显式让出）、重连换 `connection_id` 导致本地序列错位死循环（新会话重编号 outbox）、紧急停止退出内核致托盘无法恢复（改为停机不退出）、Codex 瞬断重试被误判为失败（错误事件分致命/瞬断，`summarize_codex_result` 唯一判定入口）。回归：Agent **247** 项（9 skipped，+39）、API **305** 项（14 skipped，+14）、`demo-1.0.ps1` **17/17**、前端 19 页（`/devices` 6.42 kB）。**边界**：单项目、单并发、无本地诊断页/自启/自动更新（DP-3/DP-4），卸载不清理凭据管理器条目（DP-4-04）。
- **项目时间线可用性重做（UX 修正，2026-09-16）**：原页面把 `task.claimed`/`run.failed` 这类机器码当标题、`actor_kind` 恒为 `system`（历史事件里人类操作也被记成系统）、对象 chip 永远显示 `project`，用户看不出发生了什么。现在新增 `apps/web/lib/events.ts` 作为唯一翻译层（事件类型→中文标题、payload 状态/结论/原因→中文、按**主体 id** 判角色、对象→可跳转去处），`apps/web/app/timeline/page.tsx` 重做：顶部「当前进展」（任务通过率/待审核/待接收交接/未通过门禁/最近活动）+ 按天分组 + 分类筛选（带计数）+ 相邻同质事件合并为「×N 次」+ 搜索 + 可选「显示机器码」（审计时对照）。回归：API 306 项 OK（新增 CORS 契约测试）、前端 19 页构建通过（`/timeline` 6.52 kB）。
- **「任务怎么开始跑」的入口补齐（UX 修正，2026-09-16）**：用户接入 Agent 并应用模板包后的真实困惑是"启动按钮在哪"——平台是拉取模型（D5/DE5），网页里**不存在也不需要**"开始"按钮，但界面既没说这件事，也没告诉用户任务为什么不动。三处改动：① 后端 `PATCH /api/tasks/{id}` 新增 JSON 体 `resource_policy`（校验只接受 `worker_executor=codex` 或合法的 `worker_command`，非法值 409），让模板包生成的"无执行方式任务"能被变成可执行（此前 Agent 领走只会得到 `executor_not_configured`，界面上看不到）；② 新增 `apps/web/lib/task-flow.ts` 诊断层，镜像服务端领取规则（`claim_task` 的状态/租约、`_task_dependencies_ready` 的依赖/交接/成果物、执行方式），任务页每行给出"等待 Agent 领取/缺少执行方式/等待上游 X 通过审核/没有在线 Agent/等待人工审核"及对应按钮，并把原来看起来像"启动"的 `READY→RUNNING` 改名为「人工接管」；③ 总览页新增「下一步」引导条（建项目→应用模板包→接入 Agent→让任务跑起来，按真实状态打勾并指向下一个未完成步骤），模板包页物化完成后直接给「去启动任务」。实机验证（浏览器操作 + 真内核）：网页建任务 → 提示缺少执行方式 → 「设置执行方式」选 Codex 并填指令 → 保存 → 内核数秒内领取、Codex 执行、任务进 WAITING_REVIEW，`reply: 界面启动流程通过。`。回归：API **311** 项 OK（+5 执行方式契约测试）、前端 19 页构建通过（`/tasks` 8.02 kB）。
- **执行过程实时反馈（DP-2 补强，2026-09-16）**：此前平台侧只能看到 CLAIMED→RUNNING→结果三帧，"Agent 正在做什么"完全不可见。现在：① Agent 侧新增 `apps/agent/executor_events.py`，把 Codex 的 JSONL 事件流翻译成平台事件（`agent.process.started` / `agent.agent.message` / `agent.tool.completed` / `agent.file.changed` / `agent.process.exited`），经 Gateway 持久化队列上报（断网补传），节流 1.5 秒/条、每轮 ≤40 条并把压掉的条数写进退出事件；终态仍只走 HTTP `/complete`（避免同一 Run 被完成两次）；② 平台侧 `/runs` 显示"已运行 N 秒 + 最新一条过程"，任务详情新增"执行过程"与"执行结果"，事件流翻译补齐这些类型。**过程中发现并修掉四个真缺陷**：(a) **看板只给最早的 20 条事件**（`list_events(limit=20)` 是 ASC），导致界面永远看不到新事件、实时进度无处显示——新增 `list_latest_events` 并把 `/events` 加上 `limit`；(b) 会话轮换 connection_id 后，**入队时盖的旧身份**留在心跳 payload 里，平台判 `gateway_identity_mismatch` 拒收整帧，序号不推进 → 之后每条都被当「缺口」，积压 170 条发不出去——发送时按当前身份重盖（`_restamp_identity`）；(c) 项目授权串里的 device_id 被 --device-id / platform.json 覆盖，造成「能领任务、能报结果，但过程事件被判 `gateway_project_token_identity_mismatch`」的半通状态——授权串优先，并在启动时用 `/api/agent/me` 校验归属、不一致就明确告警；(d) 执行统计回调被当成 dict 直接转换（TypeError），异常发生在「Run 已登记、结果未提交」之间，任务永远停在 RUNNING——修正并加兜底：执行过程任何意外都会补交一条失败结果。实机验证：Codex 任务执行期间平台侧陆续出现 `process.started → agent.message（执行体回复）→ tool.completed → process.exited`，`/runs` 与任务详情可见。回归：Agent **259** 项（+12）、API **317** 项（+6）、前端 19 页。
- **Agent 回答与原始输出同步到平台（DP-2 补强，2026-09-16）**：此前回答只以被截断的 `summary` 出现在列表的一行小字里（`codex exit=… \| reply: …`），原始输出在界面上没有任何入口。现在：① Agent 侧摘要改为「回答在前、`---` 之后是诊断」，并把字段上限放宽（summary 8000 / stdout 40000 / stderr 8000）；② 平台新增 `GET /api/runs/{run_id}`（按项目做成员授权）；③ `/runs` 每条执行可点「详情」看回答全文、执行信息、信息边界与折叠的原始输出（含「复制回答」），任务详情新增「最终回答」区并链到运行控制台；④ 前端 `splitRunSummary` 兼容旧格式（从 `reply:` 里摘回答），历史记录也不会显示成一串机器码。实机验证：Codex 任务完成后，任务详情与执行详情都显示完整多段回答，诊断（exit/events/warnings）与原始 JSONL 各自归位。回归：Agent **259** 项、API **321** 项（+4）、前端 19 页。
- **内容生命周期与体验完善 · 实施计划已产出**（`docs/CONTENT_LIFECYCLE_PLAN.md`，状态 PLAN）：现状盘点结论是**机制齐了但没接线**——成果物状态机（`docs/ARTIFACT_LIFECYCLE.md`）、审核联动、归档、交付过滤都已实现，`OutputDiscovery`/`RunManifestBuilder`/`ResultUploader`/`AgentArtifactClient` 也都在仓库里，但**只在 Windows 会话 Worker 路径被实例化**（`agentd.py:911`），常驻任务循环从不采集产出、`output_artifact_ids` 恒为空。计划分 7 阶段（`CL-0` 基线与口径 → `CL-1` 执行产出落库（产出文件+回答）→ `CL-2` 内容→审核→下游闭环 → `CL-3` 内容可视化与追溯 → `CL-4` 回收与归档 → `CL-5` 交付就绪度 → `CL-6` 文档协作持久化），10 条冻结决策（产出只到待审、按执行前后清单差分采集、回答也入库、同路径产出走新版本、平台不镜像工作区、交付只认已批准、归档不硬删、上传失败不改任务成败、文件大小分级、不新造事件类型）、6 条不变式与禁区清单；最小可用（能审、能进交付）≈5.5 人日，完整 ≈10 人日。**§10 有三件待拍板事项（均给了推荐默认值）**，不拍板按默认执行。
- **CL-1 执行产出落库已退出**（交接见 `docs/handoffs/CL_1_OUTPUT_COLLECTION_HANDOFF.md`）：常驻内核跑完任务后，**本次新增/修改的工作区文件**与**回答**自动进成果物库（`PENDING_REVIEW`），并回填 Run 与任务结果的 `output_artifact_ids`。新增 `apps/agent/workspace_scan.py`（快照/差分/剪枝/上限）与 `apps/agent/output_collector.py`（产出发现 → 复用既有 `ResultUploader` 队列 → 上传；回答以 `agent_answer` 入库）；`TaskLoop` 增加采集钩子（`before` 快照 / `after` 上传），采集发生在「完成 Run」之前；采集失败只记录、**不改变任务成败**（I4），失败产出留在本地队列下次执行前补传。平台侧 `ArtifactType` **追加** `agent_answer`；成果物页补类型中文名。执行中修掉三个真问题：`agent_answer` 不在允许类型里（422）、失败运行的占位句被当成回答入库、声明式命令的诊断摘要被当成回答（现要求 `---` 分隔符）。**实机验收**：声明式任务写出 `notes/result.md` → 成果物库出现 `paper_source · PENDING_REVIEW`（含哈希与 task/run 引用）；Codex 任务的回答以 `agent_answer` 入库；重启内核后规则一致；早期 422 失败的产出被自动补传（断网/失败自愈）。回归：Agent **281** 项（+22）、API **323** 项（+2）、`demo-1.0.ps1` **17/17**、前端 19 页。**边界**：产出仍是「待审」且无界面入口（属 CL-2）、无成果物详情与归档入口（CL-3/CL-4）、>100MB 只登记不传、`worker-run` 前台调试默认不采集。
- **CL-2 内容→审核→下游闭环已退出**（交接见 `docs/handoffs/CL_2_CONTENT_REVIEW_HANDOFF.md`）：审核门禁页新增「待审成果物」分区（来源任务/运行、版本、哈希 + 内容预览 + 批准/退回，走既有 `reviews` 接口，风险门禁守卫保持生效）；成果物页每行给出「能否用于下游」的结论与原因，被退回的行可「提交新版本」（新建待审版本并写入新内容，旧版本原样保留）；建任务表单新增「输入成果物」多选（只列已批准且允许下游的内容）。**实机验收**：6 份真实待审内容在审核页可见 → 批准后 `APPROVED · downstream_allowed=True · immutable=True`；退回 → 「提交新版本」产生 v2（parent 指向 v1、PENDING_REVIEW），写入新内容后 v2 哈希变化而 **v1 一字未动**；引用未批准成果物的任务**不会被领取**、引用已批准的立即被领取（claim 对前者的保护是「跳过」而非报错，脚本按此断言）。**本阶段未改后端**。回归：API 323 项 OK、`demo-1.0.ps1` 17/17、前端 19 页（`/review` 5.03 kB、`/artifacts` 3.55 kB）。**边界**：给已存在任务补输入还不支持（PATCH 无该字段）、修订仅文本内容、成果物详情与归档属 CL-3/CL-4、交付页尚未提示未批准内容（CL-5）。
- **CL-3 内容可视化与追溯 + CL-4 回收与归档已退出**（交接见 `docs/handoffs/CL_3_CONTENT_TRACEABILITY_HANDOFF.md`）：新增只读端点 `GET /api/projects/{pid}/artifacts/{aid}/detail`（契约 `ArtifactDetail`：来源任务/运行/哈希/创建者、版本谱系、被谁引用、复核记录、门禁、孤儿标记，**在路由层用既有只读方法拼装**，SQLite 与 PG 天然一致）；成果物页 `详情` 弹窗（来源与下游可用性 + 版本谱系 + 被谁引用 + 复核记录）、`归档`（确认写明「退休不是删除」，调既有 archive 端点）、孤儿标注（来源任务已不存在，只标注不清理，D-CL-7）；`/runs` 执行详情新增「产出成果物」区块；时间线支持 `/timeline?artifact=<id>` 只看单份内容的事件（含复核事件的 `payload.target_id` 匹配），并补齐 `artifact.archived` 等事件中文标题。**实机验收**：详情弹窗显示真实来源/谱系/复核（`APPROVED · member-001`）与「被谁引用（2）」；Run 详情显示产出及其状态；内容故事线 5 条事件（含归档触发的门禁失效与「成果物已归档」）；归档后 `ARCHIVED` + `downstream_allowed=false` 且列表标注「不再作为新任务输入」；删掉来源任务后出现 3 处孤儿标注。回归：API **326** 项（+3 成果物详情契约测试）、Agent 281 项、`demo-1.0.ps1` **17/17**、前端 19 页（`/artifacts` 5.08 kB、`/runs` 7.14 kB、`/timeline` 7.55 kB）。**边界**：CL-5 交付就绪度未做（交付页仍不提示未批准内容）、孤儿无「认领」入口、归档不可逆（`ARCHIVED` 为终态）。
- **CL-5 交付就绪度已退出**（交接见 `docs/handoffs/CL_5_DELIVERY_READINESS_HANDOFF.md`）：交付页顶部新增「交付就绪度」面板，五项全部来自真实数据——未批准内容数、大纲可生成性（含缺哪类已批准素材）、交付检查失败/提示项、未通过门禁数、编译状态——副标题直接给结论（还有 N 项必须先处理 / 可以提交但有 M 项提示 / 各项就绪），每项带直达入口；并把后端**已算出**的 `excluded_unapproved` 名单显示出来（此前界面只显示一个计数），写明「提交包只装已批准素材」。进页面自动跑一次交付检查，用户不必先点按钮才知道差什么。**不把面板做成导出硬门禁**（遵守 Do NOT，仍可强制生成提交包）。实机验收：开发库真实状态下显示「4 份内容还没批准 + 大纲缺 6 类素材 + 交付检查 2 项未通过 + 4 个门禁未通过 + 还没编译」，排除名单列出 16 份内容（含归档的 second.md 与待审的 result.md，与提交包装配过滤同源）。本阶段只改前端。回归：前端 19 页通过（`/delivery` 4.9 kB）、API 326 项、Agent 281 项、`demo-1.0.ps1` 17/17。**边界**：面板不阻止导出、字数未单独展示、编译状态不跨会话（产物仍是工作区文件）；CL 主线仅剩 CL-6 文档协作持久化。
- **CL-6 文档协作持久化已退出 → CL 主线收尾**（交接见 `docs/handoffs/CL_6_DOCUMENT_DRAFTS_HANDOFF.md`）：新增 `document_drafts` 表（SQLite 建表 + PG 迁移 `016_document_drafts.sql`，含 RLS）、`DocumentDraft`/`DocumentDraftUpdate` 契约与 `GET/PUT /api/projects/{pid}/documents/{aid}/draft`；前端编辑后每 5 秒把文本写进服务端草稿（切文档/离开时兜底 flush），打开文档时**优先用草稿做协作种子**并显示「服务端草稿 rN·时间·保存者」，刷新/换设备不再丢未保存编辑；保存带 `base_revision`，落后即 409 `document_draft_conflict`，界面给出双侧对照与「采用他人版本 / 保留我的版本」选择（不静默覆盖）；已批准内容禁止存草稿；文案从「服务端不保存协作状态」改为真实行为。**持久化的是文本快照而非 CRDT 更新流**——`collaboration.py` 的中继语义逐字未动（禁区）。执行中修掉一个自己引入的真 bug：兜底 flush 的 effect 依赖 `collabText`，导致每次按键都取消节流定时器、把中间状态写进草稿（实测结果：编辑器 81 字而草稿 46 字），改为 ref 记住最新文本 + 仅在真正卸载时 flush，清掉错误草稿后重测通过。**实机验收**：编辑 → 草稿 r1 落库（接口回读含刚输入的文字）→ 刷新后内容仍在；模拟他人先保存到 r2 → 浏览器端保存撞 409 → 弹出「我的编辑 80 字 vs 服务端最新 61 字」并可选「采用他人版本」（编辑器随即切换到对方内容）。回归：API **330** 项（+4 草稿契约测试）、Agent 281 项、`demo-1.0.ps1` **17/17**、前端 19 页（`/documents` 38.3 kB）。
- **CL 主线（内容生命周期与体验完善）CL-0…CL-6 全部退出**：CL-0 口径冻结 → CL-1 执行产出落库（产出文件与回答进成果物库、待审）→ CL-2 内容→审核→下游闭环（待审入口 / 批准解锁下游 / 退回出新版本 / 建任务选输入）→ CL-3 内容可视化与追溯（详情 / 谱系 / 引用 / 故事线）→ CL-4 回收与归档（归档入口 / 孤儿标注）→ CL-5 交付就绪度（还差什么 + 排除名单）→ CL-6 文档协作持久化。各阶段交接见 `docs/handoffs/CL_*_HANDOFF.md`；计划与决策见 `docs/CONTENT_LIFECYCLE_PLAN.md`（§10 三件待拍板事项已按推荐默认执行：产出自动入库为待审、回答也作为内容、超大文件只登记不传）。
- **P3 成员管理收尾 / 能力目录 / 工时趋势 / 组织收口已退出并上线**（交接见 `docs/handoffs/P3_MEMBERS_CAPABILITIES_THROUGHPUT_HANDOFF.md`）：① **移出可撤销 + 成员变更审计**——`add_project_member` 写 `project.member_added`/`project.member_role_changed`（带 previous_role），移除事件带**被移除时的角色**，`/team` 新增审计面板（谁/何时/改了什么/撤销多少授权/释放多少派单）并提供「按原角色恢复」；② **项目改归团队** `PATCH /api/projects/{id}`（project.admin，只改归属不带人，跨组织团队拒绝）+ `/team` 上的选择器；③ **能力目录** `GET /api/team/capabilities`（逐个执行体的能力集合 = Agent tools ∪ 设备声明 ∪ 心跳，设备在线数、归属成员）+ **"没人能跑"的任务告警**（声明了 required_capabilities 却无执行体满足，带缺失能力）；④ **工时趋势** `GET /api/team/throughput?days=`（按天成功/失败 + 按成员，基于 runs 的 019 归属），`/team` 纯 CSS 柱状图；⑤ **多租户**未实施但出了方案 `docs/MULTI_TENANT_DESIGN.md`（A 部署隔离 / B 切 PG+强制 RLS / C 应用层过滤，推荐短期 A），并收口两处读泄漏：`GET /api/agents` 从全局可见改为按组织、`DELETE /api/agents/{id}` 从零授权改为本人或管理员。**顺带修掉一个影响时间线的真 bug**：前端 `listProjectEvents` 打的 `/events?limit=N` 用的是 `list_events`（sequence ASC）= **最早的 N 条**，项目事件一多时间线与新审计面板都看不到最近发生的事（与当初看板"看不到新事件"同类，当时只修了看板）→ 端点加 `latest=true` 走 `list_latest_events`，时间线改为取最近 300 条、审计取最近 60 条。另删除一处**重复路由**（新增的带鉴权 `DELETE /api/agents/{id}` 与旧的无鉴权版本同路径，旧版本仍在生效）。**验收**：新增 `test_p3.py` 7 项，全量 API **394 项通过**、前端 **24 页**构建、SSR 不变量（18×18×7）通过；浏览器实机验证四个面板与"移出→审计→按原角色恢复"闭环（库里角色回到 reviewer 并写入 member_added）；线上 22/22。**边界**：多租户隔离未实施、批量路径（注册入项目/入队/建项目带成员）不写成员事件、吞吐只统计走执行体的推进、能力词表仍自由字符串、恢复成员不自动恢复其设备授权。
- **TEAM-1 执行归属 / 项目成员管理 / 多团队 / 能力与截止时间已退出并上线**（交接见 `docs/handoffs/TEAM_1_ATTRIBUTION_MEMBERS_TEAMS_HANDOFF.md`）：用户一次点齐 DISPATCH-1 交接里记的五条 P2。**① 执行归属落库**（迁移 `019_run_attribution.sql`）：runs 新增 `device_id`/`member_id`，`execution_attribution()` 校验"设备必须与该 Agent 对得上"（伪造/串号丢弃），`member_id` 一律服务端推导（设备归属优先、其次 Agent 归属），内核在 Run 登记时带上自己的 device_id（Gateway 路径不改协议、走"最近活跃连接"兜底）；`task.claimed/progress/result_submitted` 与 `run.*` 事件改记 `actor_kind="agent"`（含 payload 里的 device/member），历史事件按同一规则回填；运行详情显示「执行者 · 设备 · 归属」。**② 项目成员管理**：`DELETE/PATCH /api/projects/{pid}/members/{member_id}`（移出时连带撤销其设备/Agent 在该项目的授权、释放派给他的未完成任务、写 `project.member_removed`，且不允许移出最后一个 owner/project_lead）；**新建项目自动带上组织内在职成员**（创建者 project_lead、其余 contributor）；修正中间件"含 /members 就要求 project.admin"的过度限制（GET 走 project.view、写才要 admin，实测 contributor 读目录 200/改角色 403）。**③ 成员工作量与新页 `/team`**：`GET /api/team/workload` 逐成员聚合派单/执行中/完成/Agent/设备在线/项目/团队，`/team` 页含工作量表、项目成员管理（角色下拉、移出确认说明会撤销授权）、团队管理（建队/加人/移出，入队即入该团队项目）、口径提醒；新建项目弹窗加「归属团队」。**④ 多团队**（范围明确收窄并说明：**多租户隔离未做**——运行时是 SQLite，RLS 只在 PG 路径生效；本轮组织可配 `PLATFORM_ORG_ID`/`PLATFORM_ORG_NAME`，组织内团队成为协作单元）。**⑤ 领取消费能力与截止时间**：`_agent_capability_set`（Agent tools ∪ 设备声明 ∪ 心跳）判定 required_capabilities，不满足则轮询跳过、点名领取拒绝；已过截止时间不再自动领取；排序改为"有截止时间的优先（最早在前）"；建单表单可设截止时间、任务行显示「⚠ 已过期」、详情可改期。**验收**：新增 19 项测试（能力/截止时间 7 + 团队 12，含伪造设备被丢弃、入队即入项目、移出连带撤销），全量 API **387 项通过**、Agent **281 项**、前端 **24 页**、SSR 不变量（18×18×7）通过；浏览器实机跑通工作量→成员管理→建队加人→建带截止时间任务（行内显示已过期）；线上 `server_verify.sh` 22/22。**边界**：多租户隔离、项目改归团队界面、能力目录、工时趋势、移出不可撤销。
- **DISPATCH-1 派单模式与个人任务中心已退出并上线**（交接见 `docs/handoffs/DISPATCH_1_MY_TASKS_HANDOFF.md`）：起因是用户提出的"团队系统与不同用户 Agent 在同一团队协作"——三路代码审计结论是**设计在、运行语义不在**，用户拍板三条口径（派单模式 + 个人任务中心 / 允许批准自己 Agent 的产出 / 内容全部可见），本阶段落实第一条并固化三条。**顺手修掉两个"现在就有问题"的 P0**：① `worker-run` 的 HTTP 领取路径（`/api/agents/{id}/tasks/claim`）不在中间件免鉴权清单里，切 `required` 后必被拦 401（我引入的回归，上线冒烟在切换之前做所以没暴露）→ 中间件改为"带 `X-Project-Capability-Token` 的请求交给 handler 按能力校验"（与 nginx 分流同构，handler 本来就逐个校验能力）；② `/api/organizations|/api/teams|/api/members` 被白名单豁免且 handler 不校验身份 → 公网匿名可建组织/团队/成员，且 `POST /api/members` 能建占位成员**抢注邮箱阻断真人注册** → 改为读取需会话、写入需管理员。**派单**：迁移 `018_task_dispatch.sql` 加 `tasks.assignee_member_id`（NULL=未指派），`claim_next_task` 只挑"派给我/未指派"、`claim_task` 拒绝他人派单（`task_assigned_to_another_member`），成员由 `agents.owner_member_id` 解析，派单目标必须是项目成员；新增 `GET /api/projects/{id}/members`（成员目录）与 `GET /api/tasks/mine`（个人任务中心三组：派给我的/我的 Agent 在执行/最近完成），PATCH 与建任务支持 `assignee_member_id`（空串取消）。前端新增 `/my-tasks` 页（导航「任务与流程」因此成组，17 个入口）、任务页建单与详情接入派单选择器、任务行显示「→ 某人」；**过程中修掉一个真 bug**：`/my-tasks` 与 `/account` 的加载 effect 早于 Provider 恢复会话（子组件 effect 先跑），硬刷新时请求不带令牌→401→页面显示空。**验收**：新增 10 项派单/口径锁定测试，API 全量 **368 项通过**、前端 **23 页**、SSR 不变量（17×17×6 分组按钮）通过；本地浏览器实机（required 模式）管理员派单给队友→行显示「→ 队友小王」→队友硬刷新在个人任务中心看到「指派给我 1」；线上 `server_verify.sh` **22/22**（新增账号面与接入面断言）。**工具链教训**：Windows 上 Python 改 `.sh`/`.conf` 会写 CRLF，Linux bash 报 `invalid option name`；已在 `pack-source.sh` 加 `bytes([13])` 字节级防呆。**未做（P2）**：执行归属落库（runs 无 device_id/member_id、事件 actor_kind 多为 system）、项目级成员管理界面、成员工作量视图、多组织/多团队、按能力负载推荐执行体。
- **AUTH-1 用户注册与账号管理已退出并上线**（交接见 `docs/handoffs/AUTH_1_ACCOUNTS_HANDOFF.md`）：按拍板的四条实施——邀请码注册、首个账号自动为管理员、存量数据归首个管理员、域名入口去 Basic 而 IP 入口保留 Basic 作后门。后端新增 `apps/api/app/accounts.py`（`hashlib.scrypt` 口令哈希 + `hmac.compare_digest`、口令策略、会话令牌 SHA-256、一次性临时口令、进程内登入节流：5 次失败锁 60 秒指数退避到 15 分钟）与迁移 `017_accounts.sql`（`human_members` 增 `password_hash`/`password_updated_at`/`last_login_at`/`is_admin`；SQLite 侧用既有 `_ensure_columns` 补列并清理历史明文会话）；端点 `POST /api/auth/{register,login,logout,password}`、`GET /api/auth/me`、`GET/PATCH /api/accounts`、`POST /api/accounts/{id}/reset-password`、`GET /api/invitations`，`/api/auth/dev/session` 在 required 模式下 403。**修掉四个既有断点**（不修切强制鉴权必坏）：① 中间件免鉴权清单不含 `/api/devices/register` 与 `/api/agents/register`（接入流程本就不带人类令牌）→ 新设备会接不进来；② 协作 WebSocket 完全没有身份校验 → 改为 `?token=` 会话校验（nginx 对该路径关访问日志）；③ `sessions.token` 明文入库且无法登出 → 改存哈希 + 登出/撤销；④ 设备注册要求"Agent 归属 == 配对创建者"，而接入脚本登记的归属是种子账号 `member-001` → 规则改为"归属账号已停用/不存在时由生成配对的人接管"（第一版判据写成"归属 == member-001 就接管"被既有测试拦住，说明测试是对的）。前端新增 `lib/auth.tsx`、`apiFetch` 统一注入令牌与 401 处理、`projectSocketUrl`，**73 处 fetch 全部收敛**并补上此前绕过 api.ts 的 5 处直连调用，成果物下载改带令牌取回后保存；新增 `/login` `/register`（支持 `?code=`）`/account` 三页（管理员多出账号管理与邀请码面板），登录页不套工作台外壳、未登录跳 `/login?next=`、顶栏头像接真实用户 + 下拉菜单、**侧栏/抽屉底部补账号入口**（原先手机上头像被隐藏导致无法退出）。**验收**：新增 28 项账号契约测试，API 全量 **358 项通过**（14 skipped）、前端 **22 页**构建通过、SSR 不变量通过；本地以 `PLATFORM_AUTH_MODE=required`（生产配置）跑通浏览器全流程：未登录跳转 → 注册首个管理员（免码）→ 自动登录并看到被接管的 16 个项目 → 账号页生成邀请码 → 退出 → 错误口令提示 → 正确登录 → 邀请链接注册队友（免码预填、无管理面板、能看到项目）→ 320px 手机档登录与抽屉退出；线上 `https://synapforge.top` 实机验证：域名入口无 Basic、`/api/projects` 无会话 401、接入端点仍返回平台 422（证明 required 模式没弄断接入）、IP 入口 Basic 后门有效。**边界**：无 SMTP（忘记密码靠管理员重置）、无 OIDC/2FA、节流是进程内的、项目级成员管理无界面（受邀成员只自动加入注册时已存在的项目）、首位管理员注册须由使用者本人完成。
- **移动端适配（UX-9）已退出并发布**（交接见 `docs/handoffs/UX_9_MOBILE_ADAPTATION_HANDOFF.md`）：CSS 断点从两级重构为四层（≥1101 / 901–1100 平板 / 761–900 / ≤760 手机 / ≤400 小屏），新增移动端底部标签栏（总览/任务/审核/AI 问答/更多，≥761px 由 CSS 隐藏）、层叠顺序重排并修掉"抽屉 z-index 高于弹窗导致弹窗被盖住"；6 个表格页单元格加 `data-label` 在窄屏变卡片式堆叠，行内对齐样式抽成 `.row-actions`/`.list-actions`/`.list-inline-actions`；graph 画布改为横向滚动保可读、ask 对话面板在手机上排前、表单网格单列、toast 限宽抬升、`viewport` 导出开启 `viewportFit: cover`；触摸目标与 iOS 输入不缩放。**过程中修掉四个真问题**：项目切换器内联 `minWidth:180` 盖过媒体查询导致顶栏全站横滚、`.item-copy`/`.table-title` 隐式网格列按 max-content 撑破容器（`overflow:hidden` 因此失效）、长不可断字符串的"文本溢出"（元素矩形正常而 scrollWidth 变大）、`.list-item` 手机档写死两列把状态 pill 挤成 0px。**验收**：四视口（320/390/768/1024）×16 路由 = 64 组几何断言全过（无横向溢出、标签栏档位正确、表格标签生效、chip 不截断）、抽屉交互（遮罩/Esc/锁滚动）实测通过，回归 API 330（当时）/Agent 281/demo 17/17/前端 19 页。**边界**：未做真机验收（无手机，验收为桌面四视口 + 几何断言 + 截图）、不做 PWA/离线、graph 的 SVG 缩放拖拽不做。
- **服务器部署已完成并端到端验收（2026-09-17）**（手册与实测记录见 `docs/SERVER_DEPLOYMENT.md` §9）：目标机 `156.239.229.143`（Ubuntu 24.04.1 / 4 vCPU / 3915MB / 根分区 39G），**公网入口 http://156.239.229.143（nginx 80 + Basic 认证；后续已加域名与账号体系，见下方 AUTH-1 条目）**，应用为该机上的 `map-api`/`map-web` 两个 systemd 单元（127.0.0.1:8000 / 3000），数据在 `/opt/math-agent-platform/apps/api/data`（SQLite + 本地对象目录），运行参数 `/etc/math-agent-platform/api.env`，凭据 `/etc/math-agent-platform/credentials.txt`。**验收证据**：`server_verify.sh` **16/16 通过**（进程/端口/直连后端/经 nginx 的 Basic 与 Bearer 分流/数据容量）；**前端在 4G 服务器上构建成功**（19/19 静态页，`--max-old-space-size=1536` + 4GB swap 压住 2.7GB 峰值，swap 零占用）；**真实端到端冒烟**——经公网生成配对 → `connect-agent.ps1` 完成设备注册（201，Token 入凭据管理器）→ `gateway-run` 连 `ws://…/ws/agents/…` 后平台侧设备 **active**（证明 WebSocket 经 nginx 可用）→ 建声明式命令任务 → `worker-run --once` **领取并执行成功**（任务 `WAITING_REVIEW`、运行 `SUCCEEDED`、stdout `deployment-smoke-ok`、事件流齐全）→ 撤销设备与授权、还原被误领的种子任务。**部署中发现并修掉两个真缺陷**：① Basic 密码文件 `640 root:root` 让 nginx worker（www-data）读不到 → 认证请求 500，改 `root:www-data`；② 分流只认 `Authorization: Bearer`，而任务循环用 `X-Project-Capability-Token` 自定义头 → "能接入、能连 WS、一 claim 就 401"，改为两种凭据任一存在即放行、由平台判令牌；另把 nginx 站点配置抽成 `scripts/deploy/nginx-site.conf` 模板（消除脚本内联副本与线上文件漂移）。**已确认无需 PostgreSQL/MinIO**（`main.py:122` 写死 SQLite Store，`DATABASE_URL` 应用侧从不读取），因此服务器上没装这两样。
- **服务器部署套件已就绪并本地干跑通过（2026-09-17，等待目标服务器凭据）**（手册见 `docs/SERVER_DEPLOYMENT.md`）：为"部署到 4C/4G/50G 单机服务器"准备了一条不依赖 Docker 的路线——摸代码后发现**平台 API 实际不使用 PostgreSQL**（`main.py:122` 是 `Store(...)` 写死 SQLite，全仓 `DATABASE_URL` 只有 compose 与文档提到、应用从不读），对象存储默认本地目录，因此服务器上装 Python ≥3.11 + Node 20 + nginx 即可，省掉 PG/MinIO 的内存与运维面。新增 `scripts/deploy/`：`remote.py`（paramiko 远程 exec/sudo/put/check，密码从文件或环境变量读，不进日志——本机无 TTY 且无 sshpass/plink，系统 ssh 无法非交互登录）、`pack-source.sh`（打部署包：排除 3.4GB 构建产物与本机数据后仅 **1.1MB / 378 条目**）、`server_bootstrap.sh`（幂等初始化：apt 依赖、Python<3.11 时用 deadsnakes 补 3.12、Node 20 官方二进制、**4GB swap**（`next build` 实测峰值 2764MB，4G 机器必须兜底）、系统用户与 systemd 单元、nginx 统一入口）、`server_release.sh`（解包→venv+pip→`npm ci`→`next build --no-lint` 限堆 1.5G→重启→自检）、`server_verify.sh`（进程/端口/直连/经 nginx/数据容量共 17 项断言）。**鉴权方案**：平台当前无登录（OIDC 未做，`PLATFORM_AUTH_MODE=development` 时无令牌一律视为 member-001），故用 nginx Basic 认证兜底，并解决了两个必须处理的冲突——Basic 头会被平台判 `invalid_authorization_header`（故 nginx 校验后**摘掉**再转发）、Agent 的 Bearer 令牌必须直通（用 `map` 按 Authorization 前缀分流，`/ws/` 整体豁免）。**已实测**（本机 nginx 1.26.2 + 彩排实例按同一份配置）：无认证 401 / 正确 Basic 200 返回真实项目 JSON / 错误 Basic 401 / Bearer 直通到 FastAPI 自己的 401 JSON / `/ws/` 未被拦；期间踩到 `auth_basic_user_file` 相对路径按 conf 目录解析导致认证请求 500，生产配置用绝对路径规避。另补 `.dockerignore`（Docker 路线以后可走：原上下文 3.4GB 且会把宿主机 `node_modules` COPY 进 Linux 镜像）。**干跑验证**：全新 venv + 仅 `requirements.txt`（28 秒 / 5 个包）在空数据目录起 API → `/health`、`/api/platform/health`、`/api/projects` 均 200 并自动建库；同时发现**首次启动会种子一个演示项目**（`store.py:814` `_seed()`，库为空时写入「C题 · 风光储能协同优化」+5 任务/成果物），平台没有删除项目接口，手册里给了清库 SQL。**待办**：拿到服务器 IP/凭据后执行 bootstrap → 上传 → release → verify，并做一次真实的配对+领任务冒烟。
- **前端导航分组与快速跳转（UX 修正，2026-09-16，交接见 `docs/handoffs/UX_8_GROUPED_NAVIGATION_HANDOFF.md`）**：侧边栏从 16 个平铺入口收敛为 **7 个入口**（5 个可折叠分组 + 2 个直达页：任务与流程、空间设置），**页面一个没少**——折叠分组的子项仍渲染在 DOM 里（每个路由的静态 HTML 都含全部 16 个入口），另加「快速跳转」搜索面板（顶栏按钮 / 侧栏「搜索页面」/ **Ctrl+K** 开关，一次列全 16 页、按标签与英文别名模糊匹配、↑↓ 选择 + Enter 打开）。`apps/web/lib/nav.ts` 成为导航唯一数据源（分组、标签、图标、说明、搜索别名、页面标题、折叠持久化键），侧边栏、面包屑与面板共用；分组默认只展开当前页所在分组，用户手动开合记进 localStorage（`math-agent-platform.nav.open-sections`），折叠时用数字小标说明内含几页；顶栏面包屑改为「分组 › 页面 / 项目」；角标归位为三处各自判断（任务 BLOCKED/NEEDS_REVISION、待审门禁、模板包未物化——此前模板包与任务共用同一个判断）。**实机验证**（浏览器 + 真 API）：首页 7 行入口 → 点开「知识库与 AI」出 3 个子项 → 搜索「论文」命中 2 条且「论文交付」排第一 → 回车直达 `/delivery` 且「审核与交付」自动展开；刷新后手动展开状态保留（实测 localStorage `{"knowledge":true}`）；几何检查 7 行互不重叠、子项缩进 28px、标签无截断、面板在视口内；移动端侧栏仍是抽屉（关闭时 -244px），抽屉内 16 个入口齐备、快速跳转按钮退化为图标；暗色主题面板配色正确（面板 `rgb(22,28,38)`、选中项 `rgb(30,43,60)`）。**SSR 校验**：16 个路由每个都含全部 16 个 `nav-*` 入口与 5 个分组按钮，`nav-item-active` 恰好 1 个且指向当前页，分组展开状态与当前页一致。回归：前端 `npm run build` 19 页通过、API **330** 项（14 skipped）、Agent **281** 项（9 skipped）——本次未改后端与 Agent，跑一遍确认无关联回归。**边界**：只做分组与检索，没有合并页面（16 个路由与 URL 逐字未变）；浏览器快捷键支持 Ctrl 与 ⌘，但界面提示文案只写 Ctrl K。

## 当前验证

### Demo 1.0 基线（2026-09-16，UX-0-01 记录）

在开始 UX 阶段之前冻结点，后续每阶段退出都要与该基线对比：

| 项 | 命令 | 基线值 |
| --- | --- | --- |
| 后端回归 | `cd apps/api && python -X utf8 -m unittest discover -s . -p "test_*.py"` | `Ran 266 tests, OK (skipped=13)`，约 110s；13 项 skipped 为环境门控（PostgreSQL/MinIO 生产依赖、非 owner 运行时角色 DSN、本机 LaTeX 引擎） |
| 前端构建 | `cd apps/web && npm run build` | 16 条路由（15 页面 + not-found）全部静态预渲染，无类型错误 |
| 端到端启动 | `.\scripts\local-infra.ps1 start` + `.\scripts\start-demo.ps1` | API 8000 / Web 3000 就绪；检索服务需单独启动于 8100 |

```text
$bundled_python -X utf8 -c "preload _cffi_backend; add apps/api/vendor and apps/api to sys.path; unittest discover" -> 95 passed, 7 integration skipped
$bundled_python -m py_compile P4-04 target modules          -> passed
Next.js 15 production build                                  -> passed
Review Center/propagation implementation                     -> compiled; browser visual and real PG paths deferred
P4-05 frontend data-driven navigation/test locators           -> implemented; local browser smoke passed; data E2E and visual regression deferred
P5-01 reproducibility context and manifest metadata           -> implemented; sandbox and OS-level audit deferred
P5-02 Docker/Podman container execution boundary             -> implemented; runtime and integration deferred
P5-03 container routing, layered mounts, manifest integration -> implemented; runtime and system audit deferred
P5-04 information-boundary audit kernel                      -> implemented; OS observer and Gate integration deferred
P5-05 reproducible rerun planner/comparator                    -> implemented; platform Run API and real execution deferred
P5-06 access observation contract and manifest propagation       -> implemented; real OS/container observers deferred
Agent full unittest discover                                     -> 145 passed, 9 conditional skipped
Windows ETW observer diagnostics/cleanup/runtime classification  -> 10 targeted tests passed
Real PostgreSQL 16 + MinIO user-mode integration (P4-04-RUN)     -> stage2: 3 passed; stage4: 4 passed
Runtime role acceptance (P4-04-RUN-PROD)                         -> 6 passed
CUMCM competition pack (阶段 6 数学建模模板)                     -> 领域 15 passed + 路由 15 passed; compliant project PASS/findings=0
API full unittest discover (with integration env)                -> 235 passed, 0 skipped（新增 LaTeX 编译 13 项）
Demo 一键启动（scripts/start-demo.ps1）                          -> 通过（API/Web/pack/文档端点均 200）
阶段 9 端点真实数据验证（health/metrics/quota/usage）              -> 通过
infra/docker-compose.yml 结构解析                                -> 通过（postgres/minio/nats/api/web 五服务）
Next.js 生产构建（16 路由，含 /graph /ai/chat）                    -> passed
主题切换与持久化（data-theme/localStorage）                     -> 通过（暗色截图留存）
个人云盘真实链路（上传→列表→导入项目）                          -> 通过（哈希一致）
浏览器漫游（11 路由 + 关键定位器）                                -> 通过（路由均 200、定位器唯一）
真实服务烟雾（pack 端点 + 页面渲染标记）                          -> 通过（pack-panel/pack-apply/pack-validate/nav-pack 齐备）
JSON parse: domain schemas, agent gateway/session schemas    -> passed
```

本批次已在真实用户态 PostgreSQL 16.9 和 MinIO 上完成阶段 2/4 集成验收，完成 P4-04-RUN-PROD 的运行时角色分离（scram-sha-256 密码认证 + 非 owner 最小权限 + 连接池故障演练），并完成阶段 6 数学建模模板包（manifest/DAG/schema/9 模板/物化器/校验器，端到端 PASS）；浏览器交互、视觉回归、真实 API 联调仍延期到后续验收。真实 ETW 管理员权限采集仍被 UAC 阻塞（详见交接文档）。

## 已知边界

- 默认运行时仍是 SQLite；`_ensure_columns()` 只适合开发兼容，不是生产迁移机制。
- PostgreSQL Repository 只完成第一批数据库事实查询，尚未覆盖完整 `PlatformRepository` 协议；P4-04-RUN-PROD 已完成真实集成验收，包括 FORCE RLS、非超级用户 owner 隔离、独立 `app_runtime` 运行时角色的 scram-sha-256 密码认证与最小权限、单池耗尽与后端丢失故障演练。仍未完成：TLS、跨实例部署、连接池上限压测、角色密码轮换与系统密钥托管、备份/回滚演练。
- P4-04-RUN 更正：真实 PostgreSQL 集成验收已于本批次完成（见上文），但当前实例是本机用户态开发部署（trust 认证、无 TLS、无独立运行时角色）；生产部署的密码认证、连接池上限、跨实例部署和故障演练仍未验收。`apps/api/vendor` 目录存在若干沙箱历史会话产生的不可读条目，当前通过 site-packages 导入绕过；后续需要重建 vendor 目录或迁移到正式依赖管理。
- S3 适配器已实现接口、SHA-256 metadata 和携带持久化 key 的跨实例 Multipart；尚未做真实 MinIO/S3 集成测试和凭证管理。
- 上传 API 当前没有正式 OIDC、设备 token、配额、流式大小限制和恶意文件扫描；项目路径授权目前是开发版 Session/RBAC 入口。
- Bundle API 当前使用临时本地文件和同步恢复；生产需要权限、大小限制、任务队列和可恢复对象写入。恢复后的事件会进入 outbox，但 outbox 本身不随项目 Bundle 作为投递状态恢复。
- SQLite 高风险 Artifact、Review、Evidence、Handoff 创建/接收/拒绝写入已收紧为业务状态、事件和 outbox 同事务；PostgreSQL Review/Handoff 写入口已纳入事务，Artifact/Bundle 全部业务编排仍未迁移到同一套事务方法。
- Event Outbox 已具备开发版原子领取、锁过期和通用发布器接口；P4-04 已加入 PostgreSQL `SKIP LOCKED` 多实例验收入口，但尚未接入 NATS、完成真实数据库验证或实现消费者幂等。
- 设备登记已具备 challenge/signature 公钥持有证明；Agent Token 已接入开发版 Windows Credential Manager，
  P3-20 已完成开发版轮换、旧 Token 失效和连接撤销
  适配器，但当前开发机的长期 `CRED_PERSIST_LOCAL_MACHINE` 写入实测返回 `1312`，尚未完成目标部署身份下
  的跨登录会话持久化验收；轮换后的新 Token 尚未完成目标部署身份下的系统凭据原子更新与恢复验收；配对注册也未接入正式 OIDC 或设备级会话认证。
- HTTP Agent 任务/Run/Artifact 写路径和 Gateway 任务/Run/事件/Handoff/Review/Artifact 命令均已强制使用项目能力 Token，但这只是开发版 SQLite 控制平面边界；Artifact 文件内容仍通过 HTTP/Multipart 上传。
- `AgentConnection`、WebSocket Gateway、心跳接收、序号确认、断线补传和本地事件缓存均已有开发版实现；真实 TLS、跨进程和长时间恢复仍未验证。
- P3-10 当前只是传输无关的会话 Worker Broker；P3-11 已在本机 Windows 同账户环境完成 Named Pipe
  开发版验证，P3-16 已新增可测试的 Windows Service/Session 0 适配边界和 readiness 契约，但尚未完成
  真实服务安装、跨账户/跨 Session 实机矩阵、服务账户 ACL 演练、长时间恢复和生产硬化；不能据此宣称本机 IPC 已生产可用。
- P3-12/P3-14 的标准 Runner 仍使用 stdin/stdout/stderr 管道；P3-15 已增加真实 ConPTY 开发版后端。
  P3-13 已完成开发版云端事件上传、Artifact 绑定和 Run 结果接入，但尚未实现桌面控制、Windows Service/Session 0
  Worker 的真实安装部署和正式 Run 结果门禁的生产闭环。
- P3-14 的 `MachineServiceLifecycle` 仍是传输无关状态机；P3-16 已提供 Windows Service/Session 0
  适配器，但真实 SCM 安装、Session 0 服务账户、锁屏/注销通知和 Worker 进程恢复仍需实机验收。
- `session.terminal.resize` 在标准管道 Runner 中明确 fail-closed；P3-15 的 ConPTY 后端已通过开发机
  resize、输入、输出和退出回收测试，但多账户、锁屏、注销、睡眠唤醒和服务启动矩阵仍未验收。
- P3-17 的 Codex Adapter 已通过当前版本 `0.153.4` 的版本探测和 JSONL/Runtime 契约测试，但真实
  模型任务、其他版本族/操作系统矩阵、审批响应回传和 Claude Code 语义适配仍未完成；未知协议只在
  开发版 Runtime 中 fail-closed，不能据此宣称生产 CLI 支持。
- P3-04/P3-07/P3-18/P3-20 的 PostgreSQL 设备/Gateway 方法已写入 Repository；P4-04 新增迁移 013，强制所有租户表执行 RLS，并为组织范围幂等记录和事件幂等键补齐约束，但没有真实 PostgreSQL 连接池、RLS、事务回滚、并发注册、并发轮换和并发序号验收。
- Gateway 目前可以依赖统一 Repository 协议，但默认 API 运行时仍是 SQLite；无法据此宣称跨实例 Gateway 已验收。
- Gateway 当前已支持任务/租约/进度/结果、Run、Handoff 和 Agent Review 命令的开发版处理，但 Artifact 上传、人工审批端到端协调和终端/桌面控制尚未接入。
- 结果持久化当前在业务命令执行后单独写入；跨实例并发时可能出现两个执行者都产生副作用后才由唯一约束收敛，需在后续事务化命令执行切片修复。
- P3-05/P3-06/P3-07/P3-23 已收紧 HTTP 和 Gateway 的项目能力边界，并完成开发版丢 ACK 后业务结果恢复与命令请求指纹；正式 PostgreSQL 任务租约/Run 编排、执行中抢占/恢复、同事务副作用保证和 Token 的系统密钥环管理仍未完成。
- 当前 WebSocket 测试使用协议服务和轻量 Fake WebSocket，真实 TLS、反向代理、跨进程连接和长时间断线恢复仍未验证。
- `agentd` 已提供开发版 `service-run`，具备 Machine Service 级别的自动重连退避、心跳调度、直接子进程监督和本地紧急停止；P3-16 已提供独立的 Windows Service 安装/升级/控制 CLI 和 Session 0 适配边界，但尚未完成真实服务安装、用户会话 Worker 长时间运行和系统会话通知实机验收。
- `apps/agent/runner.py` 和 `apps/agent/result_uploader.py` 已提供开发版 Python/Command Runner
  结果接入；可以生成 Run Manifest、上传 Artifact 和写入平台事件，但仍未完成系统调用级文件访问
  审计、生产隔离和跨服务事务。
- P3-09 已将 `execution_profile` 贯通 Run 领域对象和数据库；旧数据库/旧幂等响应使用兼容的
  HEADLESS 默认值，仍需生产迁移演练。
- `credential_store.py` 已接入 Windows Credential Manager 开发版；当前不能把命令行参数或未验证的
  Session Credential 作为生产凭据方案，必须完成目标用户/服务身份下的持久化、ACL、轮换和恢复验收。
- 本地状态库目前没有加密、磁盘配额、日志轮转和损坏恢复策略，仍属于开发版恢复日志。
- Handoff 接收人匹配、拒绝/返工/汇总语义、Fanout 多接收方收据、Handoff Gate 下游门禁和风险关闭操作已在 P4-01/P4-02 开发版接入；正式设备会话/OIDC、跨实例幂等竞争和生产 RLS 仍未完成。
- Run 已能由本地开发版 Manifest 登记容器调用上下文和信息边界审计结果；Docker/Podman 真实隔离、系统调用级文件/网络观察、未来数据真实阻断和平台 Run 表的执行后端结构化登记仍未完成。
- WebSocket 仍是进程内广播，NATS JetStream 和断线事件游标尚未接入。
- P4-03 的 Gate 影响图传播已完成 SQLite/PostgreSQL 代码切片；P4-04 已加入真实 PostgreSQL 多实例传播验收入口，但当前仍因没有数据库服务未执行；当前没有浏览器自动化、移动端视觉和多实例传播作业的真实验收。

## 本批次新增接口

- `POST /api/devices/pairings`
- `POST /api/devices/register`
- `GET /api/devices`
- `POST /api/devices/{device_id}/revoke`
- `POST/GET /api/projects/{project_id}/device-grants`
- `POST /api/device-grants/{grant_id}/revoke`
- `WS /ws/agents/{device_id}`
- `apps/agent/agentd.py gateway-queue`
- `apps/agent/agentd.py gateway-recover`
- `apps/agent/agentd.py gateway-run`
- `X-Project-Capability-Token`：HTTP Agent 任务/Run 写请求的项目能力 Token 请求头
- `apps/agent/agentd.py lease-heartbeat`
- Gateway message types：`agent.task.claim`、`agent.task.lease.heartbeat`、`agent.task.progress`、`agent.task.result`、`agent.run.create`、`agent.run.complete`、`agent.handoff.create`、`agent.handoff.accept`、`agent.handoff.reject`、`agent.review.submit`、`agent.artifact.create`
- Gateway ACK payload：`command_result`
- `packages/agent_protocol/gateway.schema.json`
- `apps/agent/agentd.py service-run`
- `apps/agent/agentd.py service-emergency-stop`
- `apps/agent/agentd.py service-clear-emergency-stop`
- `packages/agent_protocol/session.schema.json`
- `apps/agent/credential_store.py`：Windows Credential Manager、目标名校验和系统凭据错误边界
- `apps/agent/test_credential_store.py`：内存凭据、Windows API Fake、持久化失败和 Agent 取证测试
- `apps/agent/agentd.py credential-save`
- `apps/agent/agentd.py credential-delete`
- `POST /api/devices/{device_id}/rotate-token`
- `Device.token_version`、`Device.token_rotated_at` 和 `DeviceTokenRotateRequest`
- `apps/api/migrations/009_device_token_rotation.sql`

- Gateway `agent.handoff.create`
- Gateway `agent.handoff.accept`
- Gateway `agent.review.submit`
- Gateway `agent.artifact.create`
- `PostgresRepository.create_handoff()`、`accept_handoff()`、`create_review()`
- SQLite/PostgreSQL Handoff/Review 请求指纹幂等记录
- `GatewayCommandResult.request_hash` 和 `apps/api/migrations/010_gateway_command_request_hash.sql`
- `POST /api/handoffs/{handoff_id}/reject`
- `GET /api/projects/{project_id}/review-center`
- `apps/api/app/risk_rules.py`：统一风险规范化和 Gate 判定
- `apps/api/migrations/011_workflow_review_relations.sql`
- `apps/api/migrations/013_p4_04_force_rls_and_event_idempotency.sql`
- `apps/api/test_stage4_integration.py`
- `scripts/test-stage4-integration.ps1`

- `POST /api/artifacts/{artifact_id}/content`
- `GET /api/artifacts/{artifact_id}/content`
- `POST /api/artifacts/{artifact_id}/multipart`
- `PUT /api/artifacts/{artifact_id}/multipart/{upload_id}/parts/{part_number}`
- `POST /api/artifacts/{artifact_id}/multipart/{upload_id}/complete`
- `DELETE /api/artifacts/{artifact_id}/multipart/{upload_id}`
- `POST /api/agent/projects/{project_id}/artifacts`
- `POST /api/agent/artifacts/{artifact_id}/content`
- `POST/PUT /api/agent/artifacts/{artifact_id}/multipart...`
- `GET/POST /api/projects/{project_id}/git`
- `POST/GET /api/projects/{project_id}/git/index`
- `GET /api/projects/{project_id}/export`
- `POST /api/projects/restore`
- `apps/api/app/postgres_repository.py`：第一批 PostgreSQL Repository 查询与事务接口
- `apps/api/app/outbox.py`：传输无关的 EventOutbox Dispatcher 开发版
- `apps/agent/named_pipe_transport.py`：Windows Named Pipe、ACL 和对端进程身份传输适配
- `packages/agent_protocol/session.schema.json`：User Session Worker IPC Schema
- `apps/agent/session_runtime.py`：User Session Worker 与 Runner 事件编排
- `apps/agent/result_uploader.py`：输出发现、哈希、Run Manifest 和 Artifact 上传队列
- `AgentEventPayload`：Gateway 运行事件载荷；`RuntimeEventUploader`：本地事件入队适配器
- `apps/agent/windows_service_adapter.py`：Windows Service/Session 0 开发版适配器
- `apps/agent/test_windows_service_adapter.py`：SCM、WTS 事件、Worker 协调和生命周期审计契约测试
- `apps/agent/cli_adapters.py`：Codex/Claude CLI 能力探测、Codex JSONL Adapter 和协议 fail-closed
- `apps/agent/test_cli_adapters.py`：版本、命令、安全参数、JSONL 解析和 Claude 能力状态测试
- `apps/agent/agentd.py cli-capability`：本地外部 CLI 能力诊断入口
- `apps/agent/test_container_runner.py`：容器挂载、输出目录门禁、输入重叠和 Manifest 脱敏测试
- `SessionRunStartPayload.execution_backend`：显式选择 `host` 或 `container`
- `agentd session-worker-run --container-runtime docker|podman`：显式启用容器后端
- `packages/competition_packs/loader.py`：pack manifest/TemplateSpec/DagTask/UpgradeRule/PackManifest/CompetitionPack 加载、`{{var}}` 渲染 fail-closed、`plan_upgrade()`、`resolve_pack_id()`
- `packages/competition_packs/materializer.py`：`PackMaterializer.apply()`（DAG→任务+依赖、模板→成果物+工作区骨架、问题子集、幂等）
- `packages/competition_packs/validation.py`：`PackValidator.validate()` / `validate_pack()` / `_markers_for()` / `_is_scaffold()`（四问覆盖、章节与字数、官方结果表、JSON 必需/禁止字段、信息边界）
- `packages/competition_packs/cumcm/manifest.json`：pack 元数据、9 模板、必需成果物、校验规则、信息边界规则、升级规则
- `packages/competition_packs/cumcm/four_question_dag.json`：17 个任务的四问 DAG
- `packages/competition_packs/cumcm/schemas/problem_facts_schema.json`、`data_profile_schema.json`
- `packages/competition_packs/cumcm/templates/`：PROBLEM_ANALYSIS.md、PROBLEM_FACTS.json、DATA_PROFILE.json、MODELING_REPORT.md、CODE_README.md、EXPERIMENT_PLAN.md、RESULT_TABLE_TEMPLATE.md、COMP_REVIEW.md、PAPER_OUTLINE.md
- `apps/api/test_competition_packs.py`：pack 加载/渲染/物化/校验 15 项契约测试

## W-1 项目工作区（2026-09-22）

- **W-1 已交付并上线**（`https://synapforge.top`，`server_verify.sh` 22/22；交接 `docs/handoffs/W1_PROJECT_WORKSPACE_HANDOFF.md`）。项目内有了主工作区域：`/workspace` 单页四 Tab（聊天 / 任务 / 成果空间 / 概览）+ 常驻成员概览。
- **Agent 进群聊但不发言**：`Store._insert_event()` 是事件→卡片的唯一实现点（同一事务），Agent 的领取/进度/成果/复核动作由服务端渲染成卡片（带 `ref_task_id`/`ref_artifact_id` 可跳转），`packages/agent_protocol` 完全没动；运维噪声不进流。
- **实时**：同步路由跑在线程池里没有事件循环，推送用 `lifespan` 抓住的主循环 + `run_coroutine_threadsafe`；维护循环每 10 秒兜底桥接与推送。`connected` 帧直接带最近 50 条消息，页面首屏不必再拉一次。
- **老项目历史一次性进流**：启动时按项目水位线回填（线上 2 个项目 29 张卡片），水位线按"已检查序号"推进，非卡事件不会造成漏扫或重复。
- **测试**：新增 `apps/api/test_workspace.py` 16 项；全量 410 项（仅 2 项 LaTeX 环境失败）；前端 25 页；SSR 不变量 19×19×7；浏览器实机在 `required` 模式下验证了"发言 + Agent 卡片实时到达"。
- **本轮修掉三个缺陷**：`project_workspace` 变量遮蔽导致 viewer 身份错人（补回归测试）、`run.*` 卡片显示「任务」而非任务名（含线上 60 条卡片就地重写）、窄屏面包屑逐字竖排。

## W-2 任务板与成果空间（2026-09-22）

- **W-2 已交付并上线**（`https://synapforge.top`，`server_verify.sh` 22/22；交接 `docs/handoffs/W2_TASK_BOARD_DELIVERABLES_HANDOFF.md`）。
- **派单口径落成代码**（`Store._dispatch_policy`）：manual（默认）只有队长能改负责人；hybrid/auto 下成员可认领**无人**任务、可释放自己认领的；任何人不能派给别人、不能动别人负责的任务；目标必须是项目成员。批量派单 `POST /api/projects/{id}/tasks/assign` 逐条走同一策略、按条返回失败原因。
- **派单进群聊**：`task.dispatched` 事件（`action=dispatch|self_claim|release`）→ 卡片「队长把任务「X」派给了 Y」/「Y 认领了任务「X」」/「回到未指派」。
- **成果空间聚合**：`GET /api/projects/{id}/deliverables`（读时聚合、不落表）——成果物 + 文档三层版本（草稿/提交/批准）+ 交接单 + 复核与门禁 + 未关闭风险；`reviewer` 统一解析成显示名。
- **测试**：新增 `apps/api/test_workspace_dispatch.py` **22 项**，其中 **5 项是 HTTP 层**（TestClient + `required` 模式）——本轮抓到并修掉 `PATCH /api/tasks/{id}` 缺 `Request` 参数导致的 500（store 层测试发现不了），顺带把该端点的审计主体从写死的 `member-001` 改成真实会话成员。全量 **432 项**（仅 2 项 LaTeX 环境失败）；前端 25 页；SSR 19×19×7；浏览器实机验证批量派单与成果空间四面板；线上就地验证零副作用。

## W-3 全自动调度器（2026-09-22）

- **W-3 已交付并上线**（`https://synapforge.top`，`server_verify.sh` 22/22；交接 `docs/handoffs/W3_AUTO_SCHEDULER_HANDOFF.md`）。
- **粒度**：`task_mode=auto` 下**每个 tick 每个项目只推进一件事**（派一条任务，或提示一次"没人能跑"），每一步都进群聊；队长切回「队长派单」立即停手（已派出的活保留）。
- **调度器只做两件低风险可逆的事**：① 按能力匹配把"依赖已满足、无负责人、未过期"的任务派给**有在线且对项目有授权的合格执行体**的成员（负载最轻者优先）；② 派不出去时写一次 `task.auto_unmatched`（幂等键去重），卡片直说缺什么能力。**不做**自动批准门禁、不自动改任务状态。
- **驱动**：维护循环每拍（10 秒）、切到 auto 的那一刻、模板包物化之后——三个入口，都推群聊。
- **测试**：新增 `apps/api/test_workspace_auto.py` **15 项**（含"切回 manual 立即停手""无人可跑只提示一次""离线/无授权执行体不作为目标""依赖未满足跳过""负载均衡"）；全量 **447 项**（仅 2 项 LaTeX 环境失败）；前端 25 页；SSR 19×19×7；浏览器实机走通"切 auto → 立刻派单 → 下一拍提示没人能跑 → 切回 manual 停手"。
- **线上**：部署后就地验证——调度器可调用、线上两个项目都是 manual → 动作"无"、事件与消息数完全不变（零副作用）、三服务 active。

## W-4 导航收敛（2026-09-22，项目工作区收口）

- **W-4 已交付并上线**（交接 `docs/handoffs/W4_NAVIGATION_CONSOLIDATION_HANDOFF.md`）：侧栏 7 组 → **6 组 19 入口**，新增「高级工具」收编建模模板包 / 论文交付 / 知识库 / 超图 / AI 问答 / 运行控制台 / 设备与接入 / 项目时间线；**只隐藏不删除**（路由、页面、深链、Ctrl+K 全部保持）。
- 纯前端改动（`lib/nav.ts` + SSR 不变量脚本的 6 分组期望值）；SSR 不变量 19 路由 × 19 入口 × 6 分组；浏览器实机核对 6 行平铺、被移动页面的面包屑与分组自动展开。
- **项目工作区（W-1～W-4）四期全部交付**：工作区骨架 → 任务板派单与成果空间 → 全自动调度器 → 导航收敛。
- 随本次发布按用户要求把 **IP 入口 nginx Basic 口令改为 `<Basic 口令已脱敏>`**（旧口令与其文件均已备份为 `*.bak-20260922`）；域名入口仍走账号登录，平台口令有 ≥10 位/非纯数字策略，设不成 6 位纯数字。

## W-5 黑白灰配色（2026-09-22）

- **W-5 已交付并上线**（交接 `docs/handoffs/W5_MONOCHROME_THEME_HANDOFF.md`）：整套 UI 换成黑白灰——强调色改为近黑（`--accent #171717`，深色主题近白），去掉蓝紫渐变、大块彩色底与彩色气泡/徽章；侧栏 `#101823` → `#111111`；状态表达改为「中性底 + 6px 语义色圆点」；吞吐柱改灰阶；焦点环改近黑描边。
- **保留三处降饱和语义色**（green `#3f6b52` / amber `#7a5c1e` / red `#a13c34`，深色主题对应亮版）只用于状态点、关键数字与危险按钮——否则"卡住/未通过"在满屏灰里看不出来。想要全灰的追加 CSS 已写进交接 §4。
- **实现方式**：重写浅色/深色两套令牌（`--blue`/`--violet` 保留为指向中性值的别名，避免大改规则）+ 全局替换散落十六进制色 + 末尾追加覆盖块统一按钮/徽章/气泡/图表；`theme-color` meta 与一处内联色同步。**零结构改动**（无组件/布局/交互变化）。
- **验收**：构建 25 页、SSR 19×19×6、浅色/深色/手机三种状态浏览器实机核对；线上 `server_verify.sh` 22/22，线上 CSS 实测旧蓝紫残留计数全为 0。

## W-6 工作区铺满 · 输入框附件与提及（2026-09-22）

- **W-6 已交付并上线**（交接 `docs/handoffs/W6_WORKSPACE_FILL_AND_COMPOSER_HANDOFF.md`）。
- **布局**：工作区页面补上 `.page-content` 容器；应用骨架改为「内容区自己滚」（`.main-area` 高度 100dvh + overflow），聊天 Tab 时左栏撑满剩余高度、输入区钉底、聊天流内部滚动，**右栏与左栏等高、底边对齐**；≤980px 撤销铺满（避免上下堆叠时把聊天流压扁）。
- **输入框**：新增「上传文件」（云盘上传 → 导入项目成果物 → 发一条带 `ref_artifact_id` 的消息，气泡里可跳「查看成果物」）与 `@成员` / `/Agent` 提及（候选弹层、↑↓/Enter/Esc、消息里浅底高亮）。后端新增 `ProjectMessageCreate.ref_artifact_id` 并校验成果物属于本项目（跨项目引用 409 `message_artifact_not_in_project`）。
- **口径**：提及只用于点名与留痕，**不会自动派活**（Agent 读不到聊天，协议禁区）——输入框下方的提示与交接文档都写明。
- **验收**：`test_workspace.py` 新增 2 项（全量 449 项，仅 2 项 LaTeX 环境失败）；构建 25 页；SSR 19×19×6；桌面实测左右栏均 611px 且底边对齐、页面不滚动；手机无横向溢出；线上 `server_verify.sh` 22/22，线上 CSS 含铺满与提及规则。

## W-7 四项体验修正（2026-09-22）

- **W-7 已交付并上线**（交接 `docs/handoffs/W7_UX_FIXES_HANDOFF.md`），纯前端：①总览引导把「应用模板包」改为「准备任务」双路径（自定义任务 / 模板包，完成条件只看有没有任务）；②任务板新增「新建任务」弹窗（标题/说明/阶段/优先级/负责人/截止，自定义任务不再被模板定死）；③聊天消息收紧——系统事件降为 17px 一行灰字、Agent 卡片随内容自适应（min(76%,620px)）；④/ask 重构为 ChatGPT 式（宽栏对话窗口铺满 + 侧栏会话管理 + 未选会话输入即自动新建）。
- **过程中修掉两个回归**：/ask 首屏请求早于令牌恢复导致 401（加 `ready && authenticated` 门控）；自动建会话后重拉列表清掉乐观气泡（带 preferredId 只刷侧栏）。
- **验收**：449 项后端全量不变（无后端改动）、25 页、SSR 19×19×6；浏览器实测四项闭环（卡片一行小字 / 新建任务落库进群聊 / 引导不再指向模板包 / ask 双栏等高 677px）；线上 22/22。

## W-8 品牌 · 开放注册 · ask 侧栏 · 引导 · 演示（2026-09-22）

- **W-8 已交付并上线**（交接 `docs/handoffs/W8_BRAND_OPENREG_DEMO_HANDOFF.md`）：品牌改为 **synapforge · Agent 协作平台**（五处）；**开放邮箱注册**（`PLATFORM_OPEN_REGISTRATION` 默认开：免码注册即 contributor 并自动加入组织内既有项目，带码仍按码上角色，首个账号仍自动管理员；关闭开关即回邀请码模式）；/ask 侧栏会话管理 ChatGPT 式美化（紧凑行+左侧高亮条+悬停删除）；工作区新用户引导面板（三步上手+演示入口，可关）；**多 Agent 文档撰写演示项目**（`scripts/deploy/_demo_doc_writing.py` 幂等种子，用真实 store 调用串"写手+复核"协作故事：4 任务/4 成果物/42 条群聊，线上已种）。
- 新用户闭环：**注册（免码）→ 自动看到演示项目 → 引导面板 → 一键看演示 → 群聊读完整协作故事**。
- **验收**：全量 **453 项**（+4 开放注册测试）；25 页；SSR 19×19×6；浏览器免码注册→令牌→见演示项目全链路；线上 `server_verify.sh` 22/22、公网注册 201、探针账号验证后已清理。

## W-9 桌面端发布 · 下载入口 · 通用 CLI 执行体（2026-09-22）

- **W-9 已交付并上线**（交接 `docs/handoffs/W9_DESKTOP_RELEASE_CLI_EXECUTORS_HANDOFF.md`）。
- **桌面端同步问题澄清**：壳（Electron 托盘+内核管理+配对深链）**不内嵌网页**，网页永远来自服务器；内核走稳定 Agent 协议（本轮未动），所以不被前端迭代拖旧。本轮仍做了品牌改名 **synapforge** + 版本 **0.1.1** + 重建 sidecar 与 NSIS 安装包（`synapforge-setup-0.1.1-x64.exe`，143.8MB，未签名）。
- **平台下载入口**：nginx 域名入口新增 `location ^~ /downloads/`（静态直出，不走 Node）；界面 4 处入口（登录页/注册页/工作区引导/设备页新增常驻「桌面端（推荐）」面板）。线上实测：200 + Content-Length 一致 + **sha256 与本地构建完全一致**。
- **通用 CLI 执行体** `worker_executor="cli"`：提示词驱动命令模板（`{prompt}` 占位符），stdout 即结果、退出码即成败（与声明式命令同一套边界）。API 白名单 `{codex, cli}`（未知执行体仍拒）；Agent 侧 `_executor_kind`/`_cli_command`；`/tasks` 执行方式弹窗新增第三项；`agent_inventory` 新增 workbuddy / zcode 安装探测。新增 `apps/agent/test_cli_executor.py` **12 项**，Agent 套件 293 项全通过、API 453 项。
- **豆包桌面端：接不了**（GUI 无 CLI/自动化接口）——如实记为边界；将来有官方 CLI 时用 `cli` 执行体填模板即可，无需改平台代码。

## W-10 聊天命令 · 成果物抽屉 · /ask 改版（2026-09-22）

- **W-10 已交付并上线**（交接 `docs/handoffs/W10_CHAT_COMMANDS_ARTIFACT_DRAWER_ASK_HANDOFF.md`）。
- **聊天区 `/task` 快捷建任务**：`/task 标题` 回车即建（命令优先于发言），随后发一条带 `ref_task_id` 的消息（气泡可跳任务）；`/` 选择器把命令排在 Agent 之前，输入框上方有预览条。后端 `ProjectMessageCreate.ref_task_id` + 跨项目引用校验。
- **成果物右侧抽屉**：聊天（人类上传消息 / Agent 成果卡片 / 系统行）与成果空间都能一键就地展开——正文 + 来源与状态（产出任务、运行、门禁、血缘、复核）+ 下载 + 成果物库深链；类型以服务端详情为准，二进制不硬解。演示项目重建为**带正文**版本（四份 markdown 文档）。
- **/ask 仿参考图改版**：左列 264px 会话（新对话整宽按钮 + 按日期分组 + 悬停删除），右列对话区顶部工具条（知识库下拉 + 检索会话 + 管理文档 →）、底部整宽输入钉底；窄屏会话列折到上方。
- **顺带修掉真 bug**：`Content-Disposition` 里塞中文文件名触发 Starlette latin-1 编码错误 → **中文名成果物下载一直是 500**；改为 RFC 5987 `filename*=UTF-8''` + ASCII 兜底，三处调用统一走 `content_disposition()`。边界：成果物批准后不可写内容（不可变），给老演示补正文靠重建（`scripts/deploy/_demo_reseed.py`）。
- **验收**：API **455 项**（+2）、25 页、SSR 19×19×6；浏览器三项闭环；线上 22/22，演示内容接口 200 且头正确。

## W-11 对外口径：不再强调「超图」（2026-09-22）

- 用户口径：超图是自家创新点，**对外统一说「图」**。用户可见文案全部改口径：导航/页面标题「超图可视化」→「**图谱可视化**」、`/kb` 与 `/ask` 的「Hyper-RAG」「超图索引/管线」→「索引/检索」、图谱页 tab「超图探索」→「图谱探索」、命令面板与 API 错误文案同步，共 **20 处**。
- 未动的：代码标识（`hypergraph-canvas`、`/api/hypergraph/*`、`hyper-rag-service` 服务名与向后端代理的路径）——那是实现标识，改了会牵连测试与服务契约。
- 已上线：`server_verify.sh` 22/22；线上 `/graph` 已显示「图谱可视化」且页面无「超图」字样，`/kb` 无「Hyper-RAG」字样。

## AIP 对照调研（2026-09-22，非交付物）

- 用户要求"能否下载 AIP 协议文件或开源项目对照一下"。产出两份文档：
  - `docs/reference/aip/standard-notes.md`：AIP 社区站七部分解读文章的**逐篇要点摘录**（带原文链接）；
  - `docs/AIP_COMPARISON.md`：**与 synapforge 的逐维度对照 + 可借鉴清单**（按落地性价比排序）。
- **能拿到的**：规范解读文章（总体架构 / 身份码 / 身份管理 / 智能体描述 / 智能体发现 / 智能体交互）与《能力互联》深度文；**拿不到的**：标准正文（国标渠道，无公开下载）、协议代码（AtomGit 社区项目页需账号与令牌，匿名不可 clone）、第 7 部分「工具调用」解读页已 404——都已在文档里如实标注。
- **最值得借鉴的四条**：①能力描述结构化（"技能表"：技能名+版本+输入+输出），解决我们能力词表自由字符串的坑；②发现产出"候选列表+推荐"而不是布尔过滤；③身份拆"包序列号/实例序列号"两段；④任务补"预算"与"显式证据要求"两个意图字段。另有"信誉分用现有吞吐数据做派单权重"一条。
- **明确不建议照搬**：交互层报文（我们自有协议是仓库禁区，要互通应加 AIP 网关翻译而不是改内核）、CA 三方分离体系（单组织闭环收益低）、可计价/可清算（未商业化前无必要）。

## AIP-1 计划（2026-09-22，**计划未实施**）

- `docs/AIP_1_PLAN.md`：把上面对照里的**第 1–4 条 + 信誉分**做成可执行的改造计划，分三期上线：
  - **AIP-1a 能力卡**（技能名 + 版本 + 输入 + 输出）：新增 `skill_match.py`（技能 id 归一化、`name@>=1.2` 版本约束的最小语义），
    并拆开今天被混在一起的**技能域**与**授权范围域**（`_agent_capability_set` 把 `task.claim`/`artifact.write` 这类 scope 当技能算了——这是误匹配的直接来源）；
  - **AIP-1b 候选列表 + 推荐**：`rank_task_candidates` 单实现，同时服务任务详情、`/team` 补位建议与 auto 调度器选人（平滑成功率 + 负载 + 完全确定的排序，界面显示样本量）；
  - **AIP-1c 身份两段**：`package_id`/`instance_id`/`package_source`（上报优先、推断显式标注），`/team` 加"执行体程序包 × 实例"聚合；
  - **AIP-1d 意图两字段**：`budget`（`max_seconds`/`max_attempts` **强制**，`max_tokens` **只记录不强制**）与 `evidence_requirements`（读时缺口展示；门禁规则仅非空时生效，不自动批准）。
- 数据变更：迁移 `021_agent_description.sql`（`agents` +4 列、`tasks` +2 列、`agents(package_id)` 索引），SQLite 侧同步建表与 `_ensure_columns`；
  **`register_agent` 必须改成显式列名 INSERT**（现在是位置插入，加列即报错）；迁移清单同步 `test_platform_contracts.py`。
- 需要拍板的三个点（文档 §12，已给推荐）：技能域是否现在收窄（推荐做）、token 预算"只记录"是否接受（推荐接受）、上线节奏（推荐 a+b / c / d 三次）。

## AIP-1 第 1 批：能力卡 · 候选推荐 · 身份两段（2026-09-22，**已上线**）

- 按 `docs/AIP_1_PLAN.md` 推进的第 1 批（a+b+c）已交付并上线，交接见 `docs/handoffs/AIP_1AB_DESCRIPTION_DISCOVERY_HANDOFF.md`。
- **1a 能力卡**：新增 `apps/api/app/skill_match.py`（技能名归一化 + `名字@>=1.2` 最小版本语义，不做同义词、不做全套 semver）；
  `agents.capability_cards`（迁移 `021_agent_description.sql`）；**把授权范围（`task.claim`/`artifact.write`）从技能域里拆出去**
  ——那是真 bug：以前一条任务写 `required_capabilities: ["artifact.write"]` 会被"有该授权的机器"满足；老内核零改动照旧工作。
- **1b 候选推荐**：`Store.rank_task_candidates` 单实现（在线 → 平滑成功率 → 在手件数 → 稳定序），
  任务详情、`/team` 补位建议与 **auto 调度器共用同一排序**（界面推荐的第一条就是调度器会派的那台）；
  新端点 `GET /api/tasks/{task_id}/candidates`；离线机器也列出（标注"开机即可接"），界面显示样本量。
- **1c 身份两段**：`package_id`（执行体程序包）+ `instance_id`（实例）+ `package_source`（reported/inferred）；
  取值优先级 **内核上报 > 设备运行态探测值 > 空**（刻意不做 `agent_id` 前缀兜底：`agent-<uuid>` 的前缀不携带执行体信息，
  假分组比"未知"更坏）；设备上报运行态后自动把"未知"补成探测到的版本；`/team` 新增"执行体程序包 × 实例"聚合。
- **前端**：`/team` 技能芯片带版本 + 详情展开能力卡（输入/产出）+ 授权范围分栏 + 程序包聚合 + 候选提示；
  工作区任务板每行「推荐执行体」展开候选面板（含理由与「派给 TA」）。
- **验收**：后端 **483 项**（+12 skill_match / +18 agent_description，仅 2 项既有 LaTeX 环境失败）、Agent **298 项（9 skipped）**、
  前端 25 页 + SSR 19×19×6、浏览器实机（`/team` 与任务板均截图确认）、线上 `server_verify.sh` **22/22**；
  线上用临时探针账号读能力目录与候选端点（**探针账号已清理**），并用 `_aip1_verify.py` 走真实 store 路径验证
  版本下限、大小写归一化、推荐排序与调度器一致性（跑完自清理临时任务）。
- **踩到的坑**：`register_agent` 原来是位置 INSERT，加列即 `table agents has 16 columns but 12 values were supplied`
  → 改显式列名（`_seed` 与快照恢复两处同类写法一并修）；`/api/team/capabilities` 是逐字段构造响应，
  漏字段不会报错只会静默丢失（已补 `packages`）。
- **未做**：桌面端安装包未重建（新参数对已装用户下次封包生效，不影响功能）；能力卡的输入/产出只展示不参与匹配；
  技能名不做"登记表"硬校验。

## AIP-1 第 2 批：意图对象（预算 / 显式证据要求）2026-09-22，**已上线**

- AIP-1 的最后一期，交接见 `docs/handoffs/AIP_1D_TASK_INTENT_HANDOFF.md`。迁移 `022_task_intent.sql`：`tasks` + `budget`/`evidence_requirements`。
- **强制两项**：`max_seconds`（领取时把租约压到上限以内，**续租也不能越过**）、`max_attempts`（数既有 `task_leases`，
  用尽后点名领取报 `task_budget_exhausted`、轮询与自动调度器跳过、写幂等一次性告警）；`max_tokens` **只记录不强制**
  （平台无用量采集，界面如实标注"未强制"）。
- **证据要求**：读时算缺口（`GET /api/tasks/{id}` 返回 `TaskDetail`：任务 + 预算态 + 缺口）；**批准时**校验，
  缺证据 → 拒绝批准并在门禁留下 `task:evidence_requirements` 的 blocking finding（`status=FAILED`）；空值 = 零行为变化、不自动批准。
- **前端**：`/tasks` 详情弹窗新增「预算与证据要求（意图对象）」区块（数字字段 + 证据要求行 + 执行态 + 缺口 + 保存）。
- **验收**：后端 **497 项**（+12 `test_task_intent`，仅 2 项既有 LaTeX 失败）、Agent 298 项、前端 25 页 + SSR 19×19×6、
  浏览器实机保存闭环、线上 `server_verify.sh` **22/22**；`_aip1d_verify.py` 线上四项全 PASS（租约夹紧/次数用尽/缺口+门禁留痕/自清理）。
- **踩到的坑**：`Path.write_text` 在 Windows 把 `\n` 写成 `\r\n` → `pack-source.sh` 的 CRLF 防呆**拦下打包**（`SystemExit(1)`），
  当时 tarball 还是上一批的——看到 "uploaded" 不等于包是新的；清理临时任务要先删 `event_outbox`（`event_id REFERENCES events(id)`）再删 `events`。
- **未做**：token 预算强制（需执行体回报用量）、列表页/交付页的缺口角标、AIP 网关与对外描述接口。

## COST-1：执行用量回报 → 预算可判定（2026-09-23，**已上线**）

- 补上 AIP-1d 的"未做第一条"（token 预算当时只能"只记录"），交接见 `docs/handoffs/COST_1_USAGE_BUDGET_HANDOFF.md`。
- **采集**：`ExecutorEventReporter` 从执行体 JSONL 的 `turn.completed.usage` 累计 token（`feed` 不再因"没有 sink"就跳过解析）；
  `TaskLoop` 自己观测耗时（monotonic 前后差）；完成上报带 `usage`。迁移 `023_run_usage.sql`：`runs.usage` jsonb。
- **判定**：完成 Run 时超预算 → 一次性事件 `task.budget_exceeded` + 聊天卡片；**批准任务时** → 门禁 `task:usage_budget_exceeded`
  blocking finding + 拒绝批准。超时判定留 10%/30 秒余量。**超出预算不自动改任务状态**（判断权留人工门禁）。
- **诚实口径**：不报用量 = 没数据（`usage_reported_runs=0`，界面不显示"0 tokens"）；token 出处（`codex-jsonl`/`agent-reported`/
  `platform-observed`）与耗时出处（`agent`/`platform`）**分开记**。
- **展示**：`/runs` 列表与详情显示用量与出处；`/tasks` 详情显示"已回报 N tokens（M 次执行有数）"；
  `/tasks` 与工作区任务行加「缺证据 N」「超预算」角标（新端点 `GET /api/projects/{id}/task-flags`，读时批量聚合避免 N+1）。
- **验收**：后端 **508 项**（+11 `test_run_usage`，仅 2 项既有 LaTeX 失败）、Agent **302 项（9 skipped）**、前端 25 页 + SSR 19×19×6、
  浏览器实机三处（清单角标 / 任务详情用量 / 运行详情出处 chip）、线上 `server_verify.sh` **22/22**；
  `_cost1_verify.py` 线上 **13 项全 PASS**（含门禁留痕与自清理）。
- **边界**：只有回报了用量的执行体才能判定 token 上限（通用 CLI 通常没有用量）；累计口径是整条任务、单次上限按每次执行判定；
  桌面端安装包未重建（老内核不报用量，不影响功能）。

## DESKTOP-GUI：完整功能桌面客户端（内嵌工作台）2026-09-23，**已上线**

- 用户拍板形态：**内嵌远程工作台 + 可配服务器地址**（计划 `docs/DESKTOP_CLIENT_FULL_GUI_PLAN.md` 的路线 A），
  桌面能力全选（通知+角标、开机自启、内核面板并入主窗口、拖拽上传/深链/快捷键），更新方式＝检查更新 + 提示下载。
- **桌面端**：主窗口从"状态小窗 + 丢给浏览器打工作台"改成**工作台就在应用窗口里**（`persist:synapforge` 持久登录态、尺寸记忆、
  应用菜单与快捷键、外链外部打开、离线页）；系统通知（开始执行/完成/断连，点击直达「我的任务」）、任务栏叠加角标、
  开机自启开关、可配服务器地址、`map://task/<id>` 深链、检查更新（读 `/downloads/latest.json`）、首次运行向导（`setup.html` 重写）。
  内核能力经 preload 桥（`window.synapforgeShell`，20 个方法）接进工作台。
- **前端**：新增 `lib/desktop.ts` 与 `components/local-agent-panel.tsx`（本机 Agent 面板：连接/在执行/已完成/队列/执行体/日志/重扫/暂停/紧急停止），
  顶栏入口仅在桌面端出现（浏览器里零变化）；聊天区拖拽上传；`/tasks?task=<id>` 深链（读 `location.search`，保住全静态预渲染）；4 处下载链接指向 0.2.0。
- **内核**：`sidecar_entry._force_utf8_stdio()`——修复**首次运行真 bug**：打包 exe 在 cp1252 stdout 下打中文日志抛
  `UnicodeEncodeError` → 内核反复崩溃 → 新装用户配不上对（0.1.1 也有）；回归测试 `test_sidecar_entry.py`。
- **验收**：前端 25 页全静态 + SSR 19×19×6；Agent **304 项（9 skipped）**；**打包应用自检对生产**
  `SELFCHECK ok:true / panel_open:true / kernel_contract:v1 / kernel_agents:4`，退出码 0（用真实安装包解包目录 + 线上站点 + 真实内核）；
  线上 `server_verify.sh` **22/22**；`latest.json`（sha256 `b534cf2b…`）与安装包 143,822,449 字节线上可下载且与本地一致；探针账号已清理。
- 交接：`docs/handoffs/DESKTOP_GUI_EMBEDDED_WORKBENCH_HANDOFF.md`。边界：安装包未签名（SmartScreen 提示）、
  通知尚未覆盖"派给我的任务/待复核"（需前端主动请求壳通知）、工作台需要网络（本地离线平台不做）。

## DESKTOP-NOTIFY：桌面通知覆盖「派给我的任务 / 待我复核」（2026-09-23，**已上线**）

- 用户要求补上上一轮的"未做第一条"，交接见 `docs/handoffs/DESKTOP_NOTIFY_ATTENTION_HANDOFF.md`。
- **平台**：新端点 `GET /api/my-attention`（`MyAttention`）——派给我的未结束任务 + 我该复核的未决任务，
  复核口径与 `review.approve` 权限对齐（owner/project_lead/reviewer），只含通知与跳转所需的最小字段。
- **桌面端**：后台每 60 秒拉一次并**发系统通知，点击直达任务详情**；待办数进托盘角标；本机 Agent 面板显示提醒状态块。
  **降噪规则**（纯函数 + 8 项 `node --test`）：首见即通知、`NEEDS_REVISION` 再报一次、普通状态流转不刷屏、
  处理完消失不通知、已见键落 `attention.json`（跨启动不重复）、401 只提示一次并停止轮询。
  **会话令牌只在内存里**（不落盘）：重启后需重新登录才继续轮询——刻意取舍。
- **前端**：`lib/desktop-attention.ts` 登录后交令牌、收到派单事件催壳立刻拉一次（≥5 秒去抖）；浏览器里空操作。
- **验收**：后端 **515 项**（+7）、Agent 304 项、桌面纯逻辑 8 项、前端 25 页全静态；
  **打包 0.2.1 对生产实测**：`通知(assigned)：有任务派给你 — [通知验证] 派给我的任务`、`通知(review)：待你复核 — [通知验证] 待我复核的任务`
  均按预期出现，`SELFCHECK ok:true / attention.ok:true / panel_open:true / kernel_contract:v1`，退出码 0；
  同目录第二次启动通知数 0（跨启动去重）；线上 `server_verify.sh` **22/22**；`latest.json` → 0.2.1；
  线上临时探针数据已按外键顺序删净。
- **构建坑**：electron-builder 在 GitHub 不可达时需带镜像（`ELECTRON_BUILDER_BINARIES_MIRROR` + `ELECTRON_MIRROR` 指 npmmirror），
  只靠"走缓存"不够——`winCodeSign` 可能未缓存。
- **边界**：重启后需重新登录才继续轮询；@我/评论/交接回执未纳入；不发邮件与手机推送。

## CLOUD-1 计划：云端执行体服务器（2026-09-23，**v2 修订：执行体改 opencode + 适配器已实现，部署待百炼 key**）

- `docs/CLOUD_1_CLOUD_AGENT_PLAN.md`：把"另一台服务器跑常驻 Agent 接入平台"写成可执行计划。用户已拍板：另买一台
  （4C8G 或 8C8G）、执行体跑 **zcode** + **阿里百炼 deepseek-4.1-flash**、云端 Agent **只执行任务，与平台运维及宿主机运维无关**。
- **v2 修订（用户拍板）**：执行体 **zcode → opencode**；服务器**已到手**（`154.219.99.75`，Ubuntu 22.04、4C8G、40G、全新）；
  实测推翻了 v1 的"换执行体 = 平台零改动"——`opencode run --format json` 的 stdout 是**事件流**，通用通道既不解析过程事件也拿不到用量，
  所以按用户拍板走了**方案 B（加协议适配）**，见下一节。
- **实测到的事实**（细节与原文样本见 `docs/handoffs/CLOUD_1_OPENCODE_ADAPTER_HANDOFF.md`）：opencode **1.18.32** 官方安装器装成
  185 MB 独立二进制（**不需要 Node**）；`opencode run` 是非交互入口、`--format json` 输出事件流；免费模型**前两次跑通、之后连续 5 次挂住**
  （日志停在 `init`，无输出无退出码，出网正常）→ **不作生产路径**；坏模型名**挂住而不是报错** → 必须靠 `max_seconds`/systemd 超时兜底。
  内核在 Ubuntu 22.04 的 **Python 3.10** 上可跑（67 个文件 `ast` 3.10 语法检查通过，无 3.11+/3.12+ 专有 API），**不必装 deadsnakes**。
- **部署侧仍是那两件**：①**文件凭据后端**（POSIX 0600，Windows 仍走凭据管理器；这是"桌面端令牌不落盘"约束的限定例外，需写进约束清单）
  ②systemd 模板单元 + 安装/配对脚本 + 加固项（另注：该机**没有 swap**，建议加 swap 或给单元设 `MemoryMax`）。
- 分期：P0 代码准备（适配器 ✅ 已完成；剩凭据后端与封装，零风险、本机可测）→ P1 单实例跑通（六项断言，含过程事件与失败关闭）→
  P2 多实例与演示真跑 → P3 网页派活桥（`/ask` → 任务）→ P4（可选）多执行体竞技 + 云端花费只读视图。
- 待用户确认：百炼 `base_url` / `api_key` / 精确模型 id（只写服务器 0600 env 文件）、专用项目名、
  是否按 `CLOUD_1_CLOUD_AGENT_PLAN.md` §5 的顺序把 SSH 改为密钥登录、要不要同时上 codex 作第二执行体。

## CLOUD-1 方案 B：opencode 执行体适配（2026-09-23，**代码完成，未上线**）

- 用户拍板"先按方案 B 来做"：给 opencode 加协议适配，让云端执行体（那台新机器）的**过程事件与用量**能进平台。
  交接 `docs/handoffs/CLOUD_1_OPENCODE_ADAPTER_HANDOFF.md`（含服务器实跑原文样本）。
- **协议化**：`ExecutorEventReporter` 新增 `event_protocol`（`codex`/`opencode`/`none`，**默认 codex** → 既有行为零回归）；
  opencode 分支把 `text`→`agent.message`、`tool_use`→`tool.completed`/`file.changed`（按 `callID` 去重、只报完成态）、
  `step_finish`→用量，出处标 **`opencode-jsonl`**（与 codex 的 `codex-jsonl` 分开记）。
  新增 `apps/agent/opencode_executor.py`：解析 + 摘要（答案在最前、诊断在后）+ 模板 `opencode run --format json --auto {prompt}`。
- **协议怎么选**：`agentd._event_protocol`——显式 `resource_policy.worker_events` > 命令模板首词命中已知执行体（`opencode`）> `none`；
  API 侧 `_validated_resource_policy` 校验 `worker_events`（非法值、与执行体不匹配都在入口拒绝）。
  `opencode` 也进了通用 CLI 探测清单（本机 Agent 面板可见）。
- **顺手修掉一个真 bug**：`reporter` 在常驻体里是进程级复用、用量计数器只在构造时归零 → **第二个任务会报"前两个任务之和"的 token**
  （COST-1 的预算判定跟着虚高）；现在 `started()` 每次执行先 `reset_for_run()`（缓冲/用量/计数/去重集合一起清），并有测试固定住。
- **样本是实跑原文**：opencode 1.18.32 在 `154.219.99.75` 上跑出的 3 行与 7 行 JSONL，**逐字**进测试常量（不是手写近似值）。
- **验收**：Agent **327 项**（304 → +23，9 项既有跳过）、API **515 项**（仅 2 项既有 LaTeX 环境失败）、`py_compile` 通过；
  **真实端到端没做**（要百炼 key），部署（文件凭据后端 / systemd 封装）未开工。
- **边界**：成功判定**只看退出码**（错误事件形状未采到样本，不假装能判）；opencode 的 `cost` 已解析进摘要但**未进 `RunUsage` 契约**
  （花费视图留 P4）；`cache`/`reasoning` 明细丢弃（契约无字段，不虚报）；**前端任务弹窗还没有 opencode 预设**
  （手填模板即可跑，协议会自动判为 opencode），留到 P1 与前端一起发。

## CLOUD-1 部署（2026-09-23，**P1 完成：云端执行体已上线，可跑真任务**）

- P0 三件全部落地并测试：**stdin 隐患修复**（内核给非交互子进程的 stdin 从"继承父进程"改成 `DEVNULL` + 回归测试）、
  **`FileCredentialStore`**（POSIX 0600/0700、目标名哈希不落明文、权限过宽**拒绝**、`os.replace` 原子写、工厂 `auto`）、
  **`deploy/cloud-agent/`**（`install` / `configure-opencode-provider` / `pair` / `prepare-platform-side` / `map-agent@.service` / `README`）。
- **两条被实测推翻的判断（已在计划与交接里更正）**：①「免费模型不可靠」**是错的**——真因是
  **`opencode run` 会读 stdin**，不重定向 `< /dev/null` 就挂住（真 key 也复现，重定向后立即返回）；
  ②「Python 3.10 可跑内核」**是错的**——内核用 `from datetime import UTC`（3.11+），执行体机已装 **Python 3.12.14**（deadsnakes）。
- **服务器 `154.219.99.75` 已装好**：`synapforge` 用户（非 root/无 sudo）、内核 + venv
  （cryptography 50.0.1 + websockets）、opencode **1.18.32**、百炼 provider（别名 `deepseek`，模型串 `deepseek/deepseek-v4.1-flash`，
  **以服务用户身份实跑返回 `OK`**，用量 input 7339/output 2）、`map-agent@.service` 已装**未启动**。
- **部署级发现**：内核给执行体传环境变量是**按白名单**的（`_EXECUTOR_ENVIRONMENT_KEYS`）→ **模型 key 放 systemd env
  传不到 opencode**，必须写进 opencode 自己的 0600 配置（平台不该知道模型是谁家的）。
- 基线：Agent **342 项**（+14 凭据 / +1 stdin）、API **515 项**（仅 2 项既有 LaTeX 环境失败）、部署脚本 `bash -n` / `py_compile` 通过。
- **P1 端到端（线上真跑，2026-09-23）**：设备 `device-cloud-01` / Agent `agent-cloud-01`（platform=linux、active）配对完成，
  只授权「云端智能体」项目（`06f9df2b-…`）；`map-agent@cloud` enabled + active（**0 次重启**、常驻 ~152MB）。
  真跑多条任务：每条 11–16 秒完成、4000–7600 tokens、产出成果物；Run 摘要形如
  `CLOUD-VERIFY\n---\nopencode exit=0 events=3 tools=0 files=0 | tokens=6332（opencode-jsonl） | cost=0 | 产出 1 个成果物（待审）`。
  `scripts/deploy/_cloud_agent_verify.py`（平台机上跑）**六项断言全 PASS + 自清理通过**：设备在线 / 真跑完成 / 用量出处 `opencode-jsonl` /
  耗时出自执行体 / 过程事件（`agent.process.started`、`agent.agent.message`、`agent.process.exited`）到达 / 有成果物。
  **用量不跨任务累加**也已线上验证（第二条 Run 报 7347 而不是两任务之和）。
- **部署期发现并修掉 6 个真问题**（明细见交接文档 §7）：`--url` 子命令遮蔽全局参数（daemon 连了 127.0.0.1，事件全卡 outbox）；
  `credential-save` 缺 `--state-path`（凭据会落到当前目录）；`requirements.txt` 少 `pydantic`；`--grant-only` 参数解析；
  Python 输出缓冲导致 journal 只显示 systemd 行；验证脚本自身（`runs` 表无 `exit_code`、删任务前要先清 `handoffs`/`task_leases`）。
- **未做**：不带 `--auto` 的审批行为验证；P2（第二个执行体 / 演示真跑）、P3（`/ask` 派活桥）。

## MY-AGENT M-1：对话专用通道（2026-09-23，**已上线并线上验证；M-2 页面未做**）

- 用户拍板「对话分两类」：**「项目工作」跑任务、「对话」是单纯对话**。据此加了**不碰任务体系**的对话通道
  （不建 Task、不进任务板、不产生 Run/成果物噪声）。交接 `docs/handoffs/MY_AGENT_M1_CHAT_CHANNEL_HANDOFF.md`。
- **平台**：新模块 `apps/api/app/agent_chat.py` + 迁移 `024_agent_conversations.sql`
  （`agent_conversations` / `agent_turns` / `agent_turn_events`）+ 9 条路由（人类：`/api/my-agent/*`；执行体：`/api/agents/{id}/chat-turns/*`）
  + 新能力 **`chat.run`**（默认授权能力清单已加，后端与前端清单同步）。
- **内核**：`apps/agent/chat_loop.py`（轮询取活 → 跑执行体 → 过程事件逐条回传 → complete 带正文/用量/会话句柄），
  与任务循环**共用执行体槽位**；`agentd` 心跳新增 `models`/`default_model`（来自 `opencode models` 真探测，启动时**后台预热**）；
  `opencode_executor` 解析 `sessionID`。多轮上下文交给 **opencode 自己的 session**（`--session`），平台不拼历史、不存 key。
- **线上验证**：可用执行体列出 **10 个模型**（默认 `deepseek/deepseek-v4.1-flash`）；两轮真对话
  「记住 MANGO → 问它暗号」**答出 MANGO**（句柄冒泡、按轮用量、过程事件、任务板零留痕全部 ✅）。
- **部署期修的 4 个真问题**：①授权选取取到旧行（改为"任一有效授权带 chat.run 即通过"）；
  ②模型列表恒空（`executable` 存的是绝对路径，却拿去比字面量 `opencode`）；③首次 `opencode models` 冷启动
  拖住心跳（改后台预热 + 超时 30 秒）；④`claim_turn` 忘把会话句柄塞进返回的轮次（第二轮会丢上下文）。
- 基线：Agent **359 项**、API **527 项**（仅 2 项既有 LaTeX 环境失败）。**边界**：停止只改状态（无中断通道）、
  不做逐字流式、cache 不计入用量口径。
- **M-2 页面（同日完成）**：`/my-agent` 上线——两模式 tab（对话 / 项目工作）、三栏（会话列表 / 对话流 + composer /
  本轮执行细节）、设备与模型下拉**来自执行体真探测**、过程事件卡片、自动选中最近会话；导航把「AI 问答」换成
  「我的智能体」并提到第一组，手机底栏同改（`/ask` 页保留但不再是入口）。实机验收（桌面 1440 + 手机 390）：
  页面上发消息 → 回复 + 用量 + 过程事件齐全，无横向溢出。前端 **26 页静态**。
  **页面形态按用户追加要求定稿**：左上角不放标题文字（H1 视觉隐藏）、模式切换移到标题右上角、执行细节由常驻右栏改为**抽屉**（默认收起、点开即用），主区域全留给对话；修掉「收起抽屉仍撑出滚动宽度」这条实测坑。
  **部署坑**：生产构建必须 `export NEXT_PUBLIC_API_URL=https://synapforge.top`（否则线上接口全 Failed to fetch，
  首次重建漏了、靠浏览器实机发现）。
- **M-3 附件（同日完成）**：新增 Agent 专用下载路由 `GET /api/agent/artifacts/{id}/content`（**只要求 `artifact.read`**，
  不要求成果物由该 Agent 创建——输入通常是人上传的）；内核 `apps/agent/input_fetcher.py` 把文件落到 `<workspace>/inputs/`
  （真名优先、重名加序号、挡路径穿越、超限拒绝、失败如实回报并写进提示词）；对话轮次可带附件（存 id+名，claim 回传元数据），
  任务的 `input_artifacts` 也接上了（此前内核完全不读）；页面输入区加附件按钮（走云盘→成果物→随本轮）。
  **实测**：上传 md → 导入成果物 → 对话带附件 → 执行体日志 `[inputs] 已落到 inputs/sample.md` 并**读出文件内容**（回复 `M3-INPUT-OK`）。
  **抓到并修掉一个名字 bug**：`Content-Disposition` 里 ASCII 兜底排在 `filename*` 前面，按顺序解析会把中文名换成 `artifact.md`
  （现改为两遍扫描 + 平台给的名字优先）。
- **M-4 真中断（2026-09-23 已上线并线上验证）**：平台加 `GET /api/agents/{id}/chat-turns/{turn_id}`（执行体跑的过程中读状态）；
  内核每 3 秒查一次，一旦 `CANCELLED` 就**杀掉子进程**（真中断，不再等它跑完）。
  抓到并修掉一个判据 bug：`runner is self._default_run_command` **恒为假**（每次取绑定方法都是新对象），
  线上表现是「平台已取消、执行体照跑」；判据改成「有没有注入自定义 runner」，并加回归测试
  （真起 30 秒进程、1 秒后取消，断言很快返回且 complete 带 `cancelled_by_member`）。
  部署过程中执行体入站曾**临时不可达约 10 分钟**（本机与平台机都超时，但出站心跳正常）——**自己恢复了**，说明是一次
  临时阻断（不是 IP 变更、不是凭据、也不是 known_hosts），期间未做任何处置。
  **线上验证**：发一轮让它 `sleep 300` 的任务 → 中途点停止 → 9 秒后内核日志 `[chat] 轮次 … 被取消，已杀掉执行体进程`，
  `pgrep -f "sleep 300"` 无残留 ✅。
- **页面顶部空白修复**（用户反馈）：量出来头部那行被撑到 **187px**（本该 33px）——根因是 `.page-content` 是 grid，
  **auto 行会把容器剩余空间按份分给头部**；给 `#my-agent` 显式写 `grid-template-rows: auto minmax(0, 1fr); align-content: start`
  后头部回到 33px、对话区铺满整屏（160→870）。
- **M-5 计划已起草**（`docs/MY_AGENT_M5_STREAMING_AND_MODES_PLAN.md`，待拍板 2 项）：思考可见（`--thinking` + 新事件类型）、
  计划模式/思考强度（`--agent`/`--variant`，与模型切换同构）、**逐字流式需换通道**（常驻 `opencode serve` + SSE，独立一期）、
  以及"正文分段显示"这个今天就能做的小改。
- **M-5 分段显示已上线（2026-09-23）**：过程中把已到达的 `agent.message` 事件按段落渲染进气泡（诚实标注「生成中（分段显示，不是逐字流）」）；
  同时把对话轮询的空转退避**封顶 5 秒**（默认 30 秒会让「发出去半天没反应」）。
  **实测一个重要结论**：`--format json` 是「一步一条事件」，**纯问答只在最后发一次 text**（对照：一条问答跑 45 秒、平台上只有 `process.started`）——
  所以分段显示对**用工具的多步任务**有效，纯问答仍是非流式；**真流式必须换通道**。
- **M-5a/b 计划**（`docs/MY_AGENT_M5_STREAMING_AND_MODES_PLAN.md`）与 **M-5c 真流式执行计划**
  （`docs/MY_AGENT_M5C_STREAMING_EXECUTION_PLAN.md`，用户已拍板要做）已起草：S-0 采 SSE 样本 → S-1 常驻 `opencode serve` →
  S-2 逐字（`delta` 事件，无需迁移）→ S-3 工具实时 + 权限卡片（取代 `--auto`）→ S-4 思考/模式/强度；
  保留 `--format json` 作为**自动降级通道**并如实标注。
  基线：Agent **369 项** / API **531 项**（仅 2 项既有 LaTeX 环境失败）。

## MY-AGENT M-6：多智能体角色预设（2026-09-23，**R-1 + R-2 已上线并线上验证**）

- **用户诉求**：写作/审核/检索等**角色预设**；「先从数学建模工作流内找对应提示词完成预设，然后你自行补充」；
  「提示词一定要足够详细」「有一个专门的步骤进行提示词撰写」；并明确 **先写执行计划、后续再执行**。
- **计划**：`docs/MY_AGENT_M6_AGENT_ROLES_EXECUTION_PLAN.md`（12 个角色 + 分期 R-1…R-5 + 提示词撰写规范 §4 + 底料来源索引附录 B）。
- **opencode 的 agent 机制已实测（不是猜的，2026-09-23 在执行体上探针验证）**：
  自定义 agent = `~/.config/opencode/agent/<名>.md`（`agents/` 同样识别），frontmatter 的 `description`/`mode`/`tools`
  **都被解析**，**md 正文就是系统提示**（`debug agent` 回显 `prompt` 字段），`opencode run --agent <名>` **真会用它**
  （探针要求回答以 `ROLE_OK` 开头，实跑事件流里就是 `ROLE_OK …`）。附带事实：`opencode agent create` 是**交互式**的
  （非交互会挂住，所以定义一律手写 md）；`agent list` 必须在**工作目录**里跑（从 `/root` 跑会 `PermissionDenied /root/opencode.jsonc`）；
  `agent list` 输出**不含 description**；`step_finish.tokens` 带逐轮账（探针 3 行提示即 3,545 input tokens，**角色提示词变长的代价可量化**）。
- **底料普查结论**（数学建模工作流 11 个子技能 / 9,395 行）：审题/建模/编程/逻辑复核/论文写作/编译合规 **六个角色有成熟提示词可蒸馏**
  （含硬规则、禁令、阈值、检查清单原文）；**检索、作图、论文评审打分三个角色只有碎片或只有规则库，需要新写**；
  `comp-review` 全文近乎可直接搬；工作流里**没有 `paper-figure` 角色文档**（规则库极全、角色缺失）。
- **三个关键设计决定**：① 提示词**只存仓库**（`deploy/cloud-agent/roles/*.md`，可评审可 lint），平台只存"选了哪个角色"，
  **不做第二个真源**；② 角色 id 对齐**已有 pack 阶段词表**（`docs/competition_workflow_adapter.json`），不发明新命名；
  ③ 对话通道的角色 = **人设+方法论+输出契约**（自包含），**不产 PDF、不跑闸门**——文件合同与脚本那套属于**项目工作通道**。
- **排期判断**：M-5b（模式/强度）与 M-6 **共用同一条管道**（会话级设置→claim→内核插命令行→心跳探测→页面下拉），合并做一次；
  建议顺序 **M-6 R-1+R-2（角色机制 + 用户点名的三个角色，不碰执行链）→ M-5c S-0（采样本，零风险）→ M-5b 并入 R-4 →
  M-5c S-1…S-4（常驻 server 与真流式，内核最脆弱的改造，放在角色第一批交付之后）**。

### R-1 + R-2 + R-3 + R-3b 已交付（2026-09-23，**12 个角色全部上线**，交接 `docs/handoffs/MY_AGENT_M6_ROLES_HANDOFF.md`）

- **交付**：角色下拉上线（12 个角色 + 内置计划模式）——审题分析 / 建模与求解设计 / 编程实现 / 图表设计 /
  逻辑对抗复核 / 论文评审 / 中文论文写作 / 论文·Word / 英文论文 / 编译合规 / 文献检索 / 选题规划。
  提示词从数学建模工作流提炼，来源逐条登记在 `deploy/cloud-agent/roles/SOURCES.md`（哪些是"近全文可搬"、
  哪些是"碎片组装 + 大量新写"都写明）；`scripts/prompt_lint.py` 做提示词质量门槛（11 条机械判据）；
  `install-cloud-agent.sh` 第 6 步装角色（只装 `mm-*.md`）。
- **工具边界是真的、而且页面上如实标**：4 个角色（建模/编程/图表/编译）可执行命令与写文件，
  下拉带「（可执行）」+ 选中后显示「可执行命令」；其余 8 个只读。判定取自角色文件的 `tools` 开关
  （**没写 = opencode 默认开启；来源不明的按"能"报**——方向是宁可多标）。
- **机制实测**（不是抄文档）：角色 = `~/.config/opencode/agent/<名>.md`，frontmatter 的 `description`/`mode`/`tools` 都生效、
  **正文即系统提示**、`opencode run --agent <名>` 真选中；`agent create` 是交互式的（会挂住，别用）；
  `agent list` 必须在工作目录跑、且**输出不含 description**（说明改从角色文件读）。
- **口径**：提示词只存仓库（平台只存"选了谁"）；角色名合法性不在平台校验（名字不存在时 opencode 自己报错、如实回传成 FAILED）；
  `agent_conversations.role` 是设置（**只影响下一轮**）、`agent_turns.role` 是这一轮实际用的（历史不被篡改）。
- **验收证据**：`_m6_roles_verify.py`（平台机上跑、`--only` 可分批、跑完自清理）逐角色真跑一轮并断言行形状、
  只读角色无 `file.changed`、`mm-coding` **必须真用过工具**（它真写了脚本跑出结果，事件里有 `file.changed` + `tool.completed`）、
  `mm-paper-zh-docx` 回复里 **0 处 LaTeX 命令**、每轮有 `process.started`、用量可读；
  心跳里的角色清单逐个核过 `executes` 标记；执行体 journal 里能看到真实 argv
  `opencode run --format json --auto -m … --agent mm-review {prompt}`；浏览器实机验证下拉/两个"下一轮生效"提示/回答头与抽屉。
- **顺手修掉的真问题**：①模型切换原本**静默无效**（只写建会话路径，中途换对下一轮没影响）②抽屉盖住自己的开关（点不动、收不起来）。
- **基线**：Agent **378 项** OK（11 skipped）/ API **537 项**（仅 2 项既有 LaTeX 环境失败）/ 前端 tsc 通过 / 线上 `server_verify` 22/22。
- **未做**：R-4（M-5b 模式/强度并入、角色文件 sha256 防漂移）、角色级权限卡片（与 M-5c S-3 合流）。
  **已知**：`opencode/*-free` 免费模型实跑报 `FreeTierError`（平台如实显示失败）；执行体重启后第一次模型探测可能超时（TTL 后自愈）；
  角色提示词的权重类判断（如 `mm-critique` 的六维权重）是**按证据推的相对口径**，不是官方 rubric（提示词里已声明）。

## 下一批次

0. 项目工作区（设计见 `docs/PROJECT_WORKSPACE_DESIGN.md`）：**W-1 / W-2 / W-3 / W-4 四期全部完成并上线**（交接 `W1_PROJECT_WORKSPACE_HANDOFF.md`、`W2_TASK_BOARD_DELIVERABLES_HANDOFF.md`、`W3_AUTO_SCHEDULER_HANDOFF.md`）——工作区四 Tab、事件→卡片桥 + WS 实时推送、派单口径与批量派单、成果空间五段聚合、**auto 模式调度器（能力匹配自动派单 + 派不出去时的一次性提示，每拍每项目只推进一件事，队长切回 manual 即停手）**。W-4 把侧栏收敛为 6 组（深页收进最后的「高级工具」，只隐藏不删除）。
1. 阶段 6 数学建模模板收尾：Web 工作台模板面板（pack 列表、物化进度、一键应用、骨架预览/导出、校验报告展示），以及未测试项记录中的回归补测（见交接文档 `P6_CUMCM_MATH_TEMPLATE_PACK_HANDOFF.md`）。pack HTTP 端点已完成。
2. 阶段 6 剩余工作包：项目模板导出（导出可复用模板骨架）、信息边界审计 Gate（把 pack 的 `information_boundary` 规则接入平台 Gate）、数模审计脚本生成机器 Review、现有 C 题交接包无损导入。
3. 阶段 7：文档模板三层版本（草稿/提交/批准）与 Evidence 关系（结论/图表/运行/文档段落关系）。
4. 管理员权限 ETW 实机验收：当前用户在管理员组但处于 UAC 过滤令牌（`schtasks /RL HIGHEST` 亦返回拒绝），需要一次交互式提升会话完成真实 ETL 采集、样本字段矩阵冻结和权限矩阵；完成后修正解析规则并增加固定样本回归。
2. P4-04-RUN-PROD 的生产收尾：TLS/反向代理、跨实例部署、连接池上限压测、运行时角色密码轮换与密钥托管、备份与回滚演练；本机已完成非 owner 角色最小权限、密码认证和故障演练验收。
3. 把认证从开发 Session 入口迁移到 OIDC，并加入 Agent 设备密钥和项目范围 token。
4. 增加上传配额、流式大小限制、恶意文件扫描和 Bundle 恢复任务化。
5. 接入 NATS/实际消费者并完成生产 outbox 故障演练；补齐 PostgreSQL 业务写入事务。
6. 完成设备授权纳入生产运行时前的目标身份 RLS/并发复验。
7. 重建 `apps/api/vendor` 依赖目录，清除沙箱损坏 ACL 条目。**2026-09-16 复现并补充**：该目录当前不可读（`icacls` 亦被拒），`scripts/start-api.ps1` 因此在本机无法启动——它的依赖探测 `& $python -c "import fastapi"` 会打印 traceback，而 PS 5.1 的 `$ErrorActionPreference='Stop'` 遇到原生 stderr 直接终止脚本。临时绕行：用系统 site-packages（Python 3.12 已装 fastapi/uvicorn/pydantic）且 `PYTHONPATH` 不含 vendor 启动 API，实测可用；脚本层面建议把探测改成 `2>$null` + `$LASTEXITCODE` 判定。
8. 完成 Windows Credential Manager 在目标用户/服务身份下的持久化、ACL、轮换、注销/重启恢复，并继续 Windows Service、User Session Worker 和本机 IPC 的真实安装、权限与恢复验收。
9. P3-23 已完成 Artifact 元数据 Gateway 命令和请求指纹；Gateway 业务执行中的跨实例抢占、结果持久化与全部副作用同事务协调继续延期。
10. P4-05 浏览器端到端验收、真实 API 联调和阶段 4 开发版退出评审。
11. P5-06-REAL 后续：Linux eBPF/LSM、容器观察桥接、网络阻断和平台 Review/Gate 自动写入。
12. P5-05 可复现重跑的平台 Run 接入、正式 Artifact 快照绑定和真实执行。

## MY-AGENT 对话体验（2026-09-24 已上线）

- 「我的智能体」回答改按 **Markdown + KaTeX** 渲染（`apps/web/components/markdown.tsx`：`react-markdown` + `remark-gfm` + `remark-math` + `rehype-katex`；**不给 `rehype-raw`**，回复里的 HTML 只当文本）；模式切换（对话/项目工作）移到**对话框底部**（实测输入框 bottom 410 → tablist 434）。
- **结构化选择卡**：`apps/web/lib/agent-choice.ts` 只认回复**末尾**的 `synapforge-choice` fenced block（普通编号列表、别的 json fence、坏 JSON 都不成卡且不吞原文），渲染成单选/多选/文本表单，必填未选完不可提交；提交后选择以可读文案作为下一条用户消息回发（提交失败不显示成已提交）。
- **协议注入在内核层**：`apps/agent/chat_loop.py` 的 `CHOICE_PROTOCOL_INSTRUCTION` 在每轮提示词末尾追加（此前只是定义未使用），因此任何角色都能出卡；`mm-analysis` / `mm-modeling` / `mm-topic` 三个"把选择权交回用户"的角色补写了"何时出卡、何时仍用文字清单"，来源台账 `SOURCES.md` 新增「选择卡协议」一节。
- 测试：Agent **380 项**通过（+2：协议注入存在且只追加一次、带附件+角色时顺序固定）；API **537 项**（仅 2 项既有 LaTeX 环境失败；注意必须 `PYTHONPATH=apps/api:.`，否则 5 个模块导入失败）；`apps/web/scripts/verify-agent-choice.mjs` **29 项断言**（解析/20 类非法形状拒绝/HTML 只当文本/回发文案，测的是本地 tsc 编出的真源码）；角色 `prompt_lint.py` 12/12 全绿。
- 线上实测（2026-09-24）：一轮真对话同时出 Markdown 表格、两个 KaTeX 公式与一张选择卡；选完提交 → 卡片锁定并回发选择 → 执行体续跑并再次出卡；刷新后历史完整。**修掉一个线上可见的真 bug**：全局 `input, select, textarea { width: 100% }` 把卡片里的 radio 拉成 287px、文字容器压到 0 宽（按字换行）→ 卡片内 radio/checkbox 写死 16px + 文字容器 `flex:1 1 auto; min-width:0`。交接 `docs/handoffs/MY_AGENT_CHOICE_CARDS_HANDOFF.md`，部署记录见 `docs/SERVER_DEPLOYMENT.md` §10。
- 边界：卡片是协议约定不是强保证；卡片"已提交"是前端内存态（刷新回到可提交），要记录需把提交事件落库（未做）。

## MY-AGENT M-5c S-0（2026-09-24 已完成采样）

- **在云端执行体上采到了真实的 `opencode serve` + SSE 事件样本**（opencode 1.18.32，探针装→测→必删，机器状态与采样前一致）：
  一轮"ls → 写文件 → 输出表格"共 16 类事件，其中 `message.part.delta` **51 条逐字增量**（`properties.field` ∈ {text,reasoning}、`properties.delta` 为片段）、
  `message.part.updated` 为整段快照（`tool` part 的 8 次更新构成 pending→running→completed 状态机、`step-finish.tokens` 是每步用量账）、
  `permission.asked` → 回复后 `permission.replied`、`file.edited`、`session.idle`（轮次结束信号）。
- **两条会影响 S-1 实现的实测结论**：① **必须走 v1 通道**（`POST /session` + `POST /session/{id}/message` + `GET /event`）——
  v2 的 `/api/*` 会话运行器**认不出配置里的自定义 provider**（opencode 日志原文 `SessionRunnerModel.ModelUnavailableError: Model unavailable: deepseek/deepseek-v4.1-flash`），
  且 v2 的默认模型是它自带的免费 zen 模型（实测 401 `missing_api_key`）；② **权限请求会真拦路**——`permission.asked` 不回复则该轮不继续（探针实测卡住），
  S-3 必须显式定"没人看着时的默认策略"。另：serve 要设 `OPENCODE_SERVER_PASSWORD`（实测日志警告 `server is unsecured`）；
  `POST …/message` 阻塞到轮次结束才返回（或改用 `POST …/prompt_async`）；`auth.json` 凭据在 v1 通道下**不需要**（A/B 实测）。
- 交接 `docs/handoffs/MY_AGENT_M5C_S0_SSE_SAMPLES_HANDOFF.md`（含逐条样本原文与 v1/v2 对照表、S-1…S-4 的具体改动）。
- 未测（别当已知）：v1 下思考是否有 delta（本轮只有整段快照）、`POST /session/{id}/abort` 的真实中止语义、同 serve 并发两会话。

## MY-AGENT M-5c S-1（2026-09-24 已上线：对话轮次改走常驻 serve）

- 内核新增 `apps/agent/opencode_server.py`：常驻 `opencode serve` 的生命周期（**懒启动、就绪探测、探活失败重启、退场清理**）+ v1 通道封装 + 一轮对话的编排。
  口令每次启动随机生成、只在内存里；服务只监听回环；系统单元加 `--chat-server-port 4199`（**不配=0 就完全不启用**）。
- 轮次执行：`POST /session` → `POST /session/{id}/message`（阻塞）→ `GET /event` 的 SSE 翻译成平台既有过程事件（`agent.message`/`tool.completed`/`file.changed`）；
  用量取 `info.tokens` 并标 `source=opencode-serve`；**取消改用 `POST /session/{id}/abort`**；**server 起不来就自动降级回 `opencode run` CLI 通道**并在日志写明。
- 线上真跑（2026-09-24）：journal 三段日志（服务已启动 / 走常驻服务通道 / 完成（66 字，serve 通道））；平台 `agent_turns.usage.source = opencode-serve`、页面显示 `8570 tokens · opencode-serve`；
  **`kill -9` 掉 server 后下一轮自动重启并续上上下文**；`systemctl restart` 后无孤儿进程、端口释放。测试：`test_opencode_server.py` 16 项 + 循环通道切换 3 项（真 HTTP 假 server，不依赖真 opencode）；Agent 基线 **399** 项。
- 协议细节（本次实测，别再撞）：Basic 认证用户名固定 `opencode`；就绪探针 `GET /api/health`；**v1 `POST /session` 不接受 `model`（带上 400），模型只在发消息时带**；SSE 自然终点是 `session.idle`；权限请求默认按 `--auto` 等价语义放行（真卡片属 S-3）。
- 交接 `docs/handoffs/MY_AGENT_M5C_S1_SERVE_CHANNEL_HANDOFF.md`。未做：逐字 `delta` 帧（S-2）、权限卡片（S-3）。

## MY-AGENT M-5c S-2（2026-09-24 已上线：回答边生成边出现）

- 内核从 SSE 的 `message.part.delta` 攒增量并按 **`delta` 事件**推给平台（平台零改动：`event_type` 本就是自由字符串）；
  增量**绕开** reporter（1.5s 节流 + 40 条上限会压掉正文），自带节奏（0.8s/32 字一批，封顶 240 条，**收尾一定补发剩下的**，一个字不丢）。
- 页面：`liveSegments` = `agent.message`（整段，CLI 通道）+ `delta` 拼接（增量，serve 通道）；执行中轮询 900ms（空闲 3000ms）；
  抽屉过滤 `delta`（避免刷屏）；状态文案按有没有 `delta` 二选一——**「生成中（增量显示）」vs「生成中（分段显示，不是逐字流）」由事件决定，不夸大**。
- **关键实测坑**：思考（reasoning）与正文（text）的增量**都是 `field=text`**，只能按 `message.part.updated` 的 `part.id → part.type` 区分；
  第一版把模型的英文思考混进了气泡。现在按类型表过滤：`reasoning` 丢弃、类型未知先攒着不猜，`unknown_deltas` 进 `process.exited`（正常 0）。
  这也纠正了 S-0 的误读："思考只有整段快照"不成立——reasoning 也发 delta。
- 线上实测：一轮问答在 20s 时气泡已有 201 字中文正文（4 个 KaTeX 公式边到边渲染），23s 完成（467 字、8546 tokens · opencode-serve）；
  刷新后回答完整、无残留流式状态；全库 `delta` 事件里含英文思考的条数 = 0。
- 测试：`test_opencode_server.py` **19 项**（+3：思考不混入、封顶不丢字、无 delta 通道时退回整段）；Agent 全量 **402** 项；前端 tsc + 生产构建通过；平台 `server_verify` 22/22。
- 交接 `docs/handoffs/MY_AGENT_M5C_S2_DELTA_STREAMING_HANDOFF.md`。边界：思考展示属 S-4、权限卡片属 S-3；轮次事件读取端 `LIMIT 500`（按 sequence 升序），靠"节奏 + 封顶"把单轮压在 500 以内。

## 「我的智能体」体验四改（2026-09-24 已上线）

- **① 对话区固定高度**：真因是 `#my-agent` 的网格行下限写成 `min-content`（= 内容高度），对话越长页面越长、顶部工具栏被顶出视野（实测 6 轮时第一行 7709px）。改成 `#my-agent{grid-template-rows:minmax(0,1fr) auto}` + `.my-agent-layout{grid-template-rows:minmax(0,1fr)}` + 工作视图自己滚；线上实测页面不再滚动、`.ask-messages` 内部滚动、工具栏常驻。
- **②③ 输入区仿 zcode**：卡片式输入区 + 底部控件行（`+` / 角色 / 附件数 / 模型 / 发送）+「+」展开的添加面板（附件 / 角色 13 项 / 执行体，带图标与一行说明，点外或 Esc 收起）；模型与角色从顶部工具栏挪进控件行；图标 14→17/18。
- **④ 对话产出可下载 / 转云盘**：内核新增 `apps/agent/chat_outputs.py`（执行前快照、执行后差分上传；**无 Run**、轮次级幂等键、`inputs/` 不算产出、单文件 64MB 与单轮 20 个上限、**上传失败不改轮次成败**）；平台迁移 `026_agent_turn_outputs.sql`（`agent_turns.outputs`）+ 契约 + `POST /api/drive/from-artifact/{artifact_id}`（复制进云盘、内容去重、仅项目成员）；页面每轮回答下方给出 `名字 + 大小 + [下载] + [转入云盘]`。
- 线上实测：写 `notes/squares.md` → 页面出现 `squares.md 136 B` 两个动作 → 转入云盘后云盘文件数 3→4 → 下载触发浏览器下载事件；**哈希对账**：执行体文件与平台成果物 `content_hash` 同为 `b7473cb44b58851c74f516f04048234c2aeb585c28450ca7b24d3e9a5927c315`、136 B、`PENDING_REVIEW`。
- 两个已知事项（不改，记录）：**下载不能用 `<a href>`**（不带 Authorization，强制鉴权下 401 —— 用 `downloadArtifactContent`）；**成果物没有删除接口**（测试产出的 `squares.md` 仍在项目里，请从审核页退回；删行会让那一轮的下载按钮 404）。另：同一会话内换角色时协议层真的换了（日志 `agent=mm-coding`），但模型可能接着上一轮的拒绝说下去——要干净开场就开新会话。
- 测试：Agent **409** 项（新增 `test_chat_outputs.py` 7 项）；API **544** 项（新增 `test_agent_chat_outputs.py` 7 项；迁移清单契约补 `026`）；前端 tsc + 构建通过；平台 `server_verify` 22/22。交接 `docs/handoffs/MY_AGENT_CHAT_UX_AND_OUTPUTS_HANDOFF.md`。

## MY-AGENT M-5c S-3（2026-09-24 已上线：权限卡片，真拒绝就不执行）

- **实测口径（探针三遍）**：同一个 serve 上只换权限回复的取值——`reject` → HTTP 200 且**文件确实没被写出来**；`once` / `always` → 写出来了。字段原样收：`permission`（如 `external_directory`）、`patterns`（如 `["/tmp/*"]`）、`metadata.filepath`、`tool.callID`。
- 平台：迁移 `027_agent_turn_approvals.sql` + 表 + 契约 `AgentChatTurnApproval{,Request,Decision,State}` + 四个接口（执行体上报 / 执行体轮询状态 / 执行体标过期 / 人做决定 `once|always|reject`）。幂等：主键是 opencode 的 `per_…`，**决策只认第一次**，`decided_by` 记谁批的。
- 内核：`permission.asked` → 组卡上报 → **有界等待**人批（`chat_approval_timeout_seconds` 默认 300s）→ 按决定回复；**等不到就 reject + 标 EXPIRED**（没人批 = 不执行，与 `--auto` 时代相反，刻意如此）；上报/轮询失败不挂死这一轮；过程事件 `approval.requested`/`approval.decided` 进抽屉。
- 页面：待批准卡片（标题 + 一句话 + `permission · patterns` + 三档按钮），批准后显示「已批准 · 由你（昵称） · once」；等批时状态文案「等你批准（执行体已停下等你的决定）」；新会话重置为默认角色（原来会继承上一条的只读角色，实测踩到）；被拒导致"0 字回复"时如实说明一句。
- **线上验收（两个方向）**：拒绝 → 卡片 `is-denied`、journal `→ reject（已回复：True）`、平台行 `DENIED`，**执行体私有 tmp 里没有该文件**；批准（always）→ `is-approved`、平台行 `APPROVED`、**文件存在且内容为 `APPROVED`**。
- **机关**：执行体单元的 `PrivateTmp=yes` 让它看到的 `/tmp` 是私有的（真实路径 `/tmp/systemd-private-…-map-agent@cloud.service-…/tmp`）——在宿主 `/tmp` 查文件永远"没有"，拿它当证据会得出错误结论（这一期就差点误判）。
- 测试：Agent **413** 项（+4）、API **554** 项（+10）；前端 tsc + 构建通过；`server_verify` 22/22。交接 `docs/handoffs/MY_AGENT_M5C_S3_APPROVAL_CARDS_HANDOFF.md`。边界：CLI 回退通道没有权限事件（仍是 `--auto`）；默认保守（没人批不执行），要无人值守放行把 `chat_approvals=false`。
- **本期自己引入又修掉的回归**：新表 `agent_turn_approvals` 的外键没进删除路径，导致**带权限请求的会话删不掉**（界面点确认没反应）——`delete_conversation` 现在先删审批行再删轮次，并加了契约测试。**规律**：往"挂在轮次上"的表里加东西时必须同步改删除路径（历史上 events 也是这么补的）。

## MY-AGENT M-6 R-4 其二/三（2026-09-24：角色指纹 · 硬规则摘要 · 防漂移）

- 内核：角色探测新增 `sha256`（前 12 位）/`bytes`/`modified_at`/**硬规则摘要**（按 `## … 硬规则 …` 一节抽前三条——该骨架由 `prompt_lint.py` 机械校验，按标题抽是稳定的）/`drifted`/`installed_at`；摘要抓不到就空（不编）。
- 防漂移：安装脚本第 6 步写 `.mm-roles.json`（`{installed_at, roles:{名: sha256}}`）当"仓库那份在执行体上的投影"；文件哈希与清单不符 = 执行体上被手工改过。**真机实测**：手工给 `mm-research.md` 追加两行 → 页面显示新哈希 `3f1cf2943886` 并报橙字告警 → 按告警指引重跑安装脚本 → 恢复"与部署清单一致"。
- 页面：抽屉「角色定义」卡（角色名 + 定义版本 + 说明 + 硬规则前三条 + 一致/告警），取**这一轮实际用的角色**（`detailTurn.role`），保证"页面显示的"与"实际跑的"对得上。
- 测试：内核新增 `RoleFingerprintTests`（指纹/摘要/无清单不报漂移/一致不报/不一致必报），Agent **416** 项；平台 `test_agent_chat*` 40 项；前端 tsc + 构建通过；`server_verify` 22/22（两次发布）。交接 `docs/handoffs/MY_AGENT_M6_R4_ROLE_FINGERPRINT_HANDOFF.md`。
- 未做（如实记）：**"模式/强度并入"**（= M-5c S-4 的 `--variant`，常驻通道静默无效，两条路等拍板）；**清单只能证明执行体侧未被手工改**，"仓库改了 md 未重装"需部署时上报仓库侧哈希（后继项）；"角色 md 写 model 谁赢"的实测因 lint 仍禁 `model` 而无落点。

## MY-AGENT M-5c S-4（2026-09-24：思考块已上线；强度按证据暂缓）

- **思考块（已交付）**：内核把 `reasoning` 段增量按新事件 `thinking` 发出去（绕开 reporter、节奏 1.2s、**封顶后收尾补发不丢字**；`process.exited` 带 `thinking_events` 对账）；页面抽屉里「思考过程」折叠块（默认收起）+ 状态文案「思考中（还没开始写正文）」；`thinking`/`delta` 都不进过程事件列表。
  **真跑实测**：224 字推理原文可见；气泡只有答案；事件表 `thinking×5 / delta×4`；**全库回答混入思考的轮次 = 0**。测试：`test_opencode_server.py` 25 项（+2），Agent 415 项。
- **强度（`--variant`）没上，附证据**：CLI 通道接受该参数（`high/minimal/max/default` exit=0，同题 token 画像不同），但**常驻通道把 `message.model.variant` 收下却不生效**——发完消息回读会话仍是 `model.variant = "default"`（v1/v2 两条读接口一致）。上它就是"设了没生效"的静默坑；两条可选路（①只做 CLI 通道并在页面标注；②继续探 serve 侧配置声明 `variants` 的正确形状）见交接 §3。
- **顺手修的两个真问题**：① **中间列横向溢出**（`.my-agent-main`/`.panel` 缺 `min-width: 0`，宽内容把列撑到 1353px / 容器 708px，输入框与发送键跑到视口外→"点不着打不进字"）——已修并注入验证；② **设备授权 24h 过期导致对话通道中断**（执行体每 5s `401 device_project_token_expired`、新建会话报裸错误码）——已用 30 天新授权恢复；**设计缺口**：到期无提醒、无续期提示，建议"授权可选有效期 + 列表显示到期 + 一键续期"（细交接 §4）。
- 交接 `docs/handoffs/MY_AGENT_M5C_S4_THINKING_HANDOFF.md`。

## FM-6 显式跨空间传输与生产加固（2026-09-24：**部分交付**，端到端 15/15）

- **已交付**：跨空间显式传输两条路（云盘→工作区、工作区→云盘 + 存进云盘）、冲突口径（同名不覆盖、
  哈希不符拒收、每次调用都是独立的一次显式复制）、传输历史（会话 + 操作终态 + 失败原因 + 可重试）、
  过期会话回收（删对象）、孤儿对象扫描（**只报告不删**）、S3 后端协议测试（注入客户端 + 云盘服务在 S3 上跑通）。
- **验收** `scripts/deploy/_fm6_verify.py` **15/15**（真 HTTP + 真 Worker：传输往返、并发 8 路全部有终态、
  同名冲突如实失败、回收删对象、扫描不删对象）。测试：`test_file_transfers.py` 12 项、`test_s3_object_store.py` 5 项。
- **测试抓到的真问题**：会话与操作之间没对号（`operation_id` 不回填）→ 传输历史看不到成败与原因；已修。
- **前端跨空间传输入口已做并过浏览器实机**（2026-09-24 追加）：云盘侧「复制到工作区」（目标路径 + 允许覆盖）、
  工作区侧「保存到云盘」（自动两步 + 同名冲突给改名入口）；实机走通双向、冲突与改名；
  顺带修掉一个真 bug——切换「位置」不回云盘不重拉目录（新存的文件不出现）。
- **大文件断点续传已交付**（2026-09-24 追加，迁移 `032`）：`file_transfer_parts` 分片台账 + 续传游标
  + 平台分片接口 + 内核分块重试（哈希一致的片跳过、失败写清重试次数）+ 前端大文件分片上传；
  端到端 `scripts/deploy/_fm6_resume_verify.py` **13/13**（跳片→游标指出缺哪片→补上→逐字节一致；
  注入"第 2 片先失败"→ 自动重试成功；一直失败 → 如实失败不收口）。测试：平台 12 项 + 内核 7 项。
  **顺带修两个真问题**：传输会话按组织收口（同组织同事能读别人的传输内容）→ 改按 owner；
  游标在没收到片时报默认 8MB → 内核把整份大文件当一片传（现改为如实报 0，由客户端定）。
- **未做（逐条）**：真 MinIO/S3 端到端（本机无 Docker）、PostgreSQL/RLS 真机验证、
  长时间 soak 与备份/恢复演练、定时回收任务、批量跨空间传输、浏览器直传云盘的分片化、
  旧接口 `Deprecation` 头、`/devices` 授权视图。
- 交接 `docs/handoffs/FM_6_FILE_TRANSFER_HARDENING_HANDOFF.md`（含 FM 全计划收尾状态与下一步建议顺序）。

## FM-5 云盘文件访问授权与 Agent 读取（2026-09-24：**已交付，平台 20 + 内核 6 + 端到端 22/22**）

- 迁移 `031`（`file_access_grants` / `file_access_grant_nodes` / `file_access_leases`，全开 RLS）+ 服务层：
  默认只读能力（写/删/移动**不在可授予集合**）、粒度 file/folder/drive、文件夹授权**快照**语义
  （勾"包含以后新增"才动态）、过期上限 30 天、撤销 = `epoch+1` **不删行**、设备/成员/任务/Agent 联动撤销。
- Agent 侧：`/api/agent/file-leases/exchange`（明文只签一次、库里只存哈希）+ `/api/agent/drive/files`
  + `/node/{id}` + `/node/{id}/content`（每次重判范围/能力/epoch/设备）+ `/materialize`；
  内核 `drive_materializer.py` 物化到 `<workspace>/inputs/`（sha256 对账、不覆盖、失败如实记账、写 Manifest），
  接入 `daemon-run --drive-grant <id>`。
- **测试抓到真问题**：授权列表原先只按组织收口 → 同组织同事能看到你的授权对象；已改为按 `owner_member_id` 收口。
- 验收 `scripts/deploy/_fm5_verify.py` **22/22**（含撤销演练：撤销后同段 lease 立刻失效、已物化副本按计划保留）。
- **前端已过浏览器实机**（2026-09-24 追加）：详情抽屉「授权给哪些 Agent」显示空态 → 弹窗授权
  （选工作区 + 1/7/30 天）→ 卡片显示 `agent-fm4 · 这个文件 · 到期 …` → 一键撤销后显示「已撤销」；
  平台侧核对 capabilities 只含三项只读能力、`revoked_at` 已写入。
- **偏差**：`/devices` 的授权视图没做；续期接口可用但页面无入口。交接 `docs/handoffs/FM_5_AGENT_DRIVE_GRANTS_HANDOFF.md`。

## FM-3 Agent 工作区文件服务（2026-09-24：**已交付，内核 20 + 平台 22 + 端到端 27/27**）

- **平台侧**：迁移 `030`（`agent_workspaces`/`workspace_operations`/`file_transfer_sessions`/`workspace_audit`，全开 RLS）
  + 服务层（登记、幂等入队、领取、start/progress/complete、取消、过期回收、传输会话、审计）
  + 两套 HTTP 契约（Agent 侧 `X-Agent-Id` + 项目能力令牌；浏览器侧人类会话）。**Gateway 零改动**。
  - 平台只做**语法**路径校验，权威校验在 Agent；工作区只存 `workspace_identity`（路径哈希），**不持有宿主机路径**；
  - 离线**照常入队**保持 `queued`（`fail_when_offline` 才立刻失败）；`complete` 只认第一次。
- **内核侧**：`file_worker.py`（领取→执行→回报；**两道锁**：逐级 `lstat` 拒 symlink/junction/reparse
  ＋ `resolve()`+`commonpath` 兜底；上传先写临时文件再 `os.replace`）与 `safe_archive.py`（先解到临时目录、
  校验通过再原子移动）。`daemon-run --workspace-files` **默认关**（需要新能力，见交接 §4）。
- **新增能力** `workspace.files.{read,write,claim}`：**旧授权串里没有**，已配对设备需重新签发授权。
- **验收**：`scripts/deploy/_fm3_verify.py` **27/27**（真平台 + 真 Worker 真动文件；逃逸样本两道锁全拒、
  保护路径拒删且文件仍在、离线不假装、幂等 409、审计无路径明文）。
- 测试：内核 **20**（含 Windows junction 用例）、平台 **22**；交接 `docs/handoffs/FM_3_AGENT_WORKSPACE_FILE_SERVICE_HANDOFF.md`。

## FM-4 统一文件管理界面（2026-09-24：**已交付并通过浏览器实机验收**）

- `/drive` 左侧「位置」栏 = 个人云盘 + 每个已接入的 Agent 工作区（在线状态 + 最后同步时间）；右侧同一套浏览形态。
- 工作区那侧**队列语义**：每个动作入队 → Agent 领取 → 执行，状态全程可见（失败可重试、排队可取消），
  **不乐观改本地列表**；**保护路径不显示操作入口**（`.git`/`.math-agent-platform`/工作区根/自报保护目录）；
  **离线只读缓存**（最后一次成功目录落 `localStorage` + 「最后同步 X」，不再塞注定失败的 list）。
- 内核一处修正：`list` 结果返回**生效的**保护清单，否则页面会以为 `.git` 可删、点了必被拒。
- 浏览器实机：多根切换、目录进出、新建/上传/下载（真执行）、详情抽屉、离线缓存与排队、390/900/1280/1440
  无横向滚动；页面全文无宿主机绝对路径。
- 交接 `docs/handoffs/FM_4_UNIFIED_FILE_UI_HANDOFF.md`。

## FM-2 个人云盘文件管理器（2026-09-24：**已交付，后端 24 项 + 端到端 47/47 + 浏览器实机走完全流程，未部署**）

- **安全解压**（`app/archive.py` + `POST /api/drive/extractions`）：Zip Slip/绝对路径/盘符/UNC、
  zip symlink、tar symlink/hardlink/设备文件、控制字符、保留名、超深、条目数、单文件与总量、
  压缩比（zip bomb）、归档内冲突、不支持格式、空归档——**22 类攻击样本一律拒绝**；
  嵌套归档与 `__MACOSX`/`.DS_Store` 跳过并如实计数。扫描阶段**不写任何东西**，落库中途失败**补偿清理**
  （用户看到的要么完整、要么什么都没有）。配额在整份解压上一次性过闸。
- **`/drive` 改成目录式文件管理器**：面包屑/双击进入/搜索/排序/游标分页、新建文件夹、
  拖拽上传（**XHR 真进度**）、下载、行内改名、移动（目录选择弹窗）、复制、删除→回收站、
  回收站恢复/彻底清除（各带二次确认）、批量选择与批量操作、解压入口、**详情抽屉**（sha256/修订/
  来源成果物/项目引用/**最近审计**/操作按钮），保留"加入项目"与"转换入知识库"。移动端 ≤900px 收成标签行。
- **浏览器实机抓出并修掉两个真缺陷**：①`loadNodes/loadTrash` 的 `useCallback` 依赖 `notify`（每次渲染都换
  identity）→ 每渲染重新拉目录 + 刚点开的控件被下次渲染换掉（"点了没反应"）；②页面两片空白——全局
  `.page-content` 的行会把剩余高度分掉（指标卡被撑到 214px）、全局 `input/select { width:100% }` 把排序
  下拉拉成整行。修法都在本页作用域内（没动共享 `globals.css`）。
- 样式放独立的 `app/drive/drive.css`（避开并行改动）；测试 `test_drive_extraction.py` 24 项、
  `scripts/deploy/_fm2_verify.py` **47/47**；API 全量通过（仅 2 项既有 LaTeX 失败）、前端 tsc + 构建通过。
- **偏差**：解压不递归嵌套归档、无批量下载、解压为同步请求（异步队列与断点续传属 FM-6）。
  交接 `docs/handoffs/FM_2_DRIVE_FILE_MANAGER_HANDOFF.md`。

## FM-1 个人云盘正式数据模型与服务层（2026-09-24：**已交付并端到端验收 26/26，未部署**）

- **数据模型**：`029_drive_nodes.sql` 建 `drive_nodes`（目录树）+ `drive_project_refs`（项目引用）
  + `drive_object_cleanup`（对象清理队列）+ `drive_audit`（审计），四张表都开 RLS；
  部分唯一索引保证"同父同名唯一（回收站名字可重用）"与"每成员一个根"。老表 `personal_drive_files`
  一行不动，启动时**幂等回填**进节点树（同 id/storage_key/hash/created_at），`project_ids` 展开成引用行。
- **服务层**（`app/drive.py`）：建目录、上传（同目录同名 409、不覆盖）、下载（每次校验 hash）、
  改名/移动（拒环、修订冲突 409）、复制（同名自动 `-副本`）、软删除→回收站→恢复→彻底清除、
  分页/搜索/排序、审计。错误码用计划 §6.5 的 `file_*` 词表；旧路由保持历史形状与历史错误码。
- **四个确定答案**：名字按 NFKC+大小写折叠归一（Windows/macOS 上是同一个名字）；
  配额按**去重后的物理对象**算、软删除仍计入；写操作走进程内写锁 + 事务（SQLite 只有一条连接，
  一个线程 rollback 会丢掉另一个线程未提交的插入——并发测试抓出来的真问题）；
  对象删除**先落库后删对象**，失败留 `pending`+`attempts`+`last_error` 可重试。
- **两处刻意的行为变化**：同内容上传不再合并成同一条记录（现在是两个文件、共用对象、只收一份费）；
  旧 `DELETE /api/drive/{id}` 仍是硬删，新 `DELETE /api/drive/nodes/{id}` 才进回收站。
  「转入云盘」保持幂等（点两次同一个成果物 → 同一份文件；同名不同内容自动加 `-2`）。
- **验收**：`scripts/deploy/_fm1_verify.py` **26/26**（真 uvicorn + 真 multipart + 真下载头 +
  真并发 + required 模式）；新增 `test_drive_nodes.py` 40 项；API 全量 **602 项**（仅 2 项既有 LaTeX 失败）。
- **偏差**：PostgreSQL 侧只到 schema/RLS/索引，服务层 PG 实现与 advisory lock 留 FM-6；
  写锁是进程内的（单 worker 正确）；解压归 FM-2。交接 `docs/handoffs/FM_1_DRIVE_SCHEMA_AND_SERVICE_HANDOFF.md`。

## FM-0 文件管理前置：阻塞修复 · 工作区口径 · 契约边界（2026-09-24，**已完成并端到端验收，未部署**）

- **修了一个"从来没拿到输入"的真 bug**：`agentd._execute_task` 引用从未定义的 `loop_identity`
  （参数名是 `identity`），`NameError` 被"取不到输入也照常跑"的兜底吃掉——任务照常成功上报，
  但提示词里写着"输入文件处理失败"、`inputs/` 永远空着。现在提示词列文件、清单记账。
- **工作区口径统一成一处**（`_workspace_root`）：注册上报的 `local_workspace`、Runner 的 cwd、
  输入落点、产出扫描根是同一个目录；桌面壳新增「Agent 工作目录…」（托盘/菜单/本机页），
  从 `desktop.json` 持久化、每次启动显式传 `--workspace`，内核也把选择合并写进 `platform.json`
  （打包后 cwd 是安装目录这条老坑到此为止）。
- **`inputs/` 不再被当成产出**：任务通道的扫描器补齐顶层排除（对话通道早就排除了）——
  以前输入会被重新上传成"本轮成果物"，同一份文件在项目里出现两次。
- **成员响应不再带宿主机绝对路径**：新增 `app/path_privacy.py`，`GET /api/agents` 给
  `workspace_identity`（sha256 前 12 位）而不是 `local_workspace`；Run 的 `observed_input_files`
  （含边界结论里嵌的那份）与 Artifact 的 `source_path` 只留文件名；设备侧注册/心跳仍返回原值以便对账。
- **输入清单**：`<workspace>/.math-agent-platform/inputs-manifest.json` 记
  `artifact id → 相对路径 → sha256 → 字节数`（FM-5 输入 Manifest 的底座）。
- **端到端验收** `scripts/deploy/_fm0_verify.py`（临时库/临时对象目录跑真 API + 真内核）：**9/9 通过**
  ——输入落地、字节与 hash 一致、清单对账、提示词无"处理失败"、Runner cwd == 注册工作区、
  输入未被重复上传、Run `SUCCEEDED`、任务 `WAITING_REVIEW`。
- **事实核对**：生产对象存储 `OBJECT_STORE_BACKEND=local`（22 个对象；24 个 artifact 里 **5 个没有
  `storage_key`**）；云端工作区 `/srv/synapforge/cloud` 与单元文件一致。**Compose 变量不一致已记录**：
  `infra/docker-compose.yml`（禁区）传的 `STAGE4_MINIO_ENDPOINT` 没有任何生产代码读，Compose 里的
  MinIO 起了但从不被用——留给 FM-6（S3/MinIO 生产化）一起修。
- 测试：Agent **439** 项（11 skipped）、平台 **563** 项（2 项既有 LaTeX 环境失败）、`node --check` 通过；
  本期**未改 `apps/web`**、未跑部署脚本。交接 `docs/handoffs/FM_0_BLOCKERS_AND_WORKSPACE_HANDOFF.md`。
