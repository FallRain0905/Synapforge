from __future__ import annotations

import hashlib
import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
import unittest
from datetime import UTC, datetime
from uuid import uuid4

from app.migrations import apply_migrations, migration_files
from app.object_store import S3ObjectStore


MINIO_READY = bool(os.getenv("STAGE2_MINIO_ENDPOINT") and importlib.util.find_spec("boto3"))
POSTGRES_READY = bool(
    os.getenv("STAGE2_POSTGRES_DSN")
    and importlib.util.find_spec("psycopg")
    and importlib.util.find_spec("psycopg_pool")
)


@unittest.skipUnless(MINIO_READY, "set STAGE2_MINIO_ENDPOINT and install requirements-prod.txt")
class MinIOIntegrationTests(unittest.TestCase):
    def test_real_minio_object_and_cross_instance_multipart(self) -> None:
        import boto3

        endpoint = os.environ["STAGE2_MINIO_ENDPOINT"]
        bucket = os.getenv("STAGE2_MINIO_BUCKET", "math-agent-platform-test")
        client = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=os.getenv("STAGE2_MINIO_ACCESS_KEY", "platform"),
            aws_secret_access_key=os.getenv("STAGE2_MINIO_SECRET_KEY", "platform-dev-only"),
            region_name=os.getenv("STAGE2_MINIO_REGION", "us-east-1"),
        )
        try:
            client.head_bucket(Bucket=bucket)
        except Exception:
            client.create_bucket(Bucket=bucket)
        objects = S3ObjectStore(bucket, client=client)
        key = f"integration/{uuid4().hex}/result.json"
        content = b"integration-result"
        stored = objects.put_bytes(key, content, "application/json")
        self.assertEqual(objects.head(key).content_hash, hashlib.sha256(content).hexdigest())

        upload_key = f"integration/{uuid4().hex}/large.bin"
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


