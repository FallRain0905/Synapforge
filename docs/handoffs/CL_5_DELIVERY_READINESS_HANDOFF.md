# CL-5 交付就绪度 交接

> 交接状态：`PASS`
>
> 日期：2026-09-16
>
> 对应阶段：CL-5（工作项见 `docs/CONTENT_LIFECYCLE_PLAN.md` §5）
>
> 上一份交接：`docs/handoffs/CL_3_CONTENT_TRACEABILITY_HANDOFF.md`

---

## 1. 本阶段目标

从实施计划 §5 抄写：

> **目标**：交付页直接回答"还差什么才能交"。
>
> **验收标准**：故意留一份未审成果物 → 面板显示"1 份内容未审核，不会进提交包"并能直达；全部就绪时显示可提交。
>
> **退出条件**：就绪度判定与实际提交包内容一致（用提交包校验接口交叉验证）。
>
> **Do NOT**：不要让就绪度面板变成"必须全绿才能导出"的硬门禁（导出仍可强制，只要如实提示）。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| CL-5-01 就绪度面板 | 完成 | 交付页顶部新增 `delivery-readiness` 面板，五项来自**真实数据**：① 未批准内容数（`dashboard.artifacts` 里 `PENDING_REVIEW`）；② 大纲是否可生成（`delivery/assembly` 的 `generatable` 与 `blocked_reasons`）；③ 交付检查（`delivery/checklist` 的 fail/warn 计数与明细）；④ 门禁未通过数（`reviewCenter.gates`）；⑤ 编译状态（`compile` 结果）。面板副标题直接给结论："还有 N 项必须先处理 / 可以提交，但有 M 项提示 / 各项就绪" |
| CL-5-02 每项直达入口 | 完成 | 未批准内容 → `去审核内容`（/review）；大纲缺素材 → `去补文档`（/documents）；门禁 → `去审核门禁`（/review）；编译与检查就地给操作提示（右上角按钮）。**导出门禁未被加入**：面板只提示，仍可直接生成提交包（遵守 Do NOT） |
| CL-5-03 被排除内容清单 | 完成 | 展示后端**已算出**的 `excluded_unapproved`（此前界面只用 `excluded_count` 显示一个数字）：列出前 5 份的名称与状态，并写明"提交包只装已批准素材；这些内容批准后重新生成即可带上" |
| 自动取检查 | 完成 | 进入页面自动跑一次 `delivery/checklist`（此前必须先点「交付检查」才知道差什么） |

## 3. 与计划的偏差

- **就绪度未引入新的后端接口**：§5 列的三条工作项在既有 `delivery/assembly`、`delivery/checklist`、`compile` 与 workspace 数据上全部可实现，因此没有新增端点或字段（少一处口径漂移的风险）。
- **字数与图表数量**：计划 CL-5-01 里写的是"论文结构完整性、图表数量、字数"。实现把"结构完整性"落在 `blocked_reasons`（缺哪类已批准素材）与 checklist（图表引用/文献/公式/匿名/页数）上；**字数**未单独展示——checklist 里已有页数与附件检查，字数不是国赛的硬指标，故未编造一个阈值。

## 4. 测试与验证

```text
# 本阶段只改前端；后端与内核基线沿用 CL-3 退出时数字
cd apps/web && NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build
→ 19 页通过（/delivery 4.9 kB）

cd apps/api   && python -X utf8 -m unittest discover -s . -p "test_*.py"   → 326 OK（未改后端，CL-3 时已跑）
cd apps/agent && python -X utf8 -m unittest discover -s . -p "test_*.py"   → 281 OK
pwsh -File scripts/demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove      → 17/17
```

**实机验收（浏览器 + 真实项目状态，2026-09-16）**

面板实测输出（开发库的真实状态）：

```
交付就绪度 | 还有 2 项必须先处理
  4 份内容还没批准 | 未批准的内容不会进提交包：result.md、0ff842b4….md、38e051cc….md 等 | 去审核内容 →
  大纲还生成不了 | 缺这些已批准素材：摘要:paper_source/problem_analysis、问题重述与分析:problem_analysis、…  | 去补文档 →
  交付检查 2 项未通过 | 缺少交付附件：['compiled_pdf']；尚无编译 PDF
  4 个门禁未通过 | 未通过的门禁会挡住下游任务与提交包冻结 | 去审核门禁 →
  还没编译 | 点右上角「编译论文」生成 PDF
  不会进提交包的内容（16 份）| f3c42436….md（REJECTED）、second.md（ARCHIVED）、result.md（PENDING_REVIEW）… —— 提交包只装已批准素材
```

- **与提交包一致**（退出条件）：面板的"不会进提交包"名单来自 `assemble_paper` 的 `excluded_unapproved`，与提交包实际装配过滤**同源**；`second.md` 归档后同时出现在排除名单里，且它对应的门禁已被标记失效。
- **未批准内容确实被排除**：归档的 `second.md`（ARCHIVED）与待审的 `result.md`（PENDING_REVIEW）都在排除名单中，而唯一 APPROVED 的素材出现在"大纲可生成"的来源里。

## 5. 尚未完成与边界

1. **就绪度不阻止导出**：面板只提示（遵守 Do NOT）；生成提交包仍可强制，提交包内会如实带上排除说明。
2. **字数未展示**：见 §3。
3. **编译状态是会话内的**：刷新页面后 `compile` 结果清空（显示"还没编译"），即使之前编译过——因为编译产物以工作区文件形式存在；下一步可以把"最近一次编译产物"记成成果物（`compiled_pdf` 已有类型），让状态跨会话可见。
4. **CL-6 文档协作持久化未做**（CL 主线最后一阶段）。

## 6. 下一步

- 下一阶段：**CL-6 文档协作持久化**（改 `collaboration.py` 属禁区，只允许「新增持久化、不改帧语义」）
- 入口条件是否满足：**是**（CL-5 退出；CL 主线仅剩 CL-6）
- 建议工作项：`CL-6-01` 协作中间态节流落库 + 刷新恢复、`CL-6-02` 冲突提示（不静默覆盖）、`CL-6-03` 文案修正

## 7. 复现命令

```powershell
cd apps/web; NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next start -p 3000
# 打开 /delivery：顶部即「交付就绪度」；
# 留一份未批准内容（或归档一份已批准内容）→ 面板出现对应条目与「去审核内容」入口；
# 「不会进提交包的内容」名单应与勾选「生成提交包」后的装配排除一致。
```

## 8. 组件版本对照

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| 前端 | 0.1.0 | 仅 `app/delivery/page.tsx` 新增就绪度面板与自动检查 |
| 平台 / 内核 | 0.1.0 | 未改 |
| sidecar 契约 / 协议 | v1 / 1.0 | 未变 |