"""Studio 的本地表：集群、角色、运行、进程记录。

只做持久化，不做业务判断；业务在 `studio/commands.py`，事实源仍是同一个
`state.db`（WAL）。所有函数接收 `synapforge.db.open_store()` 返回的 Store。
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

CLUSTER_STATUSES = ("READY", "RUNNING", "PAUSED", "STOPPED", "DONE", "FAILED")
RUN_STATUSES = ("RUNNING", "PAUSED", "STOPPED", "DONE", "FAILED")
ROLE_BACKENDS = ("claude", "command")

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_clusters (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    name TEXT NOT NULL,
    objective TEXT NOT NULL,
    workspace_root TEXT NOT NULL,
    lead_role_id TEXT,
    status TEXT NOT NULL DEFAULT 'READY',
    policy_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_cluster_roles (
    id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    name TEXT NOT NULL,
    agent_id TEXT NOT NULL UNIQUE,
    local_agent_id TEXT NOT NULL DEFAULT '',
    role_prompt TEXT NOT NULL DEFAULT '',
    backend TEXT NOT NULL DEFAULT 'claude',
    model TEXT NOT NULL DEFAULT '',
    args_json TEXT NOT NULL DEFAULT '[]',
    system_prompt TEXT NOT NULL DEFAULT '',
    workspace TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    budget_json TEXT NOT NULL DEFAULT '{}',
    requires_review INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_local_cluster_roles_cluster
    ON local_cluster_roles(cluster_id, sort_order);

CREATE TABLE IF NOT EXISTS local_cluster_runs (
    id TEXT PRIMARY KEY,
    cluster_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    round INTEGER NOT NULL DEFAULT 0,
    started_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    stopped_at TEXT,
    checkpoint_json TEXT NOT NULL DEFAULT '{}',
    budget_used_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_local_cluster_runs_cluster
    ON local_cluster_runs(cluster_id, started_at DESC);

CREATE TABLE IF NOT EXISTS local_cluster_processes (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    pid INTEGER,
    status TEXT NOT NULL DEFAULT 'STARTING',
    command_json TEXT NOT NULL DEFAULT '[]',
    log_path TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    exit_code INTEGER
);

CREATE INDEX IF NOT EXISTS idx_local_cluster_processes_run
    ON local_cluster_processes(run_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_local_cluster_processes_role
    ON local_cluster_processes(role_id, started_at DESC);

CREATE TABLE IF NOT EXISTS local_providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL DEFAULT 'anthropic',
    base_url TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    models_json TEXT NOT NULL DEFAULT '[]',
    note TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    is_default INTEGER NOT NULL DEFAULT 0,
    last_check_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_provider_keys (
    id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT '',
    last_check_json TEXT NOT NULL DEFAULT '{}',
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_local_provider_keys_provider
    ON local_provider_keys(provider_id, sort_order);

CREATE TABLE IF NOT EXISTS local_agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    backend TEXT NOT NULL DEFAULT 'claude',
    provider_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    command TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    workspace TEXT NOT NULL,
    allow_bash INTEGER NOT NULL DEFAULT 0,
    allow_web INTEGER NOT NULL DEFAULT 1,
    skills_json TEXT NOT NULL DEFAULT '[]',
    mcp_json TEXT NOT NULL DEFAULT '[]',
    mcp_managed INTEGER NOT NULL DEFAULT 0,
    plugins_json TEXT NOT NULL DEFAULT '[]',
    project_id TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_agent_sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_local_agent_sessions_agent
    ON local_agent_sessions(agent_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS local_plugins (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    version TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    skills_json TEXT NOT NULL DEFAULT '[]',
    mcp_json TEXT NOT NULL DEFAULT '[]',
    agents_json TEXT NOT NULL DEFAULT '[]',
    left_json TEXT NOT NULL DEFAULT '[]',
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_profiles (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    note TEXT NOT NULL DEFAULT '',
    snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS local_agent_runs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    task_id TEXT NOT NULL DEFAULT '',
    task_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RUNNING',
    backend TEXT NOT NULL DEFAULT 'claude',
    provider_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    workspace TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    usage_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    artifact_ids_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_local_agent_runs_agent
    ON local_agent_runs(agent_id, started_at DESC);

CREATE TABLE IF NOT EXISTS local_agent_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_local_agent_steps_run_seq
    ON local_agent_steps(run_id, seq);
"""


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def ensure_schema(store: Any) -> None:
    store.db.executescript(SCHEMA)
    store.db.commit()
    _ensure_column(store, "local_agents", "session_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(store, "local_agents", "allow_web", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(store, "local_agents", "provider_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(store, "local_agents", "command", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(store, "local_agents", "skills_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(store, "local_agents", "mcp_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(store, "local_agents", "mcp_managed", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(store, "local_agents", "plugins_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(store, "local_agent_runs", "provider_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(store, "local_agent_runs", "backend", "TEXT NOT NULL DEFAULT 'claude'")
    _ensure_column(store, "local_cluster_roles", "local_agent_id", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(store, "local_cluster_roles", "role_prompt", "TEXT NOT NULL DEFAULT ''")


def _ensure_column(store: Any, table: str, column: str, ddl: str) -> None:
    """老库补列：`CREATE TABLE IF NOT EXISTS` 不会给已有表加列。"""

    try:
        columns = {row["name"] for row in
                   store.db.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:  # noqa: BLE001
        return
    if column not in columns:
        store.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        store.db.commit()


def _load(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _cluster(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "project_id": row["project_id"],
        "name": row["name"],
        "objective": row["objective"],
        "workspace_root": row["workspace_root"],
        "lead_role_id": row["lead_role_id"],
        "status": row["status"],
        "policy": _load(row["policy_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _role(row: Any) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "cluster_id": row["cluster_id"],
        "name": row["name"],
        "agent_id": row["agent_id"],
        "local_agent_id": row["local_agent_id"] if "local_agent_id" in keys else "",
        "role_prompt": row["role_prompt"] if "role_prompt" in keys else "",
        "backend": row["backend"],
        "model": row["model"],
        "args": _load(row["args_json"], []),
        "system_prompt": row["system_prompt"],
        "workspace": row["workspace"],
        "capabilities": _load(row["capabilities_json"], []),
        "budget": _load(row["budget_json"], {}),
        "requires_review": bool(row["requires_review"]),
        "sort_order": int(row["sort_order"]),
        "created_at": row["created_at"],
    }


def _run(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "cluster_id": row["cluster_id"],
        "status": row["status"],
        "round": int(row["round"]),
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "stopped_at": row["stopped_at"],
        "checkpoint": _load(row["checkpoint_json"], {}),
        "budget_used": _load(row["budget_used_json"], {}),
    }


def _process(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "role_id": row["role_id"],
        "pid": row["pid"],
        "status": row["status"],
        "command": _load(row["command_json"], []),
        "log_path": row["log_path"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "exit_code": row["exit_code"],
    }


# ---- clusters ---------------------------------------------------------------


def create_cluster(store: Any, *, project_id: str, name: str, objective: str,
                   workspace_root: str, policy: dict[str, Any] | None = None,
                   lead_role_id: str | None = None) -> dict[str, Any]:
    cluster_id = new_id("cluster")
    created = now_iso()
    store.db.execute(
        "INSERT INTO local_clusters (id, project_id, name, objective, workspace_root,"
        " lead_role_id, status, policy_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'READY', ?, ?, ?)",
        (cluster_id, project_id, name, objective, workspace_root, lead_role_id,
         json.dumps(policy or {}, ensure_ascii=False), created, created),
    )
    store.db.commit()
    return get_cluster(store, cluster_id)  # type: ignore[return-value]


def get_cluster(store: Any, cluster_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_clusters WHERE id = ?", (str(cluster_id),)).fetchone()
    return _cluster(row) if row else None


def list_clusters(store: Any) -> list[dict[str, Any]]:
    rows = store.db.execute("SELECT * FROM local_clusters ORDER BY created_at DESC").fetchall()
    return [_cluster(r) for r in rows]


def update_cluster(store: Any, cluster_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"project_id", "name", "objective", "workspace_root", "lead_role_id", "status", "policy"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "policy":
            sets.append("policy_json = ?")
            params.append(json.dumps(value or {}, ensure_ascii=False))
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return get_cluster(store, cluster_id)
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(str(cluster_id))
    store.db.execute(f"UPDATE local_clusters SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_cluster(store, cluster_id)


def delete_cluster(store: Any, cluster_id: str) -> None:
    """只删 studio 自己的三张表；任务/事件/成果物保留为历史。"""

    store.db.execute("DELETE FROM local_cluster_roles WHERE cluster_id = ?", (str(cluster_id),))
    store.db.execute("DELETE FROM local_cluster_runs WHERE cluster_id = ?", (str(cluster_id),))
    store.db.execute("DELETE FROM local_clusters WHERE id = ?", (str(cluster_id),))
    store.db.commit()


# ---- roles ------------------------------------------------------------------


def add_role(store: Any, *, cluster_id: str, name: str, agent_id: str,
             backend: str = "claude", model: str = "", args: list[str] | None = None,
             system_prompt: str = "", workspace: str = "",
             capabilities: list[str] | None = None,
             budget: dict[str, Any] | None = None,
             requires_review: bool = False, sort_order: int = 0,
             local_agent_id: str = "", role_prompt: str = "") -> dict[str, Any]:
    role_id = new_id("role")
    store.db.execute(
        "INSERT INTO local_cluster_roles (id, cluster_id, name, agent_id, local_agent_id,"
        " role_prompt, backend, model, args_json, system_prompt, workspace,"
        " capabilities_json, budget_json, requires_review, sort_order, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (role_id, str(cluster_id), name, agent_id, str(local_agent_id or ""),
         str(role_prompt or ""), backend, model,
         json.dumps(args or [], ensure_ascii=False), system_prompt, workspace,
         json.dumps(capabilities or [], ensure_ascii=False),
         json.dumps(budget or {}, ensure_ascii=False),
         1 if requires_review else 0, int(sort_order), now_iso()),
    )
    store.db.commit()
    return get_role(store, role_id)  # type: ignore[return-value]


def get_role(store: Any, role_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_cluster_roles WHERE id = ?", (str(role_id),)).fetchone()
    return _role(row) if row else None


def list_roles(store: Any, cluster_id: str) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_cluster_roles WHERE cluster_id = ?"
        " ORDER BY sort_order ASC, created_at ASC", (str(cluster_id),)).fetchall()
    return [_role(r) for r in rows]


def delete_role(store: Any, role_id: str) -> None:
    store.db.execute("DELETE FROM local_cluster_roles WHERE id = ?", (str(role_id),))
    store.db.commit()


def update_role(store: Any, role_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "backend", "model", "args", "system_prompt", "workspace",
               "capabilities", "budget", "requires_review", "sort_order",
               "local_agent_id", "role_prompt"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {"args", "capabilities", "budget"}:
            sets.append(f"{key}_json = ?")
            params.append(json.dumps(value or ([] if key != "budget" else {}), ensure_ascii=False))
        elif key == "requires_review":
            sets.append("requires_review = ?")
            params.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return get_role(store, role_id)
    params.append(str(role_id))
    store.db.execute(f"UPDATE local_cluster_roles SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_role(store, role_id)


# ---- runs -------------------------------------------------------------------


def create_run(store: Any, cluster_id: str) -> dict[str, Any]:
    run_id = new_id("run")
    created = now_iso()
    store.db.execute(
        "INSERT INTO local_cluster_runs (id, cluster_id, status, round, started_at,"
        " updated_at, checkpoint_json, budget_used_json)"
        " VALUES (?, ?, 'RUNNING', 0, ?, ?, '{}', '{}')",
        (run_id, str(cluster_id), created, created),
    )
    store.db.commit()
    return get_run(store, run_id)  # type: ignore[return-value]


def get_run(store: Any, run_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_cluster_runs WHERE id = ?", (str(run_id),)).fetchone()
    return _run(row) if row else None


def latest_run(store: Any, cluster_id: str) -> dict[str, Any] | None:
    row = store.db.execute(
        "SELECT * FROM local_cluster_runs WHERE cluster_id = ?"
        " ORDER BY started_at DESC LIMIT 1", (str(cluster_id),)).fetchone()
    return _run(row) if row else None


def list_runs(store: Any, cluster_id: str | None = None) -> list[dict[str, Any]]:
    if cluster_id:
        rows = store.db.execute(
            "SELECT * FROM local_cluster_runs WHERE cluster_id = ?"
            " ORDER BY started_at DESC", (str(cluster_id),)).fetchall()
    else:
        rows = store.db.execute(
            "SELECT * FROM local_cluster_runs ORDER BY started_at DESC").fetchall()
    return [_run(r) for r in rows]


def update_run(store: Any, run_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"status", "round", "stopped_at", "checkpoint", "budget_used"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {"checkpoint", "budget_used"}:
            sets.append(f"{key}_json = ?")
            params.append(json.dumps(value or {}, ensure_ascii=False))
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return get_run(store, run_id)
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(str(run_id))
    store.db.execute(f"UPDATE local_cluster_runs SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_run(store, run_id)


# ---- processes --------------------------------------------------------------


def create_process(store: Any, *, run_id: str, role_id: str, pid: int | None,
                   command: list[str], log_path: str,
                   status: str = "RUNNING") -> dict[str, Any]:
    process_id = new_id("proc")
    store.db.execute(
        "INSERT INTO local_cluster_processes (id, run_id, role_id, pid, status,"
        " command_json, log_path, started_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (process_id, str(run_id), str(role_id), pid, status,
         json.dumps(command, ensure_ascii=False), log_path, now_iso()),
    )
    store.db.commit()
    return get_process(store, process_id)  # type: ignore[return-value]


def get_process(store: Any, process_id: str) -> dict[str, Any] | None:
    row = store.db.execute(
        "SELECT * FROM local_cluster_processes WHERE id = ?", (str(process_id),)).fetchone()
    return _process(row) if row else None


def finish_process(store: Any, process_id: str, *, status: str,
                   exit_code: int | None) -> None:
    store.db.execute(
        "UPDATE local_cluster_processes SET status = ?, exit_code = ?, finished_at = ?"
        " WHERE id = ?", (status, exit_code, now_iso(), str(process_id)))
    store.db.commit()


def update_process_pid(store: Any, process_id: str, pid: int | None) -> None:
    store.db.execute(
        "UPDATE local_cluster_processes SET pid = ?, status = 'RUNNING' WHERE id = ?",
        (pid, str(process_id)))
    store.db.commit()


def list_processes(store: Any, run_id: str) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_cluster_processes WHERE run_id = ?"
        " ORDER BY started_at DESC", (str(run_id),)).fetchall()
    return [_process(r) for r in rows]


def latest_process_for_role(store: Any, role_id: str) -> dict[str, Any] | None:
    row = store.db.execute(
        "SELECT * FROM local_cluster_processes WHERE role_id = ?"
        " ORDER BY started_at DESC LIMIT 1", (str(role_id),)).fetchone()
    return _process(row) if row else None


def running_processes(store: Any) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_cluster_processes WHERE status IN ('STARTING','RUNNING')"
        " ORDER BY started_at DESC").fetchall()
    return [_process(r) for r in rows]


# ---- 单 Agent 工作台 --------------------------------------------------------


def _provider(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "kind": row["kind"],
        "base_url": row["base_url"],
        "api_key": row["api_key"],
        "models": _load(row["models_json"], []),
        "note": row["note"],
        "enabled": bool(row["enabled"]),
        "is_default": bool(row["is_default"]),
        "last_check": _load(row["last_check_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _provider_key(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "provider_id": row["provider_id"],
        "label": row["label"],
        "api_key": row["api_key"],
        "enabled": bool(row["enabled"]),
        "status": row["status"],
        "last_check": _load(row["last_check_json"], {}),
        "sort_order": int(row["sort_order"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def add_provider_key(store: Any, *, provider_id: str, api_key: str,
                     label: str = "", enabled: bool = True,
                     status: str = "") -> dict[str, Any]:
    key_id = new_id("pkey")
    created = now_iso()
    row = store.db.execute(
        "SELECT COALESCE(MAX(sort_order), -1) AS n FROM local_provider_keys"
        " WHERE provider_id = ?", (str(provider_id),)).fetchone()
    order = int(row["n"]) + 1
    store.db.execute(
        "INSERT INTO local_provider_keys (id, provider_id, label, api_key, enabled,"
        " status, last_check_json, sort_order, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?)",
        (key_id, str(provider_id), label, api_key, 1 if enabled else 0, status,
         order, created, created))
    store.db.commit()
    return get_provider_key(store, key_id)  # type: ignore[return-value]


def get_provider_key(store: Any, key_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_provider_keys WHERE id = ?",
                           (str(key_id),)).fetchone()
    return _provider_key(row) if row else None


def list_provider_keys(store: Any, provider_id: str) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_provider_keys WHERE provider_id = ?"
        " ORDER BY sort_order ASC, created_at ASC", (str(provider_id),)).fetchall()
    return [_provider_key(row) for row in rows]


def list_all_provider_keys(store: Any) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_provider_keys ORDER BY provider_id, sort_order").fetchall()
    return [_provider_key(row) for row in rows]


def update_provider_key(store: Any, key_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"label", "api_key", "enabled", "status", "last_check", "sort_order"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "last_check":
            sets.append("last_check_json = ?")
            params.append(json.dumps(value or {}, ensure_ascii=False))
        elif key == "enabled":
            sets.append("enabled = ?")
            params.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return get_provider_key(store, key_id)
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(str(key_id))
    store.db.execute(f"UPDATE local_provider_keys SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_provider_key(store, key_id)


def delete_provider_key(store: Any, key_id: str) -> None:
    store.db.execute("DELETE FROM local_provider_keys WHERE id = ?", (str(key_id),))
    store.db.commit()


def delete_provider_keys(store: Any, provider_id: str) -> None:
    store.db.execute("DELETE FROM local_provider_keys WHERE provider_id = ?",
                     (str(provider_id),))
    store.db.commit()


def _agent(row: Any) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "name": row["name"],
        "backend": row["backend"],
        "provider_id": row["provider_id"] if "provider_id" in keys else "",
        "model": row["model"],
        "command": row["command"] if "command" in keys else "",
        "system_prompt": row["system_prompt"],
        "workspace": row["workspace"],
        "allow_bash": bool(row["allow_bash"]),
        "allow_web": bool(row["allow_web"]) if "allow_web" in keys else True,
        "skills": _load(row["skills_json"], []) if "skills_json" in keys else [],
        "mcp": _load(row["mcp_json"], []) if "mcp_json" in keys else [],
        "mcp_managed": bool(row["mcp_managed"]) if "mcp_managed" in keys else False,
        "plugins": _load(row["plugins_json"], []) if "plugins_json" in keys else [],
        "project_id": row["project_id"],
        "session_id": row["session_id"] if "session_id" in keys else "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _agent_run(row: Any) -> dict[str, Any]:
    keys = row.keys()
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "project_id": row["project_id"],
        "task_id": row["task_id"],
        "task_text": row["task_text"],
        "status": row["status"],
        "backend": row["backend"] if "backend" in keys else "claude",
        "provider_id": row["provider_id"] if "provider_id" in keys else "",
        "model": row["model"],
        "session_id": row["session_id"],
        "workspace": row["workspace"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "usage": _load(row["usage_json"], {}),
        "error": row["error"],
        "artifact_ids": _load(row["artifact_ids_json"], []),
    }


def _step(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "run_id": row["run_id"],
        "seq": int(row["seq"]),
        "kind": row["kind"],
        "title": row["title"],
        "detail": row["detail"],
        "payload": _load(row["payload_json"], {}),
        "created_at": row["created_at"],
    }


def create_provider(store: Any, *, name: str, kind: str = "anthropic",
                    base_url: str = "", api_key: str = "",
                    models: list[str] | None = None, note: str = "",
                    enabled: bool = True, is_default: bool = False) -> dict[str, Any]:
    provider_id = new_id("prov")
    created = now_iso()
    if is_default:
        store.db.execute("UPDATE local_providers SET is_default = 0")
    store.db.execute(
        "INSERT INTO local_providers (id, name, kind, base_url, api_key, models_json,"
        " note, enabled, is_default, last_check_json, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)",
        (provider_id, name, kind, base_url, api_key,
         json.dumps(models or [], ensure_ascii=False), note,
         1 if enabled else 0, 1 if is_default else 0, created, created))
    store.db.commit()
    return get_provider(store, provider_id)  # type: ignore[return-value]


def get_provider(store: Any, provider_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_providers WHERE id = ?",
                           (str(provider_id),)).fetchone()
    return _provider(row) if row else None


def get_provider_by_name(store: Any, name: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_providers WHERE name = ?",
                           (str(name),)).fetchone()
    return _provider(row) if row else None


def list_providers(store: Any) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_providers ORDER BY is_default DESC, created_at ASC").fetchall()
    return [_provider(r) for r in rows]


def update_provider(store: Any, provider_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "kind", "base_url", "api_key", "models", "note",
               "enabled", "is_default", "last_check"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "models":
            sets.append("models_json = ?")
            params.append(json.dumps(value or [], ensure_ascii=False))
        elif key == "last_check":
            sets.append("last_check_json = ?")
            params.append(json.dumps(value or {}, ensure_ascii=False))
        elif key in {"enabled", "is_default"}:
            sets.append(f"{key} = ?")
            params.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return get_provider(store, provider_id)
    if fields.get("is_default"):
        store.db.execute("UPDATE local_providers SET is_default = 0")
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(str(provider_id))
    store.db.execute(f"UPDATE local_providers SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_provider(store, provider_id)


def delete_provider(store: Any, provider_id: str) -> None:
    store.db.execute("UPDATE local_agents SET provider_id = '' WHERE provider_id = ?",
                     (str(provider_id),))
    store.db.execute("DELETE FROM local_provider_keys WHERE provider_id = ?",
                     (str(provider_id),))
    store.db.execute("DELETE FROM local_providers WHERE id = ?", (str(provider_id),))
    store.db.commit()


def _session(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "agent_id": row["agent_id"],
        "title": row["title"],
        "archived": bool(row["archived"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def ensure_agent_session(store: Any, *, session_id: str, agent_id: str,
                         title: str = "") -> dict[str, Any] | None:
    """会话元数据幂等登记；title 只在已有为空时补。"""

    sid = str(session_id or "").strip()
    if not sid:
        return None
    row = store.db.execute("SELECT * FROM local_agent_sessions WHERE id = ?",
                           (sid,)).fetchone()
    now = now_iso()
    if row is None:
        store.db.execute(
            "INSERT INTO local_agent_sessions (id, agent_id, title, archived, created_at,"
            " updated_at) VALUES (?, ?, ?, 0, ?, ?)",
            (sid, str(agent_id), str(title or "")[:200], now, now))
    else:
        updates = ["updated_at = ?"]
        params: list[Any] = [now]
        if title and not row["title"]:
            updates.append("title = ?")
            params.append(str(title)[:200])
        params.append(sid)
        store.db.execute(
            f"UPDATE local_agent_sessions SET {', '.join(updates)} WHERE id = ?", params)
    store.db.commit()
    return get_agent_session(store, sid)


def get_agent_session(store: Any, session_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_agent_sessions WHERE id = ?",
                           (str(session_id),)).fetchone()
    return _session(row) if row else None


def list_agent_sessions(store: Any, agent_id: str | None = None) -> list[dict[str, Any]]:
    if agent_id:
        rows = store.db.execute(
            "SELECT * FROM local_agent_sessions WHERE agent_id = ?"
            " ORDER BY updated_at DESC", (str(agent_id),)).fetchall()
    else:
        rows = store.db.execute(
            "SELECT * FROM local_agent_sessions ORDER BY updated_at DESC").fetchall()
    return [_session(row) for row in rows]


def update_agent_session(store: Any, session_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"title", "archived"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "archived":
            sets.append("archived = ?")
            params.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?")
            params.append(str(value or "")[:200])
    if not sets:
        return get_agent_session(store, session_id)
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(str(session_id))
    store.db.execute(f"UPDATE local_agent_sessions SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_agent_session(store, session_id)


def _plugin(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "version": row["version"],
        "source": row["source"],
        "path": row["path"],
        "skills": _load(row["skills_json"], []),
        "mcp": _load(row["mcp_json"], []),
        "agents": _load(row["agents_json"], []),
        "left": _load(row["left_json"], []),
        "installed_at": row["installed_at"],
        "updated_at": row["updated_at"],
    }


def create_plugin(store: Any, *, name: str, description: str = "", version: str = "",
                  source: str = "", path: str = "", skills: list[Any] | None = None,
                  mcp: list[str] | None = None, agents: list[str] | None = None,
                  left: list[Any] | None = None) -> dict[str, Any]:
    plugin_id = new_id("plugin")
    created = now_iso()
    store.db.execute(
        "INSERT INTO local_plugins (id, name, description, version, source, path,"
        " skills_json, mcp_json, agents_json, left_json, installed_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (plugin_id, name, description, version, source, path,
         json.dumps(skills or [], ensure_ascii=False),
         json.dumps(mcp or [], ensure_ascii=False),
         json.dumps(agents or [], ensure_ascii=False),
         json.dumps(left or [], ensure_ascii=False), created, created))
    store.db.commit()
    return get_plugin(store, plugin_id)  # type: ignore[return-value]


def get_plugin(store: Any, plugin_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_plugins WHERE id = ?",
                           (str(plugin_id),)).fetchone()
    return _plugin(row) if row else None


def get_plugin_by_name(store: Any, name: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_plugins WHERE name = ?",
                           (str(name),)).fetchone()
    return _plugin(row) if row else None


def list_plugins(store: Any) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_plugins ORDER BY installed_at ASC").fetchall()
    return [_plugin(row) for row in rows]


def update_plugin(store: Any, plugin_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "description", "version", "source", "path", "skills",
               "mcp", "agents", "left"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {"skills", "mcp", "agents", "left"}:
            sets.append(f"{key}_json = ?")
            params.append(json.dumps(value or [], ensure_ascii=False))
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return get_plugin(store, plugin_id)
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(str(plugin_id))
    store.db.execute(f"UPDATE local_plugins SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_plugin(store, plugin_id)


def delete_plugin(store: Any, plugin_id: str) -> None:
    rows = store.db.execute("SELECT id, plugins_json FROM local_agents").fetchall()
    for row in rows:
        plugins = _load(row["plugins_json"], [])
        kept = [item for item in plugins if str(item) != str(plugin_id)]
        if len(kept) != len(plugins):
            store.db.execute("UPDATE local_agents SET plugins_json = ? WHERE id = ?",
                             (json.dumps(kept, ensure_ascii=False), row["id"]))
    store.db.execute("DELETE FROM local_plugins WHERE id = ?", (str(plugin_id),))
    store.db.commit()


def agents_using_plugin(store: Any, plugin_id: str) -> list[str]:
    out: list[str] = []
    for row in store.db.execute("SELECT id, name, plugins_json FROM local_agents").fetchall():
        if str(plugin_id) in [str(item) for item in _load(row["plugins_json"], [])]:
            out.append(row["name"])
    return out


def _profile(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "note": row["note"],
        "snapshot": _load(row["snapshot_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_profile(store: Any, *, name: str, note: str = "",
                   snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
    profile_id = new_id("profile")
    created = now_iso()
    store.db.execute(
        "INSERT INTO local_profiles (id, name, note, snapshot_json, created_at,"
        " updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (profile_id, name, note, json.dumps(snapshot or {}, ensure_ascii=False),
         created, created))
    store.db.commit()
    return get_profile(store, profile_id)  # type: ignore[return-value]


def get_profile(store: Any, profile_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_profiles WHERE id = ?",
                           (str(profile_id),)).fetchone()
    return _profile(row) if row else None


def get_profile_by_name(store: Any, name: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_profiles WHERE name = ?",
                           (str(name),)).fetchone()
    return _profile(row) if row else None


def list_profiles(store: Any) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_profiles ORDER BY created_at ASC").fetchall()
    return [_profile(row) for row in rows]


def update_profile(store: Any, profile_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "note", "snapshot"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key == "snapshot":
            sets.append("snapshot_json = ?")
            params.append(json.dumps(value or {}, ensure_ascii=False))
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return get_profile(store, profile_id)
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(str(profile_id))
    store.db.execute(f"UPDATE local_profiles SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_profile(store, profile_id)


def delete_profile(store: Any, profile_id: str) -> None:
    store.db.execute("DELETE FROM local_profiles WHERE id = ?", (str(profile_id),))
    store.db.commit()


def create_agent(store: Any, *, name: str, backend: str = "claude", model: str = "",
                 system_prompt: str = "", workspace: str = "",
                 allow_bash: bool = False, allow_web: bool = True,
                 project_id: str = "", session_id: str = "",
                 provider_id: str = "", command: str = "",
                 skills: list[str] | None = None, mcp: list[str] | None = None,
                 mcp_managed: bool = False, plugins: list[str] | None = None) -> dict[str, Any]:
    agent_id = new_id("agent")
    created = now_iso()
    store.db.execute(
        "INSERT INTO local_agents (id, name, backend, provider_id, model, command,"
        " system_prompt, workspace, allow_bash, allow_web, skills_json, mcp_json,"
        " mcp_managed, plugins_json, project_id, session_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (agent_id, name, backend, provider_id, model, command,
         system_prompt, workspace,
         1 if allow_bash else 0, 1 if allow_web else 0,
         json.dumps(skills or [], ensure_ascii=False),
         json.dumps(mcp or [], ensure_ascii=False),
         1 if mcp_managed else 0,
         json.dumps(plugins or [], ensure_ascii=False), project_id, session_id,
         created, created))
    store.db.commit()
    return get_agent(store, agent_id)  # type: ignore[return-value]


def get_agent(store: Any, agent_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_agents WHERE id = ?", (str(agent_id),)).fetchone()
    return _agent(row) if row else None


def list_agents(store: Any) -> list[dict[str, Any]]:
    rows = store.db.execute("SELECT * FROM local_agents ORDER BY created_at ASC").fetchall()
    return [_agent(r) for r in rows]


def update_agent(store: Any, agent_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"name", "backend", "provider_id", "model", "command", "system_prompt",
               "workspace", "allow_bash", "allow_web", "skills", "mcp", "mcp_managed",
               "plugins", "project_id", "session_id"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {"skills", "mcp", "plugins"}:
            sets.append(f"{key}_json = ?")
            params.append(json.dumps(value or [], ensure_ascii=False))
        elif key in {"allow_bash", "allow_web", "mcp_managed"}:
            sets.append(f"{key} = ?")
            params.append(1 if value else 0)
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return get_agent(store, agent_id)
    sets.append("updated_at = ?")
    params.append(now_iso())
    params.append(str(agent_id))
    store.db.execute(f"UPDATE local_agents SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_agent(store, agent_id)


def delete_agent(store: Any, agent_id: str) -> None:
    store.db.execute("DELETE FROM local_agent_steps WHERE run_id IN"
                     " (SELECT id FROM local_agent_runs WHERE agent_id = ?)", (str(agent_id),))
    store.db.execute("DELETE FROM local_agent_runs WHERE agent_id = ?", (str(agent_id),))
    store.db.execute("DELETE FROM local_agent_sessions WHERE agent_id = ?", (str(agent_id),))
    store.db.execute("DELETE FROM local_agents WHERE id = ?", (str(agent_id),))
    store.db.commit()


def create_agent_run(store: Any, *, agent_id: str, project_id: str, task_id: str,
                     task_text: str, model: str, workspace: str,
                     status: str = "QUEUED", backend: str = "claude",
                     provider_id: str = "") -> dict[str, Any]:
    run_id = new_id("arun")
    store.db.execute(
        "INSERT INTO local_agent_runs (id, agent_id, project_id, task_id, task_text,"
        " status, backend, provider_id, model, workspace, started_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, str(agent_id), str(project_id), str(task_id), task_text, status,
         backend, provider_id, model, workspace, now_iso()))
    store.db.commit()
    return get_agent_run(store, run_id)  # type: ignore[return-value]


def get_agent_run(store: Any, run_id: str) -> dict[str, Any] | None:
    row = store.db.execute("SELECT * FROM local_agent_runs WHERE id = ?", (str(run_id),)).fetchone()
    return _agent_run(row) if row else None


def latest_agent_run(store: Any, agent_id: str) -> dict[str, Any] | None:
    row = store.db.execute(
        "SELECT * FROM local_agent_runs WHERE agent_id = ?"
        " ORDER BY started_at DESC LIMIT 1", (str(agent_id),)).fetchone()
    return _agent_run(row) if row else None


def list_agent_runs(store: Any, agent_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_agent_runs WHERE agent_id = ?"
        " ORDER BY started_at DESC LIMIT ?", (str(agent_id), int(limit))).fetchall()
    return [_agent_run(r) for r in rows]


def next_queued_run(store: Any, agent_id: str) -> dict[str, Any] | None:
    row = store.db.execute(
        "SELECT * FROM local_agent_runs WHERE agent_id = ? AND status = 'QUEUED'"
        " ORDER BY started_at ASC LIMIT 1", (str(agent_id),)).fetchone()
    return _agent_run(row) if row else None


def queue_position(store: Any, agent_id: str) -> int:
    row = store.db.execute(
        "SELECT COUNT(*) AS n FROM local_agent_runs WHERE agent_id = ? AND status = 'QUEUED'",
        (str(agent_id),)).fetchone()
    return int(row["n"])


def list_all_agent_runs(store: Any, limit: int = 200) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT r.*, a.name AS agent_name FROM local_agent_runs r"
        " LEFT JOIN local_agents a ON a.id = r.agent_id"
        " ORDER BY r.started_at DESC LIMIT ?", (int(limit),)).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = _agent_run(row)
        item["agent_name"] = row["agent_name"] if "agent_name" in row.keys() else ""
        out.append(item)
    return out


def update_agent_run(store: Any, run_id: str, **fields: Any) -> dict[str, Any] | None:
    allowed = {"status", "model", "session_id", "finished_at", "usage", "error", "artifact_ids"}
    sets: list[str] = []
    params: list[Any] = []
    for key, value in fields.items():
        if key not in allowed:
            continue
        if key in {"usage", "artifact_ids"}:
            sets.append(f"{key}_json = ?")
            params.append(json.dumps(value or ([] if key == "artifact_ids" else {}),
                                     ensure_ascii=False))
        else:
            sets.append(f"{key} = ?")
            params.append(value)
    if not sets:
        return get_agent_run(store, run_id)
    params.append(str(run_id))
    store.db.execute(f"UPDATE local_agent_runs SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()
    return get_agent_run(store, run_id)


def append_agent_step(store: Any, *, run_id: str, kind: str, title: str,
                      detail: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    row = store.db.execute(
        "SELECT COALESCE(MAX(seq), 0) AS n FROM local_agent_steps WHERE run_id = ?",
        (str(run_id),)).fetchone()
    seq = int(row["n"]) + 1
    step_id = new_id("step")
    store.db.execute(
        "INSERT INTO local_agent_steps (id, run_id, seq, kind, title, detail,"
        " payload_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (step_id, str(run_id), seq, kind, title, detail[:20000],
         json.dumps(payload or {}, ensure_ascii=False), now_iso()))
    store.db.commit()
    return _step(store.db.execute(
        "SELECT * FROM local_agent_steps WHERE id = ?", (step_id,)).fetchone())


def update_agent_step(store: Any, step_id: str, *, title: str | None = None,
                      detail: str | None = None) -> None:
    sets: list[str] = []
    params: list[Any] = []
    if title is not None:
        sets.append("title = ?")
        params.append(str(title))
    if detail is not None:
        sets.append("detail = ?")
        params.append(str(detail)[:20000])
    if not sets:
        return
    params.append(str(step_id))
    store.db.execute(f"UPDATE local_agent_steps SET {', '.join(sets)} WHERE id = ?", params)
    store.db.commit()


def list_agent_steps(store: Any, run_id: str, *, after_seq: int = 0,
                     limit: int = 2000) -> list[dict[str, Any]]:
    rows = store.db.execute(
        "SELECT * FROM local_agent_steps WHERE run_id = ? AND seq > ?"
        " ORDER BY seq ASC LIMIT ?", (str(run_id), int(after_seq), int(limit))).fetchall()
    return [_step(r) for r in rows]

