# 内容生命周期与体验完善 · 实施计划（CL 主线）

> 日期：2026-09-16
>
> 版本：v1（阶段 `CL-0` … `CL-6`）
>
> 状态：**PLAN**（待开工；§10 有三件需要拍板的事，已给推荐默认值）
>
> 上游：`docs/ARTIFACT_LIFECYCLE.md`（状态机，**不改语义**）、`docs/DESKTOP_CLIENT_IMPLEMENTATION_PLAN.md`（DP 主线，DP-2 已退出）、`docs/DEMO_1_0_IMPLEMENTATION_PLAN.md`（Demo 1.0，已收尾）
>
> 前置结论：**"回答与输出同步到平台"已完成**（见 `IMPLEMENTATION_STATUS.md` 2026-09-16 两条）。本计划解决它剩下的半截——内容进了平台，但还没有**生命周期**。

---

## 0. 本文档的用法（防偏移的第一道闸）

**编号空间**：`CL-*` = 本主线阶段与工作项；`D-CL-*` = 本主线的冻结决策；`I1–I6` = 不变式。与 `UX-*`（Demo 1.0）、`DP-*`（桌面端）、`D#/B#`（架构规划）**互不占用**。

**两条硬性使用规则**

1. **开工前先读**：§1（现状与证据）、§3（冻结决策）、§5（你所在阶段的细则）。上下文被压缩后按 §6.1 的恢复协议回来。
2. **决策不可改写**：要改走 §6.5 追加 `D-CL-11`，并在原决策行标注「被 D-CL-x 取代」。

**基线必须先跑**（§7）：Agent 259 / API 321 / `demo-1.0.ps1` 17 项 / 前端 19 页——不达标不进入任何阶段。

---

## 1. 任务背景与现状证据

### 1.1 一句话

平台的**成果物状态机、审核联动、交付管线早就写好了**，但**执行产出从来没有进来过**：
常驻内核跑完一个任务，除了 Run 里的摘要与 stdout，工作区里真实生成的文件没有任何一条路径进入成果物库。
于是"内容生命周期"在界面上看着像有的（成果物库、文档版本、交付包），实际上是空的或只有模板骨架。

### 1.2 现状证据（代码 + 现场）

| 能力 | 现状 | 证据位置 |
| --- | --- | --- |
| 成果物状态机 | **已实现**：`DRAFT/PENDING_REVIEW → APPROVED / REJECTED → ARCHIVED`；`APPROVED` 需 member 审核、`downstream_allowed=1`、`immutable=1` | `docs/ARTIFACT_LIFECYCLE.md`、`store.py:3222`（审核联动）、`store.py:2509`（内容上传置待审）、`store.py:2579`（归档） |
| 产出采集与上传（内核侧） | **已实现但没接线**：`OutputDiscovery`（工作区发现+哈希+MIME）、`RunManifestBuilder`（运行清单）、`ResultUploader`（本地持久化上传队列）、`AgentArtifactClient` | `apps/agent/result_uploader.py`；仅在 `agentd.session_worker_run`（Windows 会话 Worker）里被实例化（`agentd.py:911`） |
| 常驻任务循环 | **不采集产出**：`TaskLoop._run_assignment` 只上报 `summary/stdout/stderr`，从不创建成果物、不填 `output_artifact_ids` | `apps/agent/task_loop.py`；`RunComplete`/`TaskResultSubmit` 都有 `output_artifact_ids` 字段（`contracts.py:278`、`:327`）但传的是空数组 |
| Agent 端成果物接口 | **已实现**：创建 + 内容上传 + 分片上传（含 `artifact.write` 能力校验） | `main.py:1999`、`:2050`、`:2082`–`:2122` |
| 回答（上一轮新增） | 已进 Run 的 `summary`（回答在前）与 `/runs` 详情，**但不是内容**：无版本、不能审核、不进交付 | `IMPLEMENTATION_STATUS.md` 2026-09-16「Agent 回答与原始输出同步到平台」 |
| 交付管线 | **已实现**：装配/检查/编译/提交包/校验，只认 `APPROVED` 内容；并且**已经算出**被排除项（`excluded`）与阻塞原因（`blocked_reasons`）——只是界面没展示 | `main.py:1400`–`:1510`、`delivery.py:126`（excluded）、`:123`（blocked_reasons） |
| 文档三层版本 | **已实现**：草稿→提交→批准，差异/合并/证据/快照齐全；**协作中间态只在浏览器会话**（点「保存草稿」才写回） | `docs/DOCUMENTS` 相关页面 + `documents/page.tsx:370` 的提示文案 |
| 回收 | 只有归档端点与状态，**没有界面入口**；孤儿内容无口径 | `store.py:2579`、无前端调用点 |

