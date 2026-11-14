# synapforge studio · 本地 Agent 集群工作台

不依赖服务器、没有登录、没有 Next.js。`synapforge studio` 在本机 `127.0.0.1` 起一个控制台，
把已安装的 CLI Agent（Claude Code / 自定义命令）组成 **lead + worker** 集群，用共享任务板、
`ask/answer`、成果物回执跑跨小时/跨天的任务。

## 快速开始

```bash
python scripts/synapforge_studio.py
# 或
python -m synapforge studio --port 0 --no-browser
```

终端会打印带 token 的地址，例如：

```
http://127.0.0.1:8765/?token=...
```

用浏览器打开这个地址（token 只用于本机控制面；重启会复用同一状态目录下的 token）。
状态库默认 `~/.synapforge/state.db`，工作区默认 `~/.synapforge/workspace/`。

第一次使用：打开侧栏「模型与智能体」→「导入 Claude 设置」，或到「本机路由」点「重新扫描」
把 magpie / Ollama / LM Studio 加为渠道；然后给 Agent 选渠道和模型。

## 先决条件

- Python ≥ 3.11（仓库现有依赖即可，不需要 PostgreSQL / MinIO / Node）。
- 至少一个可用的 CLI Agent：
  - **Claude Code**：`claude` 在 PATH，或装在 VS Code 扩展里。控制台会显示探测到的路径/版本。
  - **自定义命令模板**：任何能无头执行、并把结果写进工作区的 CLI。ZCode / WorkBuddy / Kimi
    等都可以用模板接入（见下）。
- Claude Code 的 `ANTHROPIC_BASE_URL` 不要带 `/v1` 后缀：Claude Code 自己会追加
  `/v1/messages`，写成 `https://host/v1` 会变成 `/v1/v1/messages`（404）。

## 概念

| 对象 | 含义 |
|---|---|
| **集群（Cluster）** | 一个目标 + 一个 lead + N 个 worker；有独立的项目、工作区和运行记录 |
| **角色（Role）** | 名字、执行体、模型、system prompt、工作区、预算；每个角色对应一个 Agent id |
| **运行（Run）** | 一次长周期执行；有轮次、预算、检查点，可暂停/恢复/停止 |
| **任务（Task）** | 共享任务板上的工作单元；lead 建任务并指派给 worker，worker 用 claim/start/finish |
| **成果物（Artifact）** | worker 写进工作区的文件，登记后带 hash 回执；可在 UI 人工批准/退回 |

调度器**不常驻 Agent**：每个角色是一次性 CLI 会话（先读 `inbox`，处理一项，退出）。
状态全在 SQLite，Agent 进程退出/崩溃不会丢协作事实。

## 两种模式

- **Agent 工作台（默认 `/`）**：新建 Agent → 选模型 / 角色卡 → 安排一个任务 →
  实时看步骤 → 拿到交付文件。适合“部署一个任务，跑完给交付”。
- **集群工作（侧栏「工作」组）**：lead + worker 多角色、共享任务板、长周期协作，
  角色直接复用已有 Agent，worker 可并发。适合“多个 Agent 分工完成一个大目标”。
  （`/clusters` 独立页面仍然保留，功能等价。）

## Agent 工作台（默认首页）

1. 左上角「＋ 新建 Agent」：填名字、执行体、渠道、模型、角色卡/预设提示词、工作区；
   可勾选「允许联网搜索（WebSearch / WebFetch）」，默认开启。
2. 在左栏选中 Agent，底部输入任务，Enter 发送。
3. 中间按主流 Agent 的对话样式实时出现：你的任务是一个气泡，Agent 的正文直接排版，
   `Thinking` / 工具调用 / 工具结果 / 系统步骤 / 交付登记收成可折叠的一行（点开看全文）。
4. 顶部显示状态、渠道 · 模型、上下文用量（`已用 / 上下文窗口`）。
5. 右侧 `Model` 可以直接切渠道、改模型；`Role card` 可以改角色卡。
6. 右侧 `Deliverables` 列出工作区文件：同名文件按版本分组（`v4 · 294 B`），
   点开预览可切换 v1–v4，`批准交付` 后状态变 `APPROVED`。
