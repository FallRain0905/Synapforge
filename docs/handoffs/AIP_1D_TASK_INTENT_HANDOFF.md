# AIP-1 第 2 批交接：意图对象的两个缺项（预算 / 显式证据要求）· 已上线

> 2026-09-22 · 计划见 `docs/AIP_1_PLAN.md` §6，本期交付 **AIP-1d**（AIP-1 的最后一期）
> 线上：https://synapforge.top · 发布记录 `docs/SERVER_DEPLOYMENT.md` §10 · 上一批见 `AIP_1AB_DESCRIPTION_DISCOVERY_HANDOFF.md`

## 1 交付内容

| 字段 | 语义 | 强度（诚实表） |
| --- | --- | --- |
| `budget.max_seconds` | 单次执行墙钟上限（30–86400 秒） | **强制**：领取时把租约到期压到 `min(请求, 上限)`，**续租也不能越过上限**（`heartbeat_lease` 里同样夹紧，过了上限直接判租约过期） |
| `budget.max_attempts` | 整条任务允许的领取次数（1–50） | **强制**：领取前数 `task_leases`（每次领取一行）；超限 → 点名领取报 `task_budget_exhausted`，轮询 `claim_next_task` **跳过**（不整体失败），自动调度器也跳过并写一次性告警 |
| `budget.max_tokens` | token 上限 | **只记录**：平台没有 token 计量（`runs` 无 token 字段、`RunComplete` 无 usage），写进任务并原样回显，界面明确标"未强制：平台不采集用量" |
| `evidence_requirements` | `[{evidence_type, min_count, note}]`，类型复用既有 Evidence 词表 | 读时算缺口（展示）+ **批准时校验**：缺证据 → 拒绝批准，并在门禁上留下 `task:evidence_requirements` 的 blocking finding（`status=FAILED`）；**不自动批准**、不阻止提交复核 |

**为什么 token 只记录不做强制**：没有用量采集就没有"花钱"的事实依据，硬做一个看起来在管成本的字段比诚实标注更坏。
要真强制，前置工作是"执行体在完成时回报用量"（codex 的 JSONL 有 usage、cli 执行体没有）——留给后续。

## 2 落点

- 迁移 `apps/api/migrations/022_task_intent.sql`：`tasks` + `budget`/`evidence_requirements`（jsonb）+ 两个排查用表达式索引；
  迁移清单与断言已同步 `test_platform_contracts.py`。
- `contracts.py`：`TaskBudget`（自带 `budget_requires_a_dimension` 校验）、`EvidenceRequirement`、`TaskEvidenceGap`、
  `TaskBudgetState`、`TaskDetail`；`TaskCreate`/`Task`/`TaskUpdateRequest` 各加两字段（PATCH 沿用"出现才生效"，`null`/`[]` = 清除）。
- `store.py`：`_validated_budget` / `_validated_evidence_requirements`（类型与数量把关）、`task_evidence_gaps`（读时缺口）、
  `_task_budget_state`（已领取次数/是否用尽）、`_note_budget_exhausted`（幂等一次性事件）、
  `claim_task` 的强制两项、`claim_next_task` 与 `auto_dispatch_tick` 的跳过、`heartbeat_lease` 的上限夹紧、
  `create_review` 批准路径的证据校验、聊天卡片文案（`task.budget_exhausted`）、快照恢复带两字段。
- `main.py`：`GET /api/tasks/{task_id}`（新增，返回 `TaskDetail`：任务 + 预算态 + 证据缺口）；
  `PATCH /api/tasks/{id}` 透传两字段与"是否出现"。
- 前端：`/tasks` 详情弹窗新增「预算与证据要求（意图对象）」区块——三个数字输入（秒/次数/未强制的 token）、
  证据要求行（类型/条数/说明，可增删）、执行态一行（`已领取 1 / 3 次`）、证据缺口提示、保存按钮；
  `lib/api.ts` 加 `TaskBudget`/`EvidenceRequirement`/`TaskBudgetState`/`TaskEvidenceGap`/`TaskDetail` 与 `getTaskDetail`。
- 验证脚本 `scripts/deploy/_aip1d_verify.py`（新，跑完自清理）。

**范围说明**：编辑入口只放在 `/tasks` 详情（单一位置），工作区任务板只保留"推荐执行体"；避免两处表单各自演化。
`/tasks` 列表与交付页的缺口角标未做（展示面已由详情承担），列在"未做"里。

## 3 验收（全部实测）

| 项 | 结果 |
| --- | --- |
| 后端全量 | **497 项**（+12 `test_task_intent`），仅 2 项既有 LaTeX 环境失败 |
| Agent 套件 | 298 项（9 skipped）不变 |
| 前端 | `tsc --noEmit` 干净、`next build` 25 页、SSR 不变量 19×19×6 |
| 浏览器实机（本地） | `/tasks` 详情：字段回显（600 秒 / 2 次 / 5000 tokens）、执行态"已领取 1 / 2 次 · token 上限只记录不强制"、证据要求两行（运行记录 1、成果物 2）、缺口框、保存后提示"预算与证据要求已保存" |
| 线上发布 | `server_release.sh` 成功；`server_verify.sh` **22/22** |
| 线上功能（`_aip1d_verify.py`） | 四项全 PASS：租约 60s ≤ 60s 且续租不越界；`max_attempts=1` 第二次领取被拒 + 事件恰好一条；缺口 1 条 + 批准被拒 + 门禁 `FAILED` 且含 `task:evidence_requirements`；清理后 0 残留（任务/租赁/孤儿行） |
| 线上包内容 | 发布的 `tasks` chunk 含"预算与证据要求"与 `task-intent-gaps`；线上库 `AIP1%` 任务 0 条、孤儿租约 0、`task.budget_exhausted` 事件 0（验证痕迹已清干净） |

## 4 踩到的坑（都值得记住）

1. **`Path.write_text` 在 Windows 会把 `\n` 写成 `\r\n`**：我用 Python 脚本批量改源码（`store.py`/`main.py`/`contracts.py`/`api.ts` 等）之后，
   `pack-source.sh` 的 CRLF 防呆**拦下了发布**（`scripts/deploy/_aip1_verify.py` 带 CR）——注意它是 `SystemExit(1)`，
   **打包失败但我已经上传了上一次的包**，差点把第 1 批的代码当成第 2 批发布。教训：`pack-source.sh` 报 CRLF 时先转 LF 再重打，
   看到 "uploaded" 不等于 tarball 是新的。
2. **`event_outbox.event_id REFERENCES events(id)`**：验证脚本清理临时任务时先删 `events` 会 FK 失败——
   顺序必须是 租约/门禁 → `event_outbox` → `events` → 任务（脚本里已按此顺序并加了断言）。
3. **线上复核人不能随便挑**：`create_review` 要求复核人是项目成员且角色允许 `review.approve`；
   验证脚本改为从 `project_memberships` 里取 `owner`/`project_lead`。

## 5 未做

- token 预算的强制（需要执行体回报用量）；`/tasks` 列表与交付页的证据缺口角标；
  AIP 网关（对外讲 AIP 的翻译层）与"智能体描述"对外只读接口（`/.well-known/aip/agents`）——都在 `docs/AIP_COMPARISON.md` §6，
  属于"真要接入 AIP 才做"的范畴。