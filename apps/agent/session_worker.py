"""Transport-neutral policy boundary for a Windows User Session Worker.

The module does not open a socket or named pipe. It validates the shared IPC
contract and models the worker lifecycle so a transport adapter can be added
without moving authorization into the Windows-specific layer.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from collections import deque
from typing import Any, Callable, Literal
from uuid import uuid4

try:
    from packages.agent_protocol import (
        SessionIpcRequest,
        SessionIpcResponse,
        SessionPeer,
        SessionRunContext,
        SessionLifecycleTransition,
    )
except ModuleNotFoundError:  # Support direct script execution during development.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from packages.agent_protocol import SessionIpcRequest, SessionIpcResponse, SessionPeer, SessionRunContext, SessionLifecycleTransition


SessionState = Literal["unknown", "logged_in", "locked", "logged_out"]
WorkerState = Literal["STARTING", "READY", "RUNNING", "WAITING_APPROVAL", "PAUSED", "STOPPING", "STOPPED", "FAILED"]


class SessionPolicyError(ValueError):
    """Stable error family for local IPC policy rejection."""


@dataclass(frozen=True)
class SessionWorkerPolicy:
    allowed_execution_modes: tuple[str, ...] = ("USER_SESSION", "INTERACTIVE_DESKTOP")
    allowed_capabilities: tuple[str, ...] = ("session.run", "terminal.input", "terminal.resize")
    desktop_capability: str = "desktop.control"
    require_authenticated_user_sid: bool = True
    approval_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        valid_modes = {"HEADLESS", "USER_SESSION", "INTERACTIVE_DESKTOP"}
        if not self.allowed_execution_modes or any(mode not in valid_modes for mode in self.allowed_execution_modes):
            raise ValueError("session_allowed_execution_modes_invalid")
        if not self.allowed_capabilities:
            raise ValueError("session_allowed_capabilities_required")
        if self.approval_ttl_seconds <= 0:
            raise ValueError("session_approval_ttl_invalid")


@dataclass
class SessionWorkerRecord:
    worker_id: str
    user_session_id: str
    user_sid: str
    state: WorkerState = "STARTING"
    session_state: SessionState = "unknown"
    supported_execution_modes: tuple[str, ...] = ("USER_SESSION",)
    capabilities: tuple[str, ...] = ()
    active_run_ids: set[str] = field(default_factory=set)


class SessionWorkerBroker:
    """Validate and route session-worker requests without choosing a transport."""

    def __init__(
        self,
        policy: SessionWorkerPolicy | None = None,
        *,
        on_transition: Callable[[SessionLifecycleTransition], None] | None = None,
    ) -> None:
        self.policy = policy or SessionWorkerPolicy()
        self._workers: dict[str, SessionWorkerRecord] = {}
        self._results: dict[tuple[str, str], tuple[str, SessionIpcResponse]] = {}
        self._approved_requests: dict[str, datetime] = {}
        self._transitions: deque[SessionLifecycleTransition] = deque()
        self._on_transition = on_transition

    def set_transition_handler(self, handler: Callable[[SessionLifecycleTransition], None] | None) -> None:
        self._on_transition = handler

    def poll_transitions(self, limit: int = 100) -> list[SessionLifecycleTransition]:
        if limit < 1:
            raise ValueError("session_transition_limit_invalid")
        transitions: list[SessionLifecycleTransition] = []
        while self._transitions and len(transitions) < limit:
            transitions.append(self._transitions.popleft())
        return transitions

    def register_worker(
        self,
        worker_id: str,
        user_session_id: str,
        user_sid: str,
        *,
        supported_execution_modes: tuple[str, ...] = ("USER_SESSION",),
        capabilities: tuple[str, ...] = (),
    ) -> SessionWorkerRecord:
        if not worker_id or not user_session_id or not user_sid:
            raise SessionPolicyError("session_worker_identity_required")
        if any(mode not in {"HEADLESS", "USER_SESSION", "INTERACTIVE_DESKTOP"} for mode in supported_execution_modes):
            raise SessionPolicyError("session_worker_execution_mode_invalid")
        granted = tuple(sorted(set(capabilities) & set(self.policy.allowed_capabilities + (self.policy.desktop_capability,))))
        record = SessionWorkerRecord(
            worker_id=worker_id,
            user_session_id=user_session_id,
            user_sid=user_sid,
            state="READY",
            supported_execution_modes=supported_execution_modes,
            capabilities=granted,
        )
        self._workers[worker_id] = record
        self._record_transition(
            record,
            event_type="worker.registered",
            from_state=None,
            to_state=record.state,
            reason="worker_registered",
        )
        return record

    def get_worker(self, worker_id: str) -> SessionWorkerRecord:
        record = self._workers.get(worker_id)
        if record is None:
            raise SessionPolicyError("session_worker_not_registered")
        return record

    def complete_run(self, worker_id: str, run_id: str) -> None:
        """Release a locally completed run without accepting a new IPC command."""

        record = self.get_worker(worker_id)
        was_active = run_id in record.active_run_ids
        record.active_run_ids.discard(run_id)
        if record.state not in {"STOPPING", "STOPPED", "FAILED"}:
            record.state = "READY" if record.session_state in {"logged_in", "locked"} else "PAUSED"
        if was_active:
            self._record_transition(
                record,
                event_type="run.stopped",
                from_state="RUNNING",
                to_state=record.state,
                reason="run_completed",
                cleanup_run_ids=[run_id],
            )

    def restore_run(self, worker_id: str, run_id: str) -> None:
        """Restore an active run when execution cleanup fails after authorization."""

        record = self.get_worker(worker_id)
        if record.state not in {"STOPPED", "FAILED"}:
            record.active_run_ids.add(run_id)
            record.state = "RUNNING"

    def replace_idempotent_response(self, request: SessionIpcRequest, response: SessionIpcResponse) -> None:
        """Replace a cached result after a post-authorization execution failure."""

        cached = self._results.get((request.worker_id, request.idempotency_key))
        if cached is None or cached[0] != self._fingerprint(request):
            raise SessionPolicyError("session_idempotency_result_not_found")
        self._results[(request.worker_id, request.idempotency_key)] = (cached[0], response)

    def approve_desktop_request(self, approval_id: str, *, now: datetime | None = None) -> None:
        if not approval_id:
            raise SessionPolicyError("session_approval_id_required")
        self._approved_requests[approval_id] = now or datetime.now(UTC)

    def revoke_desktop_approval(self, approval_id: str) -> None:
        self._approved_requests.pop(approval_id, None)

    def handle(
        self,
        request: SessionIpcRequest,
        authenticated_peer: SessionPeer,
        *,
        now: datetime | None = None,
    ) -> SessionIpcResponse:
        timestamp = now or datetime.now(UTC)
        fingerprint = self._fingerprint(request)
        cached = self._results.get((request.worker_id, request.idempotency_key))
        if cached is not None:
            previous_fingerprint, previous_response = cached
            if previous_fingerprint != fingerprint:
                return self._response(request, "REJECTED", "session_idempotency_conflict", timestamp)
            return self._response(
                request,
                "DUPLICATE",
                payload={"original": previous_response.model_dump(mode="json")},
                responded_at=timestamp,
            )

        try:
            self._validate_peer(request, authenticated_peer)
            record = self.get_worker(request.worker_id)
            self._validate_request(request, record, timestamp)
            response = self._apply(request, record, timestamp)
        except SessionPolicyError as error:
            response = self._response(request, "REJECTED", str(error), timestamp)
        self._results[(request.worker_id, request.idempotency_key)] = (fingerprint, response)
        return response

    def _validate_peer(self, request: SessionIpcRequest, authenticated_peer: SessionPeer) -> None:
        if request.peer.peer_id != authenticated_peer.peer_id or request.peer.peer_kind != authenticated_peer.peer_kind:
            raise SessionPolicyError("session_peer_identity_mismatch")
        if authenticated_peer.process_id is not None:
            if request.peer.process_id is None or request.peer.process_id != authenticated_peer.process_id:
                raise SessionPolicyError("session_process_identity_mismatch")
        if authenticated_peer.windows_session_id is not None:
            if request.peer.windows_session_id is None or request.peer.windows_session_id != authenticated_peer.windows_session_id:
                raise SessionPolicyError("session_windows_session_identity_mismatch")
        if self.policy.require_authenticated_user_sid and request.peer.peer_kind != "machine_service":
            if not request.peer.user_sid or not authenticated_peer.user_sid:
                raise SessionPolicyError("session_user_sid_required")
            if request.peer.user_sid != authenticated_peer.user_sid:
                raise SessionPolicyError("session_user_sid_mismatch")

    def _validate_request(self, request: SessionIpcRequest, record: SessionWorkerRecord, now: datetime) -> None:
        if request.worker_id != record.worker_id:
            raise SessionPolicyError("session_worker_identity_mismatch")
        if request.user_session_id is not None and request.user_session_id != record.user_session_id:
            raise SessionPolicyError("session_id_mismatch")
        if request.peer.peer_kind == "user_session_worker" and request.peer.peer_id != record.worker_id:
            raise SessionPolicyError("session_worker_peer_identity_mismatch")
        if request.peer.peer_kind in {"user_session_worker", "desktop_ui"} and request.peer.user_sid != record.user_sid:
            raise SessionPolicyError("session_worker_user_sid_mismatch")
        if request.peer.peer_kind not in {"machine_service", "desktop_ui", "user_session_worker"}:
            raise SessionPolicyError("session_peer_kind_not_allowed")
        if record.state == "STOPPED" and request.message_type != "session.worker.shutdown":
            raise SessionPolicyError("session_worker_stopped")
        if request.message_type in {"session.run.start", "session.run.stop", "session.terminal.input", "session.terminal.resize", "session.desktop.control"}:
            assert request.context is not None
            self._validate_context(request.context, record, for_start=request.message_type != "session.run.stop")
        if request.message_type in {"session.terminal.input", "session.terminal.resize", "session.desktop.control"}:
            assert request.context is not None
            if request.context.run_id not in record.active_run_ids:
                raise SessionPolicyError("session_run_not_active")
        if request.message_type in {"session.run.start", "session.run.stop"} and "session.run" not in record.capabilities:
            raise SessionPolicyError("session_run_capability_missing")
        if request.message_type == "session.desktop.control":
            if request.peer.peer_kind != "machine_service":
                raise SessionPolicyError("session_desktop_control_machine_only")
            if request.approval_id is None or not self._approval_is_valid(request.approval_id, now):
                raise SessionPolicyError("session_desktop_control_approval_invalid")
            if self.policy.desktop_capability not in record.capabilities:
                raise SessionPolicyError("session_desktop_control_capability_missing")
        if request.message_type == "session.terminal.input":
            if request.peer.peer_kind != "machine_service":
                raise SessionPolicyError("session_terminal_machine_only")
            if "terminal.input" not in record.capabilities:
                raise SessionPolicyError("session_terminal_capability_missing")
        if request.message_type == "session.terminal.resize":
            if request.peer.peer_kind != "machine_service":
                raise SessionPolicyError("session_terminal_machine_only")
            if "terminal.resize" not in record.capabilities:
                raise SessionPolicyError("session_terminal_resize_capability_missing")
        if request.message_type in {"session.run.start", "session.run.stop"} and request.peer.peer_kind != "machine_service":
            raise SessionPolicyError("session_run_machine_only")
        if request.message_type == "session.hello":
            if request.peer.peer_kind == "user_session_worker":
                return
            if request.peer.peer_kind != "machine_service" or request.payload.get("readiness_probe") is not True:
                raise SessionPolicyError("session_status_worker_only")
            if request.user_session_id != record.user_session_id:
                raise SessionPolicyError("session_id_mismatch")
        if request.message_type == "session.status" and request.peer.peer_kind != "user_session_worker":
            raise SessionPolicyError("session_status_worker_only")
        if request.message_type == "session.approval.respond" and request.peer.peer_kind != "desktop_ui":
            raise SessionPolicyError("session_approval_desktop_ui_only")
        if request.message_type == "session.worker.shutdown" and request.peer.peer_kind != "machine_service":
            raise SessionPolicyError("session_shutdown_machine_only")

    def _validate_context(self, context: SessionRunContext, record: SessionWorkerRecord, *, for_start: bool = True) -> None:
        if context.execution_mode not in self.policy.allowed_execution_modes:
            raise SessionPolicyError("session_execution_mode_not_allowed")
        if context.execution_mode not in record.supported_execution_modes:
            raise SessionPolicyError("session_worker_execution_mode_not_supported")
        if context.allow_desktop_control and context.execution_mode != "INTERACTIVE_DESKTOP":
            raise SessionPolicyError("session_desktop_mode_mismatch")
        if context.allow_desktop_control and self.policy.desktop_capability not in record.capabilities:
            raise SessionPolicyError("session_desktop_control_capability_missing")
        if context.allow_remote_terminal and "terminal.input" not in record.capabilities and "terminal.resize" not in record.capabilities:
            raise SessionPolicyError("session_terminal_capability_missing")
        if for_start and record.session_state in {"logged_out", "unknown"}:
            raise SessionPolicyError("session_user_not_available")
        if for_start and context.execution_mode == "INTERACTIVE_DESKTOP" and record.session_state != "logged_in":
            raise SessionPolicyError("session_desktop_requires_unlocked_session")
        if record.state in {"STOPPING", "STOPPED", "FAILED"}:
            raise SessionPolicyError("session_worker_not_ready")

    def _apply(self, request: SessionIpcRequest, record: SessionWorkerRecord, timestamp: datetime) -> SessionIpcResponse:
        if request.message_type == "session.status":
            previous_state = record.state
            state = request.payload.get("session_state")
            if state not in {"unknown", "logged_in", "locked", "logged_out"}:
                raise SessionPolicyError("session_state_invalid")
            record.session_state = state
            if record.active_run_ids:
                record.state = "RUNNING" if state in {"logged_in", "locked"} else "PAUSED"
            else:
                record.state = "READY" if state in {"logged_in", "locked"} else "PAUSED"
            self._record_transition(
                record,
                event_type="session.status.changed",
                from_state=previous_state,
                to_state=record.state,
                reason=f"session_state:{state}",
            )
            return self._response(request, "COMPLETED", responded_at=timestamp, payload={"state": record.state})
        if request.message_type == "session.hello":
            record.state = "READY"
            return self._response(
                request,
                "COMPLETED",
                responded_at=timestamp,
                payload={
                    "state": record.state,
                    "worker_id": record.worker_id,
                    "user_session_id": record.user_session_id,
                },
            )
        if request.message_type == "session.run.start":
            assert request.context is not None
            if request.context.run_id in record.active_run_ids:
                raise SessionPolicyError("session_run_already_active")
            record.active_run_ids.add(request.context.run_id)
            record.state = "RUNNING"
            self._record_transition(
                record,
                event_type="run.started",
                from_state="READY",
                to_state=record.state,
                reason="run_start_authorized",
            )
            return self._response(request, "ACCEPTED", responded_at=timestamp, payload={"run_id": request.context.run_id})
        if request.message_type == "session.run.stop":
            assert request.context is not None
            if request.context.run_id not in record.active_run_ids:
                raise SessionPolicyError("session_run_not_active")
            record.active_run_ids.discard(request.context.run_id)
            if record.active_run_ids:
                record.state = "RUNNING"
            else:
                record.state = "READY" if record.session_state in {"logged_in", "locked"} else "PAUSED"
            self._record_transition(
                record,
                event_type="run.stopped",
                from_state="RUNNING",
                to_state=record.state,
                reason="run_stop_authorized",
                cleanup_run_ids=[request.context.run_id],
            )
            return self._response(request, "COMPLETED", responded_at=timestamp, payload={"run_id": request.context.run_id})
        if request.message_type == "session.approval.respond":
            decision = request.payload.get("decision")
            if decision not in {"APPROVED", "DENIED"}:
                raise SessionPolicyError("session_approval_decision_invalid")
            if request.approval_id is not None:
                if decision == "APPROVED":
                    self.approve_desktop_request(request.approval_id, now=timestamp)
                else:
                    self.revoke_desktop_approval(request.approval_id)
            return self._response(request, "COMPLETED", responded_at=timestamp, payload={"decision": decision})
        if request.message_type == "session.worker.shutdown":
            previous_state = record.state
            cleanup_run_ids = sorted(record.active_run_ids)
            record.state = "STOPPED"
            record.active_run_ids.clear()
            self._record_transition(
                record,
                event_type="worker.stopped",
                from_state=previous_state,
                to_state=record.state,
                reason="worker_shutdown_authorized",
                cleanup_run_ids=cleanup_run_ids,
            )
            return self._response(request, "COMPLETED", responded_at=timestamp, payload={"state": record.state})
        if request.message_type in {"session.terminal.input", "session.terminal.resize", "session.desktop.control"}:
            return self._response(request, "ACCEPTED", responded_at=timestamp)
        raise SessionPolicyError("session_message_type_not_implemented")

    def _approval_is_valid(self, approval_id: str, now: datetime) -> bool:
        created = self._approved_requests.get(approval_id)
        if created is None:
            return False
        age = (now - created).total_seconds()
        if age < 0 or age > self.policy.approval_ttl_seconds:
            self._approved_requests.pop(approval_id, None)
            return False
        return True

    def _record_transition(
        self,
        record: SessionWorkerRecord,
        *,
        event_type: str,
        from_state: str | None,
        to_state: str,
        reason: str,
        cleanup_run_ids: list[str] | None = None,
    ) -> None:
        transition = SessionLifecycleTransition(
            transition_id=f"transition-{uuid4().hex}",
            worker_id=record.worker_id,
            user_session_id=record.user_session_id,
            event_type=event_type,
            from_state=from_state,
            to_state=to_state,
            session_state=record.session_state,
            active_run_ids=sorted(record.active_run_ids),
            cleanup_run_ids=cleanup_run_ids or [],
            reason=reason,
            occurred_at=datetime.now(UTC),
        )
        self._transitions.append(transition)
        if self._on_transition is not None:
            try:
                self._on_transition(transition)
            except Exception:
                pass

    @staticmethod
    def _fingerprint(request: SessionIpcRequest) -> str:
        payload = request.model_dump(mode="json")
        payload.pop("request_id", None)
        payload.pop("sent_at", None)
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

    @staticmethod
    def _response(
        request: SessionIpcRequest,
        status: Literal["ACCEPTED", "COMPLETED", "REJECTED", "FAILED", "DUPLICATE"],
        error_code: str | None = None,
        responded_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
    ) -> SessionIpcResponse:
        return SessionIpcResponse(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            worker_id=request.worker_id,
            status=status,
            error_code=error_code,
            responded_at=responded_at or datetime.now(UTC),
            payload=payload or {},
        )


__all__ = ["SessionPolicyError", "SessionWorkerBroker", "SessionWorkerPolicy", "SessionWorkerRecord"]
