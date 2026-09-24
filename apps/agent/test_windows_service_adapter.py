from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from windows_service_adapter import (
    FailureRecoveryPolicy,
    ServiceInstallSpec,
    SessionSnapshot,
    SessionWorkerCoordinator,
    WindowsServiceError,
    WindowsServiceHost,
    WindowsServiceInstaller,
    WorkerHandle,
    WorkerLaunchRequest,
    make_session_worker_command,
    session_event_from_wts,
)


class FakeServiceBackend:
    def __init__(self, *, exists: bool = False) -> None:
        self.exists = exists
        self.calls: list[tuple[str, object]] = []

    def install(self, spec: ServiceInstallSpec) -> None:
        self.calls.append(("install", spec.service_name))
        if self.exists:
            raise WindowsServiceError("service_already_exists")
        self.exists = True

    def update(self, spec: ServiceInstallSpec) -> None:
        self.calls.append(("update", spec.service_name))

    def uninstall(self, service_name: str) -> None:
        self.calls.append(("uninstall", service_name))
        self.exists = False

    def start(self, service_name: str) -> None:
        self.calls.append(("start", service_name))

    def stop(self, service_name: str, timeout_seconds: float = 10.0) -> None:
        self.calls.append(("stop", service_name))

    def status(self, service_name: str) -> str:
        return "STOPPED"

    def configure_recovery(self, service_name: str, policy: FailureRecoveryPolicy) -> None:
        self.calls.append(("recovery", (service_name, policy)))


class FakeSessionBackend:
    def __init__(self) -> None:
        self.sessions: list[SessionSnapshot] = []
        self.running: dict[int, bool] = {}
        self.launches: list[WorkerLaunchRequest] = []
        self.stops: list[WorkerHandle] = []
        self.readiness_probes: list[WorkerHandle] = []
        self.fail_readiness = False
        self._next_pid = 1000

    def enumerate_sessions(self) -> list[SessionSnapshot]:
        return list(self.sessions)

    def launch_worker(self, request: WorkerLaunchRequest) -> WorkerHandle:
        self._next_pid += 1
        handle = WorkerHandle(
            request.worker_id,
            request.session.session_id,
            request.session.windows_session_id,
            self._next_pid,
            request.pipe_name,
        )
        self.launches.append(request)
        self.running[handle.process_id] = True
        return handle

    def stop_worker(self, handle: WorkerHandle, timeout_seconds: float = 5.0) -> None:
        self.stops.append(handle)
        self.running[handle.process_id] = False

    def worker_is_running(self, handle: WorkerHandle) -> bool:
        return self.running.get(handle.process_id, False)

    def wait_worker_ready(self, handle: WorkerHandle, **kwargs: object) -> None:
        del kwargs
        self.readiness_probes.append(handle)
        if self.fail_readiness:
            raise WindowsServiceError("worker_readiness_failed:test")

    def release_worker_handle(self, handle: WorkerHandle) -> None:
        del handle


class FakeDispatcher:
    def __init__(self) -> None:
        self.service_name: str | None = None

    def run(self, service_name, on_start, on_stop, on_session_event) -> None:
        del on_session_event
        self.service_name = service_name
        on_start()
        on_stop()


class WindowsServiceInstallerTests(unittest.TestCase):
    def spec(self) -> ServiceInstallSpec:
        return ServiceInstallSpec(
            service_name="MathAgentService",
            display_name="Math Agent Service",
            command=(r"C:\Math Agent\agentd.exe", "service-host", "--service"),
        )

    def test_install_applies_recovery_policy(self) -> None:
        backend = FakeServiceBackend()
        result = WindowsServiceInstaller(backend).install_or_update(self.spec())
        self.assertEqual(result, "INSTALLED")
        self.assertEqual([name for name, _ in backend.calls], ["install", "recovery"])

    def test_existing_service_is_updated_and_recovery_is_reapplied(self) -> None:
        backend = FakeServiceBackend(exists=True)
        result = WindowsServiceInstaller(backend).install_or_update(self.spec())
        self.assertEqual(result, "UPDATED")
        self.assertEqual([name for name, _ in backend.calls], ["install", "update", "recovery"])

    def test_command_and_policy_validation_is_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "windows_service_name_invalid"):
            ServiceInstallSpec("bad name", "Display", ("agent.exe",))
        with self.assertRaisesRegex(ValueError, "service_failure_restart_delays_invalid"):
            FailureRecoveryPolicy(())


class SessionWorkerCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.backend = FakeSessionBackend()
        self.session = SessionSnapshot("session-001", 7, "S-1-5-21-100", "logged_in", "alice")
        self.backend.sessions = [self.session]
        self.coordinator = SessionWorkerCoordinator(
            self.backend,
            worker_command=(r"C:\Python\python.exe", "-m", "agentd", "session-worker-run"),
            worker_cwd=str(Path(self.temp_dir.name).resolve()),
            pipe_name_factory=lambda item: rf"\\.\pipe\math-agent-{item.windows_session_id}",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reconcile_launches_worker_and_records_binding(self) -> None:
        results = self.coordinator.reconcile()
        self.assertEqual(len(self.backend.launches), 1)
        self.assertEqual(results[-1].state, "WORKER_READY")
        audit = self.coordinator.launch_audits()[0]
        self.assertEqual(audit.user_sid, self.session.user_sid)
        self.assertEqual(audit.windows_session_id, self.session.windows_session_id)
        self.assertEqual(audit.pipe_name, r"\\.\pipe\math-agent-7")
        self.assertEqual(len(self.backend.readiness_probes), 1)
        self.assertEqual(self.backend.readiness_probes[0].worker_id, "worker-session-001")

    def test_readiness_failure_does_not_mark_worker_ready(self) -> None:
        self.backend.fail_readiness = True
        results = self.coordinator.reconcile()
        slot = self.coordinator.slots[self.session.session_id]
        self.assertEqual(slot.lifecycle.state, "FAILED")
        self.assertIsNone(slot.worker)
        self.assertEqual(len(self.backend.stops), 1)
        self.assertEqual(results[-1].state, "FAILED")

    def test_worker_exit_becomes_degraded_then_is_recovered(self) -> None:
        self.coordinator.reconcile()
        handle = self.coordinator.slots[self.session.session_id].worker
        assert handle is not None
        self.backend.running[handle.process_id] = False
        result = self.coordinator.poll_worker_exits()[0]
        self.assertEqual(result.state, "DEGRADED")
        self.coordinator.reconcile()
        self.assertEqual(len(self.backend.launches), 2)
        self.assertEqual(self.coordinator.slots[self.session.session_id].lifecycle.state, "WORKER_READY")

    def test_reconcile_does_not_turn_a_locked_session_into_unlocked(self) -> None:
        self.coordinator.reconcile()
        self.coordinator.handle_event(self.session.session_id, "session.locked")
        self.coordinator.reconcile()
        slot = self.coordinator.slots[self.session.session_id]
        self.assertEqual(slot.lifecycle.session_state, "locked")
        self.coordinator.handle_event(self.session.session_id, "session.unlocked")
        self.assertEqual(self.coordinator.slots[self.session.session_id].lifecycle.session_state, "logged_in")

    def test_logout_stops_worker_and_removes_slot(self) -> None:
        self.coordinator.reconcile()
        self.backend.sessions = []
        results = self.coordinator.reconcile()
        self.assertEqual(results[0].session_state, "logged_out")
        self.assertEqual(len(self.backend.stops), 1)
        self.assertEqual(self.coordinator.slots, {})

    def test_logout_transition_keeps_the_previous_session_identity(self) -> None:
        transitions = []
        coordinator = SessionWorkerCoordinator(
            self.backend,
            worker_command=(r"C:\Python\python.exe", "-m", "agentd"),
            worker_cwd=str(Path(self.temp_dir.name).resolve()),
            pipe_name_factory=lambda item: rf"\\.\pipe\math-agent-{item.windows_session_id}",
            on_lifecycle_transition=transitions.append,
        )
        coordinator.reconcile()
        self.backend.sessions = []
        coordinator.reconcile()
        logout = [item for item in transitions if item.event_type == "session.status.changed"][-1]
        self.assertEqual(logout.user_session_id, "session-001")
        self.assertEqual(logout.session_state, "logged_out")

    def test_stop_all_completes_worker_lifecycle(self) -> None:
        self.coordinator.reconcile()
        results = self.coordinator.stop_all()
        self.assertEqual(results[-1].state, "STOPPED")
        self.assertEqual(self.coordinator.slots, {})

    def test_windows_service_host_delegates_to_dispatcher(self) -> None:
        dispatcher = FakeDispatcher()
        host = WindowsServiceHost(self.coordinator, dispatcher=dispatcher)
        host.run_as_windows_service("MathAgentMachineService")
        self.assertEqual(dispatcher.service_name, "MathAgentMachineService")
        self.assertEqual(len(self.backend.stops), 1)

class SessionEventMappingTests(unittest.TestCase):
    def test_wts_events_map_to_shared_signals(self) -> None:
        self.assertEqual(session_event_from_wts(5), "session.logged_in")
        self.assertEqual(session_event_from_wts(6), "session.logged_out")
        self.assertEqual(session_event_from_wts(7), "session.locked")
        self.assertEqual(session_event_from_wts(8), "session.unlocked")
        self.assertIsNone(session_event_from_wts(99))

    def test_worker_command_contains_session_specific_identity(self) -> None:
        command = make_session_worker_command(
            (r"C:\Python\python.exe", r"C:\Agent\agentd.py"),
            pipe_name=r"\\.\pipe\math-agent-7",
            worker_id="worker-session-001",
            user_session_id="session-001",
            user_sid="S-1-5-21-100",
            workspace=r"C:\Workspace",
            allowed_peer_id="machine-001",
            allowed_sids=("S-1-5-18",),
            allowed_session_ids=(0,),
            state_path=r"C:\State\worker.db",
        )
        self.assertEqual(command[:2], (r"C:\Python\python.exe", r"C:\Agent\agentd.py"))
        self.assertIn("--pipe-name", command)
        self.assertIn(r"\\.\pipe\math-agent-7", command)
        self.assertIn("--user-sid", command)
        self.assertIn("S-1-5-21-100", command)


if __name__ == "__main__":
    unittest.main()
