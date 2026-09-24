from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from app.contracts import (
    AgentHeartbeat,
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    DeviceRegisterRequest,
    GatewayEnvelope,
    ReviewerKind,
    TaskCreate,
    TaskStatus,
)
from app.gateway import GatewayProtocolError, GatewayService
from app import main
from app.store import DEV_ORG_ID, Store
from packages.agent_protocol import SessionIpcEvent
from starlette.websockets import WebSocketDisconnect
from device_test_support import registration_request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class FakeGatewayWebSocket:
    def __init__(self, token: str, messages: list[dict]) -> None:
        self.headers = {"authorization": f"Bearer {token}"}
        self.query_params = {
            "session_id": "session-route-001",
            "connection_id": "connection-route-001",
        }
        self.messages = iter(messages)
        self.sent: list[dict] = []
        self.accepted = False
        self.closed_code: int | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, value: dict) -> None:
        self.sent.append(value)

    async def receive_json(self) -> dict:
        try:
            return next(self.messages)
        except StopIteration as error:
            raise WebSocketDisconnect(code=1000) from error

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code


class GatewayContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.agent = self.store.register_agent(AgentRegister(agent_id="gateway-agent", display_name="Gateway Agent"))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        self.credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                self.agent.agent_id,
                "device-gateway-001",
                device_name="Gateway workstation",
                platform="windows",
                agent_version="0.2.0",
                capabilities=["task.claim"],
            )
        )
        self.project = self.store.list_projects()[0]
        self.project_credential = self.store.create_device_project_grant(
            self.project.id,
            DeviceProjectGrantCreate(device_id=self.credential.device.device_id),
            "member-001",
        )
        self.gateway = GatewayService(self.store)
        self.context = self.gateway.authenticate(
            self.credential.device_token,
            device_id=self.credential.device.device_id,
            session_id="session-gateway-001",
            connection_id="connection-gateway-001",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _heartbeat_message(self, sequence: int, message_id: str = "agent-message-001") -> dict:
        heartbeat = AgentHeartbeat(
            device_id=self.credential.device.device_id,
            agent_id=self.agent.agent_id,
            session_id="session-gateway-001",
            connection_id="connection-gateway-001",
            agent_version="0.2.0",
            capabilities=["task.claim"],
            sent_at=datetime.now(UTC),
        )
        return GatewayEnvelope(
            device_id=self.credential.device.device_id,
            agent_id=self.agent.agent_id,
            session_id="session-gateway-001",
            connection_id="connection-gateway-001",
            sequence=sequence,
            message_id=message_id,
            idempotency_key=f"idempotency-{message_id}",
            message_type="agent.heartbeat",
            sent_at=datetime.now(UTC),
            payload=heartbeat.model_dump(mode="json"),
        ).model_dump(mode="json")

    def _command_message(self, message_type: str, payload: dict, sequence: int, message_id: str) -> dict:
        return GatewayEnvelope(
            device_id=self.credential.device.device_id,
            agent_id=self.agent.agent_id,
            session_id="session-gateway-001",
            connection_id="connection-gateway-001",
            sequence=sequence,
            message_id=message_id,
            idempotency_key=f"idempotency-{message_id}",
            message_type=message_type,
            sent_at=datetime.now(UTC),
            payload=payload,
        ).model_dump(mode="json")

    def test_handshake_binds_path_and_connection_identity(self) -> None:
        self.assertEqual(self.context.device.device_id, self.credential.device.device_id)
        self.assertEqual(self.context.connection.status, "CONNECTED")
        connected = self.gateway.connected_message(self.context)
        self.assertEqual(connected.message_type, "gateway.connected")
        self.assertEqual(connected.sequence, 1)
        with self.assertRaisesRegex(GatewayProtocolError, "gateway_device_path_mismatch"):
            self.gateway.authenticate(
                self.credential.device_token,
                device_id="another-device",
                session_id="session-gateway-002",
                connection_id="connection-gateway-002",
            )

    def test_heartbeat_sequence_ack_duplicate_and_gap_replay(self) -> None:
        self.gateway.connected_message(self.context)
        first = self.gateway.receive(self.context, self._heartbeat_message(1))
        self.assertEqual(first.message_type, "gateway.ack")
        self.assertEqual(first.payload["highest_contiguous_sequence"], 1)
        self.assertFalse(first.payload["duplicate"])
        self.assertEqual(self.store.get_agent_connection("connection-gateway-001").last_received_sequence, 1)
        self.assertEqual(self.store.heartbeat(self.agent.agent_id).status, "online")

        duplicate = self.gateway.receive(self.context, self._heartbeat_message(1))
        self.assertEqual(duplicate.message_type, "gateway.ack")
        self.assertTrue(duplicate.payload["duplicate"])
        self.assertEqual(self.store.get_agent_connection("connection-gateway-001").last_received_sequence, 1)

        gap = self.gateway.receive(self.context, self._heartbeat_message(3, "agent-message-003"))
        self.assertEqual(gap.message_type, "gateway.replay_required")
        self.assertEqual(gap.payload["after_sequence"], 1)
        self.assertEqual(self.store.get_agent_connection("connection-gateway-001").last_received_sequence, 1)

        second = self.gateway.receive(self.context, self._heartbeat_message(2, "agent-message-002"))
        self.assertEqual(second.payload["highest_contiguous_sequence"], 2)

    def test_invalid_payload_and_identity_do_not_advance_input_sequence(self) -> None:
        self.gateway.connected_message(self.context)
        invalid = self._heartbeat_message(1)
        invalid["payload"]["agent_id"] = "forged-agent"
        response = self.gateway.receive(self.context, invalid)
        self.assertEqual(response.message_type, "gateway.error")
        self.assertEqual(response.payload["code"], "gateway_identity_mismatch")
        self.assertEqual(self.store.get_agent_connection("connection-gateway-001").last_received_sequence, 0)

        unsupported = self._heartbeat_message(1)
        unsupported["message_type"] = "desktop.execute"
        response = self.gateway.receive(self.context, unsupported)
        self.assertEqual(response.payload["code"], "gateway_message_type_not_supported")
        self.assertEqual(self.store.get_agent_connection("connection-gateway-001").last_received_sequence, 0)

    def test_revoked_connection_cannot_accept_or_send_protocol_messages(self) -> None:
        self.store.revoke_device(self.credential.device.device_id, "member-001", "test revoke")
        with self.assertRaisesRegex(GatewayProtocolError, "device_connection_revoked"):
            self.gateway.receive(self.context, self._heartbeat_message(1))

    def test_gateway_task_command_requires_project_token_and_returns_result_ack(self) -> None:
        self.store.create_task(self.project.id, TaskCreate(title="Gateway command task"))
        response = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.task.claim",
                {"project_id": str(self.project.id), "project_token": self.project_credential.project_token},
                1,
                "gateway-command-001",
            ),
        )
        self.assertEqual(response.message_type, "gateway.ack")
        command_result = response.payload["command_result"]
        self.assertEqual(command_result["message_type"], "agent.task.claim")
        self.assertIsNotNone(command_result["result"])
        self.assertIn("lease", command_result["result"])

        invalid = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.task.claim",
                {"project_id": str(self.project.id), "project_token": "prj_invalid_gateway_token"},
                2,
                "gateway-command-002",
            ),
        )
        self.assertEqual(invalid.message_type, "gateway.error")
        self.assertEqual(invalid.payload["code"], "device_project_token_invalid")
        self.assertEqual(self.store.get_agent_connection(self.context.connection.connection_id).last_received_sequence, 2)

    def test_duplicate_command_recovers_persisted_business_result_after_ack_loss(self) -> None:
        self.store.create_task(self.project.id, TaskCreate(title="Recoverable Gateway command"))
        message = self._command_message(
            "agent.task.claim",
            {"project_id": str(self.project.id), "project_token": self.project_credential.project_token},
            1,
            "gateway-recovery-001",
        )
        first = self.gateway.receive(self.context, message)
        repeated = self.gateway.receive(self.context, message)

        self.assertEqual(first.payload["command_result"], repeated.payload["command_result"])
        self.assertTrue(repeated.payload["duplicate"])
        stored = self.store.get_gateway_command_result(
            self.context.connection.connection_id,
            message_id="gateway-recovery-001",
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "SUCCEEDED")
        self.assertEqual(stored.result, first.payload["command_result"]["result"])

    def test_same_idempotency_key_with_new_message_id_reuses_command_result(self) -> None:
        self.store.create_task(self.project.id, TaskCreate(title="Idempotent Gateway command"))
        first = self._command_message(
            "agent.task.claim",
            {"project_id": str(self.project.id), "project_token": self.project_credential.project_token},
            1,
            "gateway-idempotency-001",
        )
        second = dict(first)
        second["sequence"] = 2
        second["message_id"] = "gateway-idempotency-002"
        first_result = self.gateway.receive(self.context, first)
        second_result = self.gateway.receive(self.context, second)

        self.assertTrue(second_result.payload["duplicate"])
        self.assertEqual(first_result.payload["command_result"], second_result.payload["command_result"])
        self.assertEqual(self.store.get_agent_connection(self.context.connection.connection_id).last_received_sequence, 2)

    def test_duplicate_failed_command_recovers_persisted_error(self) -> None:
        message = self._command_message(
            "agent.task.claim",
            {"project_id": str(self.project.id), "project_token": "prj-invalid-gateway-token"},
            1,
            "gateway-error-recovery-001",
        )
        first = self.gateway.receive(self.context, message)
        repeated = self.gateway.receive(self.context, message)

        self.assertEqual(first.message_type, "gateway.error")
        self.assertEqual(repeated.message_type, "gateway.error")
        self.assertEqual(first.payload, repeated.payload)
        stored = self.store.get_gateway_command_result(
            self.context.connection.connection_id,
            idempotency_key=message["idempotency_key"],
        )
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "FAILED")
        self.assertEqual(stored.error_code, "device_project_token_invalid")

    def test_gateway_run_create_and_complete_use_project_capability_token(self) -> None:
        created = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.run.create",
                {"project_id": str(self.project.id), "project_token": self.project_credential.project_token},
                1,
                "gateway-run-create-001",
            ),
        )
        self.assertEqual(created.message_type, "gateway.ack")
        run = created.payload["command_result"]["result"]
        self.assertEqual(run["status"], "RUNNING")

        completed = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.run.complete",
                {
                    "project_id": str(self.project.id),
                    "project_token": self.project_credential.project_token,
                    "run_id": run["id"],
                    "success": False,
                    "summary": "gateway test",
                },
                2,
                "gateway-run-complete-001",
            ),
        )
        self.assertEqual(completed.message_type, "gateway.ack")
        self.assertEqual(completed.payload["command_result"]["result"]["status"], "FAILED")

    def test_gateway_handoff_create_accept_and_duplicate_recovery(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="Gateway handoff task"))
        payload = {
            "project_id": str(self.project.id),
            "project_token": self.project_credential.project_token,
            "task_id": str(task.id),
            "receiver": {"type": "agent", "id": self.agent.agent_id},
            "objective": "把结构化结果交给下游 Agent",
            "completed": ["完成初步分析"],
            "key_conclusions": ["结论需要独立复核"],
            "assumptions": ["输入数据尚未经过最终审计"],
            "next_actions": ["复核模型假设"],
        }
        message = self._command_message("agent.handoff.create", payload, 1, "gateway-handoff-create-001")
        first = self.gateway.receive(self.context, message)
        self.assertEqual(first.message_type, "gateway.ack")
        handoff = first.payload["command_result"]["result"]
        self.assertEqual(handoff["receipt_status"], "PENDING")
        self.assertEqual(handoff["sender_agent_id"], self.agent.agent_id)

        repeated = self.gateway.receive(self.context, message)
        self.assertTrue(repeated.payload["duplicate"])
        self.assertEqual(repeated.payload["command_result"], first.payload["command_result"])

        accepted = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.handoff.accept",
                {
                    "project_id": str(self.project.id),
                    "project_token": self.project_credential.project_token,
                    "handoff_id": handoff["id"],
                },
                2,
                "gateway-handoff-accept-001",
            ),
        )
        self.assertEqual(accepted.message_type, "gateway.ack")
        self.assertEqual(accepted.payload["command_result"]["result"]["receipt_status"], "ACCEPTED")

    def test_gateway_agent_review_can_block_but_cannot_approve(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="Gateway review task"))
        self.store.update_task(task.id, TaskStatus.RUNNING, None, None)
        self.store.update_task(task.id, TaskStatus.WAITING_REVIEW, None, None)
        blocked = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.review.submit",
                {
                    "project_id": str(self.project.id),
                    "project_token": self.project_credential.project_token,
                    "target_type": "task",
                    "target_id": str(task.id),
                    "verdict": "NEEDS_REVISION",
                    "summary": "需要补充边界审计",
                    "findings": [{"severity": "major", "text": "缺少输入文件清单"}],
                },
                1,
                "gateway-review-submit-001",
            ),
        )
        self.assertEqual(blocked.message_type, "gateway.ack")
        review = blocked.payload["command_result"]["result"]
        self.assertEqual(review["reviewer"], self.agent.agent_id)
        self.assertEqual(review["reviewer_kind"], "agent")
        self.assertEqual(self.store.get_task(task.id).status, "NEEDS_REVISION")

        approval = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.review.submit",
                {
                    "project_id": str(self.project.id),
                    "project_token": self.project_credential.project_token,
                    "target_type": "task",
                    "target_id": str(task.id),
                    "verdict": "APPROVED",
                    "summary": "Agent cannot approve",
                },
                2,
                "gateway-review-approve-001",
            ),
        )
        self.assertEqual(approval.message_type, "gateway.error")
        self.assertEqual(approval.payload["code"], "human_approval_required")

    def test_gateway_receiver_can_reject_handoff_and_duplicate_replays_decision(self) -> None:
        task = self.store.create_task(self.project.id, TaskCreate(title="Gateway reject handoff"))
        created = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.handoff.create",
                {
                    "project_id": str(self.project.id),
                    "project_token": self.project_credential.project_token,
                    "task_id": str(task.id),
                    "receiver": {"type": "agent", "id": self.agent.agent_id},
                    "objective": "待接收的结果",
                },
                1,
                "gateway-reject-create-001",
            ),
        )
        handoff_id = created.payload["command_result"]["result"]["id"]
        rejected = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.handoff.reject",
                {
                    "project_id": str(self.project.id),
                    "project_token": self.project_credential.project_token,
                    "handoff_id": handoff_id,
                    "reason": "需要补充输入范围",
                    "findings": [{"severity": "major", "code": "scope", "text": "输入范围未声明"}],
                },
                2,
                "gateway-reject-001",
            ),
        )
        self.assertEqual(rejected.message_type, "gateway.ack")
        result = rejected.payload["command_result"]["result"]
        self.assertEqual(result["receipt_status"], "REJECTED")
        repeated = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.handoff.reject",
                {
                    "project_id": str(self.project.id),
                    "project_token": self.project_credential.project_token,
                    "handoff_id": handoff_id,
                    "reason": "需要补充输入范围",
                    "findings": [{"severity": "major", "code": "scope", "text": "输入范围未声明"}],
                },
                3,
                "gateway-reject-002",
            ),
        )
        self.assertEqual(repeated.message_type, "gateway.ack")
        self.assertEqual(repeated.payload["command_result"]["result"]["receipt_status"], "REJECTED")

    def test_agent_runtime_event_is_project_scoped_idempotent_and_completes_run(self) -> None:
        created = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.run.create",
                {"project_id": str(self.project.id), "project_token": self.project_credential.project_token},
                1,
                "gateway-event-run-create-001",
            ),
        )
        run = created.payload["command_result"]["result"]
        event = SessionIpcEvent(
            event_id="runtime-event-001",
            worker_id="worker-gateway-001",
            event_type="run.completed",
            sequence=1,
            project_id=str(self.project.id),
            run_id=run["id"],
            occurred_at=datetime.now(UTC),
            payload={"summary": "runtime completed", "exit_code": 0},
        )
        message = self._command_message(
            "agent.event",
            {
                "project_id": str(self.project.id),
                "project_token": self.project_credential.project_token,
                "event": event.model_dump(mode="json"),
            },
            2,
            "gateway-runtime-event-001",
        )
        first = self.gateway.receive(self.context, message)
        self.assertEqual(first.message_type, "gateway.ack")
        self.assertEqual(self.store.get_run(UUID(run["id"])).status, "SUCCEEDED")

        duplicate = self.gateway.receive(self.context, message)
        self.assertEqual(duplicate.message_type, "gateway.ack")
        self.assertTrue(duplicate.payload["duplicate"])
        runtime_events = [event for event in self.store.list_events(self.project.id) if event.event_type == "run.completed"]
        self.assertEqual(len(runtime_events), 1)

    def test_agent_runtime_event_rejects_missing_project_capability(self) -> None:
        event = SessionIpcEvent(
            event_id="runtime-event-002",
            worker_id="worker-gateway-001",
            event_type="process.stdout",
            sequence=1,
            project_id=str(self.project.id),
            run_id=None,
            occurred_at=datetime.now(UTC),
            payload={"text": "hello"},
        )
        response = self.gateway.receive(
            self.context,
            self._command_message(
                "agent.event",
                {"project_id": str(self.project.id), "project_token": "invalid-runtime-token", "event": event.model_dump(mode="json")},
                1,
                "gateway-runtime-event-002",
            ),
        )
        self.assertEqual(response.message_type, "gateway.error")
        self.assertEqual(response.payload["code"], "device_project_token_invalid")

    def test_fastapi_gateway_route_authenticates_and_closes_connection_cleanly(self) -> None:
        message = self._heartbeat_message(1)
        message["session_id"] = "session-route-001"
        message["connection_id"] = "connection-route-001"
        message["payload"]["session_id"] = "session-route-001"
        message["payload"]["connection_id"] = "connection-route-001"
        websocket = FakeGatewayWebSocket(self.credential.device_token, [message])
        with patch.object(main, "gateway", self.gateway), patch.object(main, "store", self.store):
            asyncio.run(main.agent_gateway(websocket, self.credential.device.device_id))
        self.assertTrue(websocket.accepted)
        self.assertIsNone(websocket.closed_code)
        self.assertEqual([item["message_type"] for item in websocket.sent], ["gateway.connected", "gateway.ack"])
        self.assertEqual(self.store.get_agent_connection("connection-route-001").status, "DISCONNECTED")


