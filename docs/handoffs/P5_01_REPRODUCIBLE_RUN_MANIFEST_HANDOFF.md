# P5-01 可复现运行 Manifest 交接文档

> 日期：2026-09-14
>
> 状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务：建立阶段 5 的可复现执行上下文和运行 Manifest 基础契约

## 1. 本轮目标

将 Runner 的执行请求从“命令和工作区”扩展为可追溯的运行上下文，使每次本地执行可以记录代码、环境、依赖、参数、随机种子、工具版本、数据访问策略和输入观察状态，为后续容器隔离与信息边界审计提供稳定输入。

## 2. 已完成

### 2.1 Runner 请求上下文

修改文件：`apps/agent/runner.py`

`RunnerRequest` 新增并校验：

- `source_commit`
- `environment_image_digest`
- `dependency_lock`
- `parameters`
- `random_seed`
- `model_provider`
- `model_name`
- `tool_versions`
- `data_access_policy`
- `observed_input_files`

字典字段会在请求创建时复制为独立对象，避免调用方在执行期间修改 Manifest 所依赖的元数据。

### 2.2 Session 协议传递

修改文件：

- `packages/agent_protocol/__init__.py`
- `packages/agent_protocol/session.schema.json`
- `apps/agent/session_runtime.py`

`SessionRunStartPayload` 已增加相同的复现上下文，Session Worker 在构造 `RunnerRequest` 时完整传递这些字段。这样本地会话启动和最终运行记录可以使用同一份执行描述。

### 2.3 Manifest 内容

修改文件：`apps/agent/result_uploader.py`

`RunManifestBuilder` 从 `1.0` 升级为 `1.1`，新增：

- `reproducibility`：代码提交、环境镜像、依赖锁、参数、随机种子、模型、工具版本、网络策略和数据访问策略。
- `input_access`：声明输入、观察输入和未声明输入。
- `information_boundary`：信息边界允许状态、违规项和观察状态。
- `process.stdout_bytes` 与 `process.stderr_bytes`。
- `manifest_sha256`：对不包含自身哈希字段的规范化 JSON 计算 SHA-256。

当执行请求有声明输入但没有 `observed_input_files` 时，Manifest 会记录 `input_observation_not_captured`，并将边界状态判为不允许，避免把未完成的输入审计误标为通过。

## 3. 已验证内容

已完成目标 Python 模块编译和共享 Session Schema 结构同步检查。现有 Web 构建和本地 Runner/Agent 历史回归保持有效。

本轮未执行完整测试套件，未启动容器运行时，也未进行真实文件访问跟踪或同环境重跑；这些按当前项目节奏留到后续统一验收。

## 4. 未完成事项

- Docker/Podman 容器 Runner 尚未接入 `LocalRunner`。
- CPU、内存、进程数、网络和系统级超时限制尚未由操作系统或容器强制执行。
- 当前 `observed_input_files` 仍由调用方提供，尚未接入系统调用级文件访问审计。
- Manifest 还没有独立平台 Artifact Schema 和服务端正式版本登记接口。
- 相同输入、环境镜像和锁文件的真实重跑尚未完成。
- 失败运行、取消运行与正式成果物门禁的阶段 5 组合验收尚未完成。

## 5. 下一步

1. 实现 `ContainerRunner` 和 Docker/Podman 运行时探测，默认网络关闭、工作区只读挂载并保留显式输出挂载。
2. 将资源限制和容器运行结果接入 Run Manifest，记录运行时版本和镜像 digest。
3. 增加文件访问观察器；观察器不可用时保持 fail-closed，并将原因写入信息边界审计。
4. 建立可复现重跑入口，比较输出 Artifact 哈希和 Manifest 哈希。
5. 将机器审计结果接入 Review/Gate，并继续保持阶段 5 的 `PASS_WITH_ASSUMPTIONS` 口径直到真实隔离验收完成。

## 6. 交接输入

- Runner 契约：`apps/agent/runner.py`
- Session 协议：`packages/agent_protocol/__init__.py`
- Session Schema：`packages/agent_protocol/session.schema.json`
- Manifest 和输出上传：`apps/agent/result_uploader.py`
- Session Worker 编排：`apps/agent/session_runtime.py`
- 阶段主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 实施状态：`docs/IMPLEMENTATION_STATUS.md`
- 设备连接补充设计：`docs/AGENT_DEVICE_CONNECTION_DESIGN.md`

## 7. 结论

P5-01 已完成开发版的可复现上下文和 Manifest 基础能力，后续执行者可以直接在现有 `RunnerRequest` 和 `RunManifestBuilder` 上接入容器隔离与系统级审计，不需要重新设计字段。容器、资源、文件访问和真实重跑仍是未完成项。
