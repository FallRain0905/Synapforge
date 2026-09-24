# P5-05 可复现重跑交接文档

> 日期：2026-09-15
>
> 状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务：从已通过信息边界审计的 Run Manifest 规划新的可复现 Run，并比较输出、稳定复现事实和信息边界

## 1. 本轮目标

在 P5-01 至 P5-04 的 Runner、Manifest、容器执行和信息边界契约之上，增加可复现重跑开发版入口。重跑必须使用新的 Run 身份，显式恢复源运行的代码、环境、参数、随机种子和数据访问策略，并且不能因为工作区位置变化而产生虚假的哈希差异。

## 2. 已完成

### 2.1 重跑计划

新增：`apps/agent/reproducible_rerun.py`

`ReproducibleReplayPlanner.plan()`：

- 只接受 `information_boundary.allowed == true` 的源 Manifest。
- 源 Manifest 必须包含与其内容一致的稳定复现哈希；内容被修改而哈希未同步时直接拒绝。
- 必须由调用方提供新的非空 `replay_run_id`。
- 恢复项目、任务、Agent、设备、工作区、执行模式、终端配置、命令和输入/输出声明。
- 恢复 `source_commit`、环境镜像 digest、dependency lock、参数、随机种子、模型提供方/模型名、工具版本和数据访问策略。
- 环境变量值不从 Manifest 还原；调用方提供的环境变量名称集合必须与源 Manifest 完全一致，否则拒绝重跑。
- 将输入、输出、观测文件、数据访问元数据和命令中位于源工作区内的绝对路径迁移到目标工作区。
- 源工作区之外的绝对路径不会被静默放行，会以 `ReplayPolicyError` 拒绝。
- 容器运行时、镜像和执行细节被保留；容器配置不因重跑规划而降级为宿主机执行。

### 2.2 稳定复现哈希

修改：`apps/agent/result_uploader.py`

`reproducibility_hash()` 现在：

- 排除 `run_id`、顶层工作区路径、进程动态信息、Manifest 自身哈希和稳定哈希字段。
- 排除容器执行细节中的机器运行时命令和工作区字段。
- 将工作区内的输入、输出、观测记录、边界记录、输出记录和工作负载命令中的绝对路径规范化为相对路径。
- 保留真正影响结果的命令、输入声明、输出声明、执行策略、复现上下文和边界结果。

这保证了同一内容迁移到另一个工作区后，`manifest_sha256` 可以因 Run 身份和进程信息不同而变化，但稳定 `reproducibility_hash` 不会仅因路径变化而变化。

### 2.3 比较器和执行器

`ReplayComparator.compare()`：

- 比较源 Manifest 中登记的输出相对路径和内容哈希。
- 报告缺失输出、意外输出和输出哈希变化。
- 比较稳定复现哈希。
- 显式要求重跑 Manifest 的信息边界仍为 `allowed == true`。
- 只有输出哈希、稳定复现哈希和信息边界全部满足时才返回 `matched=true`。

`ReplayExecutor.execute()`：

- 根据 `host/container` 选择对应 Runner。
- 重跑结束后重新发现输出并创建 Manifest。
- 调用比较器返回 `ReplayExecutionResult`。

## 3. 测试结果

新增：`apps/agent/test_reproducible_rerun.py`

覆盖 8 项契约：

1. 未通过信息边界审计的源 Manifest 不能规划重跑。
2. 环境变量名称集合不一致时拒绝重跑。
3. 新 Run ID、输入/输出路径、观测路径和命令路径能够迁移到新工作区。
4. 相同内容在新工作区重跑时，稳定复现哈希和输出哈希能够匹配；动态 Manifest 哈希可以不同。
5. 输出内容变化会被报告为 `output_hash_mismatch`。
6. 重跑信息边界不允许时不能得到匹配结果。
7. 容器运行时、镜像、工作区和工作负载命令上下文能够保留并迁移。
8. 稳定复现上下文被篡改但哈希未更新时拒绝重跑规划。

验证命令：

```powershell
$env:PYTHONPATH = "$(Resolve-Path apps/agent).Path;$(Resolve-Path .).Path"
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" `
  -m unittest discover -s apps/agent -p 'test_reproducible_rerun.py' -v
```

结果：`8 passed`。

完整 Agent 回归：

```text
Ran 134 tests in 7.799s
OK (skipped=1)
```

目标模块 `py_compile`：通过。

## 4. 未完成和风险

- 尚未接入平台 API 创建新的平台 Run，也没有把源输入 Artifact 固定为平台侧不可变快照。
- 尚未通过平台 Artifact API 创建重跑输出并保存比较结果、Review、Evidence 和 Gate 状态。
- 尚未在真实宿主机 Runner 中执行端到端重跑；`ReplayExecutor` 目前是本地 Agent 级执行接口。
- 当前环境没有 Docker 或 Podman，未完成真实容器重跑和容器内输出上传验收。
- `observed_input_files` 在真实 OS 观察器接入前仍可能来自请求载荷，不能单独证明进程实际读取集合。
- 尚未实现 Windows ETW/minifilter、Linux eBPF/LSM 或容器系统调用级文件观察器，也未实现网络外发观察。
- 哈希比较通过不等于模型结果一定正确；数值容差、随机算法非确定性、外部服务响应和浮点平台差异仍需单独定义。
- 重跑比较不一致时，当前只返回比较对象，尚未自动把 Run 标为待复核或阻断下游任务。

## 5. 下一步

1. 进入 P5-06，接入一个真实受控运行环境的文件访问观察器，并把观察记录可靠写入 Manifest。
2. 在平台 API 增加重跑 Run 创建、输入 Artifact 快照、源 Run 关联和比较结果持久化。
3. 将比较失败映射为平台 Review/Evidence/Gate finding，阻止未经复核的结果进入正式成果物链。
4. 先完成真实容器重跑与网络禁用验收，再扩展 Windows/Linux 文件和网络观察矩阵。
5. 为二进制、浮点和随机模型增加精确哈希、容差比较和不确定性标记策略。

## 6. 交接输入

- 重跑规划与比较：`apps/agent/reproducible_rerun.py`
- Manifest 和稳定哈希：`apps/agent/result_uploader.py`
- Runner 请求：`apps/agent/runner.py`
- 容器 Runner：`apps/agent/container_runner.py`
- 信息边界审计：`apps/agent/information_boundary.py`
- P5-05 契约测试：`apps/agent/test_reproducible_rerun.py`
- 主执行计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 实施状态：`docs/IMPLEMENTATION_STATUS.md`
- 前置交接：`docs/handoffs/P5_04_INFORMATION_BOUNDARY_AUDIT_HANDOFF.md`

## 7. 结论

P5-05 已完成可复现重跑的开发版规划、工作区迁移、稳定复现哈希自校验、输出比较和信息边界门禁，并通过 8 项专项测试及 Agent 全量回归。当前成果可以作为后续平台 Run API、真实容器重跑和 OS 级观察器实现的契约基础，但尚不能宣称生产级可复现执行已经完成。
