# 阶段 6 数学建模模板 pack 交接文档

> 日期：2026-09-15
>
> 状态：`PASS`（pack 加载/物化/校验端到端验收 + HTTP 端点接入 + Web 工作台面板通过；阶段 6 剩余项待做）
>
> 任务：按 PROJECT_EXECUTION_PLAN §13 完成 CUMCM 正式领域工作流包中「数学建模模板」部分——pack manifest 与版本升级规则、题面事实/数据画像、四问任务 DAG、建模/代码/实验/复核/论文模板

## 1. 本轮结论

阶段 6 的数学建模模板已从"文档约定"变成**可加载、可物化、可校验**的领域包：

```text
packages/competition_packs/cumcm/
  manifest.json            pack 元数据 + 9 模板 + 校验规则 + 信息边界 + 升级规则
  four_question_dag.json   17 个任务的四问 DAG
  schemas/                 题面事实、数据画像 JSON Schema
  templates/               9 个模板骨架
        ↓ loader.load_pack('cumcm')
        ↓ materializer.apply(project)   17 任务（含依赖）+ 成果物骨架 + 幂等
        ↓ validator.validate(...)       四问覆盖 / 官方结果表 / 信息边界
```

## 2. 已完成

### 2.1 Loader（`packages/competition_packs/loader.py`）

- `PackManifest`/`TemplateSpec`/`DagTask`/`UpgradeRule` 数据结构，加载时即校验重复模板 id、默认题号、非平台成果物类型与 DAG 环依赖。
- `CompetitionPack.render_template()` 与 `placeholders()`：`{{variable}}` 渲染，缺变量时 fail-closed（`pack_template_variables_missing`），全部模板渲染后无残留占位符、JSON 模板仍是合法 JSON。
- `PackManifest.plan_upgrade(from_version)`：跨版本汇总新增/重命名/移除成果物与兼容级别；降级拒绝（`pack_downgrade_not_supported`）。
- `resolve_pack_id()`：把项目的 `competition_pack`（如 `cumcm-2026`）解析为包 id，兼容别名。

### 2.2 CUMCM pack 内容

- **9 个模板**：`PROBLEM_ANALYSIS.md`、`PROBLEM_FACTS.json`、`DATA_PROFILE.json`、`MODELING_REPORT.md`、`CODE_README.md`、`EXPERIMENT_PLAN.md`、`RESULT_TABLE_TEMPLATE.md`、`COMP_REVIEW.md`、`PAPER_OUTLINE.md`；
  版本升级规则已含一条 1.0.0 → 1.1.0（新增能力清单与跨问题台账、`review.md` 重命名为 `COMP_REVIEW.md`）。
- **四问 DAG（17 任务）**：题面事实 → 四问拆解 → 每问 建模/代码/实验 → 独立复核 → 论文装配 → 交付冻结；「问题一代码与计算」等下游任务自动依赖对应建模任务。
- **图式**：`problem_facts_schema.json`（题面参数/实体/约束/时间口径/来源哈希）与 `data_profile_schema.json`（数据时间范围、字段画像、质量发现、信息边界）;数值型填空字段允许 `null`，便于模板先落骨架。
- **校验规则**：每类成果物的必需章节/最少字数（模型报告要求含「求解思路」）、`problem_facts` 必需与禁止字段、`result_table` 官方文件名与内容哈希要求、`code` 入口与输出路径、以及四问覆盖（期望 [1,2,3,4]）与信息边界（deny 未来数据、要求声明输入、观察模式 system）。

### 2.3 Materializer（`packages/competition_packs/materializer.py`）

- `apply(project_id, questions=..., output_dir=..., ...)`：按拓扑顺序创建任务并写入 `dependency_task_ids`；把模板渲染为成果物（含 `content_hash`、`mime_type`、`data_policy`）并写入工作区骨架。
- 幂等：重复执行创建 0 任务/0 成果物；已存在同名成果物（如种子项目的 `PROBLEM_ANALYSIS.md`）时不改记录，但会补齐缺失的工作区骨架文件，保证 9 个模板全部可见。
- 问题子集：`questions=[1]` 只物化第 1 问分支，其余问题不生成任务。

