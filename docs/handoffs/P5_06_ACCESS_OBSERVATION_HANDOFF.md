# P5-06 文件与网络访问观察交接文档

> 日期：2026-09-15
>
> 状态：`PASS_WITH_ASSUMPTIONS`
>
> 任务：建立文件/网络访问观察的统一契约，并把观察结果传入 Runner、Session、Manifest 和可复现重跑链路

## 1. 本轮目标

在 P5-04 信息边界审计内核和 P5-05 可复现重跑的基础上，冻结真实观察器的输入协议。观察器必须能够按 Run/进程范围提供文件读取、文件写入和网络连接事件；观察器缺失或异常时不能把“没有记录”解释为“没有访问”。

本轮优先完成跨平台适配所需的开发版数据面，不宣称已经完成 Windows ETW、Linux eBPF 或容器系统调用追踪。

## 2. 已完成

### 2.1 观察领域对象

修改：`apps/agent/information_boundary.py`

- 新增 `NetworkObservation`，记录主机、端口、协议、方向和观察来源。
- 新增 `AccessObservationCapture`，统一承载文件观察、网络观察、捕获状态和失败原因。
- 新增 `AccessObservationProvider` 协议，约束 `start(workspace_path, process_id)` 和 `stop()`。
- 新增 `FailClosedAccessObservationController`，在观察器缺失、异常、返回非法捕获或必需记录缺失时返回失败闭环状态。
- 新增 `InformationBoundaryAudit.evaluate_network()`，支持 `deny-by-default`、`allow-listed` 和 `unrestricted` 三类网络策略。

### 2.2 外部事件桥接

新增：`apps/agent/access_observer.py`

- `AccessTraceEventNormalizer` 将统一 JSON 事件归一化为 `FileObservation` 或 `NetworkObservation`。
- `AccessObservationRecorder` 提供线程安全事件接收、工作区绑定和进程 ID 过滤。
- `JsonlAccessObservationProvider` 为 ETW/eBPF/strace/容器桥接程序提供 JSONL 接口。
- 未知事件类型、缺少路径/主机或时间格式错误会被记录为错误，不会静默丢失为成功。

当前冻结的事件示例：

```json
{"type":"file.read","path":"input/data.csv","process_id":"p1","observation_source":"system"}
```

```json
{"type":"network.connect","host":"api.example.com","port":443,"protocol":"tcp","direction":"egress","process_id":"p1","observation_source":"system"}
```

### 2.3 执行链路接入

- `RunnerRequest` 增加 `observed_network_connections`。
- `SessionRunStartPayload` 和 `session.schema.json` 增加网络观测记录字段。
- `SessionWorkerRuntime` 将网络观测传入 Runner 请求。
- `RunManifestBuilder` 新增 `network_access` 结果，并将网络违规合并进 `information_boundary`。
- `ReproducibleReplayPlanner` 从源 Manifest 恢复网络观测记录。
- `deny-by-default` 下检测到出站连接生成 `fatal / network_egress_not_allowed`。
- `allow-listed` 下未命中允许主机生成 `fatal / network_host_not_allowlisted`。

## 3. 验证情况

已完成：

```text
目标 Python 模块 py_compile       -> passed
P5-05 专项回归                    -> 8 passed
ConPTY 独立回归                   -> 8 passed
```

本轮完整 Agent 回归首次运行出现 1 个既有 ConPTY 输入回显断言失败；随后单独复跑 `test_conpty_runner.py` 为 8 项通过，第二次完整回归为 `134 passed, 1 skipped`。该波动仍需后续纳入稳定性验收，但当前没有留下确定性失败。

## 4. 未完成和风险

- 尚未实现 Windows ETW、文件系统 minifilter 或其他能证明真实读取的 Windows 采集器。
- 尚未实现 Linux eBPF/LSM、auditd 或容器内系统调用采集器。
- `JsonlAccessObservationProvider` 目前是外部桥接入口，不会自行产生操作系统事件。
- 尚未把观察器生命周期绑定到真实 Runner 的进程树、子进程和容器 PID namespace。
- 尚未实现真实网络阻断；当前网络审计只根据已提供的观察事件进行判定。
- 尚未把观察失败自动写入平台 Review、Evidence 和 Gate。
- Session Schema 目前允许网络观测对象的通用字段，尚未冻结更细的 JSON Schema 字段约束和版本升级迁移。
- 当前没有增加专门的 P5-06 单元测试，细节测试和跨平台实机矩阵留到统一测试批次。

## 5. 下一步

1. 实现一个真实平台观察器，优先选择当前 Windows 开发机可验证的进程范围方案。
2. 将观察器挂接到 Runner 的启动、进程树追踪和结束生命周期，生成真实 `system` 来源事件。
3. 完成容器路径映射、容器网络事件和 `--network none` 实际验收。
4. 把观察失败、未来数据和非法外发事件转化为平台 Run/Review/Gate 状态。
5. 再扩展 Linux eBPF/LSM 和跨平台进程树测试。

## 6. 交接输入

- 观察器契约：`apps/agent/information_boundary.py`
- JSONL 桥接：`apps/agent/access_observer.py`
- Runner 请求：`apps/agent/runner.py`
- Session 协议：`packages/agent_protocol/__init__.py`、`packages/agent_protocol/session.schema.json`
- Manifest：`apps/agent/result_uploader.py`
- 重跑：`apps/agent/reproducible_rerun.py`
- 主计划：`docs/PROJECT_EXECUTION_PLAN.md`
- 实施状态：`docs/IMPLEMENTATION_STATUS.md`
- 前置交接：`docs/handoffs/P5_05_REPRODUCIBLE_RERUN_HANDOFF.md`

## 7. 结论

P5-06 已完成文件/网络观察契约、JSONL 外部桥接、网络边界规则和 Runner/Session/Manifest/重跑传递链的开发版切片。真实 OS/容器系统调用观察、进程树绑定、网络阻断和平台门禁联动仍未完成，下一轮进入 P5-06-REAL。
