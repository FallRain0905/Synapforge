# COST-1 交接：执行用量回报 → token/耗时预算可判定 · 已上线

> 2026-09-23 · 接 AIP-1d 的"未做第一条"（见 `docs/handoffs/AIP_1D_TASK_INTENT_HANDOFF.md` §5）
> 线上：https://synapforge.top · 发布记录 `docs/SERVER_DEPLOYMENT.md` §10

## 1 解决了什么

AIP-1d 给任务加了 `budget.max_tokens`，但平台没有任何用量采集，所以那时只能标注"只记录不强制"。
本期把"**执行体回报用量**"这条链路补齐，token 上限从"只记录"变成"可判定"：

| 环节 | 做法 |
| --- | --- |
| 采集 | 执行体的 JSONL 里本来就有 `turn.completed.usage`（`codex_executor` 已在解析）；现在 `ExecutorEventReporter` 顺带累计，`TaskLoop` 把它随完成上报一起发（`usage`） |
| 耗时 | **由任务循环自己观测**（`monotonic` 前后差），不依赖执行体自报——更可信；两者出处分开记（`seconds_source`） |
| 落库 | 迁移 `023_run_usage.sql`：`runs.usage jsonb`；`total_tokens` 缺省按 in+out 推导 |
| 判定 | 完成 Run 时：超预算 → 一次性事件 `task.budget_exceeded` + 聊天卡片；**批准任务时**：超预算 → 门禁 `task:usage_budget_exceeded` blocking finding + 拒绝批准（不自动批准） |
| 展示 | `/runs` 列表与详情显示用量（含**出处**）、`/tasks` 详情显示"已回报 N tokens（M 次执行有数）"、`/tasks` 与工作区任务行加「缺证据 N」「超预算」角标 |

**诚实性是本期的核心约束**（延续 AIP-1d 的态度）：

- **不报用量 = 没数据，不是 0**。`usage_reported_runs = 0` 时界面说"还没有执行体回报过用量"，不显示"0 tokens"（那会被读成"没花"）。
- **出处必须标出来**：token 的 `source`（`codex-jsonl` / `agent-reported` / `platform-observed`）与耗时的 `seconds_source`（`agent` / `platform`）分开记——混成一个字段就分不清"谁在说话"。
- **超出预算不自动改任务状态**：只留痕（事件 + 门禁 finding），判断权留给人工门禁。系统不替人做"这笔超支算不算"的决定。
- **超时判定有余量**：`max_seconds` 超限判定允许 10% 或 30 秒的余量（进程收尾、上报延迟不该被判成超时）。

## 2 落点

**后端**
- `apps/api/migrations/023_run_usage.sql`（新）：`runs` + `usage` jsonb + `(usage -> 'total_tokens')` 索引；迁移清单与断言同步 `test_platform_contracts.py`。
- `contracts.py`：`RunUsage`（自动补 `total_tokens`）、`RunComplete.usage`（可选）、`Run.usage`、`TaskFlags`、`TaskBudgetState` + `tokens_used`/`usage_reported_runs`。
- `store.py`：
  - `_normalized_usage`（token 取回报、耗时自报优先/平台兜底、出处分开记）、`_usage_overruns`（超限判定，含余量）、
  - `_note_budget_exceeded`（按 Run 幂等的一次性事件）、`task_usage_summary`（累计用量）、
  - `complete_run` 落 usage 并即时判定、`create_review` 批准路径加 `task:usage_budget_exceeded` finding、
    `_task_budget_state` 补累计量、`project_task_flags`（批量角标，两次分组查询）、聊天卡片 `task.budget_exceeded`。
- `main.py`：`GET /api/projects/{project_id}/task-flags`（列表角标用；读时聚合，避免列表逐条查库）。

**Agent 侧**
- `executor_events.py`：`feed/flush` **不再因为没有 sink 就跳过解析**（用量只在输出里能得到），新增 `usage()`；
  `turn.completed` 的 `usage` 累计但不作为过程事件上报（群聊不该被用量刷屏）。
- `task_loop.py`：执行前后用 `monotonic` 计时；`usage_provider` 注入 token；完成上报带 `usage`。
- `agentd.py`：reporter 总是创建（`emit=None` 时只解析不发事件），把 `reporter.usage` 接进任务循环。

**前端**
- `lib/api.ts`：`RunUsage`、`TaskFlags`、`getProjectTaskFlags`；`Run.usage`、`TaskBudgetState` 扩字段。
- `/runs`：列表行显示"用量 N tokens · 耗时 M 秒"，详情加 chip「用量 10800 tokens（codex-jsonl）· 耗时 94 秒（执行体自报）」。
- `/tasks`：详情执行态改为"已回报 N tokens（M 次执行有数）/ 还没有执行体回报过用量"；清单行加「缺证据 N」「超预算」角标。
- 工作区任务板：同一对来自 `/task-flags` 的角标。

## 3 验收（全部实测）

| 项 | 结果 |
| --- | --- |
| 后端全量 | **508 项**（+11 `test_run_usage`），仅 2 项既有 LaTeX 环境失败 |
| Agent 套件 | **302 项（9 skipped）**（+4：reporter 用量 2 + 任务循环上报 2） |
| 前端 | `tsc --noEmit` 干净、`next build` 25 页、SSR 不变量 19×19×6 |
| 浏览器实机（本地） | 任务清单「缺证据 1」「超预算」角标；任务详情"已回报 10800 tokens（1 次执行有数）"；运行详情 chip「用量 10800 tokens（codex-jsonl）· 耗时 94 秒（执行体自报）」；运行列表行"用量 10800 tokens · 耗时 94 秒"；聊天卡片"任务「修订定稿」这次执行超出预算（10800 tokens / 上限 5000），批准时会被门禁拦下" |
| 线上发布 | `server_release.sh` 成功；`server_verify.sh` **22/22** |
| 线上功能（`_cost1_verify.py`） | **13 项全 PASS**：用量推导与出处、一次性事件、聊天卡片、累计用量回显、批准被拒 + 门禁 `FAILED` 含 `task:usage_budget_exceeded`、不报用量时字段为空且无事件、清理后 0 残留 |
| 线上包内容 | `tasks` chunk 含 `task-overrun-` 与"已回报"；`/api/projects/{id}/task-flags` 匿名 401（鉴权生效）；线上库 `COST1%` 任务 0、`budget_exceeded` 事件 0、`runs.usage` 列已建 |

## 4 边界与未做

- **只有回报了用量才能判定 token 上限**：通用 CLI 执行体（`worker_executor=cli` / 声明式命令）通常没有用量可报，
  这类任务的 token 上限仍然是"不可判定"（界面如实说"还没有执行体回报过用量"，不假装在管）。
- **累计口径是"整条任务"**：`tokens_used` 是该任务所有 Run 的合计；单次上限（`max_tokens`）按**每次执行**判定。
  没有做"整条任务的 token 总额"（那需要在预算里再加一个维度，按需再说）。
- **桌面端安装包未重建**：内核侧的用量回报要等下次封包才对已装用户生效（老内核只是不报用量，功能不受影响）。
- 交付页（`/workspace` 成果空间）的任务行未加角标（角标已覆盖 `/tasks` 与工作区任务板两处）。