### 2.4 Validation（`packages/competition_packs/validation.py`）

- `validate(artifacts, documents, project_id)` → `ValidationReport`（status/allowed/worst_severity/coverage/findings），严重度口径 fatal/major/minor 映射 BLOCKED/NEEDS_REVISION/PASS_WITH_ASSUMPTIONS/PASS，可直接喂给平台 Review。
- 误报治理（本轮重点）：
  - pack 下发的模板骨架按 `data_policy.template_id` 识别并豁免官方文件名校验与四问覆盖判定（不用文件名判断，避免把真实交付的 `MODELING_REPORT.md` 误判为空骨架）；
  - 文本不可读时不判定四问覆盖，避免把"调用方未提供内容"伪装成"内容缺失"；
  - 问题标记同时接受中文数字（`问题一`）与阿拉伯数字（`问题1`/`问题 1`/`Q1`/`Question 1`），与物化器渲染的 `### 问题1` 对齐；
  - 官方文件名规则对 pack 管理的正式结果表判 major，对导入的遗留结果表仅提示 minor。
- `validate_pack()` 自检 pack 自身（模板成果物类型必须是平台已识别类型）。

## 2.5 HTTP 接入（本轮新增）

| 端点 | 作用 |
| --- | --- |
| `GET /api/competition-packs` | pack 概要（版本、问题数、模板数、DAG 任务数、必需成果物） |
| `GET /api/competition-packs/{pack_id}` | 详情：模板（含占位符清单）、DAG、校验规则、信息边界规则、升级规则 |
| `GET /api/competition-packs/{pack_id}/templates/{template_id}` | 导出未渲染骨架 |
| `GET /api/projects/{project_id}/competition-pack` | 项目视角：解析后的 pack + 物化进度（任务/成果物齐备度、缺失清单） |
| `POST /api/projects/{project_id}/competition-pack/apply` | 物化（任务 + 成果物 + 内容入库）；`Idempotency-Key` 必填，同键重放返回原响应 |
| `POST /api/projects/{project_id}/competition-pack/validate` | 按 pack 规则校验，返回可喂给 Review 的报告 |
| `GET /api/projects/{project_id}/competition-pack/templates/{template_id}` | 按项目上下文渲染导出（`questions=1,2` 可选） |

要点：
- 规则只存在于 pack 内，`apps/api/app/pack_api.py` 仅做编排，不在 API 层复制竞赛判断；
- 物化语义收紧为 **"空骨架不授予四问覆盖，填写后按真实交付物校验"**——用 `data_policy.template_hash` 与当前 `content_hash` 比对区分未填写骨架与已填写交付物，避免写完的报告被永久当成空模板；
- 项目级端点复用既有项目鉴权中间件（GET→`project.view`，POST→`project.write`）。

## 2.6 Web 工作台面板（本轮新增）

`apps/web/components/dashboard-shell.tsx` 新增「建模模板包」面板（`data-testid="pack-panel"`）：

- **物化进度**：`pack-progress` 显示任务/成果物齐备度与百分比，`pack-missing` 列出待补齐项；
- **问题范围**：`pack-question-1..4` 可勾选，仅物化所选问题（不选则全部）；
- **模板清单**：`pack-template-list` / `pack-template-<id>` 列出 9 个模板（文件名、阶段、是否官方格式），每项可「预览导出」；
- **一键应用**：`pack-apply` 调用 `POST .../competition-pack/apply`（幂等键由前端生成）；
- **运行校验**：`pack-validate` 调用 `POST .../competition-pack/validate`，`pack-validation-report` / `pack-findings` 展示状态、覆盖面与按严重度着色的发现；
- **模板预览弹窗**：`pack-preview-modal` / `pack-preview-body` 展示渲染后的骨架，支持复制与下载；
- 侧边栏 `nav-pack` 入口，未完成物化时显示提醒点。

验证：`npm run build` 通过；以生产构建启动（`:3100`）后页面 HTML 中 `pack-panel`、`pack-progress`、`pack-apply`、`pack-validate`、`pack-template-list`、`nav-pack`、`一键应用模板包`、`运行模板校验`、`物化进度`、`问题范围` 全部存在。真实服务链路上，对新建项目 apply 得到 17 任务 / 9 成果物，validate 仅报真实缺口 `audit_report`。

