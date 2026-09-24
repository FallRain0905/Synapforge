# 阶段 3 开发版退出评审

> 评审编号：`STAGE3-DEV-EXIT-2026-09-14`
>
> 评审结论：`PASS_WITH_ASSUMPTIONS`
>
> 适用范围：Agent Gateway、本地 Agent、Runner/Adapter、User Session Worker 和 Artifact 元数据通道
>
> 重要说明：本文件只批准“开发版主线进入阶段 4”，不批准生产环境切换。

## 1. 评审目标

阶段 3 的目标是形成一条可持续扩展的 Agent 执行与协作纵向链路：设备可以建立 Gateway 连接，Agent 可以领取和执行任务，运行结果可以通过本地恢复队列返回，结构化 Handoff/Review 可以推动协作，Artifact 可以登记并上传，且关键身份和权限边界由服务端掌握。

## 2. 开发版完成矩阵

| 能力 | 当前结论 | 主要证据 |
| --- | --- | --- |
| 设备配对、challenge/signature 和 Token | 开发完成 | `apps/api/app/device_identity.py`、设备 Repository、P3-18/P3-20 交接 |
| 项目能力 Token 和设备/Agent 绑定 | 开发完成 | `apps/api/app/store.py`、`apps/api/app/gateway.py` |
| Gateway 握手、心跳、序号、重复帧和补传 | 开发完成 | `apps/api/app/gateway.py`、Gateway 协议 Schema |
| 任务、租约、进度、结果和 Run 命令 | 开发完成 | Gateway 命令处理和本地恢复客户端 |
| Gateway 命令结果和 ACK 丢失恢复 | 开发完成但非生产级 | `gateway_command_results`、P3-07 结果恢复链路 |
| Handoff 创建/接收和 Agent Review | 开发完成 | P3-22 Gateway/Repository 实现和交接文档 |
| Artifact 元数据 Gateway 命令 | 开发完成 | `agent.artifact.create`，P3-23 |
| Artifact 文件上传 | 开发完成开发版边界 | Agent HTTP 单文件/Multipart，Gateway 不承载二进制内容 |
| Machine Service、User Session Worker 和本机 IPC | 开发完成开发版 | Machine Service、Named Pipe、Session Runtime 和生命周期适配器 |
| PIPE/ConPTY Runner 和外部 CLI Adapter | 开发完成开发版 | Runner、ConPTY、Codex Adapter；真实 CLI 任务矩阵延期 |
| 本地紧急停止、恢复日志和输出哈希 | 开发完成开发版 | Agent 本地状态库、Runner 和 ResultUploader |

## 3. 本轮 P3-23 交付

- 新增 `agent.artifact.create` Gateway 命令。
- Gateway 强制校验 `artifact.write` 项目能力、当前 Agent 身份、任务/Run/输入 Artifact 的项目归属。
- Artifact 的内容上传继续使用 Agent HTTP/Multipart 通道，避免控制协议承载大文件。
- `GatewayCommandResult` 增加 `request_hash`，计算时排除项目能力 Token。
- 同一连接范围内同一幂等键复用不同命令内容时返回 `gateway_command_request_mismatch`。
- PostgreSQL 增加 `010_gateway_command_request_hash.sql`；SQLite 使用兼容字段。
- 已生成 P3-23 交接文档：`docs/handoffs/P3_23_STAGE3_DEV_EXIT_HANDOFF.md`。

## 4. 阶段 3 开发版退出条件

以下条件满足，可以将阶段 4 作为主线继续开发：

1. 核心 Gateway 消息类型、设备身份、项目能力和本地 Agent 执行边界已经固化。
2. 任务、Run、Handoff、Review 和 Artifact 元数据可以通过统一 Repository/Gateway 边界流转。
3. Agent 不能通过 Gateway 伪造成员身份批准结果。
4. 断线恢复、命令结果和请求指纹有开发版协议定义。
5. 每轮工作有实施状态、执行计划和交接文档可追溯。

## 5. 生产退出阻塞项

以下项目不影响阶段 4 开发启动，但阻止“阶段 3 生产退出”声明：

- 真实 PostgreSQL 连接池、RLS、事务回滚、并发幂等和跨实例序号验证。
- Gateway 业务副作用、执行中抢占/恢复和结果持久化的统一事务协调。
- 真实 MinIO/S3、NATS JetStream、TLS、反向代理和长连接恢复。
- Windows Service/Session 0 安装、跨账户 Worker、锁屏/注销/睡眠唤醒实机矩阵。
- pywinpty/ConPTY 依赖在目标 Agent 环境中的安装、升级和运行验证。
- 真实 Codex 任务、多版本兼容、审批回传和 Claude Code 语义适配。
- OS 级网络、资源、进程树和文件访问隔离。
- 正式 OIDC、设备 Credential Manager 目标身份恢复和 HTTP Handoff 主体认证。
- Artifact 上传配额、恶意文件扫描、病毒检测和生产运维策略。

## 6. 测试延期记录

遵循当前项目的快速推进决策，本轮不重复运行完整测试矩阵。已执行目标模块 `py_compile` 和迁移/Schema 文件解析；最近一次完整基线为：

```text
API：82 passed，3 个真实 PostgreSQL/MinIO 条件测试 skipped
Agent：114 passed，9 个 ConPTY/平台条件测试 skipped
```

P3-23 新增命令的完整 Gateway 回归、真实 PostgreSQL 迁移测试、跨实例竞争测试、Artifact 上传端到端测试和 Web 构建均延期，下一次统一测试时必须补回。

本轮已执行最小运行时冒烟：Artifact 元数据创建、同消息回放和同幂等键改载荷阻断均通过。

## 7. 主线切换决定

阶段 3 开发版：**完成**。

阶段 3 生产版：**未完成，保持延期验收**。

下一主线：进入阶段 4，优先实现 Handoff 拒绝/返工/汇总、统一风险规则、审核中心后端契约和数据驱动门禁。

## 8. 关联文档

- `docs/PROJECT_EXECUTION_PLAN.md`
- `docs/IMPLEMENTATION_STATUS.md`
- `docs/AGENT_DEVICE_CONNECTION_DESIGN.md`
- `docs/handoffs/P3_22_GATEWAY_HANDOFF_REVIEW_HANDOFF.md`
- `docs/handoffs/P3_23_STAGE3_DEV_EXIT_HANDOFF.md`
