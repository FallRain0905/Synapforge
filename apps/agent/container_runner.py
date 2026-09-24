"""Container execution boundary for Docker and Podman.

The module only builds and supervises an explicit argv invocation. It does not
invoke a shell, silently fall back to the host runner, or claim that a
container runtime is available when it cannot be resolved.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, Mapping, Sequence

try:
    from .machine_service import LocalProcessSupervisor, ProcessSpec
    from .runner import RunnerHandle, RunnerPolicyError, RunnerRequest
except ImportError:  # Support direct development execution.
    from machine_service import LocalProcessSupervisor, ProcessSpec
    from runner import RunnerHandle, RunnerPolicyError, RunnerRequest


ContainerRuntimeName = Literal["docker", "podman"]
_IMAGE_DIGEST = re.compile(r"@sha256:[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ContainerRuntime:
    """A resolved Docker/Podman executable."""

    name: ContainerRuntimeName
    executable: str | None = None

    def resolve(self) -> str | None:
        if self.executable:
            candidate = os.path.abspath(os.path.expanduser(self.executable))
            return candidate if os.path.isfile(candidate) else None
        return shutil.which(self.name)

    def require_resolved(self) -> str:
        resolved = self.resolve()
        if not resolved:
            raise RunnerPolicyError(f"container_runtime_not_available:{self.name}")
        return resolved


@dataclass(frozen=True)
class ContainerLimits:
    """Limits passed to the container runtime, not merely recorded as metadata."""

    cpus: float = 1.0
    memory_bytes: int = 2 * 1024 * 1024 * 1024
    pids_limit: int = 256

    def __post_init__(self) -> None:
        if self.cpus <= 0:
            raise ValueError("container_cpus_invalid")
        if self.memory_bytes < 16 * 1024 * 1024:
            raise ValueError("container_memory_limit_invalid")
        if self.pids_limit < 1:
            raise ValueError("container_pids_limit_invalid")


@dataclass(frozen=True)
class ContainerPolicy:
    """Fail-closed policy for the first container runner slice."""

    allowed_roots: tuple[str, ...]
    require_image_digest: bool = True
    read_only_workspace: bool = True
    allow_network: bool = False
    limits: ContainerLimits = ContainerLimits()

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise ValueError("container_allowed_roots_required")
        if any(not isinstance(root, str) or not root.strip() for root in self.allowed_roots):
            raise ValueError("container_allowed_root_invalid")
        if self.allow_network:
            raise ValueError("container_network_must_remain_disabled")
        if not self.read_only_workspace:
            raise ValueError("container_workspace_must_be_read_only")

    def validate(self, image: str, request: RunnerRequest) -> str:
        if not isinstance(image, str) or not image.strip():
            raise RunnerPolicyError("container_image_required")
        image = image.strip()
        if self.require_image_digest and not _IMAGE_DIGEST.search(image):
            raise RunnerPolicyError("container_image_digest_required")
        if request.profile.mode != "HEADLESS":
            raise RunnerPolicyError("container_runner_requires_headless")
        if request.profile.network_policy != "deny-by-default":
            raise RunnerPolicyError("container_network_policy_not_supported")
        if request.terminal_backend != "PIPE":
            raise RunnerPolicyError("container_terminal_backend_not_supported")
        workspace = self._normalise(request.workspace_path)
        if not any(self._inside(workspace, root) for root in self.allowed_roots):
            raise RunnerPolicyError("container_workspace_not_allowlisted")
        for label, paths in (("input", request.input_paths), ("output", request.output_paths)):
            for raw_path in paths:
                candidate = self._normalise(raw_path if os.path.isabs(raw_path) else os.path.join(workspace, raw_path))
                if not self._inside(candidate, workspace):
                    raise RunnerPolicyError(f"container_{label}_outside_workspace")
        return image

    def plan_mounts(self, request: RunnerRequest) -> tuple["ContainerMount", ...]:
        """Keep the workspace read-only and expose only output directories as rw."""

        if request.execution_backend != "container":
            raise RunnerPolicyError("container_request_backend_mismatch")
        workspace = self._normalise(request.workspace_path)
        output_parents: list[str] = []
        for raw_path in request.output_paths:
            candidate = self._normalise(raw_path if os.path.isabs(raw_path) else os.path.join(workspace, raw_path))
            parent = self._normalise(os.path.dirname(candidate))
            if parent == workspace:
                raise RunnerPolicyError("container_output_requires_dedicated_directory")
            if not self._inside(parent, workspace):
                raise RunnerPolicyError("container_output_parent_outside_workspace")
            if parent not in output_parents:
                output_parents.append(parent)

        # A declared input must never become writable merely because it shares
        # a directory with an output.
        for raw_path in request.input_paths:
            candidate = self._normalise(raw_path if os.path.isabs(raw_path) else os.path.join(workspace, raw_path))
            if any(self._inside(candidate, parent) for parent in output_parents):
                raise RunnerPolicyError("container_input_overlaps_writable_output")

        # Mount the shortest parent first and omit nested mounts: the broader
        # directory already provides the required write access.
        output_parents = sorted(output_parents, key=lambda value: (value.count(os.sep), value))
        compact: list[str] = []
        for parent in output_parents:
            if not any(self._inside(parent, existing) for existing in compact):
                compact.append(parent)
        mounts = [ContainerMount(workspace, "/workspace", "ro")]
        for parent in compact:
            relative = os.path.relpath(parent, workspace).replace(os.sep, "/")
            target = str(PurePosixPath("/workspace") / PurePosixPath(relative))
            mounts.append(ContainerMount(parent, target, "rw"))
        return tuple(mounts)

    @staticmethod
    def _normalise(path: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))

    @classmethod
    def _inside(cls, path: str, root: str) -> bool:
        try:
            return os.path.commonpath([path, cls._normalise(root)]) == cls._normalise(root)
        except ValueError:
            return False


@dataclass(frozen=True)
class ContainerMount:
    source: str
    target: str
    mode: Literal["ro", "rw"]

    def __post_init__(self) -> None:
        if self.mode not in {"ro", "rw"}:
            raise ValueError("container_mount_mode_invalid")
        if not self.source or not self.target.startswith("/"):
            raise ValueError("container_mount_path_invalid")

    def as_manifest(self) -> dict[str, str]:
        return {"target": self.target, "mode": self.mode}

    def as_cli(self) -> str:
        return f"type=bind,source={self.source},target={self.target},{self.mode}"


@dataclass(frozen=True)
class ContainerInvocation:
    runtime: ContainerRuntimeName
    image: str
    command: tuple[str, ...]
    workspace_path: str
    network_mode: str
    read_only_rootfs: bool
    read_only_workspace: bool
    limits: ContainerLimits
    mounts: tuple[ContainerMount, ...]
    environment_keys: tuple[str, ...]

    def as_manifest(self) -> dict[str, object]:
        return {
            "runtime": self.runtime,
            "image": self.image,
            "command": _redact_runtime_command(self.command),
            "workload_command": list(self.command[self.command.index(self.image) + 1 :]),
            "workspace_path": self.workspace_path,
            "network_mode": self.network_mode,
            "read_only_rootfs": self.read_only_rootfs,
            "read_only_workspace": self.read_only_workspace,
            "environment_keys": list(self.environment_keys),
            "mounts": [mount.as_manifest() for mount in self.mounts],
            "limits": {
                "cpus": self.limits.cpus,
                "memory_bytes": self.limits.memory_bytes,
                "pids_limit": self.limits.pids_limit,
            },
        }


class ContainerCommandBuilder:
    """Construct a non-shell runtime command from a validated request."""

    @staticmethod
    def build(
        runtime: ContainerRuntime,
        image: str,
        request: RunnerRequest,
        policy: ContainerPolicy,
        mounts: Sequence[ContainerMount] | None = None,
    ) -> tuple[str, ...]:
        image = policy.validate(image, request)
        executable = runtime.require_resolved()
        planned_mounts = tuple(mounts or policy.plan_mounts(request))
        limits = policy.limits
        command: list[str] = [
            executable,
            "run",
            "--rm",
            "--init",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--cpus",
            str(limits.cpus),
            "--memory",
            str(limits.memory_bytes),
            "--pids-limit",
            str(limits.pids_limit),
            "--label",
            f"math-agent-project={request.project_id}",
            "--label",
            f"math-agent-run={request.run_id}",
        ]
        for mount in planned_mounts:
            command.extend(("--mount", mount.as_cli()))
        command.extend(("--workdir", "/workspace"))
        for key, value in sorted(request.environment.items()):
            command.extend(("--env", f"{key}={value}"))
        command.extend((image, *request.command))
        return tuple(command)


class ContainerRunner:
    """Run a request through Docker or Podman using the local supervisor."""

    def __init__(self, supervisor: LocalProcessSupervisor, policy: ContainerPolicy, runtime: ContainerRuntime) -> None:
        self.supervisor = supervisor
        self.policy = policy
        self.runtime = runtime
        self._run_process_ids: dict[str, str] = {}
        self._invocations: dict[str, ContainerInvocation] = {}

    def build_invocation(self, image: str, request: RunnerRequest) -> ContainerInvocation:
        validated_image = self.policy.validate(image, request)
        mounts = self.policy.plan_mounts(request)
        command = ContainerCommandBuilder.build(self.runtime, validated_image, request, self.policy, mounts)
        return ContainerInvocation(
            runtime=self.runtime.name,
            image=validated_image,
            command=command,
            workspace_path=self.policy._normalise(request.workspace_path),
            network_mode="none",
            read_only_rootfs=True,
            read_only_workspace=self.policy.read_only_workspace,
            limits=self.policy.limits,
            mounts=mounts,
            environment_keys=tuple(sorted(request.environment)),
        )

    async def start(self, image: str, request: RunnerRequest) -> RunnerHandle:
        if request.run_id in self._run_process_ids:
            raise RunnerPolicyError("container_run_already_active")
        invocation = self.build_invocation(image, request)
        return await self.start_invocation(invocation, request)

    async def start_invocation(self, invocation: ContainerInvocation, request: RunnerRequest) -> RunnerHandle:
        if request.run_id in self._run_process_ids:
            raise RunnerPolicyError("container_run_already_active")
        if request.execution_backend != "container":
            raise RunnerPolicyError("container_request_backend_mismatch")
        if request.container_runtime != self.runtime.name:
            raise RunnerPolicyError("container_request_runtime_mismatch")
        if request.container_image != invocation.image:
            raise RunnerPolicyError("container_request_image_mismatch")
        for mount in invocation.mounts[1:]:
            Path(mount.source).mkdir(parents=True, exist_ok=True)
        spec = ProcessSpec(
            run_id=request.run_id,
            command=invocation.command,
            cwd=request.workspace_path,
            env={},
            inherit_environment=False,
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
            stdin_enabled=False,
            project_id=request.project_id,
            task_id=request.task_id,
            agent_id=request.agent_id,
            device_id=request.device_id,
            workspace_id=request.workspace_id,
            workspace_path=request.workspace_path,
            terminal_columns=request.terminal_size[0],
            terminal_rows=request.terminal_size[1],
            terminal_backend="PIPE",
        )
        handle = await self.supervisor.start(spec)
        self._run_process_ids[request.run_id] = handle.process_id
        self._invocations[request.run_id] = invocation
        return RunnerHandle(run_id=request.run_id, process_id=handle.process_id)

    async def wait(self, run_id: str):
        process_id = self._run_process_ids.get(run_id)
        if process_id is None:
            raise KeyError("container_run_not_active")
        try:
            return await self.supervisor.wait(process_id)
        finally:
            self._run_process_ids.pop(run_id, None)
            self._invocations.pop(run_id, None)

    async def run(self, image: str, request: RunnerRequest):
        handle = await self.start(image, request)
        return await self.wait(handle.run_id)

    async def request_stop(self, run_id: str) -> None:
        process_id = self._run_process_ids.get(run_id)
        if process_id is None:
            raise KeyError("container_run_not_active")
        await self.supervisor.request_stop(process_id)

    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(self._run_process_ids)

    def invocation_for(self, run_id: str) -> ContainerInvocation | None:
        return self._invocations.get(run_id)

    async def write_stdin(self, run_id: str, data: str) -> None:
        raise RunnerPolicyError("container_terminal_input_not_supported")

    async def resize_terminal(self, run_id: str, *args: object, **kwargs: object) -> None:
        raise RunnerPolicyError("container_terminal_resize_not_supported")


def _redact_runtime_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    redact_next = False
    for token in command:
        if redact_next:
            key = token.split("=", 1)[0]
            redacted.append(f"{key}=<redacted>")
            redact_next = False
        else:
            redacted.append(token)
            redact_next = token == "--env"
    return redacted


__all__ = [
    "ContainerCommandBuilder",
    "ContainerInvocation",
    "ContainerMount",
    "ContainerLimits",
    "ContainerPolicy",
    "ContainerRunner",
    "ContainerRuntime",
]
