"""Agent 工作区文件服务（FM-3）：平台侧（登记、队列、领取、状态、传输会话、审计）。

## 这一层的边界（计划 §4 的架构图，落成代码就是这几条）

1. **平台不入站连接任何人家的电脑**：浏览器 POST 一个操作 → 落在 `workspace_operations` 队列里 →
   Agent 主动 `claim` → 执行 → `complete`。平台只负责入队、路由与记账。
2. **平台不做路径解析**：这里只做**语法校验**（拒绝绝对路径、`..`、盘符、UNC、控制字符——纵深防御），
   真正的权威校验在 Agent 侧（它才知道符号链接、junction、reparse point 指到哪去）。
3. **不持有宿主机绝对路径**：工作区用 `workspace_identity`（路径的 sha256 前缀，FM-0 的 `path_privacy`
   同一口径）标识；浏览器响应里没有绝对路径。
4. **大文件不进 Gateway/WebSocket 帧**：走 `file_transfer_sessions` + 对象存储（见 `transfer_*`）。
5. **Gateway 零改动**：文件操作是独立的、版本化的轮询契约。

## 幂等与失败口径

- 同一个 `(workspace_id, idempotency_key)`：`request_hash` 相同 → 返回既有操作；**不同 → 冲突**
  （`operation_idempotency_conflict`，安全要求第 12 条）。
- `complete` 只认第一次：重复提交不会把已完成的操作改成另一个结果。
- `claim` 的心跳过期 → `expired`，并带明确错误码（不让页面显示一个假的"执行中"）。
- Agent 离线时**照常入队**（保持 `queued`，如实展示"等 Agent 上线"），不假装已经执行；
  需要立刻失败时传 `fail_when_offline=True`。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator
from uuid import uuid4

OPERATION_TYPES = ("list", "stat", "mkdir", "upload", "download", "rename", "move", "copy", "delete", "extract")
WRITE_OPERATION_TYPES = {"mkdir", "upload", "rename", "move", "copy", "delete", "extract"}
OPERATION_STATUSES = ("queued", "claimed", "running", "succeeded", "failed", "cancelled", "expired")
TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
MAX_PATH_LENGTH = 1024
MAX_SEGMENT_LENGTH = 240
MAX_DEPTH = 32
DEFAULT_LEASE_SECONDS = 120
MAX_LEASE_SECONDS = 3600
# 大文件阈值：超过它就一定要走对象存储（不进任何帧）
LARGE_FILE_BYTES = 8 * 1024 * 1024

_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class WorkspaceError(RuntimeError):
    """稳定错误族（与计划 §6.5 的词表对齐）。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.detail = detail or ""


@dataclass(frozen=True)
class WorkspaceActor:
    """操作者上下文：云盘那边（FM-1）用同一套口径——权限判定不依赖中间件。"""

    member_id: str
    organization_id: str


def actor_for(store: Any, member_id: str) -> WorkspaceActor:
    row = store.db.execute("SELECT organization_id FROM human_members WHERE id = ?", (str(member_id),)).fetchone()
    if row is None:
        raise WorkspaceError("workspace_actor_unknown", str(member_id))
    return WorkspaceActor(member_id=str(member_id), organization_id=str(row["organization_id"]))


# ---- 表结构（SQLite 侧；PostgreSQL 见 migrations/030_agent_workspaces.sql） ----

