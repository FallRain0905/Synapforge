"""Runner and Adapter contracts for controlled local execution.

This module is deliberately independent from the API application. It validates
the execution boundary before handing a process to ``LocalProcessSupervisor``.
OS-level network isolation, desktop sessions, and container sandboxes remain
separate adapters and are not silently claimed by this development runner.
"""

from __future__ import annotations

import os
import platform
import sys
from dataclasses import dataclass, field
from typing import Literal, Mapping, Protocol, Sequence

try:
    from .machine_service import LocalProcessSupervisor, ProcessResult, ProcessSpec
    from .information_boundary import AccessObservationCapture, AccessObservationProvider
except ImportError:  # Support direct script execution during development.
    from machine_service import LocalProcessSupervisor, ProcessResult, ProcessSpec
    from information_boundary import AccessObservationCapture, AccessObservationProvider


ExecutionMode = Literal["HEADLESS", "USER_SESSION", "INTERACTIVE_DESKTOP"]
NetworkPolicy = Literal["deny-by-default", "allow-listed", "unrestricted"]
ExecutionBackend = Literal["host", "container"]


class RunnerPolicyError(ValueError):
    """Stable error family for a request rejected before process creation."""


@dataclass(frozen=True)
class ExecutionProfile:
    mode: ExecutionMode = "HEADLESS"
    requires_user_session: bool = False
    allow_remote_terminal: bool = False
    allow_desktop_control: bool = False
    network_policy: NetworkPolicy = "deny-by-default"
    auto_retry: bool = False

    def __post_init__(self) -> None:
        if self.mode not in {"HEADLESS", "USER_SESSION", "INTERACTIVE_DESKTOP"}:
            raise RunnerPolicyError("execution_mode_invalid")
        if self.network_policy not in {"deny-by-default", "allow-listed", "unrestricted"}:
            raise RunnerPolicyError("network_policy_invalid")
        if self.mode == "HEADLESS" and self.requires_user_session:
            raise RunnerPolicyError("headless_cannot_require_user_session")
        if self.mode == "HEADLESS" and self.allow_desktop_control:
            raise RunnerPolicyError("headless_cannot_allow_desktop_control")
        if self.mode in {"USER_SESSION", "INTERACTIVE_DESKTOP"} and not self.requires_user_session:
            raise RunnerPolicyError("interactive_profile_requires_user_session")
        if self.mode != "INTERACTIVE_DESKTOP" and self.allow_desktop_control:
            raise RunnerPolicyError("desktop_control_requires_interactive_profile")


