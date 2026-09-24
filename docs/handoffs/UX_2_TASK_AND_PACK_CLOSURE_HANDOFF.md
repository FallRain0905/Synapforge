# UX-2 任务与模板闭环 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-16
>
> 对应阶段：UX-2（工作项定义见 `docs/DEMO_1_0_IMPLEMENTATION_PLAN.md` §5 UX-2）
>
> 上一份交接：`docs/handoffs/UX_1_FRONTEND_FOUNDATION_AND_PROJECT_START_HANDOFF.md`

---

## 1. 本阶段目标

从计划书 §5 UX-2 抄写：

> **目标**：把"模板包"从"只能操作已绑定的那个"变成"可选、可懂"，把任务从"只能看列表行"变成"能看详情、能改派"。
>
> **入口条件**：UX-1 退出。
>
> **退出条件**：`getCompetitionPacks` 不再是死代码；任务负责人无任何硬编码名字残留。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| UX-2-01 任务详情 | 完成 | `apps/web/app/tasks/page.tsx` 新增详情弹窗（`task-detail-modal`，宽弹窗）：状态/阶段/优先级/复核与数据边界 chips、负责人（可改）、任务说明、阻塞原因、完成标准、**依赖任务**（含各自状态）、**关联成果物**（产出与输入分开标注）、**交接**、**最近 5 条事件**。全部数据取自已在 `WorkspaceProvider` 中的 dashboard/review-center，未新增端点 |
| UX-2-02 负责人真实列表 + 改派 | 完成 | 删除硬编码的 `Mira/Aster/Nova/Reviewer`；`assigneeOptions()` 由 `dashboard.agents` 派生（值为 `agent_id`，标注 `display_name`），并保留"历史值"选项以兼容种子数据里的显示名。创建弹窗与详情弹窗共用同一选项集；改派走 `updateTask(id, { assignee })`。`lib/api.ts` 的 `updateTask` 由"只发 status"扩展为 `TaskPatch{status?, assignee?, blocked_reason?}`（后端 PATCH 用查询参数接收） |
| UX-2-03 状态机提示与拒绝原因 | 完成 | 新增 `TASK_TRANSITIONS` 与 `store._task_transition_allowed` 对齐；按钮文案带目标状态（如"开始 → RUNNING"）与 `title` 后果说明；BLOCKED/FAILED/CANCELLED 走 `ConfirmDialog` 二次确认。**修掉两个必然失败的按钮**（见 §3 偏差 1） |
| UX-2-04 模板包选择器 | 完成 | `apps/web/app/pack/page.tsx` 接线 `getCompetitionPacks()`（原死代码），新增「可用模板包（N）」面板：包名、版本、模板数、DAG 任务数、题号与问题范围；已绑定的包标注"已绑定"，未绑定的包提供"用此包新建项目"（复用 UX-1 的 `CreateProjectModal`）。项目包无法解析时显示 `pack-unbound` 面板并给出创建入口（含后端返回的 `pack_not_found:<id>` 原因） |
| UX-2-05 题号选择 | 完成 | 页头新增 `pack-problem-code` 下拉，默认取 `project.problem_code`，选项来自所选包 summary 的 `problem_codes`；apply 时随 `problem_code` 一并提交（此前页面只发 `questions`） |
| UX-2-06 apply 前确认 | 完成 | `ConfirmDialog`（`pack-apply-confirm`）在应用前列出：包名与版本、**将补建的任务数与成果物数**（取 `materialization.missing_tasks/missing_artifacts`）、题号、问题范围，并说明 apply 幂等。应用后 toast 报实际新增数（`created_task_count`/`created_artifact_count`） |

**顺带修正**：`apps/web/lib/api.ts` 的 `Task` 类型补上后端契约已有但前端漏掉的字段（`parent_task_id`、`dependency_task_ids`、`acceptance_criteria`、`required_capabilities`、`input_handoff_ids`、`requires_human_approval`、`deadline`），否则详情弹窗无法通过类型检查。