7. 保存后下一次任务生效；正在跑的任务使用下发时的渠道/模型快照。

**侧栏与视图（对齐 magpie）**

- **工作**：`Agent 工作台`（默认）、`运行记录`（所有任务，点进看完整步骤）、`会话`（同一 Claude session 的多次运行聚合）、`交付`（所有成果物，按 Agent + 文件名聚合版本）、**`集群工作`**（lead + worker 长周期任务，原生视图，不再跳转）。
- **Agents**：Agent 列表；**点整张卡片**就展开它的会话下拉（再点收起），不用找小箭头。
- **模型**：`模型与智能体`（智能体 / 渠道 / 执行体 / 能力 / 方案）、`路由`（本机网关 + key 池）、`扩展库`（跨 Agent 集中管理技能/MCP/插件）。
- **观测**：`用量`（花费、tokens 构成、缓存命中、按 Agent/模型/渠道/天）。
- **系统**：`设置`。
- 右上角 `◐` 切换明暗主题，选择记在 localStorage。

## 模型与智能体（`模型与智能体` 页）

五个 Tab：**智能体 / 渠道 / 本机路由 / 执行体 / 能力**。这一页是 Agent 的配置中心，
工作台里的改动和这里共用同一份 SQLite。

### 智能体

每个 Agent 一张卡片，可直接改：

- **执行体**：`Claude Code`（`stream-json` 逐步显示，支持 `--resume`）或 `自定义命令模板`。
- **渠道**：本机默认（沿用 `~/.claude/settings.json`）或任意已保存渠道。
- **模型**：输入或从该渠道的模型列表里选；渠道切换后 datalist 会跟着换。
- **角色卡 / 提示词**：这张卡就是工作台里的 `Role card`，两处保存等价。
- **允许 Bash / 允许联网**、**工作区**、**保存配置**、**检测**（用该 Agent 的渠道做连接检测）、
  **删除**（删除运行记录，保留工作区文件）。

Claude Code 只能配 Anthropic 协议渠道；OpenAI 兼容渠道会提示换成自定义命令执行体。
自定义命令模板占位符：`{prompt}` `{workspace}` `{model}` `{agent_id}` `{state}`；
渠道的 base URL/key 通过环境变量注入，不出现在命令行里。

### 渠道

一个渠道 = 协议 + base URL + key + 模型列表。支持新建、编辑、删除、启用/停用、设为默认。

- **手工新建**：填名字、协议、base URL、key、模型（每行一个，可留空靠检测自动填）。
  本地网关（magpie / Ollama / LM Studio）不需要 key 也能建。
- **协议**：`Anthropic`（Claude Code 可用）或 `OpenAI 兼容`（命令执行体）。
- **检测**：拉 `/v1/models`，显示 HTTP 状态、延迟、模型数量和模型列表；401/403 会明确标成认证失败。
- **深度检测**：发一条 `max_tokens=1` 的真实消息（会花极少 token），用于确认模型真的能回。
- **模型列表**：检测成功后会把新发现的模型合并进渠道；也可以手工每行一个。
- **密钥**：只存本机 SQLite；接口只返回 `sk-…abcd` 掩码和 `has_key`，编辑时留空表示不修改。

### 渠道路由（多 key + 失败切换）

一个渠道可以挂多个 key，运行按顺序挑可用的；失败自动换下一个：

- 渠道卡片上的 `多 key N` 打开 key 池：主 key + 追加 key（标签、启用/停用、单独检测、恢复、删除）。
- 每次任务开始会在时间线写一条 `选择 key`，列出可用 key 和本次使用的那一个。
- 失败分类：
  - `401 / 403 / invalid token / unauthorized` → 该 key 标 `failed`，换下一个；
  - `429 / rate limit / quota / 额度 / 限流` → 标 `limited`，冷却 10 分钟后自动恢复；
  - 其它错误（模型不存在、网络断了）→ 不换 key，直接失败，避免无意义重试。