@unittest.skipUnless(POSTGRES_READY, "set STAGE2_POSTGRES_DSN and install requirements-prod.txt")
class PostgreSQLMigrationIntegrationTests(unittest.TestCase):
    def test_migrations_apply_and_enable_project_rls(self) -> None:
        dsn = os.environ["STAGE2_POSTGRES_DSN"]
        expected_versions = {path.name for path in migration_files()}
        first_apply = apply_migrations(dsn)
        second_apply = apply_migrations(dsn)
        self.assertTrue(set(first_apply).issubset(expected_versions))
        self.assertEqual(second_apply, [])
        import psycopg

        with psycopg.connect(dsn) as connection:
            recorded_versions = {
                row[0]
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
            self.assertEqual(recorded_versions, expected_versions)
            row = connection.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'projects'").fetchone()
            self.assertTrue(row and row[0])
            outbox_row = connection.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'event_outbox'").fetchone()
            self.assertTrue(outbox_row and outbox_row[0])
            gateway_result_row = connection.execute("SELECT relrowsecurity FROM pg_class WHERE relname = 'gateway_command_results'").fetchone()
            self.assertTrue(gateway_result_row and gateway_result_row[0])

            for table_name in ("devices", "device_pairings", "device_project_grants", "agent_connections", "device_token_rotations"):
                rls_row = connection.execute(
                    "SELECT relrowsecurity FROM pg_class WHERE relname = %s",
                    (table_name,),
                ).fetchone()
                self.assertTrue(rls_row and rls_row[0], table_name)

            columns = {
                (row[0], row[1])
                for row in connection.execute(
                    """
                    SELECT table_name, column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND ((table_name = 'devices' AND column_name IN ('token_version', 'token_rotated_at'))
                       OR (table_name = 'device_pairings' AND column_name = 'challenge_hash'))
                    """
                ).fetchall()
            }
            self.assertEqual(
                columns,
                {
                    ("devices", "token_version"),
                    ("devices", "token_rotated_at"),
                    ("device_pairings", "challenge_hash"),
                },
            )

            rotation_columns = {
                row[0]
                for row in connection.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'device_token_rotations'
                    """
                ).fetchall()
            }
            self.assertEqual(
                rotation_columns,
                {
                    "id",
                    "device_id",
                    "organization_id",
                    "rotated_by",
                    "reason",
                    "token_version",
                    "created_at",
                },
            )

    def test_postgres_device_rotation_is_atomic_and_serialized(self) -> None:
        """Exercise the P3-20 repository slice when a real PostgreSQL is available."""
        import psycopg

        from app.contracts import DevicePairingCreate, DeviceRegisterRequest
        from app.device_identity import parse_public_key, sign_registration
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from app.postgres_repository import PostgresRepository

        dsn = os.environ["STAGE2_POSTGRES_DSN"]
        organization_id = uuid4()
        team_id = uuid4()
        project_id = uuid4()
        member_id = f"integration-member-{uuid4().hex}"
        agent_id = f"integration-agent-{uuid4().hex}"
        device_id = f"integration-device-{uuid4().hex}"
        now = datetime.now(UTC)

        with psycopg.connect(dsn) as connection:
            connection.execute(
                "INSERT INTO organizations (id, name, slug, created_at) VALUES (%s, %s, %s, %s)",
                (organization_id, "Integration Organization", f"integration-{organization_id.hex}", now),
            )
            connection.execute(
                "INSERT INTO teams (id, organization_id, name, created_at) VALUES (%s, %s, %s, %s)",
                (team_id, organization_id, "Integration Team", now),
            )
            connection.execute(
                "INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (%s, %s, %s, %s, %s, 'active', %s)",
                (member_id, organization_id, team_id, f"{member_id}@example.test", "Integration Member", now),
            )
            connection.execute(
                "INSERT INTO memberships (member_id, team_id, role, created_at) VALUES (%s, %s, 'owner', %s)",
                (member_id, team_id, now),
            )
            connection.execute(
                "INSERT INTO projects (id, organization_id, team_id, created_by, name, competition_pack, description, stage, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, 'CUMCM', %s, 'analysis', %s, %s)",
                (project_id, organization_id, team_id, member_id, "Integration Project", "P3-20", now, now),
            )
            connection.execute(
                "INSERT INTO project_memberships (project_id, member_id, role, created_at) VALUES (%s, %s, 'owner', %s)",
                (project_id, member_id, now),
            )
            connection.execute(
                "INSERT INTO agents (agent_id, display_name, owner_member_id, model_provider, model_name, supported_tools, supported_languages, max_concurrency, network_policy, status, last_seen) VALUES (%s, %s, %s, 'local', 'integration', '[]', '[\"python\"]', 1, 'deny-by-default', 'online', %s)",
                (agent_id, "Integration Agent", member_id, now),
            )
            connection.commit()

        repository = PostgresRepository(dsn, organization_id=organization_id)
        try:
            pairing = repository.create_device_pairing(
                DevicePairingCreate(organization_id=organization_id, expires_in_seconds=900),
                member_id,
            )
            private_key = Ed25519PrivateKey.generate()
            public_key = private_key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ).decode("ascii")
            _, public_key_fingerprint = parse_public_key(public_key)
            signature = sign_registration(
                private_key,
                pairing.id,
                pairing.challenge,
                agent_id,
                device_id,
                public_key_fingerprint,
            )
            credential = repository.register_device(
                DeviceRegisterRequest(
                    pairing_code=pairing.pairing_code,
                    pairing_id=pairing.id,
                    challenge=pairing.challenge,
                    challenge_signature=signature,
                    agent_id=agent_id,
                    device_id=device_id,
                    device_name="Integration Device",
                    public_key=public_key,
                    platform="windows",
                    agent_version="integration",
                    capabilities=["task.claim"],
                )
            )
            connection = repository.open_agent_connection(device_id, "integration-session", f"connection-{uuid4().hex}")
            rotated = repository.rotate_device_token(device_id, member_id, "integration rotation")
            self.assertEqual(rotated.device.token_version, 2)
            self.assertNotEqual(rotated.device_token, credential.device_token)
            with self.assertRaises(PermissionError):
                repository.resolve_device_token(credential.device_token)
            with psycopg.connect(dsn) as check:
                row = check.execute(
                    "SELECT token_version, token_rotated_at, status FROM devices WHERE device_id = %s",
                    (device_id,),
                ).fetchone()
                self.assertEqual(row[0], 2)
                self.assertIsNotNone(row[1])
                self.assertEqual(row[2], "active")
                self.assertEqual(
                    check.execute(
                        "SELECT status FROM agent_connections WHERE connection_id = %s",
                        (connection.connection_id,),
                    ).fetchone()[0],
                    "REVOKED",
                )
                self.assertEqual(
                    check.execute(
                        "SELECT token_version FROM device_token_rotations WHERE device_id = %s",
                        (device_id,),
                    ).fetchone()[0],
                    2,
                )

            def rotate_once() -> int:
                worker = PostgresRepository(dsn, organization_id=organization_id)
                try:
                    return worker.rotate_device_token(device_id, member_id, "concurrent rotation").device.token_version
                finally:
                    worker.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                versions = sorted(executor.map(lambda _: rotate_once(), range(2)))
            self.assertEqual(versions, [3, 4])
        finally:
            repository.close()
            with psycopg.connect(dsn) as cleanup:
                # device_pairings references devices, so delete pairings first.
                cleanup.execute("DELETE FROM device_token_rotations WHERE device_id = %s", (device_id,))
                cleanup.execute("DELETE FROM agent_connections WHERE device_id = %s", (device_id,))
                cleanup.execute("DELETE FROM device_pairings WHERE organization_id = %s", (organization_id,))
                cleanup.execute("DELETE FROM devices WHERE device_id = %s", (device_id,))
                cleanup.execute("DELETE FROM agent_project_grants WHERE project_id = %s", (project_id,))
                cleanup.execute("DELETE FROM project_memberships WHERE project_id = %s", (project_id,))
                cleanup.execute("DELETE FROM projects WHERE id = %s", (project_id,))
                cleanup.execute("DELETE FROM memberships WHERE member_id = %s", (member_id,))
                cleanup.execute("DELETE FROM agents WHERE agent_id = %s", (agent_id,))
                cleanup.execute("DELETE FROM human_members WHERE id = %s", (member_id,))
                cleanup.execute("DELETE FROM teams WHERE id = %s", (team_id,))
                cleanup.execute("DELETE FROM organizations WHERE id = %s", (organization_id,))
                cleanup.commit()


if __name__ == "__main__":
    unittest.main()
