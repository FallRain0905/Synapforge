from __future__ import annotations

import asyncio
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from fastapi import UploadFile
from starlette.responses import Response

from app import main
from app.contracts import ArtifactCreate, HumanMemberCreate, OrganizationCreate, SessionCreate, TeamCreate
from app.store import Store


class HttpBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.old_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.old_store
        self.store.close()
        self.temp_dir.cleanup()

    def test_file_upload_retries_are_idempotent_and_request_bound(self) -> None:
        artifact = self.store.create_artifact(self.project.id, ArtifactCreate(name="http-result.json", artifact_type="result_table"))
        first = main.upload_artifact_content(
            artifact.id,
            UploadFile(file=io.BytesIO(b"first"), filename="http-result.json"),
            idempotency_key="upload-key-001",
        )
        second = main.upload_artifact_content(
            artifact.id,
            UploadFile(file=io.BytesIO(b"first"), filename="http-result.json"),
            idempotency_key="upload-key-001",
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(self.store.get_artifact_content(artifact.id), b"first")

        with self.assertRaisesRegex(HTTPException, "idempotency_key_reused_for_different_request"):
            main.upload_artifact_content(
                artifact.id,
                UploadFile(file=io.BytesIO(b"different"), filename="http-result.json"),
                idempotency_key="upload-key-001",
            )

    def test_multipart_and_git_write_contracts_require_http_idempotency_key(self) -> None:
        artifact = self.store.create_artifact(self.project.id, ArtifactCreate(name="multipart-http.json", artifact_type="result_table"))
        with self.assertRaises(HTTPException) as context:
            main.initiate_artifact_multipart(artifact.id)
        self.assertEqual(context.exception.detail, "idempotency_key_required")

    def test_upload_limit_is_enforced_before_storage(self) -> None:
        artifact = self.store.create_artifact(self.project.id, ArtifactCreate(name="limited-http.json", artifact_type="result_table"))
        with patch.dict(os.environ, {"MAX_ARTIFACT_UPLOAD_BYTES": "4"}):
            with self.assertRaises(HTTPException) as context:
                main.upload_artifact_content(
                    artifact.id,
                    UploadFile(file=io.BytesIO(b"too-large"), filename="limited-http.json"),
                    idempotency_key="limited-key-001",
                )
        self.assertEqual(context.exception.status_code, 413)
        self.assertEqual(context.exception.detail, "artifact_upload_too_large")
        self.assertIsNone(self.store.get_artifact(artifact.id).storage_key)

    def test_project_authorization_uses_session_and_rejects_cross_project_reads(self) -> None:
        organization = self.store.create_organization(OrganizationCreate(name="HTTP 第二组织", slug="http-second-org"))
        team = self.store.create_team(TeamCreate(organization_id=organization.id, name="HTTP 第二队伍"))
        member = self.store.create_member(HumanMemberCreate(organization_id=organization.id, team_id=team.id, email="http-second@example.local", display_name="HTTP 第二成员", role="owner"))
        other_project = self.store.create_project(
            main.ProjectCreate(name="HTTP 隔离项目", organization_id=organization.id, team_id=team.id, created_by=member.id)
        )
        session = self.store.create_session(SessionCreate(member_id=member.id))

        with patch.dict(os.environ, {"PLATFORM_AUTH_MODE": "required"}):
            request = main.Request({"type": "http", "headers": []})
            request.scope["headers"] = [(b"authorization", f"Bearer {session.token}".encode())]
            self.assertEqual(main._request_member_id(request), member.id)
            with self.assertRaises(PermissionError):
                self.store.authorize_member(self.project.id, member.id, "project.view")
            self.store.authorize_member(other_project.id, member.id, "project.view")

            async def next_handler(_: object) -> Response:
                return Response(status_code=200)

            denied_request = main.Request(
                {
                    "type": "http",
                    "method": "GET",
                    "path": f"/api/projects/{self.project.id}",
                    "headers": [(b"authorization", f"Bearer {session.token}".encode())],
                }
            )
            response = asyncio.run(main.enforce_project_authorization(denied_request, next_handler))
            self.assertEqual(response.status_code, 403)

    def test_bundle_restore_checks_manifest_organization_before_replay(self) -> None:
        organization = self.store.create_organization(OrganizationCreate(name="Restore 第二组织", slug="restore-second-org"))
        team = self.store.create_team(TeamCreate(organization_id=organization.id, name="Restore 第二队伍"))
        member = self.store.create_member(HumanMemberCreate(organization_id=organization.id, team_id=team.id, email="restore-second@example.local", display_name="Restore 第二成员", role="owner"))
        session = self.store.create_session(SessionCreate(member_id=member.id))
        bundle = Path(self.temp_dir.name) / "bundle.zip"
        self.store.export_project_bundle(self.project.id, bundle)
        request = main.Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/api/projects/restore",
                "headers": [(b"authorization", f"Bearer {session.token}".encode())],
            }
        )
        with patch.dict(os.environ, {"PLATFORM_AUTH_MODE": "required"}):
            with self.assertRaises(HTTPException) as context:
                main.restore_project_bundle(
                    request,
                    UploadFile(file=io.BytesIO(bundle.read_bytes()), filename="bundle.zip"),
                    idempotency_key="restore-key-001",
                )
        self.assertEqual(context.exception.status_code, 403)

    def test_device_token_rotation_route_returns_new_credential(self) -> None:
        from app.contracts import AgentRegister, DevicePairingCreate
        from app.store import DEV_ORG_ID
        from device_test_support import registration_request
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from uuid import UUID

        agent = self.store.register_agent(
            AgentRegister(agent_id="http-rotation-agent", display_name="HTTP Rotation Agent", owner_member_id="member-001")
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        credential = self.store.register_device(
            registration_request(pairing, Ed25519PrivateKey.generate(), agent.agent_id, "http-rotation-device")
        )
        request = main.Request({"type": "http", "method": "POST", "path": "/api/devices/http-rotation-device/rotate-token", "headers": []})
        rotated = main.rotate_device_token(
            "http-rotation-device",
            main.DeviceTokenRotateRequest(reason="http test"),
            request,
        )
        self.assertNotEqual(rotated.device_token, credential.device_token)
        self.assertEqual(rotated.device.token_version, 2)


if __name__ == "__main__":
    unittest.main()