- 全部 key 都失败时如实报错（`所有 key 都失败`），不伪造成功。
- `恢复` 会清掉 key 的状态；检测通过也会自动恢复。
- key 池里主 key 不能删（去「编辑渠道」里清空），追加 key 可以停用或删除。

### 本机路由

点「重新扫描」会检查：

- `~/.claude/settings.json` 的 `ANTHROPIC_BASE_URL`（标为“当前 Claude 路由”）；
- `~/.codex/config.toml`、`~/.config/opencode/opencode.json`、环境变量；
- 本机常见端口：`3425` magpie、`11434` Ollama、`1234` LM Studio、`4000` LiteLLM、
  `3000` one-api/New API、`8000/8080/8081/5000/8888/9000` 通用 OpenAI 网关。

每条路由显示可达性、延迟、模型列表和来源；点「添加为渠道」会由服务端重新读取本机配置里的 key
（前端不传 key），然后就能在 Agent 卡片里选它的模型。例如把 magpie 的
`auto/glm-5.3-flash` 直接作为 Claude Code 的模型跑通。

### 扩展（记忆 / 技能 / MCP）

Agent 卡上的「扩展」按钮打开三个 Tab；都按单个 Agent 生效，下一个任务开始时应用。

- **记忆**：编辑工作区里的 `CLAUDE.md` / `AGENTS.md` / `MEMORY.md`。
  Claude Code 会自动读取这些文件；自定义命令执行体会把内容拼进 prompt。
  保存后下一个任务生效，文件超过 512 KB 会被拒绝。
- **技能**：从 `~/.claude/skills`、`~/.agents/skills`、Claude 插件 skills 里勾选，
  也可以填一个本地目录「导入技能」（复制到 `~/.agents/skills/<name>`，不联网）。
  勾选的技能会复制到工作区 `.claude/skills/<name>`，只对这个 Agent 生效；
  全局 `~/.claude/skills` 仍然可用。清空勾选会删掉本工具挂载的副本
  （由 `.claude/skills/.synapforge-managed.json` 记录，不会动用户自己放进去的目录）。
- **MCP**：列出 `~/.claude.json`（全局/项目）和工作区 `.mcp.json` 里的服务器。
  勾选「只启用选中的 MCP」后，运行器会生成一份 0600 的 `mcp-config.json`，
  用 `--mcp-config … --strict-mcp-config` 启动 Claude Code；不勾选则沿用本机全部 MCP。
  配置里的 env/headers 只在服务端生成，前端只看到脱敏后的命令/地址。
- **插件**：填一个本地 Claude Code 插件目录（含 `.claude-plugin/plugin.json`、
  `skills/`、`.mcp.json`、`agents/`）安装。插件会复制到
  `<state>/studio/plugins/<name>`，启用后把它的 skills / MCP / 子代理按 Agent 挂载；
  子代理写到工作区 `.claude/agents/<plugin>--<name>.md`。
  支持更新（重新从源目录复制）和卸载（删除副本，并从所有 Agent 的启用列表移除）。
  `hooks` / slash commands / LSP 暂不携带，会在插件卡片上明确列出。

> 插件安装的是目录，不是 npm/marketplace 源；插件库和 Agent 工作区都在本机，
> 卸载只删本工具复制的那份，不碰用户自己的源目录。

> 技能是“复制”而不是软链：Windows 上没有管理员权限也能用，代价是每个 Agent 一份副本。
> 改完技能/记忆后如果当前会话正在跑，改动从下一个任务开始生效。

### 方案（Profile）

把当前**所有 Agent** 的执行体、渠道、模型、角色卡、Bash/联网开关、扩展选择
（技能 / MCP / 插件）存成一个名字，之后一键切回。

- 保存：`方案` Tab → `＋ 保存当前方案`，起名（例如「省钱」「高质量」）。
- 应用：`应用` 会按 Agent **名字**匹配；方案里有但当前不存在的 Agent 跳过，
  当前有但方案里没有的 Agent 保持原样——不会因为切方案删掉别人。
