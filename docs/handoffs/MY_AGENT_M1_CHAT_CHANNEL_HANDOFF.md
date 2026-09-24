# MY-AGENT M-1 交接：对话专用通道（平台 + 内核）· 已上线

> 2026-09-23 · 上游 `docs/MY_AGENT_CONSOLE_PLAN.md`（v2：用户拍板"对话分两类"）
> 状态：**M-1 完成并线上验证**（两轮真对话答对暗号）；**M-2 页面未做**（见 §6）
> 一句话：给「我的智能体」的**单纯对话**加了一条不碰任务体系的通道，多轮上下文交给 opencode 自己的会话。

## 1 解决了什么

用户拍板：**「项目工作」跑任务、「对话」是单纯对话**——所以对话不能复用任务（那会让每句话都在任务板留痕、
要判复核、还产生 Run/成果物噪声）。本期加了一条独立通道：

| 环节 | 做法 |
| --- | --- |
| 存储 | 新表 `agent_conversations` / `agent_turns` / `agent_turn_events`（**不建 Task、不进任务板**）；迁移 `024_agent_conversations.sql` |
| 人类接口 | `/api/my-agent/agents`（可用执行体 + **真实探测到的模型列表**）、`conversations` CRUD、`/turns`、`/messages`、`/turns/{id}/events`、`/turns/{id}/stop` |
| 执行体接口 | `POST /api/agents/{id}/chat-turns/claim`（取活）、`.../{turn}/complete`、`.../{turn}/events`（逐条进度）——鉴权沿用"项目能力令牌 + **新能力 `chat.run`**" |
| 内核 | 新模块 `apps/agent/chat_loop.py`：**轮询取活 → 跑执行体 → 过程事件逐条回传 → complete 带正文/用量/会话句柄**；与任务循环**共用执行体槽位**（一台机器同时只跑一个进程） |
| 多轮上下文 | 靠 **opencode 自己的 session**：内核从事件流里取 `sessionID` 上报 → 平台存进会话 → 下一轮把 `--session <ses_…>` 交给执行体。**平台不拼历史、不存模型 key** |
| 模型切换 | 内核探测 `opencode models`（+ 读它配置里的默认模型）→ 随心跳 `resource_summary` 上报 → 平台缓存 → 每轮把 `-m provider/model` 插在提示词**前面** |
| 权限 | 默认授权能力新增 `chat.run`（后端 + 前端清单）；没有这个能力的设备取不到聊天轮次 |

## 2 落点

- 平台：`apps/api/app/agent_chat.py`（新）、`contracts.py`（5 个模型 + 默认能力加 `chat.run`）、`main.py`（9 条路由 + `ensure_schema`）、`migrations/024_agent_conversations.sql`、`apps/web/lib/api.ts`（默认能力清单）。
- 内核：`apps/agent/chat_loop.py`（新）、`agentd.py`（挂载对话循环 + 心跳带模型 + 执行体槽位 + `--chat-turns/--no-chat-turns`、`--chat-idle-seconds`）、`agent_inventory.py`（`opencode models` 探测 + 后台预热）、`opencode_executor.py`（解析 `sessionID`）。
- 测试：`apps/agent/test_agent_chat.py`（10 项）、`apps/api/test_agent_chat.py`（12 项）、`test_agent_daemon.py`（3 项模型/预热）、`test_opencode_executor.py`（会话提取）。基线：**Agent 359 项 / API 527 项**（仅 2 项既有 LaTeX 环境失败）。

## 3 验收（线上实测，2026-09-23）

```
== 可用执行体 ==  device-cloud-01 · 云端执行体 · linux · 在线 · 执行体=opencode
                 模型 10 个 · 默认 deepseek/deepseek-v4.1-flash（来自执行体真探测）
== 第 1 轮 ==  "Remember this code word: MANGO. Reply with exactly: STORED"  → DONE，回复 STORED
               用量 {"total_tokens": 61, "source": "opencode-jsonl"}  会话句柄 ses_f33681460ffe…
== 第 2 轮 ==  "What was the code word? Reply with just the word."         → DONE，回复 **MANGO**
               句柄不变（会话冒泡）· 过程事件 #1 process.started / #2 agent.message
== 结论 ==  多轮上下文 ✅ · 句柄冒泡 ✅ · 按轮用量 ✅ · 过程事件 ✅ · 任务板零留痕 ✅
```

## 4 部署期发现的 4 个真问题（都已修）