## 2.7 机器 Review 与信息边界 Gate（本轮新增）

`POST /api/projects/{project_id}/competition-pack/review`：

- **verdict 映射**：任一 `fatal` → `BLOCKED`；任一 `major` → `NEEDS_REVISION`；否则不创建 Review；
- **保留人工批准门禁**：干净结果返回 `created=false` + `machine_review_clean_requires_human_approval`——平台规则禁止 agent 提交 `APPROVED`，正式批准必须由人工完成；
- **严重度映射**：pack 的 `info` 降级为平台合规的 `minor`（平台仅接受 fatal/major/minor）；
- **目标选择**：优先已填写的 `result_table`/`audit_report` 交付物（按 `template_hash` 排除未改动骨架），其次骨架槽位，最后「独立复核与一致性检查」任务；无目标时 404 `machine_review_target_missing`；
- **信息边界范围**：`scope="information_boundary"` 只把 `future_data_allowed`/`information_boundary_missing` 写入 Review/Gate；
- **幂等**：同一 `Idempotency-Key` 重放返回同一 Review；
- 创建 Review 时会同步建立/更新对应 Gate（NEEDS_REVISION → Gate `FAILED`）。

## 2.8 现有 C 题交接包的可解释导入（本轮新增）

`POST /api/projects/{project_id}/imports/cumcm-handoff`（`CumcmHandoffImporter`）：

| 目录（前缀匹配） | 成果物类型 |
| --- | --- |
| `01_项目工作区` | 沿用既有工作区分类（题面分析/事实/数据画像/建模/代码/结果表/图表/论文…） |
| `00_复审入口` | `review_report` |
| `02_外部参考*` | `problem_source` |
| `03_工作流插件` | `code` |
| 其他顶层文件 | `problem_source`（**可解释回退登记**，并列入报告的 `unknown_files`） |

报告字段：`layout_detected`、`declared_files`、`imported_artifacts`、`skipped_artifacts`、`classification`（类型→计数）、`unknown_files`、`hash_mismatches`、`lossless`、`created_tasks`、`warnings`。

关键语义：
- **前缀匹配目录名**——真实包用 `02_外部参考_其他模型` 这类带后缀名字，精确匹配会把整目录误判为"未识别"；
- **无损判定**：所有参与文件入库且哈希一致才 `lossless=True`；未分类文件计入 `declared` 并回退登记，不静默丢弃；
- **重复导入幂等**：第二次全部 `skipped`，`created_tasks=0`。

真实包实测（`C题四问完整交接包`）：declared 1964 / imported 1964 / skipped 0 / lossless True / 哈希不符 0；分类 paper_source 943、problem_source 454、code 321、result_table 176、figure 16、compiled_pdf 13、audit_report 10、review_report 10、data_profile 5、model_spec 6、problem_analysis 5、problem_facts 5；导入后 pack 校验可继续执行并报真实缺口 `experiment_plan`。

## 2.17 工作台多页面重构（本轮）

| 路由 | 内容 |
| --- | --- |
| `/` | 项目总览：指标、任务流、Agent 与运行、模板包进度、门禁、风险与交接、最近事件 |
| `/tasks` | 阶段看板 + 任务清单（状态推进/返工/阻塞）+ 新建任务弹窗 |
| `/pack` | 模板包物化与校验：问题范围、模板预览导出、一键应用、模板校验、信息边界审计（预览/落库门禁） |
| `/documents` | 文档三层版本：时间线、差异（含合并确认）、评论、快照、关系与影响面、协作编辑在线状态 |
| `/review` | 审核门禁、复核意见、风险分配/关闭/重开、交接收据 |
| `/delivery` | 装配（仅批准素材）、交付检查、真实编译、提交包与跨部署恢复校验 |
| `/artifacts`、`/handoffs`、`/runs`、`/timeline`、`/settings` | 成果物库（类型筛选/下载）、交接收据、运行与 Agent 编队、事件时间线（搜索）、平台指标与配额 |

