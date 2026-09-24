from __future__ import annotations

import tempfile
import unittest
import hashlib
import io
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from app import main
from app.contracts import (
    AgentRegister,
    AgentTaskClaimRequest,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    DeviceRegisterRequest,
    RunComplete,
    RunCreate,
    TaskClaimRequest,
    TaskCreate,
    TaskLeaseHeartbeat,
    TaskProgressRequest,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class AgentCapabilityHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="capability-agent", display_name="Capability Agent", owner_member_id="member-001")
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        device_credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                self.agent.agent_id,
                "device-capability-001",
                device_name="Capability workstation",
                capabilities=["task.claim", "run.create"],
            )
        )
        self.device = device_credential.device
        self.full_grant = self.store.create_device_project_grant(
            self.project.id,
            DeviceProjectGrantCreate(device_id=self.device.device_id),
            "member-001",
        )

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    @staticmethod
    def _request(token: str | None = None, agent_id: str | None = None) -> Request:
        headers = []
        if token is not None:
            headers.append((b"x-project-capability-token", token.encode("utf-8")))
        if agent_id is not None:
            headers.append((b"x-agent-id", agent_id.encode("utf-8")))
        return Request({"type": "http", "method": "POST", "path": "/api/agent-operation", "headers": headers})

    def test_machine_mutations_require_project_capability_token(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="Capability gated task"))
        with self.assertRaisesRegex(HTTPException, "agent_project_token_required"):
            main.claim_next_task(
                self.agent.agent_id,
                AgentTaskClaimRequest(project_id=self.project.id, idempotency_key="missing-token-001"),
                self._request(),
            )
        self.assertEqual(self.store.get_task(task.id).status, "READY")

    def test_project_token_covers_claim_lease_progress_and_run_lifecycle(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="Capability lifecycle task"))
        request = self._request(self.full_grant.project_token)
        assignment = main.claim_task(
            task.id,
            TaskClaimRequest(agent_id=self.agent.agent_id, idempotency_key="capability-claim-001"),
            request,
        )

        lease = main.heartbeat_task_lease(
            assignment.lease.lease_token,
            TaskLeaseHeartbeat(agent_id=self.agent.agent_id, project_id=self.project.id, extend_seconds=300),
            request,
        )
        self.assertEqual(lease.project_id, self.project.id)

        progress = main.progress_task(
            task.id,
            TaskProgressRequest(
                agent_id=self.agent.agent_id,
                lease_token=assignment.lease.lease_token,
                status="RUNNING",
                idempotency_key="capability-progress-001",
            ),
            request,
        )
        self.assertEqual(progress.status, "RUNNING")

        run = main.create_run(
            self.project.id,
            RunCreate(
                task_id=task.id,
                agent_id=self.agent.agent_id,
                idempotency_key="capability-run-001",
            ),
            request,
        )
        self.assertEqual(run.status, "RUNNING")
        completed = main.complete_run(run.id, RunComplete(success=False, summary="agent capability test"), request)
        self.assertEqual(completed.status, "FAILED")

    def test_token_is_bound_to_project_agent_and_capability(self) -> None:
        other_project = self.store.create_project(
            main.ProjectCreate(
                name="Capability Other Project",
                organization_id=UUID(DEV_ORG_ID),
                team_id=self.project.team_id,
                created_by="member-001",
            )
        )
        other_task = self.store.create_task(other_project.id, TaskCreate(title="Other project task"))
        with self.assertRaisesRegex(HTTPException, "device_project_token_invalid"):
            main.claim_next_task(
                self.agent.agent_id,
                AgentTaskClaimRequest(project_id=other_project.id, idempotency_key="cross-project-001"),
                self._request(self.full_grant.project_token),
            )
        with self.assertRaisesRegex(HTTPException, "agent_project_agent_mismatch"):
            main.claim_next_task(
                "forged-agent",
                AgentTaskClaimRequest(project_id=self.project.id, idempotency_key="agent-mismatch-001"),
                self._request(self.full_grant.project_token),
            )
        self.assertEqual(self.store.get_task(other_task.id).status, "READY")

        claim_only = self.store.create_device_project_grant(
            self.project.id,
            DeviceProjectGrantCreate(device_id=self.device.device_id, capabilities=["task.claim"]),
            "member-001",
        )
        with self.assertRaisesRegex(HTTPException, "device_capability_denied"):
            main.create_run(
                self.project.id,
                RunCreate(agent_id=self.agent.agent_id, idempotency_key="capability-denied-001"),
                self._request(claim_only.project_token),
            )

    def test_agent_artifact_upload_is_bound_to_run_and_is_idempotent(self) -> None:
        request = self._request(self.full_grant.project_token, self.agent.agent_id)
        run = main.create_run(
            self.project.id,
            RunCreate(agent_id=self.agent.agent_id, idempotency_key="artifact-run-create-001"),
            request,
        )
        artifact = main.create_agent_artifact(
            self.project.id,
            main.ArtifactCreate(
                name="agent-result.json",
                artifact_type="result_table",
                content_hash=hashlib.sha256(b"agent-result").hexdigest(),
                run_id=run.id,
                mime_type="application/json",
            ),
            request,
            "agent-artifact-create-001",
        )
        uploaded = main.upload_agent_artifact_content(
            artifact.id,
            request,
            UploadFile(file=io.BytesIO(b"agent-result"), filename="agent-result.json"),
            hashlib.sha256(b"agent-result").hexdigest(),
            "agent-artifact-upload-001",
        )
        repeated = main.upload_agent_artifact_content(
            artifact.id,
            request,
            UploadFile(file=io.BytesIO(b"agent-result"), filename="agent-result.json"),
            hashlib.sha256(b"agent-result").hexdigest(),
            "agent-artifact-upload-001",
        )
        self.assertEqual(uploaded.content_hash, repeated.content_hash)
        completed = main.complete_run(
            run.id,
            RunComplete(success=True, output_artifact_ids=[artifact.id]),
            request,
            "agent-run-complete-001",
        )
        self.assertEqual(completed.status, "SUCCEEDED")

    def test_lease_token_cannot_be_rebound_to_another_project(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="Lease scope task"))
        assignment = main.claim_task(
            task.id,
            TaskClaimRequest(agent_id=self.agent.agent_id, idempotency_key="lease-scope-claim-001"),
            self._request(self.full_grant.project_token),
        )
        other_project = self.store.create_project(
            main.ProjectCreate(
                name="Lease Scope Other Project",
                organization_id=UUID(DEV_ORG_ID),
                team_id=self.project.team_id,
                created_by="member-001",
            )
        )
        other_grant = self.store.create_device_project_grant(
            other_project.id,
            DeviceProjectGrantCreate(device_id=self.device.device_id),
            "member-001",
        )
        with self.assertRaisesRegex(HTTPException, "lease_project_mismatch"):
            main.heartbeat_task_lease(
                assignment.lease.lease_token,
                TaskLeaseHeartbeat(agent_id=self.agent.agent_id, project_id=other_project.id, extend_seconds=300),
                self._request(other_grant.project_token),
            )
        self.assertEqual(self.store.get_task(task.id).status, "CLAIMED")


if __name__ == "__main__":
    unittest.main()
