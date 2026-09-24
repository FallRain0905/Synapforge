# 阶段 3 P3-05 项目能力 Token 运行时强制接入交接

> 交接状态：PASS_WITH_ASSUMPTIONS
>
> 日期：2026-09-13
>
> 对应任务：P3-05 将项目范围能力 Token 接入 Agent 任务与 Run 的 HTTP 写路径

## 1. 本轮目标

P3-01/P3-04 已经建立设备 Token 和项目授权 Token 的开发版存储，但旧的 Agent 任务接口仍可
只凭 `agent_id` 操作。这个边界会造成设备级身份和项目级权限混淆：一台已登记设备可能被
错误地当作所有项目的 Agent 使用。

本轮目标是把项目能力 Token 真正接入机器写操作，至少保证：

1. 没有项目能力 Token 的机器写请求不能执行。
2. Token 必须绑定当前项目和当前 Agent。
3. Token 必须包含当前操作所需的能力。
4. 租约 Token 不能借助另一个项目的能力 Token 被延长。
5. 本地 Agent CLI 能够使用新的请求约定。

## 2. 已完成内容

### 2.1 HTTP 鉴权约定

机器写请求使用以下请求头：

```text
X-Project-Capability-Token: prj_...
```

该 Token 不替代 WebSocket 连接使用的设备 Token，也不替代人类成员的 Session。两者职责
不同：

| 凭证 | 证明内容 | 使用位置 |
| --- | --- | --- |
| 人类 Session | 成员身份和项目管理权限 | Web/API 人类操作 |
| 设备 Token | 已登记设备身份和设备生命周期 | Gateway 握手 |
| 项目能力 Token | 某设备代表的 Agent 在某项目的具体能力 | Agent 任务/Run 写操作 |

### 2.2 能力和操作绑定

| 操作 | 所需能力 | 项目来源 |
| --- | --- | --- |
| 指定任务领取 | `task.claim` | Task.project_id |
| 按项目领取下一任务 | `task.claim` | 请求体 `project_id` |
| 租约心跳 | `task.lease` | 请求体 `project_id` |
| 任务进度 | `task.progress` | Task.project_id |
| 任务结果 | `task.result` | Task.project_id |
| 创建 Run | `run.create` | URL `project_id` |
| 完成 Run | `run.complete` | Run.project_id |

无效、撤销或过期 Token 返回 HTTP 401。Token 能力不足、Agent 不匹配或租约项目不一致
返回 HTTP 403。缺少项目 ID 的按项目领取请求返回 HTTP 400。

### 2.3 请求模型和存储边界

- `TaskLeaseHeartbeat` 增加必填 `project_id`。
- `PlatformRepository.heartbeat_lease()` 和 SQLite `Store.heartbeat_lease()` 接受可选项目
  范围，并在存在时校验 lease 的真实项目 ID。
- `DeviceProjectGrantCreate` 的默认能力包含任务完整生命周期、Artifact 基础操作以及
  Run 创建/完成。
- 项目能力 Token 的验证统一经过 `resolve_device_project_token()`，不会返回 Token 哈希。

### 2.4 CLI

`apps/agent/agentd.py` 已支持：

- `claim --project-id ... --project-token ...`
- `lease-heartbeat --project-id ... --project-token ...`
- `progress --project-token ...`
- `complete --project-token ...`

CLI 会通过 `X-Project-Capability-Token` 传递 Token，不会把 Token 写入本地恢复状态库。

## 3. 修改文件

- `apps/api/app/contracts.py`
  - 增加租约心跳项目字段。
  - 扩展项目授权默认能力集合。
- `apps/api/app/repository.py`
  - 扩展租约心跳 Repository 签名。
- `apps/api/app/store.py`
  - 增加 lease 项目归属校验。
- `apps/api/app/main.py`
  - 增加 `_require_agent_capability()`。
  - 收紧任务领取、租约心跳、进度、结果和 Run 写路径。
- `apps/agent/agentd.py`
  - 增加请求头传递和 `lease-heartbeat` 命令。
- `apps/api/test_agent_capability.py`
  - 新增项目能力 Token 纯后端契约测试。
- `README.md`
  - 更新 Agent CLI 示例和凭证说明。
- `docs/PROJECT_EXECUTION_PLAN.md`
  - 增加 P3-05 切片、验收和下一轮队列。
- `docs/IMPLEMENTATION_STATUS.md`
  - 更新当前阶段、验证计数和已知边界。

## 4. 测试与验证

### 4.1 新增测试

`apps/api/test_agent_capability.py` 共覆盖：

- 缺少项目 Token 时不能领取任务。
- 合法 Token 可以完成领取、租约心跳、进度和 Run 创建/完成。
- Token 不能跨项目使用。
- Token 不能冒充另一个 Agent。
- 只有 `task.claim` 的 Token 不能创建 Run。
- 一个项目的 lease 不能用另一个项目的 Token 延长。

### 4.2 当前验证结果

```text
后端 unittest：61 passed，2 skipped
跳过：真实 PostgreSQL/RLS/连接池和 MinIO/S3 集成测试
```

已完成的开发版验证不等于真实 PostgreSQL、TLS、跨实例 Gateway 或生产身份验收。

## 5. 尚未完成

1. Gateway 的 `agent.event` 仍是占位；任务领取、租约控制和 Run 命令尚未在 WebSocket
   消息层实现同一套项目能力 Token 上下文。
2. PostgreSQL Repository 尚未覆盖完整任务租约和 Run 编排，当前 API 默认仍使用 SQLite。
3. 设备公钥 challenge/signature 持有证明尚未实现。
4. 设备 Token 和项目 Token 尚未接入 Windows Credential Manager 或其他系统密钥环。
5. HTTP Token 目前是项目能力边界，但还没有短期轮换、撤销事件推送和审计查询界面。
6. 真实 PostgreSQL migration、RLS、连接池、并发序号和设备撤销竞态尚未运行。

## 6. 下一轮建议

按依赖顺序建议继续：

1. 为 Gateway 设计并实现 `task.claim`、`task.lease.heartbeat`、`run.create` 和
   `run.complete` 消息契约；消息 payload 必须包含项目 ID，服务端从连接上下文和项目
   Token 同时核验 Agent 身份与能力。
2. 抽取 `packages/agent-protocol` 或等价跨端协议包，让 `apps/agent` 不直接依赖
   `apps/api` 的 Python 模型。
3. 实现 Machine Agent Service 的自动重连、心跳调度、断线退避、进程监督和本地紧急停止。
4. 在 PostgreSQL 可用后运行迁移 001 至 005，并补任务租约、RLS、跨实例序号和撤销竞态测试。
5. 再把项目能力 Token 纳入真实 Gateway E2E 和第二台设备验收。

## 7. 复现命令

```powershell
$env:PYTHONPATH = "$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest apps.api.test_agent_capability -v
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s apps/api -p 'test_*.py' -v

$env:PYTHONPATH = "$(Resolve-Path apps/agent);$(Resolve-Path apps/api);$(Resolve-Path apps/api/vendor)"
C:\Users\19855\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe -m unittest discover -s apps/agent -p 'test_*.py' -v
```

调用机器写 API 时，除请求体要求外，还必须带：

```text
X-Project-Capability-Token: <the project token returned by POST /api/projects/{project_id}/device-grants>
```

不要把静态测试、开发版 SQLite 或现有 Gateway Fake WebSocket 结果解释为生产安全验收。