实现：`app/globals.css`（设计系统：令牌 + 组件类 + 1024/760 响应式 + 移动抽屉）、`components/shell.tsx`（侧栏/顶栏/页面标题）、`lib/workspace.tsx`（项目选择与数据上下文、WS 联动刷新）、`components/ui.tsx`（Panel/Metric/StatusPill/Progress/Modal/FindingList）。旧 `components/dashboard-shell.tsx` 已移除。

保留的 `data-testid`：`nav-*`、`pack-panel`、`pack-apply`、`pack-validate`、`pack-template-list`、`pack-progress`、`pack-planned-count`、`document-panel`、`document-list`、`document-tab-*`、`document-submit/revise/merge`、`document-comment-*`、`document-snapshot`、`document-relation-*`、`document-impact*`、`document-collab*`、`delivery-*`、`task-*`、`project-overview`、`review-center`、`risk-*` 等，便于后续统一回归。

## 2.18 主题切换与个人云盘（本轮）

**黑夜/白天主题**
- CSS：`[data-theme="dark"]` 覆盖全套令牌（背景/表面/边框/文字/蓝绿琥珀红紫/阴影）+ 针对顶栏、代码块、进度条、chips、空状态、输入框、toast、文档评论等的暗色适配；
- 上下文：`lib/theme.tsx`（ThemeProvider + useTheme），选择写入 localStorage（`math-agent-theme`）并在挂载时恢复；
- 顶栏：`theme-toggle` 按钮（月亮⇄太阳）。实测刷新后主题保持。

**个人云盘**（`apps/api/app/personal_drive.py` + 4 端点 + `/drive` 前端页）

| 端点 | 语义 |
| --- | --- |
| `GET /api/drive` | 当前成员文件列表与用量（配额 200MB） |
| `POST /api/drive/upload` | multipart 上传；同内容去重；超限 413 `drive_quota_exceeded` |
| `DELETE /api/drive/{file_id}` | 删除；被项目引用返回 409 |
| `POST /api/projects/{id}/drive/import` | 导入项目：登记 Artifact（按扩展名映射类型）、写入项目对象存储、记录引用 |

- 成员隔离：他人文件不可见、不可删（测试走真实 Session 认证路径验证）；
- 内容寻址：SHA-256 相同的重复上传直接复用记录，不重复占配额；
- 压缩包：zip/tar/gz/tgz/7z/rar/bz2/xz 标记 `is_archive`，导入按 `problem_source` 登记（解包属后续工作）；
- 导入映射：csv/xlsx/xls/json/tsv→result_table、pdf→compiled_pdf、tex/md/docx→paper_source、py/r/m/jl→code、png/jpg/jpeg/svg→figure，默认 problem_source；
- 引用保护：`project_ids` 记录已导入项目，非空时禁止删除。

**总览页精简**：指标行 + 项目/模板包进度 + 门禁风险摘要 + 6 张快速链接卡（模板包/文档/审核/交付/任务/云盘）+ 三个摘要面板（运行与 Agent/交接与成果物/最近事件），长列表全部移入各自子页面。

## 3. 验证

```text
pack 自检                                       -> PASS
物化（种子 CUMCM 项目）                          -> 17 tasks / 7 artifacts created（2 个已存在）/ 9 个工作区文件
幂等重跑                                        -> 0 tasks / 0 artifacts created
合规项目校验                                    -> PASS | allowed=True | findings=0 | coverage [1,2,3,4]
种子项目校验                                    -> PASS_WITH_ASSUMPTIONS | 4 项 minor（3 项文本未提供 + 1 项遗留结果表命名）
apps/api/test_competition_packs.py              -> 15 passed（领域）
apps/api/test_competition_packs_api.py          -> 15 passed（HTTP 端点）
API 全量回归（含集成环境变量）                   -> 145 passed
Next.js 生产构建                                -> passed
真实服务烟雾（pack 端点 + 页面标记）             -> 通过
```

运行命令：

```powershell
$env:PYTHONPATH = "$(Get-Location);$(Get-Location)\apps\api"
python -X utf8 -m unittest discover -s apps/api -p 'test_competition_packs.py' -v
```

## 2.9 阶段 7 文档三层版本与证据链（本轮新增）

`apps/api/app/document_api.py` + 5 个端点：