### 1.3 因此用户看到的是（体验侧）

- **成果物库**：平铺列表，看不出"这份东西是谁在哪个任务/哪次执行里产出的、有几个版本、能不能被下游用、被谁引用过"。
- **执行详情**：能看到回答与原始输出，看不到"这次执行产出了哪些文件"。
- **交付页**：能编译、能导出，但不告诉你"还差什么才算能交"（缺结构、缺图表、有未审核内容）。
- **审核门禁**：能审任务/交接，但**待审成果物**没有专门的入口与"为什么不能用于下游"的解释。
- **时间线**：内容相关事件（`artifact.created/revised/submitted`）混在事件流里，没有形成"内容故事线"。

---

## 2. 目标与非目标

### 2.1 目标（可验收口径）

1. **执行产出自动成为内容**：内核跑完任务后，本次新增/修改的工作区文件与回答自动进入成果物库（`PENDING_REVIEW`），并回填到 Run 与任务结果；
2. **内容能被审核并解锁下游**：待审成果物有明确入口，审核结论驱动 `downstream_allowed`，被拒后的修订路径清楚（新版本而非覆盖）；
3. **内容可追溯**：任一成果物能回答"从哪来（任务/运行/设备）、有几版、谁批的、被谁用、边界是什么"；
4. **交付就绪度可判定**：交付页给出"还差什么"和直达入口；
5. **不回归**：Demo 1.0 与三套回归保持全绿（I1）。

### 2.2 非目标（本轮明确不做）

- **不做工作区镜像**：不把工作区同步成对象存储（只按清单采产出，D-CL-5）；
- **不做硬删除**：内容回收一律归档，保留审计（D-CL-7）；
- **不做内容级权限**（谁能看哪份成果物）：沿用项目成员制，权限模型另议；
- **不做富文本/在线表格**：文档仍是 Markdown 三层版本；
- **不改 `ARTIFACT_LIFECYCLE.md` 的状态机语义**（I2）；
- **不做跨项目内容共享**：成果物仍归属单一项目。

---

## 3. 冻结决策

本主线期间视为冻结基线，变更走 §6.5。

| # | 决策 | 内容与理由 | 来源 |
| --- | --- | --- | --- |
| D-CL-1 | **产出即成果物，但只到「待审」** | 执行产出以 `PENDING_REVIEW` 入库，绝不自动 `APPROVED`：内容能否进下游/交付必须有人签字（沿用现状态机，I2） | 本计划 |
| D-CL-2 | **采集范围 = 本次执行新增/修改的工作区文件** | 执行前后各取一次文件清单（路径+大小+mtime），差集即产出；排除 `.git`、`node_modules`、`.next`、`__pycache__`、`.venv`、`dist*`、`data/*.db` 等；清单由 `RunManifestBuilder` 落进运行清单 | 本计划 |
| D-CL-3 | **回答也作为内容入库**（`artifact_type=agent_answer`） | 回答是可交付内容（结论、推导、说明），值得有版本与审核；内容为 Markdown 全文。默认开启，任务可用 `resource_policy.inline_answer_artifact=false` 关闭 | 上一轮遗留建议 |
| D-CL-4 | **同路径再次产出 = 新版本，不覆盖** | 沿用「APPROVED 不可覆盖」；修订产生新内容哈希或新版本记录 | `ARTIFACT_LIFECYCLE.md` |
| D-CL-5 | **平台不做工作区镜像 / 全盘扫描** | 只接收清单里列出的产出；内核不读工作区之外的路径（I3、I5） | 本计划 |
| D-CL-6 | **交付只认 `APPROVED` 且 `downstream_allowed`** | 沿用现有过滤；未审内容出现在交付页只作为"待处理"提示 | `ARTIFACT_LIFECYCLE.md` |
| D-CL-7 | **回收用归档**（`ARCHIVED`），不做硬删 | 审计与引用关系保留；"删除"不是本轮目标。孤儿内容（任务/项目已不存在）只标记，不清理 | 本计划 |
| D-CL-8 | **上传在任务循环内同步完成**，失败由本地队列重试 | 不引入独立上传守护线程：上传失败**不改变任务成败**（I4），队列保留待重试；下一次循环启动时补传 | 本计划 |
| D-CL-9 | **文件大小分级**：≤8MB 直传 / 8–100MB 分片 / >100MB 只登记（路径+哈希+MIME，不上传） | 避免把工作区当对象存储；超限情况在 Run 摘要里写明 | 本计划 |
| D-CL-10 | **不新造事件类型**：内容相关事件沿用 `artifact.*` / `run.*` / `task.*` | 时间线与看板已能消费既有类型；新类型等于让所有消费方追着改 | 本计划 |

