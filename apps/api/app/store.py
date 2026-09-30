from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from zipfile import ZIP_DEFLATED, ZipFile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from . import event_catalog, skill_match

from .contracts import (
    Agent,
    AgentConnection,
    AgentConnectionStatus,
    Device,
    DeviceCredential,
    DevicePairing,
    DevicePairingCreate,
    DevicePairingStatus,
    DeviceProjectCredential,
    DeviceProjectGrant,
    DeviceProjectGrantCreate,
    DeviceRegisterRequest,
    DeviceRevokeRequest,
    DeviceRuntimeState,
    DocumentDraft,
    DeviceStatus,
    AgentRegister,
    Artifact,
    ArtifactCreate,
    Evidence,
    EvidenceCreate,
    Event,
    EventOutbox,
    ExecutionProfile,
    GatewayCommandResult,
    Gate,
    GitFileIndex,
    GitRepository,
    GitRepositoryCreate,
    HumanMember,
    HumanMemberCreate,
    Handoff,
    HandoffCreate,
    HandoffReceipt,
    HandoffReceiptStatus,
    HandoffRecipientType,
    Invitation,
    InvitationCreate,
    # 账号系统（AUTH-1）
    AccountUpdateRequest,
    AccountView,
    RegisterRequest,
    Membership,
    Organization,
    OrganizationCreate,
    Project,
    ProjectCreate,
    ProjectMessage,
    Review,
    ReviewCreate,
    ReviewerKind,
    RiskDecisionRequest,
    RiskRegistryEntry,
    Run,
    RunComplete,
    RunCreate,
    Session,
    SessionCreate,
    Team,
    TeamCreate,
    AgentProjectGrant,
    Task,
    TaskClaimRequest,
    TaskLease,
    TaskProgressRequest,
    TaskResult,
    TaskResultSubmit,
    TaskCreate,
    TaskStatus,
)
from .object_store import LocalObjectStore, ObjectStore
from .git_adapter import LocalGitProvider
from .device_identity import DeviceIdentityError, create_challenge, hash_secret, verify_registration_signature
from .risk_rules import blocking_findings, ensure_approvable, gate_rules, normalize_findings, risk_summary
from .accounts import (
    hash_password,
    hash_token,
    is_valid_email,
    new_session_token,
    new_temporary_password,
    normalize_email,
    validate_password,
    verify_password,
)

# 组织/团队：默认是开发期种子身份。部署时可用环境变量指向已有组织行
# （真正的"多租户隔离"不在本轮范围：运行时走 SQLite，RLS 只在 PostgreSQL 仓储路径生效，
#   所以一个部署 = 一个组织；组织内可以有多个团队，这是本轮实现的协作单元。）
DEV_ORG_ID = os.getenv("PLATFORM_ORG_ID") or "00000000-0000-4000-8000-000000000001"
DEV_TEAM_ID = os.getenv("PLATFORM_TEAM_ID") or "00000000-0000-4000-8000-000000000002"
DEFAULT_ORG_NAME = os.getenv("PLATFORM_ORG_NAME") or "开发组织"

# 开发期种子账号：首个真实账号注册时，其名下数据会被一次性接管，该行改为 suspended 保留（外键与审计）
LEGACY_MEMBER_ID = "member-001"

# 会话有效期（秒）：30 天。服务端可撤销，到期即删。
SESSION_TTL_SECONDS = 30 * 24 * 3600

# Agent 心跳超时阈值 = 3 × 30s 心跳周期。Demo 1.0 期间固定，不做成环境变量（计划 §3 D3）。
AGENT_HEARTBEAT_TIMEOUT_SECONDS = 90


def now() -> str:
    return datetime.now(UTC).isoformat()


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


class _SerializedCursor:
    """把游标的取数操作也放进连接锁里。

    只锁 ``execute`` 是不够的：取数（fetchone/fetchall/迭代）同样会碰连接的语句缓存，
    另一个线程此时执行语句就会触发
    ``sqlite3.InterfaceError: bad parameter or other API misuse``。
    """

    def __init__(self, cursor: sqlite3.Cursor, lock: threading.RLock) -> None:
        self._cursor = cursor
        self._lock = lock

    def fetchone(self) -> Any:
        with self._lock:
            return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        with self._lock:
            return self._cursor.fetchall()

    def fetchmany(self, size: int = 1) -> list[Any]:
        with self._lock:
            return self._cursor.fetchmany(size)

    def __iter__(self) -> Any:
        with self._lock:
            rows = self._cursor.fetchall()
        return iter(rows)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _SerializedConnection:
    """串行化访问的 sqlite3 连接包装。

    Store 是"单连接 + check_same_thread=False"的设计，但平台里同时存在两条访问路径：
    FastAPI 的线程池请求与维护扫描线程（心跳超时、租约回收）。并发使用同一个连接会抛
    ``sqlite3.InterfaceError: bad parameter or other API misuse``，表现为请求 404/500
    与 WebSocket 建连后立刻断开。这里用一把可重入锁把所有连接操作串行化。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        object.__setattr__(self, "_connection", connection)
        object.__setattr__(self, "_lock", threading.RLock())

    def execute(self, *args: Any, **kwargs: Any) -> _SerializedCursor:
        with self._lock:
            cursor = self._connection.execute(*args, **kwargs)
        return _SerializedCursor(cursor, self._lock)

    def executemany(self, *args: Any, **kwargs: Any) -> _SerializedCursor:
        with self._lock:
            cursor = self._connection.executemany(*args, **kwargs)
        return _SerializedCursor(cursor, self._lock)

    def executescript(self, *args: Any, **kwargs: Any) -> _SerializedCursor:
        with self._lock:
            cursor = self._connection.executescript(*args, **kwargs)
        return _SerializedCursor(cursor, self._lock)

    def commit(self) -> None:
        with self._lock:
            self._connection.commit()

    def rollback(self) -> None:
        with self._lock:
            self._connection.rollback()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "_SerializedConnection":
        with self._lock:
            self._connection.__enter__()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        with self._lock:
            self._connection.__exit__(exc_type, exc, traceback)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._connection, name, value)


class Store:
    def __init__(self, path: str | Path = "data/platform.db", object_store: ObjectStore | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.object_store = object_store or LocalObjectStore(self.path.parent / "objects")
        self.db = _SerializedConnection(sqlite3.connect(self.path, check_same_thread=False))
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self._init_schema()
        self._seed()
        self._backfill_handoff_receipts()
        self._ensure_activity_event()

    def close(self) -> None:
        self.db.close()

    def _backfill_handoff_receipts(self) -> None:
        rows = self.db.execute(
            "SELECT h.* FROM handoffs h WHERE NOT EXISTS (SELECT 1 FROM handoff_receipts r WHERE r.handoff_id = h.id)"
        ).fetchall()
        for row in rows:
            try:
                receiver = json.loads(row["receiver"])
            except (TypeError, json.JSONDecodeError):
                receiver = row["receiver"]
            handoff_type = row["handoff_type"] or "RELAY"
            try:
                entries = self._handoff_recipient_entries(receiver, handoff_type)
            except ValueError:
                continue
            for receiver_type, receiver_id in entries:
                self.db.execute(
                    "INSERT OR IGNORE INTO handoff_receipts (id, handoff_id, project_id, receiver_type, receiver_id, status, received_by, received_at, decision_reason, decision_findings, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(uuid4()), row["id"], row["project_id"], receiver_type, receiver_id, row["receipt_status"] or "PENDING", row["received_by"], row["received_at"], row["decision_reason"], row["decision_findings"] or "[]", row["created_at"]),
                )
        self.db.commit()

    def _init_schema(self) -> None:
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS organizations (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                slug TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                name TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS human_members (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                team_id TEXT REFERENCES teams(id),
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                -- 账号字段（迁移 017）；历史成员为 NULL 即无口令、不可登录，由管理员重置接管
                password_hash TEXT,
                password_updated_at TEXT,
                last_login_at TEXT,
                is_admin INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS memberships (
                member_id TEXT NOT NULL REFERENCES human_members(id),
                team_id TEXT NOT NULL REFERENCES teams(id),
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(member_id, team_id)
            );
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                organization_id TEXT REFERENCES organizations(id),
                team_id TEXT REFERENCES teams(id),
                created_by TEXT NOT NULL DEFAULT 'member-001',
                name TEXT NOT NULL,
                competition_pack TEXT NOT NULL,
                problem_code TEXT,
                description TEXT NOT NULL,
                stage TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_memberships (
                project_id TEXT NOT NULL REFERENCES projects(id),
                member_id TEXT NOT NULL REFERENCES human_members(id),
                role TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(project_id, member_id)
            );
            CREATE TABLE IF NOT EXISTS project_messages (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                seq INTEGER NOT NULL,
                sender_kind TEXT NOT NULL,
                sender_member_id TEXT REFERENCES human_members(id),
                sender_agent_id TEXT,
                sender_name TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'text',
                ref_event_id INTEGER,
                ref_artifact_id TEXT,
                ref_task_id TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(project_id, seq)
            );
            CREATE TABLE IF NOT EXISTS project_chat_watermarks (
                project_id TEXT PRIMARY KEY REFERENCES projects(id),
                last_event_sequence INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS agent_project_grants (
                agent_id TEXT NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id),
                capabilities TEXT NOT NULL DEFAULT '[]',
                granted_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(agent_id, project_id)
            );
            CREATE TABLE IF NOT EXISTS git_repositories (
                project_id TEXT PRIMARY KEY REFERENCES projects(id),
                provider TEXT NOT NULL,
                remote_url TEXT,
                local_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS git_file_indexes (
                project_id TEXT NOT NULL REFERENCES projects(id),
                commit_sha TEXT NOT NULL,
                path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                indexed_at TEXT NOT NULL,
                PRIMARY KEY(project_id, commit_sha, path)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                member_id TEXT NOT NULL REFERENCES human_members(id),
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS invitations (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                team_id TEXT REFERENCES teams(id),
                email TEXT NOT NULL,
                role TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                stage TEXT NOT NULL,
                status TEXT NOT NULL,
                assignee TEXT NOT NULL,
                priority TEXT NOT NULL,
                requires_review INTEGER NOT NULL,
                allow_future_data INTEGER NOT NULL,
                input_artifacts TEXT NOT NULL,
                input_handoff_ids TEXT NOT NULL DEFAULT '[]',
                output_types TEXT NOT NULL,
                parent_task_id TEXT,
                dependency_task_ids TEXT NOT NULL DEFAULT '[]',
                acceptance_criteria TEXT NOT NULL DEFAULT '[]',
                required_capabilities TEXT NOT NULL DEFAULT '[]',
                budget TEXT NOT NULL DEFAULT '{}',
                evidence_requirements TEXT NOT NULL DEFAULT '[]',
                deadline TEXT,
                information_boundary TEXT NOT NULL DEFAULT '{}',
                resource_policy TEXT NOT NULL DEFAULT '{}',
                requires_human_approval INTEGER NOT NULL DEFAULT 1,
                assignee_member_id TEXT,
                blocked_reason TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS handoffs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                task_id TEXT NOT NULL REFERENCES tasks(id),
                sender_agent_id TEXT NOT NULL,
                receiver TEXT NOT NULL,
                status TEXT NOT NULL,
                objective TEXT NOT NULL,
                completed TEXT NOT NULL,
                input_artifacts TEXT NOT NULL,
                output_artifacts TEXT NOT NULL,
                key_conclusions TEXT NOT NULL,
                assumptions TEXT NOT NULL,
                evidence_refs TEXT NOT NULL,
                open_questions TEXT NOT NULL,
                risks TEXT NOT NULL,
                next_actions TEXT NOT NULL,
                requires_human_approval INTEGER NOT NULL,
                schema_version TEXT NOT NULL DEFAULT '1.0',
                handoff_type TEXT NOT NULL DEFAULT 'RELAY',
                input_handoff_ids TEXT NOT NULL DEFAULT '[]',
                revision_of_handoff_id TEXT,
                revision_number INTEGER NOT NULL DEFAULT 1,
                receipt_status TEXT NOT NULL DEFAULT 'PENDING',
                received_by TEXT,
                received_at TEXT,
                decision_reason TEXT,
                decision_findings TEXT NOT NULL DEFAULT '[]',
                idempotency_key TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS handoff_receipts (
                id TEXT PRIMARY KEY,
                handoff_id TEXT NOT NULL REFERENCES handoffs(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                receiver_type TEXT NOT NULL,
                receiver_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING',
                received_by TEXT,
                received_at TEXT,
                decision_reason TEXT,
                decision_findings TEXT NOT NULL DEFAULT '[]',
                idempotency_key TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(handoff_id, receiver_type, receiver_id)
            );
            CREATE INDEX IF NOT EXISTS idx_handoff_receipts_handoff ON handoff_receipts(handoff_id);
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                name TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                description TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                version INTEGER NOT NULL,
                status TEXT NOT NULL,
                source_path TEXT,
                task_id TEXT,
                run_id TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                data_policy TEXT NOT NULL,
                input_artifact_ids TEXT NOT NULL DEFAULT '[]',
                git_commit TEXT,
                snapshot_ref TEXT,
                created_by_kind TEXT NOT NULL DEFAULT 'member',
                approved_by TEXT,
                approved_at TEXT,
                downstream_allowed INTEGER NOT NULL DEFAULT 0,
                storage_key TEXT,
                size_bytes INTEGER,
                mime_type TEXT,
                immutable INTEGER NOT NULL DEFAULT 0,
                parent_artifact_id TEXT,
                archived_at TEXT
            );
            CREATE TABLE IF NOT EXISTS artifact_multipart_uploads (
                upload_id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL REFERENCES artifacts(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                storage_key TEXT NOT NULL,
                mime_type TEXT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE',
                completed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                task_id TEXT,
                agent_id TEXT NOT NULL,
                -- 执行归属（迁移 019）：哪台设备、谁的机器。device_id 由执行体上报，
                -- member_id 一律服务端从设备/Agent 归属推导（不信任客户端自报）
                device_id TEXT,
                member_id TEXT,
                status TEXT NOT NULL,
                source_commit TEXT,
                input_artifact_ids TEXT NOT NULL,
                environment_image_digest TEXT,
                dependency_lock TEXT,
                parameters TEXT NOT NULL,
                random_seed INTEGER,
                model_provider TEXT,
                model_name TEXT,
                tool_versions TEXT NOT NULL,
                network_policy TEXT NOT NULL,
                execution_profile TEXT NOT NULL DEFAULT '{}',
                data_access_policy TEXT NOT NULL,
                observed_input_files TEXT NOT NULL,
                output_artifact_ids TEXT NOT NULL,
                stdout TEXT NOT NULL,
                stderr TEXT NOT NULL,
                summary TEXT NOT NULL,
                information_boundary TEXT NOT NULL,
                usage TEXT NOT NULL DEFAULT '{}',
                started_at TEXT NOT NULL,
                completed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, started_at DESC);
            CREATE TABLE IF NOT EXISTS agents (
                agent_id TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                owner_member_id TEXT NOT NULL,
                model_provider TEXT NOT NULL,
                model_name TEXT NOT NULL,
                supported_tools TEXT NOT NULL,
                supported_languages TEXT NOT NULL,
                max_concurrency INTEGER NOT NULL,
                local_workspace TEXT,
                network_policy TEXT NOT NULL,
                status TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                capability_cards TEXT NOT NULL DEFAULT '[]',
                package_id TEXT,
                instance_id TEXT,
                package_source TEXT NOT NULL DEFAULT 'inferred'
            );
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                agent_id TEXT NOT NULL REFERENCES agents(agent_id),
                owner_member_id TEXT NOT NULL REFERENCES human_members(id),
                device_name TEXT NOT NULL,
                public_key TEXT NOT NULL,
                public_key_fingerprint TEXT NOT NULL UNIQUE,
                device_token_hash TEXT NOT NULL UNIQUE,
                platform TEXT NOT NULL,
                agent_version TEXT NOT NULL,
                capabilities TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                token_version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                last_seen TEXT,
                revoked_at TEXT,
                token_rotated_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_devices_organization ON devices(organization_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS device_token_rotations (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
                organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
                rotated_by TEXT NOT NULL REFERENCES human_members(id),
                reason TEXT NOT NULL,
                token_version INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_device_token_rotations_device ON device_token_rotations(device_id, created_at DESC);
            CREATE TABLE IF NOT EXISTS device_pairings (
                id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL REFERENCES organizations(id),
                created_by TEXT NOT NULL REFERENCES human_members(id),
                code_hash TEXT NOT NULL UNIQUE,
                challenge_hash TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING',
                expires_at TEXT NOT NULL,
                device_id TEXT REFERENCES devices(device_id),
                consumed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_device_pairings_pending ON device_pairings(status, expires_at);
            CREATE TABLE IF NOT EXISTS device_project_grants (
                id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                agent_id TEXT NOT NULL REFERENCES agents(agent_id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                token_hash TEXT NOT NULL UNIQUE,
                capabilities TEXT NOT NULL,
                granted_by TEXT NOT NULL REFERENCES human_members(id),
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_device_project_grants_scope ON device_project_grants(device_id, project_id, expires_at);
            CREATE TABLE IF NOT EXISTS agent_connections (
                connection_id TEXT PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                agent_id TEXT NOT NULL REFERENCES agents(agent_id),
                session_id TEXT NOT NULL,
                transport TEXT NOT NULL,
                status TEXT NOT NULL,
                last_received_sequence INTEGER NOT NULL DEFAULT 0,
                last_sent_sequence INTEGER NOT NULL DEFAULT 0,
                connected_at TEXT NOT NULL,
                last_heartbeat_at TEXT NOT NULL,
                disconnected_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_agent_connections_device ON agent_connections(device_id, status);
            CREATE TABLE IF NOT EXISTS document_drafts (
                artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                content TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_document_drafts_project ON document_drafts(project_id, updated_at DESC);
            CREATE TABLE IF NOT EXISTS device_runtime_state (
                device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
                connection_id TEXT,
                agent_version TEXT,
                adapter_versions TEXT NOT NULL DEFAULT '{}',
                capabilities TEXT NOT NULL DEFAULT '[]',
                running_run_ids TEXT NOT NULL DEFAULT '[]',
                local_queue_length INTEGER NOT NULL DEFAULT 0,
                user_session_state TEXT NOT NULL DEFAULT 'unknown',
                resource_summary TEXT NOT NULL DEFAULT '{}',
                reported_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gateway_command_results (
                id TEXT PRIMARY KEY,
                connection_id TEXT NOT NULL REFERENCES agent_connections(connection_id) ON DELETE CASCADE,
                message_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 1),
                message_type TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
                response_type TEXT NOT NULL CHECK (response_type IN ('gateway.ack', 'gateway.error')),
                request_hash TEXT,
                result TEXT,
                error_code TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(connection_id, message_id),
                UNIQUE(connection_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_gateway_command_results_message ON gateway_command_results(connection_id, message_id);
            CREATE INDEX IF NOT EXISTS idx_gateway_command_results_idempotency ON gateway_command_results(connection_id, idempotency_key);
            CREATE TABLE IF NOT EXISTS reviews (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                verdict TEXT NOT NULL,
                summary TEXT NOT NULL,
                findings TEXT NOT NULL,
                evidence_ids TEXT NOT NULL DEFAULT '[]',
                reviewer TEXT NOT NULL,
                reviewer_kind TEXT NOT NULL DEFAULT 'agent',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gates (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                target_type TEXT NOT NULL,
                target_id TEXT,
                status TEXT NOT NULL,
                required_human_approval INTEGER NOT NULL,
                blocking_findings TEXT NOT NULL,
                rules TEXT NOT NULL,
                review_ids TEXT NOT NULL DEFAULT '[]',
                evidence_ids TEXT NOT NULL DEFAULT '[]',
                risk_summary TEXT NOT NULL DEFAULT '{}',
                input_snapshot TEXT NOT NULL DEFAULT '{}',
                invalidated_at TEXT,
                invalidation_reason TEXT,
                approved_by TEXT,
                approved_at TEXT,
                UNIQUE(project_id, target_type, target_id)
            );
            CREATE TABLE IF NOT EXISTS risks (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                review_id TEXT NOT NULL REFERENCES reviews(id),
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                code TEXT NOT NULL,
                severity TEXT NOT NULL,
                message TEXT NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                evidence_refs TEXT NOT NULL DEFAULT '[]',
                owner TEXT,
                resolution_reason TEXT,
                resolved_by TEXT,
                resolved_at TEXT,
                closure_evidence_ids TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_risks_target ON risks(project_id, target_type, target_id);
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                claim TEXT NOT NULL,
                evidence_type TEXT NOT NULL,
                artifact_id TEXT,
                run_id TEXT,
                source_ref TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL REFERENCES projects(id),
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                actor_kind TEXT NOT NULL DEFAULT 'system',
                object_type TEXT,
                object_id TEXT,
                idempotency_key TEXT,
                schema_version TEXT NOT NULL DEFAULT '1.0',
                UNIQUE(project_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS event_outbox (
                id TEXT PRIMARY KEY,
                event_id TEXT NOT NULL UNIQUE REFERENCES events(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                status TEXT NOT NULL DEFAULT 'PENDING',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                locked_at TEXT,
                lock_expires_at TEXT,
                delivered_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_event_outbox_pending ON event_outbox(status, available_at, created_at);
            CREATE TABLE IF NOT EXISTS task_leases (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL REFERENCES tasks(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                agent_id TEXT NOT NULL REFERENCES agents(agent_id),
                lease_token TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                issued_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                last_heartbeat TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_task_leases_active ON task_leases(task_id, status);
            CREATE TABLE IF NOT EXISTS idempotency_records (
                key TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                response TEXT NOT NULL,
                request_hash TEXT,
                created_at TEXT NOT NULL
            );
            """
        )
        self._ensure_columns(
            "projects",
            {
                "organization_id": "TEXT",
                "team_id": "TEXT",
                "created_by": "TEXT NOT NULL DEFAULT 'member-001'",
            },
        )
        self._ensure_columns(
            "tasks",
            {
                "parent_task_id": "TEXT",
                "dependency_task_ids": "TEXT NOT NULL DEFAULT '[]'",
                "acceptance_criteria": "TEXT NOT NULL DEFAULT '[]'",
                "required_capabilities": "TEXT NOT NULL DEFAULT '[]'",
                "deadline": "TEXT",
                "information_boundary": "TEXT NOT NULL DEFAULT '{}'",
                "resource_policy": "TEXT NOT NULL DEFAULT '{}'",
                "requires_human_approval": "INTEGER NOT NULL DEFAULT 1",
                "input_handoff_ids": "TEXT NOT NULL DEFAULT '[]'",
            },
        )
        self._ensure_columns("handoffs", {"schema_version": "TEXT NOT NULL DEFAULT '1.0'"})
        self._ensure_columns(
            "handoffs",
            {
                "handoff_type": "TEXT NOT NULL DEFAULT 'RELAY'",
                "input_handoff_ids": "TEXT NOT NULL DEFAULT '[]'",
                "revision_of_handoff_id": "TEXT",
                "revision_number": "INTEGER NOT NULL DEFAULT 1",
                "receipt_status": "TEXT NOT NULL DEFAULT 'PENDING'",
                "received_by": "TEXT",
                "received_at": "TEXT",
                "decision_reason": "TEXT",
                "decision_findings": "TEXT NOT NULL DEFAULT '[]'",
                "idempotency_key": "TEXT",
            },
        )
        self._ensure_columns(
            "gates",
            {
                "input_snapshot": "TEXT NOT NULL DEFAULT '{}'",
                "invalidated_at": "TEXT",
                "invalidation_reason": "TEXT",
            },
        )
        self._ensure_columns("reviews", {"reviewer_kind": "TEXT NOT NULL DEFAULT 'agent'", "evidence_ids": "TEXT NOT NULL DEFAULT '[]'"})
        self._ensure_columns("gates", {"review_ids": "TEXT NOT NULL DEFAULT '[]'", "evidence_ids": "TEXT NOT NULL DEFAULT '[]'", "risk_summary": "TEXT NOT NULL DEFAULT '{}'"})
        self._ensure_columns(
            "events",
            {
                "actor_kind": "TEXT NOT NULL DEFAULT 'system'",
                "object_type": "TEXT",
                "object_id": "TEXT",
                "idempotency_key": "TEXT",
                "schema_version": "TEXT NOT NULL DEFAULT '1.0'",
            },
        )
        self._ensure_columns(
            "artifacts",
            {
                "input_artifact_ids": "TEXT NOT NULL DEFAULT '[]'",
                "git_commit": "TEXT",
                "snapshot_ref": "TEXT",
                "created_by_kind": "TEXT NOT NULL DEFAULT 'member'",
                "approved_by": "TEXT",
                "approved_at": "TEXT",
                "downstream_allowed": "INTEGER NOT NULL DEFAULT 0",
                "storage_key": "TEXT",
                "size_bytes": "INTEGER",
                "mime_type": "TEXT",
                "immutable": "INTEGER NOT NULL DEFAULT 0",
                "parent_artifact_id": "TEXT",
                "archived_at": "TEXT",
            },
        )
        self._ensure_columns(
            "artifact_multipart_uploads",
            {
                "status": "TEXT NOT NULL DEFAULT 'ACTIVE'",
                "completed_at": "TEXT",
            },
        )
        self._ensure_columns("idempotency_records", {"request_hash": "TEXT"})
        self._ensure_columns("gateway_command_results", {"request_hash": "TEXT"})
        self._ensure_columns("event_outbox", {"lock_expires_at": "TEXT"})
        self._ensure_columns("runs", {"execution_profile": "TEXT NOT NULL DEFAULT '{}'"})
        self._ensure_columns("device_pairings", {"challenge_hash": "TEXT"})
        self._ensure_columns(
            "devices",
            {
                "token_version": "INTEGER NOT NULL DEFAULT 1",
                "token_rotated_at": "TEXT",
            },
        )
        self.db.execute(
            """
            INSERT OR IGNORE INTO event_outbox
                (id, event_id, project_id, status, attempts, available_at, created_at, updated_at)
            SELECT id, id, project_id, 'PENDING', 0, created_at, created_at, created_at
            FROM events
            """
        )
        # 账号字段（对应 PG 迁移 017）：给既有库补列，新库由上面的 CREATE TABLE 带上
        self._ensure_columns(
            "human_members",
            {
                "password_hash": "TEXT",
                "password_updated_at": "TEXT",
                "last_login_at": "TEXT",
                "is_admin": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        # 派单模式（对应 PG 迁移 018）：任务可指派给成员，领取时按成员过滤
        self._ensure_columns("tasks", {"assignee_member_id": "TEXT"})
        # 执行归属（对应 PG 迁移 019）
        self._ensure_columns("runs", {"device_id": "TEXT", "member_id": "TEXT"})
        # 项目工作区（对应 PG 迁移 020）：立项目标/人数、任务推进模式、聊天流
        self._ensure_columns(
            "projects",
            {
                "goal": "TEXT",
                "target_member_count": "INTEGER",
                "task_mode": "TEXT NOT NULL DEFAULT 'manual'",
            },
        )
        # 执行体描述（对应 PG 迁移 021，AIP-1a/1c）：能力卡 + 包/实例两段身份
        self._ensure_columns(
            "agents",
            {
                "capability_cards": "TEXT NOT NULL DEFAULT '[]'",
                "package_id": "TEXT",
                "instance_id": "TEXT",
                "package_source": "TEXT NOT NULL DEFAULT 'inferred'",
            },
        )
        self.db.execute("UPDATE agents SET instance_id = agent_id WHERE instance_id IS NULL")
        self._backfill_agent_packages()
        # 意图对象（对应 PG 迁移 022，AIP-1d）：预算与显式证据要求
        self._ensure_columns(
            "tasks",
            {
                "budget": "TEXT NOT NULL DEFAULT '{}'",
                "evidence_requirements": "TEXT NOT NULL DEFAULT '[]'",
            },
        )
        # 执行用量回报（对应 PG 迁移 023，COST-1）
        self._ensure_columns("runs", {"usage": "TEXT NOT NULL DEFAULT '{}'"})
        # 历史事件的 actor_kind 多为 system（早期不区分主体）：把已知的 Agent 行为补成 agent，
        # 让"谁家的机器干的活"在时间线上直接可读
        self.db.execute(
            "UPDATE events SET actor_kind = 'agent' WHERE actor_kind = 'system' "
            "AND actor LIKE 'agent-%' AND event_type IN "
            "('task.claimed', 'task.progress', 'task.result_submitted', 'run.created', 'run.completed', 'run.failed', 'run.blocked')"
        )
        # sessions.token 语义改为存 SHA-256（64 位十六进制）；历史明文令牌（43 字符）无法再解析，清掉
        self.db.execute("DELETE FROM sessions WHERE length(token) != 64")
        self.db.commit()

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in self.db.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _seed_identity(self) -> None:
        created = now()
        self.db.execute("INSERT OR IGNORE INTO organizations (id, name, slug, created_at) VALUES (?, ?, 'dev-org', ?)", (DEV_ORG_ID, DEFAULT_ORG_NAME, created))
        self.db.execute("INSERT OR IGNORE INTO teams (id, organization_id, name, created_at) VALUES (?, ?, '示例建模队', ?)", (DEV_TEAM_ID, DEV_ORG_ID, created))
        self.db.execute("INSERT OR IGNORE INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES ('member-001', ?, ?, 'member-001@example.local', '开发负责人', 'active', ?)", (DEV_ORG_ID, DEV_TEAM_ID, created))
        self.db.execute("INSERT OR IGNORE INTO memberships (member_id, team_id, role, created_at) VALUES ('member-001', ?, 'owner', ?)", (DEV_TEAM_ID, created))
        self.db.execute("UPDATE projects SET organization_id = COALESCE(organization_id, ?), team_id = COALESCE(team_id, ?), created_by = COALESCE(created_by, 'member-001')", (DEV_ORG_ID, DEV_TEAM_ID))
        self.db.execute("INSERT OR IGNORE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) SELECT a.agent_id, p.id, '[\"task.claim\", \"artifact.read\", \"artifact.write\", \"run.create\", \"run.event\"]', 'member-001', ? FROM agents a CROSS JOIN projects p", (created,))
        self.db.commit()

    def _seed(self) -> None:
        self._seed_identity()
        if self.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]:
            return
        created = now()
        project_id = str(uuid4())
        self.db.execute(
            "INSERT INTO projects (id, organization_id, team_id, created_by, name, competition_pack, problem_code, description, stage, progress, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, DEV_ORG_ID, DEV_TEAM_ID, "member-001", "C题 · 风光储能协同优化", "cumcm-2026", "C", "以 C 题四问交接包为样板，验证任务交接、滚动仿真和证据审计。", "review", 68, created, created),
        )
        self.db.execute("INSERT OR IGNORE INTO project_memberships (project_id, member_id, role, created_at) VALUES (?, 'member-001', 'project_lead', ?)", (project_id, created))
        seed_tasks = [
            ("题面事实与数据画像", "对题面、附件和时间边界建立可审计事实台账。", "problem_analysis", "Lin / 审题 Agent", "high", "APPROVED"),
            ("日前购电计划与因果执行", "完成问题二模型、执行策略和结果表。", "modeling", "Mira / 模型 Agent", "critical", "APPROVED"),
            ("滚动仿真与消融实验", "检查问题三滚动窗口的因果信息边界和消融结果。", "simulation", "Aster / 仿真 Agent", "high", "WAITING_REVIEW"),
            ("四问审计与风险清单", "复核 SOC、调整费、跨问一致性和未来信息使用。", "review", "Reviewer / 复核 Agent", "critical", "READY"),
            ("论文素材与交付包", "将已批准结果映射到论文、图表和提交目录。", "paper", "Nova / 写作 Agent", "medium", "DRAFT"),
        ]
        task_ids: list[str] = []
        for title, description, stage, assignee, priority, status in seed_tasks:
            task_id = str(uuid4())
            task_ids.append(task_id)
            self.db.execute(
                "INSERT INTO tasks (id, project_id, title, description, stage, status, assignee, priority, requires_review, allow_future_data, input_artifacts, input_handoff_ids, output_types, parent_task_id, dependency_task_ids, acceptance_criteria, required_capabilities, deadline, information_boundary, resource_policy, requires_human_approval, blocked_reason, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, project_id, title, description, stage, status, assignee, priority, 1, 0, "[]", "[]", "[]", None, "[]", "[]", "[]", None, "{}", "{}", 1, None, created),
            )
        artifacts = [
            ("PROBLEM_ANALYSIS.md", "problem_analysis", "题面任务回译、关键概念对齐和图表规划。", "APPROVED", "02_项目工作区/PROBLEM_ANALYSIS.md"),
            ("MODELING_REPORT.md", "model_spec", "四问模型、假设、信息边界和参数定义。", "APPROVED", "01_项目工作区/MODELING_REPORT.md"),
            ("problem_3_results.json", "result_table", "问题三滚动仿真、消融和结果指标。", "PENDING_REVIEW", "01_项目工作区/figures/problem_3_results.json"),
            ("INFORMATION_BOUNDARY_REVIEW.md", "audit_report", "执行层未来信息使用审查。", "PENDING_REVIEW", "01_项目工作区/INFORMATION_BOUNDARY_REVIEW.md"),
            ("main.tex", "paper_source", "待交付论文源文件。", "DRAFT", "01_项目工作区/paper/main.tex"),
        ]
        for name, artifact_type, description, status, source_path in artifacts:
            self.db.execute(
                "INSERT INTO artifacts (id, project_id, name, artifact_type, description, content_hash, version, status, source_path, task_id, run_id, created_by, created_at, data_policy, input_artifact_ids, git_commit, snapshot_ref, created_by_kind, approved_by, approved_at, downstream_allowed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), project_id, name, artifact_type, description, self._hash(name + description), 1, status, source_path, None, None, "team-lead", created, json.dumps({"future_data": "deny"}), "[]", None, None, "member", "team-lead" if status == "APPROVED" else None, created if status == "APPROVED" else None, int(status == "APPROVED")),
            )
        seed_agents = [
            # AIP-1a 之后：技能（能干什么）与授权范围（被允许干什么）是两套词表，
            # 示例执行体声明的是**技能**，授权范围在 agent_project_grants 里。
            (
                "agent-mira",
                "Mira / 模型 Agent",
                "cloud",
                "reasoning-pro",
                ["python", "scipy", "highs"],
                [("python", "3.12", ["data_profile"], ["model_spec", "code"]), ("scipy", "", ["problem_facts"], ["result_table"])],
            ),
            (
                "agent-aster",
                "Aster / 仿真 Agent",
                "local",
                "qwen3-coder",
                ["python", "pandas", "matplotlib"],
                [("python", "3.12", ["model_spec"], ["result_table", "figure"]), ("pandas", "", ["data_profile"], ["result_table"])],
            ),
            (
                "agent-nova",
                "Nova / 写作 Agent",
                "cloud",
                "writing-review",
                ["markdown", "latex"],
                [("markdown", "", ["result_table", "figure"], ["paper_source"]), ("latex", "", ["paper_source"], ["compiled_pdf"])],
            ),
        ]
        for agent_id, display_name, provider, model, languages, cards in seed_agents:
            self.db.execute(
                "INSERT INTO agents (agent_id, display_name, owner_member_id, model_provider, model_name, supported_tools, "
                "supported_languages, max_concurrency, local_workspace, network_policy, status, last_seen, capability_cards) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    agent_id,
                    display_name,
                    "member-001",
                    provider,
                    model,
                    json.dumps([card[0] for card in cards]),
                    json.dumps(languages),
                    1,
                    "C:/Users/19855/Documents/ChatGPT/数学建模/C题工作区",
                    "deny-by-default",
                    "online",
                    created,
                    json.dumps(
                        [
                            {"skill": skill, "version": version, "inputs": inputs, "outputs": outputs, "description": ""}
                            for skill, version, inputs, outputs in cards
                        ],
                        ensure_ascii=False,
                    ),
                ),
            )
        self.db.execute("INSERT OR IGNORE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES (?, ?, ?, 'member-001', ?)", (agent_id, project_id, json.dumps(["task.claim", "artifact.read", "artifact.write", "run.create", "run.event"]), created))
        handoff_id = str(uuid4())
        self.db.execute(
            "INSERT INTO handoffs (id, project_id, task_id, sender_agent_id, receiver, status, objective, completed, input_artifacts, output_artifacts, key_conclusions, assumptions, evidence_refs, open_questions, risks, next_actions, requires_human_approval, schema_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (handoff_id, project_id, task_ids[2], "agent-aster", "Reviewer / 复核 Agent", "PASS_WITH_ASSUMPTIONS", "核对问题三滚动仿真的可实施性和消融实验。", json.dumps(["完成窗口级结果对比", "生成审计输入清单"]), json.dumps(["problem_3_results.json"]), json.dumps(["INFORMATION_BOUNDARY_REVIEW.md"]), json.dumps(["Q3 需要以冻结储能轨迹执行，不能把完整未来真实曲线送入执行层。"]), json.dumps(["预测窗口和结算窗口的定义已分开。"]), json.dumps(["INFORMATION_BOUNDARY_REVIEW.md"]), json.dumps(["需要确认论文是否准确区分 oracle 对照和正式策略。"]), json.dumps([{ "severity": "major", "text": "审计结论需要由独立 Agent 复核。" }]), json.dumps(["复核审计脚本", "决定问题三门禁"]), 1, "1.0", created),
        )
        self.db.commit()
        self.add_event(project_id, "project.seeded", "system", {"message": "CUMCM 2026 C题样例项目已建立"})

    def _ensure_activity_event(self) -> None:
        if self.db.execute("SELECT COUNT(*) FROM events").fetchone()[0]:
            return
        row = self.db.execute("SELECT id FROM projects ORDER BY created_at LIMIT 1").fetchone()
        if row:
            self.add_event(UUID(row["id"]), "project.synced", "system", {"message": "开发存储已恢复，项目时间线可用"})

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _json(value: str) -> Any:
        return json.loads(value)

    def _project(self, row: sqlite3.Row) -> Project:
        values = dict(row)
        values["organization_id"] = UUID(values["organization_id"]) if values.get("organization_id") else None
        values["team_id"] = UUID(values["team_id"]) if values.get("team_id") else None
        values["created_by"] = values.get("created_by") or "member-001"
        # 工作区字段：老行（迁移前创建）没有值，给默认，避免 Pydantic 直接炸
        values["goal"] = values.get("goal")
        values["target_member_count"] = values.get("target_member_count")
        values["task_mode"] = values.get("task_mode") or "manual"
        values["created_at"] = parse_time(values["created_at"])
        values["updated_at"] = parse_time(values["updated_at"])
        return Project(**values)

    def _organization(self, row: sqlite3.Row) -> Organization:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["created_at"] = parse_time(values["created_at"])
        return Organization(**values)

    def _team(self, row: sqlite3.Row) -> Team:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["organization_id"] = UUID(values["organization_id"])
        values["created_at"] = parse_time(values["created_at"])
        return Team(**values)

    def _member(self, row: sqlite3.Row) -> HumanMember:
        values = dict(row)
        values["organization_id"] = UUID(values["organization_id"])
        values["team_id"] = UUID(values["team_id"]) if values["team_id"] else None
        values["created_at"] = parse_time(values["created_at"])
        # 账号列（017）不属于 HumanMember；显式剔除，避免依赖 pydantic 的 extra 行为
        for extra in ("password_hash", "password_updated_at", "last_login_at", "is_admin"):
            values.pop(extra, None)
        return HumanMember(**values)

    def _invitation(self, row: sqlite3.Row) -> Invitation:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["organization_id"] = UUID(values["organization_id"])
        values["team_id"] = UUID(values["team_id"]) if values["team_id"] else None
        values["expires_at"] = parse_time(values["expires_at"])
        values["created_at"] = parse_time(values["created_at"])
        return Invitation(**values)

    def list_organizations(self) -> list[Organization]:
        return [self._organization(row) for row in self.db.execute("SELECT * FROM organizations ORDER BY created_at")]

    def create_organization(self, data: OrganizationCreate) -> Organization:
        organization_id = str(uuid4())
        timestamp = now()
        self.db.execute("INSERT INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)", (organization_id, data.name, data.slug, timestamp))
        self.db.commit()
        return self._organization(self.db.execute("SELECT * FROM organizations WHERE id = ?", (organization_id,)).fetchone())

    def list_teams(self, organization_id: UUID) -> list[Team]:
        return [self._team(row) for row in self.db.execute("SELECT * FROM teams WHERE organization_id = ? ORDER BY created_at", (str(organization_id),))]

    def create_team(self, data: TeamCreate) -> Team:
        organization = self.db.execute("SELECT id FROM organizations WHERE id = ?", (str(data.organization_id),)).fetchone()
        if not organization:
            raise KeyError("organization_not_found")
        team_id = str(uuid4())
        timestamp = now()
        self.db.execute("INSERT INTO teams (id, organization_id, name, created_at) VALUES (?, ?, ?, ?)", (team_id, str(data.organization_id), data.name, timestamp))
        self.db.commit()
        return self._team(self.db.execute("SELECT * FROM teams WHERE id = ?", (team_id,)).fetchone())

    def create_member(self, data: HumanMemberCreate) -> HumanMember:
        organization = self.db.execute("SELECT id FROM organizations WHERE id = ?", (str(data.organization_id),)).fetchone()
        if not organization:
            raise KeyError("organization_not_found")
        if data.team_id:
            team = self.db.execute("SELECT organization_id FROM teams WHERE id = ?", (str(data.team_id),)).fetchone()
            if not team or team["organization_id"] != str(data.organization_id):
                raise ValueError("team_not_in_organization")
        member_id = str(uuid4())
        timestamp = now()
        self.db.execute("INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)", (member_id, str(data.organization_id), str(data.team_id) if data.team_id else None, data.email, data.display_name, timestamp))
        if data.team_id:
            self.db.execute("INSERT INTO memberships (member_id, team_id, role, created_at) VALUES (?, ?, ?, ?)", (member_id, str(data.team_id), data.role, timestamp))
        self.db.commit()
        return self._member(self.db.execute("SELECT * FROM human_members WHERE id = ?", (member_id,)).fetchone())

    def get_member(self, member_id: str) -> HumanMember:
        row = self.db.execute("SELECT * FROM human_members WHERE id = ?", (member_id,)).fetchone()
        if not row:
            raise KeyError("member_not_found")
        return self._member(row)

    def create_session(self, data: SessionCreate) -> Session:
        member = self.get_member(data.member_id)
        if member.status != "active":
            raise PermissionError("member_not_active")
        return self._issue_session(member, data.expires_in_seconds)

    def _issue_session(self, member: HumanMember, expires_in_seconds: int = SESSION_TTL_SECONDS) -> Session:
        """签发会话：库里只存令牌的 SHA-256（与设备令牌一致），明文只在响应里出现一次。"""

        token = new_session_token()
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
        self.db.execute(
            "INSERT INTO sessions (token, member_id, expires_at) VALUES (?, ?, ?)",
            (hash_token(token), member.id, expires_at.isoformat()),
        )
        self.db.commit()
        return Session(token=token, member_id=member.id, expires_at=expires_at)

    def resolve_session(self, token: str) -> HumanMember:
        # 兼容一次：库里的列名仍是 token，但存的是哈希
        row = self.db.execute("SELECT member_id, expires_at FROM sessions WHERE token = ?", (hash_token(token),)).fetchone()
        if not row:
            raise PermissionError("invalid_session")
        if parse_time(row["expires_at"]) <= datetime.now(UTC):
            self.db.execute("DELETE FROM sessions WHERE token = ?", (hash_token(token),))
            self.db.commit()
            raise PermissionError("session_expired")
        return self.get_member(row["member_id"])

    def delete_session(self, token: str) -> None:
        """登出：直接删除该会话，无需等过期。"""

        self.db.execute("DELETE FROM sessions WHERE token = ?", (hash_token(token),))
        self.db.commit()

    def delete_sessions_for_member(self, member_id: str, *, keep_token: str | None = None) -> int:
        """改密/重置后撤销该成员的其他会话；keep_token 用于保留当前设备不掉线。"""

        if keep_token:
            cursor = self.db.execute("DELETE FROM sessions WHERE member_id = ? AND token != ?", (member_id, hash_token(keep_token)))
        else:
            cursor = self.db.execute("DELETE FROM sessions WHERE member_id = ?", (member_id,))
        self.db.commit()
        return cursor.rowcount or 0

    def prune_expired_sessions(self) -> int:
        cursor = self.db.execute("DELETE FROM sessions WHERE expires_at <= ?", (datetime.now(UTC).isoformat(),))
        self.db.commit()
        return cursor.rowcount or 0

    # ---------- 账号：注册 / 登录 / 口令（AUTH-1） ----------

    def count_accounts_with_password(self) -> int:
        """已设口令的账号数。为 0 表示还没有真实账号——首个注册者成为管理员。"""

        return int(self.db.execute("SELECT COUNT(*) AS c FROM human_members WHERE password_hash IS NOT NULL AND status = 'active'").fetchone()["c"])

    def find_member_by_email(self, email: str) -> HumanMember | None:
        row = self.db.execute("SELECT * FROM human_members WHERE lower(email) = ?", (normalize_email(email),)).fetchone()
        return self._member(row) if row else None

    def account_view(self, member: HumanMember) -> AccountView:
        row = self.db.execute("SELECT password_hash, last_login_at, is_admin FROM human_members WHERE id = ?", (member.id,)).fetchone()
        return AccountView(
            member=member,
            has_password=bool(row["password_hash"]),
            is_admin=bool(row["is_admin"]),
            last_login_at=parse_time(row["last_login_at"]) if row["last_login_at"] else None,
        )

    def register_account(
        self, data: RegisterRequest, *, allow_open_registration: bool = False
    ) -> tuple[Session, AccountView]:
        """注册：邀请码路径（带角色）或开放注册（`allow_open_registration`，一律 contributor）。

        首个注册者免码并自动成为管理员（并接管存量 member-001 的数据）——这条优先级最高，
        与是否开放注册无关：空库的"第一个账号"永远是管理员。
        """

        email = normalize_email(data.email)
        if not is_valid_email(email):
            raise ValueError("email_invalid")
        validate_password(data.password)
        display_name = data.display_name.strip()
        if len(display_name) < 2:
            raise ValueError("display_name_too_short")
        if self.find_member_by_email(email):
            raise ValueError("email_already_registered")
        first_admin = self.count_accounts_with_password() == 0
        # 无邀请码的注册只允许两种情况：首个账号（成为管理员）或开放注册（成为 contributor）
        code = (data.invite_code or "").strip() or None
        invitation = self._consume_invitation(code, email, allow_missing=first_admin or (allow_open_registration and code is None))
        role = invitation.role if invitation else ("owner" if first_admin else "contributor")
        member = self._create_account(email, display_name, data.password, role=role)
        if first_admin:
            # 首个账号即管理员：账号管理的权限来源（与项目角色是两套），并接管存量数据
            self.db.execute("UPDATE human_members SET is_admin = 1 WHERE id = ?", (member.id,))
            self.db.commit()
            member = self.get_member(member.id)
            self._claim_legacy_ownership(member.id)
        else:
            # 受邀成员默认加入组织内**现有**项目（角色取自邀请码）。
            # 否则新队友登录后看不到任何项目——列表是按项目成员关系过滤的（list_projects_for_member）。
            # 注：这是"小团队开箱即用"的默认；项目级成员管理界面（增删成员）尚未做，属已知边界。
            self._join_organization_projects(member.id, role=role)
        self._touch_login(member.id)
        session = self._issue_session(member)
        return session, self.account_view(member)

    def authenticate_account(self, email: str, password: str) -> AccountView:
        """校验邮箱口令。失败一律同一条错误码，避免暴露"邮箱是否存在"。"""

        member = self.find_member_by_email(email)
        if not member:
            raise PermissionError("invalid_credentials")
        row = self.db.execute("SELECT password_hash, status FROM human_members WHERE id = ?", (member.id,)).fetchone()
        if not verify_password(password, row["password_hash"]):
            raise PermissionError("invalid_credentials")
        if row["status"] != "active":
            raise PermissionError("account_suspended")
        self._touch_login(member.id)
        return self.account_view(self.get_member(member.id))

    def login_account(self, email: str, password: str) -> tuple[Session, AccountView]:
        view = self.authenticate_account(email, password)
        self.prune_expired_sessions()
        session = self._issue_session(view.member)
        return session, view

    def change_password(self, member_id: str, current_password: str, new_password: str, *, keep_token: str | None = None) -> AccountView:
        row = self.db.execute("SELECT password_hash FROM human_members WHERE id = ?", (member_id,)).fetchone()
        if not row:
            raise KeyError("member_not_found")
        if not verify_password(current_password, row["password_hash"]):
            raise PermissionError("current_password_invalid")
        validate_password(new_password)
        self._set_password(member_id, new_password)
        self.delete_sessions_for_member(member_id, keep_token=keep_token)
        return self.account_view(self.get_member(member_id))

    def reset_password(self, member_id: str, *, actor_member_id: str) -> tuple[str, AccountView]:
        """管理员重置：生成一次性临时口令并撤销该成员全部会话。"""

        if member_id == actor_member_id:
            raise ValueError("use_password_change_for_self")
        member = self.get_member(member_id)
        temporary = new_temporary_password()
        self._set_password(member.id, temporary)
        self.delete_sessions_for_member(member.id)
        return temporary, self.account_view(member)

    def list_accounts(self) -> list[AccountView]:
        rows = self.db.execute("SELECT * FROM human_members ORDER BY created_at").fetchall()
        return [self.account_view(self._member(row)) for row in rows]

    def update_account(self, member_id: str, data: AccountUpdateRequest, *, actor_member_id: str) -> AccountView:
        member = self.get_member(member_id)
        if data.status is not None:
            if member_id == actor_member_id and data.status != "active":
                raise ValueError("cannot_suspend_self")
            self.db.execute("UPDATE human_members SET status = ? WHERE id = ?", (data.status, member_id))
        if data.is_admin is not None:
            if member_id == actor_member_id and not data.is_admin:
                raise ValueError("cannot_demote_self")
            if not data.is_admin and self._count_admins() <= 1 and self._is_admin(member_id):
                raise ValueError("last_admin_cannot_be_demoted")
            self.db.execute("UPDATE human_members SET is_admin = ? WHERE id = ?", (1 if data.is_admin else 0, member_id))
        self.db.commit()
        return self.account_view(self.get_member(member_id))

    def _join_organization_projects(self, member_id: str, *, role: str = "contributor") -> int:
        """把成员加入组织内现有项目（幂等）。角色沿用项目角色词表，与邀请码里的取值一致。"""

        if role not in {"owner", "project_lead", "contributor", "reviewer", "observer"}:
            role = "contributor"
        member = self.db.execute("SELECT organization_id FROM human_members WHERE id = ?", (member_id,)).fetchone()
        if not member:
            raise KeyError("member_not_found")
        projects = self.db.execute("SELECT id FROM projects WHERE organization_id = ?", (member["organization_id"],)).fetchall()
        timestamp = now()
        added = 0
        for project in projects:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO project_memberships (project_id, member_id, role, created_at) VALUES (?, ?, ?, ?)",
                (project["id"], member_id, role, timestamp),
            )
            added += cursor.rowcount or 0
        self.db.commit()
        return added

    def _count_admins(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) AS c FROM human_members WHERE is_admin = 1 AND status = 'active'").fetchone()["c"])

    def _is_admin(self, member_id: str) -> bool:
        row = self.db.execute("SELECT is_admin FROM human_members WHERE id = ?", (member_id,)).fetchone()
        return bool(row and row["is_admin"])

    def _set_password(self, member_id: str, password: str) -> None:
        timestamp = now()
        self.db.execute(
            "UPDATE human_members SET password_hash = ?, password_updated_at = ? WHERE id = ?",
            (hash_password(password), timestamp, member_id),
        )
        self.db.commit()

    def _touch_login(self, member_id: str) -> None:
        self.db.execute("UPDATE human_members SET last_login_at = ? WHERE id = ?", (now(), member_id))
        self.db.commit()

    def _create_account(self, email: str, display_name: str, password: str, *, role: str = "contributor") -> HumanMember:
        member_id = str(uuid4())
        timestamp = now()
        self.db.execute(
            "INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at, password_hash, password_updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            (member_id, DEV_ORG_ID, DEV_TEAM_ID, email, display_name, timestamp, hash_password(password), timestamp),
        )
        self.db.execute(
            "INSERT OR IGNORE INTO memberships (member_id, team_id, role, created_at) VALUES (?, ?, ?, ?)",
            (member_id, DEV_TEAM_ID, role, timestamp),
        )
        self.db.commit()
        return self.get_member(member_id)

    def _consume_invitation(self, code: str | None, email: str, *, allow_missing: bool) -> Invitation | None:
        """校验并核销邀请码：必须存在、PENDING、未过期、邮箱匹配（首个管理员可免码）。"""

        if not code or not code.strip():
            if allow_missing:
                return None
            raise PermissionError("invite_code_required")
        token = code.strip()
        row = self.db.execute("SELECT * FROM invitations WHERE token = ? OR id = ?", (token, token)).fetchone()
        if not row:
            raise PermissionError("invite_code_invalid")
        if row["status"] != "PENDING":
            raise PermissionError("invite_code_used")
        if parse_time(row["expires_at"]) <= datetime.now(UTC):
            self.db.execute("UPDATE invitations SET status = 'EXPIRED' WHERE id = ?", (row["id"],))
            self.db.commit()
            raise PermissionError("invite_code_expired")
        invited_email = normalize_email(row["email"])
        if invited_email and invited_email not in {"*", "any"} and invited_email != email:
            raise PermissionError("invite_code_email_mismatch")
        self.db.execute("UPDATE invitations SET status = 'ACCEPTED' WHERE id = ?", (row["id"],))
        self.db.commit()
        return self._invitation(self.db.execute("SELECT * FROM invitations WHERE id = ?", (row["id"],)).fetchone())

    def _claim_legacy_ownership(self, member_id: str) -> dict[str, int]:
        """首个管理员接管存量 member-001 的数据归属（幂等；member-001 保留但停用）。

        为什么保留 member-001 行：多张表以它为外键（events.actor 等是纯文本，但
        devices/agents 的 owner_member_id 是 FK），物理删除会破坏审计链。
        """

        if member_id == LEGACY_MEMBER_ID:
            return {}
        member = self.db.execute("SELECT id FROM human_members WHERE id = ?", (LEGACY_MEMBER_ID,)).fetchone()
        if not member:
            return {}
        moved: dict[str, int] = {}
        table_columns = [
            ("projects", "created_by"),
            ("artifacts", "created_by"),
            ("events", "actor"),
            ("reviews", "reviewer"),
            ("evidence", "created_by"),
            ("agents", "owner_member_id"),
            ("devices", "owner_member_id"),
            ("device_pairings", "created_by"),
            ("device_project_grants", "granted_by"),
            ("device_token_rotations", "rotated_by"),
            ("document_drafts", "updated_by"),
            ("risks", "owner"),
            ("risks", "resolved_by"),
        ]
        for table, column in table_columns:
            cursor = self.db.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (member_id, LEGACY_MEMBER_ID))
            if cursor.rowcount:
                moved[f"{table}.{column}"] = cursor.rowcount
        # 权限表的主键含 member_id：不能直接 UPDATE（新账号可能已有同键行），
        # 改成"先按新账号插入、再删旧账号的授权"，权限与角色原样保留。
        for table, key_column in (("memberships", "team_id"), ("project_memberships", "project_id")):
            cursor = self.db.execute(
                f"INSERT OR IGNORE INTO {table} (member_id, {key_column}, role, created_at) "
                f"SELECT ?, {key_column}, role, created_at FROM {table} WHERE member_id = ?",
                (member_id, LEGACY_MEMBER_ID),
            )
            inserted = cursor.rowcount or 0
            cursor = self.db.execute(f"DELETE FROM {table} WHERE member_id = ?", (LEGACY_MEMBER_ID,))
            deleted = cursor.rowcount or 0
            if inserted or deleted:
                moved[f"{table}.member_id"] = deleted
        # 存量会话（若有）随账号一起转移，避免切换后被孤儿化
        self.db.execute("UPDATE sessions SET member_id = ? WHERE member_id = ?", (member_id, LEGACY_MEMBER_ID))
        self.db.execute("UPDATE human_members SET status = 'suspended' WHERE id = ?", (LEGACY_MEMBER_ID,))
        self.db.commit()
        return moved

    def admin_member_ids(self) -> list[str]:
        return [row["id"] for row in self.db.execute("SELECT id FROM human_members WHERE is_admin = 1 AND status = 'active'")]

    def require_admin(self, member_id: str) -> None:
        if not self._is_admin(member_id):
            raise PermissionError("admin_required")
        member = self.get_member(member_id)
        if member.status != "active":
            raise PermissionError("account_suspended")

    def create_invitation(self, data: InvitationCreate) -> Invitation:
        organization = self.db.execute("SELECT id FROM organizations WHERE id = ?", (str(data.organization_id),)).fetchone()
        if not organization:
            raise KeyError("organization_not_found")
        if data.team_id:
            team = self.db.execute("SELECT organization_id FROM teams WHERE id = ?", (str(data.team_id),)).fetchone()
            if not team or team["organization_id"] != str(data.organization_id):
                raise ValueError("team_not_in_organization")
        invitation_id = str(uuid4())
        token = secrets.token_urlsafe(32)
        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(seconds=data.expires_in_seconds)
        self.db.execute("INSERT INTO invitations (id, organization_id, team_id, email, role, token, status, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)", (invitation_id, str(data.organization_id), str(data.team_id) if data.team_id else None, data.email, data.role, token, expires_at.isoformat(), created_at.isoformat()))
        self.db.commit()
        return Invitation(id=UUID(invitation_id), organization_id=data.organization_id, team_id=data.team_id, email=data.email, role=data.role, token=token, status="PENDING", expires_at=expires_at, created_at=created_at)

    def list_invitations(self, *, limit: int = 50) -> list[Invitation]:
        rows = self.db.execute("SELECT * FROM invitations ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [self._invitation(row) for row in rows]

    def accept_invitation(self, token: str, display_name: str) -> HumanMember:
        row = self.db.execute("SELECT * FROM invitations WHERE token = ?", (token,)).fetchone()
        if not row:
            raise KeyError("invitation_not_found")
        if row["status"] != "PENDING":
            raise ValueError("invitation_not_pending")
        if parse_time(row["expires_at"]) <= datetime.now(UTC):
            self.db.execute("UPDATE invitations SET status = 'EXPIRED' WHERE id = ?", (row["id"],))
            self.db.commit()
            raise ValueError("invitation_expired")
        member = self.create_member(HumanMemberCreate(organization_id=UUID(row["organization_id"]), team_id=UUID(row["team_id"]) if row["team_id"] else None, email=row["email"], display_name=display_name, role=row["role"]))
        self.db.execute("UPDATE invitations SET status = 'ACCEPTED' WHERE id = ?", (row["id"],))
        self.db.commit()
        return member

    def add_project_member(self, project_id: UUID, member_id: str, role: str = "contributor") -> None:
        if role not in {"owner", "project_lead", "contributor", "reviewer", "observer"}:
            raise ValueError("invalid_project_role")
        project = self.db.execute("SELECT organization_id FROM projects WHERE id = ?", (str(project_id),)).fetchone()
        member = self.db.execute("SELECT organization_id FROM human_members WHERE id = ?", (member_id,)).fetchone()
        if not project or not member:
            raise KeyError("project_or_member_not_found")
        if project["organization_id"] != member["organization_id"]:
            raise PermissionError("member_not_in_project_organization")
        previous = self.db.execute(
            "SELECT role FROM project_memberships WHERE project_id = ? AND member_id = ?", (str(project_id), member_id)
        ).fetchone()
        timestamp = now()
        self.db.execute("INSERT OR REPLACE INTO project_memberships (project_id, member_id, role, created_at) VALUES (?, ?, ?, ?)", (str(project_id), member_id, role, timestamp))
        self.db.commit()
        # 成员变更进事件流：成员管理面板的"审计"与"恢复"都读它
        self.add_event(
            project_id,
            "project.member_role_changed" if previous else "project.member_added",
            member_id,
            {"member_id": member_id, "role": role, "previous_role": previous["role"] if previous else None},
            actor_kind="member",
        )

    def list_team_members(self, team_id: UUID) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """
            SELECT m.member_id, m.role, h.display_name, h.email, h.status
            FROM memberships m JOIN human_members h ON h.id = m.member_id
            WHERE m.team_id = ?
            ORDER BY CASE m.role WHEN 'owner' THEN 0 WHEN 'project_lead' THEN 1 ELSE 2 END, h.display_name
            """,
            (str(team_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_team_member(self, team_id: UUID, member_id: str, role: str = "contributor", *, actor_member_id: str | None = None) -> dict[str, Any]:
        """加入团队 = 获得团队身份 + **自动加入该团队的所有项目**（否则"入队了却什么都看不见"）。"""

        if role not in {"owner", "project_lead", "contributor", "reviewer", "observer"}:
            raise ValueError("invalid_team_role")
        team = self.db.execute("SELECT organization_id FROM teams WHERE id = ?", (str(team_id),)).fetchone()
        if not team:
            raise KeyError("team_not_found")
        member = self.db.execute("SELECT organization_id, status FROM human_members WHERE id = ?", (member_id,)).fetchone()
        if not member:
            raise KeyError("member_not_found")
        if member["organization_id"] != team["organization_id"]:
            raise ValueError("member_not_in_team_organization")
        if member["status"] != "active":
            raise ValueError("member_not_active")
        timestamp = now()
        self.db.execute(
            "INSERT OR REPLACE INTO memberships (member_id, team_id, role, created_at) VALUES (?, ?, ?, ?)",
            (member_id, str(team_id), role, timestamp),
        )
        joined = 0
        for row in self.db.execute("SELECT id FROM projects WHERE team_id = ?", (str(team_id),)).fetchall():
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO project_memberships (project_id, member_id, role, created_at) VALUES (?, ?, ?, ?)",
                (row["id"], member_id, role, timestamp),
            )
            joined += cursor.rowcount or 0
        self.db.commit()
        return {"member_id": member_id, "team_id": str(team_id), "role": role, "joined_projects": joined}

    def remove_team_member(self, team_id: UUID, member_id: str, *, actor_member_id: str | None = None) -> dict[str, Any]:
        """移出团队：连带移出该团队的所有项目（授权撤销复用 remove_project_member）。"""

        row = self.db.execute("SELECT 1 FROM memberships WHERE team_id = ? AND member_id = ?", (str(team_id), member_id)).fetchone()
        if not row:
            raise KeyError("team_member_not_found")
        left_projects = 0
        for project in self.db.execute("SELECT id FROM projects WHERE team_id = ?", (str(team_id),)).fetchall():
            try:
                self.remove_project_member(UUID(project["id"]), member_id)
                left_projects += 1
            except KeyError:
                continue  # 本来就不在项目里
        self.db.execute("DELETE FROM memberships WHERE team_id = ? AND member_id = ?", (str(team_id), member_id))
        self.db.commit()
        return {"member_id": member_id, "team_id": str(team_id), "left_projects": left_projects}

    def member_workload(self, organization_id: UUID) -> list[dict[str, Any]]:
        """成员工作量：谁身上有多少活、手上有几台机器。团队视图与派单决策都用它。"""

        open_states = "('READY', 'CLAIMED', 'RUNNING', 'BLOCKED', 'NEEDS_REVISION', 'WAITING_REVIEW')"
        members = self.db.execute(
            "SELECT id, display_name, email, status, is_admin FROM human_members WHERE organization_id = ? ORDER BY display_name",
            (str(organization_id),),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for member in members:
            member_id = member["id"]
            assigned = self.db.execute(
                f"SELECT COUNT(*) AS c FROM tasks WHERE assignee_member_id = ? AND status IN {open_states}", (member_id,)
            ).fetchone()["c"]
            agents = [row["agent_id"] for row in self.db.execute("SELECT agent_id FROM agents WHERE owner_member_id = ?", (member_id,))]
            running = 0
            completed = 0
            if agents:
                placeholders = ",".join("?" for _ in agents)
                running = self.db.execute(
                    f"SELECT COUNT(*) AS c FROM tasks WHERE assignee IN ({placeholders}) AND status IN ('CLAIMED', 'RUNNING')", agents
                ).fetchone()["c"]
                completed = self.db.execute(
                    f"SELECT COUNT(*) AS c FROM tasks WHERE assignee IN ({placeholders}) AND status IN ('APPROVED', 'FAILED', 'CANCELLED')", agents
                ).fetchone()["c"]
            devices = self.db.execute(
                "SELECT COUNT(*) AS c, SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) AS online FROM devices WHERE owner_member_id = ?",
                (member_id,),
            ).fetchone()
            projects = self.db.execute(
                "SELECT COUNT(*) AS c FROM project_memberships WHERE member_id = ?", (member_id,)
            ).fetchone()["c"]
            teams = self.db.execute(
                "SELECT t.name FROM memberships m JOIN teams t ON t.id = m.team_id WHERE m.member_id = ? ORDER BY t.name", (member_id,)
            ).fetchall()
            items.append(
                {
                    "member_id": member_id,
                    "display_name": member["display_name"],
                    "email": member["email"],
                    "status": member["status"],
                    "is_admin": bool(member["is_admin"]),
                    "assigned_open": assigned,
                    "running": running,
                    "completed": completed,
                    "agents": len(agents),
                    "devices": devices["c"] or 0,
                    "devices_active": devices["online"] or 0,
                    "projects": projects,
                    "teams": [row["name"] for row in teams],
                }
            )
        return items

    def update_project_team(self, project_id: UUID, team_id: UUID | None) -> Project:
        """把已有项目改归到某个团队（或脱离团队）。团队成员不会被自动带入——本操作只改归属。"""

        project = self.get_project(project_id)
        if team_id is None:
            self.db.execute("UPDATE projects SET team_id = ?, updated_at = ? WHERE id = ?", (None, now(), str(project_id)))
        else:
            team = self.db.execute("SELECT organization_id FROM teams WHERE id = ?", (str(team_id),)).fetchone()
            if not team:
                raise KeyError("team_not_found")
            if team["organization_id"] != str(project.organization_id):
                raise ValueError("team_not_in_project_organization")
            self.db.execute("UPDATE projects SET team_id = ?, updated_at = ? WHERE id = ?", (str(team_id), now(), str(project_id)))
        self.db.commit()
        self.add_event(project_id, "project.team_changed", "member", {"team_id": str(team_id) if team_id else None}, actor_kind="member")
        return self.get_project(project_id)

    # ---- 项目工作区（W-1）：设置、聊天流、概览 ----

    TASK_STATUS_LABELS = {
        "READY": "回到待执行",
        "CLAIMED": "已被领取",
        "RUNNING": "执行中",
        "BLOCKED": "受阻",
        "NEEDS_REVISION": "需要修改",
        "WAITING_REVIEW": "等待复核",
        "APPROVED": "已通过",
        "DONE": "已完成",
    }

    def update_project_settings(
        self,
        project_id: UUID,
        *,
        team_id: UUID | None = None,
        set_team: bool = False,
        goal: str | None = None,
        target_member_count: int | None = None,
        task_mode: str | None = None,
    ) -> Project:
        """PATCH 语义的通用更新：只改显式给出的字段。

        team_id 用 `set_team` 区分"改为空（脱离团队）"与"本次不动团队"——沿用既有
        update_project_team 的校验（团队必须在同组织内）。工作区字段（目标/人数/
        推进模式）由队长或管理员改。
        """

        if task_mode is not None and task_mode not in {"manual", "hybrid", "auto"}:
            raise ValueError("unsupported_task_mode")
        project = self.get_project(project_id)
        updates: list[str] = []
        params: list[Any] = []
        if set_team:
            if team_id is not None:
                team = self.db.execute("SELECT organization_id FROM teams WHERE id = ?", (str(team_id),)).fetchone()
                if not team:
                    raise KeyError("team_not_found")
                if team["organization_id"] != str(project.organization_id):
                    raise ValueError("team_not_in_project_organization")
            updates.append("team_id = ?")
            params.append(str(team_id) if team_id else None)
        if goal is not None:
            updates.append("goal = ?")
            params.append(goal or None)
        if target_member_count is not None:
            updates.append("target_member_count = ?")
            params.append(target_member_count)
        if task_mode is not None:
            updates.append("task_mode = ?")
            params.append(task_mode)
        if not updates:
            return project
        updates.append("updated_at = ?")
        params.append(now())
        params.append(str(project_id))
        self.db.execute(f"UPDATE projects SET {', '.join(updates)} WHERE id = ?", params)
        self.db.commit()
        if set_team:
            self.add_event(project_id, "project.team_changed", "member", {"team_id": str(team_id) if team_id else None}, actor_kind="member")
        if task_mode is not None:
            self.add_event(project_id, "project.task_mode_changed", "member", {"task_mode": task_mode}, actor_kind="member")
        return self.get_project(project_id)

    def max_project_message_seq(self, project_id: UUID) -> int:
        row = self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM project_messages WHERE project_id = ?", (str(project_id),)).fetchone()
        return int(row[0] or 0)

    def post_project_message(
        self,
        project_id: UUID,
        member_id: str,
        content: str,
        ref_artifact_id: UUID | None = None,
        ref_task_id: UUID | None = None,
    ) -> ProjectMessage:
        """人类成员在项目聊天流里发一条消息（权限在调用方按 project.chat 校验）。

        `ref_artifact_id` 用于"上传的文件"这类需要跳转的引用——只允许指向本项目的成果物，
        否则消息里会挂上别人项目的对象（越权引用）。
        """

        self.get_project(project_id)
        member = self.get_member(member_id)
        name = member.display_name
        if ref_artifact_id is not None:
            artifact = self.db.execute(
                "SELECT project_id FROM artifacts WHERE id = ?", (str(ref_artifact_id),)
            ).fetchone()
            if not artifact or artifact["project_id"] != str(project_id):
                raise ValueError("message_artifact_not_in_project")
        if ref_task_id is not None:
            referenced = self.db.execute("SELECT project_id FROM tasks WHERE id = ?", (str(ref_task_id),)).fetchone()
            if not referenced or referenced["project_id"] != str(project_id):
                raise ValueError("message_task_not_in_project")
        message_id = str(uuid4())
        seq = self.max_project_message_seq(project_id) + 1
        timestamp = now()
        self.db.execute(
            "INSERT INTO project_messages (id, project_id, seq, sender_kind, sender_member_id, sender_agent_id, sender_name, content, message_type, ref_event_id, ref_artifact_id, ref_task_id, created_at) "
            "VALUES (?, ?, ?, 'human', ?, NULL, ?, ?, 'text', NULL, ?, ?, ?)",
            (
                message_id,
                str(project_id),
                seq,
                member_id,
                name,
                content.strip(),
                str(ref_artifact_id) if ref_artifact_id else None,
                str(ref_task_id) if ref_task_id else None,
                timestamp,
            ),
        )
        self.db.commit()
        return self._project_message(self.db.execute("SELECT * FROM project_messages WHERE id = ?", (message_id,)).fetchone())

    def list_project_messages(
        self,
        project_id: UUID,
        *,
        before_seq: int | None = None,
        after_seq: int | None = None,
        limit: int = 60,
    ) -> list[ProjectMessage]:
        """按 seq 游标读取聊天流。

        - `after_seq`：向前增量（WS 断线补齐 / 广播推进）；
        - `before_seq`：向后翻历史页；
        - 都不给：最近 limit 条（首屏）。
        返回一律按 seq 升序，前端直接 append。
        """

        if after_seq is not None:
            rows = self.db.execute(
                "SELECT * FROM project_messages WHERE project_id = ? AND seq > ? ORDER BY seq ASC LIMIT ?",
                (str(project_id), int(after_seq), limit),
            ).fetchall()
            return [self._project_message(row) for row in rows]
        if before_seq is not None:
            rows = self.db.execute(
                "SELECT * FROM project_messages WHERE project_id = ? AND seq < ? ORDER BY seq DESC LIMIT ?",
                (str(project_id), int(before_seq), limit),
            ).fetchall()
            return [self._project_message(row) for row in reversed(rows)]
        rows = self.db.execute(
            "SELECT * FROM project_messages WHERE project_id = ? ORDER BY seq DESC LIMIT ?",
            (str(project_id), limit),
        ).fetchall()
        return [self._project_message(row) for row in reversed(rows)]

    def _project_message(self, row: sqlite3.Row) -> ProjectMessage:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["project_id"] = UUID(values["project_id"])
        values["ref_artifact_id"] = UUID(values["ref_artifact_id"]) if values.get("ref_artifact_id") else None
        values["ref_task_id"] = UUID(values["ref_task_id"]) if values.get("ref_task_id") else None
        values["created_at"] = parse_time(values["created_at"])
        values["sender_name"] = values.get("sender_name") or ""
        return ProjectMessage(**values)

    def _task_title(self, task_id: Any) -> str | None:
        if not task_id:
            return None
        row = self.db.execute("SELECT title FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        return row["title"] if row else None

    def _run_task_title(self, run_id: Any) -> str | None:
        """run.* 事件的 payload 只有 run_id——任务名要顺着 runs.task_id 找回来。"""

        if not run_id:
            return None
        row = self.db.execute("SELECT task_id FROM runs WHERE id = ?", (str(run_id),)).fetchone()
        return self._task_title(row["task_id"]) if row and row["task_id"] else None

    def _artifact_name(self, artifact_id: Any) -> str | None:
        if not artifact_id:
            return None
        row = self.db.execute("SELECT name FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
        return row["name"] if row else None

    def _display_name(self, member_id: str | None) -> str:
        if not member_id:
            return ""
        row = self.db.execute("SELECT display_name FROM human_members WHERE id = ?", (member_id,)).fetchone()
        return row["display_name"] if row else member_id

    def _agent_display_name(self, agent_id: str | None) -> str:
        if not agent_id:
            return ""
        row = self.db.execute("SELECT display_name FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return row["display_name"] if row else agent_id

    def _chat_card_content(self, event_type: str, payload: dict[str, Any]) -> tuple[str, UUID | None, UUID | None] | None:
        """把事件渲染成一句人话 + 可跳转引用；返回 None 的事件不进聊天流。

        只挑"项目叙事里有意义"的事件——每次心跳、内容入库、seed/sync 之类的
        运维噪声不进聊天，否则群聊会被机器噪声淹没。
        """

        task_id = payload.get("task_id")
        artifact_id = payload.get("artifact_id")
        task_ref = UUID(str(task_id)) if task_id else None
        artifact_ref = UUID(str(artifact_id)) if artifact_id else None
        artifact_name = self._artifact_name(artifact_id)
        if event_type == "task.auto_assigned":
            title = self._task_title(task_id) or "未命名任务"
            target = payload.get("assignee_member_id")
            matched = payload.get("matched_capabilities") or []
            suffix = f"（能力匹配：{'、'.join(str(item) for item in matched)}）" if matched else "（能力匹配）"
            return f"全自动：把任务「{title}」派给了 {self._display_name(target)}{suffix}", task_ref, None
        if event_type == "task.auto_unmatched":
            title = self._task_title(task_id) or "未命名任务"
            required = payload.get("required_capabilities") or []
            return (
                f"全自动：没有在线执行体能跑「{title}」（需要 {'、'.join(str(item) for item in required)}），"
                "已停下等你处理"
            ), task_ref, None
        if event_type == "task.budget_exceeded":
            title = self._task_title(task_id) or "未命名任务"
            overruns = payload.get("overruns") or []
            parts = []
            for item in overruns:
                if item.get("code") == "tokens":
                    parts.append(f"{item.get('reported')} tokens / 上限 {item.get('limit')}")
                elif item.get("code") == "seconds":
                    parts.append(f"{item.get('reported')} 秒 / 上限 {item.get('limit')}")
            detail = "、".join(parts) or "超限"
            return f"任务「{title}」这次执行超出预算（{detail}），批准时会被门禁拦下", task_ref, None
        if event_type == "task.budget_exhausted":
            title = self._task_title(task_id) or "未命名任务"
            attempts = payload.get("attempts")
            limit = payload.get("max_attempts")
            return (
                f"任务「{title}」的预算用尽（已领取 {attempts}/{limit} 次），不再接受新的执行；"
                "要重试请调整预算或放宽次数"
            ), task_ref, None
        if event_type == "task.dispatched":
            title = self._task_title(task_id) or "未命名任务"
            target = payload.get("assignee_member_id")
            action = str(payload.get("action") or "dispatch")
            if action == "release":
                return f"任务「{title}」回到未指派（谁先轮到谁跑）", task_ref, None
            if action == "self_claim":
                return f"{self._display_name(target)} 认领了任务「{title}」", task_ref, None
            if target:
                return f"队长把任务「{title}」派给了 {self._display_name(target)}", task_ref, None
            return f"任务「{title}」的负责人已更新", task_ref, None
        if event_type == "task.created":
            return f"创建了任务「{payload.get('title') or self._task_title(task_id) or '未命名'}」", task_ref, None
        if event_type == "task.claimed":
            title = self._task_title(task_id) or "未命名任务"
            return f"领取了任务「{title}」", task_ref, None
        if event_type == "task.progress":
            title = self._task_title(task_id) or "未命名任务"
            label = self.TASK_STATUS_LABELS.get(str(payload.get("status")), str(payload.get("status") or "更新"))
            message = payload.get("message")
            suffix = f"：{message}" if message else ""
            return f"任务「{title}」{label}{suffix}", task_ref, None
        if event_type == "task.result_submitted":
            title = self._task_title(task_id) or "未命名任务"
            label = self.TASK_STATUS_LABELS.get(str(payload.get("status")), str(payload.get("status") or ""))
            return f"提交了任务「{title}」的成果（{label}）", task_ref, artifact_ref
        if event_type == "task.lease.recycled":
            title = self._task_title(task_id) or "未命名任务"
            return f"「{title}」的执行租约超时，任务回到待领取", task_ref, None
        if event_type == "run.created":
            title = self._task_title(task_id) or self._run_task_title(payload.get("run_id")) or "未命名任务"
            return f"开始执行「{title}」", task_ref, None
        if event_type in {"run.completed", "run.failed", "run.blocked"}:
            title = self._task_title(task_id) or self._run_task_title(payload.get("run_id")) or "未命名任务"
            summary = payload.get("summary") or ""
            if event_type == "run.completed":
                return f"执行完成「{title}」{('：' + summary) if summary else ''}", task_ref, None
            if event_type == "run.failed":
                return f"执行失败「{title}」{('：' + summary) if summary else ''}", task_ref, None
            return f"「{title}」的执行被信息边界拦截", task_ref, None
        if event_type == "artifact.submitted":
            return f"提交成果物「{artifact_name or '成果物'}」，等待复核", None, artifact_ref
        if event_type == "artifact.imported":
            return f"导入了成果物「{payload.get('name') or artifact_name or '成果物'}」", None, artifact_ref
        if event_type == "review.created":
            target_type = str(payload.get("target_type") or "")
            target_id = payload.get("target_id")
            if target_type == "artifact":
                target = self._artifact_name(target_id) or "成果物"
                ref = None
                artifact_ref = UUID(str(target_id)) if target_id else None
            else:
                target = self._task_title(target_id) or "任务"
                ref = UUID(str(target_id)) if target_id else None
            verdict = "通过" if str(payload.get("verdict")) == "APPROVED" else "需修改"
            return f"复核「{target}」：{verdict}", ref, artifact_ref
        if event_type == "handoff.created":
            return f"发出交接单（{payload.get('handoff_type') or '标准'}）", None, None
        if event_type == "evidence.created":
            return f"登记了证据（{payload.get('evidence_type') or '通用'}）", None, None
        if event_type == "gate.invalidated":
            return "上游变更导致门禁失效，需要重新复核", task_ref, None
        if event_type == "risk.updated":
            return "风险登记更新", None, None
        if event_type == "project.created":
            return f"项目已创建：{payload.get('name') or ''}", None, None
        if event_type == "project.member_added":
            return f"{self._display_name(payload.get('member_id'))} 加入项目（{payload.get('role') or 'contributor'}）", None, None
        if event_type == "project.member_role_changed":
            return f"{self._display_name(payload.get('member_id'))} 的项目角色变更为 {payload.get('role') or ''}", None, None
        if event_type == "project.member_removed":
            return f"{self._display_name(payload.get('member_id'))} 被移出项目", None, None
        if event_type == "project.team_changed":
            return "项目归属团队已变更", None, None
        if event_type == "project.task_mode_changed":
            labels = {"manual": "队长派单", "hybrid": "派单 + 成员认领", "auto": "模板全自动"}
            return f"任务推进模式已切换为「{labels.get(str(payload.get('task_mode')), payload.get('task_mode') or '')}」", None, None
        return None

    def _bridge_chat_event(self, event: Event) -> ProjectMessage | None:
        """把一条事件写成聊天卡片（事务内，不 commit——由调用方提交）。

        这是"Agent 实时输出进群聊"的唯一实现：不复制业务内容，只做一次渲染 +
        保存引用；Agent 协议因此完全不用改。
        """

        card = self._chat_card_content(event.event_type, event.payload or {})
        if card is None:
            # 非卡事件也要推进水位线：否则定时兜底会把它们当"没检查过"再扫一遍
            self._advance_chat_watermark(event.project_id, event.sequence)
            return None
        content, task_ref, artifact_ref = card
        sender_kind = "agent" if str(event.actor_kind) == "agent" else "system"
        message_id = str(uuid4())
        seq = self.max_project_message_seq(event.project_id) + 1
        timestamp = event.created_at.isoformat() if hasattr(event.created_at, "isoformat") else now()
        self.db.execute(
            "INSERT INTO project_messages (id, project_id, seq, sender_kind, sender_member_id, sender_agent_id, sender_name, content, message_type, ref_event_id, ref_artifact_id, ref_task_id, created_at) "
            "VALUES (?, ?, ?, ?, NULL, ?, ?, ?, 'card', ?, ?, ?, ?)",
            (
                message_id,
                str(event.project_id),
                seq,
                sender_kind,
                event.actor if sender_kind == "agent" else None,
                self._agent_display_name(event.actor) if sender_kind == "agent" else "",
                content,
                event.sequence,
                str(artifact_ref) if artifact_ref else None,
                str(task_ref) if task_ref else None,
                timestamp,
            ),
        )
        self._advance_chat_watermark(event.project_id, event.sequence)
        return self._project_message(self.db.execute("SELECT * FROM project_messages WHERE id = ?", (message_id,)).fetchone())

    def _advance_chat_watermark(self, project_id: UUID, sequence: int) -> None:
        """只前进不后退地记录"已检查到的事件序号"（同一事务内，由调用方提交）。"""

        self.db.execute(
            "INSERT INTO project_chat_watermarks (project_id, last_event_sequence, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(project_id) DO UPDATE SET "
            "last_event_sequence = MAX(project_chat_watermarks.last_event_sequence, excluded.last_event_sequence), "
            "updated_at = excluded.updated_at",
            (str(project_id), int(sequence), now()),
        )

    def _chat_watermark(self, project_id: UUID) -> int:
        row = self.db.execute("SELECT last_event_sequence FROM project_chat_watermarks WHERE project_id = ?", (str(project_id),)).fetchone()
        return int(row["last_event_sequence"]) if row else 0

    def catch_up_project_messages(self, project_id: UUID | None = None, limit: int = 500) -> list[ProjectMessage]:
        """把"还没进聊天流"的事件补成卡片，推进水位线。

        三个用途：启动时补历史（老项目的过往事件一次性进流）、定时兜底（任何漏了
        即时桥接的路径都在这里补齐）、以及作为重活时的幂等重放。
        水位线按"已检查到的事件序号"推进，因此非卡事件也不会导致漏扫。
        """

        project_ids = (
            [str(project_id)]
            if project_id is not None
            else [row["id"] for row in self.db.execute("SELECT id FROM projects").fetchall()]
        )
        created: list[ProjectMessage] = []
        for pid in project_ids:
            watermark = self._chat_watermark(UUID(pid))
            rows = self.db.execute(
                "SELECT * FROM events WHERE project_id = ? AND sequence > ? ORDER BY sequence ASC LIMIT ?",
                (pid, watermark, limit),
            ).fetchall()
            if not rows:
                continue
            for row in rows:
                event = Event(
                    id=UUID(row["id"]),
                    project_id=UUID(row["project_id"]),
                    sequence=row["sequence"],
                    event_type=row["event_type"],
                    actor=row["actor"],
                    payload=self._json(row["payload"]),
                    created_at=parse_time(row["created_at"]),
                    actor_kind=row["actor_kind"],
                    object_type=row["object_type"],
                    object_id=UUID(row["object_id"]) if row["object_id"] else None,
                    idempotency_key=row["idempotency_key"],
                    schema_version=row["schema_version"],
                )
                message = self._bridge_chat_event(event)
                if message:
                    created.append(message)
            self.db.execute(
                "INSERT OR REPLACE INTO project_chat_watermarks (project_id, last_event_sequence, updated_at) VALUES (?, ?, ?)",
                (pid, rows[-1]["sequence"], now()),
            )
            self.db.commit()
        return created

    # 成果空间里的"文档"：文本类成果物（草稿/提交/批准三层版本那套）。
    # 其余类型（figure/result_table/run_manifest…）仍然算成果物，只是不在"文档"面板里重复出现。
    DOCUMENT_ARTIFACT_TYPES = {
        "problem_source",
        "problem_facts",
        "problem_analysis",
        "data_profile",
        "model_spec",
        "code",
        "experiment_plan",
        "review_report",
        "audit_report",
        "paper_source",
    }

    def project_deliverables(self, project_id: UUID) -> dict[str, Any]:
        """成果空间聚合：成果物 / 交接 / 文档 / 门禁与复核 / 风险。

        读时聚合，不落新表；四个面板都只给"统计 + 最近若干条 + 可跳转的 id"。
        """

        self.get_project(project_id)

        artifact_status: dict[str, int] = {}
        documents: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        for item in self.list_artifacts(project_id):
            status = str(item.status)
            artifact_status[status] = artifact_status.get(status, 0) + 1
            brief = {
                "id": item.id,
                "name": item.name,
                "artifact_type": str(item.artifact_type),
                "version": item.version,
                "status": status,
                "downstream_allowed": bool(item.downstream_allowed),
                "task_id": item.task_id,
                "run_id": item.run_id,
                "created_by": item.created_by,
                "created_by_kind": str(item.created_by_kind),
                "created_at": item.created_at,
            }
            artifacts.append(brief)
            if str(item.artifact_type) in self.DOCUMENT_ARTIFACT_TYPES:
                draft = self.get_document_draft(project_id, item.id)
                if status == "APPROVED":
                    layer = "approved"
                elif status in {"SUBMITTED", "PENDING_REVIEW"}:
                    layer = "submitted"
                elif draft is not None:
                    layer = "draft"
                else:
                    layer = "draft"
                documents.append({**brief, "layer": layer, "has_draft": draft is not None})

        artifacts.sort(key=lambda row: (row["created_at"], str(row["id"])), reverse=True)
        documents.sort(key=lambda row: (row["created_at"], str(row["id"])), reverse=True)

        handoff_status: dict[str, int] = {}
        handoffs: list[dict[str, Any]] = []
        for item in self.list_handoffs(project_id):
            status = str(item.status)
            handoff_status[status] = handoff_status.get(status, 0) + 1
            handoffs.append(
                {
                    "id": item.id,
                    "task_id": item.task_id,
                    "sender_agent_id": item.sender_agent_id,
                    "status": status,
                    "objective": item.objective,
                    "key_conclusions": item.key_conclusions[:3],
                    "open_questions": item.open_questions[:3],
                    "created_at": item.created_at,
                }
            )
        handoffs.sort(key=lambda row: (row["created_at"], str(row["id"])), reverse=True)

        gates = []
        for gate in self.list_gates(project_id):
            gates.append(
                {
                    "id": gate.id,
                    "target_type": str(gate.target_type),
                    "target_id": str(gate.target_id),
                    "status": str(gate.status),
                    "blocking_count": len(gate.blocking_findings or []),
                    "approved_by": gate.approved_by,
                    "approved_at": gate.approved_at,
                }
            )

        reviews = [
            {
                "id": item.id,
                "target_type": str(item.target_type),
                "target_id": str(item.target_id),
                "verdict": str(item.verdict),
                # 老数据的 reviewer 存的是成员 id：界面显示名字更好读（未知就原样返回）
                "reviewer": self._display_name(item.reviewer) if item.reviewer else "",
                "reviewer_kind": str(item.reviewer_kind),
                "summary": item.summary,
                "created_at": item.created_at,
            }
            for item in self.list_reviews(project_id)
        ]
        reviews.sort(key=lambda row: row["created_at"], reverse=True)

        risks = self.list_risks(project_id)
        layer_counts: dict[str, int] = {"draft": 0, "submitted": 0, "approved": 0}
        for document in documents:
            layer_counts[document["layer"]] = layer_counts.get(document["layer"], 0) + 1

        return {
            "artifacts": {"total": len(artifacts), "by_status": artifact_status, "recent": artifacts[:25]},
            "handoffs": {"total": len(handoffs), "by_status": handoff_status, "recent": handoffs[:12]},
            "documents": {"total": len(documents), "by_layer": layer_counts, "recent": documents[:15]},
            "gates": {
                "total": len(gates),
                "open": sum(1 for gate in gates if gate["status"] != "PASSED"),
                "items": gates[:12],
            },
            "reviews": {"total": len(reviews), "recent": reviews[:8]},
            "risks": {"total": len(risks), "open": sum(1 for risk in risks if not risk.resolved)},
        }

    def project_workspace(self, project_id: UUID, member_id: str | None = None) -> dict[str, Any]:
        """工作区首屏聚合：项目、成员、Agent、任务/成果摘要、最近聊天。

        一次请求拿全，避免首屏打五六个接口；成员负载与 Agent 在线状态都是读时聚合。
        """

        project = self.get_project(project_id)
        timestamp = now()

        members: list[dict[str, Any]] = []
        for item in self.list_project_members(project_id):
            # 注意：这里**不能**复用参数名 member_id——它会在循环后被覆盖，
            # 让 viewer 的"我是谁"变成成员列表里的最后一个人（曾经真的踩过）。
            entry_member_id = item["member_id"]
            counts = self.db.execute(
                "SELECT "
                "SUM(CASE WHEN status IN ('READY','CLAIMED','RUNNING','BLOCKED','NEEDS_REVISION','WAITING_REVIEW') AND assignee_member_id = ? THEN 1 ELSE 0 END) AS open_tasks, "
                "SUM(CASE WHEN status = 'RUNNING' AND assignee_member_id = ? THEN 1 ELSE 0 END) AS running_tasks "
                "FROM tasks WHERE project_id = ?",
                (entry_member_id, entry_member_id, str(project_id)),
            ).fetchone()
            agent_rows = self.db.execute(
                "SELECT a.agent_id, a.status, a.last_seen, "
                "(SELECT COUNT(*) FROM agent_connections c WHERE c.agent_id = a.agent_id AND c.status = 'CONNECTED') AS connected "
                "FROM agents a WHERE a.owner_member_id = ?",
                (entry_member_id,),
            ).fetchall()
            last_activity = max((row["last_seen"] for row in agent_rows if row["last_seen"]), default=None)
            members.append(
                {
                    **item,
                    "open_tasks": int(counts["open_tasks"] or 0),
                    "running_tasks": int(counts["running_tasks"] or 0),
                    "agents": len(agent_rows),
                    "agents_online": sum(1 for row in agent_rows if row["connected"] or row["status"] == "online"),
                    "last_activity": parse_time(last_activity) if last_activity else None,
                }
            )

        agents: list[dict[str, Any]] = []
        agent_rows = self.db.execute(
            "SELECT a.agent_id, a.display_name, a.owner_member_id, h.display_name AS owner_name, a.status, a.last_seen, "
            "(SELECT COUNT(*) FROM agent_connections c WHERE c.agent_id = a.agent_id AND c.status = 'CONNECTED') AS connected "
            "FROM agents a LEFT JOIN human_members h ON h.id = a.owner_member_id "
            "WHERE a.agent_id IN (SELECT agent_id FROM agent_project_grants WHERE project_id = ?) "
            "ORDER BY a.last_seen DESC",
            (str(project_id),),
        ).fetchall()
        for row in agent_rows:
            current = self.db.execute(
                "SELECT id, title FROM tasks WHERE project_id = ? AND assignee = ? AND status IN ('CLAIMED','RUNNING') ORDER BY updated_at DESC LIMIT 1",
                (str(project_id), row["agent_id"]),
            ).fetchone()
            agents.append(
                {
                    "agent_id": row["agent_id"],
                    "display_name": row["display_name"],
                    "owner_member_id": row["owner_member_id"],
                    "owner_name": row["owner_name"] or row["owner_member_id"],
                    "status": row["status"],
                    "connected": bool(row["connected"]) or row["status"] == "online",
                    "last_seen": parse_time(row["last_seen"]) if row["last_seen"] else None,
                    "current_task_id": UUID(current["id"]) if current else None,
                    "current_task_title": current["title"] if current else None,
                }
            )

        status_rows = self.db.execute(
            "SELECT status, COUNT(*) AS c FROM tasks WHERE project_id = ? GROUP BY status", (str(project_id),)
        ).fetchall()
        open_states = ("READY", "CLAIMED", "RUNNING", "BLOCKED", "NEEDS_REVISION", "WAITING_REVIEW")
        task_rows = self.db.execute(
            "SELECT t.id, t.title, t.status, t.stage, t.priority, t.assignee_member_id, t.deadline, h.display_name AS assignee_name "
            "FROM tasks t LEFT JOIN human_members h ON h.id = t.assignee_member_id "
            "WHERE t.project_id = ? AND t.status IN (?, ?, ?, ?, ?, ?) "
            "ORDER BY (t.deadline IS NULL), t.deadline ASC, t.updated_at DESC LIMIT 30",
            (str(project_id), *open_states),
        ).fetchall()

        artifact_rows = self.db.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'APPROVED' THEN 1 ELSE 0 END) AS approved, "
            "SUM(CASE WHEN status = 'PENDING_REVIEW' THEN 1 ELSE 0 END) AS pending "
            "FROM artifacts WHERE project_id = ?",
            (str(project_id),),
        ).fetchone()
        recent_artifacts = self.db.execute(
            "SELECT id, name, status, version, artifact_type, created_at FROM artifacts WHERE project_id = ? ORDER BY created_at DESC LIMIT 8",
            (str(project_id),),
        ).fetchall()

        role = self._member_project_role(project_id, member_id) if member_id else None
        viewer = {
            "member_id": member_id,
            "role": role,
            "can_chat": role in {"owner", "project_lead", "contributor", "reviewer"},
            "can_manage": role in {"owner", "project_lead"},
        }

        return {
            "project": project,
            "viewer": viewer,
            "members": members,
            "agents": agents,
            "tasks": {
                "total": sum(int(row["c"]) for row in status_rows),
                "by_status": {row["status"]: int(row["c"]) for row in status_rows},
                "open_items": [
                    {
                        "id": UUID(row["id"]),
                        "title": row["title"],
                        "status": row["status"],
                        "stage": row["stage"],
                        "priority": row["priority"],
                        "assignee_member_id": row["assignee_member_id"],
                        "assignee_name": row["assignee_name"],
                        "deadline": parse_time(row["deadline"]) if row["deadline"] else None,
                    }
                    for row in task_rows
                ],
            },
            "artifacts": {
                "total": int(artifact_rows["total"] or 0),
                "approved": int(artifact_rows["approved"] or 0),
                "pending_review": int(artifact_rows["pending"] or 0),
                "recent": [
                    {
                        "id": UUID(row["id"]),
                        "name": row["name"],
                        "status": row["status"],
                        "version": row["version"],
                        "kind": row["artifact_type"] or "deliverable",
                        "created_at": parse_time(row["created_at"]),
                    }
                    for row in recent_artifacts
                ],
            },
            "messages": [message.model_dump() for message in self.list_project_messages(project_id, limit=50)],
            "generated_at": timestamp,
        }

    def capability_catalog(self, organization_id: UUID) -> dict[str, Any]:
        """能力目录：谁（哪台机器）能跑什么、哪条任务没人能跑、以及"差一点"的候选。

        AIP-1 之后这个视图分三层（见 docs/AIP_1_PLAN.md）：
          * `agents`：每个执行体的**技能卡**（技能名 + 版本 + 输入/输出）与**授权范围**分栏，
            另带包/实例两段身份（`package_id`/`instance_id`，推断值可识别）；
          * `packages`：同一执行体程序包的多实例聚合（"我装了三台 codex"不再靠字符串猜）；
          * `unmet_tasks`：声明了技能要求却没人满足的任务，改成**按归一化+版本**判定，
            并给出"这几台差哪一项"（候选列表，而不是布尔过滤）。
        """

        agents = self.db.execute(
            "SELECT a.agent_id, a.display_name, a.owner_member_id, h.display_name AS owner_name, a.status AS agent_status, "
            "a.last_seen, a.package_id, a.instance_id, a.package_source "
            "FROM agents a LEFT JOIN human_members h ON h.id = a.owner_member_id "
            "LEFT JOIN projects p ON 1 = 1 WHERE h.organization_id = ? GROUP BY a.agent_id",
            (str(organization_id),),
        ).fetchall()
        device_rows = self.db.execute(
            "SELECT device_id, agent_id, status, capabilities, last_seen FROM devices WHERE organization_id = ?", (str(organization_id),)
        ).fetchall()
        devices_by_agent: dict[str, list[dict[str, Any]]] = {}
        for row in device_rows:
            devices_by_agent.setdefault(row["agent_id"], []).append(dict(row))

        reliability = self._agent_reliability()

        entries: list[dict[str, Any]] = []
        for agent in agents:
            agent_id = agent["agent_id"]
            devices = devices_by_agent.get(agent_id, [])
            cards = self._agent_cards(agent_id)
            skill_versions = self._agent_skill_versions(agent_id)
            stats = reliability.get(agent_id, {"succeeded": 0, "failed": 0, "total": 0, "success_rate": None})
            entries.append(
                {
                    "agent_id": agent_id,
                    "display_name": agent["display_name"],
                    "owner_member_id": agent["owner_member_id"],
                    "owner_name": agent["owner_name"] or agent["owner_member_id"],
                    "agent_status": agent["agent_status"],
                    "last_seen": agent["last_seen"],
                    "capabilities": sorted(skill_versions),
                    "skill_versions": skill_versions,
                    "cards": cards,
                    "scope_capabilities": sorted(self._agent_scope_set(agent_id)),
                    "package_id": agent["package_id"],
                    "instance_id": agent["instance_id"] or agent_id,
                    "package_source": agent["package_source"],
                    "runs_total": stats["total"],
                    "success_rate": stats["success_rate"],
                    "devices": [
                        {"device_id": item["device_id"], "status": item["status"], "last_seen": item["last_seen"]} for item in devices
                    ],
                    "devices_online": sum(1 for item in devices if item["status"] == "active"),
                }
            )

        # 没人能跑的任务：项目里 READY/NEEDS_REVISION 且 required_capabilities 不被任何执行体满足
        # 判定用全体执行体的"技能 → 版本"合并表（归一化 + 版本下限，AIP-1a）
        known: dict[str, str] = {}
        for entry in entries:
            known = skill_match.merge_version_maps(known, entry["skill_versions"])
        unmet: list[dict[str, Any]] = []
        rows = self.db.execute(
            "SELECT t.id, t.title, t.project_id, t.required_capabilities, p.name AS project_name FROM tasks t "
            "JOIN projects p ON p.id = t.project_id "
            "WHERE p.organization_id = ? AND t.status IN ('READY', 'NEEDS_REVISION') AND t.required_capabilities NOT IN ('[]', '')",
            (str(organization_id),),
        ).fetchall()
        for row in rows:
            try:
                required = json.loads(row["required_capabilities"] or "[]")
            except (TypeError, ValueError):
                required = []
            missing = skill_match.unsatisfied_requirements(required, known)
            if missing:
                unmet.append(
                    {
                        "task_id": row["id"],
                        "title": row["title"],
                        "project_id": row["project_id"],
                        "project_name": row["project_name"],
                        "required_capabilities": [str(item) for item in required],
                        "missing_capabilities": missing,
                        "candidates": self._candidate_preview(UUID(row["project_id"]), row["id"], required, reliability=reliability),
                    }
                )
        return {"agents": entries, "packages": self._package_rollup(entries), "unmet_tasks": unmet}

    @staticmethod
    def _package_rollup(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """执行体程序包聚合：一个包几台实例、在线几台、包级成功率。"""

        buckets: dict[str, dict[str, Any]] = {}
        for entry in entries:
            # 包身份未知的执行体不进聚合：宁可不显示，也不要把互不相干的执行体归成"同一个包"
            package_id = entry.get("package_id")
            if not package_id:
                continue
            bucket = buckets.setdefault(
                package_id,
                {
                    "package_id": package_id,
                    "source": entry.get("package_source") or "inferred",
                    "instances": 0,
                    "instances_online": 0,
                    "skills": set(),
                    "succeeded": 0,
                    "failed": 0,
                    "total": 0,
                    "members": set(),
                },
            )
            bucket["instances"] += 1
            if entry.get("agent_status") == "online":
                bucket["instances_online"] += 1
            bucket["skills"].update(entry.get("capabilities") or [])
            bucket["members"].add(str(entry.get("owner_name") or entry.get("owner_member_id") or ""))
            if entry.get("package_source") == "reported":
                bucket["source"] = "reported"
            rate = entry.get("success_rate")
            total = entry.get("runs_total") or 0
            if rate is not None and total:
                succeeded = round(rate * total)
                bucket["succeeded"] += succeeded
                bucket["failed"] += total - succeeded
                bucket["total"] += total
        rollup: list[dict[str, Any]] = []
        for bucket in buckets.values():
            total = bucket["total"]
            rollup.append(
                {
                    "package_id": bucket["package_id"],
                    "source": bucket["source"],
                    "instances": bucket["instances"],
                    "instances_online": bucket["instances_online"],
                    "skills": sorted(bucket["skills"]),
                    "succeeded": bucket["succeeded"],
                    "failed": bucket["failed"],
                    "total": total,
                    "success_rate": round((bucket["succeeded"] + 2) / (total + 4), 3) if total else None,
                    "members": sorted(item for item in bucket["members"] if item),
                }
            )
        return sorted(rollup, key=lambda item: (-item["instances"], item["package_id"]))

    def _agent_reliability(self) -> dict[str, dict[str, Any]]:
        """按执行体聚合历史执行结果（一次分组查询，不逐台查）。

        平滑成功率 = (成功 + 2) / (总数 + 4)（Beta 先验）：没有执行记录时是 0.5 的中性值，
        不会因为"跑过一次成功"就排到有几十次经验的前面。样本量（total）由界面显示。
        """

        rows = self.db.execute(
            "SELECT agent_id, status, COUNT(*) AS c FROM runs WHERE agent_id IS NOT NULL GROUP BY agent_id, status"
        ).fetchall()
        buckets: dict[str, dict[str, Any]] = {}
        for row in rows:
            entry = buckets.setdefault(row["agent_id"], {"succeeded": 0, "failed": 0, "total": 0, "success_rate": None})
            count = int(row["c"] or 0)
            entry["total"] += count
            if row["status"] == "SUCCEEDED":
                entry["succeeded"] += count
            elif row["status"] in {"FAILED", "BLOCKED"}:
                entry["failed"] += count
        for entry in buckets.values():
            entry["success_rate"] = round((entry["succeeded"] + 2) / (entry["total"] + 4), 3)
        return buckets

    def rank_task_candidates(
        self,
        project_id: UUID,
        task_row: sqlite3.Row | None,
        *,
        limit: int = 20,
        reliability: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """给一条任务排出候选执行体（AIP-1b）：能在哪几台跑、按什么理由排序。

        单实现、三处共用：任务详情展示、`/team` 的"这几台能跑"补位建议、auto 调度器的选人。
        排序键完全确定（同输入同输出）：在线 → 平滑成功率 → 该成员在手任务数 → member_id → agent_id。
        离线机器也会列出（标注出来），因为"开机就能接"往往比"重新找机器"快；
        调度器只取在线候选。
        """

        required = self._json(task_row["required_capabilities"] or "[]") if task_row is not None else []
        reliability = reliability if reliability is not None else self._agent_reliability()
        roles = {item["member_id"]: item["role"] for item in self.list_project_members(project_id)}
        grants = self.db.execute(
            "SELECT g.agent_id, a.owner_member_id, a.display_name, a.status FROM agent_project_grants g "
            "JOIN agents a ON a.agent_id = g.agent_id WHERE g.project_id = ?",
            (str(project_id),),
        ).fetchall()

        satisfied: list[dict[str, Any]] = []
        partial: list[dict[str, Any]] = []
        for grant in grants:
            member_id = grant["owner_member_id"]
            if roles.get(member_id) not in {"owner", "project_lead", "contributor"}:
                continue
            versions = self._agent_skill_versions(grant["agent_id"])
            missing = skill_match.unsatisfied_requirements(required, versions)
            stats = reliability.get(grant["agent_id"], {"succeeded": 0, "failed": 0, "total": 0, "success_rate": 0.5})
            online = grant["status"] == "online"
            rate = stats["success_rate"] if stats["total"] else 0.5
            load = self._open_task_count(project_id, member_id)
            item = {
                "agent_id": grant["agent_id"],
                "display_name": grant["display_name"],
                "member_id": member_id,
                "online": online,
                "load": load,
                "runs_total": stats["total"],
                "succeeded": stats["succeeded"],
                "failed": stats["failed"],
                "success_rate": rate,
                "matched_skills": [
                    str(value)
                    for value in required
                    if str(value) not in missing
                ],
                "missing_skills": missing,
                "reason": (
                    f"{'全满足' if not missing else '缺 ' + '、'.join(missing)}"
                    f" · 成功率 {rate:.2f}（{stats['total']} 次）"
                    f" · 在手 {load} 件"
                    f"{'' if online else ' · 离线，开机即可接'}"
                ),
            }
            (partial if missing else satisfied).append(item)

        def sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
            return (0 if item["online"] else 1, -item["success_rate"], item["load"], item["member_id"], item["agent_id"])

        satisfied.sort(key=sort_key)
        partial.sort(key=sort_key)
        return {
            "required_capabilities": [str(item) for item in required],
            "satisfied": satisfied[:limit],
            "partial": partial[:limit],
            "satisfied_total": len(satisfied),
            "partial_total": len(partial),
        }

    def _candidate_preview(
        self, project_id: UUID, task_id: str, required: list[Any], *, reliability: dict[str, dict[str, Any]] | None = None
    ) -> list[dict[str, Any]]:
        """`/team` 目录里给"没人能跑"的任务附最多 5 条候选（谁差哪一项、谁差得最少）。"""

        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if row is None:
            return []
        ranked = self.rank_task_candidates(project_id, row, limit=5, reliability=reliability)
        merged = ranked["satisfied"] + ranked["partial"]
        return merged[:5]

    def throughput(self, organization_id: UUID, *, days: int = 14) -> dict[str, Any]:
        """近 N 天吞吐：按天的执行数（成功/失败）与按成员的完成数。

        数据来源是 runs（started_at/completed_at + 019 之后落库的 member_id），
        比"当前待办计数"更能回答"这两周团队实际推了多少活"。
        """

        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        rows = self.db.execute(
            "SELECT r.completed_at, r.status, r.member_id, r.agent_id FROM runs r JOIN projects p ON p.id = r.project_id "
            "WHERE p.organization_id = ? AND r.started_at >= ?",
            (str(organization_id), since),
        ).fetchall()
        day_buckets: dict[str, dict[str, int]] = {}
        member_counts: dict[str, dict[str, Any]] = {}
        for row in rows:
            stamp = row["completed_at"] or ""
            day = stamp[:10] if stamp else ""
            if not day:
                continue
            bucket = day_buckets.setdefault(day, {"succeeded": 0, "failed": 0, "total": 0})
            bucket["total"] += 1
            if row["status"] == "SUCCEEDED":
                bucket["succeeded"] += 1
            elif row["status"] in {"FAILED", "BLOCKED"}:
                bucket["failed"] += 1
            member_id = row["member_id"] or ""
            if member_id:
                entry = member_counts.setdefault(member_id, {"member_id": member_id, "display_name": member_id, "succeeded": 0, "failed": 0, "total": 0})
                entry["total"] += 1
                if row["status"] == "SUCCEEDED":
                    entry["succeeded"] += 1
                elif row["status"] in {"FAILED", "BLOCKED"}:
                    entry["failed"] += 1
        for member_id, entry in member_counts.items():
            member = self.db.execute("SELECT display_name FROM human_members WHERE id = ?", (member_id,)).fetchone()
            if member:
                entry["display_name"] = member["display_name"]
        return {
            "days": days,
            "daily": [{"day": day, **day_buckets[day]} for day in sorted(day_buckets)],
            "members": sorted(member_counts.values(), key=lambda item: item["total"], reverse=True),
        }

    def remove_project_member(self, project_id: UUID, member_id: str) -> dict[str, Any]:
        """把成员移出项目：连带撤销他在这个项目上的设备/Agent 授权，并释放派给他的任务。

        为什么必须撤销授权：设备/Agent 的项目授权是独立于成员关系的（dvc_/prj_ 两套令牌），
        只删 project_memberships 的话，他的机器照样能领这个项目的任务——那就是个洞。
        """

        project = self.get_project(project_id)
        membership = self.db.execute(
            "SELECT role FROM project_memberships WHERE project_id = ? AND member_id = ?",
            (str(project_id), member_id),
        ).fetchone()
        if not membership:
            raise KeyError("project_member_not_found")
        if membership["role"] in {"owner", "project_lead"}:
            leads = self.db.execute(
                "SELECT COUNT(*) AS c FROM project_memberships WHERE project_id = ? AND member_id != ? AND role IN ('owner', 'project_lead')",
                (str(project_id), member_id),
            ).fetchone()["c"]
            if not leads:
                raise ValueError("last_project_lead_cannot_be_removed")

        # 释放派给该成员、还没结束的任务（回到"谁先轮到谁跑"）
        open_states = "('READY', 'CLAIMED', 'RUNNING', 'BLOCKED', 'NEEDS_REVISION', 'WAITING_REVIEW')"
        released = self.db.execute(
            f"UPDATE tasks SET assignee_member_id = NULL WHERE project_id = ? AND assignee_member_id = ? AND status IN {open_states}",
            (str(project_id), member_id),
        ).rowcount or 0

        # 撤销该成员名下设备与 Agent 在这个项目上的授权
        devices = [row["device_id"] for row in self.db.execute("SELECT device_id FROM devices WHERE owner_member_id = ?", (member_id,))]
        agents = [row["agent_id"] for row in self.db.execute("SELECT agent_id FROM agents WHERE owner_member_id = ?", (member_id,))]
        revoked_grants = 0
        for device_id in devices:
            cursor = self.db.execute(
                "UPDATE device_project_grants SET revoked_at = ? WHERE device_id = ? AND project_id = ? AND revoked_at IS NULL",
                (now(), device_id, str(project_id)),
            )
            revoked_grants += cursor.rowcount or 0
        for agent_id in agents:
            cursor = self.db.execute("DELETE FROM agent_project_grants WHERE agent_id = ? AND project_id = ?", (agent_id, str(project_id)))
            revoked_grants += cursor.rowcount or 0

        self.db.execute("DELETE FROM project_memberships WHERE project_id = ? AND member_id = ?", (str(project_id), member_id))
        self.db.commit()
        self.add_event(
            project_id,
            "project.member_removed",
            member_id,
            {
                "member_id": member_id,
                # 记下被移除时的角色：界面据此提供"恢复"，恢复时按原角色加回去
                "role": membership["role"],
                "released_tasks": released,
                "revoked_grants": revoked_grants,
                "devices": devices,
            },
            actor_kind="member",
        )
        return {
            "member_id": member_id,
            "released_tasks": released,
            "revoked_grants": revoked_grants,
            "revoked_devices": devices,
        }

    def authorize_member(self, project_id: UUID, member_id: str, permission: str) -> None:
        role = self._member_project_role(project_id, member_id)
        if not role:
            raise PermissionError("member_project_access_denied")
        allowed = {
            "project.view": {"owner", "project_lead", "contributor", "reviewer", "observer"},
            "project.write": {"owner", "project_lead", "contributor"},
            "project.admin": {"owner", "project_lead"},
            # 项目聊天：所有能看到项目的人都能发言，observer（只读观察）除外
            "project.chat": {"owner", "project_lead", "contributor", "reviewer"},
            "task.create": {"owner", "project_lead", "contributor"},
            "task.execute": {"owner", "project_lead", "contributor"},
            "review.submit": {"owner", "project_lead", "reviewer"},
            "review.approve": {"owner", "project_lead", "reviewer"},
            "project.export": {"owner", "project_lead"},
        }
        if role not in allowed.get(permission, set()):
            raise PermissionError("permission_denied")

    def authorize_organization_member(self, organization_id: UUID, member_id: str, permission: str = "project.restore") -> None:
        member = self.db.execute("SELECT organization_id, status FROM human_members WHERE id = ?", (member_id,)).fetchone()
        if not member or member["organization_id"] != str(organization_id) or member["status"] != "active":
            raise PermissionError("member_organization_access_denied")
        rows = self.db.execute(
            "SELECT m.role FROM memberships m JOIN teams t ON t.id = m.team_id WHERE m.member_id = ? AND t.organization_id = ?",
            (member_id, str(organization_id)),
        ).fetchall()
        allowed = {
            "project.restore": {"owner", "project_lead"},
            "project.admin": {"owner", "project_lead"},
        }
        if not any(row["role"] in allowed.get(permission, set()) for row in rows):
            raise PermissionError("organization_permission_denied")

    def list_projects_for_member(self, member_id: str) -> list[Project]:
        self.get_member(member_id)
        rows = self.db.execute("SELECT p.* FROM projects p JOIN project_memberships pm ON pm.project_id = p.id WHERE pm.member_id = ? ORDER BY p.updated_at DESC", (member_id,))
        return [self._project(row) for row in rows]

    def _member_project_role(self, project_id: UUID, member_id: str) -> str | None:
        row = self.db.execute("SELECT role FROM project_memberships WHERE project_id = ? AND member_id = ?", (str(project_id), member_id)).fetchone()
        return row["role"] if row else None

    def _device(self, row: sqlite3.Row) -> Device:
        values = dict(row)
        values["organization_id"] = UUID(values["organization_id"])
        values["capabilities"] = self._json(values["capabilities"])
        values["status"] = DeviceStatus(values["status"])
        values["created_at"] = parse_time(values["created_at"])
        values["last_seen"] = parse_time(values["last_seen"]) if values["last_seen"] else None
        values["revoked_at"] = parse_time(values["revoked_at"]) if values["revoked_at"] else None
        values.pop("public_key", None)
        values.pop("device_token_hash", None)
        return Device(**values)

    def get_device(self, device_id: str) -> Device:
        row = self.db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        if not row:
            raise KeyError("device_not_found")
        return self._device(row)

    def list_devices_for_member(self, member_id: str) -> list[Device]:
        member = self.get_member(member_id)
        rows = self.db.execute(
            "SELECT * FROM devices WHERE organization_id = ? ORDER BY created_at DESC",
            (str(member.organization_id),),
        ).fetchall()
        # B4：列表端带上最近心跳运行态（设备数量是个位数，逐台查询足够）。
        return [
            self._device(row).model_copy(update={"runtime": self.get_device_runtime_state(row["device_id"])})
            for row in rows
        ]

    def list_device_project_grants_for_device(self, device_id: str) -> list[DeviceProjectGrant]:
        """B3：某设备被授权的全部项目（`GET /api/agent/me` 用）。已撤销/过期的也返回，由调用方判断。"""

        rows = self.db.execute(
            "SELECT * FROM device_project_grants WHERE device_id = ? AND revoked_at IS NULL ORDER BY created_at DESC",
            (device_id,),
        ).fetchall()
        return [self._device_project_grant(row) for row in rows]

    def create_device_pairing(self, data: DevicePairingCreate, created_by: str) -> DevicePairing:
        self.authorize_organization_member(data.organization_id, created_by, "project.admin")
        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(seconds=data.expires_in_seconds)
        pairing_id = uuid4()
        pairing_code = f"map_{secrets.token_urlsafe(24)}"
        code_hash = hashlib.sha256(pairing_code.encode("utf-8")).hexdigest()
        challenge = create_challenge()
        challenge_hash = hash_secret(challenge)
        self.db.execute(
            "INSERT INTO device_pairings (id, organization_id, created_by, code_hash, challenge_hash, status, expires_at, created_at) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?)",
            (str(pairing_id), str(data.organization_id), created_by, code_hash, challenge_hash, expires_at.isoformat(), created_at.isoformat()),
        )
        self.db.commit()
        return DevicePairing(
            id=pairing_id,
            organization_id=data.organization_id,
            created_by=created_by,
            status=DevicePairingStatus.PENDING,
            expires_at=expires_at,
            created_at=created_at,
            pairing_code=pairing_code,
            challenge=challenge,
        )

    def register_device(self, data: DeviceRegisterRequest) -> DeviceCredential:
        code_hash = hashlib.sha256(data.pairing_code.encode("utf-8")).hexdigest()
        pairing = self.db.execute("SELECT * FROM device_pairings WHERE id = ?", (str(data.pairing_id),)).fetchone()
        if not pairing:
            raise PermissionError("device_pairing_invalid")
        if not secrets.compare_digest(pairing["code_hash"], code_hash):
            raise PermissionError("device_pairing_invalid")
        if pairing["status"] != DevicePairingStatus.PENDING.value:
            raise ValueError("device_pairing_not_pending")
        if parse_time(pairing["expires_at"]) <= datetime.now(UTC):
            self.db.execute("UPDATE device_pairings SET status = 'EXPIRED' WHERE id = ?", (pairing["id"],))
            self.db.commit()
            raise ValueError("device_pairing_expired")

        agent = self.db.execute("SELECT owner_member_id FROM agents WHERE agent_id = ?", (data.agent_id,)).fetchone()
        if not agent:
            raise KeyError("agent_not_found")
        if agent["owner_member_id"] != pairing["created_by"]:
            # 归属账号仍在职 → 拒绝（防冒名接管别人的 Agent）。
            # 归属账号已停用/不存在 → 允许生成配对的人接管：接入脚本（`agentd register`）
            # 登记的 Agent 归属是开发期种子账号 member-001，账号系统上线后它被停用，
            # 不接管的话新设备接入必然 403。
            owner = self.db.execute("SELECT status FROM human_members WHERE id = ?", (agent["owner_member_id"],)).fetchone()
            if owner and owner["status"] == "active":
                raise PermissionError("device_agent_owner_mismatch")
            self.db.execute("UPDATE agents SET owner_member_id = ? WHERE agent_id = ?", (pairing["created_by"], data.agent_id))

        device_id = data.device_id
        if not device_id:
            raise ValueError("device_id_required_for_signature")
        if not pairing["challenge_hash"] or not secrets.compare_digest(pairing["challenge_hash"], hash_secret(data.challenge)):
            raise PermissionError("device_pairing_challenge_invalid")
        try:
            public_key_fingerprint = verify_registration_signature(
                data.public_key,
                data.challenge_signature,
                data.pairing_id,
                data.challenge,
                data.agent_id,
                device_id,
            )
        except DeviceIdentityError as error:
            raise ValueError(str(error)) from error
        if self.db.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone():
            raise ValueError("device_id_already_registered")
        if self.db.execute("SELECT 1 FROM devices WHERE public_key_fingerprint = ?", (public_key_fingerprint,)).fetchone():
            raise ValueError("device_public_key_already_registered")

        created_at = datetime.now(UTC)
        device_token = f"dvc_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(device_token.encode("utf-8")).hexdigest()
        self.db.execute(
            "INSERT INTO devices (device_id, organization_id, agent_id, owner_member_id, device_name, public_key, public_key_fingerprint, device_token_hash, platform, agent_version, capabilities, status, created_at, last_seen, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL)",
            (
                device_id,
                pairing["organization_id"],
                data.agent_id,
                pairing["created_by"],
                data.device_name,
                data.public_key,
                public_key_fingerprint,
                token_hash,
                data.platform,
                data.agent_version,
                json.dumps(data.capabilities, ensure_ascii=False),
                created_at.isoformat(),
                created_at.isoformat(),
            ),
        )
        update = self.db.execute(
            "UPDATE device_pairings SET status = 'CONSUMED', device_id = ?, consumed_at = ? WHERE id = ? AND status = 'PENDING'",
            (device_id, created_at.isoformat(), pairing["id"]),
        )
        if update.rowcount != 1:
            self.db.rollback()
            raise ValueError("device_pairing_already_consumed")
        self.db.commit()
        return DeviceCredential(device=self.get_device(device_id), device_token=device_token)

    def rotate_device_token(self, device_id: str, rotated_by: str, reason: str = "rotated_by_member") -> DeviceCredential:
        """Atomically replace a device token and invalidate live connections.

        The replacement token is returned only from this call. No plaintext
        token is written to SQLite; the rotation table is an audit trail only.
        """
        if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
            raise ValueError("device_token_rotation_reason_invalid")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
            if not row:
                raise KeyError("device_not_found")
            self.authorize_organization_member(UUID(row["organization_id"]), rotated_by, "project.admin")
            if row["status"] != DeviceStatus.ACTIVE.value:
                raise PermissionError("device_revoked")

            token = f"dvc_{secrets.token_urlsafe(32)}"
            token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            rotated_at = now()
            token_version = int(row["token_version"] or 1) + 1
            self.db.execute(
                "UPDATE devices SET device_token_hash = ?, token_version = ?, token_rotated_at = ? WHERE device_id = ?",
                (token_hash, token_version, rotated_at, device_id),
            )
            self.db.execute(
                "UPDATE agent_connections SET status = 'REVOKED', disconnected_at = ? WHERE device_id = ? AND status != 'REVOKED'",
                (rotated_at, device_id),
            )
            self.db.execute(
                "INSERT INTO device_token_rotations (id, device_id, organization_id, rotated_by, reason, token_version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (str(uuid4()), device_id, row["organization_id"], rotated_by, reason, token_version, rotated_at),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return DeviceCredential(device=self.get_device(device_id), device_token=token)

    def resolve_device_token(self, device_token: str) -> Device:
        token_hash = hashlib.sha256(device_token.encode("utf-8")).hexdigest()
        row = self.db.execute("SELECT * FROM devices WHERE device_token_hash = ?", (token_hash,)).fetchone()
        if not row:
            raise PermissionError("device_token_invalid")
        if row["status"] != DeviceStatus.ACTIVE.value:
            raise PermissionError("device_revoked")
        return self._device(row)

    def revoke_device(self, device_id: str, revoked_by: str, reason: str = "revoked_by_member") -> Device:
        row = self.db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        if not row:
            raise KeyError("device_not_found")
        organization_id = UUID(row["organization_id"])
        self.authorize_organization_member(organization_id, revoked_by, "project.admin")
        if row["status"] != DeviceStatus.REVOKED.value:
            revoked_at = now()
            self.db.execute("UPDATE devices SET status = 'revoked', revoked_at = ? WHERE device_id = ?", (revoked_at, device_id))
            self.db.execute("UPDATE device_project_grants SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL", (revoked_at, device_id))
            self.db.execute("UPDATE agent_connections SET status = 'REVOKED', disconnected_at = ? WHERE device_id = ? AND status != 'REVOKED'", (revoked_at, device_id))
            self.db.commit()
            # 设备撤销后它的 Agent 已无可用连接 → 置 offline（UX-3-02）。
            for row in self.db.execute(
                "SELECT DISTINCT agent_id FROM agent_connections WHERE device_id = ?", (device_id,)
            ).fetchall():
                self._agent_offline_if_disconnected(row["agent_id"])
            self.db.commit()
        return self.get_device(device_id)

    def _device_project_grant(self, row: sqlite3.Row) -> DeviceProjectGrant:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["project_id"] = UUID(values["project_id"])
        values["capabilities"] = self._json(values["capabilities"])
        values["expires_at"] = parse_time(values["expires_at"])
        values["revoked_at"] = parse_time(values["revoked_at"]) if values["revoked_at"] else None
        values["created_at"] = parse_time(values["created_at"])
        values.pop("token_hash", None)
        return DeviceProjectGrant(**values)

    def create_device_project_grant(self, project_id: UUID, data: DeviceProjectGrantCreate, granted_by: str) -> DeviceProjectCredential:
        project = self.db.execute("SELECT organization_id FROM projects WHERE id = ?", (str(project_id),)).fetchone()
        if not project:
            raise KeyError("project_not_found")
        self.authorize_member(project_id, granted_by, "project.admin")
        device = self.db.execute("SELECT * FROM devices WHERE device_id = ?", (data.device_id,)).fetchone()
        if not device:
            raise KeyError("device_not_found")
        if device["organization_id"] != project["organization_id"]:
            raise PermissionError("device_project_organization_mismatch")
        if device["status"] != DeviceStatus.ACTIVE.value:
            raise PermissionError("device_revoked")

        created_at = datetime.now(UTC)
        expires_at = created_at + timedelta(seconds=data.expires_in_seconds)
        grant_id = uuid4()
        project_token = f"prj_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(project_token.encode("utf-8")).hexdigest()
        self.db.execute(
            "INSERT INTO device_project_grants (id, device_id, agent_id, project_id, token_hash, capabilities, granted_by, expires_at, revoked_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
            (str(grant_id), data.device_id, device["agent_id"], str(project_id), token_hash, json.dumps(data.capabilities, ensure_ascii=False), granted_by, expires_at.isoformat(), created_at.isoformat()),
        )
        # Preserve compatibility with the legacy task-claim path while the
        # Gateway is being introduced: the token remains the narrower scope.
        self.db.execute(
            "INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES (?, ?, ?, ?, ?)",
            (device["agent_id"], str(project_id), json.dumps(data.capabilities, ensure_ascii=False), granted_by, created_at.isoformat()),
        )
        self.db.commit()
        grant = self._device_project_grant(self.db.execute("SELECT * FROM device_project_grants WHERE id = ?", (str(grant_id),)).fetchone())
        return DeviceProjectCredential(grant=grant, project_token=project_token)

    def list_device_project_grants(self, project_id: UUID, member_id: str) -> list[DeviceProjectGrant]:
        self.authorize_member(project_id, member_id, "project.view")
        rows = self.db.execute("SELECT * FROM device_project_grants WHERE project_id = ? ORDER BY created_at DESC", (str(project_id),))
        return [self._device_project_grant(row) for row in rows]

    def revoke_device_project_grant(self, grant_id: UUID, revoked_by: str) -> DeviceProjectGrant:
        row = self.db.execute("SELECT * FROM device_project_grants WHERE id = ?", (str(grant_id),)).fetchone()
        if not row:
            raise KeyError("device_project_grant_not_found")
        self.authorize_member(UUID(row["project_id"]), revoked_by, "project.admin")
        self.db.execute("UPDATE device_project_grants SET revoked_at = COALESCE(revoked_at, ?) WHERE id = ?", (now(), str(grant_id)))
        self.db.commit()
        return self._device_project_grant(self.db.execute("SELECT * FROM device_project_grants WHERE id = ?", (str(grant_id),)).fetchone())

    def resolve_device_project_token(self, project_token: str, project_id: UUID, capability: str | None = None) -> DeviceProjectGrant:
        token_hash = hashlib.sha256(project_token.encode("utf-8")).hexdigest()
        row = self.db.execute(
            "SELECT g.*, d.status AS device_status FROM device_project_grants g JOIN devices d ON d.device_id = g.device_id WHERE g.token_hash = ? AND g.project_id = ?",
            (token_hash, str(project_id)),
        ).fetchone()
        if not row:
            raise PermissionError("device_project_token_invalid")
        if row["device_status"] != DeviceStatus.ACTIVE.value or row["revoked_at"] is not None:
            raise PermissionError("device_project_token_revoked")
        if parse_time(row["expires_at"]) <= datetime.now(UTC):
            raise PermissionError("device_project_token_expired")
        grant = self._device_project_grant(row)
        if capability and capability not in grant.capabilities:
            raise PermissionError("device_capability_denied")
        return grant

    def resolve_device_agent_project_grant(self, device_id: str, agent_id: str, project_id: UUID, capability: str | None = None) -> DeviceProjectGrant:
        """Resolve a grant from the already authenticated device connection."""
        row = self.db.execute(
            "SELECT g.*, d.status AS device_status FROM device_project_grants g JOIN devices d ON d.device_id = g.device_id WHERE g.device_id = ? AND d.agent_id = ? AND g.project_id = ?",
            (device_id, agent_id, str(project_id)),
        ).fetchone()
        if not row:
            raise PermissionError("device_project_grant_invalid")
        if row["device_status"] != DeviceStatus.ACTIVE.value or row["revoked_at"] is not None:
            raise PermissionError("device_project_grant_revoked")
        if parse_time(row["expires_at"]) <= datetime.now(UTC):
            raise PermissionError("device_project_grant_expired")
        grant = self._device_project_grant(row)
        if capability and capability not in grant.capabilities:
            raise PermissionError("device_capability_denied")
        return grant

    def open_agent_connection(self, device_id: str, session_id: str, connection_id: str, transport: str = "websocket") -> AgentConnection:
        if transport not in {"websocket", "long_poll"}:
            raise ValueError("invalid_gateway_transport")
        device = self.db.execute("SELECT * FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        if not device:
            raise KeyError("device_not_found")
        if device["status"] != DeviceStatus.ACTIVE.value:
            raise PermissionError("device_revoked")
        timestamp = now()
        self.db.execute(
            "INSERT INTO agent_connections (connection_id, device_id, agent_id, session_id, transport, status, connected_at, last_heartbeat_at) VALUES (?, ?, ?, ?, ?, 'CONNECTED', ?, ?)",
            (connection_id, device_id, device["agent_id"], session_id, transport, timestamp, timestamp),
        )
        self.db.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (timestamp, device_id))
        self.db.commit()
        return self._agent_connection(self.db.execute("SELECT * FROM agent_connections WHERE connection_id = ?", (connection_id,)).fetchone())

    def get_agent_connection(self, connection_id: str) -> AgentConnection:
        row = self.db.execute("SELECT * FROM agent_connections WHERE connection_id = ?", (connection_id,)).fetchone()
        if not row:
            raise KeyError("agent_connection_not_found")
        return self._agent_connection(row)

    def record_gateway_receive(self, connection_id: str, sequence: int) -> tuple[str, int]:
        if sequence < 1:
            raise ValueError("gateway_sequence_invalid")
        row = self.db.execute("SELECT status, last_received_sequence FROM agent_connections WHERE connection_id = ?", (connection_id,)).fetchone()
        if not row:
            raise KeyError("agent_connection_not_found")
        if row["status"] == AgentConnectionStatus.REVOKED.value:
            raise PermissionError("device_connection_revoked")
        current = int(row["last_received_sequence"])
        if sequence <= current:
            return "DUPLICATE", current
        if sequence != current + 1:
            raise ValueError(f"gateway_sequence_gap:{current + 1}:{sequence}")
        self.db.execute(
            "UPDATE agent_connections SET last_received_sequence = ?, last_heartbeat_at = ? WHERE connection_id = ?",
            (sequence, now(), connection_id),
        )
        self.db.commit()
        return "ACCEPTED", sequence

    def next_gateway_send_sequence(self, connection_id: str) -> int:
        row = self.db.execute("SELECT status, last_sent_sequence FROM agent_connections WHERE connection_id = ?", (connection_id,)).fetchone()
        if not row:
            raise KeyError("agent_connection_not_found")
        if row["status"] == AgentConnectionStatus.REVOKED.value:
            raise PermissionError("device_connection_revoked")
        sequence = int(row["last_sent_sequence"]) + 1
        self.db.execute("UPDATE agent_connections SET last_sent_sequence = ? WHERE connection_id = ?", (sequence, connection_id))
        self.db.commit()
        return sequence

    def record_agent_heartbeat(self, connection_id: str, heartbeat: Any) -> AgentConnection:
        row = self.db.execute("SELECT * FROM agent_connections WHERE connection_id = ?", (connection_id,)).fetchone()
        if not row:
            raise KeyError("agent_connection_not_found")
        if row["status"] != AgentConnectionStatus.CONNECTED.value:
            raise PermissionError("agent_connection_not_active")
        if (
            row["device_id"] != heartbeat.device_id
            or row["agent_id"] != heartbeat.agent_id
            or row["session_id"] != heartbeat.session_id
            or row["connection_id"] != heartbeat.connection_id
        ):
            raise PermissionError("gateway_identity_mismatch")
        timestamp = now()
        self.db.execute("UPDATE agent_connections SET last_heartbeat_at = ? WHERE connection_id = ?", (timestamp, connection_id))
        self.db.execute("UPDATE devices SET last_seen = ? WHERE device_id = ?", (timestamp, heartbeat.device_id))
        self.db.execute("UPDATE agents SET status = 'online', last_seen = ? WHERE agent_id = ?", (timestamp, heartbeat.agent_id))
        self._record_device_runtime_state(heartbeat, connection_id, timestamp)
        self.db.commit()
        return self.get_agent_connection(connection_id)

    def _record_device_runtime_state(self, heartbeat: Any, connection_id: str, timestamp: str) -> None:
        """B1/B2：心跳里的运行态落库。

        此前只更新时间戳，`adapter_versions` / `capabilities` / `running_run_ids` /
        `local_queue_length` / `user_session_state` / `resource_summary` 全被丢弃，
        平台侧因此看不到"这台机器上有什么执行体、在跑什么、排了多长"。
        落库只用于展示；授权判定仍然只看设备 Token 与项目授权串。
        """

        def _as_dict(value: Any) -> dict[str, Any]:
            return dict(value) if isinstance(value, dict) else {}

        adapter_versions = {str(key): str(value) for key, value in _as_dict(getattr(heartbeat, "adapter_versions", None)).items()}
        capabilities = [str(item) for item in (getattr(heartbeat, "capabilities", None) or [])]
        running_run_ids = [str(item) for item in (getattr(heartbeat, "running_run_ids", None) or [])]
        resource_summary = _as_dict(getattr(heartbeat, "resource_summary", None))
        local_queue_length = max(0, int(getattr(heartbeat, "local_queue_length", 0) or 0))
        user_session_state = str(getattr(heartbeat, "user_session_state", "unknown") or "unknown")
        agent_version = str(getattr(heartbeat, "agent_version", "") or "")
        payload = (
            str(heartbeat.device_id),
            str(connection_id),
            agent_version or None,
            json.dumps(adapter_versions, ensure_ascii=False),
            json.dumps(capabilities, ensure_ascii=False),
            json.dumps(running_run_ids, ensure_ascii=False),
            local_queue_length,
            user_session_state,
            json.dumps(resource_summary, ensure_ascii=False),
            timestamp,
        )
        self.db.execute(
            "INSERT INTO device_runtime_state (device_id, connection_id, agent_version, adapter_versions, capabilities, running_run_ids, local_queue_length, user_session_state, resource_summary, reported_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET connection_id = excluded.connection_id, agent_version = excluded.agent_version, "
            "adapter_versions = excluded.adapter_versions, capabilities = excluded.capabilities, running_run_ids = excluded.running_run_ids, "
            "local_queue_length = excluded.local_queue_length, user_session_state = excluded.user_session_state, "
            "resource_summary = excluded.resource_summary, reported_at = excluded.reported_at",
            payload,
        )
        # B2：设备能力/版本以最近一次自报为准——装了或卸了执行体，重新注册前平台就能看到。
        if capabilities:
            self.db.execute(
                "UPDATE devices SET capabilities = ? WHERE device_id = ?",
                (json.dumps(capabilities, ensure_ascii=False), heartbeat.device_id),
            )
        if agent_version:
            self.db.execute("UPDATE devices SET agent_version = ? WHERE device_id = ?", (agent_version, heartbeat.device_id))
        # AIP-1c：设备自报运行态后，把该执行体的包信息从"前缀猜测"升级为探测到的具体版本
        if adapter_versions:
            device = self.db.execute("SELECT agent_id FROM devices WHERE device_id = ?", (str(heartbeat.device_id),)).fetchone()
            if device:
                self._upgrade_agent_package(device["agent_id"])

    def _device_runtime_state(self, row: sqlite3.Row) -> DeviceRuntimeState:
        values = dict(row)
        values["adapter_versions"] = self._json(values["adapter_versions"] or "{}")
        values["capabilities"] = self._json(values["capabilities"] or "[]")
        values["running_run_ids"] = self._json(values["running_run_ids"] or "[]")
        values["resource_summary"] = self._json(values["resource_summary"] or "{}")
        values["local_queue_length"] = int(values["local_queue_length"] or 0)
        values["reported_at"] = parse_time(values["reported_at"])
        return DeviceRuntimeState(**values)

    def get_device_runtime_state(self, device_id: str) -> DeviceRuntimeState | None:
        row = self.db.execute("SELECT * FROM device_runtime_state WHERE device_id = ?", (device_id,)).fetchone()
        return self._device_runtime_state(row) if row else None

    def close_agent_connection(self, connection_id: str, status: str = "DISCONNECTED") -> AgentConnection:
        if status not in {AgentConnectionStatus.DISCONNECTED.value, AgentConnectionStatus.REVOKED.value}:
            raise ValueError("invalid_agent_connection_close_status")
        row = self.db.execute("SELECT status, agent_id FROM agent_connections WHERE connection_id = ?", (connection_id,)).fetchone()
        if not row:
            raise KeyError("agent_connection_not_found")
        if row["status"] != AgentConnectionStatus.REVOKED.value:
            self.db.execute(
                "UPDATE agent_connections SET status = ?, disconnected_at = ? WHERE connection_id = ?",
                (status, now(), connection_id),
            )
            self.db.commit()
            # 最后一个连接断开 → Agent 置 offline（UX-3-02：此前只改连接状态，Agent 会永远显示在线）。
            if status == AgentConnectionStatus.DISCONNECTED.value:
                self._agent_offline_if_disconnected(row["agent_id"])
                self.db.commit()
        return self.get_agent_connection(connection_id)

    def _agent_connection(self, row: sqlite3.Row) -> AgentConnection:
        values = dict(row)
        values["status"] = AgentConnectionStatus(values["status"])
        values["connected_at"] = parse_time(values["connected_at"])
        values["last_heartbeat_at"] = parse_time(values["last_heartbeat_at"])
        values["disconnected_at"] = parse_time(values["disconnected_at"]) if values["disconnected_at"] else None
        return AgentConnection(**values)

    def get_gateway_command_result(
        self,
        connection_id: str,
        *,
        message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> GatewayCommandResult | None:
        if not message_id and not idempotency_key:
            raise ValueError("gateway_command_lookup_key_required")
        if message_id and idempotency_key:
            row = self.db.execute(
                "SELECT * FROM gateway_command_results WHERE connection_id = ? AND message_id = ? AND idempotency_key = ?",
                (connection_id, message_id, idempotency_key),
            ).fetchone()
        elif message_id:
            row = self.db.execute(
                "SELECT * FROM gateway_command_results WHERE connection_id = ? AND message_id = ?",
                (connection_id, message_id),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM gateway_command_results WHERE connection_id = ? AND idempotency_key = ?",
                (connection_id, idempotency_key),
            ).fetchone()
        return self._gateway_command_result(row) if row else None

    def save_gateway_command_result(self, result: GatewayCommandResult) -> GatewayCommandResult:
        connection = self.db.execute(
            "SELECT 1 FROM agent_connections WHERE connection_id = ?",
            (result.connection_id,),
        ).fetchone()
        if not connection:
            raise KeyError("agent_connection_not_found")
        existing = self.get_gateway_command_result(result.connection_id, message_id=result.message_id)
        if existing:
            if existing.idempotency_key != result.idempotency_key or existing.message_type != result.message_type:
                raise ValueError("gateway_command_result_identity_conflict")
            if existing.request_hash and result.request_hash and existing.request_hash != result.request_hash:
                raise ValueError("gateway_command_request_mismatch")
            return existing
        existing = self.get_gateway_command_result(result.connection_id, idempotency_key=result.idempotency_key)
        if existing:
            if existing.message_type != result.message_type:
                raise ValueError("gateway_command_result_identity_conflict")
            if existing.request_hash and result.request_hash and existing.request_hash != result.request_hash:
                raise ValueError("gateway_command_request_mismatch")
            return existing
        self.db.execute(
            "INSERT INTO gateway_command_results (id, connection_id, message_id, idempotency_key, sequence, message_type, status, response_type, request_hash, result, error_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(result.id),
                result.connection_id,
                result.message_id,
                result.idempotency_key,
                result.sequence,
                result.message_type,
                result.status,
                result.response_type,
                result.request_hash,
                json.dumps(result.result, ensure_ascii=False) if result.result is not None else None,
                result.error_code,
                result.created_at.isoformat(),
            ),
        )
        self.db.commit()
        return result

    def _gateway_command_result(self, row: sqlite3.Row) -> GatewayCommandResult:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["result"] = self._json(values["result"]) if values["result"] is not None else None
        values["created_at"] = parse_time(values["created_at"])
        return GatewayCommandResult(**values)

    def grant_agent_project(self, grant: AgentProjectGrant) -> AgentProjectGrant:
        if not self.db.execute("SELECT 1 FROM agents WHERE agent_id = ?", (grant.agent_id,)).fetchone():
            raise KeyError("agent_not_found")
        project = self.db.execute("SELECT organization_id FROM projects WHERE id = ?", (str(grant.project_id),)).fetchone()
        if not project:
            raise KeyError("project_not_found")
        self.authorize_member(grant.project_id, grant.granted_by, "task.create")
        timestamp = now()
        self.db.execute("INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES (?, ?, ?, ?, ?)", (grant.agent_id, str(grant.project_id), json.dumps(grant.capabilities), grant.granted_by, timestamp))
        self.db.commit()
        return AgentProjectGrant(agent_id=grant.agent_id, project_id=grant.project_id, capabilities=grant.capabilities, granted_by=grant.granted_by, created_at=parse_time(timestamp))

    # ---------- 派单（P1-1） ----------

    # 谁能改任务的"负责人"（W-2 派单口径）：
    #   * 队长（owner/project_lead）：派给谁、改派、取消都行；
    #   * 普通成员：只能在**非 manual 模式**下认领**还没有负责人**的任务（把负责人设成自己），
    #     以及释放自己认领的任务。默认 manual 就是"任务由队长派遣"，成员不能自己挑活。
    DISPATCH_LEAD_ROLES = {"owner", "project_lead"}

    def _dispatch_policy(
        self,
        project_id: UUID,
        task_row: sqlite3.Row,
        actor_member_id: str,
        target_member_id: str | None,
    ) -> str:
        """校验一次派单改动，返回动作名（dispatch / self_claim / release）。"""

        role = self._member_project_role(project_id, actor_member_id)
        if role in self.DISPATCH_LEAD_ROLES:
            return "release" if target_member_id is None else "dispatch"
        current = task_row["assignee_member_id"]
        if target_member_id is None:
            if current and current == actor_member_id:
                return "release"
            raise PermissionError("task_dispatch_requires_lead")
        if target_member_id != actor_member_id:
            # 成员不能把任务派给别人（那是队长的活）
            raise PermissionError("task_dispatch_requires_lead")
        project = self.get_project(project_id)
        if (project.task_mode or "manual") == "manual":
            raise PermissionError("task_self_claim_disabled_in_manual_mode")
        if current and current != actor_member_id:
            raise PermissionError("task_assigned_to_another_member")
        return "self_claim"

    AUTO_ACTOR = "platform-auto"

    def _open_task_count(self, project_id: UUID, member_id: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE project_id = ? AND assignee_member_id = ? AND status IN "
            "('READY','CLAIMED','RUNNING','BLOCKED','NEEDS_REVISION','WAITING_REVIEW')",
            (str(project_id), member_id),
        ).fetchone()
        return int(row["c"] or 0)

    def _online_agents_by_member(self, project_id: UUID) -> dict[str, list[str]]:
        """项目里"在线且对这个项目有授权"的 Agent，按成员归组。"""

        rows = self.db.execute(
            "SELECT a.agent_id, a.owner_member_id FROM agents a "
            "JOIN agent_project_grants g ON g.agent_id = a.agent_id AND g.project_id = ? "
            "WHERE a.status = 'online' ORDER BY a.agent_id",
            (str(project_id),),
        ).fetchall()
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["owner_member_id"], []).append(row["agent_id"])
        return grouped

    def _auto_dispatch_candidate(
        self, project_id: UUID, task_row: sqlite3.Row, *, reliability: dict[str, dict[str, Any]] | None = None
    ) -> tuple[str, str, list[str]] | None:
        """给一条任务找一个"有合格在线执行体"的成员，返回 (member_id, agent_id, 命中的能力)。

        与界面用的是**同一个排序函数**（`rank_task_candidates`，AIP-1b）：在线 → 平滑成功率 →
        在手任务数 → member_id → agent_id。所以"界面推荐的第一个"就是"调度器会派的那一个"，
        不会出现"页面说 A 更合适、系统派给了 B"。
        """

        ranked = self.rank_task_candidates(project_id, task_row, limit=50, reliability=reliability)
        for item in ranked["satisfied"]:
            if item["online"]:
                matched = sorted(str(value) for value in self._json(task_row["required_capabilities"] or "[]"))
                return item["member_id"], item["agent_id"], matched
        return None

    def auto_dispatch_tick(self, project_id: UUID, *, limit: int = 1) -> list[Event]:
        """auto 模式的一次自动推进；返回本次产生的事件（供广播）。

        调用点：维护循环每拍（所有 auto 项目）+ 切到 auto / 物化任务之后立即跑一次。
        """

        project = self.get_project(project_id)
        if (project.task_mode or "manual") != "auto":
            return []
        events: list[Event] = []
        candidates = self.db.execute(
            "SELECT * FROM tasks WHERE project_id = ? AND status IN ('READY','NEEDS_REVISION') "
            "AND assignee_member_id IS NULL ORDER BY (deadline IS NULL), deadline ASC, updated_at ASC LIMIT 50",
            (str(project_id),),
        ).fetchall()

        dispatched = 0
        # 成功率统计每次 tick 只查一次（同一个排序函数被逐条任务调用，不重复打库）
        reliability = self._agent_reliability()
        for row in candidates:
            task_id = UUID(row["id"])
            if self._deadline_passed(row):
                continue
            # 预算用尽的任务不再派人（派了也领不走）；写一次性告警让队长看见
            budget_state = self._task_budget_state(row)
            if budget_state["exhausted"]:
                note = self._note_budget_exhausted(project_id, task_id, budget_state)
                if note is not None:
                    events.append(note)
                continue
            # 依赖没满足的任务不该提前派人（等它 eligible 了下一个 tick 再说）
            try:
                self._assert_task_claimable_inputs(self.get_task(task_id))
            except ValueError:
                continue
            match = self._auto_dispatch_candidate(project_id, row, reliability=reliability)
            if match is None:
                required = sorted(str(item) for item in self._json(row["required_capabilities"] or "[]"))
                if not required:
                    continue
                # 一次提示：同一个任务只报一次"没人能跑"。
                # add_event 命中幂等键时会把旧事件原样返回——那算"已经报过"，不能再广播一次。
                key = f"auto-unmatched:{task_id}"
                if self.db.execute(
                    "SELECT 1 FROM events WHERE project_id = ? AND idempotency_key = ?", (str(project_id), key)
                ).fetchone():
                    continue
                events.append(
                    self.add_event(
                        project_id,
                        "task.auto_unmatched",
                        self.AUTO_ACTOR,
                        {"task_id": str(task_id), "required_capabilities": required},
                        actor_kind="system",
                        object_type="task",
                        object_id=task_id,
                        idempotency_key=key,
                    )
                )
                continue
            member_id, agent_id, matched = match
            previous = row["assignee_member_id"]
            self.db.execute(
                "UPDATE tasks SET assignee_member_id = ?, updated_at = ? WHERE id = ?",
                (member_id, now(), str(task_id)),
            )
            self.db.commit()
            events.append(
                self.add_event(
                    project_id,
                    "task.auto_assigned",
                    self.AUTO_ACTOR,
                    {
                        "task_id": str(task_id),
                        "assignee_member_id": member_id,
                        "previous_assignee_member_id": previous,
                        "agent_id": agent_id,
                        "matched_capabilities": matched,
                        "action": "auto_assigned",
                    },
                    actor_kind="system",
                    object_type="task",
                    object_id=task_id,
                )
            )
            dispatched += 1
            if dispatched >= limit:
                break
        return events

    def assign_tasks(
        self,
        project_id: UUID,
        task_ids: list[UUID],
        assignee_member_id: str | None,
        actor_member_id: str,
    ) -> dict[str, Any]:
        """批量派单（队长一键把物化出来的任务分下去）。

        逐条走同一套策略：一条不合法只影响这一条，其余照常执行——
        批量操作要么全成功要么全失败会让"18 条里有 1 条已被别人认领"变成人工排除的体力活。
        """

        target = (assignee_member_id or "").strip() or None
        self._validate_dispatch_target(project_id, target)
        updated: list[str] = []
        failures: list[dict[str, str]] = []
        for task_id in task_ids:
            try:
                task = self.update_task(
                    task_id,
                    None,
                    None,
                    None,
                    actor=actor_member_id,
                    actor_kind="member",
                    assignee_member_id=target or "",
                    dispatch_provided=True,
                )
                updated.append(str(task.id))
            except (KeyError, ValueError, PermissionError) as error:
                # KeyError 的 str() 带引号（"'task_not_found'"），错误码要稳定可比对
                reason = error.args[0] if isinstance(error, KeyError) and error.args else str(error)
                failures.append({"task_id": str(task_id), "reason": str(reason)})
        return {"updated": len(updated), "task_ids": updated, "failures": failures}

    def _validate_dispatch_target(self, project_id: UUID, assignee_member_id: str | None) -> None:
        """指派目标必须是项目成员；否则会出现"指派给一个看不到这个项目的人"。"""

        if not assignee_member_id:
            return
        if not self.db.execute(
            "SELECT 1 FROM project_memberships WHERE project_id = ? AND member_id = ?",
            (str(project_id), assignee_member_id),
        ).fetchone():
            raise ValueError("assignee_not_project_member")

    def device_owner_member(self, device_id: str) -> str | None:
        row = self.db.execute("SELECT owner_member_id FROM devices WHERE device_id = ?", (device_id,)).fetchone()
        return row["owner_member_id"] if row else None

    def execution_attribution(self, agent_id: str, device_id: str | None) -> tuple[str | None, str | None]:
        """推导一次执行的归属，返回 (device_id, member_id)。

        device_id 以执行体上报的为准（它自己最清楚），但必须与该 Agent 对得上，否则丢弃；
        member_id 一律服务端推导（设备归属优先，其次 Agent 归属）——不接受客户端自报的成员身份。
        """

        if device_id:
            row = self.db.execute(
                "SELECT agent_id, owner_member_id FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row and row["agent_id"] == agent_id:
                return device_id, row["owner_member_id"]
        # 没带设备信息（老版本执行体）：退回该 Agent 最近一次活跃连接所属的设备
        row = self.db.execute(
            "SELECT d.device_id, d.owner_member_id FROM agent_connections c "
            "JOIN devices d ON d.device_id = c.device_id WHERE c.agent_id = ? "
            "ORDER BY c.connected_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if row:
            return row["device_id"], row["owner_member_id"]
        return None, self.member_for_agent(agent_id)

    def member_for_agent(self, agent_id: str) -> str | None:
        """执行体归属的成员：领取时用它判断"这个任务是不是派给我的"。"""

        row = self.db.execute("SELECT owner_member_id FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        return row["owner_member_id"] if row else None

    def _agent_skill_set(self, agent_id: str) -> set[str]:
        """执行体的**技能**集合（只用于任务匹配）。

        技能域 = 能力卡里的 skill ∪ 老写法 `supported_tools` ∪ 心跳/运行时自报里**不属于授权范围前缀**的值。
        授权范围（`task.claim`、`artifact.write`…）是另一套词表，走 `_agent_scope_set`，
        不参与任务匹配——历史上两者被混在一个集合里，导致"声明了 artifact.write 授权"就能满足
        要求 `artifact.write` 的任务（AIP-1a 修的误匹配，见 docs/AIP_1_PLAN.md §3.3）。
        """

        return set(skill_match.card_versions(self._agent_cards(agent_id))) | set(self._agent_skill_names(agent_id))

    def _agent_scope_set(self, agent_id: str) -> set[str]:
        """执行体的**授权范围**（能力令牌域）：设备声明与项目授权，不参与任务匹配。"""

        values: set[str] = set()

        def absorb(raw: Any) -> None:
            if not raw:
                return
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                return
            if isinstance(parsed, dict):
                values.update(str(key) for key, enabled in parsed.items() if enabled)
            elif isinstance(parsed, list):
                values.update(str(item) for item in parsed)

        device = self._agent_device_row(agent_id)
        if device:
            absorb(device["capabilities"])
            runtime = self.db.execute(
                "SELECT capabilities FROM device_runtime_state WHERE device_id = ?", (device["device_id"],)
            ).fetchone()
            if runtime:
                absorb(runtime["capabilities"])
        for row in self.db.execute("SELECT capabilities FROM agent_project_grants WHERE agent_id = ?", (agent_id,)):
            absorb(row["capabilities"])
        return values

    def _agent_skill_names(self, agent_id: str) -> set[str]:
        """非卡片来源的技能名：`supported_tools` 与运行时自报里非授权范围的值。"""

        def names(raw: Any) -> set[str]:
            if not raw:
                return set()
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                return set()
            candidates = list(parsed.keys()) if isinstance(parsed, dict) else list(parsed or [])
            return {
                normalized
                for item in candidates
                for normalized in [skill_match.normalize_skill_id(item)]
                if normalized is not None and not skill_match.is_scope_token(normalized)
            }

        values: set[str] = set()
        agent = self.db.execute("SELECT supported_tools FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if agent:
            values |= names(agent["supported_tools"])
        device = self._agent_device_row(agent_id)
        if device:
            values |= names(device["capabilities"])
            runtime = self.db.execute(
                "SELECT capabilities FROM device_runtime_state WHERE device_id = ?", (device["device_id"],)
            ).fetchone()
            if runtime:
                values |= names(runtime["capabilities"])
        return values

    def _agent_device_row(self, agent_id: str) -> sqlite3.Row | None:
        """该执行体最近连接的那台设备（能力/运行态都挂在设备上）。"""

        return self.db.execute(
            "SELECT d.device_id, d.capabilities FROM agent_connections c JOIN devices d ON d.device_id = c.device_id "
            "WHERE c.agent_id = ? ORDER BY c.connected_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()

    def _agent_cards(self, agent_id: str) -> list[dict[str, Any]]:
        row = self.db.execute("SELECT capability_cards FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if not row:
            return []
        return self._normalized_cards(row["capability_cards"])

    def _agent_skill_versions(self, agent_id: str) -> dict[str, str]:
        """"技能名 → 版本"（卡片版本优先，非卡片来源记为未声明版本）。"""

        versions = skill_match.card_versions(self._agent_cards(agent_id))
        for name in self._agent_skill_names(agent_id):
            versions.setdefault(name, "")
        return versions

    def _agent_capability_set(self, agent_id: str) -> set[str]:
        """兼容别名：历史上技能与授权范围合在一起用，现按技能域收窄（授权范围另见 `_agent_scope_set`）。"""

        return self._agent_skill_set(agent_id)

    def _task_requirements_satisfied(self, task_row: sqlite3.Row, capabilities: set[str] | dict[str, str]) -> bool:
        """任务要求是否被满足：技能名归一化 + 最小版本约束（AIP-1a）。

        `capabilities` 可以是技能名集合（无版本信息）或"技能名 → 版本"映射；
        历史库里写成 `Python` 的要求读时归一化，无需数据回填即可与 `python` 匹配。
        """

        try:
            required = json.loads(task_row["required_capabilities"] or "[]")
        except (TypeError, ValueError):
            required = []
        if not required:
            return True
        provided = capabilities if isinstance(capabilities, dict) else {name: "" for name in capabilities}
        return not skill_match.unsatisfied_requirements(required, provided)

    @staticmethod
    def _deadline_passed(task_row: sqlite3.Row) -> bool:
        raw = task_row["deadline"]
        if not raw:
            return False
        try:
            return parse_time(raw) <= datetime.now(UTC)
        except (TypeError, ValueError):
            return False

    def _assert_dispatch_allows(self, task_row: sqlite3.Row, member_id: str | None) -> None:
        target = task_row["assignee_member_id"]
        if target and target != (member_id or ""):
            raise PermissionError("task_assigned_to_another_member")

    def agent_ids_for_member(self, member_id: str) -> list[str]:
        rows = self.db.execute("SELECT agent_id FROM agents WHERE owner_member_id = ?", (member_id,)).fetchall()
        return [row["agent_id"] for row in rows]

    def my_tasks(self, member_id: str, *, recent_limit: int = 8) -> dict[str, list[dict[str, Any]]]:
        """个人任务中心：指派给我的 / 我的 Agent 正在跑的 / 我的 Agent 最近完成的。

        成员维度的关联有两条：任务上的 assignee_member_id（派单），以及 agents.owner_member_id
        （执行体和成员的关系）——后者是"我的机器在替我干活"的唯一可靠来源。
        """

        member = self.get_member(member_id)
        if member.status != "active":
            raise PermissionError("account_suspended")
        agents = self.agent_ids_for_member(member_id)

        def enrich(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for row in rows:
                task = self._task(row)
                project = self.db.execute("SELECT name FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
                assignee_name = None
                if row["assignee_member_id"]:
                    owner = self.db.execute("SELECT display_name FROM human_members WHERE id = ?", (row["assignee_member_id"],)).fetchone()
                    assignee_name = owner["display_name"] if owner else row["assignee_member_id"]
                items.append(
                    {
                        "task": task,
                        "project_name": project["name"] if project else "",
                        "assignee_member_name": assignee_name,
                        "deadline_passed": self._deadline_passed(row),
                        "executor_agent_id": row["assignee"] if row["status"] in {"CLAIMED", "RUNNING"} else None,
                        "lease_active": bool(
                            self.db.execute(
                                "SELECT 1 FROM task_leases WHERE task_id = ? AND status = 'ACTIVE'", (row["id"],)
                            ).fetchone()
                        ),
                    }
                )
            return items

        open_states = "('READY', 'CLAIMED', 'RUNNING', 'BLOCKED', 'NEEDS_REVISION', 'WAITING_REVIEW')"
        assigned = self.db.execute(
            f"SELECT * FROM tasks t WHERE t.assignee_member_id = ? AND t.status IN {open_states} "
            "ORDER BY CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, t.updated_at ASC",
            (member_id,),
        ).fetchall()

        if agents:
            placeholders = ",".join("?" for _ in agents)
            running = self.db.execute(
                f"SELECT * FROM tasks t WHERE t.assignee IN ({placeholders}) AND t.status IN ('CLAIMED', 'RUNNING') "
                "ORDER BY t.updated_at DESC",
                agents,
            ).fetchall()
            recent = self.db.execute(
                f"SELECT * FROM tasks t WHERE t.assignee IN ({placeholders}) AND t.status IN ('APPROVED', 'FAILED', 'CANCELLED') "
                "ORDER BY t.updated_at DESC LIMIT ?",
                [*agents, recent_limit],
            ).fetchall()
        else:
            running, recent = [], []

        return {
            "assigned": enrich(assigned),
            "running": enrich(running),
            "recent": enrich(recent),
        }

    # 我该复核的角色（与 `authorize_member("review.approve")` 保持一致）
    REVIEW_APPROVER_ROLES = ("owner", "project_lead", "reviewer")

    def my_attention(self, member_id: str) -> dict[str, Any]:
        """桌面通知用：派给我的未结束任务 + 我所在项目里等我复核的任务（DESKTOP-NOTIFY）。

        两个口径都在**我有权看到的项目**内（project_memberships），不跨组织：
          * assigned：`assignee_member_id = 我` 且未结束；"退回给我"（NEEDS_REVISION）也算，
            因为那正是"要你动手"的信号；
          * review_pending：`status = WAITING_REVIEW` 且我在该项目里的角色能 review.approve。
        只返回最小字段（标题/项目/状态/负责人/是否过期），客户端自己去重与限流。
        """

        member = self.get_member(member_id)
        if member.status != "active":
            raise PermissionError("account_suspended")

        roles = {
            row["project_id"]: row["role"]
            for row in self.db.execute("SELECT project_id, role FROM project_memberships WHERE member_id = ?", (member_id,))
        }
        if not roles:
            return {"assigned": [], "review_pending": [], "assigned_total": 0, "review_total": 0, "generated_at": now()}
        placeholders = ",".join("?" for _ in roles)

        def rows_to_items(rows: list[sqlite3.Row], kind: str) -> list[dict[str, Any]]:
            items: list[dict[str, Any]] = []
            for row in rows:
                project = self.db.execute("SELECT name FROM projects WHERE id = ?", (row["project_id"],)).fetchone()
                items.append(
                    {
                        "task_id": UUID(row["id"]),
                        "title": row["title"],
                        "project_id": UUID(row["project_id"]),
                        "project_name": project["name"] if project else "",
                        "status": row["status"],
                        "kind": kind,
                        "executor_agent_id": row["assignee"] if row["status"] in {"CLAIMED", "RUNNING"} else None,
                        "deadline_passed": self._deadline_passed(row),
                    }
                )
            return items

        open_states = "('READY', 'CLAIMED', 'RUNNING', 'BLOCKED', 'NEEDS_REVISION', 'WAITING_REVIEW')"
        assigned_rows = self.db.execute(
            f"SELECT * FROM tasks WHERE assignee_member_id = ? AND status IN {open_states} "
            "ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, updated_at ASC",
            (member_id,),
        ).fetchall()

        approver_projects = [project_id for project_id, role in roles.items() if role in self.REVIEW_APPROVER_ROLES]
        review_rows: list[sqlite3.Row] = []
        if approver_projects:
            review_placeholders = ",".join("?" for _ in approver_projects)
            review_rows = self.db.execute(
                f"SELECT * FROM tasks WHERE project_id IN ({review_placeholders}) AND status = 'WAITING_REVIEW' "
                "ORDER BY updated_at ASC",
                approver_projects,
            ).fetchall()

        assigned = rows_to_items(assigned_rows, "assigned")
        review = rows_to_items(review_rows, "review")
        return {
            "assigned": assigned,
            "review_pending": review,
            "assigned_total": len(assigned),
            "review_total": len(review),
            "generated_at": now(),
        }

    def list_project_members(self, project_id: UUID) -> list[dict[str, Any]]:
        """项目成员目录：派单选择器与成员展示都用它。"""

        rows = self.db.execute(
            """
            SELECT pm.member_id, pm.role, m.display_name, m.email, m.status
            FROM project_memberships pm
            JOIN human_members m ON m.id = pm.member_id
            WHERE pm.project_id = ?
            ORDER BY CASE pm.role WHEN 'owner' THEN 0 WHEN 'project_lead' THEN 1 ELSE 2 END, m.display_name
            """,
            (str(project_id),),
        ).fetchall()
        return [
            {
                "member_id": row["member_id"],
                "role": row["role"],
                "display_name": row["display_name"],
                "email": row["email"],
                "status": row["status"],
            }
            for row in rows
        ]

    def _assert_agent_project_access(self, agent_id: str, project_id: UUID) -> None:
        if not self.db.execute("SELECT 1 FROM agent_project_grants WHERE agent_id = ? AND project_id = ?", (agent_id, str(project_id))).fetchone():
            raise PermissionError("agent_project_access_denied")

    def register_git_repository(self, data: GitRepositoryCreate) -> GitRepository:
        self.get_project(data.project_id)
        root = Path(data.local_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("git_root_must_be_directory")
        head_commit: str | None = None
        try:
            head_commit = LocalGitProvider(root).head()
        except ValueError:
            if data.provider == "local":
                raise
        self.db.execute("INSERT OR REPLACE INTO git_repositories (project_id, provider, remote_url, local_path) VALUES (?, ?, ?, ?)", (str(data.project_id), data.provider, data.remote_url, str(root)))
        self.db.commit()
        return GitRepository(project_id=data.project_id, provider=data.provider, remote_url=data.remote_url, local_path=str(root), head_commit=head_commit)

    def get_git_repository(self, project_id: UUID) -> GitRepository:
        row = self.db.execute("SELECT * FROM git_repositories WHERE project_id = ?", (str(project_id),)).fetchone()
        if not row:
            raise KeyError("git_repository_not_found")
        head_commit: str | None = None
        try:
            head_commit = LocalGitProvider(row["local_path"]).head()
        except ValueError:
            pass
        return GitRepository(project_id=project_id, provider=row["provider"], remote_url=row["remote_url"], local_path=row["local_path"], head_commit=head_commit)

    def index_git_repository(self, project_id: UUID, commit_sha: str | None = None) -> list[GitFileIndex]:
        repository = self.get_git_repository(project_id)
        provider = LocalGitProvider(repository.local_path)
        commit = commit_sha or provider.head()
        indexed_at = now()
        files = provider.index_commit(commit)
        for file in files:
            self.db.execute("INSERT OR REPLACE INTO git_file_indexes (project_id, commit_sha, path, content_hash, size_bytes, indexed_at) VALUES (?, ?, ?, ?, ?, ?)", (str(project_id), commit, file.path, file.content_hash, file.size_bytes, indexed_at))
        self.db.commit()
        return [GitFileIndex(project_id=project_id, commit_sha=commit, path=file.path, content_hash=file.content_hash, size_bytes=file.size_bytes, indexed_at=parse_time(indexed_at)) for file in files]

    def list_git_index(self, project_id: UUID, commit_sha: str | None = None) -> list[GitFileIndex]:
        query = "SELECT * FROM git_file_indexes WHERE project_id = ?"
        params: list[Any] = [str(project_id)]
        if commit_sha:
            query += " AND commit_sha = ?"
            params.append(commit_sha)
        query += " ORDER BY path"
        result = []
        for row in self.db.execute(query, params):
            result.append(GitFileIndex(project_id=UUID(row["project_id"]), commit_sha=row["commit_sha"], path=row["path"], content_hash=row["content_hash"], size_bytes=row["size_bytes"], indexed_at=parse_time(row["indexed_at"])))
        return result

    def list_projects(self) -> list[Project]:
        return [self._project(row) for row in self.db.execute("SELECT * FROM projects ORDER BY updated_at DESC")]

    def list_project_ids(self) -> list[UUID]:
        """所有项目 id（维护循环/聊天兜底扫描用，避免映射整行）。"""

        return [UUID(row["id"]) for row in self.db.execute("SELECT id FROM projects").fetchall()]

    def get_project(self, project_id: UUID) -> Project:
        row = self.db.execute("SELECT * FROM projects WHERE id = ?", (str(project_id),)).fetchone()
        if not row:
            raise KeyError("project_not_found")
        return self._project(row)

    def create_project(self, data: ProjectCreate) -> Project:
        organization_id = data.organization_id or UUID(DEV_ORG_ID)
        team_id = data.team_id or UUID(DEV_TEAM_ID)
        team = self.db.execute("SELECT organization_id FROM teams WHERE id = ?", (str(team_id),)).fetchone()
        member = self.db.execute("SELECT organization_id FROM human_members WHERE id = ?", (data.created_by,)).fetchone()
        if not team or team["organization_id"] != str(organization_id):
            raise ValueError("team_not_in_organization")
        if not member or member["organization_id"] != str(organization_id):
            raise PermissionError("creator_not_in_organization")
        project_id = str(uuid4())
        timestamp = now()
        self.db.execute(
            "INSERT INTO projects (id, organization_id, team_id, created_by, name, competition_pack, problem_code, description, goal, target_member_count, task_mode, stage, progress, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, str(organization_id), str(team_id), data.created_by, data.name, data.competition_pack, data.problem_code, data.description, data.goal or None, data.target_member_count, data.task_mode, "problem_intake", 0, timestamp, timestamp),
        )
        self.db.execute("INSERT INTO project_memberships (project_id, member_id, role, created_at) VALUES (?, ?, 'project_lead', ?)", (project_id, data.created_by, timestamp))
        # 组织内已有成员自动加入新项目（否则"新建项目后老成员看不见"——这是用户明确指出的缺口）。
        # 创建者是 project_lead，其余在职成员按 contributor 入项目；后续可在成员管理里调整。
        for row in self.db.execute(
            "SELECT id FROM human_members WHERE organization_id = ? AND status = 'active' AND id != ?",
            (str(organization_id), data.created_by),
        ).fetchall():
            self.db.execute(
                "INSERT OR IGNORE INTO project_memberships (project_id, member_id, role, created_at) VALUES (?, ?, 'contributor', ?)",
                (project_id, row["id"], timestamp),
            )
        self.db.commit()
        self.add_event(UUID(project_id), "project.created", "member-001", {"name": data.name})
        return self.get_project(UUID(project_id))

    def _task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=UUID(row["id"]),
            project_id=UUID(row["project_id"]),
            title=row["title"],
            description=row["description"],
            stage=row["stage"],
            status=row["status"],
            assignee=row["assignee"],
            assignee_member_id=row["assignee_member_id"],
            priority=row["priority"],
            requires_review=bool(row["requires_review"]),
            allow_future_data=bool(row["allow_future_data"]),
            input_artifacts=self._json(row["input_artifacts"]),
            input_handoff_ids=[UUID(value) for value in self._json(row["input_handoff_ids"] or "[]")],
            output_types=self._json(row["output_types"]),
            parent_task_id=UUID(row["parent_task_id"]) if row["parent_task_id"] else None,
            dependency_task_ids=[UUID(value) for value in self._json(row["dependency_task_ids"] or "[]")],
            acceptance_criteria=self._json(row["acceptance_criteria"] or "[]"),
            required_capabilities=self._json(row["required_capabilities"] or "[]"),
            # 意图对象（AIP-1d）：空预算用 None 表示"不设限"，空证据要求用空列表表示"不要求"
            budget=self._json(row["budget"] or "{}") or None,
            evidence_requirements=self._json(row["evidence_requirements"] or "[]"),
            deadline=parse_time(row["deadline"]) if row["deadline"] else None,
            information_boundary=self._json(row["information_boundary"] or "{}"),
            resource_policy=self._json(row["resource_policy"] or "{}"),
            requires_human_approval=bool(row["requires_human_approval"]),
            blocked_reason=row["blocked_reason"],
            updated_at=parse_time(row["updated_at"]),
        )

    def get_task(self, task_id: UUID) -> Task:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if not row:
            raise KeyError("task_not_found")
        return self._task(row)

    def list_tasks(self, project_id: UUID) -> list[Task]:
        rows = self.db.execute("SELECT * FROM tasks WHERE project_id = ? ORDER BY CASE priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 ELSE 2 END, updated_at DESC", (str(project_id),))
        return [self._task(row) for row in rows]

    def create_task(self, project_id: UUID, data: TaskCreate, *, actor: str | None = None) -> Task:
        self.get_project(project_id)
        self._validate_dispatch_target(project_id, data.assignee_member_id)
        if data.parent_task_id:
            parent = self.get_task(data.parent_task_id)
            if parent.project_id != project_id:
                raise ValueError("parent_task_not_in_project")
        dependency_ids = list(dict.fromkeys(data.dependency_task_ids))
        if data.parent_task_id and data.parent_task_id in dependency_ids:
            raise ValueError("task_parent_dependency_cycle")
        for dependency_id in dependency_ids:
            dependency = self.get_task(dependency_id)
            if dependency.project_id != project_id:
                raise ValueError("dependency_task_not_in_project")
            frontier = list(dependency.dependency_task_ids)
            visited: set[UUID] = set()
            while frontier:
                ancestor = frontier.pop()
                if ancestor in visited:
                    continue
                visited.add(ancestor)
                if ancestor == data.parent_task_id:
                    raise ValueError("task_dependency_parent_cycle")
                if ancestor == dependency_id:
                    raise ValueError("task_dependency_cycle")
                try:
                    frontier.extend(self.get_task(ancestor).dependency_task_ids)
                except KeyError:
                    raise ValueError("dependency_task_not_found") from None
        for handoff_id in data.input_handoff_ids:
            handoff = self.db.execute("SELECT project_id FROM handoffs WHERE id = ?", (str(handoff_id),)).fetchone()
            if not handoff or handoff["project_id"] != str(project_id):
                raise ValueError("input_handoff_not_in_project")
        task_id = str(uuid4())
        timestamp = now()
        requires_human_approval = data.requires_review if data.requires_human_approval is None else data.requires_human_approval
        budget = self._validated_budget(data.budget)
        evidence_requirements = self._validated_evidence_requirements(data.evidence_requirements)
        self.db.execute("INSERT INTO tasks (id, project_id, title, description, stage, status, assignee, assignee_member_id, priority, requires_review, allow_future_data, input_artifacts, input_handoff_ids, output_types, parent_task_id, dependency_task_ids, acceptance_criteria, required_capabilities, budget, evidence_requirements, deadline, information_boundary, resource_policy, requires_human_approval, blocked_reason, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (task_id, str(project_id), data.title, data.description, data.stage, "READY", data.assignee, data.assignee_member_id, data.priority, int(data.requires_review), int(data.allow_future_data), json.dumps(data.input_artifacts), json.dumps([str(value) for value in data.input_handoff_ids]), json.dumps(data.output_types), str(data.parent_task_id) if data.parent_task_id else None, json.dumps([str(value) for value in dependency_ids]), json.dumps(data.acceptance_criteria, ensure_ascii=False), json.dumps(skill_match.normalize_capability_list(data.required_capabilities)), json.dumps(budget, ensure_ascii=False), json.dumps(evidence_requirements, ensure_ascii=False), data.deadline.isoformat() if data.deadline else None, json.dumps(data.information_boundary, ensure_ascii=False), json.dumps(data.resource_policy, ensure_ascii=False), int(requires_human_approval), None, timestamp))
        self.db.commit()
        event_actor = actor or "member-001"
        self.add_event(project_id, "task.created", event_actor, {"task_id": task_id, "title": data.title})
        # 目录族事件（W2.1，只增不改）：老事件名保留给既有消费方，新族供项目流/团队视图用。
        self.add_event(
            project_id,
            "project.task.created",
            event_actor,
            {"task_id": task_id, "title": data.title, "stage": data.stage, "priority": data.priority},
            actor_kind="member" if actor else "system",
            object_type="task",
            object_id=UUID(task_id),
        )
        return self._task(self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())

    @staticmethod
    def _validated_budget(budget: Any) -> dict[str, Any]:
        """预算落库前的规范化（空 = 不设限）。维度取值由 `TaskBudget` 契约把关，这里只做形状检查。"""

        if budget is None:
            return {}
        values = budget if isinstance(budget, dict) else getattr(budget, "model_dump", lambda: {})()
        cleaned = {key: int(value) for key, value in values.items() if value is not None and key in {"max_seconds", "max_attempts", "max_tokens"}}
        return cleaned

    @staticmethod
    def _validated_evidence_requirements(items: Any) -> list[dict[str, Any]]:
        """证据要求落库前的规范化：类型必须在既有 Evidence 词表内，数量取正整数。"""

        allowed = {"artifact", "run", "event", "external_source"}
        result: list[dict[str, Any]] = []
        for item in items or []:
            values = item if isinstance(item, dict) else getattr(item, "model_dump", lambda: {})()
            kind = str(values.get("evidence_type") or "").strip()
            if kind not in allowed:
                raise ValueError("evidence_type_invalid")
            count = int(values.get("min_count") or 1)
            if count < 1 or count > 20:
                raise ValueError("evidence_min_count_invalid")
            result.append({"evidence_type": kind, "min_count": count, "note": str(values.get("note") or "")})
        return result

    def project_task_flags(self, project_id: UUID) -> list[dict[str, Any]]:
        """项目内所有任务的轻量标记（读时聚合）：证据缺口条数、用量是否超预算。

        两次分组查询（证据按类型计数、用量按任务汇总）+ 一次任务列表，避免逐条任务查库。
        只返回"有要求或有回报"的任务——大多数任务没有这两样，不必占位。
        """

        evidence_counts: dict[str, int] = {}
        for row in self.db.execute(
            "SELECT evidence_type, COUNT(*) AS c FROM evidence WHERE project_id = ? GROUP BY evidence_type", (str(project_id),)
        ):
            evidence_counts[str(row["evidence_type"])] = int(row["c"] or 0)
        usage_by_task: dict[str, dict[str, Any]] = {}
        for row in self.db.execute(
            "SELECT r.task_id, r.usage FROM runs r JOIN tasks t ON t.id = r.task_id WHERE t.project_id = ?", (str(project_id),)
        ):
            usage = self._json(row["usage"] or "{}")
            if not usage:
                continue
            bucket = usage_by_task.setdefault(str(row["task_id"]), {"tokens": 0, "reported": 0, "max_seconds": None})
            if isinstance(usage.get("total_tokens"), int):
                bucket["tokens"] += int(usage["total_tokens"])
                bucket["reported"] += 1
            if isinstance(usage.get("seconds"), (int, float)):
                bucket["max_seconds"] = max(bucket["max_seconds"] or 0.0, float(usage["seconds"]))

        flags: list[dict[str, Any]] = []
        for row in self.db.execute(
            "SELECT id, budget, evidence_requirements FROM tasks WHERE project_id = ?", (str(project_id),)
        ).fetchall():
            requirements = self._json(row["evidence_requirements"] or "[]")
            missing = 0
            for requirement in requirements:
                kind = str(requirement.get("evidence_type") or "")
                required = int(requirement.get("min_count") or 1)
                missing += max(0, required - evidence_counts.get(kind, 0))
            usage = usage_by_task.get(row["id"], {"tokens": 0, "reported": 0, "max_seconds": None})
            budget = self._json(row["budget"] or "{}")
            overrun = False
            if budget.get("max_tokens") and usage["tokens"] > int(budget["max_tokens"]):
                overrun = True
            if budget.get("max_seconds") and usage["max_seconds"] is not None:
                limit = int(budget["max_seconds"])
                if usage["max_seconds"] > max(limit * 1.1, limit + 30):
                    overrun = True
            if not missing and not overrun:
                continue
            flags.append(
                {
                    "task_id": row["id"],
                    "evidence_missing": missing,
                    "usage_overrun": overrun,
                    "usage_reported": usage["reported"] > 0,
                }
            )
        return flags

    def task_evidence_gaps(self, task_id: UUID) -> list[dict[str, Any]]:
        """任务的证据缺口（读时算，不落库）：要求哪类证据、已有几条、还缺几条。

        证据来源是项目内的 Evidence 记录（`evidence_type` 维度），与门禁用的是同一份数据。
        """

        row = self.db.execute("SELECT project_id, evidence_requirements FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if not row:
            raise KeyError("task_not_found")
        requirements = self._json(row["evidence_requirements"] or "[]")
        if not requirements:
            return []
        counts: dict[str, int] = {}
        for item in self.db.execute(
            "SELECT evidence_type, COUNT(*) AS c FROM evidence WHERE project_id = ? GROUP BY evidence_type",
            (row["project_id"],),
        ):
            counts[str(item["evidence_type"])] = int(item["c"] or 0)
        gaps: list[dict[str, Any]] = []
        for requirement in requirements:
            kind = str(requirement.get("evidence_type") or "")
            required = int(requirement.get("min_count") or 1)
            present = counts.get(kind, 0)
            gaps.append(
                {
                    "evidence_type": kind,
                    "required": required,
                    "present": present,
                    "missing": max(0, required - present),
                    "note": str(requirement.get("note") or ""),
                }
            )
        return gaps

    def _task_budget_state(self, task_row: sqlite3.Row) -> dict[str, Any]:
        """任务的预算执行态：已领取次数与是否超限（用于领取前判定与界面展示）。"""

        budget = self._json(task_row["budget"] or "{}")
        attempts = int(
            self.db.execute("SELECT COUNT(*) AS c FROM task_leases WHERE task_id = ?", (task_row["id"],)).fetchone()["c"] or 0
        )
        max_attempts = budget.get("max_attempts")
        # 累计用量：把该任务所有 Run 的 usage 加起来（有回报的才有数，没回报的不假装是 0）
        tokens_used = 0
        reported_runs = 0
        for row in self.db.execute("SELECT usage FROM runs WHERE task_id = ?", (task_row["id"],)):
            usage = self._json(row["usage"] or "{}")
            total = usage.get("total_tokens")
            if isinstance(total, int):
                tokens_used += total
                reported_runs += 1
        return {
            "attempts": attempts,
            "max_attempts": int(max_attempts) if max_attempts else None,
            "exhausted": bool(max_attempts) and attempts >= int(max_attempts),
            "max_seconds": int(budget["max_seconds"]) if budget.get("max_seconds") else None,
            "max_tokens": int(budget["max_tokens"]) if budget.get("max_tokens") else None,
            "tokens_used": tokens_used,
            "usage_reported_runs": reported_runs,
        }

    def update_task(
        self,
        task_id: UUID,
        status: TaskStatus | None,
        assignee: str | None,
        blocked_reason: str | None,
        actor: str = "member-001",
        actor_kind: str = "member",
        resource_policy: dict[str, Any] | None = None,
        assignee_member_id: str | None = None,
        dispatch_provided: bool = False,
        deadline: str | None = None,
        deadline_provided: bool = False,
        budget: Any = None,
        budget_provided: bool = False,
        evidence_requirements: Any = None,
        evidence_provided: bool = False,
    ) -> Task:
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if not row:
            raise KeyError("task_not_found")
        if dispatch_provided:
            # 空串 = 取消指派（回到"谁先轮到谁跑"）；有值则必须是项目成员
            target = (assignee_member_id or "").strip() or None
            project_id = UUID(row["project_id"])
            self._validate_dispatch_target(project_id, target)
            # W-2 派单口径：队长可派/改派/取消；成员只能在非 manual 模式下认领无人任务或释放自己的。
            # 例外：调度器（platform-auto）不是成员，auto 模式的自动派单由它自己决定目标。
            if actor == self.AUTO_ACTOR and actor_kind == "system":
                action = "auto_assigned"
            else:
                action = self._dispatch_policy(project_id, row, actor, target)
            previous = row["assignee_member_id"]
            self.db.execute("UPDATE tasks SET assignee_member_id = ?, updated_at = ? WHERE id = ?", (target, now(), str(task_id)))
            self.db.commit()
            row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
            # 派单要能在群聊/时间线里看见（"这条怎么突然归他了"）
            self.add_event(
                project_id,
                "task.dispatched",
                actor,
                {
                    "task_id": str(task_id),
                    "assignee_member_id": target,
                    "previous_assignee_member_id": previous,
                    "action": action,
                },
                actor_kind="member",
                object_type="task",
                object_id=task_id,
            )
        if deadline_provided:
            # 空串 = 清除截止时间；有值必须是可解析的时间
            target_deadline = (deadline or "").strip() or None
            if target_deadline:
                parse_time(target_deadline)  # 解析失败会抛错，交给路由层映射成 409
            self.db.execute("UPDATE tasks SET deadline = ? WHERE id = ?", (target_deadline, str(task_id)))
            self.db.commit()
            row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if budget_provided:
            # 意图对象（AIP-1d）：null / {} = 清除预算；有值则按维度规范化后写入
            cleaned = self._validated_budget(budget)
            self.db.execute(
                "UPDATE tasks SET budget = ?, updated_at = ? WHERE id = ?",
                (json.dumps(cleaned, ensure_ascii=False), now(), str(task_id)),
            )
            self.db.commit()
            row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if evidence_provided:
            cleaned_requirements = self._validated_evidence_requirements(evidence_requirements)
            self.db.execute(
                "UPDATE tasks SET evidence_requirements = ?, updated_at = ? WHERE id = ?",
                (json.dumps(cleaned_requirements, ensure_ascii=False), now(), str(task_id)),
            )
            self.db.commit()
            row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        new_status = status.value if status else row["status"]
        new_assignee = assignee if assignee is not None else row["assignee"]
        current_status = row["status"]
        if status and not self._task_transition_allowed(current_status, new_status):
            raise ValueError("invalid_task_transition")
        if status and new_status == "APPROVED":
            raise PermissionError("task_approval_requires_review")
        if status and new_status == "WAITING_REVIEW" and actor_kind not in {"member", "agent", "system"}:
            raise PermissionError("invalid_actor_kind")
        policy = self._validated_resource_policy(resource_policy) if resource_policy is not None else self._json(row["resource_policy"] or "{}")
        self.db.execute(
            "UPDATE tasks SET status = ?, assignee = ?, blocked_reason = ?, resource_policy = ?, updated_at = ? WHERE id = ?",
            (new_status, new_assignee, blocked_reason, json.dumps(policy, ensure_ascii=False), now(), str(task_id)),
        )
        self.db.commit()
        payload: dict[str, Any] = {"task_id": str(task_id), "status": new_status, "actor_kind": actor_kind}
        if resource_policy is not None:
            # 执行方式变化要能在时间线里看见（"为什么这个任务突然开始跑了"）
            payload["resource_policy"] = policy
        self.add_event(UUID(row["project_id"]), "task.updated", actor, payload)
        return self._task(self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone())

    @staticmethod
    def _validated_resource_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
        """校验任务执行方式。只允许平台真正支持的取值，避免"配了但跑不起来"。

        历史事件：授权串缺能力会 403、任务没有执行方式会 `executor_not_configured`——
        这两种失败都发生在 Agent 侧，用户在界面上看不到原因，所以入口处就该挡住明显错的配置。
        """

        if not isinstance(policy, dict):
            raise ValueError("resource_policy_invalid")
        executor = str(policy.get("worker_executor") or "").strip().lower()
        # codex = 协议适配（JSONL 事件）；cli = 通用提示词驱动（命令模板 + {prompt} 占位，
        # stdout 即结果——语义同声明式命令，只是提示词来自任务而不是写死在 argv 里）。
        if executor and executor not in {"codex", "cli"}:
            raise ValueError(f"unsupported_worker_executor:{executor}")
        # 事件协议（可选）：显式声明 stdout 按哪种协议解析。不填也能跑——cli 的执行体若在
        # Agent 侧已知（如 opencode），Agent 会按命令模板首词自行判定；声明与推断不一致时以声明为准。
        events = str(policy.get("worker_events") or "").strip().lower()
        if events and events not in {"codex", "opencode", "none"}:
            raise ValueError(f"unsupported_worker_events:{events}")
        if events == "codex" and executor != "codex":
            raise ValueError("worker_events_codex_requires_codex_executor")
        if events == "opencode" and executor != "cli":
            raise ValueError("worker_events_opencode_requires_cli_executor")
        command = policy.get("worker_command")
        if command is not None:
            if isinstance(command, str):
                command = [command]
            if not isinstance(command, (list, tuple)) or not command or not all(str(item).strip() for item in command):
                raise ValueError("worker_command_invalid")
        if executor == "cli":
            # 模板里必须有 {prompt} 占位符：没有它就等于声明式命令，该用 command 路径而不是 cli
            template = command if isinstance(command, (list, tuple)) else ([command] if command else [])
            if not template:
                raise ValueError("cli_executor_requires_command_template")
            if not any("{prompt}" in str(item) for item in template):
                raise ValueError("cli_executor_template_requires_prompt_placeholder")
        if not executor and command is None:
            raise ValueError("task_executor_required")
        return policy

    @staticmethod
    def _task_transition_allowed(current: str, target: str) -> bool:
        allowed = {
            "DRAFT": {"READY", "CANCELLED"},
            "READY": {"CLAIMED", "RUNNING", "BLOCKED", "CANCELLED"},
            "CLAIMED": {"RUNNING", "READY", "BLOCKED", "FAILED", "CANCELLED"},
            "RUNNING": {"WAITING_REVIEW", "APPROVED", "BLOCKED", "FAILED", "CANCELLED"},
            "WAITING_REVIEW": {"APPROVED", "NEEDS_REVISION", "BLOCKED"},
            "NEEDS_REVISION": {"READY", "CLAIMED", "CANCELLED"},
            "BLOCKED": {"READY", "CANCELLED"},
            "FAILED": {"READY", "CANCELLED"},
            "APPROVED": set(),
            "CANCELLED": set(),
        }
        return current == target or target in allowed.get(current, set())

    def _operation(self, key: str, operation: str, request_hash: str | None = None) -> Any | None:
        row = self.db.execute("SELECT operation, response, request_hash FROM idempotency_records WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        if row["operation"] != operation:
            raise ValueError("idempotency_key_reused_for_different_operation")
        if request_hash and row["request_hash"] and row["request_hash"] != request_hash:
            raise ValueError("idempotency_key_reused_for_different_request")
        return json.loads(row["response"])

    @staticmethod
    def _request_hash(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _save_operation(self, key: str, operation: str, response: Any, request_hash: str | None = None, *, commit: bool = True) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO idempotency_records (key, operation, response, request_hash, created_at) VALUES (?, ?, ?, ?, ?)",
            (key, operation, json.dumps(response, ensure_ascii=False), request_hash, now()),
        )
        if commit:
            self.db.commit()

    def get_idempotent_response(self, key: str, operation: str, request_hash: str | None = None) -> Any | None:
        """Return a previously stored HTTP response after validating its request identity."""
        return self._operation(key, operation, request_hash)

    def save_idempotent_response(self, key: str, operation: str, response: Any, request_hash: str | None = None) -> None:
        """Persist an HTTP response for safe client retries."""
        self._save_operation(key, operation, response, request_hash)

    def _lease(self, row: sqlite3.Row) -> TaskLease:
        return TaskLease(
            lease_id=UUID(row["id"]),
            task_id=UUID(row["task_id"]),
            project_id=UUID(row["project_id"]),
            agent_id=row["agent_id"],
            # 归属列容错：个别查询（恢复/显式列清单）可能没带这两列
            device_id=row["device_id"] if "device_id" in row.keys() else None,
            member_id=row["member_id"] if "member_id" in row.keys() else None,
            lease_token=row["lease_token"],
            status=row["status"],
            issued_at=parse_time(row["issued_at"]),
            expires_at=parse_time(row["expires_at"]),
        )

    def _expire_leases(self) -> None:
        self.db.execute("UPDATE task_leases SET status = 'EXPIRED' WHERE status = 'ACTIVE' AND expires_at <= ?", (now(),))
        self.db.commit()

    def _active_lease_for_task(self, task_id: UUID) -> sqlite3.Row | None:
        self._expire_leases()
        return self.db.execute("SELECT * FROM task_leases WHERE task_id = ? AND status = 'ACTIVE' ORDER BY issued_at DESC LIMIT 1", (str(task_id),)).fetchone()

    def _validate_agent(self, agent_id: str) -> None:
        if not self.db.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone():
            raise KeyError("agent_not_found")

    def _task_dependencies_ready(self, task: Task) -> bool:
        for dependency_id in task.dependency_task_ids:
            row = self.db.execute("SELECT status, project_id FROM tasks WHERE id = ?", (str(dependency_id),)).fetchone()
            if not row or row["project_id"] != str(task.project_id) or row["status"] != "APPROVED":
                return False
        for handoff_id in task.input_handoff_ids:
            row = self.db.execute("SELECT receipt_status, status, requires_human_approval, project_id FROM handoffs WHERE id = ?", (str(handoff_id),)).fetchone()
            gate = self.db.execute("SELECT * FROM gates WHERE project_id = ? AND target_type = 'handoff' AND target_id = ?", (str(task.project_id), str(handoff_id))).fetchone()
            if gate:
                gate = self._refresh_gate_if_stale(gate)
            gate_ready = not row or not bool(row["requires_human_approval"]) or (gate and gate["status"] == "PASSED")
            if not row or row["project_id"] != str(task.project_id) or row["receipt_status"] != "ACCEPTED" or row["status"] not in {"PASS", "PASS_WITH_ASSUMPTIONS"} or not gate_ready:
                return False
        for artifact_id in task.input_artifacts:
            row = self.db.execute("SELECT status, downstream_allowed, project_id FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
            if not row or row["project_id"] != str(task.project_id) or row["status"] != "APPROVED" or not bool(row["downstream_allowed"]):
                return False
        return True

    def _assert_task_claimable_inputs(self, task: Task) -> None:
        if not self._task_dependencies_ready(task):
            raise ValueError("task_dependencies_or_inputs_not_approved")

    def _claim_result_from_operation(self, payload: dict[str, Any]) -> tuple[Task, TaskLease]:
        return self.get_task(UUID(payload["task"]["id"])), TaskLease(**payload["lease"])

    def claim_task(self, task_id: UUID, data: TaskClaimRequest) -> tuple[Task, TaskLease]:
        self._validate_agent(data.agent_id)
        previous = self._operation(data.idempotency_key, "task.claim")
        if previous:
            return self._claim_result_from_operation(previous)
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        if not row:
            raise KeyError("task_not_found")
        self._assert_agent_project_access(data.agent_id, UUID(row["project_id"]))
        self._assert_dispatch_allows(row, self.member_for_agent(data.agent_id))
        if not self._task_requirements_satisfied(row, self._agent_skill_versions(data.agent_id)):
            raise PermissionError("task_capabilities_not_satisfied")
        if self._deadline_passed(row):
            raise ValueError("task_deadline_passed")
        if row["status"] not in {"READY", "NEEDS_REVISION"}:
            raise ValueError("task_not_claimable")
        self._assert_task_claimable_inputs(self.get_task(task_id))
        active = self._active_lease_for_task(task_id)
        if active:
            raise ValueError("task_already_leased")
        # 意图对象（AIP-1d）：预算强制两项。**这里是唯一咽喉**——claim_next_task 也走本方法。
        budget_state = self._task_budget_state(row)
        if budget_state["exhausted"]:
            self._note_budget_exhausted(UUID(row["project_id"]), task_id, budget_state)
            raise ValueError("task_budget_exhausted")
        issued = datetime.now(UTC)
        lease_seconds = data.lease_seconds
        if budget_state["max_seconds"]:
            # 墙钟上限：不新增计时器，直接把租约到期时间压到预算以内（心跳可续，但不能越过上限）
            lease_seconds = min(lease_seconds, budget_state["max_seconds"])
        expires = issued + timedelta(seconds=lease_seconds)
        lease_id = uuid4()
        lease = TaskLease(lease_id=lease_id, task_id=task_id, project_id=UUID(row["project_id"]), agent_id=data.agent_id, lease_token=uuid4().hex, status="ACTIVE", issued_at=issued, expires_at=expires)
        self.db.execute("INSERT INTO task_leases VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (str(lease.lease_id), str(task_id), str(lease.project_id), data.agent_id, lease.lease_token, lease.status, lease.issued_at.isoformat(), lease.expires_at.isoformat(), lease.issued_at.isoformat()))
        self.db.execute("UPDATE tasks SET status = 'CLAIMED', assignee = ?, updated_at = ? WHERE id = ?", (data.agent_id, now(), str(task_id)))
        self.db.commit()
        self.add_event(
            lease.project_id,
            "task.claimed",
            data.agent_id,
            {"task_id": str(task_id), "lease_id": str(lease.lease_id), "expires_at": lease.expires_at.isoformat()},
            actor_kind="agent",
        )
        task = self.get_task(task_id)
        payload = {"task": task.model_dump(mode="json"), "lease": lease.model_dump(mode="json")}
        self._save_operation(data.idempotency_key, "task.claim", payload)
        return task, lease

    def _note_budget_exhausted(self, project_id: UUID, task_id: UUID, state: dict[str, Any]) -> Event | None:
        """预算用尽时写**一次性**事件（幂等键，重复触发不会再刷屏）；本次真的写了才返回事件。

        为什么要事件：`max_attempts` 用尽后任务会"谁也不动"，只有告警可见，
        队长才知道该改预算或放弃这条任务（同 `task.auto_unmatched` 的既有做法）。
        """

        key = f"budget-exhausted:{task_id}"
        if self.db.execute("SELECT 1 FROM events WHERE project_id = ? AND idempotency_key = ?", (str(project_id), key)).fetchone():
            return None
        return self.add_event(
            project_id,
            "task.budget_exhausted",
            self.AUTO_ACTOR,
            {"task_id": str(task_id), "attempts": state["attempts"], "max_attempts": state["max_attempts"]},
            actor_kind="system",
            object_type="task",
            object_id=task_id,
            idempotency_key=key,
        )

    def _normalized_usage(self, data: RunComplete, run_row: sqlite3.Row, completed: str) -> dict[str, Any]:
        """规范化执行用量：token 取执行体回报，耗时优先自报、缺省用平台观测的 started_at/completed_at。

        `source` 记出处：只有执行体回报过 token 才写它的来源（如 `codex-jsonl`），
        平台兜底的耗时统一记 `platform-observed`——让人能分辨"回报"与"观测"。
        """

        reported = data.usage.model_dump(exclude_none=True) if getattr(data, "usage", None) is not None else {}
        usage: dict[str, Any] = {}
        for key in ("input_tokens", "output_tokens", "total_tokens", "turns"):
            value = reported.get(key)
            if isinstance(value, int):
                usage[key] = value
        if "total_tokens" not in usage and ("input_tokens" in usage or "output_tokens" in usage):
            usage["total_tokens"] = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
        # 两个来源分开记：token 的出处与耗时的出处不是一回事，混成一个字段会看不出谁在说话
        usage["source"] = str(reported.get("source") or ("agent-reported" if "total_tokens" in usage else "platform-observed"))
        seconds = reported.get("seconds")
        if isinstance(seconds, (int, float)):
            usage["seconds"] = round(float(seconds), 2)
            usage["seconds_source"] = "agent"
        else:
            started = parse_time(run_row["started_at"])
            usage["seconds"] = round(max(0.0, (parse_time(completed) - started).total_seconds()), 2)
            usage["seconds_source"] = "platform"
        return usage

    @staticmethod
    def _usage_overruns(task_row: sqlite3.Row | None, usage: dict[str, Any]) -> list[dict[str, Any]]:
        """这次执行的用量是否超了任务预算（返回超限项，空 = 没超或没预算）。"""

        if task_row is None:
            return []
        raw_budget = task_row["budget"]
        if isinstance(raw_budget, (str, bytes)):
            try:
                budget = json.loads(raw_budget or "{}")
            except (TypeError, ValueError):
                budget = {}
        else:
            budget = raw_budget or {}
        if not isinstance(budget, dict):
            budget = {}
        overruns: list[dict[str, Any]] = []
        max_tokens = budget.get("max_tokens")
        total_tokens = usage.get("total_tokens")
        if max_tokens and isinstance(total_tokens, int) and total_tokens > int(max_tokens):
            overruns.append({"code": "tokens", "reported": total_tokens, "limit": int(max_tokens)})
        max_seconds = budget.get("max_seconds")
        seconds = usage.get("seconds")
        # 允许 10% 或 30 秒的余量：进程收尾、上报延迟不该被判成超时
        if max_seconds and isinstance(seconds, (int, float)) and float(seconds) > max(int(max_seconds) * 1.1, int(max_seconds) + 30):
            overruns.append({"code": "seconds", "reported": round(float(seconds), 1), "limit": int(max_seconds)})
        return overruns

    def _note_budget_exceeded(self, project_id: UUID, task_id: str | None, run_id: UUID, overruns: list[dict[str, Any]]) -> Event | None:
        """用量超预算的一次性事件（按 Run 幂等）：同一 Run 重复上报不会重复刷屏。"""

        if not task_id:
            return None
        key = f"budget-exceeded:{run_id}"
        if self.db.execute("SELECT 1 FROM events WHERE project_id = ? AND idempotency_key = ?", (str(project_id), key)).fetchone():
            return None
        return self.add_event(
            project_id,
            "task.budget_exceeded",
            self.AUTO_ACTOR,
            {"task_id": str(task_id), "run_id": str(run_id), "overruns": overruns},
            actor_kind="system",
            object_type="task",
            object_id=UUID(str(task_id)),
            idempotency_key=key,
        )

    def task_usage_summary(self, task_id: UUID) -> dict[str, Any]:
        """任务已回报的用量汇总（读时算）：次数、tokens、最近一次来源。给界面与门禁共用。"""

        rows = self.db.execute(
            "SELECT usage, agent_id, completed_at FROM runs WHERE task_id = ? ORDER BY started_at DESC", (str(task_id),)
        ).fetchall()
        tokens = 0
        reported = 0
        latest: dict[str, Any] = {}
        max_seconds: float | None = None
        for row in rows:
            usage = self._json(row["usage"] or "{}")
            if not usage:
                continue
            if isinstance(usage.get("total_tokens"), int):
                tokens += int(usage["total_tokens"])
                reported += 1
            if isinstance(usage.get("seconds"), (int, float)):
                max_seconds = max(max_seconds or 0.0, float(usage["seconds"]))
            if not latest:
                latest = {**usage, "agent_id": row["agent_id"], "completed_at": row["completed_at"]}
        return {"runs_total": len(rows), "usage_reported_runs": reported, "tokens_used": tokens, "longest_seconds": max_seconds, "latest": latest}

    def claim_next_task(self, data: TaskClaimRequest, project_id: UUID | None = None, stages: list[str] | None = None) -> tuple[Task, TaskLease] | None:
        self._validate_agent(data.agent_id)
        self._expire_leases()
        clauses = ["t.status IN ('READY', 'NEEDS_REVISION')", "NOT EXISTS (SELECT 1 FROM task_leases l WHERE l.task_id = t.id AND l.status = 'ACTIVE')"]
        params: list[Any] = []
        # 派单：只领"指派给我"或"未指派"的；未指派保持原有的先到先得语义
        claiming_member = self.member_for_agent(data.agent_id)
        clauses.append("(t.assignee_member_id IS NULL OR t.assignee_member_id = ?)")
        params.append(claiming_member or "")
        # 已过截止时间的任务不再自动领取（需要人工改期或取消，否则会被一台台机器反复领走再失败）
        clauses.append("(t.deadline IS NULL OR t.deadline > ?)")
        params.append(datetime.now(UTC).isoformat())
        if project_id:
            self._assert_agent_project_access(data.agent_id, project_id)
            clauses.append("t.project_id = ?")
            params.append(str(project_id))
        if stages:
            placeholders = ",".join("?" for _ in stages)
            clauses.append(f"t.stage IN ({placeholders})")
            params.extend(stages)
        # 排序：有截止时间的优先（最早在前），再按优先级与更新时间
        rows = self.db.execute(
            f"SELECT t.* FROM tasks t WHERE {' AND '.join(clauses)} "
            "ORDER BY (t.deadline IS NULL), t.deadline ASC, "
            "CASE t.priority WHEN 'critical' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, t.updated_at ASC",
            params,
        ).fetchall()
        capabilities = self._agent_skill_versions(data.agent_id)
        for row in rows:
            # 能力不满足的任务直接跳过：领下来必然失败，白耗一次执行
            if not self._task_requirements_satisfied(row, capabilities):
                continue
            # 预算用尽的也跳过（不是整体失败）：点名的 claim_task 会明确报 task_budget_exhausted，
            # 轮询则安静地换下一条，避免一条卡死的任务把整轮领取都堵住
            if self._task_budget_state(row)["exhausted"]:
                continue
            task = self.get_task(UUID(row["id"]))
            if not self._task_dependencies_ready(task):
                continue
            return self.claim_task(task.id, data)
        return None

    def heartbeat_lease(self, lease_token: str, agent_id: str, extend_seconds: int, project_id: UUID | None = None) -> TaskLease:
        row = self.db.execute("SELECT * FROM task_leases WHERE lease_token = ?", (lease_token,)).fetchone()
        if not row:
            raise KeyError("lease_not_found")
        if row["agent_id"] != agent_id:
            raise PermissionError("lease_agent_mismatch")
        if project_id is not None and row["project_id"] != str(project_id):
            raise PermissionError("lease_project_mismatch")
        if row["status"] != "ACTIVE" or parse_time(row["expires_at"]) <= datetime.now(UTC):
            self.db.execute("UPDATE task_leases SET status = 'EXPIRED' WHERE id = ?", (row["id"],))
            self.db.commit()
            raise ValueError("lease_expired")
        expires = datetime.now(UTC) + timedelta(seconds=extend_seconds)
        # 预算的墙钟上限对**续租**同样成立：不能靠反复心跳把 max_seconds 绕过去
        task_row = self.db.execute("SELECT budget FROM tasks WHERE id = ?", (row["task_id"],)).fetchone()
        budget = self._json(task_row["budget"] or "{}") if task_row else {}
        if budget.get("max_seconds"):
            ceiling = parse_time(row["issued_at"]) + timedelta(seconds=int(budget["max_seconds"]))
            if expires > ceiling:
                expires = ceiling
            if expires <= datetime.now(UTC):
                self.db.execute("UPDATE task_leases SET status = 'EXPIRED' WHERE id = ?", (row["id"],))
                self.db.commit()
                raise ValueError("lease_budget_exhausted")
        self.db.execute("UPDATE task_leases SET expires_at = ?, last_heartbeat = ? WHERE id = ?", (expires.isoformat(), now(), row["id"]))
        self.db.commit()
        return self._lease(self.db.execute("SELECT * FROM task_leases WHERE id = ?", (row["id"],)).fetchone())

    def _lease_for_task(self, task_id: UUID, agent_id: str, token: str) -> sqlite3.Row:
        self._expire_leases()
        row = self.db.execute("SELECT * FROM task_leases WHERE task_id = ? AND lease_token = ?", (str(task_id), token)).fetchone()
        if not row:
            raise KeyError("lease_not_found")
        if row["agent_id"] != agent_id:
            raise PermissionError("lease_agent_mismatch")
        if row["status"] != "ACTIVE":
            raise ValueError("lease_not_active")
        return row

    def update_task_progress(self, task_id: UUID, data: TaskProgressRequest) -> Task:
        previous = self._operation(data.idempotency_key, "task.progress")
        if previous:
            return self.get_task(UUID(previous["task_id"]))
        lease = self._lease_for_task(task_id, data.agent_id, data.lease_token)
        task = self.get_task(task_id)
        current_status = task.status.value if isinstance(task.status, TaskStatus) else str(task.status)
        if not self._task_transition_allowed(current_status, data.status):
            raise ValueError("invalid_task_transition")
        self.db.execute("UPDATE tasks SET status = ?, blocked_reason = ?, updated_at = ? WHERE id = ?", (data.status, data.message if data.status == "BLOCKED" else None, now(), str(task_id)))
        self.db.commit()
        self.add_event(UUID(lease["project_id"]), "task.progress", data.agent_id, {"task_id": str(task_id), "status": data.status, "message": data.message}, actor_kind="agent")
        updated = self.get_task(task_id)
        self._save_operation(data.idempotency_key, "task.progress", {"task_id": str(task_id)})
        return updated

    def submit_task_result(self, task_id: UUID, data: TaskResultSubmit) -> TaskResult:
        previous = self._operation(data.idempotency_key, "task.result")
        if previous:
            return TaskResult(task=Task(**previous["task"]), lease=TaskLease(**previous["lease"]), accepted=previous["accepted"], message=previous.get("message", ""))
        lease_row = self._lease_for_task(task_id, data.agent_id, data.lease_token)
        task = self.get_task(task_id)
        for artifact_id in data.output_artifact_ids:
            artifact = self.db.execute("SELECT project_id FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
            if not artifact or artifact["project_id"] != str(task.project_id):
                raise ValueError("output_artifact_not_in_project")
        if data.handoff_id:
            handoff = self.db.execute("SELECT project_id, task_id FROM handoffs WHERE id = ?", (str(data.handoff_id),)).fetchone()
            if not handoff or handoff["project_id"] != str(task.project_id) or handoff["task_id"] != str(task_id):
                raise ValueError("handoff_not_attached_to_task")
        target_status = "WAITING_REVIEW" if data.success and (task.requires_review or task.requires_human_approval) else ("APPROVED" if data.success else "FAILED")
        self.db.execute("UPDATE tasks SET status = ?, updated_at = ?, blocked_reason = NULL WHERE id = ?", (target_status, now(), str(task_id)))
        self.db.execute("UPDATE task_leases SET status = 'RELEASED', last_heartbeat = ? WHERE id = ?", (now(), lease_row["id"]))
        self.db.commit()
        self.add_event(
            task.project_id,
            "task.result_submitted",
            data.agent_id,
            {
                "task_id": str(task_id),
                "status": target_status,
                "output_artifact_ids": [str(value) for value in data.output_artifact_ids],
                "handoff_id": str(data.handoff_id) if data.handoff_id else None,
            },
            actor_kind="agent",
        )
        result = TaskResult(task=self.get_task(task_id), lease=self._lease(self.db.execute("SELECT * FROM task_leases WHERE id = ?", (lease_row["id"],)).fetchone()), accepted=True, message=data.summary)
        self._save_operation(data.idempotency_key, "task.result", result.model_dump(mode="json"))
        return result

    def _handoff_receipt(self, row: sqlite3.Row) -> HandoffReceipt:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["handoff_id"] = UUID(values["handoff_id"])
        values["decision_findings"] = self._json(values.get("decision_findings") or "[]")
        values["created_at"] = parse_time(values["created_at"])
        values["received_at"] = parse_time(values["received_at"]) if values.get("received_at") else None
        return HandoffReceipt(**values)

    def _list_handoff_receipts(self, handoff_id: UUID) -> list[HandoffReceipt]:
        rows = self.db.execute("SELECT * FROM handoff_receipts WHERE handoff_id = ? ORDER BY created_at ASC, receiver_id ASC", (str(handoff_id),)).fetchall()
        return [self._handoff_receipt(row) for row in rows]

    @staticmethod
    def _handoff_recipient_entries(receiver: Any, handoff_type: str) -> list[tuple[str, str]]:
        if handoff_type != "FANOUT":
            if isinstance(receiver, str):
                return [("team", receiver)]
            if isinstance(receiver, dict):
                receiver_type = str(receiver.get("type") or receiver.get("kind") or "")
                receiver_id = receiver.get("id")
                if receiver_type in {"agent", "member", "team", "agent_group"} and isinstance(receiver_id, str) and receiver_id:
                    return [(receiver_type, receiver_id)]
            raise ValueError("invalid_handoff_receiver")
        if isinstance(receiver, dict):
            receiver_type = str(receiver.get("type") or receiver.get("kind") or "")
            ids = receiver.get("ids")
            if receiver_type == "agents":
                receiver_type = "agent"
            elif receiver_type == "members":
                receiver_type = "member"
            if receiver_type not in {"agent", "member", "team", "agent_group"} or not isinstance(ids, list):
                raise ValueError("fanout_receiver_list_required")
            raw_entries: list[Any] = [{"type": receiver_type, "id": value} for value in ids]
        elif isinstance(receiver, list):
            raw_entries = receiver
        else:
            raise ValueError("fanout_receiver_list_required")
        entries: list[tuple[str, str]] = []
        for item in raw_entries:
            if not isinstance(item, dict):
                raise ValueError("fanout_receiver_entry_invalid")
            receiver_type = str(item.get("type") or item.get("kind") or "")
            receiver_id = item.get("id")
            if receiver_type not in {"agent", "member", "team", "agent_group"} or not isinstance(receiver_id, str) or not receiver_id:
                raise ValueError("fanout_receiver_entry_invalid")
            entry = (receiver_type, receiver_id)
            if entry in entries:
                raise ValueError("fanout_receiver_duplicate")
            entries.append(entry)
        if not entries:
            raise ValueError("fanout_receiver_list_required")
        return entries

    def _sync_handoff_receipt_state(self, handoff_id: UUID) -> sqlite3.Row:
        rows = self.db.execute("SELECT * FROM handoff_receipts WHERE handoff_id = ? ORDER BY received_at DESC, created_at ASC", (str(handoff_id),)).fetchall()
        if not rows:
            return self.db.execute("SELECT * FROM handoffs WHERE id = ?", (str(handoff_id),)).fetchone()
        statuses = {row["status"] for row in rows}
        aggregate_status = "REJECTED" if "REJECTED" in statuses else ("ACCEPTED" if statuses == {"ACCEPTED"} else "PENDING")
        latest = next((row for row in rows if row["received_at"]), None)
        self.db.execute(
            "UPDATE handoffs SET receipt_status = ?, received_by = ?, received_at = ? WHERE id = ?",
            (aggregate_status, latest["received_by"] if latest else None, latest["received_at"] if latest else None, str(handoff_id)),
        )
        return self.db.execute("SELECT * FROM handoffs WHERE id = ?", (str(handoff_id),)).fetchone()

    def _handoff(self, row: sqlite3.Row) -> Handoff:
        values = dict(row)
        for key in ["completed", "input_artifacts", "output_artifacts", "key_conclusions", "assumptions", "evidence_refs", "open_questions", "risks", "next_actions", "input_handoff_ids", "decision_findings"]:
            values[key] = self._json(values[key])
        values["id"] = UUID(values["id"])
        values["project_id"] = UUID(values["project_id"])
        values["task_id"] = UUID(values["task_id"])
        values["input_handoff_ids"] = [UUID(value) for value in values.get("input_handoff_ids", [])]
        values["revision_of_handoff_id"] = UUID(values["revision_of_handoff_id"]) if values.get("revision_of_handoff_id") else None
        values["revision_number"] = int(values.get("revision_number") or 1)
        if str(values["receiver"]).startswith("{"):
            values["receiver"] = self._json(values["receiver"])
        values["requires_human_approval"] = bool(values["requires_human_approval"])
        values["handoff_type"] = values.get("handoff_type", "RELAY")
        values["receipt_status"] = values.get("receipt_status", "PENDING")
        values["created_at"] = parse_time(values["created_at"])
        values["received_at"] = parse_time(values["received_at"]) if values.get("received_at") else None
        values["receipts"] = self._list_handoff_receipts(values["id"])
        return Handoff(**values)

    def list_handoffs(self, project_id: UUID) -> list[Handoff]:
        return [self._handoff(row) for row in self.db.execute("SELECT * FROM handoffs WHERE project_id = ? ORDER BY created_at DESC", (str(project_id),))]

    def get_handoff(self, handoff_id: UUID) -> Handoff:
        row = self.db.execute("SELECT * FROM handoffs WHERE id = ?", (str(handoff_id),)).fetchone()
        if not row:
            raise KeyError("handoff_not_found")
        return self._handoff(row)

    @staticmethod
    def _validate_handoff_receiver(receiver: Any, handoff_type: str) -> None:
        if isinstance(receiver, str):
            if not receiver.strip():
                raise ValueError("handoff_receiver_required")
            return
        if isinstance(receiver, dict):
            receiver_type = receiver.get("type") or receiver.get("kind")
            if not isinstance(receiver_type, str):
                raise ValueError("handoff_receiver_type_required")
            if receiver_type == "agent" and not isinstance(receiver.get("id"), str):
                raise ValueError("handoff_agent_receiver_id_required")
            if handoff_type == "FANOUT":
                ids = receiver.get("ids")
                if receiver_type not in {"agents", "agent_group", "team"} or not isinstance(ids, list) or not ids or not all(isinstance(value, str) for value in ids):
                    raise ValueError("fanout_receiver_list_required")
            return
        if isinstance(receiver, list) and handoff_type == "FANOUT" and receiver and all(isinstance(item, dict) for item in receiver):
            return
        raise ValueError("invalid_handoff_receiver")

    def create_handoff(self, project_id: UUID, data: HandoffCreate, sender_agent_id: str = "agent-platform") -> Handoff:
        self.get_project(project_id)
        request_fingerprint = self._request_hash(data.model_dump(mode="json", exclude={"idempotency_key"}))
        if data.idempotency_key:
            previous = self._operation(data.idempotency_key, "handoff.create", request_fingerprint)
            if previous:
                return Handoff(**previous)
        task = self.get_task(data.task_id)
        if task.project_id != project_id:
            raise ValueError("task_not_in_project")
        handoff_type = data.handoff_type.value if hasattr(data.handoff_type, "value") else str(data.handoff_type)
        self._validate_handoff_receiver(data.receiver, handoff_type)
        recipient_entries = self._handoff_recipient_entries(data.receiver, handoff_type)
        if handoff_type == "AGGREGATE" and not data.input_handoff_ids:
            raise ValueError("aggregate_handoff_inputs_required")
        if handoff_type != "AGGREGATE" and data.input_handoff_ids:
            raise ValueError("only_aggregate_handoff_accepts_inputs")
        revision_number = 1
        if data.revision_of_handoff_id:
            previous = self.get_handoff(data.revision_of_handoff_id)
            if previous.project_id != project_id or previous.task_id != data.task_id:
                raise ValueError("handoff_revision_scope_mismatch")
            if previous.receipt_status != "REJECTED" or previous.status != "NEEDS_REVISION":
                raise ValueError("handoff_revision_requires_rejection")
            revision_number = previous.revision_number + 1
        elif data.status == "NEEDS_REVISION":
            raise ValueError("new_handoff_cannot_start_needs_revision")
        for reference in [*data.input_artifacts, *data.output_artifacts, *data.evidence_refs]:
            try:
                artifact_id = UUID(reference)
            except (ValueError, TypeError):
                continue
            artifact = self.db.execute("SELECT project_id FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
            if not artifact or artifact["project_id"] != str(project_id):
                raise ValueError("handoff_artifact_not_in_project")
        for input_handoff_id in data.input_handoff_ids:
            input_handoff = self.get_handoff(input_handoff_id)
            if input_handoff.project_id != project_id:
                raise ValueError("handoff_input_not_in_project")
            if input_handoff.receipt_status != "ACCEPTED" or input_handoff.status not in {"PASS", "PASS_WITH_ASSUMPTIONS"}:
                raise ValueError("aggregate_input_handoff_not_ready")
            gate = self.db.execute("SELECT * FROM gates WHERE project_id = ? AND target_type = 'handoff' AND target_id = ?", (str(project_id), str(input_handoff_id))).fetchone()
            if gate:
                gate = self._refresh_gate_if_stale(gate)
            if input_handoff.requires_human_approval and (not gate or gate["status"] != "PASSED"):
                raise ValueError("aggregate_input_handoff_gate_not_passed")
        handoff_id = str(uuid4())
        timestamp = now()
        payload = [json.dumps(getattr(data, name)) for name in ["completed", "input_artifacts", "output_artifacts", "key_conclusions", "assumptions", "evidence_refs", "open_questions", "risks", "next_actions"]]
        handoff_status = data.status.value if hasattr(data.status, "value") else data.status
        receiver = json.dumps(data.receiver, ensure_ascii=False) if isinstance(data.receiver, (dict, list)) else data.receiver
        input_handoffs = json.dumps([str(value) for value in data.input_handoff_ids])
        decision_findings = json.dumps([], ensure_ascii=False)
        try:
            self.db.execute("INSERT INTO handoffs (id, project_id, task_id, sender_agent_id, receiver, status, objective, completed, input_artifacts, output_artifacts, key_conclusions, assumptions, evidence_refs, open_questions, risks, next_actions, requires_human_approval, schema_version, handoff_type, input_handoff_ids, revision_of_handoff_id, revision_number, receipt_status, received_by, received_at, decision_reason, decision_findings, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (handoff_id, str(project_id), str(data.task_id), sender_agent_id, receiver, handoff_status, data.objective, *payload, int(data.requires_human_approval), data.schema_version, handoff_type, input_handoffs, str(data.revision_of_handoff_id) if data.revision_of_handoff_id else None, revision_number, "PENDING", None, None, None, decision_findings, data.idempotency_key, timestamp))
            for receiver_type, receiver_id in recipient_entries:
                self.db.execute(
                    "INSERT INTO handoff_receipts (id, handoff_id, project_id, receiver_type, receiver_id, status, decision_findings, created_at) VALUES (?, ?, ?, ?, ?, 'PENDING', '[]', ?)",
                    (str(uuid4()), handoff_id, str(project_id), receiver_type, receiver_id, timestamp),
                )
            self._insert_event(project_id, "handoff.created", sender_agent_id, {"handoff_id": handoff_id, "status": handoff_status, "handoff_type": handoff_type, "receivers": [{"type": item[0], "id": item[1]} for item in recipient_entries], "revision_of_handoff_id": str(data.revision_of_handoff_id) if data.revision_of_handoff_id else None}, actor_kind="agent", object_type="handoff", object_id=UUID(handoff_id), idempotency_key=f"handoff-event:{handoff_id}")
            result = self._handoff(self.db.execute("SELECT * FROM handoffs WHERE id = ?", (handoff_id,)).fetchone())
            if data.idempotency_key:
                self.db.execute("INSERT OR IGNORE INTO idempotency_records (key, operation, response, request_hash, created_at) VALUES (?, 'handoff.create', ?, ?, ?)", (data.idempotency_key, json.dumps(result.model_dump(mode="json"), ensure_ascii=False), request_fingerprint, timestamp))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return result

    def accept_handoff(self, handoff_id: UUID, receiver: str, actor_kind: str = "agent") -> Handoff:
        row = self.db.execute("SELECT * FROM handoffs WHERE id = ?", (str(handoff_id),)).fetchone()
        if not row:
            raise KeyError("handoff_not_found")
        receipt = self.db.execute("SELECT * FROM handoff_receipts WHERE handoff_id = ? AND receiver_id = ?", (str(handoff_id), receiver)).fetchone()
        if not receipt:
            raise ValueError("handoff_receiver_mismatch")
        if receipt["status"] == "ACCEPTED":
            return self._handoff(row)
        if receipt["status"] == "REJECTED":
            raise ValueError("handoff_rejected_requires_revision")
        if row["status"] not in {"PASS", "PASS_WITH_ASSUMPTIONS"}:
            raise ValueError("handoff_not_acceptible")
        timestamp = now()
        try:
            self.db.execute("UPDATE handoff_receipts SET status = 'ACCEPTED', received_by = ?, received_at = ? WHERE id = ?", (receiver, timestamp, receipt["id"]))
            self._sync_handoff_receipt_state(handoff_id)
            self._insert_event(UUID(row["project_id"]), "handoff.accepted", receiver, {"handoff_id": str(handoff_id), "actor_kind": actor_kind}, actor_kind=actor_kind, object_type="handoff", object_id=handoff_id, idempotency_key=f"handoff-accepted:{handoff_id}:{receiver}")
            result = self._handoff(self.db.execute("SELECT * FROM handoffs WHERE id = ?", (str(handoff_id),)).fetchone())
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return result

    def reject_handoff(self, handoff_id: UUID, actor: str, reason: str, findings: list[dict[str, Any]] | None = None, actor_kind: str = "agent") -> Handoff:
        row = self.db.execute("SELECT * FROM handoffs WHERE id = ?", (str(handoff_id),)).fetchone()
        if not row:
            raise KeyError("handoff_not_found")
        receipt = self.db.execute("SELECT * FROM handoff_receipts WHERE handoff_id = ? AND receiver_id = ?", (str(handoff_id), actor)).fetchone()
        if not receipt:
            raise ValueError("handoff_receiver_mismatch")
        if receipt["status"] == "REJECTED":
            return self._handoff(row)
        if receipt["status"] == "ACCEPTED":
            raise ValueError("accepted_handoff_cannot_be_rejected")
        normalized = normalize_findings(findings or [])
        timestamp = now()
        try:
            self.db.execute("UPDATE handoffs SET status = 'NEEDS_REVISION', decision_reason = ?, decision_findings = ? WHERE id = ?", (reason, json.dumps(normalized, ensure_ascii=False), str(handoff_id)))
            self.db.execute("UPDATE handoff_receipts SET status = 'REJECTED', received_by = ?, received_at = ?, decision_reason = ?, decision_findings = ? WHERE id = ?", (actor, timestamp, reason, json.dumps(normalized, ensure_ascii=False), receipt["id"]))
            self._sync_handoff_receipt_state(handoff_id)
            self._insert_event(UUID(row["project_id"]), "handoff.rejected", actor, {"handoff_id": str(handoff_id), "reason": reason, "findings": normalized, "actor_kind": actor_kind}, actor_kind=actor_kind, object_type="handoff", object_id=handoff_id, idempotency_key=f"handoff-rejected:{handoff_id}")
            result = self._handoff(self.db.execute("SELECT * FROM handoffs WHERE id = ?", (str(handoff_id),)).fetchone())
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return result

    def _artifact(self, row: sqlite3.Row) -> Artifact:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["project_id"] = UUID(values["project_id"])
        values["task_id"] = UUID(values["task_id"]) if values["task_id"] else None
        values["run_id"] = UUID(values["run_id"]) if values["run_id"] else None
        values["data_policy"] = self._json(values["data_policy"])
        values["input_artifact_ids"] = [UUID(value) for value in self._json(values.get("input_artifact_ids", "[]"))]
        values["approved_at"] = parse_time(values["approved_at"]) if values.get("approved_at") else None
        values["created_by_kind"] = values.get("created_by_kind", "member")
        values["downstream_allowed"] = bool(values.get("downstream_allowed", 0))
        values["immutable"] = bool(values.get("immutable", 0))
        values["parent_artifact_id"] = UUID(values["parent_artifact_id"]) if values.get("parent_artifact_id") else None
        values["archived_at"] = parse_time(values["archived_at"]) if values.get("archived_at") else None
        values["created_at"] = parse_time(values["created_at"])
        return Artifact(**values)

    def list_artifacts(self, project_id: UUID) -> list[Artifact]:
        return [self._artifact(row) for row in self.db.execute("SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at DESC", (str(project_id),))]

    def get_artifact(self, artifact_id: UUID) -> Artifact:
        row = self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
        if not row:
            raise KeyError("artifact_not_found")
        return self._artifact(row)

    def create_artifact(self, project_id: UUID, data: ArtifactCreate, created_by: str = "member-001", created_by_kind: str = "member", *, deduplicate: bool = True, _commit: bool = True) -> Artifact:
        self.get_project(project_id)
        if data.status == "APPROVED":
            raise PermissionError("artifact_approval_requires_review")
        if deduplicate and data.content_hash:
            existing = self.db.execute("SELECT * FROM artifacts WHERE project_id = ? AND name = ? AND content_hash = ? AND status != 'ARCHIVED'", (str(project_id), data.name, data.content_hash)).fetchone()
            if existing:
                return self._artifact(existing)
        for input_id in data.input_artifact_ids:
            row = self.db.execute("SELECT project_id FROM artifacts WHERE id = ?", (str(input_id),)).fetchone()
            if not row or row["project_id"] != str(project_id):
                raise ValueError("input_artifact_not_in_project")
        if data.task_id:
            task = self.db.execute("SELECT project_id FROM tasks WHERE id = ?", (str(data.task_id),)).fetchone()
            if not task or task["project_id"] != str(project_id):
                raise ValueError("artifact_task_not_in_project")
        if data.run_id:
            run = self.db.execute("SELECT project_id FROM runs WHERE id = ?", (str(data.run_id),)).fetchone()
            if not run or run["project_id"] != str(project_id):
                raise ValueError("artifact_run_not_in_project")
        if data.git_commit:
            repository = self.db.execute("SELECT provider, local_path FROM git_repositories WHERE project_id = ?", (str(project_id),)).fetchone()
            if repository and repository["provider"] == "local" and not LocalGitProvider(repository["local_path"]).commit_exists(data.git_commit):
                raise ValueError("git_commit_not_found")
        artifact_id = str(uuid4())
        timestamp = now()
        content_hash = data.content_hash or self._hash(f"{data.name}:{data.description}:{timestamp}")
        version = int(self.db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM artifacts WHERE project_id = ? AND name = ?", (str(project_id), data.name)).fetchone()[0])
        try:
            self.db.execute("INSERT INTO artifacts (id, project_id, name, artifact_type, description, content_hash, version, status, source_path, task_id, run_id, created_by, created_at, data_policy, input_artifact_ids, git_commit, snapshot_ref, created_by_kind, approved_by, approved_at, downstream_allowed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (artifact_id, str(project_id), data.name, data.artifact_type, data.description, content_hash, version, data.status, data.source_path, str(data.task_id) if data.task_id else None, str(data.run_id) if data.run_id else None, created_by, timestamp, json.dumps(data.data_policy), json.dumps([str(value) for value in data.input_artifact_ids]), data.git_commit, data.snapshot_ref, created_by_kind, None, None, 0))
            if data.mime_type:
                self.db.execute("UPDATE artifacts SET mime_type = ? WHERE id = ?", (data.mime_type, artifact_id))
            self._insert_event(
                project_id,
                "artifact.created",
                created_by,
                {"artifact_id": artifact_id, "name": data.name, "artifact_type": data.artifact_type},
                actor_kind=created_by_kind,
                object_type="artifact",
                object_id=UUID(artifact_id),
            )
            # 目录族事件（W2.1）：含内容哈希——这是 B 侧 receipt 契约（RECEIPT_FORMAT.md）
            # 在平台侧的对应物；工具级 receipt 随 agentd 上报在溯源期接入。
            self._insert_event(
                project_id,
                "project.artifact.uploaded",
                created_by,
                {
                    "artifact_id": artifact_id,
                    "name": data.name,
                    "artifact_type": data.artifact_type,
                    "version": version,
                    "status": data.status,
                    "content_hash": content_hash,
                },
                actor_kind=created_by_kind,
                object_type="artifact",
                object_id=UUID(artifact_id),
            )
            if _commit:
                self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._artifact(self.db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone())

    def store_artifact_content(self, artifact_id: UUID, content: bytes, mime_type: str | None = None, expected_hash: str | None = None) -> Artifact:
        row = self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
        if not row:
            raise KeyError("artifact_not_found")
        if bool(row["immutable"]):
            raise ValueError("approved_artifact_is_immutable")
        content_hash = hashlib.sha256(content).hexdigest()
        if expected_hash and expected_hash != content_hash:
            raise ValueError("artifact_content_hash_mismatch")
        key = row["storage_key"] or f"projects/{row['project_id']}/artifacts/{artifact_id}/v{row['version']}"
        stored = self.object_store.put_bytes(key, content, mime_type or row["mime_type"])
        try:
            self.db.execute("UPDATE artifacts SET storage_key = ?, content_hash = ?, size_bytes = ?, mime_type = ? WHERE id = ?", (stored.key, stored.content_hash, stored.size_bytes, stored.mime_type, str(artifact_id)))
            self._insert_event(
                UUID(row["project_id"]),
                "artifact.content_stored",
                row["created_by"],
                {"artifact_id": str(artifact_id), "storage_key": stored.key, "size_bytes": stored.size_bytes, "content_hash": stored.content_hash},
                object_type="artifact",
                object_id=artifact_id,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._artifact(self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone())

    def initiate_artifact_multipart(self, artifact_id: UUID, mime_type: str | None = None) -> dict[str, Any]:
        artifact = self.get_artifact(artifact_id)
        if artifact.immutable:
            raise ValueError("approved_artifact_is_immutable")
        storage_key = artifact.storage_key or f"projects/{artifact.project_id}/artifacts/{artifact_id}/v{artifact.version}"
        upload_id = self.object_store.initiate_multipart(storage_key, mime_type or artifact.mime_type)
        timestamp = now()
        self.db.execute("INSERT INTO artifact_multipart_uploads (upload_id, artifact_id, project_id, storage_key, mime_type, created_at) VALUES (?, ?, ?, ?, ?, ?)", (upload_id, str(artifact_id), str(artifact.project_id), storage_key, mime_type or artifact.mime_type, timestamp))
        self.db.commit()
        return {"upload_id": upload_id, "artifact_id": str(artifact_id), "storage_key": storage_key, "created_at": timestamp}

    def upload_artifact_part(self, artifact_id: UUID, upload_id: str, part_number: int, content: bytes) -> str:
        row = self.db.execute("SELECT * FROM artifact_multipart_uploads WHERE upload_id = ? AND artifact_id = ? AND status = 'ACTIVE'", (upload_id, str(artifact_id))).fetchone()
        if not row:
            raise KeyError("multipart_upload_not_found")
        artifact = self.get_artifact(artifact_id)
        if artifact.immutable:
            raise ValueError("approved_artifact_is_immutable")
        return self.object_store.upload_part(upload_id, part_number, content, row["storage_key"])

    def complete_artifact_multipart(self, artifact_id: UUID, upload_id: str, expected_hash: str | None = None) -> Artifact:
        row = self.db.execute("SELECT * FROM artifact_multipart_uploads WHERE upload_id = ? AND artifact_id = ?", (upload_id, str(artifact_id))).fetchone()
        if not row:
            raise KeyError("multipart_upload_not_found")
        if row["status"] == "COMPLETED":
            return self.get_artifact(artifact_id)
        if row["status"] != "ACTIVE":
            raise ValueError("multipart_upload_not_active")
        artifact = self.get_artifact(artifact_id)
        if artifact.immutable:
            self.object_store.abort_multipart(upload_id)
            self.db.execute("UPDATE artifact_multipart_uploads SET status = 'ABORTED', completed_at = ? WHERE upload_id = ?", (now(), upload_id))
            self.db.commit()
            raise ValueError("approved_artifact_is_immutable")
        stored = self.object_store.complete_multipart(upload_id, row["storage_key"], row["mime_type"])
        if expected_hash and expected_hash != stored.content_hash:
            self.object_store.delete(stored.key)
            self.db.execute("UPDATE artifact_multipart_uploads SET status = 'ABORTED', completed_at = ? WHERE upload_id = ?", (now(), upload_id))
            self.db.commit()
            raise ValueError("artifact_content_hash_mismatch")
        try:
            self.db.execute("UPDATE artifacts SET storage_key = ?, content_hash = ?, size_bytes = ?, mime_type = ? WHERE id = ?", (stored.key, stored.content_hash, stored.size_bytes, stored.mime_type, str(artifact_id)))
            self.db.execute("UPDATE artifact_multipart_uploads SET status = 'COMPLETED', completed_at = ? WHERE upload_id = ?", (now(), upload_id))
            self._insert_event(
                artifact.project_id,
                "artifact.content_stored",
                artifact.created_by,
                {"artifact_id": str(artifact_id), "storage_key": stored.key, "size_bytes": stored.size_bytes, "content_hash": stored.content_hash, "upload_id": upload_id},
                object_type="artifact",
                object_id=artifact_id,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self.get_artifact(artifact_id)

    def abort_artifact_multipart(self, artifact_id: UUID, upload_id: str) -> None:
        row = self.db.execute("SELECT * FROM artifact_multipart_uploads WHERE upload_id = ? AND artifact_id = ? AND status = 'ACTIVE'", (upload_id, str(artifact_id))).fetchone()
        if not row:
            raise KeyError("multipart_upload_not_found")
        self.object_store.abort_multipart(upload_id, row["storage_key"])
        self.db.execute("UPDATE artifact_multipart_uploads SET status = 'ABORTED', completed_at = ? WHERE upload_id = ?", (now(), upload_id))
        self.db.commit()

    def cleanup_stale_artifact_multipart_uploads(self, max_age_seconds: int = 86400) -> int:
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        rows = self.db.execute("SELECT upload_id, storage_key FROM artifact_multipart_uploads WHERE status = 'ACTIVE' AND created_at <= ?", (cutoff.isoformat(),)).fetchall()
        for row in rows:
            self.object_store.abort_multipart(row["upload_id"], row["storage_key"])
            self.db.execute("UPDATE artifact_multipart_uploads SET status = 'ABORTED', completed_at = ? WHERE upload_id = ?", (now(), row["upload_id"]))
        self.db.commit()
        return len(rows)

    def create_artifact_version(self, project_id: UUID, parent_artifact_id: UUID, data: ArtifactCreate, created_by: str = "member-001", created_by_kind: str = "member") -> Artifact:
        parent = self.db.execute("SELECT project_id, name, artifact_type FROM artifacts WHERE id = ?", (str(parent_artifact_id),)).fetchone()
        if not parent:
            raise KeyError("parent_artifact_not_found")
        if parent["project_id"] != str(project_id):
            raise ValueError("parent_artifact_not_in_project")
        if data.name != parent["name"] or data.artifact_type != parent["artifact_type"]:
            raise ValueError("artifact_version_identity_mismatch")
        try:
            artifact = self.create_artifact(project_id, data, created_by=created_by, created_by_kind=created_by_kind, deduplicate=False, _commit=False)
            self.db.execute("UPDATE artifacts SET parent_artifact_id = ? WHERE id = ?", (str(parent_artifact_id), str(artifact.id)))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._artifact(self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact.id),)).fetchone())

    def get_artifact_content(self, artifact_id: UUID) -> bytes:
        row = self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
        if not row:
            raise KeyError("artifact_not_found")
        if not row["storage_key"]:
            raise KeyError("artifact_content_not_stored")
        content = self.object_store.get_bytes(row["storage_key"])
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != row["content_hash"]:
            raise ValueError("artifact_stored_content_hash_mismatch")
        if row["size_bytes"] is not None and len(content) != row["size_bytes"]:
            raise ValueError("artifact_stored_content_size_mismatch")
        return content

    def submit_artifact_for_review(
        self,
        artifact_id: UUID,
        actor: str = "member-001",
        actor_kind: str = "member",
        evidence_ids: list[UUID] | None = None,
    ) -> Artifact:
        """草稿 → 提交：把 DRAFT 成果物冻结为待审版本。

        提交必须带证据（`evidence_type="artifact"` 且指向该成果物），
        否则草稿无法进入正式版本门禁——这是"实时草稿不能绕过门禁"的地基。
        """

        row = self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
        if not row:
            raise KeyError("artifact_not_found")
        if row["status"] == "PENDING_REVIEW":
            return self._artifact(row)
        if row["status"] != "DRAFT":
            raise ValueError("artifact_not_in_draft")
        linked = self.db.execute(
            "SELECT id FROM evidence WHERE project_id = ? AND artifact_id = ?",
            (row["project_id"], str(artifact_id)),
        ).fetchall()
        linked_ids = {item["id"] for item in linked}
        for evidence_id in evidence_ids or []:
            if str(evidence_id) not in linked_ids:
                evidence = self.db.execute(
                    "SELECT project_id, artifact_id FROM evidence WHERE id = ?", (str(evidence_id),)
                ).fetchone()
                if not evidence or evidence["project_id"] != row["project_id"] or evidence["artifact_id"] != str(artifact_id):
                    raise ValueError("document_evidence_not_linked_to_artifact")
        if not linked_ids and not evidence_ids:
            raise ValueError("document_evidence_required")
        project_id = UUID(row["project_id"])
        timestamp = now()
        try:
            self.db.execute(
                "UPDATE artifacts SET status = 'PENDING_REVIEW', downstream_allowed = 0 WHERE id = ?",
                (str(artifact_id),),
            )
            self._ensure_gate(project_id, "artifact", artifact_id)
            self._insert_event(
                project_id,
                "artifact.submitted",
                actor,
                {"artifact_id": str(artifact_id), "evidence_count": len(linked_ids)},
                actor_kind=actor_kind,
                object_type="artifact",
                object_id=artifact_id,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._artifact(self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone())

    def revise_artifact(
        self,
        project_id: UUID,
        parent_artifact_id: UUID,
        data: ArtifactCreate,
        actor: str = "member-001",
        actor_kind: str = "member",
    ) -> Artifact:
        """退回/再起草：从已有版本派生一个新的 DRAFT 版本，保留完整历史。

        已批准版本始终保持不可变；修订只新增版本，不修改旧版本。
        """

        parent = self.db.execute(
            "SELECT project_id, status FROM artifacts WHERE id = ?", (str(parent_artifact_id),)
        ).fetchone()
        if not parent:
            raise KeyError("parent_artifact_not_found")
        if parent["project_id"] != str(project_id):
            raise ValueError("parent_artifact_not_in_project")
        draft = data.model_copy(update={"status": "DRAFT"})
        artifact = self.create_artifact_version(
            project_id, parent_artifact_id, draft, created_by=actor, created_by_kind=actor_kind
        )
        try:
            self._ensure_gate(project_id, "artifact", artifact.id)
            self._insert_event(
                project_id,
                "artifact.revised",
                actor,
                {"artifact_id": str(artifact.id), "parent_artifact_id": str(parent_artifact_id)},
                actor_kind=actor_kind,
                object_type="artifact",
                object_id=artifact.id,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._artifact(self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact.id),)).fetchone())

    def get_document_draft(self, project_id: UUID, artifact_id: UUID) -> DocumentDraft | None:
        row = self.db.execute(
            "SELECT * FROM document_drafts WHERE artifact_id = ? AND project_id = ?",
            (str(artifact_id), str(project_id)),
        ).fetchone()
        return self._document_draft(row) if row else None

    def save_document_draft(
        self,
        project_id: UUID,
        artifact_id: UUID,
        content: str,
        *,
        base_revision: int,
        updated_by: str,
    ) -> DocumentDraft:
        """保存协作草稿。

        `base_revision` 是客户端上次看到的修订号（首次保存传 0）：与服务端不一致就抛
        `document_draft_conflict`——**不静默覆盖**别人的编辑（CL-6-02）。
        已批准（不可变）的内容不允许再存草稿（与"保存草稿"的既有守卫一致）。
        """

        artifact = self.db.execute(
            "SELECT project_id, status, immutable FROM artifacts WHERE id = ?", (str(artifact_id),)
        ).fetchone()
        if not artifact:
            raise KeyError("artifact_not_found")
        if artifact["project_id"] != str(project_id):
            raise ValueError("artifact_not_in_project")
        if bool(artifact["immutable"]) or artifact["status"] == "APPROVED":
            raise PermissionError("approved_artifact_is_immutable")

        existing = self.get_document_draft(project_id, artifact_id)
        if existing is None:
            if int(base_revision) not in (0, 1):
                raise ValueError("document_draft_conflict")
        elif int(base_revision) != existing.revision:
            raise ValueError("document_draft_conflict")

        timestamp = now()
        revision = (existing.revision + 1) if existing else 1
        self.db.execute(
            "INSERT INTO document_drafts (artifact_id, project_id, content, revision, updated_by, updated_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(artifact_id) DO UPDATE SET content = excluded.content, revision = excluded.revision, "
            "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
            (str(artifact_id), str(project_id), content, revision, updated_by, timestamp, timestamp),
        )
        self.db.commit()
        return self.get_document_draft(project_id, artifact_id)

    def _document_draft(self, row: sqlite3.Row) -> DocumentDraft:
        values = dict(row)
        values["artifact_id"] = UUID(values["artifact_id"])
        values["project_id"] = UUID(values["project_id"])
        values["updated_at"] = parse_time(values["updated_at"])
        values["created_at"] = parse_time(values["created_at"])
        return DocumentDraft(**values)

    def archive_artifact(self, artifact_id: UUID) -> Artifact:
        row = self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
        if not row:
            raise KeyError("artifact_not_found")
        if row["status"] == "ARCHIVED":
            return self._artifact(row)
        if row["status"] != "APPROVED":
            raise ValueError("only_approved_artifact_can_be_archived")
        timestamp = now()
        try:
            self.db.execute("UPDATE artifacts SET status = 'ARCHIVED', downstream_allowed = 0, archived_at = ? WHERE id = ?", (timestamp, str(artifact_id)))
            self._insert_event(
                UUID(row["project_id"]),
                "artifact.archived",
                "member-001",
                {"artifact_id": str(artifact_id)},
                actor_kind="member",
                object_type="artifact",
                object_id=artifact_id,
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._artifact(self.db.execute("SELECT * FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone())

    def import_artifact(
        self,
        project_id: UUID,
        *,
        name: str,
        artifact_type: str,
        description: str,
        source_path: str,
        content_hash: str,
        data_policy: dict[str, Any] | None = None,
        created_by: str = "importer",
    ) -> tuple[Artifact, bool]:
        """登记已有工作区文件，不复制、不改写原文件。"""
        self.get_project(project_id)
        existing = self.db.execute("SELECT * FROM artifacts WHERE project_id = ? AND name = ? AND content_hash = ?", (str(project_id), name, content_hash)).fetchone()
        if existing:
            return self._artifact(existing), False
        version_row = self.db.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM artifacts WHERE project_id = ? AND name = ?", (str(project_id), name)).fetchone()
        artifact_id = str(uuid4())
        timestamp = now()
        self.db.execute(
            "INSERT INTO artifacts (id, project_id, name, artifact_type, description, content_hash, version, status, source_path, task_id, run_id, created_by, created_at, data_policy, input_artifact_ids, git_commit, snapshot_ref, created_by_kind, approved_by, approved_at, downstream_allowed) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (artifact_id, str(project_id), name, artifact_type, description, content_hash, int(version_row[0]), "DRAFT", source_path, None, None, created_by, timestamp, json.dumps(data_policy or {}, ensure_ascii=False), "[]", None, None, "system", None, None, 0),
        )
        self.db.commit()
        self.add_event(project_id, "artifact.imported", created_by, {"artifact_id": artifact_id, "name": name, "source_path": source_path, "content_hash": content_hash})
        return self._artifact(self.db.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()), True

    def _run(self, row: sqlite3.Row) -> Run:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["project_id"] = UUID(values["project_id"])
        values["task_id"] = UUID(values["task_id"]) if values["task_id"] else None
        for key in ["input_artifact_ids", "output_artifact_ids"]:
            values[key] = [UUID(value) for value in self._json(values[key])]
        for key in ["parameters", "tool_versions", "data_access_policy", "information_boundary"]:
            values[key] = self._json(values[key])
        # 执行用量（COST-1）：老行没有该列时为 {}
        values["usage"] = self._json(values.get("usage") or "{}")
        execution_profile = self._json(values.get("execution_profile") or "{}")
        if execution_profile.get("network_policy", "deny-by-default") == "deny-by-default" and values["network_policy"] != "deny-by-default":
            execution_profile["network_policy"] = values["network_policy"]
        values["execution_profile"] = ExecutionProfile.model_validate(execution_profile)
        values["observed_input_files"] = self._json(values["observed_input_files"])
        values["started_at"] = parse_time(values["started_at"])
        values["completed_at"] = parse_time(values["completed_at"]) if values["completed_at"] else None
        return Run(**values)

    @staticmethod
    def _run_payload(payload: dict[str, Any]) -> Run:
        values = dict(payload)
        profile = values.get("execution_profile") or {}
        if profile.get("network_policy", "deny-by-default") == "deny-by-default" and values.get("network_policy") != "deny-by-default":
            profile["network_policy"] = values["network_policy"]
        values["execution_profile"] = ExecutionProfile.model_validate(profile)
        return Run(**values)

    def list_runs(self, project_id: UUID) -> list[Run]:
        return [self._run(row) for row in self.db.execute("SELECT * FROM runs WHERE project_id = ? ORDER BY started_at DESC", (str(project_id),))]

    def get_run(self, run_id: UUID) -> Run:
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (str(run_id),)).fetchone()
        if not row:
            raise KeyError("run_not_found")
        return self._run(row)

    def _run_input_boundary(self, project_id: UUID, task_id: UUID | None, artifact_ids: list[UUID], observed_input_files: list[str]) -> dict[str, Any]:
        violations: list[dict[str, Any]] = []
        allowed_paths: set[str] = set()
        task = self.get_task(task_id) if task_id else None
        for artifact_id in artifact_ids:
            row = self.db.execute("SELECT * FROM artifacts WHERE id = ? AND project_id = ?", (str(artifact_id), str(project_id))).fetchone()
            if not row:
                violations.append({"severity": "fatal", "code": "input_artifact_not_in_project", "artifact_id": str(artifact_id)})
                continue
            if row["source_path"]:
                allowed_paths.add(str(Path(row["source_path"]).resolve()))
            policy = self._json(row["data_policy"])
            if task and not task.allow_future_data and policy.get("future_data") != "deny":
                violations.append({"severity": "fatal", "code": "future_data_boundary_violation", "artifact_id": str(artifact_id), "policy": policy.get("future_data", "unknown")})
        for observed in observed_input_files:
            try:
                normalized = str(Path(observed).expanduser().resolve())
            except OSError:
                normalized = observed
            if allowed_paths and normalized not in allowed_paths:
                violations.append({"severity": "major", "code": "undeclared_input_file", "path": observed})
        return {"allowed": not violations, "violations": violations, "checked_artifact_ids": [str(value) for value in artifact_ids], "observed_input_files": observed_input_files}

    def create_run(self, project_id: UUID, data: RunCreate) -> Run:
        previous = self._operation(data.idempotency_key, "run.create")
        if previous:
            return self._run_payload(previous)
        self.get_project(project_id)
        self._validate_agent(data.agent_id)
        self._assert_agent_project_access(data.agent_id, project_id)
        if data.task_id:
            task = self.get_task(data.task_id)
            if task.project_id != project_id:
                raise ValueError("task_not_in_project")
        boundary = self._run_input_boundary(project_id, data.task_id, data.input_artifact_ids, data.observed_input_files)
        run_device_id, run_member_id = self.execution_attribution(data.agent_id, getattr(data, "device_id", None))
        run_id = uuid4()
        started = datetime.now(UTC)
        status = "BLOCKED" if not boundary["allowed"] else "RUNNING"
        self.db.execute(
            "INSERT INTO runs (id, project_id, task_id, agent_id, device_id, member_id, status, source_commit, input_artifact_ids, environment_image_digest, dependency_lock, parameters, random_seed, model_provider, model_name, tool_versions, network_policy, execution_profile, data_access_policy, observed_input_files, output_artifact_ids, stdout, stderr, summary, information_boundary, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (str(run_id), str(project_id), str(data.task_id) if data.task_id else None, data.agent_id, run_device_id, run_member_id, status, data.source_commit, json.dumps([str(value) for value in data.input_artifact_ids]), data.environment_image_digest, data.dependency_lock, json.dumps(data.parameters, ensure_ascii=False), data.random_seed, data.model_provider, data.model_name, json.dumps(data.tool_versions, ensure_ascii=False), data.network_policy, json.dumps(data.execution_profile.model_dump(mode="json"), ensure_ascii=False), json.dumps(data.data_access_policy, ensure_ascii=False), json.dumps(data.observed_input_files, ensure_ascii=False), "[]", "", "", "", json.dumps(boundary, ensure_ascii=False), started.isoformat(), None),
        )
        self.db.commit()
        event_type = "run.blocked" if status == "BLOCKED" else "run.created"
        self.add_event(
            project_id,
            event_type,
            data.agent_id,
            {"run_id": str(run_id), "information_boundary": boundary, "device_id": run_device_id, "member_id": run_member_id},
            actor_kind="agent",
        )
        result = self._run(self.db.execute("SELECT * FROM runs WHERE id = ?", (str(run_id),)).fetchone())
        self._save_operation(data.idempotency_key, "run.create", result.model_dump(mode="json"))
        return result

    def complete_run(self, run_id: UUID, data: RunComplete) -> Run:
        if data.idempotency_key:
            previous = self._operation(data.idempotency_key, "run.complete")
            if previous:
                return self._run_payload(previous)
        row = self.db.execute("SELECT * FROM runs WHERE id = ?", (str(run_id),)).fetchone()
        if not row:
            raise KeyError("run_not_found")
        if row["status"] in {"SUCCEEDED", "FAILED", "BLOCKED"}:
            return self._run(row)
        existing_observed = self._json(row["observed_input_files"])
        observed = data.observed_input_files or existing_observed
        boundary = self._run_input_boundary(UUID(row["project_id"]), UUID(row["task_id"]) if row["task_id"] else None, [UUID(value) for value in self._json(row["input_artifact_ids"])], observed)
        for artifact_id in data.output_artifact_ids:
            artifact = self.db.execute("SELECT project_id, run_id, created_by, created_by_kind FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
            if not artifact or artifact["project_id"] != row["project_id"]:
                boundary["allowed"] = False
                boundary["violations"].append({"severity": "fatal", "code": "output_artifact_not_in_project", "artifact_id": str(artifact_id)})
            elif artifact["run_id"] != str(run_id) or artifact["created_by"] != row["agent_id"] or artifact["created_by_kind"] != "agent":
                boundary["allowed"] = False
                boundary["violations"].append({"severity": "fatal", "code": "output_artifact_run_or_agent_mismatch", "artifact_id": str(artifact_id)})
        status = "BLOCKED" if not boundary["allowed"] else ("SUCCEEDED" if data.success else "FAILED")
        completed = now()
        usage = self._normalized_usage(data, row, completed)
        self.db.execute("UPDATE runs SET status = ?, observed_input_files = ?, output_artifact_ids = ?, stdout = ?, stderr = ?, summary = ?, information_boundary = ?, usage = ?, completed_at = ? WHERE id = ?", (status, json.dumps(observed, ensure_ascii=False), json.dumps([str(value) for value in data.output_artifact_ids]), data.stdout, data.stderr, data.summary, json.dumps(boundary, ensure_ascii=False), json.dumps(usage, ensure_ascii=False), completed, str(run_id)))
        self.db.commit()
        event_type = {"SUCCEEDED": "run.completed", "FAILED": "run.failed", "BLOCKED": "run.blocked"}[status]
        self.add_event(UUID(row["project_id"]), event_type, row["agent_id"], {"run_id": str(run_id), "status": status, "information_boundary": boundary, "usage": usage})
        # COST-1：这次的用量超了任务预算就立刻留痕（一次性事件 + 聊天卡片）；
        # 平台不因此改任务状态——超预算要靠人工门禁处理，不假装"系统已经处理了"。
        overruns = self._usage_overruns(self.db.execute("SELECT * FROM tasks WHERE id = ?", (row["task_id"],)).fetchone() if row["task_id"] else None, usage)
        if overruns:
            self._note_budget_exceeded(UUID(row["project_id"]), row["task_id"], run_id, overruns)
        result = self._run(self.db.execute("SELECT * FROM runs WHERE id = ?", (str(run_id),)).fetchone())
        if data.idempotency_key:
            self._save_operation(data.idempotency_key, "run.complete", result.model_dump(mode="json"))
        return result

    def _agent(self, row: sqlite3.Row) -> Agent:
        values = dict(row)
        values["supported_tools"] = self._json(values["supported_tools"])
        values["supported_languages"] = self._json(values["supported_languages"])
        values["last_seen"] = parse_time(values["last_seen"])
        values["capability_cards"] = self._normalized_cards(values.get("capability_cards"))
        return Agent(**values)

    def list_agents(self, organization_id: UUID | None = None) -> list[Agent]:
        if organization_id is None:
            rows = self.db.execute("SELECT * FROM agents ORDER BY last_seen DESC")
        else:
            # 组织收口：Agent 列表不再全局可见（此前任何登录成员都能看到全部 Agent、含本地工作区路径）
            rows = self.db.execute(
                "SELECT a.* FROM agents a LEFT JOIN human_members h ON h.id = a.owner_member_id "
                "WHERE h.organization_id = ? ORDER BY a.last_seen DESC",
                (str(organization_id),),
            )
        return [self._agent(row) for row in rows]

    def register_agent(self, data: AgentRegister) -> Agent:
        timestamp = now()
        cards = self._normalized_cards(getattr(data, "capability_cards", None))
        # 技能集合 = 卡片技能 ∪ 老写法 supported_tools（归一化后）；授权范围值不进这一列
        tools = self._merge_skill_names([card["skill"] for card in cards], data.supported_tools)
        executor = getattr(data, "executor", None)
        package_id, package_source = self._derive_package(executor, data.agent_id)
        # 老内核重复注册时不要把已知的包信息降级成前缀兜底（上报 > 探测推断 > 前缀猜测）
        existing = self.db.execute(
            "SELECT package_id, package_source FROM agents WHERE agent_id = ?", (data.agent_id,)
        ).fetchone()
        if existing and existing["package_id"] and not package_id:
            # 老内核重复注册时不要把已经知道的包信息抹掉（上报 > 探测推断 > 未知）
            package_id, package_source = existing["package_id"], existing["package_source"]
        # 显式列名：加列后位置 INSERT 会直接报错（AIP-1a 的坑，见 docs/AIP_1_PLAN.md §3.5）
        self.db.execute(
            "INSERT INTO agents (agent_id, display_name, owner_member_id, model_provider, model_name, supported_tools, "
            "supported_languages, max_concurrency, local_workspace, network_policy, status, last_seen, capability_cards, "
            "package_id, instance_id, package_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'online', ?, ?, ?, ?, ?) "
            "ON CONFLICT(agent_id) DO UPDATE SET display_name=excluded.display_name, model_provider=excluded.model_provider, "
            "model_name=excluded.model_name, supported_tools=excluded.supported_tools, supported_languages=excluded.supported_languages, "
            "max_concurrency=excluded.max_concurrency, local_workspace=excluded.local_workspace, network_policy=excluded.network_policy, "
            "status='online', last_seen=excluded.last_seen, capability_cards=excluded.capability_cards, "
            "package_id=excluded.package_id, instance_id=excluded.instance_id, package_source=excluded.package_source",
            (
                data.agent_id,
                data.display_name,
                data.owner_member_id,
                data.model_provider,
                data.model_name,
                json.dumps(tools),
                json.dumps(data.supported_languages),
                data.max_concurrency,
                data.local_workspace,
                data.network_policy,
                timestamp,
                json.dumps(cards, ensure_ascii=False),
                package_id,
                data.agent_id,
                package_source,
            ),
        )
        self.db.commit()
        return self._agent(self.db.execute("SELECT * FROM agents WHERE agent_id = ?", (data.agent_id,)).fetchone())

    @staticmethod
    def _merge_skill_names(*sources: Any) -> list[str]:
        """合并多个来源的技能名（保序、归一化、去重、剔除授权范围值）。"""

        merged: list[str] = []
        for source in sources:
            for item in source or []:
                normalized = skill_match.normalize_skill_id(item)
                if normalized is None or skill_match.is_scope_token(normalized):
                    continue
                if normalized not in merged:
                    merged.append(normalized)
        return merged

    @staticmethod
    def _normalized_cards(raw: Any) -> list[dict[str, Any]]:
        """能力卡归一化（技能 id 小写折叠；非法技能名整卡丢弃——写入侧已 422 校验）。"""

        cards: list[dict[str, Any]] = []
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "[]")
            except (TypeError, ValueError):
                raw = []
        for item in raw or []:
            values = item if isinstance(item, dict) else getattr(item, "__dict__", {})
            skill = skill_match.normalize_skill_id(values.get("skill"))
            if skill is None:
                continue
            cards.append(
                {
                    "skill": skill,
                    "version": str(values.get("version") or "").strip(),
                    "inputs": [str(value) for value in (values.get("inputs") or [])],
                    "outputs": [str(value) for value in (values.get("outputs") or [])],
                    "description": str(values.get("description") or ""),
                }
            )
        return cards

    def _infer_package_from_runtime(self, agent_id: str) -> str | None:
        """按最近连接设备的运行态推断执行体程序包（探测值比自报可信）。"""

        runtime = self.db.execute(
            "SELECT s.adapter_versions FROM agent_connections c JOIN device_runtime_state s ON s.device_id = c.device_id "
            "WHERE c.agent_id = ? ORDER BY c.connected_at DESC LIMIT 1",
            (agent_id,),
        ).fetchone()
        if not runtime or not runtime["adapter_versions"]:
            return None
        try:
            versions = json.loads(runtime["adapter_versions"])
        except (TypeError, ValueError):
            return None
        for kind, version in sorted(versions.items()):
            if str(version).strip():
                return f"{str(kind).lower()}@{str(version).strip()}"
        return None

    def _derive_package(self, executor: Any, agent_id: str) -> tuple[str | None, str]:
        """执行体身份两段里的"程序包"：内核上报优先，其次设备探测值，都没有就**不猜**。

        `agent_id` 前缀兜底被有意放弃：`agent-<uuid>`、`agent-mira` 这类前缀不携带执行体信息，
        硬拼一个 `legacy:agent` 会把互不相干的执行体归成"同一个包"——那是假分组，比"未知"更坏。
        未知就留空，界面显示"—"，见 docs/AIP_1_PLAN.md §5.2。
        """

        if executor:
            values = executor if isinstance(executor, dict) else getattr(executor, "__dict__", {})
            kind = str(values.get("kind") or "").strip().lower()
            version = str(values.get("version") or "").strip()
            package = str(values.get("package") or "").strip()
            if kind:
                return (package or f"{kind}@{version or 'unknown'}"), "reported"
        return self._infer_package_from_runtime(agent_id), "inferred"

    def _backfill_agent_packages(self) -> None:
        """给历史行推断 package_id（幂等）。

        只写"包信息为空且能被设备探测值补上"的行。已上报（reported）或已有具体版本的，一律不动；
        没有探测信号就保持空——宁可显示"未知"，也不要造一个假的分组。
        """

        rows = self.db.execute("SELECT agent_id FROM agents WHERE package_id IS NULL OR package_id = ''").fetchall()
        for row in rows:
            inferred = self._infer_package_from_runtime(row["agent_id"])
            if not inferred:
                continue
            self.db.execute(
                "UPDATE agents SET package_id = ?, package_source = 'inferred', "
                "instance_id = COALESCE(instance_id, agent_id) WHERE agent_id = ?",
                (inferred, row["agent_id"]),
            )
        self.db.commit()

    def _upgrade_agent_package(self, agent_id: str) -> None:
        """设备上报运行态后，把未知的包补成探测到的具体版本（只在可补时写库）。"""

        row = self.db.execute("SELECT package_id, package_source FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if not row or row["package_source"] != "inferred" or row["package_id"]:
            return
        inferred = self._infer_package_from_runtime(agent_id)
        if not inferred:
            return
        self.db.execute("UPDATE agents SET package_id = ? WHERE agent_id = ?", (inferred, agent_id))

    def heartbeat(self, agent_id: str) -> Agent:
        self.db.execute("UPDATE agents SET status = 'online', last_seen = ? WHERE agent_id = ?", (now(), agent_id))
        self.db.commit()
        row = self.db.execute("SELECT * FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if not row:
            raise KeyError("agent_not_found")
        return self._agent(row)

    def _mark_agent_offline(
        self,
        agent_id: str,
        reason: str,
        *,
        timeout_seconds: int | None = None,
        last_seen: str | None = None,
    ) -> list[Event]:
        """把 Agent 置 offline，并给它的每个授权项目写 ``agent.offline`` 事件。

        Agent 本身不挂项目（`agent_project_grants` 才是归属），所以事件按授权项目分发；
        一个项目都没有的 Agent 只改状态、不写事件。
        """

        self.db.execute("UPDATE agents SET status = 'offline' WHERE agent_id = ?", (agent_id,))
        payload: dict[str, Any] = {"agent_id": agent_id, "reason": reason}
        if timeout_seconds is not None:
            payload["timeout_seconds"] = timeout_seconds
        if last_seen:
            payload["last_seen"] = last_seen
        events: list[Event] = []
        grants = self.db.execute("SELECT project_id FROM agent_project_grants WHERE agent_id = ?", (agent_id,)).fetchall()
        for grant in grants:
            events.append(
                self.add_event(UUID(grant["project_id"]), "agent.offline", agent_id, payload, actor_kind="system", object_type="agent")
            )
        return events

    def expire_stale_agents(
        self,
        timeout_seconds: int = AGENT_HEARTBEAT_TIMEOUT_SECONDS,
        *,
        reference_time: str | None = None,
    ) -> list[Event]:
        """把超过阈值没有心跳的在线 Agent 置 offline（UX-3-01）。

        阈值固定为 3 × 30s 心跳周期；不做成环境变量（见 Demo 1.0 计划 §3 D3）。
        """

        moment = parse_time(reference_time) if reference_time else datetime.now(UTC)
        cutoff = (moment - timedelta(seconds=timeout_seconds)).isoformat()
        rows = self.db.execute(
            "SELECT agent_id, last_seen FROM agents WHERE status != 'offline' AND last_seen <= ?",
            (cutoff,),
        ).fetchall()
        events: list[Event] = []
        for row in rows:
            events.extend(
                self._mark_agent_offline(
                    row["agent_id"], "heartbeat_timeout", timeout_seconds=timeout_seconds, last_seen=row["last_seen"]
                )
            )
        self.db.commit()
        return events

    def _agent_offline_if_disconnected(self, agent_id: str) -> list[Event]:
        """Gateway 连接全部断开后把 Agent 置 offline（UX-3-02）。"""

        remaining = self.db.execute(
            "SELECT 1 FROM agent_connections WHERE agent_id = ? AND status = ? LIMIT 1",
            (agent_id, AgentConnectionStatus.CONNECTED.value),
        ).fetchone()
        if remaining:
            return []
        row = self.db.execute("SELECT status FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if not row or row["status"] == "offline":
            return []
        return self._mark_agent_offline(agent_id, "gateway_disconnected")

    def recycle_expired_leases(self, *, reference_time: str | None = None) -> list[Event]:
        """租约过期后的任务去向（Demo 1.0 计划 §3 D4）。

        - 从 ``CLAIMED``（还没开工）超时：回到 ``READY``，任何 Agent 都能重新领取；
        - 从 ``RUNNING``（已开工）超时：交回 ``NEEDS_REVISION`` 并登记机器复核与风险，
          避免把半成品当未开工导致重复劳动；
        - 其他状态只释放租约，不动任务状态。
        """

        moment = reference_time or now()
        expired = self.db.execute(
            "SELECT * FROM task_leases WHERE status = 'ACTIVE' AND expires_at <= ?", (moment,)
        ).fetchall()
        events: list[Event] = []
        for lease in expired:
            self.db.execute("UPDATE task_leases SET status = 'EXPIRED' WHERE id = ?", (lease["id"],))
            row = self.db.execute("SELECT id, project_id, status FROM tasks WHERE id = ?", (lease["task_id"],)).fetchone()
            if row is None:
                continue
            project_id = UUID(row["project_id"])
            task_id = UUID(row["id"])
            payload: dict[str, Any] = {
                "task_id": row["id"],
                "lease_id": lease["id"],
                "agent_id": lease["agent_id"],
                "expired_at": lease["expires_at"],
                "previous_status": row["status"],
            }
            if row["status"] == "CLAIMED":
                self.db.execute("UPDATE tasks SET status = 'READY', updated_at = ? WHERE id = ?", (now(), row["id"]))
                events.append(
                    self.add_event(project_id, "task.lease.expired", "platform", payload, object_type="task", object_id=task_id)
                )
            elif row["status"] == "RUNNING":
                self.db.commit()
                self.create_review(
                    project_id,
                    ReviewCreate(
                        target_type="task",
                        target_id=task_id,
                        verdict="NEEDS_REVISION",
                        summary=f"租约过期：{lease['agent_id']} 执行中失联，任务交回修订。",
                        findings=[
                            {
                                "severity": "major",
                                "code": "lease_expired_during_execution",
                                "message": f"Agent {lease['agent_id']} 的租约于 {lease['expires_at']} 过期，任务仍为 RUNNING。",
                            }
                        ],
                        reviewer="platform-maintenance",
                        reviewer_kind=ReviewerKind.SYSTEM,
                        idempotency_key=f"lease-recycle:{lease['id']}",
                    ),
                )
                events.append(
                    self.add_event(project_id, "task.lease.recycled", "platform", payload, object_type="task", object_id=task_id)
                )
        self.db.commit()
        return events

    def unregister_agent(self, agent_id: str) -> None:
        if not self.db.execute("SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)).fetchone():
            raise KeyError("agent_not_found")
        self.db.execute("DELETE FROM task_leases WHERE agent_id = ?", (agent_id,))
        self.db.execute("DELETE FROM agents WHERE agent_id = ?", (agent_id,))
        self.db.commit()

    def _review(self, row: sqlite3.Row) -> Review:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["project_id"] = UUID(values["project_id"])
        values["target_id"] = UUID(values["target_id"])
        values["findings"] = self._json(values["findings"])
        values["evidence_ids"] = [UUID(value) for value in self._json(values.get("evidence_ids") or "[]")]
        values["risk_summary"] = risk_summary(values["findings"])
        values["created_at"] = parse_time(values["created_at"])
        return Review(**values)

    def _gate(self, row: sqlite3.Row) -> Gate:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["project_id"] = UUID(values["project_id"])
        values["target_id"] = UUID(values["target_id"]) if values["target_id"] else None
        values["required_human_approval"] = bool(values["required_human_approval"])
        values["blocking_findings"] = self._json(values["blocking_findings"])
        values["rules"] = self._json(values["rules"])
        values["review_ids"] = [UUID(value) for value in self._json(values.get("review_ids") or "[]")]
        values["evidence_ids"] = [UUID(value) for value in self._json(values.get("evidence_ids") or "[]")]
        values["risk_summary"] = self._json(values.get("risk_summary") or "{}")
        values["input_snapshot"] = self._json(values.get("input_snapshot") or "{}")
        values["invalidated_at"] = parse_time(values["invalidated_at"]) if values.get("invalidated_at") else None
        values["approved_at"] = parse_time(values["approved_at"]) if values["approved_at"] else None
        return Gate(**values)

    def list_gates(self, project_id: UUID) -> list[Gate]:
        rows = self.db.execute("SELECT * FROM gates WHERE project_id = ? ORDER BY rowid DESC", (str(project_id),)).fetchall()
        for row in rows:
            self._refresh_gate_if_stale(row)
        refreshed = [self._refresh_gate_if_stale(row) for row in self.db.execute("SELECT * FROM gates WHERE project_id = ? ORDER BY rowid DESC", (str(project_id),)).fetchall()]
        self.db.commit()
        return [self._gate(row) for row in refreshed]

    def _ensure_gate(self, project_id: UUID, target_type: str, target_id: UUID, required_human_approval: bool = True) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM gates WHERE project_id = ? AND target_type = ? AND target_id = ?", (str(project_id), target_type, str(target_id))).fetchone()
        if row:
            return row
        gate_id = str(uuid4())
        self.db.execute("INSERT INTO gates (id, project_id, target_type, target_id, status, required_human_approval, blocking_findings, rules, review_ids, evidence_ids, risk_summary, input_snapshot, invalidated_at, invalidation_reason, approved_by, approved_at) VALUES (?, ?, ?, ?, 'OPEN', ?, '[]', ?, '[]', '[]', '{}', '{}', NULL, NULL, NULL, NULL)", (gate_id, str(project_id), target_type, str(target_id), int(required_human_approval), json.dumps(gate_rules(target_type))))
        return self.db.execute("SELECT * FROM gates WHERE id = ?", (gate_id,)).fetchone()

    def _gate_target_snapshot(self, target_type: str, target_id: UUID | None) -> dict[str, Any]:
        if target_id is None:
            return {}
        tables = {"task": "tasks", "artifact": "artifacts", "handoff": "handoffs"}
        table = tables.get(target_type)
        if not table:
            return {"type": target_type, "id": str(target_id)}
        row = self.db.execute(f"SELECT * FROM {table} WHERE id = ?", (str(target_id),)).fetchone()
        if not row:
            return {"type": target_type, "id": str(target_id), "missing": True}
        artifact_refs: list[str] = []
        if target_type == "task":
            artifact_refs = [str(value) for value in self._json(row["input_artifacts"] or "[]")]
            value = {"status": row["status"], "updated_at": row["updated_at"], "input_artifacts": artifact_refs, "input_handoff_ids": self._json(row["input_handoff_ids"] or "[]"), "dependency_task_ids": self._json(row["dependency_task_ids"] or "[]")}
        elif target_type == "artifact":
            artifact_refs = [str(value) for value in self._json(row["input_artifact_ids"] or "[]")]
            value = {"version": row["version"], "content_hash": row["content_hash"], "status": row["status"], "immutable": bool(row["immutable"]) if "immutable" in row.keys() else False, "input_artifact_ids": artifact_refs}
        else:
            artifact_refs = [str(value) for value in [*self._json(row["input_artifacts"] or "[]"), *self._json(row["output_artifacts"] or "[]")]]
            value = {"status": row["status"], "revision_number": row["revision_number"] if "revision_number" in row.keys() else 1, "artifact_refs": artifact_refs, "input_handoff_ids": self._json(row["input_handoff_ids"] or "[]")}
        sources: list[dict[str, Any]] = []
        for reference in artifact_refs:
            try:
                artifact_id = UUID(reference)
            except ValueError:
                continue
            artifact = self.db.execute("SELECT id, version, content_hash, status FROM artifacts WHERE id = ?", (str(artifact_id),)).fetchone()
            sources.append(dict(artifact) if artifact else {"id": str(artifact_id), "missing": True})
        return {"type": target_type, "id": str(target_id), "fingerprint": self._request_hash(value), "source_artifacts": sources}

    def _evidence_snapshot(self, evidence_ids: list[UUID]) -> list[dict[str, Any]]:
        snapshots: list[dict[str, Any]] = []
        for evidence_id in evidence_ids:
            evidence = self.db.execute("SELECT * FROM evidence WHERE id = ?", (str(evidence_id),)).fetchone()
            if not evidence:
                snapshots.append({"id": str(evidence_id), "missing": True})
                continue
            source: dict[str, Any] = {"claim": evidence["claim"], "evidence_type": evidence["evidence_type"], "source_ref": evidence["source_ref"]}
            if evidence["artifact_id"]:
                artifact = self.db.execute("SELECT version, content_hash, status FROM artifacts WHERE id = ?", (evidence["artifact_id"],)).fetchone()
                source["artifact"] = dict(artifact) if artifact else {"missing": True}
            if evidence["run_id"]:
                run = self.db.execute("SELECT status, output_artifact_ids, information_boundary FROM runs WHERE id = ?", (evidence["run_id"],)).fetchone()
                source["run"] = dict(run) if run else {"missing": True}
            snapshots.append({"id": str(evidence_id), "fingerprint": self._request_hash(source)})
        return snapshots

    def _capture_gate_snapshot(self, project_id: UUID, target_type: str, target_id: UUID | None, review_ids: list[str], evidence_ids: list[str]) -> dict[str, Any]:
        evidence_uuid_ids = [UUID(value) for value in evidence_ids]
        reviews: list[dict[str, Any]] = []
        for review_id in review_ids:
            review = self.db.execute("SELECT verdict, summary, findings, evidence_ids FROM reviews WHERE id = ? AND project_id = ?", (review_id, str(project_id))).fetchone()
            if review:
                reviews.append({"id": review_id, "fingerprint": self._request_hash(dict(review))})
            else:
                reviews.append({"id": review_id, "missing": True})
        return {
            "target": self._gate_target_snapshot(target_type, target_id),
            "review_ids": list(review_ids),
            "reviews": reviews,
            "evidence_ids": list(evidence_ids),
            "evidence": self._evidence_snapshot(evidence_uuid_ids),
        }

    def _gate_snapshot_is_current(self, row: sqlite3.Row) -> bool:
        snapshot = self._json(row["input_snapshot"] or "{}")
        if not snapshot:
            return True
        current = self._capture_gate_snapshot(
            UUID(row["project_id"]),
            row["target_type"],
            UUID(row["target_id"]) if row["target_id"] else None,
            [str(value) for value in snapshot.get("review_ids", [])],
            [str(value) for value in snapshot.get("evidence_ids", [])],
        )
        return current == snapshot

    def _tasks_affected_by_gate(self, gate: sqlite3.Row) -> list[str]:
        """Walk project references so a stale gate becomes visible downstream."""
        project_id = str(gate["project_id"])
        tasks = [dict(item) for item in self.db.execute("SELECT id, dependency_task_ids, input_artifacts, input_handoff_ids FROM tasks WHERE project_id = ?", (project_id,))]
        artifacts = [dict(item) for item in self.db.execute("SELECT id, input_artifact_ids FROM artifacts WHERE project_id = ?", (project_id,))]
        handoffs = [dict(item) for item in self.db.execute("SELECT id, task_id, input_artifacts, output_artifacts, input_handoff_ids FROM handoffs WHERE project_id = ?", (project_id,))]

        root_type = gate["target_type"]
        root_id = str(gate["target_id"]) if gate["target_id"] else None
        if not root_id or root_type == "project":
            return []

        queue: list[tuple[str, str]] = [(root_type, root_id)]
        seen: set[tuple[str, str]] = set()
        affected: set[str] = set()
        while queue:
            node_type, node_id = queue.pop(0)
            node = (node_type, node_id)
            if node in seen:
                continue
            seen.add(node)
            if node_type == "task":
                if node_id in {str(item["id"]) for item in tasks}:
                    affected.add(node_id)
                for item in tasks:
                    references = {str(value) for value in self._json(item.get("dependency_task_ids") or "[]")}
                    if node_id in references:
                        queue.append(("task", str(item["id"])))
                for item in handoffs:
                    if str(item.get("task_id")) == node_id:
                        queue.append(("handoff", str(item["id"])))
            elif node_type == "artifact":
                for item in artifacts:
                    references = {str(value) for value in self._json(item.get("input_artifact_ids") or "[]")}
                    if node_id in references:
                        queue.append(("artifact", str(item["id"])))
                for item in tasks:
                    references = {str(value) for value in self._json(item.get("input_artifacts") or "[]")}
                    if node_id in references:
                        queue.append(("task", str(item["id"])))
                for item in handoffs:
                    references = {str(value) for value in [*self._json(item.get("input_artifacts") or "[]"), *self._json(item.get("output_artifacts") or "[]")]}
                    if node_id in references:
                        queue.append(("handoff", str(item["id"])))
            elif node_type == "handoff":
                for item in handoffs:
                    references = {str(value) for value in self._json(item.get("input_handoff_ids") or "[]")}
                    if node_id in references:
                        queue.append(("handoff", str(item["id"])))
                for item in tasks:
                    references = {str(value) for value in self._json(item.get("input_handoff_ids") or "[]")}
                    if node_id in references:
                        queue.append(("task", str(item["id"])))
        return sorted(affected)

    def _propagate_gate_invalidation(self, gate: sqlite3.Row, reason: str) -> None:
        """Invalidate dependent task gates and make stale work explicit."""
        project_id = str(gate["project_id"])
        task_ids = self._tasks_affected_by_gate(gate)
        for task_id in task_ids:
            task = self.db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not task or task["status"] in {"FAILED", "CANCELLED"}:
                continue
            current = task["status"]
            next_status = "NEEDS_REVISION" if current in {"APPROVED", "WAITING_REVIEW", "NEEDS_REVISION"} else "BLOCKED"
            blocked_reason = f"upstream_gate_invalidated:{gate['id']}:{reason}"
            if current != next_status or task["blocked_reason"] != blocked_reason:
                self.db.execute("UPDATE tasks SET status = ?, blocked_reason = ?, updated_at = ? WHERE id = ?", (next_status, blocked_reason, now(), task_id))
                self._insert_event(
                    UUID(project_id),
                    "task.upstream_gate_invalidated",
                    "system",
                    {"task_id": task_id, "gate_id": gate["id"], "gate_target_type": gate["target_type"], "gate_target_id": gate["target_id"], "reason": reason, "status": next_status},
                    actor_kind="system",
                    object_type="task",
                    object_id=UUID(task_id),
                    idempotency_key=f"task-gate-propagation:{gate['id']}:{task_id}:{reason}",
                )
            downstream_gate = self.db.execute("SELECT * FROM gates WHERE project_id = ? AND target_type = 'task' AND target_id = ?", (project_id, task_id)).fetchone()
            if downstream_gate and downstream_gate["status"] == "PASSED":
                timestamp = now()
                self.db.execute("UPDATE gates SET status = 'INVALIDATED', invalidated_at = ?, invalidation_reason = ?, approved_by = NULL, approved_at = NULL WHERE id = ?", (timestamp, "upstream_gate_invalidated", downstream_gate["id"]))
                self._insert_event(
                    UUID(project_id),
                    "gate.invalidated",
                    "system",
                    {"gate_id": downstream_gate["id"], "target_type": "task", "target_id": task_id, "reason": "upstream_gate_invalidated", "source_gate_id": gate["id"]},
                    actor_kind="system",
                    object_type="gate",
                    object_id=UUID(downstream_gate["id"]),
                    idempotency_key=f"gate-propagation:{gate['id']}:{downstream_gate['id']}:{reason}",
                )

    def _refresh_gate_if_stale(self, row: sqlite3.Row) -> sqlite3.Row:
        if row["status"] != "PASSED" or self._gate_snapshot_is_current(row):
            return row
        timestamp = now()
        self.db.execute(
            "UPDATE gates SET status = 'INVALIDATED', invalidated_at = ?, invalidation_reason = ?, approved_by = NULL, approved_at = NULL WHERE id = ?",
            (timestamp, "input_snapshot_changed", row["id"]),
        )
        self._insert_event(
            UUID(row["project_id"]),
            "gate.invalidated",
            "system",
            {"gate_id": row["id"], "target_type": row["target_type"], "target_id": row["target_id"], "reason": "input_snapshot_changed"},
            actor_kind="system",
            object_type="gate",
            object_id=UUID(row["id"]),
            idempotency_key=f"gate-invalidated:{row['id']}:{row['input_snapshot']}",
        )
        self._propagate_gate_invalidation(row, "input_snapshot_changed")
        return self.db.execute("SELECT * FROM gates WHERE id = ?", (row["id"],)).fetchone()

    def create_evidence(self, project_id: UUID, data: EvidenceCreate) -> Evidence:
        self.get_project(project_id)
        if data.evidence_type == "artifact" and not data.artifact_id:
            raise ValueError("artifact_evidence_requires_artifact_id")
        if data.evidence_type == "run" and not data.run_id:
            raise ValueError("run_evidence_requires_run_id")
        if data.artifact_id:
            row = self.db.execute("SELECT project_id FROM artifacts WHERE id = ?", (str(data.artifact_id),)).fetchone()
            if not row or row["project_id"] != str(project_id):
                raise ValueError("evidence_artifact_not_in_project")
        if data.run_id:
            row = self.db.execute("SELECT project_id FROM runs WHERE id = ?", (str(data.run_id),)).fetchone()
            if not row or row["project_id"] != str(project_id):
                raise ValueError("evidence_run_not_in_project")
        evidence_id = str(uuid4())
        timestamp = now()
        try:
            self.db.execute("INSERT INTO evidence (id, project_id, claim, evidence_type, artifact_id, run_id, source_ref, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (evidence_id, str(project_id), data.claim, data.evidence_type, str(data.artifact_id) if data.artifact_id else None, str(data.run_id) if data.run_id else None, data.source_ref, data.created_by, timestamp))
            self._insert_event(
                project_id,
                "evidence.created",
                data.created_by,
                {"evidence_id": evidence_id, "evidence_type": data.evidence_type},
                object_type="evidence",
                object_id=UUID(evidence_id),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._evidence(self.db.execute("SELECT * FROM evidence WHERE id = ?", (evidence_id,)).fetchone())

    def _evidence(self, row: sqlite3.Row) -> Evidence:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["project_id"] = UUID(values["project_id"])
        values["artifact_id"] = UUID(values["artifact_id"]) if values["artifact_id"] else None
        values["run_id"] = UUID(values["run_id"]) if values["run_id"] else None
        values["created_at"] = parse_time(values["created_at"])
        return Evidence(**values)

    def list_evidence(self, project_id: UUID) -> list[Evidence]:
        return [self._evidence(row) for row in self.db.execute("SELECT * FROM evidence WHERE project_id = ? ORDER BY created_at DESC", (str(project_id),))]

    def create_review(self, project_id: UUID, data: ReviewCreate) -> Review:
        self.get_project(project_id)
        request_fingerprint = self._request_hash(data.model_dump(mode="json", exclude={"idempotency_key"}))
        if data.idempotency_key:
            previous = self._operation(data.idempotency_key, "review.create", request_fingerprint)
            if previous:
                return Review.model_validate(previous)
        target_id = str(data.target_id)
        target = self.db.execute(f"SELECT * FROM {data.target_type}s WHERE id = ? AND project_id = ?", (target_id, str(project_id))).fetchone()
        if not target:
            raise KeyError("review_target_not_found")
        normalized_findings = normalize_findings(data.findings)
        if any(item.get("resolved") for item in normalized_findings):
            raise ValueError("finding_resolution_requires_risk_action")
        if data.verdict == "APPROVED":
            normalized_findings = ensure_approvable(normalized_findings)
        for evidence_id in data.evidence_ids:
            evidence = self.db.execute("SELECT project_id FROM evidence WHERE id = ?", (str(evidence_id),)).fetchone()
            if not evidence or evidence["project_id"] != str(project_id):
                raise ValueError("review_evidence_not_in_project")
        reviewer_kind = data.reviewer_kind.value if hasattr(data.reviewer_kind, "value") else str(data.reviewer_kind)
        if data.verdict == "APPROVED" and reviewer_kind != "member":
            raise PermissionError("human_approval_required")
        if reviewer_kind == "member":
            self.authorize_member(project_id, data.reviewer, "review.approve" if data.verdict == "APPROVED" else "review.submit")
        if data.verdict == "APPROVED":
            open_risks = self.db.execute(
                "SELECT 1 FROM risks WHERE project_id = ? AND target_type = ? AND target_id = ? AND resolved = 0 AND severity IN ('fatal', 'major') LIMIT 1",
                (str(project_id), data.target_type, target_id),
            ).fetchone()
            if open_risks:
                raise ValueError("review_blocked_by_open_risks")
        # COST-1：用量超预算同样在批准时校验——"超了"这件事必须有人看见并决定，不能静默放过。
        if data.verdict == "APPROVED" and data.target_type == "task":
            task_row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(target_id),)).fetchone()
            overrun_findings: list[dict[str, Any]] = []
            for run_row in self.db.execute("SELECT usage FROM runs WHERE task_id = ?", (str(target_id),)):
                usage = self._json(run_row["usage"] or "{}")
                for item in self._usage_overruns(task_row, usage):
                    label = "token" if item["code"] == "tokens" else "耗时"
                    unit = " tokens" if item["code"] == "tokens" else " 秒"
                    overrun_findings.append(
                        {
                            "code": "task:usage_budget_exceeded",
                            "severity": "major",
                            "message": f"执行超出{label}预算：本次 {item['reported']}{unit} / 上限 {item['limit']}{unit}",
                            "resolved": False,
                        }
                    )
            if overrun_findings:
                findings = normalize_findings(overrun_findings)
                self._ensure_gate(project_id, data.target_type, data.target_id)
                self.db.execute(
                    "UPDATE gates SET status = 'FAILED', blocking_findings = ?, risk_summary = ? "
                    "WHERE project_id = ? AND target_type = ? AND target_id = ?",
                    (
                        json.dumps(findings, ensure_ascii=False),
                        json.dumps(risk_summary(findings), ensure_ascii=False),
                        str(project_id),
                        data.target_type,
                        str(target_id),
                    ),
                )
                self.db.commit()
                raise ValueError("task_usage_budget_exceeded:" + overrun_findings[0]["message"])
        # 意图对象（AIP-1d）：任务的**显式证据要求**在批准时校验（非空才生效，空 = 零行为变化）。
        # 做法：把"缺证据"落成门禁的 blocking finding（可审计），再拒绝这次批准——
        # 不自动批准、也不阻止提交复核；证据补上后重新提交即可通过。
        if data.verdict == "APPROVED" and data.target_type == "task":
            gaps = [gap for gap in self.task_evidence_gaps(target_id) if gap["missing"] > 0]
            if gaps:
                findings = normalize_findings(
                    [
                        {
                            "code": "task:evidence_requirements",
                            "severity": "major",
                            "message": f"证据要求未满足：{gap['evidence_type']} 需 {gap['required']} 条、现有 {gap['present']} 条"
                            + (f"（{gap['note']}）" if gap["note"] else ""),
                            "resolved": False,
                        }
                        for gap in gaps
                    ]
                )
                self._ensure_gate(project_id, data.target_type, data.target_id)
                self.db.execute(
                    "UPDATE gates SET status = 'FAILED', blocking_findings = ?, risk_summary = ? "
                    "WHERE project_id = ? AND target_type = ? AND target_id = ?",
                    (
                        json.dumps(findings, ensure_ascii=False),
                        json.dumps(risk_summary(findings), ensure_ascii=False),
                        str(project_id),
                        data.target_type,
                        str(target_id),
                    ),
                )
                self.db.commit()
                detail = "、".join(f"{gap['evidence_type']} 缺 {gap['missing']} 条" for gap in gaps)
                raise ValueError(f"task_evidence_requirements_unmet:{detail}")
        try:
            self._ensure_gate(project_id, data.target_type, data.target_id)
            if data.target_type == "task":
                current = target["status"]
                if data.verdict == "APPROVED" and current not in {"WAITING_REVIEW", "NEEDS_REVISION", "APPROVED"}:
                    raise ValueError("task_not_waiting_for_review")
                if data.verdict != "APPROVED" and current == "APPROVED":
                    raise ValueError("approved_task_is_immutable")
                next_status = {"APPROVED": "APPROVED", "NEEDS_REVISION": "NEEDS_REVISION", "BLOCKED": "BLOCKED"}[data.verdict]
                self.db.execute("UPDATE tasks SET status = ?, blocked_reason = ?, updated_at = ? WHERE id = ?", (next_status, data.summary if next_status == "BLOCKED" else None, now(), target_id))
            elif data.target_type == "artifact":
                if data.verdict == "APPROVED":
                    self.db.execute("UPDATE artifacts SET status = 'APPROVED', approved_by = ?, approved_at = ?, downstream_allowed = 1, immutable = 1 WHERE id = ?", (data.reviewer, now(), target_id))
                elif data.verdict in {"NEEDS_REVISION", "BLOCKED"}:
                    self.db.execute("UPDATE artifacts SET status = 'REJECTED', downstream_allowed = 0 WHERE id = ?", (target_id,))
            elif data.target_type == "handoff":
                if data.verdict == "APPROVED" and target["receipt_status"] == "REJECTED":
                    raise ValueError("rejected_handoff_requires_revision")
                next_status = {"APPROVED": "PASS", "NEEDS_REVISION": "NEEDS_REVISION", "BLOCKED": "BLOCKED"}[data.verdict]
                self.db.execute("UPDATE handoffs SET status = ? WHERE id = ?", (next_status, target_id))
            gate_status = {"APPROVED": "PASSED", "NEEDS_REVISION": "FAILED", "BLOCKED": "BLOCKED"}[data.verdict]
            approved_at = now() if data.verdict == "APPROVED" else None
            review_id = str(uuid4())
            timestamp = now()
            current_gate = self.db.execute("SELECT review_ids, evidence_ids FROM gates WHERE project_id = ? AND target_type = ? AND target_id = ?", (str(project_id), data.target_type, target_id)).fetchone()
            review_ids = self._json(current_gate["review_ids"] if current_gate else "[]")
            evidence_ids = self._json(current_gate["evidence_ids"] if current_gate else "[]")
            review_ids.append(review_id)
            evidence_ids.extend(str(value) for value in data.evidence_ids if str(value) not in evidence_ids)
            summary = risk_summary(normalized_findings)
            self.db.execute("INSERT INTO reviews (id, project_id, target_type, target_id, verdict, summary, findings, evidence_ids, reviewer, reviewer_kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (review_id, str(project_id), data.target_type, str(data.target_id), data.verdict, data.summary, json.dumps(normalized_findings, ensure_ascii=False), json.dumps([str(value) for value in data.evidence_ids]), data.reviewer, reviewer_kind, timestamp))
            for finding in normalized_findings:
                self.db.execute(
                    "INSERT INTO risks (id, project_id, review_id, target_type, target_id, code, severity, message, resolved, evidence_refs, owner, resolution_reason, resolved_by, resolved_at, closure_evidence_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, NULL, NULL, '[]', ?)",
                    (str(uuid4()), str(project_id), review_id, data.target_type, target_id, finding["code"], finding["severity"], finding["message"], json.dumps(finding.get("evidence_refs", []), ensure_ascii=False), timestamp),
                )
            snapshot = self._capture_gate_snapshot(project_id, data.target_type, data.target_id, review_ids, evidence_ids)
            self.db.execute("UPDATE gates SET input_snapshot = ?, invalidated_at = NULL, invalidation_reason = NULL WHERE project_id = ? AND target_type = ? AND target_id = ?", (json.dumps(snapshot, ensure_ascii=False), str(project_id), data.target_type, target_id))
            self.db.execute("UPDATE gates SET status = ?, blocking_findings = ?, rules = ?, review_ids = ?, evidence_ids = ?, risk_summary = ?, approved_by = ?, approved_at = ? WHERE project_id = ? AND target_type = ? AND target_id = ?", (gate_status, json.dumps(blocking_findings(normalized_findings), ensure_ascii=False), json.dumps(gate_rules(data.target_type)), json.dumps(review_ids), json.dumps(evidence_ids), json.dumps(summary, ensure_ascii=False), data.reviewer if data.verdict == "APPROVED" else None, approved_at, str(project_id), data.target_type, target_id))
            self._insert_event(
                project_id,
                "review.created",
                data.reviewer,
                {"review_id": review_id, "target_type": data.target_type, "target_id": target_id, "verdict": data.verdict, "reviewer_kind": reviewer_kind, "risk_summary": summary},
                actor_kind=reviewer_kind,
                object_type="review",
                object_id=UUID(review_id),
            )
            result = self._review(self.db.execute("SELECT * FROM reviews WHERE id = ?", (review_id,)).fetchone())
            if data.idempotency_key:
                self.db.execute(
                    "INSERT OR IGNORE INTO idempotency_records (key, operation, response, request_hash, created_at) VALUES (?, 'review.create', ?, ?, ?)",
                    (data.idempotency_key, json.dumps(result.model_dump(mode="json"), ensure_ascii=False), request_fingerprint, timestamp),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return result

    def list_reviews(self, project_id: UUID) -> list[Review]:
        return [self._review(row) for row in self.db.execute("SELECT * FROM reviews WHERE project_id = ? ORDER BY created_at DESC", (str(project_id),))]

    def _risk(self, row: sqlite3.Row) -> RiskRegistryEntry:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["project_id"] = UUID(values["project_id"])
        values["review_id"] = UUID(values["review_id"])
        values["target_id"] = UUID(values["target_id"])
        values["resolved"] = bool(values["resolved"])
        values["evidence_refs"] = self._json(values.get("evidence_refs") or "[]")
        values["closure_evidence_ids"] = [UUID(value) for value in self._json(values.get("closure_evidence_ids") or "[]")]
        values["resolved_at"] = parse_time(values["resolved_at"]) if values.get("resolved_at") else None
        values["created_at"] = parse_time(values["created_at"])
        return RiskRegistryEntry(**values)

    def list_risks(self, project_id: UUID) -> list[RiskRegistryEntry]:
        return [self._risk(row) for row in self.db.execute("SELECT * FROM risks WHERE project_id = ? ORDER BY created_at DESC", (str(project_id),))]

    def update_risk(self, project_id: UUID, risk_id: UUID, data: RiskDecisionRequest, actor: str = "member-001") -> RiskRegistryEntry:
        request_fingerprint = self._request_hash({"project_id": str(project_id), "risk_id": str(risk_id), **data.model_dump(mode="json", exclude={"idempotency_key"})})
        if data.idempotency_key:
            previous = self._operation(data.idempotency_key, "risk.update", request_fingerprint)
            if previous:
                return RiskRegistryEntry.model_validate(previous)
        row = self.db.execute("SELECT * FROM risks WHERE id = ? AND project_id = ?", (str(risk_id), str(project_id))).fetchone()
        if not row:
            raise KeyError("risk_not_found")
        evidence_ids = [str(value) for value in data.evidence_ids]
        for evidence_id in evidence_ids:
            evidence = self.db.execute("SELECT project_id FROM evidence WHERE id = ?", (evidence_id,)).fetchone()
            if not evidence or evidence["project_id"] != str(project_id):
                raise ValueError("risk_closure_evidence_not_in_project")
        if data.action == "ASSIGN" and not data.owner:
            raise ValueError("risk_owner_required")
        if data.action == "RESOLVE" and (not data.reason or not evidence_ids):
            raise ValueError("risk_resolution_reason_and_evidence_required")
        timestamp = now()
        if data.action == "ASSIGN":
            self.db.execute("UPDATE risks SET owner = ? WHERE id = ?", (data.owner, str(risk_id)))
        elif data.action == "RESOLVE":
            self.db.execute("UPDATE risks SET resolved = 1, owner = COALESCE(?, owner), resolution_reason = ?, resolved_by = ?, resolved_at = ?, closure_evidence_ids = ? WHERE id = ?", (data.owner, data.reason, actor, timestamp, json.dumps(evidence_ids), str(risk_id)))
        else:
            self.db.execute("UPDATE risks SET resolved = 0, resolution_reason = NULL, resolved_by = NULL, resolved_at = NULL, closure_evidence_ids = '[]' WHERE id = ?", (str(risk_id),))
            gate = self.db.execute("SELECT * FROM gates WHERE project_id = ? AND target_type = ? AND target_id = ?", (str(project_id), row["target_type"], row["target_id"])).fetchone()
            if gate and row["severity"] in {"fatal", "major"} and gate["status"] == "PASSED":
                self.db.execute("UPDATE gates SET status = 'BLOCKED', invalidated_at = ?, invalidation_reason = ?, approved_by = NULL, approved_at = NULL WHERE id = ?", (timestamp, "risk_reopened", gate["id"]))
                self._propagate_gate_invalidation(gate, "risk_reopened")
        self._insert_event(project_id, "risk.updated", actor, {"risk_id": str(risk_id), "action": data.action, "reason": data.reason, "evidence_ids": evidence_ids}, actor_kind="member", object_type="risk", object_id=risk_id)
        result = self._risk(self.db.execute("SELECT * FROM risks WHERE id = ?", (str(risk_id),)).fetchone())
        if data.idempotency_key:
            self._save_operation(data.idempotency_key, "risk.update", result.model_dump(mode="json"), request_hash=request_fingerprint, commit=False)
        self.db.commit()
        return result

    def export_project_bundle(self, project_id: UUID, destination: str | Path) -> Path:
        project = self.get_project(project_id)
        target = Path(destination).expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        artifacts = self.list_artifacts(project_id)
        project_id_text = str(project_id)
        organization = self.db.execute("SELECT * FROM organizations WHERE id = ?", (str(project.organization_id),)).fetchone() if project.organization_id else None
        team = self.db.execute("SELECT * FROM teams WHERE id = ?", (str(project.team_id),)).fetchone() if project.team_id else None
        project_memberships = [dict(row) for row in self.db.execute("SELECT * FROM project_memberships WHERE project_id = ? ORDER BY member_id", (project_id_text,))]
        member_ids = [item["member_id"] for item in project_memberships]
        members: list[dict[str, Any]] = []
        memberships: list[dict[str, Any]] = []
        if member_ids:
            placeholders = ",".join("?" for _ in member_ids)
            members = [dict(row) for row in self.db.execute(f"SELECT * FROM human_members WHERE id IN ({placeholders}) ORDER BY id", member_ids)]
            memberships = [dict(row) for row in self.db.execute(f"SELECT * FROM memberships WHERE member_id IN ({placeholders}) ORDER BY member_id, team_id", member_ids)]
        agent_grants = [dict(row) for row in self.db.execute("SELECT * FROM agent_project_grants WHERE project_id = ? ORDER BY agent_id", (project_id_text,))]
        agent_ids = [item["agent_id"] for item in agent_grants]
        agents: list[dict[str, Any]] = []
        if agent_ids:
            placeholders = ",".join("?" for _ in agent_ids)
            agents = [dict(row) for row in self.db.execute(f"SELECT * FROM agents WHERE agent_id IN ({placeholders}) ORDER BY agent_id", agent_ids)]
        git_repository_row = self.db.execute("SELECT * FROM git_repositories WHERE project_id = ?", (project_id_text,)).fetchone()
        git_repository = dict(git_repository_row) if git_repository_row else None
        if git_repository:
            try:
                git_repository["head_commit"] = LocalGitProvider(git_repository["local_path"]).head()
            except ValueError:
                git_repository["head_commit"] = None
        object_contents: dict[str, bytes] = {}
        object_checksums: list[dict[str, Any]] = []
        for artifact in artifacts:
            if not artifact.storage_key:
                continue
            try:
                content = self.object_store.get_bytes(artifact.storage_key)
            except KeyError as error:
                raise ValueError(f"artifact_object_missing:{artifact.id}") from error
            if hashlib.sha256(content).hexdigest() != artifact.content_hash:
                raise ValueError(f"artifact_hash_mismatch:{artifact.id}")
            if artifact.size_bytes is not None and len(content) != artifact.size_bytes:
                raise ValueError(f"artifact_size_mismatch:{artifact.id}")
            object_contents[artifact.storage_key] = content
            object_checksums.append({"path": f"objects/{artifact.storage_key}", "sha256": artifact.content_hash, "size_bytes": len(content)})
        manifest = {
            "schema_version": "1.1",
            "exported_at": now(),
            "object_checksums": object_checksums,
            "organization": dict(organization) if organization else None,
            "team": dict(team) if team else None,
            "members": members,
            "memberships": memberships,
            "project_memberships": project_memberships,
            "agents": agents,
            "agent_project_grants": agent_grants,
            "project": project.model_dump(mode="json"),
            "tasks": [item.model_dump(mode="json") for item in self.list_tasks(project_id)],
            "handoffs": [item.model_dump(mode="json") for item in self.list_handoffs(project_id)],
            "artifacts": [item.model_dump(mode="json") for item in artifacts],
            "runs": [item.model_dump(mode="json") for item in self.list_runs(project_id)],
            "reviews": [item.model_dump(mode="json") for item in self.list_reviews(project_id)],
            "gates": [item.model_dump(mode="json") for item in self.list_gates(project_id)],
            "evidence": [item.model_dump(mode="json") for item in self.list_evidence(project_id)],
            "risks": [item.model_dump(mode="json") for item in self.list_risks(project_id)],
            "events": [item.model_dump(mode="json") for item in self.list_events(project_id, limit=100000)],
            "git_repository": git_repository,
            "git_index": [item.model_dump(mode="json") for item in self.list_git_index(project_id)],
        }
        with ZipFile(target, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            for artifact in artifacts:
                if not artifact.storage_key:
                    continue
                archive.writestr(f"objects/{artifact.storage_key}", object_contents[artifact.storage_key])
        return target

    def restore_project_bundle(self, bundle_path: str | Path) -> Project:
        source = Path(bundle_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError("bundle_not_found")
        with ZipFile(source, "r") as archive:
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("invalid_project_bundle") from error
            if manifest.get("schema_version") not in {"1.0", "1.1"} or not isinstance(manifest.get("project"), dict):
                raise ValueError("unsupported_project_bundle_schema")
            project_data = manifest["project"]
            try:
                project_id = str(UUID(str(project_data["id"])))
            except (KeyError, ValueError, TypeError) as error:
                raise ValueError("invalid_project_id") from error
            if self.db.execute("SELECT 1 FROM projects WHERE id = ?", (project_id,)).fetchone():
                raise ValueError("project_already_exists")
            self._validate_project_bundle_manifest(manifest)
            object_contents: dict[str, bytes] = {}
            checksum_entries = manifest.get("object_checksums", [])
            checksum_by_path: dict[str, dict[str, Any]] = {}
            for entry in checksum_entries:
                path = entry.get("path")
                if not isinstance(path, str) or path in checksum_by_path:
                    raise ValueError("invalid_bundle_object_checksums")
                checksum_by_path[path] = entry
            expected_prefix = f"projects/{project_id}/"
            for artifact in manifest.get("artifacts", []):
                storage_key = artifact.get("storage_key")
                if not storage_key:
                    continue
                normalized_key = storage_key.replace("\\", "/").lstrip("/")
                if normalized_key != storage_key or not normalized_key.startswith(expected_prefix) or ".." in Path(normalized_key).parts:
                    raise ValueError(f"artifact_storage_key_outside_project:{artifact['id']}")
                try:
                    content = archive.read(f"objects/{normalized_key}")
                except KeyError as error:
                    raise ValueError(f"artifact_object_missing:{artifact['id']}") from error
                if hashlib.sha256(content).hexdigest() != artifact["content_hash"]:
                    raise ValueError(f"artifact_hash_mismatch:{artifact['id']}")
                if artifact.get("size_bytes") is not None and len(content) != artifact["size_bytes"]:
                    raise ValueError(f"artifact_size_mismatch:{artifact['id']}")
                checksum = checksum_by_path.get(f"objects/{normalized_key}")
                if checksum is not None and (checksum.get("sha256") != artifact["content_hash"] or checksum.get("size_bytes") != len(content)):
                    raise ValueError(f"bundle_checksum_mismatch:{artifact['id']}")
                object_contents[normalized_key] = content
            expected_object_paths = {f"objects/{key}" for key in object_contents}
            if checksum_entries and set(checksum_by_path) != expected_object_paths:
                raise ValueError("bundle_object_checksum_entries_mismatch")
            organization_id = project_data.get("organization_id") or DEV_ORG_ID
            team_id = project_data.get("team_id") or DEV_TEAM_ID
            creator = project_data.get("created_by") or "member-001"
            organization = manifest.get("organization") or {"id": organization_id, "name": "恢复组织", "slug": f"restored-{organization_id[:8]}", "created_at": now()}
            team = manifest.get("team") or {"id": team_id, "organization_id": organization_id, "name": "恢复队伍", "created_at": now()}
            members = manifest.get("members") or [{"id": creator, "organization_id": organization_id, "team_id": team_id, "email": f"{creator}@restored.local", "display_name": creator, "status": "active", "created_at": now()}]
            project_memberships = manifest.get("project_memberships") or [{"project_id": project_id, "member_id": creator, "role": "project_lead", "created_at": now()}]
            written_keys: list[str] = []
            try:
                with self.db:
                    self.db.execute("INSERT OR IGNORE INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)", (organization["id"], organization["name"], organization["slug"], organization["created_at"]))
                    self.db.execute("INSERT OR IGNORE INTO teams (id, organization_id, name, created_at) VALUES (?, ?, ?, ?)", (team["id"], team["organization_id"], team["name"], team["created_at"]))
                    for member in members:
                        self.db.execute("INSERT OR IGNORE INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (member["id"], member["organization_id"], member.get("team_id"), member["email"], member["display_name"], member["status"], member["created_at"]))
                    for membership in manifest.get("memberships", []):
                        self.db.execute("INSERT OR IGNORE INTO memberships (member_id, team_id, role, created_at) VALUES (?, ?, ?, ?)", (membership["member_id"], membership["team_id"], membership["role"], membership["created_at"]))
                    self.db.execute("INSERT INTO projects (id, organization_id, team_id, created_by, name, competition_pack, problem_code, description, stage, progress, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (project_id, organization_id, team_id, creator, project_data["name"], project_data["competition_pack"], project_data.get("problem_code"), project_data["description"], project_data["stage"], project_data["progress"], project_data["created_at"], project_data["updated_at"]))
                    for membership in project_memberships:
                        self.db.execute("INSERT INTO project_memberships (project_id, member_id, role, created_at) VALUES (?, ?, ?, ?)", (project_id, membership["member_id"], membership["role"], membership["created_at"]))
                    for agent in manifest.get("agents", []):
                        # 快照恢复要带上 AIP-1 的两段身份与能力卡，否则恢复后描述信息全丢
                        self.db.execute("INSERT OR IGNORE INTO agents (agent_id, display_name, owner_member_id, model_provider, model_name, supported_tools, supported_languages, max_concurrency, local_workspace, network_policy, status, last_seen, capability_cards, package_id, instance_id, package_source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (agent["agent_id"], agent["display_name"], agent["owner_member_id"], agent["model_provider"], agent["model_name"], agent["supported_tools"], agent["supported_languages"], agent["max_concurrency"], agent.get("local_workspace"), agent["network_policy"], agent["status"], agent["last_seen"], agent.get("capability_cards") or "[]", agent.get("package_id"), agent.get("instance_id") or agent["agent_id"], agent.get("package_source") or "inferred"))
                    for grant in manifest.get("agent_project_grants", []):
                        self.db.execute("INSERT INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES (?, ?, ?, ?, ?)", (grant["agent_id"], project_id, grant["capabilities"], grant["granted_by"], grant["created_at"]))
                    for task in manifest.get("tasks", []):
                        self.db.execute("INSERT INTO tasks (id, project_id, title, description, stage, status, assignee, priority, requires_review, allow_future_data, input_artifacts, input_handoff_ids, output_types, parent_task_id, dependency_task_ids, acceptance_criteria, required_capabilities, budget, evidence_requirements, deadline, information_boundary, resource_policy, requires_human_approval, blocked_reason, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (task["id"], project_id, task["title"], task["description"], task["stage"], task["status"], task["assignee"], task["priority"], int(task["requires_review"]), int(task["allow_future_data"]), json.dumps(task["input_artifacts"]), json.dumps(task.get("input_handoff_ids", [])), json.dumps(task["output_types"]), task.get("parent_task_id"), json.dumps(task.get("dependency_task_ids", [])), json.dumps(task.get("acceptance_criteria", []), ensure_ascii=False), json.dumps(task.get("required_capabilities", [])), json.dumps(task.get("budget") or {}, ensure_ascii=False), json.dumps(task.get("evidence_requirements") or [], ensure_ascii=False), task.get("deadline"), json.dumps(task.get("information_boundary", {}), ensure_ascii=False), json.dumps(task.get("resource_policy", {}), ensure_ascii=False), int(task.get("requires_human_approval", True)), task.get("blocked_reason"), task["updated_at"]))
                    for run in manifest.get("runs", []):
                        self.db.execute("INSERT INTO runs (id, project_id, task_id, agent_id, status, source_commit, input_artifact_ids, environment_image_digest, dependency_lock, parameters, random_seed, model_provider, model_name, tool_versions, network_policy, execution_profile, data_access_policy, observed_input_files, output_artifact_ids, stdout, stderr, summary, information_boundary, started_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (run["id"], project_id, run.get("task_id"), run["agent_id"], run["status"], run.get("source_commit"), json.dumps(run.get("input_artifact_ids", [])), run.get("environment_image_digest"), run.get("dependency_lock"), json.dumps(run.get("parameters", {}), ensure_ascii=False), run.get("random_seed"), run.get("model_provider"), run.get("model_name"), json.dumps(run.get("tool_versions", {}), ensure_ascii=False), run["network_policy"], json.dumps(run.get("execution_profile", {"mode": "HEADLESS", "requires_user_session": False, "allow_remote_terminal": False, "allow_desktop_control": False, "network_policy": run["network_policy"], "auto_retry": False}), ensure_ascii=False), json.dumps(run.get("data_access_policy", {}), ensure_ascii=False), json.dumps(run.get("observed_input_files", []), ensure_ascii=False), json.dumps(run.get("output_artifact_ids", [])), run.get("stdout", ""), run.get("stderr", ""), run.get("summary", ""), json.dumps(run.get("information_boundary", {}), ensure_ascii=False), run["started_at"], run.get("completed_at")))
                    for artifact in manifest.get("artifacts", []):
                        storage_key = artifact.get("storage_key")
                        if storage_key:
                            stored = self.object_store.put_bytes(storage_key, object_contents[storage_key], artifact.get("mime_type"))
                            written_keys.append(storage_key)
                            if stored.content_hash != artifact["content_hash"] or (artifact.get("size_bytes") is not None and stored.size_bytes != artifact["size_bytes"]):
                                raise ValueError(f"artifact_object_store_mismatch:{artifact['id']}")
                        self.db.execute("INSERT INTO artifacts (id, project_id, name, artifact_type, description, content_hash, version, status, source_path, task_id, run_id, created_by, created_at, data_policy, input_artifact_ids, git_commit, snapshot_ref, created_by_kind, approved_by, approved_at, downstream_allowed, storage_key, size_bytes, mime_type, immutable, parent_artifact_id, archived_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (artifact["id"], project_id, artifact["name"], artifact["artifact_type"], artifact["description"], artifact["content_hash"], artifact["version"], artifact["status"], artifact.get("source_path"), artifact.get("task_id"), artifact.get("run_id"), artifact["created_by"], artifact["created_at"], json.dumps(artifact["data_policy"], ensure_ascii=False), json.dumps(artifact.get("input_artifact_ids", [])), artifact.get("git_commit"), artifact.get("snapshot_ref"), artifact.get("created_by_kind", "member"), artifact.get("approved_by"), artifact.get("approved_at"), int(artifact.get("downstream_allowed", False)), storage_key, artifact.get("size_bytes"), artifact.get("mime_type"), int(artifact.get("immutable", False)), artifact.get("parent_artifact_id"), artifact.get("archived_at")))
                    for handoff in manifest.get("handoffs", []):
                        receiver = json.dumps(handoff["receiver"], ensure_ascii=False) if isinstance(handoff["receiver"], (dict, list)) else handoff["receiver"]
                        self.db.execute("INSERT INTO handoffs (id, project_id, task_id, sender_agent_id, receiver, status, objective, completed, input_artifacts, output_artifacts, key_conclusions, assumptions, evidence_refs, open_questions, risks, next_actions, requires_human_approval, schema_version, handoff_type, input_handoff_ids, revision_of_handoff_id, revision_number, receipt_status, received_by, received_at, decision_reason, decision_findings, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (handoff["id"], project_id, handoff["task_id"], handoff["sender_agent_id"], receiver, handoff["status"], handoff["objective"], json.dumps(handoff.get("completed", []), ensure_ascii=False), json.dumps(handoff.get("input_artifacts", []), ensure_ascii=False), json.dumps(handoff.get("output_artifacts", []), ensure_ascii=False), json.dumps(handoff.get("key_conclusions", []), ensure_ascii=False), json.dumps(handoff.get("assumptions", []), ensure_ascii=False), json.dumps(handoff.get("evidence_refs", []), ensure_ascii=False), json.dumps(handoff.get("open_questions", []), ensure_ascii=False), json.dumps(handoff.get("risks", []), ensure_ascii=False), json.dumps(handoff.get("next_actions", []), ensure_ascii=False), int(handoff.get("requires_human_approval", True)), handoff.get("schema_version", "1.0"), handoff.get("handoff_type", "RELAY"), json.dumps(handoff.get("input_handoff_ids", []), ensure_ascii=False), handoff.get("revision_of_handoff_id"), int(handoff.get("revision_number", 1)), handoff.get("receipt_status", "PENDING"), handoff.get("received_by"), handoff.get("received_at"), handoff.get("decision_reason"), json.dumps(handoff.get("decision_findings", []), ensure_ascii=False), handoff.get("idempotency_key"), handoff["created_at"]))
                        for receipt in handoff.get("receipts", []):
                            self.db.execute("INSERT INTO handoff_receipts (id, handoff_id, project_id, receiver_type, receiver_id, status, received_by, received_at, decision_reason, decision_findings, idempotency_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (receipt["id"], handoff["id"], project_id, receipt["receiver_type"], receipt["receiver_id"], receipt.get("status", "PENDING"), receipt.get("received_by"), receipt.get("received_at"), receipt.get("decision_reason"), json.dumps(receipt.get("decision_findings", []), ensure_ascii=False), receipt.get("idempotency_key"), receipt["created_at"]))
                    for review in manifest.get("reviews", []):
                        self.db.execute("INSERT INTO reviews (id, project_id, target_type, target_id, verdict, summary, findings, evidence_ids, reviewer, reviewer_kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (review["id"], project_id, review["target_type"], review["target_id"], review["verdict"], review["summary"], json.dumps(review.get("findings", []), ensure_ascii=False), json.dumps(review.get("evidence_ids", []), ensure_ascii=False), review["reviewer"], review.get("reviewer_kind", "agent"), review["created_at"]))
                    for gate in manifest.get("gates", []):
                        self.db.execute("INSERT INTO gates (id, project_id, target_type, target_id, status, required_human_approval, blocking_findings, rules, review_ids, evidence_ids, risk_summary, input_snapshot, invalidated_at, invalidation_reason, approved_by, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (gate["id"], project_id, gate["target_type"], gate.get("target_id"), gate["status"], int(gate.get("required_human_approval", True)), json.dumps(gate.get("blocking_findings", []), ensure_ascii=False), json.dumps(gate.get("rules", []), ensure_ascii=False), json.dumps(gate.get("review_ids", []), ensure_ascii=False), json.dumps(gate.get("evidence_ids", []), ensure_ascii=False), json.dumps(gate.get("risk_summary", {}), ensure_ascii=False), json.dumps(gate.get("input_snapshot", {}), ensure_ascii=False), gate.get("invalidated_at"), gate.get("invalidation_reason"), gate.get("approved_by"), gate.get("approved_at")))
                    for evidence in manifest.get("evidence", []):
                        self.db.execute("INSERT INTO evidence (id, project_id, claim, evidence_type, artifact_id, run_id, source_ref, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (evidence["id"], project_id, evidence["claim"], evidence["evidence_type"], evidence.get("artifact_id"), evidence.get("run_id"), evidence.get("source_ref"), evidence["created_by"], evidence["created_at"]))
                    for risk in manifest.get("risks", []):
                        self.db.execute("INSERT INTO risks (id, project_id, review_id, target_type, target_id, code, severity, message, resolved, evidence_refs, owner, resolution_reason, resolved_by, resolved_at, closure_evidence_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (risk["id"], project_id, risk["review_id"], risk["target_type"], risk["target_id"], risk["code"], risk["severity"], risk["message"], int(risk.get("resolved", False)), json.dumps(risk.get("evidence_refs", []), ensure_ascii=False), risk.get("owner"), risk.get("resolution_reason"), risk.get("resolved_by"), risk.get("resolved_at"), json.dumps(risk.get("closure_evidence_ids", []), ensure_ascii=False), risk["created_at"]))
                    for event in manifest.get("events", []):
                        self.db.execute("INSERT INTO events (id, project_id, sequence, event_type, actor, payload, created_at, actor_kind, object_type, object_id, idempotency_key, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (event["id"], project_id, event["sequence"], event["event_type"], event["actor"], json.dumps(event.get("payload", {}), ensure_ascii=False), event["created_at"], event.get("actor_kind", "system"), event.get("object_type"), event.get("object_id"), event.get("idempotency_key"), event.get("schema_version", "1.0")))
                        self.db.execute("INSERT INTO event_outbox (id, event_id, project_id, status, attempts, available_at, locked_at, lock_expires_at, delivered_at, last_error, created_at, updated_at) VALUES (?, ?, ?, 'PENDING', 0, ?, NULL, NULL, NULL, NULL, ?, ?)", (event["id"], event["id"], project_id, event["created_at"], event["created_at"], event["created_at"]))
                    git_repository = manifest.get("git_repository")
                    if git_repository:
                        self.db.execute("INSERT INTO git_repositories (project_id, provider, remote_url, local_path) VALUES (?, ?, ?, ?)", (project_id, git_repository["provider"], git_repository.get("remote_url"), git_repository["local_path"]))
                    for item in manifest.get("git_index", []):
                        self.db.execute("INSERT INTO git_file_indexes (project_id, commit_sha, path, content_hash, size_bytes, indexed_at) VALUES (?, ?, ?, ?, ?, ?)", (project_id, item["commit_sha"], item["path"], item["content_hash"], item["size_bytes"], item["indexed_at"]))
            except Exception:
                for key in written_keys:
                    self.object_store.delete(key)
                raise
        return self.get_project(UUID(project_id))

    @staticmethod
    def inspect_project_bundle(bundle_path: str | Path) -> dict[str, str | None]:
        """Read only the identity metadata needed before authorizing restore."""
        source = Path(bundle_path).expanduser().resolve()
        if not source.is_file():
            raise ValueError("bundle_not_found")
        try:
            with ZipFile(source, "r") as archive:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            project = manifest.get("project")
            if manifest.get("schema_version") not in {"1.0", "1.1"} or not isinstance(project, dict):
                raise ValueError("unsupported_project_bundle_schema")
            project_id = str(UUID(str(project["id"])))
            organization_id = project.get("organization_id")
            if organization_id is not None:
                organization_id = str(UUID(str(organization_id)))
            return {"project_id": project_id, "organization_id": organization_id}
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            if isinstance(error, ValueError) and str(error) in {"unsupported_project_bundle_schema"}:
                raise
            raise ValueError("invalid_project_bundle_identity") from error

    @staticmethod
    def _validate_project_bundle_manifest(manifest: dict[str, Any]) -> None:
        project_id = str(manifest["project"]["id"])
        collections = {
            "task": manifest.get("tasks", []),
            "handoff": manifest.get("handoffs", []),
            "artifact": manifest.get("artifacts", []),
            "run": manifest.get("runs", []),
            "review": manifest.get("reviews", []),
            "gate": manifest.get("gates", []),
            "evidence": manifest.get("evidence", []),
            "risk": manifest.get("risks", []),
            "event": manifest.get("events", []),
        }
        ids: dict[str, set[str]] = {}
        for name, items in collections.items():
            item_ids: list[str] = []
            for item in items:
                try:
                    item_ids.append(str(UUID(str(item["id"]))))
                except (KeyError, ValueError, TypeError) as error:
                    raise ValueError(f"invalid_bundle_{name}_id") from error
            if len(item_ids) != len(set(item_ids)):
                raise ValueError(f"duplicate_bundle_{name}_id")
            if any(str(item.get("project_id")) != project_id for item in items):
                raise ValueError(f"bundle_{name}_project_mismatch")
            ids[name] = set(item_ids)
        task_ids = ids["task"]
        handoff_ids = ids["handoff"]
        artifact_ids = ids["artifact"]
        run_ids = ids["run"]
        for task in collections["task"]:
            references = [*task.get("dependency_task_ids", [])]
            if task.get("parent_task_id"):
                references.append(task["parent_task_id"])
            if any(str(value) not in task_ids for value in references):
                raise ValueError(f"bundle_task_reference_missing:{task['id']}")
            if any(str(value) not in handoff_ids for value in task.get("input_handoff_ids", [])):
                raise ValueError(f"bundle_task_handoff_missing:{task['id']}")
            if any(str(value) not in artifact_ids for value in task.get("input_artifacts", [])):
                raise ValueError(f"bundle_task_artifact_missing:{task['id']}")
        for handoff in collections["handoff"]:
            if str(handoff["task_id"]) not in task_ids:
                raise ValueError(f"bundle_handoff_task_missing:{handoff['id']}")
            if any(str(value) not in handoff_ids for value in handoff.get("input_handoff_ids", [])):
                raise ValueError(f"bundle_handoff_input_missing:{handoff['id']}")
            if handoff.get("revision_of_handoff_id") and str(handoff["revision_of_handoff_id"]) not in handoff_ids:
                raise ValueError(f"bundle_handoff_revision_missing:{handoff['id']}")
            for reference in [*handoff.get("input_artifacts", []), *handoff.get("output_artifacts", []), *handoff.get("evidence_refs", [])]:
                try:
                    reference_id = str(UUID(str(reference)))
                except (ValueError, TypeError):
                    continue
                if reference_id not in artifact_ids:
                    raise ValueError(f"bundle_handoff_artifact_missing:{handoff['id']}")
        for artifact in collections["artifact"]:
            if artifact.get("task_id") and str(artifact["task_id"]) not in task_ids:
                raise ValueError(f"bundle_artifact_task_missing:{artifact['id']}")
            if artifact.get("run_id") and str(artifact["run_id"]) not in run_ids:
                raise ValueError(f"bundle_artifact_run_missing:{artifact['id']}")
            references = [*artifact.get("input_artifact_ids", [])]
            if artifact.get("parent_artifact_id"):
                references.append(artifact["parent_artifact_id"])
            if any(str(value) not in artifact_ids for value in references):
                raise ValueError(f"bundle_artifact_reference_missing:{artifact['id']}")
        for run in collections["run"]:
            if run.get("task_id") and str(run["task_id"]) not in task_ids:
                raise ValueError(f"bundle_run_task_missing:{run['id']}")
            references = [*run.get("input_artifact_ids", []), *run.get("output_artifact_ids", [])]
            if any(str(value) not in artifact_ids for value in references):
                raise ValueError(f"bundle_run_artifact_missing:{run['id']}")
        target_ids = {
            "task": task_ids,
            "artifact": artifact_ids,
            "handoff": handoff_ids,
            "run": run_ids,
            "review": ids["review"],
            "gate": ids["gate"],
            "evidence": ids["evidence"],
            "risk": ids["risk"],
            "project": {project_id},
        }
        for item in [*collections["review"], *collections["gate"]]:
            target_type = item["target_type"]
            target_id = item.get("target_id")
            if target_id is not None and (target_type not in target_ids or str(target_id) not in target_ids[target_type]):
                raise ValueError(f"bundle_target_missing:{item['id']}")
        for evidence in collections["evidence"]:
            if evidence.get("artifact_id") and str(evidence["artifact_id"]) not in artifact_ids:
                raise ValueError(f"bundle_evidence_artifact_missing:{evidence['id']}")
            if evidence.get("run_id") and str(evidence["run_id"]) not in run_ids:
                raise ValueError(f"bundle_evidence_run_missing:{evidence['id']}")
        for handoff in collections["handoff"]:
            for receipt in handoff.get("receipts", []):
                if str(receipt.get("handoff_id")) != str(handoff["id"]):
                    raise ValueError(f"bundle_handoff_receipt_mismatch:{handoff['id']}")
        for risk in collections["risk"]:
            if str(risk.get("review_id")) not in ids["review"]:
                raise ValueError(f"bundle_risk_review_missing:{risk['id']}")
            if any(str(value) not in ids["evidence"] for value in risk.get("closure_evidence_ids", [])):
                raise ValueError(f"bundle_risk_evidence_missing:{risk['id']}")
        for event in collections["event"]:
            object_type = event.get("object_type")
            object_id = event.get("object_id")
            if object_type:
                if not object_id or object_type not in target_ids or str(object_id) not in target_ids[object_type]:
                    raise ValueError(f"bundle_event_object_missing:{event['id']}")
        git_repository = manifest.get("git_repository")
        if git_repository and str(git_repository.get("project_id")) != project_id:
            raise ValueError("bundle_git_repository_project_mismatch")
        if any(str(item.get("project_id")) != project_id for item in manifest.get("git_index", [])):
            raise ValueError("bundle_git_index_project_mismatch")

    def _insert_event(
        self,
        project_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        *,
        actor_kind: str = "system",
        object_type: str | None = None,
        object_id: UUID | None = None,
        idempotency_key: str | None = None,
        schema_version: str = "1.0",
    ) -> Event:
        next_sequence = self.db.execute("SELECT COALESCE(MAX(sequence), 0) + 1 FROM events WHERE project_id = ?", (str(project_id),)).fetchone()[0]
        event_id = str(uuid4())
        timestamp = now()
        if str(event_type).startswith("project.") and str(event_type).count(".") >= 2:
            # 目录咽喉（W2.1）：保留命名空间是**目录形状** `project.<family>.<action>`（≥3 段）。
            # 落在这里的事件必须在 event_catalog 注册且信封合法，未注册名直接拒绝
            # （工作包 C 的契约：发出侧拦截，绝不发出去让消费方猜）。
            # 老轨名字不受影响：`artifact.created`（无前缀）与 `project.seeded`（两段，种子事件）
            # 都只是碰巧长得像，不进目录——只增不改。
            spec = event_catalog.EVENTS.get(str(event_type))
            problems = event_catalog.validate_envelope(
                {
                    "event": event_type,
                    "seq": next_sequence,
                    "schema_version": spec.version if spec else 1,
                    "occurred_at": timestamp,
                    "payload": payload if isinstance(payload, dict) else {},
                }
            )
            if problems:
                raise ValueError("project_event_catalog_rejected:" + "; ".join(problems))
        self.db.execute("INSERT INTO events (id, project_id, sequence, event_type, actor, payload, created_at, actor_kind, object_type, object_id, idempotency_key, schema_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (event_id, str(project_id), next_sequence, event_type, actor, json.dumps(payload, ensure_ascii=False), timestamp, actor_kind, object_type, str(object_id) if object_id else None, idempotency_key, schema_version))
        self.db.execute("INSERT INTO event_outbox (id, event_id, project_id, status, attempts, available_at, locked_at, lock_expires_at, delivered_at, last_error, created_at, updated_at) VALUES (?, ?, ?, 'PENDING', 0, ?, NULL, NULL, NULL, NULL, ?, ?)", (str(uuid4()), event_id, str(project_id), timestamp, timestamp, timestamp))
        event = Event(id=UUID(event_id), project_id=project_id, sequence=next_sequence, event_type=event_type, actor=actor, payload=payload, created_at=parse_time(timestamp), actor_kind=actor_kind, object_type=object_type, object_id=object_id, idempotency_key=idempotency_key, schema_version=schema_version)
        # 工作区聊天桥：事件在这里统一转成聊天卡片（同一事务，由调用方提交）。
        # 所有事件路径（含直接调 _insert_event 的）都被覆盖，Agent 侧零改动。
        self._bridge_chat_event(event)
        return event

    def add_event(
        self,
        project_id: UUID,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
        *,
        actor_kind: str = "system",
        object_type: str | None = None,
        object_id: UUID | None = None,
        idempotency_key: str | None = None,
        schema_version: str = "1.0",
    ) -> Event:
        if idempotency_key:
            previous = self._operation(idempotency_key, "event.create")
            if previous:
                return Event.model_validate(previous)
        try:
            event = self._insert_event(
                project_id,
                event_type,
                actor,
                payload,
                actor_kind=actor_kind,
                object_type=object_type,
                object_id=object_id,
                idempotency_key=idempotency_key,
                schema_version=schema_version,
            )
            if idempotency_key:
                self.db.execute(
                    "INSERT OR IGNORE INTO idempotency_records (key, operation, response, request_hash, created_at) VALUES (?, 'event.create', ?, NULL, ?)",
                    (idempotency_key, json.dumps(event.model_dump(mode="json"), ensure_ascii=False), now()),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return event

    def _event_outbox(self, row: sqlite3.Row) -> EventOutbox:
        values = dict(row)
        values["id"] = UUID(values["id"])
        values["event_id"] = UUID(values["event_id"])
        values["project_id"] = UUID(values["project_id"])
        for key in ["available_at", "locked_at", "lock_expires_at", "delivered_at", "created_at", "updated_at"]:
            values[key] = parse_time(values[key]) if values[key] else None
        return EventOutbox(**values)

    def list_event_outbox(self, project_id: UUID | None = None, *, status: str | None = None, limit: int = 100) -> list[EventOutbox]:
        query = "SELECT * FROM event_outbox WHERE 1 = 1"
        params: list[Any] = []
        if project_id is not None:
            query += " AND project_id = ?"
            params.append(str(project_id))
        if status is not None:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at ASC LIMIT ?"
        params.append(max(1, min(limit, 1000)))
        return [self._event_outbox(row) for row in self.db.execute(query, params)]

    def list_pending_event_outbox(self, limit: int = 100) -> list[EventOutbox]:
        current = now()
        rows = self.db.execute(
            "SELECT * FROM event_outbox WHERE (status IN ('PENDING', 'FAILED') AND available_at <= ?) OR (status = 'PROCESSING' AND lock_expires_at <= ?) ORDER BY created_at ASC LIMIT ?",
            (current, current, max(1, min(limit, 1000))),
        )
        return [self._event_outbox(row) for row in rows]

    def claim_event_outbox(self, limit: int = 100, lock_seconds: int = 60) -> list[EventOutbox]:
        if lock_seconds < 1:
            raise ValueError("event_outbox_lock_seconds_invalid")
        current = datetime.now(UTC)
        current_text = current.isoformat()
        expires_text = (current + timedelta(seconds=lock_seconds)).isoformat()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            rows = self.db.execute(
                "SELECT id FROM event_outbox WHERE (status IN ('PENDING', 'FAILED') AND available_at <= ?) OR (status = 'PROCESSING' AND lock_expires_at <= ?) ORDER BY created_at ASC LIMIT ?",
                (current_text, current_text, max(1, min(limit, 1000))),
            ).fetchall()
            ids = [row["id"] for row in rows]
            for outbox_id in ids:
                self.db.execute(
                    "UPDATE event_outbox SET status = 'PROCESSING', locked_at = ?, lock_expires_at = ?, updated_at = ? WHERE id = ?",
                    (current_text, expires_text, current_text, outbox_id),
                )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        claimed = self.db.execute(f"SELECT * FROM event_outbox WHERE id IN ({placeholders}) ORDER BY created_at ASC", ids)
        return [self._event_outbox(row) for row in claimed]

    def mark_event_outbox_delivered(self, outbox_id: UUID) -> EventOutbox:
        row = self.db.execute("SELECT * FROM event_outbox WHERE id = ?", (str(outbox_id),)).fetchone()
        if not row:
            raise KeyError("event_outbox_not_found")
        if row["status"] == "DELIVERED":
            return self._event_outbox(row)
        if row["status"] not in {"PENDING", "PROCESSING", "FAILED"}:
            raise ValueError("event_outbox_invalid_status")
        timestamp = now()
        try:
            self.db.execute("UPDATE event_outbox SET status = 'DELIVERED', delivered_at = COALESCE(delivered_at, ?), locked_at = NULL, lock_expires_at = NULL, updated_at = ? WHERE id = ?", (timestamp, timestamp, str(outbox_id)))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._event_outbox(self.db.execute("SELECT * FROM event_outbox WHERE id = ?", (str(outbox_id),)).fetchone())

    def mark_event_outbox_failed(self, outbox_id: UUID, error: str, retry_at: datetime | None = None) -> EventOutbox:
        row = self.db.execute("SELECT * FROM event_outbox WHERE id = ?", (str(outbox_id),)).fetchone()
        if not row:
            raise KeyError("event_outbox_not_found")
        if row["status"] == "DELIVERED":
            raise ValueError("event_outbox_already_delivered")
        if not error.strip():
            raise ValueError("event_outbox_error_required")
        timestamp = now()
        available_at = retry_at.isoformat() if retry_at else timestamp
        try:
            self.db.execute("UPDATE event_outbox SET status = 'FAILED', attempts = attempts + 1, available_at = ?, locked_at = NULL, lock_expires_at = NULL, last_error = ?, updated_at = ? WHERE id = ?", (available_at, error[:2000], timestamp, str(outbox_id)))
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        return self._event_outbox(self.db.execute("SELECT * FROM event_outbox WHERE id = ?", (str(outbox_id),)).fetchone())

    def list_latest_events(self, project_id: UUID, limit: int = 20) -> list[Event]:
        """最近 N 条事件（仍按时间升序返回）。

        看板此前用的是 `list_events(limit=20)` —— 那是**最早的** 20 条，于是
        "项目跑了半天、界面上一条新事件都看不到"，实时进度也没处可显示。
        """

        rows = self.db.execute(
            "SELECT * FROM events WHERE project_id = ? ORDER BY sequence DESC LIMIT ?",
            (str(project_id), max(1, limit)),
        ).fetchall()
        return [self._event(row) for row in reversed(rows)]

    def list_events(self, project_id: UUID, after: int = 0, limit: int = 100) -> list[Event]:
        rows = self.db.execute("SELECT * FROM events WHERE project_id = ? AND sequence > ? ORDER BY sequence ASC LIMIT ?", (str(project_id), after, limit))
        result = []
        for row in rows:
            result.append(self._event(row))
        return result

    def _event(self, row: sqlite3.Row) -> Event:
        return Event(
            id=UUID(row["id"]),
            project_id=UUID(row["project_id"]),
            sequence=row["sequence"],
            event_type=row["event_type"],
            actor=row["actor"],
            payload=self._json(row["payload"]),
            created_at=parse_time(row["created_at"]),
            actor_kind=row["actor_kind"],
            object_type=row["object_type"],
            object_id=UUID(row["object_id"]) if row["object_id"] else None,
            idempotency_key=row["idempotency_key"],
            schema_version=row["schema_version"],
        )

    def get_event(self, event_id: UUID) -> Event:
        row = self.db.execute("SELECT * FROM events WHERE id = ?", (str(event_id),)).fetchone()
        if not row:
            raise KeyError("event_not_found")
        return self._event(row)
