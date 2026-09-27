# 重新定位实施计划（阶段 0–4）

> 关联文档：`docs/PRODUCT_STATUS_AND_REPOSITIONING_PLAN.md`（产品定位与验收口径的来源）
> 状态：待拍板。每期开工前按本计划确认范围；借鉴点均来自本地克隆 `reference-repos/{deer-flow,autogen}` 的逐行源码调查（2026-09-26）。
> 约定：工作项编号 W<阶段>.<序号>；迁移号从 034 起顺延；每期交付后跑全量回归（API / Agent 基线只升不降）。

---

## 0. 总览

| 阶段 | 内容 | 关键产出 | 规模（净工作量） | 最大风险 |
|---|---|---|---|---|
| 0 | 产品定位重排 | 三入口首页、导航重组、状态词典、/ask 归并 | 2–3 天 | 文案口径反复 |
| 1 | 单 Agent 工作台产品化 | stop_reason 语义、转入项目生产、SSE 直连 | 5–7 天 | opencode 侧信号有限 |
| 2 | 多 Agent 协作主线 | 事件契约、团队视图、provenance、双执行体 e2e | 8–12 天 | 第二执行体形态（D1） |
| 3 | 通用垂直工作流包 | Workflow/版本/门禁判定器/推进器，CUMCM 迁移 | 10–15 天 | 抽象过度设计 |
| 4 | 自定义自动化编排 | 模板 schema、编辑器、试运行、反向保存 | 6–10 天 | 编辑器范围膨胀 |

**依赖关系**：阶段 0 独立可先行；W1.1（stop_reason）与 W2.1（事件契约）是后面所有阶段的公共底座，应最先做；阶段 3 依赖阶段 2 的事件目录与 provenance；阶段 4 只是阶段 3 之上的编辑器。

**两条最高杠杆的先行项**（小改动、被全路线复用）：
1. **W1.1 stop_reason 语义集**——"任务成功 ≠ 成果批准"、失败/取消/预算触顶如实呈现，全靠它。
2. **W2.1 事件目录与契约**——团队视图、工作流推进、自定义模板的新事件，全都往这一套目录里挂。

**借鉴来源一句话**：deer-flow 抄"长任务工程化"（判停、验收、溯源、触顶降级），autogen 抄"协作与编排抽象"（单决策者、控制消息数据化、可组合门禁、账本推进）。

---

## 1. 借鉴映射总表

| # | 借鉴模式 | 来源（file:line） | 用在 | 我们的落点 |
|---|---|---|---|---|
| 1 | 类型化 blocker + 最新可见产出 SHA-256 判"无进展"熔断 | deer-flow `runtime/goal.py:333-362` | W3.2 推进器防空转 | `workflow_engine.py` 续跑判定 |
| 2 | 确定性验收：四类字面条件 + fail-closed UNVERIFIED | deer-flow `subagents/acceptance_checks.py:88-90,1697-1749` | W3.3 GatePolicy 判定器 | `acceptance.py` |
| 3 | Tool receipt（args/output 哈希凭证） | deer-flow `tool_receipt.py:93-123` | W2.4 provenance | artifacts 溯源字段 |
| 4 | 触顶剥离工具调用 + stop_reason，不抛异常 | deer-flow `token_budget_middleware.py:303-382`、`loop_detection_middleware.py:780-817` | W1.1 真实状态 | agentd/chat_loop |
| 5 | 事件目录 additive 演进：只增不改、未知忽略、seq 递增 | deer-flow `runtime/events/catalog.py:58-94`、`contracts/run_event_stream_contract.json` | W2.1 事件契约 | events/event_outbox 契约文档 |
| 6 | StreamBridge 可插拔（实时与持久日志解耦） | deer-flow `runtime/stream_bridge/base.py:57-90` | W1.3 SSE 直连 | 平台内存 bridge + 轮询兜底 |
| 7 | server-owned 元数据剥离 + 审计归属不信自报标志 | deer-flow `gateway/services.py:133-160`、`audit_context.py:10-29` | W2.2 可信归属 | 写路由身份注入（对齐 RLS） |
| 8 | worker lease/heartbeat/过期接管/fencing | deer-flow `runtime/runs/manager.py:641-645,1442-1489` | W2.6 e2e 支撑 | task_leases 补强 |
| 9 | 单决策者 + `select_speaker` 可插拔 | autogen `_base_group_chat_manager.py:172-193` | W3.2 推进器派发决策 | 推进器"下一个节点"策略字段 |
| 10 | 控制消息数据化（异常也是事件） | autogen `_events.py:38-114`、`SerializableException:10-35` | W2.1 事件目录 | gate/review/失败全部走事件 |
| 11 | 可组合终止/门禁条件（& / \|），交给人是一等条件 | autogen `_termination.py:79-83`、`HandoffTermination:313` | W3.3 门禁组合 | gate 条件 and/or |
| 12 | HandoffMessage（target + context 注入） | autogen `messages.py:421` | W1.2 交给另一个 Agent | promote API 的交接意图 |
| 13 | Magentic-One 账本（facts/plan/round/stall + 停滞重规划） | autogen `_magentic_one_orchestrator.py:300-439` | W3.2 推进器账本 | workflow run 进度账本 |
| 14 | LLM 决策带回退链与纠正重试 | autogen `_selector_group_chat.py:232-308` | W3.2/W4 派发与选角 | 策略失败 fallback 链 |
| 15 | 压缩保留集 + 有界笔记 + 归档检索句柄 | deer-flow `summarization_middleware.py:777-810`、`task_continuity/*` | 备选（暂不排期） | 长会话上下文管理 |

