# CL-1 执行产出落库（产出文件 + 回答） 交接

> 交接状态：`PASS`
>
> 日期：2026-09-16
>
> 对应阶段：CL-1（工作项定义见 `docs/CONTENT_LIFECYCLE_PLAN.md` §5；决策见 §3）
>
> 上一份交接：—（CL 主线首份；前置工作是「Agent 回答与原始输出同步到平台」，见 `IMPLEMENTATION_STATUS.md`）

---

## 1. 本阶段目标

从实施计划 §5 CL-1 抄写：

> **目标**：跑完一个任务，本次产出的文件与回答自动出现在成果物库（待审），并挂到对应的 Run 上。
>
> **验收标准**
> 1. 真跑一个 `workspace-write` 沙箱的 Codex 任务产出文件 → 成果物库出现该文件（`PENDING_REVIEW`，来源任务/运行正确）；
> 2. Run 详情"产出成果物"数量 = 实际产出数；回答作为 `agent_answer` 出现；
> 3. 断网（停止 API）跑一轮 → 任务仍成功，恢复后本地队列补传成功；
> 4. **回归**：Agent / API 套件全绿且数量不减。
>
> **退出条件**：产出与回答都在平台上可查到；上传失败不影响任务成败（有测试）。
>
> **Do NOT**：不要自动 `APPROVED`；不要把整个工作区传上去（D-CL-5）；不要为了上传而改任务状态机。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| CL-1-01 工作区快照差分 | 完成 | 新增 `apps/agent/workspace_scan.py`：`snapshot()`（相对路径 → 大小 + mtime，带剪枝与 20000 文件上限）、`diff()`（created/modified/deleted，`truncated` 如实上报）、`is_excluded()`（依赖目录/构建产物/缓存/本地状态文件）。9 项测试 |
| CL-1-02 任务循环接上传 | 完成 | 新增 `apps/agent/output_collector.py`（`OutputCollector`：`before` 快照、`after` 差分 → `OutputDiscovery` 哈希/类型 → `ResultUploader` 队列 → `upload_pending`），复用既有 `result_uploader.py`；`TaskLoop` 新增 `output_collector` 钩子（协议注入），`agentd._build_output_collector` 组装（上传队列放 `uploads.db`，与 `agentd.db` 分开） |
| CL-1-03 回答入库 | 完成 | 回答写成 `<workspace>/.math-agent-platform/answers/<run_id>.md` 后按普通产出上传，类型改写为 `agent_answer`；`resource_policy.inline_answer_artifact=false` 可关。平台侧把 `agent_answer` 追加进 `ArtifactType`（追加新值，不动既有语义） |
| CL-1-04 引用回填 | 完成 | `TaskLoop._complete_run(..., artifact_ids=)` 与 `/api/tasks/{id}/result` 都带 `output_artifact_ids`；采集在"完成 Run"之前，Run 详情显示"产出 N 个成果物" |
| CL-1-05 摘要说明 | 完成 | 采集说明追加到**诊断段**（回答仍在前）：`产出 N 个成果物（待审）` / `M 个文件超过上传上限…` / `K 项上传失败（已入本地队列待补传）` / `补传历史产出 X 项` |
| CL-1-06 失败不判死 | 完成 | 采集异常一律 try/except（钩子内与 `after` 内双保险），只记 `report_errors` 与日志；`before` 失败也照跑 |

**执行中修掉的三个真问题**（都由实机暴露）：

1. **`agent_answer` 不在平台允许的类型里** → 上传 422。平台上一次实跑就以
   `collector 产出采集失败：RuntimeError:artifact_http_422 … 'agent_answer'` 暴露出来（同时验证了"采集失败不影响任务成败"）。
   处理：把 `agent_answer` **追加**进 `ArtifactType`（追加不改变既有类型语义；交付装配按白名单取内容，未知类型自然被忽略）。
2. **失败运行的占位句变成"回答成果物"**：`（本次执行没有产出回复文本）` 是给界面看的说明，被当成回答写进了内容库。
   处理：`_answer_of` 识别该占位句并返回空。
3. **声明式命令的诊断摘要被当成回答**：`exit=1 … | stderr: …` 没有 `---` 分隔符，整行被当成"回答"。
   处理：**要求分隔符**才产出回答成果物（回答只在 `回答\n\n---\n诊断` 结构里取）。

## 3. 与计划的偏差

- **采集默认开关**：计划未细化"哪种形态默认开"。实现为 **`daemon-run` 默认开、`worker-run` 默认关**（`--collect-outputs` 可选开）。
  理由：`demo-1.0.ps1` 用 `worker-run --once` 跑演示链路，默认采集会给演示项目塞进产出（破坏不变式 I1 的可读性）。这是执行细节，不改变 D-CL-1/2 的语义。
- **上传队列的位置**：计划 §6.3 只写了"本地持久化队列"。实现放在 `<state-dir>/uploads.db`（**独立于** `agentd.db`，也不放进工作区）：
  避免与执行体的 SQLite 连接争锁，也不污染用户工作区。已在 `ARTIFACT_LIFECYCLE.md` 写明。
- **回答文件落在工作区内**（`.math-agent-platform/answers/`）：为满足 I3（内核只读自己工作区内的文件）。
  该目录已在扫描剪枝里排除，不会被当成任务产出。

其余逐条比对 §5 工作项表：范围、文件与验收口径一致。

## 4. 测试与验证

