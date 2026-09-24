"""First production PostgreSQL repository slice.

The SQLite :class:`Store` remains the development implementation.  This
repository deliberately owns only the database-backed read/write slice that
is already stable enough to validate against the PostgreSQL schema.  Runner,
object-store, lease, and the remaining review orchestration remain explicit
follow-up work instead of silently falling back to SQLite behavior.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator
from uuid import UUID, uuid4

from .contracts import (
    AgentConnection,
    AgentConnectionStatus,
    AgentHeartbeat,
    Artifact,
    ArtifactCreate,
    Event,
    EventOutbox,
    ExecutionProfile,
    GatewayCommandResult,
    Evidence,
    Gate,
    GitFileIndex,
    GitRepository,
    Handoff,
    HandoffCreate,
    HandoffReceipt,
    Device,
    DeviceCredential,
    DeviceRuntimeState,
    DocumentDraft,
    DevicePairing,
    DevicePairingCreate,
    DevicePairingStatus,
    DeviceProjectCredential,
    DeviceProjectGrant,
    DeviceProjectGrantCreate,
    DeviceRegisterRequest,
    DeviceStatus,
    Project,
    ProjectCreate,
    Review,
    ReviewCreate,
    RiskDecisionRequest,
    RiskRegistryEntry,
    Run,
    Task,
    TaskCreate,
)
from .device_identity import DeviceIdentityError, create_challenge, hash_secret, verify_registration_signature
from .risk_rules import blocking_findings, ensure_approvable, gate_rules, normalize_findings, risk_summary


def _now() -> datetime:
    return datetime.now(UTC)


class PostgresRepositoryError(RuntimeError):
    """Stable error family for PostgreSQL runtime and pool failures.

    Callers get a machine-readable ``code`` instead of a driver-specific
    exception so that API handlers can map pool exhaustion or a lost backend
    to a deterministic response without importing psycopg.
    """

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


class PostgresRepository:
    """Transactional PostgreSQL repository with optional tenant RLS scope.

    ``psycopg`` and ``psycopg_pool`` are optional development dependencies.
    Instantiating this class without the production dependencies raises a
    clear error; importing the module remains safe for SQLite-only tests.
    """

    def __init__(
        self,
        dsn: str,
        *,
        organization_id: UUID | None = None,
        min_size: int = 1,
        max_size: int = 10,
        open_pool: bool = True,
        timeout: float = 5.0,
        max_idle: float = 300.0,
        max_lifetime: float = 3600.0,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise ValueError("postgres_dsn_required")
        if not isinstance(min_size, int) or isinstance(min_size, bool) or min_size < 1:
            raise ValueError("postgres_pool_min_size_invalid")
        if not isinstance(max_size, int) or isinstance(max_size, bool) or max_size < min_size:
            raise ValueError("postgres_pool_size_range_invalid")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("postgres_pool_timeout_invalid")
        if not isinstance(max_idle, (int, float)) or isinstance(max_idle, bool) or max_idle <= 0:
            raise ValueError("postgres_pool_max_idle_invalid")
        if not isinstance(max_lifetime, (int, float)) or isinstance(max_lifetime, bool) or max_lifetime <= 0:
            raise ValueError("postgres_pool_max_lifetime_invalid")
        if max_idle > max_lifetime:
            raise ValueError("postgres_pool_max_idle_exceeds_lifetime")
        try:
            from psycopg.rows import dict_row
            from psycopg.types.json import Jsonb
            from psycopg_pool import ConnectionPool, PoolTimeout
        except ImportError as error:  # pragma: no cover - production-only dependency
            raise RuntimeError("psycopg_and_psycopg_pool_required_for_postgres_repository") from error

        self.organization_id = organization_id
        self._jsonb = Jsonb
        self._pool_timeout = float(timeout)
        self._pool_timeout_error = PoolTimeout
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=min_size,
            max_size=max_size,
            timeout=self._pool_timeout,
            max_idle=max_idle,
            max_lifetime=max_lifetime,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        if open_pool:
            self.pool.open(wait=True)

    def close(self) -> None:
        self.pool.close()

    def pool_stats(self) -> dict[str, Any]:
        """Expose pool capacity and wait metrics without leaking the DSN."""

        stats = self.pool.get_stats()
        get = stats.get if isinstance(stats, dict) else (lambda key, default=None: getattr(stats, key, default))
        return {
            "pool_min": get("pool_min"),
            "pool_max": get("pool_max"),
            "pool_size": get("pool_size"),
            "pool_available": get("pool_available"),
            "requests_waiting": get("requests_waiting"),
            "requests_errors": get("requests_errors"),
            "timeout_seconds": self._pool_timeout,
        }

    def _pool_error(self, error: Exception) -> PostgresRepositoryError:
        if isinstance(error, self._pool_timeout_error):
            return PostgresRepositoryError("postgres_pool_timeout")
        return PostgresRepositoryError("postgres_connection_unavailable", str(error)[:200])

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        """Borrow one connection and apply the transaction-local RLS scope.

        Pool exhaustion and a lost backend surface as
        :class:`PostgresRepositoryError` with a stable code; errors raised by
        the caller's own statements keep their original type.
        """
        import psycopg

        context = self.pool.connection()
        try:
            connection = context.__enter__()
        except (self._pool_timeout_error, psycopg.Error) as error:
            raise self._pool_error(error) from error
        try:
            with connection.transaction():
                if self.organization_id:
                    connection.execute(
                        "SELECT set_config('app.organization_id', %s, true)",
                        (str(self.organization_id),),
                    )
                yield connection
        finally:
            context.__exit__(*sys.exc_info())

    @staticmethod
    def _json(value: Any, default: Any) -> Any:
        if value is None:
            return default
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return value
        return value

    @staticmethod
    def _request_hash(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _project(row: dict[str, Any]) -> Project:
        return Project.model_validate(row)

    @staticmethod
    def _task(row: dict[str, Any]) -> Task:
        values = dict(row)
        for key in [
            "input_artifacts",
            "input_handoff_ids",
            "output_types",
            "dependency_task_ids",
            "acceptance_criteria",
            "required_capabilities",
        ]:
            values[key] = PostgresRepository._json(values.get(key), [])
        values["information_boundary"] = PostgresRepository._json(values.get("information_boundary"), {})
        values["resource_policy"] = PostgresRepository._json(values.get("resource_policy"), {})
        return Task.model_validate(values)

    @staticmethod
    def _artifact(row: dict[str, Any]) -> Artifact:
        values = dict(row)
        values["data_policy"] = PostgresRepository._json(values.get("data_policy"), {})
        values["input_artifact_ids"] = [UUID(str(value)) for value in PostgresRepository._json(values.get("input_artifact_ids"), [])]
        return Artifact.model_validate(values)

    @staticmethod
    def _run(row: dict[str, Any]) -> Run:
        values = dict(row)
        for key in ["input_artifact_ids", "output_artifact_ids"]:
            values[key] = [UUID(str(value)) for value in PostgresRepository._json(values.get(key), [])]
        for key in ["parameters", "tool_versions", "data_access_policy", "information_boundary"]:
            values[key] = PostgresRepository._json(values.get(key), {})
        execution_profile = PostgresRepository._json(values.get("execution_profile"), {})
        if execution_profile.get("network_policy", "deny-by-default") == "deny-by-default" and values.get("network_policy") != "deny-by-default":
            execution_profile["network_policy"] = values["network_policy"]
        values["execution_profile"] = ExecutionProfile.model_validate(execution_profile)
        values["observed_input_files"] = PostgresRepository._json(values.get("observed_input_files"), [])
        return Run.model_validate(values)

    @staticmethod
    def _handoff_receipt(row: dict[str, Any]) -> HandoffReceipt:
        values = dict(row)
        values["id"] = UUID(str(values["id"]))
        values["handoff_id"] = UUID(str(values["handoff_id"]))
        values["decision_findings"] = PostgresRepository._json(values.get("decision_findings"), [])
        return HandoffReceipt.model_validate(values)

    def _handoff(self, row: dict[str, Any] | None = None, connection: Any | None = None) -> Handoff:
        # Keep direct class-level decoding available for contract tests and old
        # callers; repository reads hydrate receipts when an instance exists.
        repository = self
        if row is None:
            row = repository  # type: ignore[assignment]
            repository = None  # type: ignore[assignment]
        values = dict(row)
        for key in [
            "receiver",
            "completed",
            "input_artifacts",
            "output_artifacts",
            "key_conclusions",
            "assumptions",
            "evidence_refs",
            "open_questions",
            "risks",
            "next_actions",
            "input_handoff_ids",
            "decision_findings",
        ]:
            default = {} if key == "receiver" else []
            values[key] = PostgresRepository._json(values.get(key), default)
        handoff_id = UUID(str(values["id"]))
        if repository is None:
            receipt_rows = []
        elif connection is not None:
            receipt_rows = connection.execute("SELECT * FROM handoff_receipts WHERE handoff_id = %s ORDER BY created_at ASC, receiver_id ASC", (handoff_id,)).fetchall()
        else:
            with repository.transaction() as read_connection:
                receipt_rows = read_connection.execute("SELECT * FROM handoff_receipts WHERE handoff_id = %s ORDER BY created_at ASC, receiver_id ASC", (handoff_id,)).fetchall()
        values["receipts"] = [repository._handoff_receipt(receipt) for receipt in receipt_rows] if repository is not None else []
        return Handoff.model_validate(values)

    @staticmethod
    def _review(row: dict[str, Any]) -> Review:
        values = dict(row)
        values["findings"] = PostgresRepository._json(values.get("findings"), [])
        values["evidence_ids"] = [UUID(str(value)) for value in PostgresRepository._json(values.get("evidence_ids"), [])]
        values["risk_summary"] = risk_summary(values["findings"])
        return Review.model_validate(values)

    @staticmethod
    def _gate(row: dict[str, Any]) -> Gate:
        values = dict(row)
        values["blocking_findings"] = PostgresRepository._json(values.get("blocking_findings"), [])
        values["rules"] = PostgresRepository._json(values.get("rules"), [])
        values["review_ids"] = [UUID(str(value)) for value in PostgresRepository._json(values.get("review_ids"), [])]
        values["evidence_ids"] = [UUID(str(value)) for value in PostgresRepository._json(values.get("evidence_ids"), [])]
        values["risk_summary"] = PostgresRepository._json(values.get("risk_summary"), {})
        values["input_snapshot"] = PostgresRepository._json(values.get("input_snapshot"), {})
        return Gate.model_validate(values)

    @staticmethod
    def _evidence(row: dict[str, Any]) -> Evidence:
        return Evidence.model_validate(row)

    @staticmethod
    def _event(row: dict[str, Any]) -> Event:
        values = dict(row)
        values["payload"] = PostgresRepository._json(values.get("payload"), {})
        return Event.model_validate(values)

    @staticmethod
    def _event_outbox(row: dict[str, Any]) -> EventOutbox:
        return EventOutbox.model_validate(row)

    @staticmethod
    def _device(row: dict[str, Any]) -> Device:
        values = dict(row)
        values["capabilities"] = PostgresRepository._json(values.get("capabilities"), [])
        values.pop("public_key", None)
        values.pop("device_token_hash", None)
        return Device.model_validate(values)

    @staticmethod
    def _device_project_grant(row: dict[str, Any]) -> DeviceProjectGrant:
        values = dict(row)
        values["capabilities"] = PostgresRepository._json(values.get("capabilities"), [])
        values.pop("token_hash", None)
        values.pop("device_status", None)
        return DeviceProjectGrant.model_validate(values)

    @staticmethod
    def _agent_connection(row: dict[str, Any]) -> AgentConnection:
        return AgentConnection.model_validate(row)

    @staticmethod
    def _gateway_command_result(row: dict[str, Any]) -> GatewayCommandResult:
        values = dict(row)
        values["result"] = PostgresRepository._json(values.get("result"), None)
        return GatewayCommandResult.model_validate(values)

    @staticmethod
    def _device_pairing(
        row: dict[str, Any], pairing_code: str | None = None, challenge: str | None = None
    ) -> DevicePairing:
        values = dict(row)
        values.pop("code_hash", None)
        values.pop("challenge_hash", None)
        if pairing_code is not None:
            values["pairing_code"] = pairing_code
        if challenge is not None:
            values["challenge"] = challenge
        return DevicePairing.model_validate(values)

    def _authorize_organization_admin(self, connection: Any, organization_id: UUID, member_id: str) -> None:
        row = connection.execute(
            """
            SELECT 1
            FROM human_members m
            JOIN memberships ms ON ms.member_id = m.id
            JOIN teams t ON t.id = ms.team_id
            WHERE m.id = %s AND m.organization_id = %s AND m.status = 'active'
              AND t.organization_id = %s AND ms.role IN ('owner', 'project_lead')
            LIMIT 1
            """,
            (member_id, organization_id, organization_id),
        ).fetchone()
        if not row:
            raise PermissionError("organization_permission_denied")

    def _authorize_project_admin(self, connection: Any, project_id: UUID, member_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM project_memberships WHERE project_id = %s AND member_id = %s AND role IN ('owner', 'project_lead')",
            (project_id, member_id),
        ).fetchone()
        if not row:
            raise PermissionError("permission_denied")

    def _authorize_project_member(self, connection: Any, project_id: UUID, member_id: str, permission: str) -> None:
        """Apply the same project review roles as the development Store."""
        allowed = {
            "review.submit": {"owner", "project_lead", "reviewer"},
            "review.approve": {"owner", "project_lead", "reviewer"},
        }
        row = connection.execute(
            """
            SELECT pm.role
            FROM project_memberships pm
            JOIN human_members hm ON hm.id = pm.member_id
            WHERE pm.project_id = %s AND pm.member_id = %s AND hm.status = 'active'
            """,
            (project_id, member_id),
        ).fetchone()
        if not row:
            raise PermissionError("member_project_access_denied")
        if row["role"] not in allowed.get(permission, set()):
            raise PermissionError("permission_denied")

    @staticmethod
    def _lock_project(connection: Any, project_id: UUID) -> None:
        """Serialize project-local side effects across repository instances."""
        row = connection.execute("SELECT id FROM projects WHERE id = %s FOR UPDATE", (project_id,)).fetchone()
        if not row:
            raise KeyError("project_not_found")

    def _scoped_idempotency_key(self, key: str) -> str:
        """Namespace request keys so tenants cannot collide on the global PK."""
        if not self.organization_id:
            raise PermissionError("organization_scope_required_for_idempotency")
        return f"{self.organization_id}:{key}"

    def _load_idempotent_response(self, connection: Any, key: str, operation: str, request_hash: str | None = None) -> Any | None:
        row = connection.execute(
            "SELECT operation, response, request_hash FROM idempotency_records WHERE key = %s AND organization_id = %s",
            (self._scoped_idempotency_key(key), self.organization_id),
        ).fetchone()
        if not row:
            return None
        if row["operation"] != operation:
            raise ValueError("idempotency_key_reused_for_different_operation")
        if request_hash and row["request_hash"] and row["request_hash"] != request_hash:
            raise ValueError("idempotency_key_reused_for_different_request")
        return row["response"]

    def _store_idempotent_response(
        self,
        connection: Any,
        key: str,
        operation: str,
        response: Any,
        created_at: datetime,
        request_hash: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO idempotency_records (key, organization_id, operation, response, request_hash, created_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (key) DO NOTHING
            """,
            (self._scoped_idempotency_key(key), self.organization_id, operation, self._jsonb(response), request_hash, created_at),
        )

    def get_device(self, device_id: str) -> Device:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,)).fetchone()
        if not row:
            raise KeyError("device_not_found")
        return self._device(row)

    def list_devices_for_member(self, member_id: str) -> list[Device]:
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT d.*, s.connection_id AS runtime_connection_id, s.agent_version AS runtime_agent_version,
                       s.adapter_versions AS runtime_adapter_versions, s.capabilities AS runtime_capabilities,
                       s.running_run_ids AS runtime_running_run_ids, s.local_queue_length AS runtime_local_queue_length,
                       s.user_session_state AS runtime_user_session_state, s.resource_summary AS runtime_resource_summary,
                       s.reported_at AS runtime_reported_at, s.device_id AS runtime_device_id
                FROM devices d
                JOIN human_members m ON m.organization_id = d.organization_id
                LEFT JOIN device_runtime_state s ON s.device_id = d.device_id
                WHERE m.id = %s AND m.status = 'active'
                ORDER BY d.created_at DESC
                """,
                (member_id,),
            ).fetchall()
        return [self._device_with_runtime(row) for row in rows]

    @classmethod
    def _device_with_runtime(cls, row: dict[str, Any]) -> Device:
        """B4：把 LEFT JOIN 出来的运行态列拼回 Device.runtime（无心跳时为 None）。"""

        values = dict(row)
        runtime_values = {
            "device_id": values.pop("runtime_device_id", None),
            "connection_id": values.pop("runtime_connection_id", None),
            "agent_version": values.pop("runtime_agent_version", None),
            "adapter_versions": values.pop("runtime_adapter_versions", None),
            "capabilities": values.pop("runtime_capabilities", None),
            "running_run_ids": values.pop("runtime_running_run_ids", None),
            "local_queue_length": values.pop("runtime_local_queue_length", None),
            "user_session_state": values.pop("runtime_user_session_state", None),
            "resource_summary": values.pop("runtime_resource_summary", None),
            "reported_at": values.pop("runtime_reported_at", None),
        }
        device = cls._device(values)
        if runtime_values["device_id"] and runtime_values["reported_at"] is not None:
            return device.model_copy(update={"runtime": cls._device_runtime_state(runtime_values)})
        return device

    def create_device_pairing(self, data: DevicePairingCreate, created_by: str) -> DevicePairing:
        created_at = _now()
        expires_at = created_at + timedelta(seconds=data.expires_in_seconds)
        pairing_id = uuid4()
        pairing_code = f"map_{secrets.token_urlsafe(24)}"
        code_hash = hashlib.sha256(pairing_code.encode("utf-8")).hexdigest()
        challenge = create_challenge()
        challenge_hash = hash_secret(challenge)
        with self.transaction() as connection:
            self._authorize_organization_admin(connection, data.organization_id, created_by)
            connection.execute(
                "INSERT INTO device_pairings (id, organization_id, created_by, code_hash, challenge_hash, status, expires_at, created_at) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s)",
                (pairing_id, data.organization_id, created_by, code_hash, challenge_hash, expires_at, created_at),
            )
            row = connection.execute("SELECT * FROM device_pairings WHERE id = %s", (pairing_id,)).fetchone()
        return self._device_pairing(row, pairing_code, challenge)

    def register_device(self, data: DeviceRegisterRequest) -> DeviceCredential:
        code_hash = hashlib.sha256(data.pairing_code.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            pairing = connection.execute(
                "SELECT * FROM device_pairings WHERE id = %s FOR UPDATE", (data.pairing_id,)
            ).fetchone()
            if not pairing:
                raise PermissionError("device_pairing_invalid")
            if not secrets.compare_digest(pairing["code_hash"], code_hash):
                raise PermissionError("device_pairing_invalid")
            if pairing["status"] != DevicePairingStatus.PENDING.value:
                raise ValueError("device_pairing_not_pending")
            if pairing["expires_at"] <= _now():
                connection.execute("UPDATE device_pairings SET status = 'EXPIRED' WHERE id = %s", (pairing["id"],))
                raise ValueError("device_pairing_expired")
            agent = connection.execute("SELECT owner_member_id FROM agents WHERE agent_id = %s", (data.agent_id,)).fetchone()
            if not agent:
                raise KeyError("agent_not_found")
            if agent["owner_member_id"] != pairing["created_by"]:
                raise PermissionError("device_agent_owner_mismatch")

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
            if connection.execute("SELECT 1 FROM devices WHERE device_id = %s", (device_id,)).fetchone():
                raise ValueError("device_id_already_registered")
            if connection.execute("SELECT 1 FROM devices WHERE public_key_fingerprint = %s", (public_key_fingerprint,)).fetchone():
                raise ValueError("device_public_key_already_registered")
            created_at = _now()
            device_token = f"dvc_{secrets.token_urlsafe(32)}"
            token_hash = hashlib.sha256(device_token.encode("utf-8")).hexdigest()
            connection.execute(
                """
                INSERT INTO devices
                    (device_id, organization_id, agent_id, owner_member_id, device_name,
                     public_key, public_key_fingerprint, device_token_hash, platform,
                     agent_version, capabilities, status, created_at, last_seen, revoked_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'active', %s, %s, NULL)
                """,
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
                    self._jsonb(data.capabilities),
                    created_at,
                    created_at,
                ),
            )
            updated = connection.execute(
                "UPDATE device_pairings SET status = 'CONSUMED', device_id = %s, consumed_at = %s WHERE id = %s AND status = 'PENDING'",
                (device_id, created_at, pairing["id"]),
            )
            if updated.rowcount != 1:
                raise ValueError("device_pairing_already_consumed")
            row = connection.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,)).fetchone()
        return DeviceCredential(device=self._device(row), device_token=device_token)

    def resolve_device_token(self, device_token: str) -> Device:
        token_hash = hashlib.sha256(device_token.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM devices WHERE device_token_hash = %s", (token_hash,)).fetchone()
        if not row:
            raise PermissionError("device_token_invalid")
        if row["status"] != DeviceStatus.ACTIVE.value:
            raise PermissionError("device_revoked")
        return self._device(row)

    def rotate_device_token(self, device_id: str, rotated_by: str, reason: str = "rotated_by_member") -> DeviceCredential:
        """Atomically replace a device token and invalidate live connections."""
        if not isinstance(reason, str) or not 1 <= len(reason) <= 500:
            raise ValueError("device_token_rotation_reason_invalid")
        token = f"dvc_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        rotated_at = _now()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM devices WHERE device_id = %s FOR UPDATE", (device_id,)).fetchone()
            if not row:
                raise KeyError("device_not_found")
            self._authorize_organization_admin(connection, UUID(str(row["organization_id"])), rotated_by)
            if row["status"] != DeviceStatus.ACTIVE.value:
                raise PermissionError("device_revoked")
            token_version = int(row.get("token_version") or 1) + 1
            connection.execute(
                "UPDATE devices SET device_token_hash = %s, token_version = %s, token_rotated_at = %s WHERE device_id = %s",
                (token_hash, token_version, rotated_at, device_id),
            )
            connection.execute(
                "UPDATE agent_connections SET status = 'REVOKED', disconnected_at = %s WHERE device_id = %s AND status != 'REVOKED'",
                (rotated_at, device_id),
            )
            connection.execute(
                "INSERT INTO device_token_rotations (id, device_id, organization_id, rotated_by, reason, token_version, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (uuid4(), device_id, row["organization_id"], rotated_by, reason, token_version, rotated_at),
            )
            updated = connection.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,)).fetchone()
        return DeviceCredential(device=self._device(updated), device_token=token)

    def revoke_device(self, device_id: str, revoked_by: str, reason: str = "revoked_by_member") -> Device:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM devices WHERE device_id = %s FOR UPDATE", (device_id,)).fetchone()
            if not row:
                raise KeyError("device_not_found")
            self._authorize_organization_admin(connection, UUID(str(row["organization_id"])), revoked_by)
            if row["status"] != DeviceStatus.REVOKED.value:
                timestamp = _now()
                connection.execute("UPDATE devices SET status = 'revoked', revoked_at = %s WHERE device_id = %s", (timestamp, device_id))
                connection.execute("UPDATE device_project_grants SET revoked_at = COALESCE(revoked_at, %s) WHERE device_id = %s", (timestamp, device_id))
                connection.execute("UPDATE agent_connections SET status = 'REVOKED', disconnected_at = %s WHERE device_id = %s AND status != 'REVOKED'", (timestamp, device_id))
            updated = connection.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,)).fetchone()
        return self._device(updated)

    def create_device_project_grant(self, project_id: UUID, data: DeviceProjectGrantCreate, granted_by: str) -> DeviceProjectCredential:
        created_at = _now()
        expires_at = created_at + timedelta(seconds=data.expires_in_seconds)
        grant_id = uuid4()
        project_token = f"prj_{secrets.token_urlsafe(32)}"
        token_hash = hashlib.sha256(project_token.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            project = connection.execute("SELECT organization_id FROM projects WHERE id = %s", (project_id,)).fetchone()
            if not project:
                raise KeyError("project_not_found")
            self._authorize_project_admin(connection, project_id, granted_by)
            device = connection.execute("SELECT * FROM devices WHERE device_id = %s", (data.device_id,)).fetchone()
            if not device:
                raise KeyError("device_not_found")
            if device["organization_id"] != project["organization_id"]:
                raise PermissionError("device_project_organization_mismatch")
            if device["status"] != DeviceStatus.ACTIVE.value:
                raise PermissionError("device_revoked")
            connection.execute(
                "INSERT INTO device_project_grants (id, device_id, agent_id, project_id, token_hash, capabilities, granted_by, expires_at, revoked_at, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)",
                (grant_id, data.device_id, device["agent_id"], project_id, token_hash, self._jsonb(data.capabilities), granted_by, expires_at, created_at),
            )
            connection.execute(
                """
                INSERT INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (agent_id, project_id) DO UPDATE SET
                    capabilities = EXCLUDED.capabilities, granted_by = EXCLUDED.granted_by, created_at = EXCLUDED.created_at
                """,
                (device["agent_id"], project_id, self._jsonb(data.capabilities), granted_by, created_at),
            )
            row = connection.execute("SELECT * FROM device_project_grants WHERE id = %s", (grant_id,)).fetchone()
        return DeviceProjectCredential(grant=self._device_project_grant(row), project_token=project_token)

    def list_device_project_grants(self, project_id: UUID, member_id: str) -> list[DeviceProjectGrant]:
        with self.transaction() as connection:
            row = connection.execute("SELECT 1 FROM project_memberships WHERE project_id = %s AND member_id = %s", (project_id, member_id)).fetchone()
            if not row:
                raise PermissionError("member_project_access_denied")
            rows = connection.execute("SELECT * FROM device_project_grants WHERE project_id = %s ORDER BY created_at DESC", (project_id,)).fetchall()
        return [self._device_project_grant(row) for row in rows]

    def revoke_device_project_grant(self, grant_id: UUID, revoked_by: str) -> DeviceProjectGrant:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM device_project_grants WHERE id = %s FOR UPDATE", (grant_id,)).fetchone()
            if not row:
                raise KeyError("device_project_grant_not_found")
            self._authorize_project_admin(connection, UUID(str(row["project_id"])), revoked_by)
            connection.execute("UPDATE device_project_grants SET revoked_at = COALESCE(revoked_at, %s) WHERE id = %s", (_now(), grant_id))
            updated = connection.execute("SELECT * FROM device_project_grants WHERE id = %s", (grant_id,)).fetchone()
        return self._device_project_grant(updated)

    def resolve_device_project_token(self, project_token: str, project_id: UUID, capability: str | None = None) -> DeviceProjectGrant:
        token_hash = hashlib.sha256(project_token.encode("utf-8")).hexdigest()
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT g.*, d.status AS device_status
                FROM device_project_grants g
                JOIN devices d ON d.device_id = g.device_id
                WHERE g.token_hash = %s AND g.project_id = %s
                """,
                (token_hash, project_id),
            ).fetchone()
        if not row:
            raise PermissionError("device_project_token_invalid")
        if row["device_status"] != DeviceStatus.ACTIVE.value or row["revoked_at"] is not None:
            raise PermissionError("device_project_token_revoked")
        if row["expires_at"] <= _now():
            raise PermissionError("device_project_token_expired")
        grant = self._device_project_grant(row)
        if capability and capability not in grant.capabilities:
            raise PermissionError("device_capability_denied")
        return grant

    def open_agent_connection(self, device_id: str, session_id: str, connection_id: str, transport: str = "websocket") -> AgentConnection:
        if transport not in {"websocket", "long_poll"}:
            raise ValueError("invalid_gateway_transport")
        timestamp = _now()
        with self.transaction() as connection:
            device = connection.execute("SELECT * FROM devices WHERE device_id = %s", (device_id,)).fetchone()
            if not device:
                raise KeyError("device_not_found")
            if device["status"] != DeviceStatus.ACTIVE.value:
                raise PermissionError("device_revoked")
            connection.execute(
                "INSERT INTO agent_connections (connection_id, device_id, agent_id, session_id, transport, status, connected_at, last_heartbeat_at) VALUES (%s, %s, %s, %s, %s, 'CONNECTED', %s, %s)",
                (connection_id, device_id, device["agent_id"], session_id, transport, timestamp, timestamp),
            )
            connection.execute("UPDATE devices SET last_seen = %s WHERE device_id = %s", (timestamp, device_id))
            row = connection.execute("SELECT * FROM agent_connections WHERE connection_id = %s", (connection_id,)).fetchone()
        return self._agent_connection(row)

    def get_agent_connection(self, connection_id: str) -> AgentConnection:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM agent_connections WHERE connection_id = %s", (connection_id,)).fetchone()
        if not row:
            raise KeyError("agent_connection_not_found")
        return self._agent_connection(row)

    def record_gateway_receive(self, connection_id: str, sequence: int) -> tuple[str, int]:
        if sequence < 1:
            raise ValueError("gateway_sequence_invalid")
        with self.transaction() as connection:
            row = connection.execute("SELECT status, last_received_sequence FROM agent_connections WHERE connection_id = %s FOR UPDATE", (connection_id,)).fetchone()
            if not row:
                raise KeyError("agent_connection_not_found")
            if row["status"] == AgentConnectionStatus.REVOKED.value:
                raise PermissionError("device_connection_revoked")
            current = int(row["last_received_sequence"])
            if sequence <= current:
                return "DUPLICATE", current
            if sequence != current + 1:
                raise ValueError(f"gateway_sequence_gap:{current + 1}:{sequence}")
            connection.execute("UPDATE agent_connections SET last_received_sequence = %s, last_heartbeat_at = %s WHERE connection_id = %s", (sequence, _now(), connection_id))
        return "ACCEPTED", sequence

    def next_gateway_send_sequence(self, connection_id: str) -> int:
        with self.transaction() as connection:
            row = connection.execute("SELECT status, last_sent_sequence FROM agent_connections WHERE connection_id = %s FOR UPDATE", (connection_id,)).fetchone()
            if not row:
                raise KeyError("agent_connection_not_found")
            if row["status"] == AgentConnectionStatus.REVOKED.value:
                raise PermissionError("device_connection_revoked")
            sequence = int(row["last_sent_sequence"]) + 1
            connection.execute("UPDATE agent_connections SET last_sent_sequence = %s WHERE connection_id = %s", (sequence, connection_id))
        return sequence

    def record_agent_heartbeat(self, connection_id: str, heartbeat: AgentHeartbeat) -> AgentConnection:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM agent_connections WHERE connection_id = %s FOR UPDATE", (connection_id,)).fetchone()
            if not row:
                raise KeyError("agent_connection_not_found")
            if row["status"] != AgentConnectionStatus.CONNECTED.value:
                raise PermissionError("agent_connection_not_active")
            if any(
                row[key] != value
                for key, value in {
                    "device_id": heartbeat.device_id,
                    "agent_id": heartbeat.agent_id,
                    "session_id": heartbeat.session_id,
                    "connection_id": heartbeat.connection_id,
                }.items()
            ):
                raise PermissionError("gateway_identity_mismatch")
            timestamp = _now()
            connection.execute("UPDATE agent_connections SET last_heartbeat_at = %s WHERE connection_id = %s", (timestamp, connection_id))
            connection.execute("UPDATE devices SET last_seen = %s WHERE device_id = %s", (timestamp, heartbeat.device_id))
            connection.execute("UPDATE agents SET status = 'online', last_seen = %s WHERE agent_id = %s", (timestamp, heartbeat.agent_id))
            self._write_device_runtime_state(connection, heartbeat, connection_id, timestamp)
            updated = connection.execute("SELECT * FROM agent_connections WHERE connection_id = %s", (connection_id,)).fetchone()
        return self._agent_connection(updated)

    def _write_device_runtime_state(self, connection: Any, heartbeat: AgentHeartbeat, connection_id: str, timestamp: str) -> None:
        """B1/B2：心跳运行态落库 + 设备能力刷新（与 SQLite 侧同语义）。"""

        capabilities = [str(item) for item in (heartbeat.capabilities or [])]
        agent_version = heartbeat.agent_version or ""
        connection.execute(
            """
            INSERT INTO device_runtime_state (
                device_id, connection_id, agent_version, adapter_versions, capabilities,
                running_run_ids, local_queue_length, user_session_state, resource_summary, reported_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (device_id) DO UPDATE SET
                connection_id = EXCLUDED.connection_id,
                agent_version = EXCLUDED.agent_version,
                adapter_versions = EXCLUDED.adapter_versions,
                capabilities = EXCLUDED.capabilities,
                running_run_ids = EXCLUDED.running_run_ids,
                local_queue_length = EXCLUDED.local_queue_length,
                user_session_state = EXCLUDED.user_session_state,
                resource_summary = EXCLUDED.resource_summary,
                reported_at = EXCLUDED.reported_at
            """,
            (
                heartbeat.device_id,
                connection_id,
                agent_version or None,
                self._jsonb(dict(heartbeat.adapter_versions or {})),
                self._jsonb(capabilities),
                self._jsonb([str(item) for item in (heartbeat.running_run_ids or [])]),
                max(0, int(heartbeat.local_queue_length or 0)),
                heartbeat.user_session_state or "unknown",
                self._jsonb(dict(heartbeat.resource_summary or {})),
                timestamp,
            ),
        )
        if capabilities:
            connection.execute("UPDATE devices SET capabilities = %s WHERE device_id = %s", (self._jsonb(capabilities), heartbeat.device_id))
        if agent_version:
            connection.execute("UPDATE devices SET agent_version = %s WHERE device_id = %s", (agent_version, heartbeat.device_id))

    def get_device_runtime_state(self, device_id: str) -> DeviceRuntimeState | None:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM device_runtime_state WHERE device_id = %s", (device_id,)).fetchone()
        return self._device_runtime_state(row) if row else None

    @staticmethod
    def _device_runtime_state(row: dict[str, Any]) -> DeviceRuntimeState:
        values = dict(row)
        values["adapter_versions"] = PostgresRepository._json(values.get("adapter_versions"), {})
        values["capabilities"] = PostgresRepository._json(values.get("capabilities"), [])
        values["running_run_ids"] = PostgresRepository._json(values.get("running_run_ids"), [])
        values["resource_summary"] = PostgresRepository._json(values.get("resource_summary"), {})
        values["local_queue_length"] = int(values.get("local_queue_length") or 0)
        return DeviceRuntimeState.model_validate(values)

    def list_device_project_grants_for_device(self, device_id: str) -> list[DeviceProjectGrant]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM device_project_grants WHERE device_id = %s AND revoked_at IS NULL ORDER BY created_at DESC",
                (device_id,),
            ).fetchall()
        return [self._device_project_grant(row) for row in rows]

    def close_agent_connection(self, connection_id: str, status: str = "DISCONNECTED") -> AgentConnection:
        if status not in {AgentConnectionStatus.DISCONNECTED.value, AgentConnectionStatus.REVOKED.value}:
            raise ValueError("invalid_agent_connection_close_status")
        with self.transaction() as connection:
            row = connection.execute("SELECT status FROM agent_connections WHERE connection_id = %s FOR UPDATE", (connection_id,)).fetchone()
            if not row:
                raise KeyError("agent_connection_not_found")
            if row["status"] != AgentConnectionStatus.REVOKED.value:
                connection.execute("UPDATE agent_connections SET status = %s, disconnected_at = %s WHERE connection_id = %s", (status, _now(), connection_id))
            updated = connection.execute("SELECT * FROM agent_connections WHERE connection_id = %s", (connection_id,)).fetchone()
        return self._agent_connection(updated)

    def get_gateway_command_result(
        self,
        connection_id: str,
        *,
        message_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> GatewayCommandResult | None:
        if not message_id and not idempotency_key:
            raise ValueError("gateway_command_lookup_key_required")
        with self.transaction() as connection:
            if message_id and idempotency_key:
                row = connection.execute(
                    "SELECT * FROM gateway_command_results WHERE connection_id = %s AND message_id = %s AND idempotency_key = %s",
                    (connection_id, message_id, idempotency_key),
                ).fetchone()
            elif message_id:
                row = connection.execute(
                    "SELECT * FROM gateway_command_results WHERE connection_id = %s AND message_id = %s",
                    (connection_id, message_id),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM gateway_command_results WHERE connection_id = %s AND idempotency_key = %s",
                    (connection_id, idempotency_key),
                ).fetchone()
        return self._gateway_command_result(row) if row else None

    def save_gateway_command_result(self, result: GatewayCommandResult) -> GatewayCommandResult:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM gateway_command_results WHERE connection_id = %s AND message_id = %s",
                (result.connection_id, result.message_id),
            ).fetchone()
            if existing:
                mapped = self._gateway_command_result(existing)
                if mapped.idempotency_key != result.idempotency_key or mapped.message_type != result.message_type:
                    raise ValueError("gateway_command_result_identity_conflict")
                if mapped.request_hash and result.request_hash and mapped.request_hash != result.request_hash:
                    raise ValueError("gateway_command_request_mismatch")
                return mapped
            existing = connection.execute(
                "SELECT * FROM gateway_command_results WHERE connection_id = %s AND idempotency_key = %s",
                (result.connection_id, result.idempotency_key),
            ).fetchone()
            if existing:
                mapped = self._gateway_command_result(existing)
                if mapped.message_type != result.message_type:
                    raise ValueError("gateway_command_result_identity_conflict")
                if mapped.request_hash and result.request_hash and mapped.request_hash != result.request_hash:
                    raise ValueError("gateway_command_request_mismatch")
                return mapped
            connection.execute(
                """
                INSERT INTO gateway_command_results
                    (id, connection_id, message_id, idempotency_key, sequence,
                     message_type, status, response_type, request_hash, result, error_code, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    result.id,
                    result.connection_id,
                    result.message_id,
                    result.idempotency_key,
                    result.sequence,
                    result.message_type,
                    result.status,
                    result.response_type,
                    result.request_hash,
                    self._jsonb(result.result) if result.result is not None else None,
                    result.error_code,
                    result.created_at,
                ),
            )
        return result

    def list_projects(self) -> list[Project]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        return [self._project(row) for row in rows]

    def get_project(self, project_id: UUID) -> Project:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
        if not row:
            raise KeyError("project_not_found")
        return self._project(row)

    def create_project(self, data: ProjectCreate) -> Project:
        project_id = uuid4()
        timestamp = _now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO projects
                    (id, organization_id, team_id, created_by, name, competition_pack,
                     problem_code, description, stage, progress, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    project_id,
                    data.organization_id,
                    data.team_id,
                    data.created_by,
                    data.name,
                    data.competition_pack,
                    data.problem_code,
                    data.description,
                    "intake",
                    0,
                    timestamp,
                    timestamp,
                ),
            )
            row = connection.execute("SELECT * FROM projects WHERE id = %s", (project_id,)).fetchone()
        return self._project(row)

    def list_tasks(self, project_id: UUID) -> list[Task]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM tasks WHERE project_id = %s ORDER BY updated_at DESC", (project_id,)).fetchall()
        return [self._task(row) for row in rows]

    def get_task(self, task_id: UUID) -> Task:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
        if not row:
            raise KeyError("task_not_found")
        return self._task(row)

    def create_task(self, project_id: UUID, data: TaskCreate) -> Task:
        task_id = uuid4()
        timestamp = _now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO tasks
                    (id, project_id, title, description, stage, status, assignee, priority,
                     requires_review, allow_future_data, input_artifacts, input_handoff_ids,
                     output_types, parent_task_id, dependency_task_ids, acceptance_criteria,
                     required_capabilities, deadline, information_boundary, resource_policy,
                     requires_human_approval, blocked_reason, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'DRAFT', %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, NULL, %s)
                """,
                (
                    task_id,
                    project_id,
                    data.title,
                    data.description,
                    data.stage,
                    data.assignee,
                    data.priority,
                    data.requires_review,
                    data.allow_future_data,
                    self._jsonb(data.input_artifacts),
                    self._jsonb([str(value) for value in data.input_handoff_ids]),
                    self._jsonb(data.output_types),
                    data.parent_task_id,
                    self._jsonb([str(value) for value in data.dependency_task_ids]),
                    self._jsonb(data.acceptance_criteria),
                    self._jsonb(data.required_capabilities),
                    data.deadline,
                    self._jsonb(data.information_boundary),
                    self._jsonb(data.resource_policy),
                    data.requires_human_approval if data.requires_human_approval is not None else True,
                    timestamp,
                ),
            )
            row = connection.execute("SELECT * FROM tasks WHERE id = %s", (task_id,)).fetchone()
        return self._task(row)

    def list_artifacts(self, project_id: UUID) -> list[Artifact]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM artifacts WHERE project_id = %s ORDER BY created_at DESC", (project_id,)).fetchall()
        return [self._artifact(row) for row in rows]

    def get_document_draft(self, project_id: UUID, artifact_id: UUID) -> DocumentDraft | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM document_drafts WHERE artifact_id = %s AND project_id = %s",
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
        """与 SQLite 侧同语义：修订号不一致即冲突，已批准内容不可再存草稿。"""

        with self.transaction() as connection:
            artifact = connection.execute(
                "SELECT project_id, status, immutable FROM artifacts WHERE id = %s", (str(artifact_id),)
            ).fetchone()
            if not artifact:
                raise KeyError("artifact_not_found")
            if str(artifact["project_id"]) != str(project_id):
                raise ValueError("artifact_not_in_project")
            if bool(artifact["immutable"]) or artifact["status"] == "APPROVED":
                raise PermissionError("approved_artifact_is_immutable")
            existing = connection.execute(
                "SELECT revision FROM document_drafts WHERE artifact_id = %s", (str(artifact_id),)
            ).fetchone()
            if existing is None:
                if int(base_revision) not in (0, 1):
                    raise ValueError("document_draft_conflict")
                revision = 1
            else:
                if int(base_revision) != int(existing["revision"]):
                    raise ValueError("document_draft_conflict")
                revision = int(existing["revision"]) + 1
            timestamp = _now()
            connection.execute(
                """
                INSERT INTO document_drafts (artifact_id, project_id, content, revision, updated_by, updated_at, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (artifact_id) DO UPDATE SET
                    content = EXCLUDED.content, revision = EXCLUDED.revision,
                    updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at
                """,
                (str(artifact_id), str(project_id), content, revision, updated_by, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM document_drafts WHERE artifact_id = %s", (str(artifact_id),)
            ).fetchone()
        return self._document_draft(row)

    @staticmethod
    def _document_draft(row: dict[str, Any]) -> DocumentDraft:
        values = dict(row)
        for field_name in ("artifact_id", "project_id"):
            if not isinstance(values[field_name], UUID):
                values[field_name] = UUID(str(values[field_name]))
        return DocumentDraft.model_validate(values)

    def get_artifact(self, artifact_id: UUID) -> Artifact:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
        if not row:
            raise KeyError("artifact_not_found")
        return self._artifact(row)

    def create_artifact(self, project_id: UUID, data: ArtifactCreate, created_by: str = "member-001", created_by_kind: str = "member") -> Artifact:
        if data.status == "APPROVED":
            raise PermissionError("artifact_approval_requires_review")
        artifact_id = uuid4()
        timestamp = _now()
        content_hash = data.content_hash or hashlib.sha256(f"{data.name}:{data.description}:{timestamp.isoformat()}".encode()).hexdigest()
        with self.transaction() as connection:
            if data.content_hash:
                existing = connection.execute(
                    "SELECT * FROM artifacts WHERE project_id = %s AND name = %s AND content_hash = %s AND status != 'ARCHIVED'",
                    (project_id, data.name, data.content_hash),
                ).fetchone()
                if existing:
                    return self._artifact(existing)
            version = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM artifacts WHERE project_id = %s AND name = %s",
                (project_id, data.name),
            ).fetchone()["version"]
            connection.execute(
                """
                INSERT INTO artifacts
                    (id, project_id, name, artifact_type, description, content_hash, version,
                     status, source_path, task_id, run_id, created_by, created_at, data_policy,
                     input_artifact_ids, git_commit, snapshot_ref, created_by_kind,
                     approved_by, approved_at, downstream_allowed, storage_key, size_bytes,
                     mime_type, immutable, parent_artifact_id, archived_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, NULL, NULL, false, NULL, NULL, %s, false, NULL, NULL)
                """,
                (
                    artifact_id,
                    project_id,
                    data.name,
                    data.artifact_type,
                    data.description,
                    content_hash,
                    version,
                    data.status,
                    data.source_path,
                    data.task_id,
                    data.run_id,
                    created_by,
                    timestamp,
                    self._jsonb(data.data_policy),
                    self._jsonb([str(value) for value in data.input_artifact_ids]),
                    data.git_commit,
                    data.snapshot_ref,
                    created_by_kind,
                    data.mime_type,
                ),
            )
            row = connection.execute("SELECT * FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
        return self._artifact(row)

    def list_runs(self, project_id: UUID) -> list[Run]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM runs WHERE project_id = %s ORDER BY started_at DESC", (project_id,)).fetchall()
        return [self._run(row) for row in rows]

    def get_run(self, run_id: UUID) -> Run:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE id = %s", (run_id,)).fetchone()
        if not row:
            raise KeyError("run_not_found")
        return self._run(row)

    def list_handoffs(self, project_id: UUID) -> list[Handoff]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM handoffs WHERE project_id = %s ORDER BY created_at DESC", (project_id,)).fetchall()
        return [self._handoff(row) for row in rows]

    def get_handoff(self, handoff_id: UUID) -> Handoff:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM handoffs WHERE id = %s", (handoff_id,)).fetchone()
        if not row:
            raise KeyError("handoff_not_found")
        return self._handoff(row)

    def _ensure_gate(self, connection: Any, project_id: UUID, target_type: str, target_id: UUID) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM gates WHERE project_id = %s AND target_type = %s AND target_id = %s FOR UPDATE",
            (project_id, target_type, target_id),
        ).fetchone()
        if row:
            return row
        gate_id = uuid4()
        connection.execute(
            """
            INSERT INTO gates
                (id, project_id, target_type, target_id, status, required_human_approval,
                 blocking_findings, rules, review_ids, evidence_ids, risk_summary,
                 input_snapshot, invalidated_at, invalidation_reason,
                 approved_by, approved_at)
            VALUES (%s, %s, %s, %s, 'OPEN', true, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL, NULL)
            ON CONFLICT (project_id, target_type, target_id) DO NOTHING
            """,
            (gate_id, project_id, target_type, target_id, self._jsonb([]), self._jsonb(gate_rules(target_type)), self._jsonb([]), self._jsonb([]), self._jsonb({}), self._jsonb({})),
        )
        return connection.execute(
            "SELECT * FROM gates WHERE project_id = %s AND target_type = %s AND target_id = %s FOR UPDATE",
            (project_id, target_type, target_id),
        ).fetchone()

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
            raw_entries = [{"type": receiver_type, "id": value} for value in ids] if isinstance(ids, list) else []
        elif isinstance(receiver, list):
            raw_entries = receiver
        else:
            raw_entries = []
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

    def create_handoff(self, project_id: UUID, data: HandoffCreate, sender_agent_id: str = "agent-platform") -> Handoff:
        """Create a structured handoff and its audit event atomically."""
        request_fingerprint = self._request_hash(data.model_dump(mode="json", exclude={"idempotency_key"}))
        with self.transaction() as connection:
            self._lock_project(connection, project_id)
            if data.idempotency_key:
                previous = self._load_idempotent_response(connection, data.idempotency_key, "handoff.create", request_fingerprint)
                if previous is not None:
                    return self._handoff(previous, connection)

            task = connection.execute("SELECT project_id FROM tasks WHERE id = %s", (data.task_id,)).fetchone()
            if not task:
                raise KeyError("task_not_found")
            if task["project_id"] != project_id:
                raise ValueError("task_not_in_project")

            for reference in [*data.input_artifacts, *data.output_artifacts, *data.evidence_refs]:
                try:
                    artifact_id = UUID(str(reference))
                except (TypeError, ValueError):
                    continue
                artifact = connection.execute("SELECT project_id FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
                if not artifact or artifact["project_id"] != project_id:
                    raise ValueError("handoff_artifact_not_in_project")

            handoff_id = uuid4()
            timestamp = _now()
            status = data.status.value if hasattr(data.status, "value") else str(data.status)
            handoff_type = data.handoff_type.value if hasattr(data.handoff_type, "value") else str(data.handoff_type)
            if handoff_type == "AGGREGATE" and not data.input_handoff_ids:
                raise ValueError("aggregate_handoff_inputs_required")
            if handoff_type != "AGGREGATE" and data.input_handoff_ids:
                raise ValueError("only_aggregate_handoff_accepts_inputs")
            recipient_entries = self._handoff_recipient_entries(data.receiver, handoff_type)
            revision_number = 1
            if data.revision_of_handoff_id:
                previous = connection.execute("SELECT project_id, task_id, status, receipt_status, revision_number FROM handoffs WHERE id = %s FOR UPDATE", (data.revision_of_handoff_id,)).fetchone()
                if not previous or previous["project_id"] != project_id or previous["task_id"] != data.task_id:
                    raise ValueError("handoff_revision_scope_mismatch")
                if previous["receipt_status"] != "REJECTED" or previous["status"] != "NEEDS_REVISION":
                    raise ValueError("handoff_revision_requires_rejection")
                revision_number = int(previous["revision_number"] or 1) + 1
            elif status == "NEEDS_REVISION":
                raise ValueError("new_handoff_cannot_start_needs_revision")
            for input_handoff_id in data.input_handoff_ids:
                input_handoff = connection.execute("SELECT project_id, status, receipt_status, requires_human_approval FROM handoffs WHERE id = %s", (input_handoff_id,)).fetchone()
                if not input_handoff or input_handoff["project_id"] != project_id or input_handoff["receipt_status"] != "ACCEPTED" or input_handoff["status"] not in {"PASS", "PASS_WITH_ASSUMPTIONS"}:
                    raise ValueError("aggregate_input_handoff_not_ready")
                gate = connection.execute("SELECT status FROM gates WHERE project_id = %s AND target_type = 'handoff' AND target_id = %s", (project_id, input_handoff_id)).fetchone()
                if input_handoff["requires_human_approval"] and (not gate or gate["status"] != "PASSED"):
                    raise ValueError("aggregate_input_handoff_gate_not_passed")
            connection.execute(
                """
                INSERT INTO handoffs
                    (id, project_id, task_id, sender_agent_id, receiver, status, objective,
                     completed, input_artifacts, output_artifacts, key_conclusions, assumptions,
                     evidence_refs, open_questions, risks, next_actions, requires_human_approval,
                     schema_version, handoff_type, input_handoff_ids, revision_of_handoff_id,
                     revision_number, receipt_status, received_by, received_at, decision_reason,
                     decision_findings, idempotency_key, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s, %s, %s, %s, %s, %s, 'PENDING', NULL, NULL, NULL, %s, %s, %s)
                """,
                (
                    handoff_id,
                    project_id,
                    data.task_id,
                    sender_agent_id,
                    self._jsonb(data.receiver),
                    status,
                    data.objective,
                    self._jsonb(data.completed),
                    self._jsonb(data.input_artifacts),
                    self._jsonb(data.output_artifacts),
                    self._jsonb(data.key_conclusions),
                    self._jsonb(data.assumptions),
                    self._jsonb(data.evidence_refs),
                    self._jsonb(data.open_questions),
                    self._jsonb(data.risks),
                    self._jsonb(data.next_actions),
                    data.requires_human_approval,
                    data.schema_version,
                    handoff_type,
                    self._jsonb([str(value) for value in data.input_handoff_ids]),
                    data.revision_of_handoff_id,
                    revision_number,
                    self._jsonb([]),
                    data.idempotency_key,
                    timestamp,
                ),
            )
            for receiver_type, receiver_id in recipient_entries:
                connection.execute(
                    "INSERT INTO handoff_receipts (id, handoff_id, project_id, receiver_type, receiver_id, status, decision_findings, created_at) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s)",
                    (uuid4(), handoff_id, project_id, receiver_type, receiver_id, self._jsonb([]), timestamp),
                )
            event = self._insert_event(
                connection,
                project_id,
                "handoff.created",
                sender_agent_id,
                {"handoff_id": str(handoff_id), "status": status, "handoff_type": handoff_type, "receivers": [{"type": item[0], "id": item[1]} for item in recipient_entries]},
                actor_kind="agent",
                object_type="handoff",
                object_id=handoff_id,
                idempotency_key=f"handoff-event:{handoff_id}",
            )
            row = connection.execute("SELECT * FROM handoffs WHERE id = %s", (handoff_id,)).fetchone()
            result = self._handoff(row, connection)
            if data.idempotency_key:
                self._store_idempotent_response(connection, data.idempotency_key, "handoff.create", result.model_dump(mode="json"), timestamp, request_fingerprint)
            return result

    @staticmethod
    def _sync_handoff_receipt_state(connection: Any, handoff_id: UUID) -> dict[str, Any]:
        rows = connection.execute("SELECT status, received_by, received_at FROM handoff_receipts WHERE handoff_id = %s ORDER BY received_at DESC NULLS LAST, created_at ASC", (handoff_id,)).fetchall()
        if not rows:
            return connection.execute("SELECT * FROM handoffs WHERE id = %s", (handoff_id,)).fetchone()
        statuses = {row["status"] for row in rows}
        aggregate_status = "REJECTED" if "REJECTED" in statuses else ("ACCEPTED" if statuses == {"ACCEPTED"} else "PENDING")
        latest = next((row for row in rows if row["received_at"] is not None), None)
        connection.execute(
            "UPDATE handoffs SET receipt_status = %s, received_by = %s, received_at = %s WHERE id = %s",
            (aggregate_status, latest["received_by"] if latest else None, latest["received_at"] if latest else None, handoff_id),
        )
        return connection.execute("SELECT * FROM handoffs WHERE id = %s", (handoff_id,)).fetchone()

    def accept_handoff(self, handoff_id: UUID, receiver: str, actor_kind: str = "agent") -> Handoff:
        """Accept a passed handoff and record the receipt event atomically."""
        with self.transaction() as connection:
            project_row = connection.execute("SELECT project_id FROM handoffs WHERE id = %s", (handoff_id,)).fetchone()
            if not project_row:
                raise KeyError("handoff_not_found")
            project_id = UUID(str(project_row["project_id"]))
            self._lock_project(connection, project_id)
            row = connection.execute("SELECT * FROM handoffs WHERE id = %s FOR UPDATE", (handoff_id,)).fetchone()
            if not row:
                raise KeyError("handoff_not_found")
            receipt = connection.execute("SELECT * FROM handoff_receipts WHERE handoff_id = %s AND receiver_id = %s FOR UPDATE", (handoff_id, receiver)).fetchone()
            if not receipt:
                raise ValueError("handoff_receiver_mismatch")
            if receipt["status"] == "ACCEPTED":
                return self._handoff(row, connection)
            if receipt["status"] == "REJECTED":
                raise ValueError("handoff_rejected_requires_revision")
            if row["status"] not in {"PASS", "PASS_WITH_ASSUMPTIONS"}:
                raise ValueError("handoff_not_acceptible")
            timestamp = _now()
            connection.execute(
                "UPDATE handoff_receipts SET status = 'ACCEPTED', received_by = %s, received_at = %s WHERE id = %s",
                (receiver, timestamp, receipt["id"]),
            )
            self._sync_handoff_receipt_state(connection, handoff_id)
            self._insert_event(
                connection,
                UUID(str(row["project_id"])),
                "handoff.accepted",
                receiver,
                {"handoff_id": str(handoff_id), "actor_kind": actor_kind},
                actor_kind=actor_kind,
                object_type="handoff",
                object_id=handoff_id,
                idempotency_key=f"handoff-accepted:{handoff_id}:{receiver}",
            )
            return self._handoff(connection.execute("SELECT * FROM handoffs WHERE id = %s", (handoff_id,)).fetchone(), connection)

    def reject_handoff(self, handoff_id: UUID, actor: str, reason: str, findings: list[dict[str, Any]] | None = None, actor_kind: str = "agent") -> Handoff:
        normalized = normalize_findings(findings or [])
        with self.transaction() as connection:
            project_row = connection.execute("SELECT project_id FROM handoffs WHERE id = %s", (handoff_id,)).fetchone()
            if not project_row:
                raise KeyError("handoff_not_found")
            project_id = UUID(str(project_row["project_id"]))
            self._lock_project(connection, project_id)
            row = connection.execute("SELECT * FROM handoffs WHERE id = %s FOR UPDATE", (handoff_id,)).fetchone()
            if not row:
                raise KeyError("handoff_not_found")
            receipt = connection.execute("SELECT * FROM handoff_receipts WHERE handoff_id = %s AND receiver_id = %s FOR UPDATE", (handoff_id, actor)).fetchone()
            if not receipt:
                raise ValueError("handoff_receiver_mismatch")
            if receipt["status"] == "REJECTED":
                return self._handoff(row, connection)
            if receipt["status"] == "ACCEPTED":
                raise ValueError("accepted_handoff_cannot_be_rejected")
            timestamp = _now()
            connection.execute(
                "UPDATE handoffs SET status = 'NEEDS_REVISION', decision_reason = %s, decision_findings = %s WHERE id = %s",
                (reason, self._jsonb(normalized), handoff_id),
            )
            connection.execute(
                "UPDATE handoff_receipts SET status = 'REJECTED', received_by = %s, received_at = %s, decision_reason = %s, decision_findings = %s WHERE id = %s",
                (actor, timestamp, reason, self._jsonb(normalized), receipt["id"]),
            )
            self._sync_handoff_receipt_state(connection, handoff_id)
            self._insert_event(
                connection,
                UUID(str(row["project_id"])),
                "handoff.rejected",
                actor,
                {"handoff_id": str(handoff_id), "reason": reason, "findings": normalized, "actor_kind": actor_kind},
                actor_kind=actor_kind,
                object_type="handoff",
                object_id=handoff_id,
                idempotency_key=f"handoff-rejected:{handoff_id}",
            )
            return self._handoff(connection.execute("SELECT * FROM handoffs WHERE id = %s", (handoff_id,)).fetchone(), connection)

    def list_reviews(self, project_id: UUID) -> list[Review]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM reviews WHERE project_id = %s ORDER BY created_at DESC", (project_id,)).fetchall()
        return [self._review(row) for row in rows]

    def list_risks(self, project_id: UUID) -> list[RiskRegistryEntry]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM risks WHERE project_id = %s ORDER BY created_at DESC", (project_id,)).fetchall()
        return [self._risk(row) for row in rows]

    @staticmethod
    def _risk(row: dict[str, Any]) -> RiskRegistryEntry:
        values = dict(row)
        for key in ["id", "project_id", "review_id", "target_id"]:
            values[key] = UUID(str(values[key]))
        values["resolved"] = bool(values["resolved"])
        values["evidence_refs"] = PostgresRepository._json(values.get("evidence_refs"), [])
        values["closure_evidence_ids"] = [UUID(str(value)) for value in PostgresRepository._json(values.get("closure_evidence_ids"), [])]
        return RiskRegistryEntry.model_validate(values)

    def update_risk(self, project_id: UUID, risk_id: UUID, data: RiskDecisionRequest, actor: str = "member-001") -> RiskRegistryEntry:
        request_fingerprint = self._request_hash({"project_id": str(project_id), "risk_id": str(risk_id), **data.model_dump(mode="json", exclude={"idempotency_key"})})
        with self.transaction() as connection:
            self._lock_project(connection, project_id)
            if data.idempotency_key:
                previous = self._load_idempotent_response(connection, data.idempotency_key, "risk.update", request_fingerprint)
                if previous is not None:
                    return self._risk(previous)
            row = connection.execute("SELECT * FROM risks WHERE id = %s AND project_id = %s FOR UPDATE", (risk_id, project_id)).fetchone()
            if not row:
                raise KeyError("risk_not_found")
            evidence_ids = [str(value) for value in data.evidence_ids]
            for evidence_id in data.evidence_ids:
                evidence = connection.execute("SELECT project_id FROM evidence WHERE id = %s", (evidence_id,)).fetchone()
                if not evidence or evidence["project_id"] != project_id:
                    raise ValueError("risk_closure_evidence_not_in_project")
            if data.action == "ASSIGN" and not data.owner:
                raise ValueError("risk_owner_required")
            if data.action == "RESOLVE" and (not data.reason or not evidence_ids):
                raise ValueError("risk_resolution_reason_and_evidence_required")
            timestamp = _now()
            if data.action == "ASSIGN":
                connection.execute("UPDATE risks SET owner = %s WHERE id = %s", (data.owner, risk_id))
            elif data.action == "RESOLVE":
                connection.execute("UPDATE risks SET resolved = true, owner = COALESCE(%s, owner), resolution_reason = %s, resolved_by = %s, resolved_at = %s, closure_evidence_ids = %s WHERE id = %s", (data.owner, data.reason, actor, timestamp, self._jsonb(evidence_ids), risk_id))
            else:
                connection.execute("UPDATE risks SET resolved = false, resolution_reason = NULL, resolved_by = NULL, resolved_at = NULL, closure_evidence_ids = '[]'::jsonb WHERE id = %s", (risk_id,))
                gate = connection.execute("SELECT * FROM gates WHERE project_id = %s AND target_type = %s AND target_id = %s FOR UPDATE", (project_id, row["target_type"], row["target_id"])).fetchone()
                if gate and row["severity"] in {"fatal", "major"} and gate["status"] == "PASSED":
                    connection.execute("UPDATE gates SET status = 'BLOCKED', invalidated_at = %s, invalidation_reason = %s, approved_by = NULL, approved_at = NULL WHERE id = %s", (timestamp, "risk_reopened", gate["id"]))
                    self._propagate_gate_invalidation(connection, gate, "risk_reopened")
            self._insert_event(connection, project_id, "risk.updated", actor, {"risk_id": str(risk_id), "action": data.action, "reason": data.reason, "evidence_ids": evidence_ids}, actor_kind="member", object_type="risk", object_id=risk_id)
            result = self._risk(connection.execute("SELECT * FROM risks WHERE id = %s", (risk_id,)).fetchone())
            if data.idempotency_key:
                self._store_idempotent_response(connection, data.idempotency_key, "risk.update", result.model_dump(mode="json"), timestamp, request_fingerprint)
            return result

    def create_review(self, project_id: UUID, data: ReviewCreate) -> Review:
        """Apply a review, gate transition, audit event, and idempotency record atomically."""
        target_tables = {"task": "tasks", "artifact": "artifacts", "handoff": "handoffs"}
        target_table = target_tables.get(data.target_type)
        if not target_table:
            raise ValueError("review_target_type_invalid")
        request_fingerprint = self._request_hash(data.model_dump(mode="json", exclude={"idempotency_key"}))

        with self.transaction() as connection:
            self._lock_project(connection, project_id)
            if data.idempotency_key:
                previous = self._load_idempotent_response(connection, data.idempotency_key, "review.create", request_fingerprint)
                if previous is not None:
                    return self._review(previous)

            target = connection.execute(
                f"SELECT * FROM {target_table} WHERE id = %s AND project_id = %s FOR UPDATE",
                (data.target_id, project_id),
            ).fetchone()
            if not target:
                raise KeyError("review_target_not_found")

            normalized_findings = normalize_findings(data.findings)
            if any(item.get("resolved") for item in normalized_findings):
                raise ValueError("finding_resolution_requires_risk_action")
            if data.verdict == "APPROVED":
                normalized_findings = ensure_approvable(normalized_findings)
            for evidence_id in data.evidence_ids:
                evidence = connection.execute("SELECT project_id FROM evidence WHERE id = %s", (evidence_id,)).fetchone()
                if not evidence or evidence["project_id"] != project_id:
                    raise ValueError("review_evidence_not_in_project")
            reviewer_kind = data.reviewer_kind.value if hasattr(data.reviewer_kind, "value") else str(data.reviewer_kind)
            if data.verdict == "APPROVED" and reviewer_kind != "member":
                raise PermissionError("human_approval_required")
            if reviewer_kind == "member":
                permission = "review.approve" if data.verdict == "APPROVED" else "review.submit"
                self._authorize_project_member(connection, project_id, data.reviewer, permission)
            if data.verdict == "APPROVED":
                open_risk = connection.execute(
                    "SELECT 1 FROM risks WHERE project_id = %s AND target_type = %s AND target_id = %s AND resolved = false AND severity IN ('fatal', 'major') LIMIT 1",
                    (project_id, data.target_type, data.target_id),
                ).fetchone()
                if open_risk:
                    raise ValueError("review_blocked_by_open_risks")

            self._ensure_gate(connection, project_id, data.target_type, data.target_id)
            timestamp = _now()
            if data.target_type == "task":
                current = target["status"]
                if data.verdict == "APPROVED" and current not in {"WAITING_REVIEW", "NEEDS_REVISION", "APPROVED"}:
                    raise ValueError("task_not_waiting_for_review")
                if data.verdict != "APPROVED" and current == "APPROVED":
                    raise ValueError("approved_task_is_immutable")
                next_status = {"APPROVED": "APPROVED", "NEEDS_REVISION": "NEEDS_REVISION", "BLOCKED": "BLOCKED"}[data.verdict]
                connection.execute(
                    "UPDATE tasks SET status = %s, blocked_reason = %s, updated_at = %s WHERE id = %s",
                    (next_status, data.summary if next_status == "BLOCKED" else None, timestamp, data.target_id),
                )
            elif data.target_type == "artifact":
                if data.verdict == "APPROVED":
                    connection.execute(
                        "UPDATE artifacts SET status = 'APPROVED', approved_by = %s, approved_at = %s, downstream_allowed = true, immutable = true WHERE id = %s",
                        (data.reviewer, timestamp, data.target_id),
                    )
                else:
                    connection.execute(
                        "UPDATE artifacts SET status = 'REJECTED', downstream_allowed = false WHERE id = %s",
                        (data.target_id,),
                    )
            else:
                if data.verdict == "APPROVED" and target["receipt_status"] == "REJECTED":
                    raise ValueError("rejected_handoff_requires_revision")
                next_status = {"APPROVED": "PASS", "NEEDS_REVISION": "NEEDS_REVISION", "BLOCKED": "BLOCKED"}[data.verdict]
                connection.execute("UPDATE handoffs SET status = %s WHERE id = %s", (next_status, data.target_id))

            gate_status = {"APPROVED": "PASSED", "NEEDS_REVISION": "FAILED", "BLOCKED": "BLOCKED"}[data.verdict]
            connection.execute(
                """
                UPDATE gates
                SET status = %s, blocking_findings = %s, rules = %s,
                    risk_summary = %s, approved_by = %s, approved_at = %s
                WHERE project_id = %s AND target_type = %s AND target_id = %s
                """,
                (
                    gate_status,
                    self._jsonb(blocking_findings(normalized_findings)),
                    self._jsonb(gate_rules(data.target_type)),
                    self._jsonb(risk_summary(normalized_findings)),
                    data.reviewer if data.verdict == "APPROVED" else None,
                    timestamp if data.verdict == "APPROVED" else None,
                    project_id,
                    data.target_type,
                    data.target_id,
                ),
            )

            review_id = uuid4()
            current_gate = connection.execute("SELECT review_ids, evidence_ids FROM gates WHERE project_id = %s AND target_type = %s AND target_id = %s FOR UPDATE", (project_id, data.target_type, data.target_id)).fetchone()
            review_ids = self._json(current_gate["review_ids"] if current_gate else None, [])
            evidence_ids = self._json(current_gate["evidence_ids"] if current_gate else None, [])
            review_ids.append(str(review_id))
            evidence_ids.extend(str(value) for value in data.evidence_ids if str(value) not in evidence_ids)
            summary = risk_summary(normalized_findings)
            connection.execute("UPDATE gates SET review_ids = %s, evidence_ids = %s, risk_summary = %s WHERE project_id = %s AND target_type = %s AND target_id = %s", (self._jsonb(review_ids), self._jsonb(evidence_ids), self._jsonb(summary), project_id, data.target_type, data.target_id))
            connection.execute(
                """
                INSERT INTO reviews
                    (id, project_id, target_type, target_id, verdict, summary, findings,
                     evidence_ids, reviewer, reviewer_kind, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (review_id, project_id, data.target_type, data.target_id, data.verdict, data.summary, self._jsonb(normalized_findings), self._jsonb([str(value) for value in data.evidence_ids]), data.reviewer, reviewer_kind, timestamp),
            )
            for finding in normalized_findings:
                connection.execute(
                    """
                    INSERT INTO risks
                        (id, project_id, review_id, target_type, target_id, code, severity,
                         message, resolved, evidence_refs, owner, resolution_reason,
                         resolved_by, resolved_at, closure_evidence_ids, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, false, %s, NULL, NULL, NULL, NULL, %s, %s)
                    """,
                    (uuid4(), project_id, review_id, data.target_type, data.target_id, finding["code"], finding["severity"], finding["message"], self._jsonb(finding.get("evidence_refs", [])), self._jsonb([]), timestamp),
                )
            snapshot = self._capture_gate_snapshot(connection, project_id, data.target_type, data.target_id, review_ids, evidence_ids)
            connection.execute("UPDATE gates SET input_snapshot = %s, invalidated_at = NULL, invalidation_reason = NULL WHERE project_id = %s AND target_type = %s AND target_id = %s", (self._jsonb(snapshot), project_id, data.target_type, data.target_id))
            self._insert_event(
                connection,
                project_id,
                "review.created",
                data.reviewer,
                {"review_id": str(review_id), "target_type": data.target_type, "target_id": str(data.target_id), "verdict": data.verdict, "reviewer_kind": reviewer_kind, "risk_summary": summary},
                actor_kind=reviewer_kind,
                object_type="review",
                object_id=review_id,
                idempotency_key=f"review-event:{review_id}",
            )
            result = self._review(connection.execute("SELECT * FROM reviews WHERE id = %s", (review_id,)).fetchone())
            if data.idempotency_key:
                self._store_idempotent_response(connection, data.idempotency_key, "review.create", result.model_dump(mode="json"), timestamp, request_fingerprint)
            return result

    def list_gates(self, project_id: UUID) -> list[Gate]:
        with self.transaction() as connection:
            self._lock_project(connection, project_id)
            rows = connection.execute("SELECT * FROM gates WHERE project_id = %s ORDER BY approved_at DESC NULLS FIRST", (project_id,)).fetchall()
            for row in rows:
                self._refresh_gate_if_stale(connection, row)
            refreshed = [self._refresh_gate_if_stale(connection, row) for row in connection.execute("SELECT * FROM gates WHERE project_id = %s ORDER BY approved_at DESC NULLS FIRST", (project_id,)).fetchall()]
        return [self._gate(row) for row in refreshed]

    def _gate_target_snapshot(self, connection: Any, target_type: str, target_id: UUID | None) -> dict[str, Any]:
        if target_id is None:
            return {}
        tables = {"task": "tasks", "artifact": "artifacts", "handoff": "handoffs"}
        table = tables.get(target_type)
        if not table:
            return {"type": target_type, "id": str(target_id)}
        row = connection.execute(f"SELECT * FROM {table} WHERE id = %s", (target_id,)).fetchone()
        if not row:
            return {"type": target_type, "id": str(target_id), "missing": True}
        artifact_refs: list[str] = []
        if target_type == "task":
            artifact_refs = [str(value) for value in self._json(row.get("input_artifacts"), [])]
            value = {"status": row["status"], "updated_at": row.get("updated_at"), "input_artifacts": artifact_refs, "input_handoff_ids": self._json(row.get("input_handoff_ids"), []), "dependency_task_ids": self._json(row.get("dependency_task_ids"), [])}
        elif target_type == "artifact":
            artifact_refs = [str(value) for value in self._json(row.get("input_artifact_ids"), [])]
            value = {"version": row["version"], "content_hash": row["content_hash"], "status": row["status"], "immutable": row.get("immutable", False), "input_artifact_ids": artifact_refs}
        else:
            artifact_refs = [str(value) for value in [*self._json(row.get("input_artifacts"), []), *self._json(row.get("output_artifacts"), [])]]
            value = {"status": row["status"], "revision_number": row.get("revision_number", 1), "artifact_refs": artifact_refs, "input_handoff_ids": self._json(row.get("input_handoff_ids"), [])}
        sources: list[dict[str, Any]] = []
        for reference in artifact_refs:
            try:
                artifact_id = UUID(reference)
            except ValueError:
                continue
            artifact = connection.execute("SELECT id, version, content_hash, status FROM artifacts WHERE id = %s", (artifact_id,)).fetchone()
            sources.append(dict(artifact) if artifact else {"id": str(artifact_id), "missing": True})
        return {"type": target_type, "id": str(target_id), "fingerprint": self._request_hash(value), "source_artifacts": sources}

    def _capture_gate_snapshot(self, connection: Any, project_id: UUID, target_type: str, target_id: UUID | None, review_ids: list[str], evidence_ids: list[str]) -> dict[str, Any]:
        reviews: list[dict[str, Any]] = []
        for review_id in review_ids:
            review = connection.execute("SELECT verdict, summary, findings, evidence_ids FROM reviews WHERE id = %s AND project_id = %s", (UUID(review_id), project_id)).fetchone()
            reviews.append({"id": review_id, "fingerprint": self._request_hash(review)} if review else {"id": review_id, "missing": True})
        evidence: list[dict[str, Any]] = []
        for evidence_id in evidence_ids:
            item = connection.execute("SELECT claim, evidence_type, source_ref, artifact_id, run_id FROM evidence WHERE id = %s AND project_id = %s", (UUID(evidence_id), project_id)).fetchone()
            evidence.append({"id": evidence_id, "fingerprint": self._request_hash(item)} if item else {"id": evidence_id, "missing": True})
        return {"target": self._gate_target_snapshot(connection, target_type, target_id), "review_ids": list(review_ids), "reviews": reviews, "evidence_ids": list(evidence_ids), "evidence": evidence}

    def _gate_snapshot_is_current(self, connection: Any, row: dict[str, Any]) -> bool:
        snapshot = self._json(row.get("input_snapshot"), {})
        if not snapshot:
            return True
        current = self._capture_gate_snapshot(connection, UUID(str(row["project_id"])), row["target_type"], UUID(str(row["target_id"])) if row.get("target_id") else None, [str(value) for value in snapshot.get("review_ids", [])], [str(value) for value in snapshot.get("evidence_ids", [])])
        return current == snapshot

    def _tasks_affected_by_gate(self, connection: Any, gate: dict[str, Any]) -> list[str]:
        """Walk project references so a stale gate becomes visible downstream."""
        project_id = gate["project_id"]
        tasks = connection.execute("SELECT id, dependency_task_ids, input_artifacts, input_handoff_ids FROM tasks WHERE project_id = %s", (project_id,)).fetchall()
        artifacts = connection.execute("SELECT id, input_artifact_ids FROM artifacts WHERE project_id = %s", (project_id,)).fetchall()
        handoffs = connection.execute("SELECT id, task_id, input_artifacts, output_artifacts, input_handoff_ids FROM handoffs WHERE project_id = %s", (project_id,)).fetchall()
        root_type = gate["target_type"]
        root_id = str(gate["target_id"]) if gate.get("target_id") else None
        if not root_id or root_type == "project":
            return []

        queue: list[tuple[str, str]] = [(root_type, root_id)]
        seen: set[tuple[str, str]] = set()
        affected: set[str] = set()
        task_ids = {str(item["id"]) for item in tasks}
        while queue:
            node_type, node_id = queue.pop(0)
            node = (node_type, node_id)
            if node in seen:
                continue
            seen.add(node)
            if node_type == "task":
                if node_id in task_ids:
                    affected.add(node_id)
                for item in tasks:
                    if node_id in {str(value) for value in self._json(item.get("dependency_task_ids"), [])}:
                        queue.append(("task", str(item["id"])))
                for item in handoffs:
                    if str(item.get("task_id")) == node_id:
                        queue.append(("handoff", str(item["id"])))
            elif node_type == "artifact":
                for item in artifacts:
                    if node_id in {str(value) for value in self._json(item.get("input_artifact_ids"), [])}:
                        queue.append(("artifact", str(item["id"])))
                for item in tasks:
                    if node_id in {str(value) for value in self._json(item.get("input_artifacts"), [])}:
                        queue.append(("task", str(item["id"])))
                for item in handoffs:
                    refs = [*self._json(item.get("input_artifacts"), []), *self._json(item.get("output_artifacts"), [])]
                    if node_id in {str(value) for value in refs}:
                        queue.append(("handoff", str(item["id"])))
            elif node_type == "handoff":
                for item in handoffs:
                    if node_id in {str(value) for value in self._json(item.get("input_handoff_ids"), [])}:
                        queue.append(("handoff", str(item["id"])))
                for item in tasks:
                    if node_id in {str(value) for value in self._json(item.get("input_handoff_ids"), [])}:
                        queue.append(("task", str(item["id"])))
        return sorted(affected)

    def _propagate_gate_invalidation(self, connection: Any, gate: dict[str, Any], reason: str) -> None:
        """Invalidate dependent task gates and make stale work explicit."""
        project_id = gate["project_id"]
        for task_id in self._tasks_affected_by_gate(connection, gate):
            task = connection.execute("SELECT * FROM tasks WHERE id = %s", (UUID(task_id),)).fetchone()
            if not task or task["status"] in {"FAILED", "CANCELLED"}:
                continue
            current = task["status"]
            next_status = "NEEDS_REVISION" if current in {"APPROVED", "WAITING_REVIEW", "NEEDS_REVISION"} else "BLOCKED"
            blocked_reason = f"upstream_gate_invalidated:{gate['id']}:{reason}"
            if current != next_status or task.get("blocked_reason") != blocked_reason:
                connection.execute("UPDATE tasks SET status = %s, blocked_reason = %s, updated_at = %s WHERE id = %s", (next_status, blocked_reason, _now(), UUID(task_id)))
                self._insert_event(connection, project_id, "task.upstream_gate_invalidated", "system", {"task_id": task_id, "gate_id": str(gate["id"]), "gate_target_type": gate["target_type"], "gate_target_id": str(gate["target_id"]) if gate.get("target_id") else None, "reason": reason, "status": next_status}, actor_kind="system", object_type="task", object_id=UUID(task_id), idempotency_key=f"task-gate-propagation:{gate['id']}:{task_id}:{reason}")
            downstream_gate = connection.execute("SELECT * FROM gates WHERE project_id = %s AND target_type = 'task' AND target_id = %s FOR UPDATE", (project_id, UUID(task_id))).fetchone()
            if downstream_gate and downstream_gate["status"] == "PASSED":
                timestamp = _now()
                connection.execute("UPDATE gates SET status = 'INVALIDATED', invalidated_at = %s, invalidation_reason = %s, approved_by = NULL, approved_at = NULL WHERE id = %s", (timestamp, "upstream_gate_invalidated", downstream_gate["id"]))
                self._insert_event(connection, project_id, "gate.invalidated", "system", {"gate_id": str(downstream_gate["id"]), "target_type": "task", "target_id": task_id, "reason": "upstream_gate_invalidated", "source_gate_id": str(gate["id"])}, actor_kind="system", object_type="gate", object_id=UUID(str(downstream_gate["id"])), idempotency_key=f"gate-propagation:{gate['id']}:{downstream_gate['id']}:{reason}")

    def _refresh_gate_if_stale(self, connection: Any, row: dict[str, Any]) -> dict[str, Any]:
        locked = connection.execute("SELECT * FROM gates WHERE id = %s FOR UPDATE", (row["id"],)).fetchone()
        if not locked or locked["status"] != "PASSED" or self._gate_snapshot_is_current(connection, locked):
            return locked or row
        row = locked
        timestamp = _now()
        connection.execute("UPDATE gates SET status = 'INVALIDATED', invalidated_at = %s, invalidation_reason = %s, approved_by = NULL, approved_at = NULL WHERE id = %s", (timestamp, "input_snapshot_changed", row["id"]))
        self._insert_event(connection, UUID(str(row["project_id"])), "gate.invalidated", "system", {"gate_id": str(row["id"]), "target_type": row["target_type"], "target_id": str(row["target_id"]) if row.get("target_id") else None, "reason": "input_snapshot_changed"}, actor_kind="system", object_type="gate", object_id=UUID(str(row["id"])), idempotency_key=f"gate-invalidated:{row['id']}:{row.get('input_snapshot')}" )
        self._propagate_gate_invalidation(connection, row, "input_snapshot_changed")
        return connection.execute("SELECT * FROM gates WHERE id = %s", (row["id"],)).fetchone()

    def list_evidence(self, project_id: UUID) -> list[Evidence]:
        with self.transaction() as connection:
            rows = connection.execute("SELECT * FROM evidence WHERE project_id = %s ORDER BY created_at DESC", (project_id,)).fetchall()
        return [self._evidence(row) for row in rows]

    def list_events(self, project_id: UUID, after: int = 0, limit: int = 100) -> list[Event]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE project_id = %s AND sequence > %s ORDER BY sequence ASC LIMIT %s",
                (project_id, after, limit),
            ).fetchall()
        return [self._event(row) for row in rows]

    def get_event(self, event_id: UUID) -> Event:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM events WHERE id = %s", (event_id,)).fetchone()
        if not row:
            raise KeyError("event_not_found")
        return self._event(row)

    def _insert_event(
        self,
        connection: Any,
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
        self._lock_project(connection, project_id)
        if idempotency_key:
            existing = connection.execute(
                "SELECT * FROM events WHERE project_id = %s AND idempotency_key = %s",
                (project_id, idempotency_key),
            ).fetchone()
            if existing:
                return self._event(existing)
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM events WHERE project_id = %s",
            (project_id,),
        ).fetchone()["sequence"]
        event_id = uuid4()
        timestamp = _now()
        event = Event(
            id=event_id,
            project_id=project_id,
            sequence=sequence,
            event_type=event_type,
            actor=actor,
            payload=payload,
            created_at=timestamp,
            actor_kind=actor_kind,
            object_type=object_type,
            object_id=object_id,
            idempotency_key=idempotency_key,
            schema_version=schema_version,
        )
        connection.execute(
            """
            INSERT INTO events
                (id, project_id, sequence, event_type, actor, payload, created_at,
                 actor_kind, object_type, object_id, idempotency_key, schema_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (event_id, project_id, sequence, event_type, actor, self._jsonb(payload), timestamp, actor_kind, object_type, object_id, idempotency_key, schema_version),
        )
        connection.execute(
            """
            INSERT INTO event_outbox
                (id, event_id, project_id, status, attempts, available_at, created_at, updated_at)
            VALUES (%s, %s, %s, 'PENDING', 0, %s, %s, %s)
            """,
            (uuid4(), event_id, project_id, timestamp, timestamp, timestamp),
        )
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
        with self.transaction() as connection:
            self._lock_project(connection, project_id)
            if idempotency_key:
                existing = connection.execute(
                    "SELECT response FROM idempotency_records WHERE key = %s AND organization_id = %s AND operation = %s",
                    (self._scoped_idempotency_key(idempotency_key), self.organization_id, "event.create"),
                ).fetchone()
                if existing:
                    return self._event(existing["response"])
            connection.execute("SELECT id FROM projects WHERE id = %s FOR UPDATE", (project_id,)).fetchone()
            event = self._insert_event(
                connection,
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
            timestamp = event.created_at
            if idempotency_key:
                connection.execute(
                    "INSERT INTO idempotency_records (key, organization_id, operation, response, created_at) VALUES (%s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                    (self._scoped_idempotency_key(idempotency_key), self.organization_id, "event.create", self._jsonb(event.model_dump(mode="json")), timestamp),
                )
        return event

    def list_event_outbox(self, project_id: UUID | None = None, *, status: str | None = None, limit: int = 100) -> list[EventOutbox]:
        query = "SELECT * FROM event_outbox WHERE 1 = 1"
        params: list[Any] = []
        if project_id is not None:
            query += " AND project_id = %s"
            params.append(project_id)
        if status is not None:
            query += " AND status = %s"
            params.append(status)
        query += " ORDER BY created_at ASC LIMIT %s"
        params.append(max(1, min(limit, 1000)))
        with self.transaction() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._event_outbox(row) for row in rows]

    def list_pending_event_outbox(self, limit: int = 100) -> list[EventOutbox]:
        with self.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM event_outbox WHERE (status IN ('PENDING', 'FAILED') AND available_at <= %s) OR (status = 'PROCESSING' AND lock_expires_at <= %s) ORDER BY created_at ASC LIMIT %s",
                (_now(), _now(), max(1, min(limit, 1000))),
            ).fetchall()
        return [self._event_outbox(row) for row in rows]

    def claim_event_outbox(self, limit: int = 100, lock_seconds: int = 60) -> list[EventOutbox]:
        if lock_seconds < 1:
            raise ValueError("event_outbox_lock_seconds_invalid")
        now = _now()
        expires = now + timedelta(seconds=lock_seconds)
        with self.transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM event_outbox
                WHERE (status IN ('PENDING', 'FAILED') AND available_at <= %s)
                   OR (status = 'PROCESSING' AND lock_expires_at <= %s)
                ORDER BY created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT %s
                """,
                (now, now, max(1, min(limit, 1000))),
            ).fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE event_outbox SET status = 'PROCESSING', locked_at = %s, lock_expires_at = %s, updated_at = %s WHERE id = %s",
                    (now, expires, now, row["id"]),
                )
            claimed = connection.execute(
                "SELECT * FROM event_outbox WHERE id = ANY(%s) ORDER BY created_at ASC",
                ([row["id"] for row in rows],),
            ).fetchall() if rows else []
        return [self._event_outbox(row) for row in claimed]

    def mark_event_outbox_delivered(self, outbox_id: UUID) -> EventOutbox:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM event_outbox WHERE id = %s FOR UPDATE", (outbox_id,)).fetchone()
            if not row:
                raise KeyError("event_outbox_not_found")
            if row["status"] == "DELIVERED":
                return self._event_outbox(row)
            if row["status"] not in {"PENDING", "PROCESSING", "FAILED"}:
                raise ValueError("event_outbox_invalid_status")
            timestamp = _now()
            connection.execute(
                "UPDATE event_outbox SET status = 'DELIVERED', delivered_at = COALESCE(delivered_at, %s), locked_at = NULL, lock_expires_at = NULL, updated_at = %s WHERE id = %s",
                (timestamp, timestamp, outbox_id),
            )
            updated = connection.execute("SELECT * FROM event_outbox WHERE id = %s", (outbox_id,)).fetchone()
        return self._event_outbox(updated)

    def mark_event_outbox_failed(self, outbox_id: UUID, error: str, retry_at: datetime | None = None) -> EventOutbox:
        if not error.strip():
            raise ValueError("event_outbox_error_required")
        available_at = retry_at or _now()
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM event_outbox WHERE id = %s FOR UPDATE", (outbox_id,)).fetchone()
            if not row:
                raise KeyError("event_outbox_not_found")
            if row["status"] == "DELIVERED":
                raise ValueError("event_outbox_already_delivered")
            connection.execute(
                "UPDATE event_outbox SET status = 'FAILED', attempts = attempts + 1, available_at = %s, locked_at = NULL, lock_expires_at = NULL, last_error = %s, updated_at = %s WHERE id = %s",
                (available_at, error[:2000], _now(), outbox_id),
            )
            updated = connection.execute("SELECT * FROM event_outbox WHERE id = %s", (outbox_id,)).fetchone()
        return self._event_outbox(updated)

    def get_idempotent_response(self, key: str, operation: str, request_hash: str | None = None) -> Any | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT operation, response, request_hash FROM idempotency_records WHERE key = %s AND organization_id = %s",
                (self._scoped_idempotency_key(key), self.organization_id),
            ).fetchone()
        if not row:
            return None
        if row["operation"] != operation:
            raise ValueError("idempotency_key_reused_for_different_operation")
        if request_hash and row.get("request_hash") and row["request_hash"] != request_hash:
            raise ValueError("idempotency_key_reused_for_different_request")
        return row["response"]

    def save_idempotent_response(self, key: str, operation: str, response: Any, request_hash: str | None = None) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO idempotency_records (key, organization_id, operation, response, request_hash, created_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (key) DO NOTHING",
                (self._scoped_idempotency_key(key), self.organization_id, operation, self._jsonb(response), request_hash, _now()),
            )

    def get_git_repository(self, project_id: UUID) -> GitRepository:
        with self.transaction() as connection:
            row = connection.execute("SELECT * FROM git_repositories WHERE project_id = %s", (project_id,)).fetchone()
        if not row:
            raise KeyError("git_repository_not_found")
        return GitRepository(project_id=project_id, provider=row["provider"], remote_url=row["remote_url"], local_path=row["local_path"], head_commit=None)

    def list_git_index(self, project_id: UUID, commit_sha: str | None = None) -> list[GitFileIndex]:
        query = "SELECT * FROM git_file_indexes WHERE project_id = %s"
        params: list[Any] = [project_id]
        if commit_sha:
            query += " AND commit_sha = %s"
            params.append(commit_sha)
        query += " ORDER BY path"
        with self.transaction() as connection:
            rows = connection.execute(query, params).fetchall()
        return [GitFileIndex.model_validate(row) for row in rows]
