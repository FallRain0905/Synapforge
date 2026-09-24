from __future__ import annotations

import hashlib
import importlib.util
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.contracts import (
    HandoffCreate,
    HandoffType,
    HandoffStatus,
    ReviewerKind,
    ReviewCreate,
    RiskDecisionRequest,
    TaskCreate,
)
from app.migrations import apply_migrations, migration_files
from app.object_store import S3ObjectStore
from app.postgres_repository import PostgresRepository


POSTGRES_READY = bool(
    os.getenv("STAGE4_POSTGRES_DSN")
    and importlib.util.find_spec("psycopg")
    and importlib.util.find_spec("psycopg_pool")
)
MINIO_READY = bool(os.getenv("STAGE4_MINIO_ENDPOINT") and importlib.util.find_spec("boto3"))


@unittest.skipUnless(POSTGRES_READY, "set STAGE4_POSTGRES_DSN and install requirements-prod.txt")
class PostgreSQLStage4IntegrationTests(unittest.TestCase):
    dsn = os.getenv("STAGE4_POSTGRES_DSN", "")

    @staticmethod
    def _scoped_connection(dsn: str, organization_id: UUID):
        import psycopg

        connection = psycopg.connect(dsn)
        connection.execute("SELECT set_config('app.organization_id', %s, false)", (str(organization_id),))
        return connection

    @classmethod
    def _create_tenant(cls, suffix: str) -> tuple[UUID, UUID, str, UUID]:
        organization_id = uuid4()
        team_id = uuid4()
        member_id = f"stage4-member-{suffix}-{uuid4().hex}"
        project_id = uuid4()
        now = datetime.now(UTC)
        with cls._scoped_connection(cls.dsn, organization_id) as connection:
            connection.execute(
                "INSERT INTO organizations (id, name, slug, created_at) VALUES (%s, %s, %s, %s)",
                (organization_id, f"Stage 4 {suffix}", f"stage4-{suffix}-{organization_id.hex}", now),
            )
            connection.execute(
                "INSERT INTO teams (id, organization_id, name, created_at) VALUES (%s, %s, %s, %s)",
                (team_id, organization_id, f"Stage 4 Team {suffix}", now),
            )
            connection.execute(
                """
                INSERT INTO human_members
                    (id, organization_id, team_id, email, display_name, status, created_at)
                VALUES (%s, %s, %s, %s, %s, 'active', %s)
                """,
                (member_id, organization_id, team_id, f"{member_id}@example.test", "Stage 4 Member", now),
            )
            connection.execute(
                "INSERT INTO memberships (member_id, team_id, role, created_at) VALUES (%s, %s, 'owner', %s)",
                (member_id, team_id, now),
            )
            connection.execute(
                """
                INSERT INTO projects
                    (id, organization_id, team_id, created_by, name, competition_pack,
                     description, stage, progress, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, 'CUMCM', %s, 'analysis', 0, %s, %s)
                """,
                (project_id, organization_id, team_id, member_id, f"Stage 4 Project {suffix}", "integration", now, now),
            )
            connection.execute(
                "INSERT INTO project_memberships (project_id, member_id, role, created_at) VALUES (%s, %s, 'owner', %s)",
                (project_id, member_id, now),
            )
        return organization_id, project_id, member_id, team_id

    @classmethod
    def _cleanup_tenant(cls, organization_id: UUID) -> None:
        # Tenant rows have no ON DELETE CASCADE across the board, so delete
        # children before parents, ordered by foreign-key dependency.  The
        # connection is unscoped (superuser or RLS-exempt) by design: cleanup
        # must reach rows regardless of the tenant filter.
        import psycopg

        with psycopg.connect(cls.dsn) as connection:
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
                "DELETE FROM artifact_multipart_uploads WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM runs WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM task_leases WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM tasks WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM idempotency_records WHERE organization_id = %s",
                "DELETE FROM git_file_indexes WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM git_repositories WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM gateway_command_results WHERE connection_id IN (SELECT connection_id FROM agent_connections WHERE device_id IN (SELECT device_id FROM devices WHERE organization_id = %s))",
                "DELETE FROM device_token_rotations WHERE organization_id = %s",
                "DELETE FROM agent_connections WHERE device_id IN (SELECT device_id FROM devices WHERE organization_id = %s)",
                "DELETE FROM device_project_grants WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM device_pairings WHERE organization_id = %s",
                "DELETE FROM devices WHERE organization_id = %s",
                "DELETE FROM agent_project_grants WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM agents WHERE owner_member_id IN (SELECT id FROM human_members WHERE organization_id = %s)",
                "DELETE FROM sessions WHERE member_id IN (SELECT id FROM human_members WHERE organization_id = %s)",
                "DELETE FROM invitations WHERE organization_id = %s",
                "DELETE FROM project_memberships WHERE project_id IN (SELECT id FROM projects WHERE organization_id = %s)",
                "DELETE FROM projects WHERE organization_id = %s",
                "DELETE FROM memberships WHERE member_id IN (SELECT id FROM human_members WHERE organization_id = %s)",
                "DELETE FROM human_members WHERE organization_id = %s",
                "DELETE FROM teams WHERE organization_id = %s",
                "DELETE FROM organizations WHERE id = %s",
            ):
                connection.execute(statement, (organization_id,))

    def test_migrations_are_current_and_all_tenant_tables_force_rls(self) -> None:
        first_apply = apply_migrations(self.dsn)
        self.assertEqual(apply_migrations(self.dsn), [])
        self.assertTrue(set(first_apply).issubset({path.name for path in migration_files()}))

        import psycopg

        expected_tables = {
            "organizations", "teams", "human_members", "memberships", "projects",
            "project_memberships", "agents", "agent_project_grants", "git_repositories",
            "git_file_indexes", "sessions", "invitations", "tasks", "handoffs", "runs",
            "artifacts", "artifact_multipart_uploads", "reviews", "gates", "evidence",
            "events", "event_outbox", "task_leases", "idempotency_records", "devices",
            "device_pairings", "device_project_grants", "agent_connections",
            "gateway_command_results", "device_token_rotations", "handoff_receipts", "risks",
        }
        with psycopg.connect(self.dsn) as connection:
            rows = connection.execute(
                """
                SELECT c.relname, c.relforcerowsecurity
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema() AND c.relname = ANY(%s)
                """,
                (list(expected_tables),),
            ).fetchall()
            self.assertEqual({row[0] for row in rows}, expected_tables)
            self.assertTrue(all(row[1] for row in rows))
            columns = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'idempotency_records'
                    """
                ).fetchall()
            }
            self.assertIn("organization_id", columns)

    def test_rls_hides_other_tenants_from_table_owner_connection(self) -> None:
        # A superuser bypasses RLS entirely (even with FORCE ROW LEVEL
        # SECURITY), and typical development DSNs are superusers.  Prove the
        # policy on a dedicated database whose owner is a non-superuser role.
        import psycopg

        marker = uuid4().hex[:12]
        database = f"stage4_rls_{marker}"
        role = f"stage4_owner_{marker}"
        role_password = f"stage4-owner-{marker}".replace("'", "")
        admin = psycopg.connect(self.dsn, autocommit=True)
        try:
            # PostgreSQL rejects bind parameters in utility statements such as
            # CREATE ROLE, so the literal is interpolated; it is generated
            # locally from a uuid4 hex and contains no quoting characters.
            # The deployment enforces scram-sha-256 for TCP connections, so the
            # disposable owner role needs its own password.
            admin.execute(f"""CREATE ROLE "{role}" LOGIN PASSWORD '{role_password}'""")
            admin.execute(f'CREATE DATABASE "{database}" OWNER "{role}"')
            dsn = self._role_dsn(role, database, role_password)
            apply_migrations(dsn)
            owner = psycopg.connect(dsn)
            try:
                organization_a, project_a = self._insert_rls_fixture(owner, "rls-a")
                organization_b, project_b = self._insert_rls_fixture(owner, "rls-b")
            finally:
                owner.close()

            def project_count(connection, organization_id: str, project_id: UUID) -> int:
                connection.execute("SELECT set_config('app.organization_id', %s, false)", (organization_id,))
                return connection.execute("SELECT count(*) FROM projects WHERE id = %s", (project_id,)).fetchone()[0]

            checked = psycopg.connect(dsn)
            try:
                self.assertEqual(project_count(checked, str(organization_a), project_a), 1)
                self.assertEqual(project_count(checked, str(organization_a), project_b), 0)
                self.assertEqual(project_count(checked, str(organization_b), project_a), 0)
                self.assertEqual(project_count(checked, str(organization_b), project_b), 1)
            finally:
                checked.close()
        finally:
            # Teardown must not mask an earlier failure: only drop what exists,
            # otherwise a failed setup reports "database does not exist" instead
            # of the real cause.
            if admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone():
                admin.execute(f'DROP DATABASE "{database}"')
            if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone():
                admin.execute(f'DROP ROLE "{role}"')
            admin.close()

    def _role_dsn(self, role: str, database: str, password: str | None = None) -> str:
        from urllib.parse import urlsplit, urlunsplit

        parts = urlsplit(self.dsn)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or 5432
        credential = f":{password}" if password else (f":{parts.password}" if parts.password else "")
        return urlunsplit(("postgresql", f"{role}{credential}@{host}:{port}", f"/{database}", "", ""))

    def _insert_rls_fixture(self, connection, suffix: str) -> tuple[UUID, UUID]:
        now = datetime.now(UTC)
        organization_id = uuid4()
        team_id = uuid4()
        member_id = f"stage4-rls-{suffix}-{uuid4().hex}"
        project_id = uuid4()
        connection.execute("BEGIN")
        connection.execute("SELECT set_config('app.organization_id', %s, false)", (str(organization_id),))
        connection.execute(
            "INSERT INTO organizations (id, name, slug, created_at) VALUES (%s, %s, %s, %s)",
            (organization_id, f"Stage 4 RLS {suffix}", f"stage4-rls-{suffix}-{organization_id.hex}", now),
        )
        connection.execute(
            "INSERT INTO teams (id, organization_id, name, created_at) VALUES (%s, %s, %s, %s)",
            (team_id, organization_id, "Stage 4 RLS Team", now),
        )
        connection.execute(
            "INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (%s, %s, %s, %s, %s, 'active', %s)",
            (member_id, organization_id, team_id, f"{member_id}@example.test", "Stage 4 RLS Member", now),
        )
        connection.execute(
            "INSERT INTO memberships (member_id, team_id, role, created_at) VALUES (%s, %s, 'owner', %s)",
            (member_id, team_id, now),
        )
        connection.execute(
            "INSERT INTO projects (id, organization_id, team_id, created_by, name, competition_pack, description, stage, progress, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, 'CUMCM', %s, 'analysis', 0, %s, %s)",
            (project_id, organization_id, team_id, member_id, "Stage 4 RLS Project", "rls", now, now),
        )
        connection.execute(
            "INSERT INTO project_memberships (project_id, member_id, role, created_at) VALUES (%s, %s, 'owner', %s)",
            (project_id, member_id, now),
        )
        connection.commit()
        return organization_id, project_id

    def test_fanout_risk_gate_propagation_and_outbox_are_multi_instance_safe(self) -> None:
        organization_id, project_id, member_id, _ = self._create_tenant("workflow")
        repositories: list[PostgresRepository] = []
        try:
            primary = PostgresRepository(self.dsn, organization_id=organization_id, min_size=1, max_size=2)
            repositories.append(primary)
            relay_task = primary.create_task(project_id, TaskCreate(title="Fanout relay task"))
            fanout = primary.create_handoff(
                project_id,
                HandoffCreate(
                    task_id=relay_task.id,
                    receiver=[{"type": "agent", "id": "receiver-a"}, {"type": "agent", "id": "receiver-b"}],
                    handoff_type=HandoffType.FANOUT,
                    status=HandoffStatus.PASS_WITH_ASSUMPTIONS,
                    objective="parallel acceptance",
                    requires_human_approval=False,
                ),
                sender_agent_id="sender-agent",
            )

            def accept(receiver: str):
                repository = PostgresRepository(self.dsn, organization_id=organization_id, min_size=1, max_size=2)
                repositories.append(repository)
                return repository.accept_handoff(fanout.id, receiver)

            with ThreadPoolExecutor(max_workers=2) as executor:
                accepted = list(executor.map(accept, ["receiver-a", "receiver-b"]))
            # Whichever receipt commits first still sees the other as PENDING;
            # each result must at least record its own receiver as ACCEPTED.
            for item, receiver in zip(accepted, ["receiver-a", "receiver-b"]):
                own = next(r for r in item.receipts if str(r.receiver_id) == receiver)
                self.assertEqual(str(own.status), "ACCEPTED")
            persisted_handoff = primary.get_handoff(fanout.id)
            self.assertEqual(len(persisted_handoff.receipts), 2)
            self.assertEqual(str(persisted_handoff.receipt_status), "ACCEPTED")
            self.assertTrue(all(str(item.status) == "ACCEPTED" for item in persisted_handoff.receipts))

            root_task = primary.create_task(project_id, TaskCreate(title="Root review task"))
            downstream_task = primary.create_task(
                project_id,
                TaskCreate(title="Downstream review task", dependency_task_ids=[root_task.id]),
            )
            risk_task = primary.create_task(project_id, TaskCreate(title="Risk task"))
            with self._scoped_connection(self.dsn, organization_id) as connection:
                now = datetime.now(UTC)
                connection.execute(
                    "UPDATE tasks SET status = 'WAITING_REVIEW', updated_at = %s WHERE id IN (%s, %s)",
                    (now, root_task.id, downstream_task.id),
                )
            primary.create_review(
                project_id,
                ReviewCreate(
                    target_type="task",
                    target_id=root_task.id,
                    verdict="APPROVED",
                    summary="root approved",
                    reviewer=member_id,
                    reviewer_kind=ReviewerKind.MEMBER,
                ),
            )
            primary.create_review(
                project_id,
                ReviewCreate(
                    target_type="task",
                    target_id=downstream_task.id,
                    verdict="APPROVED",
                    summary="downstream approved",
                    reviewer=member_id,
                    reviewer_kind=ReviewerKind.MEMBER,
                ),
            )
            risk_review = primary.create_review(
                project_id,
                ReviewCreate(
                    target_type="task",
                    target_id=risk_task.id,
                    verdict="NEEDS_REVISION",
                    summary="independent review found a major issue",
                    findings=[{"code": "RISK-1", "severity": "major", "message": "needs correction"}],
                ),
            )
            risk = next(item for item in primary.list_risks(project_id) if item.review_id == risk_review.id)

            def assign_risk(_: int):
                repository = PostgresRepository(self.dsn, organization_id=organization_id, min_size=1, max_size=2)
                repositories.append(repository)
                return repository.update_risk(
                    project_id,
                    risk.id,
                    RiskDecisionRequest(action="ASSIGN", owner="review-owner", idempotency_key="stage4-risk-assignment-1"),
                    actor=member_id,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                risk_results = list(executor.map(assign_risk, [1, 2]))
            self.assertEqual({item.id for item in risk_results}, {risk.id})
            self.assertEqual(risk_results[0].owner, "review-owner")
            with self._scoped_connection(self.dsn, organization_id) as connection:
                risk_events = connection.execute(
                    "SELECT count(*) FROM events WHERE project_id = %s AND event_type = 'risk.updated'",
                    (project_id,),
                ).fetchone()[0]
            self.assertEqual(risk_events, 1)

            with self._scoped_connection(self.dsn, organization_id) as connection:
                connection.execute(
                    "UPDATE tasks SET description = 'changed after approval', updated_at = %s WHERE id = %s",
                    (datetime.now(UTC), root_task.id),
                )
            gates = {gate.target_id: gate for gate in primary.list_gates(project_id)}
            self.assertEqual(gates[root_task.id].status, "INVALIDATED")
            self.assertEqual(gates[downstream_task.id].status, "INVALIDATED")
            self.assertEqual(primary.get_task(root_task.id).status, "NEEDS_REVISION")
            self.assertEqual(primary.get_task(downstream_task.id).status, "NEEDS_REVISION")

            pending_before = {item.id for item in primary.list_pending_event_outbox(limit=1000)}

            def claim(_: int):
                repository = PostgresRepository(self.dsn, organization_id=organization_id, min_size=1, max_size=2)
                repositories.append(repository)
                return repository.claim_event_outbox(limit=1000)

            with ThreadPoolExecutor(max_workers=2) as executor:
                claimed = [item for batch in executor.map(claim, [1, 2]) for item in batch]
            self.assertEqual({item.id for item in claimed}, pending_before)
            self.assertEqual(len(claimed), len({item.id for item in claimed}))
        finally:
            for repository in repositories:
                repository.close()
            self._cleanup_tenant(organization_id)


@unittest.skipUnless(MINIO_READY, "set STAGE4_MINIO_ENDPOINT and install requirements-prod.txt")
class MinIOStage4IntegrationTests(unittest.TestCase):
    def test_cross_instance_multipart_and_hash_integrity(self) -> None:
        import boto3

        endpoint = os.environ["STAGE4_MINIO_ENDPOINT"]
        bucket = os.getenv("STAGE4_MINIO_BUCKET", "math-agent-platform-stage4-test")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.getenv("STAGE4_MINIO_ACCESS_KEY", "platform"),
            aws_secret_access_key=os.getenv("STAGE4_MINIO_SECRET_KEY", "platform-dev-only"),
            region_name=os.getenv("STAGE4_MINIO_REGION", "us-east-1"),
        )
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            client.create_bucket(Bucket=bucket)
        objects = S3ObjectStore(bucket, client=client)
        key = f"stage4/{uuid4().hex}/result.json"
        content = b"stage4-result"
        objects.put_bytes(key, content, "application/json")
        self.assertEqual(objects.head(key).content_hash, hashlib.sha256(content).hexdigest())

        upload_key = f"stage4/{uuid4().hex}/large.bin"
        upload_id = objects.initiate_multipart(upload_key, "application/octet-stream")
        restarted = S3ObjectStore(bucket, client=client)
        # Real S3 semantics require every part except the last to be at least
        # 5 MiB; the fake backend in unit tests accepted tiny parts.
        part_one = b"a" * (5 * 1024 * 1024)
        restarted.upload_part(upload_id, 2, b"world", upload_key)
        restarted.upload_part(upload_id, 1, part_one, upload_key)
        completed = restarted.complete_multipart(upload_id, upload_key, "application/octet-stream")
        self.assertEqual(completed.content_hash, hashlib.sha256(part_one + b"world").hexdigest())
        client.delete_object(Bucket=bucket, Key=key)
        client.delete_object(Bucket=bucket, Key=upload_key)


if __name__ == "__main__":
    unittest.main()
