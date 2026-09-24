# MY-AGENT 计划：「我的智能体」——把 AI 问答页换成 opencode 对话工作页

> 2026-09-23 起草 · **v4：M-1（对话通道）、M-2（页面）、M-3（附件）都已完成并线上验证；M-4 待做** · 用户已拍板「对话分两类」
> 用户诉求（原话要点）：把「AI 问答」页换成「我的智能体」并成为主页面；一个**完整的 opencode 对话页面**，
> 包含所有功能（上传文件、切换模型……）；因为 agent 已经接到平台了，所以要一个**专门的对话工作页**，像 agent 对话工作台。
> 上游：`docs/CLOUD_1_CLOUD_AGENT_PLAN.md`（云端执行体已上线，P1 六项全绿）、`docs/handoffs/CLOUD_1_OPENCODE_ADAPTER_HANDOFF.md`（事件协议与真实样本）、`docs/handoffs/W1_PROJECT_WORKSPACE_HANDOFF.md`（工作区聊天 + WS 推送）

---

## 1 结论先说

**能做，而且大部分管道已经通了**——云端执行体（opencode）已经在跑真任务，事件流、用量、成果物、WebSocket 推送都在。
缺的不是"能不能对话"，而是三件**表达力**上的事：

| 缺什么 | 今天的状态 | 本计划怎么补 |
| --- | --- | --- |
| **会话**（多轮上下文） | 内核每个任务新起一个进程，没有会话概念 | 借 **opencode 自己的 session**（`--session`）：内核把事件里的 `sessionID` 上报，平台存进会话，下一轮把 `--session <id>` 写进命令模板。**上下文由执行体维护，平台只存句柄** |
| **模型** | 只能改服务器上的 `opencode.json` 默认值 | 内核探测 `opencode models` → 随心跳上报 → 平台缓存到 Agent 行 → 页面下拉选择 → 每轮把 `-m provider/model` 写进模板 |
| **输入文件** | 只有"产出上传"是通的；`input_artifacts` 平台有、Agent **完全不读**，也没有成果物下载路由 | 加 **Agent 专用下载路由** + 内核把 `input_artifacts` 落到 workspace（执行体就能读到） |

**关键的架构选择（我的推荐）**：对话的每一轮**仍然是一个 task + run**，不新造"聊天轮次"概念。
理由：任务体系已经自带 **审计、用量、预算、事件、成果物、通知、可追溯**；新造一套聊天存储会把这七样全部重写一遍，
而且要把事件类型白名单、Gateway 校验、门禁全部改一遍。代价是"对话任务"会出现在任务板里——
用 `stage="chat"` + 专用项目（「云端智能体」）隔离，任务板加一个"隐藏对话类任务"的默认过滤即可。

---

## 2 现状：能直接复用的（探查结论，带出处）

| 能力 | 现状 | 复用方式 |
| --- | --- | --- |
| 执行体接线 | 设备 `device-cloud-01` / Agent `agent-cloud-01` 在线，`worker_executor="cli"` + opencode 模板 | 直接用作对话的执行体 |
| 过程事件 | `ExecutorEventReporter` → Gateway `agent.event` → 平台 `agent.process.started / agent.agent.message / agent.tool.completed / agent.file.changed / agent.process.exited` | 对话页直接渲染这些事件 |
| 实时推送 | `/ws/projects/{id}` + `ConnectionManager` + 前端 `lib/workspace.tsx` 的 `subscribeMessages` | 加一种 `agent.delta`/复用 `project.message` 帧即可驱动对话流 |
| 用量 | Run 的 `usage`（`source=opencode-jsonl`）已上线 | 每轮对话显示 token/耗时 |
| 文件出口 | 云盘 → object store → 项目成果物（`drive/{owner}/{file_id}`） | 上传仍走这条，导入为成果物 |
| opencode 事件解析 | `apps/agent/opencode_executor.py` 有 `parse_opencode_jsonl`（回复/工具/文件/用量/cost），且有**真实样本测试** | 扩展出 `sessionID` 与 model 字段 |
| 导航 | `apps/web/lib/nav.ts`（6 组，`/ask` 在「高级工具」组） | 把 `/ask` 改成 `/my-agent`「我的智能体」，提到主组 |

