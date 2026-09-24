from __future__ import annotations

import unittest
from datetime import UTC, datetime
from uuid import uuid4

from app.contracts import HandoffStatus
from app.postgres_repository import PostgresRepository


class PostgresRepositoryContractTests(unittest.TestCase):
    def test_module_imports_without_postgres_runtime_dependencies(self) -> None:
        self.assertTrue(hasattr(PostgresRepository, "transaction"))
        self.assertTrue(hasattr(PostgresRepository, "list_projects"))
        self.assertTrue(hasattr(PostgresRepository, "add_event"))
        self.assertTrue(hasattr(PostgresRepository, "list_pending_event_outbox"))
        self.assertTrue(hasattr(PostgresRepository, "mark_event_outbox_delivered"))
        self.assertTrue(hasattr(PostgresRepository, "mark_event_outbox_failed"))
        for method in [
            "get_device",
            "create_device_pairing",
            "register_device",
            "resolve_device_token",
            "rotate_device_token",
            "create_device_project_grant",
            "resolve_device_project_token",
            "open_agent_connection",
            "record_gateway_receive",
            "next_gateway_send_sequence",
            "record_agent_heartbeat",
            "get_device_runtime_state",
            "list_device_project_grants_for_device",
            "get_gateway_command_result",
            "save_gateway_command_result",
            "create_handoff",
            "accept_handoff",
            "create_review",
        ]:
            self.assertTrue(hasattr(PostgresRepository, method))

    def test_jsonb_rows_are_mapped_to_domain_models(self) -> None:
        task_id = uuid4()
        project_id = uuid4()
        handoff_id = uuid4()
        task = PostgresRepository._task(
            {
                "id": task_id,
                "project_id": project_id,
                "title": "PostgreSQL task",
                "description": "repository mapping",
                "stage": "modeling",
                "status": "READY",
                "assignee": "Unassigned",
                "priority": "medium",
                "requires_review": True,
                "allow_future_data": False,
                "input_artifacts": [],
                "input_handoff_ids": [handoff_id],
                "output_types": ["model_spec"],
                "parent_task_id": None,
                "dependency_task_ids": [],
                "acceptance_criteria": ["queryable"],
                "required_capabilities": ["python"],
                "deadline": None,
                "information_boundary": {},
                "resource_policy": {},
                "requires_human_approval": True,
                "blocked_reason": None,
                "updated_at": datetime.now(UTC),
            }
        )
        self.assertEqual(task.id, task_id)
        self.assertEqual(task.project_id, project_id)
        self.assertEqual(task.input_handoff_ids, [handoff_id])
        self.assertEqual(task.required_capabilities, ["python"])

    def test_plain_jsonb_string_is_preserved_for_handoff_receiver(self) -> None:
        handoff_id = uuid4()
        project_id = uuid4()
        task_id = uuid4()
        handoff = PostgresRepository._handoff(
            {
                "id": handoff_id,
                "project_id": project_id,
                "task_id": task_id,
                "sender_agent_id": "agent-a",
                "receiver": "Team",
                "status": HandoffStatus.PASS_WITH_ASSUMPTIONS.value,
                "objective": "handoff",
                "completed": [],
                "input_artifacts": [],
                "output_artifacts": [],
                "key_conclusions": [],
                "assumptions": [],
                "evidence_refs": [],
                "open_questions": [],
                "risks": [],
                "next_actions": [],
                "requires_human_approval": True,
                "schema_version": "1.0",
                "handoff_type": "RELAY",
                "receipt_status": "PENDING",
                "received_by": None,
                "received_at": None,
                "idempotency_key": None,
                "created_at": datetime.now(UTC),
            }
        )
        self.assertEqual(handoff.receiver, "Team")

    def test_device_and_connection_rows_are_mapped_without_secrets(self) -> None:
        now = datetime.now(UTC)
        device = PostgresRepository._device(
            {
                "device_id": "device-001",
                "organization_id": uuid4(),
                "agent_id": "agent-001",
                "owner_member_id": "member-001",
                "device_name": "Windows workstation",
                "public_key": "not-returned",
                "public_key_fingerprint": "a" * 64,
                "device_token_hash": "b" * 64,
                "platform": "windows",
                "agent_version": "0.2.0",
                "capabilities": ["task.claim"],
                "status": "active",
                "created_at": now,
                "last_seen": now,
                "revoked_at": None,
            }
        )
        connection = PostgresRepository._agent_connection(
            {
                "connection_id": "connection-001",
                "device_id": device.device_id,
                "agent_id": device.agent_id,
                "session_id": "session-001",
                "transport": "websocket",
                "status": "CONNECTED",
                "last_received_sequence": 3,
                "last_sent_sequence": 2,
                "connected_at": now,
                "last_heartbeat_at": now,
                "disconnected_at": None,
            }
        )
        self.assertEqual(device.device_id, "device-001")
        self.assertFalse(hasattr(device, "public_key"))
        self.assertEqual(connection.last_received_sequence, 3)
        self.assertEqual(connection.last_sent_sequence, 2)


if __name__ == "__main__":
    unittest.main()