> 明确不抄的：deer-flow 整体 harness（148k 行，按模式取不搬框架）；autogen 的 pub/sub 总线（我们是 DB + SSE 形态，不需要进程内 actor 总线）；delta checkpoint 线性化（等真需要 fork 再说）。

---

## 2. 阶段 0：产品基础重定位

**目标回顾**：界面、文案、导航先表达"Agent 协作生产平台"的正确层级（规划文档 §6 阶段 0）。纯前端 + 文案，无迁移。

| 工作项 | 内容 | 落点 | 借鉴 |
|---|---|---|---|
| W0.1 | 首页三入口：Agent 工作台 / 协作项目 / 自动化工作流，每卡一句话+直达按钮 | `apps/web/app/page.tsx`（或登录后 dashboard） | — |
| W0.2 | 导航重组：三组（工作台 / 项目协作 / 工作流工具）；数学建模模板移入工作流组；老轨只隐藏不删除 | `apps/web/lib/nav.ts` | 老轨纪律 |
| W0.3 | /ask 归并：路由保留、302 → `/my-agent`（带原 query），导航移除（见 D2） | `apps/web/app/ask/page.tsx` | — |
| W0.4 | 状态词典组件：task/run/artifact/handoff/review/gate 每个状态一句用户可读解释；"模板骨架 ≠ 完成结果""自动 = 受约束推进"固定文案 | 新建 `apps/web/lib/status-dictionary.ts` + 各页挂提示 | 命名参考 deer-flow stop_reason 分类法 |
| W0.5 | my-agent 无 Agent 空态直连"接入执行体"引导 | `apps/web/app/my-agent/page.tsx` | — |

**验收对照**（规划 §6）：新用户首屏看懂三种用法；不进数学建模模板也能开始工作；分得清对话与正式生产；不会把应用模板当结果。

---

## 3. 阶段 1：单 Agent 工作台产品化

**目标回顾**：单 Agent 体验可信、顺滑，且能转入正式项目生产（规划 §6 阶段 1）。

### W1.1 stop_reason 语义与真实状态（迁移 034，先行）

- `agent_turns` 增列 `stop_reason`，枚举：`completed / failed / cancelled / token_capped / turn_capped / timeout / permission_timeout / unknown`（枚举 = deer-flow stop_reason ∪ autogen 终止族，收窄到我们能真实判定的）。
- **平台侧先落地四种**：`cancelled`（现有 stop）、`timeout`（平台超时门）、`permission_timeout`（审批超时）、`failed`（执行体异常上报）。执行体侧 `token_capped/turn_capped` 依赖 opencode 会话结束信号映射，后补（见 D5）。
- 借鉴：deer-flow 触顶时**剥离工具调用 + 强制收尾文本**，不抛异常、不破坏 tool_call 配对（`token_budget_middleware.py:303-382`）——对应到 chat_loop 的超时/取消路径，保证 turn 结束时事件流是合法收尾而不是断头。
- 前端：turn 结束状态条如实渲染每种 stop_reason；页面不伪造"已完成"。
- 测试：HTTP 层（写路由声明 Request）+ agentd/chat_loop 单测。

### W1.2 转入项目生产

