from __future__ import annotations

import asyncio
import platform
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Sequence

from local_state import LocalAgentState
from machine_service import LocalProcessSupervisor
from runner import (
    AdapterDescriptor,
    CommandAdapter,
    ExecutionProfile,
    LocalRunner,
    PythonAdapter,
    RunnerPolicyError,
    RunnerRequest,
    WorkspacePolicy,
)


class AllowListedNetwork:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def validate(self, profile: ExecutionProfile, allowed_hosts: Sequence[str]) -> None:
        self.calls.append(tuple(allowed_hosts))


class RunnerContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "project-workspace"
        self.workspace.mkdir()
        self.state = LocalAgentState(self.root / "agent.db")
        self.supervisor = LocalProcessSupervisor(self.state, stop_timeout_seconds=0.2)
        self.descriptor = AdapterDescriptor(
            adapter_id="python-dev",
            agent_name="Python",
            agent_version=platform.python_version(),
            adapter_version="0.1.0",
            supported_os=(platform.system(),),
            supported_execution_profiles=("HEADLESS",),
        )
        self.adapter = CommandAdapter(self.descriptor)
        self.policy = WorkspacePolicy(
            allowed_roots=(str(self.root),),
            allowed_executables=(sys.executable,),
            allowed_environment_keys=("PYTHONUNBUFFERED",),
        )

    def tearDown(self) -> None:
        self.state.close()
        self.temp_dir.cleanup()

    def request(self, **overrides: object) -> RunnerRequest:
        values: dict[str, object] = {
            "project_id": "project-001",
            "task_id": "task-001",
            "run_id": "run-001",
            "agent_id": "agent-001",
            "device_id": "device-001",
            "workspace_id": "workspace-001",
            "workspace_path": str(self.workspace),
            "command": (sys.executable, "-c", "print('runner-ok')"),
        }
        values.update(overrides)
        return RunnerRequest(**values)

    async def test_command_adapter_executes_only_after_workspace_and_binary_validation(self) -> None:
        runner = LocalRunner(self.supervisor, self.policy, [self.adapter])
        result = await runner.run("python-dev", self.request())
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertIn("runner-ok", result.stdout)

    async def test_runner_can_start_write_stdin_and_stop_without_consuming_handle_twice(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="interactive-python",
            agent_name="Python",
            agent_version=platform.python_version(),
            adapter_version="0.1.0",
            supported_os=(platform.system(),),
            supported_execution_profiles=("USER_SESSION",),
        )
        adapter = CommandAdapter(descriptor)
        runner = LocalRunner(self.supervisor, self.policy, [adapter])
        request = self.request(
            run_id="run-interactive-001",
            profile=ExecutionProfile(mode="USER_SESSION", requires_user_session=True, allow_remote_terminal=True),
            command=(sys.executable, "-c", "import sys; print(sys.stdin.readline().strip())"),
        )
        handle = await runner.start("interactive-python", request)
        self.assertEqual(handle.run_id, "run-interactive-001")
        await runner.write_stdin(handle.run_id, "hello\n")
        result = await runner.wait(handle.run_id)
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertIn("hello", result.stdout)

    async def test_runner_stop_requests_termination_and_waits_for_result(self) -> None:
        descriptor = AdapterDescriptor(
            adapter_id="interactive-python-stop",
            agent_name="Python",
            agent_version=platform.python_version(),
            adapter_version="0.1.0",
            supported_os=(platform.system(),),
            supported_execution_profiles=("USER_SESSION",),
        )
        runner = LocalRunner(self.supervisor, self.policy, [CommandAdapter(descriptor)])
        request = self.request(
            run_id="run-stop-001",
            profile=ExecutionProfile(mode="USER_SESSION", requires_user_session=True),
            command=(sys.executable, "-c", "import time; time.sleep(30)"),
        )
        await runner.start("interactive-python-stop", request)
        await runner.request_stop(request.run_id)
        result = await runner.wait(request.run_id)
        self.assertEqual(result.status, "CANCELLED")

    async def test_python_adapter_binds_run_context_and_interpreter(self) -> None:
        runner = LocalRunner(
            self.supervisor,
            self.policy,
            [PythonAdapter(self.descriptor)],
        )
        result = await runner.run("python-dev", self.request())
        self.assertEqual(result.status, "SUCCEEDED")
        process = self.state.list_managed_processes()[0]
        self.assertEqual(process["project_id"], "project-001")
        self.assertEqual(process["task_id"], "task-001")
        self.assertEqual(process["agent_id"], "agent-001")
        self.assertEqual(process["device_id"], "device-001")
        self.assertEqual(process["workspace_id"], "workspace-001")
        self.assertEqual(Path(process["workspace_path"]), self.workspace)

    async def test_workspace_and_nested_input_output_paths_are_checked(self) -> None:
        runner = LocalRunner(self.supervisor, self.policy, [self.adapter])
        outside = self.root / "other-project" / "secret.csv"
        outside.parent.mkdir()
        with self.assertRaisesRegex(RunnerPolicyError, "input_outside_workspace"):
            await runner.run("python-dev", self.request(input_paths=(str(outside),)))
        outside_workspace = self.root.parent / (self.root.name + "-outside-workspace")
        with self.assertRaisesRegex(RunnerPolicyError, "workspace_path_not_allowlisted"):
            await runner.run("python-dev", self.request(workspace_path=str(outside_workspace)))

    async def test_binary_and_environment_allowlists_fail_closed(self) -> None:
        runner = LocalRunner(self.supervisor, self.policy, [self.adapter])
        with self.assertRaisesRegex(RunnerPolicyError, "executable_not_allowlisted"):
            await runner.run("python-dev", self.request(command=("cmd.exe", "/c", "echo unsafe")))
        with self.assertRaisesRegex(RunnerPolicyError, "environment_key_not_allowlisted"):
            await runner.run("python-dev", self.request(environment={"SECRET_TOKEN": "do-not-pass"}))

    async def test_execution_profile_must_match_adapter_and_capabilities(self) -> None:
        runner = LocalRunner(self.supervisor, self.policy, [self.adapter])
        with self.assertRaisesRegex(RunnerPolicyError, "adapter_execution_profile_not_supported"):
            await runner.run(
                "python-dev",
                self.request(profile=ExecutionProfile(mode="USER_SESSION", requires_user_session=True)),
            )
        with self.assertRaisesRegex(RunnerPolicyError, "headless_cannot_allow_desktop_control"):
            ExecutionProfile(allow_desktop_control=True)
        with self.assertRaisesRegex(RunnerPolicyError, "execution_mode_invalid"):
            ExecutionProfile(mode="UNKNOWN")  # type: ignore[arg-type]
        with self.assertRaisesRegex(RunnerPolicyError, "network_policy_invalid"):
            ExecutionProfile(network_policy="UNKNOWN")  # type: ignore[arg-type]

    async def test_non_denied_network_requires_explicit_enforcer(self) -> None:
        allowlisted_policy = WorkspacePolicy(
            allowed_roots=(str(self.root),),
            allowed_executables=(sys.executable,),
            allowed_network_hosts=("pypi.org",),
        )
        runner = LocalRunner(self.supervisor, allowlisted_policy, [self.adapter])
        with self.assertRaisesRegex(RunnerPolicyError, "network_enforcement_unavailable"):
            await runner.run(
                "python-dev",
                self.request(profile=ExecutionProfile(network_policy="allow-listed")),
            )

        enforcer = AllowListedNetwork()
        runner = LocalRunner(
            self.supervisor,
            allowlisted_policy,
            [self.adapter],
            network_enforcer=enforcer,
        )
        result = await runner.run(
            "python-dev",
            self.request(profile=ExecutionProfile(network_policy="allow-listed")),
        )
        self.assertEqual(result.status, "SUCCEEDED")
        self.assertEqual(enforcer.calls, [("pypi.org",)])

    async def test_adapter_cannot_escape_workspace_or_change_run_binding(self) -> None:
        class WrongCwdAdapter:
            def __init__(self, descriptor: AdapterDescriptor, cwd: str, run_id: str) -> None:
                self.descriptor = descriptor
                self.cwd = cwd
                self.run_id = run_id

            def build_process_spec(self, request: RunnerRequest):
                from machine_service import ProcessSpec

                return ProcessSpec(
                    run_id=self.run_id,
                    command=request.command,
                    cwd=self.cwd,
                    inherit_environment=False,
                    project_id=request.project_id,
                    task_id=request.task_id,
                    agent_id=request.agent_id,
                    device_id=request.device_id,
                    workspace_id=request.workspace_id,
                    workspace_path=request.workspace_path,
                )

        with self.assertRaisesRegex(RunnerPolicyError, "adapter_cwd_outside_workspace"):
            await LocalRunner(
                self.supervisor,
                self.policy,
                [WrongCwdAdapter(self.descriptor, str(self.root), "run-001")],
            ).run("python-dev", self.request())
        with self.assertRaisesRegex(RunnerPolicyError, "adapter_run_id_mismatch"):
            await LocalRunner(
                self.supervisor,
                self.policy,
                [WrongCwdAdapter(self.descriptor, str(self.workspace), "other-run")],
            ).run("python-dev", self.request())

    async def test_adapter_cannot_reenable_inherited_host_environment(self) -> None:
        class InheritingAdapter:
            def __init__(self, descriptor: AdapterDescriptor) -> None:
                self.descriptor = descriptor

            def build_process_spec(self, request: RunnerRequest):
                from machine_service import ProcessSpec

                return ProcessSpec(
                    run_id=request.run_id,
                    command=request.command,
                    cwd=request.workspace_path,
                    inherit_environment=True,
                    project_id=request.project_id,
                    task_id=request.task_id,
                    agent_id=request.agent_id,
                    device_id=request.device_id,
                    workspace_id=request.workspace_id,
                    workspace_path=request.workspace_path,
                )

        with self.assertRaisesRegex(RunnerPolicyError, "inherited_environment_not_allowed"):
            await LocalRunner(self.supervisor, self.policy, [InheritingAdapter(self.descriptor)]).run("python-dev", self.request())

    async def test_adapter_cannot_forge_process_ownership_context(self) -> None:
        class WrongOwnerAdapter:
            def __init__(self, descriptor: AdapterDescriptor) -> None:
                self.descriptor = descriptor

            def build_process_spec(self, request: RunnerRequest):
                from machine_service import ProcessSpec

                return ProcessSpec(
                    run_id=request.run_id,
                    command=request.command,
                    project_id="other-project",
                    task_id=request.task_id,
                    agent_id=request.agent_id,
                    device_id=request.device_id,
                    workspace_id=request.workspace_id,
                    workspace_path=request.workspace_path,
                    cwd=request.workspace_path,
                    inherit_environment=False,
                )

        with self.assertRaisesRegex(RunnerPolicyError, "adapter_project_id_mismatch"):
            await LocalRunner(
                self.supervisor,
                self.policy,
                [WrongOwnerAdapter(self.descriptor)],
            ).run("python-dev", self.request())


if __name__ == "__main__":
    unittest.main()
