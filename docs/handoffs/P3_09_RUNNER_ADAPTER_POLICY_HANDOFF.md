# P3-09 Runner/Adapter 与工作区策略交接文档

> 日期：2026-09-13
>
> 状态：开发版完成，生产级执行隔离未完成
>
> 对应计划：`docs/PROJECT_EXECUTION_PLAN.md` 第 10.12 节

## 1. 本轮目标

将 P3-08 的机器级进程监督提升为可审计的 Runner/Adapter 调用边界：

- 定义统一 Runner 请求和 Adapter 能力描述。
- 保证每个进程绑定项目、任务、Run、Agent、Device 和 Workspace。
- 约束执行模式与用户会话/桌面权限。
- 对工作区、输入输出路径、可执行文件和环境变量做 fail-closed 校验。
- 在没有 OS 级网络隔离时拒绝非默认网络策略。
- 接入第一个普通 Python Adapter。
- 将 `execution_profile` 正式保存进 Run 领域对象和数据库。

## 2. 已完成内容

### 2.1 本地 Runner 契约

实现文件：`apps/agent/runner.py`

主要类型：

```text
RunnerRequest
ExecutionProfile
AdapterDescriptor
RunnerAdapter
CommandAdapter
PythonAdapter
WorkspacePolicy
LocalRunner
NetworkEnforcer
RunnerPolicyError
```

`RunnerRequest` 必须包含：

```text
project_id
task_id
run_id
agent_id
device_id
workspace_id
workspace_path
command
```

`task_id` 可以为空，表示没有关联具体任务；其余归属字段不能为空。

### 2.2 执行模式

基础契约支持：

```text
HEADLESS
USER_SESSION
INTERACTIVE_DESKTOP
```

当前规则：

- `HEADLESS` 不能要求用户会话，也不能开启桌面控制。
- `USER_SESSION` 和 `INTERACTIVE_DESKTOP` 必须声明需要用户会话。
- 桌面控制只能出现在 `INTERACTIVE_DESKTOP`。
- Adapter 必须声明支持的操作系统和执行模式；不匹配的请求在创建进程前拒绝。

当前只有 `HEADLESS` 直接执行；另外两种模式等待 User Session Worker 和本机 IPC。

### 2.3 工作区与网络策略

`WorkspacePolicy`：

- 将配置根目录、工作区、输入路径和输出路径规范化为绝对路径。
- 拒绝不在允许根目录内的工作区。
- 拒绝不在当前工作区内的输入/输出路径。
- 对命令第一个 argv 做精确可执行文件白名单匹配。
- 对环境变量键做白名单匹配。
- `ProcessSpec.inherit_environment` 默认是 `False`，Adapter 不能重新启用宿主机环境继承。
- `allow-listed` 和 `unrestricted` 网络需要显式 `NetworkEnforcer`；没有 Enforcer 时直接拒绝。

注意：当前 `NetworkEnforcer` 是接口，不代表已经实现防火墙、容器或系统沙箱隔离。

### 2.4 进程归属和持久化

`ProcessSpec` 已要求并传递以下上下文：

```text
project_id
task_id
agent_id
device_id
workspace_id
workspace_path
```

`apps/agent/local_state.py` 的 `managed_processes` 表新增对应字段，并保留对旧 SQLite 数据库的
增量列兼容。启动、失败、运行中和结束状态都写入本地恢复库；`run_states` 的 payload 同时记录
这些上下文，便于故障复盘。

### 2.5 Python Adapter

`PythonAdapter` 使用配置的 Python 解释器和请求中的 Python argv，复用 `CommandAdapter` 和
`LocalProcessSupervisor`。它不经过 shell，并继承 P3-08 的 stdout、stderr、退出码、超时、取消
和 Run 状态记录行为。

它是普通 Python 执行器的第一版契约，不是完整平台 Runner 流程：当前还没有自动下载输入成果物、
创建正式 Run Manifest、发现输出文件、上传 Artifact 或写入平台审计事件。

