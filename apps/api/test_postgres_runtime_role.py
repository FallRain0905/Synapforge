"""P4-04-RUN-PROD acceptance: production-shaped runtime role and pool limits.

These tests exercise the deployment shape that the development验收 (which ran
as the schema owner) could not: a dedicated non-owner role with password
authentication that can neither run DDL nor bypass row-level security.

Set ``STAGE4_RUNTIME_DSN`` to the runtime role DSN, for example::

    postgresql://app_runtime:app-runtime-dev-only@127.0.0.1:54329/postgres

The suite is skipped when the variable is absent so the default SQLite
development runtime stays green.
"""

from __future__ import annotations

import importlib.util
import os
import threading
import time
import unittest
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.contracts import HandoffCreate, HandoffStatus, HandoffType, TaskCreate
from app.postgres_repository import PostgresRepository, PostgresRepositoryError


RUNTIME_DSN = os.getenv("STAGE4_RUNTIME_DSN", "")
RUNTIME_READY = bool(
    RUNTIME_DSN
    and importlib.util.find_spec("psycopg")
    and importlib.util.find_spec("psycopg_pool")
)


@unittest.skipUnless(RUNTIME_READY, "set STAGE4_RUNTIME_DSN to the non-owner runtime role DSN")
class RuntimeRoleAcceptanceTests(unittest.TestCase):
    """Least privilege, RLS isolation and pool failure behaviour."""

    @staticmethod
    def _connect():
        import psycopg

        return psycopg.connect(RUNTIME_DSN, autocommit=True)

    @classmethod
    def _create_tenant(cls, suffix: str) -> tuple[UUID, UUID, str]:
        """Insert one tenant through the runtime role, scoped by RLS."""

        organization_id = uuid4()
        team_id = uuid4()
        member_id = f"runtime-member-{suffix}-{uuid4().hex}"
        project_id = uuid4()
        now = datetime.now(UTC)
        with cls._connect() as connection:
            connection.execute("SELECT set_config('app.organization_id', %s, false)", (str(organization_id),))
            connection.execute(
                "INSERT INTO organizations (id, name, slug, created_at) VALUES (%s, %s, %s, %s)",
                (organization_id, f"Runtime {suffix}", f"runtime-{suffix}-{organization_id.hex}", now),
            )
            connection.execute(
                "INSERT INTO teams (id, organization_id, name, created_at) VALUES (%s, %s, %s, %s)",
                (team_id, organization_id, f"Runtime Team {suffix}", now),
            )
            connection.execute(
                "INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (%s, %s, %s, %s, %s, 'active', %s)",
                (member_id, organization_id, team_id, f"{member_id}@example.test", "Runtime Member", now),
            )
            connection.execute(
                "INSERT INTO memberships (member_id, team_id, role, created_at) VALUES (%s, %s, 'owner', %s)",
                (member_id, team_id, now),
            )
            connection.execute(
                "INSERT INTO projects (id, organization_id, team_id, created_by, name, competition_pack, description, stage, progress, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, 'CUMCM', %s, 'analysis', 0, %s, %s)",
                (project_id, organization_id, team_id, member_id, f"Runtime Project {suffix}", "runtime", now, now),
            )
            connection.execute(
                "INSERT INTO project_memberships (project_id, member_id, role, created_at) VALUES (%s, %s, 'owner', %s)",
                (project_id, member_id, now),
            )
        return organization_id, project_id, member_id

    @classmethod
    def _cleanup_tenant(cls, organization_id: UUID) -> None:
        with cls._connect() as connection:
            connection.execute("SELECT set_config('app.organization_id', %s, false)", (str(organization_id),))
            for statement in (
                "DELETE FROM event_outbox WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM events WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM risks WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM evidence WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM gates WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM reviews WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM handoff_receipts WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM handoffs WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM artifacts WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM runs WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM tasks WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM project_memberships WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM projects WHERE organization_id = %s",
                "DELETE FROM memberships WHERE member_id IN (SELECT id FROM human_members WHERE organization_id = %s)",
                "DELETE FROM human_members WHERE organization_id = %s",
                "DELETE FROM teams WHERE organization_id = %s",
                "DELETE FROM organizations WHERE id = %s",
            ):
                connection.execute(statement, (organization_id,))

    def test_runtime_role_is_least_privilege_and_cannot_bypass_rls(self) -> None:
        import psycopg

        with self._connect() as connection:
            current_user = connection.execute("SELECT current_user").fetchone()[0]
            self.assertNotEqual(current_user, "platform")
            attributes = connection.execute(
                "SELECT rolsuper, rolbypassrls, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles WHERE rolname = current_user"
            ).fetchone()
            self.assertEqual(attributes, (False, False, False, False, True))

            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("CREATE TABLE runtime_ddl_probe (id int)")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("SELECT * FROM pg_authid")
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                connection.execute("CREATE ROLE runtime_role_probe")

    def test_runtime_role_sees_no_rows_without_organization_scope(self) -> None:
        organization_id, project_id, _ = self._create_tenant("unscoped")
        try:
            with self._connect() as connection:
                self.assertEqual(connection.execute("SELECT count(*) FROM projects").fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT count(*) FROM organizations").fetchone()[0], 0)
                connection.execute("SELECT set_config('app.organization_id', %s, false)", (str(organization_id),))
                self.assertEqual(connection.execute("SELECT count(*) FROM projects WHERE id = %s", (project_id,)).fetchone()[0], 1)
        finally:
            self._cleanup_tenant(organization_id)

    def test_runtime_role_isolates_tenants_under_rls(self) -> None:
        organization_a, project_a, _ = self._create_tenant("a")
        organization_b, project_b, _ = self._create_tenant("b")
        try:
            with self._connect() as connection:
                connection.execute("SELECT set_config('app.organization_id', %s, false)", (str(organization_a),))
                self.assertEqual(connection.execute("SELECT count(*) FROM projects WHERE id = %s", (project_a,)).fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT count(*) FROM projects WHERE id = %s", (project_b,)).fetchone()[0], 0)
                connection.execute("SELECT set_config('app.organization_id', %s, false)", (str(organization_b),))
                self.assertEqual(connection.execute("SELECT count(*) FROM projects WHERE id = %s", (project_a,)).fetchone()[0], 0)
                self.assertEqual(connection.execute("SELECT count(*) FROM projects WHERE id = %s", (project_b,)).fetchone()[0], 1)
        finally:
            self._cleanup_tenant(organization_a)
            self._cleanup_tenant(organization_b)

    def test_runtime_role_completes_workflow_through_repository(self) -> None:
        organization_id, project_id, _ = self._create_tenant("workflow")
        repository = PostgresRepository(RUNTIME_DSN, organization_id=organization_id, min_size=1, max_size=3)
        try:
            task = repository.create_task(project_id, TaskCreate(title="Runtime task"))
            handoff = repository.create_handoff(
                project_id,
                HandoffCreate(
                    task_id=task.id,
                    receiver=[{"type": "agent", "id": "runtime-receiver"}],
                    handoff_type=HandoffType.FANOUT,
                    status=HandoffStatus.PASS,
                    objective="runtime acceptance",
                    requires_human_approval=False,
                ),
                sender_agent_id="runtime-sender",
            )
            accepted = repository.accept_handoff(handoff.id, "runtime-receiver")
            self.assertEqual(str(accepted.receipt_status), "ACCEPTED")
            pending = repository.list_pending_event_outbox(limit=100)
            self.assertTrue(pending)
            claimed = repository.claim_event_outbox(limit=100)
            self.assertTrue(claimed)
            stats = repository.pool_stats()
            self.assertEqual(stats["pool_max"], 3)
        finally:
            repository.close()
            self._cleanup_tenant(organization_id)

    def test_pool_exhaustion_returns_stable_timeout_code(self) -> None:
        organization_id, project_id, _ = self._create_tenant("pool")
        # One repository instance owns one pool: a second borrower must wait on
        # that same pool, so max_size=1 genuinely exhausts it.
        repository = PostgresRepository(RUNTIME_DSN, organization_id=organization_id, min_size=1, max_size=1, timeout=0.5)
        acquired = threading.Event()
        release = threading.Event()
        failure: list[Exception] = []

        def hold_connection() -> None:
            try:
                with repository.transaction() as connection:
                    acquired.set()
                    connection.execute("SELECT 1")
                    release.wait(timeout=20)
            except Exception as error:  # pragma: no cover - diagnostic path
                failure.append(error)

        worker = threading.Thread(target=hold_connection, daemon=True)
        worker.start()
        try:
            self.assertTrue(acquired.wait(timeout=10))
            started = time.monotonic()
            with self.assertRaises(PostgresRepositoryError) as caught:
                with repository.transaction() as connection:
                    connection.execute("SELECT 1")
            self.assertEqual(caught.exception.code, "postgres_pool_timeout")
            self.assertLess(time.monotonic() - started, 15)
            stats = repository.pool_stats()
            self.assertGreaterEqual(stats["requests_errors"], 1)
        finally:
            release.set()
            worker.join(timeout=10)
            repository.close()
            self._cleanup_tenant(organization_id)
        self.assertEqual(failure, [])

    def test_lost_backend_surfaces_as_unavailable_and_recovers(self) -> None:
        import psycopg

        organization_id, project_id, _ = self._create_tenant("recovery")
        repository = PostgresRepository(RUNTIME_DSN, organization_id=organization_id, min_size=1, max_size=1)
        try:
            with repository.transaction() as connection:
                backend_pid = connection.execute("SELECT pg_backend_pid()").fetchone()["pg_backend_pid"]
            with psycopg.connect(RUNTIME_DSN, autocommit=True) as killer:
                killer.execute("SELECT pg_terminate_backend(%s)", (backend_pid,))
            with self.assertRaises((PostgresRepositoryError, psycopg.Error)):
                with repository.transaction() as connection:
                    connection.execute("SELECT 1")
            # The pool must hand out a healthy connection again.
            with repository.transaction() as connection:
                self.assertEqual(connection.execute("SELECT 1 AS ok").fetchone()["ok"], 1)
        finally:
            repository.close()
            self._cleanup_tenant(organization_id)


if __name__ == "__main__":
    unittest.main()
