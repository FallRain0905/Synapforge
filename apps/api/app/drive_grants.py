"""云盘文件访问授权（FM-5）：Grant / Grant Node / Lease，以及 Agent 专用读取。

## 为什么是独立一套（不复用项目能力令牌）

项目能力令牌回答"**这台设备能不能替这个项目干活**"，不回答"**它能读我云盘里的哪些文件**"。
云盘是成员私有资产，Agent 默认只应看到被显式授权的那一小块。计划 §1 D1/D2 已拍板：

- Agent 默认能力只有 `drive.metadata.read` / `drive.file.read` / `drive.file.import`
  ——**没有**删除、移动、改名、覆盖；
- 授权粒度：单文件 / 文件夹 / 用户明确选择的整盘；文件夹授权默认只覆盖**授权当时**的节点，
  勾了"包含以后新增"才动态覆盖；
- 授权必须绑定成员、Agent、设备、项目（以及可选的任务/Run/对话轮次），带过期时间与撤销版本。

## 撤销怎么联动（不靠删行）

撤销 = `revocation_epoch + 1` + 记 `revoked_at/by/reason`。所有校验都比对 epoch（lease 记住签发时的
epoch）。于是设备撤销、成员被移出项目、令牌轮换这些操作只要推高 epoch，"撤销后新读取立即失败"就是
自然结果，而不是靠一堆 if 分支去补。

## 审计

每次允许/拒绝都写 `drive_audit`（FM-1 那张表，字段口径不变）：谁、哪个 Agent/设备、为哪个项目/Run、
读了哪个节点、用的是哪个 grant/lease、允许还是拒绝、为什么。
"""

from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator
from uuid import uuid4

# 默认能力（计划 §1 D1）：只读 + 可导入项目；没有写/删/移动
DEFAULT_CAPABILITIES = ("drive.metadata.read", "drive.file.read", "drive.file.import")
ALLOWED_CAPABILITIES = frozenset(
    {
        "drive.metadata.read",
        "drive.file.read",
        "drive.file.import",
        "drive.file.write",
        "drive.file.delete",
        "drive.file.move",
        "drive.file.rename",
        "drive.file.copy_to_drive",
    }
)
# 成员**允许授予**的上限：第一期只开放只读与导入（写权限还没实现，授了也没用，不如不给）
GRANTABLE_CAPABILITIES = frozenset({"drive.metadata.read", "drive.file.read", "drive.file.import"})
MAX_GRANT_TTL_SECONDS = 30 * 24 * 3600
DEFAULT_GRANT_TTL_SECONDS = 7 * 24 * 3600
DEFAULT_LEASE_TTL_SECONDS = 900
MAX_LEASE_TTL_SECONDS = 3600


