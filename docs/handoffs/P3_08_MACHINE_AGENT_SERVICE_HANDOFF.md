# P3-08 Machine Agent Service 交接文档

> 日期：2026-09-13
>
> 状态：开发版完成，生产退出条件未满足
>
> 对应计划：`docs/PROJECT_EXECUTION_PLAN.md` 第 10.11 节

## 1. 本轮目标

把 P3-03 中的单次 `gateway-run` 客户端提升为机器级 Agent 服务原语，完成：

- Gateway 连接失败后的自动重连和指数退避。
- 独立心跳调度，并沿用本地持久化出站队列。
- 本地紧急停止和 fail-closed 行为。
- 直接子进程的启动、输出采集、超时、取消和结果记录。
- 服务重启时对遗留进程记录进行 `ABANDONED` 标记。
- `agentd` 的服务启动、紧急停止和解除停止入口。

本轮不包含 Windows Service 安装、用户桌面会话控制、系统密钥环、进程树沙箱和真实基础设施验收。

## 2. 已完成内容

### 2.1 机器级服务

实现文件：`apps/agent/machine_service.py`

主要类型：

```text
MachineServiceConfig
MachineAgentService
LocalProcessSupervisor
ProcessSpec
ProcessResult
```

`MachineAgentService.run_forever()` 的行为：

1. 启动前检查本地 `service_control` 紧急停止标志。
2. 将本地恢复库中上一次实例的 `STARTING/RUNNING` 进程记录标记为 `ABANDONED`。
3. 解析设备 Token，启动心跳调度和一次 `DurableGatewayClient.run_once()` 会话。
4. Gateway 正常断开、连接异常或 Token 解析失败时进入重连等待。
5. 按配置的 `reconnect_base_seconds` 和 `reconnect_max_seconds` 做指数退避。
6. 服务停止、外部紧急停止或任务取消时停止连接监督并终止当前受管进程。

心跳通过 `client.queue_event("agent.heartbeat", ...)` 写入本地 Gateway 队列，实际发送、ACK 和补传仍由
`DurableGatewayClient` 负责。这样心跳与其他出站事件共享序号、幂等和恢复机制。

### 2.2 本地安全控制

`apps/agent/local_state.py` 新增 `service_control` 表和以下接口：

```text
set_emergency_stop(reason)
clear_emergency_stop()
emergency_stop_state()
is_emergency_stopped()
```

停止标志由本地 SQLite 共享，因此另一个本地控制进程可以在服务连接仍然存在或服务出现异常时设置停止状态。
服务检测到停止标志后会取消 Gateway 连接任务并停止受管进程；服务重新启动时仍保持停止状态，直到显式清除。

### 2.3 进程监督

`LocalProcessSupervisor` 使用 `asyncio.create_subprocess_exec()`，不经过 shell。每个进程绑定 `run_id`，本地记录包含：

- process ID、Run ID、命令、工作目录和 PID。
- `RUNNING`、`SUCCEEDED`、`FAILED`、`TIMED_OUT`、`CANCELLED`、`ABANDONED` 状态。
- stdout、stderr、退出码和开始/结束时间。
- 对应 `run_states` 中的当前状态和结果摘要。

启动失败也会创建完整的 `FAILED` 进程记录和 Run 失败状态。输出采集有字节上限，避免无限制占用内存；停止时先终止，
超时后再强制结束。

### 2.4 CLI 入口

实现文件：`apps/agent/agentd.py`

```text
service-run
service-emergency-stop
service-clear-emergency-stop
```

开发版启动示例：

```powershell
$python = "C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
& $python apps/agent/agentd.py service-run `
  --uri "ws://localhost:8000/ws/agents/device-001?session_id=session-001&connection_id=connection-001" `
  --device-token "<development-device-token>" `
  --state-path "$HOME\.math-agent-platform\agentd.db" `
  --device-id device-001 `
  --agent-id agent-001 `
  --session-id session-001 `
  --connection-id connection-001 `
  --agent-version 0.1.0 `
  --capabilities task.claim task.progress run.create
```

从另一个本地终端紧急停止：

```powershell
& $python apps/agent/agentd.py service-emergency-stop `
  --state-path "$HOME\.math-agent-platform\agentd.db" `
  --reason "operator_stop"
