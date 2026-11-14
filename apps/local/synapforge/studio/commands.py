"""Studio 的业务层：集群、角色、运行、指令、复核。

所有写操作最终落在既有的 `synapforge.commands` 与 `Store` 上；studio 只增加
"集群/角色/运行"这层组织，不复制任务/租约/事件语义。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from .. import commands as base
from . import store as studio_store

DEFAULT_POLICY: dict[str, Any] = {
    "max_wall_minutes": 480,
    "max_rounds": 200,
    "max_invocations_per_role": 30,
    "role_timeout_seconds": 1800,
    "max_parallel_workers": 3,
    "auto_approve": True,
    "human_review": False,
    "allow_bash": False,
}

MAX_ROLE_NAME = 60


class StudioError(RuntimeError):
    """带稳定 code 的 studio 错误；细节进 str()，方便 UI/脚本显示。"""

    def __init__(self, code: str, errors: list[str] | None = None) -> None:
        self.code = code
        self.errors = list(errors or [])
        detail = "; ".join(self.errors)
        super().__init__(f"{code}: {detail}" if detail else code)


def _dump(obj: Any) -> Any:
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj


def _load(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        import json

        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _slug(text: str, fallback: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip()).strip("-").lower()
    return (raw or fallback)[:40]


def _event(store: Any, project_id: str, event_type: str, actor: str,
           payload: dict[str, Any]) -> None:
    try:
        store.add_event(UUID(str(project_id)), event_type, actor, payload, actor_kind="agent")
    except Exception:  # noqa: BLE001 — 事件失败不该让控制操作失败
        pass


def _role_specs(lead: dict[str, Any], roles: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    specs = [dict(lead or {})] + [dict(r) for r in (roles or [])]
    if len(specs) < 2:
        raise StudioError("cluster_needs_workers", ["至少需要一个 lead 和一个 worker"])
    seen: set[str] = set()
    for index, spec in enumerate(specs):
        reused = str(spec.get("agent_id") or "").strip()
        name = str(spec.get("name") or "").strip()
        if reused and not name:
            # 复用已有 Agent：名字留空时用 Agent 名，稍后解析
            spec["name"] = ""
        if not reused and not name:
            raise StudioError("role_name_required", [f"第 {index + 1} 个角色缺少名称"])
        if name and len(name) > MAX_ROLE_NAME:
            raise StudioError("role_name_too_long", [f"{name}: 名称不能超过 {MAX_ROLE_NAME} 字"])
        if name:
            if name in seen:
                raise StudioError("role_name_duplicate", [f"{name}: 角色名重复"])
            seen.add(name)
        backend = str(spec.get("backend") or "claude").strip().lower()
        if backend not in studio_store.ROLE_BACKENDS:
            raise StudioError("role_backend_invalid", [f"{name or reused}: 只支持 claude / command"])
        if backend == "command":
            args = spec.get("args")
            template = spec.get("command") or spec.get("template")
            if not args and not template:
                raise StudioError("role_command_required",
                                  [f"{name}: command 执行体必须给命令模板"])
        spec["name"] = name
        spec["backend"] = backend
    return specs


def _apply_agent(spec: dict[str, Any], agent: dict[str, Any]) -> dict[str, Any]:
    """把已有 Agent 的配置并进角色 spec；spec 里显式给的值优先。

    Agent 是"这个角色是谁"的模板，集群额外只保留「本轮职责」。
    """

    if not str(spec.get("name") or "").strip():
        spec["name"] = str(agent["name"])
    spec.setdefault("backend", agent.get("backend") or "claude")
    spec["backend"] = agent.get("backend") or spec["backend"]
    if not str(spec.get("model") or "").strip():
        spec["model"] = str(agent.get("model") or "")
    role_prompt = str(spec.get("role_prompt") or "").strip()
    base_prompt = str(agent.get("system_prompt") or "").strip()
    if role_prompt:
        spec["system_prompt"] = (base_prompt + "\n\n本轮职责：" + role_prompt).strip() \
            if base_prompt else "本轮职责：" + role_prompt
    else:
        spec["system_prompt"] = base_prompt
    spec["local_agent_id"] = str(agent["id"])
    spec["role_prompt"] = role_prompt
    spec.setdefault("provider_id", agent.get("provider_id") or "")
    spec["provider_id"] = agent.get("provider_id") or ""
    spec["allow_bash"] = bool(agent.get("allow_bash"))
    spec["allow_web"] = bool(agent.get("allow_web", True))
    spec["skills"] = list(agent.get("skills") or [])
    spec["mcp"] = list(agent.get("mcp") or [])
    spec["mcp_managed"] = bool(agent.get("mcp_managed"))
    spec["plugins"] = list(agent.get("plugins") or [])
    if spec.get("command") is None and agent.get("command"):
        spec["command"] = agent.get("command")
    if spec.get("args") is None and agent.get("command"):
        spec["args"] = [agent["command"]]
    budget = dict(spec.get("budget") or {})
    budget.setdefault("allow_bash", bool(agent.get("allow_bash")))
    spec["budget"] = budget
    return spec


def _resolve_spec_agents(store: Any, specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 spec 里的 `agent_id`（local_agents.id）解析成 Agent 并合并。"""

    resolved: list[dict[str, Any]] = []
    for spec in specs:
        reused = str(spec.get("agent_id") or "").strip()
        if not reused:
            resolved.append(spec)
            continue
        agent = studio_store.get_agent(store, reused)
        if agent is None:
            raise StudioError("role_agent_not_found", [f"{reused}: Agent 不存在"])
        resolved.append(_apply_agent(spec, agent))
    seen: set[str] = set()
    for index, spec in enumerate(resolved):
        name = str(spec.get("name") or "").strip()
        if not name:
            raise StudioError("role_name_required", [f"第 {index + 1} 个角色缺少名称"])
        if name in seen:
            raise StudioError("role_name_duplicate", [f"{name}: 角色名重复"])
        seen.add(name)
        spec["name"] = name
    return resolved


