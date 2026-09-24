# MY-AGENT M-6 执行计划：多智能体角色预设（写作 / 审核 / 检索 / 建模 / 编程 / 编译 …）

> 2026-09-23 起草 · 状态：**R-1 / R-2 / R-3 / R-3b 全部交付并上线验证（12 个角色）**（交接 `docs/handoffs/MY_AGENT_M6_ROLES_HANDOFF.md`）；
> 仅剩 R-4（M-5b 模式与强度并入、角色文件 sha256 防漂移）与 R-5（后继项）待做
> 用户原话：「多智能体角色预设 比如写作 agent 审核 agent 检索 agent 等等 / 先从数学建模工作流内找对应提示词完成预设 / 然后你自行补充 / 提示词一定要足够详细 / 你可以不用一步完成提示词 / 写进计划 / 有一个专门的步骤进行提示词撰写」
> 上游：`docs/MY_AGENT_CONSOLE_PLAN.md`（M-1…M-4 已上线）、`docs/MY_AGENT_M5C_STREAMING_EXECUTION_PLAN.md`（真流式，用户已拍板）

> **实施后的三处偏差（都改好了，见交接 §6）**：
> ① 角色元数据不再需要 manifest —— 名字取 `opencode agent list`（权威可用性）、说明取角色文件自己的 frontmatter
> （单一真源，少一份要同步的清单）；
> ② `description` 变成**有格式约定的字段**：必须「短名 · 一句话」且短名 ≤8 字（页面下拉取「·」前的部分当标签），
> 这条进了 `prompt_lint.py`；
> ③ 顺手修掉两个实测出来的真问题：模型切换原本是静默无效（只写了建会话路径）、抽屉会盖住自己的开关。

---

## 0 结论先说

1. **机制不用猜**（本次已在执行体上实测）：opencode 1.18.32 的自定义 agent **就是一个 markdown 文件**——
   `~/.config/opencode/agent/<名>.md`，frontmatter 写 `description` / `mode` / `tools`，**正文就是系统提示**；
   `opencode run --agent <名>` 真能选中它（探针跑出约定口令 `ROLE_OK`，样本原文见附录 A）。
2. **底料已经在手**：数学建模工作流（`~/.agents/skills/math-modeling-competition/`，11 个子技能、9,395 行）里，
   **审题 / 建模 / 编程 / 逻辑复核 / 论文写作 / 编译合规** 六个角色有成熟提示词可直接蒸馏（含大量"硬规则 + 禁令 + 检查清单"）；
   **检索 / 作图 / 论文评审打分** 三个角色工作流里只有碎片或只有规则库，**需要新写**。
3. **本期不做全流水线移植**：对话通道的角色 = **人设 + 方法论 + 输出契约**（自包含，不依赖仓库里的任何脚本/模板文件）。
   文件合同与闸门脚本（`PROBLEM_ANALYSIS.md` / `CAPABILITY_CHECKLIST.json` / 22 项编译 Gate …）属于**项目工作通道**
   （既有 pack + `competition_workflow_adapter` 已覆盖）。两边**共享同一份"方法内核"文字**，但不共用运行机制。

---

## 1 已实测的机制事实（2026-09-23，执行体 `154.219.99.75`，opencode 1.18.32）

| 事实 | 证据 |
| --- | --- |
| 自定义 agent = `~/.config/opencode/agent/<名>.md`（`agents/` 目录**同样被识别**） | 两个候选目录各放一个探针 md，`opencode agent list` 里**两个都出现** |
| frontmatter 的 `description` / `mode` / `tools`（逐工具开关）**都被解析** | `opencode debug agent probe-role` 输出 `"mode": "primary"`、`"description": "probe role…"`、`"tools": {"bash": false, "edit": false, "write": false, "read": true, …}` |
| **markdown 正文 = 系统提示**（不需要额外字段） | 同上输出里 `"prompt": "你是「探针角色」。无论用户问什么…"`（即正文原文） |
| `opencode run --agent <名>` **真的会用它** | 探针要求"回复必须以 ROLE_OK 开头"，实跑事件流里的 text 就是 `ROLE_OK 我是「探针角色」，…` |
| 内置 agent 名单 | `build` / `plan` / `compaction` / `summary` / `title`（primary）＋ `explore` / `general`（subagent） |
| `opencode agent create` **是交互式的**（非交互会挂住） | 探针里 `</dev/null` 仍超时 → **定义一律手写 md，不用这个命令** |
| `opencode agent list` 必须在**工作目录**里跑 | 从 `/root` 跑报 `PermissionDenied: FileSystem.access (/root/opencode.jsonc)`；`cd /srv/synapforge/cloud` 后正常 |
| `agent list` 输出**不含 description**（只有 `名 (mode)` + 权限 JSON 数组） | 探针输出原文 → 角色元数据（中文名/简介）**不能靠解析 CLI 输出**，改由仓库 manifest 提供（见 §5 R-1） |
| `step_finish` 事件带**逐轮 token 账**（含 `reasoning` 字段）与 `cost` | 探针实跑：`"tokens":{"total":3592,"input":3545,"output":22,"reasoning":25,…},"cost":0` → **角色提示词变长的代价可量化**（见 §4.3） |