class GrantError(RuntimeError):
    """稳定错误族（计划 §6.5 的词表）。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.detail = detail or ""


@contextmanager
def _transaction(store: Any) -> Iterator[None]:
    try:
        yield
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _field(row: Any, name: str, default: Any = None) -> Any:
    try:
        value = row[name]
    except (KeyError, IndexError):
        return default
    return default if value is None else value


_SQLITE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS file_access_grants (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        owner_member_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        device_id TEXT,
        project_id TEXT NOT NULL,
        task_id TEXT,
        run_id TEXT,
        conversation_id TEXT,
        turn_id TEXT,
        scope_type TEXT NOT NULL,
        root_node_id TEXT,
        include_future_nodes INTEGER NOT NULL DEFAULT 0,
        capabilities TEXT NOT NULL DEFAULT '[]',
        expires_at TEXT NOT NULL,
        revocation_epoch INTEGER NOT NULL DEFAULT 1,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        revoked_at TEXT,
        revoked_by TEXT,
        revoke_reason TEXT,
        renewed_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS file_access_grants_owner_idx ON file_access_grants(owner_member_id, created_at)",
    "CREATE INDEX IF NOT EXISTS file_access_grants_agent_idx ON file_access_grants(agent_id, expires_at)",
    """
    CREATE TABLE IF NOT EXISTS file_access_grant_nodes (
        id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL REFERENCES file_access_grants(id),
        drive_node_id TEXT NOT NULL,
        content_hash TEXT,
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS file_access_grant_nodes_unique_idx ON file_access_grant_nodes(grant_id, drive_node_id)",
    "CREATE INDEX IF NOT EXISTS file_access_grant_nodes_node_idx ON file_access_grant_nodes(drive_node_id)",
    """
    CREATE TABLE IF NOT EXISTS file_access_leases (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        grant_id TEXT NOT NULL REFERENCES file_access_grants(id),
        agent_id TEXT NOT NULL,
        device_id TEXT,
        project_id TEXT NOT NULL,
        run_id TEXT,
        token_hash TEXT NOT NULL,
        revocation_epoch INTEGER NOT NULL DEFAULT 1,
        expires_at TEXT NOT NULL,
        used_at TEXT,
        revoked_at TEXT,
        created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS file_access_leases_token_idx ON file_access_leases(token_hash)",
    "CREATE INDEX IF NOT EXISTS file_access_leases_grant_idx ON file_access_leases(grant_id, expires_at)",
)


def _schema_ready(store: Any) -> bool:
    try:
        return store.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'file_access_grants'"
        ).fetchone() is not None
    except Exception:  # noqa: BLE001 - PG 由迁移建表
        return True


def ensure_schema(store: Any) -> None:
    if _schema_ready(store):
        return
    for statement in _SQLITE_SCHEMA:
        store.db.execute(statement)
    store.db.commit()


def _with_schema(function: Any) -> Any:
    def wrapper(store: Any, *args: Any, **kwargs: Any) -> Any:
        ensure_schema(store)
        return function(store, *args, **kwargs)

    wrapper.__name__ = getattr(function, "__name__", "wrapper")
    return wrapper


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _row_to_grant(row: Any) -> dict[str, Any]:
    value = dict(row)
    value["capabilities"] = json.loads(_field(row, "capabilities", "[]") or "[]")
    value["id"] = str(value["id"])
    return value


def _audit(
    store: Any,
    *,
    organization_id: str,
    member_id: str | None,
    agent_id: str | None,
    action: str,
    node_id: str | None = None,
    capability: str = "",
    decision: str = "allow",
    reason: str = "",
    grant_id: str | None = None,
) -> None:
    """写云盘审计（复用 FM-1 的 `drive_audit` 表：字段口径一致，不另造一套）。"""

    store.db.execute(
        """
        INSERT INTO drive_audit
            (id, organization_id, member_id, node_id, parent_id, action, capability, decision, reason,
             name_hash, content_hash, size_bytes, revision, created_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, NULL, NULL, 0, NULL, ?)
        """,
        (str(uuid4()), organization_id, member_id or f"agent:{agent_id}", node_id, action, capability, decision, reason or (grant_id or ""), _now()),
    )


# ---- 授权范围（snapshot / 动态） ---------------------------------------------


def _descendant_ids(store: Any, root_id: str) -> list[str]:
    rows = store.db.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM drive_nodes WHERE id = ?
            UNION ALL
            SELECT n.id FROM drive_nodes n JOIN subtree s ON n.parent_id = s.id
        )
        SELECT id FROM subtree
        """,
        (str(root_id),),
    ).fetchall()
    return [str(row["id"]) for row in rows]


def _ancestor_ids(store: Any, node_id: str) -> list[str]:
    chain: list[str] = []
    current = str(node_id)
    seen: set[str] = set()
    while current and current not in seen:
        seen.add(current)
        chain.append(current)
        row = store.db.execute("SELECT parent_id FROM drive_nodes WHERE id = ?", (current,)).fetchone()
        current = str(row["parent_id"]) if row and row["parent_id"] else ""
    return chain


