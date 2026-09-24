# P3-16 Windows Service/Session 0 交接文档

> 完成日期：2026-09-13
>
> 状态：纯后端开发版适配切片完成，真实服务验收未完成

## 1. 本轮目标

把 P3-14 的传输无关生命周期、P3-11 的 Named Pipe、P3-12 的 Session Worker Runtime 和 P3-15 的
ConPTY 后端接到 Windows Service/Session 0 的真实宿主边界。服务身份和交互用户身份必须保持分离。

## 2. 已完成

实现文件：`apps/agent/windows_service_adapter.py`

### 2.1 服务控制

- `ServiceInstallSpec`：服务名、显示名、命令、启动类型、服务账户、依赖和失败恢复策略。
- `FailureRecoveryPolicy`：多次失败重启延迟和 reset period。
- `CtypesServiceControlBackend`：使用 `OpenSCManagerW`、`CreateServiceW`、
  `ChangeServiceConfigW/2W`、`StartServiceW`、`ControlService`、`QueryServiceStatusEx` 和
  `DeleteService`，不通过 shell 解析 `sc.exe` 输出。
- `WindowsServiceInstaller`：安装失败因已存在时进入显式更新路径，并重新应用失败恢复策略。
- `agentd.py service-install`：显式安装或更新入口。
- `agentd.py service-control start|stop|status|uninstall`：显式服务控制入口。

### 2.2 Session 0 与用户 Worker

- `WindowsSessionProcessBackend` 使用 WTS 枚举会话、查询用户名和 `WTSQueryUserToken`。
- 使用 `CreateProcessAsUserW` 在目标用户 Session 启动 Worker，不把 LocalSystem/服务账户当成交互用户。
- 用户环境块、Worker 命令、工作目录和 Pipe 名称在启动请求中明确传递。
- Worker 启动、终止和存活检查均通过 Win32 API；服务不直接执行桌面自动化。
- WTS disconnected 状态在内部表示为 locked，避免把断开连接误记为全新的登录。

### 2.3 生命周期编排

- `SessionWorkerCoordinator` 以 `MachineServiceLifecycle` 为唯一状态策略来源。
- `reconcile()` 发现新会话、启动 Worker、回收注销会话并触发故障恢复。
- `poll_worker_exits()` 将 Worker 进程退出转换为 `worker.exited`，生命周期进入 DEGRADED，后续巡检触发显式恢复。
- `session_event_from_wts()` 映射登录、注销、锁屏和解锁事件。
- `CtypesServiceDispatcherBackend` 提供 `StartServiceCtrlDispatcherW`、停止/关机和
  `SERVICE_CONTROL_SESSIONCHANGE` 回调边界。
- `WindowsServiceHost` 启动后台巡检线程，持续处理会话变化和 Worker 退出；停止时先停止巡检，再回收 Worker。
- `agentd.py service-host` 将真实 WTS/用户 Token 后端、按会话绑定的 Worker 命令和 Dispatcher 宿主串起来；
  Worker 命令支持解释器加脚本的完整启动前缀。
- `WorkerLaunchAudit` 保留 Worker PID、用户 Session、Windows Session ID、SID、Pipe、命令和工作目录。
- 注销生命周期转移保留注销前的用户 Session ID，避免清理后审计主体丢失。

## 3. 测试结果

专项文件：`apps/agent/test_windows_service_adapter.py`

```text
P3-16 adapter tests: 13 passed
Agent full suite: 94 passed, 1 skipped
API full suite: 71 passed, 2 skipped
compileall: passed
```

测试使用 Fake SCM、Fake Session Process Backend 和 Fake Dispatcher，不安装或启动开发机上的系统服务。
覆盖服务安装/更新、失败恢复配置校验、WTS 事件映射、Worker 启动绑定、Worker 退出恢复、注销回收、
服务停止、审计 Session 身份保留和 Dispatcher 委托。

## 4. 尚未完成

- 尚未以管理员权限在真实 Windows SCM 安装、升级、回滚、启动和卸载服务。
- 尚未验证服务账户权限、SeAssignPrimaryToken/SeIncreaseQuota 等权限和真实 `CreateProcessAsUserW`。
- Worker 启动后已增加 Named Pipe readiness/`session.hello` 握手：服务端带 `readiness_probe=true` 的
  `session.hello` 经过 OS 对端身份、机器服务 Peer ID、Session 0、Worker ID 和用户 Session 校验后才返回
  READY；当前仍未在真实服务环境验证。
- 已保留 Worker 进程句柄作为优先身份锚点；尚未完成真实服务环境下的句柄生命周期、进程树回收和 PID 重用实测。
- 尚未实现进程树终止、资源限制、网络隔离、文件访问系统审计和凭据密钥环。
- 尚未完成多账户、跨 Session、锁屏、注销、睡眠唤醒、服务崩溃恢复和 Worker 重启实机矩阵。
- `StartServiceCtrlDispatcherW` 的 SCM 宿主已封装并由 `service-host` 接入，但尚未完成安装包和升级器。
- 桌面控制仍然关闭，必须由用户 Session Worker、独立审批和独立审计提供。

## 5. 下一步

1. 增加真实 Pipe readiness/`session.hello` handshake，再把 `worker.ready` 从“进程创建成功”提升为“Worker 已认证可服务”。
2. 在隔离测试机执行管理员权限 SCM 安装、升级、失败恢复和卸载演练。
3. 保留并验证 Worker process handle，补进程树回收和 PID 重用防护。
4. 运行单账户、多账户、锁屏/解锁、注销、睡眠/唤醒和服务重启矩阵。
5. 与 Gateway 长时间断线、Event Outbox、Artifact 上传和 ConPTY Run 组合验收。
6. 完成后再进行阶段 3 生产退出评审。

## 6. 交接入口

- 主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 设备连接设计：`docs/AGENT_DEVICE_CONNECTION_DESIGN.md`
- 当前状态：`docs/IMPLEMENTATION_STATUS.md`
- 生命周期策略：`apps/agent/service_lifecycle.py`
- Service/Session 0 适配器：`apps/agent/windows_service_adapter.py`
- 适配器测试：`apps/agent/test_windows_service_adapter.py`
- User Session Runtime：`apps/agent/session_runtime.py`