## 3. 与计划的偏差

**有偏差，共 3 处：**

1. **修掉了两个必然被服务端拒绝的按钮（计划外，但属 UX-2-03 的目标）。** 现状任务页提供 `WAITING_REVIEW → APPROVED`（"通过"）与 `RUNNING → NEEDS_REVISION`（"返工"）两个动作，前者撞 `task_approval_requires_review`（403，已实测），后者撞 `invalid_task_transition`（409，状态机不允许该迁移）。二者都是**永远失败**的按钮。现改为：状态迁移表从 `_task_transition_allowed` 推导，`WAITING_REVIEW` 行只提供"返工 → NEEDS_REVISION / 阻塞"，并在行内给出「去审核」入口（批准必须由人工复核产生，见决策 D9 与 UX-6-02）。
2. **"未绑定包时给出绑定入口"实现为"用该包新建项目"，而不是换包。** 平台没有项目级换包端点（`PATCH/PUT /api/projects*` 不存在），且换包会改变四问 DAG 与成果物骨架。因此入口落在创建路径上，并在页面上明确说明这一限制。若后续要支持换包，属于决策变更（计划书 §10 P-1/D12 相关）。
3. **`blocked_reason` 仍未采集。** 后端 `PATCH` 支持该字段，本阶段只在详情里展示已有原因，未在"阻塞"确认弹窗里收集输入。理由：确认弹窗是通用组件，加入表单输入会改动其契约；留待需要时单独设计。

## 4. 测试与验证

**基线命令（计划书 §7）**

```text
后端：python -X utf8 -m unittest discover -s . -p "test_*.py"（apps/api）
      → Ran 266 tests, OK (skipped=13)，35.9s（本阶段未改后端）
前端：cd apps/web && npm run build
      → 16 条路由全部静态预渲染；/tasks 2.69 kB → 5.24 kB，/pack 3.41 kB → 4.76 kB
类型：npx tsc --noEmit → 无错误
```

**浏览器端到端验收（真实前端 + 真实后端，独立临时库）**

夹具与 UX-1 相同：`scripts/acceptance_empty_api.py`（8010）+ `NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next dev -p 3014`。

| 验收标准 | 操作 | 结果 |
| --- | --- | --- |
| 1. 能看到可用模板包列表与版本 | 打开 `/pack` | `pack-catalog` 显示「CUMCM 数学建模竞赛工作流包 · 已绑定 / v1.1.0 · 9 个模板骨架 · 17 个流程任务 / 题号 A–E · 问题 1–4」 |
| 1. 能选题号 | 页头下拉 | 选项 `题号未指定 + A–E`，默认选中项目题号 `C` |
| 1. 应用前有确认 | 点"一键应用模板包" | `pack-apply-confirm`：「包：CUMCM… v1.1.0 / 将补建 **17** 个任务、**9** 个模板成果物 / 题号：C；问题范围：全部 / 幂等说明」 |
| 1. 应用后报实际数量 | 确认应用 | 物化进度变为 **任务 17/17 · 成果物 9/9**（预告数与实际数一致） |
| 2. 负责人来自真实 Agent | 创建/详情下拉 | 选项为 `未指派`、`Mira / 模型 Agent（agent-mira）`、`Aster / 仿真 Agent（agent-aster）`、`Nova / 写作 Agent（agent-nova）` |
| 2. 改派并刷新保持 | 详情里改派为 `agent-aster` → 刷新 | toast「已指派给 agent-aster」；刷新后列表行与详情下拉均为 `agent-aster` |
| 3. 详情能看到依赖与关联成果物 | 打开任务详情 | 依赖任务（1）显示前置任务的标题/阶段/状态；关联成果物分"产出"（PROBLEM_FACTS.json / PROBLEM_ANALYSIS.md）与"输入"（DATA_PROFILE.json）两类渲染。**注**：模板骨架产出的成果物 `task_id` 为空，为验证该渲染分支，在临时库里手工把两个成果物的 `task_id` 指向任务、并把一个成果物写入任务的 `input_artifacts` |
| 附加：审批边界 | 查看 `WAITING_REVIEW` 任务 | 按钮为"返工 → NEEDS_REVISION / 阻塞 → BLOCKED"，无"通过"；行内出现「去审核」；直接 `PATCH status=APPROVED` 实测返回 **403 `task_approval_requires_review`** |
| 附加：破坏性操作确认 | 点"阻塞" | `task-transition-confirm`：任务名 + 后果 + 目标状态；取消后状态不变（仍 WAITING_REVIEW） |
| 附加：未绑定包 | 用 `competition_pack=not-a-real-pack` 建项目后打开 `/pack` | `pack-unbound`：说明该包无法解析、平台不支持换包，并提供"用模板包新建项目"；catalog 中该包出现"用此包新建项目" |
| 附加：错误透传 | 提交 200 字任务名 | toast：`title：String should have at most 180 characters`（服务端 422 字段级原因） |