@_with_schema
def grant_node_ids(store: Any, grant: dict[str, Any]) -> set[str]:
    """这个 Grant 覆盖哪些节点。

    - `file`：只看那一个节点（授权行里就它自己）；
    - `folder`：默认用**授权时的快照**（`include_future_nodes=false`）；勾了"包含以后新增"就按
      祖先链动态判断（文件在一棵被授权子树里就算覆盖）。
    """

    if grant.get("include_future_nodes"):
        return set()  # 动态模式：靠祖先链判断，不用快照集合
    rows = store.db.execute(
        "SELECT drive_node_id FROM file_access_grant_nodes WHERE grant_id = ?", (str(grant["id"]),)
    ).fetchall()
    return {str(row["drive_node_id"]) for row in rows}


def _snapshot_nodes(store: Any, actor_owner: str, node_id: str, scope_type: str) -> list[dict[str, Any]]:
    """把授权范围内的节点做成快照（含 content_hash 与 revision：物化时按它校验）。"""

    ids = [str(node_id)] if scope_type == "file" else _descendant_ids(store, node_id)
    rows = store.db.execute(
        f"SELECT id, kind, content_hash, revision FROM drive_nodes WHERE id IN ({','.join('?' for _ in ids)}) AND purged_at IS NULL",
        tuple(ids),
    ).fetchall()
    return [{"drive_node_id": str(row["id"]), "content_hash": row["content_hash"], "revision": int(row["revision"] or 1)} for row in rows]


def _covered(store: Any, grant: dict[str, Any], node_id: str) -> bool:
    """节点是否落在授权范围内（含动态模式）。"""

    if grant.get("include_future_nodes"):
        root = grant.get("root_node_id")
        if not root:
            return False
        return str(root) in _ancestor_ids(store, node_id)
    return str(node_id) in grant_node_ids(store, grant)


# ---- Grant 管理（人类侧） ----------------------------------------------------


