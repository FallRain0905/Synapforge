"""通用垂直工作流包（W3.1，实施计划第五期）。

定义形状的权威是 ``docs/WORKFLOW_SCHEMA.md`` v1（工作包 C 定稿）；本模块负责：
- **服务端校验**（schema §4 八条清单，逐条列出错误，绝不静默丢弃）；
- **版本化存储**（定义存 JSON 列，版本一经发布只读——追加新版本，永不改写旧定义）；
- **运行物化**（节点 → 平台普通任务，depends_on → 任务依赖；Task/Run/Artifact/Handoff
  全部沿用既有对象，换工作流包不重写内核——schema §1 规则 3）。

门禁的**执行**（何时评估 gate、派发、续跑）是推进器（W3.2）的职责，不在这里；
本模块只做"定义合法"与"物化骨架"，并如实标注：应用模板生成的是骨架，不是结果。
"""

from __future__ import annotations

import json
import re
from typing import Any
from uuid import UUID, uuid4

from . import acceptance
from .contracts import TaskCreate

__all__ = [
    "WorkflowError",
    "ensure_schema",
    "validate_definition",
    "create_workflow",
    "add_workflow_version",
    "get_workflow",
    "list_workflows",
    "start_workflow_run",
    "get_workflow_run",
    "list_project_workflow_runs",
    "preview_definition",
    "preview_workflow_version",
    "draft_from_project",
]

KNOWN_SCHEMA_VERSION = 1
NODE_MODES = {"manual", "hybrid", "auto"}
BUDGET_KEYS = {"max_seconds", "max_attempts", "max_tokens"}
_KEY_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"
_TEMPLATE_VAR_RE = re.compile(r"\{\{input\.([A-Za-z0-9_-]+)\}\}")


class WorkflowError(RuntimeError):
    """稳定错误族：定义非法 / 不存在 / 版本冲突。``errors`` 逐条列出（schema §4 规则 8）。"""

    def __init__(self, code: str, errors: list[str] | None = None) -> None:
        super().__init__(code if not errors else f"{code}:{' | '.join(errors[:3])}")
        self.code = code
        self.errors = errors or []