---

## 4. 阶段总览与依赖

```text
CL-0 基线与口径冻结
      │
      ├─> CL-1 执行产出落库（含回答入库）   ← 关键路径，价值最大
      │         │
      │         ├─> CL-2 内容→审核→下游闭环
      │         │         │
      │         │         └─> CL-5 交付就绪度
      │         │
      │         └─> CL-3 内容可视化（成果物详情 / Run 产出区 / 时间线故事线）
      │
      └─> CL-4 回收与归档（可与 CL-2 并行）
CL-6 文档协作持久化（独立，可与 CL-3 并行）
```

依赖是入口条件而非建议：`CL-1` 未退出不开始 `CL-2/CL-3`（没有真实内容就没有可审、可看的东西）。

| 阶段 | 名称 | 人日 |
| --- | --- | --- |
| CL-0 | 基线与口径冻结 | 0.5 |
| CL-1 | 执行产出落库（产出文件 + 回答） | 2.0 |
| CL-2 | 内容→审核→下游闭环 | 1.5 |
| CL-3 | 内容可视化与追溯 | 2.0 |
| CL-4 | 回收与归档 | 1.0 |
| CL-5 | 交付就绪度 | 1.5 |
| CL-6 | 文档协作持久化 | 1.5 |
| | **合计** | **10.0** |

关键路径：`CL-0 → CL-1 → CL-2 → CL-5`（≈5.5 人日到"内容能审核、能进交付"）。

---

## 5. 阶段细则

### CL-0：基线与口径冻结

**目标**：把现状写成可断言的口径，避免后面"以为改了其实是另一件事"。

**入口条件**：无（随时）。

| 编号 | 工作项 | 关键文件 |
| --- | --- | --- |
| CL-0-01 | `ARTIFACT_LIFECYCLE.md` 增补两节：**产出采集口径**（D-CL-2/9）与**回答入库口径**（D-CL-3），只增不删既有语义 | `docs/ARTIFACT_LIFECYCLE.md` |
| CL-0-02 | 把 §1.2 的现状表写进 `docs/IMPLEMENTATION_STATUS.md`（含"机制齐了没接线"的结论） | `docs/IMPLEMENTATION_STATUS.md` |
| CL-0-03 | 跑 §7 基线并记录数字（Agent / API / demo / 前端） | — |

**验收**：两节文档已合并；基线数字与 §7 一致。
**退出条件**：口径文档可被 CL-1 直接引用。
**Do NOT**：不要在这一步改状态机或加接口。

---

### CL-1：执行产出落库（产出文件 + 回答）

**目标**：跑完一个任务，本次产出的文件与回答自动出现在成果物库（待审），并挂到对应的 Run 上。

**入口条件**：CL-0 退出。