| 端点 | 作用 |
| --- | --- |
| `GET /api/documents/layers` | 三层定义（draft/submitted/approved）与门禁规则 |
| `GET /api/projects/{id}/documents/{artifact_id}/timeline` | 版本链 + 每层追溯（成员/Agent 类型/任务/运行/Git commit/审批人/证据） |
| `GET /api/projects/{id}/documents/{artifact_id}/evidence` | 证据链查询（claim、来源运行、提交追溯） |
| `POST .../documents/{artifact_id}/submit` | 草稿 → 提交（冻结待审，**必须带证据**） |
| `POST .../documents/{artifact_id}/revise` | 退回/再起草：派生新的草稿版本 |

三层映射：`draft`=DRAFT（可编辑）、`submitted`=PENDING_REVIEW（冻结，不进下游）、`approved`=APPROVED（不可变、允许下游）；`REJECTED` 与再次起草都回到 `draft`。

门禁不变量：
- **提交必须带证据**：`document_evidence_required` / `document_evidence_not_linked_to_artifact`，草稿无法绕过正式版本门禁；
- **批准只能由人工产生**：agent 提交 `APPROVED` 仍被平台 `human_approval_required` 拒绝；
- **修订只新增版本**：`revise_artifact` 经 `parent_artifact_id` 串链，已批准版本始终保持不可变。

真实服务验证：draft → 无证据提交 400 → 带证据提交 submitted → 人工 Review 后 approved（downstream_allowed=True、immutable=True）→ 修订派生新 draft（链 `['approved','draft']`）→ 证据链返回 claim。

## 2.10 文档版本面板（Web，阶段 7）

`apps/web/components/dashboard-shell.tsx` 新增「文档版本」面板（`data-testid="document-panel"`，侧边栏 `nav-documents`）：

- **文档列表**：按文档类成果物过滤（`paper_source`/`model_spec`/`problem_analysis`/`audit_report`/`review_report`/`submission_bundle`/`experiment_plan`），显示层级徽标（草稿/提交/批准）；
- **三层版本时间线**：`document-timeline` / `document-revisions` 展示每版内容哈希、创建者与成员/Agent 类型、Git commit、批准人、是否允许下游引用、关联证据数；
- **证据链**：`document-evidence` 列出 claim、证据类型与来源；
- **动作**：`document-submit`（提交待审，草稿层才可用；缺证据时提示"提交需要先关联证据"）、`document-revise`（派生新草稿，历史保留）。

联调验证：dashboard 成果物 → 前端文档过滤 → `timeline`（`draft` 层 + `member/member-001` 追溯）→ 证据链；`npm run build` 通过，SSR HTML 标记齐备。

## 2.11 版本 Diff / 合并确认 / 评论 / 快照 / 关系（阶段 7）

| 端点 | 作用 |
| --- | --- |
| `GET .../documents/{id}/diff?from_revision=&to_revision=` | 版本间 unified diff + 增删行/hunk 统计 + 层级/Git/任务/运行/作者/批准人变化 |
| `POST .../documents/{id}/merge` | 合并确认：选定一方版本内容派生新草稿（`document.merged` 留痕）；批准版本不可为目标；登记 Git 仓库时校验提交存在 |
| `POST/GET .../documents/{id}/comments` | 评论与建议（`kind=comment|suggestion`，可锚定段落） |
| `POST/GET .../documents/{id}/snapshots` | 文档快照（revision + content_hash 检查点） |
| `POST/GET .../documents/{id}/relations` | 结论/图表/运行/任务 ↔ 文档段落关系 |
| `GET /api/projects/{id}/impact?target_type=&target_id=` | 影响面定位：结果/图表/运行变化后受影响的文档与段落 |

设计要点：三者都基于平台事件流（typed events + 查询），**不需要数据库迁移**；Diff 两侧内容必须已落库，否则 fail-closed；合并只新增版本，两端均不被改写。

## 2.12 协作编辑与文档面板标签页（阶段 7 收口）

