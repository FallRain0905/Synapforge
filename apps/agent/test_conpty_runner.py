from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from datetime import UTC, datetime

from conpty_runner import ConPtyError, ConPtyProcessSupervisor, conpty_capability
from local_state import LocalAgentState
from machine_service import ProcessSpec
from runner import AdapterDescriptor, CommandAdapter, ConPtyAdapter, ExecutionProfile, LocalRunner, RunnerPolicyError, WorkspacePolicy
from session_runtime import SessionRuntimeConfig, SessionWorkerRuntime
from packages.agent_protocol import SessionIpcRequest, SessionPeer, SessionRunContext


@unittest.skipUnless(os.name == "nt", "ConPTY requires Windows")
class ConPtyProcessSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        if not conpty_capability()["available"]:
            self.skipTest("pywinpty is not installed")
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state = LocalAgentState(self.root / "agent.db")
        self.supervisor = ConPtyProcessSupervisor(self.state, stop_timeout_seconds=0.5)

    def tearDown(self) -> None:
        self.state.close()
        self.temp_dir.cleanup()

    def spec(self, run_id: str, command: tuple[str, ...], **overrides: object) -> ProcessSpec:
        values: dict[str, object] = {
            "run_id": run_id,
            "command": command,
            "project_id": "project-001",
            "task_id": "task-001",
            "agent_id": "agent-001",
            "device_id": "device-001",
            "workspace_id": "workspace-001",
            "workspace_path": str(self.root),
            "cwd": str(self.root),
            "stdin_enabled": True,
            "terminal_backend": "CONPTY",
            "terminal_columns": 100,
            "terminal_rows": 30,
        }
        values.update(overrides)
        return ProcessSpec(**values)

    async def test_real_conpty_captures_output_and_accepts_input(self) -> None:
        script = self.root / "io_test.py"
        script.write_text("import sys\nprint('pty-ready', flush=True)\nprint('got:' + sys.stdin.readline().strip())\n", encoding="utf-8")
        handle = await self.supervisor.start(
            self.spec(
                "conpty-io-001",
                (sys.executable, str(script)),
            )
        )
        await self.supervisor.write_stdin(handle.process_id, "conpty-input\r")
        result = await self.supervisor.wait(handle.process_id)
        self.assertEqual(result.status, "SUCCEEDED", result)
        self.assertIn("pty-ready", result.stdout)
        self.assertIn("got:conpty-input", result.stdout)
        self.assertEqual(result.stderr, "")

    async def test_real_conpty_resize_changes_terminal_dimensions(self) -> None:
        script = self.root / "resize_test.py"
        script.write_text("import time\nprint('resize-ready', flush=True)\ntime.sleep(2)\n", encoding="utf-8")
        handle = await self.supervisor.start(
            self.spec(
                "conpty-resize-001",
                (sys.executable, str(script)),
            )
        )
        await asyncio.sleep(0.15)
        await self.supervisor.resize(handle.process_id, 120, 40)
        result = await self.supervisor.wait(handle.process_id)
        self.assertEqual(result.status, "SUCCEEDED", result)
        self.assertIn("resize-ready", result.stdout)

    async def test_real_conpty_cancel_is_recorded(self) -> None:
        handle = await self.supervisor.start(
            self.spec("conpty-cancel-001", (sys.executable, "-c", "import time; time.sleep(10)"))
        )
        await self.supervisor.request_stop(handle.process_id)
        result = await self.supervisor.wait(handle.process_id)
        self.assertEqual(result.status, "CANCELLED")

    async def test_non_conpty_spec_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConPtyError, "conpty_requires_conpty_process_spec"):
            await self.supervisor.start(self.spec("conpty-invalid-001", (sys.executable, "-c", "pass"), terminal_backend="PIPE"))

    async def test_hybrid_local_runner_routes_explicit_conpty_request(self) -> None:
        script = self.root / "hybrid_test.py"
        script.write_text("print('hybrid-conpty-ok', flush=True)\n", encoding="utf-8")
        from machine_service import LocalProcessSupervisor
        from conpty_runner import HybridProcessSupervisor

        descriptor = AdapterDescriptor(
            adapter_id="conpty-command",
            agent_name="ConPTY test agent",
            agent_version="test",
            adapter_version="0.1.0",
            supported_os=("Windows",),
            supported_execution_profiles=("USER_SESSION",),
        )
        runner = LocalRunner(
            HybridProcessSupervisor(
                LocalProcessSupervisor(self.state, stop_timeout_seconds=0.5),
                self.supervisor,
            ),
            WorkspacePolicy(allowed_roots=(str(self.root),), allowed_executables=(sys.executable,)),
            [CommandAdapter(descriptor)],
            current_os="Windows",
        )
        result = await runner.run(
            "conpty-command",
            self._runner_request("hybrid-run-001", (sys.executable, str(script))),
        )
        self.assertEqual(result.status, "SUCCEEDED", result)
        self.assertIn("hybrid-conpty-ok", result.stdout)

    async def test_conpty_adapter_rejects_pipe_requests(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="conpty-only",
            agent_name="ConPTY adapter",
            agent_version="test",
            adapter_version="0.1.0",
            supported_os=("Windows",),
            supported_execution_profiles=("USER_SESSION",),
        )
        from runner import RunnerRequest

        request = RunnerRequest(
            project_id="project-001",
            task_id="task-001",
            run_id="conpty-adapter-001",
            agent_id="agent-001",
            device_id="device-001",
            workspace_id="workspace-001",
            workspace_path=str(self.root),
            command=(sys.executable, "-c", "pass"),
            profile=ExecutionProfile(mode="USER_SESSION", requires_user_session=True),
            terminal_backend="PIPE",
        )
        with self.assertRaisesRegex(RunnerPolicyError, "conpty_adapter_requires_conpty_backend"):
            ConPtyAdapter(descriptor).build_process_spec(request)

    async def test_hybrid_recovers_managed_process_records(self) -> None:
        from machine_service import LocalProcessSupervisor
        from conpty_runner import HybridProcessSupervisor

        self.state.register_process(
            "orphan-conpty-001",
            "orphan-run-001",
            [sys.executable, "-c", "pass"],
            str(self.root),
            None,
            "RUNNING",
            project_id="project-001",
            task_id="task-001",
            agent_id="agent-001",
            device_id="device-001",
            workspace_id="workspace-001",
            workspace_path=str(self.root),
        )
        hybrid = HybridProcessSupervisor(
            LocalProcessSupervisor(self.state, stop_timeout_seconds=0.5),
            self.supervisor,
        )
        self.assertEqual(hybrid.recover_orphan_records(), 1)
        record = self.state.list_managed_processes()[0]
        self.assertEqual(record["status"], "ABANDONED")

    async def test_session_runtime_routes_conpty_and_resize_event(self) -> None:
        script = self.root / "runtime_conpty_test.py"
        script.write_text("import time\nprint('runtime-pty-ready', flush=True)\ntime.sleep(1)\n", encoding="utf-8")
        from machine_service import LocalProcessSupervisor
        from conpty_runner import HybridProcessSupervisor

        descriptor = AdapterDescriptor(
            adapter_id="runtime-conpty",
            agent_name="ConPTY runtime test agent",
            agent_version="test",
            adapter_version="0.1.0",
            supported_os=("Windows",),
            supported_execution_profiles=("USER_SESSION",),
        )
        runner = LocalRunner(
            HybridProcessSupervisor(
                LocalProcessSupervisor(self.state, stop_timeout_seconds=0.5),
                ConPtyProcessSupervisor(self.state, stop_timeout_seconds=0.5),
            ),
            WorkspacePolicy(allowed_roots=(str(self.root),), allowed_executables=(sys.executable,)),
            [CommandAdapter(descriptor)],
            current_os="Windows",
        )
        runtime = SessionWorkerRuntime(
            SessionRuntimeConfig(
                worker_id="worker-conpty-001",
                user_session_id="session-conpty-001",
                user_sid="S-1-5-21-conpty",
                supported_execution_modes=("USER_SESSION",),
                capabilities=("session.run", "terminal.input", "terminal.resize"),
            ),
            runner,
        )
        peer = SessionPeer(peer_id="machine-conpty-001", peer_kind="machine_service")
        worker_peer = SessionPeer(peer_id="worker-conpty-001", peer_kind="user_session_worker", user_sid="S-1-5-21-conpty")
        context = SessionRunContext(
            project_id="project-001",
            task_id="task-001",
            run_id="run-runtime-conpty-001",
            agent_id="agent-001",
            device_id="device-001",
            workspace_id="workspace-001",
            workspace_path=str(self.root),
            execution_mode="USER_SESSION",
            allow_remote_terminal=True,
        )

        def request(message_type: str, key: str, payload: dict[str, object] | None = None) -> SessionIpcRequest:
            return SessionIpcRequest(
                request_id=f"request-{key}",
                idempotency_key=f"idempotency-{key}",
                message_type=message_type,
                peer=peer,
                worker_id="worker-conpty-001",
                user_session_id="session-conpty-001",
                context=context,
                sent_at=datetime.now(UTC),
                payload=payload or {},
            )

        try:
            status = request("session.status", "status", {"session_state": "logged_in"})
            status = status.model_copy(update={"peer": worker_peer})
            self.assertEqual(runtime.handle(status, worker_peer).status, "COMPLETED")
            start = request(
                "session.run.start",
                "start",
                {
                    "adapter_id": "runtime-conpty",
                    "command": [sys.executable, str(script)],
                    "terminal_backend": "CONPTY",
                    "terminal_size": {"columns": 100, "rows": 30},
                },
            )
            self.assertEqual(runtime.handle(start, peer).status, "ACCEPTED")
            for _ in range(100):
                if any(event.event_type == "process.started" for event in runtime.poll_events()):
                    break
                await asyncio.sleep(0.01)
            resize = request("session.terminal.resize", "resize", {"columns": 120, "rows": 40})
            self.assertEqual(runtime.handle(resize, peer).status, "ACCEPTED")
            deadline = asyncio.get_running_loop().time() + 5
            events = []
            while asyncio.get_running_loop().time() < deadline:
                events.extend(runtime.poll_events())
                if any(event.event_type == "run.completed" for event in events):
                    break
                await asyncio.sleep(0.01)
            self.assertIn("terminal.resized", [event.event_type for event in events])
            self.assertIn("run.completed", [event.event_type for event in events])
        finally:
            runtime.close()

    def _runner_request(self, run_id: str, command: tuple[str, ...]):
        from runner import RunnerRequest

        return RunnerRequest(
            project_id="project-001",
            task_id="task-001",
            run_id=run_id,
            agent_id="agent-001",
            device_id="device-001",
            workspace_id="workspace-001",
            workspace_path=str(self.root),
            command=command,
            profile=ExecutionProfile(mode="USER_SESSION", requires_user_session=True),
            terminal_backend="CONPTY",
        )


if __name__ == "__main__":
    unittest.main()