> 探针文件用完即删（配置目录已恢复原状）；探针的一次真跑花费可忽略（3,592 input tokens）。

---

## 2 目标形态

```
仓库（唯一真源）                     执行体（安装 + 认它）              平台（只存"选了谁"）        浏览器
┌──────────────────────────┐   install-cloud  ┌────────────────────┐  ┌──────────────────┐  ┌──────────────┐
│ deploy/cloud-agent/roles/│ ───────────────► │ ~/.config/opencode/ │  │ agent_conversations│  │ 工具条：角色▾ │
│  mm-paper-zh.md          │   装到 agent/ 目录 │   agent/mm-*.md     │  │  + role 列         │  │ 抽屉：角色说明 │
│  mm-review.md            │                  │                    │  │                    │  │ + 硬规则摘要   │
│  …（12 个）+ manifest.json│                  │ opencode agent list │─►│ 心跳上报 roles[]   │  │              │
└──────────────────────────┘                  │  ──► --agent 选中   │  │ 轮次 claim 带 role │  │ 会话级记忆    │
        ▲ 提示词在这里评审/版本化                └────────────────────┘  └──────────────────┘  └──────────────┘
```

三条设计原则：

1. **单一真源**：提示词只存在仓库 `deploy/cloud-agent/roles/*.md`（可评审、可 diff、可 lint、进 CI）。
   平台**不存提示词正文**，只存"这个会话选了哪个角色"（一列）。执行体上的 md 由部署脚本安装，**不允许手工改**。
2. **共享词表**：角色 id 与**已有的 pack 阶段词表对齐**（`problem_analysis` / `modeling` / `coding` / `review` / `paper` / `delivery`，
   见 `docs/competition_workflow_adapter.json`），不发明第二套命名。
3. **不假装**：执行体上报的 `roles[]` 是"**实际可用**的角色"（来自 `opencode agent list`）；页面只显示执行体真有的角色；
   拉不到角色列表时**只显示「默认（无角色）」**，不显示一个点不动的假下拉。

数据流（与现有模型切换同构，不新增概念）：会话级 `role` → 轮次 claim 返回 `role` → 内核 `build_chat_command` 在 `{prompt}` 前插
`--agent <role>`（现有插入点，`apps/agent/chat_loop.py:49-72` 已经在插 `--session` / `-m`）。

---

## 3 角色清单（12 个）

「底料」= 数学建模工作流里的来源；「补写」= 工作流里没有、需要我新写的部分。