@_with_schema
def create_grant(
    store: Any,
    actor: Any,
    *,
    owner_drive_actor: Any,
    node_id: str | None,
    agent_id: str,
    device_id: str | None,
    project_id: str,
    scope_type: str,
    capabilities: list[str] | None = None,
    include_future_nodes: bool = False,
    task_id: str | None = None,
    run_id: str | None = None,
    conversation_id: str | None = None,
    turn_id: str | None = None,
    expires_in_seconds: int = DEFAULT_GRANT_TTL_SECONDS,
) -> dict[str, Any]:
    """建一条授权。创建时把"谁能被授权、授多大、绑谁"全部校验一遍（计划 §6.2）。"""

    from . import drive

    if scope_type not in {"file", "folder", "drive"}:
        raise GrantError("file_access_scope_invalid", scope_type)
    wanted = list(capabilities or DEFAULT_CAPABILITIES)
    if not wanted:
        raise GrantError("file_access_capability_required")
    forbidden = sorted(set(wanted) - GRANTABLE_CAPABILITIES)
    if forbidden:
        # 写权限（删除/移动/改名/覆盖）一律不给：第一期还没实现，授了也是空头承诺
        raise GrantError("file_access_capability_denied", ",".join(forbidden))
    ttl = int(expires_in_seconds or DEFAULT_GRANT_TTL_SECONDS)
    if ttl <= 0 or ttl > MAX_GRANT_TTL_SECONDS:
        raise GrantError("file_access_expiry_invalid", str(ttl))

    # ① 节点必须是**当前成员**的云盘节点（跨成员直接拒）
    root_node = None
    if scope_type == "drive":
        root_node = drive.root_node(store, owner_drive_actor)
        node_id = str(root_node["id"])
    else:
        if not node_id:
            raise GrantError("file_access_scope_required")
        node = drive.get_node(store, owner_drive_actor, str(node_id))
        if scope_type == "file" and node["kind"] != "file":
            raise GrantError("file_access_scope_invalid", "not_a_file")
        if scope_type == "folder" and node["kind"] != "directory":
            raise GrantError("file_access_scope_invalid", "not_a_directory")
        node_id = str(node["id"])

    # ② Agent 与设备存在，且设备对该项目有授权（否则给了文件也没用）
    agent_row = store.db.execute("SELECT owner_member_id FROM agents WHERE agent_id = ?", (str(agent_id),)).fetchone()
    if agent_row is None:
        raise GrantError("agent_not_found")
    if device_id:
        device_row = store.db.execute("SELECT status FROM devices WHERE device_id = ?", (str(device_id),)).fetchone()
        if device_row is None:
            raise GrantError("device_not_found")
        if str(device_row["status"]) != "active":
            raise GrantError("device_revoked")
        grant_row = store.db.execute(
            "SELECT 1 FROM device_project_grants WHERE device_id = ? AND project_id = ? AND revoked_at IS NULL",
            (str(device_id), str(project_id)),
        ).fetchone()
        if grant_row is None:
            raise GrantError("device_project_grant_invalid")

    # ③ 成员属于该项目；任务/Run/对话都属于同一个项目（绑定关系不能串项目）
    try:
        store.authorize_member(__import__("uuid").UUID(str(project_id)), actor.member_id, "project.view")
    except Exception as error:  # noqa: BLE001
        raise GrantError("project_membership_required", str(error)) from error
    for value, label, table, column in (
        (task_id, "task", "tasks", "id"),
        (run_id, "run", "runs", "id"),
        (conversation_id, "conversation", "agent_conversations", "id"),
        (turn_id, "turn", "agent_turns", "id"),
    ):
        if not value:
            continue
        row = store.db.execute(f"SELECT project_id FROM {table} WHERE {column} = ?", (str(value),)).fetchone()
        if row is None:
            raise GrantError("file_access_binding_invalid", f"{label}_not_found")
        if str(row["project_id"]) != str(project_id):
            raise GrantError("file_access_binding_invalid", f"{label}_project_mismatch")

    grant_id = str(uuid4())
    timestamp = _now()
    expires = (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()
    with _transaction(store):
        store.db.execute(
            """
            INSERT INTO file_access_grants
                (id, organization_id, owner_member_id, agent_id, device_id, project_id, task_id, run_id,
                 conversation_id, turn_id, scope_type, root_node_id, include_future_nodes, capabilities,
                 expires_at, revocation_epoch, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                grant_id,
                actor.organization_id,
                actor.member_id,
                str(agent_id),
                device_id,
                str(project_id),
                task_id,
                run_id,
                conversation_id,
                turn_id,
                scope_type,
                str(node_id),
                int(bool(include_future_nodes)),
                json.dumps(wanted, ensure_ascii=False),
                expires,
                actor.member_id,
                timestamp,
            ),
        )
        for entry in _snapshot_nodes(store, actor.member_id, str(node_id), scope_type):
            store.db.execute(
                "INSERT INTO file_access_grant_nodes (id, grant_id, drive_node_id, content_hash, revision, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (str(uuid4()), grant_id, entry["drive_node_id"], entry["content_hash"], entry["revision"], timestamp),
            )
        _audit(
            store,
            organization_id=actor.organization_id,
            member_id=actor.member_id,
            agent_id=str(agent_id),
            action="grant:create",
            node_id=str(node_id),
            capability=",".join(wanted),
            grant_id=grant_id,
        )
    return get_grant(store, actor, grant_id)


@_with_schema
def get_grant(store: Any, actor: Any, grant_id: str) -> dict[str, Any]:
    """取一条授权。**只按 owner 收口**：云盘是成员私有资产，"同组织"不等于"你能看别人的授权"
    （平台其他地方按组织收口，这里不能照抄——照抄会让任何同事看到你把哪些文件授权给了哪台设备）。
    """

    row = store.db.execute(
        "SELECT * FROM file_access_grants WHERE id = ? AND organization_id = ? AND owner_member_id = ?",
        (str(grant_id), actor.organization_id, actor.member_id),
    ).fetchone()
    if row is None:
        raise GrantError("file_access_grant_not_found")
    grant = _row_to_grant(row)
    grant["node_ids"] = sorted(grant_node_ids(store, grant))
    lease_rows = store.db.execute(
        "SELECT id, agent_id, device_id, expires_at, used_at, revoked_at FROM file_access_leases WHERE grant_id = ? ORDER BY created_at DESC LIMIT 20",
        (str(grant_id),),
    ).fetchall()
    grant["leases"] = [dict(item) for item in lease_rows]
    return grant


@_with_schema
def list_grants(store: Any, actor: Any, *, node_id: str | None = None, agent_id: str | None = None) -> list[dict[str, Any]]:
    """列出**我发出的**授权（owner 收口，同上）。Agent 侧接口不经过这里。"""

    clauses = ["organization_id = ?", "owner_member_id = ?"]
    params: list[Any] = [actor.organization_id, actor.member_id]
    if agent_id:
        clauses.append("agent_id = ?")
        params.append(str(agent_id))
    rows = store.db.execute(
        f"SELECT * FROM file_access_grants WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 200",
        tuple(params),
    ).fetchall()
    grants = [_row_to_grant(row) for row in rows]
    if node_id:
        # 只列"覆盖到这个节点"的（详情抽屉就是这么用的）
        grants = [grant for grant in grants if _covered(store, grant, str(node_id)) or str(grant.get("root_node_id")) == str(node_id)]
    for grant in grants:
        grant["node_count"] = len(grant_node_ids(store, grant))
        grant["active"] = not grant.get("revoked_at") and (_parse_time(grant.get("expires_at")) or datetime.now(UTC)) > datetime.now(UTC)
    return grants


@_with_schema
def revoke_grant(store: Any, actor: Any, grant_id: str, *, reason: str = "revoked_by_member") -> dict[str, Any]:
    """撤销：epoch +1（不是删行），并把该 Grant 下所有 lease 一并作废。"""

    grant = get_grant(store, actor, grant_id)
    if grant.get("revoked_at"):
        return grant
    timestamp = _now()
    with _transaction(store):
        store.db.execute(
            "UPDATE file_access_grants SET revoked_at = ?, revoked_by = ?, revoke_reason = ?, revocation_epoch = revocation_epoch + 1 WHERE id = ?",
            (timestamp, actor.member_id, reason[:200], str(grant_id)),
        )
        store.db.execute(
            "UPDATE file_access_leases SET revoked_at = ?, revocation_epoch = revocation_epoch + 1 WHERE grant_id = ? AND revoked_at IS NULL",
            (timestamp, str(grant_id)),
        )
        _audit(
            store,
            organization_id=actor.organization_id,
            member_id=actor.member_id,
            agent_id=str(grant["agent_id"]),
            action="grant:revoke",
            node_id=str(grant.get("root_node_id") or ""),
            capability=",".join(grant.get("capabilities") or []),
            decision="deny",
            reason=reason,
            grant_id=str(grant_id),
        )
    return get_grant(store, actor, grant_id)


@_with_schema
def renew_grant(store: Any, actor: Any, grant_id: str, *, expires_in_seconds: int = DEFAULT_GRANT_TTL_SECONDS) -> dict[str, Any]:
    grant = get_grant(store, actor, grant_id)
    if grant.get("revoked_at"):
        raise GrantError("file_access_grant_revoked")
    ttl = int(expires_in_seconds or DEFAULT_GRANT_TTL_SECONDS)
    if ttl <= 0 or ttl > MAX_GRANT_TTL_SECONDS:
        raise GrantError("file_access_expiry_invalid", str(ttl))
    expires = (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()
    with _transaction(store):
        store.db.execute(
            "UPDATE file_access_grants SET expires_at = ?, renewed_at = ? WHERE id = ?", (expires, _now(), str(grant_id))
        )
        store.db.execute(
            "UPDATE file_access_leases SET revoked_at = COALESCE(revoked_at, ?) WHERE grant_id = ? AND expires_at < ?",
            (_now(), str(grant_id), expires),
        )
    return get_grant(store, actor, grant_id)


@_with_schema
def revoke_grants_for(store: Any, *, device_id: str | None = None, member_id: str | None = None, task_id: str | None = None, agent_id: str | None = None) -> int:
    """联动撤销：设备撤销、成员移除、任务结束、Agent 注销时把相关授权一并作废。

    返回受影响条数。**不删行**——留着才能回答"这份文件当时授权给了谁"（审计要求）。
    """

    clauses: list[str] = ["revoked_at IS NULL"]
    params: list[Any] = []
    for column, value in (("device_id", device_id), ("owner_member_id", member_id), ("task_id", task_id), ("agent_id", agent_id)):
        if value:
            clauses.append(f"{column} = ?")
            params.append(str(value))
    if len(clauses) == 1:
        return 0
    timestamp = _now()
    rows = store.db.execute(f"SELECT id FROM file_access_grants WHERE {' AND '.join(clauses)}", tuple(params)).fetchall()
    for row in rows:
        with _transaction(store):
            store.db.execute(
                "UPDATE file_access_grants SET revoked_at = ?, revoked_by = 'system', revoke_reason = ?, revocation_epoch = revocation_epoch + 1 WHERE id = ?",
                (timestamp, "revoked_by_coupling", str(row["id"])),
            )
            store.db.execute(
                "UPDATE file_access_leases SET revoked_at = ?, revocation_epoch = revocation_epoch + 1 WHERE grant_id = ? AND revoked_at IS NULL",
                (timestamp, str(row["id"])),
            )
    return len(rows)


# ---- Lease 与 Agent 读取 -----------------------------------------------------


@_with_schema
def exchange_lease(
    store: Any,
    *,
    grant_id: str,
    agent_id: str,
    device_id: str | None,
    project_id: str,
    run_id: str | None = None,
    ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> dict[str, Any]:
    """用项目能力令牌 + Agent/设备身份换一个**短期** lease（明文只返回这一次）。"""

    row = store.db.execute("SELECT * FROM file_access_grants WHERE id = ?", (str(grant_id),)).fetchone()
    if row is None:
        raise GrantError("file_access_grant_not_found")
    grant = _row_to_grant(row)
    if grant.get("revoked_at"):
        raise GrantError("file_access_grant_revoked")
    if _parse_time(grant.get("expires_at")) and _parse_time(grant.get("expires_at")) <= datetime.now(UTC):
        raise GrantError("file_access_grant_expired")
    if str(grant["agent_id"]) != str(agent_id):
        raise GrantError("file_access_agent_mismatch")
    if grant.get("device_id") and device_id and str(grant["device_id"]) != str(device_id):
        raise GrantError("file_access_device_mismatch")
    if str(grant["project_id"]) != str(project_id):
        raise GrantError("file_access_project_mismatch")
    if grant.get("run_id") and run_id and str(grant["run_id"]) != str(run_id):
        raise GrantError("file_access_run_mismatch")
    if device_id:
        device_row = store.db.execute("SELECT status FROM devices WHERE device_id = ?", (str(device_id),)).fetchone()
        if device_row is None or str(device_row["status"]) != "active":
            raise GrantError("device_revoked")

    ttl = max(60, min(int(ttl_seconds or DEFAULT_LEASE_TTL_SECONDS), MAX_LEASE_TTL_SECONDS))
    token = f"lease_{secrets.token_urlsafe(32)}"
    lease_id = str(uuid4())
    timestamp = _now()
    expires = (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat()
    with _transaction(store):
        store.db.execute(
            """
            INSERT INTO file_access_leases
                (id, organization_id, grant_id, agent_id, device_id, project_id, run_id, token_hash,
                 revocation_epoch, expires_at, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lease_id,
                str(grant["organization_id"]),
                str(grant_id),
                str(agent_id),
                device_id,
                str(project_id),
                run_id,
                _hash_token(token),
                int(grant.get("revocation_epoch") or 1),
                expires,
                timestamp,
            ),
        )
        _audit(
            store,
            organization_id=str(grant["organization_id"]),
            member_id=None,
            agent_id=str(agent_id),
            action="lease:exchange",
            node_id=str(grant.get("root_node_id") or ""),
            capability="drive.file.read",
            grant_id=str(grant_id),
        )
    return {
        "lease": {
            "id": lease_id,
            "token": token,  # 明文只在这里出现一次
            "grant_id": str(grant_id),
            "expires_at": expires,
            "capabilities": grant.get("capabilities") or [],
            "revocation_epoch": int(grant.get("revocation_epoch") or 1),
        }
    }


def _load_lease(store: Any, token: str, *, agent_id: str, project_id: str, device_id: str | None = None) -> dict[str, Any]:
    row = store.db.execute("SELECT * FROM file_access_leases WHERE token_hash = ?", (_hash_token(token),)).fetchone()
    if row is None:
        raise GrantError("file_access_lease_invalid")
    lease = dict(row)
    if lease.get("revoked_at"):
        raise GrantError("file_access_lease_revoked")
    if str(lease["agent_id"]) != str(agent_id):
        raise GrantError("file_access_agent_mismatch")
    if str(lease["project_id"]) != str(project_id):
        raise GrantError("file_access_project_mismatch")
    if lease.get("device_id") and device_id and str(lease["device_id"]) != str(device_id):
        raise GrantError("file_access_device_mismatch")
    if _parse_time(lease.get("expires_at")) and _parse_time(lease.get("expires_at")) <= datetime.now(UTC):
        raise GrantError("file_access_lease_expired")
    grant_row = store.db.execute("SELECT * FROM file_access_grants WHERE id = ?", (str(lease["grant_id"]),)).fetchone()
    if grant_row is None:
        raise GrantError("file_access_grant_not_found")
    grant = _row_to_grant(grant_row)
    if grant.get("revoked_at"):
        raise GrantError("file_access_grant_revoked")
    if int(lease.get("revocation_epoch") or 0) != int(grant.get("revocation_epoch") or 0):
        # epoch 对不上 = 这段授权在签发之后被撤销/续期/联动作废过
        raise GrantError("file_access_grant_revoked", "revocation_epoch_changed")
    if _parse_time(grant.get("expires_at")) and _parse_time(grant.get("expires_at")) <= datetime.now(UTC):
        raise GrantError("file_access_grant_expired")
    return {"lease": lease, "grant": grant}


@_with_schema
def agent_list_granted(store: Any, *, lease_token: str, agent_id: str, project_id: str, device_id: str | None = None) -> dict[str, Any]:
    """Agent 列出**被授权范围内**的文件（只有这一小块，看不到别的）。"""

    from . import drive

    loaded = _load_lease(store, lease_token, agent_id=agent_id, project_id=project_id, device_id=device_id)
    grant = loaded["grant"]
    capabilities = set(grant.get("capabilities") or [])
    if "drive.metadata.read" not in capabilities:
        _audit(
            store,
            organization_id=str(grant["organization_id"]),
            member_id=None,
            agent_id=agent_id,
            action="agent:list",
            decision="deny",
            reason="capability_missing",
            grant_id=str(grant["id"]),
        )
        store.db.commit()
        raise GrantError("file_access_capability_denied", "drive.metadata.read")

    node_ids = grant_node_ids(store, grant)
    owner_actor = drive.DriveActor(member_id=str(grant["owner_member_id"]), organization_id=str(grant["organization_id"]))
    entries: list[dict[str, Any]] = []
    if grant.get("include_future_nodes") and grant.get("root_node_id"):
        # 动态模式：扫这棵子树
        rows = store.db.execute(
            "SELECT * FROM drive_nodes WHERE owner_member_id = ? AND deleted_at IS NULL AND purged_at IS NULL AND kind = 'file'",
            (str(grant["owner_member_id"]),),
        ).fetchall()
        for row in rows:
            if _covered(store, grant, str(row["id"])):
                entries.append({**drive._row_to_node(row), "granted_content_hash": row["content_hash"]})
    else:
        for node_id in sorted(node_ids):
            try:
                node = drive.get_node(store, owner_actor, node_id)
            except drive.DriveError:
                continue  # 授权后被删/被清除的节点：如实跳过（下次列就没有它了）
            if node["kind"] == "file":
                entries.append(node)
    _audit(
        store,
        organization_id=str(grant["organization_id"]),
        member_id=None,
        agent_id=agent_id,
        action="agent:list",
        # 记在授权根节点上：成员在文件详情里就能看到"这个 Agent 什么时候列过我的文件"
        node_id=str(grant.get("root_node_id") or ""),
        capability="drive.metadata.read",
        grant_id=str(grant["id"]),
    )
    store.db.commit()
    return {
        "grant_id": str(grant["id"]),
        "scope_type": grant.get("scope_type"),
        "include_future_nodes": bool(grant.get("include_future_nodes")),
        "capabilities": sorted(capabilities),
        "expires_at": grant.get("expires_at"),
        "entries": entries,
    }


@_with_schema
def agent_read_content(
    store: Any, *, lease_token: str, agent_id: str, project_id: str, device_id: str | None, node_id: str
) -> tuple[dict[str, Any], bytes]:
    """Agent 读某个文件的正文：**范围、能力、epoch、设备状态**逐条校验，每次读都重新判。"""

    from . import drive

    loaded = _load_lease(store, lease_token, agent_id=agent_id, project_id=project_id, device_id=device_id)
    grant = loaded["grant"]
    capabilities = set(grant.get("capabilities") or [])
    if "drive.file.read" not in capabilities:
        raise GrantError("file_access_capability_denied", "drive.file.read")
    if not _covered(store, grant, str(node_id)):
        _audit(
            store,
            organization_id=str(grant["organization_id"]),
            member_id=None,
            agent_id=agent_id,
            action="agent:read",
            node_id=str(node_id),
            capability="drive.file.read",
            decision="deny",
            reason="scope_denied",
            grant_id=str(grant["id"]),
        )
        store.db.commit()
        raise GrantError("file_access_scope_denied", str(node_id))
    owner_actor = drive.DriveActor(member_id=str(grant["owner_member_id"]), organization_id=str(grant["organization_id"]))
    try:
        node, content = drive.read_content(store, owner_actor, str(node_id))
    except drive.DriveError as error:
        raise GrantError("file_node_not_found", error.detail) from error
    with _transaction(store):
        store.db.execute("UPDATE file_access_leases SET used_at = ? WHERE id = ?", (_now(), str(loaded["lease"]["id"])))
        _audit(
            store,
            organization_id=str(grant["organization_id"]),
            member_id=None,
            agent_id=agent_id,
            action="agent:read",
            node_id=str(node_id),
            capability="drive.file.read",
            grant_id=str(grant["id"]),
        )
    return node, content


@_with_schema
def agent_materialize_manifest(
    store: Any, *, lease_token: str, agent_id: str, project_id: str, device_id: str | None = None
) -> dict[str, Any]:
    """物化清单：Agent 要往 `<workspace>/inputs/` 落哪些文件（含哈希与大小，落完自己对账）。"""

    listing = agent_list_granted(store, lease_token=lease_token, agent_id=agent_id, project_id=project_id, device_id=device_id)
    entries = []
    for node in listing["entries"]:
        entries.append(
            {
                "node_id": node["id"],
                "name": node["name"],
                "size_bytes": node["size_bytes"],
                "content_hash": node["content_hash"],
                "revision": node.get("revision", 1),
                "source": "drive_grant",
            }
        )
    return {
        "grant_id": listing["grant_id"],
        "expires_at": listing["expires_at"],
        "entries": entries,
    }