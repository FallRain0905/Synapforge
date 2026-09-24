# P3-22 Gateway 接力、接收与 Agent 复核交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务编号：`P3-22`
>
> 完成日期：2026-09-14
>
> 适用范围：Gateway 开发版、SQLite 开发 Store、PostgreSQL Repository 开发版契约

## 1. 本轮目标

本轮目标是补齐 Gateway 与结构化协作协议之间的开发版缺口，使本地 Agent 可以通过平台完成以下操作：

1. 创建结构化 Handoff。
2. 接收指向自身的 Handoff。
3. 提交 Agent Review，并推动任务、成果物或 Handoff 进入需要返工/阻塞的状态。
4. 保证 Agent 不能伪装成员提交人工 `APPROVED`。
5. 为 SQLite 和 PostgreSQL 的 Handoff/Review 写入建立可替换的 Repository 契约和幂等语义。

本轮完成的是开发版纵向契约，不代表阶段 3 生产退出，也不代表真实 PostgreSQL、MinIO、NATS、OIDC 或 Windows Service 已验收。

## 2. 已完成内容

### 2.1 Gateway 命令

新增并登记以下消息类型：

```text
agent.handoff.create
agent.handoff.accept
agent.review.submit
```

Gateway 对三类命令执行以下检查：

- 连接上下文仍然有效，设备、Agent 和连接身份一致。
- 项目能力 Token 属于目标项目，并具备对应能力：`handoff.create`、`handoff.accept` 或 `review.submit`。
- Handoff 的任务、输入成果物、输出成果物和证据引用属于同一项目。
- Handoff 接收命令只能由交接包声明的目标 Agent 接收。
- Review 的 `reviewer` 和 `reviewer_kind` 由已认证 Gateway 连接覆盖，不能由 Agent 载荷伪造。
- Agent 提交 `APPROVED` 会返回 `human_approval_required`，不会改变目标为人工批准状态。

### 2.2 PostgreSQL Repository

`PostgresRepository` 已新增：

- `create_handoff()`
- `accept_handoff()`
- `create_review()`

实现边界如下：

- `create_handoff()` 在一个事务中写入 Handoff、创建事件并保存幂等响应。
- `accept_handoff()` 在一个事务中锁定 Handoff、写入接收信息并创建接收事件；已经接收的 Handoff 重放时返回原对象。
- `create_review()` 在一个事务中锁定目标，执行目标状态变化，创建或更新 Gate，写入 Review、Event、EventOutbox 和幂等响应。
- PostgreSQL 幂等记录保存请求指纹；相同幂等键和不同请求指纹会返回冲突，不会静默覆盖原请求。
- 方法通过 `PlatformRepository` 边界提供，Gateway 不依赖具体存储实现。

### 2.3 SQLite Store

SQLite 已支持 Handoff 和 Review 的请求指纹幂等：

- 相同幂等键、相同请求内容会返回原对象。
- 相同幂等键、不同请求内容会返回 `idempotency_key_reused_for_different_request`。
- Review 的目标状态、Gate、Review、事件和幂等记录在一次 SQLite 事务中处理。
- Agent Review 不能批准；成员 Review 才能将目标批准。

## 3. 协议和数据变化

### 3.1 领域协议

P3-22 使用已有的 `HandoffCreate`、`Handoff` 和 `ReviewCreate` 领域模型，补齐 Gateway 命令能力和跨端登记。领域 Schema 和协议目录已登记三类 Gateway 命令，默认项目能力 Token 配置也已同步。

关键字段仍由 Handoff 结构化保存：

```text
task_id
receiver
status
objective
completed
input_artifacts
output_artifacts
key_conclusions
assumptions
evidence_refs
open_questions
risks
next_actions
requires_human_approval
schema_version
handoff_type
receipt_status
```

Review 的目标类型为 `task`、`artifact` 或 `handoff`，结论为 `APPROVED`、`NEEDS_REVISION` 或 `BLOCKED`。

### 3.2 数据库迁移

