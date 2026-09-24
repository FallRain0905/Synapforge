# MY-AGENT M-5c S-0 交接：真实 `opencode serve` + SSE 事件样本

> 2026-09-24 · 状态：**S-0 完成**（样本是真的，不是猜的）· 计划 `docs/MY_AGENT_M5C_STREAMING_EXECUTION_PLAN.md`
> 采样机：云端执行体 `154.219.99.75`，opencode **1.18.32**，provider 别名 `deepseek` → 百炼 `deepseek/deepseek-v4.1-flash`
> 探针纪律：装→测→**必删**（会话、serve 进程、`/tmp/oc-*` 文件全部清掉，`auth.json` 已删；机器状态与采样前一致）

---

## 1 结论先说（三条，都会影响 S-1 怎么写）

1. **走 v1 通道**：`POST /session` → `POST /session/{id}/message` → `GET /event`（SSE）。
   **不要用 `/api/*` 那一套 v2 接口**——v2 的会话运行器**认不出我们配置里的自定义 provider**，
   会话会卡住并发 `SessionRunnerModel.ModelUnavailableError: Model unavailable: deepseek/deepseek-v4.1-flash`
   （opencode 自己的日志原文）。v1 通道用同一份配置、同一个模型，实测正常出字。
2. **逐字流是真的、且字段明确**：`message.part.delta` 事件里 `properties.field ∈ {text, reasoning}`、
   `properties.delta` 就是增量片段（本轮 51 条 text delta）；`message.part.updated` 是**整段快照**
   （思考块的 text 只在 updated 里增长：0 → 40 → 184 字）。→ S-2 的 `delta` 帧有现成来源。
3. **权限请求真的会拦路**：模型要写 `/tmp/…` 时发出 `permission.asked`，
   **在回复之前这一轮不会继续**（探针就卡在这里，直到我回了 `{"response":"always"}`）。
   → S-3 的权限卡片有真实事件；同时 S-1 必须决定"没人看着时怎么自动回"（等价于现在的 `--auto`）。

## 2 实测到的完整事件清单（一轮：ls → 写文件 → 表格）

| 事件类型 | 条数 | 说明 |
| --- | --- | --- |
| `server.connected` / `server.heartbeat` | 1 / 17 | 连接与心跳 |
| `plugin.added` | 45 | 每个插件一条（噪音，S-1 可忽略） |
| `catalog.updated` / `reference.updated` / `integration.updated` | 2 / 1 / 1 | 目录与集成状态 |
| `session.created` / `session.updated` / `session.status` / `session.diff` / `session.idle` | 1 / 6 / 8 / 4 / 1 | **`session.idle` = 这一轮结束**（S-2 的完成信号） |
| `message.updated` | 14 | 消息级状态（含 `info.model`、`info.agent`、cost/tokens） |
| `message.part.updated` | 21 | 整段快照：`step-start` 3 / `reasoning` 4 / `text` 3 / **`tool` 8** / `step-finish` 3 |
| **`message.part.delta`** | 51 | **逐字增量**（本轮全部是 `field=text`） |
| `permission.asked` / `permission.replied` | 1 / 1 | 权限请求与回复 |
| `file.edited` / `file.watcher.updated` | 1 / 1 | 文件改动（产出采集可复用） |

## 3 逐条样本原文（截断处标 `…`）

**① 逐字增量（S-2 的 delta 帧就是这个）**
```json
{"id":"evt_0d0d8b4d30015pJ413UJSPny2B","type":"message.part.delta","properties":{"sessionID":"ses_…","messageID":"msg_…","partID":"prt_…","field":"text","delta":"Let"}}
```

**② 思考块（`reasoning`）——只有整段快照，没有 delta**
```json
{"type":"message.part.updated","properties":{"sessionID":"ses_…","part":{"id":"prt_…","type":"reasoning","text":"","time":{"start":1790210258125}}}}
```
（同一 `partID` 的 text 随后增长：`"" → 40 字 → 184 字`；本轮 4 条 reasoning 更新，0 条 reasoning delta）

**③ 工具调用生命周期（`tool` part 的 8 次更新 = 状态机）**
```text
pending   {input, raw, status}                                  tool=bash
running   {input, status, time}
running   {input, metadata, status, time}
completed {input, metadata, output, status, time, title}        ← 完成后才有 output/title
```
```json
{"type":"message.part.updated","properties":{"part":{"type":"tool","tool":"bash","callID":"call_…","state":{"status":"pending","input":{},"raw":""}}}}
```

**④ 每步用量（会话级 token 账，`step-finish`）**
```json
{"type":"message.part.updated","properties":{"part":{"type":"step-finish","reason":"tool-calls","tokens":{"total":7846,"input":7793,"output":37,"reasoning":16,"cache":{"write":0,"read":0}},"cost":0}}}
```

