# MY-AGENT M-5c 执行计划：真流式（换通道：opencode serve + SSE）

> 2026-09-23 起草 · **S-0 ～ S-3 已于 2026-09-24 完成交付，仅剩 S-4**（逐阶段证据见 `docs/handoffs/MY_AGENT_M5C_S0_SSE_SAMPLES_HANDOFF.md`、`MY_AGENT_M5C_S1_SERVE_CHANNEL_HANDOFF.md`、`MY_AGENT_M5C_S2_DELTA_STREAMING_HANDOFF.md`、`MY_AGENT_M5C_S3_APPROVAL_CARDS_HANDOFF.md`）
> 用户已拍板「一起做，这是体验里很重要的部分」 · 上游 `docs/MY_AGENT_M5_STREAMING_AND_MODES_PLAN.md`

> **S-0 的三条硬结论（照这个改 S-1，别再自己发现一遍）**：
> ① **用 v1 通道**（`POST /session` + `POST /session/{id}/message` + `GET /event`）——v2 的 `/api/*` 会话运行器
> **认不出配置里的自定义 provider**（`ModelUnavailableError: Model unavailable: deepseek/deepseek-v4.1-flash`）；
> ② 逐字增量是 `message.part.delta`（`properties.field` + `properties.delta`），轮次结束看 `session.idle`，
> 用量看 `step-finish.tokens`；③ **权限请求会真拦路**（`permission.asked`，不回就不继续）→ S-3 必须定"没人看着时怎么办"。　
> 另：serve 要设 `OPENCODE_SERVER_PASSWORD`（实测警告 `server is unsecured`）；`POST …/message` 是**阻塞返回**（或用 `prompt_async`）。

---

## 0 为什么必须换通道（实测证据）

今天这一轮我做了对照：一条**纯问答**在 `--format json` 下跑了 45 秒，平台上收到的轮次事件**只有 `process.started`**，
正文直到最后才一次性到达。原因：**该格式是"一步一条事件"**——opencode 在一轮收尾时才把整段 text 作为一条事件发出。

所以：

- **分段显示（已上线）**只对"用工具的多步任务"有效（每步之间会有中间段落）；
- **纯问答永远是非流式**——这不是页面问题，是通道粒度问题；
- 要让文字**边生成边出现**，必须用 opencode 的**服务端接口**（它有真正的增量事件）。

---

## 1 目标形态

```
内核（执行体那台）                                 平台                              浏览器
┌──────────────────────────────┐        ┌────────────────────────┐      ┌─────────────────┐
│ chat_loop                     │        │ 轮次事件 / delta 帧      │      │ 气泡内逐段追加   │
│  ├ opencode serve (常驻)      │  HTTP  │ agent_turn_events      │  WS  │ （不是等 complete）│
│  │   ├ POST /session/…/message│ ─────► │   event_type=delta     │ ───► │                 │
│  │   └ GET /event  (SSE)      │        │   event_type=thinking  │      │ 思考块可折叠     │
│  └ 仍保留 --format json 回退   │        │   event_type=tool.*    │      │ 工具/权限卡片    │
└──────────────────────────────┘        └────────────────────────┘      └─────────────────┘
```

要点：

- **一个工作目录一个常驻 server**（不是每轮起一个）：`opencode serve --port <随机>`，内核负责启动、就绪探测、崩溃重启、退出清理；
- 轮次执行改为：**在 server 上建/续会话 → 发消息 → 订阅 SSE**，把增量原样转成平台的 `delta` 事件；
- **保留 `--format json` 通道作为回退**：SSE 起不来（或版本不支持）就自动降级，页面如实标注「已降级为分段显示」——
  不假装是流式。

---

## 2 分期（每期独立可验）