_SQLITE_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS agent_workspaces (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        device_id TEXT,
        project_id TEXT,
        display_name TEXT NOT NULL,
        workspace_identity TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'desktop',
        status TEXT NOT NULL DEFAULT 'offline',
        policy_version TEXT NOT NULL DEFAULT '1',
        protected_paths TEXT NOT NULL DEFAULT '[]',
        last_seen_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS agent_workspaces_identity_idx ON agent_workspaces(agent_id, workspace_identity)",
    "CREATE INDEX IF NOT EXISTS agent_workspaces_org_idx ON agent_workspaces(organization_id, status)",
    """
    CREATE TABLE IF NOT EXISTS workspace_operations (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        workspace_id TEXT NOT NULL REFERENCES agent_workspaces(id),
        project_id TEXT,
        requested_by_member_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        device_id TEXT,
        operation_type TEXT NOT NULL,
        relative_path TEXT NOT NULL DEFAULT '',
        arguments TEXT NOT NULL DEFAULT '{}',
        expected_revision TEXT,
        status TEXT NOT NULL DEFAULT 'queued',
        idempotency_key TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        claimed_at TEXT,
        started_at TEXT,
        completed_at TEXT,
        heartbeat_at TEXT,
        result TEXT,
        error_code TEXT,
        error_message TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS workspace_operations_idempotency_idx ON workspace_operations(workspace_id, idempotency_key)",
    "CREATE INDEX IF NOT EXISTS workspace_operations_queue_idx ON workspace_operations(workspace_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS workspace_operations_agent_idx ON workspace_operations(agent_id, status, created_at)",
    """
    CREATE TABLE IF NOT EXISTS file_transfer_sessions (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        owner_member_id TEXT,
        operation_id TEXT,
        workspace_id TEXT,
        source_type TEXT NOT NULL,
        source_id TEXT,
        source_hash TEXT,
        target_type TEXT NOT NULL,
        target_id TEXT,
        storage_key TEXT NOT NULL,
        expected_size INTEGER NOT NULL DEFAULT 0,
        expected_hash TEXT,
        uploaded_size INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'initialized',
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS file_transfer_sessions_status_idx ON file_transfer_sessions(status, expires_at)",
    "CREATE INDEX IF NOT EXISTS file_transfer_sessions_operation_idx ON file_transfer_sessions(operation_id)",
    # FM-6 断点续传：分片表 + 会话上的多分片会话 id / 分片大小（老库用 _ensure_columns 补列）
    """
    CREATE TABLE IF NOT EXISTS file_transfer_parts (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        transfer_id TEXT NOT NULL REFERENCES file_transfer_sessions(id),
        part_number INTEGER NOT NULL,
        size_bytes INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL DEFAULT '',
        etag TEXT NOT NULL DEFAULT '',
        attempts INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS file_transfer_parts_unique_idx ON file_transfer_parts(transfer_id, part_number)",
    "CREATE INDEX IF NOT EXISTS file_transfer_parts_transfer_idx ON file_transfer_parts(transfer_id)",
    """
    CREATE TABLE IF NOT EXISTS workspace_audit (
        id TEXT PRIMARY KEY,
        organization_id TEXT NOT NULL,
        workspace_id TEXT,
        operation_id TEXT,
        member_id TEXT,
        agent_id TEXT,
        action TEXT NOT NULL,
        relative_path_hash TEXT,
        capability TEXT NOT NULL DEFAULT '',
        decision TEXT NOT NULL DEFAULT 'allow',
        reason TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS workspace_audit_workspace_idx ON workspace_audit(workspace_id, created_at)",
)


def _schema_ready(store: Any) -> bool:
    try:
        return store.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'agent_workspaces'"
        ).fetchone() is not None
    except Exception:  # noqa: BLE001 - 非 SQLite 后端（PG）由迁移建表
        return True


def _ensure_columns(store: Any, table: str, columns: dict[str, str]) -> None:
    """给已建好的表补列（老库升级用；PG 侧由迁移的 ADD COLUMN IF NOT EXISTS 负责）。"""

    try:
        existing = {row["name"] for row in store.db.execute(f"PRAGMA table_info({table})")}
    except Exception:  # noqa: BLE001 - 非 SQLite：跳过
        return
    for name, definition in columns.items():
        if name not in existing:
            store.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def ensure_schema(store: Any) -> None:
    if _schema_ready(store):
        # 表在 ≠ 列齐：老库（FM-5 之前建的）缺 FM-6 的两列，这里补齐
        _ensure_columns(
            store,
            "file_transfer_sessions",
            {"multipart_upload_id": "TEXT", "part_size_bytes": "INTEGER NOT NULL DEFAULT 0", "owner_member_id": "TEXT"},
        )
        store.db.commit()
        return
    for statement in _SQLITE_SCHEMA:
        store.db.execute(statement)
    _ensure_columns(
        store,
        "file_transfer_sessions",
        {"multipart_upload_id": "TEXT", "part_size_bytes": "INTEGER NOT NULL DEFAULT 0", "owner_member_id": "TEXT"},
    )
    store.db.commit()


def _with_schema(function: Any) -> Any:
    def wrapper(store: Any, *args: Any, **kwargs: Any) -> Any:
        ensure_schema(store)
        return function(store, *args, **kwargs)

    wrapper.__name__ = getattr(function, "__name__", "wrapper")
    return wrapper


@contextmanager
def _transaction(store: Any) -> Iterator[None]:
    """写事务：与云盘那边一样，SQLite 只有一条连接，`rollback()` 是全局的。"""

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


# ---- 路径校验（语法层；权威校验在 Agent 侧） ---------------------------------


def validate_relative_path(value: str, *, allow_empty: bool = True) -> str:
    """把用户/浏览器给的路径收敛成规范相对路径；有一点可疑就拒绝。

    拒绝清单（计划 §7.1）：绝对路径、`..`、盘符、UNC、NUL/控制字符、超长、超深、
    Windows 保留名与尾随点/空格。**不做**符号链接判断——那要看真实文件系统，只有 Agent 能做。
    """

    raw = str(value or "").strip()
    if not raw:
        if allow_empty:
            return ""
        raise WorkspaceError("workspace_path_required")
    text = unicodedata.normalize("NFC", raw).replace("\\", "/")
    if len(text) > MAX_PATH_LENGTH:
        raise WorkspaceError("workspace_path_invalid", "too_long")
    if text.startswith("/") or text.startswith("//"):
        raise WorkspaceError("workspace_path_invalid", "absolute")
    if _WINDOWS_DRIVE.match(text):
        raise WorkspaceError("workspace_path_invalid", "drive_letter")
    if "\x00" in text or any(ord(character) < 32 for character in text):
        raise WorkspaceError("workspace_path_invalid", "control_character")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise WorkspaceError("workspace_path_outside_root", "parent_segment")
        if len(part) > MAX_SEGMENT_LENGTH:
            raise WorkspaceError("workspace_path_invalid", "segment_too_long")
        if part.endswith((".", " ")):
            raise WorkspaceError("workspace_path_invalid", "trailing_dot_or_space")
        parts.append(part)
    if len(parts) > MAX_DEPTH:
        raise WorkspaceError("workspace_path_invalid", "too_deep")
    return "/".join(parts)


def path_hash(relative: str) -> str:
    """路径的哈希（审计里只记哈希，不记路径本身）。"""

    return hashlib.sha256(str(relative or "").encode("utf-8")).hexdigest()[:32]


def workspace_identity_for(absolute_workspace: str | None) -> str | None:
    """宿主机工作区路径 → 稳定标识（与 FM-0 的 `path_privacy.workspace_identity` 同口径）。"""

    if absolute_workspace is None:
        return None
    normalized = str(absolute_workspace).strip().replace("\\", "/").rstrip("/")
    if not normalized:
        return None
    return f"ws-{hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:12]}"


def request_hash(operation_type: str, relative_path: str, arguments: dict[str, Any]) -> str:
    payload = json.dumps(
        {"type": operation_type, "path": relative_path, "args": arguments}, ensure_ascii=False, sort_keys=True
    )
    # 时间戳/随机值不许进 arguments：同 key 同内容才算幂等，混进时间就是"每次都不一样"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---- 工作区登记 -------------------------------------------------------------


def _row_to_workspace(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "agent_id": str(row["agent_id"]),
        "device_id": _field(row, "device_id"),
        "project_id": _field(row, "project_id"),
        "display_name": str(row["display_name"]),
        "workspace_identity": str(row["workspace_identity"]),
        "kind": str(_field(row, "kind", "desktop")),
        "status": str(_field(row, "status", "offline")),
        "policy_version": str(_field(row, "policy_version", "1")),
        "protected_paths": json.loads(_field(row, "protected_paths", "[]") or "[]"),
        "last_seen_at": _field(row, "last_seen_at"),
        "created_at": _field(row, "created_at"),
        "updated_at": _field(row, "updated_at"),
    }


@_with_schema
def register_workspace(
    store: Any,
    *,
    agent_id: str,
    organization_id: str,
    display_name: str,
    workspace_identity: str,
    device_id: str | None = None,
    project_id: str | None = None,
    kind: str = "desktop",
    protected_paths: list[str] | None = None,
    policy_version: str = "1",
) -> dict[str, Any]:
    """登记/更新一个工作区（Agent 心跳时上报；同一 `(agent_id, workspace_identity)` 只保留一条）。"""

    if not str(workspace_identity or "").strip():
        raise WorkspaceError("workspace_identity_required")
    if kind not in {"cloud", "desktop"}:
        raise WorkspaceError("workspace_kind_invalid", kind)
    protected = [validate_relative_path(item, allow_empty=False) for item in (protected_paths or [])]
    timestamp = _now()
    existing = store.db.execute(
        "SELECT * FROM agent_workspaces WHERE agent_id = ? AND workspace_identity = ?",
        (str(agent_id), str(workspace_identity)),
    ).fetchone()
    with _transaction(store):
        if existing is None:
            workspace_id = str(uuid4())
            store.db.execute(
                """
                INSERT INTO agent_workspaces
                    (id, organization_id, agent_id, device_id, project_id, display_name, workspace_identity,
                     kind, status, policy_version, protected_paths, last_seen_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id,
                    str(organization_id),
                    str(agent_id),
                    device_id,
                    project_id,
                    str(display_name or "工作区"),
                    str(workspace_identity),
                    kind,
                    str(policy_version or "1"),
                    json.dumps(protected, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
        else:
            workspace_id = str(existing["id"])
            store.db.execute(
                """
                UPDATE agent_workspaces
                SET display_name = ?, device_id = ?, project_id = ?, kind = ?, status = 'online',
                    policy_version = ?, protected_paths = ?, last_seen_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    str(display_name or existing["display_name"]),
                    device_id or _field(existing, "device_id"),
                    project_id or _field(existing, "project_id"),
                    kind,
                    str(policy_version or "1"),
                    json.dumps(protected, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    workspace_id,
                ),
            )
    return _row_to_workspace(store.db.execute("SELECT * FROM agent_workspaces WHERE id = ?", (workspace_id,)).fetchone())


@_with_schema
def touch_workspace(store: Any, agent_id: str, *, status: str = "online") -> int:
    """心跳：把该 Agent 的所有工作区标成 online（返回受影响条数）。"""

    if status not in {"online", "offline", "unavailable"}:
        raise WorkspaceError("workspace_status_invalid", status)
    timestamp = _now()
    with _transaction(store):
        cursor = store.db.execute(
            "UPDATE agent_workspaces SET status = ?, last_seen_at = ?, updated_at = ? WHERE agent_id = ?",
            (status, timestamp, timestamp, str(agent_id)),
        )
    return int(cursor.rowcount or 0)


@_with_schema
def mark_agent_offline(store: Any, agent_id: str) -> int:
    return touch_workspace(store, agent_id, status="offline")


@_with_schema
def list_workspaces(store: Any, actor: WorkspaceActor) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM agent_workspaces WHERE organization_id = ? ORDER BY kind ASC, display_name ASC",
        (actor.organization_id,),
    ).fetchall()
    return [_row_to_workspace(row) for row in rows]