```

解除停止：

```powershell
& $python apps/agent/agentd.py service-clear-emergency-stop `
  --state-path "$HOME\.math-agent-platform\agentd.db"
```

命令行设备 Token 仅用于开发验证，不能作为生产凭据存储方案。

## 3. 验证证据

新增测试：`apps/agent/test_machine_service.py`

覆盖范围：

- 指数退避递增和上限。
- 同步、异步 Token Provider。
- Gateway 连接失败后的自动重连。
- 心跳按间隔写入本地队列。
- 通过共享 SQLite 外部设置紧急停止并保持 fail-closed。
- 清除停止后可以重新启动服务。
- 子进程 stdout、stderr 和退出码。
- 非零退出、启动失败、超时和显式取消。
- `stop_all()` 批量终止。
- 服务重启时旧活动进程变为 `ABANDONED`。

本轮验证结果：

```text
apps/api unittest: 67 passed, 2 skipped
  skipped: 真实 PostgreSQL/MinIO 服务未提供的集成测试
apps/agent unittest: 17 passed
compileall apps/api apps/agent packages: passed
应用导入: 68 routes / 51 OpenAPI paths
JSON: domain schemas and packages/agent_protocol/gateway.schema.json parse passed
```

## 4. 当前协议和实现边界

已具备的开发版能力：

- 设备 Token Gateway 握手和连接身份绑定。
- 本地出站事件持久化、ACK、补传和幂等恢复。
- Gateway 任务/Run 命令及命令结果恢复。
- Machine Agent Service 连接监督、心跳和本地紧急停止。
- 直接子进程的基本生命周期和 Run 状态记录。

仍未完成的能力：

- Windows Credential Manager 或其他系统密钥环。
- Windows Service 安装、升级、后台生命周期和 Session 0 处理。
- User Session Worker、Named Pipe/本机 RPC、ConPTY、Office/浏览器/桌面控制。
- 工作区路径白名单、可执行文件白名单、进程树终止和操作系统级网络隔离。
- Docker/Podman/Windows Sandbox、资源配额和系统调用级文件/网络审计。
- 真实 TLS、反向代理长连接、跨进程恢复和多实例 Gateway 验收。
- PostgreSQL 生产运行时、业务副作用与命令结果持久化的同事务保证。
- Artifact、Handoff、Approval 和终端控制 Gateway 命令。

因此，P3-08 只能标记为开发版 `DONE`、生产状态 `PARTIAL`，不能据此宣称 CONN-02、CONN-03、CONN-04、
CONN-06 已完成生产验收。

## 5. 下一步

建议按以下顺序继续：

1. P3-09：定义 Runner/Adapter 基础契约，先接入一个普通 Python Runner。
2. P3-09：加入工作区目录、可执行文件、环境变量和网络策略的本地检查。
3. 真实 PostgreSQL/MinIO 环境启动后，执行迁移、RLS、连接池、设备撤销和跨实例序号并发测试。
4. 设计设备公钥 challenge/signature 持有证明和系统凭据存储，不再把 Token 放在命令行参数中。
5. 再实现 Windows Service、User Session Worker 和 Named Pipe IPC；桌面自动化必须继续独立授权。
6. 将 Gateway 业务副作用与 `GatewayCommandResult` 写入放入同一事务/幂等协调边界。

## 6. 交接检查清单

- [x] P3-08 代码入口和 CLI 已存在。
- [x] P3-08 本地契约测试已通过。
- [x] 启动失败、超时、取消和旧进程恢复均有明确状态。
- [x] 共享 SQLite 紧急停止可以由独立进程设置和清除。
- [x] `PROJECT_EXECUTION_PLAN.md` 已更新到 2.4。
- [x] `IMPLEMENTATION_STATUS.md` 已更新。
- [x] `AGENT_DEVICE_CONNECTION_DESIGN.md` 已更新到 0.5。
- [ ] 真实 PostgreSQL/MinIO 集成验收。
- [ ] 生产凭据存储和 Windows Service。
- [ ] User Session Worker、本机 IPC 和安全策略执行器。
- [ ] Runner/Adapter 及工作区隔离。