| 期 | 内容 | 验收（实测） |
| --- | --- | --- |
| **S-0 采样本**（半天）✅ **已交付** | 在云端执行体上起 `opencode serve` + 打 `/event`，记录**真实** SSE 事件形状：增量文字落在哪个字段、思考（reasoning）怎么发、工具调用与**权限请求**（approval）有没有事件 | 已完成：v1 通道一轮（ls→写文件→表格）采到 16 类事件；`message.part.delta`=51 条逐字增量（`field/delta`）、`tool` part 状态机（pending→running→completed）、`permission.asked`/`replied`、`step-finish.tokens`、`session.idle`；**v2 通道不可用**（自定义 provider 解析失败）。样本原文与对照表见交接文档 |
| **S-1 内核：常驻 server**（1–2 天）✅ **已交付** | `ChatServerManager`（启动/就绪/健康/重启/退出）+ 轮次走 server API；`--format json` 作为回退 | 已交付（交接 `docs/handoffs/MY_AGENT_M5C_S1_SERVE_CHANNEL_HANDOFF.md`）：`opencode_server.py` + 循环通道切换 + 单元 `--chat-server-port 4199`；线上真跑"完成（66 字，serve 通道）"、用量 `source=opencode-serve`；**kill 掉 serve 后自动重启并续上上下文**；平台重启不留孤儿；不可用自动降级到 CLI 通道。**顺带测到**：v1 `POST /session` 不接受 `model`（带上就 400），模型只在发消息时带 |
| **S-2 平台 + 页面：逐字**（1 天）✅ **已交付** | 新事件类型 `delta`（走现有 `/api/agent/turns/{id}/events`，**无需迁移**）；页面按段追加渲染；断线后用历史事件重建 | 已交付（交接 `docs/handoffs/MY_AGENT_M5C_S2_DELTA_STREAMING_HANDOFF.md`）：内核按节奏发 `delta`（**绕开 reporter 的节流/上限**，封顶后收尾补发、不丢字）；页面拼 `agent.message`+`delta`、执行中 900ms 轮询、抽屉过滤增量、文案按事件二选一。**线上实测**：20s 时气泡已有 201 字中文正文（标签「增量显示」、4 个公式已渲染），23s 完成；刷新后完整；`unknown_deltas=0`。**关键坑**：思考与正文的增量都是 `field=text`，只能按 `part.updated` 的 `part.id→type` 区分（第一版把英文思考混进了气泡） |
| **S-3 工具实时 + 权限卡片**（1–2 天）✅ **已交付** | SSE 的工具状态做成实时卡片；**权限请求做成"待批准"卡片**（可在页面上批准/拒绝，取代现在的 `--auto` 全自动） | 已交付（交接 `docs/handoffs/MY_AGENT_M5C_S3_APPROVAL_CARDS_HANDOFF.md`）：三档决定与 opencode 取值一一对应（探针实测 **`reject` 之后文件确实没写出来**、`once`/`always` 会写）；平台 `agent_turn_approvals` + 四个接口、内核上报+有界等待（超时=不执行）、页面待批准卡片。**线上两端验收**：拒绝 → 私有 tmp 里没有该文件；批准 → 文件存在且内容正确。工具状态仍以抽屉里的 `tool.completed` 呈现（实时卡片并入下一期） |
| **S-4 思考 + 模式/强度**（1 天，与 M-5a/b 合并） | 思考块（reasoning delta）折叠展示 + `--agent`/`--variant` 会话级设置（探测→心跳→下拉→插模板） | 右栏能看到思考块；切换模式/强度后命令模板里确实带上了对应参数 |

合计约 5–7 个工作日（含真机验证），每期都能单独上线。

---

## 3 风险与边界

1. **server 生命周期**是新增的最脆弱点：崩溃、端口占用、僵尸进程都要有明确处理（`ProtectSystem` 下端口与临时目录不受影响；systemd 单元已有 `Restart`）。
2. **会话映射**：平台侧的 `session_key` 已经就是 opencode 的会话 id，serve 模式下继续用它续上下文——**不需要新概念**。
3. **取消语义**：serve 模式下"停止"要改成**取消 server 上的会话/消息**（比杀进程更干净）；杀进程仍是兜底。
4. **资源**：常驻 server 每工作目录一份（内存/句柄）；单实例一个工作目录，先按 1 个 server 设计，多目录时再评估。
5. **不假装**：任何一步失败（SSE 不可用、事件形状变了）都**降级 + 标注**，绝不在页面上伪装成流式。
6. **不做**：完整 TUI 复刻、插件系统整套搬入。

---

## 4 与 M-5a/b 的关系

- M-5a（思考可见）与 M-5b（模式/强度）在 `--format json` 通道下也能做（成本小）；
- 但**做了 S-0 之后，思考与工具状态直接从 SSE 拿更完整**（含增量）——所以顺序建议：
  **S-0 → S-1 → S-2（逐字先能用）→ M-5b（模式/强度）→ S-3（权限卡片）→ S-4（思考合并）**。
- 若中途要暂停，`--format json` 通道与现有全部功能（会话/附件/中断）不受影响。

### 与 M-6（多智能体角色预设）的排期（2026-09-23 补充）

M-6 见 `docs/MY_AGENT_M6_AGENT_ROLES_EXECUTION_PLAN.md`。两条线的关系与建议顺序：

1. **M-5b 与 M-6 共用同一条管道**——「会话级设置 → claim 回传 → 内核在 `{prompt}` 前插参数 → 心跳探测 → 页面下拉」。
   角色的 `--agent` 与模式的 `--agent`/`--variant` 是同一个插入点，**合并做一次**，别分两遍改内核、两遍部署、两遍测试。
2. **建议顺序**：**M-6 R-1+R-2**（角色机制 + 写作/审核/检索三个角色——不碰执行链，只改"命令怎么拼"）→
   **本计划的 S-0**（采 SSE 样本，零风险半步）→ **M-5b 并入 M-6 R-4** → **S-1 → S-2 → S-3 → S-4**。
3. 理由：S-1 起是内核最脆弱的执行链改造，别让它挡住"角色"这个立刻可见的价值；S-3 的权限卡片会取代 `--auto`，
   届时**角色级工具边界与权限卡片要一并设计**（角色的 `tools:` 开关已实测生效，两者会叠加）。