1. **授权选取取错行**：同设备同项目可能有多条历史授权（轮换过令牌），`fetchone()` 取到的是**旧授权**
   → 建会话被判 `device_grant_missing_chat_capability`。改成"**任一有效授权带 `chat.run` 即通过**"。
2. **模型列表恒空（线上抓的）**：`inventory.models()` 拿探测条目里的 `executable` 比字面量 `"opencode"`，
   而那里存的是**解析后的绝对路径**（`/home/synapforge/.opencode/bin/opencode`）→ 永远不相等。
   改成比 `adapter_id == "opencode-cli"` 或 basename。
3. **首次探测把心跳拖住**：`opencode models` 冷启动十来秒（实测被 12 秒上限掐断 → 结果缓存成空）。
   改成**启动时后台预热**（`inventory.warm()`，别让心跳等探测）+ 超时放宽到 30 秒。
4. **会话句柄没带下去**：`claim_turn` 读了会话的 `session_key` 却忘了塞进返回的轮次 → 第二轮退化成"无上下文的新会话"。
   测试直接抓到（`claim2.turn.session_key` 是 None）。

另外两条工程细节：`chat_loop.py` 必须写包内 + 顶层两段式导入（仓库两种运行方式都支持，只写相对导入会让测试全挂）；
退避等待要可被打断（否则关内核要等满一次退避，最多 30 秒）。

## 5 边界与未做

- **停止 ≠ 真中断**：`/turns/{id}/stop` 只把轮次改成 CANCELLED，执行体仍会把这一轮跑完
  （平台还没有到 Agent 的中断通道，见计划 §7）。界面上要如实标注。
- **不做逐字流式**：`--format json` 是事件级（一段 message、一个工具卡片），不是逐字 token。
- **cache 不计入用量**：opencode 的 `tokens.total` 含 cache 读，平台按 `input+output` 记（与 codex 同口径），
  所以多轮对话里第二轮的 token 数看起来很小——这是口径，不是丢数（真实样本：input 58 + cache read 7168）。
- **同一会话一次只跑一轮**（`conversation_busy`）：内核是串行的，排队反而看不清。
- **租约**：轮次领取带 1800 秒租约，过期可被重新领取（执行体崩了不会卡死会话）。

## 6 M-2 页面（同日完成）

`/my-agent` 上线并实机验收：

- **两模式 tab**：「对话」（本页，单纯对话）与「项目工作」（列项目 → 进工作区，复用任务体系，不新增机制）；
- **三栏**：会话列表（按日期分组、可删）/ 对话流 + composer / **本轮执行细节**（状态、模型、会话句柄、用量、过程事件）；
- **设备与模型下拉来自执行体真探测**（心跳上报的 `models`/`default_model`），不是写死的；
- 导航：桌面侧栏第一组第一项「我的智能体」（`/ask` 从导航移除，页面保留但不再是入口），
  **手机底栏**由「问答」改为「智能体」；
- 刷新后自动选中最近一条会话（否则中栏空着像「对话丢了」）。

**实机验收（桌面 1440 + 手机 390）**：页面发一条消息 → 执行体回复出现在流里、用量 `7376 tokens · opencode-jsonl`
显示在气泡下、右栏出现 `#1 开始执行 / #2 执行体输出`；`scrollWidth == viewport`（无横向溢出）；验证用的会话已删除。
前端 **26 页静态**（新增一页）。

**部署坑（重要）**：生产构建必须 `export NEXT_PUBLIC_API_URL=https://synapforge.top` 再 `npm run build`，
否则包内联 `http://127.0.0.1:8000`、线上所有接口 Failed to fetch（首次重建漏了，靠浏览器实机才发现）。

## 7 下一步：M-3 / M-4

M-1 只有 API，没有页面。M-2 要做：
1. `apps/web/app/my-agent/page.tsx`：三栏（会话列表 / 对话流 / 本轮执行细节）+ composer（附件、模型下拉、设备选择）；
2. 导航：`lib/nav.ts` 把「AI 问答」换成「我的智能体」并提到主组（`/ask` 页保留或重定向到 `/kb`）；
3. 渲染：轮询 `/turns`（或 WS 增量）+ 过程事件卡片（backend 已就绪）；
4. 「项目工作」模式：列出我有权限的项目 → 进工作区（不新增机制）；
5. 前端构建（`next build`，当前 25 页）+ 线上重建 + 390px 手机视口检查。