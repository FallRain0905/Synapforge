# UX-5 Agent 任务闭环 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-16
>
> 对应阶段：UX-5（工作项定义见 `docs/DEMO_1_0_IMPLEMENTATION_PLAN.md` §5 UX-5）
>
> 上一份交接：`docs/handoffs/UX_4_ONBOARDING_AND_DEVICES_HANDOFF.md`

---

## 1. 本阶段目标

从计划书 §5 UX-5 抄写：

> **目标**：让"Agent 自动领任务"真实发生——这是"多 Agent 协作平台"这个名字成立的前提。
>
> **入口条件**：UX-4 退出。
>
> **退出条件**：存在一条"人只点界面、Agent 自动干活"的自动化验证。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| 前置：接入即授权 | 完成 | `/devices` 设备行新增「授权到项目」：选项目 + 13 项能力 chips → `POST /api/projects/{id}/device-grants`（该端点同时写设备级与 Agent 级授权，属既有能力，此前 UI/脚本完全没有入口）→ **一次性 project_token 弹窗** + 可复制的 worker 启动命令。数据层见 `lib/api.ts` 的 `createDeviceProjectGrant / listDeviceProjectGrants / projectGrantBlob / DEFAULT_DEVICE_PROJECT_CAPABILITIES`；凭据目标新增 `MathAgentPlatform/project-token/<project_id>`（`credential_store.project_token_target`） |
| UX-5-01 常驻任务循环 | 完成 | `apps/agent/agentd.py` 新增 `worker-run` 子命令：`--grant`（首次接入，解析授权串 → token 入凭据管理器 → `worker.json` 记 project/agent/device，**不落 token**）→ 之后无参启动；循环为 claim → progress(RUNNING) → 执行 → result，空队列退避轮询（`--idle-seconds` 起，指数增长到 `--max-idle-seconds`），`--once` 只跑一轮 |
| UX-5-02 空队列与紧急停止 | 部分完成 | 空队列退避与"claim 失败重试"已实现并打印；**紧急停止标志未接入 worker 循环**（`service-emergency-stop` 目前只被 machine service 使用），见 §5 |
| UX-5-03 执行适配 | 完成 | 命令取自任务声明 `resource_policy.worker_command`（字符串或 argv 数组），否则用 worker 级 `--executor-command`；都没有时**显式失败** `executor_not_configured:…`，不做"假装成功"的上报。执行走既有 `LocalRunner` + `CommandAdapter` + `LocalProcessSupervisor`（与 session worker 同一套装配），workspace 白名单限定在 `--workspace` |
| UX-5-04 执行可见性 | 完成 | worker 在执行前后用能力 Token 调用 `POST /api/projects/{id}/runs` 与 `POST /api/runs/{id}/complete`（`_require_agent_capability(..., "run.create")` 允许 Agent 调用），因此 `/runs` 能看到"谁执行、状态、摘要与 stdout 尾部"；Run 登记失败不阻塞任务执行，但会打印原因 |
| UX-5-05 端到端验证 | 完成 | 见 §4：真实平台 + 真实脚本 + 真实 LocalRunner，任务最终 APPROVED 且 Run 记录 SUCCEEDED |

**验收中修掉的两个真缺陷**：

1. **`worker-run` 缺 device_id 导致执行层直接拒绝**：`LocalRunner` 会把 device_id 写进进程规范与运行清单，缺它就抛 `RunnerPolicyError:runner_device_id_required`。现在 device_id 从授权串带入 `worker.json`，缺失时提前报 `worker_device_required`（而不是等执行时才炸）。
2. **向导打印的命令参数顺序错误**：`--url` 是 agentd 的**顶层**参数，必须放在子命令之前（`agentd.py --url X worker-run …`）。初版写成 `worker-run --url X`，用户粘贴会得到 `unrecognized arguments`。

## 3. 与计划的偏差

**有偏差，共 2 处：**

1. **新增"前置：接入即授权"工作项。** 计划 §5 UX-5 没有这一条，但 UX-4 交接已把它标为 UX-5 的必做前置：接入只给设备身份，Agent 要出现在项目看板并领任务还需要项目范围能力。实现只做接线（后端端点早已同时写两级授权），未新增后端能力。
2. **执行适配用"声明式命令"而不是按任务类型分派。** 计划举例"至少跑通一种真实任务类型，如脚本执行类"。实现选择更通用的口径：命令由任务自己声明（`resource_policy.worker_command`），worker 不内置任何领域判断；没有声明就是显式失败。好处是与领域解耦、可被任意任务复用；代价是任务创建侧要写这个字段（目前 Web 的新建任务表单还没暴露它，见 §5）。

## 4. 测试与验证

**基线命令（计划书 §7）**

```text
Agent：python -X utf8 -m unittest discover -s . -p "test_*.py"（apps/agent）
       → Ran 164 tests, OK (skipped=9)，9.6s（UX-4 为 152 → 新增 12 项）
后端：python -X utf8 -m unittest discover -s . -p "test_*.py"（apps/api）
       → Ran 282 tests, OK (skipped=13)，115.5s（本阶段未改后端）
前端：cd apps/web && npm run build → 19 个静态页（/devices 5.53 kB）；npx tsc --noEmit 无错误
```