| id | 中文名 | 对齐阶段 | 底料（工作流内） | 补写量 | 输出契约（对话通道） | 工具边界（frontmatter） |
| --- | --- | --- | --- | --- | --- | --- |
| `mm-paper-zh` | 中文论文写作 | paper | `comp-paper-zh`(1575 行) + `writing-principles.md` + `scripts/writing_rules.md` + `citation-discipline.md` | 蒸馏 | 章节骨架 + 每章"必须写什么/禁止写什么" + 图注与引用规范 + **没有数据时的如实声明** | 只读 + 检索 |
| `mm-review` | 逻辑复核（挑硬错） | review | `comp-review`(83 行，**近全文可搬**) | 少量蒸馏 | findings 表（类别/严重性/定位/证据/修法）+ `fatal_count` 判定 + 天花板声明 | **只读**（`edit/write/bash: false`，独立性靠它） |
| `mm-research` | 文献检索 | 辅助 | **无独立技能**：`comp-modeling:178-186`（按方法族去重批量调研）+ 引用三段 + `citation-discipline.md`(493 行) | **新写角色句与证据卡契约** | 证据卡（来源/年份/结论/可用性）+ **引用必须核验或标注未核验** + 检索式留档 | 只读 + `webfetch`/`websearch` |
| `mm-analysis` | 审题分析 | problem_analysis | `comp-prob-analysis`(1011 行) | 蒸馏 | 关键概念对齐表 + 子问题个数判定 + 升级触发信号表 + 待确认项 | 只读 + 检索 |
| `mm-modeling` | 建模与求解设计 | modeling | `comp-modeling`(555 行) + `error_prevention.md`(2319 行) 八类题型 | 蒸馏 | 假设（含理由/参数化/替代假设）+ 方法声称清单 + 逻辑合同要点 + 灵敏度要求 | 只读 + 计算（`bash`） |
| `mm-coding` | 编程实现 | coding | `comp-code`(765 行) + `references/checks/` 7 篇 | 蒸馏 | 实现清单（逐条对照声称）+ 反降维红线 + **数据自检铁律（禁整读大结果）** + 结果回溯表 | `bash` + `write` + `edit` |
| `mm-critique` | 论文评审打分 | review | **只有碎片**（`comp-paper-zh-docx:578-593` 五问 + `comp-compile-zh:764-773` 过度声称词表） | **新写 rubric** | 六维评分（创新性/工作量/规范性/摘要/图表/引用）+ 权重 + **评分→返工建议映射** + 过度声称清单 | 只读 |
| `mm-figure` | 图表设计 | 辅助 | **有规则无角色**：108 配方 + `figure_style_guide.md`(1386) + `figure_exemplars.md`(550) + `drawio_rules.md` + `tikz_rules.md` | **新写角色句+选型决策+图表清单契约** | 图表清单（图号/类型/数据来源/要证明什么）+ 图后解读三要素 + 禁止硬编码编数据 | 只读 + 计算 |
| `mm-compile` | 编译与合规 | delivery | `comp-compile-zh`(823 行，**22 项 FINAL GATE** + 省重编修复顺序) | 蒸馏成检查表 | 检查表（22 项缩成对话可执行版）+ 修复顺序 + **禁止凑页三件套** + 引用格式修复 | `bash`（编译）+ `edit` |
| `mm-paper-zh-docx` | 中文论文（Word） | paper | `comp-paper-zh-docx`(1231 行) | 蒸馏 | 与 `mm-paper-zh` 同构，但**保留 docx 禁令**（禁 `.tex`/`.cls`/`\begin` 等一切 LaTeX 命令） | 只读 |
| `mm-paper-en` | 英文论文写作 | paper | `comp-paper-en`(641 行) | 蒸馏 | MCM 结构（Summary Sheet 权重最高）+ 英文图注 ≤14 词 + 三要素解读 | 只读 + 检索 |
| `mm-topic` | 选题与数据规划 | problem_analysis | `comp-stats-topic`(204 行) | 蒸馏 | 四类研究设计框架（因果/预测/分类聚类/综合评价）分支 + 题目具体化与数据可得性检查 | 只读 + 检索 |
| *（无角色）* | 默认 | — | opencode `build`（现状） | 零 | 不变（全工具） | 全开 |

**页面上的分组**（工具条下拉，12 项按阶段分组）：写作（zh / en / docx）· 审核（逻辑复核 / 评审打分）· 检索 · 建模流水线（审题 / 建模 / 编程 / 编译）· 辅助（图表 / 选题）· 默认。

**明确的边界**：对话通道的角色**不产 PDF、不跑流水线、不写文件合同**（`mm-coding`/`mm-compile` 例外地能跑命令与改文件，页面上标注"该角色会改动工作目录"）。
需要全流水线时走**项目工作**（任务 + pack），那是另一条通道——本期只把"方法内核"文字共享过去，不做机制统一。

---

## 4 提示词撰写规范（**专门步骤**——"足够详细"的机械判据）

这是本计划里用户点名要的那一步：**提示词撰写不是一个动作，是有标准、有台账、有 lint、分批交付的工序**。

### 4.1 九段骨架（每个角色 md 必含，标题固定）

