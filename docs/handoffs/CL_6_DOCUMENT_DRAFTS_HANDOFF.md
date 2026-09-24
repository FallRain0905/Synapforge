# CL-6 文档协作持久化 交接（CL 主线收尾）

> 交接状态：`PASS`
>
> 日期：2026-09-16
>
> 对应阶段：CL-6（工作项见 `docs/CONTENT_LIFECYCLE_PLAN.md` §5）
>
> 上一份交接：`docs/handoffs/CL_5_DELIVERY_READINESS_HANDOFF.md`

---

## 1. 本阶段目标

从实施计划 §5 抄写：

> **目标**：协作编辑不再"刷新即丢"。
>
> **验收标准**：两人（双标签）编辑后刷新，内容仍在；冲突场景有明确提示且不丢数据。
>
> **退出条件**：刷新恢复有实测证据；`collaboration.py` 的中继语义未变（禁区）。
>
> **Do NOT**：不要改 WebSocket 中继协议；不要把协作中间态当"已提交版本"。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| CL-6-01 协作中间态节流落库 | 完成 | 新增 `document_drafts` 表（SQLite 建表 + PG 迁移 `016_document_drafts.sql`，含 RLS 策略）+ `DocumentDraft`/`DocumentDraftUpdate` 契约 + `GET/PUT /api/projects/{pid}/documents/{aid}/draft`；前端在编辑后**每 5 秒**（`scheduleDraftSave`）把文本写进服务端草稿，切文档/离开页面时兜底 flush 一次 |
| CL-6-01 刷新恢复 | 完成 | 打开文档时 `startCollaboration` **优先用服务端草稿做 Yjs 种子**（没有草稿才用平台内容），并显示「服务端草稿 rN · 最近保存时间 · 保存者」；"已保存"基线始终是**平台内容**，所以"未保存"判断不被草稿污染 |
| CL-6-02 冲突不静默覆盖 | 完成 | 保存带 `base_revision`（上次看到的修订号）：落后于服务端即 409 `document_draft_conflict`；界面弹出双侧对照（我的编辑 vs 服务端最新 + 来源人），提供「采用他人版本 / 保留我的版本并保存」两个选择，**不会自动覆盖任何一方** |
| CL-6-02 不可变守卫 | 完成 | 已批准（`APPROVED`/`immutable`）的内容不允许再存草稿（403 `approved_artifact_is_immutable`），与"保存草稿"的既有守卫一致 |
| CL-6-03 文案修正 | 完成 | 旧的"协作内容只存在于浏览器会话中（服务端不保存协作状态）"改为真实行为：自动存服务端草稿、刷新恢复、冲突时让你选 |
| 双实现一致 | 完成 | SQLite 与 PostgreSQL 两侧同语义实现（含冲突判定与不可变守卫），迁移清单测试同步更新 |

**执行中修掉一个自己引入的真 bug**：兜底 flush 的 effect 最初依赖 `collabText`，导致**每次按键**都触发一次 cleanup → 取消节流定时器并把**中间状态**写进服务端草稿（实测出现"编辑器 81 字、草稿 46 字"）。改为用 ref 记住最新文本、仅在真正卸载时 flush 一次；清掉错误草稿后重测通过。这个 bug 只有实机操作能发现（单测不会触发 React 的 effect 时序）。

## 3. 与计划的偏差

- **持久化的是文本快照，不是 CRDT 更新流**：计划只要求"协作中间态落库 + 刷新恢复"。实现选择把编辑器文本按节流写进草稿表（带修订号做冲突检测），**Yjs 增量更新仍只走 WS 中继、不落库**。这样 `collaboration.py` 的中继语义**零改动**（禁区），也避免把 CRDT 二进制更新流做成第二份事实来源。代价：多人同时编辑的中间态在刷新后以"最后保存的文本"为准（收敛仍由 Yjs 在会话内保证）。
- **冲突判定用修订号而不是内容哈希**：修订号更直观（rN），且能区分"我知道最新版本"与"我落后了"；内容相同但修订号领先时允许保存（幂等覆盖相同内容）。

## 4. 测试与验证