- `覆盖保存` 用当前配置刷新这个方案；`改名`、`删除` 只动方案本身，不影响 Agent。
- 渠道按名字解析回 id；渠道被删过的会退回「本机默认」。

### 路由（侧栏「路由」）

独立页面，不再塞在「模型与智能体」里：

- 顶部四个指标：已配置渠道、可用 / 全部 key、可达本机路由、扫描结果数。
- **渠道与 key 池**：每个渠道一张卡，含检测、多 key、编辑、删除；模型列表来自「手工保存 + 上次检测发现」的并集。
- **本机路由**：扫描 `~/.claude/settings.json`、Codex/opencode 配置、环境变量，以及本机
  `3425` magpie / `11434` Ollama / `1234` LM Studio / `4000` / `3000` / `8000` / `8080` / `8081` /
  `5000` / `8888` / `9000` 等端口；显示可达性、延迟、模型数，可一键「添加为渠道」。
- 「＋ 手动添加渠道」直接填名字、协议、base URL、key、模型（每行一个）。
- 进入页面会自动扫描一次；也可以手动「扫描本机路由」。

### 扩展库（侧栏「扩展库」）

所有 Agent 的技能 / MCP / 插件集中在一个页面：

- 每行一个扩展，右侧是该扩展在**每个 Agent** 上的开关 chip（点亮=已启用）。
- 点 chip 立刻写入该 Agent 配置；同一个技能可以同时给多个 Agent 用（例如把 `image-gen` 同时给 Writer 和 QueueTest）。
- 顶部计数 + 搜索（按名称 / 描述 / 路径过滤）。
- 技能支持「导入技能」：填本地目录复制到 `~/.agents/skills/<name>`，不联网。

### 集群工作（侧栏「工作」组）

**角色直接复用已有 Agent**：新建集群时从 Agent 列表挑 lead 和 worker，继承它的渠道、
模型、角色卡、Bash/联网开关、技能、MCP、插件；每个角色只需再填一句「本轮职责」。
角色卡和职责会拼成最终的 system prompt：

```
你是技术写手。写清晰的 Markdown，先给结论再给步骤。（Agent 角色卡）

本轮职责：负责拆解与终审（这一轮的额外要求）
```

角色工作区仍落在集群根目录下（`<root>/00-writer`、`<root>/01-worker`），
不共用 Agent 自己的工作区，保证产出可归属。

**并发执行**：`policy.max_parallel_workers`（默认 3，1 = 退回串行）。
lead 有活时只派 lead（避免和 worker 抢同一批任务）；lead 空闲时把**所有有活的 worker
同时派出去**，上限就是并发数。每个角色一个线程、自己一个 SQLite 连接、自己一个日志文件。

**失败隔离**：单个角色连续失败 2 次只把它标成 `blocked`（角色卡上显示「已阻塞」），
不暂停整个集群；其它角色继续跑。

**六个 Tab**：

| Tab | 内容 |
|---|---|
| 角色 | 角色卡网格：lead/worker 标记、渠道·模型、调用次数 3/30、待领/在跑/待答、当前任务。**点整卡展开**看工作区、协议身份、继承扩展数、本轮职责、上次退出码、日志尾部；可唤醒/停止进程/移除 |
| 任务 | 任务板，`WAITING_REVIEW` 时批准/退回 |
| 事件 | 集群事件时间线 |
| 成果物 | 所有产出，点「预览」看内容 |
| 运行 | 历次运行：轮次、开始/结束、**停止原因**（达到轮次上限 / 有任务被阻塞 / 终审提出异议 等） |
| 预算 | 全部 policy 值 + 每角色调用次数 vs 上限（带进度条） |

顶部 6 个指标：已运行、轮次、在跑/角色数、任务数、已批准、成果物。
集群多于一个时上方出现切换 chips。

**新建集群（两步）**：

