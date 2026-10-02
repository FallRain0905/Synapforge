"""协作协议存储与生命周期（D1，多 Agent 协作开发计划；协议形状权威 coordination.py）。

把 D0 冻结的协议落到既有 store：阶段报告接收、运行时信息请求全生命周期（创建→确认→
回复→消费→转问→过期）、编排决策持久化、workflow-view 聚合 DTO。

纪律：
- 事实事件与业务状态同事务写入；事件名必须在目录（project.information_request.* /
  project.feasibility_concern.* / project.stage_report.submitted，v3）；
- 幂等：同一 idempotency_key 的重复请求返回**原结果**（information_requests 幂等唯一索引）；
- 状态迁移走 coordination.check_transition（fail-closed）；
- required 请求的节点效果由 decide_node_effect 决定（WAITING/information_requested），
  引擎据此跳过被信息请求阻塞的节点。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from . import coordination, workflow_service
from .coordination import CoordinationError, check_transition

__all__ = [
    "ensure_schema",
    "submit_stage_report",
    "get_stage_reports",
    "create_information_request",
    "get_request",
    "ack_information_request",
    "respond_information_request",
    "redirect_information_request",
    "consume_information_request",
    "expire_overdue_requests",
    "list_information_requests",
    "record_decision",
    "list_decisions",
    "build_workflow_view",
]


class CoordinationStoreError(CoordinationError):
    """存储侧稳定错误族（request_not_found / budget_exceeded / duplicate...）。"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def ensure_schema(store: Any) -> None:
    if getattr(store, "_coordination_schema_ready", False):
        return
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS stage_reports (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id),
            task_id TEXT NOT NULL REFERENCES tasks(id),
            run_id TEXT,
            attempt INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            payload TEXT NOT NULL DEFAULT '{}',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            idempotency_key TEXT
        );
        CREATE TABLE IF NOT EXISTS information_requests (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id),
            run_id TEXT,
            requester_node_id TEXT,
            requester_task_id TEXT,
            requester_agent_id TEXT NOT NULL,
            provider_agent_id TEXT,
            provider_capability TEXT,
            request_type TEXT NOT NULL,
            question TEXT NOT NULL,
            blocking TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            node_effect TEXT NOT NULL DEFAULT '',
            redirect_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'OPEN',
            payload TEXT NOT NULL DEFAULT '{}',
            correlation_id TEXT,
            idempotency_key TEXT,
            response_deadline TEXT,
            responded_at TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS information_requests_idem_idx
            ON information_requests(idempotency_key) WHERE idempotency_key IS NOT NULL;
        CREATE TABLE IF NOT EXISTS orchestration_decisions (
            id TEXT PRIMARY KEY,
            organization_id TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id),
            run_id TEXT,
            node_id TEXT,
            task_id TEXT,
            policy TEXT NOT NULL,
            basis TEXT NOT NULL DEFAULT '[]',
            expected_events TEXT NOT NULL DEFAULT '[]',
            stop_reason TEXT,
            payload TEXT NOT NULL DEFAULT '{}',
            created_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        """
    )
    columns = {row[1] for row in store.db.execute("PRAGMA table_info(stage_reports)")}
    if columns and "idempotency_key" not in columns:
        store.db.execute("ALTER TABLE stage_reports ADD COLUMN idempotency_key TEXT")
    store.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS stage_reports_idem_idx ON stage_reports(idempotency_key) WHERE idempotency_key IS NOT NULL")
    store.db.commit()
    store._coordination_schema_ready = True


def _project_org(store: Any, project_id: UUID) -> str:
    row = store.db.execute("SELECT organization_id FROM projects WHERE id = ?", (str(project_id),)).fetchone()
    if row is None:
        raise CoordinationStoreError("project_not_found")
    return str(row["organization_id"])


def _event(store: Any, project_id: UUID, event_type: str, actor: str, payload: dict, *, actor_kind: str = "agent",
           object_type: str | None = None, object_id: str | None = None) -> None:
    store.add_event(
        project_id, event_type, actor, payload,
        actor_kind=actor_kind, object_type=object_type,
        object_id=UUID(object_id) if object_id else None,
    )


# ---- 阶段报告（计划 §阶段 4：报告进入接收方审核，不直接当批准结论）----------------


def submit_stage_report(
    store: Any, project_id: UUID, task_id: UUID, agent_id: str, report: dict[str, Any],
    *, run_id: str | None = None, attempt: int = 1, organization_id: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    ensure_schema(store)
    project_id = project_id if isinstance(project_id, UUID) else UUID(str(project_id))
    errors = coordination.validate_stage_report(report)
    if errors:
        raise CoordinationStoreError("stage_report_invalid", errors)
    organization_id = organization_id or _project_org(store, project_id)
    # 幂等（D3）：同一 idempotency_key 的重复提交返回**原报告**，不重复记账
    if idempotency_key:
        existing = store.db.execute(
            "SELECT * FROM stage_reports WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            return {**_report_view(existing), "duplicate": True}
    report_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO stage_reports (id, organization_id, project_id, task_id, run_id, attempt, status, summary, payload, created_by, created_at, idempotency_key)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report_id, organization_id, str(project_id), str(task_id), run_id, int(attempt),
            str(report["status"]), str(report.get("summary") or ""),
            json.dumps(report, ensure_ascii=False), agent_id, timestamp, idempotency_key,
        ),
    )
    store.db.commit()
    _event(store, project_id, "project.stage_report.submitted", agent_id,
           {"report_id": report_id, "task_id": str(task_id), "status": str(report["status"]),
            "summary": str(report.get("summary") or "")[:200]},
           actor_kind="agent", object_type="task", object_id=str(task_id))
    row = store.db.execute("SELECT * FROM stage_reports WHERE id = ?", (report_id,)).fetchone()
    return _report_view(row)


def _report_view(row: Any) -> dict[str, Any]:
    return {
        "report_id": str(row["id"]),
        "task_id": str(row["task_id"]),
        "run_id": row["run_id"],
        "attempt": int(row["attempt"]),
        "status": str(row["status"]),
        "summary": str(row["summary"] or ""),
        "payload": json.loads(row["payload"] or "{}"),
        "created_by": str(row["created_by"] or ""),
        "created_at": str(row["created_at"]),
    }


def get_stage_reports(store: Any, project_id: UUID, task_id: UUID) -> list[dict[str, Any]]:
    ensure_schema(store)
    rows = store.db.execute(
        "SELECT * FROM stage_reports WHERE project_id = ? AND task_id = ? ORDER BY created_at DESC LIMIT 20",
        (str(project_id), str(task_id)),
    ).fetchall()
    return [_report_view(row) for row in rows]


# ---- 运行时信息请求（计划 §6.5）---------------------------------------------------


def create_information_request(
    store: Any, project_id: UUID, requester_agent_id: str, body: dict[str, Any],
    *, organization_id: str | None = None,
) -> dict[str, Any]:
    ensure_schema(store)
    project_id = project_id if isinstance(project_id, UUID) else UUID(str(project_id))
    request = body.get("request") or {}
    errors = coordination.validate_information_request(request)
    if errors:
        raise CoordinationStoreError("information_request_invalid", errors)
    organization_id = organization_id or _project_org(store, project_id)

    idempotency_key = str(body.get("idempotency_key") or "").strip() or None
    if idempotency_key:
        existing = store.db.execute(
            "SELECT * FROM information_requests WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing is not None:
            # 幂等：重复请求返回**原结果**，不重复制造（§6.5.3）。
            return {**_request_view(existing), "duplicate": True}

    run_id = body.get("run_id") or None
    # 通信预算：并发/总量（转问上限在 redirect 处结算）
    open_count = store.db.execute(
        "SELECT COUNT(*) AS c FROM information_requests WHERE run_id = ? AND status IN ('OPEN','ROUTED','ACKNOWLEDGED')",
        (run_id,),
    ).fetchone()["c"] if run_id else 0
    total_count = store.db.execute(
        "SELECT COUNT(*) AS c FROM information_requests WHERE run_id = ?", (run_id,)
    ).fetchone()["c"] if run_id else 0
    ok, reason = coordination.check_request_budget(
        run_active_requests=int(open_count), run_total_requests=int(total_count), redirects_for_request=0
    )
    if not ok:
        raise CoordinationStoreError("information_request_budget_exceeded", [reason])

    request_id = str(uuid4())
    timestamp = _now()
    blocking = str(request["blocking"])
    # 边界字符串化：requester_task_id 可能是 UUID 对象（sqlite 不认）
    requester_task_id = str(body.get("requester_task_id")) if body.get("requester_task_id") else None
    node_effect = "WAITING:information_requested" if blocking == "required" else ""
    store.db.execute(
        "INSERT INTO information_requests (id, organization_id, project_id, run_id, requester_node_id, requester_task_id,"
        " requester_agent_id, provider_agent_id, provider_capability, request_type, question, blocking, reason,"
        " node_effect, redirect_count, status, payload, correlation_id, idempotency_key, response_deadline, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'OPEN', ?, ?, ?, ?, ?, ?)",
        (
            request_id, organization_id, str(project_id), run_id, body.get("requester_node_id"),
            requester_task_id, requester_agent_id, body.get("provider_agent_id"),
            body.get("provider_capability"), str(request["request_type"]), str(request["question"]),
            blocking, str(request.get("reason") or ""), node_effect,
            json.dumps(request, ensure_ascii=False), body.get("correlation_id"), idempotency_key,
            request.get("response_deadline"), timestamp, timestamp,
        ),
    )
    store.db.commit()
    _event(store, project_id, "project.information_request.created", requester_agent_id,
           {"request_id": request_id, "requester_agent_id": requester_agent_id,
            "provider_agent_id": body.get("provider_agent_id"), "request_type": request["request_type"],
            "blocking": blocking, "question": str(request["question"])[:200],
            "response_deadline": request.get("response_deadline")},
           actor_kind="agent")
    row = store.db.execute("SELECT * FROM information_requests WHERE id = ?", (request_id,)).fetchone()
    return _request_view(row)


def _request_view(row: Any) -> dict[str, Any]:
    return {
        "request_id": str(row["id"]),
        "project_id": str(row["project_id"]),
        "run_id": row["run_id"],
        "requester_node_id": row["requester_node_id"],
        "requester_task_id": row["requester_task_id"],
        "requester_agent_id": str(row["requester_agent_id"]),
        "provider_agent_id": row["provider_agent_id"],
        "provider_capability": row["provider_capability"],
        "request_type": str(row["request_type"]),
        "question": str(row["question"]),
        "blocking": str(row["blocking"]),
        "reason": str(row["reason"] or ""),
        "node_effect": str(row["node_effect"] or ""),
        "redirect_count": int(row["redirect_count"]),
        "status": str(row["status"]),
        "payload": json.loads(row["payload"] or "{}"),
        "response_deadline": row["response_deadline"],
        "responded_at": row["responded_at"],
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def get_request(store: Any, request_id: UUID) -> dict[str, Any]:
    """公开读取器：路由层先取行做项目级能力校验，再进生命周期操作。"""

    ensure_schema(store)
    return _request_view(_load_request(store, str(request_id)))


def _load_request(store: Any, request_id: str) -> Any:
    row = store.db.execute("SELECT * FROM information_requests WHERE id = ?", (str(request_id),)).fetchone()
    if row is None:
        raise CoordinationStoreError("request_not_found")
    return row


def ack_information_request(store: Any, request_id: UUID, provider_agent_id: str, outcome: str) -> dict[str, Any]:
    """接收方 ACK：received / rejected / unable_to_execute。重复 ACK 返回原结果。"""

    ensure_schema(store)
    if outcome not in ("received", "rejected", "unable_to_execute"):
        raise CoordinationStoreError("ack_outcome_invalid", [f"outcome: {outcome!r} 不在 received/rejected/unable_to_execute"])
    row = _load_request(store, request_id)
    if str(row["status"]) == "ACKNOWLEDGED" and outcome == "received":
        # 重复 ACK：返回原状态（幂等，D3 重复投递不双写）
        return {**_request_view(row), "ack_outcome": "received", "duplicate": True}
    if str(row["status"]) == "REJECTED" and outcome == "rejected":
        return {**_request_view(row), "ack_outcome": "rejected", "duplicate": True}
    if row["status"] not in {"OPEN", "ROUTED", "ACKNOWLEDGED"}:
        raise CoordinationStoreError("request_status_invalid", [f"status={row['status']} 不可 ACK"])
    if outcome == "received":
        check_transition("information_request", str(row["status"]), "ACKNOWLEDGED")
        new_status = "ACKNOWLEDGED"
    else:
        target = "REJECTED" if outcome == "rejected" else "UNAVAILABLE"
        check_transition("information_request", str(row["status"]), target)
        new_status = target
    store.db.execute(
        "UPDATE information_requests SET status = ?, updated_at = ? WHERE id = ?", (new_status, _now(), str(request_id))
    )
    store.db.commit()
    _event(store, UUID(row["project_id"]), "project.information_request.acked", provider_agent_id,
           {"request_id": str(request_id), "outcome": outcome}, actor_kind="agent")
    return {**_request_view(_load_request(store, request_id)), "ack_outcome": outcome}


def respond_information_request(store: Any, request_id: UUID, provider_agent_id: str, response: dict[str, Any]) -> dict[str, Any]:
    ensure_schema(store)
    errors = coordination.validate_information_response(response)
    if errors:
        raise CoordinationStoreError("information_response_invalid", errors)
    row = _load_request(store, request_id)
    if str(row["provider_agent_id"] or "") and str(row["provider_agent_id"]) != provider_agent_id:
        raise CoordinationStoreError("request_provider_mismatch")
    status = "ANSWERED" if response["status"] == "answered" else (
        "PROVISIONAL" if response["status"] == "provisional" else
        "UNAVAILABLE" if response["status"] == "unavailable" else "REJECTED"
    )
    check_transition("information_request", str(row["status"]), status)
    # 节点效果：answered/provisional 等请求方消费（CONSUMER_ACKNOWLEDGED）；
    # unavailable/rejected 没有可消费的回复——respond 时立即 BLOCKED（计划 §6.5.1：
    # 负面回复必须进入明确的阻塞/升级路径，不存在"消费不可用回复"这步迁移）。
    node_effect = ""
    if status in {"UNAVAILABLE", "REJECTED"}:
        node_effect = "BLOCKED:information_request_" + response["status"]
        if row["requester_task_id"]:
            store.db.execute(
                "UPDATE tasks SET status = 'BLOCKED', blocked_reason = ?, updated_at = ? WHERE id = ?",
                (f"information_request:{request_id}", _now(), str(row["requester_task_id"])),
            )
    store.db.execute(
        "UPDATE information_requests SET status = ?, node_effect = ?, payload = ?, responded_at = ?, updated_at = ? WHERE id = ?",
        (status, node_effect, json.dumps(response, ensure_ascii=False), _now(), _now(), str(request_id)),
    )
    store.db.commit()
    _event(store, UUID(row["project_id"]), "project.information_request.answered", provider_agent_id,
           {"request_id": str(request_id), "response_status": response["status"],
            "answer_summary": str(response.get("answer_summary") or "")[:200],
            "node_effect": node_effect},
           actor_kind="agent")
    view = _request_view(_load_request(store, request_id))
    if node_effect:
        view["node_effect_result"] = {"effect": "BLOCKED", "reason": node_effect}
    return view


def redirect_information_request(store: Any, request_id: UUID, actor: str, new_provider_agent_id: str | None, new_provider_capability: str | None) -> dict[str, Any]:
    row = _load_request(store, request_id)
    ok, reason = coordination.check_request_budget(
        run_active_requests=0, run_total_requests=0, redirects_for_request=int(row["redirect_count"])
    )
    if not ok:
        raise CoordinationStoreError("information_request_budget_exceeded", [reason])
    check_transition("information_request", str(row["status"]), "REDIRECTED")
    store.db.execute(
        "UPDATE information_requests SET status = 'REDIRECTED', redirect_count = redirect_count + 1,"
        " provider_agent_id = COALESCE(?, provider_agent_id), provider_capability = COALESCE(?, provider_capability),"
        " updated_at = ? WHERE id = ?",
        (new_provider_agent_id, new_provider_capability, _now(), str(request_id)),
    )
    # REDIRECTED → ROUTED（新路由生效，状态回 ROUTED 等新接收方 ACK）
    check_transition("information_request", "REDIRECTED", "ROUTED")
    store.db.execute("UPDATE information_requests SET status = 'ROUTED', updated_at = ? WHERE id = ?", (_now(), str(request_id)))
    store.db.commit()
    _event(store, UUID(row["project_id"]), "project.information_request.redirected", actor,
           {"request_id": str(request_id), "redirect_seq": int(row["redirect_count"]) + 1,
            "new_provider_agent_id": new_provider_agent_id, "new_provider_capability": new_provider_capability},
           actor_kind="agent")
    return _request_view(_load_request(store, request_id))


def consume_information_request(store: Any, request_id: UUID, requester_agent_id: str) -> dict[str, Any]:
    """请求方确认消费回复 → 节点效果由 decide_node_effect 决定（D0 验收口径）。"""

    ensure_schema(store)
    row = _load_request(store, request_id)
    if str(row["requester_agent_id"]) != requester_agent_id:
        raise CoordinationStoreError("request_consumer_mismatch")
    check_transition("information_request", str(row["status"]), "CONSUMER_ACKNOWLEDGED")
    payload = json.loads(row["payload"] or "{}")
    response_status = str(payload.get("status") or ("answered" if row["status"] == "ANSWERED" else "provisional"))
    effect, reason = coordination.decide_node_effect(str(row["blocking"]), response_status)
    if row["status"] in {"ANSWERED", "PROVISIONAL"}:
        pass  # CONSUMER_ACKNOWLEDGED 迁移合法（DISPUTED 分支留给争议流程）
    store.db.execute(
        "UPDATE information_requests SET status = 'CONSUMER_ACKNOWLEDGED', node_effect = ?, updated_at = ? WHERE id = ?",
        (f"{effect}:{reason}" if reason else effect, _now(), str(request_id)),
    )
    requester_task_id = row["requester_task_id"]
    if effect == "BLOCKED" and requester_task_id:
        store.db.execute(
            "UPDATE tasks SET status = 'BLOCKED', blocked_reason = ?, updated_at = ? WHERE id = ?",
            (f"information_request:{request_id}", _now(), str(requester_task_id)),
        )
    store.db.commit()
    _event(store, UUID(row["project_id"]), "project.information_request.consumed", requester_agent_id,
           {"request_id": str(request_id), "node_effect": effect}, actor_kind="agent")
    view = _request_view(_load_request(store, request_id))
    view["node_effect_result"] = {"effect": effect, "reason": reason}
    return view


def expire_overdue_requests(store: Any, project_id: UUID, actor: str = "system") -> list[str]:
    """超时显式化：超过 response_deadline 的 OPEN/ROUTED/ACKNOWLEDGED → EXPIRED + 事件。"""

    ensure_schema(store)
    now = _now()
    expired: list[str] = []
    rows = store.db.execute(
        "SELECT id FROM information_requests WHERE project_id = ? AND response_deadline IS NOT NULL"
        " AND response_deadline < ? AND status IN ('OPEN', 'ROUTED', 'ACKNOWLEDGED')",
        (str(project_id), now),
    ).fetchall()
    for row in rows:
        request_id = str(row["id"])
        try:
            check_transition("information_request", "ACKNOWLEDGED", "EXPIRED")
        except CoordinationError:
            continue
        store.db.execute(
            "UPDATE information_requests SET status = 'EXPIRED', updated_at = ? WHERE id = ?", (_now(), request_id)
        )
        _event(store, project_id, "project.information_request.expired", actor,
               {"request_id": request_id, "expires_at": now}, actor_kind="system")
        expired.append(request_id)
    if expired:
        store.db.commit()
    return expired


def list_information_requests(store: Any, project_id: UUID, *, agent_id: str | None = None,
                              open_only: bool = False) -> list[dict[str, Any]]:
    ensure_schema(store)
    query = "SELECT * FROM information_requests WHERE project_id = ?"
    params: list[Any] = [str(project_id)]
    if agent_id:
        query += " AND (requester_agent_id = ? OR provider_agent_id = ?)"
        params.extend([agent_id, agent_id])
    if open_only:
        query += " AND status IN ('OPEN', 'ROUTED', 'ACKNOWLEDGED')"
    query += " ORDER BY created_at DESC LIMIT 100"
    return [_request_view(row) for row in store.db.execute(query, params).fetchall()]


# ---- 编排决策持久化（计划 §阶段 6：所有全局决策可回放）-----------------------------


def record_decision(store: Any, project_id: UUID, decision: dict[str, Any], *, actor: str = "system",
                    organization_id: str | None = None, run_id: str | None = None,
                    node_id: str | None = None, task_id: str | None = None) -> dict[str, Any]:
    ensure_schema(store)
    errors = coordination.validate_orchestration_decision(decision)
    if errors:
        raise CoordinationStoreError("orchestration_decision_invalid", errors)
    # 边界字符串化：调用方（路由/引擎）传 UUID 对象，sqlite 不认
    project_id = project_id if isinstance(project_id, UUID) else UUID(str(project_id))
    organization_id = str(organization_id) if organization_id else _project_org(store, project_id)
    actor = str(actor)
    run_id = str(run_id) if run_id else None
    node_id = str(node_id) if node_id else None
    task_id = str(task_id) if task_id else None
    decision_id = str(decision["decision_id"])
    store.db.execute(
        "INSERT INTO orchestration_decisions (id, organization_id, project_id, run_id, node_id, task_id, policy, basis, expected_events, stop_reason, payload, created_by, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            decision_id, organization_id, str(project_id), run_id, node_id, task_id,
            str(decision["policy"]), json.dumps(decision.get("basis") or [], ensure_ascii=False),
            json.dumps(decision.get("expected_events") or [], ensure_ascii=False),
            decision.get("stop_reason"), json.dumps(decision, ensure_ascii=False), actor, _now(),
        ),
    )
    store.db.commit()
    return {"decision_id": decision_id, "policy": str(decision["policy"])}


def list_decisions(store: Any, project_id: UUID, *, run_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    ensure_schema(store)
    query = "SELECT * FROM orchestration_decisions WHERE project_id = ?"
    params: list[Any] = [str(project_id)]
    if run_id:
        query += " AND run_id = ?"
        params.append(run_id)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    return [
        {
            "decision_id": str(row["id"]),
            "run_id": row["run_id"],
            "node_id": row["node_id"],
            "task_id": row["task_id"],
            "policy": str(row["policy"]),
            "basis": json.loads(row["basis"] or "[]"),
            "expected_events": json.loads(row["expected_events"] or "[]"),
            "stop_reason": row["stop_reason"],
            "payload": json.loads(row["payload"] or "{}"),
            "created_by": str(row["created_by"] or ""),
            "created_at": str(row["created_at"]),
        }
        for row in store.db.execute(query, params).fetchall()
    ]


# ---- workflow-view 聚合 DTO（可视化 V1 的数据契约，WORKFLOW_VISUALIZATION_DESIGN §5）--


def build_workflow_view(store: Any, project_id: UUID) -> dict[str, Any]:
    """项目生产流程图的只读聚合：节点/边/运行时请求/阻塞链，全部来自服务端权威对象。"""

    ensure_schema(store)
    runs = workflow_service.list_project_workflow_runs(store, project_id)
    latest_run = runs[0] if runs else None
    definition: dict[str, Any] | None = None
    node_tasks: dict[str, str] = {}
    if latest_run:
        detail = workflow_service.get_workflow_run(store, project_id, UUID(latest_run["run_id"]))
        definition = detail.get("definition")
        node_tasks = detail.get("node_tasks", {})
    tasks_by_id = {str(t.id): t for t in store.list_tasks(project_id)}

    nodes: list[dict[str, Any]] = []
    for node_id, task_id in node_tasks.items():
        task = tasks_by_id.get(task_id)
        if task is None:
            continue
        status = str(task.status.value if hasattr(task.status, "value") else task.status)
        node_def = next((n for n in (definition or {}).get("nodes", []) if n.get("id") == node_id), {})
        nodes.append(
            {
                "id": node_id,
                "kind": "task",
                "stage_id": str(node_def.get("stage_id") or task.stage or ""),
                "task_id": task_id,
                "title": task.title,
                "status": status,
                "mode": str(node_def.get("mode") or ""),
                "assignee": str(getattr(task, "assignee", "") or "") or None,
                "blocked_reason": str(getattr(task, "blocked_reason", "") or "") or None,
                "budget": node_def.get("budget"),
            }
        )
    edges: list[dict[str, Any]] = []
    for node_id, task_id in node_tasks.items():
        task = tasks_by_id.get(task_id)
        if task is None:
            continue
        for dep in task.dependency_task_ids or []:
            dep_task = tasks_by_id.get(str(dep))
            edges.append({
                "kind": "dependency", "source": str(dep), "target": task_id,
                "label": dep_task.title if dep_task else "依赖",
            })

    requests = list_information_requests(store, project_id)
    runtime_requests = [
        {
            "request_id": r["request_id"],
            "requester_node_id": r["requester_node_id"],
            "requester_agent_id": r["requester_agent_id"],
            "provider_agent_id": r["provider_agent_id"],
            "request_type": r["request_type"],
            "question": r["question"],
            "blocking": r["blocking"],
            "status": r["status"],
            "response_deadline": r["response_deadline"],
        }
        for r in requests
    ]
    blocking_chains = [
        {"root_node_id": r["requester_node_id"], "reason": f"等待信息请求回复（{r['status']}）",
         "request_id": r["request_id"]}
        for r in requests
        if r["blocking"] == "required" and r["status"] in {"OPEN", "ROUTED", "ACKNOWLEDGED"}
    ]
    summary: dict[str, int] = {}
    for node in nodes:
        summary[node["status"]] = summary.get(node["status"], 0) + 1
    return {
        "project_id": str(project_id),
        "workflow": {
            "key": (latest_run or {}).get("workflow_id"),
            "version_id": (latest_run or {}).get("workflow_version_id"),
            "state": (latest_run or {}).get("status"),
        },
        "generated_at": _now(),
        "summary": summary,
        "stages": [
            {"id": str(stage.get("id")), "title": str(stage.get("title") or stage.get("id"))}
            for stage in (definition or {}).get("stages", [])
        ],
        "nodes": nodes,
        "edges": edges,
        "runtime_requests": runtime_requests,
        "blocking_chains": blocking_chains,
        "runs": runs,
    }