| 编号 | 工作项 | 对应 | 关键文件 |
| --- | --- | --- | --- |
| CL-1-01 | 工作区快照差分：执行前后各扫一次（剪枝规则见 D-CL-2），得到新增/修改文件集合 | D-CL-2 | 新增 `apps/agent/workspace_scan.py` |
| CL-1-02 | 任务循环接上传：产出 → `ResultUploader.queue_outputs` → `upload_pending`（大小分级 D-CL-9），带 `task_id`/`run_id`/`content_hash` | D-CL-1/8/9 | `apps/agent/task_loop.py`、`result_uploader.py`（复用） |
| CL-1-03 | 回答入库：`artifact_type=agent_answer`，内容为回答 Markdown；开关 `resource_policy.inline_answer_artifact` | D-CL-3 | `apps/agent/task_loop.py`、`codex_executor.py` |
| CL-1-04 | 回填引用：`/api/runs/{id}/complete` 带 `output_artifact_ids`；`/api/tasks/{id}/result` 同步 | — | `task_loop.py` |
| CL-1-05 | 摘要与过程事件如实写明产出：`产出 N 个成果物（待审）`／超限文件只登记 | D-CL-9、I4 | `codex_executor.summarize_codex_result` |
| CL-1-06 | 失败不判死：上传失败只记 `report_errors` 与本地队列，不改任务成败 | D-CL-8、I4 | `task_loop.py` |

**验收标准**

1. 真跑一个 `workspace-write` 沙箱的 Codex 任务产出文件 → 成果物库出现该文件（`PENDING_REVIEW`，来源任务/运行正确）；
2. Run 详情"产出成果物"数量 = 实际产出数；回答作为 `agent_answer` 出现；
3. 断网（停止 API）跑一轮 → 任务仍成功，恢复后本地队列补传成功；
4. **回归**：Agent / API 套件全绿且数量不减（新增测试计入）。

**退出条件**：产出与回答都在平台上可查到；上传失败不影响任务成败（有测试）。
**Do NOT**：不要自动 `APPROVED`；不要把整个工作区传上去（D-CL-5）；不要为了上传而改任务状态机。

---

### CL-2：内容→审核→下游闭环

**目标**：待审内容有去处、审核结论驱动下游、被拒后有明确修订路径。

**入口条件**：CL-1 退出。

| 编号 | 工作项 | 关键文件 |
| --- | --- | --- |
| CL-2-01 | 审核门禁页新增「待审成果物」分区（含来源任务/运行、内容预览、哈希） | `apps/web/app/review/page.tsx` |
| CL-2-02 | 成果物审核：`target_type=artifact` 的批准/退回（后端已有），界面补"为什么不能用于下游"的解释 | `review/page.tsx`、`lib/api.ts` |
| CL-2-03 | 退回后修订引导：`REJECTED` → 「提交新版本」（复用 `artifacts/{id}/versions`），并说明"覆盖被禁止" | `apps/web/app/artifacts/page.tsx` |
| CL-2-04 | 下游输入建议：建/改任务时，列出上游任务已 `APPROVED` 的产出可直接作为 `input_artifacts` | `apps/web/app/tasks/page.tsx` |

**验收标准**

1. 待审成果物在审核页可见并由成员批准 → `downstream_allowed=1`（可在任务里被引用）；
2. 退回后走「提交新版本」产生新版本且旧版本保持不可变；
3. 引用未审成果物作为下游输入时，领取校验仍然拦下（`task_dependencies_or_inputs_not_approved`）；
4. **回归**：`demo-1.0.ps1` 17 项仍全过。

**退出条件**：审核→下游→修订三段都有可复现步骤。
**Do NOT**：不要让"审核成果物"绕过风险门禁（`review_blocked_by_open_risks` 保持生效）。

---

### CL-3：内容可视化与追溯

**目标**：任一成果物能回答"从哪来、有几版、谁批的、被谁用、边界是什么"。

**入口条件**：CL-1 退出（CL-2 可并行）。

