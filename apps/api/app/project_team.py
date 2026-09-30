"""项目团队与生产路径聚合（W2.3，实施计划第二期）。

服务端聚合、前端只渲染——**数据全部来自既有权威表，不新增状态、不知道的不编**：
探不到在线就 offline，没有当前任务就空列表。

两件事分开（借 autogen 双订阅拓扑的划分）：
- `project_team` —— "谁在做什么"（定向视角）：每个在本项目有授权的 Agent 的身份、能力、
  在线、当前任务/负载、在等什么、下一件；
- `project_production_path` —— "东西从哪来到哪去"（广播视角）：成果物的产出方（任务/Run）、
  审批状态、被哪些交接引用、喂给了哪些下游任务。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

#: 与 `agent_chat.list_my_agents` 同口径：心跳 180 秒内算在线，超过就如实说不在线。
ONLINE_WINDOW_SECONDS = 180

_ACTIVE_TASK_STATUSES = {"CLAIMED", "RUNNING"}
_WAITING_TASK_STATUSES = {"BLOCKED", "NEEDS_REVISION"}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _enum_value(value: Any) -> str:
    """枚举取 .value（str-Enum 的 str() 在 3.11+ 会带类名前缀，不能直接比）。"""

    return str(value.value if hasattr(value, "value") else value)


def _task_brief(task: Any) -> dict[str, Any]:
    return {
        "task_id": str(task.id),
        "title": task.title,
        "stage": task.stage,
        "status": _enum_value(task.status),
        "priority": str(task.priority or ""),
        "blocked_reason": getattr(task, "blocked_reason", None) or "",
    }


def project_team(store: Any, project_id: UUID) -> dict[str, Any]:
    """项目里的 Agent 团队概览：每个 Agent 的身份/能力/在线/当前任务/负载/在等什么/下一件。"""

    now = _now_iso()
    online_threshold = datetime.now(UTC) - timedelta(seconds=ONLINE_WINDOW_SECONDS)
    grant_rows = store.db.execute(
        "SELECT g.device_id, g.capabilities, d.agent_id, d.device_name, d.status, d.last_seen, "
        "       a.display_name AS agent_name, a.model_name "
        "FROM device_project_grants g "
        "JOIN devices d ON d.device_id = g.device_id "
        "LEFT JOIN agents a ON a.agent_id = d.agent_id "
        "WHERE g.project_id = ? AND g.revoked_at IS NULL AND g.expires_at > ? "
        "ORDER BY d.device_id",
        (str(project_id), now),
    ).fetchall()

    tasks = store.list_tasks(project_id)
    # "谁在跑"的权威来源是两处：任务行的 assignee（claim 写的就是它）+ ACTIVE 租约的 agent_id。
    # 只看一处会漏（历史行为两处并存），所以取并集。
    active_lease_agents: dict[str, str] = {}
    for lease_row in store.db.execute(
        "SELECT task_id, agent_id FROM task_leases WHERE project_id = ? AND status = 'ACTIVE'",
        (str(project_id),),
    ):
        active_lease_agents[str(lease_row["task_id"])] = str(lease_row["agent_id"])
    handoffs = store.list_handoffs(project_id)
    pending_handoffs = [h for h in handoffs if _enum_value(getattr(h, "receipt_status", "")) == "PENDING"]

    members: list[dict[str, Any]] = []
    for row in grant_rows:
        agent_id = str(row["agent_id"] or "")
        capabilities: list[str] = []
        try:
            loaded = json.loads(row["capabilities"] or "[]")
            capabilities = [str(item) for item in loaded] if isinstance(loaded, list) else []
        except ValueError:
            capabilities = []
        last_seen = _parse_time(row["last_seen"])
        mine = [
            task
            for task in tasks
            if agent_id
            and (str(getattr(task, "assignee", "") or "") == agent_id or active_lease_agents.get(str(task.id)) == agent_id)
        ]
        current = [_task_brief(task) for task in mine if _enum_value(task.status) in _ACTIVE_TASK_STATUSES]
        waiting = [_task_brief(task) for task in mine if _enum_value(task.status) in _WAITING_TASK_STATUSES]
        ready = [_task_brief(task) for task in mine if _enum_value(task.status) == "READY"]
        receiving = [
            {"handoff_id": str(h.id), "task_id": str(h.task_id), "status": _enum_value(h.receipt_status)}
            for h in pending_handoffs
            if agent_id and agent_id in str(getattr(h, "receiver", ""))
        ]
        members.append(
            {
                "agent_id": agent_id or None,
                "agent_name": str(row["agent_name"] or ""),
                "device_id": str(row["device_id"]),
                "device_name": str(row["device_name"] or ""),
                "model_name": str(row["model_name"] or ""),
                "online": bool(last_seen and last_seen >= online_threshold),
                "device_status": str(row["status"] or "unknown"),
                "capabilities": capabilities,
                "load": len(current),
                "current_tasks": current,
                "next_tasks": ready,
                "waiting_tasks": waiting,
                "pending_handoffs": receiving,
            }
        )

    status_counts: dict[str, int] = {}
    for task in tasks:
        key = str(task.status.value if hasattr(task.status, "value") else task.status)
        status_counts[key] = status_counts.get(key, 0) + 1
    return {
        "project_id": str(project_id),
        "agents": members,
        "agent_count": len(members),
        "online_count": len([m for m in members if m["online"]]),
        "task_status_counts": status_counts,
        "pending_handoff_count": len(pending_handoffs),
        "generated_at": now,
    }


def project_production_path(store: Any, project_id: UUID, *, limit: int = 50) -> dict[str, Any]:
    """成果物 → 交接 → 下游任务的因果链（最近 limit 个成果物）。"""

    tasks = store.list_tasks(project_id)
    task_by_id = {str(task.id): task for task in tasks}
    handoffs = store.list_handoffs(project_id)
    artifacts = sorted(store.list_artifacts(project_id), key=lambda a: str(a.created_at), reverse=True)[: max(1, int(limit))]

    nodes: list[dict[str, Any]] = []
    for artifact in artifacts:
        artifact_id = str(artifact.id)
        source_task = task_by_id.get(str(artifact.task_id)) if artifact.task_id else None
        downstream = [
            _task_brief(task)
            for task in tasks
            if artifact_id in [str(item) for item in (getattr(task, "input_artifacts", None) or [])]
        ]
        linked_handoffs = [
            {"handoff_id": str(h.id), "task_id": str(h.task_id), "status": _enum_value(h.status), "receipt_status": _enum_value(h.receipt_status)}
            for h in handoffs
            if artifact_id in [str(item) for item in (getattr(h, "input_artifacts", None) or [])]
            or artifact_id in [str(item) for item in (getattr(h, "output_artifacts", None) or [])]
        ]
        nodes.append(
            {
                "artifact_id": artifact_id,
                "name": artifact.name,
                "artifact_type": str(artifact.artifact_type),
                "status": _enum_value(artifact.status),
                "version": int(getattr(artifact, "version", 1) or 1),
                "created_by": str(getattr(artifact, "created_by", "") or ""),
                "created_at": str(artifact.created_at),
                "approved_by": getattr(artifact, "approved_by", None),
                "approved_at": str(artifact.approved_at) if getattr(artifact, "approved_at", None) else None,
                "source": (
                    {"task_id": str(source_task.id), "title": source_task.title, "status": _enum_value(source_task.status)}
                    if source_task
                    else None
                ),
                "run_id": str(artifact.run_id) if getattr(artifact, "run_id", None) else None,
                # W2.4 溯源位（B 预留）：哪次工具调用产出的；人工上传/历史行为 None
                "receipt": (
                    {
                        "receipt_version": artifact.receipt.receipt_version,
                        "tool_name": artifact.receipt.tool_name,
                        "tool_call_id": artifact.receipt.tool_call_id,
                        "args_hash": artifact.receipt.args_hash,
                        "output_hash": artifact.receipt.output_hash,
                        "output_bytes": artifact.receipt.output_bytes,
                        "truncated": artifact.receipt.truncated,
                    }
                    if getattr(artifact, "receipt", None)
                    else None
                ),
                "handoffs": linked_handoffs,
                "downstream_tasks": downstream,
            }
        )
    return {
        "project_id": str(project_id),
        "nodes": nodes,
        "artifact_total": len(store.list_artifacts(project_id)),
        "task_total": len(tasks),
        "handoff_total": len(handoffs),
        "generated_at": _now_iso(),
    }