```text
# 平台（新增 4 项草稿契约测试）
cd apps/api && python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 330 tests, OK (skipped=14)          # CL-5 退出时为 326

# 内核（未改）
cd apps/agent && python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 281 tests, OK (skipped=9)

# 端到端主链路（不变式 I1）
pwsh -File scripts/demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove
→ 17 项全过

# 前端
cd apps/web && NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build
→ 19 页通过（/documents 38.3 kB）
```

**实机验收（浏览器真实编辑，2026-09-16）**

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| 节流落库 | **通过** | 编辑器里追加一行 → 状态显示「服务端草稿 r1 · 最近保存 18:18 · 保存者 member-001」；接口回读草稿 63 字且**含刚输入的文字** |
| 刷新恢复 | **通过** | 刷新页面后重新选中该文档 → 编辑器内容 63 字、**包含未保存的那一行**，状态条显示 r1 与保存时间（此前"刷新即丢"） |
| 冲突不覆盖 | **通过** | 用 API 模拟"另一位协作者"先保存（r1→r2，内容含其一行）；浏览器端（仍持 r1）继续编辑 → 节流保存撞 409 → 弹出对照框：「我的编辑（80 字）」vs「服务端最新（来自 member-001，61 字）」，两个按钮 |
| 冲突选择可用 | **通过** | 点「采用他人版本」→ 编辑器切到对方 61 字内容、弹窗关闭；另一条「保留我的版本并保存」会把我的内容存为新的服务端草稿（修订号已对齐到 r2） |
| 中继语义未变（禁区） | **通过** | `apps/api/app/collaboration.py` 本次**未改动**（git 无该文件变更；本阶段所有后端改动集中在 store/repository/postgres_repository/contracts/main 与迁移 016） |

## 5. 尚未完成与边界

1. **持久化粒度是"文本快照"**：见 §3；CRDT 更新流不落库，因此"两个离线客户端各自编辑后合并"这种场景仍需在线收敛。
2. **草稿没有清理策略**：正式保存（写回平台内容）后草稿行仍留着（修订号会重新起步）。后续可加"保存成功后删除草稿"或按时间清理。
3. **草稿不参与审核**：草稿是编辑中间态，`提交待审` 提交的仍是平台内容（符合 Do NOT）——但两者现在可能不一致（草稿比平台内容新），界面用"未保存"提示区分。
4. **CL 主线到此结束**（CL-0…CL-6 全部退出）。

## 6. 下一步

- CL 主线无剩余阶段。可选的后续（未安排）：
  - 把"最近一次编译产物"记成 `compiled_pdf` 成果物，让交付页的编译状态跨会话可见（CL-5 边界）；
  - 草稿清理策略与"草稿与平台内容不一致"的提示强化；
  - 若要把 CRDT 更新流也持久化，属新决策（会碰 `collaboration.py`，需走 D-CL 追加）。

## 7. 复现命令

```powershell
# 平台 + 前端
cd apps/api; ... uvicorn app.main:app --port 8010
cd apps/web; NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build; npx next start -p 3000

# 刷新恢复：/documents 选一份未批准文档 → 编辑 → 等 5 秒（状态条出现 rN）→ 刷新 → 重新选中 → 内容还在
# 冲突：另开终端执行（换成真实 artifact id）
#   curl -X PUT .../documents/<aid>/draft -d '{"content":"他人内容","base_revision":<当前 r>}'
#   然后在浏览器里继续编辑 → 5 秒后弹出对照选择框

# 回归
cd apps/api;   python -X utf8 -m unittest discover -s . -p "test_*.py"
cd apps/agent; python -X utf8 -m unittest discover -s . -p "test_*.py"
pwsh -File scripts/demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove
```

## 8. 组件版本对照

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| 平台 | 0.1.0 | 新增 `document_drafts` 表与迁移 `016`、`DocumentDraft`/`DocumentDraftUpdate` 契约、草稿读写端点；SQLite 与 PG 双侧同语义 |
| 前端 | 0.1.0 | 文档页：节流草稿保存、草稿恢复为协作种子、草稿状态条、冲突选择弹窗、文案修正 |
| 内核 | 0.1.0 | 未改 |
| 协作中继 | **未变** | `apps/api/app/collaboration.py` 逐字未动（禁区） |
| sidecar 契约 / 协议 | v1 / 1.0 | 未变 |