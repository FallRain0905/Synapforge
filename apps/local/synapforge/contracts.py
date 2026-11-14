"""契约成果物与版本绑定（本地层）。

**要解决的问题（实测踩到的）**：两个 Agent 通过 `ask/answer` 谈妥了接口，各自开工；
其中一方后来改了契约，另一方毫不知情，两边都"觉得自己确认过了"，最后产物合不起来。
消息是有上下文的、易失的、没有身份的——**同意必须有身份、有版本、可校验**。

设计分工：

| 对象 | 承载 |
|---|---|
| 契约正文 | **复用 artifact**：内容进对象存储、有完整 `content_hash`、有 receipt |
| 契约身份 | 本模块两表：`key → 当前 version + hash + artifact_id`（artifact 每次改写都是新 id，没有稳定身份） |
| 绑定 | 本模块：`(contract, agent) → version + hash`，表示"我正在按这一版写" |
| 同意留痕 | **复用事件**：`project.contract.published` / `project.contract.bound` |

三处强制点：
1. **发布时**——新版本会让谁失效，当场报出来；
2. **收尾时**——绑的不是最新版就**拒绝 finish**（`contract_drift`），逼它重读重绑；
3. **开工时**——`inbox` 显示同步状态。

为什么收尾要"拒绝"而不是"警告"：警告会被忽略；而"拒绝收尾 + 必须重新 bind"意味着
Agent 必须重新读一遍契约——**绑定动作本身就是"我重新看过了"的证明**。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

SCHEMA = """
CREATE TABLE IF NOT EXISTS local_contracts (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    contract_key TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    published_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(project_id, contract_key)
);
CREATE TABLE IF NOT EXISTS local_contract_bindings (
    contract_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (contract_id, agent_id)
);
"""


class ContractError(RuntimeError):
    def __init__(self, code: str, errors: list[str] | None = None) -> None:
        self.code = code
        self.errors = list(errors or [])
        detail = "; ".join(self.errors)
        super().__init__(f"{code}: {detail}" if detail else code)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _uuid(value: Any) -> str:
    return str(value if isinstance(value, UUID) else UUID(str(value)))


def ensure_schema(store: Any) -> None:
    store.db.executescript(SCHEMA)
    store.db.commit()


def canonical_bytes(body: Any) -> bytes:
    """契约正文的规范化字节：键排序、无空白、UTF-8。哈希的可复现基础。"""

    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def content_hash(body: Any) -> str:
    return hashlib.sha256(canonical_bytes(body)).hexdigest()


def latest(store: Any, project_id: Any, key: str) -> dict[str, Any] | None:
    ensure_schema(store)
    row = store.db.execute(
        "SELECT * FROM local_contracts WHERE project_id = ? AND contract_key = ?",
        (_uuid(project_id), str(key))).fetchone()
    return dict(row) if row else None


def publish(store: Any, *, project_id: Any, key: str, body: Any, artifact_id: Any,
            published_by: str) -> dict[str, Any]:
    """登记一个新版本。正文由调用方先落成 artifact（拿到 hash 与 receipt）。"""

    ensure_schema(store)
    pid, k = _uuid(project_id), str(key)
    digest = content_hash(body)
    timestamp = _now()
    current = latest(store, pid, k)

    if current is None:
        contract_id = str(uuid4())
        version = 1
        store.db.execute(
            "INSERT INTO local_contracts (id, project_id, contract_key, version, content_hash,"
            " artifact_id, published_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (contract_id, pid, k, version, digest, _uuid(artifact_id), published_by,
             timestamp, timestamp))
    else:
        contract_id = str(current["id"])
        if str(current["content_hash"]) == digest:
            # 正文没变就不造新版本：同一个 hash 反复"发布"会让版本号失去意义
            version = int(current["version"])
        else:
            version = int(current["version"]) + 1
            store.db.execute(
                "UPDATE local_contracts SET version = ?, content_hash = ?, artifact_id = ?,"
                " published_by = ?, updated_at = ? WHERE id = ?",
                (version, digest, _uuid(artifact_id), published_by, timestamp, contract_id))
    store.db.commit()

    # 发布后立刻算出"谁被这次改动甩下了"——这是原来完全缺失的信号
    stale = store.db.execute(
        "SELECT b.agent_id, b.version, b.content_hash FROM local_contract_bindings b"
        " WHERE b.contract_id = ? AND (b.version != ? OR b.content_hash != ?)",
        (contract_id, version, digest)).fetchall()
    stale_bindings = [{"agent_id": r["agent_id"], "version": int(r["version"]),
                       "content_hash": r["content_hash"]} for r in stale]

    store.add_event(UUID(pid), "project.contract.published", published_by,
                    {"contract_key": k, "version": version, "content_hash": digest,
                     "artifact_id": _uuid(artifact_id),
                     "stale_bindings": stale_bindings},
                    actor_kind="agent")
    store.db.commit()
    return {"contract_id": contract_id, "contract_key": k, "version": version,
            "content_hash": digest, "artifact_id": _uuid(artifact_id),
            "stale_bindings": stale_bindings}


def bind(store: Any, *, project_id: Any, key: str, agent_id: str,
         version: int | None = None, task_id: Any = None) -> dict[str, Any]:
    """声明"我按这一版写"。不指定版本就绑当前最新版。"""

    ensure_schema(store)
    pid, k = _uuid(project_id), str(key)
    current = latest(store, pid, k)
    if current is None:
        raise ContractError("contract_not_found", [f"{k}: 还没有发布过契约"])
    target = int(current["version"]) if version is None else int(version)
    if target != int(current["version"]):
        raise ContractError("contract_version_not_current", [
            f"{k}: 只能绑定当前版本 v{current['version']}（请求的是 v{target}）；"
            f"绑旧版本等于明知不同步还继续，没有意义"])

    digest = str(current["content_hash"])
    contract_id = str(current["id"])
    store.db.execute(
        "INSERT INTO local_contract_bindings (contract_id, agent_id, version, content_hash,"
        " task_id, created_at) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(contract_id, agent_id) DO UPDATE SET"
        " version=excluded.version, content_hash=excluded.content_hash,"
        " task_id=excluded.task_id, created_at=excluded.created_at",
        (contract_id, agent_id, target, digest,
         str(task_id) if task_id else None, _now()))
    store.db.commit()
    store.add_event(UUID(pid), "project.contract.bound", agent_id,
                    {"contract_key": k, "version": target, "content_hash": digest,
                     "task_id": str(task_id) if task_id else None},
                    actor_kind="agent")
    store.db.commit()
    return {"contract_key": k, "version": target, "content_hash": digest,
            "in_sync": True}


def binding_for(store: Any, *, project_id: Any, key: str, agent_id: str) -> dict[str, Any] | None:
    ensure_schema(store)
    row = store.db.execute(
        "SELECT b.* FROM local_contract_bindings b"
        " JOIN local_contracts c ON c.id = b.contract_id"
        " WHERE c.project_id = ? AND c.contract_key = ? AND b.agent_id = ?",
        (_uuid(project_id), str(key), agent_id)).fetchone()
    return dict(row) if row else None


def check_drift(store: Any, *, project_id: Any, agent_id: str,
                keys: list[str]) -> list[dict[str, Any]]:
    """收尾前的硬闸门：声明的必需契约必须已绑定且与最新版一致。"""

    problems: list[dict[str, Any]] = []
    for key in keys:
        current = latest(store, project_id, key)
        if current is None:
            problems.append({"contract_key": key, "problem": "not_published"})
            continue
        mine = binding_for(store, project_id=project_id, key=key, agent_id=agent_id)
        if mine is None:
            problems.append({"contract_key": key, "problem": "not_bound",
                             "latest_version": int(current["version"]),
                             "latest_hash": str(current["content_hash"])})
            continue
        if int(mine["version"]) != int(current["version"]) or \
                str(mine["content_hash"]) != str(current["content_hash"]):
            problems.append({"contract_key": key, "problem": "drifted",
                             "bound_version": int(mine["version"]),
                             "bound_hash": str(mine["content_hash"]),
                             "latest_version": int(current["version"]),
                             "latest_hash": str(current["content_hash"])})
    return problems


def status_for_agent(store: Any, *, project_id: Any, agent_id: str,
                     keys: list[str] | None = None) -> list[dict[str, Any]]:
    """给 inbox 用：每个契约的最新版 vs 我的绑定。"""

    ensure_schema(store)
    rows = store.db.execute(
        "SELECT * FROM local_contracts WHERE project_id = ? ORDER BY contract_key",
        (_uuid(project_id),)).fetchall()
    wanted = set(keys) if keys else None
    out: list[dict[str, Any]] = []
    for row in rows:
        key = str(row["contract_key"])
        if wanted is not None and key not in wanted:
            continue
        mine = binding_for(store, project_id=project_id, key=key, agent_id=agent_id)
        bound_version = int(mine["version"]) if mine else None
        out.append({
            "contract_key": key,
            "latest_version": int(row["version"]),
            "latest_hash": str(row["content_hash"]),
            "published_by": row["published_by"],
            "my_version": bound_version,
            "in_sync": mine is not None and bound_version == int(row["version"])
                       and str(mine["content_hash"]) == str(row["content_hash"]),
        })
    return out


__all__ = ["ContractError", "bind", "binding_for", "canonical_bytes", "check_drift",
           "content_hash", "ensure_schema", "latest", "publish", "status_for_agent"]