**⑤ 权限请求（S-3 的字段全在这里）**
```json
{"type":"permission.asked","properties":{"id":"per_0d0d8c214001ANISqnh5AA5Sn4","sessionID":"ses_…","permission":"external_directory","patterns":["/tmp/*"],"metadata":{"filepath":"/tmp/oc-probe-out.txt","parentDir":"/tmp"},"always":["/tmp/*"],"tool":{"messageID":"msg_…","callID":"call_…"}}}
```
回复：`POST /session/{sid}/permissions/{perId}`，体 `{"response":"always"}`（实测返回 `true`），随后事件：
```json
{"type":"permission.replied","properties":{"sessionID":"ses_…","requestID":"per_…","reply":"always"}}
```

**⑥ 轮次结束 / 文件改动**
```json
{"type":"session.idle","properties":{"sessionID":"ses_…"}}
{"type":"file.edited","properties":{"file":"/tmp/oc-probe-out.txt"}}
```

**⑦ 同步返回**：`POST /session/{id}/message` 阻塞到这一轮结束才返回，体为
`{"info": {...AssistantMessage...}, "parts": [...]}`（同一个响应里就有完整正文与用量，SSE 只是"过程可见"）。

## 4 两个通道的对照（这次实测，别再踩）

| | **v1 通道**（`/session` + `/event`） | v2 通道（`/api/session`、`/api/event`） |
| --- | --- | --- |
| 认配置里的自定义 provider（`deepseek` 别名 + 百炼 baseURL） | ✅ 实测出字（`message.updated` 里 `model={providerID:deepseek, modelID:deepseek-v4.1-flash}`） | ❌ `ModelUnavailableError`；`GET /api/model` 只列 33 个 opencode 免费模型 |
| 逐字增量 | ✅ `message.part.delta` | 事件名更细（`session.next.text.delta` / `reasoning.delta` / `tool.*`），但**用不了**（同上） |
| 免费 zen 模型 | 未测（不需要） | 实测 401 `missing_api_key`（v2 默认模型是 `nano-gpt/stealth/space-bunny-alpha` 之类，跟配置里的默认模型不是一回事） |
| `auth.json` 凭据 | **不需要**（A/B 实测：删掉 auth.json 后 v1 照样跑通，token 账 `output:2 / cache.read:2048`） | 加了也没用（v2 不认这个 provider） |
| 权限 | `permission.asked` → `POST /session/{id}/permissions/{permissionID}` | `GET /api/permission/request` + `POST /api/session/{id}/permission/{requestID}/reply`（同样不可用） |
| 中断 | `POST /session/{id}/abort` | `POST /api/session/{id}/interrupt` |

**serve 启动实测**：`opencode serve --port 4123 --print-logs --log-level INFO`（默认 `--hostname 127.0.0.1`、`--port 0` 随机）。
日志警告：`OPENCODE_SERVER_PASSWORD is not set; server is unsecured` → **S-1 必须设这个环境变量**（内核只连回环也一样，别裸奔）。

## 5 对 S-1…S-4 的具体改动（把计划里的"大概"落成"确定"）

- **S-1**：`ChatServerManager` 起 serve 时要带 `OPENCODE_SERVER_PASSWORD`（随机生成、只在内核内存里）；
  轮次执行改成 v1 三步（建/续会话 → 发消息 → 订阅 `/event`）；**降级通道仍是 `--format json`**（现成的）。
  注意两点：① 一个工作目录一个 server（会话/项目上下文跟着目录）；② `POST /session/{id}/message` 是**阻塞**返回，
  所以要么在独立线程里发、要么用 `prompt_async`（v1 也提供了 `POST /session/{id}/prompt_async`）。
- **S-2**：新事件类型 `delta`（直接用 `message.part.delta` 的 `field`/`delta`）；轮次结束以 `session.idle` 为准；
  `step-finish.tokens` 就是现成的用量账。
- **S-3**：权限卡片直接用 `permission.asked` 的字段（`permission` / `patterns` / `metadata.filepath`），
  回复走 `POST /session/{id}/permissions/{permissionID}`；**没人看着时的默认策略必须显式定**（建议：只读工具自动放行、
  写/外目录一律拒绝并在页面显示"需要你点一下"）——否则会像这次探针一样**静默卡住**。
- **S-4**：`--variant` 有对应物——v2 的 `ModelRef` 里有 `variant` 字段、会话信息里 `model.variant="default"`；
  v1 用 `POST /session/{id}/message` 的 `model` 字段或 `PATCH` 会话模型（S-4 时再采一次样本确认 v1 的变体怎么带）。

## 6 没测到的（说清楚，别当成已知）

- **v1 通道下思考（reasoning）有没有 delta**：本轮只有整段快照；v2 的事件名里有 `reasoning.delta`，但 v2 通道用不了 → 待 S-4 采一次"明确要求它想一下"的样本。
- **多轮上下文**：本轮只跑了一轮；`session` 复用与 `--session` 的等价性属 S-1 的验收项。
- **中断语义**：`POST /session/{id}/abort` 存在但没实打实试过（S-1 要测：中止后 `session.idle` 是否照常来）。
- **并发**：同一 serve 同时两个会话没测（S-1 按"一次一轮"设计即可）。