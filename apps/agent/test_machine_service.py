from __future__ import annotations

import asyncio
from dataclasses import MISSING
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from gateway_client import GatewayIdentity
from local_state import LocalAgentState
from machine_service import (
    LocalProcessSupervisor,
    MachineAgentService,
    MachineServiceConfig,
    ProcessSpec,
)


class FakeGatewayClient:
    def __init__(self, state: LocalAgentState) -> None:
        self.state = state
        self.identity = GatewayIdentity(
            device_id="device-001",
            agent_id="agent-001",
            session_id="session-001",
            connection_id="connection-001",
        )
        self.calls = 0
        self.tokens: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = 0

    def queue_event(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        return self.state.enqueue_event(
            message_type,
            payload,
            idempotency_key=idempotency_key,
            message_id=message_id,
        )

    async def run_once(self, uri: str, token: str, **_: Any) -> None:
        self.calls += 1
        self.tokens.append(token)
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


class MachineAgentServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = LocalAgentState(Path(self.temp_dir.name) / "agent.db")
        self.client = FakeGatewayClient(self.state)
        self.config = MachineServiceConfig(
            gateway_uri="ws://gateway.test/ws",
            heartbeat_interval_seconds=0.01,
            send_poll_interval_seconds=0.01,
            reconnect_base_seconds=0.01,
            reconnect_max_seconds=0.02,
            control_poll_interval_seconds=0.005,
            process_stop_timeout_seconds=0.2,
        )

    def tearDown(self) -> None:
        self.state.close()
        self.temp_dir.cleanup()

    async def test_backoff_is_exponential_and_capped(self) -> None:
        self.assertEqual(MachineAgentService.backoff_delay(1, base=0.5, maximum=3), 0.5)
        self.assertEqual(MachineAgentService.backoff_delay(2, base=0.5, maximum=3), 1.0)
        self.assertEqual(MachineAgentService.backoff_delay(4, base=0.5, maximum=3), 3)
        with self.assertRaisesRegex(ValueError, "machine_service_backoff_attempt_invalid"):
            MachineAgentService.backoff_delay(0)

    async def test_token_provider_accepts_sync_and_async_callables(self) -> None:
        sync_service = MachineAgentService(self.client, lambda: "sync-token", self.config)
        self.assertEqual(await sync_service._resolve_token(), "sync-token")

        async def get_token() -> str:
            await asyncio.sleep(0)
            return "async-token"

        async_service = MachineAgentService(self.client, get_token, self.config)
        self.assertEqual(await async_service._resolve_token(), "async-token")

    async def test_service_reconnects_after_connection_failure(self) -> None:
        calls = 0

        async def failing_run_once(*_: Any, **__: Any) -> None:
            nonlocal calls
            calls += 1
            raise ConnectionError("gateway_unavailable")

        self.client.run_once = failing_run_once  # type: ignore[method-assign]
        service = MachineAgentService(self.client, lambda: "device-token", self.config)
        task = asyncio.create_task(service.run_forever())
        for _ in range(200):
            if calls >= 3:
                break
            await asyncio.sleep(0.005)
        await service.stop()
        status = await asyncio.wait_for(task, timeout=1)
        self.assertGreaterEqual(calls, 3)
        self.assertEqual(status, "STOPPED")
        self.assertGreaterEqual(service._reconnect_attempt, 2)

    async def test_heartbeat_is_persisted_while_service_is_connected(self) -> None:
        service = MachineAgentService(
            self.client,
            lambda: "device-token",
            self.config,
            heartbeat_provider=lambda: {"device_id": "device-001", "agent_id": "agent-001"},
        )
        task = asyncio.create_task(service.run_forever())
        await asyncio.wait_for(self.client.started.wait(), timeout=1)
        for _ in range(100):
            events = self.state.pending_events()
            if len(events) >= 2:
                break
            await asyncio.sleep(0.005)
        await service.stop()
        await asyncio.wait_for(task, timeout=1)
        events = self.state.pending_events()
        self.assertGreaterEqual(len(events), 2)
        self.assertTrue(all(event["message_type"] == "agent.heartbeat" for event in events))

    async def test_external_emergency_stop_cancels_connection_and_fails_closed(self) -> None:
        service = MachineAgentService(self.client, lambda: "device-token", self.config)
        managed = await service.process_supervisor.start(
            ProcessSpec(
                run_id="run-emergency",
                command=(sys.executable, "-c", "import time; time.sleep(10)"),
                project_id="project-001",
                task_id="task-001",
                agent_id="agent-001",
                device_id="device-001",
                workspace_id="workspace-001",
                workspace_path=self.temp_dir.name,
                cwd=self.temp_dir.name,
            )
        )
        task = asyncio.create_task(service.run_forever())
        await asyncio.wait_for(self.client.started.wait(), timeout=1)
        self.state.set_emergency_stop("operator_stop")
        status = await asyncio.wait_for(task, timeout=1)
        self.assertEqual(status, "EMERGENCY_STOPPED")
        self.assertGreaterEqual(self.client.cancelled, 1)
        self.assertTrue(self.state.is_emergency_stopped())
        managed_record = next(
            item for item in self.state.list_managed_processes() if item["process_id"] == managed.process_id
        )
        self.assertEqual(managed_record["status"], "CANCELLED")

        second_client = FakeGatewayClient(self.state)
        second_service = MachineAgentService(second_client, lambda: "device-token", self.config)
        self.assertEqual(await second_service.run_forever(), "EMERGENCY_STOPPED")
        second_service.clear_emergency_stop()
        second_task = asyncio.create_task(second_service.run_forever())
        await asyncio.wait_for(second_client.started.wait(), timeout=1)
        await second_service.stop()
        self.assertEqual(await asyncio.wait_for(second_task, timeout=1), "STOPPED")


class LocalProcessSupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state = LocalAgentState(Path(self.temp_dir.name) / "agent.db")
        self.supervisor = LocalProcessSupervisor(self.state, stop_timeout_seconds=0.2)

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
            "workspace_path": self.temp_dir.name,
            "cwd": self.temp_dir.name,
        }
        values.update(overrides)
        return ProcessSpec(**values)

    def test_process_spec_keeps_execution_ownership_fields_required(self) -> None:
        fields = ProcessSpec.__dataclass_fields__
        for name in ("project_id", "agent_id", "device_id", "workspace_id", "workspace_path"):
            self.assertIs(fields[name].default, MISSING)

    async def test_non_interactive_process_gets_closed_stdin(self) -> None:
        """不给 stdin（`stdin_enabled=False`）时子进程拿到的是 /dev/null（EOF），不是父进程的输入流。

        为什么要有这条：继承父进程 stdin 会让执行体读到守护进程的输入并**永远等下去**——
        `opencode run` 实测就是这种挂法（CLOUD-1 踩过：SSH 里跑不重定向 stdin 会挂住，`< /dev/null` 就正常）。
        """

        handle = await self.supervisor.start(
            self.spec(
                "run-stdin-closed",
                (
                    sys.executable,
                    "-c",
                    "import os, stat, sys;"
                    " mode = os.fstat(0).st_mode;"
                    " print('chr' if stat.S_ISCHR(mode) else 'other');"
                    " print('eof' if sys.stdin.read() == '' else 'data')",
                ),
            )
        )
        result = await asyncio.wait_for(self.supervisor.wait(handle.process_id), timeout=5)
        self.assertEqual(result.status, "SUCCEEDED")
        # 字符设备 = /dev/null；读到空 = 立刻 EOF，不会阻塞
        self.assertIn("chr", result.stdout)
        self.assertIn("eof", result.stdout)

    async def test_success_captures_stdout_stderr_and_run_state(self) -> None:
        handle = await self.supervisor.start(
            self.spec(
                "run-success",
                (
                    sys.executable,
                    "-c",
                    "import sys; print('hello stdout'); print('hello stderr', file=sys.stderr)",
                ),
            )
        )
        result = await self.supervisor.wait(handle.process_id)
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("hello stdout", result.stdout)
        self.assertIn("hello stderr", result.stderr)
        process = self.state.list_managed_processes()[0]
        self.assertIsNotNone(process["finished_at"])
        run = self.state.db.execute("SELECT status, payload FROM run_states WHERE run_id = ?", ("run-success",)).fetchone()
        self.assertEqual(run["status"], "SUCCEEDED")
        self.assertIn("hello stdout", run["payload"])

    async def test_output_callback_obeys_max_output_limit(self) -> None:
        chunks: list[str] = []
        supervisor = LocalProcessSupervisor(
            self.state,
            stop_timeout_seconds=0.2,
            output_callback=lambda _spec, _stream, text: chunks.append(text),
        )
        handle = await supervisor.start(
            self.spec(
                "run-output-limit-callback",
                (sys.executable, "-c", "print('abcdefghij')"),
                max_output_bytes=4,
            )
        )
        result = await supervisor.wait(handle.process_id)
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 4)
        self.assertLessEqual(sum(len(chunk) for chunk in chunks), 4)

    async def test_nonzero_exit_is_failed(self) -> None:
        handle = await self.supervisor.start(
            self.spec("run-failed", (sys.executable, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(7)"))
        )
        result = await self.supervisor.wait(handle.process_id)
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.exit_code, 7)
        self.assertIn("bad", result.stderr)

    async def test_timeout_and_explicit_stop_are_recorded(self) -> None:
        timed_out = await self.supervisor.start(
            self.spec("run-timeout", (sys.executable, "-c", "import time; time.sleep(10)"), timeout_seconds=0.02)
        )
        timeout_result = await self.supervisor.wait(timed_out.process_id)
        self.assertEqual(timeout_result.status, "TIMED_OUT")

        cancelled = await self.supervisor.start(
            self.spec("run-cancelled", (sys.executable, "-c", "import time; time.sleep(10)"))
        )
        cancelled_result = await self.supervisor.stop(cancelled.process_id)
        self.assertEqual(cancelled_result.status, "CANCELLED")

    async def test_start_failure_has_finished_record_and_failed_run(self) -> None:
        with self.assertRaises(FileNotFoundError):
            await self.supervisor.start(self.spec("run-start-failure", ("program-that-does-not-exist",)))
        process = self.state.list_managed_processes()[0]
        self.assertEqual(process["status"], "FAILED")
        self.assertIsNotNone(process["finished_at"])
        self.assertTrue(process["stderr"])
        run = self.state.db.execute("SELECT status FROM run_states WHERE run_id = ?", ("run-start-failure",)).fetchone()
        self.assertEqual(run["status"], "FAILED")

    async def test_stop_all_and_orphan_recovery(self) -> None:
        first = await self.supervisor.start(
            self.spec("run-one", (sys.executable, "-c", "import time; time.sleep(10)"))
        )
        second = await self.supervisor.start(
            self.spec("run-two", (sys.executable, "-c", "import time; time.sleep(10)"))
        )
        results = await self.supervisor.stop_all()
        self.assertEqual({result.process_id for result in results}, {first.process_id, second.process_id})
        self.assertTrue(all(result.status == "CANCELLED" for result in results))
        self.assertEqual(self.supervisor.active_process_ids(), [])

        self.state.register_process("orphan-process", "orphan-run", [sys.executable], None, 123, "RUNNING")
        self.assertEqual(self.supervisor.recover_orphan_records(), 1)
        orphan = next(
            item for item in self.state.list_managed_processes() if item["process_id"] == "orphan-process"
        )
        self.assertEqual(orphan["status"], "ABANDONED")


if __name__ == "__main__":
    unittest.main()
