from __future__ import annotations

import sys
import tempfile
import os
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from packages.agent_protocol import SessionIpcRequest, SessionPeer, SessionRunContext
from container_runner import ContainerInvocation, ContainerMount
from local_state import LocalAgentState
from machine_service import LocalProcessSupervisor, ProcessResult
from runner import AdapterDescriptor, CommandAdapter, LocalRunner, WorkspacePolicy, RunnerHandle
from session_runtime import SessionRuntimeConfig, SessionWorkerRuntime
from named_pipe_transport import NamedPipeClient, NamedPipeServer, PipePeerPolicy, current_user_sid, process_session_id
from cli_adapters import CodexJsonlEventParser


class FakeContainerBackend:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.started: list[str] = []
        self._active: set[str] = set()

    def build_invocation(self, image: str, request: object) -> ContainerInvocation:
        return ContainerInvocation(
            runtime="docker",
            image=image,
            command=("docker", "run", image, "python", "job.py"),
            workspace_path=str(self.workspace),
            network_mode="none",
            read_only_rootfs=True,
            read_only_workspace=True,
            limits=__import__("container_runner").ContainerLimits(),
            mounts=(ContainerMount(str(self.workspace), "/workspace", "ro"),),
            environment_keys=(),
        )

    async def start_invocation(self, invocation: ContainerInvocation, request: object) -> RunnerHandle:
        run_id = str(request.run_id)
        self.started.append(run_id)
        self._active.add(run_id)
        return RunnerHandle(run_id=run_id, process_id=f"container-process-{run_id}")

    async def wait(self, run_id: str) -> ProcessResult:
        self._active.discard(run_id)
        return ProcessResult(
            process_id=f"container-process-{run_id}",
            run_id=run_id,
            status="SUCCEEDED",
            exit_code=0,
            stdout="container-ok",
            stderr="",
        )

    async def request_stop(self, run_id: str) -> None:
        self._active.discard(run_id)

    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(self._active)

    async def write_stdin(self, run_id: str, data: str) -> None:
        raise RuntimeError("container_terminal_input_not_supported")

    async def resize_terminal(self, run_id: str, *args: object, **kwargs: object) -> None:
        raise RuntimeError("container_terminal_resize_not_supported")


class SessionWorkerRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.state = LocalAgentState(self.root / "agent.db")
        descriptor = AdapterDescriptor(
            adapter_id="interactive-python",
            agent_name="Python",
            agent_version="test",
            adapter_version="0.1.0",
            supported_os=(__import__("platform").system(),),
            supported_execution_profiles=("USER_SESSION",),
        )
        self.runner = LocalRunner(
            LocalProcessSupervisor(self.state, stop_timeout_seconds=0.2),
            WorkspacePolicy(
                allowed_roots=(str(self.root),),
                allowed_executables=(sys.executable,),
            ),
            [CommandAdapter(descriptor)],
        )
        self.events = []
        self.runtime = SessionWorkerRuntime(
            SessionRuntimeConfig(
                worker_id="worker-runtime-001",
                user_session_id="session-runtime-001",
                user_sid="S-1-5-21-runtime",
                supported_execution_modes=("USER_SESSION", "INTERACTIVE_DESKTOP"),
                capabilities=("session.run", "terminal.input", "terminal.resize", "desktop.control"),
            ),
            self.runner,
            on_event=self._record_event,
        )
        self.worker_peer = SessionPeer(
            peer_id="worker-runtime-001",
            peer_kind="user_session_worker",
            process_id=101,
            user_sid="S-1-5-21-runtime",
        )
        self.machine_peer = SessionPeer(
            peer_id="machine-service-runtime-001",
            peer_kind="machine_service",
            process_id=None,
            user_sid=None,
        )
        status = self._request(
            "session.status",
            self.worker_peer,
            user_session_id=None,
            payload={"session_state": "logged_in"},
            request_id="runtime-status-001",
            idempotency_key="runtime-status-key-001",
        )
        self.assertEqual(self.runtime.handle(status, self.worker_peer).status, "COMPLETED")

    def _record_event(self, event: object) -> None:
        self.events.append(event)
        self.state.enqueue_event(
            "agent.event",
            {"event": event.model_dump(mode="json")},
            idempotency_key=f"runtime-event:{event.worker_id}:{event.sequence}",
            message_id=event.event_id,
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self.state.close()
        self.temp_dir.cleanup()

    def _request(self, message_type: str, peer: SessionPeer, **kwargs: object) -> SessionIpcRequest:
        return SessionIpcRequest(
            request_id=kwargs.pop("request_id", f"runtime-{message_type.replace('.', '-')}-request"),
            idempotency_key=kwargs.pop("idempotency_key", f"runtime-{message_type.replace('.', '-')}-key"),
            message_type=message_type,
            peer=peer,
            worker_id="worker-runtime-001",
            user_session_id=kwargs.pop("user_session_id", "session-runtime-001"),
            sent_at=datetime.now(UTC),
            **kwargs,
        )

    def _context(self, run_id: str, *, terminal: bool = False) -> SessionRunContext:
        return SessionRunContext(
            project_id="project-runtime-001",
            task_id="task-runtime-001",
            run_id=run_id,
            agent_id="agent-runtime-001",
            device_id="device-runtime-001",
            workspace_id="workspace-runtime-001",
            workspace_path=str(self.workspace),
            execution_mode="USER_SESSION",
            allow_remote_terminal=terminal,
        )

    def _wait_for_events(self, run_id: str, expected: set[str]) -> list[str]:
        observed: list[str] = []
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not expected.issubset(set(observed)):
            observed.extend(event.event_type for event in self.runtime.poll_events())
            time.sleep(0.01)
        self.assertTrue(expected.issubset(set(observed)), f"missing={expected - set(observed)} events={observed}")
        return observed

    def test_authorized_start_executes_runner_and_emits_completion_events(self) -> None:
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=self._context("run-runtime-001"),
            payload={
                "adapter_id": "interactive-python",
                "command": [sys.executable, "-c", "print('runtime-ok')"],
            },
            request_id="runtime-start-001",
            idempotency_key="runtime-start-key-001",
        )
        response = self.runtime.handle(request, self.machine_peer)
        self.assertEqual(response.status, "ACCEPTED")
        event_types = self._wait_for_events("run-runtime-001", {"process.started", "process.exited", "run.completed"})
        self.assertIn("process.stdout", event_types)
        self.assertEqual(self.runtime.worker.active_run_ids, set())
        self.assertTrue(any(item["message_type"] == "agent.event" for item in self.state.pending_events(limit=100)))

    def test_container_backend_is_selected_and_manifest_details_reach_completion(self) -> None:
        backend = FakeContainerBackend(self.workspace)
        runtime = SessionWorkerRuntime(
            SessionRuntimeConfig(
                worker_id="worker-container-runtime-001",
                user_session_id="session-container-runtime-001",
                user_sid="S-1-5-21-container-runtime",
                supported_execution_modes=("HEADLESS",),
                capabilities=("session.run",),
            ),
            self.runner,
            container_runners={"docker": backend},
            on_event=self._record_event,
        )
        try:
            worker_peer = SessionPeer(
                peer_id="worker-container-runtime-001",
                peer_kind="user_session_worker",
                user_sid="S-1-5-21-container-runtime",
            )
            machine_peer = SessionPeer(peer_id="machine-container-runtime-001", peer_kind="machine_service")
            status = self._request(
                "session.status",
                worker_peer,
                user_session_id=None,
                request_id="container-status-001",
                idempotency_key="container-status-key-001",
                payload={"session_state": "logged_in"},
            ).model_copy(update={"worker_id": "worker-container-runtime-001", "user_session_id": None})
            self.assertEqual(runtime.handle(status, worker_peer).status, "COMPLETED")
            context = SessionRunContext(
                project_id="project-container-runtime-001",
                task_id="task-container-runtime-001",
                run_id="run-container-runtime-001",
                agent_id="agent-container-runtime-001",
                device_id="device-container-runtime-001",
                workspace_id="workspace-container-runtime-001",
                workspace_path=str(self.workspace),
                execution_mode="HEADLESS",
            )
            request = SessionIpcRequest(
                request_id="container-start-001",
                idempotency_key="container-start-key-001",
                message_type="session.run.start",
                peer=machine_peer,
                worker_id="worker-container-runtime-001",
                user_session_id="session-container-runtime-001",
                context=context,
                sent_at=datetime.now(UTC),
                payload={
                    "adapter_id": "ignored-in-container",
                    "command": ["python", "job.py"],
                    "execution_backend": "container",
                    "container_runtime": "docker",
                    "container_image": "python@sha256:" + "b" * 64,
                },
            )
            response = runtime.handle(request, machine_peer)
            self.assertEqual(response.status, "ACCEPTED", response.model_dump(mode="json"))
            observed: list[str] = []
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not {"process.started", "run.completed"}.issubset(set(observed)):
                observed.extend(event.event_type for event in runtime.poll_events())
                time.sleep(0.01)
            self.assertTrue({"process.started", "run.completed"}.issubset(set(observed)), observed)
            self.assertEqual(backend.started, ["run-container-runtime-001"])
            started = next(event for event in self.events if event.run_id == "run-container-runtime-001" and event.event_type == "process.started")
            self.assertEqual(started.payload["execution_backend"], "container")
            self.assertEqual(observed.count("run.completed"), 1)
        finally:
            runtime.close()

    def test_terminal_input_is_forwarded_to_user_session_process(self) -> None:
        context = self._context("run-runtime-002", terminal=True)
        start = self._request(
            "session.run.start",
            self.machine_peer,
            context=context,
            payload={
                "adapter_id": "interactive-python",
                "command": [sys.executable, "-c", "import sys; print(sys.stdin.readline().strip())"],
            },
            request_id="runtime-start-002",
            idempotency_key="runtime-start-key-002",
        )
        self.assertEqual(self.runtime.handle(start, self.machine_peer).status, "ACCEPTED")
        time.sleep(0.05)
        terminal = self._request(
            "session.terminal.input",
            self.machine_peer,
            context=context,
            payload={"input": "input-ok\n"},
            request_id="runtime-terminal-001",
            idempotency_key="runtime-terminal-key-001",
        )
        self.assertEqual(self.runtime.handle(terminal, self.machine_peer).status, "ACCEPTED")
        event_types = self._wait_for_events("run-runtime-002", {"process.stdout", "run.completed"})
        self.assertIn("process.stdout", event_types)

    def test_terminal_resize_is_fail_closed_without_pty_backend(self) -> None:
        context = self._context("run-runtime-resize-001", terminal=True)
        start = self._request(
            "session.run.start",
            self.machine_peer,
            context=context,
            payload={
                "adapter_id": "interactive-python",
                "command": [sys.executable, "-c", "import time; time.sleep(10)"],
            },
            request_id="runtime-start-resize-001",
            idempotency_key="runtime-start-resize-key-001",
        )
        self.assertEqual(self.runtime.handle(start, self.machine_peer).status, "ACCEPTED")
        resize = self._request(
            "session.terminal.resize",
            self.machine_peer,
            context=context,
            payload={"columns": 120, "rows": 40},
            request_id="runtime-resize-001",
            idempotency_key="runtime-resize-key-001",
        )
        response = self.runtime.handle(resize, self.machine_peer)
        self.assertEqual(response.status, "FAILED")
        self.assertEqual(response.error_code, "session_terminal_resize_failed")
        duplicate = self.runtime.handle(resize, self.machine_peer)
        self.assertEqual(duplicate.status, "DUPLICATE")
        self.runtime.handle(
            self._request(
                "session.run.stop",
                self.machine_peer,
                context=context,
                request_id="runtime-stop-resize-001",
                idempotency_key="runtime-stop-resize-key-001",
            ),
            self.machine_peer,
        )

    def test_output_events_are_emitted_before_run_completion(self) -> None:
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=self._context("run-runtime-stream-001"),
            payload={
                "adapter_id": "interactive-python",
                "command": [sys.executable, "-c", "print('stream-ok')"],
            },
            request_id="runtime-start-stream-001",
            idempotency_key="runtime-start-stream-key-001",
        )
        self.assertEqual(self.runtime.handle(request, self.machine_peer).status, "ACCEPTED")
        deadline = time.monotonic() + 5
        observed = []
        while time.monotonic() < deadline:
            observed.extend(self.runtime.poll_events())
            if any(event.event_type == "run.completed" for event in observed):
                break
            time.sleep(0.01)
        names = [event.event_type for event in observed]
        self.assertIn("process.stdout", names)
        self.assertIn("run.completed", names)
        self.assertLess(names.index("process.stdout"), names.index("run.completed"))

    def test_cli_jsonl_events_are_normalized_into_runtime_events(self) -> None:
        class JsonlAdapter:
            descriptor = AdapterDescriptor(
                adapter_id="codex-jsonl-test",
                agent_name="Codex JSONL test",
                agent_version="0.153.4",
                adapter_version="0.1.0",
                supported_os=(__import__("platform").system(),),
                supported_execution_profiles=("USER_SESSION",),
                supported_capabilities=("cli.jsonl", "tool.events"),
                output_mode="jsonl",
            )

            def build_process_spec(self, request):
                return CommandAdapter(self.descriptor).build_process_spec(request)

            def new_event_parser(self):
                return CodexJsonlEventParser()

        self.runner.adapters["codex-jsonl-test"] = JsonlAdapter()
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=self._context("run-runtime-cli-001"),
            payload={
                "adapter_id": "codex-jsonl-test",
                "command": [
                    sys.executable,
                    "-c",
                    "import json; print(json.dumps({'type':'thread.started','thread_id':'t-1'})); print(json.dumps({'type':'item.started','item':{'type':'command_execution','command':'python x.py'}})); print(json.dumps({'type':'turn.completed'}))",
                ],
            },
            request_id="runtime-cli-start-001",
            idempotency_key="runtime-cli-start-key-001",
        )
        self.assertEqual(self.runtime.handle(request, self.machine_peer).status, "ACCEPTED")
        observed = self._wait_for_events("run-runtime-cli-001", {"run.completed"})
        self.assertIn("tool.started", observed)
        self.assertIn("run.started", observed)
        self.assertEqual(observed.count("run.completed"), 1)

    def test_invalid_start_payload_is_rejected_before_runner_submission(self) -> None:
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=self._context("run-runtime-003"),
            payload={"adapter_id": "interactive-python"},
            request_id="runtime-start-003",
            idempotency_key="runtime-start-key-003",
        )
        response = self.runtime.handle(request, self.machine_peer)
        self.assertEqual(response.status, "FAILED")
        self.assertEqual(response.error_code, "session_run_payload_invalid")
        self.assertEqual(self.runner.active_run_ids(), ())

    def test_named_pipe_request_reaches_broker_runtime_and_runner(self) -> None:
        sid = current_user_sid()
        pipe_name = f"MathAgentPlatform-runtime-{os.getpid()}-{time.time_ns()}"
        server = NamedPipeServer(
            pipe_name,
            PipePeerPolicy(
                peer_id="machine-runtime-pipe-001",
                peer_kind="machine_service",
                allowed_sid=sid,
                allowed_session_ids=(process_session_id(os.getpid()),),
            ),
        )
        self.runtime.worker.session_state = "logged_in"
        errors: list[BaseException] = []

        def serve() -> None:
            try:
                server.serve_once(self.runtime.handle)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        connection = NamedPipeClient(pipe_name).connect(timeout_seconds=5)
        try:
            peer = SessionPeer(
                peer_id="machine-runtime-pipe-001",
                peer_kind="machine_service",
                process_id=os.getpid(),
                windows_session_id=process_session_id(os.getpid()),
                user_sid=None,
            )
            request = self._request(
                "session.run.start",
                peer,
                context=self._context("run-runtime-pipe-001"),
                payload={
                    "adapter_id": "interactive-python",
                    "command": [sys.executable, "-c", "print('pipe-runtime-ok')"],
                },
                request_id="runtime-pipe-start-001",
                idempotency_key="runtime-pipe-start-key-001",
            )
            connection.send_model(request)
            response = connection.receive_response()
            self.assertEqual(response.status, "ACCEPTED")
        finally:
            connection.close()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        events = self._wait_for_events("run-runtime-pipe-001", {"run.completed"})
        self.assertIn("process.stdout", events)

    def test_interactive_desktop_is_fail_closed_without_desktop_executor(self) -> None:
        context = self._context("run-runtime-desktop-001").model_copy(
            update={"execution_mode": "INTERACTIVE_DESKTOP", "allow_desktop_control": True}
        )
        request = self._request(
            "session.run.start",
            self.machine_peer,
            context=context,
            payload={
                "adapter_id": "interactive-python",
                "command": [sys.executable, "-c", "print('must-not-run')"],
            },
            request_id="runtime-start-desktop-001",
            idempotency_key="runtime-start-desktop-key-001",
        )
        response = self.runtime.handle(request, self.machine_peer)
        self.assertEqual(response.status, "FAILED")
        self.assertEqual(response.error_code, "session_interactive_desktop_runtime_unavailable")
        self.assertEqual(self.runner.active_run_ids(), ())
        duplicate = self.runtime.handle(request, self.machine_peer)
        self.assertEqual(duplicate.status, "DUPLICATE")
        self.assertEqual(duplicate.payload["original"]["status"], "FAILED")


if __name__ == "__main__":
    unittest.main()