def _require_workspace(store: Any, actor: WorkspaceActor, workspace_id: str) -> Any:
    row = store.db.execute("SELECT * FROM agent_workspaces WHERE id = ?", (str(workspace_id),)).fetchone()
    if row is None or str(row["organization_id"]) != actor.organization_id:
        raise WorkspaceError("workspace_not_found")
    return row


@_with_schema
def get_workspace(store: Any, actor: WorkspaceActor, workspace_id: str) -> dict[str, Any]:
    return _row_to_workspace(_require_workspace(store, actor, workspace_id))


# ---- 操作队列 ---------------------------------------------------------------


def _row_to_operation(row: Any, *, include_arguments: bool = True) -> dict[str, Any]:
    value = {
        "id": str(row["id"]),
        "workspace_id": str(row["workspace_id"]),
        "project_id": _field(row, "project_id"),
        "requested_by_member_id": str(row["requested_by_member_id"]),
        "agent_id": str(row["agent_id"]),
        "device_id": _field(row, "device_id"),
        "operation_type": str(row["operation_type"]),
        "relative_path": str(_field(row, "relative_path", "")),
        "expected_revision": _field(row, "expected_revision"),
        "status": str(_field(row, "status", "queued")),
        "idempotency_key": str(row["idempotency_key"]),
        "claimed_at": _field(row, "claimed_at"),
        "started_at": _field(row, "started_at"),
        "completed_at": _field(row, "completed_at"),
        "heartbeat_at": _field(row, "heartbeat_at"),
        "result": json.loads(_field(row, "result") or "null") if _field(row, "result") else None,
        "error_code": _field(row, "error_code"),
        "error_message": _field(row, "error_message"),
        "created_at": _field(row, "created_at"),
        "updated_at": _field(row, "updated_at"),
    }
    if include_arguments:
        value["arguments"] = json.loads(_field(row, "arguments", "{}") or "{}")
    return value


def _audit(
    store: Any,
    *,
    organization_id: str,
    workspace_id: str | None,
    operation_id: str | None,
    member_id: str | None,
    agent_id: str | None,
    action: str,
    relative_path: str = "",
    capability: str = "",
    decision: str = "allow",
    reason: str = "",
) -> None:
    store.db.execute(
        """
        INSERT INTO workspace_audit
            (id, organization_id, workspace_id, operation_id, member_id, agent_id, action,
             relative_path_hash, capability, decision, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(uuid4()),
            organization_id,
            workspace_id,
            operation_id,
            member_id,
            agent_id,
            action,
            path_hash(relative_path) if relative_path else None,
            capability,
            decision,
            reason,
            _now(),
        ),
    )


@_with_schema
def create_operation(
    store: Any,
    actor: WorkspaceActor,
    workspace_id: str,
    *,
    operation_type: str,
    relative_path: str = "",
    arguments: dict[str, Any] | None = None,
    idempotency_key: str,
    expected_revision: str | None = None,
    fail_when_offline: bool = False,
) -> dict[str, Any]:
    """入队一个文件操作。

    - 幂等：同 key + 同 request_hash → 返回既有操作（可能是已完成的）；同 key + 不同 hash → 冲突；
    - Agent 离线：默认**照常入队**并保持 `queued`（如实展示"等它上线"，不假装执行过）；
      `fail_when_offline=True` 时立刻标 `failed` + `workspace_offline`。
    """

    workspace = _require_workspace(store, actor, workspace_id)
    kind = str(operation_type or "").strip().lower()
    if kind not in OPERATION_TYPES:
        raise WorkspaceError("workspace_operation_unsupported", kind)
    path = validate_relative_path(relative_path)
    if kind in {"stat", "mkdir", "rename", "move", "copy", "delete", "extract"} and not path:
        raise WorkspaceError("workspace_path_required")
    payload = dict(arguments or {})
    digest = request_hash(kind, path, payload)
    key = str(idempotency_key or "").strip()
    if not key:
        raise WorkspaceError("workspace_idempotency_key_required")

    existing = store.db.execute(
        "SELECT * FROM workspace_operations WHERE workspace_id = ? AND idempotency_key = ?",
        (str(workspace_id), key),
    ).fetchone()
    if existing is not None:
        if str(existing["request_hash"]) != digest:
            raise WorkspaceError("operation_idempotency_conflict", key)
        return _row_to_operation(existing)

    status = "queued"
    error_code = None
    error_message = None
    offline = str(_field(workspace, "status", "offline")) != "online"
    if offline and fail_when_offline:
        status = "failed"
        error_code = "workspace_offline"
        error_message = "Agent 不在线，操作未执行"
    operation_id = str(uuid4())
    timestamp = _now()
    with _transaction(store):
        store.db.execute(
            """
            INSERT INTO workspace_operations
                (id, organization_id, workspace_id, project_id, requested_by_member_id, agent_id, device_id,
                 operation_type, relative_path, arguments, expected_revision, status, idempotency_key,
                 request_hash, created_at, updated_at, completed_at, error_code, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation_id,
                actor.organization_id,
                str(workspace_id),
                _field(workspace, "project_id"),
                actor.member_id,
                str(workspace["agent_id"]),
                _field(workspace, "device_id"),
                kind,
                path,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                expected_revision,
                status,
                key,
                digest,
                timestamp,
                timestamp,
                timestamp if status != "queued" else None,
                error_code,
                error_message,
            ),
        )
        _audit(
            store,
            organization_id=actor.organization_id,
            workspace_id=str(workspace_id),
            operation_id=operation_id,
            member_id=actor.member_id,
            agent_id=str(workspace["agent_id"]),
            action=f"queue:{kind}",
            relative_path=path,
            capability="workspace.files.write" if kind in WRITE_OPERATION_TYPES else "workspace.files.read",
            decision="allow" if status == "queued" else "deny",
            reason="" if status == "queued" else "workspace_offline",
        )
    return _row_to_operation(
        store.db.execute("SELECT * FROM workspace_operations WHERE id = ?", (operation_id,)).fetchone()
    )