- API：`POST /api/agent/conversations/{id}/promote`，body 声明 Request（contracts 新模型），行为：会话产物 → artifact（复用现有 chat 产物落库链路）+ 生成 draft task（可选目标项目/任务/交接意图）。
- "交给另一个 Agent"：按 autogen `HandoffMessage`（target + context）语义，把会话摘要注入新任务的提示词上下文——不是简单转发链接。
- 前端：my-agent 消息操作组「保存为成果物 / 创建任务 / 交给另一个 Agent」；表单选目标项目与下游 Agent。
- 测试：promote 幂等（重复提交不重复建 artifact）；RLS 只允许本人会话。

### W1.3 my-agent SSE 直连 + 事件契约

- 平台新增内存 StreamBridge（publish/subscribe 两方法起步），借鉴 deer-flow `stream_bridge/base.py:57-90` 的抽象：**实时层是加速器，持久层 `agent_turn_events` 仍是权威**——断线重连走 `after=<seq>` 增量拉取，900ms 轮询降级为兜底。
- 路由：`GET /api/agent/turns/{id}/events/stream`、`GET /api/agent/conversations/{id}/stream`（SSE，帧格式 `event/data/id`）。
- 契约文档 `docs/AGENT_EVENT_CONTRACT.md`：只增不改、新增字段可选、**消费者必须忽略未知事件**、turn 内 seq 严格递增（借鉴 deer-flow `run_event_stream_contract.json`）。此后前端加新事件类型不再两端同步改。
- 明确不做：Redis bridge（多进程部署前不需要）。

### W1.4 连续路径打磨

附件 → 执行体 `inputs/` → 产出 → 下载/转云盘/转成果物全链状态如实（上传失败、权限超时都有真实状态）。回补桌面端与移动端回归用例。

**验收对照**：不看文档完成一次正常 Agent 工作；一次对话可转入项目；在线/流式/完成状态无一处伪造。

---

## 4. 阶段 2：多 Agent 协作主线（核心）

**目标回顾**：协作从"领域对象存在"变成"看得见、能操作、能验证"（规划 §6 阶段 2）。

### W2.1 项目事件目录与契约（不新增第二套协议）

- **复用 `events` / `event_outbox` 表**，只新增目录层：常量文件 + 契约文档定义 `project.task.* / project.run.* / project.artifact.* / project.handoff.* / project.review.* / project.gate.* / project.agent.*` 七类，事件体带 `schema_version`。
- 借鉴：autogen `_events.py` 把"错误也是事件"（SerializableException 上总线）——gate 拒绝、审核退回、执行失败都成为一等事件，前端不再靠 HTTP 错误码拼过程。
- 组织隔离沿用严格 RLS；事件写入一律服务端生成 seq。

### W2.2 可信归属（对齐 RLS 纪律）

- 所有项目事件/交接/成果上传的 agent 身份由服务端从令牌解析后注入，**不读客户端自报字段**；写路由剥离请求体里的 server-owned 字段（run 归属、证据、来源）。
- 借鉴：deer-flow `audit_context.py:10-29`（只信服务端安装的 recorder）+ `gateway/services.py:133-160`（统一剥离伪造元数据）。
- 测试：伪造归属字段的请求必须被剥离或 4xx。

### W2.3 团队视图与生产路径

- API：`GET /api/projects/{id}/team`（每 Agent：身份/能力/在线/当前任务/负载/等待什么/下一步）、`GET /api/projects/{id}/production-path`（artifact → handoff → 下游 task 的因果链）。
- 前端：workspace 新增「团队」与「生产」视图（不砍聊天）。中间异常状态显式呈现：任务成功但成果未入库、成果待审、退回产生新版本、等待上游批准。
- 借鉴：autogen 双订阅拓扑（定向请求 vs 广播共享，`_base_group_chat.py:206-210`）决定事件投递通道划分——"派给谁"走定向（任务租约），"大家看到什么"走广播（项目事件流）。

### W2.4 provenance 溯源

- 迁移 035：`artifacts` 增 `source_run_id`、`source_tool_hash`；evidence 链补"输入 artifact hash → run → 输出 artifact hash"。
- agentd 产物上传时生成 receipt：`{tool_name, args_sha256, output_sha256, output_bytes, created_at}`（借鉴 deer-flow `tool_receipt.py:93-123`；哈希截断为 16 hex 即可，字段名如实叫 `*_hash` 不叫 sha256，纠正原仓库的字段名误导）。
- 交接与审核页展示："此成果由哪个 Run/哪次工具调用产出，输入来自哪个已批准成果"。

### W2.5 租约补强

`task_leases` 增 fencing token 与过期接管（借鉴 deer-flow `runs/manager.py:641-645,1442-1489`）：租约过期后任务可被重新领取，旧持有者的续约/状态写入凭旧 token 直接拒绝——防止双执行体并行时双写。

