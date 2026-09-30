# 工作流 API 形状（W3.7/W4.2 前端开工依据）

> 状态：v1（迁移 036/037 落库后的真实形状，与 `apps/api/app/workflow_service.py`、
> `workflow_engine.py` 逐字段一致；本文件是前端类型开发的权威，改形状先改服务层再改这里）
> 所有者：A（形状变更由 A 在本文件同步）
> 定义形状（definition JSON）权威：`docs/WORKFLOW_SCHEMA.md` v1（C 定稿，§7 三问已回签）

## 0. 路由一览

| 方法 | 路径 | 说明 | 成功码 |
|---|---|---|---|
| POST | `/api/workflows` | 创建工作流包（definition 全量校验） | 201 |
| POST | `/api/workflows/builtin` | 安装内置包（CUMCM 主线 + 长文写作；幂等） | 200 |
| GET | `/api/workflows` | 本组织的包列表（不含 definition） | 200 |
| GET | `/api/workflows/{workflow_id}` | 单包详情（含当前 definition） | 200 |
| POST | `/api/workflows/{workflow_id}/versions` | 追加新版本（旧版本只读） | 201 |
| POST | `/api/projects/{project_id}/workflow-runs` | 应用工作流（物化任务骨架） | 201 |
| GET | `/api/projects/{project_id}/workflow-runs` | 运行列表（最近 50） | 200 |
| GET | `/api/projects/{project_id}/workflow-runs/{run_id}` | 运行详情 | 200 |
| POST | `/api/projects/{project_id}/workflow-runs/{run_id}/advance` | 推进一轮 | 200 |

鉴权：member 会话（`Authorization: Bearer`）；项目侧路由先过 `project_or_404`（无权 → 403）。

## 1. 工作流包（workflows 视图）

```json
{
  "id": "uuid",
  "key": "cumcm-main",
  "name": "数学建模（CUMCM 主线）",
  "description": "…",
  "current_version_id": "uuid | null",
  "created_by": "member-id",
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601",
  "definition": { "…schema v1 全量定义，仅详情/创建/加版本响应携带…" }
}
```

`POST /api/workflows/builtin` 响应：`{"created": ["cumcm-main", "longform-writing"], "skipped": []}`（幂等）。

`definition` 形状 = `docs/WORKFLOW_SCHEMA.md` v1（workflow/stages/role_bindings/nodes/
gate_policies/handoff_contracts/delivery_adapters）。**前端不要从响应反推 definition 的
字段集**——编辑器（W4.2）的字段集以 schema §3 为准。

## 2. 运行（runs 视图与详情）

列表条目与详情共用 `_run_view`：

```json
{
  "run_id": "uuid",
  "project_id": "uuid",
  "workflow_id": "uuid",
  "workflow_version_id": "uuid",
  "status": "RUNNING | COMPLETED | STALLED",
  "inputs": {},
  "node_tasks": { "<node_id>": "<task_id>" },
  "created_by": "…",
  "created_at": "…",
  "updated_at": "…"
}
```

详情额外携带（前端刷新场景）：

```json
{
  "deliveries": { "<node_id>": { "kind": "paper_compile", "adapter": "paper_compile",
                                 "status": "created | not_compiled | failed | unknown",
                                 "detail": {…}, "error": "仅失败时" } },
  "attempts": { "<node_id>": 2 },
  "ledger": { "round": 3, "stall_count": 0, "needs_replan": false },
  "definition": { "…冻结版本的定义…" }
}
```

## 3. 应用工作流（start）响应

```json
{
  "run_id": "uuid",
  "workflow_id": "uuid",
  "workflow_version_id": "uuid",
  "workflow_key": "cumcm-main",
  "status": "RUNNING",
  "tasks": [ { "node_id": "problem_facts", "task_id": "uuid", "mode": "auto|manual|hybrid" } ],
  "note": "已生成任务骨架，待执行与审核；应用模板不等于得到结果"
}
```

## 4. 推进（advance）响应

```json
{
  "run_id": "uuid",
  "status": "RUNNING | COMPLETED | STALLED",
  "node_statuses": { "<node_id>": "READY | CLAIMED | RUNNING | WAITING_REVIEW | APPROVED | FAILED | …" },
  "gates": [
    { "node_id": "…", "task_id": "uuid", "gate_policy": "solve-gate",
      "verdict": "HOLDS | NOT_HOLDS | UNVERIFIED", "all_hold": false,
      "leaves": [ { "criterion": "artifact:result_table approved",
                    "family": "artifact_approved", "verdict": "NOT_HOLDS", "detail": "…" } ],
      "unchecked": [],
      "on_block": "blocked | escalate_human" }
  ],
  "retried": [ "<node_id>" ],
  "deliveries": [
    { "node_id": "compile", "kind": "paper_compile", "adapter": "paper_compile",
      "status": "created | not_compiled | failed", "detail": {…}, "error": "仅失败时" }
  ],
  "round": 3,
  "stall_count": 0,
  "needs_replan": false
}
```

注意：
- `gates` 只含**本轮评估过的**节点（带 gate_policy 且任务在 WAITING_REVIEW）；没评估就没有条目。
- `deliveries` 只含**本轮实际执行交付**的节点（节点 APPROVED 且未交付过；失败的节点
  会重复出现在后续轮次直到成功）。
- `artifact:* approved` 的判定平台**可判定**：查过没有 → `NOT_HOLDS`（不是 UNVERIFIED）；
  `tests_passed` v1 恒为 `UNVERIFIED`（显式口径）。

## 5. 错误形状

- 工作流错误（create/versions/runs）：`{"detail": {"code": "<code>", "errors": ["<id>: <原因>", …]}}`
  - 422 `workflow_definition_invalid`（errors 逐条列出——编辑器校验反馈直接渲染这个数组）
  - 409 `workflow_key_exists`
  - 404 `workflow_not_found` / `workflow_version_not_found`
  - 422 `workflow_nodes_unresolvable`
- 推进错误：`{"detail": "<code>"}`，404 `workflow_run_not_found` / `workflow_version_not_found`，
  409 `workflow_run_not_running`。

## 6. 页面状态映射建议（W3.7）

- 运行状态：RUNNING=推进中、COMPLETED=已完成、STALLED=已停滞（needs_replan，需要人看）。
- 节点任务状态 → 页面桶：执行中=CLAIMED/RUNNING、待领取=READY、受阻=BLOCKED/NEEDS_REVISION、
  待审核=WAITING_REVIEW、已完成=APPROVED、失败=FAILED（与 status-dictionary 词典对齐）。
- 「骨架 ≠ 结果」文案必须挂在工作流应用动作旁（规划 §7 红线）。