**今天明确没有的**（本计划要补）：会话键、模型目录、输入文件投喂、**中断通道**（Gateway 没有 stop/cancel 命令，
平台取消任务只是改状态，执行体会继续跑）、租约续期（长对话超过 900 秒租约会被平台回收——需要在预算/租约上兜）。

---

## 3 交互模型（页面长什么样）

```
┌──────────────┬────────────────────────────────────────────┬─────────────────┐
│ 我的智能体    │  [设备/Agent ▾ 云端执行体 · opencode]        │ 本轮执行         │
│ ─────────    │  [模型 ▾ deepseek/deepseek-v4.1-flash]      │  · 状态          │
│ ＋ 新对话     │ ──────────────────────────────────────────  │  · 工具/文件      │
│ ─────────    │  你：帮我读一下 summary.md 并改错别字         │  · token/耗时     │
│ 今天          │  智能体：先看文件…（流式）                    │  · 产出成果物      │
│  · 校对文档   │  ⟐ 读取文件 summary.md（工具卡片）            │                 │
│  · 起个模型   │  智能体：改完了，3 处错别字（正文）            │  会话信息        │
│ 更早 …        │ ──────────────────────────────────────────  │  · opencode 会话 │
│              │  [📎] 说点什么…（Enter 发送，Shift+Enter 换行） │  · 项目           │
└──────────────┴────────────────────────────────────────────┴─────────────────┘
```

要点：**左**会话列表（今天/昨天/更早，可删）；**中**对话流 = 人类气泡 + 智能体正文 + 工具/文件卡片 + 每轮的执行状态；
**右**本轮执行细节（状态、工具、token、产出、可跳成果物）；**底部** composer（附件、模型选择、设备选择、发送/停止）。
「停止」在 M-4 之前是**假停止**（平台侧取消任务，执行体仍会跑完）——界面上要如实标注，别假装能中断。

---

## 4 设计：数据与接口

### 4.1 平台侧新增（迁移 `024_agent_conversations.sql`）

- `agent_conversations`：`id / member_id / project_id / device_id / agent_id / title / model / session_key / created_at / updated_at / archived_at`
  （`session_key` = opencode 的 `ses_…`，第一轮跑完由内核上报；模型默认取设备探测列表的第一个）
- `agent_turns`：`id / conversation_id / seq / role(user|assistant) / content / task_id / run_id / status / created_at`
  —— 一轮 = 一条 user 记录 + 一条 assistant 记录（assistant 的 content 由 Run 摘要/事件回填）
- `agents.available_models`（JSON 文本）：设备探测到的模型列表（心跳带上来；只读缓存，不是真值来源）

### 4.2 新接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/my-agent/agents` | 可用执行体列表（在线的、能跑 opencode 的设备）+ 各自模型列表 + 会话数 |
| GET/POST/DELETE | `/api/my-agent/conversations` | 会话 CRUD |
| GET | `/api/my-agent/conversations/{id}/turns` | 会话的轮次（含每轮 task/run/状态） |
| POST | `/api/my-agent/conversations/{id}/messages` | 发一条 → **建一个对话任务**（见 4.3）→ 返回 `task_id` |
| POST | `/api/my-agent/conversations/{id}/stop` | 取消当前轮（M-4 前只改任务状态，界面如实标注） |
| GET | `/api/agent/artifacts/{id}/content` | **Agent 专用下载**（项目能力令牌；M-3 用） |

### 4.3 一轮对话 = 一个任务（关键设计）

发消息时平台建任务：

