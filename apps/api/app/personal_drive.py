"""个人云盘（旧接口的壳）。

**真源已经搬进 `app/drive.py`**（FM-1：目录树 + 回收站 + 配额/去重 + 对象清理队列 + 审计）。
这个模块现在只做两件事：

1. 把旧接口的历史响应形状与错误码翻译过去（`GET /api/drive`、`POST /api/drive/upload`、
   `DELETE /api/drive/{file_id}`、`POST /api/projects/{id}/drive/import`、
   `POST /api/drive/from-artifact/{id}`）——旧客户端和旧测试不因这轮重构改口径；
2. 保留几个跨模块引用的常量（配额、归档后缀、文件名 → 成果物类型映射）与 `DriveError`。

**旧表 `personal_drive_files` 不再写**：启动时由 `drive.backfill_legacy()` 把老行按原 id/键/哈希
搬进节点树，搬完它是只读备份（回滚时还能对照）。

**一处刻意的语义差异**（写在这里免得被当 bug）：旧 `DELETE /api/drive/{file_id}` 仍然是**硬删**
（进回收站再立刻彻底清除），因为它的历史语义就是"删掉、配额立刻释放"；新接口
`DELETE /api/drive/nodes/{id}` 走**软删除进回收站**，两步才释放空间。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from . import drive
from .drive import DriveError  # noqa: F401 - 兼容既有 `except personal_drive.DriveError`

# 配额与"业务口径"常量：真源仍是这份（`drive.quota_bytes()` 动态读它，测试会 monkeypatch）
DRIVE_QUOTA_BYTES = drive.DEFAULT_QUOTA_BYTES
ARCHIVE_SUFFIXES = drive.ARCHIVE_SUFFIXES

# 简单的文件类型 → 平台成果物类型映射（导入项目时使用）。
DRIVE_ARTIFACT_TYPES: dict[str, str] = {
    ".csv": "result_table",
    ".xlsx": "result_table",
    ".xls": "result_table",
    ".json": "result_table",
    ".tsv": "result_table",
    ".pdf": "compiled_pdf",
    ".tex": "paper_source",
    ".md": "paper_source",
    ".docx": "paper_source",
    ".py": "code",
    ".r": "code",
    ".m": "code",
    ".jl": "code",
    ".png": "figure",
    ".jpg": "figure",
    ".jpeg": "figure",
    ".svg": "figure",
}


def _actor(store: Any, owner: str) -> drive.DriveActor:
    return _call(drive.actor_for, store, owner)


def _call(function: Any, *args: Any, **kwargs: Any) -> Any:
    """调服务层并把新错误码翻回旧错误码。

    为什么必须翻：旧路由按历史码分流 HTTP 状态（`drive_file_not_found` → 404、
    `drive_quota_exceeded` → 413、`drive_file_referenced_by_project` → 409）。服务层用的是
    计划 §6.5 的 `file_*` 码，不翻译就会全掉进 400——旧客户端的重试/提示逻辑会静默失效。
    """

    try:
        return function(*args, **kwargs)
    except drive.DriveError as error:
        raise DriveError(drive.legacy_code(error.code), error.detail) from error


def _project_ids(store: Any, node_id: str) -> list[str]:
    """旧响应里的 `project_ids`：从引用表读出来（不再有 JSON 字符串那一列）。"""

    return sorted({ref["project_id"] for ref in _call(drive.refs_for, store, node_id)})


def _entry(store: Any, node: dict[str, Any]) -> dict[str, Any]:
    """节点 → 旧响应形状（多带几个新字段不破坏旧客户端）。"""

    return {
        "id": node["id"],
        "name": node["name"],
        "size_bytes": node["size_bytes"],
        "content_hash": node["content_hash"] or "",
        "is_archive": bool(node["is_archive"]),
        "mime_type": node["mime_type"],
        "project_ids": _project_ids(store, node["id"]),
        "parent_id": node.get("parent_id"),
        "kind": node.get("kind", "file"),
        "revision": node.get("revision", 1),
        "created_at": node["created_at"],
    }


def drive_usage(store: Any, owner: str) -> dict[str, Any]:
    usage = _call(drive.usage, store, _actor(store, owner))
    # 旧口径里没有 trashed_count，但多一个字段不会伤到旧调用方；UI 要用它显示回收站角标
    return usage


def list_drive_files(store: Any, owner: str) -> list[dict[str, Any]]:
    """列**根目录**下的文件（旧接口是平面列表，映射到新模型的根目录）。"""

    actor = _actor(store, owner)
    listing = _call(drive.list_children, store, actor, None, limit=500)
    return [_entry(store, node) for node in listing["nodes"] if node["kind"] == "file"]


def upload_drive_file(
    store: Any,
    owner: str,
    *,
    name: str,
    content: bytes,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """上传到根目录（旧接口）。同名冲突按新规则报 `file_name_conflict`（不再静默改名/覆盖）。"""

    actor = _actor(store, owner)
    node = _call(drive.put_file, store, actor, None, name, content, mime_type)
    entry = _entry(store, node)
    entry["reused_object"] = bool(node["content_hash"]) and _object_shared(store, owner, node["content_hash"])
    return entry


def _object_shared(store: Any, owner: str, content_hash: str) -> bool:
    """同内容是否已有别的节点（去重命中时配额只算一份，旧接口把它如实标出来）。"""

    row = store.db.execute(
        "SELECT COUNT(*) AS c FROM drive_nodes WHERE owner_member_id = ? AND content_hash = ? AND purged_at IS NULL",
        (owner, content_hash),
    ).fetchone()
    return int(row["c"]) > 1


def delete_drive_file(store: Any, owner: str, file_id: str) -> dict[str, Any]:
    """删除（旧接口）：**硬删**——先进回收站、再立刻彻底清除，配额立刻释放。

    被项目引用时拒绝（与历史行为一致）：`drive_file_referenced_by_project` → HTTP 409。
    """

    actor = _actor(store, owner)
    try:
        drive.trash(store, actor, file_id)
        drive.purge(store, actor, file_id)
    except DriveError as error:
        raise DriveError(drive.legacy_code(error.code), error.detail) from error
    return {"id": str(file_id), "deleted": True}


def read_drive_file(store: Any, owner: str, file_id: str) -> tuple[dict[str, Any], bytes]:
    node, content = _call(drive.read_content, store, _actor(store, owner), file_id)
    return _entry(store, node), content


def _artifact_type_for(name: str) -> str:
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return DRIVE_ARTIFACT_TYPES.get(suffix, "problem_source")


def copy_artifact_to_drive(store: Any, owner: str, artifact_id: UUID) -> dict[str, Any]:
    """把一份项目成果物复制进个人云盘（对话产出的「转入云盘」）。

    为什么是**复制**而不是移动：成果物是项目资产（要过审核、可能被别人引用），云盘是个人暂存区；
    两边各留一份，语义才不混。对象去重、配额与命名清洗都由 `drive.put_file` 负责。
    """

    actor = _actor(store, owner)
    try:
        artifact = store.get_artifact(artifact_id)
        content = store.get_artifact_content(artifact_id)
    except Exception as error:  # noqa: BLE001 - 取件失败统一映射成"这份成果物拿不到"
        raise DriveError("artifact_not_found") from error
    node = _transfer_into_root(store, actor, artifact.name, content, artifact.mime_type, str(artifact_id))
    entry = _entry(store, node)
    entry["source_artifact_id"] = str(artifact_id)
    return entry


def _transfer_into_root(
    store: Any,
    actor: drive.DriveActor,
    name: str,
    content: bytes,
    mime_type: str | None,
    artifact_id: str,
) -> dict[str, Any]:
    """把一份内容放进根目录，且**重复点「转入云盘」不报错**。

    这条手势的语义是"我要它在云盘里有一份"，不是"再给我一份新的"：
    - 同名同内容（同一个成果物点了两次）→ 返回已经存在的那份（幂等）；
    - 同名不同内容（另一个成果物恰好同名）→ 自动加 `-2`/`-3` 后缀，不覆盖、也不报错。
    与文件管理器里的「复制」（明确要第二份、命名带「-副本」）区分开。
    """

    drive.ensure_schema(store)  # 这条查询跑在服务层的建表检查之前，得自己先保证表在
    content_hash = hashlib.sha256(content).hexdigest()
    existing = store.db.execute(
        """
        SELECT * FROM drive_nodes
        WHERE owner_member_id = ? AND parent_id = (SELECT id FROM drive_nodes WHERE owner_member_id = ? AND is_root = 1)
          AND name_key = ? AND content_hash = ? AND deleted_at IS NULL AND purged_at IS NULL
        """,
        (actor.member_id, actor.member_id, drive.name_key(name), content_hash),
    ).fetchone()
    if existing is not None:
        return drive._row_to_node(existing)
    candidate = name
    counter = 2
    while True:
        try:
            return _call(
                drive.put_file,
                store,
                actor,
                None,
                candidate,
                content,
                mime_type,
                source_artifact_id=artifact_id,
            )
        except DriveError as error:
            if error.code != drive.legacy_code("file_name_conflict"):
                raise
            stem, dot, suffix = name.rpartition(".")
            candidate = f"{stem}-{counter}.{suffix}" if dot else f"{name}-{counter}"
            counter += 1
            if counter > 50:
                raise


def import_drive_file_to_project(
    store: Any,
    owner: str,
    file_id: str,
    project_id: UUID,
    *,
    task_id: UUID | None = None,
    created_by: str = "member-001",
) -> dict[str, Any]:
    """把个人云盘文件导入项目空间：登记为项目成果物、并把来源写进引用表。"""

    from .contracts import ArtifactCreate

    actor = _actor(store, owner)
    node, content = _call(drive.read_content, store, actor, file_id)
    try:
        project = store.get_project(project_id)
    except KeyError as error:
        raise DriveError("drive_project_not_found") from error
    if project is None:
        raise DriveError("drive_project_not_found")

    actual_hash = hashlib.sha256(content).hexdigest()
    artifact = store.create_artifact(
        project_id,
        ArtifactCreate(
            name=node["name"],
            artifact_type=_artifact_type_for(node["name"]),
            description=f"来自个人云盘（{node['name']}，{node['size_bytes']} 字节）",
            content_hash=actual_hash,
            mime_type=node["mime_type"],
            task_id=task_id,
        ),
        created_by=created_by,
        created_by_kind="member",
    )
    artifact = store.store_artifact_content(artifact.id, content)
    with drive._transaction(store):
        drive.add_ref(store, actor, drive._raw(store, file_id), str(project_id), str(artifact.id))
        drive.audit(
            store,
            actor,
            "import_to_project",
            node=drive._raw(store, file_id),
            capability="drive.file.import",
            reason=str(artifact.id),
        )

    return {
        "drive_file_id": str(file_id),
        "artifact_id": str(artifact.id),
        "artifact_type": str(artifact.artifact_type),
        "name": node["name"],
        "content_hash": actual_hash,
        "size_bytes": node["size_bytes"],
        "project_id": str(project_id),
        "is_archive": bool(node["is_archive"]),
    }


def project_ids_for(store: Any, owner: str, file_id: str) -> list[str]:
    """某个云盘文件被哪些项目引用过（详情抽屉与删除前提示要用）。"""

    node = drive.get_node(store, _actor(store, owner), file_id)
    return _project_ids(store, node["id"])


def dump_entry(entry: dict[str, Any]) -> str:
    """调试用：把条目按旧的 JSON 形状打印（保持 `project_ids` 的键名）。"""

    return json.dumps(entry, ensure_ascii=False)


__all__ = [
    "ARCHIVE_SUFFIXES",
    "DRIVE_ARTIFACT_TYPES",
    "DRIVE_QUOTA_BYTES",
    "DriveError",
    "copy_artifact_to_drive",
    "delete_drive_file",
    "drive_usage",
    "import_drive_file_to_project",
    "list_drive_files",
    "project_ids_for",
    "read_drive_file",
    "upload_drive_file",
]