## 5. 尚未完成与边界

- **换包仍不可能**：给已有项目更换模板包没有端点，UI 只提供"用目标包新建项目"。
- **`pack-use-<pack_id>` 按钮未被验收覆盖**：平台当前只有 1 个内置包，正常情况下该按钮不出现（只有绑定了不可解析包时才显示）。逻辑已实现，等出现第二个包时需回归一次。
- **阻塞原因不可填写**（见 §3 偏差 3）。
- **`CANCELLED`/`APPROVED` 任务无任何操作按钮**，只显示不可再改的说明；没有"恢复已取消任务"的路径（后端状态机也不允许）。
- **任务详情不含 Run 记录**：`dashboard.runs` 里有 run，但详情弹窗本阶段只列事件/成果物/交接；run 与任务的关联展示留给后续需要时补。
- **`/tasks` 的阶段看板行不可点开详情**（只有清单行有"详情"按钮），保持一致性的打磨项。
- 其余 UX 阶段未做：门禁批准 UI（UX-6-02）、协作编辑落库（UX-6-01）、Agent 相关（UX-3/4/5）。

## 6. 下一步

- 下一阶段：**UX-3 Agent 状态真实性**（可与 UX-6 并行；UX-2 部分依赖它来解释"谁在执行任务"）
- 入口条件是否满足：**是**。UX-2 已退出（`getCompetitionPacks` 有真实调用方；`grep` 确认任务页无硬编码 Agent 名）。
- 建议的下一批工作项：`UX-3-01`（心跳超时扫描置 offline）、`UX-3-03`（租约回收：CLAIMED→READY、RUNNING→NEEDS_REVISION）、`UX-3-04`（接通 `broadcast_event`）
- 提醒：UX-3 会改 `apps/api/app/store.py` 与 `main.py`，必须同步补 `apps/api/test_*.py` 的契约测试，否则"后端测试数量不低于 266"这条不变式会因缺少新覆盖而失去意义。

## 7. 复现命令

```powershell
# 1) 基线
cd apps\api
$env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"     # Ran 266 tests, OK (skipped=13)
cd ..\web
npm run build                                                # 16 条路由；/tasks 5.24 kB、/pack 4.76 kB
npx tsc --noEmit                                             # 无类型错误

# 2) 验收环境（两个终端，仓库根目录起 API，apps/web 起前端）
python -X utf8 -m uvicorn scripts.acceptance_empty_api:app --host 127.0.0.1 --port 8010
cd apps\web; $env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8010"; npx next dev -p 3014

# 3) 走查要点
#    /pack  → 应用前确认应预告 17 任务 / 9 成果物；应用后物化进度 100%（17/17、9/9）
#    /tasks → 详情弹窗看依赖与关联成果物；改派为 agent-aster 后刷新应保持
#    后端守卫：PATCH /api/tasks/{id}?status=APPROVED → 403 task_approval_requires_review
```