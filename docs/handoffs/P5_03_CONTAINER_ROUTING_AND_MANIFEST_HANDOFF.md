# P5-03 容器路由、分层挂载与 Manifest 接入交接文档

> 日期：2026-09-14
>
> 状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务：将 Docker/Podman 开发版后端接入 Session Worker，建立输入只读、输出目录独立可写和运行清单记录契约

## 1. 本轮目标

在 P5-02 容器命令构造基础上，完成一条可测试的后端路由：调用方必须明确选择宿主机或容器执行；容器执行必须明确运行时和 digest 镜像；工作区不能因输出写入而整体变成可写；Run Manifest 必须能记录容器执行上下文且不泄露环境变量值。

## 2. 已完成

### 2.1 执行协议

修改：

- `apps/agent/runner.py`
- `packages/agent_protocol/__init__.py`
- `packages/agent_protocol/session.schema.json`

新增字段：

- `execution_backend`: `host` 或 `container`，默认 `host`。
- `container_runtime`: `docker` 或 `podman`。
- `container_image`: 容器完整镜像引用，容器请求必须提供。
- `execution_details`: 运行时生成的、可进入 Manifest 的执行细节。

协议门禁：

- 宿主机请求携带容器字段直接拒绝。
- 容器请求缺少运行时或镜像直接拒绝。
- 镜像默认必须带 `@sha256:<64 hex>` digest。
- 容器只接受 `HEADLESS`、`PIPE` 和 `deny-by-default` 网络策略。

### 2.2 Session Worker 路由

修改：

- `apps/agent/session_runtime.py`
- `apps/agent/session_worker.py`
- `apps/agent/agentd.py`

`SessionWorkerRuntime` 根据 `RunnerRequest.execution_backend` 选择：

- `host`：原有 `LocalRunner` 和 CLI/ConPTY 行为不变。
- `container`：从 `container_runners[runtime]` 取显式配置的 `ContainerRunner`；没有配置时返回失败，不回退到宿主机。

`agentd session-worker-run --container-runtime docker|podman` 可显式注册容器后端。该参数不存在时，Worker 不宣称容器能力；存在时额外允许 HEADLESS 请求。

### 2.3 挂载规划

修改：`apps/agent/container_runner.py`

`ContainerPolicy.plan_mounts()` 的结果为：

1. 工作区以 `/workspace:ro` 挂载。
2. 每个输出文件所在的工作区子目录以 `/workspace/<relative-parent>:rw` 挂载。
3. 工作区根目录直接输出被拒绝，避免用根目录 rw 挂载覆盖只读工作区。
4. 声明输入位于可写输出目录内时被拒绝，避免输入文件意外变成可写。
5. 多个嵌套输出目录会压缩为最短的父目录挂载，避免重复挂载。

启动前仅创建已经通过路径校验的输出父目录；不创建工作区外目录。

### 2.4 Manifest 脱敏

`ContainerInvocation.as_manifest()` 现在记录：

- 运行时名称、镜像、网络模式、只读策略和 CPU/内存/PID 限制。
- 工作负载命令和挂载目标/模式。
- 环境变量名列表。

原始 runtime argv 仍用于实际启动，但 Manifest 中的 `--env KEY=value` 会变成 `KEY=<redacted>`，同时保留 `environment_keys`。Session Runtime 将脱敏对象写入 `RunnerRequest.execution_details`，`RunManifestBuilder` 将其写入 `execution` 字段。

## 3. 验证结果

使用项目 bundled Python：

```text
apps/agent unittest discover -> 119 passed, 1 skipped
py_compile apps/agent/*.py and packages/agent_protocol/__init__.py -> passed
json.tool packages/agent_protocol/session.schema.json -> passed
json.tool packages/contracts/domain.schema.json -> passed
```

关键契约覆盖：

- 工作区只读、输出目录可写挂载参数生成。
- 工作区根目录输出拒绝。
- 输入与可写输出目录重叠拒绝。
- Manifest 环境变量值脱敏。
- Session 选择容器后端并把执行细节带入运行事件。
- 原有宿主机、ConPTY、Named Pipe、CLI 和结果上传回归。

## 4. 未完成和风险

- 当前没有 Docker 或 Podman，未执行真实容器启动；本轮 fake runtime 只验证 argv 和路由契约。
- 尚未验证 Windows Docker Desktop、Linux Docker、Podman rootless、SELinux/AppArmor 和 Windows 路径挂载差异。
- `LocalProcessSupervisor` 当前监督的是容器 CLI 进程，尚未完成容器内进程树强制回收和真实资源限制验收。
- 文件访问仍由调用方声明/观察，尚未接入系统调用级文件读取审计；未来真实数据混入问题仍需 P5-04 处理。
- 输出目录挂载按输出文件的父目录授予 rw；尚未实现更细粒度的每文件 staging/copy-back 机制。
- 容器输出可上传为 Artifact，但尚未完成真实容器运行、Manifest、Artifact、Run 完成事件和 Review/Gate 的组合验收。
- 平台 API 的 `Run` 结构目前没有独立的执行后端字段，容器信息主要由 Run Manifest 记录。

## 5. 下一步

1. 进入 P5-04：建立系统级文件访问观察器，区分声明输入、实际读取、未来数据和未声明路径，观察器不可用时 fail-closed。
2. 增加真实 Docker/Podman 固定 digest 镜像冒烟，并记录运行时版本、退出码、资源限制和输出哈希。
3. 建立可复现重跑入口，比较输出 Artifact 哈希和 Manifest 哈希。
4. 将机器审计结果接入 Review/Gate，并在真实 PostgreSQL/MinIO 环境完成组合验收。

## 6. 交接输入

- 容器后端：`apps/agent/container_runner.py`
- Runner 契约：`apps/agent/runner.py`
- Session 编排：`apps/agent/session_runtime.py`
- Session Schema：`packages/agent_protocol/session.schema.json`
- Manifest 和输出上传：`apps/agent/result_uploader.py`
- 阶段主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 实施状态：`docs/IMPLEMENTATION_STATUS.md`
- P5-02 交接：`docs/handoffs/P5_02_CONTAINER_RUNNER_HANDOFF.md`

## 7. 结论

P5-03 已完成容器执行选择、Session 路由、分层挂载和 Manifest 脱敏的开发版链路。开发版后端契约和回归测试通过，但真实容器、系统级信息边界审计、可复现重跑和生产基础设施仍未完成；阶段 5 继续保持 `PASS_WITH_ASSUMPTIONS`。
