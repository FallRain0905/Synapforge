from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from container_runner import ContainerInvocation, ContainerPolicy, ContainerRuntime, ContainerCommandBuilder
from machine_service import ProcessResult
from reproducible_rerun import ReplayComparator, ReplayPolicyError, ReproducibleReplayPlanner
from result_uploader import OutputDiscovery, RunManifestBuilder
from runner import ExecutionProfile, RunnerRequest


class ReproducibleRerunContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.source = self.root / "source"
        self.target = self.root / "target"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _prepare_workspace(self, workspace: Path) -> RunnerRequest:
        (workspace / "input").mkdir(parents=True)
        (workspace / "output").mkdir(parents=True)
        (workspace / "scripts").mkdir(parents=True)
        (workspace / "input" / "data.csv").write_text("x\n1\n", encoding="utf-8")
        (workspace / "scripts" / "solve.py").write_text("print('ok')\n", encoding="utf-8")
        (workspace / "output" / "result.json").write_text('{"value": 1}\n', encoding="utf-8")
        input_path = workspace / "input" / "data.csv"
        return RunnerRequest(
            project_id="project-replay-001",
            task_id="task-replay-001",
            run_id="run-placeholder",
            agent_id="agent-replay-001",
            device_id="device-replay-001",
            workspace_id="workspace-replay-001",
            workspace_path=str(workspace),
            command=("python", str(workspace / "scripts" / "solve.py")),
            profile=ExecutionProfile(),
            environment={"DATA_FILE": "input/data.csv"},
            input_paths=(str(input_path),),
            output_paths=("output/result.json",),
            source_commit="commit-replay-001",
            environment_image_digest="sha256:" + "b" * 64,
            dependency_lock="requirements.lock",
            parameters={"alpha": 0.5},
            random_seed=17,
            model_provider="local",
            model_name="test-model",
            tool_versions={"python": "3.12"},
            data_access_policy={
                "observation_mode": "declared",
                "observed_files": [
                    {
                        "path": str(input_path),
                        "access_mode": "read",
                        "observation_source": "declared",
                    }
                ],
            },
            observed_input_files=(str(input_path),),
        )

    def _manifest(self, workspace: Path, run_id: str, *, container: bool = False) -> tuple[dict, RunnerRequest]:
        request = replace(self._prepare_workspace(workspace), run_id=run_id)
        if container:
            image = "python@sha256:" + "a" * 64
            request = replace(
                request,
                execution_backend="container",
                container_runtime="docker",
                container_image=image,
            )
            policy = ContainerPolicy(allowed_roots=(str(self.root),))
            runtime = ContainerRuntime("docker", executable=sys.executable)
            invocation = ContainerInvocation(
                runtime="docker",
                image=image,
                command=ContainerCommandBuilder.build(runtime, image, request, policy),
                workspace_path=str(workspace.resolve()),
                network_mode="none",
                read_only_rootfs=True,
                read_only_workspace=True,
                limits=policy.limits,
                mounts=policy.plan_mounts(request),
                environment_keys=tuple(sorted(request.environment)),
            )
            request = replace(
                request,
                execution_backend="container",
                container_runtime="docker",
                container_image=image,
                execution_details=invocation.as_manifest(),
            )
        outputs = OutputDiscovery(workspace).discover(request.output_paths)
        result = ProcessResult(
            process_id=f"process-{run_id}",
            run_id=run_id,
            status="SUCCEEDED",
            exit_code=0,
            stdout="completed",
            stderr="",
        )
        return RunManifestBuilder.build(request, result, outputs), request

    def test_unapproved_source_manifest_cannot_be_replayed(self) -> None:
        manifest, _ = self._manifest(self.source, "run-source")
        manifest["information_boundary"]["allowed"] = False
        with self.assertRaisesRegex(ReplayPolicyError, "replay_source_manifest_not_approved"):
            ReproducibleReplayPlanner.plan(manifest, "run-replay", environment={"DATA_FILE": "input/data.csv"})

    def test_environment_key_set_is_required_for_replay(self) -> None:
        manifest, _ = self._manifest(self.source, "run-source")
        with self.assertRaisesRegex(ReplayPolicyError, "replay_environment_keys_mismatch"):
            ReproducibleReplayPlanner.plan(manifest, "run-replay", environment={})

    def test_tampered_source_manifest_hash_is_rejected(self) -> None:
        manifest, _ = self._manifest(self.source, "run-source")
        manifest["reproducibility"]["parameters"]["alpha"] = 0.75
        with self.assertRaisesRegex(ReplayPolicyError, "replay_source_reproducibility_hash_mismatch"):
            ReproducibleReplayPlanner.plan(manifest, "run-replay", environment={"DATA_FILE": "input/data.csv"})

    def test_plan_uses_new_run_id_and_relocates_workspace_paths(self) -> None:
        manifest, request = self._manifest(self.source, "run-source")
        plan = ReproducibleReplayPlanner.plan(
            manifest,
            "run-replay",
            workspace_path=str(self.target),
            environment=dict(request.environment),
        )

        self.assertEqual(plan.request.run_id, "run-replay")
        self.assertEqual(plan.request.workspace_path, str(self.target))
        self.assertEqual(plan.request.command[1], str(self.target / "scripts" / "solve.py"))
        self.assertEqual(plan.request.input_paths, (str(self.target / "input" / "data.csv"),))
        self.assertEqual(plan.request.observed_input_files, (str(self.target / "input" / "data.csv"),))
        self.assertEqual(
            plan.request.data_access_policy["observed_files"][0]["path"],
            str(self.target / "input" / "data.csv"),
        )

    def test_same_content_in_a_new_workspace_matches_stable_manifest(self) -> None:
        source_manifest, source_request = self._manifest(self.source, "run-source")
        target_manifest, _ = self._manifest(self.target, "run-replay")
        plan = ReproducibleReplayPlanner.plan(
            source_manifest,
            "run-replay",
            workspace_path=str(self.target),
            environment=dict(source_request.environment),
        )

        self.assertNotEqual(source_manifest["manifest_sha256"], target_manifest["manifest_sha256"])
        self.assertEqual(source_manifest["reproducibility_hash"], target_manifest["reproducibility_hash"])
        comparison = ReplayComparator.compare(plan, target_manifest)
        self.assertTrue(comparison.matched)
        self.assertTrue(comparison.output_hashes_match)
        self.assertTrue(comparison.reproducibility_hash_match)
        self.assertTrue(comparison.information_boundary_allowed)

    def test_output_hash_mismatch_is_reported(self) -> None:
        manifest, _ = self._manifest(self.source, "run-source")
        plan = ReproducibleReplayPlanner.plan(manifest, "run-replay", environment={"DATA_FILE": "input/data.csv"})
        rerun_manifest = copy.deepcopy(manifest)
        rerun_manifest["reproducibility_hash"] = plan.expected_reproducibility_hash
        rerun_manifest["outputs"][0]["content_hash"] = "0" * 64

        comparison = ReplayComparator.compare(plan, rerun_manifest)
        self.assertFalse(comparison.matched)
        self.assertFalse(comparison.output_hashes_match)
        self.assertIn("output_hash_mismatch:output/result.json", comparison.output_differences)

    def test_disallowed_rerun_boundary_blocks_match(self) -> None:
        manifest, _ = self._manifest(self.source, "run-source")
        plan = ReproducibleReplayPlanner.plan(manifest, "run-replay", environment={"DATA_FILE": "input/data.csv"})
        rerun_manifest = copy.deepcopy(manifest)
        rerun_manifest["reproducibility_hash"] = plan.expected_reproducibility_hash
        rerun_manifest["information_boundary"]["allowed"] = False

        comparison = ReplayComparator.compare(plan, rerun_manifest)
        self.assertFalse(comparison.matched)
        self.assertFalse(comparison.information_boundary_allowed)

    def test_container_context_survives_replay_planning(self) -> None:
        manifest, request = self._manifest(self.source, "run-source", container=True)
        plan = ReproducibleReplayPlanner.plan(
            manifest,
            "run-replay",
            workspace_path=str(self.target),
            environment=dict(request.environment),
        )

        self.assertEqual(plan.request.execution_backend, "container")
        self.assertEqual(plan.request.container_runtime, "docker")
        self.assertEqual(plan.request.container_image, request.container_image)
        self.assertEqual(plan.request.execution_details["workspace_path"], str(self.target))
        self.assertEqual(plan.request.execution_details["workload_command"][1], str(self.target / "scripts" / "solve.py"))


if __name__ == "__main__":
    unittest.main()