### W2.6 端到端验收剧本

- `scripts/verify_multi_agent_e2e.py`（沿 `verify_llm_channels_e2e.py` 模式：真 uvicorn + 真执行体，34+ 断言风格）。
- 剧本照规划 §6：双 Agent 并行 → A 上传成果+交接 → B 读批准输入 → 一个任务失败并重试 → 一个成果退回出新版本 → 复核 Agent 结论 → 人工批准。
- 执行体形态见 D1。
- 并发纪律：任务状态机单写者——所有状态迁移用条件更新（`UPDATE ... WHERE status=<expected>`），借鉴 autogen `sequential_message_types` 的"显式串行边界"思想在数据库层的等价物。

**验收对照**：两个 Agent 分工交接真实发生、不依赖静态剧本；任务/Run/成果/交接/证据/审核互相可追溯；失败重试退回阻塞不被隐藏；一眼看出多 Agent 协作。

---

## 5. 阶段 3：通用垂直工作流包（抽象核心）

**目标回顾**：数学建模降级为"首个工作流包实例"，抽象出通用内核（规划 §6 阶段 3）。

### W3.1 数据模型（迁移 036/037，形态见 D4）

- `workflows` + `workflow_versions`（定义存 JSON 列：stages、nodes、deps、role_bindings、handoff_contracts、gate_policies、retry_policy、human_intervention_points、delivery_adapter）+ `project_workflow_runs`（**运行绑定 version_id，模板后续修改不改写历史**——规划 §5.4 硬要求）。
- 借鉴：autogen "编排策略 = 数据 + 一个函数"（三种群聊只差 select_speaker）——我们把编排做成**版本化数据**而非硬编码；autogen `save_state` 按逻辑名存、可跨 runtime 迁移（`_base_group_chat.py:748-771`）→ workflow 定义与运行状态解耦于具体执行体。

### W3.2 推进器 `apps/api/app/workflow_engine.py`

- 主循环：依赖就绪 → 派发（能力匹配/manual-hybrid-auto 模式沿用现有逻辑）→ 验收（W3.3）→ 门禁 → 推进下一节点；每步写 W2.1 事件。
- **防误判与防空转（本计划最重的借鉴组合）**：
  - 进度账本 `{facts, plan, round, stall_count}`（借鉴 autogen Magentic-One `_magentic_one_orchestrator.py:300-439`）；
  - 续跑只认类型化 blocker（`goal_not_met_yet` 才可续）+ **最新可见产出 SHA-256 判无进展** + 续跑次数上限（借鉴 deer-flow `goal.py:333-362`，默认 max 8 次/无进展 2 次）；
  - 派发与选角失败走回退链（指定角色 → 能力匹配 → 人工派发），绝不空转（借鉴 autogen selector `_selector_group_chat.py:232-308`）。
- 失败策略：重试 / 转调试角色（handoff 语义）/ 升级人工——"交给人"是一等策略（借鉴 autogen `HandoffTermination`）。

### W3.3 门禁判定器 `apps/api/app/acceptance.py`

- 条件语法（字符串字面量，忽略大小写，照抄 deer-flow 四类并扩展两类）：
  `file:<path> exists|non-empty`、`file_written:<path>`、`tests_passed:<command>`、`artifact:<type> approved`、`review:<role> concluded`。
- **fail-closed**：任何无法判定的条件 → `UNVERIFIED`，不算通过；`tests_passed` 要求显式 pass 摘要，`0 passed` 不算过（借鉴 deer-flow `acceptance_checks.py:88-90,1697-1749`）。
- 条件可组合 and/or，人工批准是一等条件（借鉴 autogen `_termination.py:79-83`）。
- 这直接落实规划 §7 红线："任务成功 ≠ 成果物已批准"。

### W3.4 CUMCM 迁移

- `cumcm_importer.py`（330 行，现生成任务/成果骨架）改为生成 **workflow 定义 JSON**（四问 DAG → stages/nodes）；`competition_workflow_adapter.py` 的 `SKILL_STAGE_MAP`（skill→stage/produces 映射）进定义数据；10 个 `deploy/cloud-agent/roles/mm-*.md` 角色预设转 role_bindings，数学建模专属提示词收进包定义。
- 老入口保留不删（老轨纪律），新入口走通用工作流。

### W3.5 第二工作流包：长文写作

用同一抽象定义一个最小长文包（大纲→初稿→修订→定稿），验证"换包不重写 Task/Run/Artifact/Handoff/Review"；复用 `document_drafts` 与文档版本链。

### W3.6 DeliveryAdapter

