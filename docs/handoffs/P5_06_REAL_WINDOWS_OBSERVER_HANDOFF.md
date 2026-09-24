# P5-06-REAL Windows ETW 观察器交接文档

> 日期：2026-09-15
>
> 状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务：在既有文件/网络观察契约上完成 Windows 原生 ETW 开发版适配，并把真实进程范围的观察结果接入 Runner、Session 和 Run Manifest

## 1. 本轮结论

本轮完成了第一条 Windows 原生观察开发链路：

```text
LocalRunner 启动观察器
  -> logman 创建 Kernel File/Network ETW 会话
  -> 启动真实 Runner 子进程
  -> 绑定真实 OS PID 并轮询进程树
  -> 进程结束
  -> logman 停止 ETW 会话
  -> tracerpt 转换 CSV
  -> 进程范围过滤与路径/网络归一化
  -> ProcessResult
  -> SessionWorkerRuntime
  -> RunManifestBuilder 信息边界与网络审计
```

这是一条可测试的开发版适配，不是生产级系统调用审计或网络阻断实现。当前不能把它作为“所有文件访问均已被可靠观测”的证明。

## 2. 已完成

### 2.1 Windows ETW 采集器

新增：`apps/agent/windows_etw_observer.py`

- 使用 Windows inbox `logman` 分别创建独立命名的 Kernel File 和 Kernel Network 单 Provider ETW trace session，避免当前 Windows 版本重复 `-p` 或 `-pf` provider 配置的兼容性问题。
- 默认启用：
  - `Microsoft-Windows-Kernel-File`，默认 keyword `0x3F0`。
  - `Microsoft-Windows-Kernel-Network`，默认 keyword `0x30`。
- 使用 `tracerpt` 将 ETL 转为 Unicode CSV。
- 工具调用使用参数数组和 `shell=False`，不通过 Shell 拼接命令。
- 每个采集会话使用独立 ETL/CSV 路径；结束后尝试删除 logman session。
- `logman`/`tracerpt` 缺失、创建失败、停止失败、转换失败或 CSV 缺失时返回 fail-closed 结果。

### 2.2 进程范围

- `LocalProcessSupervisor.operating_system_pid()` 暴露真实子进程 PID。
- `LocalRunner` 在创建进程前启动 ETW，在创建后绑定真实 PID。
- `WindowsProcessTree` 使用 `Toolhelp32Snapshot` 建立父子关系。
- 绑定后后台轮询进程树并累积运行中出现的子进程 PID，解析 CSV 时只接收绑定集合中的事件。
- 没有绑定真实 PID 时，采集器不会返回未限定范围的事件。
- `ProcessResult` 增加观察文件、网络连接、捕获状态和失败原因字段。

### 2.3 传递链

- `LocalRunner.wait()` 停止观察器，并将规范化的文件/网络观察写入 `ProcessResult`。
- `SessionWorkerRuntime` 将观察结果回填到有效 `RunnerRequest`。
- `RunManifestBuilder` 保存：
  - `access_observation.status`
  - `access_observation.reason`
  - `input_access.observed`
  - `network_access`
  - `information_boundary`
- 如果任务要求 `observation_mode=system` 或 `network_observation_mode=system`，采集器失败不会回退为声明式通过；既有审计规则仍会产生阻断 finding。
- `agentd session-worker-run --access-observer windows-etw` 可显式启用 Windows 观察器；`--etw-output-directory` 可保留 ETL/CSV 诊断。默认值为 `none`。

## 3. 事件解析约定

当前解析器针对 `tracerpt -of CSV` 的常见字段名做兼容匹配：

```text
ProcessId / Process ID / PID
Provider / Provider Name
FileName / File Name / Path
RemoteAddress / Destination Address / Host
RemotePort / Destination Port / Port
Protocol / Transport
```

文件事件根据 Provider、`FileIo`、`read`、`write`、`rename` 和 `delete` 等文本判定读写方向；网络事件根据 Kernel Network Provider、Network、TCP 或 UDP 文本识别。所有归一化事件强制使用 `observation_source="system"`。

Windows NT device path 会尝试通过 `QueryDosDeviceW` 映射到盘符；无法映射时保留原始绝对路径，交由信息边界审计判定。

## 4. 验证

已执行：