```json
{
  "title": "对话：<前 30 字>",
  "stage": "chat",
  "description": "<本轮用户消息全文>",
  "assignee_member_id": "<设备归属人>",
  "requires_review": false,          // 对话不该卡在人工复核
  "budget": {"max_seconds": 900, "max_attempts": 1},
  "input_artifacts": ["<本轮附件的成果物 id>"],
  "resource_policy": {
    "worker_executor": "cli",
    "worker_command": ["opencode","run","--format","json","--auto", "--session","<ses_… 若有>", "-m","<模型>", "{prompt}"],
    "worker_events": "opencode",
    "worker_prompt": "<本轮消息>"
  }
}
```

- 会话续接靠 `--session`：**第一轮不带**，内核从事件里读到 `sessionID` 上报 → 平台写入 `session_key` → 之后每轮带上。
- 模型切换就是改写 `-m`（模板是任务声明的，**平台侧就能改，内核不用为模型改动**）。
- `stage="chat"` 让对话任务在任务板可过滤；`requires_review=false` 避免每句话都要审批。

**这个设计已经用真实执行体验证过（2026-09-23，在云端执行体那台机上）**：

```
第 1 轮（不带 --session）："Remember this code word: BANANA" → 回复 STORED，事件里拿到 sessionID=ses_f3384abe7ffem1JeVqvfn2lqxk
第 2 轮（--session ses_f3384abe…）："What was the code word?" → 回复 BANANA ✅
  该轮用量：input=204（cache read=7168）——上下文留在会话里，只传增量，比"把历史拼进 prompt"便宜得多
```
即：**跨进程的多轮上下文成立**，`sessionID` 在每一行事件里都有（顶层字段），内核取它不费事。

### 4.4 内核侧改动（小，三处）

1. **上报 `sessionID`**（`opencode_executor.parse_opencode_jsonl` 增读 `sessionID`）→ 放进 Run 完成载荷与摘要
   （新字段 `session_key`，平台存进会话）。**没有它就没有多轮上下文。**
2. **模型探测**：`agent_inventory` 增一项 `opencode models`（超时 5 秒、失败如实记 ERROR），
   结果随心跳 `resource_summary.models` 上报（`available_models` 之外的字段不动）。
3. **输入文件落地**（M-3）：`input_artifacts` → 逐条下载到 `<workspace>/inputs/`，文件名去重、大小上限、失败即失败
   （不静默跳过）；复用 `AgentArtifactClient` 加一个 GET 下载方法。

---

## 5 分期与验收

| 期 | 内容 | 验收（实测） |
| --- | --- | --- |
| **M-1 会话 + 模型（后端与内核）** | 迁移 024、`my-agent` 接口组、内核上报 `session_key` + 模型探测、平台缓存模型；对话轮 = 任务桥 | 平台机能用 curl 建会话→发两轮→第二轮任务**带上一轮的 `--session`**；`/api/my-agent/agents` 返回真实模型列表；Agent 套件全绿 |
| **M-2 页面（前端）** ✅ **完成**（含用户追加的三处调整） | `/my-agent`：两模式 tab 在**标题右上角**、左上角不放标题文字（H1 视觉隐藏保留语义）、**两栏**（会话列表 / 对话流+composer）、执行细节改成**右侧抽屉**按需展开、设备与模型下拉（**来自执行体真探测**）、过程事件卡片、自动选中最近会话；导航与手机底栏把「AI 问答」换成「我的智能体」 | ✅ 线上真机（桌面 1440 + 手机 390）：页面上发消息 → 回复 + 用量 + 过程事件；无横向溢出；测试会话已清理 |
| **M-3 文件** ✅ **完成** | Agent 专用下载路由（`GET /api/agent/artifacts/{id}/content`，只要求 `artifact.read`，**不要求成果物属于该 Agent**）+ 内核 `apps/agent/input_fetcher.py`（落到 `<workspace>/inputs/`、真名优先、重名加序号、挡路径穿越、上限拒绝、失败如实回报）+ 对话轮次带附件（`artifact_ids` → 轮次存 id+名 → claim 回传） + 任务 `input_artifacts` 也已接上；页面输入区加附件按钮（云盘→成果物→随本轮） | ✅ 线上实测：上传 md → 导入成果物 → 对话带附件 → 执行体 `[inputs] 已落到 inputs/sample.md` 并**读出文件内容回复**（回复 `M3-INPUT-OK`） |
| **M-4 打磨** | 中断（要新增平台→Agent 的 cancel 命令）、多设备、历史检索、把 RAG 问答并成"资料模式"（可选） | 中断真能杀掉执行体的进程；多设备切换后会话沿用各自 session |

