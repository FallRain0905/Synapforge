# P3-17 外部 CLI Adapter 交接文档

> 完成日期：2026-09-13
>
> 状态：Codex JSONL Adapter 纯后端开发版切片完成；真实模型任务、生产隔离和 Claude 语义适配未完成

## 1. 本轮目标

按照 `PROJECT_EXECUTION_PLAN.md` 和 `AGENT_DEVICE_CONNECTION_DESIGN.md`，把外部 CLI 接入从普通命令
启动提升为有版本、能力、协议和安全边界的 Adapter。首个真实目标是本机可用的 Codex CLI；Claude Code
在稳定机器可读协议冻结前只报告不可用状态。

## 2. 已完成

### 2.1 Codex Adapter

实现文件：`apps/agent/cli_adapters.py`

- `probe_cli()` 使用 `subprocess.run(..., shell=False)` 探测版本，不通过 Shell 解析命令。
- `CliCapabilityStatus` 区分 `AVAILABLE`、`NOT_INSTALLED`、`UNSUPPORTED` 和 `ERROR`，保留版本、原因、
  能力和检查时间。
- `CodexCompatibilityRule` 当前冻结并实测 `codex-cli 0.153.x`；本机探测结果为 `0.153.4`。
- `CodexAdapter.build_command()` 构造官方 `codex exec --json` 调用，支持模型、输出 Schema 和临时会话参数。
- `CodexAdapter` 在 Runner 执行前通过 `ensure_available()` 要求兼容性探测通过。
- 拒绝自动批准、危险沙箱、工作区逃逸参数（`--cd`、`--add-dir`、`--image`）和未受控配置覆盖；输出
  Schema 路径必须位于工作区内。

### 2.2 JSONL 事件

`CodexJsonlEventParser` 支持跨 stdout chunk 的增量解析，并将已登记事件转换为统一事件：

| Codex 事件 | 平台事件 |
| --- | --- |
| `thread.started`、`turn.started` | `run.started` |
| `turn.completed` | `run.completed` |
| `turn.failed`、`error` | `run.failed` |
| `item.*` 的命令/MCP/搜索项 | `tool.started` / `tool.completed` |
| `item.*` 的文件变更项 | `file.changed` |
| `item.*` 的 Agent 消息项 | `agent.message` |
| 审批状态或 `approval.requested` | `approval.requested` |

未知顶层事件、未知 Item 类型、非法 JSONL 和缺失结构均抛出 `CliProtocolError`。Session Runtime 保留
原始 `process.stdout`，同时写入归一化事件；解析失败时 Run 按失败处理，不把未知输出当成成功。

### 2.3 Claude Code 状态

`ClaudeCodeAdapter` 只用于能力诊断：

- 可执行文件不存在：`NOT_INSTALLED`。
- 可执行文件存在但语义 Adapter 未实现：`UNSUPPORTED`。
- `build_process_spec()` 始终拒绝启动，错误为 `semantic_protocol_not_implemented`。

这保证平台不会把“探测到 Claude 可执行文件”误报成“平台已经支持 Claude Code”。

### 2.4 入口和协议

- `agentd cli-capability --adapter codex|claude` 输出 JSON 能力诊断。
- `SessionIpcEvent` 新增 `run.started` 和 `agent.message`，Python 模型与
  `packages/agent_protocol/session.schema.json` 已同步。
- `LocalRunner.new_event_parser()` 为每个 Run 创建独立解析器，避免不同 Run 共用解析缓冲区。
- `SessionWorkerRuntime` 接入 Parser，处理跨 chunk 输出、最终 flush、终态去重和协议失败。

## 3. 测试结果

专项测试：

```text
apps/agent/test_cli_adapters.py：11 passed
Session Runtime JSONL 纵向测试：passed
```

全量回归：

```text
apps/agent：105 passed, 1 skipped（P3-17 切片当时的基线；P3-18 后当前全量为 106）
apps/api：71 passed, 2 skipped
compileall apps/agent packages：passed
packages/agent_protocol/session.schema.json：JSON parse passed
Codex capability probe：codex-cli 0.153.4 / AVAILABLE
Claude capability probe：NOT_INSTALLED（当前机器）
```

测试没有执行真实 Codex 模型调用，也没有修改 Windows Service、SCM 或设备凭据。

## 4. 尚未完成

- 未在真实认证和项目任务环境执行 Codex 完整模型调用、工具调用、文件输出和取消流程。
- 当前兼容性矩阵只有已验证的 `0.153.x`，尚未覆盖其他 Codex 版本、操作系统和升级/降级策略。
- `approval.requested` 已能被识别和审计，但尚未接入云端 ApprovalRequest 的响应回传与继续执行。
- Claude Code 的机器可读事件、Prompt、工具、审批和结果语义 Adapter 尚未实现。
- Runner 仍缺进程树终止、OS 级 CPU/内存/网络隔离、系统调用文件审计和生产凭据密钥环。
- Windows Service/Session 0 仍缺管理员安装、真实 Worker readiness、跨账户/锁屏/注销/睡眠唤醒实机矩阵。
- PostgreSQL、MinIO、NATS 和 Gateway 跨服务事务仍未完成生产验收。

## 5. 下一步

1. 在隔离测试工作区，用当前 `codex-cli 0.153.4` 执行一条最小真实 `exec --json` 任务，记录完整 JSONL
   事件、退出码、输出 Artifact 和 Run Manifest；不把该次试运行直接标记为生产验收。
2. 将 Codex 版本/OS/Adapter 兼容性矩阵变成可执行测试，并明确版本不兼容时的阻断诊断。
3. 建立审批事件到平台审批对象和人工响应的双向协议，未经批准不继续危险操作。
4. 并行完成 PostgreSQL/MinIO/RLS、设备 challenge/signature、系统密钥环和真实 Windows Service 验收。
5. 在上述边界完成后，再进行阶段 3 生产退出评审，并决定是否启动 Claude Code 语义 Adapter。

## 6. 交接入口

- 主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 当前状态：`docs/IMPLEMENTATION_STATUS.md`
- 连接设计：`docs/AGENT_DEVICE_CONNECTION_DESIGN.md`
- Adapter：`apps/agent/cli_adapters.py`
- Runtime：`apps/agent/session_runtime.py`
- Runner：`apps/agent/runner.py`
- 协议模型：`packages/agent_protocol/__init__.py`
- 专项测试：`apps/agent/test_cli_adapters.py`、`apps/agent/test_session_runtime.py`