## 3. Run execution_profile 领域补齐

以下位置已加入 `execution_profile`：

- `apps/api/app/contracts.py` 的 `RunCreate` 和 `Run`。
- SQLite `runs` 表和 Store 的创建/读取/Bundle 恢复逻辑。
- PostgreSQL `001_initial.sql` 和增量迁移 `007_run_execution_profile.sql`。
- PostgreSQL Repository 的 Run 映射兼容逻辑。
- `packages/contracts/domain.schema.json` 的 `Run` 定义。

网络策略同时保留在旧的 `network_policy` 字段，以兼容既有 API；`RunCreate` 会检查它和
`execution_profile.network_policy` 一致，默认 deny-by-default 时允许由执行配置补齐。

旧数据库行和旧幂等响应缺少配置时使用兼容的 HEADLESS 配置，并根据旧 `network_policy` 修正网络字段。

## 4. 验证证据

新增测试：`apps/agent/test_runner.py`

覆盖：

- Python Adapter 执行和完整进程上下文记录。
- 工作区、输入路径和输出路径越权。
- 可执行文件和环境变量白名单。
- HEADLESS/USER_SESSION/INTERACTIVE_DESKTOP 约束。
- 网络策略无 Enforcer 时 fail-closed，有 Enforcer 时正确调用。
- Adapter 伪造 Run、项目、工作区归属。
- Adapter 重新启用宿主环境继承。

本轮验证结果：

```text
apps/agent unittest: 26 passed
apps/api unittest: 68 passed, 2 skipped
  skipped: 真实 PostgreSQL/MinIO 服务未提供的集成测试
compileall apps/api apps/agent packages: passed
API 导入: 68 routes / 51 OpenAPI paths
领域和 Gateway JSON Schema: 解析通过
```

## 5. 未完成和不能宣称的能力

- `USER_SESSION`、`INTERACTIVE_DESKTOP` 尚未接入 User Session Worker、ConPTY、Named Pipe 或本机 RPC。
- 网络策略只完成应用层拒绝/委托契约，没有 OS 级网络隔离。
- 没有进程树终止、工作区挂载隔离、系统资源限制和文件系统访问审计。
- Python Adapter 仍要求调用方提供完整 argv；没有平台 Run 命令到本地执行的完整编排。
- 没有自动输出文件发现、哈希、Artifact 上传和结果门禁绑定。
- 没有 Codex、Claude Code、MCP、Office 或 Browser Adapter。
- `execution_profile` 的 PostgreSQL 迁移和真实数据库兼容性尚未执行生产验收。

因此本轮是 P3-09 开发版完成，阶段 3 生产退出仍为 `PARTIAL`。

## 6. 下一步

1. P3-10：冻结 User Session Worker 与 Machine Service 之间的本机 IPC 契约。
2. 增加一个真正的 Python Runner 编排入口，将 Run、输入 Artifact、工作区、ProcessSpec 和结果上传串起来。
3. 实现输出文件发现、内容哈希、Artifact 绑定和失败结果门禁。
4. 在隔离环境中提供实际 NetworkEnforcer，并验证路径、进程树和资源限制。
5. 启动真实 PostgreSQL/MinIO，执行 `001` 至 `007` 迁移和跨实例验收。
6. 设备 Token 接入系统密钥环，再考虑 Windows Service 安装和用户会话 Worker。

## 7. 交接检查清单

- [x] Runner/Adapter 基础契约已建立。
- [x] PythonAdapter 已接入本地监督器。
- [x] 工作区、命令、环境和网络策略有 fail-closed 测试。
- [x] 进程上下文已持久化。
- [x] `execution_profile` 已进入 Run 模型、数据库迁移和领域 Schema。
- [x] 全量后端、本地 Agent 和编译检查已通过。
- [ ] User Session Worker 和本机 IPC。
- [ ] OS 级网络/进程/路径隔离。
- [ ] 完整 Run 编排、Artifact 上传和审计事件。
- [ ] 真实 PostgreSQL/MinIO 生产验收。