@dataclass(frozen=True)
class RunnerRequest:
    project_id: str
    task_id: str | None
    run_id: str
    agent_id: str
    device_id: str
    workspace_id: str
    workspace_path: str
    command: tuple[str, ...]
    profile: ExecutionProfile = field(default_factory=ExecutionProfile)
    environment: Mapping[str, str] = field(default_factory=dict)
    input_paths: tuple[str, ...] = ()
    output_paths: tuple[str, ...] = ()
    timeout_seconds: float | None = None
    max_output_bytes: int = 1_000_000
    terminal_size: tuple[int, int] = (80, 24)
    terminal_backend: str = "PIPE"
    execution_backend: ExecutionBackend = "host"
    container_runtime: str | None = None
    container_image: str | None = None
    execution_details: Mapping[str, object] = field(default_factory=dict)
    source_commit: str | None = None
    environment_image_digest: str | None = None
    dependency_lock: str | None = None
    parameters: Mapping[str, object] = field(default_factory=dict)
    random_seed: int | None = None
    model_provider: str | None = None
    model_name: str | None = None
    tool_versions: Mapping[str, str] = field(default_factory=dict)
    data_access_policy: Mapping[str, object] = field(default_factory=dict)
    observed_input_files: tuple[str, ...] = ()
    observed_network_connections: tuple[Mapping[str, object], ...] = ()
    access_observation_diagnostics: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        for field_name in ("project_id", "run_id", "agent_id", "device_id", "workspace_id", "workspace_path"):
            if not isinstance(getattr(self, field_name), str) or not getattr(self, field_name).strip():
                raise RunnerPolicyError(f"runner_{field_name}_required")
        if not self.command or not all(isinstance(part, str) and part for part in self.command):
            raise RunnerPolicyError("runner_command_required")
        if self.task_id is not None and (not isinstance(self.task_id, str) or not self.task_id.strip()):
            raise RunnerPolicyError("runner_task_id_invalid")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise RunnerPolicyError("runner_timeout_invalid")
        if self.max_output_bytes < 1:
            raise RunnerPolicyError("runner_output_limit_invalid")
        if len(self.terminal_size) != 2 or any(not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > 1000 for value in self.terminal_size):
            raise RunnerPolicyError("runner_terminal_size_invalid")
        if self.terminal_backend not in {"PIPE", "CONPTY"}:
            raise RunnerPolicyError("runner_terminal_backend_invalid")
        if self.terminal_backend == "CONPTY" and self.profile.mode == "HEADLESS":
            raise RunnerPolicyError("conpty_requires_user_session")
        if self.execution_backend not in {"host", "container"}:
            raise RunnerPolicyError("runner_execution_backend_invalid")
        if self.container_runtime is not None and self.container_runtime not in {"docker", "podman"}:
            raise RunnerPolicyError("runner_container_runtime_invalid")
        if self.execution_backend == "container" and (not isinstance(self.container_image, str) or not self.container_image.strip()):
            raise RunnerPolicyError("runner_container_image_required")
        if self.execution_backend == "host" and (self.container_runtime is not None or self.container_image is not None):
            raise RunnerPolicyError("host_container_fields_not_allowed")
        if any(not isinstance(key, str) or not key for key in self.environment):
            raise RunnerPolicyError("runner_environment_key_invalid")
        if any(not isinstance(value, str) for value in self.environment.values()):
            raise RunnerPolicyError("runner_environment_value_invalid")
        if self.source_commit is not None and not isinstance(self.source_commit, str):
            raise RunnerPolicyError("runner_source_commit_invalid")
        if self.environment_image_digest is not None and not isinstance(self.environment_image_digest, str):
            raise RunnerPolicyError("runner_environment_image_digest_invalid")
        if self.dependency_lock is not None and not isinstance(self.dependency_lock, str):
            raise RunnerPolicyError("runner_dependency_lock_invalid")
        if self.random_seed is not None and (not isinstance(self.random_seed, int) or isinstance(self.random_seed, bool)):
            raise RunnerPolicyError("runner_random_seed_invalid")
        if any(not isinstance(key, str) or not key for key in self.tool_versions):
            raise RunnerPolicyError("runner_tool_version_key_invalid")
        if any(not isinstance(value, str) for value in self.tool_versions.values()):
            raise RunnerPolicyError("runner_tool_version_value_invalid")
        if any(not isinstance(path, str) or not path for path in self.observed_input_files):
            raise RunnerPolicyError("runner_observed_input_file_invalid")
        if any(not isinstance(item, Mapping) for item in self.observed_network_connections):
            raise RunnerPolicyError("runner_observed_network_connection_invalid")
        object.__setattr__(self, "environment", dict(self.environment))
        object.__setattr__(self, "parameters", dict(self.parameters))
        object.__setattr__(self, "tool_versions", dict(self.tool_versions))
        object.__setattr__(self, "data_access_policy", dict(self.data_access_policy))
        object.__setattr__(self, "observed_input_files", tuple(self.observed_input_files))
        object.__setattr__(self, "observed_network_connections", tuple(dict(item) for item in self.observed_network_connections))
        if self.access_observation_diagnostics is not None and not isinstance(self.access_observation_diagnostics, Mapping):
            raise RunnerPolicyError("runner_access_observation_diagnostics_invalid")
        if self.access_observation_diagnostics is not None:
            object.__setattr__(self, "access_observation_diagnostics", dict(self.access_observation_diagnostics))
        if not isinstance(self.execution_details, Mapping):
            raise RunnerPolicyError("runner_execution_details_invalid")
        object.__setattr__(self, "execution_details", dict(self.execution_details))


