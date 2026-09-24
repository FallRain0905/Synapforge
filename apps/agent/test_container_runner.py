from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

from packages.agent_protocol import SessionRunStartPayload
from container_runner import ContainerCommandBuilder, ContainerInvocation, ContainerMount, ContainerPolicy, ContainerRuntime
from machine_service import ProcessResult
from runner import ExecutionProfile, RunnerHandle, RunnerPolicyError, RunnerRequest


class ContainerRunnerContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.input_dir = self.workspace / "input"
        self.output_dir = self.workspace / "output"
        self.input_dir.mkdir()
        self.input_file = self.input_dir / "data.csv"
        self.input_file.write_text("x\n1\n", encoding="utf-8")
        self.image = "python@sha256:" + "a" * 64
        self.policy = ContainerPolicy(allowed_roots=(str(self.root),))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def request(self, **overrides: object) -> RunnerRequest:
        values: dict[str, object] = {
            "project_id": "project-container-001",
            "task_id": "task-container-001",
            "run_id": "run-container-001",
            "agent_id": "agent-container-001",
            "device_id": "device-container-001",
            "workspace_id": "workspace-container-001",
            "workspace_path": str(self.workspace),
            "command": ("python", "script.py"),
            "profile": ExecutionProfile(),
            "input_paths": (str(self.input_file),),
            "output_paths": ("output/result.json",),
            "execution_backend": "container",
            "container_runtime": "docker",
            "container_image": self.image,
            "environment": {"SAFE_MODE": "1", "SECRET_TOKEN": "must-not-be-recorded"},
        }
        values.update(overrides)
        return RunnerRequest(**values)

    def test_mount_plan_keeps_workspace_ro_and_output_parent_rw(self) -> None:
        request = self.request()
        mounts = self.policy.plan_mounts(request)
        self.assertEqual(mounts[0].source, os.path.normcase(str(self.workspace.resolve())))
        self.assertEqual(mounts[0].target, "/workspace")
        self.assertEqual(mounts[0].mode, "ro")
        self.assertEqual(mounts[1].target, "/workspace/output")
        self.assertEqual(mounts[1].mode, "rw")

        runtime = ContainerRuntime("docker", executable=sys.executable)
        invocation = ContainerCommandBuilder.build(runtime, self.image, request, self.policy, mounts)
        self.assertIn("--network", invocation)
        self.assertIn("none", invocation)
        self.assertIn("--read-only", invocation)
        self.assertTrue(
            any(item.startswith("type=bind,source=" + os.path.normcase(str(self.workspace.resolve()))) for item in invocation)
        )
        self.assertTrue(any("target=/workspace/output,rw" in item for item in invocation))

    def test_manifest_redacts_environment_values_and_retains_execution_shape(self) -> None:
        request = self.request()
        runtime = ContainerRuntime("docker", executable=sys.executable)
        invocation = ContainerInvocation(
            runtime="docker",
            image=self.image,
            command=ContainerCommandBuilder.build(runtime, self.image, request, self.policy),
            workspace_path=str(self.workspace.resolve()),
            network_mode="none",
            read_only_rootfs=True,
            read_only_workspace=True,
            limits=self.policy.limits,
            mounts=self.policy.plan_mounts(request),
            environment_keys=tuple(sorted(request.environment)),
        )
        manifest = invocation.as_manifest()
        rendered = repr(manifest)
        self.assertNotIn("must-not-be-recorded", rendered)
        self.assertEqual(manifest["workload_command"], ["python", "script.py"])
        self.assertEqual(manifest["environment_keys"], ["SAFE_MODE", "SECRET_TOKEN"])
        self.assertEqual(manifest["mounts"][0], {"target": "/workspace", "mode": "ro"})

    def test_output_at_workspace_root_is_rejected(self) -> None:
        with self.assertRaisesRegex(RunnerPolicyError, "container_output_requires_dedicated_directory"):
            self.policy.plan_mounts(self.request(output_paths=("result.json",)))

    def test_input_cannot_share_writable_output_directory(self) -> None:
        with self.assertRaisesRegex(RunnerPolicyError, "container_input_overlaps_writable_output"):
            self.policy.plan_mounts(
                self.request(
                    input_paths=("output/input.csv",),
                    output_paths=("output/result.json",),
                )
            )

    def test_session_payload_requires_explicit_container_runtime_and_image(self) -> None:
        with self.assertRaisesRegex(ValueError, "session_container_image_required"):
            SessionRunStartPayload.model_validate(
                {
                    "adapter_id": "container-command",
                    "command": ["python", "job.py"],
                    "execution_backend": "container",
                    "container_runtime": "docker",
                }
            )
        with self.assertRaisesRegex(ValueError, "session_host_container_fields_not_allowed"):
            SessionRunStartPayload.model_validate(
                {
                    "adapter_id": "host-command",
                    "command": ["python", "job.py"],
                    "container_runtime": "docker",
                    "container_image": self.image,
                }
            )


if __name__ == "__main__":
    unittest.main()
