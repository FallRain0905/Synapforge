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
    "list_project_workflow_runs",
]

KNOWN_SCHEMA_VERSION = 1
NODE_MODES = {"manual", "hybrid", "auto"}
BUDGET_KEYS = {"max_seconds", "max_attempts", "max_tokens"}
_KEY_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789-"


class WorkflowError(RuntimeError):
    """稳定错误族：定义非法 / 不存在 / 版本冲突。``errors`` 逐条列出（schema §4 规则 8）。"""

    def __init__(self, code: str, errors: list[str] | None = None) -> None:
        super().__init__(code if not errors else f"{code}:{' | '.join(errors[:3])}")
        self.code = code
        self.errors = errors or []


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def ensure_schema(store: Any) -> None:
    """dev SQLite 建表（幂等）；生产 PostgreSQL 走迁移 036（含严格 RLS）。"""

    if getattr(store, "_workflow_schema_ready", False):
        return
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
            updated_at TEXT NOT NULL
        );
        """
    )
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
                backoff = retry_policy.get("backoff_seconds")
                if "backoff_seconds" in retry_policy and (not isinstance(backoff, int) or isinstance(backoff, bool) or backoff < 0):
                    errors.append(f"node:{node_id}: retry_policy.backoff_seconds 必须 ≥0 整数")
                if "max_attempts" in retry_policy:
                    errors.append(f"node:{node_id}: max_attempts 统一放 budget（retry_policy 不再表达该约束，schema §3.4）")
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
        "INSERT INTO project_workflow_runs (id, project_id, organization_id, workflow_id, workflow_version_id, status, inputs, created_by, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?, ?, ?)",
        (run_id, str(project_id), organization_id, str(workflow_id), version_id, json.dumps(inputs or {}, ensure_ascii=False), actor, timestamp, timestamp),
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


def list_project_workflow_runs(store: Any, project_id: UUID) -> list[dict[str, Any]]:
    ensure_schema(store)
    rows = store.db.execute(
        "SELECT * FROM project_workflow_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 50", (str(project_id),)
    ).fetchall()
    return [
        {
            "run_id": str(row["id"]),
            "workflow_id": str(row["workflow_id"]),
            "workflow_version_id": str(row["workflow_version_id"]),
            "status": str(row["status"]),
            "inputs": json.loads(row["inputs"] or "{}"),
            "created_by": str(row["created_by"] or ""),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]