论文装配/编译/提交包（现有交付流程）收进适配器接口；PPT、文档导出后置。

### W3.7 工作流包页面

显式标注「生成骨架 / 待执行 / 待审核 / 可交付」；工作流实例可回答"每个 Agent 为什么执行、输入来自哪里、输出交给谁"（依赖 W2.4 provenance）。

**验收对照**：数学建模与长文共享内核；实例全程可解释；模板版本变更不改历史。

---

## 6. 阶段 4：自定义自动化编排

| 工作项 | 内容 | 借鉴 |
|---|---|---|
| W4.1 | 模板 JSON Schema + 服务端校验器（预览/校验/版本发布/复制）；沿用 cumcm_importer 的"未识别资产单列、绝不静默丢弃"校验风格 | — |
| W4.2 | 结构化表单编辑器先行（阶段/节点/角色/提示词/交接/门禁），DAG 可视化后置 | — |
| W4.3 | 试运行：模板 → 沙箱项目 dry-run，只展开任务图不真跑——防"误以为已得到结果" | — |
| W4.4 | 反向保存：从成功项目抽取 workflow 草稿 | — |
| W4.5 | 运行绑定模板版本；自定义模板新增事件走 W2.1 契约（additive），前端不破 | deer-flow 事件契约 |

---

## 7. 横切工程纪律（每期不变）

- 写路由必须声明 Request 并配 HTTP 层测试；迁移最小增量、严格 RLS；老轨只隐藏不删除。
- 不新增第二套权限/审批/事件协议（规划 §7 红线）；凭据不进仓库；提交前脱敏扫描。
- 对外文案口径：只说"图/图谱"；"自动"只承诺受约束的调度推进。
- 每期收尾：全量回归（API ≥748、Agent ≥473 基线只升不降）→ `pack-source.sh` → `server_release.sh --public-url https://synapforge.top` → `server_verify.sh` → push（github.com 间歇阻断，先直试 `git push`，失败走 api.github.com fallback）。
- 每期验收回答规划 §8 四问（不用读实现能完成？协作可见可追溯？自动化真推进？状态如实？）。

---

## 8. 需要拍板的决策点

| # | 决策 | 选项 | 推荐 |
|---|---|---|---|
| D1 | 阶段 2 e2e 的第二执行体形态 | a) 同机 systemd 模板双实例（map-agent@a/@b，零新增硬件，真实并行进程）；b) 云端 + 用户本地设备各一；c) 单执行体串行模拟 | **a**——`map-agent@.service` 本就是模板单元，双实例最省事且"并行"为真 |
| D2 | /ask 处置 | a) 302 → /my-agent + 导航移除；b) 保留双入口加说明 | **a**（老轨保留路由，只隐藏不删除） |
| D3 | W1.3 SSE 直连的期次 | a) 阶段 1 末尾做；b) 推迟到阶段 2 前单独做 | **a**——团队视图（W2.3）依赖它积累的桥接与重连经验，且改动封闭在 my-agent |
| D4 | 阶段 3 workflow 定义的落库形态 | a) JSON 列（迁移小、演进快、校验靠服务端）；b) 全表结构化（查询强、迁移重） | **a**——定义整体读写、无跨定义查询需求；运行状态仍结构化落 tasks/runs |
| D5 | stop_reason 执行体侧来源 | a) 平台侧先落 4 种，opencode 信号映射后补；b) 等摸清 opencode 结束信号一次做完 | **a**——不为等信号阻塞阶段 1 |

---

## 9. 里程碑顺序建议

| 期 | 内容 | 预估 |
|---|---|---|
| 第一期 | 阶段 0 全部 + W1.1（stop_reason，迁移 034） | 3–4 天 |
| 第二期 | W1.2 转入项目生产 + W1.4 连续路径 + W1.3 SSE 直连 | 4–6 天 |
| 第三期 | W2.1 事件契约 + W2.2 可信归属 + W2.3 团队视图 | 4–6 天 |
| 第四期 | W2.4 provenance（迁移 035）+ W2.5 租约补强 + W2.6 双执行体 e2e | 4–6 天 |
| 第五期 | W3.1 数据模型（036/037）+ W3.2 推进器 + W3.3 门禁判定器 | 6–8 天 |
| 第六期 | W3.4 CUMCM 迁移 + W3.5 长文包 + W3.6 适配器 + W3.7 页面 | 5–7 天 |
| 第七期 | 阶段 4 全部 | 6–10 天 |

每期独立可交付、可上线；第一、二期结束即可对外宣告"单 Agent 工作台"完成重定位。