@_with_schema
def claim_operations(
    store: Any,
    *,
    agent_id: str,
    workspace_id: str | None = None,
    limit: int = 4,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> list[dict[str, Any]]:
    """Agent 领取待执行操作（按创建时间最早优先）。只发给**在线**工作区。"""

    ensure_schema(store)
    lease = max(10, min(int(lease_seconds or DEFAULT_LEASE_SECONDS), MAX_LEASE_SECONDS))
    timestamp = _now()
    expires = (datetime.now(UTC) + timedelta(seconds=lease)).isoformat()
    clauses = ["agent_id = ?", "status = 'queued'"]
    params: list[Any] = [str(agent_id)]
    if workspace_id:
        clauses.append("workspace_id = ?")
        params.append(str(workspace_id))
    rows = store.db.execute(
        f"SELECT * FROM workspace_operations WHERE {' AND '.join(clauses)} ORDER BY created_at ASC LIMIT ?",
        (*params, max(1, min(int(limit or 4), 16))),
    ).fetchall()
    claimed: list[dict[str, Any]] = []
    for row in rows:
        workspace = store.db.execute("SELECT status FROM agent_workspaces WHERE id = ?", (str(row["workspace_id"]),)).fetchone()
        if workspace is None or str(workspace["status"]) != "online":
            continue  # 离线工作区不发活（它也不会来领）
        with _transaction(store):
            cursor = store.db.execute(
                """
                UPDATE workspace_operations
                SET status = 'claimed', claimed_at = ?, heartbeat_at = ?, updated_at = ?, expected_revision = COALESCE(expected_revision, ?)
                WHERE id = ? AND status = 'queued'
                """,
                (timestamp, timestamp, timestamp, expires, str(row["id"])),
            )
            if int(cursor.rowcount or 0) != 1:
                continue  # 被另一个 worker 抢走了
            _audit(
                store,
                organization_id=str(row["organization_id"]),
                workspace_id=str(row["workspace_id"]),
                operation_id=str(row["id"]),
                member_id=None,
                agent_id=str(agent_id),
                action="claim",
                relative_path=str(_field(row, "relative_path", "")),
                capability="workspace.files.claim",
            )
        claimed.append(
            _row_to_operation(store.db.execute("SELECT * FROM workspace_operations WHERE id = ?", (str(row["id"]),)).fetchone())
        )
    return claimed


def _require_operation(store: Any, operation_id: str, *, agent_id: str | None = None) -> Any:
    row = store.db.execute("SELECT * FROM workspace_operations WHERE id = ?", (str(operation_id),)).fetchone()
    if row is None:
        raise WorkspaceError("workspace_operation_not_found")
    if agent_id is not None and str(row["agent_id"]) != str(agent_id):
        raise WorkspaceError("workspace_operation_agent_mismatch")
    return row


@_with_schema
def start_operation(store: Any, operation_id: str, *, agent_id: str) -> dict[str, Any]:
    row = _require_operation(store, operation_id, agent_id=agent_id)
    if str(_field(row, "status")) in TERMINAL_STATUSES:
        raise WorkspaceError("workspace_operation_already_finished", str(_field(row, "status")))
    timestamp = _now()
    with _transaction(store):
        store.db.execute(
            "UPDATE workspace_operations SET status = 'running', started_at = ?, heartbeat_at = ?, updated_at = ? WHERE id = ?",
            (timestamp, timestamp, timestamp, str(operation_id)),
        )
    return _row_to_operation(_require_operation(store, operation_id))


@_with_schema
def progress_operation(
    store: Any,
    operation_id: str,
    *,
    agent_id: str,
    progress: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """进度/心跳：`expected_revision` 这一列复用为"租约到期时间"，进度写进 result（未完成态）。"""

    row = _require_operation(store, operation_id, agent_id=agent_id)
    timestamp = _now()
    expires = (datetime.now(UTC) + timedelta(seconds=DEFAULT_LEASE_SECONDS)).isoformat()
    with _transaction(store):
        store.db.execute(
            """
            UPDATE workspace_operations
            SET heartbeat_at = ?, updated_at = ?, expected_revision = ?,
                result = COALESCE(?, result)
            WHERE id = ? AND status IN ('claimed', 'running')
            """,
            (
                timestamp,
                timestamp,
                expires,
                json.dumps({"progress": progress or {}}, ensure_ascii=False) if progress else None,
                str(operation_id),
            ),
        )
    return _row_to_operation(_require_operation(store, operation_id))


@_with_schema
def complete_operation(
    store: Any,
    operation_id: str,
    *,
    agent_id: str,
    success: bool,
    result: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    """完成/失败。**只认第一次**：已完成的操作再提交不会改结果（重复提交是重连后的常态）。"""

    row = _require_operation(store, operation_id, agent_id=agent_id)
    if str(_field(row, "status")) in TERMINAL_STATUSES:
        return _row_to_operation(row)
    status = "succeeded" if success else "failed"
    if success and not error_code:
        error_code = None
    elif not success and not error_code:
        error_code = "workspace_operation_failed"
    timestamp = _now()
    with _transaction(store):
        store.db.execute(
            """
            UPDATE workspace_operations
            SET status = ?, completed_at = ?, updated_at = ?, result = ?, error_code = ?, error_message = ?
            WHERE id = ?
            """,
            (
                status,
                timestamp,
                timestamp,
                json.dumps(result, ensure_ascii=False) if result is not None else None,
                error_code,
                (error_message or "")[:500] or None,
                str(operation_id),
            ),
        )
        _audit(
            store,
            organization_id=str(row["organization_id"]),
            workspace_id=str(row["workspace_id"]),
            operation_id=str(operation_id),
            member_id=None,
            agent_id=str(agent_id),
            action=f"complete:{_field(row, 'operation_type')}",
            relative_path=str(_field(row, "relative_path", "")),
            capability="workspace.files.claim",
            decision="allow" if success else "deny",
            reason=error_code or "",
        )
    return _row_to_operation(_require_operation(store, operation_id))


@_with_schema
def cancel_operation(store: Any, actor: WorkspaceActor, operation_id: str) -> dict[str, Any]:
    """取消（人点的）。排队中的立即取消；已经在跑的下发取消标记（Agent 轮询时看到就会尽力停）。"""

    row = store.db.execute("SELECT * FROM workspace_operations WHERE id = ?", (str(operation_id),)).fetchone()
    if row is None or str(row["organization_id"]) != actor.organization_id:
        raise WorkspaceError("workspace_operation_not_found")
    status = str(_field(row, "status", "queued"))
    if status in TERMINAL_STATUSES:
        return _row_to_operation(row)
    timestamp = _now()
    with _transaction(store):
        if status == "queued":
            store.db.execute(
                "UPDATE workspace_operations SET status = 'cancelled', completed_at = ?, updated_at = ? WHERE id = ?",
                (timestamp, timestamp, str(operation_id)),
            )
        else:
            store.db.execute(
                "UPDATE workspace_operations SET status = 'cancelled', completed_at = ?, updated_at = ?, error_code = ? WHERE id = ?",
                (timestamp, timestamp, "workspace_operation_cancelled", str(operation_id)),
            )
        _audit(
            store,
            organization_id=actor.organization_id,
            workspace_id=str(row["workspace_id"]),
            operation_id=str(operation_id),
            member_id=actor.member_id,
            agent_id=None,
            action="cancel",
            relative_path=str(_field(row, "relative_path", "")),
            capability="workspace.files.write",
            reason="cancelled_by_member",
        )
    return _row_to_operation(store.db.execute("SELECT * FROM workspace_operations WHERE id = ?", (str(operation_id),)).fetchone())


@_with_schema
def expire_stale_operations(store: Any, *, grace_seconds: int = 0) -> list[str]:
    """把心跳过期的 claimed/running 操作标成 `expired`（不留下假的"执行中"）。"""

    now = datetime.now(UTC)
    rows = store.db.execute(
        "SELECT * FROM workspace_operations WHERE status IN ('claimed', 'running')"
    ).fetchall()
    expired: list[str] = []
    for row in rows:
        deadline = _parse_time(_field(row, "expected_revision")) or _parse_time(_field(row, "heartbeat_at"))
        if deadline is None:
            continue
        if now - timedelta(seconds=int(grace_seconds or 0)) <= deadline:
            continue
        with _transaction(store):
            store.db.execute(
                """
                UPDATE workspace_operations
                SET status = 'expired', completed_at = ?, updated_at = ?, error_code = 'workspace_operation_expired',
                    error_message = 'Agent 长时间没有回报（可能掉线了），操作未完成'
                WHERE id = ? AND status IN ('claimed', 'running')
                """,
                (_now(), _now(), str(row["id"])),
            )
        expired.append(str(row["id"]))
    return expired


@_with_schema
def list_operations(
    store: Any,
    actor: WorkspaceActor,
    workspace_id: str | None = None,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    clauses = ["organization_id = ?"]
    params: list[Any] = [actor.organization_id]
    if workspace_id:
        _require_workspace(store, actor, workspace_id)
        clauses.append("workspace_id = ?")
        params.append(str(workspace_id))
    rows = store.db.execute(
        f"SELECT * FROM workspace_operations WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
        (*params, max(1, min(int(limit or 50), 200))),
    ).fetchall()
    return [_row_to_operation(row) for row in rows]


@_with_schema
def get_operation(store: Any, actor: WorkspaceActor, operation_id: str) -> dict[str, Any]:
    row = store.db.execute("SELECT * FROM workspace_operations WHERE id = ?", (str(operation_id),)).fetchone()
    if row is None or str(row["organization_id"]) != actor.organization_id:
        raise WorkspaceError("workspace_operation_not_found")
    return _row_to_operation(row)


# ---- 传输会话（大文件走对象存储，不进任何帧） --------------------------------


def _row_to_transfer(row: Any) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "operation_id": _field(row, "operation_id"),
        "workspace_id": _field(row, "workspace_id"),
        "source_type": str(row["source_type"]),
        "source_id": _field(row, "source_id"),
        "source_hash": _field(row, "source_hash"),
        "target_type": str(row["target_type"]),
        "target_id": _field(row, "target_id"),
        "storage_key": str(row["storage_key"]),
        "expected_size": int(_field(row, "expected_size", 0)),
        "expected_hash": _field(row, "expected_hash"),
        "uploaded_size": int(_field(row, "uploaded_size", 0)),
        "status": str(_field(row, "status", "initialized")),
        "multipart_upload_id": _field(row, "multipart_upload_id"),
        "part_size_bytes": int(_field(row, "part_size_bytes", 0) or 0),
        "expires_at": _field(row, "expires_at"),
        "created_at": _field(row, "created_at"),
        "updated_at": _field(row, "updated_at"),
    }


@_with_schema
def create_transfer(
    store: Any,
    actor: WorkspaceActor,
    *,
    source_type: str,
    target_type: str,
    operation_id: str | None = None,
    workspace_id: str | None = None,
    source_id: str | None = None,
    source_hash: str | None = None,
    target_id: str | None = None,
    expected_size: int = 0,
    expected_hash: str | None = None,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """开一个传输会话（内容落在对象存储，两边只交换会话 id 与哈希）。"""

    for value, name in ((source_type, "source_type"), (target_type, "target_type")):
        if value not in {"drive", "workspace", "temp"}:
            raise WorkspaceError("workspace_transfer_type_invalid", f"{name}:{value}")
    transfer_id = str(uuid4())
    timestamp = _now()
    expires = (datetime.now(UTC) + timedelta(seconds=max(60, min(int(ttl_seconds or 3600), 86400)))).isoformat()
    storage_key = f"transfers/{actor.organization_id}/{transfer_id}"
    with _transaction(store):
        store.db.execute(
            """
            INSERT INTO file_transfer_sessions
                (id, organization_id, owner_member_id, operation_id, workspace_id, source_type, source_id, source_hash,
                 target_type, target_id, storage_key, expected_size, expected_hash, uploaded_size, status,
                 expires_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'initialized', ?, ?, ?)
            """,
            (
                transfer_id,
                actor.organization_id,
                actor.member_id,
                operation_id,
                workspace_id,
                source_type,
                source_id,
                source_hash,
                target_type,
                target_id,
                storage_key,
                int(expected_size or 0),
                expected_hash,
                expires,
                timestamp,
                timestamp,
            ),
        )
    return _row_to_transfer(store.db.execute("SELECT * FROM file_transfer_sessions WHERE id = ?", (transfer_id,)).fetchone())


@_with_schema
def write_transfer_content(store: Any, actor: WorkspaceActor, transfer_id: str, content: bytes, *, mime_type: str | None = None) -> dict[str, Any]:
    """把内容写进对象存储并校验声明的大小/哈希（不一致就拒绝，不静默接受坏数据）。"""

    row = store.db.execute("SELECT * FROM file_transfer_sessions WHERE id = ?", (str(transfer_id),)).fetchone()
    if row is None or str(row["organization_id"]) != actor.organization_id:
        raise WorkspaceError("workspace_transfer_not_found")
    expected_hash = _field(row, "expected_hash")
    actual_hash = hashlib.sha256(content).hexdigest()
    if expected_hash and str(expected_hash) != actual_hash:
        raise WorkspaceError("workspace_transfer_hash_mismatch", f"{expected_hash}!={actual_hash}")
    expected_size = int(_field(row, "expected_size", 0) or 0)
    if expected_size and expected_size != len(content):
        raise WorkspaceError("workspace_transfer_size_mismatch", f"{expected_size}!={len(content)}")
    stored = store.object_store.put_bytes(str(row["storage_key"]), content, mime_type)
    timestamp = _now()
    with _transaction(store):
        store.db.execute(
            "UPDATE file_transfer_sessions SET status = 'ready', uploaded_size = ?, expected_size = ?, source_hash = ?, updated_at = ? WHERE id = ?",
            (len(content), len(content), actual_hash, timestamp, str(transfer_id)),
        )
    return _row_to_transfer(store.db.execute("SELECT * FROM file_transfer_sessions WHERE id = ?", (str(transfer_id),)).fetchone())


@_with_schema
def read_transfer_content(store: Any, actor: WorkspaceActor, transfer_id: str, *, mark_consumed: bool = False) -> tuple[dict[str, Any], bytes]:
    row = store.db.execute("SELECT * FROM file_transfer_sessions WHERE id = ?", (str(transfer_id),)).fetchone()
    if row is None or str(row["organization_id"]) != actor.organization_id:
        raise WorkspaceError("workspace_transfer_not_found")
    deadline = _parse_time(_field(row, "expires_at"))
    if deadline and deadline <= datetime.now(UTC):
        with _transaction(store):
            store.db.execute(
                "UPDATE file_transfer_sessions SET status = 'expired', updated_at = ? WHERE id = ?", (_now(), str(transfer_id))
            )
        raise WorkspaceError("workspace_transfer_expired", str(transfer_id))
    content = store.object_store.get_bytes(str(row["storage_key"]))
    expected_hash = _field(row, "expected_hash")
    if expected_hash and hashlib.sha256(content).hexdigest() != str(expected_hash):
        raise WorkspaceError("workspace_transfer_hash_mismatch", str(expected_hash))
    if mark_consumed:
        with _transaction(store):
            store.db.execute(
                "UPDATE file_transfer_sessions SET status = 'consumed', updated_at = ? WHERE id = ?", (_now(), str(transfer_id))
            )
    return _row_to_transfer(store.db.execute("SELECT * FROM file_transfer_sessions WHERE id = ?", (str(transfer_id),)).fetchone()), content


@_with_schema
def get_transfer(store: Any, actor: WorkspaceActor, transfer_id: str) -> dict[str, Any]:
    """取一个传输会话：**按成员收口**（内容可能是他从自己私有云盘搬出来的，同组织也不行）。

    `owner_member_id` 为空的历史行（FM-6 之前建的）按"同组织可见"兜底——避免老数据突然读不到。
    """

    row = store.db.execute(
        """
        SELECT * FROM file_transfer_sessions
        WHERE id = ? AND organization_id = ?
          AND (owner_member_id IS NULL OR owner_member_id = ?)
        """,
        (str(transfer_id), actor.organization_id, actor.member_id),
    ).fetchone()
    if row is None:
        raise WorkspaceError("workspace_transfer_not_found")
    return _row_to_transfer(row)


@_with_schema
def list_transfers(store: Any, actor: WorkspaceActor, *, operation_id: str | None = None) -> list[dict[str, Any]]:
    clauses = ["organization_id = ?", "(owner_member_id IS NULL OR owner_member_id = ?)"]
    params: list[Any] = [actor.organization_id, actor.member_id]
    if operation_id:
        clauses.append("operation_id = ?")
        params.append(str(operation_id))
    rows = store.db.execute(
        f"SELECT * FROM file_transfer_sessions WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT 100",
        tuple(params),
    ).fetchall()
    return [_row_to_transfer(row) for row in rows]


MAX_PART_NUMBER = 10000  # S3 的硬限制
DEFAULT_PART_SIZE = 8 * 1024 * 1024


def _row_to_part(row: Any) -> dict[str, Any]:
    return {
        "part_number": int(row["part_number"]),
        "size_bytes": int(_field(row, "size_bytes", 0)),
        "content_hash": str(_field(row, "content_hash", "")),
        "etag": str(_field(row, "etag", "")),
        "attempts": int(_field(row, "attempts", 1)),
        "updated_at": _field(row, "updated_at"),
    }


@_with_schema
def list_transfer_parts(store: Any, actor: WorkspaceActor, transfer_id: str) -> dict[str, Any]:
    """续传游标：已经收到哪些片、每片的 sha256、还缺哪些（客户端据此只补缺的）。"""

    transfer = get_transfer(store, actor, transfer_id)
    rows = store.db.execute(
        "SELECT * FROM file_transfer_parts WHERE transfer_id = ? ORDER BY part_number ASC", (str(transfer_id),)
    ).fetchall()
    parts = [_row_to_part(row) for row in rows]
    received = [item["part_number"] for item in parts]
    expected_size = int(transfer.get("expected_size") or 0)
    # 分片大小：**没收到过任何片时如实报 0（未知）**——切片大小是客户端的选择，
    # 平台只回显"观察到的那个"（首片落地后才有）。报个默认值会让客户端以为平台要求 8MB，
    # 于是把整份大文件当一片传（验收脚本真踩到过）。
    part_size = int(transfer.get("part_size_bytes") or 0)
    # total/missing 是**提示**：收口只认"片号从 1 连续 + 长度之和 == 声明大小"，
    # 所以客户端切片与提示不同也不会把文件拼坏，最多多补一片
    total_parts = ((expected_size + part_size - 1) // part_size) if (expected_size and part_size) else 0
    missing = [number for number in range(1, total_parts + 1) if number not in received] if total_parts else []
    return {
        "transfer_id": str(transfer_id),
        "status": transfer["status"],
        "part_size_bytes": part_size,
        "expected_size": expected_size,
        "expected_hash": transfer.get("expected_hash"),
        "total_parts": total_parts,
        "received_parts": received,
        "missing_parts": missing,
        "received_bytes": sum(item["size_bytes"] for item in parts),
        "parts": parts,
    }


def _ensure_multipart(store: Any, actor: WorkspaceActor, transfer: dict[str, Any], part_size: int) -> str:
    """发起（或复用）对象存储的多分片会话。"""

    existing = str(transfer.get("multipart_upload_id") or "")
    if existing:
        return existing
    upload_id = store.object_store.initiate_multipart(str(transfer["storage_key"]), None)
    with _transaction(store):
        store.db.execute(
            "UPDATE file_transfer_sessions SET multipart_upload_id = ?, part_size_bytes = ?, status = 'uploading', updated_at = ? WHERE id = ?",
            (str(upload_id), int(part_size), _now(), str(transfer["id"])),
        )
    return str(upload_id)


@_with_schema
def upload_transfer_part(
    store: Any,
    actor: WorkspaceActor,
    transfer_id: str,
    part_number: int,
    content: bytes,
    *,
    part_hash: str | None = None,
) -> dict[str, Any]:
    """上传一片。同一片重传 = 覆盖（重试的常态），并写入新的 sha256。"""

    transfer = get_transfer(store, actor, transfer_id)
    if transfer["status"] in {"consumed", "expired", "failed"}:
        raise WorkspaceError("workspace_transfer_expired", transfer["status"])
    if not isinstance(part_number, int) or part_number < 1 or part_number > MAX_PART_NUMBER:
        raise WorkspaceError("workspace_transfer_part_invalid", str(part_number))
    if not content:
        raise WorkspaceError("workspace_transfer_part_empty", str(part_number))
    digest = hashlib.sha256(content).hexdigest()
    if part_hash and str(part_hash) != digest:
        raise WorkspaceError("workspace_transfer_part_hash_mismatch", f"{part_hash}!={digest}")
    # 分片大小以**首片**为准：客户端切多大，游标就按多大推"还缺哪些"。
    # 收口时不靠这个推出来的 total_parts，而是靠"片号从 1 连续 + 长度之和 == 声明大小"。
    part_size = int(transfer.get("part_size_bytes") or 0)
    if part_size <= 0 or int(part_number) == 1:
        part_size = len(content)
    upload_id = _ensure_multipart(store, actor, transfer, part_size)
    try:
        etag = store.object_store.upload_part(str(upload_id), int(part_number), content, key=str(transfer["storage_key"]))
    except KeyError as error:
        raise WorkspaceError("workspace_transfer_not_found", "multipart_upload_missing") from error
    timestamp = _now()
    existing = store.db.execute(
        "SELECT id, attempts FROM file_transfer_parts WHERE transfer_id = ? AND part_number = ?",
        (str(transfer_id), int(part_number)),
    ).fetchone()
    with _transaction(store):
        if existing is None:
            store.db.execute(
                """
                INSERT INTO file_transfer_parts
                    (id, organization_id, transfer_id, part_number, size_bytes, content_hash, etag, attempts, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (str(uuid4()), actor.organization_id, str(transfer_id), int(part_number), len(content), digest, str(etag), timestamp, timestamp),
            )
        else:
            store.db.execute(
                "UPDATE file_transfer_parts SET size_bytes = ?, content_hash = ?, etag = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (len(content), digest, str(etag), timestamp, str(existing["id"])),
            )
        store.db.execute(
            """
            UPDATE file_transfer_sessions
            SET uploaded_size = (SELECT COALESCE(SUM(size_bytes), 0) FROM file_transfer_parts WHERE transfer_id = ?),
                status = 'uploading', updated_at = ?
            WHERE id = ?
            """,
            (str(transfer_id), timestamp, str(transfer_id)),
        )
    return {"part": _row_to_part(store.db.execute("SELECT * FROM file_transfer_parts WHERE transfer_id = ? AND part_number = ?", (str(transfer_id), int(part_number))).fetchone()),
            "cursor": list_transfer_parts(store, actor, transfer_id)}


@_with_schema
def complete_transfer_upload(store: Any, actor: WorkspaceActor, transfer_id: str) -> dict[str, Any]:
    """收口：所有片都到齐才拼装；拼完再比对整体 sha256（对不上就删对象并标 failed）。"""

    transfer = get_transfer(store, actor, transfer_id)
    cursor = list_transfer_parts(store, actor, transfer_id)
    upload_id = str(transfer.get("multipart_upload_id") or "")
    if not upload_id:
        raise WorkspaceError("workspace_transfer_not_found", "multipart_not_started")
    received_numbers = [item["part_number"] for item in cursor["parts"]]
    if not received_numbers:
        raise WorkspaceError("workspace_transfer_parts_missing", "none")
    expected_numbers = list(range(1, len(received_numbers) + 1))
    if received_numbers != expected_numbers:
        # 片号必须从 1 连续（对象存储的多分片也要求这样）：缺片就明确报缺哪些
        missing = [number for number in range(1, max(received_numbers) + 1) if number not in received_numbers]
        raise WorkspaceError("workspace_transfer_parts_missing", ",".join(str(item) for item in missing[:20]))
    expected_size = int(transfer.get("expected_size") or 0)
    received = int(cursor["received_bytes"])
    if expected_size and received != expected_size:
        # 差得少时先回答"还缺哪几片"（对续传最有用）：按已观察到的片大小推总片数，
        # 只有推不出缺片（例如末片被截短）才报长度不符
        if received < expected_size and received_numbers:
            observed_part = cursor["parts"][0]["size_bytes"] or 0
            if observed_part:
                expected_total = (expected_size + observed_part - 1) // observed_part
                missing = [number for number in range(1, expected_total + 1) if number not in received_numbers]
                if missing:
                    raise WorkspaceError("workspace_transfer_parts_missing", ",".join(str(item) for item in missing[:20]))
        raise WorkspaceError("workspace_transfer_size_mismatch", f"{received}!={expected_size}")
    try:
        stored = store.object_store.complete_multipart(upload_id, key=str(transfer["storage_key"]))
    except (KeyError, ValueError) as error:
        raise WorkspaceError("workspace_transfer_complete_failed", str(error)) from error
    expected_hash = transfer.get("expected_hash")
    timestamp = _now()
    if expected_hash and str(stored.content_hash) != str(expected_hash):
        # 整体对不上：删掉刚拼出来的对象，标 failed（不把坏数据交出去）
        try:
            store.object_store.delete(str(transfer["storage_key"]))
        except Exception:  # noqa: BLE001 - 删不掉不影响"拒绝交付"的结论
            pass
        with _transaction(store):
            store.db.execute(
                "UPDATE file_transfer_sessions SET status = 'failed', uploaded_size = ?, updated_at = ? WHERE id = ?",
                (received, timestamp, str(transfer_id)),
            )
        raise WorkspaceError("workspace_transfer_hash_mismatch", f"{stored.content_hash}!={expected_hash}")
    with _transaction(store):
        store.db.execute(
            """
            UPDATE file_transfer_sessions
            SET status = 'ready', uploaded_size = ?, expected_size = ?, source_hash = ?, multipart_upload_id = NULL, updated_at = ?
            WHERE id = ?
            """,
            (received, received, stored.content_hash, timestamp, str(transfer_id)),
        )
    return {"transfer": get_transfer(store, actor, transfer_id), "cursor": list_transfer_parts(store, actor, transfer_id)}


@_with_schema
def abort_transfer_upload(store: Any, actor: WorkspaceActor, transfer_id: str) -> dict[str, Any]:
    """放弃续传：作废对象存储里的分片会话并删掉已收的分片记录（不留半成品）。"""

    transfer = get_transfer(store, actor, transfer_id)
    upload_id = str(transfer.get("multipart_upload_id") or "")
    if upload_id:
        try:
            store.object_store.abort_multipart(upload_id, key=str(transfer["storage_key"]))
        except Exception:  # noqa: BLE001 - 后端可能已经没有这个会话了
            pass
    with _transaction(store):
        store.db.execute("DELETE FROM file_transfer_parts WHERE transfer_id = ?", (str(transfer_id),))
        store.db.execute(
            "UPDATE file_transfer_sessions SET status = 'failed', multipart_upload_id = NULL, uploaded_size = 0, updated_at = ? WHERE id = ?",
            (_now(), str(transfer_id)),
        )
    return {"transfer": get_transfer(store, actor, transfer_id), "aborted": True}


@_with_schema
def cleanup_expired_transfers(store: Any, actor: WorkspaceActor) -> dict[str, int]:
    """过期会话回收：对象删掉、状态置 expired（只处理自己组织的）。"""

    now = datetime.now(UTC)
    rows = store.db.execute(
        "SELECT * FROM file_transfer_sessions WHERE organization_id = ? AND status IN ('initialized', 'uploading', 'ready')",
        (actor.organization_id,),
    ).fetchall()
    expired = 0
    deleted = 0
    for row in rows:
        deadline = _parse_time(_field(row, "expires_at"))
        if deadline is None or deadline > now:
            continue
        upload_id = str(_field(row, "multipart_upload_id") or "")
        if upload_id:
            # 半成品分片也要清掉（否则 .multipart/ 会一直涨）
            try:
                store.object_store.abort_multipart(upload_id, key=str(row["storage_key"]))
            except Exception:  # noqa: BLE001
                pass
        try:
            store.object_store.delete(str(row["storage_key"]))
            deleted += 1
        except Exception:  # noqa: BLE001 - 删不掉就把状态留成 expired，对象留给运维扫描
            pass
        with _transaction(store):
            store.db.execute("DELETE FROM file_transfer_parts WHERE transfer_id = ?", (str(row["id"]),))
            store.db.execute(
                "UPDATE file_transfer_sessions SET status = 'expired', multipart_upload_id = NULL, updated_at = ? WHERE id = ?",
                (_now(), str(row["id"])),
            )
        expired += 1
    return {"expired": expired, "objects_deleted": deleted}


@_with_schema
def audit_log(store: Any, actor: WorkspaceActor, *, workspace_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    clauses = ["organization_id = ?"]
    params: list[Any] = [actor.organization_id]
    if workspace_id:
        clauses.append("workspace_id = ?")
        params.append(str(workspace_id))
    rows = store.db.execute(
        f"SELECT * FROM workspace_audit WHERE {' AND '.join(clauses)} ORDER BY created_at DESC LIMIT ?",
        (*params, max(1, min(int(limit or 100), 500))),
    ).fetchall()
    return [dict(row) for row in rows]


def protected_paths_of(workspace: dict[str, Any]) -> list[str]:
    """保护目录（相对路径）：删除/移动这些路径（或它们的祖先）必须被拒绝。"""

    defaults = [".git", ".math-agent-platform", "node_modules"]
    values = list(workspace.get("protected_paths") or [])
    for item in defaults:
        if item not in values:
            values.append(item)
    return values


def is_protected(workspace: dict[str, Any], relative_path: str, *, root_is_protected: bool = True) -> bool:
    """相对路径是否落在保护范围里（含空路径 = 工作区根）。"""

    path = str(relative_path or "")
    if not path:
        return bool(root_is_protected)
    for protected in protected_paths_of(workspace):
        if path == protected or path.startswith(f"{protected}/"):
            return True
    return False


__all__ = [
    "DEFAULT_PART_SIZE",
    "LARGE_FILE_BYTES",
    "MAX_PART_NUMBER",
    "OPERATION_STATUSES",
    "OPERATION_TYPES",
    "TERMINAL_STATUSES",
    "WRITE_OPERATION_TYPES",
    "WorkspaceActor",
    "WorkspaceError",
    "abort_transfer_upload",
    "actor_for",
    "audit_log",
    "cancel_operation",
    "claim_operations",
    "cleanup_expired_transfers",
    "complete_operation",
    "complete_transfer_upload",
    "create_operation",
    "create_transfer",
    "ensure_schema",
    "expire_stale_operations",
    "get_operation",
    "get_transfer",
    "get_workspace",
    "is_protected",
    "list_operations",
    "list_transfer_parts",
    "list_transfers",
    "list_workspaces",
    "mark_agent_offline",
    "path_hash",
    "progress_operation",
    "protected_paths_of",
    "read_transfer_content",
    "register_workspace",
    "request_hash",
    "start_operation",
    "touch_workspace",
    "upload_transfer_part",
    "validate_relative_path",
    "workspace_identity_for",
    "write_transfer_content",
]