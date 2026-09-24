from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from app.contracts import (
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    DeviceRegisterRequest,
    DeviceStatus,
    AgentRegister,
    OrganizationCreate,
    TeamCreate,
    HumanMemberCreate,
    ProjectCreate,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request
from app.device_identity import parse_public_key
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class DeviceContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _register_device(self, agent_id: str = "device-agent"):
        agent = self.store.register_agent(
            AgentRegister(agent_id=agent_id, display_name="Device Agent", owner_member_id="member-001")
        )
        pairing = self.store.create_device_pairing(
            DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)),
            "member-001",
        )
        private_key = Ed25519PrivateKey.generate()
        credential = self.store.register_device(
            registration_request(
                pairing,
                private_key,
                agent.agent_id,
                f"device-{agent_id}",
                device_name="Windows workstation",
                platform="windows",
                agent_version="0.1.0",
                capabilities=["task.claim", "artifact.read"],
            )
        )
        return pairing, credential

    def test_pairing_is_one_time_and_device_secret_is_not_persisted_in_plaintext(self) -> None:
        pairing, credential = self._register_device()

        self.assertEqual(pairing.status, "PENDING")
        self.assertEqual(credential.device.status, DeviceStatus.ACTIVE)
        stored = self.store.db.execute(
            "SELECT p.code_hash, d.device_token_hash, p.status AS pairing_status, p.device_id FROM device_pairings p LEFT JOIN devices d ON d.device_id = p.device_id WHERE p.id = ?",
            (str(pairing.id),),
        ).fetchone()
        self.assertEqual(stored["pairing_status"], "CONSUMED")
        self.assertEqual(stored["device_id"], credential.device.device_id)
        self.assertEqual(stored["code_hash"], hashlib.sha256((pairing.pairing_code or "").encode()).hexdigest())
        self.assertEqual(stored["device_token_hash"], hashlib.sha256(credential.device_token.encode()).hexdigest())
        self.assertNotEqual(stored["code_hash"], pairing.pairing_code)
        self.assertNotEqual(stored["device_token_hash"], credential.device_token)
        self.assertEqual(self.store.resolve_device_token(credential.device_token).device_id, credential.device.device_id)

        with self.assertRaisesRegex(ValueError, "device_pairing_not_pending"):
            self.store.register_device(
                registration_request(
                    pairing,
                    Ed25519PrivateKey.generate(),
                    "device-agent",
                    "device-replay",
                    device_name="Replay workstation",
                )
            )

    def test_pairing_returns_one_time_challenge_and_only_hash_is_persisted(self) -> None:
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        stored = self.store.db.execute("SELECT challenge_hash FROM device_pairings WHERE id = ?", (str(pairing.id),)).fetchone()
        self.assertIsNotNone(pairing.challenge)
        self.assertEqual(stored["challenge_hash"], hashlib.sha256((pairing.challenge or "").encode()).hexdigest())
        self.assertNotEqual(stored["challenge_hash"], pairing.challenge)

    def test_registration_rejects_invalid_signature_and_does_not_consume_pairing(self) -> None:
        agent = self.store.register_agent(AgentRegister(agent_id="proof-agent", display_name="Proof Agent", owner_member_id="member-001"))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        request = registration_request(pairing, Ed25519PrivateKey.generate(), agent.agent_id, "device-proof-001")
        with self.assertRaisesRegex(ValueError, "device_signature_invalid"):
            self.store.register_device(request.model_copy(update={"challenge_signature": "A" * 88}))
        state = self.store.db.execute("SELECT status FROM device_pairings WHERE id = ?", (str(pairing.id),)).fetchone()
        self.assertEqual(state["status"], "PENDING")

    def test_registration_rejects_wrong_challenge_pairing_id_device_id_and_public_key(self) -> None:
        agent = self.store.register_agent(AgentRegister(agent_id="proof-edge-agent", display_name="Proof Edge Agent", owner_member_id="member-001"))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        private_key = Ed25519PrivateKey.generate()
        request = registration_request(pairing, private_key, agent.agent_id, "device-proof-edge")
        with self.assertRaisesRegex(PermissionError, "device_pairing_challenge_invalid"):
            self.store.register_device(request.model_copy(update={"challenge": "x" * 43}))
        with self.assertRaisesRegex(PermissionError, "device_pairing_invalid"):
            self.store.register_device(request.model_copy(update={"pairing_id": uuid4()}))
        with self.assertRaisesRegex(ValueError, "device_signature_invalid"):
            self.store.register_device(request.model_copy(update={"device_id": "device-other-id"}))
        other_key = Ed25519PrivateKey.generate()
        other_public_key = other_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode("ascii")
        with self.assertRaisesRegex(ValueError, "device_signature_invalid"):
            self.store.register_device(request.model_copy(update={"public_key": other_public_key}))

    def test_success_uses_canonical_ed25519_fingerprint(self) -> None:
        agent = self.store.register_agent(AgentRegister(agent_id="canonical-agent", display_name="Canonical Agent", owner_member_id="member-001"))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        private_key = Ed25519PrivateKey.generate()
        request = registration_request(pairing, private_key, agent.agent_id, "device-canonical-001")
        credential = self.store.register_device(request)
        _, fingerprint = parse_public_key(request.public_key)
        self.assertEqual(credential.device.public_key_fingerprint, fingerprint)
        self.assertEqual(len(fingerprint), 64)

    def test_project_token_is_bound_to_device_project_and_capability(self) -> None:
        _, credential = self._register_device()
        grant_credential = self.store.create_device_project_grant(
            self.project.id,
            DeviceProjectGrantCreate(device_id=credential.device.device_id, capabilities=["task.claim"]),
            "member-001",
        )
        grant = self.store.resolve_device_project_token(grant_credential.project_token, self.project.id, "task.claim")
        self.assertEqual(grant.device_id, credential.device.device_id)
        self.assertEqual(grant.agent_id, credential.device.agent_id)
        with self.assertRaisesRegex(PermissionError, "device_capability_denied"):
            self.store.resolve_device_project_token(grant_credential.project_token, self.project.id, "artifact.write")

        other_org = self.store.create_organization(OrganizationCreate(name="Other Device Org", slug="other-device-org"))
        other_team = self.store.create_team(TeamCreate(organization_id=other_org.id, name="Other Device Team"))
        other_member = self.store.create_member(
            HumanMemberCreate(
                organization_id=other_org.id,
                team_id=other_team.id,
                email="other-device@example.local",
                display_name="Other Device Owner",
                role="owner",
            )
        )
        other_project = self.store.create_project(
            ProjectCreate(name="Other Device Project", organization_id=other_org.id, team_id=other_team.id, created_by=other_member.id)
        )
        with self.assertRaisesRegex(PermissionError, "device_project_organization_mismatch"):
            self.store.create_device_project_grant(
                other_project.id,
                DeviceProjectGrantCreate(device_id=credential.device.device_id),
                other_member.id,
            )
        with self.assertRaisesRegex(PermissionError, "device_project_token_invalid"):
            self.store.resolve_device_project_token(grant_credential.project_token, other_project.id)

    def test_device_revocation_invalidates_all_project_credentials_and_connections(self) -> None:
        _, credential = self._register_device()
        grant_credential = self.store.create_device_project_grant(
            self.project.id,
            DeviceProjectGrantCreate(device_id=credential.device.device_id),
            "member-001",
        )
        connection = self.store.open_agent_connection(
            credential.device.device_id,
            session_id="session-001",
            connection_id="connection-001",
        )
        self.assertEqual(connection.status, "CONNECTED")
        revoked = self.store.revoke_device(credential.device.device_id, "member-001", "lost workstation")
        self.assertEqual(revoked.status, DeviceStatus.REVOKED)
        with self.assertRaisesRegex(PermissionError, "device_revoked"):
            self.store.resolve_device_token(credential.device_token)
        with self.assertRaisesRegex(PermissionError, "device_project_token_revoked"):
            self.store.resolve_device_project_token(grant_credential.project_token, self.project.id)
        stored_connection = self.store.db.execute(
            "SELECT status FROM agent_connections WHERE connection_id = ?", (connection.connection_id,)
        ).fetchone()
        self.assertEqual(stored_connection["status"], "REVOKED")

    def test_device_token_rotation_invalidates_old_token_and_live_connections(self) -> None:
        _, credential = self._register_device("rotation-agent")
        connection = self.store.open_agent_connection(
            credential.device.device_id,
            session_id="rotation-session-001",
            connection_id="rotation-connection-001",
        )

        rotated = self.store.rotate_device_token(
            credential.device.device_id,
            "member-001",
            "credential rollover",
        )
        self.assertNotEqual(rotated.device_token, credential.device_token)
        self.assertEqual(rotated.device.token_version, 2)
        self.assertIsNotNone(rotated.device.token_rotated_at)
        with self.assertRaisesRegex(PermissionError, "device_token_invalid"):
            self.store.resolve_device_token(credential.device_token)
        self.assertEqual(
            self.store.resolve_device_token(rotated.device_token).device_id,
            credential.device.device_id,
        )
        self.assertEqual(
            self.store.db.execute(
                "SELECT status FROM agent_connections WHERE connection_id = ?",
                (connection.connection_id,),
            ).fetchone()["status"],
            "REVOKED",
        )
        audit = self.store.db.execute(
            "SELECT reason, token_version, device_id FROM device_token_rotations WHERE device_id = ?",
            (credential.device.device_id,),
        ).fetchone()
        self.assertEqual(dict(audit), {
            "reason": "credential rollover",
            "token_version": 2,
            "device_id": credential.device.device_id,
        })

    def test_device_token_rotation_requires_active_device_and_admin(self) -> None:
        _, credential = self._register_device("rotation-policy-agent")
        with self.assertRaisesRegex(PermissionError, "member_organization_access_denied"):
            self.store.rotate_device_token(credential.device.device_id, "missing-member")
        self.store.revoke_device(credential.device.device_id, "member-001")
        with self.assertRaisesRegex(PermissionError, "device_revoked"):
            self.store.rotate_device_token(credential.device.device_id, "member-001")

    def test_device_pairing_requires_organization_owner_and_agent_owner_match(self) -> None:
        other_org = self.store.create_organization(OrganizationCreate(name="Pairing Org", slug="pairing-org"))
        other_team = self.store.create_team(TeamCreate(organization_id=other_org.id, name="Pairing Team"))
        member = self.store.create_member(
            HumanMemberCreate(
                organization_id=other_org.id,
                team_id=other_team.id,
                email="pairing@example.local",
                display_name="Pairing Owner",
                role="owner",
            )
        )
        wrong_agent = self.store.register_agent(AgentRegister(agent_id="wrong-owner-agent", display_name="Wrong Owner", owner_member_id="member-001"))
        agent = self.store.register_agent(AgentRegister(agent_id="correct-owner-agent", display_name="Correct Owner", owner_member_id=member.id))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=other_org.id), member.id)
        with self.assertRaisesRegex(PermissionError, "device_agent_owner_mismatch"):
            self.store.register_device(
                registration_request(
                    pairing,
                    Ed25519PrivateKey.generate(),
                    wrong_agent.agent_id,
                    "device-wrong-owner",
                    device_name="Wrong Agent",
                )
            )
        # The pairing remains usable because a failed registration does not consume it.
        valid = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                agent.agent_id,
                "device-correct-owner",
                device_name="Correct Agent",
            )
        )
        self.assertEqual(valid.device.owner_member_id, member.id)


if __name__ == "__main__":
    unittest.main()