1. **角色定位**——"你是谁、你负责什么、你不负责什么"（3–5 句，含"何时该被启用"）
2. **启用与不启用**——列出典型请求（"帮我看看这道题怎么建模"→ 启用）与不该用它的请求（"帮我写周报"→ 拒绝或建议换角色）
3. **输入与前置**——本平台的事实：用户消息里可能带 `[本轮提供了 N 个文件，已放在工作目录的 inputs/ 下：…]` 前缀；
   带 `[以下附件没有取到（不要假设它们存在）：…]` 时必须**如实说"我没有拿到这个文件"**，不许假装读过
4. **工作方法**——分步骤（先做什么、再做什么），每步给判据
5. **硬规则（禁令）**——≥8 条，动词开头（禁止/必须/不得），带阈值或具体判据
6. **输出格式**——给**可复制的骨架**（代码块里的表头/字段名），让人拿到就能用
7. **自检清单**——≥5 条，每条**可判定**（"我是否对每个结论标了数据来源？"这种不算；"每个数值是否都能指到具体文件/表格？"才算）
8. **不假装条款**——拿不到数据/没核验引用/做不到的事，**如实标注**，禁止编造与"合理估计后直接当结论"
9. **边界与不做**——明确不做什么（防角色膨胀）

### 4.2 蒸馏原则（从 9,395 行工作流里"抽什么、丢什么"）

| 保留（角色内核） | 丢弃（属于项目工作通道） |
| --- | --- |
| 硬规则、禁令、量化阈值（如"加粗全篇 ≤12 处""图注中文 ≤20 字""假设 4–5 条"） | 文件路径与脚本调用（`_utils/…`、`scripts/…`、`figures/*.json`） |
| 方法骨架与判据（方向推导四步、升级触发表、六类缺口、22 项 Gate 的判据） | 闸门的**执行机制**（跑哪个脚本、退出码、CI 怎么拦） |
| 输出契约（章节结构、findings 字段、证据卡字段） | 上游/下游章节（"本步骤依赖 X、产出给 Y"） |
| "不假装"与溯源纪律 | 工作流的进度管理话术（"不要 end_turn""跑完自问 4 条"→ 改写为对话可用的自检） |

**术语对齐**（写进每份提示词的开头术语段）：子问题/关键概念对齐表/假设问责表/方法声称清单/逻辑合同/证据卡/两级严重性（fatal·major·minor）——
与工作流同名，不另造词。

### 4.3 体量上限与 token 预算（有实测依据）

- **上限：每份 3,000–12,000 字符（中文）**，对应约 1.5k–6k tokens。
- 依据：opencode **每一步都重发系统提示**——探针那次 3 行提示的 input 就是 **3,545 tokens**（内含 opencode 自身的工具与指令开销）。
  一个 6k tokens 的角色提示 + opencode 开销 ≈ 每步 ~9k input；一轮 5 步 ≈ 45k input。
  所以：**上限不是为了省字数，是为了每步的延迟与成本**；超上限的角色必须拆成"核心 + 可选附录"（附录放在 md 末尾的 `## 附录` 段，运行时并不裁剪——只是提醒编者"正文要能独立成立"）。
- 每个角色交付时**记录实测 token 增量**（同一问题、有角色 vs 无角色各跑一次，取 `step_finish.tokens.input` 对比）。

### 4.4 `scripts/prompt_lint.py`（把"足够详细"变成机械判据）

对每个角色 md 做静态检查，**不过 lint 不算交付**：

1. frontmatter 必含 `description` / `mode`；**禁止**写 `model`（会与平台的模型下拉打架——优先级见 §6 的实测项）
2. 九段骨架**标题齐全**（按固定措辞匹配）
3. 硬规则段 ≥8 条，且 ≥6 条以「禁止/必须/不得/一律」开头
4. 输出格式段含至少一个代码块（可复制骨架）
5. 自检清单 ≥5 条，且 ≥3 条含具体字段名或阈值（数字/文件名/枚举）
6. 长度落在 3,000–12,000 字符
7. **泄漏检查**：不得出现 `_utils/`、`scripts/`、`figures/`、`CLAUDE.md`、`user_data/` 等仓库内路径，也不得出现"脚本会替你校验"这类**依赖本地资产**的表述
8. **不假装检查**：必须出现"如实标注/不许编造/拿不到就说拿不到"类条款（反向检查，缺失即 FAIL）
9. 公式写法：中文角色用 `$...$`/`$$...$$`；不得出现未闭合的 `\begin{`

