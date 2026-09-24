# UX-6 协作正确性 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-16
>
> 对应阶段：UX-6（工作项定义见 `docs/DEMO_1_0_IMPLEMENTATION_PLAN.md` §5 UX-6）
>
> 上一份交接：`docs/handoffs/UX_5_AGENT_TASK_LOOP_HANDOFF.md`

---

## 1. 本阶段目标

从计划书 §5 UX-6 抄写：

> **目标**：把界面上已经承诺、但行为不符的三处对齐：协作编辑、门禁批准、交接收据。
>
> **入口条件**：UX-1 退出（可并行于 UX-4/5）。
>
> **退出条件**：`docs/IMPLEMENTATION_STATUS.md` 中"协作编辑不保存"这条已知边界被移除。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| UX-6-01 协作编辑显式保存 | 完成 | `lib/api.ts` 新增 `saveArtifactText()`（multipart + `Idempotency-Key`，把 `approved_artifact_is_immutable` 映射为"文档已批准，内容不可再修改；请先派生新草稿"）。`app/documents/page.tsx`：编辑区头部新增「保存草稿」按钮、`有未保存的修改 / 已保存 HH:MM / 与平台内容一致` 状态（与打开时的平台内容比较），已批准文档禁用编辑并说明原因。**文案纠偏**：原提示"定稿请用「提交待审」写入正式版本"是错的（提交冻结的是平台已存内容，会丢掉未保存的编辑），现明确写"协作内容只存在于浏览器会话中（服务端不保存协作状态）：点「保存草稿」写回平台，「提交待审」把已保存的内容冻结为提交版本" |
| UX-6-02 门禁人工批准 | 完成 | `lib/api.ts` 新增 `submitReview()`（门禁批准的**唯一**入口）并把服务端守卫映射成可读原因：`review_blocked_by_open_risks`、`task_not_waiting_for_review`、`approved_task_is_immutable`、`human_approval_required`、`rejected_handoff_requires_revision`。`app/review/page.tsx`：门禁卡片改为显示**目标标题**（任务标题/成果物名/交接目标，而非 UUID）、待确认门禁提供「批准」「退回修订」，批准走 `ConfirmDialog` 说明后果（门禁派生 PASSED、成果物变不可修改），退回修订要求填写原因；卡片上提前提示"该目标还有 N 条未关闭的严重风险，服务端会拒绝批准"。**项目级门禁不给按钮**并说明"由程序规则派生，不能通过复核直接批准"（复核只接受 task/artifact/handoff） |
| UX-6-03 交接收据 | 完成 | `lib/api.ts` 新增 `acceptHandoff()/rejectHandoff()`（映射 `handoff_receiver_mismatch`、`handoff_rejected_requires_revision`、`handoff_not_acceptible`）。`app/handoffs/page.tsx`：逐条待确认收据给出「接受」「拒绝」（按钮 title 写明"以 `<receiver_id>` 身份"），接受走确认弹窗，拒绝要求填写原因（写入复核记录）；拒绝原因已存在时在卡片上显示 |
| UX-6-04 项目知识库 | 完成 | `app/kb/page.tsx` 新建弹窗新增归属选择（个人库/项目库 chips + 说明"项目库复用项目授权…退出项目即失去访问"），创建时把 `project_id` 传给 `createKb`；无项目时项目库选项禁用。弹窗副标题改为"与个人云盘不同：知识库只存文档，用于检索与超图索引" |
| UX-6-05 危险操作确认 | 完成 | 四处接入 `ConfirmDialog`：云盘删除文件（`drive-delete-confirm`）、删除 AI 会话（`ask-delete-confirm`）、模板包落库门禁（`pack-gate-confirm`，说明会写入门禁并可能阻断下游）、交付生成提交包（`delivery-bundle-confirm`，说明进入 submitted 层待人工批准）。同时把这几处的裸 `catch` 换成 `errorMessage` |

## 3. 与计划的偏差

**有偏差，共 2 处：**

1. **门禁卡片的信息量超出计划。** 计划只要求"加 Pass/Approve（人工动作），失败展示服务端原因"。实际还做了：目标标题解析（UUID → 任务名/成果物名）、批准前风险预检提示、以及"项目级门禁不可复核"的显式说明。理由：这三种信息缺失时，用户点批准要么分不清批的是什么，要么撞一个必然失败的请求。
2. **删除了 `/review` 页原先的"通过"语义残留。** UX-2 已把任务页的非法 `WAITING_REVIEW → APPROVED` 按钮换成「去审核」链接；本阶段把审核页做成真正的批准入口，两处终于对齐（批准只从 `/review` 发生）。

## 4. 测试与验证

**基线命令（计划书 §7）**

