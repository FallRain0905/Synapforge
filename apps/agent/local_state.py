"""Durable state for the local machine Agent.

This database is a recovery journal, not the platform source of truth. It
keeps enough local state to reconnect after a process crash or network outage
without storing device secrets in the SQLite file.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class LocalAgentState:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        self.db.close()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gateway_outbox (
                sequence INTEGER PRIMARY KEY,
                message_id TEXT NOT NULL UNIQUE,
                idempotency_key TEXT NOT NULL UNIQUE,
                message_type TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('PENDING', 'SENT', 'ACKED')),
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_sent_at TEXT,
                next_attempt_at TEXT NOT NULL,
                acked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_gateway_outbox_pending
                ON gateway_outbox(status, next_attempt_at, sequence);
            CREATE TABLE IF NOT EXISTS run_states (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_uploads (
                upload_id TEXT PRIMARY KEY,
                artifact_id TEXT,
                local_path TEXT NOT NULL,
                content_hash TEXT,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                project_id TEXT,
                task_id TEXT,
                run_id TEXT,
                mime_type TEXT,
                size_bytes INTEGER,
                relative_path TEXT,
                artifact_type TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS approval_states (
                approval_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lifecycle_transitions (
                transition_id TEXT PRIMARY KEY,
                worker_id TEXT NOT NULL,
                user_session_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                session_state TEXT NOT NULL,
                active_run_ids TEXT NOT NULL,
                cleanup_run_ids TEXT NOT NULL,
                reason TEXT NOT NULL,
                metadata TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lifecycle_transitions_occurred
                ON lifecycle_transitions(occurred_at, transition_id);
            CREATE TABLE IF NOT EXISTS service_control (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                emergency_stop INTEGER NOT NULL DEFAULT 0 CHECK (emergency_stop IN (0, 1)),
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS managed_processes (
                process_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                project_id TEXT,
                task_id TEXT,
                agent_id TEXT,
                device_id TEXT,
                workspace_id TEXT,
                workspace_path TEXT,
                command TEXT NOT NULL,
                cwd TEXT,
                pid INTEGER,
                status TEXT NOT NULL,
                exit_code INTEGER,
                stdout TEXT NOT NULL DEFAULT '',
                stderr TEXT NOT NULL DEFAULT '',
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_managed_processes_status
                ON managed_processes(status, started_at);
            """
        )
        self._ensure_columns(
            "managed_processes",
            {
                "project_id": "TEXT",
                "task_id": "TEXT",
                "agent_id": "TEXT",
                "device_id": "TEXT",
                "workspace_id": "TEXT",
                "workspace_path": "TEXT",
            },
        )
        self._ensure_columns(
            "pending_uploads",
            {
                "project_id": "TEXT",
                "task_id": "TEXT",
                "run_id": "TEXT",
                "mime_type": "TEXT",
                "size_bytes": "INTEGER",
                "relative_path": "TEXT",
                "artifact_type": "TEXT",
                "last_error": "TEXT",
            },
        )
        self.db.execute(
            "INSERT OR IGNORE INTO agent_metadata (key, value) VALUES ('next_gateway_sequence', '1')"
        )
        self.db.execute(
            "INSERT OR IGNORE INTO service_control (id, emergency_stop, reason, updated_at) VALUES (1, 0, '', ?)"
            , (utc_now(),)
        )
        self.db.commit()

    def _next_sequence(self) -> int:
        row = self.db.execute(
            "SELECT value FROM agent_metadata WHERE key = 'next_gateway_sequence'"
        ).fetchone()
        sequence = int(row["value"] if row else 1)
        self.db.execute(
            "INSERT INTO agent_metadata (key, value) VALUES ('next_gateway_sequence', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(sequence + 1),),
        )
        return sequence

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def enqueue_event(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        existing = self.db.execute(
            "SELECT * FROM gateway_outbox WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()
        if existing:
            return self._outbox(existing)

        created_at = utc_now()
        message_id = message_id or f"agent-{uuid4().hex}"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            # Re-check after taking the write lock so two local callers cannot
            # allocate different sequences for the same idempotency key.
            existing = self.db.execute(
                "SELECT * FROM gateway_outbox WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing:
                self.db.commit()
                return self._outbox(existing)
            sequence = self._next_sequence()
            self.db.execute(
                "INSERT INTO gateway_outbox (sequence, message_id, idempotency_key, message_type, payload, status, attempts, created_at, next_attempt_at) VALUES (?, ?, ?, ?, ?, 'PENDING', 0, ?, ?)",
                (
                    sequence,
                    message_id,
                    idempotency_key,
                    message_type,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    created_at,
                    created_at,
                ),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.get_outbox_event(sequence)

    def _outbox(self, row: sqlite3.Row) -> dict[str, Any]:
        values = dict(row)
        values["payload"] = json.loads(values["payload"])
        return values

    def get_outbox_event(self, sequence: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM gateway_outbox WHERE sequence = ?", (sequence,)
        ).fetchone()
        if not row:
            raise KeyError("local_outbox_event_not_found")
        return self._outbox(row)

    def pending_events(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM gateway_outbox WHERE status IN ('PENDING', 'SENT') AND next_attempt_at <= ? ORDER BY sequence LIMIT ?",
            (utc_now(), limit),
        ).fetchall()
        return [self._outbox(row) for row in rows]

    def mark_sent(self, sequence: int, retry_after_seconds: int = 0) -> dict[str, Any]:
        event = self.get_outbox_event(sequence)
        if event["status"] == "ACKED":
            return event
        next_attempt = datetime.now(UTC).timestamp() + max(0, retry_after_seconds)
        next_time = datetime.fromtimestamp(next_attempt, UTC).isoformat()
        self.db.execute(
            "UPDATE gateway_outbox SET status = 'SENT', attempts = attempts + 1, last_sent_at = ?, next_attempt_at = ? WHERE sequence = ? AND status != 'ACKED'",
            (utc_now(), next_time, sequence),
        )
        self.db.commit()
        return self.get_outbox_event(sequence)

    def acknowledge(self, highest_contiguous_sequence: int, message_ids: list[str] | None = None) -> int:
        if highest_contiguous_sequence < 0:
            raise ValueError("local_ack_sequence_invalid")
        timestamp = utc_now()
        updated_count = self.db.execute(
            "UPDATE gateway_outbox SET status = 'ACKED', acked_at = ?, next_attempt_at = ? WHERE sequence <= ? AND status != 'ACKED'",
            (timestamp, timestamp, highest_contiguous_sequence),
        ).rowcount
        for message_id in message_ids or []:
            updated_count += self.db.execute(
                "UPDATE gateway_outbox SET status = 'ACKED', acked_at = ?, next_attempt_at = ? WHERE message_id = ? AND status != 'ACKED'",
                (timestamp, timestamp, message_id),
            ).rowcount
        self.db.commit()
        return int(updated_count)

    def requeue_after(self, after_sequence: int) -> int:
        if after_sequence < 0:
            raise ValueError("local_replay_sequence_invalid")
        timestamp = utc_now()
        cursor = self.db.execute(
            "UPDATE gateway_outbox SET status = 'PENDING', next_attempt_at = ? WHERE sequence > ? AND status != 'ACKED'",
            (timestamp, after_sequence),
        )
        self.db.commit()
        return cursor.rowcount

    def recover_unacked(self) -> int:
        timestamp = utc_now()
        cursor = self.db.execute(
            "UPDATE gateway_outbox SET status = 'PENDING', next_attempt_at = ? WHERE status = 'SENT'",
            (timestamp,),
        )
        self.db.commit()
        return cursor.rowcount

    def renumber_pending_events(self) -> dict[str, int]:
        """把未确认事件重编为从 1 开始的连续序列（每个新连接会话开始时调用）。

        平台侧 `agent_connections.last_received_sequence` 是**按 connection_id 计**的：
        新连接从 0 开始，要求第一条消息的 sequence 正好是 1。而本地 outbox 的序列是
        跨会话单调递增的（存在 agentd.db 里），不对齐就会变成
        "平台要求重放 → 本地重放的对不上 → 再要求重放"的死循环。

        已 ACK 的事件没有重发价值，直接删除；未 ACK 的保序重编号，换新连接继续发。
        message_id / idempotency_key 不变，平台侧的幂等键因此仍然有效。
        """

        rows = self.db.execute(
            "SELECT sequence, message_id, idempotency_key, message_type, payload, status, attempts, created_at, last_sent_at, next_attempt_at "
            "FROM gateway_outbox ORDER BY sequence"
        ).fetchall()
        pending = [row for row in rows if row["status"] != "ACKED"]
        self.db.execute("DELETE FROM gateway_outbox")
        for index, row in enumerate(pending, start=1):
            self.db.execute(
                "INSERT INTO gateway_outbox (sequence, message_id, idempotency_key, message_type, payload, status, attempts, created_at, last_sent_at, next_attempt_at, acked_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (
                    index,
                    row["message_id"],
                    row["idempotency_key"],
                    row["message_type"],
                    row["payload"],
                    row["status"],
                    int(row["attempts"] or 0),
                    row["created_at"],
                    row["last_sent_at"],
                    row["next_attempt_at"],
                ),
            )
        self.db.execute(
            "INSERT INTO agent_metadata (key, value) VALUES ('next_gateway_sequence', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(len(pending) + 1),),
        )
        self.db.commit()
        return {"renumbered": len(pending), "dropped_acked": len(rows) - len(pending)}

    def set_emergency_stop(self, reason: str = "local_emergency_stop") -> None:
        self.db.execute(
            "UPDATE service_control SET emergency_stop = 1, reason = ?, updated_at = ? WHERE id = 1",
            (reason, utc_now()),
        )
        self.db.commit()

    def clear_emergency_stop(self) -> None:
        self.db.execute(
            "UPDATE service_control SET emergency_stop = 0, reason = '', updated_at = ? WHERE id = 1",
            (utc_now(),),
        )
        self.db.commit()

    def emergency_stop_state(self) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT emergency_stop, reason, updated_at FROM service_control WHERE id = 1"
        ).fetchone()
        if not row:
            return {"emergency_stop": False, "reason": "", "updated_at": None}
        return {
            "emergency_stop": bool(row["emergency_stop"]),
            "reason": row["reason"],
            "updated_at": row["updated_at"],
        }

    def is_emergency_stopped(self) -> bool:
        return bool(self.emergency_stop_state()["emergency_stop"])

    def register_process(
        self,
        process_id: str,
        run_id: str,
        command: list[str],
        cwd: str | None,
        pid: int | None,
        status: str = "RUNNING",
        *,
        project_id: str | None = None,
        task_id: str | None = None,
        agent_id: str | None = None,
        device_id: str | None = None,
        workspace_id: str | None = None,
        workspace_path: str | None = None,
    ) -> None:
        self.db.execute(
            "INSERT INTO managed_processes (process_id, run_id, project_id, task_id, agent_id, device_id, workspace_id, workspace_path, command, cwd, pid, status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                process_id,
                run_id,
                project_id,
                task_id,
                agent_id,
                device_id,
                workspace_id,
                workspace_path,
                json.dumps(command, ensure_ascii=False),
                cwd,
                pid,
                status,
                utc_now(),
            ),
        )
        self.db.commit()

    def update_process(
        self,
        process_id: str,
        status: str,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        self.db.execute(
            "UPDATE managed_processes SET status = ?, exit_code = ?, stdout = ?, stderr = ?, finished_at = ? WHERE process_id = ?",
            (status, exit_code, stdout, stderr, utc_now(), process_id),
        )
        self.db.commit()

    def list_managed_processes(self, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM managed_processes"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY started_at ASC"
        rows = self.db.execute(query, params).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            values = dict(row)
            values["command"] = json.loads(values["command"])
            result.append(values)
        return result

    def mark_managed_processes_abandoned(self) -> int:
        cursor = self.db.execute(
            "UPDATE managed_processes SET status = 'ABANDONED', finished_at = ? WHERE status IN ('STARTING', 'RUNNING')",
            (utc_now(),),
        )
        self.db.commit()
        return int(cursor.rowcount)

    def save_run_state(self, run_id: str, status: str, payload: dict[str, Any] | None = None) -> None:
        timestamp = utc_now()
        self.db.execute(
            "INSERT INTO run_states (run_id, status, payload, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET status = excluded.status, payload = excluded.payload, updated_at = excluded.updated_at",
            (run_id, status, json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), timestamp),
        )
        self.db.commit()

    def save_upload(
        self,
        local_path: str,
        *,
        upload_id: str | None = None,
        artifact_id: str | None = None,
        content_hash: str | None = None,
        status: str = "PENDING",
        project_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        mime_type: str | None = None,
        size_bytes: int | None = None,
        relative_path: str | None = None,
        artifact_type: str | None = None,
    ) -> str:
        upload_id = upload_id or f"upload-{uuid4().hex}"
        timestamp = utc_now()
        self.db.execute(
            "INSERT INTO pending_uploads (upload_id, artifact_id, local_path, content_hash, status, attempts, project_id, task_id, run_id, mime_type, size_bytes, relative_path, artifact_type, last_error, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?) ON CONFLICT(upload_id) DO UPDATE SET artifact_id = COALESCE(pending_uploads.artifact_id, excluded.artifact_id), local_path = excluded.local_path, content_hash = excluded.content_hash, status = CASE WHEN pending_uploads.status = 'SUCCEEDED' AND pending_uploads.content_hash = excluded.content_hash THEN 'SUCCEEDED' ELSE excluded.status END, project_id = COALESCE(excluded.project_id, pending_uploads.project_id), task_id = COALESCE(excluded.task_id, pending_uploads.task_id), run_id = COALESCE(excluded.run_id, pending_uploads.run_id), mime_type = COALESCE(excluded.mime_type, pending_uploads.mime_type), size_bytes = COALESCE(excluded.size_bytes, pending_uploads.size_bytes), relative_path = COALESCE(excluded.relative_path, pending_uploads.relative_path), artifact_type = COALESCE(excluded.artifact_type, pending_uploads.artifact_type), last_error = NULL, updated_at = excluded.updated_at",
            (upload_id, artifact_id, local_path, content_hash, status, project_id, task_id, run_id, mime_type, size_bytes, relative_path, artifact_type, timestamp, timestamp),
        )
        self.db.commit()
        return upload_id

    def get_upload(self, upload_id: str) -> dict[str, Any] | None:
        row = self.db.execute("SELECT * FROM pending_uploads WHERE upload_id = ?", (upload_id,)).fetchone()
        return dict(row) if row else None

    def list_uploads(self, statuses: list[str] | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM pending_uploads"
        params: list[Any] = []
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            query += f" WHERE status IN ({placeholders})"
            params.extend(statuses)
        query += " ORDER BY created_at ASC"
        return [dict(row) for row in self.db.execute(query, params).fetchall()]

    def mark_upload_attempt(self, upload_id: str, status: str = "UPLOADING") -> None:
        self.db.execute(
            "UPDATE pending_uploads SET status = ?, attempts = attempts + 1, last_error = NULL, updated_at = ? WHERE upload_id = ?",
            (status, utc_now(), upload_id),
        )
        self.db.commit()

    def complete_upload(self, upload_id: str, artifact_id: str) -> None:
        self.db.execute(
            "UPDATE pending_uploads SET artifact_id = ?, status = 'SUCCEEDED', last_error = NULL, updated_at = ? WHERE upload_id = ?",
            (artifact_id, utc_now(), upload_id),
        )
        self.db.commit()

    def attach_upload_artifact(self, upload_id: str, artifact_id: str) -> None:
        self.db.execute(
            "UPDATE pending_uploads SET artifact_id = ?, updated_at = ? WHERE upload_id = ?",
            (artifact_id, utc_now(), upload_id),
        )
        self.db.commit()

    def fail_upload(self, upload_id: str, error: str) -> None:
        self.db.execute(
            "UPDATE pending_uploads SET status = 'FAILED', last_error = ?, updated_at = ? WHERE upload_id = ?",
            (error[:2000], utc_now(), upload_id),
        )
        self.db.commit()

    def save_approval(self, approval_id: str, status: str, payload: dict[str, Any] | None = None) -> None:
        timestamp = utc_now()
        self.db.execute(
            "INSERT INTO approval_states (approval_id, status, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(approval_id) DO UPDATE SET status = excluded.status, payload = excluded.payload, updated_at = excluded.updated_at",
            (approval_id, status, json.dumps(payload or {}, ensure_ascii=False, sort_keys=True), timestamp, timestamp),
        )
        self.db.commit()

    def save_lifecycle_transition(self, transition: Any) -> None:
        """Persist a session/service lifecycle transition idempotently."""

        values = transition.model_dump(mode="json") if hasattr(transition, "model_dump") else dict(transition)
        self.db.execute(
            "INSERT OR IGNORE INTO lifecycle_transitions (transition_id, worker_id, user_session_id, event_type, from_state, to_state, session_state, active_run_ids, cleanup_run_ids, reason, metadata, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                values["transition_id"],
                values["worker_id"],
                values["user_session_id"],
                values["event_type"],
                values.get("from_state"),
                values["to_state"],
                values["session_state"],
                json.dumps(values.get("active_run_ids", []), ensure_ascii=False, sort_keys=True),
                json.dumps(values.get("cleanup_run_ids", []), ensure_ascii=False, sort_keys=True),
                values["reason"],
                json.dumps(values.get("metadata", {}), ensure_ascii=False, sort_keys=True),
                values["occurred_at"],
            ),
        )
        self.db.commit()

    def list_lifecycle_transitions(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("lifecycle_transition_limit_invalid")
        rows = self.db.execute(
            "SELECT * FROM lifecycle_transitions ORDER BY occurred_at ASC, transition_id ASC LIMIT ?",
            (limit,),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            values = dict(row)
            values["active_run_ids"] = json.loads(values["active_run_ids"])
            values["cleanup_run_ids"] = json.loads(values["cleanup_run_ids"])
            values["metadata"] = json.loads(values["metadata"])
            result.append(values)
        return result