新增 `apps/agent/test_agent_worker.py`（12 项）：授权串往返与两类非法输入、首次接入写配置与凭据（且**token 不落配置文件**）、第二次无参启动可复现身份、凭据缺失时的可执行提示、缺 device_id 提前报错、能力透传、任务级命令优先于 worker 默认、**未声明命令显式失败**、声明命令真的被执行（断言 stdout 有标记）、失败命令带 exit code、子命令已注册。

**端到端验收（真实平台 8010 + connect-agent.ps1 + 真实 LocalRunner）**

| 验收标准 | 操作 | 结果 |
| --- | --- | --- |
| 接入即可授权 | `/devices` → 「授权到项目」→ 选项目（1 个）+ 13 项能力 → 提交 | 一次性 token 弹窗出现（`prj_SUTMZh…`），worker 命令带授权串且 `--url` 在子命令前 |
| worker 首轮建立身份 | `worker-run --grant <授权串> --once` | 打印 agent_id / device_id / project_id / 13 项能力，`worker.json` 已写、token 在凭据管理器 |
| 界面建任务 → Agent 自动领取并执行 | 建带 `resource_policy.worker_command` 的任务（priority=critical）→ `worker-run --once` | `[worker] 领取任务 5a36319e… 结果：成功 · exit=0 run=run-a772a2e35d78 \| stdout: worker executed task OK` |
| 平台侧事实 | dashboard | 任务状态 **APPROVED**、assignee=**agent-worker**、事件链 `task.claimed → task.progress → task.result_submitted`（actor=agent-worker） |
| Run 台账 | 同上 | `/runs` 出现 `agent-worker · SUCCEEDED · task=0431ce9a · summary=exit=0 run=run-b4294f1e9943 \| stdout: run-record-check` |
| 不假装成功 | 建一个没有声明命令的任务 | worker 上报失败：`executor_not_configured:任务没有声明 resource_policy.worker_command…`，任务进入 FAILED（不是 APPROVED） |
| 授权串可用性 | API 侧独立复现（不经 UI） | 同一授权串驱动 worker 领取并执行成功 |

## 5. 尚未完成与边界

- **紧急停止未接入 worker 循环**（UX-5-02 只做了空队列退避）：worker 不读 `service-emergency-stop` 标志，Ctrl+C 之外没有优雅停机开关。
- **Web 新建任务表单不暴露 `resource_policy.worker_command`**：目前只能在 API 层写。要让"界面建任务 → Agent 自动干"完全自助，下一步应在任务表单加一个"执行命令"字段（或模板级默认命令）。
- **worker 不产生成果物**：结果是 summary + Run 台账，`output_artifact_ids` 恒为空；成果物上传（`ResultUploader` / `AgentArtifactClient`）尚未接进循环，因此 CUMMCM 那类"产出 result_table"的任务还跑不出正式成果物。
- **不做 lease 心跳**：worker 的租约靠 claim 时给的 `--lease-seconds` 有效期覆盖单个任务；长任务（超过租约）会被 UX-3 的回收机制判为 `RUNNING → NEEDS_REVISION`，需要 `lease-heartbeat` 才能扛住长任务。
- **单并发**：一次只领一个任务（决策 D5 明确 demo 期不做多任务并发调度）。
- **Run 记录不含 manifest/边界观测**：只写 status/summary/stdout/stderr，`information_boundary` 与 `observed_input_files` 留给后续。
- **`--grant` 的 token 会过期**（默认 86400s）：过期后 worker 会在 claim 时拿到 401/403，当前提示是原始 `http_401:…`，没有引导重新授权。

## 6. 下一步

- 下一阶段：**UX-6 协作正确性**（可与 UX-5 的补强并行）。UX-6 的三项（协作编辑落库、门禁人工批准、交接收据）都只差接线，且都已确认后端能力存在。
- UX-5 的补强建议（不阻塞 UX-6）：租约心跳、成果物上传、任务表单暴露执行命令、紧急停止。
- 若继续 UX-5 补强，优先做**租约心跳 + 成果物上传**——它们是"Agent 产出正式成果物"的最后两段。

## 7. 复现命令

```powershell
# 1) 基线
cd apps\agent; $env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"     # Ran 164 tests, OK (skipped=9)
cd ..\api
python -X utf8 -m unittest discover -s . -p "test_*.py"     # Ran 282 tests, OK (skipped=13)
cd ..\web; npm run build                                     # 19 个静态页（/devices 5.53 kB）

# 2) 端到端
python -X utf8 -m uvicorn scripts.acceptance_empty_api:app --host 127.0.0.1 --port 8010
cd apps\web; $env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8010"; npx next dev -p 3014
#    /devices → 接入设备（向导命令）→「授权到项目」→ 复制 worker 命令
#    建任务（带 resource_policy.worker_command）→ 粘贴 worker 命令 → /runs 与 /tasks 查看结果
```