**协作（Yjs CRDT）**
- 前端：`apps/web/lib/collab.ts`（Y.Doc + Y.Text，增量更新 base64 编解码，文本框整串输入换算最小插入/删除）；依赖 `yjs` 13.6.32。
- 服务端：`apps/api/app/collaboration.py` 帧校验（类型白名单 `document.update`/`document.presence`、`document_id` 必填、base64 校验、256KB 上限、稳定错误码）+ `ConnectionManager.relay()`（同项目转发、不回声给发送者、按项目隔离、断连自动剔除）。
- 面板：`document-collab` / `document-collab-editor` / `document-collab-status` / `document-collab-presence`；协作内容为内存 CRDT，**定稿仍需「提交待审」**写回正式版本。

**验证**
- `apps/web/scripts/verify-collab.mjs` → `COLLAB_CONVERGENCE_OK`（两端收敛一致、双方编辑保留、同段落并发插入不覆盖）；
- `scripts/verify-collab-relay.py` → `RELAY_E2E_OK`（真实 WS 上中继送达、无回声、非法帧 `collaboration.error`、presence 转发）；
- `apps/api/test_collaboration.py` 7 项；API 全量 183 项通过。

**文档面板标签页**：版本 / 差异（unified diff + 统计 + Git 变化 + 按版本合并）/ 评论（评论与建议、段落锚点）/ 快照（检查点）/ 关系（关联图表·结果表·成果物·运行到段落 + 影响面查询）。

## 2.13 信息边界审计 Gate 与 competition-workflow 适配（阶段 6 收尾）

**信息边界审计 Gate**（`apps/api/app/boundary_gate.py`）

- 输入：pack 的 `information_boundary` 规则 + 运行事实（`Run.information_boundary.allowed/violations`、`data_access_policy.observation_mode/allow_future_data`、`observed_input_files`、任务级 `allow_future_data`）。
- 判定：违规 → `BLOCKED`；观察缺失（要求 system 却无观察记录）、观察模式不符、任务级未来数据冲突 → `NEEDS_REVISION`；干净 → 不创建 Review（正式批准仍需人工）。
- 端点：`POST /api/projects/{id}/competition-pack/boundary-gate`（落库 Review + Gate，幂等键可重放）、`GET .../boundary-audit?task_id=`（只读预览）。
- 目标选择：任务 → 运行产出成果物 → 项目结果表槽位；缺目标 404 `boundary_gate_target_missing`。

**competition-workflow 适配**（`apps/api/app/competition_workflow_adapter.py` + `scripts/adapt-competition-workflow.py`）

- 技能 → pack 阶段与产出（如 `comp-modeling → modeling/model_spec`、`comp-code → coding/code,result_table`、`comp-review → review/review_report,audit_report`）；
- 插件模板 → pack 模板（`PAPER_PLAN_TEMPLATE.md → paper_outline` 等）；
- 检查脚本 → 平台审计入口（`capability_check.py → pack:capability_checklist`、`ai_disclosure_rules.md → pack:paper_outline.compliance`）；
- 未识别资产显式列出（`feishu-notify`、`templates/README.md`），不静默丢弃；输出已落盘 `docs/competition_workflow_adapter.json`。

## 2.14 阶段 8 交付链路与第一版 Demo（本轮）

**交付链路**（`apps/api/app/delivery.py` + 5 个端点）

| 端点 | 作用 |
| --- | --- |
| `GET .../delivery/assembly` | 按批准素材装配论文章节；未批准素材列在 `excluded_unapproved` |
| `GET .../delivery/slides` | Marp 兼容答辩提纲（每页标注来源哈希与批准人） |
| `POST .../delivery/checklist` | 图表引用 / 引用文献 / 公式标签 / 匿名 / 附件 / 页数检查 |
| `POST .../delivery/submission-bundle` | 装配+检查+清单 → `submission_bundle` 成果物并提交待审（冻结需人工批准） |
| `GET .../delivery/submission-bundle/{id}/verify?restore_check=` | 一致性校验；`restore_check=true` 在隔离 Store 上做跨部署恢复比对 |

关键语义：装配只读 `APPROVED`；校验**按 artifact_id 解析来源**（同名成果物在项目里可能不止一个，按名字匹配会误判）；冻结沿用平台不可变批准机制。

