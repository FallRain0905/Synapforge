"""动词层：协议的唯一命令入口。

CLI、MCP、本地查看器都调用这里的函数——不变量只在这里强制一次（护栏二：
不允许出现第二条实现路径）。每个动词返回可 JSON 序列化的 dict，因为主要消费者是 Agent。

本层不重写协议逻辑，只做：参数整形 + 调用已有协议函数 + 错误归一。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from app import coordination_store

from .paths import db_path, state_dir

DEFAULT_LEASE_SECONDS = 900
DEFAULT_DEADLINE_MINUTES = 30


class LocalError(RuntimeError):
    """命令层错误，带稳定 code 便于 Agent 分支处理。

    **细节要进 `str()`**：Agent 通常只看得到异常文本，code 单独放在属性里等于没说。
    """

    def __init__(self, code: str, errors: list[str] | None = None) -> None:
        self.code = code
        self.errors = list(errors or [])
        detail = "; ".join(self.errors)
        super().__init__(f"{code}: {detail}" if detail else code)


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _deadline_iso(minutes: int) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _key(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:16]}"


def _dump(obj: Any) -> Any:
    return obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj


# ---- 上下文：项目与 Agent ---------------------------------------------------


def list_projects(store: Any) -> list[dict[str, Any]]:
    return [_dump(p) for p in store.list_projects()]


def ensure_project(store: Any, name: str, *, task_mode: str = "manual") -> dict[str, Any]:
    for project in store.list_projects():
        if getattr(project, "name", None) == name:
            return _dump(project)
    from app.contracts import ProjectCreate

    return _dump(store.create_project(ProjectCreate(name=name, task_mode=task_mode)))


def ensure_agent(store: Any, agent_id: str, *, display_name: str | None = None,
                 owner_member_id: str | None = None) -> dict[str, Any]:
    """注册本地 Agent，并保证它有**自己的归属成员**。

    平台按**人**归属（`member_for_agent` 读 `agents.owner_member_id`），`claim_next_task`
    用它过滤"这活是不是派给我的"。本地多个 Agent 若共用一个 owner，谁领到谁的任务全看
    运气——实测发生过（后端 Agent 顺手领走了前端任务）。所以每个 Agent 必须有自己的归属成员。
    """

    member_id = owner_member_id or agent_member_id(agent_id)
    _ensure_local_member(store, agent_id, display_name, member_id)
    # agents 表主键是 agent_id，不是 id。
    row = store.db.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
    if row is not None:
        # 补正历史归属：老数据都挂在同一个 local-owner 上，那正是要修的缺口。
        if str(row["owner_member_id"] or "") != member_id:
            store.db.execute("UPDATE agents SET owner_member_id = ? WHERE agent_id = ?",
                             (member_id, agent_id))
            store.db.commit()
        return {k: row[k] for k in row.keys()}
    from app.contracts import AgentRegister

    return _dump(store.register_agent(AgentRegister(
        agent_id=agent_id, display_name=display_name or agent_id,
        owner_member_id=member_id)))


LOCAL_MEMBER_PREFIX = "local-agent:"


def agent_member_id(agent_id: str) -> str:
    """本地 Agent 的归属成员 id（每个 Agent 一个，互不共享）。"""

    return f"{LOCAL_MEMBER_PREFIX}{agent_id}"


def _identity_ids(store: Any) -> tuple[str | None, str | None]:
    org = store.db.execute("SELECT id FROM organizations ORDER BY created_at LIMIT 1").fetchone()
    team = store.db.execute("SELECT id FROM teams ORDER BY created_at LIMIT 1").fetchone()
    return (org["id"] if org else None, team["id"] if team else None)


def _ensure_local_member(store: Any, agent_id: str, display_name: str | None,
                         member_id: str) -> None:
    """把 Agent 的归属成员落成 `human_members` 一行，避免悬空引用。

    语义说明：平台的 `member` 是"可问责的人"。本地形态里 Agent 自己就是参与方，
    这里复用该表作为**身份锚点**，不是把 Agent 说成人。
    """

    if store.db.execute("SELECT 1 FROM human_members WHERE id = ?", (member_id,)).fetchone():
        return
    org_id, team_id = _identity_ids(store)
    if not org_id:
        return  # 连组织都没有的库，归属成员无从落；注册照常进行
    store.db.execute(
        "INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'active', ?)",
        (member_id, org_id, team_id, f"{agent_id}@local.synapforge",
         display_name or agent_id, _now_iso()),
    )
    store.db.commit()


def _ensure_project_membership(store: Any, project_id: Any, member_id: str,
                               role: str = "contributor") -> dict[str, Any]:
    """`create_task` 的 `_validate_dispatch_target` 要求指派目标必须是项目成员。"""

    pid = str(_as_uuid(project_id))
    existing = store.db.execute(
        "SELECT 1 FROM project_memberships WHERE project_id = ? AND member_id = ?",
        (pid, member_id)).fetchone()
    if existing is not None:
        return {"member_id": member_id, "duplicate": True}
    store.db.execute(
        "INSERT INTO project_memberships (project_id, member_id, role, created_at)"
        " VALUES (?, ?, ?, ?)", (pid, member_id, role, _now_iso()))
    store.db.commit()
    return {"member_id": member_id, "duplicate": False}


# 本地能力集：保留能力**声明**（skill_match 派发要用），去掉令牌/到期/撤销那一层
# ——那层管的是远程不可信多方访问，本地由操作系统做信任边界（计划 D8）。
LOCAL_CAPABILITIES = [
    "chat.run", "llm.invoke",
    "task.claim", "task.lease", "task.progress", "task.result",
    "artifact.read", "artifact.write",
    "run.create", "run.complete", "run.event",
    "handoff.create", "handoff.accept", "handoff.reject",
    "review.submit",
    "workspace.files.read", "workspace.files.write", "workspace.files.claim",
]


def grant_project_access(store: Any, *, project_id: Any, agent_id: str,
                         capabilities: list[str] | None = None,
                         granted_by: str = "local") -> dict[str, Any]:
    """本地授权：写一行 `agent_project_grants`。

    `Store._assert_agent_project_access` 只认这一张表；`device_project_grants`
    （带 token_hash / expires_at / revoked_at）是远程令牌层，本地形态不使用。
    """

    caps = list(capabilities or LOCAL_CAPABILITIES)
    pid = str(_as_uuid(project_id))
    existing = store.db.execute(
        "SELECT 1 FROM agent_project_grants WHERE agent_id = ? AND project_id = ?",
        (agent_id, pid)).fetchone()
    if existing is not None:
        return {"agent_id": agent_id, "project_id": pid, "capabilities": caps, "duplicate": True}
    store.db.execute(
        "INSERT INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (agent_id, pid, json.dumps(caps, ensure_ascii=False), granted_by, _now_iso()),
    )
    store.db.commit()
    return {"agent_id": agent_id, "project_id": pid, "capabilities": caps, "duplicate": False}


def ensure_participant(store: Any, *, project_id: Any, agent_id: str,
                       display_name: str | None = None) -> dict[str, Any]:
    """注册 Agent、授予项目访问、并把它登记为项目成员——本地参与的完整前置。"""

    agent = ensure_agent(store, agent_id, display_name=display_name)
    grant = grant_project_access(store, project_id=project_id, agent_id=agent_id)
    membership = _ensure_project_membership(store, project_id, agent_member_id(agent_id))
    return {"agent": agent, "grant": grant, "membership": membership}


# ---- 事件与视图 -------------------------------------------------------------


def recent_events(store: Any, *, project_id: Any = None, limit: int = 20,
                  after_sequence: int | None = None) -> list[dict[str, Any]]:
    """持久化事件是事实源；`sequence` 单调，按它做游标。"""

    clauses: list[str] = []
    params: list[Any] = []
    if project_id:
        clauses.append("project_id = ?")
        params.append(str(project_id))
    if after_sequence is not None:
        clauses.append("sequence > ?")
        params.append(int(after_sequence))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    rows = store.db.execute(
        f"SELECT * FROM events{where} ORDER BY sequence DESC LIMIT ?", params
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in reversed(rows):
        keys = row.keys()
        payload = row["payload"] if "payload" in keys else "{}"
        import json as _json

        try:
            payload = _json.loads(payload or "{}")
        except (TypeError, ValueError):
            payload = {}
        out.append({
            "sequence": row["sequence"] if "sequence" in keys else None,
            "event_type": row["event_type"] if "event_type" in keys else None,
            "actor": row["actor"] if "actor" in keys else None,
            "actor_kind": row["actor_kind"] if "actor_kind" in keys else None,
            "project_id": row["project_id"] if "project_id" in keys else None,
            "object_type": row["object_type"] if "object_type" in keys else None,
            "object_id": row["object_id"] if "object_id" in keys else None,
            "created_at": row["created_at"] if "created_at" in keys else None,
            "payload": payload,
        })
    return out


def _task_brief(task: Any) -> dict[str, Any]:
    data = _dump(task)
    return {key: data.get(key) for key in
            ("id", "title", "status", "stage", "assignee", "blocked_reason", "updated_at")}


def status(store: Any, *, agent_id: str | None = None, project_id: Any = None,
           event_limit: int = 20) -> dict[str, Any]:
    projects = list_projects(store)
    active = str(project_id) if project_id else (projects[0]["id"] if projects else None)
    payload: dict[str, Any] = {
        "state_dir": str(state_dir()),
        "db_path": str(db_path()),
        "projects": projects,
        "project_id": active,
        "tasks": [],
        "requests": [],
        "concerns": [],
        "events": [],
    }
    if active:
        pid = _as_uuid(active)
        payload["tasks"] = [_task_brief(t) for t in store.list_tasks(pid)]
        payload["requests"] = coordination_store.list_information_requests(store, pid, agent_id=agent_id)
        payload["concerns"] = coordination_store.list_feasibility_concerns(store, pid)
        payload["events"] = recent_events(store, project_id=pid, limit=event_limit)
    return payload


def _task_row_brief(row: Any) -> dict[str, Any]:
    keys = row.keys()
    out: dict[str, Any] = {}
    for key in ("id", "title", "status", "stage", "assignee", "assignee_member_id",
                "blocked_reason", "updated_at"):
        out[key] = row[key] if key in keys else None
    return out


def agent_member_id_of(store: Any, agent_id: str) -> str:
    """Agent 的归属成员 id：以库里为准，缺失时按本地规则派生。"""

    row = store.db.execute("SELECT owner_member_id FROM agents WHERE agent_id = ?",
                           (agent_id,)).fetchone()
    if row is not None and row["owner_member_id"]:
        return str(row["owner_member_id"])
    return agent_member_id(agent_id)


def inbox(store: Any, *, agent_id: str, project_id: Any, handoff_limit: int = 50) -> dict[str, Any]:
    """待我处理的一切：派给我还没领的、我领了在跑的、要我回答的、等我消费的、待我收的交接。

    **在途任务必须带回租约令牌**：宿主驱动的 Agent（Claude Code 的 `-p`）一次性会话结束后
    进程内存就没了，新会话只能靠这里拿回 token 才能继续 finish/consume。
    平台侧不需要这个（有常驻执行体自己记着），本地形态没有常驻体，所以令牌必须可发现。

    **`assigned_to_me` 是任务指派的另一半**：光有 `assignee_member_id` 只保证了"别人领不到"，
    被指派方还得能发现"有活派给我了"。没有这一块，指派就等于把任务藏起来了。
    """

    pid = _as_uuid(project_id)
    member_id = agent_member_id_of(store, agent_id)

    all_requests = coordination_store.list_information_requests(store, pid, agent_id=agent_id)
    to_answer = [r for r in all_requests
                 if r.get("provider_agent_id") == agent_id and r.get("status") in ("OPEN", "ROUTED")]
    to_consume = [r for r in all_requests
                  if r.get("requester_agent_id") == agent_id
                  and r.get("status") in ("ANSWERED", "PROVISIONAL")]

    # 派给我、还没被任何租约占住的可领任务
    assigned_rows = store.db.execute(
        "SELECT * FROM tasks t WHERE t.project_id = ? AND t.assignee_member_id = ?"
        " AND t.status IN ('READY', 'NEEDS_REVISION')"
        " AND NOT EXISTS (SELECT 1 FROM task_leases l"
        "                 WHERE l.task_id = t.id AND l.status = 'ACTIVE')"
        " ORDER BY (t.deadline IS NULL), t.deadline ASC, t.updated_at ASC",
        (str(pid), member_id),
    ).fetchall()
    assigned_to_me = [{**_task_row_brief(row), "claimable_now": True} for row in assigned_rows]

    lease_by_task: dict[str, dict[str, Any]] = {}
    for row in store.db.execute(
        "SELECT l.task_id, l.lease_token, l.expires_at, l.agent_id FROM task_leases l"
        " JOIN tasks t ON t.id = l.task_id"
        " WHERE l.status = 'ACTIVE' AND l.agent_id = ? AND t.project_id = ?",
        (agent_id, str(pid)),
    ).fetchall():
        lease_by_task[str(row["task_id"])] = {
            "lease_token": row["lease_token"],
            "lease_expires_at": row["expires_at"],
        }

    in_flight: list[dict[str, Any]] = []
    for task in store.list_tasks(pid):
        status = str(getattr(task, "status", "")).upper()
        if status not in ("CLAIMED", "RUNNING", "WAITING"):
            continue
        brief = _task_brief(task)
        lease = lease_by_task.get(str(brief.get("id")))
        if lease is None:
            continue  # 不是我的（或租约已过期/已被别人持有）
        in_flight.append({**brief, **lease, "owned_by_me": True})

    handoffs = []
    try:
        handoffs = [_dump(h) for h in store.list_handoffs(pid)][:handoff_limit]
    except Exception:  # noqa: BLE001 — 交接列表缺失不该让收件箱整体不可用
        handoffs = []
    # 契约同步状态：让 Agent 一眼看到"我绑的版本是不是最新的"，而不是等收尾才被拦
    try:
        contract_rows = contract_status(store, project_id=pid, agent_id=agent_id)
    except Exception:  # noqa: BLE001
        contract_rows = []
    return {
        "agent_id": agent_id,
        "member_id": member_id,
        "project_id": str(pid),
        "assigned_to_me": assigned_to_me,
        "in_flight_tasks": in_flight,
        "to_answer": to_answer,
        "to_consume": to_consume,
        "contracts": contract_rows,
        "handoffs": handoffs,
    }


# ---- 任务 -------------------------------------------------------------------


def post_message(store: Any, *, project_id: Any, sender: str, text: str,
                 sender_kind: str = "agent", to_agent: str | None = None,
                 idempotency_key: str | None = None) -> dict[str, Any]:
    """往项目群聊发一条消息。

    **消息就是事件**：不另建聊天表，也不双写 `project_messages`——群聊只是事件时间线的
    一个视图，事实源永远只有 `events` 一处。
    """

    body = str(text or "").strip()
    if not body:
        raise LocalError("message_empty", ["text: 必填且非空"])
    event = store.add_event(
        _as_uuid(project_id),
        "project.message.posted",
        sender,
        {"text": body[:4000], "sender_kind": sender_kind, "to": to_agent},
        actor_kind="member" if sender_kind == "member" else "agent",
        idempotency_key=idempotency_key,
    )
    return {"sequence": event.sequence, "actor": event.actor, "to": to_agent,
            "created_at": event.created_at.isoformat()}


def broadcast_task(store: Any, *, project_id: Any, from_member: str, text: str,
                   agent_ids: list[str], title: str | None = None,
                   stage: str = "group", idempotency_key: str | None = None) -> dict[str, Any]:
    """人类在群聊里说一句话，并把它变成**每个被点名 Agent 各自的一条任务**。

    "两个 Agent 都能收到"不需要新的广播协议：点名几个就派几个任务，
    现有 `assigned_to_me` + `claim` 的归属过滤直接生效，谁也不会领错。
    """

    agents = [str(a).strip() for a in agent_ids if str(a).strip()]
    if not agents:
        raise LocalError("broadcast_needs_agents", ["agent_ids: 至少要指名一个 Agent"])
    body = str(text or "").strip()
    if not body:
        raise LocalError("message_empty", ["text: 必填且非空"])

    key = idempotency_key or _key("broadcast")
    message = post_message(store, project_id=project_id, sender=from_member, text=body,
                           sender_kind="member", idempotency_key=f"{key}:msg")
    tasks = []
    for agent_id in agents:
        task = create_task(store, project_id=project_id, title=title or body[:60],
                           description=body, stage=stage, assignee_agent_id=agent_id)
        tasks.append({"agent_id": agent_id, "task_id": task["id"], "title": task["title"]})
    return {"message": message, "tasks": tasks}


def create_task(store: Any, *, project_id: Any, title: str, description: str = "",
                stage: str = "general", assignee: str = "Unassigned",
                assignee_agent_id: str | None = None,
                acceptance_criteria: list[str] | None = None,
                required_capabilities: list[str] | None = None,
                required_contracts: list[str] | None = None,
                requires_review: bool = True) -> dict[str, Any]:
    """建任务。给定 `assignee_agent_id` 时**指派给该 Agent**，别人领不到。

    "别人领不到"不是靠命令层判断，而是写进 `assignee_member_id`，由
    `claim_next_task` 既有的归属过滤生效——一处权威，不做第二套判断。

    `required_contracts` 声明本任务必须遵守哪些契约（写进 `resource_policy.contracts`，
    不需要迁移）；收尾时校验绑定版本，不同步就拒绝 finish。
    """

    from app.contracts import TaskCreate

    assignee_member_id = None
    if assignee_agent_id:
        # 指派意味着它要参与：顺手把注册/授权/项目成员补齐，避免"指派给一个还不在项目里的人"
        ensure_participant(store, project_id=project_id, agent_id=assignee_agent_id)
        assignee_member_id = agent_member_id(assignee_agent_id)

    resource_policy: dict[str, Any] = {}
    contracts = [str(c).strip() for c in (required_contracts or []) if str(c).strip()]
    if contracts:
        resource_policy["contracts"] = contracts

    task = store.create_task(_as_uuid(project_id), TaskCreate(
        title=title,
        description=description,
        stage=stage,
        assignee=assignee_agent_id or assignee,
        assignee_member_id=assignee_member_id,
        acceptance_criteria=list(acceptance_criteria or []),
        required_capabilities=list(required_capabilities or []),
        requires_review=requires_review,
        resource_policy=resource_policy,
    ))
    return _dump(task)


def claim(store: Any, *, project_id: Any, agent_id: str,
          lease_seconds: int = DEFAULT_LEASE_SECONDS, stages: list[str] | None = None,
          idempotency_key: str | None = None) -> dict[str, Any]:
    from app.contracts import TaskClaimRequest

    result = store.claim_next_task(
        TaskClaimRequest(agent_id=agent_id, lease_seconds=lease_seconds,
                         idempotency_key=idempotency_key or _key("claim")),
        _as_uuid(project_id),
        list(stages) if stages else None,
    )
    if result is None:
        return {"claimed": False, "reason": "no_ready_task"}
    task, lease = result
    return {"claimed": True, "task": _dump(task), "lease": _dump(lease)}


def heartbeat(store: Any, *, lease_token: str, agent_id: str,
              extend_seconds: int = DEFAULT_LEASE_SECONDS) -> dict[str, Any]:
    return {"lease": _dump(store.heartbeat_lease(lease_token, agent_id, extend_seconds))}


def mark_running(store: Any, *, task_id: Any, agent_id: str, lease_token: str,
                 message: str = "") -> dict[str, Any]:
    from app.contracts import TaskProgressRequest

    task = store.update_task_progress(_as_uuid(task_id), TaskProgressRequest(
        agent_id=agent_id, lease_token=lease_token, status="RUNNING",
        message=message, idempotency_key=_key("progress")))
    return _dump(task)


def finish_task(store: Any, *, task_id: Any, agent_id: str, lease_token: str,
                success: bool, summary: str,
                output_artifact_ids: list[str] | None = None) -> dict[str, Any]:
    """收尾前**契约闸门**：声明的必需契约必须已绑定且与最新版一致。

    这里是"能自动跑"和"能自动跑对"的分界：契约一改，绑在旧版上的 Agent 就收不了尾，
    必须重新读、重新 bind——绑定动作本身就是"我重新看过了"的证明。
    """

    from app.contracts import TaskResultSubmit

    from . import contracts as contract_store

    required = _required_contracts(store, task_id)
    if required:
        problems = contract_store.check_drift(store, project_id=_task_project(store, task_id),
                                             agent_id=agent_id, keys=required)
        if problems:
            raise LocalError("contract_drift", [
                f"{p['contract_key']}: {p['problem']}"
                + (f"（绑定 v{p.get('bound_version')} / 最新 v{p.get('latest_version')}）"
                   if p.get("problem") == "drifted" else "")
                for p in problems])

    result = store.submit_task_result(_as_uuid(task_id), TaskResultSubmit(
        agent_id=agent_id,
        lease_token=lease_token,
        success=success,
        summary=summary,
        output_artifact_ids=[_as_uuid(a) for a in (output_artifact_ids or [])],
        idempotency_key=_key("result"),
    ))
    return {"task": _dump(result.task), "accepted": result.accepted, "message": result.message}


def _task_project(store: Any, task_id: Any) -> Any:
    row = store.db.execute("SELECT project_id FROM tasks WHERE id = ?",
                           (str(task_id),)).fetchone()
    return row["project_id"] if row else None


def submit_report(store: Any, *, project_id: Any, task_id: Any, agent_id: str,
                  report: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    return coordination_store.submit_stage_report(
        store, _as_uuid(project_id), _as_uuid(task_id), agent_id, report,
        run_id=run_id, idempotency_key=_key("report"))


# ---- 成果物 -----------------------------------------------------------------


def list_artifacts(store: Any, *, project_id: Any) -> list[dict[str, Any]]:
    from .artifacts import artifact_brief

    return [artifact_brief(item) for item in store.list_artifacts(_as_uuid(project_id))]


def read_artifact(store: Any, *, artifact_id: Any) -> dict[str, Any]:
    """读回成果物内容。二进制内容用 base64，文本直接给字符串——不猜，按 mime 判。"""

    import base64

    from .artifacts import artifact_brief, read_artifact_bytes

    artifact = store.get_artifact(_as_uuid(artifact_id))
    content = read_artifact_bytes(store, artifact_id)
    brief = artifact_brief(artifact)
    mime = str(getattr(artifact, "mime_type", "") or "")
    is_text = mime.startswith("text/") or mime in ("application/json", "application/xml") or not mime
    payload: dict[str, Any] = {**brief, "size_bytes": len(content), "mime_type": mime,
                               "text": content.decode("utf-8", errors="replace") if is_text else None}
    if not is_text:
        payload["base64"] = base64.b64encode(content).decode("ascii")
    return payload


def publish_artifact(store: Any, *, project_id: Any, workspace: Any, relative_path: str,
                     agent_id: str, task_id: Any = None,
                     artifact_type: str | None = None) -> dict[str, Any]:
    """把一个工作区文件登记成成果物（带 receipt）。"""

    from .artifacts import write_artifact

    return write_artifact(store, project_id=project_id, workspace=workspace,
                          relative_path=relative_path, created_by=agent_id,
                          task_id=task_id, artifact_type=artifact_type)


# ---- 契约：版本化的接口约定 ------------------------------------------------


def publish_contract(store: Any, *, project_id: Any, key: str, body: Any,
                     agent_id: str) -> dict[str, Any]:
    """发布契约新版本。正文落成 artifact（内容哈希 + receipt），身份交给 contracts 表。"""

    from app.contracts import ArtifactCreate

    from . import contracts as contract_store
    from .artifacts import receipt_for_bytes

    if not isinstance(body, (dict, list)):
        raise LocalError("contract_body_invalid", ["body: 必须是 JSON 对象或数组"])
    label = str(key or "").strip()
    if not label:
        raise LocalError("contract_key_required", ["key: 必填"])

    content = contract_store.canonical_bytes(body)
    digest = contract_store.content_hash(body)
    receipt = receipt_for_bytes(tool_name="contract_publish",
                                tool_call_id=f"{label}@{agent_id}",
                                args={"contract_key": label}, content=content)
    artifact = store.create_artifact(
        _as_uuid(project_id),
        ArtifactCreate(name=f"contract-{label}.json", artifact_type="model_spec",
                       description=f"契约 {label}", status="PENDING_REVIEW", receipt=receipt),
        created_by=agent_id, created_by_kind="agent")
    # 内容哈希对账：publish 记的 hash 必须等于落盘字节的 hash
    store.store_artifact_content(artifact.id, content, "application/json", expected_hash=digest)
    return contract_store.publish(store, project_id=project_id, key=label, body=body,
                                  artifact_id=artifact.id, published_by=agent_id)


def show_contract(store: Any, *, project_id: Any, key: str) -> dict[str, Any]:
    """读当前版本：身份 + 正文。Agent 绑定之前必须读它。"""

    from . import contracts as contract_store

    current = contract_store.latest(store, project_id, key)
    if current is None:
        raise LocalError("contract_not_found", [f"{key}: 还没有发布过契约"])
    content = store.get_artifact_content(_as_uuid(current["artifact_id"]))
    body = json.loads(content.decode("utf-8")) if content else None
    return {"contract_key": str(current["contract_key"]), "version": int(current["version"]),
            "content_hash": str(current["content_hash"]), "published_by": current["published_by"],
            "body": body}


def bind_contract(store: Any, *, project_id: Any, key: str, agent_id: str,
                  version: int | None = None, task_id: Any = None) -> dict[str, Any]:
    """声明"我按这一版实现"。只能绑当前版本——绑旧版等于明知不同步还继续。"""

    from . import contracts as contract_store

    return contract_store.bind(store, project_id=project_id, key=key, agent_id=agent_id,
                               version=version, task_id=task_id)


def contract_status(store: Any, *, project_id: Any, agent_id: str,
                    keys: list[str] | None = None) -> list[dict[str, Any]]:
    from . import contracts as contract_store

    return contract_store.status_for_agent(store, project_id=project_id,
                                           agent_id=agent_id, keys=keys)


def _required_contracts(store: Any, task_id: Any) -> list[str]:
    """任务声明了哪些必需契约（存在 `resource_policy.contracts`，无需迁移）。"""

    row = store.db.execute("SELECT resource_policy FROM tasks WHERE id = ?",
                           (str(task_id),)).fetchone()
    if row is None:
        return []
    try:
        policy = json.loads(row["resource_policy"] or "{}")
    except (TypeError, ValueError):
        return []
    values = policy.get("contracts") if isinstance(policy, dict) else None
    return [str(v) for v in values] if isinstance(values, list) else []


# ---- 协作：信息请求 ---------------------------------------------------------


def ask(store: Any, *, project_id: Any, agent_id: str, provider_agent_id: str,
        request_type: str, question: str, reason: str, blocking: str = "required",
        deadline_minutes: int = DEFAULT_DEADLINE_MINUTES, task_id: Any = None,
        idempotency_key: str | None = None) -> dict[str, Any]:
    """向另一个 Agent 请求信息。

    `blocking=required` 时节点**真的**进 WAITING（不是只记一句备注）——本地没有常驻
    引擎循环，效果必须由命令层显式施加。
    """

    body = {
        "requester_task_id": str(task_id) if task_id else None,
        "provider_agent_id": provider_agent_id,
        "idempotency_key": idempotency_key or _key("ask"),
        "request": {
            "request_type": request_type,
            "question": question,
            "blocking": blocking,
            "reason": reason,
            "response_deadline": _deadline_iso(deadline_minutes) if blocking == "required" else None,
        },
    }
    return coordination_store.create_information_request(store, _as_uuid(project_id), agent_id, body)


def answer(store: Any, *, request_id: Any, agent_id: str, response: dict[str, Any],
           ack_outcome: str = "received") -> dict[str, Any]:
    """回答请求。OPEN/ROUTED 先 ACK（迁移表要求 ACKNOWLEDGED 才能转 ANSWERED）。"""

    rid = _as_uuid(request_id)
    view = coordination_store.get_request(store, rid)
    if view.get("status") in ("OPEN", "ROUTED"):
        coordination_store.ack_information_request(store, rid, agent_id, ack_outcome)
    return coordination_store.respond_information_request(store, rid, agent_id, response)


def consume(store: Any, *, request_id: Any, agent_id: str) -> dict[str, Any]:
    return coordination_store.consume_information_request(store, _as_uuid(request_id), agent_id)


def expire_requests(store: Any, *, project_id: Any, actor: str = "local") -> list[str]:
    return coordination_store.expire_overdue_requests(store, _as_uuid(project_id), actor)


# ---- 协作：交接 -------------------------------------------------------------


def create_handoff(store: Any, *, project_id: Any, task_id: Any, sender_agent_id: str,
                   objective: str, receiver: Any = "Team",
                   completed: list[str] | None = None,
                   open_questions: list[str] | None = None,
                   evidence_refs: list[str] | None = None,
                   next_actions: list[str] | None = None,
                   output_artifacts: list[str] | None = None,
                   requires_human_approval: bool = False) -> dict[str, Any]:
    from app.contracts import HandoffCreate

    data = HandoffCreate(
        task_id=_as_uuid(task_id),
        receiver=receiver,
        objective=objective,
        completed=list(completed or []),
        open_questions=list(open_questions or []),
        evidence_refs=list(evidence_refs or []),
        next_actions=list(next_actions or []),
        output_artifacts=list(output_artifacts or []),
        requires_human_approval=requires_human_approval,
    )
    return _dump(store.create_handoff(_as_uuid(project_id), data, sender_agent_id))


def accept_handoff(store: Any, *, handoff_id: Any, receiver: str) -> dict[str, Any]:
    return _dump(store.accept_handoff(_as_uuid(handoff_id), receiver))


def reject_handoff(store: Any, *, handoff_id: Any, actor: str, reason: str,
                   findings: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return _dump(store.reject_handoff(_as_uuid(handoff_id), actor, reason, findings))


def list_handoffs(store: Any, *, project_id: Any) -> list[dict[str, Any]]:
    return [_dump(h) for h in store.list_handoffs(_as_uuid(project_id))]


# ---- 诚实停止 ---------------------------------------------------------------


def raise_concern(store: Any, *, project_id: Any, agent_id: str, concern_type: str,
                  claim: str, basis: list[str], recommendation: str = "request_information",
                  can_continue_safely: bool = False, impact_current_node: str = "blocked",
                  task_id: Any = None, note: str = "") -> dict[str, Any]:
    impact: dict[str, Any] = {"current_node": impact_current_node}
    if note:
        impact["note"] = note
    concern = {
        "concern_type": concern_type,
        "claim": claim,
        "basis": list(basis),
        "impact": impact,
        "recommendation": recommendation,
        "can_continue_safely": can_continue_safely,
    }
    return coordination_store.raise_feasibility_concern(
        store, _as_uuid(project_id), agent_id, concern,
        task_id=str(task_id) if task_id else None)


def decide_concern(store: Any, *, concern_id: Any, policy: str, rationale: str,
                   basis: list[str], expected_events: list[str],
                   actor: str = "local-orchestrator",
                   stop_reason: str | None = None) -> dict[str, Any]:
    decision = {
        "decision_id": f"dec-concern-{str(concern_id)[:8]}",
        "policy": policy,
        "rationale": rationale,
        "basis": list(basis),
        "created_at": _now_iso(),
        "expected_events": list(expected_events),
    }
    if stop_reason:
        decision["stop_reason"] = stop_reason
    return coordination_store.decide_feasibility_concern(
        store, _as_uuid(concern_id), actor, decision)
