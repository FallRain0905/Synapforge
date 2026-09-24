from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
import json

from pydantic import ValidationError
from packages.agent_protocol import SESSION_IPC_MESSAGE_TYPES, SessionIpcRequest, SessionPeer, SessionRunContext
from session_worker import SessionPolicyError, SessionWorkerBroker, SessionWorkerPolicy


class SessionWorkerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = SessionWorkerBroker(SessionWorkerPolicy(approval_ttl_seconds=60))
        self.worker = self.broker.register_worker(
            "worker-001",
            "session-001",
            "S-1-5-21-user",
            supported_execution_modes=("USER_SESSION", "INTERACTIVE_DESKTOP"),
            capabilities=("session.run", "terminal.input", "desktop.control"),
        )
        self.worker_peer = SessionPeer(
            peer_id="worker-001",
            peer_kind="user_session_worker",
            process_id=101,
            user_sid="S-1-5-21-user",
        )
        self.machine_peer = SessionPeer(
            peer_id="machine-001",
            peer_kind="machine_service",
            process_id=100,
            user_sid=None,
        )
        self.ui_peer = SessionPeer(
            peer_id="ui-001",
            peer_kind="desktop_ui",
            process_id=102,
            user_sid="S-1-5-21-user",
        )
        self._mark_logged_in()

    def _request(self, message_type: str, peer: SessionPeer, **kwargs: object) -> SessionIpcRequest:
        if message_type == "session.run.start" and "payload" not in kwargs:
            kwargs["payload"] = {"adapter_id": "test-adapter", "command": ["python", "-c", "pass"]}
        return SessionIpcRequest(
            request_id=kwargs.pop("request_id", f"request-{message_type.replace('.', '-')}-001"),
            idempotency_key=kwargs.pop("idempotency_key", f"idempotency-{message_type.replace('.', '-')}-001"),
            message_type=message_type,
            peer=peer,
            worker_id="worker-001",
            user_session_id=kwargs.pop("user_session_id", "session-001"),
            sent_at=kwargs.pop("sent_at", datetime.now(UTC)),
            **kwargs,
        )

    def _mark_logged_in(self) -> None:
        request = self._request(
            "session.status",
            self.worker_peer,
            user_session_id=None,
            payload={"session_state": "logged_in"},
            request_id="status-request-001",
            idempotency_key="status-idempotency-001",
        )
        response = self.broker.handle(request, self.worker_peer)
        self.assertEqual(response.status, "COMPLETED")

    def context(self, mode: str = "USER_SESSION", *, terminal: bool = False, desktop: bool = False) -> SessionRunContext:
        return SessionRunContext(
            project_id="project-001",
            task_id="task-001",
            run_id="run-001",
            agent_id="agent-001",
            device_id="device-001",
            workspace_id="workspace-001",
            workspace_path="C:\\workspace\\project-001",
            execution_mode=mode,
            allow_remote_terminal=terminal,
            allow_desktop_control=desktop,
        )

    def test_machine_service_can_start_and_stop_user_session_run(self) -> None:
        start = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-001",
            idempotency_key="start-idempotency-001",
        )
        response = self.broker.handle(start, self.machine_peer)
        self.assertEqual(response.status, "ACCEPTED")
        self.assertEqual(self.worker.state, "RUNNING")
        self.assertEqual(self.worker.active_run_ids, {"run-001"})

        stop = self._request(
            "session.run.stop",
            self.machine_peer,
            context=self.context(),
            request_id="stop-request-001",
            idempotency_key="stop-idempotency-001",
        )
        response = self.broker.handle(stop, self.machine_peer)
        self.assertEqual(response.status, "COMPLETED")
        self.assertEqual(self.worker.state, "READY")
        self.assertEqual(self.worker.active_run_ids, set())

    def test_worker_status_requires_authenticated_worker_and_matching_sid(self) -> None:
        wrong_sid = SessionPeer(peer_id="worker-001", peer_kind="user_session_worker", process_id=101, user_sid="S-1-5-21-other")
        request = self._request(
            "session.status",
            wrong_sid,
            user_session_id=None,
            payload={"session_state": "logged_out"},
            request_id="status-request-002",
            idempotency_key="status-idempotency-002",
        )
        response = self.broker.handle(request, wrong_sid)
        self.assertEqual(response.status, "REJECTED")
        self.assertEqual(response.error_code, "session_worker_user_sid_mismatch")

        machine_status = self._request(
            "session.status",
            self.machine_peer,
            user_session_id=None,
            payload={"session_state": "logged_out"},
            request_id="status-request-003",
            idempotency_key="status-idempotency-003",
        )
        response = self.broker.handle(machine_status, self.machine_peer)
        self.assertEqual(response.error_code, "session_status_worker_only")

        other_worker = SessionPeer(
            peer_id="worker-002",
            peer_kind="user_session_worker",
            process_id=103,
            user_sid="S-1-5-21-user",
        )
        other_status = self._request(
            "session.status",
            other_worker,
            user_session_id=None,
            payload={"session_state": "logged_in"},
            request_id="status-request-004",
            idempotency_key="status-idempotency-004",
        )
        response = self.broker.handle(other_status, other_worker)
        self.assertEqual(response.error_code, "session_worker_peer_identity_mismatch")

    def test_machine_service_identity_does_not_require_interactive_user_sid(self) -> None:
        self.assertIsNone(self.machine_peer.user_sid)
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-service-identity-001",
            idempotency_key="start-idempotency-service-identity-001",
        )
        response = self.broker.handle(request, self.machine_peer)
        self.assertEqual(response.status, "ACCEPTED")

    def test_authenticated_process_id_must_match_request_peer(self) -> None:
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-process-identity-001",
            idempotency_key="start-idempotency-process-identity-001",
        )
        authenticated_peer = SessionPeer(
            peer_id=self.machine_peer.peer_id,
            peer_kind=self.machine_peer.peer_kind,
            process_id=999,
            user_sid=None,
        )
        response = self.broker.handle(request, authenticated_peer)
        self.assertEqual(response.error_code, "session_process_identity_mismatch")

    def test_authenticated_windows_session_id_must_match_request_peer(self) -> None:
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-windows-session-001",
            idempotency_key="start-idempotency-windows-session-001",
        )
        authenticated_peer = SessionPeer(
            peer_id=self.machine_peer.peer_id,
            peer_kind=self.machine_peer.peer_kind,
            process_id=None,
            windows_session_id=0,
            user_sid=None,
        )
        response = self.broker.handle(request, authenticated_peer)
        self.assertEqual(response.error_code, "session_windows_session_identity_mismatch")

    def test_run_lifecycle_requires_session_run_capability(self) -> None:
        restricted = SessionWorkerBroker(SessionWorkerPolicy())
        worker = restricted.register_worker(
            "worker-002",
            "session-002",
            "S-1-5-21-user",
            supported_execution_modes=("USER_SESSION",),
            capabilities=("terminal.input",),
        )
        worker.session_state = "logged_in"
        peer = SessionPeer(
            peer_id="machine-002",
            peer_kind="machine_service",
            process_id=104,
            user_sid="S-1-5-21-user",
        )
        request = SessionIpcRequest(
            request_id="start-request-no-cap-001",
            idempotency_key="start-idempotency-no-cap-001",
            message_type="session.run.start",
            peer=peer,
            worker_id="worker-002",
            user_session_id="session-002",
            context=self.context(),
            sent_at=datetime.now(UTC),
        )
        response = restricted.handle(request, peer)
        self.assertEqual(response.error_code, "session_run_capability_missing")
        self.assertEqual(worker.active_run_ids, set())

    def test_status_update_preserves_running_state(self) -> None:
        start = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-status-001",
            idempotency_key="start-idempotency-status-001",
        )
        self.assertEqual(self.broker.handle(start, self.machine_peer).status, "ACCEPTED")
        status = self._request(
            "session.status",
            self.worker_peer,
            user_session_id=None,
            payload={"session_state": "locked"},
            request_id="status-request-running-001",
            idempotency_key="status-idempotency-running-001",
        )
        response = self.broker.handle(status, self.worker_peer)
        self.assertEqual(response.status, "COMPLETED")
        self.assertEqual(self.worker.state, "RUNNING")

    def test_session_schema_matches_protocol_message_types(self) -> None:
        schema_path = Path(__file__).resolve().parents[2] / "packages" / "agent_protocol" / "session.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(
            schema["$defs"]["SessionIpcRequest"]["properties"]["message_type"]["enum"],
            list(SESSION_IPC_MESSAGE_TYPES),
        )
        self.assertEqual(
            set(schema["$defs"]),
            {"SessionPeer", "SessionRunContext", "SessionRunStartPayload", "TerminalSize", "SessionLifecycleTransition", "SessionIpcRequest", "SessionIpcResponse", "SessionIpcEvent"},
        )

    def test_terminal_input_requires_capability_and_explicit_context_permission(self) -> None:
        with self.assertRaisesRegex(ValidationError, "session_remote_terminal_not_allowed"):
            self._request(
                "session.terminal.input",
                self.machine_peer,
                context=self.context(terminal=False),
                payload={"input": "yes"},
                request_id="terminal-request-001",
                idempotency_key="terminal-idempotency-001",
            )

        start = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-terminal-001",
            idempotency_key="start-idempotency-terminal-001",
        )
        self.assertEqual(self.broker.handle(start, self.machine_peer).status, "ACCEPTED")

        allowed = self._request(
            "session.terminal.input",
            self.machine_peer,
            context=self.context(terminal=True),
            payload={"input": "yes"},
            request_id="terminal-request-002",
            idempotency_key="terminal-idempotency-002",
        )
        response = self.broker.handle(allowed, self.machine_peer)
        self.assertEqual(response.status, "ACCEPTED")

    def test_desktop_control_requires_ui_approval_and_unlocked_session(self) -> None:
        desktop_context = self.context("INTERACTIVE_DESKTOP", desktop=True)
        start = self._request(
            "session.run.start",
            self.machine_peer,
            context=desktop_context,
            request_id="start-request-desktop-001",
            idempotency_key="start-idempotency-desktop-001",
        )
        self.assertEqual(self.broker.handle(start, self.machine_peer).status, "ACCEPTED")
        approval = self._request(
            "session.approval.respond",
            self.ui_peer,
            user_session_id=None,
            approval_id="approval-001",
            payload={"decision": "APPROVED"},
            request_id="approval-request-001",
            idempotency_key="approval-idempotency-001",
        )
        self.assertEqual(self.broker.handle(approval, self.ui_peer).status, "COMPLETED")
        desktop = self._request(
            "session.desktop.control",
            self.machine_peer,
            context=desktop_context,
            approval_id="approval-001",
            payload={"action": "focus_window"},
            request_id="desktop-request-001",
            idempotency_key="desktop-idempotency-001",
        )
        self.assertEqual(self.broker.handle(desktop, self.machine_peer).status, "ACCEPTED")

        self.worker.session_state = "locked"
        locked = self._request(
            "session.desktop.control",
            self.machine_peer,
            context=desktop_context,
            approval_id="approval-001",
            payload={"action": "focus_window"},
            request_id="desktop-request-002",
            idempotency_key="desktop-idempotency-002",
        )
        response = self.broker.handle(locked, self.machine_peer)
        self.assertEqual(response.error_code, "session_desktop_requires_unlocked_session")

    def test_expired_desktop_approval_is_rejected(self) -> None:
        desktop_context = self.context("INTERACTIVE_DESKTOP", desktop=True)
        start = self._request(
            "session.run.start",
            self.machine_peer,
            context=desktop_context,
            request_id="start-request-expired-001",
            idempotency_key="start-idempotency-expired-001",
        )
        self.assertEqual(self.broker.handle(start, self.machine_peer).status, "ACCEPTED")
        created = datetime.now(UTC) - timedelta(seconds=61)
        self.broker.approve_desktop_request("approval-expired", now=created)
        request = self._request(
            "session.desktop.control",
            self.machine_peer,
            context=desktop_context,
            approval_id="approval-expired",
            payload={"action": "focus_window"},
            request_id="desktop-request-003",
            idempotency_key="desktop-idempotency-003",
        )
        response = self.broker.handle(request, self.machine_peer, now=datetime.now(UTC))
        self.assertEqual(response.error_code, "session_desktop_control_approval_invalid")

    def test_idempotency_returns_duplicate_and_conflict_is_rejected(self) -> None:
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-duplicate-001",
            idempotency_key="start-idempotency-duplicate-001",
        )
        first = self.broker.handle(request, self.machine_peer)
        second = self.broker.handle(request, self.machine_peer)
        self.assertEqual(first.status, "ACCEPTED")
        self.assertEqual(second.status, "DUPLICATE")
        conflict = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            payload={"adapter_id": "test-adapter", "command": ["python", "-c", "pass"], "different": True},
            request_id="different-request-001",
            idempotency_key="start-idempotency-duplicate-001",
        )
        conflict_response = self.broker.handle(conflict, self.machine_peer)
        self.assertEqual(conflict_response.error_code, "session_idempotency_conflict")

    def test_logged_out_user_cannot_start_run_and_shutdown_clears_runs(self) -> None:
        self.worker.session_state = "logged_out"
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-logged-out-001",
            idempotency_key="start-idempotency-logged-out-001",
        )
        response = self.broker.handle(request, self.machine_peer)
        self.assertEqual(response.error_code, "session_user_not_available")

        self.worker.session_state = "logged_in"
        start = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-shutdown-001",
            idempotency_key="start-idempotency-shutdown-001",
        )
        self.assertEqual(self.broker.handle(start, self.machine_peer).status, "ACCEPTED")
        shutdown = self._request(
            "session.worker.shutdown",
            self.machine_peer,
            user_session_id=None,
            request_id="shutdown-request-001",
            idempotency_key="shutdown-idempotency-001",
        )
        response = self.broker.handle(shutdown, self.machine_peer)
        self.assertEqual(response.status, "COMPLETED")
        self.assertEqual(self.worker.state, "STOPPED")
        self.assertEqual(self.worker.active_run_ids, set())

    def test_logged_out_user_can_stop_existing_run_and_unknown_stop_is_rejected(self) -> None:
        start = self._request(
            "session.run.start",
            self.machine_peer,
            context=self.context(),
            request_id="start-request-cleanup-001",
            idempotency_key="start-idempotency-cleanup-001",
        )
        self.assertEqual(self.broker.handle(start, self.machine_peer).status, "ACCEPTED")
        self.worker.session_state = "logged_out"
        stop = self._request(
            "session.run.stop",
            self.machine_peer,
            context=self.context(),
            request_id="stop-request-cleanup-001",
            idempotency_key="stop-idempotency-cleanup-001",
        )
        response = self.broker.handle(stop, self.machine_peer)
        self.assertEqual(response.status, "COMPLETED")
        self.assertEqual(self.worker.active_run_ids, set())
        self.assertEqual(self.worker.state, "PAUSED")

        unknown = self._request(
            "session.run.stop",
            self.machine_peer,
            context=self.context().model_copy(update={"run_id": "run-unknown"}),
            request_id="stop-request-cleanup-002",
            idempotency_key="stop-idempotency-cleanup-002",
        )
        response = self.broker.handle(unknown, self.machine_peer)
        self.assertEqual(response.error_code, "session_run_not_active")


if __name__ == "__main__":
    unittest.main()
