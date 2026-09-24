"""Transport-neutral lifecycle policy for the machine service and user worker.

This module deliberately does not call Windows Service APIs.  It converts
session-manager observations into deterministic actions that a Windows
adapter can execute and records every accepted transition for local audit.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Callable, Literal
from uuid import uuid4

try:
    from packages.agent_protocol import SessionLifecycleTransition
except ModuleNotFoundError:  # Support direct script execution during development.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from packages.agent_protocol import SessionLifecycleTransition


ServiceState = Literal[
    "STOPPED",
    "STARTING",
    "WAITING_FOR_SESSION",
    "STARTING_WORKER",
    "WORKER_READY",
    "DEGRADED",
    "STOPPING",
    "FAILED",
    "EMERGENCY_STOPPED",
]

SessionState = Literal["unknown", "logged_in", "locked", "logged_out"]
LifecycleSignal = Literal[
    "service.start",
    "session.discovered",
    "worker.ready",
    "worker.exited",
    "worker.failed",
    "session.logged_in",
    "session.locked",
    "session.unlocked",
    "session.logged_out",
    "run.started",
    "run.stopped",
    "service.stop",
    "worker.stopped",
    "emergency.stop",
    "recovery.requested",
]


class LifecyclePolicyError(ValueError):
    """Stable error family for invalid lifecycle observations or commands."""


@dataclass(frozen=True)
class LifecycleAction:
    kind: Literal[
        "discover_session",
        "start_worker",
        "stop_worker",
        "stop_runs",
        "pause_desktop_runs",
        "resume_allowed_runs",
        "mark_degraded",
        "none",
    ]
    run_ids: tuple[str, ...] = ()
    session_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class LifecycleResult:
    accepted: bool
    signal: LifecycleSignal
    previous_state: ServiceState
    state: ServiceState
    session_state: SessionState
    active_run_ids: tuple[str, ...]
    actions: tuple[LifecycleAction, ...] = ()
    error_code: str | None = None
    transition: SessionLifecycleTransition | None = None


@dataclass
class MachineServiceLifecycle:
    """Small deterministic state machine used by service/session adapters."""

    worker_id: str
    user_session_id: str | None = None
    state: ServiceState = "STOPPED"
    session_state: SessionState = "unknown"
    active_run_ids: set[str] = field(default_factory=set)
    on_transition: Callable[[SessionLifecycleTransition], None] | None = None

    def __post_init__(self) -> None:
        if len(self.worker_id) < 2:
            raise LifecyclePolicyError("lifecycle_worker_id_required")
        if self.user_session_id is not None and len(self.user_session_id) < 2:
            raise LifecyclePolicyError("lifecycle_session_id_invalid")
        if self.state not in {"STOPPED", "STARTING", "WAITING_FOR_SESSION", "STARTING_WORKER", "WORKER_READY", "DEGRADED", "STOPPING", "FAILED", "EMERGENCY_STOPPED"}:
            raise LifecyclePolicyError("lifecycle_state_invalid")
        if self.session_state not in {"unknown", "logged_in", "locked", "logged_out"}:
            raise LifecyclePolicyError("lifecycle_session_state_invalid")
        self._transitions: deque[SessionLifecycleTransition] = deque()
        self._transition_sequence = 0

    def snapshot(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "user_session_id": self.user_session_id,
            "state": self.state,
            "session_state": self.session_state,
            "active_run_ids": sorted(self.active_run_ids),
        }

    def can_start_run(self, *, execution_mode: str = "USER_SESSION") -> bool:
        if execution_mode not in {"HEADLESS", "USER_SESSION", "INTERACTIVE_DESKTOP"}:
            return False
        if self.state != "WORKER_READY":
            return False
        if self.session_state in {"unknown", "logged_out"}:
            return False
        return not (execution_mode == "INTERACTIVE_DESKTOP" and self.session_state != "logged_in")

    def poll_transitions(self, limit: int = 100) -> list[SessionLifecycleTransition]:
        if limit < 1:
            raise ValueError("lifecycle_transition_limit_invalid")
        values: list[SessionLifecycleTransition] = []
        while self._transitions and len(values) < limit:
            values.append(self._transitions.popleft())
        return values

    def apply(
        self,
        signal: LifecycleSignal,
        *,
        session_id: str | None = None,
        observed_session_state: SessionState | None = None,
        run_id: str | None = None,
        execution_mode: str = "USER_SESSION",
        reason: str = "",
    ) -> LifecycleResult:
        previous_state = self.state
        previous_session_id = self.user_session_id
        actions: list[LifecycleAction] = []
        cleanup_run_ids: tuple[str, ...] = ()

        if signal == "service.start":
            if self.state not in {"STOPPED", "DEGRADED", "FAILED"}:
                return self._reject(signal, previous_state, "lifecycle_service_already_started")
            self.state = "STARTING"
            if self.user_session_id and self.session_state in {"logged_in", "locked"}:
                self.state = "STARTING_WORKER"
                actions.append(LifecycleAction("start_worker", session_id=self.user_session_id, reason=reason or "service_start"))
            else:
                self.state = "WAITING_FOR_SESSION"
                actions.append(LifecycleAction("discover_session", reason=reason or "service_start"))

        elif signal == "session.discovered":
            if self.state not in {"STARTING", "WAITING_FOR_SESSION", "DEGRADED"}:
                return self._reject(signal, previous_state, "lifecycle_session_discovery_not_expected")
            if not session_id:
                return self._reject(signal, previous_state, "lifecycle_session_id_required")
            self.user_session_id = session_id
            self.session_state = observed_session_state or "logged_in"
            if self.session_state in {"logged_in", "locked"}:
                self.state = "STARTING_WORKER"
                actions.append(LifecycleAction("start_worker", session_id=session_id, reason=reason or "session_discovered"))
            else:
                self.state = "WAITING_FOR_SESSION"
                actions.append(LifecycleAction("discover_session", session_id=session_id, reason=reason or "session_unavailable"))

        elif signal == "worker.ready":
            if self.state not in {"STARTING_WORKER", "DEGRADED"}:
                return self._reject(signal, previous_state, "lifecycle_worker_ready_not_expected")
            if not self.user_session_id:
                return self._reject(signal, previous_state, "lifecycle_session_id_required")
            if self.session_state in {"unknown", "logged_out"}:
                return self._reject(signal, previous_state, "lifecycle_session_unavailable")
            self.state = "WORKER_READY"
            if self.session_state == "unknown":
                self.session_state = "logged_in"

        elif signal in {"session.logged_in", "session.unlocked", "session.locked", "session.logged_out"}:
            new_session_state: SessionState = {
                "session.logged_in": "logged_in",
                "session.unlocked": "logged_in",
                "session.locked": "locked",
                "session.logged_out": "logged_out",
            }[signal]
            if self.state in {"STOPPED", "STOPPING", "EMERGENCY_STOPPED"}:
                return self._reject(signal, previous_state, "lifecycle_session_update_not_allowed")
            if new_session_state != "logged_out" and self.state == "WAITING_FOR_SESSION":
                if session_id:
                    self.user_session_id = session_id
                if not self.user_session_id:
                    return self._reject(signal, previous_state, "lifecycle_session_id_required")
                self.state = "STARTING_WORKER"
                actions.append(LifecycleAction("start_worker", session_id=self.user_session_id, reason="session_available"))
            self.session_state = new_session_state
            if new_session_state == "logged_out":
                cleanup_session_id = self.user_session_id
                cleanup_run_ids = tuple(sorted(self.active_run_ids))
                self.active_run_ids.clear()
                self.state = "WAITING_FOR_SESSION"
                if cleanup_run_ids:
                    actions.append(LifecycleAction("stop_runs", cleanup_run_ids, cleanup_session_id, "session_logged_out"))
                if cleanup_session_id:
                    actions.append(LifecycleAction("stop_worker", session_id=cleanup_session_id, reason="session_logged_out"))
                self.user_session_id = None
            elif signal == "session.locked":
                if self.state == "WORKER_READY":
                    actions.append(LifecycleAction("pause_desktop_runs", tuple(sorted(self.active_run_ids)), self.user_session_id, "session_locked"))
            elif signal == "session.unlocked":
                if self.state == "WORKER_READY":
                    actions.append(LifecycleAction("resume_allowed_runs", tuple(sorted(self.active_run_ids)), self.user_session_id, "session_unlocked"))

        elif signal == "run.started":
            if not run_id:
                return self._reject(signal, previous_state, "lifecycle_run_id_required")
            if not self.can_start_run(execution_mode=execution_mode):
                return self._reject(signal, previous_state, "lifecycle_run_start_not_allowed")
            if run_id in self.active_run_ids:
                return self._reject(signal, previous_state, "lifecycle_run_already_active")
            self.active_run_ids.add(run_id)

        elif signal == "run.stopped":
            if not run_id:
                return self._reject(signal, previous_state, "lifecycle_run_id_required")
            if run_id not in self.active_run_ids:
                return self._reject(signal, previous_state, "lifecycle_run_not_active")
            self.active_run_ids.remove(run_id)

        elif signal in {"worker.exited", "worker.failed"}:
            if self.state in {"STOPPED", "EMERGENCY_STOPPED"}:
                return self._reject(signal, previous_state, "lifecycle_worker_exit_not_expected")
            cleanup_run_ids = tuple(sorted(self.active_run_ids))
            self.active_run_ids.clear()
            self.state = "DEGRADED" if signal == "worker.exited" else "FAILED"
            actions.append(LifecycleAction("stop_runs", cleanup_run_ids, self.user_session_id, signal))
            actions.append(LifecycleAction("mark_degraded", reason=reason or signal))

        elif signal == "service.stop":
            if self.state in {"STOPPED", "EMERGENCY_STOPPED"}:
                return self._reject(signal, previous_state, "lifecycle_service_not_running")
            cleanup_run_ids = tuple(sorted(self.active_run_ids))
            self.state = "STOPPING"
            actions.append(LifecycleAction("stop_runs", cleanup_run_ids, self.user_session_id, "service_stop"))
            if self.user_session_id and previous_state != "WAITING_FOR_SESSION":
                actions.append(LifecycleAction("stop_worker", session_id=self.user_session_id, reason="service_stop"))

        elif signal == "worker.stopped":
            if self.state not in {"STOPPING", "DEGRADED", "FAILED", "WAITING_FOR_SESSION"}:
                return self._reject(signal, previous_state, "lifecycle_worker_stop_not_expected")
            self.state = "STOPPED" if previous_state == "STOPPING" else "WAITING_FOR_SESSION"
            self.active_run_ids.clear()

        elif signal == "emergency.stop":
            if self.state == "EMERGENCY_STOPPED":
                return self._reject(signal, previous_state, "lifecycle_emergency_stop_already_active")
            cleanup_run_ids = tuple(sorted(self.active_run_ids))
            self.state = "EMERGENCY_STOPPED"
            actions.append(LifecycleAction("stop_runs", cleanup_run_ids, self.user_session_id, reason or "emergency_stop"))
            actions.append(LifecycleAction("stop_worker", session_id=self.user_session_id, reason=reason or "emergency_stop"))

        elif signal == "recovery.requested":
            if self.state not in {"DEGRADED", "FAILED"}:
                return self._reject(signal, previous_state, "lifecycle_recovery_not_required")
            if not self.user_session_id or self.session_state in {"unknown", "logged_out"}:
                self.state = "WAITING_FOR_SESSION"
                actions.append(LifecycleAction("discover_session", reason=reason or "recovery_requested"))
            else:
                self.state = "STARTING_WORKER"
                actions.append(LifecycleAction("start_worker", session_id=self.user_session_id, reason=reason or "recovery_requested"))
        else:
            raise LifecyclePolicyError("lifecycle_signal_not_supported")

        transition = self._transition(
            signal,
            previous_state,
            self.state,
            cleanup_run_ids,
            reason,
            transition_session_id=previous_session_id or session_id,
        )
        return LifecycleResult(
            accepted=True,
            signal=signal,
            previous_state=previous_state,
            state=self.state,
            session_state=self.session_state,
            active_run_ids=tuple(sorted(self.active_run_ids)),
            actions=tuple(actions),
            transition=transition,
        )

    def _reject(self, signal: LifecycleSignal, previous_state: ServiceState, error_code: str) -> LifecycleResult:
        return LifecycleResult(
            accepted=False,
            signal=signal,
            previous_state=previous_state,
            state=self.state,
            session_state=self.session_state,
            active_run_ids=tuple(sorted(self.active_run_ids)),
            error_code=error_code,
        )

    def _transition(
        self,
        signal: LifecycleSignal,
        previous_state: ServiceState,
        state: ServiceState,
        cleanup_run_ids: tuple[str, ...],
        reason: str,
        transition_session_id: str | None = None,
    ) -> SessionLifecycleTransition:
        event_type = {
            "service.start": "service.start.requested",
            "session.discovered": "worker.registered",
            "worker.ready": "worker.ready",
            "session.logged_in": "session.status.changed",
            "session.unlocked": "session.status.changed",
            "session.locked": "session.status.changed",
            "session.logged_out": "session.status.changed",
            "run.started": "run.started",
            "run.stopped": "run.stopped",
            "service.stop": "service.stop.requested",
            "worker.stopped": "worker.stopped",
            "worker.exited": "worker.exited",
            "worker.failed": "worker.failed",
            "emergency.stop": "emergency.stop",
            "recovery.requested": "recovery.requested",
        }[signal]
        transition = SessionLifecycleTransition(
            transition_id=f"transition-{uuid4().hex}",
            worker_id=self.worker_id,
            user_session_id=transition_session_id or self.user_session_id or "session-unassigned",
            event_type=event_type,
            from_state=previous_state,
            to_state=state,
            session_state=self.session_state,
            active_run_ids=sorted(self.active_run_ids),
            cleanup_run_ids=list(cleanup_run_ids),
            reason=reason or signal,
            occurred_at=datetime.now(UTC),
            metadata={"signal": signal},
        )
        self._transitions.append(transition)
        if self.on_transition is not None:
            try:
                self.on_transition(transition)
            except Exception:
                pass
        return transition


__all__ = [
    "LifecycleAction",
    "LifecyclePolicyError",
    "LifecycleResult",
    "MachineServiceLifecycle",
]
