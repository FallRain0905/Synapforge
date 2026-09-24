# P5-04 信息边界审计内核交接文档

> 日期：2026-09-15
>
> 状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务：建立声明输入、实际读取、未来数据和工作区范围的确定性审计内核，并以 fail-closed 方式接入 Run Manifest

## 1. 本轮目标

解决 P5-01 中“`observed_input_files` 仍由调用方提供、没有系统级文件访问观察”的协议差距。当前轮不伪造 Windows/Linux 系统调用追踪，而是先冻结审计对象、finding 规则、策略字段和观察器适配接口，为后续 OS/容器实现提供稳定契约。

## 2. 已完成

### 2.1 审计对象和规则

新增：`apps/agent/information_boundary.py`

`FileObservation` 字段：

- `path`
- `access_mode`: `read`、`write` 或 `read_write`
- `observation_source`: `system`、`declared`、`imported` 或 `unknown`
- `available_at`: 文件/数据实际可获得时间，可选
- `data_time_end`: 数据内容覆盖的最后时间，可选

`InformationBoundaryAudit.evaluate()` 输出：

- `allowed`
- `audit_status`
- `declared`
- `observed`
- `undeclared`
- `outside_workspace`
- `future_data`
- `violations`

finding 规则：

- 实际读取未在声明输入中：`major / undeclared_input_file`。
- 读取越出工作区：`fatal / observed_input_outside_workspace`。
- 文件可用时间晚于 `decision_time`：`fatal / future_data_available_after_decision`。
- 数据最后时间超过 `data_cutoff`：`fatal / future_data_beyond_cutoff`。
- 要求系统观察但没有读取记录：`major / input_observation_not_captured`。
- 要求系统观察但来源不是 `system`：`major / system_observation_incomplete`。
- 开启时间元数据要求但没有时间字段：`major / temporal_metadata_missing`。

### 2.2 观察器接口

`FileAccessObservationProvider` 冻结为：

```python
start(workspace_path: str) -> None
stop() -> Sequence[FileObservation]
```

`FailClosedObservationController` 负责调用该接口。观察器不存在、启动/停止异常或在 required 模式下没有返回记录，均返回 `not_captured`，不会转化为通过。

预留适配方向：

- Windows：ETW、文件系统 minifilter 或受控容器事件采集。
- Linux：eBPF/LSM 或容器运行时审计事件。
- 容器：运行时事件与工作区路径映射结合。

本轮没有宣称上述任一适配器已经实现。

### 2.3 Manifest 接入

修改：`apps/agent/result_uploader.py`

`RunManifestBuilder` 默认把 `data_access_policy.observation_mode` 设为 `system`，使用 `InformationBoundaryAudit` 生成 `information_boundary`。可选策略字段：

```json
{
  "observation_mode": "system",
  "decision_time": "2026-09-14T10:00:00+00:00",
  "data_cutoff": "2026-09-14T10:00:00+00:00",
  "allow_future_data": false,
  "require_temporal_metadata": true,
  "observed_files": [
    {
      "path": "input/data.csv",
      "observation_source": "system",
      "available_at": "2026-09-14T09:00:00+00:00",
      "data_time_end": "2026-09-14T09:00:00+00:00"
    }
  ]
}
```

Run Manifest 的 `information_boundary` 现在保留审计状态、观测记录和 finding；在 `allowed=false` 时，调用方不应将该 Run 作为正式下游输入。

## 3. 验证结果

使用项目 bundled Python：

```text
apps/agent.test_information_boundary -> 5 passed
apps/agent.test_result_uploader -> 6 passed
apps/agent.test_container_runner -> 5 passed
apps/agent.test_session_runtime -> 9 passed
apps/agent full unittest discover -> 126 passed, 1 skipped
py_compile apps/agent/*.py and packages/agent_protocol/__init__.py -> passed
json.tool packages/agent_protocol/session.schema.json -> passed
json.tool packages/contracts/domain.schema.json -> passed
```

## 4. 未完成和风险

- 尚未实现 Windows ETW、minifilter、Linux eBPF/LSM 或容器系统调用级文件访问观察器。
- `observed_input_files` 仍是当前 Runner 请求中的输入载荷；在真实观察器接入前，它不能证明进程实际读取集合。
- 当前只审计文件路径和可用/数据时间，不审计真实网络外发、数据库读取、内存中注入数据或外部模型服务的数据发送。
- 审计结果目前进入本地 Run Manifest，没有自动创建平台 Review、Evidence 和 Gate finding。
- `data_access_policy` 仍是通用字典，尚未升级为平台正式领域 Schema 和 API 版本化迁移。
- 未来数据的判断依赖可靠的 `available_at`/`data_time_end` 元数据；元数据来源和签名尚未验收。

## 5. 下一步

1. 进入 P5-05：实现可复现重跑入口，固定输入 Artifact、代码提交、环境、参数和随机种子，并比较输出/Manifest 哈希。
2. 选择一个受控运行环境实现真实文件访问观察器，先覆盖 Windows 开发机和容器内路径映射。
3. 将 `information_boundary.allowed=false` 转化为平台 Run 失败或 Gate 阻断，并保存 Review/Evidence 引用。
4. 增加网络外发策略与实际连接事件审计，覆盖云模型和外部 API。

## 6. 交接输入

- 审计内核：`apps/agent/information_boundary.py`
- Manifest：`apps/agent/result_uploader.py`
- Runner 请求：`apps/agent/runner.py`
- Session 协议：`packages/agent_protocol/__init__.py`
- 容器路由：`apps/agent/container_runner.py`、`apps/agent/session_runtime.py`
- 阶段主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 实施状态：`docs/IMPLEMENTATION_STATUS.md`
- P5-03 交接：`docs/handoffs/P5_03_CONTAINER_ROUTING_AND_MANIFEST_HANDOFF.md`

## 7. 结论

P5-04 已完成信息边界审计的开发版内核、规则和 fail-closed 观察器接口，并接入 Run Manifest。真实 OS 级追踪、网络观察、平台 Review/Gate 自动阻断和可复现重跑仍未完成；阶段 5 继续保持 `PASS_WITH_ASSUMPTIONS`。