```text
apps/agent/test_windows_etw_observer.py -> 5 passed
apps/agent unittest discover          -> 139 passed, 1 skipped
target modules py_compile              -> passed
logman query providers                 -> Kernel File/Network providers queryable
logman/tracerpt capability probe       -> Windows inbox tools available
real short-run smoke                    -> command shape accepted; current identity returned Access is denied
```

专项测试使用 Fake `logman/tracerpt` 命令执行器，不创建真实 ETW 会话。真实短跑已验证单 Provider `logman` 命令形态，但当前执行身份返回 `Access is denied`，尚未取得真实 ETL 样本，因此不能把上述结果解释为实机 ETW 字段验收通过。

## 5. 未完成与已知风险

### 5.1 真实 OS 验收

- 尚未在管理员权限下创建并停止真实 ETW session。
- 尚未收集当前 Windows build 的真实 `tracerpt` CSV 样本并冻结字段映射。
- 尚未验证普通用户、管理员、服务账户和 UAC 条件下的权限行为。
- 尚未验证 ETW 会话异常退出、机器休眠/唤醒、Runner 崩溃和服务重启后的残留清理。

### 5.2 观察完整性

- 进程树采用轮询，无法保证捕获已经创建并退出的极短命子进程；也不能替代内核级进程生命周期事件绑定。
- Kernel File/Network ETW 的事件字段和 `tracerpt` 输出形式可能随 Windows build、Provider keyword 和事件模板变化。
- 当前 CSV 解析是开发版启发式解析，不是对所有 ETW event schema 的完整解码。
- Python 解释器、依赖库、临时目录等运行时文件可能出现在系统读取事件中；任务策略还需要后续的运行时依赖 allowlist/分类，避免把合法运行时依赖与竞赛输入混为一类。
- 当前没有 minifilter 级别保证，也没有对丢事件、缓冲区溢出或 ETW session 资源压力建立指标。

### 5.3 安全与容器

- 观察器只负责记录，不负责阻断网络或文件访问。
- 尚未支持容器 PID namespace、容器内文件路径到宿主工作区的可靠映射。
- 尚未采集容器内网络连接，也未完成 Docker/Podman `--network none` 的组合验收。
- Linux eBPF/LSM、auditd 和跨平台进程树适配尚未开始。
- 尚未把观察失败自动写入平台 Review、Evidence 和 Gate。

## 6. 下一轮建议

按以下顺序执行：

1. 在管理员 PowerShell 中完成一次短命 Python Runner 的真实 ETW 采集，保留脱敏 ETL/CSV 样本和权限诊断。
2. 根据真实样本修正 Provider、EventName、PID、文件路径、远端地址和端口字段解析，并增加固定样本回归。
3. 增加观察器状态 Manifest：session name、provider 配置、ETL/CSV 哈希、启动/停止诊断和丢事件状态，但不得把敏感文件内容写入 Manifest。
4. 将运行时依赖根目录、工作区输入和工作区外访问分类，明确哪些路径可被策略声明为工具运行时依赖。
5. 增加真实进程树压力测试和失败清理测试，再接入容器观察。
6. 完成网络阻断后，再把“观察到外发”与“外发被阻断”分成两个独立审计结论。

## 7. 交接输入与关键文件

- 观察契约：`apps/agent/information_boundary.py`
- JSONL 桥接：`apps/agent/access_observer.py`
- Windows ETW：`apps/agent/windows_etw_observer.py`
- Runner：`apps/agent/runner.py`
- 进程监督：`apps/agent/machine_service.py`
- Session 传递：`apps/agent/session_runtime.py`
- Manifest：`apps/agent/result_uploader.py`
- 专项测试：`apps/agent/test_windows_etw_observer.py`
- 主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 实施状态：`docs/IMPLEMENTATION_STATUS.md`
- 前置交接：`docs/handoffs/P5_06_ACCESS_OBSERVATION_HANDOFF.md`

## 8. 接收方行动

接收本交接后，下一位开发者应先完成真实管理员权限样本验收，再修改解析规则。若真实 ETW 启动失败，不应改成声明式通过；应保留失败诊断并进入 `P5-06-REAL-ADMIN` 阶段。只有在样本字段、权限、异常清理和跨版本矩阵具备证据后，才能把 Windows 观察器从开发版推进到生产候选版。