1. 基本信息：名字、目标、工作区、最长运行、最多轮次、并发数、单次超时、复核策略。
2. 选角色：Lead 单选、Workers 多选（卡片显示渠道·模型·扩展数），每个选中项一个「本轮职责」输入框。

创建后自动启动。集群运行中不能加/删角色（先暂停或停止）。

### 用量（侧栏「用量」）

- 8 个指标：累计花费、tokens、运行次数、缓存命中率、每次运行均花费、每次运行均 tokens、成功/失败、累计模型耗时。
- tokens 构成（输入 / 输出 / 缓存读 / 缓存写）带比例条。
- 按天柱状图（悬停显示当天 tokens 与花费）。
- 三张明细表：按 Agent / 按模型 / 按渠道，每行含运行数、tokens、缓存命中率、花费与横向对比条。

### 执行体 / 能力

- **执行体**：显示本机探测到的 Claude Code / Codex / opencode / ZCode / Kimi / WorkBuddy 路径和版本。
- **能力**：只读列出 Skills（`~/.claude/skills`、`~/.agents/skills`、插件 skills）、
  MCP 服务器（`~/.claude.json`、工作区 `.mcp.json`）、长期记忆（`CLAUDE.md` / `AGENTS.md` / `MEMORY.md`）
  和插件。命令和 URL 会脱敏，不会显示 key。

**多任务与会话**

- 同一个 Agent 一次只跑一个任务；任务会排队，顶部显示 `排队 N`。
- 同一个 Agent 默认**续接上一个 Claude 会话**（命令带 `--resume <session_id>`），
  所以“刚才那件事”它记得。点 `Model` 右侧的「新会话」会清空 session，下一个任务从零开始。
- 任务失败后顶部出现「重试」，会复制原任务重新排队。
- 180 秒没有新输出会终止进程并标记失败（避免 API 卡住让 UI 永远 RUNNING）；
  已写出的文件仍会登记为成果物。

**侧栏会话下拉**

- 点整张 Agent 卡：选中它 + 展开它的会话列表；再点一次收起（同一时间只展开一个）。
- 每条会话显示标题、运行次数、tokens，当前会话标「当前」。
- 点会话会真正载入那段对话：主区显示该 session 的全部 run 与步骤，同时把 Agent 的当前
  session 指过去（下个任务续接它）。
- 底部「＋ 新会话」清空 session，从零开始。

**会话视图（侧栏「会话」）**

- 同一个 Claude `session_id` 的多次 run 聚合成一段会话；显示 Agent、渠道/模型、运行次数、tokens、花费、最近时间。
- 支持搜索（标题 / 任务 / Agent）、按 Agent 筛选、显示已归档。
- 点进会话：运行卡片 + 完整对话步骤；可以 `重命名`、`继续这个会话`、`导出 Markdown`、`归档`。
- **继续**会把该 Agent 的当前 session 指向选中会话（`local_agents.session_id`），下一个任务用 `--resume` 续接。
- 导出是 Markdown：包含会话元信息、每次任务、每一步的工具/思考/结果，适合归档或交给别人。
- 命令执行体没有 Claude session，不进入会话列表；它的运行仍在「运行记录」里。

实现方式：Claude Code 以 `--output-format stream-json --include-partial-messages` 运行。
`stream_event` 里的 `text_delta` / `thinking_delta` 按块聚合成同一条 step，
通过 SSE 的 `step_update` 事件推给页面（约 150ms 一次），正文边收边长；
块结束时写一次库，刷新页面仍能完整回放。工具调用/结果/系统步骤仍是 `step` 事件。
任务结束后扫工作区差分，把变更文件登记为成果物。

**渠道隔离**：Claude Code 会把 `~/.claude/settings.json` 里的 `env` 合并进运行环境，
优先级高于进程环境变量。所以选了渠道时，运行器会在
`<state>/studio/agent-runs/<run>/claude-settings.json` 写一份 0600 的临时 settings，
用 `--settings` 显式声明 `ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / ANTHROPIC_MODEL`，
运行结束立即删除。这样同一个 Claude Code 可以在本机默认、树 API、magpie 之间切换。
不选渠道时不写 settings，继续沿用本机 `~/.claude/settings.json`。

