# CL-2 内容→审核→下游闭环 交接

> 交接状态：`PASS`
>
> 日期：2026-09-16
>
> 对应阶段：CL-2（工作项见 `docs/CONTENT_LIFECYCLE_PLAN.md` §5；决策见 §3）
>
> 上一份交接：`docs/handoffs/CL_1_OUTPUT_COLLECTION_HANDOFF.md`

---

## 1. 本阶段目标

从实施计划 §5 CL-2 抄写：

> **目标**：待审内容有去处、审核结论驱动下游、被拒后有明确修订路径。
>
> **验收标准**
> 1. 待审成果物在审核页可见并由成员批准 → `downstream_allowed=1`（可在任务里被引用）；
> 2. 退回后走「提交新版本」产生新版本且旧版本保持不可变；
> 3. 引用未审成果物作为下游输入时，领取校验仍然拦下（`task_dependencies_or_inputs_not_approved`）；
> 4. **回归**：`demo-1.0.ps1` 17 项仍全过。
>
> **退出条件**：审核→下游→修订三段都有可复现步骤。
>
> **Do NOT**：不要让"审核成果物"绕过风险门禁（`review_blocked_by_open_risks` 保持生效）。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| CL-2-01 待审成果物分区 | 完成 | 审核门禁页新增 `pending-artifact-list` 面板：列出全部 `PENDING_REVIEW` 成果物，含**来源任务标题 · 运行 id · 版本 · 类型 · 内容哈希**、状态与"未批准的原因"；`查看内容` 弹窗直接读 `GET /api/artifacts/{id}/content` 预览正文（`apps/web/app/review/page.tsx`） |
| CL-2-02 成果物审核 + 下游规则解释 | 完成 | 同一面板内 `批准 / 退回修订`，走既有 `POST /api/projects/{id}/reviews`（`target_type=artifact`）——批准 → `APPROVED + downstream_allowed=1 + immutable=1`，退回 → `REJECTED + downstream_allowed=0`；界面明确写出"未批准不能作为下游输入、也不会进交付包"；退回弹窗要求填写原因（写入复核记录）。风险门禁守卫保持生效（`review_blocked_by_open_risks` 由服务端判定，界面如实显示） |
| CL-2-03 退回 → 修订引导 | 完成 | 成果物页：每行显示"能否用于下游"（✓/× + 原因 + 哈希），`REJECTED` 行出现「提交新版本」；弹窗先读旧版本正文供修改，确认后 `POST /api/projects/{id}/artifacts/{id}/versions`（名称与类型必须一致）创建**待审**新版本，再把新内容写进**新版本**——旧版本原样保留 |
| CL-2-04 下游输入建议 | 完成 | 建任务表单新增「输入成果物（可选，可多选）」：只列**已批准且允许下游**的成果物（与服务端领取校验同一口径），显示版本与产出任务；`createTask` 通过 `input_artifacts` 提交；没有可用内容时给出口径说明 |

**实测中修正的一处**：`claim` 对"输入未批准的 READY 任务"的保护方式是**跳过**（`claim_next_task` 只挑依赖就绪的任务），不是返回 409。
验收脚本因此断言"该任务没有被领走"，而不是"报错"——界面侧由任务诊断层提示"等待上游/输入未批准"（CL-1 之前那轮 UX 工作已实现）。

## 3. 与计划的偏差

- **CL-2-04 只覆盖"建任务时"选择输入**：`PATCH /api/tasks/{id}` 目前不接受 `input_artifacts`（既有契约只有状态/负责人/执行方式），因此**给已存在的任务补输入还没做**。列为本阶段边界（见 §5）。
- **修订只支持文本内容**：`提交新版本` 的编辑框是 Markdown/文本；二进制产物（图片、PDF、压缩包）不走这条，需重跑任务由内核采集新版本（弹窗里已写明）。

其余逐条比对 §5 工作项表：范围与验收口径一致。

## 4. 测试与验证

```text
# 平台（本阶段未改后端，回归确认无回退）
cd apps/api && python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 323 tests, OK (skipped=14)

# 端到端主链路（不变式 I1）
pwsh -File scripts/demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove
→ 17 项全过

# 前端
cd apps/web && NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build
→ 19 页通过（/review 5.03 kB、/artifacts 3.55 kB、/tasks 12.4 kB）
```