| 编号 | 工作项 | 关键文件 |
| --- | --- | --- |
| CL-3-01 | 成果物详情：来源（任务/运行/设备/Agent）、版本谱系、内容哈希、`data_policy`/边界、审核记录 | `apps/web/app/artifacts/page.tsx` |
| CL-3-02 | 引用关系：被哪些任务当作输入、产出了哪些交接/交付包 | 同上；后端已有的是「文档 ↔ 结论/图表/运行/任务」关系（`/api/projects/{pid}/documents/{artifact_id}/relations`），成果物维度的反查需要新增只读查询 |
| CL-3-03 | Run 详情"产出"区：跳转到对应成果物 | `apps/web/app/runs/page.tsx` |
| CL-3-04 | 时间线内容故事线：按成果物聚合其 `artifact.*` 事件（复用 `lib/events.ts`） | `apps/web/app/timeline/page.tsx` |

**验收标准**：对 CL-1 产出的真实内容，上述四处在浏览器里都能看到正确信息；时间线筛选「成果物」能定位到该内容的故事线。
**退出条件**：四个视图均有实测证据。
**Do NOT**：不要把原始 JSONL 塞进成果物详情（那是 Run 的职责）。

---

### CL-4：回收与归档

**目标**：内容能退休，且不丢审计。

**入口条件**：CL-1 退出（可与 CL-2/CL-3 并行）。

| 编号 | 工作项 | 关键文件 |
| --- | --- | --- |
| CL-4-01 | 归档入口：成果物详情「归档」（`POST /api/artifacts/{id}/archive`，需确认对话框写明后果） | `artifacts/page.tsx` |
| CL-4-02 | 孤儿口径：任务/项目已不存在的内容在列表里标注「孤儿（来源已删除）」，只提示不清理 | `store.py`（只读查询）、`artifacts/page.tsx` |
| CL-4-03 | 保留说明写进 `ARTIFACT_LIFECYCLE.md`：归档 ≠ 删除；本轮不做硬删（D-CL-7） | `docs/ARTIFACT_LIFECYCLE.md` |

**验收标准**：归档后内容不再作为新任务输入（领取校验拦下）、历史引用仍可查；孤儿标注有实测样本。
**退出条件**：归档路径端到端可走通且不可逆操作有确认。
**Do NOT**：不要实现批量删除或"清空项目内容"。

---

### CL-5：交付就绪度

**目标**：交付页直接回答"还差什么才能交"。

**入口条件**：CL-2 退出。

| 编号 | 工作项 | 关键文件 |
| --- | --- | --- |
| CL-5-01 | 就绪度面板：论文结构完整性（超结构/摘要/关键词/参考文献）、图表数量、字数、未审核成果物数、门禁状态、编译状态 | `apps/web/app/delivery/page.tsx` |
| CL-5-02 | 每项给一个直达入口（去补内容 / 去审核 / 去编译） | 同上 |
| CL-5-03 | 提交包生成前把"将被排除的内容"显示出来（后端 `delivery.assemble_paper` 已经算出 `excluded` 与 `blocked_reasons`，界面此前没展示） | `apps/web/app/delivery/page.tsx`（读已有字段） |

**验收标准**：故意留一份未审成果物 → 面板显示"1 份内容未审核，不会进提交包"并能直达；全部就绪时显示可提交。
**退出条件**：就绪度判定与实际提交包内容一致（用提交包校验接口交叉验证）。
**Do NOT**：不要让就绪度面板变成"必须全绿才能导出"的硬门禁（导出仍可强制，只要如实提示）。

---

### CL-6：文档协作持久化

**目标**：协作编辑不再"刷新即丢"。

**入口条件**：无（可与 CL-3 并行）。

| 编号 | 工作项 | 关键文件 |
| --- | --- | --- |
| CL-6-01 | 协作中间态节流落库（如 5 秒或 200 字触发一次），刷新后从服务端恢复 | `apps/web/lib/collab.ts`、`apps/api/app/collaboration.py`（**只加不改成帧语义**）、新增存储 |
| CL-6-02 | 冲突提示：他人已保存更新时给出"以谁为准"的选择，不静默覆盖 | `apps/web/app/documents/page.tsx` |
| CL-6-03 | 文案修正：把"服务端不保存协作状态"改成真实行为 | 同上 |

**验收标准**：两人（双标签）编辑后刷新，内容仍在；冲突场景有明确提示且不丢数据。
**退出条件**：刷新恢复有实测证据；`collaboration.py` 的中继语义未变（禁区）。
**Do NOT**：不要改 WebSocket 中继协议；不要把协作中间态当"已提交版本"。

