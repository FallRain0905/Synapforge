# 工作流定义 Schema（WORKFLOW_SCHEMA）

> 状态：**v1 定稿**（2026-09-30，§7 三问已由 A 回签；落库形态按 D4 冻结：
> **定义存 JSON 列**，校验靠服务端）。所有者：工作包 C。
> 设计依据：autogen"编排策略 = 数据 + 一个函数"（三种群聊只差 select_speaker →
> 我们把编排做成版本化数据而非硬编码）；deer-flow 确定性验收做门禁判定器。

## 1. 定位与硬约束

1. **模板 ≠ 结果。** 应用工作流生成的是任务/成果物骨架，页面必须标注
   「骨架 / 待执行 / 待审核 / 可交付」（规划 §6 阶段 3 验收）。
2. **版本不可变。** `workflow_versions` 一经发布只读；运行实例
   （`project_workflow_runs`）绑定 `workflow_version_id`，模板后续修改
   不改写历史运行的解释（规划 §5.4 硬要求）。
3. **内核无关。** 换工作流包不得要求重写 Task/Run/Artifact/Handoff/Review——
   本 schema 只描述"要做什么、谁做、门槛是什么"，执行一律落到平台既有对象。
4. **门禁条件必须可判定。** `gate_policy.spec` 使用
   `apps/api/app/acceptance.py` 的字面语法（五类），fail-closed 到 UNVERIFIED。

## 2. 顶层结构（示例：长文写作最小包）

```json
{
  "schema_version": 1,
  "workflow": {
    "key": "longform-writing",
    "name": "长文写作",
    "description": "大纲 → 初稿 → 修订 → 定稿",
    "inputs": [
      {"name": "topic", "required": true, "hint": "写作主题与受众"},
      {"name": "references", "required": false, "artifact_type": "document"}
    ]
  },
  "stages": [
    {"id": "outline", "title": "大纲", "goal": "确定结构与论点"},
    {"id": "draft",    "title": "初稿", "goal": "按大纲成文"},
    {"id": "revise",   "title": "修订", "goal": "按复核意见修订"},
    {"id": "final",    "title": "定稿", "goal": "排版与交付"}
  ],
  "role_bindings": [
    {
      "id": "writer",
      "role_name": "写作",
      "description": "负责成文",
      "capability_requirements": ["files.write"],
      "prompt_overrides": {"system": "你是长文作者……"}
    },
    {
      "id": "critic",
      "role_name": "复核",
      "description": "独立复核结构与事实",
      "capability_requirements": ["files.read"]
    }
  ],
  "handoff_contracts": [
    {
      "id": "outline-to-draft",
      "from_role": "writer",
      "to_role": "writer",
      "required_fields": ["outline_ready", "open_questions"],
      "artifact_refs": ["outline_doc"]
    }
  ],
  "gate_policies": [
    {
      "id": "outline-gate",
      "spec": [
        "file:outputs/outline.md non-empty",
        {"any": ["artifact:outline approved", "review:critic concluded"]}
      ],
      "on_block": "escalate_human"
    }
  ],
  "nodes": [
    {
      "id": "write_outline",
      "stage_id": "outline",
      "title": "写大纲",
      "goal": "产出结构化大纲",
      "depends_on": [],
      "mode": "auto",
      "role_binding": "writer",
      "prompt": {"task_template": "围绕 {{input.topic}} 写大纲"},
      "inputs": [],
      "outputs": [{"name": "outline_doc", "artifact_type": "document", "path": "outputs/outline.md"}],
      "gate_policy": "outline-gate",
      "budget": {"max_seconds": 3600, "max_attempts": 2, "max_tokens": 200000},
      "retry_policy": {"backoff_seconds": 30},
      "on_fail": "escalate_human",
      "human_intervention": "none"
    },
    {
      "id": "write_draft",
      "stage_id": "draft",
      "title": "写初稿",
      "goal": "按大纲成文",
      "depends_on": ["write_outline"],
      "mode": "auto",
      "role_binding": "writer",
      "inputs": [{"from_output": "outline_doc"}],
      "outputs": [{"name": "draft_doc", "artifact_type": "document", "path": "outputs/draft.md"}],
      "handoff_contract": "outline-to-draft",
      "human_intervention": "none"
    },
    {
      "id": "critique",
      "stage_id": "revise",
      "title": "独立复核",
      "depends_on": ["write_draft"],
      "mode": "auto",
      "role_binding": "critic",
      "inputs": [{"from_output": "draft_doc"}],
      "outputs": [{"name": "review_report", "artifact_type": "review_report"}],
      "human_intervention": "none"
    },
    {
      "id": "finalize",
      "stage_id": "final",
      "title": "定稿交付",
      "depends_on": ["critique"],
      "mode": "hybrid",
      "role_binding": "writer",
      "inputs": [{"from_output": "draft_doc"}, {"from_output": "review_report"}],
      "outputs": [{"name": "final_doc", "artifact_type": "document", "path": "outputs/final.md"}],
      "human_intervention": "approval_gate",
      "delivery_adapter": "docx_export"
    }
  ],
  "delivery_adapters": [
    {"id": "docx_export", "kind": "docx_export", "config": {}}
  ]
}
```