@dataclass(frozen=True)
class AdapterDescriptor:
    adapter_id: str
    agent_name: str
    agent_version: str
    adapter_version: str
    supported_os: tuple[str, ...]
    supported_execution_profiles: tuple[ExecutionMode, ...]
    supported_capabilities: tuple[str, ...] = ()
    launch_command: str = "direct-exec"
    input_mode: str = "argv"
    output_mode: str = "stdout-stderr"
    approval_mode: str = "platform-gate"
    interrupt_mode: str = "terminate"
    artifact_detection_mode: str = "explicit-paths"

    def __post_init__(self) -> None:
        for field_name in ("adapter_id", "agent_name", "agent_version", "adapter_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"adapter_{field_name}_required")
        if not self.supported_os:
            raise ValueError("adapter_supported_os_required")
        if not self.supported_execution_profiles:
            raise ValueError("adapter_supported_profiles_required")
        if any(mode not in {"HEADLESS", "USER_SESSION", "INTERACTIVE_DESKTOP"} for mode in self.supported_execution_profiles):
            raise ValueError("adapter_supported_profile_invalid")

    def supports(self, mode: ExecutionMode, current_os: str | None = None) -> bool:
        normalized_os = (current_os or platform.system()).lower()
        return mode in self.supported_execution_profiles and normalized_os in {
            value.lower() for value in self.supported_os
        }


class RunnerAdapter(Protocol):
    descriptor: AdapterDescriptor

    def build_process_spec(self, request: RunnerRequest) -> ProcessSpec:
        """Translate a validated request into a non-shell process spec."""


@dataclass(frozen=True)
class RunnerHandle:
    """Stable handle returned after a process has passed Runner policy checks."""

    run_id: str
    process_id: str


@dataclass(frozen=True)
class CommandAdapter:
    """Adapter for a pre-resolved executable and argv tuple."""

    descriptor: AdapterDescriptor

    def build_process_spec(self, request: RunnerRequest) -> ProcessSpec:
        return ProcessSpec(
            run_id=request.run_id,
            command=request.command,
            cwd=request.workspace_path,
            env=dict(request.environment),
            inherit_environment=False,
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
            stdin_enabled=request.profile.mode != "HEADLESS",
            project_id=request.project_id,
            task_id=request.task_id,
            agent_id=request.agent_id,
            device_id=request.device_id,
            workspace_id=request.workspace_id,
            workspace_path=request.workspace_path,
            terminal_columns=request.terminal_size[0],
            terminal_rows=request.terminal_size[1],
            terminal_backend=request.terminal_backend,
        )


@dataclass(frozen=True)
class ConPtyAdapter:
    """Adapter marker for requests that must execute through a real ConPTY."""

    descriptor: AdapterDescriptor

    def build_process_spec(self, request: RunnerRequest) -> ProcessSpec:
        if request.terminal_backend != "CONPTY":
            raise RunnerPolicyError("conpty_adapter_requires_conpty_backend")
        return CommandAdapter(self.descriptor).build_process_spec(request)


@dataclass(frozen=True)
class PythonAdapter:
    """Headless Python adapter using the current interpreter and argv."""

    descriptor: AdapterDescriptor
    executable: str = sys.executable

    def build_process_spec(self, request: RunnerRequest) -> ProcessSpec:
        if WorkspacePolicy._normalize(request.command[0]) != WorkspacePolicy._normalize(self.executable):
            raise RunnerPolicyError("python_executable_mismatch")
        return CommandAdapter(self.descriptor).build_process_spec(request)


@dataclass(frozen=True)
class WorkspacePolicy:
    """Allowlist for the local execution boundary."""

    allowed_roots: tuple[str, ...]
    allowed_executables: tuple[str, ...]
    allowed_environment_keys: tuple[str, ...] = ()
    allowed_network_hosts: tuple[str, ...] = ()
    allow_unrestricted_network: bool = False

    def __post_init__(self) -> None:
        if not self.allowed_roots:
            raise ValueError("workspace_allowed_roots_required")
        if not self.allowed_executables:
            raise ValueError("workspace_allowed_executables_required")
        if any(not isinstance(value, str) or not value.strip() for value in self.allowed_roots):
            raise ValueError("workspace_allowed_root_invalid")
        if any(not isinstance(value, str) or not value.strip() for value in self.allowed_executables):
            raise ValueError("workspace_allowed_executable_invalid")
        if any(not isinstance(value, str) or not value.strip() for value in self.allowed_environment_keys):
            raise ValueError("workspace_allowed_environment_key_invalid")
        if any(not isinstance(value, str) or not value.strip() for value in self.allowed_network_hosts):
            raise ValueError("workspace_allowed_network_host_invalid")

    def validate(self, request: RunnerRequest) -> None:
        workspace = self._validate_path(request.workspace_path, "workspace")
        for path in request.input_paths:
            candidate = path if os.path.isabs(path) else os.path.join(workspace, path)
            self._validate_path(candidate, "input")
            self._require_within(candidate, workspace, "input_outside_workspace")
        for path in request.output_paths:
            candidate = path if os.path.isabs(path) else os.path.join(workspace, path)
            self._validate_path(candidate, "output")
            self._require_within(candidate, workspace, "output_outside_workspace")
        self.validate_command(request.command)
        allowed_environment = {key for key in self.allowed_environment_keys}
        unexpected = sorted(set(request.environment) - allowed_environment)
        if unexpected:
            raise RunnerPolicyError("environment_key_not_allowlisted:" + ",".join(unexpected))
        if request.profile.network_policy == "allow-listed" and not self.allowed_network_hosts:
            raise RunnerPolicyError("network_allowlist_required")
        if request.profile.network_policy == "unrestricted" and not self.allow_unrestricted_network:
            raise RunnerPolicyError("unrestricted_network_not_allowed")

    def validate_process_spec(self, spec: ProcessSpec, request: RunnerRequest) -> None:
        if spec.run_id != request.run_id:
            raise RunnerPolicyError("adapter_run_id_mismatch")
        if spec.project_id != request.project_id:
            raise RunnerPolicyError("adapter_project_id_mismatch")
        if spec.task_id != request.task_id:
            raise RunnerPolicyError("adapter_task_id_mismatch")
        if spec.agent_id != request.agent_id:
            raise RunnerPolicyError("adapter_agent_id_mismatch")
        if spec.device_id != request.device_id:
            raise RunnerPolicyError("adapter_device_id_mismatch")
        if spec.workspace_id != request.workspace_id:
            raise RunnerPolicyError("adapter_workspace_id_mismatch")
        if self._normalize(spec.workspace_path) != self._normalize(request.workspace_path):
            raise RunnerPolicyError("adapter_workspace_path_mismatch")
        if spec.cwd is None:
            raise RunnerPolicyError("adapter_cwd_required")
        if spec.inherit_environment:
            raise RunnerPolicyError("inherited_environment_not_allowed")
        if spec.terminal_backend != request.terminal_backend:
            raise RunnerPolicyError("adapter_terminal_backend_mismatch")
        workspace = self._validate_path(request.workspace_path, "workspace")
        self._require_within(spec.cwd, workspace, "adapter_cwd_outside_workspace")
        self.validate_command(spec.command)
        if spec.env is not None:
            unexpected = sorted(set(spec.env) - set(self.allowed_environment_keys))
            if unexpected:
                raise RunnerPolicyError("adapter_environment_key_not_allowlisted:" + ",".join(unexpected))

    def validate_command(self, command: Sequence[str]) -> None:
        if not command:
            raise RunnerPolicyError("runner_command_required")
        candidate = command[0]
        candidate_is_path = os.path.isabs(candidate) or "/" in candidate or "\\" in candidate
        normalized_candidate = self._normalize(candidate) if candidate_is_path else candidate.casefold()
        for allowed in self.allowed_executables:
            normalized_allowed = self._normalize(allowed) if (
                os.path.isabs(allowed) or "/" in allowed or "\\" in allowed
            ) else allowed.casefold()
            if normalized_candidate == normalized_allowed:
                return
        raise RunnerPolicyError("executable_not_allowlisted")

    def _validate_path(self, path: str, label: str) -> str:
        normalized = self._normalize(path)
        if not any(self._is_within(normalized, root) for root in self.allowed_roots):
            raise RunnerPolicyError(f"{label}_path_not_allowlisted")
        return normalized

    def _require_within(self, path: str, root: str, error_code: str) -> None:
        if not self._is_within(self._normalize(path), root):
            raise RunnerPolicyError(error_code)

    @staticmethod
    def _normalize(path: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))

    @classmethod
    def _is_within(cls, path: str, root: str) -> bool:
        try:
            return os.path.commonpath([path, cls._normalize(root)]) == cls._normalize(root)
        except ValueError:
            return False


