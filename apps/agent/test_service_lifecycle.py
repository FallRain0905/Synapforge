from __future__ import annotations

import unittest

from service_lifecycle import MachineServiceLifecycle


class MachineServiceLifecycleTests(unittest.TestCase):
    def test_start_discovers_session_and_starts_worker(self) -> None:
        lifecycle = MachineServiceLifecycle("worker-001")
        result = lifecycle.apply("service.start", reason="boot")
        self.assertTrue(result.accepted)
        self.assertEqual(result.state, "WAITING_FOR_SESSION")
        self.assertEqual(result.actions[0].kind, "discover_session")

        result = lifecycle.apply("session.discovered", session_id="session-001")
        self.assertEqual(result.state, "STARTING_WORKER")
        self.assertEqual(result.actions[0].kind, "start_worker")
        result = lifecycle.apply("worker.ready")
        self.assertEqual(result.state, "WORKER_READY")
        self.assertEqual(result.transition.event_type, "worker.ready")
        self.assertTrue(lifecycle.can_start_run())

    def test_lock_blocks_desktop_but_not_user_session_and_logout_cleans_up(self) -> None:
        lifecycle = MachineServiceLifecycle("worker-001", "session-001")
        lifecycle.session_state = "logged_in"
        lifecycle.state = "WORKER_READY"
        self.assertEqual(lifecycle.apply("run.started", run_id="run-001").state, "WORKER_READY")
        result = lifecycle.apply("session.locked", reason="screen_locked")
        self.assertEqual(result.session_state, "locked")
        self.assertFalse(lifecycle.can_start_run(execution_mode="INTERACTIVE_DESKTOP"))
        self.assertTrue(lifecycle.can_start_run(execution_mode="USER_SESSION"))
        result = lifecycle.apply("session.logged_out", reason="user_logoff")
        self.assertEqual(result.state, "WAITING_FOR_SESSION")
        self.assertEqual(result.active_run_ids, ())
        self.assertEqual([action.kind for action in result.actions], ["stop_runs", "stop_worker"])
        self.assertIsNone(lifecycle.user_session_id)

    def test_worker_failure_is_degraded_and_recovery_is_explicit(self) -> None:
        lifecycle = MachineServiceLifecycle("worker-001", "session-001", state="WORKER_READY", session_state="logged_in")
        lifecycle.active_run_ids.add("run-001")
        result = lifecycle.apply("worker.exited", reason="process_lost")
        self.assertEqual(result.state, "DEGRADED")
        self.assertEqual(result.transition.event_type, "worker.exited")
        self.assertEqual(result.actions[0].kind, "stop_runs")
        result = lifecycle.apply("recovery.requested", reason="operator_retry")
        self.assertEqual(result.state, "STARTING_WORKER")
        self.assertEqual(result.actions[0].kind, "start_worker")

    def test_logged_out_session_discovery_and_worker_ready_race_are_rejected(self) -> None:
        lifecycle = MachineServiceLifecycle("worker-001")
        lifecycle.apply("service.start")
        result = lifecycle.apply("session.discovered", session_id="session-001", observed_session_state="logged_out")
        self.assertEqual(result.state, "WAITING_FOR_SESSION")
        self.assertEqual(result.actions[0].kind, "discover_session")
        ready = lifecycle.apply("worker.ready")
        self.assertFalse(ready.accepted)
        self.assertEqual(ready.error_code, "lifecycle_worker_ready_not_expected")

        lifecycle.apply("session.logged_in", session_id="session-002")
        self.assertEqual(lifecycle.state, "STARTING_WORKER")
        lifecycle.session_state = "logged_out"
        ready = lifecycle.apply("worker.ready")
        self.assertFalse(ready.accepted)
        self.assertEqual(ready.error_code, "lifecycle_session_unavailable")

    def test_service_stop_in_waiting_state_has_no_worker_stop_action(self) -> None:
        lifecycle = MachineServiceLifecycle("worker-001")
        lifecycle.apply("service.start")
        result = lifecycle.apply("service.stop")
        self.assertEqual(result.state, "STOPPING")
        self.assertEqual([action.kind for action in result.actions], ["stop_runs"])

    def test_emergency_stop_is_terminal_until_operator_clears_external_flag(self) -> None:
        lifecycle = MachineServiceLifecycle("worker-001", "session-001", state="WORKER_READY", session_state="logged_in")
        lifecycle.active_run_ids.add("run-001")
        result = lifecycle.apply("emergency.stop", reason="local_button")
        self.assertEqual(result.state, "EMERGENCY_STOPPED")
        self.assertEqual(result.actions[0].run_ids, ("run-001",))
        rejected = lifecycle.apply("service.start")
        self.assertFalse(rejected.accepted)
        self.assertEqual(rejected.error_code, "lifecycle_service_already_started")

    def test_invalid_transition_does_not_mutate_state(self) -> None:
        lifecycle = MachineServiceLifecycle("worker-001")
        result = lifecycle.apply("worker.ready")
        self.assertFalse(result.accepted)
        self.assertEqual(result.state, "STOPPED")
        self.assertEqual(lifecycle.poll_transitions(), [])


if __name__ == "__main__":
    unittest.main()