---

## 6. 防偏移机制

### 6.1 上下文压缩后的恢复协议

1. 读本文件 §0 → §3 → 你在的阶段；2. 读 `docs/IMPLEMENTATION_STATUS.md` 里 `CL-*` 的最近登记；3. 读最近一份 `docs/handoffs/CL_*_HANDOFF.md`（有则读它的"尚未完成与边界"）；4. 跑 §7 基线确认现状；5. 从交接里列出的"下一批工作项"继续，**不重新讨论已冻结决策**。

### 6.2 术语与编号冻结

`产出口径`=D-CL-2；`回答入库`=D-CL-3；`待审`=`PENDING_REVIEW`；`可用于下游`=`APPROVED 且 downstream_allowed`；`孤儿`=来源任务或项目已不存在的内容。阶段号只增不改。

### 6.3 不变式（每阶段退出前必须成立）

| # | 不变式 |
| --- | --- |
| I1 | `demo-1.0.ps1` 17 项 + Agent/API 两套回归全绿，数量不低于 §7 记录 |
| I2 | `ARTIFACT_LIFECYCLE.md` 的状态机语义不变：`APPROVED` 必须有人工审核、`APPROVED` 不可覆盖、只有 `downstream_allowed` 能进下游 |
| I3 | 内核只读**自己工作区内**的文件（`WorkspacePolicy` 之内），不读工作区外路径 |
| I4 | 内容上传失败**不改变任务成败**；失败必须在 Run 摘要与过程中如实体现 |
| I5 | 平台不镜像工作区：只收清单列出的产出（D-CL-5） |
| I6 | 凭据（设备/项目 Token）不落文件、不进成果物内容；成果物内容里出现 `dvc_`/`prj_` 前缀即视为缺陷 |

### 6.4 禁区清单（本主线期间禁止改动）

- `packages/agent_protocol` 的信封/序号/幂等语义（只能追加可选字段）；
- `apps/api/app/collaboration.py` 的中继语义（CL-6 只允许新增持久化，不改帧语义）；
- `apps/api/app/gateway.py` 的逐帧应答模型；
- `packages/competition_packs/` 内置包内容；
- `infra/docker-compose.yml`；
- **既有 PostgreSQL 迁移脚本**（新增迁移不受限，但必须同步 SQLite 侧与契约测试清单）；
- Demo 1.0 已验收页面的交互语义（只允许**新增**区块；成果物页新增详情不删除既有列表行为）。

### 6.5 决策变更流程

不允许改写 §3 已有结论；要改就追加 `D-CL-11`…，写明变更内容、原因、影响面、迁移方案、生效阶段，并在原行标注「被 D-CL-x 取代」；若影响已完成阶段，在 `IMPLEMENTATION_STATUS.md` 追加"回归影响"并重跑 §7。

### 6.6 阶段交接文档

沿用 `docs/handoffs/_TEMPLATE_UX_HANDOFF.md` 结构（目标/实际完成/偏差/验证/边界/下一步/复现命令）+ §8 版本对照（内核 / 平台 / 契约）。

### 6.7 禁止的"顺手"行为

- 顺手实现硬删/清空；
- 顺手让产出自动 `APPROVED`；
- 顺手把工作区整体同步到对象存储；
- 顺手改 `ARTIFACT_LIFECYCLE.md` 的状态机（只能增补口径说明）；
- 顺手把 `ResultUploader` 的持久化队列换成"直接传、失败就丢"。

---

## 7. 测试与验收基线

```text
# 内核
cd apps/agent && python -X utf8 -m unittest discover -s . -p "test_*.py"
# 期望：259 项 OK（9 skipped），本主线新增测试计入数量

# 平台
cd apps/api && python -X utf8 -m unittest discover -s . -p "test_*.py"
# 期望：321 项 OK（14 skipped）

# 端到端主链路（不变式 I1）
pwsh -File scripts/demo-1.0.ps1 -AutoApprove
# 期望：17 项全过

# 前端
cd apps/web && NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build
# 期望：19 页通过（新增页面/区块不减少路由数）
```