每期都独立可用；M-1+M-2 是"能用"，M-3 是"好用"，M-4 是"顺手"。

---

## 5.0 页面形态（按用户反馈定稿）

- **主区域留给对话**：左上角不放任何标题文字（`h1` 用 `.sr-only` 视觉隐藏，保留读屏与导航语义）；
- 模式切换（对话 / 项目工作）放在**标题右上角**（复用 `PageHeading` 的 `actions` 插槽）；
- 会话列表在左（252px），对话流 + 输入区占满其余宽度；
- **本轮执行细节是右侧抽屉**：默认收起，点工具条上的「执行细节」展开（运行中有绿点提示），带遮罩，点遮罩收起；
- 实测坑：收起的抽屉虽然 transform 到屏幕外，仍会**撑出滚动宽度**（外层要 `overflow: hidden` 裁掉，
  否则 `.main-area` 长出横向滚动条）；回答头用 `inline-flex` 会和正文挤在同一行（改块级 flex）。

## 5.1 部署坑（M-2 实测，必记）

**生产构建必须内联 API 地址**：`NEXT_PUBLIC_API_URL` 不设时前端会编译成 `http://127.0.0.1:8000`
（`lib/api.ts` 的默认值），线上页面于是**所有接口都 Failed to fetch**——首次重建时漏了这一步，
是浏览器实机验证才发现的。正确做法（与 `/root/deploy/server_release.sh` 一致）：

```bash
cd /opt/math-agent-platform/apps/web
export NEXT_PUBLIC_API_URL="https://synapforge.top"   # 必须是域名入口
npm run build -- --no-lint && systemctl restart map-web
```

另外两条视觉坑（都在真机上抓的）：flex 子项缺 `min-width: 0` 会让长输出把整页顶出横向滚动条；
`inline-flex` 的胶囊按钮在 grid 父容器里会被拉伸成整行（要 `justify-self: start` + `width: fit-content`）。

## 6 明确不做（本计划边界）

- **不做真正的流式 token 输出**：opencode 的 `--format json` 是**事件级**（一段一段的 message），
  不是逐字流；要做到逐字得换协议（`--print-logs`/`opencode serve` attach），那是另一个工程。页面上呈现的是
  "一段话 + 工具卡片"，不是打字机效果——**界面上不假装**。
- **不新造聊天存储**：对话轮落在任务/Run 体系里（§1 的理由）。
- **不做多实例并发同一会话**：内核一个实例串行跑任务，同一会话的两轮天然排队；不做"打断当前轮再插队"。
- **不在平台侧存模型 key**：模型目录只是"这台设备装了哪些模型"，key 仍在执行体的 0600 配置里。
- **不把 `/ask` 的 RAG 检索删掉**：知识库问答接口保留（`/kb` 页仍在用）；只是把「AI 问答」这个入口换成「我的智能体」。

---

## 7 风险（要如实说的）

1. **租约 900 秒不续期**：长对话（比如让智能体跑十几分钟）可能被平台按 TTL 回收；本计划用
   `budget.max_seconds` 兜，并在 M-4 评估续租。
2. **中断是缺的**：M-4 之前"停止"只改任务状态，执行体会跑完（界面标注）。
3. **opencode 会话是执行体本地状态**：换设备/清状态目录后 `--session` 失效——平台要能识别
   "会话已失效"并自动开新会话（M-1 的验收里包含这条的兜底：`session` 失效时退化为无 `--session`）。