P3-22 没有新增 SQL 迁移。实现复用了现有 Handoff、Review、Gate、Event、EventOutbox 和 `idempotency_records` 表；PostgreSQL 运行前仍需先应用 `001` 至 `009` 全部迁移。

## 4. 修改文件清单

本轮实现和契约核查涉及以下文件：

- `apps/api/app/gateway.py`：Gateway Handoff/Review 命令分派、项目能力和 Agent 身份校验。
- `apps/api/app/contracts.py`：Gateway 能力、Handoff/Review 请求及响应契约同步。
- `apps/api/app/store.py`：SQLite Handoff/Review 幂等请求指纹和业务写入。
- `apps/api/app/postgres_repository.py`：PostgreSQL Handoff/Review 写入口、事务和幂等辅助方法。
- `apps/api/app/main.py`：现有 HTTP Handoff 路由的边界核查对象。
- `packages/agent_protocol/__init__.py`：跨端 Gateway 命令类型登记。
- `packages/contracts/domain.json`：领域目录和 Gateway 命令目录同步。
- `packages/contracts/domain.schema.json`：领域 Schema 的 Gateway 命令和相关对象同步。
- `apps/api/test_gateway.py`：Gateway 创建、接收、Review、重复命令和 Agent 批准阻断测试。
- `apps/api/test_workflows.py`：接力、分发、门禁回滚和 Handoff/Review 幂等测试。
- `apps/api/test_postgres_repository.py`：PostgreSQL Repository 方法存在性和 JSONB 映射测试。
- `apps/api/test_platform_contracts.py`：平台契约和能力目录回归测试。

本轮收尾还更新了：

- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/AGENT_DEVICE_CONNECTION_DESIGN.md`

## 5. 验证结果

使用的 Python 解释器为：

```text
C:\Users\19855\Documents\ChatGPT\数学建模\C题工作区\.venv\Scripts\python.exe
```

### 5.1 API

```text
python -m unittest discover -s apps/api -p 'test_*.py'
82 passed, 3 skipped
```

跳过项为真实 MinIO/PostgreSQL 条件测试，原因是当前环境没有配置 `STAGE2_MINIO_ENDPOINT` 或 `STAGE2_POSTGRES_DSN`，也没有安装生产依赖。

### 5.2 Agent

```text
python -m unittest discover -s apps/agent -p 'test_*.py'
114 passed, 9 skipped
```

9 项跳过主要包括当前测试虚拟环境未安装 `pywinpty` 的 ConPTY 条件测试，以及非 Windows 分支保护测试。它们不影响本轮 Gateway/Repository 契约结论，但不能作为真实 ConPTY 或生产 Windows Service 验收证据。

### 5.3 编译和 Schema

以下目标 API 模块已通过 `py_compile`：

```text
apps/api/app/postgres_repository.py
apps/api/app/store.py
apps/api/app/gateway.py
apps/api/app/main.py
```

已有共享 Schema、Gateway Schema、Session Schema 和 API/OpenAPI 导入测试保持通过。前端本轮没有修改，未重新执行 Web 构建。

## 6. 未完成内容

### 6.1 本轮明确未实现

- Handoff 的拒绝、返工、汇总和跨任务分发完整语义。
- Artifact Gateway 命令。
- Gateway 业务副作用与 `gateway_command_results` 的跨实例统一事务协调。
- 真实 PostgreSQL 连接池、RLS、并发幂等、事务回滚和 EventOutbox 集成验收。
- 真实 MinIO/S3、NATS、OIDC、生产 TLS 和反向代理长连接验收。
- 人工审批的完整 Web/移动端到 Agent 端闭环。
- 桌面控制、ConPTY 生产矩阵和 Windows Service/Session 0 实机验收。

### 6.2 当前协议差距

- HTTP `POST /api/handoffs/{handoff_id}/accept` 仍从请求体取得 `receiver` 和 `actor_kind`，目前不能视为正式成员/Agent 主体认证；应在正式认证模式下从会话或设备上下文推导主体。
- SQLite `create_handoff()` 和 `accept_handoff()` 当前先提交 Handoff 业务行，再调用独立事件写入；事件写入失败时可能留下没有对应事件的 Handoff。该实现不应与 SQLite Review 的统一事务边界混为一谈。
- PostgreSQL Handoff 接收虽然在数据库事务中写入业务状态和事件，但 Gateway 命令结果保存仍位于业务命令执行之后；跨实例故障窗口仍由 P3-23 处理。
- Handoff 结构允许更丰富的 receiver 类型，但当前 Gateway 接收命令以 Agent 接收为主，成员、团队和汇总接收规则尚未冻结。

## 7. 风险清单

| 编号 | 严重级别 | 风险 | 处理安排 |
| --- | --- | --- | --- |
| R1 | major | SQLite Handoff 业务行和事件不是同一事务，可能出现状态与审计事件不一致 | P3-23 或阶段 4 统一高风险写入编排 |
| R2 | major | HTTP Handoff 接收主体来自请求体，开发路由存在主体伪造空间 | 接入正式认证后收紧为会话/设备上下文主体 |
| R3 | major | Gateway 业务副作用和命令结果记录不是跨实例同一事务，故障重试可能先产生副作用再收敛 | P3-23 统一命令执行、结果记录和幂等协调 |
| R4 | major | PostgreSQL Repository 尚未覆盖完整运行时协议，真实 RLS、锁竞争和回滚没有实测 | 提供真实 PostgreSQL 后运行 P3-21 集成入口并扩展回归 |
| R5 | minor | Agent 条件测试存在跳过，真实 ConPTY/Service 能力尚未由本轮测试证明 | 安装目标依赖并执行 Windows 实机矩阵 |

## 8. 下一轮建议

下一轮任务建议登记为 `P3-23`，按以下顺序推进：

1. 统一 Gateway 业务命令副作用、结果持久化和幂等键的事务协调接口。
2. 设计并实现 Artifact Gateway 命令边界，沿用项目能力、资源归属和 Agent 身份校验。
3. 为任务/Run/Handoff/Review/Artifact 归纳统一的命令执行结果协议和错误恢复规则。
4. 增加跨实例竞争、业务失败、ACK 丢失和重复投递的纯后端契约测试。
5. 准备阶段 3 开发版退出评审材料，明确开发版完成项与生产延期项。

阶段 4 再处理 Handoff 拒绝/返工/汇总、HTTP 主体认证、统一风险规则和数据驱动审核中心。

## 9. 复现和迁移注意事项

- 默认 API 运行时仍使用 SQLite；不要因为 PostgreSQL Repository 方法存在就切换生产默认运行时。
- PostgreSQL 目标环境必须应用 `apps/api/migrations/001_initial.sql` 至 `009_device_token_rotation.sql`，并执行阶段 2/3 条件集成测试。
- 测试 Agent 时使用 C 题工作区的虚拟环境；不要把存在权限异常的 `apps/api/vendor` 放在 `PYTHONPATH` 前面覆盖虚拟环境依赖。
- 真实基础设施测试需要先配置 `STAGE2_POSTGRES_DSN`、`STAGE2_MINIO_ENDPOINT` 及生产依赖。
- 重新运行 Gateway 测试时应保留 SQLite 默认 Store 的幂等状态隔离，避免复用上一次测试数据库。
- 任何后续修改都必须同步更新 `docs/PROJECT_EXECUTION_PLAN.md`、`docs/IMPLEMENTATION_STATUS.md`，并新增对应交接文档或明确记录在本轮交接中。

## 10. 交接结论

P3-22 的 Gateway 接力、Handoff 接收和 Agent Review 开发版协议已经具备可运行实现，SQLite/PostgreSQL 的核心模型和幂等语义也已有对应契约覆盖。当前可以将工作交给 P3-23，继续处理 Gateway 结果与业务副作用的统一协调；不能将本轮结果标记为生产级多实例 Gateway、正式身份认证或完整审核闭环。
