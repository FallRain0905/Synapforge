# 任务书 C：独立模块与预研（三会话并行开发）

> 日期：2026-09-26。你是三个并行会话中的 **会话 C**。本任务书自包含，开工前通读一遍即可。
> 权威文档：`docs/PRODUCT_STATUS_AND_REPOSITIONING_PLAN.md`（产品定位）、`docs/REPOSITIONING_IMPLEMENTATION_PLAN.md`（实施计划 §1 借鉴映射总表是你的设计依据）。
> 仓库根：`C:\Users\19855\Documents\ChatGPT\数学建模\math-agent-platform`。

## 0. 你的领地（**只允许新建**以下文件，绝不修改任何现有文件）

| 文件 | 用途 | 交付期 |
|---|---|---|
| `apps/api/app/acceptance.py` + `apps/api/test_acceptance.py` | 确定性验收判定器 | 第一期 |
| `apps/api/app/event_catalog.py` + `docs/AGENT_EVENT_CONTRACT.md` | 项目事件目录与契约 | 第一期 |
| `apps/api/app/stream_bridge.py` + `apps/api/test_stream_bridge.py` | 内存实时桥（可插拔） | 第二期 |
| `docs/RECEIPT_FORMAT.md` | 产物溯源 receipt 格式契约 | 第一期或第二期 |
| `apps/api/app/progress_ledger.py` + test（命名可定） | 推进器账本 + goal 判停纯逻辑 | 第三期 |
| `docs/WORKFLOW_SCHEMA.md` | workflow 定义 JSON Schema 草案 | 第二期起 |

会话 A 拥有 `apps/api/**` 现有文件与迁移；会话 B 拥有 `apps/web/**` 与 `apps/agent/**`。
**你缺接口、缺落库、需要接线时，一律写进收尾报告的"给 A 的接口需求"，自己不动现有文件。**
不跑 `npm run build`、不部署、不 push、不建迁移。commit 只含你新建的文件。

## 1. 三会话分工速览

| 会话 | 领地 | 定位 |
|---|---|---|
| A | apps/api 现有文件 + 迁移 + 部署 | 路由/迁移/落库 + 整合（消费你的模块） |
| B | apps/web + apps/agent | 前端 + 执行体 |
| C（你） | 只新建独立模块 + 契约文档 | 纯逻辑 + 单测 |

## 2. 参考实现（本地已克隆，只读）

