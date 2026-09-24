"""个人云盘：成员级私人文件暂存与一键导入项目空间。

设计约束：

- **成员隔离**：文件属于成员（`owner`），不进入任何项目；只有显式
  「导入项目」才转成项目成果物（Artifact）并受项目门禁约束；
- **配额 200MB**：按成员名下文件字节数累计，超限拒绝上传；
- **内容可寻址**：登记 SHA-256，同内容重复上传直接复用（不占额外配额）；
- **压缩包识别**：zip/tar/gz/7z/rar 归档会标记 `is_archive`，导入项目时
  自动按归档类型登记（解包属后续工作，先保证可见）。
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

DRIVE_QUOTA_BYTES = 200 * 1024 * 1024

ARCHIVE_SUFFIXES = {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz"}

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


class DriveError(RuntimeError):
    """稳定错误族：配额、文件类型或归属问题。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


def _ensure_table(store: Any) -> None:
    store.db.execute(
        """
        CREATE TABLE IF NOT EXISTS personal_drive_files (
            id TEXT PRIMARY KEY,
            owner TEXT NOT NULL,
            name TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            storage_key TEXT NOT NULL,
            mime_type TEXT,
            is_archive INTEGER NOT NULL DEFAULT 0,
            project_ids TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        )
        """
    )
    store.db.commit()


def _row_to_entry(row: Any) -> dict[str, Any]:
    import json as json_module

    return {
        "id": row["id"],
        "name": row["name"],
        "size_bytes": row["size_bytes"],
        "content_hash": row["content_hash"],
        "is_archive": bool(row["is_archive"]),
        "mime_type": row["mime_type"],
        "project_ids": json_module.loads(row["project_ids"] or "[]"),
        "created_at": row["created_at"],
    }


def drive_usage(store: Any, owner: str) -> dict[str, Any]:
    _ensure_table(store)
    row = store.db.execute(
        "SELECT COUNT(*) AS count, COALESCE(SUM(size_bytes), 0) AS bytes FROM personal_drive_files WHERE owner = ?",
        (owner,),
    ).fetchone()
    used = int(row["bytes"])
    return {
        "owner": owner,
        "quota_bytes": DRIVE_QUOTA_BYTES,
        "used_bytes": used,
        "free_bytes": max(0, DRIVE_QUOTA_BYTES - used),
        "file_count": int(row["count"]),
        "used_percent": round(used / DRIVE_QUOTA_BYTES * 100, 2),
    }


def list_drive_files(store: Any, owner: str) -> list[dict[str, Any]]:
    _ensure_table(store)
    rows = store.db.execute(
        "SELECT * FROM personal_drive_files WHERE owner = ? ORDER BY created_at DESC",
        (owner,),
    ).fetchall()
    return [_row_to_entry(row) for row in rows]


def upload_drive_file(
    store: Any,
    owner: str,
    *,
    name: str,
    content: bytes,
    mime_type: str | None = None,
) -> dict[str, Any]:
    """上传文件到个人云盘；同内容已存在时复用记录。"""

    if not isinstance(name, str) or not name.strip():
        raise DriveError("drive_file_name_required")
    cleaned = name.strip().replace("/", "_").replace("\\", "_")
    if not content:
        raise DriveError("drive_file_empty")
    _ensure_table(store)
    content_hash = hashlib.sha256(content).hexdigest()

    existing = store.db.execute(
        "SELECT * FROM personal_drive_files WHERE owner = ? AND content_hash = ?",
        (owner, content_hash),
    ).fetchone()
    if existing is not None:
        return _row_to_entry(existing)

    usage = drive_usage(store, owner)
    if usage["used_bytes"] + len(content) > DRIVE_QUOTA_BYTES:
        raise DriveError(
            "drive_quota_exceeded",
            f"{usage['used_bytes'] + len(content)}/{DRIVE_QUOTA_BYTES}",
        )

    file_id = str(uuid4())
    storage_key = f"drive/{owner}/{file_id}"
    stored = store.object_store.put_bytes(storage_key, content, mime_type)
    suffix = "." + cleaned.rsplit(".", 1)[-1].lower() if "." in cleaned else ""
    is_archive = suffix in ARCHIVE_SUFFIXES
    try:
        store.db.execute(
            """
            INSERT INTO personal_drive_files
                (id, owner, name, size_bytes, content_hash, storage_key, mime_type, is_archive, project_ids, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, '[]', ?)
            """,
            (file_id, owner, cleaned, len(content), content_hash, stored.key, mime_type, int(is_archive), datetime.now(UTC).isoformat()),
        )
        store.db.commit()
    except Exception:
        store.db.rollback()
        store.object_store.delete(stored.key)
        raise
    return _row_to_entry(
        store.db.execute("SELECT * FROM personal_drive_files WHERE id = ?", (file_id,)).fetchone()
    )


