"""显式跨空间传输（FM-6）：云盘 ⇄ Agent 工作区的**显式**动作，以及生产加固的维护口。

计划 §1 D4 的口径：**不做后台双向实时同步**，只提供显式动作：

```text
云盘 → Agent 工作区：把某个云盘节点的内容塞进一个传输会话，再入队一个 upload 操作
Agent 工作区 → 云盘：入队 download 操作（带传输会话），Agent 传完再由人「存进云盘」
```

两条路都复用已有零件（FM-1 的对象存储与配额、FM-3 的传输会话与操作队列），这里只做**编排**与
**冲突口径**：

- 云盘 → 工作区：目标同名时**默认不覆盖**（返回 `file_name_conflict`），除非显式 `overwrite=true`；
  内容按 sha256 校验（对象存储里那份与节点声称的一致才发出去）。
- 工作区 → 云盘：写进云盘时同名冲突返回 `file_name_conflict`（让用户选：改名 / 换目录 / 跳过），
  **不静默覆盖**；revision/hash 冲突走 FM-1 已有的 `file_revision_conflict` 与内容去重。

维护口（生产加固的一部分）：
- `transfer_history`：传输历史 + 失败原因（操作上的 `error_code`）；
- `cleanup_transfers`：过期会话回收（删对象 + 置 expired）——**先落库、后删对象**的口径不变；
- `scan_orphans`：孤儿对象**只报告不删**（对象存储列举能力在后端之间不一致，删无主对象不可逆）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from contextlib import contextmanager
from typing import Iterator

from . import drive, workspace_files


@contextmanager
def _transaction(store: Any) -> Iterator[None]:
    try:
        yield
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise


class TransferError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.detail = detail or ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _link_operation(store: Any, transfer_id: str, operation_id: str) -> None:
    """把会话与操作对上号。

    为什么必须显式做这一步：传输历史要回答"这次搬运成没成、为什么失败"，而失败原因记在**操作**上
    （`error_code`）。不回填这个字段，历史里就只有一堆孤立的会话——看不到成败，也就没法重试。
    """

    with _transaction(store):
        store.db.execute(
            "UPDATE file_transfer_sessions SET operation_id = ?, updated_at = ? WHERE id = ?",
            (str(operation_id), _now(), str(transfer_id)),
        )


# ---- 云盘 → Agent 工作区 ------------------------------------------------------


def drive_to_workspace(
    store: Any,
    actor: Any,
    *,
    node_id: str,
    workspace_id: str,
    relative_path: str | None = None,
    overwrite: bool = False,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """把云盘里的一个文件复制到 Agent 工作区（显式动作，不是同步）。"""

    drive_actor = drive.actor_for(store, actor.member_id)
    try:
        node, content = drive.read_content(store, drive_actor, str(node_id))
    except drive.DriveError as error:
        raise TransferError("file_node_not_found", error.detail) from error
    if node["kind"] != "file":
        raise TransferError("file_transfer_source_invalid", "not_a_file")

    workspace = workspace_files.get_workspace(store, actor, str(workspace_id))
    target = str(relative_path or node["name"]).strip() or str(node["name"])
    digest = hashlib.sha256(content).hexdigest()
    transfer = workspace_files.create_transfer(
        store,
        actor,
        source_type="drive",
        target_type="workspace",
        workspace_id=str(workspace_id),
        source_id=str(node_id),
        source_hash=digest,
        target_id=target,
        expected_size=len(content),
        expected_hash=digest,
        ttl_seconds=ttl_seconds,
    )
    # 内容从云盘搬到传输会话（平台内部搬，不经浏览器）
    workspace_files.write_transfer_content(store, actor, transfer["id"], content, mime_type=node.get("mime_type"))
    # 幂等键带上 transfer id：这是**显式动作**，再点一次就是再复制一次（同名默认不覆盖，
    # 第二次会在内核侧被拒并在操作里显示冲突）。用不含 transfer 的固定键反而危险——第二次调用会
    # 复用第一条操作（指向旧的传输会话），新会话就永远没人消费。要幂等请重试**同一条操作**。
    operation = workspace_files.create_operation(
        store,
        actor,
        str(workspace_id),
        operation_type="upload",
        relative_path=target,
        arguments={"transfer_id": transfer["id"], "overwrite": bool(overwrite), "source_drive_node_id": str(node_id)},
        idempotency_key=f"drive-to-workspace:{node_id}:{target}:{transfer['id']}",
    )
    _link_operation(store, transfer["id"], operation["id"])
    return {
        "transfer": workspace_files.get_transfer(store, actor, transfer["id"]),
        "operation": operation,
        "workspace": workspace,
        "source": node,
    }


# ---- Agent 工作区 → 云盘 ------------------------------------------------------


def workspace_to_drive(
    store: Any,
    actor: Any,
    *,
    workspace_id: str,
    relative_path: str,
    ttl_seconds: int = 3600,
) -> dict[str, Any]:
    """让 Agent 把一个工作区文件传进传输会话（最后由人「存进云盘」）。"""

    workspace = workspace_files.get_workspace(store, actor, str(workspace_id))
    path = workspace_files.validate_relative_path(str(relative_path), allow_empty=False)
    transfer = workspace_files.create_transfer(
        store,
        actor,
        source_type="workspace",
        target_type="drive",
        workspace_id=str(workspace_id),
        source_id=path,
        ttl_seconds=ttl_seconds,
    )
    operation = workspace_files.create_operation(
        store,
        actor,
        str(workspace_id),
        operation_type="download",
        relative_path=path,
        arguments={"transfer_id": transfer["id"]},
        idempotency_key=f"workspace-to-drive:{path}:{uuid4().hex[:8]}",
    )
    _link_operation(store, transfer["id"], operation["id"])
    return {
        "transfer": workspace_files.get_transfer(store, actor, transfer["id"]),
        "operation": operation,
        "workspace": workspace,
    }


def save_transfer_to_drive(
    store: Any,
    actor: Any,
    *,
    transfer_id: str,
    parent_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """把人选定的传输会话内容**存进云盘**（同名冲突 → 409，不静默覆盖）。"""

    drive_actor = drive.actor_for(store, actor.member_id)
    try:
        transfer, content = workspace_files.read_transfer_content(store, actor, str(transfer_id))
    except workspace_files.WorkspaceError as error:
        # 统一成传输错误族：调用方不需要认识两套错误码
        raise TransferError(error.code, error.detail) from error
    if transfer["status"] not in {"ready", "uploading", "consumed"}:
        raise TransferError("file_transfer_expired", transfer["status"])
    desired = str(name or "").strip() or _name_from_transfer(transfer)
    expected_hash = transfer.get("expected_hash")
    if expected_hash and hashlib.sha256(content).hexdigest() != str(expected_hash):
        raise TransferError("file_upload_hash_mismatch", str(expected_hash))
    try:
        node = drive.put_file(store, drive_actor, parent_id, desired, content, None)
    except drive.DriveError as error:
        # 同名冲突/配额/大小写都原样上报，让上层给人选（改名/换目录/跳过）
        raise TransferError(error.code, error.detail) from error
    return {"node": node, "transfer_id": str(transfer_id), "saved_bytes": len(content)}


def _name_from_transfer(transfer: dict[str, Any]) -> str:
    source_id = str(transfer.get("source_id") or "")
    if source_id and "/" not in source_id and source_id not in {"", "drive"}:
        candidate = source_id.rsplit("/", 1)[-1]
        if candidate:
            return candidate
    return f"从工作区保存-{str(transfer.get('id'))[:8]}.bin"


# ---- 传输历史 / 维护 ----------------------------------------------------------


def transfer_history(store: Any, actor: Any, *, workspace_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """传输历史：会话 + 关联操作的终态与失败原因（失败要能看见、能重试）。"""

    transfers = workspace_files.list_transfers(store, actor)
    if workspace_id:
        transfers = [item for item in transfers if str(item.get("workspace_id") or "") == str(workspace_id)]
    history: list[dict[str, Any]] = []
    for transfer in transfers[: max(1, min(int(limit or 50), 200))]:
        operation = None
        if transfer.get("operation_id"):
            operation = store.db.execute(
                "SELECT id, operation_type, status, error_code, error_message FROM workspace_operations WHERE id = ?",
                (str(transfer["operation_id"]),),
            ).fetchone()
        history.append(
            {
                **transfer,
                "operation": dict(operation) if operation else None,
                "retryable": bool(operation and str(operation["status"]) in {"failed", "expired"}),
            }
        )
    return history


def cleanup_transfers(store: Any, actor: Any) -> dict[str, int]:
    """过期会话回收（对象删掉、状态置 expired）。"""

    return workspace_files.cleanup_expired_transfers(store, actor)


def scan_orphans(store: Any, actor: Any) -> dict[str, Any]:
    """孤儿对象扫描：**只报告不删**。

    两类：
    - 云盘对象：`drive.orphan_objects()` 反向扫（对象存储里有、节点树里没人引用）；
    - 传输对象：状态已是 `consumed/expired/failed` 但仍留在存储里的会话（消费完没清掉的那种）。
    """

    drive_orphans = drive.orphan_objects(store)
    rows = store.db.execute(
        "SELECT id, storage_key, status FROM file_transfer_sessions WHERE organization_id = ? AND status IN ('consumed', 'expired', 'failed')",
        (actor.organization_id,),
    ).fetchall()
    transfer_orphans = []
    root = getattr(store.object_store, "root", None)
    for row in rows:
        key = str(row["storage_key"])
        exists = (root is not None) and (type(root) is type(root) and _exists(root, key))
        transfer_orphans.append({"id": str(row["id"]), "storage_key": key, "status": str(row["status"]), "object_present": bool(exists)})
    return {
        "drive_orphan_keys": drive_orphans,
        "drive_orphan_count": len(drive_orphans),
        "transfer_orphan_count": len(transfer_orphans),
        "transfer_orphans": transfer_orphans[:50],
        "note": "只报告不删：对象存储列举能力在后端之间不一致，删无主对象不可逆（计划 §7.3）",
        "scanned_at": _now(),
    }


def _exists(root: Any, key: str) -> bool:
    from pathlib import Path

    try:
        return (Path(root) / key).is_file()
    except OSError:
        return False


__all__ = [
    "TransferError",
    "cleanup_transfers",
    "drive_to_workspace",
    "save_transfer_to_drive",
    "scan_orphans",
    "transfer_history",
    "workspace_to_drive",
]