**第一版 Demo**：`scripts/start-demo.ps1`（基础设施→API→Web，首跑自动构建，Ctrl+C 清理）。
注意：`start-demo.ps1` 与 `local-infra.ps1` 必须保存为 **UTF-8 with BOM**，否则 Windows PowerShell 5.1 按 ANSI 读取会因中文乱码破坏语法；环境变量经父进程传递（5.1 无 `-Environment`）。

## 2.15 阶段 9 最小切片：可观测、配额与自托管部署（本轮）

**可观测与配额**（`apps/api/app/observability.py`）

| 端点 | 内容 |
| --- | --- |
| `GET /api/platform/health` | 探活：状态/版本/关键计数/outbox 待投递（compose healthcheck 使用） |
| `GET /api/platform/metrics` | 项目、任务按状态、运行按状态、成果物、已批准、事件、Review、Gate、设备、存储字节 |
| `GET /api/platform/quota` | 限额与成本单价口径 |
| `GET /api/projects/{id}/usage` | 单项目：任务/运行/成果物/事件/能力 Token、存储分解、成本估算与配额状态 |

- 成本字段一律标注为**估算**（`estimated_cost` + `basis` + note）：平台不采集模型 token 明细，只按可配置单价换算运行数与存储量。
- 写路径配额：创建运行与登记成果物时 `enforce_quota`，超限 **429** + `quota_exceeded_<resource>`；限额经 `PLATFORM_QUOTA_*`、单价经 `PLATFORM_COST_*` 覆盖，非法值回落默认。
- `quota_status` 与 `enforce_quota` 同口径（`used >= limit` 即无余量），避免"统计说没超、写入被拒"的错位。

**自托管部署**：`apps/api/Dockerfile`（依赖分层 + health 探活）、`apps/web/Dockerfile`（`NEXT_PUBLIC_API_URL` 必须作为构建参数，Next.js 构建期内联）、`infra/docker-compose.yml`（postgres/minio/nats/api/web + 健康检查 + 依赖顺序 + 配额透传）、`infra/README.md`（本机 Demo 与 Compose 两条路径；生产必做：改默认口令、改用非 owner 的 `app_runtime` 角色、配置配额与单价）。

## 2.16 后续统一测试第一批（浏览器 + LaTeX，本轮）

**浏览器交互验收**（真实浏览器，非模拟）
- 工作台加载、项目总览、指标与审核门禁正常；
- pack 面板：v1.1.0·4 问、9 模板清单、问题范围筛选齐备；**点击「一键应用模板包」→ 物化进度 1/26(4%) → 26/26(100%)、任务 17/17、成果物 9/9**；
- 文档面板：三层状态可见（4 草稿 / 1 提交 / 2 批准）；打开文档后时间线（版本、哈希、批准人）与五标签页（版本/差异/评论/快照/关系）齐备；**协作编辑区显示「已连接 · 在线 1 人」**；
- 浏览器内提交评论成功并即时显示，验证 UI→API→事件流全链路；截图已留存。

**LaTeX 真实编译**
- 环境：MiKTeX 25.12（`winget install --scope user`，免管理员）；`pypdf 6.18.1` 用于页数解析。
- 实现：`apps/api/app/latex_compile.py` + `POST /api/projects/{id}/delivery/compile`；页数解析优先 `pypdf`（真实输出为压缩对象流，纯正则读不到），回退正则启发式；引擎缺失或编译失败一律 `not_compiled`，不登记成果物。
- 实测：中文 ctexart 论文 → **2 页 PDF（47,065 字节）**，`page_count` 与 `attachments` 检查均 `pass`，PDF 可下载。
- 踩坑记录：通过 shell heredoc 传 LaTeX 源码时 `` 会被折叠成退格符导致 `^^H` 报错——**传源码请用文件载荷**（示例见 `apps/api/fixtures/sample-paper.tex`）。

## 4. 未进行的测试（后续统一补测）

按本批次"细节测试可省略、记录后统一补测"的口径，以下项目**未做**，需在后续轮次补齐：

