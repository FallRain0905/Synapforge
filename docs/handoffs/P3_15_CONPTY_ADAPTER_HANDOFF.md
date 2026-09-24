# P3-15 ConPTY Adapter 交接文档

> 完成日期：2026-09-13
>
> 状态：开发版切片完成，生产验收未完成
>
> 上一阶段：P3-14 会话生命周期与终端控制契约
>
> 下一阶段：P3-16 Windows Service/Session 0 Adapter

## 1. 本轮目标

将真实 Windows ConPTY 接入已经存在的 User Session Worker Runtime，使交互式 CLI 能够使用 PTY 语义，
同时不改变默认 PIPE Runner，也不把普通管道冒充为 ConPTY。

## 2. 已完成

### 2.1 依赖和能力探测

- 新增 `apps/agent/requirements.txt`：`pywinpty>=2.0,<3; sys_platform == "win32"`。
- `conpty_capability()` 检查 Windows、最低 Build 17763 和 pywinpty 是否可导入，并返回诊断信息。
- 新增 `agentd.py conpty-capability` 命令。
- 当前开发机结果：Windows 11 build 26200、pywinpty 2.0.15、ConPTY available=true。

### 2.2 Runner 和 Adapter

- `ProcessSpec`、`RunnerRequest`、`SessionRunStartPayload`、Run Manifest 和本地 Run 状态保存
  `terminal_backend`、列数和行数。
- `terminal_backend` 只有 `PIPE` 和 `CONPTY`；ConPTY 不允许 HEADLESS。
- `ConPtyAdapter` 只接受显式 `terminal_backend=CONPTY`。
- `HybridProcessSupervisor` 按 Run 的后端精确路由；ConPTY 不可用时 fail-closed，不静默退回 PIPE。

### 2.3 ConPTY 生命周期

`ConPtyProcessSupervisor` 已实现：

- pywinpty ConPTY 创建，初始 terminal dimensions；
- stdin 写入和合并 stdout 输出；ConPTY 不提供独立 stderr 流；
- `setwinsize` resize；
- 正常退出、非零退出、取消、超时和本地 Run 状态记录；
- 共享输出上限，超过上限后继续排空但不继续保存；
- transport EOF 和子进程生命周期的 fail-closed 处理；
- pywinpty blocking 模式，修复非阻塞包装器在子进程仍存活时误报 EOF；
- 进程创建时使用锁保护 `PYWINPTY_BLOCK` 临时环境变量；
- 不继承宿主环境时传递显式环境和最小 `PATH`；
- watcher 取消时主动终止子进程，关闭 pywinpty forwarding transport 和 reader thread；
- Hybrid supervisor 的 PIPE/CONPTY 恢复记录处理。

## 3. 测试结果

专项文件：`apps/agent/test_conpty_runner.py`

```text
ConPTY tests: 8 passed
Agent full suite: 91 passed, 1 skipped
API full suite: 71 passed, 2 skipped
compileall: passed
packages/*.schema.json parse: passed
ResourceWarning-as-error: passed
```

专项测试覆盖真实 ConPTY 输入输出、resize、取消、错误后端拒绝、Hybrid 路由、Adapter 后端拒绝、
Runtime resize 事件和孤儿记录恢复。API 的 2 个跳过项仍是外部服务依赖。

## 4. 已知边界

- 只在当前 Windows 开发机完成单账户真实 ConPTY 验证，尚未完成多账户、跨 Session、锁屏、注销、睡眠/唤醒矩阵。
- 尚未接入真实 Codex/Claude CLI 的 prompt、tool、approval、取消和版本兼容语义。
- 尚未实现进程树终止、OS 级网络/资源隔离、工作区挂载和系统调用级文件访问审计。
- 还没有 Windows Service/Session 0 的真实宿主；当前 `session-worker-run` 仍需人工启动 User Session Worker。
- ConPTY 的 resize、输入、输出和进程退出已通过开发版测试，但不等于桌面控制能力可用。
- 本地设备 Token、生产 PostgreSQL/MinIO/NATS 和跨服务正式结果门禁仍未完成。

## 5. 下一步

1. 完成 P3-16 Windows Service SCM 安装、升级、恢复和 Dispatcher 适配。
2. 从 Session 0 使用目标用户 Token 启动 User Session Worker，并增加 Pipe readiness 握手。
3. 实测服务账户 ACL、跨账户/跨 Session、锁屏/注销、睡眠/唤醒和 Worker 重启。
4. 增加真实 CLI Adapter，记录 prompt/tool/approval 事件并接入现有事件上传。
5. 完成进程句柄、进程树、资源和网络隔离后，再评估阶段 3 生产退出。

## 6. 交接入口

- 主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 设备连接设计：`docs/AGENT_DEVICE_CONNECTION_DESIGN.md`
- 当前状态：`docs/IMPLEMENTATION_STATUS.md`
- ConPTY 实现：`apps/agent/conpty_runner.py`
- Runtime：`apps/agent/session_runtime.py`
- Runner：`apps/agent/runner.py`
- 下一阶段实现：`apps/agent/windows_service_adapter.py`