if __name__ == "__main__":
    unittest.main()

class AgentRuntimeEventTests(unittest.TestCase):
    """执行体过程事件（process.started / agent.message / tool.completed / process.exited）。

    这条通道在 DP-2 之前一直没被用过：协议与 Gateway 早就支持 `agent.event`，
    但 worker 从不发，于是平台侧只能看到"领取 → 结果"两帧。这里把它钉住：
    **过程事件必须被接受、必须落成平台事件、且终态不由它完成 Run**（完成 Run 是 HTTP 的职责）。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.agent = self.store.register_agent(AgentRegister(agent_id="event-agent", display_name="Event Agent"))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        self.credential = self.store.register_device(
            registration_request(pairing, Ed25519PrivateKey.generate(), self.agent.agent_id, "device-event-001")
        )
        self.project = self.store.list_projects()[0]
        self.grant = self.store.create_device_project_grant(
            self.project.id,
            DeviceProjectGrantCreate(device_id=self.credential.device.device_id),
            "member-001",
        )
        self.gateway = GatewayService(self.store)
        self.context = self.gateway.authenticate(
            self.credential.device_token,
            device_id=self.credential.device.device_id,
            session_id="session-event-001",
            connection_id="connection-event-001",
        )
        self.sequence = 0

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _send(self, event_type: str, payload: dict, run_id: str | None = None) -> dict:
        self.sequence += 1
        envelope = GatewayEnvelope(
            device_id=self.credential.device.device_id,
            agent_id=self.agent.agent_id,
            session_id="session-event-001",
            connection_id="connection-event-001",
            sequence=self.sequence,
            message_id=f"message-{self.sequence:04d}",
            idempotency_key=f"idempotency-key-{self.sequence:04d}",
            message_type="agent.event",
            sent_at=datetime.now(UTC),
            payload={
                "project_id": str(self.project.id),
                "project_token": self.grant.project_token,
                "event": {
                    "event_id": f"event-{self.sequence:04d}",
                    "worker_id": "daemon-test",
                    "event_type": event_type,
                    "sequence": self.sequence,
                    "project_id": str(self.project.id),
                    "run_id": run_id,
                    "occurred_at": datetime.now(UTC).isoformat(),
                    "payload": payload,
                },
            },
        )
        return self.gateway.receive(self.context, envelope.model_dump(mode="json")).model_dump(mode="json")

    def test_progress_events_are_accepted_and_persisted(self) -> None:
        for event_type, payload in (
            ("process.started", {"executor": "codex"}),
            ("agent.message", {"text": "先看题面"}),
            ("tool.completed", {"tool": "shell"}),
            ("process.exited", {"exit_code": 0, "summary": "done"}),
        ):
            with self.subTest(event_type=event_type):
                response = self._send(event_type, payload)
                self.assertEqual(response["message_type"], "gateway.ack")

        stored = {event.event_type for event in self.store.list_events(self.project.id)}
        for event_type in ("agent.process.started", "agent.agent.message", "agent.tool.completed", "agent.process.exited"):
            self.assertIn(event_type, stored)

    def test_events_carry_the_task_payload_for_the_timeline(self) -> None:
        self._send("agent.message", {"text": "结论：用线性规划", "task_id": "task-1"})
        event = next(item for item in self.store.list_events(self.project.id) if item.event_type == "agent.agent.message")
        self.assertEqual(event.payload["event"]["payload"]["text"], "结论：用线性规划")
        self.assertEqual(event.actor_kind, "agent")  # 过程事件的发起方是设备上的执行体

    def test_project_token_is_required_and_removed_from_the_stored_event(self) -> None:
        response = self._send("agent.message", {"text": "x"})
        self.assertEqual(response["message_type"], "gateway.ack")
        event = next(item for item in self.store.list_events(self.project.id) if item.event_type == "agent.agent.message")
        self.assertNotIn("project_token", json.dumps(event.payload))


if __name__ == "__main__":
    unittest.main()