1. **pack 物化的平台侧回归**：`PackMaterializer` 目前只在 SQLite 开发 Store 上跑通；PostgreSQL Store、真实 RLS 作用域下的物化未测。
2. **HTTP 层**：端点已实现并有 15 项路由契约测试，但**未经过真实 ASGI 请求**（测试直接调用路由函数）；权限中间件在 pack 路径上的实际表现、真实 `Idempotency-Key` 重放竞争、上传/导出大响应未测。
3. **Web 层**：pack 面板与文档版本面板均已实现，SSR HTML 标记与真实 API 数据链路已核验，但**未做真实浏览器交互验证**（协作编辑需两个浏览器会话同时打开同一文档才能人工确认，本轮以 Yjs 收敛脚本与真实 WS 中继脚本代替；点击应用/校验/预览/提交/派生、弹窗下载、移动端布局、视觉回归均未测）。
4. **官方模板校验的真实样本**：`official_result_files`（result1..4.xlsx）仅用构造文档验证，未对真实 C 题历届结果表样本做列名/单位/精度比对。
9. **阶段 8 渲染**：LaTeX 编译与页数检查已实测通过（见 2.16）；**仍未做**：PPT 真实渲染（Marp Markdown 已生成，未接 PptxGenJS/Slidev）、Word/docx 输出、真实跨机器恢复（`restore_check=true` 仍是进程内第二套 Store 模拟）、备份与灾难演练。
11. **个人云盘后续**：解包导入（zip 展开为多个成果物）、云盘内搜索/重命名/移动、分块上传与断点续传、配额可配置化（现固定 200MB）、下载授权与生命周期清理。
10. **阶段 9 其余工作包**：NATS JetStream 消费者（当前 outbox 有 74 条待投递事件未消费）、OpenTelemetry/Prometheus/Grafana 指标导出（现为 JSON 端点）、Helm/Kubernetes 部署边界、组织/队伍级配额（现仅项目级）、真实使用数据后的计费设计。
5. **信息边界 Gate 联动**：`manifest.information_boundary` 规则尚未接入平台 Gate 与 Runner 审计，未验证"未声明输入/未来数据"是否真的阻断。
6. **机器 Review**：校验报告尚未落库为 Review/Gate/Evidence，未验证与 `risk_rules` 严重度口径的一致性。
7. **交接包导入的规模与性能**：已用真实包验证无损与幂等，但未测超大包（>1 万文件）耗时、并行导入竞争与对象存储配额。
8. **边界审计的运行时联动**：审计端点已就绪，但**尚未在 Agent 上报 Run 结果时自动触发**（目前需显式调用），也未覆盖多运行聚合与历史违规趋势。
8. **版本升级迁移实跑**：`plan_upgrade` 只验证了计划产出，未在真实项目上执行一次 1.0.0 → 1.1.0 成果物重命名迁移。
9. **并发与性能**：多项目同时物化、超大模板渲染的性能未测。
10. **跨平台路径**：模板写入在 Windows 下验证，Linux 路径与权限未测。

## 5. 关键文件

- 包实现：`packages/competition_packs/loader.py`、`materializer.py`、`validation.py`、`__init__.py`
- 包内容：`packages/competition_packs/cumcm/manifest.json`、`four_question_dag.json`、`schemas/*.json`、`templates/*`
- 测试：`apps/api/test_competition_packs.py`（15 项）
- 文档：`docs/IMPLEMENTATION_STATUS.md`、`docs/PROJECT_EXECUTION_PLAN.md`（3.28）
- 参考语料：外部 `competition-workflow`（templates/skills）与 `C题四问完整交接包`（真实成果物清单与能力清单结构）

## 6. 接收方行动

1. 先做平台接入（HTTP 端点 + Web 面板），再补交接包导入与 Gate/Review 联动；接入时复用 `PackMaterializer`/`PackValidator`，不要在 API 层重写规则。
2. 校验规则改动必须同步更新 `test_competition_packs.py`；新增误报治理时优先在 pack 侧（manifest 规则）解决，而不是在 API 层打补丁。
3. 模板文件名即交付文件名（`MODELING_REPORT.md` 等），不要为了区分脚手架而改名——脚手架与交付物由 `data_policy.template_id` 区分。
4. 阶段 7 的文档三层版本（草稿/提交/批准）应建立在本文档的模板与校验结果之上，避免另起一套文档模型。