```text
后端：python -X utf8 -m unittest discover -s . -p "test_*.py"（apps/api）
      → Ran 282 tests, OK (skipped=13)，115.8s（本阶段未改后端）
Agent：apps/agent 全量 → 164 tests, OK (skipped=9)（本阶段未改 agent）
前端：cd apps/web && npm run build → 17 个页面全部静态预渲染；npx tsc --noEmit 无错误
      /documents 37.4 kB、/review 3.61 kB、/handoffs 2.15 kB、/kb 3.17 kB、/drive 3.4 kB、/ask 3.48 kB、/pack 4.93 kB、/delivery 3.52 kB
```

**浏览器端到端验收（真实前端 + 真实后端 + 独立临时库）**

| 验收标准 | 操作 | 结果 |
| --- | --- | --- |
| **协作编辑内容真的落库** | 打开 `PAPER_OUTLINE.md`（1869 字节）→ 键盘输入标记 → 保存草稿 → **刷新页面**再打开 | 输入后状态变「有未保存的修改」且按钮可用；保存后变「已保存 00:02」；刷新后内容长度为 **1886**（含标记）——此前刷新即丢 |
| 门禁可在界面批准 | `/review` 点「批准」→ 确认 | 卡片显示真实任务名「题面事实台账与数据画像」而非 UUID；确认弹窗说明后果；批准后门禁 **FAILED → PASSED**、指标「待确认 1 → 0」 |
| 交接收据可操作 | `/handoffs` 点「接受」→ 确认 | 收据 `member-001 · PENDING → ACCEPTED`；指标「已接受 0 → 1、待确认 1 → 0」；接受后该条不再显示操作按钮 |
| 项目知识库 | `/kb` 新建 → 点「项目知识库」→ 创建 | 提示文案正确（"项目库复用项目授权…"）；toast「项目知识库已创建」；列表出现该库 |
| 危险操作二次确认 | `/pack` 点「落库门禁」 | 弹出 `pack-gate-confirm`（说明会写入门禁并可能阻断下游），取消后不执行；`pack-apply-confirm` 回归仍在 |

## 5. 尚未完成与边界

- **协作编辑仍是"单机草稿"模型**：服务端不保存协作状态（决策 D8 明确不引入 Yjs 持久化），因此多人同时编辑时，**只有点「保存草稿」的人**会把内容写回；他人未保存的改动不会被保存。界面已如实说明，但多人并发保存仍是"后写覆盖先写"。
- **保存不产生版本**：`store_artifact_content` 覆盖当前内容（不新增 revision），只有「提交待审」产生版本。因此"保存草稿"不留历史——需要历史请用「快照」或「派生新草稿」。
- **交接收据的"以谁的身份"未做身份选择**：按钮按 pending 收据逐条给出并标注 receiver_id，但操作者始终是开发模式的 `member-001`；点错会得到"这份交接不是发给你的"。真实部署接入身份服务后应改为只显示"发给我的"收据。
- **门禁的"退回修订"不产生风险记录**：只写 Review + 门禁快照（服务端 `create_review` 的行为），要登记具体风险需要传 findings——UI 目前不采集。
- **`/review` 的复核意见列表仍是只读展示**（不显示 findings 明细），门禁的 rules/review_ids/evidence_ids 未展开。
- **`delivery-bundle-confirm` 与 `ask-delete-confirm` 未实操点击**：代码路径与 `drive`/`pack` 完全同构（同一组件、同一 state 模式），本轮验收抽验了 pack 与 drive 两处；ask/delivery 建议在 UX-7 的端到端演示脚本里覆盖。

## 6. 下一步

- 下一阶段：**UX-7 打磨与 Demo 1.0 验收**（入口条件：UX-1 至 UX-6 全部退出，现已满足）
- 建议的下一批工作项：`UX-7-01`（加载态全覆盖）、`UX-7-02`（死代码清理或接线）、`UX-7-03`（设置页"测试连接"）、`UX-7-04`（答辩提纲可下载；`/graph` 的"实体管理/关系管理"改名）、`UX-7-05`（`scripts/demo-1.0.ps1` 端到端演示脚本，覆盖本轮未实操的两处确认）、`UX-7-06`（验收清单 `docs/DEMO_1_0_ACCEPTANCE.md`）、`UX-7-07`（README 新手视角重写 + 状态归档）
- 提醒：UX-7-05 的演示脚本应把 UX-6 的四处确认与 UX-5 的 worker 执行串起来，作为 Demo 1.0 的最终验收证据。

## 7. 复现命令

```powershell
# 1) 基线
cd apps\api; $env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"     # Ran 282 tests, OK (skipped=13)
cd ..\web; npm run build                                     # 17 个页面

# 2) 端到端
python -X utf8 -m uvicorn scripts.acceptance_empty_api:app --host 127.0.0.1 --port 8010
cd apps\web; $env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8010"; npx next dev -p 3014

# 3) 走查要点
#    /documents → 选文档 → 输入 → 保存草稿 → 刷新后内容仍在
#    /review    → 门禁卡片点「批准」→ 状态变 PASSED
#    /handoffs  → 待确认收据点「接受」→ 变 ACCEPTED
#    /kb        → 新建时选「项目知识库」
#    /pack      → 点「落库门禁」应弹确认
```