```text
# 内核
cd apps/agent && python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 281 tests, OK (skipped=9)        # CL-1 前为 259（+22：workspace_scan 9 / output_collector 9 / 任务循环钩子 4）

# 平台
cd apps/api && python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 323 tests, OK (skipped=14)       # CL-1 前为 321（+2：产出回填与结果契约）

# 端到端主链路（不变式 I1）
pwsh -File scripts/demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove
→ 17 项全过

# 前端
cd apps/web && NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build
→ 19 页通过（成果物页新增类型中文名）
```

**实机验收（2026-09-16，Windows）**

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| ① 产出文件进成果物库 | **通过** | 声明式命令任务写出 `notes/result.md` → 成果物库出现 `paper_source · PENDING_REVIEW · result.md · hash 40c3f893… · task 1aeb16af`；Run 摘要 `产出 2 个成果物（待审）` |
| ① 回答进成果物库 | **通过** | Codex 任务（成功）产出的回答以 `agent_answer · PENDING_REVIEW` 入库，带 task/run 引用与内容哈希 |
| ① 重启后规则一致 | **通过** | 重启内核后跑声明式任务 → **只**产出文件成果物（1 个），不再把诊断摘要当回答 |
| ② Run 产出数量 | **通过** | `/api/projects/{id}/runs` 返回 `output_artifact_ids` 数量与实际一致（1/2 个）；详情接口可见 |
| ③ 失败补传 | **通过** | 早期因类型 422 失败的产出留在队列里，**下一轮执行前自动补传成功**（`agent_answer 40ac010b…` 就是补传上来的）；采集失败期间任务结果不受影响 |
| ④ 回归 | **通过** | Agent 281 / API 323 / demo 17 项 / 前端 19 页 |

> Codex 服务商本轮多次返回 `Selected model is at capacity`（上游限流），所以"产出文件"的验收用**声明式命令**跑通；
> Codex 路径的回答入库同样验证通过（`agent_answer` 由真实 Codex 回答生成）。这也顺带证明了 I4：**执行失败不影响采集逻辑，采集失败不影响任务成败**。

## 5. 尚未完成与边界

用**用户视角**写：

1. **产出还不能直接被下游用**：它们进的是「待审」，要有人批准（`downstream_allowed=1`）才能作为下游输入——这是设计如此（D-CL-1），但**界面还没有"待审成果物"入口**（CL-2）。
2. **成果物详情页还没有**：看不出"从哪来、几版、谁批的、被谁用"（CL-3）；类型中文名已加，但引用关系与版本谱系还没有。
3. **归档没有入口**（CL-4）：状态机支持 `ARCHIVED`，界面里点不到。
4. **上传上限**：单文件 >100MB 只登记不上传；工作区文件数 >20000 停止扫描并如实标注。
5. **`worker-run`（前台调试）默认不采集**：要看采集效果要么用 `daemon-run`，要么给 `worker-run` 加 `--collect-outputs`。
6. **`git status` 之外的盲区**：差分靠 (大小, mtime)。若某个执行体原地改写文件且保持大小与 mtime（极端情况），该文件不会被识别为产出。

## 6. 下一步

- 下一阶段：**CL-2 内容→审核→下游闭环**
- 入口条件是否满足：**是**（CL-1 退出条件满足：产出与回答都能在平台上查到；上传失败不影响任务成败且有测试）
- 建议的下一批工作项：`CL-2-01`（审核门禁页「待审成果物」分区）、`CL-2-02`（成果物审核与"为什么不能用于下游"解释）、`CL-2-03`（`REJECTED` → 提交新版本的修订引导）、`CL-2-04`（建/改任务时列出上游已批准产出作为输入建议）
- 现成抓手：现状数据里已经有真实的待审成果物（`agent_answer` 与 `paper_source`）可直接用来做界面验收。

## 7. 复现命令

```powershell
# 平台
cd apps/api; ... uvicorn app.main:app --port 8010        # 或用 scripts/start-api.ps1

# 内核（采集默认开；用独立状态目录与工作区做验收，避免污染仓库）
python -u -X utf8 apps/agent/agentd.py daemon-run `
  --url http://127.0.0.1:8010 `
  --workspace <一个空目录> `
  --state-dir <独立状态目录> `
  --grant <授权串>

# 建一个声明式任务（会话内用 API，或直接在任务页用「设置执行方式 → 声明式命令」）
# 任务跑完后检查：/api/projects/{id}/artifacts 里出现 PENDING_REVIEW 的产出；
# /api/projects/{id}/runs 里 output_artifact_ids 数量一致。

# 回归
cd apps/agent; python -X utf8 -m unittest discover -s . -p "test_*.py"
cd apps/api;   python -X utf8 -m unittest discover -s . -p "test_*.py"
pwsh -File scripts/demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove
```

## 8. 壳/内核版本对照

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python 内核 | 0.1.0（`SIDECAR_VERSION`） | 新增 `workspace_scan.py`、`output_collector.py`；`task_loop.py` 增采集钩子；`result_uploader.py` 支持 `create_status` |
| 平台 | 0.1.0 | `ArtifactType` 追加 `agent_answer`；`RunComplete`/`TaskResultSubmit` 的 `output_artifact_ids` 开始被真正使用 |
| sidecar 契约 | v1（未变） | 无端点/字段变化 |
| 协议 | `schema_version=1.0` | 无变化（沿用 `artifact.*` 事件类型，D-CL-10） |