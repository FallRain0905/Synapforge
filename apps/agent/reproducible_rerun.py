"""Plan and compare reproducible reruns from an approved Run Manifest."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

try:
    from .result_uploader import DiscoveredOutput, OutputDiscovery, RunManifestBuilder, reproducibility_hash
    from .runner import ExecutionProfile, RunnerRequest
except ImportError:  # Support direct development execution.
    from result_uploader import DiscoveredOutput, OutputDiscovery, RunManifestBuilder, reproducibility_hash
    from runner import ExecutionProfile, RunnerRequest


class ReplayPolicyError(ValueError):
    """Stable error family for a rerun that cannot be reproduced safely."""


@dataclass(frozen=True)
class ReplayPlan:
    request: RunnerRequest
    expected_output_hashes: Mapping[str, str]
    expected_reproducibility_hash: str | None
    source_manifest_sha256: str | None


@dataclass(frozen=True)
class ReplayComparison:
    matched: bool
    output_hashes_match: bool
    reproducibility_hash_match: bool
    information_boundary_allowed: bool
    output_differences: tuple[str, ...]
    expected_reproducibility_hash: str | None
    actual_reproducibility_hash: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "output_hashes_match": self.output_hashes_match,
            "reproducibility_hash_match": self.reproducibility_hash_match,
            "information_boundary_allowed": self.information_boundary_allowed,
            "output_differences": list(self.output_differences),
            "expected_reproducibility_hash": self.expected_reproducibility_hash,
            "actual_reproducibility_hash": self.actual_reproducibility_hash,
        }


@dataclass(frozen=True)
class ReplayExecutionResult:
    process_result: Any
    manifest: Mapping[str, Any]
    comparison: ReplayComparison


class ReproducibleReplayPlanner:
    """Turn a previously approved manifest into a new, explicit run request."""

    @classmethod
    def plan(
        cls,
        manifest: Mapping[str, Any],
        replay_run_id: str,
        *,
        workspace_path: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> ReplayPlan:
        if not isinstance(manifest, Mapping):
            raise ReplayPolicyError("replay_manifest_required")
        boundary = manifest.get("information_boundary")
        if not isinstance(boundary, Mapping) or boundary.get("allowed") is not True:
            raise ReplayPolicyError("replay_source_manifest_not_approved")
        if not isinstance(replay_run_id, str) or not replay_run_id.strip():
            raise ReplayPolicyError("replay_run_id_required")
        recorded_reproducibility_hash = manifest.get("reproducibility_hash")
        if not isinstance(recorded_reproducibility_hash, str) or not recorded_reproducibility_hash:
            raise ReplayPolicyError("replay_source_reproducibility_hash_required")
        if recorded_reproducibility_hash != reproducibility_hash(manifest):
            raise ReplayPolicyError("replay_source_reproducibility_hash_mismatch")

        reproducibility = manifest.get("reproducibility")
        if not isinstance(reproducibility, Mapping):
            raise ReplayPolicyError("replay_reproducibility_context_required")
        command = manifest.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ReplayPolicyError("replay_command_required")
        profile_data = manifest.get("execution_profile")
        if not isinstance(profile_data, Mapping):
            raise ReplayPolicyError("replay_execution_profile_required")
        profile_fields = {
            key: profile_data[key]
            for key in ("mode", "requires_user_session", "allow_remote_terminal", "allow_desktop_control", "network_policy", "auto_retry")
            if key in profile_data
        }
        try:
            profile = ExecutionProfile(**profile_fields)
        except Exception as error:
            raise ReplayPolicyError(f"replay_execution_profile_invalid:{error}") from error

        original_workspace = str(manifest.get("workspace_path") or "")
        target_workspace = workspace_path or original_workspace
        if not target_workspace:
            raise ReplayPolicyError("replay_workspace_required")
        command = cls._relocate_paths(command, original_workspace, target_workspace)
        declared = cls._relocate_paths(
            manifest.get("input_access", {}).get("declared", []) if isinstance(manifest.get("input_access"), Mapping) else [],
            original_workspace,
            target_workspace,
        )
        observed = cls._relocate_paths(
            manifest.get("input_access", {}).get("observed", []) if isinstance(manifest.get("input_access"), Mapping) else [],
            original_workspace,
            target_workspace,
        )
        output_paths = cls._relocate_paths(manifest.get("output_paths", []), original_workspace, target_workspace)

        expected_environment_keys = {str(value) for value in manifest.get("environment_keys", [])}
        supplied_environment = dict(environment or {})
        if set(supplied_environment) != expected_environment_keys:
            raise ReplayPolicyError("replay_environment_keys_mismatch")

        execution_backend = str(manifest.get("execution_backend", "host"))
        execution = manifest.get("execution") if isinstance(manifest.get("execution"), Mapping) else {}
        container_runtime: str | None = None
        container_image: str | None = None
        if execution_backend == "container":
            container_runtime = str(execution.get("runtime") or "")
            container_image = str(execution.get("image") or "")
            if not container_runtime or not container_image:
                raise ReplayPolicyError("replay_container_context_required")
        elif execution_backend != "host":
            raise ReplayPolicyError("replay_execution_backend_invalid")

        input_access = manifest.get("input_access") if isinstance(manifest.get("input_access"), Mapping) else {}
        data_policy = reproducibility.get("data_access_policy", {})
        if not isinstance(data_policy, Mapping):
            raise ReplayPolicyError("replay_data_access_policy_invalid")
        data_policy = dict(data_policy)
        if isinstance(data_policy.get("observed_files"), list):
            relocated_metadata: list[dict[str, Any]] = []
            for item in data_policy["observed_files"]:
                if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
                    continue
                item_copy = dict(item)
                item_copy["path"] = cls._relocate_paths(
                    [str(item["path"])], original_workspace, target_workspace
                )[0]
                relocated_metadata.append(item_copy)
            data_policy["observed_files"] = relocated_metadata
        network_access = manifest.get("network_access") if isinstance(manifest.get("network_access"), Mapping) else {}
        observed_network_connections = tuple(
            dict(item)
            for item in network_access.get("observed", [])
            if isinstance(item, Mapping)
        )
        execution_details = cls._relocate_value(dict(execution), original_workspace, target_workspace)
        request = RunnerRequest(
            project_id=str(manifest.get("project_id") or ""),
            task_id=str(manifest["task_id"]) if manifest.get("task_id") else None,
            run_id=replay_run_id,
            agent_id=str(manifest.get("agent_id") or ""),
            device_id=str(manifest.get("device_id") or ""),
            workspace_id=str(manifest.get("workspace_id") or ""),
            workspace_path=target_workspace,
            command=tuple(command),
            profile=profile,
            environment=supplied_environment,
            input_paths=tuple(declared),
            output_paths=tuple(output_paths),
            timeout_seconds=manifest.get("timeout_seconds"),
            max_output_bytes=int(manifest.get("max_output_bytes", 1_000_000)),
            terminal_size=(
                int((manifest.get("terminal_size") or {}).get("columns", 80)),
                int((manifest.get("terminal_size") or {}).get("rows", 24)),
            ),
            terminal_backend=str(manifest.get("terminal_backend", "PIPE")),
            execution_backend=execution_backend,  # type: ignore[arg-type]
            container_runtime=container_runtime,
            container_image=container_image,
            execution_details=execution_details,
            source_commit=str(reproducibility.get("source_commit") or "") or None,
            environment_image_digest=str(reproducibility.get("environment_image_digest") or "") or None,
            dependency_lock=str(reproducibility.get("dependency_lock") or "") or None,
            parameters=dict(reproducibility.get("parameters") or {}),
            random_seed=reproducibility.get("random_seed"),
            model_provider=str(reproducibility.get("model_provider") or "") or None,
            model_name=str(reproducibility.get("model_name") or "") or None,
            tool_versions=dict(reproducibility.get("tool_versions") or {}),
            data_access_policy=data_policy,
            observed_input_files=tuple(observed),
            observed_network_connections=observed_network_connections,
        )
        expected_outputs = {
            str(item.get("relative_path")): str(item.get("content_hash"))
            for item in (manifest.get("outputs") or [])
            if isinstance(item, Mapping) and item.get("relative_path") and item.get("content_hash")
        }
        return ReplayPlan(
            request=request,
            expected_output_hashes=expected_outputs,
            expected_reproducibility_hash=recorded_reproducibility_hash,
            source_manifest_sha256=str(manifest.get("manifest_sha256")) if manifest.get("manifest_sha256") else None,
        )

    @classmethod
    def _relocate_paths(cls, paths: Any, original_workspace: str, target_workspace: str) -> list[str]:
        if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)):
            raise ReplayPolicyError("replay_paths_invalid")
        result: list[str] = []
        for raw in paths:
            if not isinstance(raw, str) or not raw:
                raise ReplayPolicyError("replay_path_invalid")
            if not os.path.isabs(raw):
                result.append(raw)
                continue
            if not original_workspace or not cls._inside(raw, original_workspace):
                raise ReplayPolicyError("replay_absolute_path_outside_source_workspace")
            relative = os.path.relpath(cls._normalise(raw), cls._normalise(original_workspace))
            result.append(os.path.join(target_workspace, relative))
        return result

    @classmethod
    def _relocate_value(cls, value: Any, original_workspace: str, target_workspace: str, *, key: str | None = None) -> Any:
        if isinstance(value, Mapping):
            return {
                str(item_key): cls._relocate_value(item_value, original_workspace, target_workspace, key=str(item_key))
                for item_key, item_value in value.items()
            }
        if isinstance(value, list):
            if key in {"workload_command", "input_paths", "output_paths", "declared", "observed"}:
                return cls._relocate_paths(value, original_workspace, target_workspace)
            return [cls._relocate_value(item, original_workspace, target_workspace) for item in value]
        if isinstance(value, str):
            if key == "workspace_path":
                return target_workspace
            if key in {"path", "normalised_path", "source_path"}:
                return cls._relocate_paths([value], original_workspace, target_workspace)[0]
        return value

    @staticmethod
    def _normalise(path: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))

    @classmethod
    def _inside(cls, path: str, root: str) -> bool:
        try:
            return os.path.commonpath([cls._normalise(path), cls._normalise(root)]) == cls._normalise(root)
        except ValueError:
            return False


class ReplayComparator:
    @staticmethod
    def compare(plan: ReplayPlan, rerun_manifest: Mapping[str, Any], outputs: Sequence[DiscoveredOutput] | None = None) -> ReplayComparison:
        actual_outputs = {
            str(item.get("relative_path")): str(item.get("content_hash"))
            for item in (rerun_manifest.get("outputs") or [])
            if isinstance(item, Mapping) and item.get("relative_path") and item.get("content_hash")
        }
        if outputs is not None:
            actual_outputs.update({item.relative_path: item.content_hash for item in outputs})
        differences: list[str] = []
        for path in sorted(set(plan.expected_output_hashes) | set(actual_outputs)):
            expected = plan.expected_output_hashes.get(path)
            actual = actual_outputs.get(path)
            if expected is None:
                differences.append(f"unexpected_output:{path}")
            elif actual is None:
                differences.append(f"missing_output:{path}")
            elif expected != actual:
                differences.append(f"output_hash_mismatch:{path}")
        recorded_actual_hash = rerun_manifest.get("reproducibility_hash")
        actual_hash = reproducibility_hash(rerun_manifest)
        output_match = not differences
        manifest_match = (
            isinstance(recorded_actual_hash, str)
            and recorded_actual_hash == actual_hash
            and plan.expected_reproducibility_hash is not None
            and actual_hash == plan.expected_reproducibility_hash
        )
        boundary = rerun_manifest.get("information_boundary")
        boundary_allowed = isinstance(boundary, Mapping) and boundary.get("allowed") is True
        return ReplayComparison(
            matched=output_match and manifest_match and boundary_allowed,
            output_hashes_match=output_match,
            reproducibility_hash_match=manifest_match,
            information_boundary_allowed=boundary_allowed,
            output_differences=tuple(differences),
            expected_reproducibility_hash=plan.expected_reproducibility_hash,
            actual_reproducibility_hash=actual_hash,
        )


class ReplayExecutor:
    """Execute a planned host or container rerun and compare its new Manifest."""

    @staticmethod
    async def execute(runner: Any, adapter_id: str, plan: ReplayPlan) -> ReplayExecutionResult:
        if plan.request.execution_backend == "container":
            process_result = await runner.run(plan.request.container_image or "", plan.request)
        else:
            process_result = await runner.run(adapter_id, plan.request)
        outputs = OutputDiscovery(plan.request.workspace_path).discover(plan.request.output_paths)
        manifest = RunManifestBuilder.build(plan.request, process_result, outputs)
        comparison = ReplayComparator.compare(plan, manifest, outputs)
        return ReplayExecutionResult(process_result, manifest, comparison)


__all__ = [
    "ReplayComparison",
    "ReplayExecutionResult",
    "ReplayExecutor",
    "ReplayPlan",
    "ReplayPolicyError",
    "ReplayComparator",
    "ReproducibleReplayPlanner",
]