## 3. 字段规范

### 3.1 顶层

| 字段 | 必填 | 说明 |
|---|---|---|
| `schema_version` | ✓ | 本 schema 的版本，当前 `1` |
| `workflow` | ✓ | `key`（全局唯一、稳定不变）、`name`、`description`、`inputs[]` |
| `stages[]` | ✓ | 非空；阶段只是展示与归组，**不表达执行顺序**（顺序只看 `depends_on`） |
| `nodes[]` | ✓ | 非空；执行单元 |
| `role_bindings[]` | ✓ | 角色定义；数学建模包的 `roles/mm-*.md` 内容进 `prompt_overrides` |
| `handoff_contracts[]` | 可选 | 结构化交接的字段约定 |
| `gate_policies[]` | 可选 | 门禁；被节点引用 |
| `delivery_adapters[]` | 可选 | 交付适配器（论文编译/文档导出/提交包） |

### 3.2 node

| 字段 | 必填 | 约束 |
|---|---|---|
| `id` / `stage_id` / `title` / `goal` | ✓ | id 全工作流唯一 |
| `depends_on[]` | ✓（可为空数组） | 引用其他 node id；**必须无环** |
| `mode` | ✓ | `manual / hybrid / auto`（沿用平台三模式语义；auto=受约束派发，不承诺无人值守） |
| `role_binding` | **互斥校验** | `auto/hybrid` **必须有**；`manual` **必须没有**（见 §4 规则 6——互斥，不是可选缺省） |
| `prompt` | 可选 | `task_template` 支持 `{{input.<name>}}` 与 `{{input.from_output}}` 插值 |
| `inputs[]` | 可选 | `{from_output: <node 输出名>}` 或 `{input: <workflow.inputs.name>}` |
| `outputs[]` | ≥0 | `{name, artifact_type, path?}`；name 节点内唯一。**`path` 相对项目工作区根**（与 workspace_files API 同边界），禁止绝对路径与 `..`（探针越界一律 UNVERIFIED）；`outputs/` 是约定子目录，模板建议这么写但不是边界 |
| `gate_policy` / `retry_policy` / `handoff_contract` / `delivery_adapter` | 可选 | 引用对应定义 |
| `on_fail` | 默认 `escalate_human` | `retry` / `handoff:<role_binding_id>` / `escalate_human` |
| `human_intervention` | 默认 `none` | `none` / `before` / `after` / `approval_gate`（人工批准是一等介入点，对应 autogen `HandoffTermination` 语义） |

### 3.3 gate_policy

- `spec`：acceptance.py 的 GateSpec（字符串叶子 + `{"all": []}` / `{"any": []}` 组合）。
  每个字符串必须能被五类语法解析，否则模板校验失败。
- `on_block`：`blocked`（停下来等输入）或 `escalate_human`（升级人工，
  发 `project.gate.escalated` 事件）。

### 3.4 budget 与 retry_policy（任务治理，§7① 定稿）

- 节点预算字段 `budget: {max_seconds, max_attempts, max_tokens}`，
  **直接映射平台既有 `TaskBudget`**（contracts.py）：领取（claim）与心跳两处
  硬校验、超限拒领并写一次性告警事件；用量来自 `runs.usage` 回报。
- **不接 `llm_member_quotas`**：那是"渠道免费额度"线（按成员记账、代理对话
  扣减）；节点预算是任务治理语义。两套额度不许搅在一起。
- `retry_policy` 只保留重试节奏与失败处置：`backoff_seconds`（≥0）、
  节点级 `on_fail`（`retry` / `handoff:<role_binding_id>` / `escalate_human`）；
  `max_attempts` 统一放 `budget`（避免两处表达同一约束）。