数据：`local_agents`（含 `provider_id` / `command` / `skills_json` / `mcp_json` /
`mcp_managed` / `plugins_json` / `session_id`）/ `local_agent_runs`（含 `QUEUED`、渠道/执行体快照）/
`local_agent_steps` / `local_agent_sessions`（会话标题/归档）/
`local_plugins`（插件库）/ `local_profiles`（方案快照）/
`local_provider_keys`（渠道 key 池）/
`local_providers`（渠道，key 只在本机），仍在同一个 `~/.synapforge/state.db`。

## 独立集群页（`/clusters`，可选）

主界面侧栏「工作 → 集群工作」已经是原生视图，日常用那个就够。
`/clusters` 保留为独立整页（左栏集群列表、右栏角色检查器、底部群聊输入框），
适合想同时看多个集群或全屏看日志时使用。两者读写同一份 SQLite。

## 执行体

### Claude Code

角色 `backend=claude`。默认命令：

```
claude -p <prompt> --settings <临时渠道 settings> --mcp-config <mcp.json> \
  --strict-mcp-config --permission-mode acceptEdits \
  --allowedTools "mcp__synapforge,Write,Read,Edit"
```

- 复用 Agent 的角色会带上 `--settings`（渠道 env）和合并后的 MCP（synapforge + Agent 自己的 + 插件的）。
- 角色选了渠道时，`--settings` 是必须的：Claude Code 会合并 `~/.claude/settings.json` 的 env，
  不显式覆盖就会继续打到本机默认地址。

- 勾选“允许 Claude 使用 Bash”会追加 `Bash`。
- 模型可留空（用 Claude Code 当前配置），也可在角色里填 `--model` 的值。
- 每次调用都会带上一个只属于该角色的 MCP 配置（`scripts/synapforge_mcp.py`），
  所以 Agent 只能看到自己的收件箱/任务，不会串号。

### 自定义命令模板

角色 `backend=command`，模板里可用占位符：

```
{prompt} {mcp_config} {workspace} {agent_id} {project_id} {state}
```

例：

```
my-agent --prompt {prompt} --mcp-config {mcp_config} --cwd {workspace}
```

命令用 `shlex` 拆参数、**不启用 shell**；如果模板里没有 `{prompt}`，会把 prompt 追加到最后。
CLI 需要自己支持 MCP（或用环境变量 `SYNAPFORGE_MCP_CONFIG` 自己读）才能参与协作。

## 长周期与恢复

- 每个角色同时最多一个进程；lead 优先，worker 只在 `inbox` 有活时被拉起。
- `暂停`：不再派新活；当前进程可以继续跑完，也可以在右侧“停止当前进程”。
- `停止`：取消当前进程，run → `STOPPED`。
- `恢复`：从任务/租约状态继续；进程是新的会话，记忆靠任务板和事件。
- 预算：`max_wall_minutes`、`max_rounds`、每角色 `max_invocations`、单次 `role_timeout_seconds`；
  超限自动暂停并在检查点写明原因。
- 收尾：worker 任务全部结束且有成果物 → 自动建“集群终审”任务给 lead；lead 完成后 run → `DONE`。
- 停止/崩溃后重启 studio：RUNNING 的 run 会被标成 `PAUSED`，在 UI 点恢复即可续跑。

## 人工复核

- 任务 `WAITING_REVIEW`：UI 任务表里“批准 / 退回”。
- 成果物 `PENDING_REVIEW`：成果物 Tab 里“批准 / 退回”。
- 集群策略：
  - `auto_approve=true`：任务复核自动通过（默认），成果物仍留 `PENDING_REVIEW` 给人看。
  - `human_review=true`：出现 `WAITING_REVIEW` 就暂停，等人处理。

## 数据位置