**实机验收（浏览器 + 真待审内容，2026-09-16）**

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| ① 待审可见 + 批准解锁下游 | **通过** | 审核页列出 6 份真实待审内容（`second.md`/`result.md`/`agent_answer` 等），含来源任务与哈希；批准 `second.md` → 接口回读 `APPROVED · downstream_allowed=True · immutable=True · approved_by=member-001`，列表行变为"✓ 已批准：可以被下游任务引用" |
| ② 退回 → 新版本、旧版本不可变 | **通过** | 退回 `f3c42436….md`（v1）→ `REJECTED`；界面「提交新版本」创建 v2（`parent_artifact_id` 指向 v1、状态 `PENDING_REVIEW`）；再给 v2 写入新内容后：v2 哈希 `2bde331a→283b25e8`，**v1 哈希与状态一字未动** |
| ③ 未批准内容不能被下游用 | **通过** | 建任务引用未批准成果物 → `claim` 返回 `null`（该任务没被领走，任务停在 READY 并给出原因）；引用已批准成果物 → 同一条 claim 立即领走 |
| ④ 回归 | **通过** | API 323 项 OK、`demo-1.0.ps1` 17/17、前端 19 页构建通过 |

复现脚本：`scripts/cl2_acceptance_flow.py`（读临时凭证文件 `content_acceptance_credential.json`，**跑完请删除**：里面有项目能力 Token）。

## 5. 尚未完成与边界

1. **给已存在的任务补"输入成果物"还不行**（PATCH 不支持该字段）：只能在**建任务时**选；需要改既有任务时目前得新建任务。
2. **二进制产物的修订仍要走重跑**：界面修订只处理文本；图片/PDF 请重跑任务让内核采集新版本。
3. **成果物详情页还没有**（CL-3）：目前能看到来源/哈希/版本/下游可用性，但看不到"版本谱系、被谁引用过、边界策略、审核记录"的完整视图；归档入口也还没有（CL-4）。
4. **交付侧还没有"待处理内容"提示**（CL-5）：未批准内容不会进交付包，但交付页不会主动告诉你还差几份内容没批。
5. **直接给被退回版本覆盖内容（API 路径）不会自动回到待审**：界面走的是"提交新版本"（新建 `PENDING_REVIEW` 版本），这是被推荐且已验证的路径；若日后要走"同版本重交"，需要改存储端的状态语义（属状态机变更，需走决策追加）。

## 6. 下一步

- 下一阶段：**CL-3 内容可视化与追溯**（可与 CL-4 并行）
- 入口条件是否满足：**是**（CL-1 已退出，CL-2 三段闭环已验证）
- 建议的下一批工作项：`CL-3-01`（成果物详情：来源/版本谱系/哈希/边界/审核记录）、`CL-3-03`（Run 详情"产出"区跳转）、`CL-3-04`（时间线内容故事线）、`CL-4-01`（归档入口）、`CL-4-02`（孤儿标注）
- 现成数据：`/api/projects/{id}/artifacts` 已有 `parent_artifact_id`/`content_hash`/`downstream_allowed`/`task_id`/`run_id`；`reviews` 里有针对成果物的复核记录可用于"谁批的"。

## 7. 复现命令

```powershell
# 平台与内核（采集默认开）
python -u -X utf8 apps/agent/agentd.py daemon-run --url http://127.0.0.1:8010 --workspace <工作区> --state-dir <状态目录> --grant <授权串>

# 跑一个产生内容的任务（声明式命令最简单）
# 然后：/review 页 →「待审成果物」批准或退回；/artifacts 页 → 被退回的行点「提交新版本」
# 验证领取保护：建两个任务，一个引用未批准成果物、一个引用已批准成果物，观察 claim 只领走后者

# 回归
cd apps/api;  python -X utf8 -m unittest discover -s . -p "test_*.py"
pwsh -File scripts/demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove
cd apps/web;  NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build
```

## 8. 组件版本对照

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| 前端 | 0.1.0 | 改动集中在 `app/review/page.tsx`（待审成果物分区）、`app/artifacts/page.tsx`（来源/下游/修订）、`app/tasks/page.tsx`（输入成果物选择）、`lib/api.ts`（`createArtifactVersion`、`createTask` 支持 `input_artifacts`、Artifact 类型补 `parent_artifact_id`/`ARCHIVED`） |
| 平台 | 0.1.0 | **未改后端**：审核、版本、领取校验均为既有能力（本阶段只是把它们接到界面上） |
| 内核 | 0.1.0 | 未改 |
| sidecar 契约 / 协议 | v1 / 1.0 | 未变 |