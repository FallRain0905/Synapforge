# P3-13 Agent 事件、Artifact 与 Run 结果接入交接文档

> 日期：2026-09-13
>
> 状态：PASS_WITH_ASSUMPTIONS
>
> 对应计划：`docs/PROJECT_EXECUTION_PLAN.md` 第 10.16 节

## 1. 本轮目标

把 P3-12 产生的本地 Runtime 事件和进程结果接入平台控制平面，完成开发版的：

```text
本地 Runtime
  -> Durable Gateway outbox
  -> Gateway 身份/项目能力/Run 归属校验
  -> 平台 Event 与 Run 状态

工作区显式输出
  -> SHA-256/大小/MIME/相对路径
  -> 本地 pending_uploads
  -> Agent Artifact 元数据和内容/Multipart
  -> Run 完成结果引用
```

本轮没有实现 ConPTY、桌面控制、Windows Service、真实 PostgreSQL/MinIO 或 OS 级隔离。

## 2. 已完成

### 2.1 Gateway 事件契约

- `packages/agent_protocol.AgentEventPayload` 正式定义 `project_id`、项目能力令牌和
  `SessionIpcEvent`。
- `SessionIpcEvent` 增加项目归属字段，防止一个连接把本地事件伪装到其他项目。
- `packages/agent_protocol/gateway.schema.json` 和 `session.schema.json` 已同步。
- Gateway 对 `agent.event` 重新检查设备、Agent、项目令牌、事件项目和 Run 所属关系。
- 事件落库时只保存事件内容，不保存 `project_token`。
- Event 使用稳定的本地/Gateway 幂等键，重复消息不会重复产生平台 Event。

### 2.2 Run 结果

- `run.completed` 和 `run.failed` Runtime 事件可以完成同 Agent 的平台 Run。
- 完成载荷支持状态、退出码、stdout/stderr 摘要、输入文件清单和输出 Artifact ID。
- 输出 Artifact 必须在同项目、绑定同一 Run、由同一 Agent 创建，否则 Run 被标记为信息边界阻断。
- Gateway `agent.run.complete` 现在把入站幂等键传入 Run 完成路径；旧的直接开发调用仍有确定性
  兼容键。

### 2.3 Agent 输出上传

- `apps/agent/result_uploader.py` 新增：
  - `OutputDiscovery`：只扫描显式声明且位于工作区内的文件。
  - `RunManifestBuilder`：生成包含命令、执行配置、输入输出、退出结果和 stdout/stderr 摘要哈希
    的 `run_manifest`。
  - `AgentArtifactClient`：使用项目能力 Token 和 `X-Agent-Id` 调用 Agent 专用接口。
  - `ResultUploader`：写入本地 `pending_uploads`，支持失败重试和成功复用。
- 小于等于 8 MiB 的文件走单文件内容接口；更大的文件走 Multipart。
- Multipart 每片默认 8 MiB，初始化、分片、完成均使用确定性幂等键。
- 排队后文件内容发生变化时拒绝上传，避免 Artifact 哈希和实际内容不一致。
- 本地状态新增项目、任务、Run、MIME、大小、相对路径、Artifact 类型和错误字段。

### 2.4 Agent Artifact API

新增开发版路径：

```text
POST /api/agent/projects/{project_id}/artifacts
POST /api/agent/artifacts/{artifact_id}/content
POST /api/agent/artifacts/{artifact_id}/multipart
PUT  /api/agent/artifacts/{artifact_id}/multipart/{upload_id}/parts/{part_number}
POST /api/agent/artifacts/{artifact_id}/multipart/{upload_id}/complete
```

每个路径均要求项目能力 Token、`X-Agent-Id` 和请求幂等键。创建接口校验 Run 项目/Agent 归属；
内容接口再次校验 Artifact 创建 Agent。新 Artifact 初始为 `DRAFT`，仍需 Review 才能批准为下游输入。

### 2.5 Runtime 和 CLI

- `SessionWorkerRuntime` 支持 `on_run_complete` 回调：进程结束后先生成并上传输出，再生成带 Artifact
  引用的终态事件。
- `agentd session-worker-run` 增加开发版 `--url`、`--agent-id` 和 `--project-token`，配置后会
  使用结果上传器并将带项目绑定的事件写入 Gateway outbox。
- 未配置项目令牌时，CLI 不写入无法上传的 `agent.event` 载荷；当前仍可作为只运行本地 Runner 的
  开发入口。

## 3. 测试证据

```text
apps/api: 71 passed, 2 skipped
apps/agent: 63 passed, 1 skipped
python -m compileall -q apps/api apps/agent packages: passed
JSON Schema parse: passed
Gateway runtime event contract: passed
Agent Artifact capability/idempotency contract: passed
Output discovery/hash/retry contract: passed
```

真实 PostgreSQL/MinIO 测试仍因环境未提供而跳过；这不是本轮代码测试失败。

## 4. 未完成与风险

- 当前 Agent Artifact 路径是开发版 HTTP 接口，不是经过 OIDC、设备密钥环、TLS 反向代理和生产配额
  验收的正式上传服务。
- Gateway 事件先推进入站序号，再执行业务落库；若服务在序号记录后、事件落库前崩溃，仍需要后续
  事务化接收/重放协调来保证无损恢复。
- `ResultUploader` 生成的本地 Manifest 会放在工作区 `.math-agent-platform/run-manifests`，
  这是开发版约定，未来需要版本化工作区策略和清理策略。
- 当前输出发现只接受文件，不自动递归目录；调用方应传入具体输出文件路径。
- Gateway 完成事件会通过 `complete_run` 产生平台 `run.completed` 事件；运行时原始事件另有
  `agent.run.completed` 事件记录，正式 UI 需要明确区分二者。
- PostgreSQL Repository 尚未覆盖本轮新增 Agent Artifact 端点所需的完整事务协议。
- 没有完成 ConPTY、真实 CLI Prompt/Resize、Windows Service/Session 0、进程树终止、资源限制、
  文件系统访问审计和网络隔离。

## 5. 下一步

进入 P3-14：

1. 冻结 ConPTY/交互式 CLI 的 Windows 版本兼容、PTY resize、取消和资源回收契约。
2. 设计 Windows Service 到 User Session Worker 的启动、用户会话发现、锁屏/注销和恢复状态机。
3. 对桌面执行器设置人工审批、窗口范围、屏幕/输入审计和明确的 fail-closed 规则。
4. 将 Gateway 接收、事件入库、Run 完成和幂等记录收敛到生产数据库事务。
5. 并行完成真实 PostgreSQL/MinIO、设备 challenge/signature 和系统密钥环验收。

## 6. 复审启动入口

复审时优先阅读：

```text
docs/PROJECT_EXECUTION_PLAN.md
docs/AGENT_DEVICE_CONNECTION_DESIGN.md
apps/api/app/gateway.py
apps/api/app/main.py
apps/agent/session_runtime.py
apps/agent/result_uploader.py
apps/agent/gateway_client.py
apps/api/test_gateway.py
apps/api/test_agent_capability.py
apps/agent/test_result_uploader.py
```

复审结论：本轮开发版目标已完成，生产验收条件未满足，不能把本轮结果描述为完整 SaaS Agent
执行平台。
