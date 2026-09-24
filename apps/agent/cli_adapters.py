"""Adapters for external command-line Agents.

The platform owns the stable event vocabulary.  This module is the only place
where a supported CLI's machine-readable protocol is interpreted.  A protocol
or version that is not explicitly understood is rejected rather than treated
as ordinary text or as a successful run.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Sequence

try:
    from .machine_service import ProcessSpec
    from .runner import AdapterDescriptor, RunnerPolicyError, RunnerRequest, CommandAdapter
except ImportError:  # Support direct script execution during development.
    from machine_service import ProcessSpec
    from runner import AdapterDescriptor, RunnerPolicyError, RunnerRequest, CommandAdapter


CliCapabilityState = Literal["AVAILABLE", "NOT_INSTALLED", "UNSUPPORTED", "ERROR"]


class CliAdapterError(ValueError):
    """Stable error family for CLI discovery and invocation rejection."""


class CliProtocolError(CliAdapterError):
    """Raised when a CLI emits an event outside the frozen adapter contract."""


@dataclass(frozen=True)
class CliCapabilityStatus:
    adapter_id: str
    executable: str
    state: CliCapabilityState
    version: str | None
    reason: str
    capabilities: tuple[str, ...]
    checked_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "executable": self.executable,
            "state": self.state,
            "version": self.version,
            "reason": self.reason,
            "capabilities": list(self.capabilities),
            "checked_at": self.checked_at.isoformat(),
        }


@dataclass(frozen=True)
class NormalizedCliEvent:
    """One event in the platform vocabulary derived from a CLI JSON object."""

    source_type: str
    event_type: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CodexCompatibilityRule:
    """An explicitly tested Codex version family."""

    version_prefix: str
    protocol: str
    tested: bool = True


_VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+(?:\.\d+){0,2})(?!\d)")


def parse_cli_version(output: str) -> str | None:
    """Extract a semantic-looking version from a CLI version response."""

    match = _VERSION_RE.search(output or "")
    return match.group(1) if match else None


def _resolve_executable(executable: str) -> str | None:
    if not executable or not executable.strip():
        return None
    candidate = Path(executable).expanduser()
    if candidate.is_absolute() or any(char in executable for char in ("/", "\\")):
        return str(candidate) if candidate.exists() else None
    return shutil.which(executable)


def probe_cli(
    *,
    adapter_id: str,
    executable: str,
    capabilities: Sequence[str],
    accepted_version_prefixes: Sequence[str] | None,
    version_args: Sequence[str] = ("--version",),
    timeout_seconds: float = 5.0,
) -> CliCapabilityStatus:
    """Probe a CLI without a shell and report a non-ambiguous capability state."""

    checked_at = datetime.now(UTC)
    resolved = _resolve_executable(executable)
    if resolved is None:
        return CliCapabilityStatus(
            adapter_id=adapter_id,
            executable=executable,
            state="NOT_INSTALLED",
            version=None,
            reason="executable_not_found",
            capabilities=tuple(capabilities),
            checked_at=checked_at,
        )
    try:
        completed = subprocess.run(
            [resolved, *version_args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return CliCapabilityStatus(
            adapter_id=adapter_id,
            executable=resolved,
            state="ERROR",
            version=None,
            reason="version_probe_timeout",
            capabilities=tuple(capabilities),
            checked_at=checked_at,
        )
    except OSError as error:
        return CliCapabilityStatus(
            adapter_id=adapter_id,
            executable=resolved,
            state="ERROR",
            version=None,
            reason=f"version_probe_os_error:{type(error).__name__}",
            capabilities=tuple(capabilities),
            checked_at=checked_at,
        )

    version = parse_cli_version(f"{completed.stdout}\n{completed.stderr}")
    if completed.returncode != 0:
        return CliCapabilityStatus(
            adapter_id=adapter_id,
            executable=resolved,
            state="ERROR",
            version=version,
            reason=f"version_probe_exit:{completed.returncode}",
            capabilities=tuple(capabilities),
            checked_at=checked_at,
        )
    if version is None:
        return CliCapabilityStatus(
            adapter_id=adapter_id,
            executable=resolved,
            state="ERROR",
            version=None,
            reason="version_not_parseable",
            capabilities=tuple(capabilities),
            checked_at=checked_at,
        )
    if accepted_version_prefixes is not None and not any(version.startswith(prefix) for prefix in accepted_version_prefixes):
        return CliCapabilityStatus(
            adapter_id=adapter_id,
            executable=resolved,
            state="UNSUPPORTED",
            version=version,
            reason="version_not_in_compatibility_matrix",
            capabilities=tuple(capabilities),
            checked_at=checked_at,
        )
    return CliCapabilityStatus(
        adapter_id=adapter_id,
        executable=resolved,
        state="AVAILABLE",
        version=version,
        reason="compatible_version",
        capabilities=tuple(capabilities),
        checked_at=checked_at,
    )


class CodexJsonlEventParser:
    """Parse the tested Codex ``exec --json`` JSONL event vocabulary."""

    _TOP_LEVEL_TYPES = {
        "thread.started",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "error",
        "approval.requested",
        "item.started",
        "item.updated",
        "item.completed",
    }
    _ITEM_TYPES = {
        "agent_message",
        "command_execution",
        "file_change",
        "file_change_output",
        "mcp_tool_call",
        "web_search_call",
        "todo_list",
        "plan_update",
        "reasoning",
    }
    _APPROVAL_STATES = {"awaiting_approval", "requires_approval", "approval_required", "pending_approval"}

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, text: str) -> list[NormalizedCliEvent]:
        if not isinstance(text, str):
            raise CliProtocolError("cli_output_must_be_text")
        self._buffer += text
        lines = self._buffer.splitlines(keepends=True)
        complete: list[str] = []
        self._buffer = ""
        for line in lines:
            if line.endswith(("\n", "\r")):
                complete.append(line.strip())
            else:
                self._buffer = line
        return [self._parse_line(line) for line in complete if line]

    def finish(self) -> list[NormalizedCliEvent]:
        if not self._buffer.strip():
            self._buffer = ""
            return []
        line = self._buffer.strip()
        self._buffer = ""
        return [self._parse_line(line)]

    def _parse_line(self, line: str) -> NormalizedCliEvent:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise CliProtocolError(f"codex_jsonl_invalid:{error.msg}") from error
        if not isinstance(value, dict):
            raise CliProtocolError("codex_event_must_be_object")
        source_type = value.get("type")
        if not isinstance(source_type, str) or not source_type:
            raise CliProtocolError("codex_event_type_required")
        if source_type not in self._TOP_LEVEL_TYPES:
            raise CliProtocolError(f"codex_event_type_unsupported:{source_type}")

        if source_type == "thread.started":
            return NormalizedCliEvent(source_type, "run.started", {"source": "codex", "thread_id": value.get("thread_id")})
        if source_type == "turn.started":
            return NormalizedCliEvent(source_type, "run.started", {"source": "codex", "phase": "turn"})
        if source_type == "turn.completed":
            return NormalizedCliEvent(source_type, "run.completed", {"source": "codex", "usage": value.get("usage")})
        if source_type in {"turn.failed", "error"}:
            return NormalizedCliEvent(
                source_type,
                "run.failed",
                {"source": "codex", "error": value.get("error", value.get("message", value))},
            )
        if source_type == "approval.requested":
            return NormalizedCliEvent(source_type, "approval.requested", {"source": "codex", "approval": value})
        if source_type in {"item.started", "item.updated", "item.completed"}:
            item = value.get("item")
            if not isinstance(item, dict):
                raise CliProtocolError(f"codex_{source_type}_item_required")
            return self._item_event(source_type, item)
        raise CliProtocolError(f"codex_event_type_unhandled:{source_type}")

    def _item_event(self, source_type: str, item: dict[str, Any]) -> NormalizedCliEvent:
        item_type = item.get("type")
        if not isinstance(item_type, str) or item_type not in self._ITEM_TYPES:
            raise CliProtocolError(f"codex_item_type_unsupported:{item_type}")
        status = item.get("status")
        if item.get("requires_approval") is True or status in self._APPROVAL_STATES:
            return NormalizedCliEvent(
                source_type,
                "approval.requested",
                {"source": "codex", "item_type": item_type, "approval": item},
            )
        if item_type == "agent_message":
            return NormalizedCliEvent(
                source_type,
                "agent.message",
                {"source": "codex", "text": item.get("text", item.get("message", "")), "item": item},
            )
        if item_type in {"file_change", "file_change_output"}:
            return NormalizedCliEvent(
                source_type,
                "file.changed",
                {"source": "codex", "phase": source_type.removeprefix("item."), "item": item},
            )
        event_type = "tool.completed" if source_type == "item.completed" else "tool.started"
        return NormalizedCliEvent(
            source_type,
            event_type,
            {"source": "codex", "item_type": item_type, "item": item},
        )


class CodexAdapter:
    """Headless Codex adapter using the official ``exec --json`` interface."""

    # 已验证版本族：每个前缀都对应一次真实的 `exec --json` 跑通记录。
    # 0.154 于 2026-09-16 用 ChatGPT 桌面端自带的 codex-cli 0.154.0-alpha.6.2 实测：
    # 事件流为 thread.started / turn.started / item.completed / turn.completed，
    # 工具事件计入 tool_events，最终回复由 item.completed(agent_message) 提取（见 docs/CODEX_EXECUTOR.md §5）。
    VERSION_RULES = (
        CodexCompatibilityRule("0.153.", "exec-json-v1"),
        CodexCompatibilityRule("0.154.", "exec-json-v1"),
    )
    CAPABILITIES = (
        "cli.version",
        "cli.jsonl",
        "tool.events",
        "approval.events",
        "file.events",
    )
    _UNSAFE_FLAGS = {
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--approve-for-me",
    }
    _PATH_ESCAPE_FLAGS = {"--cd", "-C", "--add-dir", "--image"}
    _CONFIG_OVERRIDE_FLAGS = {"--config", "-c", "--profile", "-p"}

    def __init__(self, executable: str = "codex") -> None:
        self.executable = executable
        self.descriptor = AdapterDescriptor(
            adapter_id="codex-cli",
            agent_name="Codex CLI",
            agent_version="unknown",
            adapter_version="0.1.0",
            supported_os=("Windows", "Linux", "Darwin"),
            supported_execution_profiles=("HEADLESS",),
            supported_capabilities=self.CAPABILITIES,
            launch_command="codex exec --json",
            input_mode="argv",
            output_mode="jsonl",
            approval_mode="platform-gate",
            interrupt_mode="terminate",
            artifact_detection_mode="explicit-paths",
        )

    def probe(self, timeout_seconds: float = 5.0) -> CliCapabilityStatus:
        return probe_cli(
            adapter_id=self.descriptor.adapter_id,
            executable=self.executable,
            capabilities=self.CAPABILITIES,
            accepted_version_prefixes=tuple(rule.version_prefix for rule in self.VERSION_RULES if rule.tested),
            timeout_seconds=timeout_seconds,
        )

    def ensure_available(self, timeout_seconds: float = 5.0) -> CliCapabilityStatus:
        status = self.probe(timeout_seconds)
        if status.state != "AVAILABLE":
            raise CliAdapterError(f"codex_cli_unavailable:{status.state}:{status.reason}")
        return status

    def build_command(
        self,
        prompt_or_command: str | Sequence[str],
        *,
        model: str | None = None,
        output_schema: str | None = None,
        ephemeral: bool = True,
    ) -> tuple[str, ...]:
        if isinstance(prompt_or_command, str):
            if not prompt_or_command.strip():
                raise CliAdapterError("codex_prompt_required")
            command = [self.executable, "exec"]
            prompt = prompt_or_command
        else:
            command = list(prompt_or_command)
            if not command or not command[0]:
                raise CliAdapterError("codex_command_required")
            input_name = os.path.basename(command[0]).casefold()
            configured_name = os.path.basename(self.executable).casefold()
            names_match = input_name == configured_name or {
                input_name,
                configured_name,
            } <= {"codex", "codex.exe"}
            if not names_match:
                raise CliAdapterError("codex_executable_mismatch")
            if len(command) < 2 or command[1] != "exec":
                raise CliAdapterError("codex_exec_subcommand_required")
            prompt = ""
        self._reject_unsafe_flags(command)
        if "--json" not in command:
            command[2:2] = ["--json"]
        if "--color" not in command:
            command[2:2] = ["--color", "never"]
        if ephemeral and "--ephemeral" not in command:
            command[2:2] = ["--ephemeral"]
        if model is not None:
            if not model.strip():
                raise CliAdapterError("codex_model_invalid")
            if "--model" not in command:
                command[2:2] = ["--model", model]
        if output_schema is not None:
            if not output_schema.strip() or any(char in output_schema for char in ("\r", "\n")):
                raise CliAdapterError("codex_output_schema_invalid")
            if "--output-schema" not in command:
                command[2:2] = ["--output-schema", output_schema]
        if prompt:
            command.append(prompt)
        return tuple(command)

    def build_process_spec(self, request: RunnerRequest) -> ProcessSpec:
        if request.profile.mode != "HEADLESS":
            raise RunnerPolicyError("codex_requires_headless_profile")
        command = self.build_command(request.command, ephemeral=True)
        self._validate_command_paths(command, request.workspace_path)
        return CommandAdapter(self.descriptor).build_process_spec(request.__class__(
            project_id=request.project_id,
            task_id=request.task_id,
            run_id=request.run_id,
            agent_id=request.agent_id,
            device_id=request.device_id,
            workspace_id=request.workspace_id,
            workspace_path=request.workspace_path,
            command=command,
            profile=request.profile,
            environment=request.environment,
            input_paths=request.input_paths,
            output_paths=request.output_paths,
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=request.max_output_bytes,
            terminal_size=request.terminal_size,
            terminal_backend=request.terminal_backend,
        ))

    def new_event_parser(self) -> CodexJsonlEventParser:
        return CodexJsonlEventParser()

    def _reject_unsafe_flags(self, command: Sequence[str]) -> None:
        if any(token in self._UNSAFE_FLAGS for token in command):
            raise CliAdapterError("codex_unsafe_approval_or_sandbox_flag")
        if any(token in self._PATH_ESCAPE_FLAGS for token in command):
            raise CliAdapterError("codex_path_escape_flag_not_allowed")
        if any(token in self._CONFIG_OVERRIDE_FLAGS for token in command):
            raise CliAdapterError("codex_config_override_not_allowed")
        for index, token in enumerate(command[:-1]):
            if token in {"--sandbox", "-s"} and command[index + 1] == "danger-full-access":
                raise CliAdapterError("codex_unrestricted_sandbox_not_allowed")

    def _validate_command_paths(self, command: Sequence[str], workspace_path: str) -> None:
        workspace = os.path.normcase(os.path.realpath(os.path.abspath(workspace_path)))
        for index, token in enumerate(command[:-1]):
            if token != "--output-schema":
                continue
            schema_path = command[index + 1]
            candidate = schema_path if os.path.isabs(schema_path) else os.path.join(workspace, schema_path)
            normalized = os.path.normcase(os.path.realpath(os.path.abspath(candidate)))
            try:
                within_workspace = os.path.commonpath([normalized, workspace]) == workspace
            except ValueError:
                within_workspace = False
            if not within_workspace:
                raise CliAdapterError("codex_output_schema_outside_workspace")


class ClaudeCodeAdapter:
    """Explicitly unavailable until Claude's stable machine protocol is frozen."""

    CAPABILITIES: tuple[str, ...] = ("cli.version",)

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or "claude"
        self.descriptor = AdapterDescriptor(
            adapter_id="claude-code",
            agent_name="Claude Code",
            agent_version="unknown",
            adapter_version="0.1.0",
            supported_os=("Windows", "Linux", "Darwin"),
            supported_execution_profiles=("HEADLESS",),
            supported_capabilities=self.CAPABILITIES,
            launch_command="unavailable",
            input_mode="unsupported",
            output_mode="unsupported",
            approval_mode="unsupported",
            interrupt_mode="unsupported",
            artifact_detection_mode="unsupported",
        )

    def probe(self, timeout_seconds: float = 5.0) -> CliCapabilityStatus:
        status = probe_cli(
            adapter_id=self.descriptor.adapter_id,
            executable=self.executable,
            capabilities=self.CAPABILITIES,
            accepted_version_prefixes=None,
            timeout_seconds=timeout_seconds,
        )
        if status.state == "NOT_INSTALLED":
            return status
        return CliCapabilityStatus(
            adapter_id=status.adapter_id,
            executable=status.executable,
            state="UNSUPPORTED",
            version=status.version,
            reason="claude_cli_semantic_adapter_not_implemented",
            capabilities=status.capabilities,
            checked_at=status.checked_at,
        )

    def build_process_spec(self, request: RunnerRequest) -> ProcessSpec:
        raise CliAdapterError("claude_cli_adapter_unavailable:semantic_protocol_not_implemented")

    def new_event_parser(self) -> None:
        return None


__all__ = [
    "ClaudeCodeAdapter",
    "CliAdapterError",
    "CliCapabilityStatus",
    "CliProtocolError",
    "CodexAdapter",
    "CodexCompatibilityRule",
    "CodexJsonlEventParser",
    "NormalizedCliEvent",
    "parse_cli_version",
    "probe_cli",
]
