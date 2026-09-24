import csv
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from information_boundary import AccessObservationCapture, FileObservation
from local_state import LocalAgentState
from runner import AdapterDescriptor, CommandAdapter, LocalRunner, RunnerRequest, WorkspacePolicy
from machine_service import LocalProcessSupervisor
from windows_etw_observer import WindowsEtwAccessObservationProvider, WindowsEtwConfig


class FakeEtwCommandRunner:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.commands: list[tuple[str, ...]] = []

    def run(self, command: list[str] | tuple[str, ...], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        self.commands.append(tuple(command))
        if command and str(command[0]).lower().endswith("logman.exe") and len(command) > 1 and command[1] == "create":
            etl_output = Path(command[command.index("-o") + 1])
            etl_output.parent.mkdir(parents=True, exist_ok=True)
            etl_output.write_bytes(b"fake-etl-payload")
        if command and str(command[0]).lower().endswith("tracerpt.exe"):
            output = Path(command[command.index("-o") + 1])
            with output.open("w", encoding="utf-16", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["Provider", "ProcessId", "EventName", "FileName", "RemoteAddress", "RemotePort", "Protocol"])
                writer.writerow(["Microsoft-Windows-Kernel-File", "123", "FileIo_Read", str(self.workspace / "input" / "data.csv"), "", "", ""])
                writer.writerow(["Microsoft-Windows-Kernel-File", "999", "FileIo_Read", str(self.workspace / "ignored.csv"), "", "", ""])
                writer.writerow(["Microsoft-Windows-Kernel-Network", "456", "TcpSend", "", "203.0.113.10", "443", "tcp"])
        return subprocess.CompletedProcess(list(command), 0, "", "")


class FakeProcessTree:
    def pids_for(self, root_pid: int) -> set[int]:
        self.root_pid = root_pid
        return {root_pid, 456}


class FailingCleanupCommandRunner(FakeEtwCommandRunner):
    """logman delete fails so cleanup diagnostics must be recorded."""

    def run(self, command, timeout_seconds):
        result = super().run(command, timeout_seconds)
        if len(command) >= 2 and command[1] == "delete":
            return subprocess.CompletedProcess(list(command), 1, "", "session not found")
        return result


class FakeRunnerObserver:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.started = False
        self.bound_pid: int | None = None

    def start(self, workspace_path: str, process_id: str | None = None) -> None:
        self.started = workspace_path == str(self.workspace)

    def bind_process(self, process_id: int | str | None) -> None:
        self.bound_pid = int(process_id) if process_id is not None else None

    def stop(self) -> AccessObservationCapture:
        return AccessObservationCapture(
            "captured",
            (FileObservation(str(self.workspace / "input" / "data.csv"), observation_source="system"),),
        )


class WindowsEtwObserverTests(unittest.TestCase):
    def test_etw_csv_is_filtered_to_bound_process_tree_and_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            commands = FakeEtwCommandRunner(workspace)
            provider = WindowsEtwAccessObservationProvider(
                WindowsEtwConfig(output_directory=str(root / "traces")),
                command_runner=commands,
                process_tree=FakeProcessTree(),
                is_windows=lambda: True,
                which=lambda name: f"{name}.exe",
            )
            provider.start(str(workspace))
            provider.bind_process(123)
            capture = provider.stop()

            self.assertEqual(capture.status, "captured")
            self.assertEqual([item.path for item in capture.file_observations], [str(workspace / "input" / "data.csv")])
            self.assertEqual(len(capture.network_observations), 1)
            self.assertEqual(capture.network_observations[0].host, "203.0.113.10")
            self.assertEqual(capture.network_observations[0].port, 443)
            self.assertIn("-p", commands.commands[0])

    def test_file_path_can_be_extracted_from_tracerpt_description(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = WindowsEtwAccessObservationProvider(is_windows=lambda: True)
            provider._workspace_path = str(workspace)
            provider._bound_pids = {123}
            rows = [{
                "Provider": "Microsoft-Windows-Kernel-File",
                "ProcessId": "123",
                "EventName": "FileIo_Read",
                "Description": f"FileName: {workspace / 'input' / 'description.csv'}",
            }]
            files: list[FileObservation] = []
            network: list[object] = []
            provider._parse_row(rows[0], files, network, {123})
            self.assertEqual(files[0].path, str(workspace / "input" / "description.csv"))

    def test_missing_windows_support_fails_closed(self) -> None:
        provider = WindowsEtwAccessObservationProvider(is_windows=lambda: False)
        with self.assertRaisesRegex(RuntimeError, "requires_windows"):
            provider.start("C:/workspace")

    def test_unbound_session_does_not_return_unscoped_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = WindowsEtwAccessObservationProvider(
                WindowsEtwConfig(output_directory=str(root / "traces")),
                command_runner=FakeEtwCommandRunner(workspace),
                process_tree=FakeProcessTree(),
                is_windows=lambda: True,
                which=lambda name: f"{name}.exe",
            )
            provider.start(str(workspace))
            capture = provider.stop()
            self.assertEqual(capture.status, "not_captured")
            self.assertEqual(capture.reason, "windows_etw_process_binding_missing")

    def test_local_runner_binds_observer_to_real_child_pid_and_returns_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            (workspace / "input").mkdir(parents=True)
            state = LocalAgentState(root / "agent.db")
            try:
                observer = FakeRunnerObserver(workspace)
                descriptor = AdapterDescriptor(
                    adapter_id="python-observed",
                    agent_name="Python",
                    agent_version=sys.version.split()[0],
                    adapter_version="0.1.0",
                    supported_os=(__import__("platform").system(),),
                    supported_execution_profiles=("HEADLESS",),
                )
                runner = LocalRunner(
                    LocalProcessSupervisor(state, stop_timeout_seconds=0.2),
                    WorkspacePolicy((str(root),), (sys.executable,)),
                    [CommandAdapter(descriptor)],
                    access_observation_provider=observer,
                )
                request = RunnerRequest(
                    project_id="project-001",
                    task_id="task-001",
                    run_id="run-observed-001",
                    agent_id="agent-001",
                    device_id="device-001",
                    workspace_id="workspace-001",
                    workspace_path=str(workspace),
                    command=(sys.executable, "-c", "pass"),
                    input_paths=("input/data.csv",),
                    data_access_policy={"observation_mode": "system"},
                )
                result = __import__("asyncio").run(runner.run("python-observed", request))
                self.assertEqual(result.status, "SUCCEEDED")
                self.assertEqual(result.observed_input_files, (str(workspace / "input" / "data.csv"),))
                self.assertEqual(result.access_observation_status, "captured")
                self.assertIsNotNone(observer.bound_pid)
            finally:
                state.close()

    def test_stop_returns_diagnostics_with_session_and_trace_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            commands = FakeEtwCommandRunner(workspace)
            provider = WindowsEtwAccessObservationProvider(
                WindowsEtwConfig(output_directory=str(root / "traces")),
                command_runner=commands,
                process_tree=FakeProcessTree(),
                is_windows=lambda: True,
                which=lambda name: f"{name}.exe",
            )
            provider.start(str(workspace))
            provider.bind_process(123)
            capture = provider.stop()

            self.assertIsNotNone(capture.diagnostics)
            diagnostics = dict(capture.diagnostics)
            self.assertEqual(diagnostics["adapter"], "windows-etw")
            self.assertEqual(diagnostics["stop_status"], "ok")
            self.assertEqual(diagnostics["cleanup_status"], "ok")
            self.assertEqual(diagnostics["file_events"], 1)
            self.assertEqual(diagnostics["network_events"], 1)
            self.assertEqual(sorted(diagnostics["bound_process_ids"]), [123, 456])
            self.assertIn("started_at", diagnostics)
            self.assertIn("finished_at", diagnostics)
            sessions = diagnostics["sessions"]
            self.assertEqual(len(sessions), 2)
            file_session = next(name for name in sessions if name.endswith("-file"))
            self.assertEqual(sessions[file_session]["provider"], "Microsoft-Windows-Kernel-File")
            self.assertTrue(sessions[file_session]["retained"])
            self.assertIsNotNone(sessions[file_session]["etl_sha256"])
            self.assertIsNotNone(sessions[file_session]["csv_sha256"])
            network_session = next(name for name in sessions if name.endswith("-network"))
            self.assertEqual(sessions[network_session]["provider"], "Microsoft-Windows-Kernel-Network")

    def test_delete_failure_is_recorded_in_cleanup_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = WindowsEtwAccessObservationProvider(
                WindowsEtwConfig(output_directory=str(root / "traces")),
                command_runner=FailingCleanupCommandRunner(workspace),
                process_tree=FakeProcessTree(),
                is_windows=lambda: True,
                which=lambda name: f"{name}.exe",
            )
            provider.start(str(workspace))
            provider.bind_process(123)
            capture = provider.stop()

            self.assertEqual(capture.status, "captured")
            self.assertEqual(capture.diagnostics["cleanup_status"], "failed")
            self.assertTrue(capture.diagnostics["cleanup_errors"])
            self.assertEqual(len(capture.diagnostics["cleanup_errors"]), 2)

    def test_stop_after_start_failure_returns_binding_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            provider = WindowsEtwAccessObservationProvider(
                WindowsEtwConfig(output_directory=str(root / "traces")),
                command_runner=FakeEtwCommandRunner(workspace),
                process_tree=FakeProcessTree(),
                is_windows=lambda: True,
                which=lambda name: f"{name}.exe",
            )
            provider.start(str(workspace))
            capture = provider.stop()
            self.assertEqual(capture.status, "not_captured")
            self.assertEqual(capture.diagnostics["stop_status"], "failed")

    def test_runtime_dependency_reads_are_classified_and_not_undeclared(self) -> None:
        from information_boundary import FileObservation, InformationBoundaryAudit

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            runtime_root = root / "runtime"
            (workspace / "input").mkdir(parents=True)
            runtime_root.mkdir()
            result = InformationBoundaryAudit.evaluate(
                str(workspace),
                ["input/data.csv"],
                [
                    FileObservation(str(workspace / "input" / "data.csv"), observation_source="system"),
                    FileObservation(str(runtime_root / "python312.dll"), observation_source="system"),
                    FileObservation(str(workspace / "input" / "secret.csv"), observation_source="system"),
                ],
                {"runtime_dependency_roots": [str(runtime_root)], "observation_mode": "system"},
            )
            self.assertTrue(result["allowed"] is False)
            self.assertEqual(result["runtime_dependencies"], [str(runtime_root / "python312.dll")])
            self.assertEqual(result["undeclared"], [str(workspace / "input" / "secret.csv")])
            categories = {record["path"]: record["category"] for record in result["observed"]}
            self.assertEqual(categories[str(runtime_root / "python312.dll")], "runtime_dependency")
            self.assertEqual(categories[str(workspace / "input" / "data.csv")], "task_input")

    def test_declared_file_inside_runtime_root_is_still_a_task_input(self) -> None:
        from information_boundary import FileObservation, InformationBoundaryAudit

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            shared = root / "shared"
            (workspace / "input").mkdir(parents=True)
            shared.mkdir()
            (shared / "model.pkl").write_text("data", encoding="utf-8")
            result = InformationBoundaryAudit.evaluate(
                str(workspace),
                [str(shared / "model.pkl")],
                [FileObservation(str(shared / "model.pkl"), observation_source="system")],
                {"runtime_dependency_roots": [str(shared)], "observation_mode": "system"},
            )
            # Declaration keeps the file a task input rather than a runtime
            # dependency; the read still breaches the workspace boundary and
            # is reported by the existing outside-workspace rule.
            self.assertEqual(result["runtime_dependencies"], [])
            self.assertEqual(result["undeclared"], [])
            self.assertEqual(result["observed"][0]["category"], "task_input")
            self.assertTrue(any(item["code"] == "observed_input_outside_workspace" for item in result["violations"]))
            self.assertFalse(result["allowed"])


if __name__ == "__main__":
    unittest.main()