4. **`sessionID` 只在事件流里**：若某轮执行体没吐事件（例如提前失败），会话键拿不到 → 该轮退化为无上下文。

---

## 8 拍板结果（2026-09-23）

1. **对话分两类**（用户定的）：
   - **「项目工作」**：跑任务的——沿用现有 Task/Run/派单/复核/成果物全套，在对话页里以"项目工作"模式呈现（选项目 → 与该项目的 Agent 协作，进展即任务与事件）；
   - **「对话」**：**单纯对话**——**不建任务、不进任务板、不走复核**。为此新增一条**专用聊天通道**（§4.5），
     平台存会话与轮次，内核加一个轻量轮询循环把轮次取走、跑 opencode、把回复与过程事件回传。
2. **会话上下文**：按推荐——靠 opencode 自己的 `--session`（已验证，见 §4.3）。
3. **入口**：按推荐——「AI 问答」改成「我的智能体」并提到主组；RAG 问答能力保留在 `/kb` 页。

### 4.5 专用聊天通道（「对话」类的实现；与任务体系完全分开）

为什么另开一条：用户明确"对话就是单纯对话"——若复用任务，每句话都会在任务板留痕、要判 `requires_review`、
还会产生 Run/成果物噪声。所以聊天轮次有自己的表与接口，**不碰 Task**。

| 端 | 接口 | 说明 |
| --- | --- | --- |
| 平台（人类） | `POST /api/my-agent/conversations` | 建会话（member、设备、项目、模型） |
| 平台（人类） | `POST /api/my-agent/conversations/{id}/messages` | 追加一条用户消息 → 建一条 `PENDING` 轮次 |
| 平台（人类） | `GET /api/my-agent/conversations/{id}/turns` | 轮次列表（含状态、回复、用量、过程事件） |
| 平台（人类） | `POST /api/my-agent/turns/{id}/stop` | 取消该轮（内核侧 M-4 才真中断；先如实标注） |
| 平台（Agent） | `POST /api/agents/{agent_id}/chat-turns/claim` | 执行体取走一条待办轮次（项目能力令牌 + `chat.run` 能力） |
| 平台（Agent） | `POST /api/agents/{agent_id}/chat-turns/{turn_id}/complete` | 回传：回复正文、用量、`session_key`、退出码 |
| 平台（Agent） | `POST /api/agent/turns/{turn_id}/events` | 过程事件（复用 `agent.event` 语义，但**挂在轮次上而不是 Run 上**） |

- 表：`agent_conversations`（`id/member_id/project_id/device_id/agent_id/title/model/session_key/…`）、
  `agent_turns`（`id/conversation_id/seq/kind(user|assistant)/content/status/usage/session_key/error/…`）。
- 权限：新增能力名 **`chat.run`**（与 `task.claim` 并列），授权串里带上；没这个能力的设备取不到聊天轮次。
- 事件：`AgentEventPayload` 允许 `turn_id` 替代 `run_id`（网关校验从"必须是本 Agent 的 Run"放宽为
  "必须是本 Agent 的轮次"），前端拿同一套分组渲染。
- **内核侧**：daemon 里加一个**串行**的 chat 轮询循环（与任务循环互斥，避免同时开两个 opencode），
  取到轮次 → `opencode run --format json --auto [--session <s>] [-m <model>] "<prompt>"` →
  事件实时经 Gateway 回传 → 完成后 `complete`（正文取最后一段 `text`，用量取 `step_finish` 累加，`session_key` 取事件里的 `sessionID`）。

**「项目工作」模式不新增机制**：它就是现有的项目工作区（任务板 / 派单 / 成果物 / 事件卡片）。
对话页里把它做成另一个 tab：列出我有权限的项目 → 进入即跳工作区（或内嵌最近的对话流）。