def _normalise_args(spec: dict[str, Any]) -> list[str]:
    raw = spec.get("args")
    if raw is None:
        raw = spec.get("command") or spec.get("template") or []
    if isinstance(raw, str):
        return [raw]
    return [str(x) for x in raw]


def _workspace_for(root: Path, spec: dict[str, Any], index: int) -> Path:
    raw = str(spec.get("workspace") or "").strip()
    if raw:
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = root / target
    else:
        target = root / f"{index:02d}-{_slug(str(spec.get('name')), f'role-{index}')}"
    target = target.resolve()
    root = root.resolve()
    if target != root and root not in target.parents:
        raise StudioError("role_workspace_escape", [f"{target}: 工作区必须位于 {root} 内"])
    target.mkdir(parents=True, exist_ok=True)
    return target


def create_cluster(store: Any, *, name: str, objective: str,
                   workspace_root: str, lead: dict[str, Any],
                   roles: list[dict[str, Any]] | None = None,
                   policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """创建集群：项目、每个角色的 Agent/授权/成员、工作区、3 张 studio 表。

    角色 ID 形如 `role-<cluster 后 6 位>-<序号>`，保证跨集群唯一；显示名用 role.name。
    """

    from app.contracts import ProjectCreate

    label = str(name or "").strip()
    goal = str(objective or "").strip()
    if len(label) < 2:
        raise StudioError("cluster_name_required", ["集群名至少 2 个字符"])
    if len(goal) < 2:
        raise StudioError("cluster_objective_required", ["集群目标至少 2 个字符"])
    if any(c["name"] == label for c in studio_store.list_clusters(store)):
        raise StudioError("cluster_name_exists", [f"{label}: 已有同名集群"])

    root_raw = str(workspace_root or "").strip()
    if not root_raw:
        raise StudioError("workspace_required", ["必须给集群工作区目录"])
    root = Path(root_raw).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    specs = _resolve_spec_agents(store, _role_specs(lead, roles))
    merged_policy = {**DEFAULT_POLICY, **(policy or {})}

    cluster = studio_store.create_cluster(
        store, project_id="", name=label, objective=goal,
        workspace_root=str(root), policy=merged_policy)
    cluster_id = cluster["id"]

    project = store.create_project(ProjectCreate(
        name=f"{label} · {uuid4().hex[:6]}", task_mode="manual"))
    studio_store.update_cluster(store, cluster_id, project_id=str(project.id))

    created_roles: list[dict[str, Any]] = []
    for index, spec in enumerate(specs):
        agent_id = f"role-{cluster_id[-6:]}-{index + 1}"
        base.ensure_participant(store, project_id=project.id, agent_id=agent_id,
                                display_name=str(spec["name"]))
        workspace = _workspace_for(root, spec, index)
        budget = dict(spec.get("budget") or {})
        # 复用 Agent 时把扩展配置一起带进角色，运行时按 Agent 的方式挂载
        for key in ("skills", "mcp", "plugins"):
            if spec.get(key):
                budget[key] = list(spec[key])
        if "mcp_managed" in spec:
            budget["mcp_managed"] = bool(spec["mcp_managed"])
        if "provider_id" in spec:
            budget["provider_id"] = str(spec.get("provider_id") or "")
        role = studio_store.add_role(
            store, cluster_id=cluster_id, name=str(spec["name"]), agent_id=agent_id,
            backend=str(spec["backend"]), model=str(spec.get("model") or ""),
            args=_normalise_args(spec),
            system_prompt=str(spec.get("system_prompt") or ""),
            workspace=str(workspace),
            capabilities=list(spec.get("capabilities") or []),
            budget=budget,
            requires_review=bool(spec.get("requires_review", False)),
            sort_order=index,
            local_agent_id=str(spec.get("local_agent_id") or ""),
            role_prompt=str(spec.get("role_prompt") or ""),
        )
        created_roles.append(role)
        if index == 0:
            studio_store.update_cluster(store, cluster_id, lead_role_id=role["id"])

    _event(store, str(project.id), "studio.cluster.created", "local-operator", {
        "cluster_id": cluster_id, "name": label, "roles": [r["name"] for r in created_roles],
    })
    return cluster_detail(store, cluster_id)  # type: ignore[return-value]


def get_cluster_or_error(store: Any, cluster_id: str) -> dict[str, Any]:
    cluster = studio_store.get_cluster(store, cluster_id)
    if cluster is None:
        raise StudioError("cluster_not_found", [str(cluster_id)])
    return cluster


def list_clusters(store: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cluster in studio_store.list_clusters(store):
        run = studio_store.latest_run(store, cluster["id"])
        out.append({
            **cluster,
            "run": run,
            "role_count": len(studio_store.list_roles(store, cluster["id"])),
        })
    return out


def cluster_detail(store: Any, cluster_id: str) -> dict[str, Any]:
    cluster = get_cluster_or_error(store, cluster_id)
    pid = cluster["project_id"]
    roles = studio_store.list_roles(store, cluster_id)
    run = studio_store.latest_run(store, cluster_id)
    role_by_agent = {r["agent_id"]: r for r in roles}

    tasks: list[dict[str, Any]] = []
    if pid:
        for task in store.list_tasks(pid):
            data = _dump(task)
            data["role_name"] = (role_by_agent.get(str(data.get("assignee") or "")) or {}).get("name", "")
            tasks.append(data)

    role_views: list[dict[str, Any]] = []
    used = (run or {}).get("budget_used") or {}
    invocations = dict(used.get("invocations") or {})
    max_inv = int((cluster.get("policy") or {}).get("max_invocations_per_role") or 30)
    for role in roles:
        box: dict[str, Any] = {}
        try:
            box = base.inbox(store, agent_id=role["agent_id"], project_id=pid)
        except Exception:  # noqa: BLE001 — 单个角色收件箱失败不拖垮整个视图
            box = {}
        current = next((t for t in tasks
                        if str(t.get("assignee") or "") == role["agent_id"]
                        and str(t.get("status") or "").upper() in ("CLAIMED", "RUNNING", "WAITING")), None)
        local_agent = (studio_store.get_agent(store, role["local_agent_id"])
                       if role.get("local_agent_id") else None)
        role_views.append({
            **role,
            "agent_name": (local_agent or {}).get("name") or "",
            "agent_missing": bool(role.get("local_agent_id") and local_agent is None),
            "invocations": int(invocations.get(role["id"], 0)),
            "max_invocations": max_inv,
            "inbox": {
                "assigned_to_me": len(box.get("assigned_to_me") or []),
                "in_flight": len(box.get("in_flight_tasks") or []),
                "to_answer": len(box.get("to_answer") or []),
                "to_consume": len(box.get("to_consume") or []),
            },
            "current_task": {"id": current.get("id"), "title": current.get("title"),
                             "status": current.get("status")} if current else None,
            "process": studio_store.latest_process_for_role(store, role["id"]),
        })

    by_status: dict[str, int] = {}
    for task in tasks:
        key = str(task.get("status") or "?")
        by_status[key] = by_status.get(key, 0) + 1

    artifacts = base.list_artifacts(store, project_id=pid) if pid else []
    events = base.recent_events(store, project_id=pid, limit=120) if pid else []

    elapsed = 0
    if run:
        try:
            started = datetime.fromisoformat(str(run["started_at"]).replace("Z", "+00:00"))
            elapsed = max(0, int((datetime.now(UTC) - started).total_seconds()))
        except (TypeError, ValueError):
            elapsed = 0

    return {
        **cluster,
        "roles": role_views,
        "run": {**run, "elapsed_seconds": elapsed} if run else None,
        "runs": studio_store.list_runs(store, cluster_id)[:10],
        "tasks": tasks,
        "events": events,
        "artifacts": artifacts,
        "processes": studio_store.list_processes(store, run["id"]) if run else [],
        "stats": {
            "tasks": len(tasks),
            "by_status": by_status,
            "artifacts": len(artifacts),
            "roles": len(roles),
        },
    }


def update_role(store: Any, role_id: str, **fields: Any) -> dict[str, Any]:
    role = studio_store.get_role(store, role_id)
    if role is None:
        raise StudioError("role_not_found", [str(role_id)])
    if "backend" in fields and fields["backend"] not in studio_store.ROLE_BACKENDS:
        raise StudioError("role_backend_invalid", ["只支持 claude / command"])
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if not name:
            raise StudioError("role_name_required", ["角色名不能为空"])
        fields["name"] = name
    if "args" in fields and isinstance(fields["args"], str):
        fields["args"] = [fields["args"]]
    role_prompt = fields.pop("role_prompt", None)
    if role_prompt is not None:
        text = str(role_prompt).strip()
        local_id = str(fields.get("local_agent_id") or role.get("local_agent_id") or "")
        agent = studio_store.get_agent(store, local_id) if local_id else None
        base_prompt = str((agent or {}).get("system_prompt") or "").strip()
        fields["role_prompt"] = text
        fields["system_prompt"] = (base_prompt + "\n\n本轮职责：" + text).strip() \
            if (base_prompt and text) else (text or base_prompt)
    role = studio_store.update_role(store, role_id, **fields)
    return role  # type: ignore[return-value]


def add_role(store: Any, cluster_id: str, *, agent_id: str = "",
             name: str = "", role_prompt: str = "", **overrides: Any) -> dict[str, Any]:
    """给已有集群加一个角色；`agent_id` 是 local_agents.id（复用已有 Agent）。"""

    cluster = get_cluster_or_error(store, cluster_id)
    if str(cluster.get("status") or "").upper() in ("RUNNING", "PAUSED"):
        raise StudioError("cluster_busy", ["先暂停或停止集群，再改角色"])
    existing = studio_store.list_roles(store, cluster_id)
    spec: dict[str, Any] = {"agent_id": str(agent_id or "").strip(),
                            "role_prompt": str(role_prompt or "").strip()}
    if name:
        spec["name"] = str(name).strip()
    spec.update({k: v for k, v in overrides.items() if v not in (None, "")})
    resolved = _resolve_spec_agents(store, [spec])[0]
    if any(r["name"] == resolved["name"] for r in existing):
        raise StudioError("role_name_duplicate", [f"{resolved['name']}: 角色名重复"])

    index = len(existing)
    protocol_id = f"role-{cluster_id[-6:]}-{index + 1}"
    while any(r["agent_id"] == protocol_id for r in existing):
        index += 1
        protocol_id = f"role-{cluster_id[-6:]}-{index + 1}"
    root = Path(str(cluster["workspace_root"]))
    workspace = _workspace_for(root, resolved, index)
    base.ensure_participant(store, project_id=cluster["project_id"],
                            agent_id=protocol_id, display_name=str(resolved["name"]))
    budget = dict(resolved.get("budget") or {})
    for key in ("skills", "mcp", "plugins"):
        if resolved.get(key):
            budget[key] = list(resolved[key])
    if "mcp_managed" in resolved:
        budget["mcp_managed"] = bool(resolved["mcp_managed"])
    if resolved.get("provider_id"):
        budget["provider_id"] = str(resolved["provider_id"])
    role = studio_store.add_role(
        store, cluster_id=cluster_id, name=str(resolved["name"]), agent_id=protocol_id,
        backend=str(resolved["backend"]), model=str(resolved.get("model") or ""),
        args=_normalise_args(resolved),
        system_prompt=str(resolved.get("system_prompt") or ""),
        workspace=str(workspace),
        capabilities=list(resolved.get("capabilities") or []),
        budget=budget,
        requires_review=bool(resolved.get("requires_review", False)),
        sort_order=index,
        local_agent_id=str(resolved.get("local_agent_id") or ""),
        role_prompt=str(resolved.get("role_prompt") or ""),
    )
    _event(store, str(cluster["project_id"]), "studio.role.added", "local-operator", {
        "cluster_id": cluster_id, "role": role["name"], "role_id": role["id"],
    })
    return role


def delete_role(store: Any, role_id: str) -> dict[str, Any]:
    role = studio_store.get_role(store, role_id)
    if role is None:
        raise StudioError("role_not_found", [str(role_id)])
    cluster = get_cluster_or_error(store, role["cluster_id"])
    if str(cluster.get("lead_role_id") or "") == role_id:
        raise StudioError("role_is_lead", ["lead 角色不能删除"])
    if str(cluster.get("status") or "").upper() in ("RUNNING", "PAUSED"):
        raise StudioError("cluster_busy", ["先暂停或停止集群，再删角色"])
    studio_store.delete_role(store, role_id)
    return {"deleted": True, "role_id": role_id, "name": role["name"]}


def start_cluster(store: Any, cluster_id: str, *,
                  kickoff: bool = True) -> dict[str, Any]:
    """创建 run +（可选）lead 的 kickoff 任务；调度线程由 Supervisor 启动。

    `kickoff=False` 用于「计划已确认、任务板已有任务」的场景：不再让 lead 重复拆解。
    """

    cluster = get_cluster_or_error(store, cluster_id)
    roles = studio_store.list_roles(store, cluster_id)
    lead = next((r for r in roles if r["id"] == cluster.get("lead_role_id")), None)
    if lead is None:
        raise StudioError("cluster_lead_missing", ["集群没有 lead 角色"])
    if cluster["status"] == "RUNNING":
        return {"started": False, "reason": "already_running",
                "run": studio_store.latest_run(store, cluster_id)}
    if cluster["status"] == "PAUSED":
        return {"started": False, "reason": "paused",
                "run": studio_store.latest_run(store, cluster_id)}

    run = studio_store.create_run(store, cluster_id)
    studio_store.update_cluster(store, cluster_id, status="RUNNING")
    task = None
    if kickoff:
        roster = "\n".join(
            f"- {r['name']}（{'lead' if r['id'] == lead['id'] else 'worker'}，agent_id={r['agent_id']}）"
            for r in roles)
        task = base.create_task(
            store, project_id=cluster["project_id"],
            title="拆解目标并派活",
            description=(
                f"集群目标：{cluster['objective']}\n\n角色名册：\n{roster}\n\n"
                "你是 lead。先看任务板；如果还没有 worker 任务，把目标拆成 2-4 个可独立完成的任务，"
                "用 synapforge_task_create 指派给对应 worker。不要替 worker 执行。"
            ),
            stage="kickoff", assignee_agent_id=lead["agent_id"], requires_review=False)
    _event(store, cluster["project_id"], "studio.cluster.started", "local-operator", {
        "cluster_id": cluster_id, "run_id": run["id"],
        "kickoff_task_id": task["id"] if task else "",
    })
    return {"started": True, "run": run, "task": task, "cluster": cluster}


def pause_cluster(store: Any, cluster_id: str, *, reason: str = "manual") -> dict[str, Any]:
    cluster = get_cluster_or_error(store, cluster_id)
    run = studio_store.latest_run(store, cluster_id)
    if run is None:
        raise StudioError("run_not_found", ["集群还没有运行记录"])
    studio_store.update_run(store, run["id"], status="PAUSED",
                            checkpoint={**run.get("checkpoint", {}), "paused_reason": reason})
    studio_store.update_cluster(store, cluster_id, status="PAUSED")
    _event(store, cluster["project_id"], "studio.cluster.paused", "local-operator", {
        "cluster_id": cluster_id, "run_id": run["id"], "reason": reason,
    })
    return {"paused": True, "run_id": run["id"], "reason": reason}


def resume_cluster(store: Any, cluster_id: str) -> dict[str, Any]:
    cluster = get_cluster_or_error(store, cluster_id)
    run = studio_store.latest_run(store, cluster_id)
    if run is None:
        raise StudioError("run_not_found", ["集群还没有运行记录"])
    if run["status"] == "STOPPED":
        raise StudioError("run_stopped", ["已停止的运行不能恢复，请重新开始"])
    studio_store.update_run(store, run["id"], status="RUNNING",
                            checkpoint={**run.get("checkpoint", {}), "paused_reason": None})
    studio_store.update_cluster(store, cluster_id, status="RUNNING")
    _event(store, cluster["project_id"], "studio.cluster.resumed", "local-operator", {
        "cluster_id": cluster_id, "run_id": run["id"],
    })
    return {"resumed": True, "run_id": run["id"]}


def stop_cluster(store: Any, cluster_id: str, *, reason: str = "manual") -> dict[str, Any]:
    cluster = get_cluster_or_error(store, cluster_id)
    run = studio_store.latest_run(store, cluster_id)
    if run is None:
        raise StudioError("run_not_found", ["集群还没有运行记录"])
    studio_store.update_run(store, run["id"], status="STOPPED", stopped_at=_now(),
                            checkpoint={**run.get("checkpoint", {}), "stopped_reason": reason})
    studio_store.update_cluster(store, cluster_id, status="STOPPED")
    _event(store, cluster["project_id"], "studio.cluster.stopped", "local-operator", {
        "cluster_id": cluster_id, "run_id": run["id"], "reason": reason,
    })
    return {"stopped": True, "run_id": run["id"], "reason": reason}


def send_instruction(store: Any, cluster_id: str, text: str,
                     role_id: str | None = None) -> dict[str, Any]:
    cluster = get_cluster_or_error(store, cluster_id)
    body = str(text or "").strip()
    if not body:
        raise StudioError("instruction_empty", ["指令不能为空"])
    roles = studio_store.list_roles(store, cluster_id)
    if role_id:
        role = next((r for r in roles if r["id"] == role_id), None)
        if role is None:
            raise StudioError("role_not_found", [str(role_id)])
        task = base.create_task(
            store, project_id=cluster["project_id"],
            title=(body[:120] if len(body) >= 2 else f"指令：{body}"),
            description=body, stage="instruction",
            assignee_agent_id=role["agent_id"], requires_review=False)
        message = base.post_message(store, project_id=cluster["project_id"],
                                    sender="local-operator", text=body, sender_kind="member")
        return {"task": task, "message": message, "to": role["name"]}
    message = base.broadcast_task(
        store, project_id=cluster["project_id"], from_member="local-operator",
        text=body, agent_ids=[r["agent_id"] for r in roles], stage="instruction")
    return {"broadcast": message}


def review_task(store: Any, task_id: str, verdict: str, summary: str,
                reviewer: str = "member-001") -> dict[str, Any]:
    from app.contracts import ReviewCreate

    verdict = str(verdict or "").upper()
    if verdict not in {"APPROVED", "NEEDS_REVISION", "BLOCKED"}:
        raise StudioError("review_verdict_invalid", ["只支持 APPROVED / NEEDS_REVISION / BLOCKED"])
    task = store.get_task(UUID(str(task_id)))
    if task is None:
        raise StudioError("task_not_found", [str(task_id)])
    review = store.create_review(UUID(str(task.project_id)), ReviewCreate(
        target_type="task", target_id=UUID(str(task_id)), verdict=verdict,
        summary=str(summary or "本地控制台复核")[:2000],
        reviewer=reviewer, reviewer_kind="member"))
    return {"review": _dump(review), "task": _dump(store.get_task(UUID(str(task_id))))}


def review_artifact(store: Any, artifact_id: str, verdict: str, summary: str,
                    reviewer: str = "member-001") -> dict[str, Any]:
    from app.contracts import ReviewCreate

    verdict = str(verdict or "").upper()
    if verdict not in {"APPROVED", "NEEDS_REVISION", "BLOCKED"}:
        raise StudioError("review_verdict_invalid", ["只支持 APPROVED / NEEDS_REVISION / BLOCKED"])
    artifact = store.get_artifact(UUID(str(artifact_id)))
    if artifact is None:
        raise StudioError("artifact_not_found", [str(artifact_id)])
    review = store.create_review(UUID(str(artifact.project_id)), ReviewCreate(
        target_type="artifact", target_id=UUID(str(artifact_id)), verdict=verdict,
        summary=str(summary or "本地控制台复核")[:2000],
        reviewer=reviewer, reviewer_kind="member"))
    return {"review": _dump(review), "artifact": _dump(store.get_artifact(UUID(str(artifact_id))))}


def auto_approve_pending(store: Any, cluster_id: str) -> int:
    """把 WAITING_REVIEW 的任务自动批掉；返回批准数量。"""

    cluster = get_cluster_or_error(store, cluster_id)
    approved = 0
    for task in store.list_tasks(cluster["project_id"]):
        if str(getattr(task, "status", "")).upper() != "WAITING_REVIEW":
            continue
        try:
            review_task(store, str(task.id), "APPROVED", "supervisor auto-approve")
            approved += 1
        except Exception:  # noqa: BLE001 — 单条失败继续，不阻断收尾
            continue
    return approved


def ensure_final_review_task(store: Any, cluster_id: str) -> dict[str, Any] | None:
    cluster = get_cluster_or_error(store, cluster_id)
    tasks = store.list_tasks(cluster["project_id"])
    if any(str(getattr(t, "stage", "")) == "final_review" for t in tasks):
        return None
    lead = next((r for r in studio_store.list_roles(store, cluster_id)
                 if r["id"] == cluster.get("lead_role_id")), None)
    if lead is None:
        return None
    task = base.create_task(
        store, project_id=cluster["project_id"], title="集群终审",
        description=("worker 任务已全部结束。请复核本集群的成果物与任务结论："
                     "检查是否覆盖目标、是否有缺证据的结论；把结论写进工作区并 finish。"),
        stage="final_review", assignee_agent_id=lead["agent_id"], requires_review=False)
    return task


def mark_done(store: Any, cluster_id: str) -> dict[str, Any]:
    cluster = get_cluster_or_error(store, cluster_id)
    run = studio_store.latest_run(store, cluster_id)
    if run is not None:
        studio_store.update_run(store, run["id"], status="DONE")
    studio_store.update_cluster(store, cluster_id, status="DONE")
    _event(store, cluster["project_id"], "studio.cluster.done", "local-operator", {
        "cluster_id": cluster_id, "run_id": run["id"] if run else None,
    })
    return {"done": True, "run_id": run["id"] if run else None}


def delete_cluster(store: Any, cluster_id: str) -> dict[str, Any]:
    cluster = get_cluster_or_error(store, cluster_id)
    studio_store.delete_cluster(store, cluster_id)
    return {"deleted": True, "cluster_id": cluster_id, "name": cluster["name"]}