- 重试不清洗失败现场：`project.task.failed` 事件已记录 `stop_reason`，
  重试是**新尝试**（`project.task.retried` 带尝试序号），不覆盖历史。

## 4. 服务端校验清单（W4.1 校验器按此实现）

1. `schema_version` 已知；顶层各段存在且非空（可选段除外）。
2. id 唯一性：node / stage / role_binding / gate_policy / handoff_contract / delivery_adapter。
3. 引用完整性：`depends_on`、`stage_id`、`role_binding`、`gate_policy`、
   `handoff_contract`、`delivery_adapter`、`inputs.from_output` 全部可解析。
4. DAG 无环 + 所有节点可达（从零入度节点出发）。
5. gate spec 逐条可被 acceptance 解析（调用 `evaluate_criterion` 做干跑：
   全部探针/查询传 None，不出现 `undecidable` 之外的异常）。
6. **role_binding 互斥校验（§7③ 定稿）**：`auto/hybrid` 节点**必须**有
   `role_binding`；`manual` 节点**必须没有**（人工节点路由到成员
   `assignee_member_id` 或公共派单队列，不设 required_capabilities）。
   这是互斥校验，不是可选缺省。`budget` 字段形状须匹配
   `TaskBudget`（`max_seconds / max_attempts / max_tokens`）。
7. 交付节点（无下游消费者且声明 `delivery_adapter`）至少一个输出。
8. 校验错误**逐条列出**（id + 原因），绝不静默丢弃——沿用
   `cumcm_importer` 的"未识别资产单列"纪律。

## 5. 与平台对象的映射（A 落地用）

| schema 概念 | 平台对象 | 事件（AGENT_EVENT_CONTRACT） |
|---|---|---|
| node 实例化 | task（depends_on → 任务依赖） | `project.task.created / claimed / failed / retried` |
| node 执行 | run | `project.run.started / finished / failed` |
| outputs | artifact（artifact_type, path） | `project.artifact.uploaded / approved / rejected` |
| handoff_contract 实例 | handoff | `project.handoff.sent / accepted / rejected` |
| gate_policy 评估 | acceptance.py → GateResult | `project.gate.evaluated / passed / blocked / escalated` |
| human_intervention | 现有 approvals 协议 | （审批事件沿用既有通道，不新增第二套审批） |
| 角色执行进度 | progress_ledger（C 第三期交付） | 推进器内部账本，不直接对外 |

## 6. CUMCM 映射对照（W3.4 预案）

`competition_workflow_adapter.py` 的 `SKILL_STAGE_MAP` → 本 schema 的
stage/outputs：`problem_analysis / modeling / coding / review / paper / delivery`
六个 stage；`comp-*` 技能 → role_bindings + prompt_overrides（来源
`deploy/cloud-agent/roles/mm-*.md`）；四问 DAG → nodes 的 depends_on。
现有 `cumcm_importer.py` 的骨架生成逻辑改为产出本 schema 的 JSON，
老入口保留不删。

**参考预案 fixture**：`workflow_examples/cumcm-four-questions.json`（四问
并行建模、手工收口节点、交付适配器的完整形状示例，已过 `validate_definition`
实测）——W3.4 权威导入物的形状对照物；规范程度见 `workflow_examples/README.md`。

## 7. 演进规则

- 本 schema 与事件契约同规：**只增不改**。新增可选字段允许；改字段语义、
  删字段、改校验语义 = 新 schema_version + 新 workflow key 或显式迁移。
- 模板内容修改 = 新 `workflow_version`；运行绑定不迁移。
- **定稿记录（2026-09-30，A 回签）**：
  ① `budget` 不接 `llm_member_quotas`（那是渠道免费额度线）；节点预算直接
  映射平台 `TaskBudget`（claim/心跳硬校验、超限拒领+一次性告警事件），
  用量来自 `runs.usage`——见 §3.4。
  ② `path` 相对根 = **项目工作区根**（与 workspace_files API 同边界、
  RLS 语义一致），不是 `outputs/`；`outputs/` 降级为约定子目录；
  探针禁绝对路径与 `..`，越界一律 UNVERIFIED（`PathGuardProbe` 已按此实现）。
  ③ `manual` 节点允许且必须无 `role_binding`（互斥校验，见 §4 规则 6）。
