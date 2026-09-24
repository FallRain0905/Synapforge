# CL-3 内容可视化与追溯 + CL-4 回收与归档 交接

> 交接状态：`PASS`
>
> 日期：2026-09-16
>
> 对应阶段：CL-3、CL-4（工作项见 `docs/CONTENT_LIFECYCLE_PLAN.md` §5）
>
> 上一份交接：`docs/handoffs/CL_2_CONTENT_REVIEW_HANDOFF.md`

---

## 1. 本阶段目标

从实施计划 §5 抄写：

> **CL-3 目标**：任一成果物能回答"从哪来、有几版、谁批的、被谁用、边界是什么"。
> **CL-3 验收**：对 CL-1 产出的真实内容，四个视图（详情、引用、Run 产出区、时间线故事线）在浏览器里都能看到正确信息。
> **CL-3 退出**：四个视图均有实测证据。
> **CL-3 Do NOT**：不要把原始 JSONL 塞进成果物详情（那是 Run 的职责）。
>
> **CL-4 目标**：内容能退休，且不丢审计。
> **CL-4 验收**：归档后内容不再作为新任务输入（领取校验拦下）、历史引用仍可查；孤儿标注有实测样本。
> **CL-4 退出**：归档路径端到端可走通且不可逆操作有确认。
> **CL-4 Do NOT**：不要实现批量删除或"清空项目内容"。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| CL-3-01 成果物详情 | 完成 | 新增 `GET /api/projects/{pid}/artifacts/{aid}/detail`（契约 `ArtifactDetail`）+ 成果物页 `详情` 弹窗：**来源**（产出任务标题/执行 Agent/运行时间/内容哈希/创建者与其身份）、**能否用于下游**、**版本谱系**（同名称全部版本，标出当前查看、含各版本哈希与状态）、**复核记录**、是否孤儿 |
| CL-3-02 引用关系 | 完成 | 详情里的「被谁引用」= 把该成果物放进 `input_artifacts` 的任务（标题 + 状态）；后端用既有 `list_tasks` 过滤拼装 |
| CL-3-03 Run 产出区 | 完成 | `/runs` 执行详情新增「产出成果物（N）」区块：名称、版本、类型、内容哈希、当前状态与「去成果物库 →」 |
| CL-3-04 时间线内容故事线 | 完成 | 成果物详情弹窗提供「在时间线看它的事件」→ `/timeline?artifact=<id>`：只显示该内容相关事件（`object_id`/`payload.artifact_id`/**复核事件的 `payload.target_id`**），带提示条与「看完整事件流」出口；补齐 `artifact.archived` 等事件类型的中文标题 |
| CL-4-01 归档入口 | 完成 | 成果物页每行 `归档`（`ARCHIVED` 行不再显示），确认对话框写明后果："不再作为新任务输入，但内容、版本谱系与历史引用关系全部保留——这是退休，不是删除"；调用既有 `POST /api/artifacts/{id}/archive` |
| CL-4-02 孤儿口径 | 完成 | 列表与详情都标注「孤儿内容：来源任务已不存在（保留审计，不影响引用关系）」；后端在详情里给出 `orphan`/`orphan_reason`（来源任务或来源运行记录缺失），**只标注不清理**（D-CL-7） |
| CL-4-03 保留说明 | 完成 | 口径已写在 `ARTIFACT_LIFECYCLE.md`（CL-0-01 追加章节）与归档确认文案里 |

**实现取向**：详情端点**不新增 store 方法**，全部用已有只读方法在路由层拼装——这样 SQLite 与 PostgreSQL 两条实现天然一致，不必为 PG 再写一遍等价查询。

## 3. 与计划的偏差

- **CL-3-04 采用"按内容过滤时间线"而不是"另建聚合视图"**：计划只写"按成果物聚合其 `artifact.*` 事件（复用 `lib/events.ts`）"。实现为 URL 参数驱动的过滤（`/timeline?artifact=<id>`），复用了已有的按天分组、分类筛选与翻译层；比新造一个聚合面板更省、也避免同一份数据两处渲染。
- **归档后的门禁失效是既有行为**：归档会触发 `input_snapshot_changed` → 门禁置为失效（时间线上能看到"门禁失效"）。本阶段未改这条语义，只是在故事线里如实显示。

## 4. 测试与验证

```text
# 平台（新增 3 项成果物详情契约测试）
cd apps/api && python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 326 tests, OK (skipped=14)        # CL-2 退出时为 323

# 内核（本阶段未改）
cd apps/agent && python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 281 tests, OK (skipped=9)

# 端到端主链路（不变式 I1）
pwsh -File scripts/demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove
→ 17 项全过

# 前端
cd apps/web && NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build
→ 19 页通过（/artifacts 5.08 kB、/runs 7.14 kB、/timeline 7.55 kB）
```

**实机验收（浏览器 + 真实内容，2026-09-16）**

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| CL-3-01 详情（来源/谱系/复核） | **通过** | `second.md` 详情：产出任务「CL-1 产出验收 #4（重启后）」、执行 `agent-fallrain · 09/16 14:53`、哈希 `64937a5a96cd…`、创建者 `agent-fallrain（agent）`、"可以（已批准且允许下游引用）"、版本谱系 v1 APPROVED（当前查看）、复核记录 `APPROVED · member-001 · 人工复核通过` |
| CL-3-02 引用关系 | **通过** | 同一弹窗「被谁引用（2）」列出两个引用它的任务及其状态 |
| CL-3-03 Run 产出区 | **通过** | `/runs` 执行详情显示「产出成果物」：`second.md · v1 · paper_source · 哈希 64937a5a96… · ARCHIVED · 去成果物库 →`（归档状态如实反映） |
| CL-3-04 内容故事线 | **通过** | `/timeline?artifact=306931bd…` 显示该内容 5 条事件：门禁失效（归档触发）→ **成果物已归档** → 提交审核结论（通过）→ 内容已落库 → 新增成果物；分类计数"审核与证据 2 / 成果物 3"一致 |
| CL-4-01 归档 | **通过** | 归档确认写明后果 → 执行后行变为 `× 已归档：保留审计，但不再作为新任务输入 · ARCHIVED`，归档按钮消失；`/api/projects/{id}/artifacts` 回读 `status=ARCHIVED`、`downstream_allowed=false` |
| CL-4-02 孤儿标注 | **通过** | 删掉一个来源任务后（测试数据），列表出现 3 个「孤儿内容：来源任务已不存在」标注，内容与引用关系仍在；接口 `orphan=true`、`orphan_reason="来源任务已不存在"` |

## 5. 尚未完成与边界

1. **CL-5 交付就绪度未做**：交付页仍不会主动提示"还有 N 份内容未批准、不会进提交包"（后端 `delivery.assemble_paper` 已经算出 `excluded`，只差界面展示）。
2. **孤儿只标注不处理**：没有"重新挂到某个任务"或"批量归档孤儿"的操作（D-CL-7 明确不做清理；是否要提供"认领"入口待定）。
3. **详情里的边界信息只显示哈希与状态**：`data_policy`/信息边界的逐项解释还没展开（Run 详情里有边界结论，成果物详情只给结论）。
4. **时间线过滤只能从详情跳转**：没有独立的"按内容筛选"下拉（列表页选一份 → 详情 → 看事件）。
5. **归档不可逆**：没有"解除归档"（`ARCHIVED` 在状态机里是终态）。若日后需要恢复，属状态机变更，要走决策追加。

## 6. 下一步

- 下一阶段：**CL-5 交付就绪度**（CL 主线的最后一个功能阶段）
- 入口条件是否满足：**是**（CL-2 已退出；CL-1 的内容已在成果物库里可追溯）
- 建议的下一批工作项：`CL-5-01`（就绪度面板：论文结构/图表/字数/未审成果物数/门禁/编译状态）、`CL-5-02`（每项直达入口）、`CL-5-03`（把后端已算出的 `excluded` 与 `blocked_reasons` 显示出来）
- 之后是 **CL-6 文档协作持久化**（独立，可与 CL-5 并行）

## 7. 复现命令

```powershell
# 平台 + 前端
cd apps/api; ... uvicorn app.main:app --port 8010
cd apps/web; NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build; npx next start -p 3000

# 看四个视图
#   /artifacts → 任意内容「详情」；「在时间线看它的事件」；行内「归档」
#   /runs → 任意执行「详情」→「产出成果物」
#   /timeline?artifact=<成果物 id> → 该内容的完整故事线

# 孤儿样本（只影响开发库）
#   sqlite3 apps/api/data/platform.db "DELETE FROM tasks WHERE id='<产出该内容的任务>'" → 刷新 /artifacts

# 回归
cd apps/api;   python -X utf8 -m unittest discover -s . -p "test_*.py"
cd apps/agent; python -X utf8 -m unittest discover -s . -p "test_*.py"
pwsh -File scripts/demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove
```

## 8. 组件版本对照

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| 平台 | 0.1.0 | 新增 `ArtifactDetail`/`ArtifactLineageEntry`/`ArtifactConsumer` 契约与只读详情端点；**未改状态机** |
| 前端 | 0.1.0 | 成果物页详情弹窗 + 归档 + 孤儿标注；Run 详情产出区；时间线按内容过滤；事件翻译补 `artifact.archived` 等 |
| 内核 | 0.1.0 | 未改 |
| sidecar 契约 / 协议 | v1 / 1.0 | 未变 |