class NetworkEnforcer(Protocol):
    def validate(self, profile: ExecutionProfile, allowed_hosts: Sequence[str]) -> None: ...


class TerminalResizeEnforcer(Protocol):
    """PTY-specific resize operation; pipe runners must not emulate it."""

    async def resize(self, process_id: str, columns: int, rows: int, pixel_width: int | None = None, pixel_height: int | None = None) -> None: ...


class LocalRunner:
    """Validate and run one adapter request through the process supervisor."""

    def __init__(
        self,
        supervisor: LocalProcessSupervisor,
        policy: WorkspacePolicy,
        adapters: Sequence[RunnerAdapter],
        *,
        network_enforcer: NetworkEnforcer | None = None,
        terminal_resize_enforcer: TerminalResizeEnforcer | None = None,
        access_observation_provider: AccessObservationProvider | None = None,
        current_os: str | None = None,
    ) -> None:
        self.supervisor = supervisor
        self.policy = policy
        self.adapters = {adapter.descriptor.adapter_id: adapter for adapter in adapters}
        self.network_enforcer = network_enforcer
        self.terminal_resize_enforcer = terminal_resize_enforcer or (
            supervisor if callable(getattr(supervisor, "resize", None)) else None
        )
        self.access_observation_provider = access_observation_provider
        self.current_os = (current_os or platform.system()).lower()
        self._run_process_ids: dict[str, str] = {}
        self._run_requests: dict[str, RunnerRequest] = {}
        self._observation_captures: dict[str, AccessObservationCapture] = {}
        if not self.adapters:
            raise ValueError("runner_adapters_required")

    async def run(self, adapter_id: str, request: RunnerRequest) -> ProcessResult:
        handle = await self.start(adapter_id, request)
        return await self.wait(handle.run_id)

    async def start(self, adapter_id: str, request: RunnerRequest) -> RunnerHandle:
        if request.run_id in self._run_process_ids:
            raise RunnerPolicyError("runner_run_already_active")
        adapter = self.adapters.get(adapter_id)
        if adapter is None:
            raise RunnerPolicyError("adapter_not_found")
        if not adapter.descriptor.supports(request.profile.mode, self.current_os):
            raise RunnerPolicyError("adapter_execution_profile_not_supported")
        capability_check = getattr(adapter, "ensure_available", None)
        if callable(capability_check):
            capability_check()
        self.policy.validate(request)
        supports_backend = getattr(self.supervisor, "supports_terminal_backend", None)
        if callable(supports_backend) and not supports_backend(request.terminal_backend):
            raise RunnerPolicyError("conpty_backend_unavailable" if request.terminal_backend == "CONPTY" else "terminal_backend_unavailable")
        if request.profile.network_policy != "deny-by-default":
            if self.network_enforcer is None:
                raise RunnerPolicyError("network_enforcement_unavailable")
            self.network_enforcer.validate(request.profile, self.policy.allowed_network_hosts)
        spec = adapter.build_process_spec(request)
        self.policy.validate_process_spec(spec, request)
        observer_started = False
        if self.access_observation_provider is not None:
            try:
                self.access_observation_provider.start(request.workspace_path)
                observer_started = True
            except Exception as error:
                requires_files = str(request.data_access_policy.get("observation_mode", "declared")) == "system"
                requires_network = str(request.data_access_policy.get("network_observation_mode", "declared")) == "system"
                if requires_files or requires_network:
                    raise RunnerPolicyError(f"access_observer_start_failed:{error}") from error
        try:
            handle = await self.supervisor.start(spec)
        except Exception:
            if observer_started:
                try:
                    self.access_observation_provider.stop()  # type: ignore[union-attr]
                except Exception:
                    pass
            raise
        if observer_started:
            bind_process = getattr(self.access_observation_provider, "bind_process", None)
            if callable(bind_process):
                operating_system_pid = getattr(self.supervisor, "operating_system_pid", lambda _process_id: None)(handle.process_id)
                try:
                    bind_process(operating_system_pid)
                except Exception as error:
                    await self.supervisor.stop(handle.process_id)
                    try:
                        self.access_observation_provider.stop()
                    except Exception:
                        pass
                    raise RunnerPolicyError(f"access_observer_process_binding_failed:{error}") from error
        self._run_process_ids[request.run_id] = handle.process_id
        self._run_requests[request.run_id] = request
        return RunnerHandle(run_id=request.run_id, process_id=handle.process_id)

    async def wait(self, run_id: str) -> ProcessResult:
        process_id = self._run_process_ids.get(run_id)
        if process_id is None:
            raise KeyError("runner_run_not_active")
        request = self._run_requests.get(run_id)
        capture = self._observation_captures.pop(run_id, None)
        try:
            result = await self.supervisor.wait(process_id)
            if self.access_observation_provider is not None and capture is None and request is not None:
                try:
                    raw_capture = self.access_observation_provider.stop()
                    capture = raw_capture if isinstance(raw_capture, AccessObservationCapture) else AccessObservationCapture(
                        "not_captured", reason="access_observer_invalid_capture"
                    )
                except Exception as error:
                    capture = AccessObservationCapture("not_captured", reason=f"access_observer_stop_failed:{error}")
            if capture is not None:
                result = ProcessResult(
                    result.process_id,
                    result.run_id,
                    result.status,
                    result.exit_code,
                    result.stdout,
                    result.stderr,
                    tuple(
                        item.path
                        for item in capture.file_observations
                        if item.access_mode in {"read", "read_write"}
                    ),
                    tuple(
                        {
                            "host": item.host,
                            "port": item.port,
                            "protocol": item.protocol,
                            "direction": item.direction,
                            "observation_source": item.observation_source,
                        }
                        for item in capture.network_observations
                    ),
                    capture.status,
                    capture.reason,
                    dict(capture.diagnostics) if capture.diagnostics else None,
                )
            return result
        except Exception:
            if self.access_observation_provider is not None and request is not None and capture is None:
                try:
                    self.access_observation_provider.stop()
                except Exception:
                    pass
            raise
        finally:
            self._run_process_ids.pop(run_id, None)
            self._run_requests.pop(run_id, None)

    async def request_stop(self, run_id: str) -> None:
        process_id = self._run_process_ids.get(run_id)
        if process_id is None:
            raise KeyError("runner_run_not_active")
        await self.supervisor.request_stop(process_id)

    async def write_stdin(self, run_id: str, data: str) -> None:
        process_id = self._run_process_ids.get(run_id)
        if process_id is None:
            raise KeyError("runner_run_not_active")
        await self.supervisor.write_stdin(process_id, data)

    async def resize_terminal(
        self,
        run_id: str,
        columns: int,
        rows: int,
        *,
        pixel_width: int | None = None,
        pixel_height: int | None = None,
    ) -> None:
        if self.terminal_resize_enforcer is None:
            raise RunnerPolicyError("terminal_resize_runtime_unavailable")
        process_id = self._run_process_ids.get(run_id)
        if process_id is None:
            raise KeyError("runner_run_not_active")
        await self.terminal_resize_enforcer.resize(process_id, columns, rows, pixel_width, pixel_height)

    def active_run_ids(self) -> tuple[str, ...]:
        return tuple(self._run_process_ids)

    def new_event_parser(self, adapter_id: str) -> object | None:
        """Create an optional parser owned by the selected Adapter."""
        adapter = self.adapters.get(adapter_id)
        if adapter is None:
            raise RunnerPolicyError("adapter_not_found")
        factory = getattr(adapter, "new_event_parser", None)
        return factory() if callable(factory) else None


__all__ = [
    "AdapterDescriptor",
    "CommandAdapter",
    "ConPtyAdapter",
    "ExecutionMode",
    "ExecutionProfile",
    "LocalRunner",
    "NetworkEnforcer",
    "NetworkPolicy",
    "PythonAdapter",
    "RunnerAdapter",
    "RunnerHandle",
    "RunnerPolicyError",
    "RunnerRequest",
    "TerminalResizeEnforcer",
    "WorkspacePolicy",
]