def _enum(value: Any) -> str:
    return str(value.value if hasattr(value, "value") else value)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def ensure_schema(store: Any) -> None:
    """dev SQLite 建表（幂等）；生产 PostgreSQL 走迁移 036（含严格 RLS）。"""

    if getattr(store, "_workflow_schema_ready", False):
        return
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(project_workflow_runs)")}
    if columns and "node_tasks" not in columns:
        store.db.execute("ALTER TABLE project_workflow_runs ADD COLUMN node_tasks TEXT NOT NULL DEFAULT '{}'")
    if columns and "ledger" not in columns:
        store.db.execute("ALTER TABLE project_workflow_runs ADD COLUMN ledger TEXT NOT NULL DEFAULT '{}'")
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            key TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            current_version_id TEXT,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS workflows_key_idx ON workflows(key);
        CREATE TABLE IF NOT EXISTS workflow_versions (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL REFERENCES workflows(id),
            version INTEGER NOT NULL,
            definition TEXT NOT NULL,
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS workflow_versions_workflow_version_idx
            ON workflow_versions(workflow_id, version);
        CREATE TABLE IF NOT EXISTS project_workflow_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            organization_id TEXT NOT NULL,
            workflow_id TEXT NOT NULL,
            workflow_version_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'RUNNING',
            inputs TEXT NOT NULL DEFAULT '{}',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            node_tasks TEXT NOT NULL DEFAULT '{}',
            ledger TEXT NOT NULL DEFAULT '{}'
        );
        """
    )
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(project_workflow_runs)")}
    if columns and "node_tasks" not in columns:
        store.db.execute("ALTER TABLE project_workflow_runs ADD COLUMN node_tasks TEXT NOT NULL DEFAULT '{}'")
    if columns and "ledger" not in columns:
        store.db.execute("ALTER TABLE project_workflow_runs ADD COLUMN ledger TEXT NOT NULL DEFAULT '{}'")
    store.db.commit()
    store._workflow_schema_ready = True


# ---- 校验（schema §4 八条，逐条列出）----------------------------------------


def _walk_gate_spec(spec: Any, errors: list[str], where: str) -> None:
    """schema §4 规则 5：spec 逐条干跑 acceptance（全 None 探针），undecidable 可接受，
    任何异常 = 模板非法。顶层数组 = 隐含 all（schema §2 示例形态）；``{"all": []}`` /
    ``{"any": []}`` 递归。"""

    if isinstance(spec, list):
        if not spec:
            errors.append(f"{where}: gate spec 不能是空数组")
            return
        for child in spec:
            _walk_gate_spec(child, errors, where)
        return
    if isinstance(spec, str):
        try:
            acceptance.evaluate_criterion(spec)
        except Exception as error:  # noqa: BLE001 - 干跑不允许任何意外异常
            errors.append(f"{where}: gate 条件无法解析（{error.__class__.__name__}: {error}）")
        return
    if isinstance(spec, dict):
        if set(spec.keys()) != {"all"} and set(spec.keys()) != {"any"}:
            errors.append(f"{where}: gate 组合只支持 all/any 单键")
            return
        children = spec["all" if "all" in spec else "any"]
        if not isinstance(children, list) or not children:
            errors.append(f"{where}: gate 组合的子条件必须是非空数组")
            return
        for child in children:
            _walk_gate_spec(child, errors, where)
        return
    errors.append(f"{where}: gate 条件必须是字符串或 all/any 组合")


def validate_definition(definition: Any) -> list[str]:
    """schema §4 服务端校验清单。返回逐条错误（空列表 = 合法）。"""

    errors: list[str] = []
    if not isinstance(definition, dict):
        return ["definition: 必须是 JSON 对象"]
    if definition.get("schema_version") != KNOWN_SCHEMA_VERSION:
        errors.append(f"schema_version: 仅支持 {KNOWN_SCHEMA_VERSION}（收到 {definition.get('schema_version')!r}）")

    workflow = definition.get("workflow")
    if not isinstance(workflow, dict) or not str(workflow.get("key") or "").strip():
        errors.append("workflow: 缺 key")
    else:
        key = str(workflow["key"])
        if len(key) < 2 or len(key) > 64 or any(ch not in _KEY_CHARS for ch in key):
            errors.append(f"workflow.key: 只允许小写字母/数字/连字符（2-64 位），收到 {key!r}")
    if not isinstance(workflow, dict) or not str(workflow.get("name") or "").strip():
        errors.append("workflow: 缺 name")

    stages = definition.get("stages")
    nodes = definition.get("nodes")
    role_bindings = definition.get("role_bindings")
    gate_policies = definition.get("gate_policies") or []
    handoff_contracts = definition.get("handoff_contracts") or []
    delivery_adapters = definition.get("delivery_adapters") or []
    if not isinstance(stages, list) or not stages:
        errors.append("stages: 必须是非空数组")
    if not isinstance(nodes, list) or not nodes:
        errors.append("nodes: 必须是非空数组")
    if not isinstance(role_bindings, list) or not role_bindings:
        errors.append("role_bindings: 必须是非空数组")
    if errors:
        return errors

    stage_ids = {str(item.get("id") or "") for item in stages if isinstance(item, dict)}
    binding_ids = {str(item.get("id") or "") for item in role_bindings if isinstance(item, dict)}
    gate_ids = {str(item.get("id") or "") for item in gate_policies if isinstance(item, dict)}
    handoff_ids = {str(item.get("id") or "") for item in handoff_contracts if isinstance(item, dict)}
    adapter_ids = {str(item.get("id") or "") for item in delivery_adapters if isinstance(item, dict)}

    node_ids: list[str] = []
    output_names: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            errors.append("nodes: 存在非对象条目")
            continue
        node_id = str(node.get("id") or "")
        if not node_id:
            errors.append("nodes: 存在缺 id 的节点")
            continue
        node_ids.append(node_id)
        for output in node.get("outputs") or []:
            if isinstance(output, dict) and output.get("name"):
                output_names.add(str(output["name"]))
    duplicates = {nid for nid in node_ids if node_ids.count(nid) > 1}
    if duplicates:
        errors.append(f"nodes: id 重复 {sorted(duplicates)}")

    # 规则 2：各段 id 唯一
    def _check_unique(section: str, items: list) -> None:
        seen: list[str] = []
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                seen.append(str(item["id"]))
        dups = sorted({sid for sid in seen if seen.count(sid) > 1})
        if dups:
            errors.append(f"{section}: id 重复 {dups}")

    _check_unique("stages", stages)
    _check_unique("role_bindings", role_bindings)
    _check_unique("gate_policies", gate_policies)
    _check_unique("handoff_contracts", handoff_contracts)
    _check_unique("delivery_adapters", delivery_adapters)

    # 规则 3 + 6：节点引用完整性与 role_binding 互斥
    node_by_id = {str(node.get("id")): node for node in nodes if isinstance(node, dict) and node.get("id")}
    downstream_ids: set[str] = set()
    for node_id, node in node_by_id.items():
        stage_id = str(node.get("stage_id") or "")
        if stage_id not in stage_ids:
            errors.append(f"node:{node_id}: stage_id {stage_id!r} 无法解析")
        mode = str(node.get("mode") or "")
        if mode not in NODE_MODES:
            errors.append(f"node:{node_id}: mode 必须是 manual/hybrid/auto（收到 {mode!r}）")
        binding = node.get("role_binding")
        if mode in {"auto", "hybrid"} and not binding:
            errors.append(f"node:{node_id}: auto/hybrid 节点必须有 role_binding（互斥校验，schema §4 规则 6）")
        if mode == "manual" and binding:
            errors.append(f"node:{node_id}: manual 节点必须没有 role_binding（人工节点路由到成员）")
        if binding and str(binding) not in binding_ids:
            errors.append(f"node:{node_id}: role_binding {binding!r} 无法解析")
        depends_on = node.get("depends_on")
        if not isinstance(depends_on, list):
            errors.append(f"node:{node_id}: depends_on 必须是数组（可为空）")
            depends_on = []
        for dep in depends_on:
            dep_id = str(dep)
            if dep_id not in node_by_id:
                errors.append(f"node:{node_id}: depends_on {dep_id!r} 无法解析")
            elif dep_id == node_id:
                errors.append(f"node:{node_id}: depends_on 引用了自己")
            else:
                downstream_ids.add(dep_id)
        gate_ref = node.get("gate_policy")
        if gate_ref and str(gate_ref) not in gate_ids:
            errors.append(f"node:{node_id}: gate_policy {gate_ref!r} 无法解析")
        handoff_ref = node.get("handoff_contract")
        if handoff_ref and str(handoff_ref) not in handoff_ids:
            errors.append(f"node:{node_id}: handoff_contract {handoff_ref!r} 无法解析")
        adapter_ref = node.get("delivery_adapter")
        if adapter_ref and str(adapter_ref) not in adapter_ids:
            errors.append(f"node:{node_id}: delivery_adapter {adapter_ref!r} 无法解析")
        budget = node.get("budget")
        if budget is not None:
            if not isinstance(budget, dict) or set(budget.keys()) - BUDGET_KEYS or not budget:
                errors.append(f"node:{node_id}: budget 只允许 {sorted(BUDGET_KEYS)} 且至少一项")
            elif any(not isinstance(value, int) or isinstance(value, bool) or value < 1 for value in budget.values()):
                errors.append(f"node:{node_id}: budget 取值必须是 ≥1 的整数")
        retry_policy = node.get("retry_policy")
        if retry_policy is not None:
            if not isinstance(retry_policy, dict):
                errors.append(f"node:{node_id}: retry_policy 必须是对象")
            else:
                # 封闭键集（schema §3.4 定稿 48bba40）：只允许 backoff_seconds；
                # on_fail 是**节点级**字段，写进 retry_policy 属位置错误，逐键点名。
                unknown = sorted(set(retry_policy.keys()) - {"backoff_seconds"})
                if unknown:
                    errors.append(f"node:{node_id}: retry_policy 只允许 backoff_seconds（多余键 {unknown}；on_fail 是节点级字段，max_attempts 归 budget）")
                backoff = retry_policy.get("backoff_seconds")
                if "backoff_seconds" in retry_policy and (not isinstance(backoff, int) or isinstance(backoff, bool) or backoff < 0):
                    errors.append(f"node:{node_id}: retry_policy.backoff_seconds 必须 ≥0 整数")
        for item in node.get("outputs") or []:
            path = item.get("path") if isinstance(item, dict) else None
            if path is not None and (str(path).startswith("/") or str(path).startswith("\\") or ".." in str(path).replace("\\", "/").split("/")):
                errors.append(f"node:{node_id}: 输出 path {path!r} 必须是工作区根相对路径（禁止绝对路径与 ..）")
        for item in node.get("inputs") or []:
            if isinstance(item, dict):
                ref = item.get("from_output")
                if ref and str(ref) not in output_names:
                    errors.append(f"node:{node_id}: inputs.from_output {ref!r} 无法解析")
                if not ref and not item.get("input"):
                    errors.append(f"node:{node_id}: inputs 条目缺 from_output/input")
        # 规则 7：交付节点（声明 delivery_adapter 且无下游消费者）至少一个输出
        if adapter_ref and node_id not in downstream_ids and not (node.get("outputs") or []):
            errors.append(f"node:{node_id}: 交付节点必须至少声明一个输出")

    # 规则 4：DAG 无环 + 全部可达（Kahn）
    indegree = {nid: 0 for nid in node_by_id}
    edges: dict[str, list[str]] = {nid: [] for nid in node_by_id}
    for node_id, node in node_by_id.items():
        for dep in node.get("depends_on") or []:
            dep_id = str(dep)
            if dep_id in node_by_id and dep_id != node_id:
                edges[dep_id].append(node_id)
                indegree[node_id] += 1
    frontier = [nid for nid, degree in indegree.items() if degree == 0]
    visited: set[str] = set()
    while frontier:
        current = frontier.pop()
        visited.add(current)
        for nxt in edges[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                frontier.append(nxt)
    if len(visited) != len(node_by_id):
        cycle_members = sorted(set(node_by_id) - visited)
        errors.append(f"nodes: 依赖存在环或不可达（涉及 {cycle_members[:8]}）")

    # 规则 5：gate spec 干跑
    for policy in gate_policies:
        if isinstance(policy, dict) and policy.get("id"):
            _walk_gate_spec(policy.get("spec"), errors, f"gate_policy:{policy['id']}")

    return errors


# ---- 版本化存储 -------------------------------------------------------------


def _workflow_view(store: Any, row: Any, *, with_definition: bool = False) -> dict[str, Any]:
    view = {
        "id": str(row["id"]),
        "key": str(row["key"]),
        "name": str(row["name"]),
        "description": str(row["description"] or ""),
        "current_version_id": str(row["current_version_id"]) if row["current_version_id"] else None,
        "created_by": str(row["created_by"] or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }
    if with_definition and row["current_version_id"]:
        version_row = store.db.execute(
            "SELECT definition FROM workflow_versions WHERE id = ?", (str(row["current_version_id"]),)
        ).fetchone()
        view["definition"] = json.loads(version_row["definition"]) if version_row else None
    return view


def create_workflow(store: Any, organization_id: str, actor: str, definition: dict[str, Any]) -> dict[str, Any]:
    ensure_schema(store)
    organization_id, actor = str(organization_id), str(actor)  # 路由可能传 UUID 对象
    errors = validate_definition(definition)
    if errors:
        raise WorkflowError("workflow_definition_invalid", errors)
    workflow = definition["workflow"]
    key = str(workflow["key"])
    if store.db.execute("SELECT 1 FROM workflows WHERE key = ?", (key,)).fetchone():
        raise WorkflowError("workflow_key_exists", [f"workflow.key: {key!r} 已存在"])
    workflow_id = str(uuid4())
    version_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO workflows (id, organization_id, key, name, description, current_version_id, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (workflow_id, organization_id, key, str(workflow["name"]), str(workflow.get("description") or ""), version_id, actor, timestamp, timestamp),
    )
    store.db.execute(
        "INSERT INTO workflow_versions (id, workflow_id, version, definition, created_by, created_at) VALUES (?, ?, 1, ?, ?, ?)",
        (version_id, workflow_id, json.dumps(definition, ensure_ascii=False), actor, timestamp),
    )
    store.db.commit()
    row = store.db.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
    return _workflow_view(store, row, with_definition=True)


def add_workflow_version(store: Any, workflow_id: UUID, actor: str, definition: dict[str, Any]) -> dict[str, Any]:
    """追加新版本（旧版本只读，永不改写——schema §1 规则 2）。"""

    ensure_schema(store)
    errors = validate_definition(definition)
    if errors:
        raise WorkflowError("workflow_definition_invalid", errors)
    row = store.db.execute("SELECT * FROM workflows WHERE id = ?", (str(workflow_id),)).fetchone()
    if row is None:
        raise WorkflowError("workflow_not_found")
    next_version = int(
        store.db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM workflow_versions WHERE workflow_id = ?", (str(workflow_id),)).fetchone()[0]
    )
    version_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO workflow_versions (id, workflow_id, version, definition, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (version_id, str(workflow_id), next_version, json.dumps(definition, ensure_ascii=False), actor, timestamp),
    )
    # 展示名/描述跟随**当前已发布版本**（版本定义只追加、不更新；行上的字段是"当前版本"的投影）
    store.db.execute(
        "UPDATE workflows SET name = ?, description = ?, current_version_id = ?, updated_at = ? WHERE id = ?",
        (
            str(definition.get("workflow", {}).get("name") or row["name"]),
            str(definition.get("workflow", {}).get("description") or row["description"] or ""),
            version_id,
            timestamp,
            str(workflow_id),
        ),
    )
    store.db.commit()
    updated = store.db.execute("SELECT * FROM workflows WHERE id = ?", (str(workflow_id),)).fetchone()
    return _workflow_view(store, updated, with_definition=True)


def get_workflow(store: Any, workflow_id: UUID, organization_id: str) -> dict[str, Any]:
    ensure_schema(store)
    row = store.db.execute(
        "SELECT * FROM workflows WHERE id = ? AND organization_id = ?", (str(workflow_id), organization_id)
    ).fetchone()
    if row is None:
        raise WorkflowError("workflow_not_found")
    return _workflow_view(store, row, with_definition=True)


def list_workflows(store: Any, organization_id: str) -> list[dict[str, Any]]:
    ensure_schema(store)
    rows = store.db.execute(
        "SELECT * FROM workflows WHERE organization_id = ? ORDER BY updated_at DESC", (organization_id,)
    ).fetchall()
    return [_workflow_view(store, row) for row in rows]


# ---- 运行物化（节点 → 任务骨架）---------------------------------------------


def start_workflow_run(
    store: Any,
    project_id: UUID,
    organization_id: str,
    actor: str,
    workflow_id: UUID,
    inputs: dict[str, Any],
    version_id: UUID | None = None,
) -> dict[str, Any]:
    """绑定冻结版本并物化节点为任务骨架。**生成的是骨架，不是结果**（schema §1 规则 1）。"""

    ensure_schema(store)
    organization_id, actor = str(organization_id), str(actor)  # 路由可能传 UUID 对象
    project_id = project_id if isinstance(project_id, UUID) else UUID(str(project_id))
    workflow_row = store.db.execute(
        "SELECT * FROM workflows WHERE id = ? AND organization_id = ?", (str(workflow_id), organization_id)
    ).fetchone()
    if workflow_row is None:
        raise WorkflowError("workflow_not_found")
    version_id = str(version_id) if version_id else str(workflow_row["current_version_id"])
    version_row = store.db.execute(
        "SELECT * FROM workflow_versions WHERE id = ? AND workflow_id = ?", (version_id, str(workflow_id))
    ).fetchone()
    if version_row is None:
        raise WorkflowError("workflow_version_not_found")
    definition = json.loads(version_row["definition"])

    run_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO project_workflow_runs (id, project_id, organization_id, workflow_id, workflow_version_id, status, inputs, node_tasks, ledger, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?, '{}', ?, ?, ?)",
        (run_id, str(project_id), organization_id, str(workflow_id), version_id, json.dumps(inputs or {}, ensure_ascii=False), json.dumps({}), actor, timestamp, timestamp),
    )

    binding_by_id = {str(b.get("id")): b for b in definition.get("role_bindings", []) if isinstance(b, dict) and b.get("id")}
    node_tasks: dict[str, str] = {}
    created: list[dict[str, Any]] = []
    # 拓扑序物化（依赖的任务 id 先生成才能挂 dependency_task_ids）
    pending = {str(n["id"]): n for n in definition.get("nodes", []) if isinstance(n, dict) and n.get("id")}
    while pending:
        progressed = False
        for node_id in sorted(list(pending.keys())):
            node = pending[node_id]
            deps = [str(d) for d in (node.get("depends_on") or [])]
            if any(dep in pending for dep in deps):
                continue
            binding = binding_by_id.get(str(node.get("role_binding") or ""), {})
            template = str((node.get("prompt") or {}).get("task_template") or node.get("goal") or "")
            for name, value in (inputs or {}).items():
                template = template.replace("{{input." + str(name) + "}}", str(value))
            task = store.create_task(
                project_id,
                TaskCreate(
                    title=str(node.get("title") or node_id),
                    description=template,
                    stage=str(node.get("stage_id") or ""),
                    dependency_task_ids=[node_tasks[dep] for dep in deps if dep in node_tasks],
                    required_capabilities=[str(item) for item in (binding.get("capability_requirements") or [])],
                    budget=node.get("budget"),
                ),
                actor=actor,
            )
            node_tasks[node_id] = str(task.id)
            created.append({"node_id": node_id, "task_id": str(task.id), "mode": str(node.get("mode") or "")})
            pending.pop(node_id)
            progressed = True
        if not progressed:
            # validate_definition 已保证无环；这里是双保险
            raise WorkflowError("workflow_nodes_unresolvable", [f"nodes: {sorted(pending)}"])
    store.db.execute(
        "UPDATE project_workflow_runs SET node_tasks = ?, updated_at = ? WHERE id = ?",
        (json.dumps(node_tasks, ensure_ascii=False), _now(), run_id),
    )
    store.db.commit()
    return {
        "run_id": run_id,
        "workflow_id": str(workflow_id),
        "workflow_version_id": version_id,
        "workflow_key": str(workflow_row["key"]),
        "status": "RUNNING",
        "tasks": created,
        # 诚实标注（schema §1 规则 1）：这是任务/成果物骨架
        "note": "已生成任务骨架，待执行与审核；应用模板不等于得到结果",
    }


def _run_view(row: Any) -> dict[str, Any]:
    return {
        "run_id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "workflow_id": str(row["workflow_id"]),
        "workflow_version_id": str(row["workflow_version_id"]),
        "status": str(row["status"]),  # RUNNING / COMPLETED / STALLED
        "inputs": json.loads(row["inputs"] or "{}"),
        # node_id → task_id（引擎的状态权威）：前端把任务状态映射回节点靠它
        "node_tasks": json.loads(row["node_tasks"] or "{}"),
        "created_by": str(row["created_by"] or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def get_workflow_run(store: Any, project_id: UUID, run_id: UUID) -> dict[str, Any]:
    """运行详情（含 node_tasks 映射与账本快照：gates/deliveries 的持久结果）。"""

    ensure_schema(store)
    row = store.db.execute(
        "SELECT * FROM project_workflow_runs WHERE id = ? AND project_id = ?", (str(run_id), str(project_id))
    ).fetchone()
    if row is None:
        raise WorkflowError("workflow_run_not_found")
    view = _run_view(row)
    state = json.loads(row["ledger"] or "{}") or {}
    view["deliveries"] = state.get("deliveries", {})
    view["attempts"] = state.get("attempts", {})
    # 逐节点任务状态（B 期五回签：详情页刷新就能画状态桶，不必等一次 advance）
    node_statuses: dict[str, str] = {}
    for node_id, task_id in view["node_tasks"].items():
        task_row = store.db.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if task_row is not None:
            status = task_row["status"]
            node_statuses[node_id] = str(status.value if hasattr(status, "value") else status)
    view["node_statuses"] = node_statuses
    ledger = state.get("ledger") or {}
    view["ledger"] = {
        "round": ledger.get("round", 0),
        "stall_count": ledger.get("stall_count", 0),
        "needs_replan": ledger.get("needs_replan", False),
    }
    version_row = store.db.execute(
        "SELECT definition FROM workflow_versions WHERE id = ?", (row["workflow_version_id"],)
    ).fetchone()
    view["definition"] = json.loads(version_row["definition"]) if version_row else None
    return view


def list_project_workflow_runs(store: Any, project_id: UUID) -> list[dict[str, Any]]:
    ensure_schema(store)
    rows = store.db.execute(
        "SELECT * FROM project_workflow_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 50", (str(project_id),)
    ).fetchall()
    return [_run_view(row) for row in rows]


# ---- W4.3 试运行（dry-run 预览：只展开任务图，不创建对象、不派发）-------------


def _interpolate(template: str, inputs: dict[str, Any]) -> str:
    result = str(template or "")
    for name, value in (inputs or {}).items():
        result = result.replace("{{input." + str(name) + "}}", str(value))
    return result


def preview_definition(definition: Any, inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """试运行（W4.3）：展开任务图给用户看"应用之后会有什么"，**不创建任何对象**。

    与 start_workflow_run 的区别只有一个：不写库、不建任务、不进运行列表。
    warnings 收集"运行期才会变成问题"的事（输入缺失、人工节点、v1 判定边界），
    防"误以为已得到结果"（规划 §7：应用模板 ≠ 得到结果）。
    """

    inputs = inputs or {}
    errors = validate_definition(definition)
    warnings: list[str] = []
    if errors:
        return {"valid": False, "errors": errors, "warnings": warnings, "plan": None}

    nodes = definition.get("nodes", [])
    provided = set(str(k) for k in inputs.keys())
    plan_nodes: list[dict[str, Any]] = []
    edges: list[list[str]] = []
    manual_count = 0
    tests_gates = 0
    for node in nodes:
        node_id = str(node.get("id"))
        template = str((node.get("prompt") or {}).get("task_template") or node.get("goal") or "")
        resolved = _interpolate(template, inputs)
        leftover = sorted({match for match in _TEMPLATE_VAR_RE.findall(resolved)})
        if leftover:
            warnings.append(f"node:{node_id}: 模板引用了未提供的输入 {leftover}（运行时将保留占位符原样）")
        mode = str(node.get("mode") or "")
        if mode == "manual":
            manual_count += 1
        for dep in node.get("depends_on") or []:
            edges.append([str(dep), node_id])
        budget = node.get("budget") or {}
        policy_id = node.get("gate_policy")
        policy = next((p for p in definition.get("gate_policies", []) if isinstance(p, dict) and str(p.get("id")) == str(policy_id)), None)
        gate_spec = (policy or {}).get("spec")
        for leaf in _flat_leaves(gate_spec):
            if str(leaf).lower().startswith("tests_passed:"):
                tests_gates += 1
        plan_nodes.append(
            {
                "node_id": node_id,
                "title": str(node.get("title") or node_id),
                "stage_id": str(node.get("stage_id") or ""),
                "mode": mode,
                "role_binding": node.get("role_binding"),
                "resolved_prompt": resolved,
                "depends_on": [str(dep) for dep in (node.get("depends_on") or [])],
                "outputs": [
                    {"name": out.get("name"), "artifact_type": out.get("artifact_type"), "path": out.get("path")}
                    for out in (node.get("outputs") or [])
                    if isinstance(out, dict)
                ],
                "gate_policy": policy_id,
                "budget": budget,
                "on_fail": str(node.get("on_fail") or "escalate_human"),
                "human_intervention": str(node.get("human_intervention") or "none"),
                "delivery_adapter": node.get("delivery_adapter"),
                "requires_human": mode == "manual" or str(node.get("human_intervention") or "none") != "none",
            }
        )
    if manual_count:
        warnings.append(f"{manual_count} 个 manual 节点需要人工处理（路由到成员或公共派单队列）")
    if tests_gates:
        warnings.append(f"{tests_gates} 个 tests_passed 门禁条件 v1 恒 UNVERIFIED（平台没有测试输出记录）")
    adapters = [str(n.get("delivery_adapter")) for n in nodes if n.get("delivery_adapter")]
    if adapters:
        warnings.append(f"交付适配器 {sorted(set(adapters))} 在节点批准后执行，失败会如实记账不阻塞完成")
    return {
        "valid": True,
        "errors": [],
        "warnings": warnings,
        "plan": {
            "workflow": {
                "key": str(definition.get("workflow", {}).get("key") or ""),
                "name": str(definition.get("workflow", {}).get("name") or ""),
            },
            "inputs_provided": sorted(provided),
            "nodes": plan_nodes,
            "edges": edges,
        },
    }


def _flat_leaves(spec: Any) -> list[str]:
    if isinstance(spec, list):
        leaves: list[str] = []
        for child in spec:
            leaves.extend(_flat_leaves(child))
        return leaves
    if isinstance(spec, dict):
        for key in ("all", "any"):
            if key in spec:
                return _flat_leaves(spec[key])
        return []
    return [str(spec)]


def preview_workflow_version(
    store: Any, organization_id: str, workflow_id: UUID, inputs: dict[str, Any] | None, version_id: UUID | None = None
) -> dict[str, Any]:
    """按已发布版本试运行（org 隔离与 start 同口径）。"""

    ensure_schema(store)
    organization_id = str(organization_id)
    workflow_row = store.db.execute(
        "SELECT * FROM workflows WHERE id = ? AND organization_id = ?", (str(workflow_id), organization_id)
    ).fetchone()
    if workflow_row is None:
        raise WorkflowError("workflow_not_found")
    version_id = str(version_id) if version_id else str(workflow_row["current_version_id"])
    version_row = store.db.execute(
        "SELECT * FROM workflow_versions WHERE id = ? AND workflow_id = ?", (version_id, str(workflow_id))
    ).fetchone()
    if version_row is None:
        raise WorkflowError("workflow_version_not_found")
    result = preview_definition(json.loads(version_row["definition"]), inputs)
    result["workflow_id"] = str(workflow_id)
    result["workflow_version_id"] = version_id
    return result


# ---- W4.4 反向保存（从成功项目抽 workflow 定义草稿）---------------------------


def draft_from_project(store: Any, project_id: UUID) -> dict[str, Any]:
    """从项目的真实任务图抽 workflow 定义**草稿**（W4.4）。

    草稿是待补全的起点，不是可直接发布的模板：gate_policy 按历史批准情况**建议**、
    handoff_contract/delivery_adapter 历史数据里不存在留空、key/name 给建议值——
    都要经编辑器人工确认。取消的任务不进草稿；历史批准的产物类型 → 建议门禁。
    """

    ensure_schema(store)
    project = store.get_project(project_id)
    tasks = [t for t in store.list_tasks(project_id) if _enum(t.status) != "CANCELLED"]
    if not tasks:
        raise WorkflowError("workflow_draft_empty", ["project: 没有可抽取的任务（取消的任务不计入）"])

    task_ids = {str(t.id) for t in tasks}
    agents = {
        str(row["agent_id"]): str(row["display_name"] or row["agent_id"])
        for row in store.db.execute("SELECT agent_id, display_name FROM agents")
    }
    role_caps: dict[str, set[str]] = {}
    role_for_task: dict[str, str] = {}
    for t in tasks:
        assignee = str(getattr(t, "assignee", "") or "")
        if assignee and assignee != "Unassigned" and assignee in agents:
            role_for_task[str(t.id)] = assignee
            role_caps.setdefault(assignee, set()).update(str(c) for c in (t.required_capabilities or []))

    role_bindings = [
        {
            "id": rid,
            "role_name": agents[rid],
            "capability_requirements": sorted(caps),
            "prompt_overrides": {"system": f"沿用 {agents[rid]} 在原项目中的执行方式（草稿，请人工校对）"},
        }
        for rid, caps in sorted(role_caps.items())
    ]

    gate_policies: list[dict[str, Any]] = []
    stage_order: list[str] = []
    nodes: list[dict[str, Any]] = []
    for t in tasks:
        tid = str(t.id)
        node_id = "task-" + tid[:8]
        stage = str(t.stage or "delivery")
        if stage not in stage_order:
            stage_order.append(stage)
        depends = ["task-" + str(d)[:8] for d in (t.dependency_task_ids or []) if str(d) in task_ids]
        node: dict[str, Any] = {
            "id": node_id,
            "stage_id": stage,
            "title": t.title,
            "goal": (t.description or t.title)[:200],
            "depends_on": depends,
            "mode": "auto" if role_for_task.get(tid) else "manual",
        }
        if role_for_task.get(tid):
            node["role_binding"] = role_for_task[tid]
        outs: list[dict[str, Any]] = []
        for row in store.db.execute(
            "SELECT artifact_type, COUNT(*) AS c FROM artifacts WHERE task_id = ? GROUP BY artifact_type ORDER BY artifact_type",
            (tid,),
        ):
            artifact_type = str(row["artifact_type"])
            outs.append({"name": f"{artifact_type}_{row['c']}_{len(outs) + 1}", "artifact_type": artifact_type})
        if outs:
            node["outputs"] = outs
            if _enum(t.status) == "APPROVED":
                gate_id = f"gate-{node_id}"
                gate_policies.append(
                    {"id": gate_id, "spec": [f"artifact:{outs[0]['artifact_type']} approved"], "on_block": "escalate_human"}
                )
                node["gate_policy"] = gate_id
        nodes.append(node)

    definition = {
        "schema_version": 1,
        "workflow": {
            "key": f"from-project-{str(project_id)[:8]}",
            "name": f"{project.name}（草稿）",
            "description": "从成功项目反向抽取的草稿（W4.4）：门禁为历史批准建议，交接与交付需人工补全",
            "inputs": [],
        },
        "stages": [{"id": stage, "title": stage} for stage in stage_order],
        "role_bindings": role_bindings,
        "gate_policies": gate_policies,
        "nodes": nodes,
    }
    validation = validate_definition(definition)
    return {
        "definition": definition,
        "validation_errors": validation,
        "task_count": len(tasks),
        "note": "草稿需在编辑器补全（交接/交付/输入插值）后经 /api/workflows 发布；发布前不会影响任何运行",
    }
