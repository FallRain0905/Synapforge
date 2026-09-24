"""知识库与内置 AI 的存储层（Phase A）。

双形态知识库：
- **项目知识库**：`project_id` 非空，挂项目，复用项目授权；
- **个人知识库**：`project_id` 为空，属成员，可通过 `kb_shares` 分享给其他成员。

知识库只存文档（Markdown 文本 + 来源引用），与个人云盘（文件暂存 + 配额）
语义分离。向量/超图数据在独立的 hyper-rag-service 缓存目录中，平台只登记
索引状态与文档哈希映射。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from .contracts import (
    KbDocumentCreate,
    KbCreate,
    KbShareCreate,
)


class KnowledgeBaseError(RuntimeError):
    """稳定错误族：知识库归属、分享或索引问题。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(content: str) -> str:
    return hashlib.md5(content.strip().encode("utf-8")).hexdigest()


def ensure_kb_tables(store: Any) -> None:
    store.db.executescript(
        """
        CREATE TABLE IF NOT EXISTS knowledge_bases (
            id TEXT PRIMARY KEY,
            owner_member_id TEXT NOT NULL,
            project_id TEXT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            visibility TEXT NOT NULL DEFAULT 'private',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kb_documents (
            id TEXT PRIMARY KEY,
            kb_id TEXT NOT NULL,
            title TEXT NOT NULL,
            content_md TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'manual',
            source_artifact_id TEXT,
            source_drive_file_id TEXT,
            index_status TEXT NOT NULL DEFAULT 'not_indexed',
            indexed_at TEXT,
            index_error TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kb_shares (
            id TEXT PRIMARY KEY,
            kb_id TEXT NOT NULL,
            member_id TEXT NOT NULL,
            permission TEXT NOT NULL DEFAULT 'read',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS ai_settings (
            member_id TEXT PRIMARY KEY,
            llm_api_key TEXT NOT NULL DEFAULT '',
            llm_base_url TEXT NOT NULL DEFAULT '',
            llm_model TEXT NOT NULL DEFAULT '',
            embedding_api_key TEXT NOT NULL DEFAULT '',
            embedding_base_url TEXT NOT NULL DEFAULT '',
            embedding_model TEXT NOT NULL DEFAULT '',
            embedding_dimensions INTEGER NOT NULL DEFAULT 1024,
            mineru_api_key TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kb_conversations (
            id TEXT PRIMARY KEY,
            member_id TEXT NOT NULL,
            kb_id TEXT,
            title TEXT NOT NULL,
            mode TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS kb_messages (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            sources TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL
        );
        """
    )
    store.db.commit()


# ---- 访问控制 -----------------------------------------------------------