```
~/.synapforge/state.db                 # 项目/任务/租约/事件/成果物 + studio 4 张表
~/.synapforge/workspace/<cluster>/     # 每个角色的工作区
~/.synapforge/studio/<cluster>/<run>/  # MCP 配置与每次调用的日志
~/.synapforge/objects/                 # 成果物内容（对象存储）
```

## HTTP 控制面

所有接口只绑 `127.0.0.1`，写接口要求 `X-Studio-Token` + 同源 `Origin`：

```
GET  /api/studio/state[?cluster_id=]
GET  /api/studio/clusters[/{id}]
POST /api/studio/clusters
POST /api/studio/clusters/{id}/roles      # {agent_id, role_prompt} 从 Agent 加角色
PATCH /api/studio/roles/{id}
DELETE /api/studio/roles/{id}             # lead 不能删；集群运行时不能删
POST /api/studio/clusters/{id}/start|pause|resume|stop
POST /api/studio/roles/{id}/start|stop
POST /api/studio/instructions
POST /api/studio/tasks/{id}/review
POST /api/studio/artifacts/{id}/review
GET  /api/studio/roles/{id}/logs?lines=200

GET  /api/studio/agents
POST /api/studio/agents
GET  /api/studio/agents/{id}[?after_seq=]
PATCH|DELETE /api/studio/agents/{id}
POST /api/studio/agents/{id}/tasks
POST /api/studio/agents/{id}/new-session
POST /api/studio/agents/{id}/test          # 检测该 Agent 的渠道（deep=true 发最小消息）
GET  /api/studio/agents/{id}/extensions    # 技能/MCP/记忆清单 + 当前选择
PUT  /api/studio/agents/{id}/extensions    # {skills,mcp,mcp_managed}
GET  /api/studio/agents/{id}/memory
PUT  /api/studio/agents/{id}/memory        # {name,content}
GET  /api/studio/sessions[?agent_id=&q=&archived=1]
GET  /api/studio/sessions/{id}             # 会话 + 每次 run + steps
GET  /api/studio/sessions/{id}/export      # {filename, markdown}
PATCH /api/studio/sessions/{id}            # {title}
POST /api/studio/sessions/{id}/activate    # 下个任务续接这个会话
POST /api/studio/sessions/{id}/archive     # {archived}
GET  /api/studio/plugins                   # 插件库
POST /api/studio/plugins                   # {path} 安装本地插件目录
POST /api/studio/plugins/{id}/update       # 从源目录重新复制
DELETE /api/studio/plugins/{id}            # 删除副本 + 从 Agent 移除
GET  /api/studio/library                   # 扩展库：技能/MCP/插件 + 每个 Agent 的使用情况
GET  /api/studio/usage                     # 总览 + by_agent/by_model/by_channel/by_day
GET  /api/studio/profiles                  # 方案列表
POST /api/studio/profiles                  # {name,note,id?} 保存/覆盖
PATCH /api/studio/profiles/{id}            # {name,note}
POST /api/studio/profiles/{id}/apply       # 应用到所有同名 Agent
DELETE /api/studio/profiles/{id}
GET  /api/studio/providers
POST /api/studio/providers
PATCH|DELETE /api/studio/providers/{id}
POST /api/studio/providers/{id}/test       # 连接检测
GET  /api/studio/providers/{id}/keys       # key 池 + 可用数
POST /api/studio/providers/{id}/keys       # {api_key,label} 追加 key
PATCH|DELETE /api/studio/providers/{id}/keys/{key_id}
POST /api/studio/providers/{id}/keys/test  # {key_id} 单 key 检测
POST /api/studio/providers/{id}/keys/restore  # {key_id} 清状态
POST /api/studio/providers/import          # 导入 ~/.claude/settings.json
POST /api/studio/providers/import-route    # {url, kind} 把扫描结果变成渠道
POST /api/studio/skills/import             # {path} 本地技能目录导入
GET  /api/studio/routes[?remote=0]         # 扫描本机路由
GET  /api/studio/capabilities              # Skills / MCP / 记忆 / 插件
GET  /api/studio/overview
GET  /api/studio/runs[/{id}]
GET  /api/studio/artifacts
GET  /api/studio/usage
GET  /api/studio/settings
POST /api/studio/runs/{id}/retry
POST /api/studio/runs/{id}/stop
GET  /api/studio/runs/{id}/stream?token=…     # SSE：step / run / done
GET  /api/studio/artifacts/{id}               # 文本预览
GET  /api/studio/models                       # 模型目录 + 渠道
GET  /api/studio/executors
```