### 4.5 来源台账 `deploy/cloud-agent/roles/SOURCES.md`

每个角色一个条目：**哪几条硬规则来自工作流的哪个文件哪一段**（`skills/comp-review/SKILL.md:40-51` 这种），哪些是**新补**的（写明"新补 + 理由"）。
作用：① 让"从工作流来"这件事可核查，不是口头声明；② 工作流更新时知道该同步哪个角色。

---

## 5 分期（每期独立可上线、独立可验）

| 期 | 内容 | 改动面 | 验收（实测） |
| --- | --- | --- | --- |
| **R-1 机制落地**（0.5–1 天）✅ **已交付** | ① 仓库 `deploy/cloud-agent/roles/`（**只放 1 个样板角色**）；② `install-cloud-agent.sh` 安装 md 到 `~/.config/opencode/agent/`（0644，属主 synapforge）；③ 内核探测 `opencode agent list`（在 workspace 里跑，超时兜底）→ 心跳 `resource_summary.roles`；④ 会话级 `role` 列（迁移 025）+ 契约 + claim 回传；⑤ `build_chat_command` 增 `--agent`（同一插入点，加单测）；⑥ 页面工具条加角色下拉（数据来自心跳；拉不到就只有「默认」） | 内核 3 文件 + API 2 文件 + 迁移 + 页面 | 真跑一轮：执行体日志命令行**确实带 `--agent mm-…`**；回复体现角色；关掉角色后回到现状；**执行体缺角色时页面显示「默认」而不是空下拉** |
| **R-2 三个点名角色**（1.5–2 天）✅ **已交付** | `mm-paper-zh`（写作）/ `mm-review`（审核）/ `mm-research`（检索）三份提示词 + lint + 台账 + 各自真跑验收 | 只写 md + lint + 部署 | 三个角色各真跑 1–2 轮：写作给**章节骨架+规范**而不是伪造数值；审核给 **findings 表 + fatal 判定**；检索给**证据卡 + 未核验标注**。`mm-review` 试图写文件应**无 `file.changed` 事件**（工具边界真生效） |
| **R-3 流水线四角色**（2 天）✅ **已交付** | `mm-analysis` / `mm-modeling` / `mm-coding` / `mm-figure` | 同 R-2 | 每角色一轮：审题给**对齐表 + 子问题个数**；建模给**假设三件套 + 方法声称**；编程给**实现清单 + 自检铁律**；作图给**图表清单 + 每图"要证明什么"** |
| **R-3b 收尾五角色**（2 天）✅ **已交付** | `mm-critique` / `mm-compile` / `mm-paper-zh-docx` / `mm-paper-en` / `mm-topic` | 同 R-2 | 评审给**六维评分 + 返工映射**；编译给**检查表 + 修复顺序**；docx 版**不出现任何 LaTeX 命令**；英文版给 **Summary Sheet 优先**结构；选题给**四类框架分支** |
| **R-4 细节复刻**（0.5–1 天） | ① 实测优先级：角色 md 里的 `model` / `tools` 与命令行 `-m` / `--variant` 谁赢（决定 §4.4 那条禁令能不能放开）；② 抽屉里展示角色说明与硬规则摘要（来自 manifest）；③ 上报角色文件 **sha256**，页面标注"角色定义版本"防执行体与仓库漂移；④ 角色切换后的文案（"下一轮生效"） | 内核 + 页面 | 命中：能指出命令行里哪条参数生效；抽屉能看角色说明；仓库改了 md 未重装时页面**能看出不一致** |
| **R-5（后继，不在本期）** | ① 项目工作通道复用同一份"方法内核"（把 roles/*.md 注入任务 prompt）；② 自定义角色（平台端编辑并下发给执行体）；③ 角色级权限卡片（与 M-5c S-3 合流） | — | 另立计划 |

**交付节奏**：R-1 + R-2 上一个版本（约 2–3 天，**第一次让用户看到"角色"这件事真的在用**）；R-3 / R-3b 分批上；R-4 随后。

---

## 6 与 M-5c（真流式）/ M-5b（模式·强度）的关系与排期

**关键判断：M-5b 与 M-6 共用同一条管道**——「会话级设置 → claim 回传 → 内核插入命令行 → 心跳探测 → 页面下拉」这套，角色的 `--agent` 与模式的 `--agent`/`--variant` 是**同一个插入点、同一处探测、同一处下拉**。所以**合并做一次**，别分两遍改内核、两遍部署、两遍测试。

建议顺序（理由：先做**不碰执行链**的部分，再做高风险的内核改造）：

1. **M-6 R-1 + R-2**（角色机制 + 三个点名角色）——不碰 `opencode run` 的执行链，只改"命令怎么拼"。
2. **M-5c S-0**（在云端采 `opencode serve` + SSE 的真实事件形状）——**零风险、半步时间**，随时可插在前面或并行（它只读不写线上）。
3. **M-5b 并入 M-6 R-4**——模式（`--agent` 之外 opencode 还有 `--variant` 强度）与角色同管道，一次实测、一次上线。
4. **M-5c S-1 → S-2 → S-3 → S-4**——常驻 server 与逐字流式，是内核最脆弱的改造，放在角色预设第一批交付**之后**，别让它挡住"角色"这个立刻可见的价值。
   （若流式先做完，S-3 的权限卡片会顺带把 `--auto` 全自动换掉——那时角色与权限的关系要在 S-3 里一并设计。）

---

## 7 风险与边界

1. **提示词质量是最大变量**：角色好不好用几乎全看提示词（机制是现成的）。对策 = §4 的九段骨架 + lint + 台账 + **真跑验收**（每角色至少一轮）。
2. **每步重发系统提示的成本**：角色越长，每步越贵越慢（§4.3 有实测依据）。对策 = 体量上限 + 交付时记录 token 增量。
3. **漂移**：执行体上的 md 与仓库不一致（手工改、忘记重装）。对策 = R-4 上报 sha256 + 页面上可见；部署脚本是唯一安装途径。
4. **角色与模型/强度的冲突**：若角色 md 里写了 `model`，可能与页面选的模型打架（真优先级未测）。对策 = lint 禁止写 `model`；R-4 实测后再决定是否放开。
5. **`websearch` 能力未验证**：opencode 有 `websearch` 工具，但百炼 provider 下是否真能用**没测过**。对策 = R-2 里先测；拿不到就让 `mm-research` 靠 `webfetch` 打具体站点/API，并在角色里写清"检索不到就说检索不到"。
6. **对话通道不等于流水线**：角色**不产 PDF、不跑闸门**。页面上要标注，避免用户以为"选了编译角色就能拿到 PDF"。要全流水线请走**项目工作**。
7. **不做**：角色市场/分享、平台端在线编辑提示词（R-5 才可能）、把 12 个阶段的全套脚本与模板装到执行体（那是项目工作通道的事）、角色级计费。
8. **不做重复真源**：平台侧**不存提示词正文**（只存"选了哪个角色"）。谁想改提示词，改仓库、重新部署——这条守住，才不会出现"平台改一版、执行体跑另一版"。

---

## 8 验收口径总表（怎么算完成，先说好）

- **机制**：真跑一轮里，执行体日志的命令行**带 `--agent <角色 id>`**；角色 id 与 `opencode agent list` 里的一致。
- **可见性**：页面角色下拉的选项**等于执行体上报的角色集合**（不是硬编码列表）；执行体没有角色时只有「默认」。
- **角色真的生效**：该角色的一轮回复里出现**只有它才有的输出形状**（审核→findings 字段；检索→证据卡字段；写作→章节骨架）。
- **工具边界生效**：只读角色在被要求改文件时**不产生 `file.changed`**，并如实说"我不能改文件"。
- **提示词质量**：`prompt_lint.py` 全绿 + `SOURCES.md` 有台账 + 长度在区间内。
- **不假装**：故意给一个不存在的附件名，角色必须回答"没有拿到这个文件"，不得编造内容。
- **token 账**：每个角色有一组"有/无角色"的 input token 实测对比记录。

---

## 附录 A 本次实测样本原文（2026-09-23，执行体 `154.219.99.75`）

**探针角色定义** `/home/synapforge/.config/opencode/agent/probe-role.md`（用完即删）：

```markdown
---
description: probe role for mechanism check
mode: primary
tools:
  bash: false
  edit: false
  write: false
---

你是「探针角色」。无论用户问什么，你的回复必须以 ROLE_OK 开头，然后一句话自我介绍。
```

**`opencode debug agent probe-role` 的关键字段**（截去 permission 数组）：

```json
{
  "name": "probe-role",
  "mode": "primary",
  "options": {},
  "native": false,
  "prompt": "你是「探针角色」。无论用户问什么，你的回复必须以 ROLE_OK 开头，然后一句话自我介绍。",
  "description": "probe role for mechanism check",
  "tools": { "invalid": true, "question": false, "bash": false, "read": true, "glob": true, "grep": true,
             "edit": false, "write": false, "task": true, "webfetch": true, "todowrite": true, "skill": true }
}
```

**真跑一轮的事件流**（`opencode run --agent probe-role --format json '你是谁？' </dev/null`，逐字）：

```jsonl
{"type":"step_start","timestamp":1790158934186,"sessionID":"ses_f32368c6cffeyLytj0uKWiVp2l","part":{"id":"prt_0cdc990910018bT4JBJruMA3V4","messageID":"msg_0cdc97ec7001zf9Zc3cvO5gL54","sessionID":"ses_f32368c6cffeyLytj0uKWiVp2l","type":"step-start"}}
{"type":"text","timestamp":1790158935015,"sessionID":"ses_f32368c6cffeyLytj0uKWiVp2l","part":{"id":"prt_0cdc9926f0018FRHR4bG0hFcWf","messageID":"msg_0cdc97ec7001zf9Zc3cvO5gL54","sessionID":"ses_f32368c6cffeyLytj0uKWiVp2l","type":"text","text":"ROLE_OK 我是「探针角色」，一个用于探测和验证系统响应行为的角色实例。","time":{"start":1790158934639,"end":1790158934946}}}
{"type":"step_finish","timestamp":1790158935015,"sessionID":"ses_f32368c6cffeyLytj0uKWiVp2l","part":{"id":"prt_0cdc993af001P64DXyLb1OmsWo","reason":"stop","messageID":"msg_0cdc97ec7001zf9Zc3cvO5gL54","sessionID":"ses_f32368c6cffeyLytj0uKWiVp2l","type":"step-finish","tokens":{"total":3592,"input":3545,"output":22,"reasoning":25,"cache":{"write":0,"read":0}},"cost":0}}
```

→ 结论：**md 正文当了系统提示、`--agent` 生效、工具开关生效、token 账可读**。

---

## 附录 B 底料来源索引（工作流 → 角色）

工作流根：`C:\Users\19855\.agents\skills\math-modeling-competition\`（同一份内容在工作区 `competition-workflow/` 下另有一份）。

| 角色 | 直接底料（相对路径:行） | 需补写的部分 |
| --- | --- | --- |
| `mm-review` | `skills/comp-review/SKILL.md:8-11`（角色定位段，**可整段搬**）、`:26-31`（输入"只读摘要、禁整读大 JSON"）、`:40-51`（六类检查项 + 天花板声明）、`:53-68`（输出 schema：`findings[].category/severity/where/evidence/fix` + `fatal_count`）、`:71-83`（fatal 硬门禁） | 把"脚本路径/退出码"改成对话可用的判据；补"对话场景下如何要材料"（要求用户贴结果或附文件）。**工具边界有底料支撑**：原文 frontmatter 就是 `allowed-tools: Bash(*), Read, Grep, Glob, Agent`（无 Edit/Write），所以"只读角色"不是我发明的 |
| `mm-paper-zh` | `skills/comp-paper-zh/SKILL.md:44-62`（章节结构）、`:344-358`（模板铁律）、`:431-508`（摘要最后写 + 分段 + 加粗 ≤12）、`:668-686`（图后解读三要素 + 反例/正例）、`:795-800`（数值必须来自结果文件）、`:1049-1057`（去 AI 痕迹：正文禁 itemize）、`:1091-1129`（引用：上标/全局递增/禁编 BibTeX）、`:1288-1296`（页数扩展防幻觉表）；`references/writing-principles.md`；`scripts/writing_rules.md`；`references/citation-discipline.md` | 无（蒸馏为主）；需在开头补"对话通道没有你的结果文件，用户必须贴数据" |
| `mm-research` | `skills/comp-modeling/SKILL.md:178-186`（按方法族去重批量调研）、`skills/comp-paper-zh/SKILL.md:776-791` 与 `skills/comp-paper-en/SKILL.md:223-238`（文献预检索 + 引用池）、`references/citation-discipline.md`（五步验证 + 5 类失败排障 + `[VERIFY]` 占位） | **角色句 + 检索式改写策略 + 证据卡字段 + "未核验"标注法**（工作流无独立检索技能） |
| `mm-analysis` | `skills/comp-prob-analysis/SKILL.md:33-42`（完成铁律）、`:230-239`（子问题识别规则）、`:241-264`（关键概念对齐表 + 作用对象排除式 + 铁律）、`:346-365`（能力清单 + 可证伪断言）、`:398-403`（数据事实台账 role 三分 + `given` 段）、`:471-490`（图表预规划硬规则）、`:725-848`（逐句自检 + 升级触发表） | 蒸馏；去掉 `_utils/*.py` 调用 |
| `mm-modeling` | `skills/comp-modeling/SKILL.md:94-133`（不能无声降级 + 不合法理由）、`:152-174`（假设三件套）、`:221-231`（参数口径表）、`:312-335`（方法声称 + must/forbid）、`:337-364`（逻辑合同 8 类 + 方向推导四步）、`:378-396`（假设问责表 + 目标/约束原文溯源）、`:550-555`（通用禁止声明）；`scripts/error_prevention.md` 八类题型 | 蒸馏 |
| `mm-coding` | `skills/comp-code/SKILL.md:107-113`（忠实实现铁律 + 反降维红线）、`:117-135`（约束闭环审计 + 基线同审）、`:455-495`（数据自检铁律 A/B）、`:519-546`（`validate_capability` 硬断言）、`:705`（考官角色：拿证据核）、`:711-714`（不画图职责边界）、`:728-742`（无数据时生成数据质量规范）；`skills/comp-code/references/checks/`（7 篇） | 蒸馏；对话通道下"能改文件"要显式提示 |
| `mm-critique` | `skills/comp-paper-zh-docx/SKILL.md:578-593`（五问 + 评分 1-10）、`skills/comp-paper-en-docx/SKILL.md:452-461`（英文同构）、`skills/comp-compile-zh/SKILL.md:764-773`（过度声称词表） | **六维 rubric + 权重 + 评分→返工映射**（工作流无评委视角） |
| `mm-figure` | `scripts/figure_style_guide.md`（选型决策表 + 配色 + 15 组技巧）、`scripts/figure_exemplars.md`（领域标志图触发表）、`scripts/figure_recipes_*.md`（108 配方）、`scripts/tikz_rules.md`、`scripts/drawio_rules.md`、`skills/comp-prob-analysis/SKILL.md:471-490`（FIGURE_MANIFEST 硬规则） | **角色句 + 图表清单字段 + 图后解读三要素 + 禁硬编码编数**（工作流只有规则库，无 `paper-figure` 角色文档） |
| `mm-compile` | `skills/comp-compile-zh/SKILL.md:185-248`（编译流程与错误修复环）、`:315-321`（合规 6 项）、`:356-394`（页数铁律 + 禁止凑页三件套）、`:397-784`（22 项 FINAL GATE）、`:788-796`（省重编修复顺序）；`scripts/compile_check.sh` 的判据 | 蒸馏成"对话里能照做的检查表"；去掉脚本调用 |
| `mm-paper-zh-docx` | `skills/comp-paper-zh-docx/SKILL.md:46-53`（禁产 LaTeX 系列硬清单）、`:811-856`（图号起句去套路化检测） | 蒸馏 |
| `mm-paper-en` | `skills/comp-paper-en/SKILL.md:41-53`（MCM 结构 + Summary Sheet 权重）、`:209`（解读三要素）、`:216`（图注 ≤14 词） | 蒸馏 |
| `mm-topic` | `skills/comp-stats-topic/SKILL.md:31-36`（四类研究设计框架）、`:196-204`（题目/数据/方法/创新/查重五条） | 蒸馏 |

**已确认与本计划无关**：`skills/feishu-notify`（工具型，无角色设计需求）；`templates/`（工作流输入模板，不是角色提示词）；
`scripts/humanities-*.md`（捆绑的人文社科资源，与本流水线无关）。