def can_access_kb(store: Any, kb_row: Mapping[str, Any], member_id: str) -> bool:
    """项目知识库按项目成员；个人知识库按 owner 或 kb_shares。"""

    if kb_row["owner_member_id"] == member_id:
        return True
    project_id = kb_row["project_id"]
    if project_id:
        try:
            members = {
                row["member_id"]
                for row in store.db.execute(
                    "SELECT member_id FROM project_memberships WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            }
            if member_id in members:
                return True
        except Exception:
            pass
    shared = store.db.execute(
        "SELECT 1 FROM kb_shares WHERE kb_id = ? AND member_id = ? LIMIT 1",
        (kb_row["id"], member_id),
    ).fetchone()
    return shared is not None


def get_kb_row(store: Any, kb_id: str) -> Mapping[str, Any] | None:
    return store.db.execute("SELECT * FROM knowledge_bases WHERE id = ?", (kb_id,)).fetchone()


def require_kb_access(store: Any, kb_id: str, member_id: str) -> Mapping[str, Any]:
    row = get_kb_row(store, kb_id)
    if row is None:
        raise KnowledgeBaseError("kb_not_found")
    if not can_access_kb(store, row, member_id):
        raise KnowledgeBaseError("kb_access_denied")
    return row


# ---- 知识库 CRUD ---------------------------------------------------------


def create_kb(store: Any, data: KbCreate, member_id: str) -> dict[str, Any]:
    ensure_kb_tables(store)
    if data.project_id is not None:
        store.get_project(data.project_id)
    kb_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO knowledge_bases (id, owner_member_id, project_id, name, description, visibility, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (kb_id, member_id, str(data.project_id) if data.project_id else None, data.name, data.description, "shared" if data.shared else "private", timestamp, timestamp),
    )
    store.db.commit()
    return _kb_payload(store, get_kb_row(store, kb_id))


def list_kbs(store: Any, member_id: str, project_id: UUID | None = None) -> list[dict[str, Any]]:
    ensure_kb_tables(store)
    rows = store.db.execute("SELECT * FROM knowledge_bases ORDER BY created_at DESC").fetchall()
    result = []
    for row in rows:
        if not can_access_kb(store, row, member_id):
            continue
        if project_id is not None and row["project_id"] != str(project_id):
            continue
        result.append(_kb_payload(store, row))
    return result


def _kb_payload(store: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    docs = store.db.execute("SELECT COUNT(*) AS n FROM kb_documents WHERE kb_id = ?", (row["id"],)).fetchone()
    shares = store.db.execute("SELECT member_id, permission FROM kb_shares WHERE kb_id = ?", (row["id"],)).fetchall()
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row["description"],
        "project_id": row["project_id"],
        "owner_member_id": row["owner_member_id"],
        "visibility": row["visibility"],
        "document_count": docs["n"] if docs else 0,
        "shares": [{"member_id": share["member_id"], "permission": share["permission"]} for share in shares],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def share_kb(store: Any, kb_id: str, data: KbShareCreate, member_id: str) -> dict[str, Any]:
    ensure_kb_tables(store)
    row = get_kb_row(store, kb_id)
    if row is None:
        raise KnowledgeBaseError("kb_not_found")
    if row["owner_member_id"] != member_id:
        raise KnowledgeBaseError("kb_share_only_owner")
    existing = store.db.execute(
        "SELECT id FROM kb_shares WHERE kb_id = ? AND member_id = ?",
        (kb_id, data.member_id),
    ).fetchone()
    if existing:
        store.db.execute("UPDATE kb_shares SET permission = ? WHERE id = ?", (data.permission, existing["id"]))
    else:
        store.db.execute(
            "INSERT INTO kb_shares (id, kb_id, member_id, permission, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid4()), kb_id, data.member_id, data.permission, _now()),
        )
    store.db.execute("UPDATE knowledge_bases SET visibility = 'shared', updated_at = ? WHERE id = ?", (_now(), kb_id))
    store.db.commit()
    return _kb_payload(store, get_kb_row(store, kb_id))


# ---- 文档 ---------------------------------------------------------------


def add_kb_document(store: Any, kb_id: str, data: KbDocumentCreate, member_id: str) -> dict[str, Any]:
    ensure_kb_tables(store)
    require_kb_access(store, kb_id, member_id)
    if not data.content_md.strip():
        raise KnowledgeBaseError("kb_document_content_required")
    doc_id = str(uuid4())
    content_hash = _hash(data.content_md)
    store.db.execute(
        """
        INSERT INTO kb_documents
            (id, kb_id, title, content_md, content_hash, source_type, source_artifact_id, source_drive_file_id, index_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'not_indexed', ?)
        """,
        (doc_id, kb_id, data.title, data.content_md, content_hash, data.source_type,
         str(data.source_artifact_id) if data.source_artifact_id else None,
         data.source_drive_file_id, _now()),
    )
    store.db.execute("UPDATE knowledge_bases SET updated_at = ? WHERE id = ?", (_now(), kb_id))
    store.db.commit()
    return _doc_payload(get_kb_row_doc(store, doc_id))


def get_kb_row_doc(store: Any, doc_id: str) -> Mapping[str, Any]:
    return store.db.execute("SELECT * FROM kb_documents WHERE id = ?", (doc_id,)).fetchone()


def _doc_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kb_id": row["kb_id"],
        "title": row["title"],
        "content_md": row["content_md"],
        "content_hash": row["content_hash"],
        "source_type": row["source_type"],
        "source_artifact_id": row["source_artifact_id"],
        "source_drive_file_id": row["source_drive_file_id"],
        "index_status": row["index_status"],
        "indexed_at": row["indexed_at"],
        "index_error": row["index_error"],
        "created_at": row["created_at"],
    }


def list_kb_documents(store: Any, kb_id: str, member_id: str, *, include_content: bool = False) -> list[dict[str, Any]]:
    ensure_kb_tables(store)
    require_kb_access(store, kb_id, member_id)
    rows = store.db.execute("SELECT * FROM kb_documents WHERE kb_id = ? ORDER BY created_at DESC", (kb_id,)).fetchall()
    result = []
    for row in rows:
        payload = _doc_payload(row)
        if not include_content:
            payload["content_md"] = ""
            payload["content_md_bytes"] = len(row["content_md"] or "")
        result.append(payload)
    return result


def mark_index_status(store: Any, doc_id: str, status: str, error: str | None = None) -> None:
    store.db.execute(
        "UPDATE kb_documents SET index_status = ?, indexed_at = ?, index_error = ? WHERE id = ?",
        (status, _now() if status in {"indexed", "failed"} else None, error, doc_id),
    )
    store.db.commit()


# ---- AI 凭据 ------------------------------------------------------------


def save_ai_settings(store: Any, member_id: str, data: Mapping[str, Any]) -> dict[str, Any]:
    ensure_kb_tables(store)
    allowed = {
        "llm_api_key", "llm_base_url", "llm_model",
        "embedding_api_key", "embedding_base_url", "embedding_model", "embedding_dimensions",
        "mineru_api_key",
    }
    columns = {key: value for key, value in data.items() if key in allowed}
    if not columns:
        raise KnowledgeBaseError("ai_settings_no_fields")
    sets = ", ".join(f"{key} = ?" for key in columns)
    values = list(columns.values())
    store.db.execute(
        f"INSERT INTO ai_settings (member_id, {', '.join(columns)}, updated_at) VALUES (?, {', '.join('?' for _ in columns)}, ?) "
        f"ON CONFLICT(member_id) DO UPDATE SET {sets}, updated_at = ?",
        [member_id, *values, _now(), *values, _now()],
    )
    store.db.commit()
    return get_ai_settings(store, member_id)


def get_ai_settings(store: Any, member_id: str) -> dict[str, Any]:
    ensure_kb_tables(store)
    row = store.db.execute("SELECT * FROM ai_settings WHERE member_id = ?", (member_id,)).fetchone()
    if row is None:
        return {
            "member_id": member_id,
            "llm_api_key": "", "llm_base_url": "", "llm_model": "",
            "embedding_api_key": "", "embedding_base_url": "", "embedding_model": "",
            "embedding_dimensions": 1024, "mineru_api_key": "",
        }
    return dict(row)


def ai_credentials_ready(settings: Mapping[str, Any]) -> bool:
    return bool(settings.get("llm_api_key") and settings.get("embedding_api_key"))


# ---- 会话与消息 -----------------------------------------------------------


def create_conversation(store: Any, member_id: str, *, kb_id: str | None, title: str, mode: str) -> dict[str, Any]:
    ensure_kb_tables(store)
    if mode not in {"chat", "rag"}:
        raise KnowledgeBaseError("conversation_mode_invalid")
    if kb_id:
        require_kb_access(store, kb_id, member_id)
    conversation_id = str(uuid4())
    timestamp = _now()
    store.db.execute(
        "INSERT INTO kb_conversations (id, member_id, kb_id, title, mode, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (conversation_id, member_id, kb_id, title, mode, timestamp, timestamp),
    )
    store.db.commit()
    return _conversation_payload(store, conversation_id)


def list_conversations(store: Any, member_id: str) -> list[dict[str, Any]]:
    ensure_kb_tables(store)
    rows = store.db.execute(
        "SELECT * FROM kb_conversations WHERE member_id = ? ORDER BY updated_at DESC",
        (member_id,),
    ).fetchall()
    result = []
    for row in rows:
        payload = _conversation_payload(store, row["id"])
        payload.pop("messages", None)
        result.append(payload)
    return result


def delete_conversation(store: Any, member_id: str, conversation_id: str) -> bool:
    ensure_kb_tables(store)
    row = store.db.execute(
        "SELECT id FROM kb_conversations WHERE id = ? AND member_id = ?",
        (conversation_id, member_id),
    ).fetchone()
    if row is None:
        return False
    store.db.execute("DELETE FROM kb_messages WHERE conversation_id = ?", (conversation_id,))
    store.db.execute("DELETE FROM kb_conversations WHERE id = ?", (conversation_id,))
    store.db.commit()
    return True


def add_message(store: Any, member_id: str, conversation_id: str, *, role: str, content: str, sources: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    ensure_kb_tables(store)
    conversation = store.db.execute(
        "SELECT * FROM kb_conversations WHERE id = ? AND member_id = ?",
        (conversation_id, member_id),
    ).fetchone()
    if conversation is None:
        raise KnowledgeBaseError("conversation_not_found")
    if role not in {"user", "assistant"}:
        raise KnowledgeBaseError("message_role_invalid")
    message_id = str(uuid4())
    store.db.execute(
        "INSERT INTO kb_messages (id, conversation_id, role, content, sources, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, conversation_id, role, content, json.dumps(list(sources), ensure_ascii=False), _now()),
    )
    store.db.execute("UPDATE kb_conversations SET updated_at = ? WHERE id = ?", (_now(), conversation_id))
    store.db.commit()
    return {
        "id": message_id,
        "conversation_id": conversation_id,
        "role": role,
        "content": content,
        "sources": list(sources),
        "created_at": _now(),
    }


def _conversation_payload(store: Any, conversation_id: str) -> dict[str, Any]:
    row = store.db.execute("SELECT * FROM kb_conversations WHERE id = ?", (conversation_id,)).fetchone()
    messages = store.db.execute(
        "SELECT * FROM kb_messages WHERE conversation_id = ? ORDER BY created_at ASC",
        (conversation_id,),
    ).fetchall()
    return {
        "id": row["id"],
        "member_id": row["member_id"],
        "kb_id": row["kb_id"],
        "title": row["title"],
        "mode": row["mode"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "messages": [
            {
                "id": message["id"],
                "role": message["role"],
                "content": message["content"],
                "sources": json.loads(message["sources"] or "[]"),
                "created_at": message["created_at"],
            }
            for message in messages
        ],
    }


__all__ = [
    "KnowledgeBaseError",
    "add_kb_document",
    "add_message",
    "ai_credentials_ready",
    "can_access_kb",
    "create_conversation",
    "create_kb",
    "delete_conversation",
    "ensure_kb_tables",
    "get_ai_settings",
    "get_kb_row",
    "list_conversations",
    "list_kb_documents",
    "list_kbs",
    "mark_index_status",
    "require_kb_access",
    "save_ai_settings",
    "share_kb",
]