def delete_drive_file(store: Any, owner: str, file_id: str) -> dict[str, Any]:
    _ensure_table(store)
    row = store.db.execute(
        "SELECT * FROM personal_drive_files WHERE id = ? AND owner = ?",
        (file_id, owner),
    ).fetchone()
    if row is None:
        raise DriveError("drive_file_not_found")
    if row["project_ids"] and row["project_ids"] != "[]":
        raise DriveError("drive_file_referenced_by_project")
    try:
        store.db.execute("DELETE FROM personal_drive_files WHERE id = ?", (file_id,))
        store.db.commit()
    except Exception:
        store.db.rollback()
        raise
    store.object_store.delete(row["storage_key"])
    return {"id": file_id, "deleted": True}


def _artifact_type_for(name: str) -> str:
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return DRIVE_ARTIFACT_TYPES.get(suffix, "problem_source")


def copy_artifact_to_drive(store: Any, owner: str, artifact_id: UUID) -> dict[str, Any]:
    """把一份项目成果物复制进个人云盘（对话产出的「转入云盘」，2026-09-24 用户需求）。

    为什么是**复制**而不是移动：成果物是项目资产（要过审核、可能被别人引用），云盘是个人暂存区；
    两边各留一份，语义才不混。去重（内容寻址）、配额与命名清洗都由 `upload_drive_file` 负责，
    这里只做"取件 → 落盘"。
    """

    _ensure_table(store)
    try:
        artifact = store.get_artifact(artifact_id)
        content = store.get_artifact_content(artifact_id)
    except Exception as error:  # noqa: BLE001 - 取件失败统一映射成"这份成果物拿不到"
        raise DriveError("artifact_not_found") from error
    entry = upload_drive_file(store, owner, name=artifact.name, content=content, mime_type=artifact.mime_type)
    # 留一条来源：云盘里能看出它是从哪个成果物转过来的（不重复搬内容，只记 id）
    entry["source_artifact_id"] = str(artifact_id)
    return entry


def import_drive_file_to_project(
    store: Any,
    owner: str,
    file_id: str,
    project_id: UUID,
    *,
    task_id: UUID | None = None,
    created_by: str = "member-001",
) -> dict[str, Any]:
    """把个人云盘文件导入项目空间：登记为项目成果物并保留来源。"""

    from .contracts import ArtifactCreate

    _ensure_table(store)
    row = store.db.execute(
        "SELECT * FROM personal_drive_files WHERE id = ? AND owner = ?",
        (file_id, owner),
    ).fetchone()
    if row is None:
        raise DriveError("drive_file_not_found")
    project = store.get_project(project_id)
    if project is None:
        raise DriveError("drive_project_not_found")

    content = store.object_store.get_bytes(row["storage_key"])
    actual_hash = hashlib.sha256(content).hexdigest()
    if actual_hash != row["content_hash"]:
        raise DriveError("drive_file_hash_mismatch")

    artifact = store.create_artifact(
        project_id,
        ArtifactCreate(
            name=row["name"],
            artifact_type=_artifact_type_for(row["name"]),
            description=f"来自个人云盘（{row['name']}，{row['size_bytes']} 字节）",
            content_hash=actual_hash,
            mime_type=row["mime_type"],
            task_id=task_id,
        ),
        created_by=created_by,
        created_by_kind="member",
    )
    artifact = store.store_artifact_content(artifact.id, content)

    import json as json_module

    project_ids = json_module.loads(row["project_ids"] or "[]")
    if str(project_id) not in project_ids:
        project_ids.append(str(project_id))
        store.db.execute(
            "UPDATE personal_drive_files SET project_ids = ? WHERE id = ?",
            (json_module.dumps(project_ids), file_id),
        )
        store.db.commit()

    return {
        "drive_file_id": file_id,
        "artifact_id": str(artifact.id),
        "artifact_type": str(artifact.artifact_type),
        "name": row["name"],
        "content_hash": actual_hash,
        "size_bytes": row["size_bytes"],
        "project_id": str(project_id),
        "is_archive": bool(row["is_archive"]),
    }


def read_drive_file(store: Any, owner: str, file_id: str) -> tuple[dict[str, Any], bytes]:
    _ensure_table(store)
    row = store.db.execute(
        "SELECT * FROM personal_drive_files WHERE id = ? AND owner = ?",
        (file_id, owner),
    ).fetchone()
    if row is None:
        raise DriveError("drive_file_not_found")
    return _row_to_entry(row), store.object_store.get_bytes(row["storage_key"])


__all__ = [
    "ARCHIVE_SUFFIXES",
    "DRIVE_ARTIFACT_TYPES",
    "DRIVE_QUOTA_BYTES",
    "DriveError",
    "delete_drive_file",
    "drive_usage",
    "import_drive_file_to_project",
    "list_drive_files",
    "read_drive_file",
    "upload_drive_file",
]