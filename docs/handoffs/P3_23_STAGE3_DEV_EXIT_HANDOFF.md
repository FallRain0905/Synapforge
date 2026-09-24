# P3-23 阶段 3 开发版收尾交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务编号：`P3-23`
>
> 完成日期：2026-09-14
>
> 下一主线：阶段 4 `P4-01`

## 1. 本轮目标

快速完成阶段 3 的开发版收尾，补齐 Artifact Gateway 元数据边界，增强 Gateway 命令结果的幂等识别，并形成阶段 3 开发版退出评审材料。真实数据库、多实例并发、Windows Service 和完整测试按项目决策延期。

## 2. 已完成

### 2.1 Artifact Gateway 命令

新增：

```text
agent.artifact.create
```

命令行为：

- 使用 `artifact.write` 项目能力 Token。
- 创建主体固定为当前 Gateway 连接的 Agent，忽略或拒绝载荷中的伪造 `created_by`/`created_by_kind`。
- 校验可选 `task_id`、`run_id` 和 `input_artifact_ids` 的项目归属。
- `run_id` 如果存在，还要求 Run 属于当前 Agent。
- 禁止通过创建命令直接生成 `APPROVED` Artifact，批准仍由 Review/Gate 完成。
- 只传输 Artifact 元数据；实际文件继续使用 Agent HTTP 单文件或 Multipart 上传。

### 2.2 Gateway 请求指纹

`GatewayCommandResult` 新增可选 `request_hash` 字段：

- 指纹包含命令类型和业务载荷。
- 项目能力 Token 从指纹中排除，不把秘密写入持久化结果。
- 同一连接范围内以相同幂等键提交不同载荷时，返回 `gateway_command_request_mismatch`。
- 原有无指纹历史结果仍可读取，保持开发版向后兼容。

### 2.3 数据库与契约

- PostgreSQL 新增 `apps/api/migrations/010_gateway_command_request_hash.sql`。
- SQLite 初始化和兼容列逻辑增加 `gateway_command_results.request_hash`。
- `packages/agent_protocol/gateway.schema.json`、`packages/contracts/domain.schema.json` 和 `packages/contracts/domain.json` 已同步。
- 主计划、实施状态、迁移说明、阶段 2 清单和设备连接设计已同步更新。

## 3. 修改文件

- `packages/agent_protocol/__init__.py`
- `packages/agent_protocol/gateway.schema.json`
- `packages/contracts/domain.schema.json`
- `packages/contracts/domain.json`
- `apps/api/app/gateway.py`
- `apps/api/app/store.py`
- `apps/api/app/postgres_repository.py`
- `apps/api/migrations/010_gateway_command_request_hash.sql`
- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/POSTGRESQL_MIGRATIONS.md`
- `docs/POSTGRES_REPOSITORY.md`
- `docs/STAGE2_EXIT_CHECKLIST.md`
- `docs/AGENT_DEVICE_CONNECTION_DESIGN.md`
- `docs/STAGE3_EXIT_REVIEW.md`

## 4. 本轮验证

已执行：

```text
py_compile：通过
迁移 SQL 文件存在性与 Schema JSON 解析：通过
Artifact 元数据创建/重复回放/同幂等键改载荷阻断冒烟：通过
```

最近一次完整基线测试记录：

```text
API：82 passed，3 个真实 PostgreSQL/MinIO 条件测试 skipped
Agent：114 passed，9 个 ConPTY/平台条件测试 skipped
```

本轮没有重新执行完整 API/Agent 测试、真实 PostgreSQL/MinIO 测试或 Web 构建，以优先完成阶段主线收尾。

## 5. 未完成事项

- 新增 `agent.artifact.create` 尚未执行完整 Gateway 回归和真实 Agent 端到端上传验证。
- Gateway 请求指纹尚未进行真实 PostgreSQL 并发竞争、唯一约束和事务回滚验证。
- 当前只解决“已完成结果重放的请求内容识别”，没有实现跨实例“执行中”命令租约、抢占、超时恢复或分布式锁。
- Gateway 业务副作用与 `gateway_command_results` 仍不是跨实例同一事务；故障窗口可能需要后续恢复审计。
- SQLite Handoff 创建/接收事件仍与业务提交分离，沿用 P3-22 风险记录。
- HTTP Handoff 接收仍从请求体读取 `receiver`/`actor_kind`，正式认证后需要从会话或设备主体推导。
- Artifact 文件上传、审核、Evidence 图谱和正式下游门禁仍需阶段 4/5 完整联调。
- 真实 PostgreSQL/MinIO、NATS、OIDC、TLS、Windows Service/Session 0、ConPTY 依赖、真实 CLI 和 OS 隔离均未完成生产验收。

## 6. 阶段切换结论

阶段 3 开发版可以关闭，阶段 4 可以作为主线继续推进。阶段 3 生产版不得关闭，所有未完成项应保留在生产验收 backlog，并在后续基础设施轮次统一测试。

下一轮建议：

1. 开始 `P4-01`，实现 Handoff 拒绝、返工、汇总和统一风险规则。
2. 将审核中心后端契约与 Task/Artifact/Gate/Evidence 关系接通。
3. 后续基础设施轮次统一补测 P3-23 的 Artifact Gateway、请求指纹、真实 PostgreSQL 和跨实例场景。

## 7. 复现与迁移注意

- 默认运行时仍是 SQLite；生产 PostgreSQL 必须先应用 `001` 至 `010` 全部迁移。
- 迁移 `010` 只增加可选请求指纹列，不修改历史结果状态，不需要回填秘密数据。
- 文件内容不要通过 Gateway JSON 命令发送，继续使用现有 Agent Artifact HTTP/Multipart 接口。
- 测试时不要把权限异常的 `apps/api/vendor` 放到 `PYTHONPATH` 前面。
- 下一轮开始必须先阅读 `docs/IMPLEMENTATION_STATUS.md`，结束时更新状态、主计划和新的交接文档。