---

## 8. 风险与回滚

| 风险 | 触发信号 | 处理 |
| --- | --- | --- |
| 产出采集把无关文件带进平台 | 成果物库里出现 `.next/`、`node_modules/`、日志文件 | 剪枝规则补齐（D-CL-2）+ 在清单里记录"被排除数量"，必要时增加按扩展名白名单的开关 |
| 上传拖慢任务收尾 | 任务 `RUNNING → WAITING_REVIEW` 时间明显变长 | 逐项计时；先跳过 >100MB（D-CL-9），必要时把上传改成"先完成任务上报、再补传" |
| 大文件把对象存储撑爆 | 存储计量异常增长 | 配额（`PLATFORM_QUOTA_*`）已在；超限时只登记不传并提示 |
| 内容里的凭据泄露 | 成果物内容命中 `dvc_`/`prj_`/`sk-` 前缀 | I6 加自动化断言（扫描上传内容，命中则拒绝并在摘要里写明原因） |
| 协作持久化引入冲突 | CL-6 后出现内容回退 | 冲突走"以谁为准"显式选择；持久化只写草稿层，不动提交/批准层 |

**回滚**：每个阶段独立可回——CL-1 可通过任务级开关（`resource_policy.inline_answer_artifact=false` 与采集开关）关掉；CL-2/3/5 是纯前端区块；CL-4 是新增端点调用；CL-6 可退回"仅浏览器会话"。

---

## 9. 工作量与顺序建议

- **最小可用（到达"内容能审、能进交付"）**：CL-0 → CL-1 → CL-2 → CL-5 ≈ **5.5 人日**；
- 完整体验（加可视化、回收、协作持久化）：+5.5 人日 ≈ 11 人日（含缓冲）；
- 建议顺序：先 CL-1（价值最大、依赖最少），随后 CL-2 与 CL-3 并行（审与看是同一批用户动作的两面），CL-4 插空，CL-6 独立推进。

---

## 10. 待拍板（三件，已给推荐值；不拍板就按推荐执行）

1. **产出是否自动入库？** 推荐：**自动入库为「待审」**（D-CL-1）。另一条路是"先在界面确认再入库"，更保守但每轮都要点一次。
2. **回答要不要也当成果物？** 推荐：**默认开**（D-CL-3），长回答值得有版本与审核；不想要的团队可在任务里关掉。
3. **超大文件怎么办？** 推荐：**>100MB 只登记不传**（D-CL-9），界面标注"内容未上传，仅记录路径与哈希"。

---

## 附录 A：关键文件地图

| 主题 | 文件 |
| --- | --- |
| 内核产出采集与上传 | `apps/agent/result_uploader.py`（已有）、新增 `apps/agent/workspace_scan.py`、`apps/agent/task_loop.py` |
| 内核摘要口径 | `apps/agent/codex_executor.py` |
| 平台成果物 | `apps/api/app/store.py`（状态机/审核联动/归档）、`apps/api/app/main.py`（artifact 端点）、`docs/ARTIFACT_LIFECYCLE.md` |
| 平台交付 | `apps/api/app/delivery.py`、`apps/web/app/delivery/page.tsx` |
| 前端内容视图 | `apps/web/app/artifacts/page.tsx`、`apps/web/app/review/page.tsx`、`apps/web/app/runs/page.tsx`、`apps/web/lib/events.ts` |
| 文档协作 | `apps/web/lib/collab.ts`、`apps/api/app/collaboration.py`（禁区） |

## 附录 B：上游证据索引

- 现状盘点：本文件 §1.2（含代码行号）；
- 已有状态机：`docs/ARTIFACT_LIFECYCLE.md`；
- 回答同步的上一轮成果：`docs/IMPLEMENTATION_STATUS.md`（2026-09-16「执行过程实时反馈」「Agent 回答与原始输出同步到平台」）；
- 内核上传队列的设计与测试：`apps/agent/test_result_uploader.py`（8 项）；
- 交付过滤口径：`apps/api/app/delivery.py` + `main.py:1454`（submission-bundle）。