# P5-02 容器 Runner 交接文档

> 日期：2026-09-14
>
> 状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务：建立 Docker/Podman 容器执行边界开发版后端

## 1. 本轮目标

在已有宿主机 `LocalRunner` 之外，建立一个可审计、可复用、默认收紧的 Docker/Podman 执行后端。容器运行时不可用或策略不满足时必须明确失败，不能静默退回宿主机执行。

## 2. 已完成

新增文件：`apps/agent/container_runner.py`

### 2.1 运行时解析

`ContainerRuntime` 支持：

- Docker 和 Podman 两种运行时名称。
- 自动使用 `PATH` 解析可执行文件。
- 传入绝对路径时检查文件存在。
- 运行时不可用时返回 `container_runtime_not_available:<runtime>`。

### 2.2 容器策略

`ContainerPolicy` 当前默认约束：

- 镜像必须使用 `@sha256:<64 hex>` digest。
- 只接受 `HEADLESS` 执行模式和 `PIPE` 后端。
- 只接受 `deny-by-default` 网络策略。
- 工作区必须位于允许根目录内。
- 输入和输出路径必须位于工作区内。
- 默认资源限制为 1 CPU、2 GiB 内存和 256 个 PIDs，可按策略显式调整。

### 2.3 命令构造和监督

`ContainerCommandBuilder` 以 argv tuple 构造运行命令，使用：

```text
run --rm --init --network none --read-only
--cap-drop=ALL --security-opt=no-new-privileges
--cpus ... --memory ... --pids-limit ...
--label project/run --mount workspace --workdir /workspace
<image> <request.command...>
```

命令不会经过 Shell。`ContainerRunner` 将容器 CLI 作为受监督的本地进程，保留 Run、Task、Agent、Device 和 Workspace 归属字段，并复用停止、超时、输出限制和本地恢复状态。

## 3. 已验证内容

- `apps/agent/container_runner.py` 编译通过。
- 使用本机 Python 可执行文件作为 fake runtime 完成命令构造冒烟，确认网络关闭、资源参数、镜像和工作区挂载均进入 argv。
- 真实 Docker/Podman 未安装，因此未启动容器。

## 4. 未完成事项

- `ContainerRunner` 尚未接入 `SessionWorkerRuntime` 的默认路由，也没有新增平台 API 选择容器执行后端。
- 当前工作区 bind mount 默认可写；只读工作区策略已可配置，但输出目录的独立可写挂载尚未实现。
- 尚未验证 Windows Docker Desktop、Linux Docker、Podman rootless 和跨平台路径挂载。
- 尚未验证容器进程树终止、资源限制、网络阻断、镜像拉取策略和运行超时。
- 容器调用信息还没有自动并入 `RunManifestBuilder` 的 `container` 字段。
- 尚未完成容器输出 Artifact 上传和正式 Review/Gate 组合验收。

## 5. 下一步

1. 在有 Docker 或 Podman 的环境中执行固定 digest 镜像的真实冒烟。
2. 将 `ContainerInvocation.as_manifest()` 接入 Run Manifest。
3. 为输入只读挂载和显式输出目录建立更细粒度的挂载规划。
4. 将容器选择加入 Session Run 启动协议，但保留默认宿主机/容器策略和人工审批边界。
5. 记录容器运行时版本、镜像 digest、资源限制、退出码和输出哈希，并接入阶段 5 的信息边界 Review/Gate。

## 6. 交接输入

- 容器后端：`apps/agent/container_runner.py`
- Runner 请求：`apps/agent/runner.py`
- 本地监督器：`apps/agent/machine_service.py`
- Session 编排：`apps/agent/session_runtime.py`
- 可复现 Manifest：`apps/agent/result_uploader.py`
- 阶段主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 实施状态：`docs/IMPLEMENTATION_STATUS.md`
- P5-01 交接：`docs/handoffs/P5_01_REPRODUCIBLE_RUN_MANIFEST_HANDOFF.md`

## 7. 结论

P5-02 已完成容器执行边界的开发版代码基础。下一位执行者可以从真实 runtime 冒烟、Manifest 接入和 Session 路由开始；在这些事项完成前，阶段 5 仍不能标记为生产完成。
