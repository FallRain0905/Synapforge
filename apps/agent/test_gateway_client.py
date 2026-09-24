from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

try:
    from packages.agent_protocol import AgentEventPayload, GatewayEnvelope, GatewayEventAck, GatewayReplayRequest, SessionIpcEvent
except ModuleNotFoundError:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from packages.agent_protocol import AgentEventPayload, GatewayEnvelope, GatewayEventAck, GatewayReplayRequest, SessionIpcEvent
from gateway_client import DurableGatewayClient, GatewayIdentity
from local_state import LocalAgentState


class DurableGatewayClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = LocalAgentState(Path(self.temp_dir.name) / "agent.db")
        self.identity = GatewayIdentity(
            device_id="device-001",
            agent_id="agent-001",
            session_id="session-001",
            connection_id="connection-001",
        )
        self.client = DurableGatewayClient(self.state, self.identity)

    def tearDown(self) -> None:
        self.state.close()
        self.temp_dir.cleanup()

    def _server_frame(self, message_type: str, payload: dict, sequence: int = 1) -> dict:
        return GatewayEnvelope(
            device_id=self.identity.device_id,
            agent_id=self.identity.agent_id,
            session_id=self.identity.session_id,
            connection_id=self.identity.connection_id,
            sequence=sequence,
            message_id=f"server-message-{sequence}",
            idempotency_key=f"server-idempotency-{sequence}",
            message_type=message_type,
            sent_at=datetime.now(UTC),
            payload=payload,
        ).model_dump(mode="json")

    def test_pending_event_is_enveloped_and_ack_removes_it(self) -> None:
        event = self.client.queue_event(
            "agent.event",
            {"run_id": "run-001"},
            idempotency_key="client-event-001",
            message_id="client-message-001",
        )
        envelopes = self.client.pending_envelopes()
        self.assertEqual(len(envelopes), 1)
        self.assertEqual(envelopes[0].sequence, event["sequence"])
        self.assertEqual(self.state.get_outbox_event(1)["status"], "SENT")
        ack = GatewayEventAck(
            device_id=self.identity.device_id,
            connection_id=self.identity.connection_id,
            highest_contiguous_sequence=1,
            acknowledged_message_ids=["client-message-001"],
            received_at=datetime.now(UTC),
        )
        result = self.client.handle_server_message(self._server_frame("gateway.ack", ack.model_dump(mode="json")))
        self.assertEqual(result["acked_count"], 1)
        self.assertEqual(self.state.get_outbox_event(1)["status"], "ACKED")
        self.assertFalse(self.state.pending_events())

    def test_replay_request_requeues_sent_events_after_the_confirmed_prefix(self) -> None:
        self.client.queue_event("agent.event", {"index": 1}, idempotency_key="client-event-001")
        self.client.queue_event("agent.event", {"index": 2}, idempotency_key="client-event-002")
        self.client.pending_envelopes()
        replay = GatewayReplayRequest(
            device_id=self.identity.device_id,
            connection_id=self.identity.connection_id,
            after_sequence=0,
            limit=1000,
        )
        result = self.client.handle_server_message(self._server_frame("gateway.replay_required", replay.model_dump(mode="json")))
        self.assertEqual(result["requeued_count"], 2)
        self.assertEqual([item["sequence"] for item in self.state.pending_events()], [1, 2])

    def test_server_identity_mismatch_is_rejected(self) -> None:
        frame = self._server_frame("gateway.connected", {"connection": {}})
        frame["device_id"] = "other-device"
        with self.assertRaisesRegex(PermissionError, "gateway_server_identity_mismatch"):
            self.client.handle_server_message(frame)

    def test_command_result_is_returned_from_ack(self) -> None:
        self.client.queue_event(
            "agent.task.claim",
            {"project_id": "project-001", "project_token": "prj-test-token"},
            idempotency_key="client-command-001",
            message_id="client-command-message-001",
        )
        self.client.pending_envelopes()
        ack = GatewayEventAck(
            device_id=self.identity.device_id,
            connection_id=self.identity.connection_id,
            highest_contiguous_sequence=1,
            acknowledged_message_ids=["client-command-message-001"],
            received_at=datetime.now(UTC),
        ).model_dump(mode="json")
        ack["command_result"] = {
            "message_type": "agent.task.claim",
            "result": {"task": {"id": "task-001"}, "lease": {"lease_token": "lease-001"}},
        }
        result = self.client.handle_server_message(self._server_frame("gateway.ack", ack))
        self.assertEqual(result["command_result"]["message_type"], "agent.task.claim")
        self.assertEqual(result["command_result"]["result"]["lease"]["lease_token"], "lease-001")

    def test_runtime_event_uploader_builds_project_scoped_durable_payload(self) -> None:
        from gateway_client import RuntimeEventUploader

        uploader = RuntimeEventUploader(self.client)
        uploader.bind_project("11111111-1111-1111-1111-111111111111", "project-token-123456")
        event = SessionIpcEvent(
            event_id="runtime-client-event-001",
            worker_id="worker-client-001",
            event_type="process.stdout",
            sequence=1,
            project_id="11111111-1111-1111-1111-111111111111",
            run_id=None,
            occurred_at=datetime.now(UTC),
            payload={"text": "hello"},
        )
        queued = uploader.enqueue(event)
        payload = AgentEventPayload.model_validate(queued["payload"])
        self.assertEqual(str(payload.project_id), event.project_id)
        self.assertEqual(payload.event.event_id, event.event_id)
        self.assertEqual(queued["message_id"], event.event_id)


if __name__ == "__main__":
    unittest.main()

class IdentityRestampTests(unittest.TestCase):
    """入队时盖的身份可能过期（每个会话都换 connection_id）：发送时必须按当前身份重盖。

    真实事故：积压的心跳 payload 里是旧 connection_id，平台判 gateway_identity_mismatch
    拒绝整帧 → 序号没被记录 → 之后每一条都被当成"序号缺口"，事件永远发不出去。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = LocalAgentState(Path(self.temp_dir.name) / "agent.db")
        self.client = DurableGatewayClient(
            self.state,
            GatewayIdentity(device_id="device-1", agent_id="agent-1", session_id="session-1", connection_id="connection-old"),
        )

    def tearDown(self) -> None:
        self.state.close()
        self.temp_dir.cleanup()

    def test_heartbeat_payload_is_restamped_on_send(self) -> None:
        self.client.queue_event(
            "agent.heartbeat",
            {
                "device_id": "device-1",
                "agent_id": "agent-1",
                "session_id": "session-1",
                "connection_id": "connection-old",
                "agent_version": "0.1.0",
            },
            idempotency_key="heartbeat-key-1",
        )
        # 会话轮换：换一个 connection_id
        self.client.identity = GatewayIdentity(
            device_id="device-1", agent_id="agent-1", session_id="session-1", connection_id="connection-new"
        )
        envelope = self.client.pending_envelopes(limit=10)[0]
        self.assertEqual(envelope.connection_id, "connection-new")
        self.assertEqual(envelope.payload["connection_id"], "connection-new")
        self.assertEqual(envelope.payload["agent_id"], "agent-1")

    def test_payloads_without_identity_fields_are_untouched(self) -> None:
        self.client.queue_event("agent.event", {"event": {"event_type": "agent.message"}}, idempotency_key="event-key-1")
        envelope = self.client.pending_envelopes(limit=10)[0]
        self.assertEqual(envelope.payload, {"event": {"event_type": "agent.message"}})


if __name__ == "__main__":
    unittest.main()