`C:\Users\19855\Documents\ChatGPT\数学建模\reference-repos\` 下：

- `deer-flow/backend/packages/harness/deerflow/`
  - `subagents/acceptance_checks.py:88-90,1697-1749` —— 验收语法与 fail-closed
  - `runtime/stream_bridge/base.py:57-90` —— 桥抽象（publish/subscribe）
  - `runtime/goal.py:333-362` —— 类型化 blocker + 可见产出 SHA-256 判停
  - `runtime/events/catalog.py:58-94` + 仓库根 `contracts/run_event_stream_contract.json` —— 事件契约
- `autogen/python/packages/autogen-agentchat/src/autogen_agentchat/teams/_group_chat/`
  - `_events.py:38-114` —— 控制消息数据化
  - `_magentic_one_orchestrator.py:300-439` —— 进度账本
  - `base/_termination.py:79-83` —— 条件可组合（and/or）

借鉴模式抄思想与接口形状，不搬代码（许可证与代码风格都不同，且我们是 DB+SSE 形态）。

## 3. 全局纪律

- 每段代码 `python -m py_compile` 验证后再继续；测试跑 `cd apps/api && python -X utf8 -m unittest test_acceptance -v`（单文件）。
- 你的测试会被全量 discover 收进基线（基线只升不降，是好事），所以测试必须**自包含、无网络、无固定端口、临时目录自清理**。
- 凭据/口令不进任何文件；不改 README；不动禁区文件（globals.css、login、register、shell.tsx）。
- 环境出现内容串行损坏（工具结果自相矛盾）立即停手报告。

## 4. 第一期任务（现在就做）

### 4.1 `acceptance.py` 确定性验收判定器（对应实施计划 W3.3）

- 条件语法（字符串字面量，忽略大小写，照 deer-flow 四类并扩展两类）：
  - `file:<path> exists` / `file:<path> non-empty`
  - `file_written:<path>`
  - `tests_passed:<command>`
  - `artifact:<type> approved`
  - `review:<role> concluded`
- **fail-closed**：未知语法、路径越界、无法探测 → `checked=False, holds=False`，标记 UNVERIFIED；`all_hold` 要求所有叶子 checked 且 holds。
- `tests_passed` 只认显式 pass 摘要，`0 passed` 或无摘要 = UNVERIFIED。
- 条件可组合 and/or（照 autogen `_termination.py` 的组合语义）；`artifact/review` 两类通过**注入的判定接口**查询（你不查数据库，定义 Protocol 让 A 接线）。
- 文件探针同样走注入接口（`probe` Protocol），便于测试与平台侧替换。
- 全套单测：每类语法的通过/失败/UNVERIFIED 三态、组合语义、注入接口的桩测试。

### 4.2 `event_catalog.py` + 事件契约文档（对应 W2.1）

- 七类事件常量：`project.task.* / project.run.* / project.artifact.* / project.handoff.* / project.review.* / project.gate.* / project.agent.*`；每事件带 `schema_version`。
- 提供目录校验函数（事件名必须注册在目录、未知事件名拒绝发出）。
- `docs/AGENT_EVENT_CONTRACT.md` 契约四规则：**只增不改、新增字段可选、消费者必须忽略未知事件、turn/项目内 seq 严格递增**；SSE 帧格式 `event/data/id`。
- 参考 deer-flow 事件目录与 autogen `_events.py`（错误/终止也是一等事件）。

### 4.3 `docs/RECEIPT_FORMAT.md` receipt 格式契约（对应 W2.4，本期至少出草案）

字段：`tool_name, tool_call_id, args_hash, output_hash, output_bytes, status, created_at`。
**哈希为 SHA-256 截断 16 hex，字段名如实叫 `*_hash`**（deer-flow 字段名叫 sha256 但实为截断值，是命名误导，我们纠正）。B 在 agentd 生成、A 校验落库，文档写清两侧职责。

### 4.4 收尾报告

- 模块清单 + 单测结果（`unittest` 输出摘要）
- "给 A 的接线说明"：每个模块的 import 方式、公开函数签名、预期接线点（哪条路由/哪个服务）
- "给 B 的契约确认"：receipt 字段、事件类型清单

## 5. 第二期任务预告

- `stream_bridge.py`：`publish(topic, event)` / `subscribe(topic, after_seq)`，内存实现、线程安全、订阅游标（`after=<seq>` 续传）、背压上限；单测覆盖并发发布与断线重放。参考 deer-flow `stream_bridge/base.py:57-90`（抽象一致，实现是单进程内存版，不做 Redis）。
- `docs/WORKFLOW_SCHEMA.md` 草案：stages/nodes/deps/role_bindings/handoff_contracts/gate_policies/retry_policy/human_intervention_points/delivery_adapter；定义存 JSON 列（D4 已冻结），校验靠服务端。

## 6. 第三期任务预告

- `progress_ledger.py`：账本 `{facts, plan, round, stall_count}` 序列化与更新规则（autogen Magentic-One 语义）+ goal 判停纯逻辑（类型化 blocker 白名单只有 `goal_not_met_yet` 可续、最新可见产出 SHA-256 判无进展、`max_continuations=8 / max_no_progress=2`，deer-flow `goal.py` 语义）+ 派发回退链（指定角色→能力匹配→人工，autogen selector 语义）。

## 7. 完成标准（第一期）

- [ ] `acceptance.py` + 全套单测绿（三态 × 五类语法 × 组合）
- [ ] `event_catalog.py` + 契约文档落地，校验函数可独立运行
- [ ] `RECEIPT_FORMAT.md` 至少草案
- [ ] 单文件 unittest 全绿、py_compile 全过
- [ ] 报告：模块清单、给 A 的接线说明、给 B 的契约确认、遗留项