## 边界（M2.5）

- 每个角色/Agent 独立工作区；**还不是共享代码库/自动合并**。集群角色复用 Agent 的配置，
  但工作区是集群自己的（`<root>/00-writer`），不共用 Agent 自己的工作区。
- 集群角色在**创建时快照** Agent 配置；之后改 Agent 不会影响已建好的集群角色。
- 集群并发是"每个角色一个线程"，同一角色不会并发两次；lead 有活时只派 lead。
- 单角色失败 2 次标 `blocked` 并继续跑其它角色；**不会自动恢复**，需要人工处理后重试。
- 集群运行中不能加/删角色（先暂停或停止）。
- 工作台支持 `claude` 与 `command` 两种执行体；Codex/opencode 原生事件适配未做。
- 渠道不做跨渠道自动切换（一个 Agent 只用一个渠道）；同一渠道内会按 key 池切换。
  额度余额、路由组（按内容选模型）未做。
- 本机路由扫描只连 `127.0.0.1` 的常见端口和本机已有配置；不扫局域网、不扫公网网段。
- 检测默认只拉 `/v1/models`（免费）；深度检测会发一条 `max_tokens=1` 的消息，需手动触发。
- 能力页的 Skills / MCP / 插件可以按 Agent 选择；插件只携带 skills / MCP / 子代理，
  `hooks` / slash commands / LSP 未携带。
- 技能是复制到工作区 `.claude/skills`，不是共享安装；同一技能给 N 个 Agent 会有 N 份副本。
- 插件从本地目录安装，不是 npm/marketplace 在线源；更新需要源目录仍在。
- 会话只聚合有 Claude `session_id` 的运行；自定义命令执行体不进会话列表，也没有续接。
- MCP strict 模式只对 Claude Code 生效；自定义命令执行体只拿到 `SYNAPFORGE_*` 环境变量。
- 记忆只能编辑工作区里的三个约定文件，不做自动摘要/长期记忆蒸馏。
- 联网搜索（WebSearch/WebFetch）已接进 `allow_web`；但本机实测外部搜索返回空、WebFetch 被网络策略阻断，
  这是网络环境问题，不是权限问题——Agent 会如实写进交付，不会伪造来源。
- 步骤是 `stream-json` 事件级 + `--include-partial-messages` 的文本增量：
  assistant / thinking 是逐块生长的，工具调用与系统步骤仍是一次性出现。
- 会话续接依赖 Claude 的 session；session 丢失时任务会失败，点「新会话」重来。
  换渠道不会自动新建会话，必要时手动点「新会话」。
- 同名交付在 UI 里按版本分组，但底层每条仍是独立 artifact（version 字段都为 1）。
- 只有手动「重试」，没有定时/自动重试。
- 没有打包成单 exe/托盘（M3）；当前从源码启动。
- 不处理多机、账号、权限；这是单机单用户的工具。

## 测试

```bash
cd apps/local
python -X utf8 -m unittest discover -s tests -p "test_*.py"
```

覆盖：集群/角色/运行持久化、建集群/启停/指令/复核、假后端双 Agent 全流程、
HTTP token/Origin、渠道 CRUD/连接检测/本机路由/多 key 池与失败分类、会话聚合/重命名/导出、
技能挂载/MCP 配置/记忆读写、插件安装/解析/挂载/卸载、方案保存/应用、
**集群角色复用 Agent（继承配置/本轮职责/同 Agent 进多集群/加删角色）**、
**集群并发（同时跑/并发上限/失败隔离）**、UI JS 语法。
