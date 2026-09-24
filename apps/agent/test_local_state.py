from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from packages.agent_protocol import SessionLifecycleTransition
from datetime import UTC, datetime

from local_state import LocalAgentState


class LocalAgentStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = LocalAgentState(Path(self.temp_dir.name) / "agent.db")

    def tearDown(self) -> None:
        self.state.close()
        self.temp_dir.cleanup()

    def test_event_sequence_and_idempotency_survive_reopen(self) -> None:
        first = self.state.enqueue_event(
            "agent.event",
            {"run_id": "run-1", "value": 1},
            idempotency_key="local-event-001",
            message_id="local-message-001",
        )
        replay = self.state.enqueue_event(
            "agent.event",
            {"different": True},
            idempotency_key="local-event-001",
            message_id="local-message-different",
        )
        second = self.state.enqueue_event(
            "agent.event",
            {"run_id": "run-2"},
            idempotency_key="local-event-002",
        )
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(replay["sequence"], first["sequence"])
        self.assertEqual(replay["payload"], first["payload"])
        self.assertEqual(second["sequence"], 2)
        self.state.mark_sent(1)
        self.state.close()

        reopened = LocalAgentState(Path(self.temp_dir.name) / "agent.db")
        try:
            self.assertEqual(reopened.get_outbox_event(1)["status"], "SENT")
            self.assertEqual(reopened.get_outbox_event(2)["sequence"], 2)
            self.assertEqual(reopened.recover_unacked(), 1)
            self.assertEqual(reopened.pending_events()[0]["sequence"], 1)
        finally:
            reopened.close()
            self.state = LocalAgentState(Path(self.temp_dir.name) / "agent.db")

    def test_ack_and_replay_update_only_unconfirmed_events(self) -> None:
        for index in range(1, 4):
            self.state.enqueue_event(
                "agent.event",
                {"index": index},
                idempotency_key=f"local-event-{index:03d}",
            )
        self.state.mark_sent(1)
        self.state.mark_sent(2)
        self.state.mark_sent(3)
        self.assertEqual(self.state.acknowledge(1), 1)
        self.assertEqual(self.state.get_outbox_event(1)["status"], "ACKED")
        self.assertEqual(self.state.get_outbox_event(2)["status"], "SENT")
        self.assertEqual(self.state.requeue_after(1), 2)
        self.assertEqual([item["sequence"] for item in self.state.pending_events()], [2, 3])
        self.assertEqual(self.state.acknowledge(3), 2)
        self.assertFalse(self.state.pending_events())

    def test_renumber_pending_events_aligns_the_new_connection(self) -> None:
        """重连时平台要求序列从 1 开始：未确认事件保序重编号、已确认事件清掉。"""

        for index in range(1, 6):
            self.state.enqueue_event("agent.heartbeat", {"index": index}, idempotency_key=f"event-{index:03d}")
        self.state.mark_sent(1)
        self.state.mark_sent(2)
        self.state.acknowledge(2)

        result = self.state.renumber_pending_events()
        self.assertEqual(result, {"renumbered": 3, "dropped_acked": 2})
        pending = self.state.pending_events(limit=10)
        self.assertEqual([item["sequence"] for item in pending], [1, 2, 3])
        # 内容和幂等键保持不变：平台侧的幂等判定仍然有效
        self.assertEqual([item["payload"]["index"] for item in pending], [3, 4, 5])
        self.assertEqual(pending[0]["idempotency_key"], "event-003")
        # 新事件接着 4 号继续，不会撞主键
        fresh = self.state.enqueue_event("agent.heartbeat", {"index": 99}, idempotency_key="event-new")
        self.assertEqual(fresh["sequence"], 4)

    def test_renumber_pending_events_on_empty_outbox_is_a_noop(self) -> None:
        self.assertEqual(self.state.renumber_pending_events(), {"renumbered": 0, "dropped_acked": 0})
        self.assertEqual(self.state.enqueue_event("agent.heartbeat", {}, idempotency_key="first")["sequence"], 1)

    def test_run_upload_and_approval_state_are_durable(self) -> None:
        self.state.save_run_state("run-001", "RUNNING", {"task_id": "task-001"})
        upload_id = self.state.save_upload("C:/workspace/result.json", artifact_id="artifact-001", content_hash="abc")
        self.state.save_approval("approval-001", "PENDING", {"target": "artifact-001"})
        self.state.close()

        reopened = LocalAgentState(Path(self.temp_dir.name) / "agent.db")
        try:
            run = reopened.db.execute("SELECT status, payload FROM run_states WHERE run_id = 'run-001'").fetchone()
            upload = reopened.db.execute("SELECT upload_id, status FROM pending_uploads WHERE upload_id = ?", (upload_id,)).fetchone()
            approval = reopened.db.execute("SELECT status, payload FROM approval_states WHERE approval_id = 'approval-001'").fetchone()
            self.assertEqual(run["status"], "RUNNING")
            self.assertIn("task-001", run["payload"])
            self.assertEqual(upload["status"], "PENDING")
            self.assertEqual(approval["status"], "PENDING")
            self.assertIn("artifact-001", approval["payload"])
        finally:
            reopened.close()
            self.state = LocalAgentState(Path(self.temp_dir.name) / "agent.db")

    def test_lifecycle_transitions_are_idempotent_and_queryable(self) -> None:
        transition = SessionLifecycleTransition(
            transition_id="transition-001",
            worker_id="worker-001",
            user_session_id="session-001",
            event_type="emergency.stop",
            from_state="WORKER_READY",
            to_state="EMERGENCY_STOPPED",
            session_state="logged_in",
            active_run_ids=["run-001"],
            cleanup_run_ids=["run-001"],
            reason="local_button",
            occurred_at=datetime.now(UTC),
            metadata={"signal": "emergency.stop"},
        )
        self.state.save_lifecycle_transition(transition)
        self.state.save_lifecycle_transition(transition)
        rows = self.state.list_lifecycle_transitions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["active_run_ids"], ["run-001"])
        self.assertEqual(rows[0]["cleanup_run_ids"], ["run-001"])
        self.assertEqual(rows[0]["metadata"]["signal"], "emergency.stop")


if __name__ == "__main__":
    unittest.main()
