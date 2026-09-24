"""Cross-process Agent Gateway protocol models.

This package intentionally contains transport contracts only. It must not
import the API application's domain repository or service modules.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


GATEWAY_COMMAND_TYPES = (
    "agent.task.claim",
    "agent.task.lease.heartbeat",
    "agent.task.progress",
    "agent.task.result",
    "agent.run.create",
    "agent.run.complete",
    "agent.handoff.create",
    "agent.handoff.accept",
    "agent.handoff.reject",
    "agent.review.submit",
    "agent.artifact.create",
)

SESSION_IPC_MESSAGE_TYPES = (
    "session.hello",
    "session.status",
    "session.run.start",
    "session.run.stop",
    "session.terminal.input",
    "session.terminal.resize",
    "session.approval.respond",
    "session.desktop.control",
    "session.worker.shutdown",
)


class ProtocolModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, use_enum_values=True)


class GatewayEnvelope(ProtocolModel):
    device_id: str
    agent_id: str
    session_id: str
    connection_id: str
    sequence: int = Field(ge=1)
    message_id: str = Field(min_length=8, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=160)
    message_type: str = Field(min_length=1, max_length=120)
    schema_version: str = "1.0"
    sent_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentHeartbeat(ProtocolModel):
    device_id: str
    agent_id: str
    session_id: str
    connection_id: str
    agent_version: str
    adapter_versions: dict[str, str] = Field(default_factory=dict)
    capabilities: list[str] = Field(default_factory=list)
    running_run_ids: list[UUID] = Field(default_factory=list)
    local_queue_length: int = Field(default=0, ge=0)
    user_session_state: Literal["unknown", "logged_in", "locked", "logged_out"] = "unknown"
    resource_summary: dict[str, Any] = Field(default_factory=dict)
    sent_at: datetime


class GatewayEventAck(ProtocolModel):
    device_id: str
    connection_id: str
    highest_contiguous_sequence: int = Field(default=0, ge=0)
    acknowledged_message_ids: list[str] = Field(default_factory=list)
    received_at: datetime


class GatewayReplayRequest(ProtocolModel):
    device_id: str
    connection_id: str
    after_sequence: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=1000)


class GatewayCommandResult(ProtocolModel):
    """Durable business outcome for one command idempotency scope."""

    id: UUID
    connection_id: str
    message_id: str = Field(min_length=8, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=160)
    sequence: int = Field(ge=1)
    message_type: str = Field(min_length=1, max_length=120)
    status: Literal["SUCCEEDED", "FAILED"]
    response_type: Literal["gateway.ack", "gateway.error"]
    request_hash: str | None = Field(default=None, min_length=64, max_length=64)
    result: Any | None = None
    error_code: str | None = None
    created_at: datetime

    @model_validator(mode="after")
    def validate_response(self) -> "GatewayCommandResult":
        if self.status == "SUCCEEDED" and self.response_type != "gateway.ack":
            raise ValueError("gateway_command_success_must_use_ack")
        if self.status == "FAILED" and self.response_type != "gateway.error":
            raise ValueError("gateway_command_failure_must_use_error")
        if self.status == "FAILED" and not self.error_code:
            raise ValueError("gateway_command_failure_requires_error_code")
        if self.status == "SUCCEEDED" and self.error_code is not None:
            raise ValueError("gateway_command_success_cannot_have_error_code")
        return self


class SessionPeer(ProtocolModel):
    peer_id: str = Field(min_length=2, max_length=160)
    peer_kind: Literal["machine_service", "user_session_worker", "desktop_ui"]
    process_id: int | None = Field(default=None, ge=1)
    windows_session_id: int | None = Field(default=None, ge=0)
    user_sid: str | None = Field(default=None, min_length=2, max_length=256)


class SessionRunContext(ProtocolModel):
    project_id: str = Field(min_length=2, max_length=160)
    task_id: str | None = Field(default=None, min_length=2, max_length=160)
    run_id: str = Field(min_length=2, max_length=160)
    agent_id: str = Field(min_length=2, max_length=160)
    device_id: str = Field(min_length=2, max_length=160)
    workspace_id: str = Field(min_length=2, max_length=160)
    workspace_path: str = Field(min_length=1, max_length=4096)
    execution_mode: Literal["HEADLESS", "USER_SESSION", "INTERACTIVE_DESKTOP"]
    allow_remote_terminal: bool = False
    allow_desktop_control: bool = False


class TerminalSize(ProtocolModel):
    """Requested terminal dimensions for a PTY-backed run."""

    columns: int = Field(ge=1, le=1000)
    rows: int = Field(ge=1, le=1000)
    pixel_width: int | None = Field(default=None, ge=0, le=100000)
    pixel_height: int | None = Field(default=None, ge=0, le=100000)


class SessionRunStartPayload(ProtocolModel):
    adapter_id: str = Field(min_length=2, max_length=160)
    command: list[str] = Field(min_length=1, max_length=256)
    environment: dict[str, str] = Field(default_factory=dict)
    input_paths: list[str] = Field(default_factory=list, max_length=256)
    output_paths: list[str] = Field(default_factory=list, max_length=256)
    timeout_seconds: float | None = Field(default=None, gt=0)
    max_output_bytes: int = Field(default=1_000_000, ge=1, le=64 * 1024 * 1024)
    network_policy: Literal["deny-by-default", "allow-listed", "unrestricted"] = "deny-by-default"
    terminal_size: TerminalSize = Field(default_factory=lambda: TerminalSize(columns=80, rows=24))
    terminal_backend: Literal["PIPE", "CONPTY"] = "PIPE"
    execution_backend: Literal["host", "container"] = "host"
    container_runtime: Literal["docker", "podman"] | None = None
    container_image: str | None = Field(default=None, min_length=1, max_length=512)
    source_commit: str | None = None
    environment_image_digest: str | None = None
    dependency_lock: str | None = None
    parameters: dict[str, Any] = Field(default_factory=dict)
    random_seed: int | None = None
    model_provider: str | None = None
    model_name: str | None = None
    tool_versions: dict[str, str] = Field(default_factory=dict)
    data_access_policy: dict[str, Any] = Field(default_factory=dict)
    observed_input_files: list[str] = Field(default_factory=list, max_length=256)
    observed_network_connections: list[dict[str, Any]] = Field(default_factory=list, max_length=256)

    @model_validator(mode="after")
    def validate_command(self) -> "SessionRunStartPayload":
        if any(not isinstance(part, str) or not part for part in self.command):
            raise ValueError("session_run_command_invalid")
        if self.execution_backend == "container" and not self.container_image:
            raise ValueError("session_container_image_required")
        if self.execution_backend == "container" and self.container_runtime is None:
            raise ValueError("session_container_runtime_required")
        if self.execution_backend == "host" and (self.container_runtime is not None or self.container_image is not None):
            raise ValueError("session_host_container_fields_not_allowed")
        return self


class SessionLifecycleTransition(ProtocolModel):
    """Auditable service/worker/session lifecycle transition."""

    transition_id: str = Field(min_length=8, max_length=160)
    worker_id: str = Field(min_length=2, max_length=160)
    user_session_id: str = Field(min_length=2, max_length=160)
    event_type: Literal[
        "service.start.requested",
        "worker.registered",
        "worker.ready",
        "worker.exited",
        "session.status.changed",
        "run.started",
        "run.stopped",
        "service.stop.requested",
        "worker.stopped",
        "worker.failed",
        "emergency.stop",
        "recovery.requested",
    ]
    from_state: str | None = None
    to_state: str
    session_state: Literal["unknown", "logged_in", "locked", "logged_out"]
    active_run_ids: list[str] = Field(default_factory=list)
    cleanup_run_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=1000)
    occurred_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


class SessionIpcRequest(ProtocolModel):
    schema_version: str = "1.0"
    request_id: str = Field(min_length=8, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=160)
    message_type: Literal[
        "session.hello",
        "session.status",
        "session.run.start",
        "session.run.stop",
        "session.terminal.input",
        "session.terminal.resize",
        "session.approval.respond",
        "session.desktop.control",
        "session.worker.shutdown",
    ]
    peer: SessionPeer
    worker_id: str = Field(min_length=2, max_length=160)
    user_session_id: str | None = Field(default=None, min_length=2, max_length=160)
    context: SessionRunContext | None = None
    approval_id: str | None = Field(default=None, min_length=2, max_length=160)
    sent_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_command_boundary(self) -> "SessionIpcRequest":
        context_required = {
            "session.run.start",
            "session.run.stop",
            "session.terminal.input",
            "session.terminal.resize",
            "session.desktop.control",
        }
        if self.message_type in context_required and self.context is None:
            raise ValueError("session_context_required")
        if self.message_type in {"session.run.start", "session.run.stop", "session.terminal.input", "session.terminal.resize"} and self.user_session_id is None:
            raise ValueError("session_user_id_required")
        if self.message_type in {"session.terminal.input", "session.terminal.resize"} and not self.context.allow_remote_terminal:
            raise ValueError("session_remote_terminal_not_allowed")
        if self.message_type == "session.terminal.resize":
            TerminalSize.model_validate(self.payload)
        if self.message_type == "session.desktop.control":
            if self.context.execution_mode != "INTERACTIVE_DESKTOP" or not self.context.allow_desktop_control:
                raise ValueError("session_desktop_control_not_allowed")
            if self.approval_id is None:
                raise ValueError("session_desktop_control_approval_required")
        if self.message_type == "session.approval.respond" and self.approval_id is None:
            raise ValueError("session_approval_id_required")
        return self


class SessionIpcResponse(ProtocolModel):
    schema_version: str = "1.0"
    request_id: str = Field(min_length=8, max_length=160)
    idempotency_key: str = Field(min_length=8, max_length=160)
    worker_id: str = Field(min_length=2, max_length=160)
    status: Literal["ACCEPTED", "COMPLETED", "REJECTED", "FAILED", "DUPLICATE"]
    error_code: str | None = None
    responded_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_error(self) -> "SessionIpcResponse":
        if self.status in {"REJECTED", "FAILED"} and not self.error_code:
            raise ValueError("session_failure_requires_error_code")
        if self.status not in {"REJECTED", "FAILED"} and self.error_code is not None:
            raise ValueError("session_success_cannot_have_error_code")
        return self


class SessionIpcEvent(ProtocolModel):
    schema_version: str = "1.0"
    event_id: str = Field(min_length=8, max_length=160)
    worker_id: str = Field(min_length=2, max_length=160)
    event_type: Literal[
        "process.started",
        "process.stdout",
        "process.stderr",
        "terminal.resized",
        "run.started",
        "terminal.prompt",
        "agent.message",
        "approval.requested",
        "tool.started",
        "tool.completed",
        "file.changed",
        "artifact.created",
        "process.exited",
        "run.completed",
        "run.failed",
    ]
    sequence: int = Field(ge=1)
    project_id: str = Field(min_length=2, max_length=160)
    run_id: str | None = Field(default=None, min_length=2, max_length=160)
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class AgentEventPayload(ProtocolModel):
    """Durable Gateway payload for one local runtime event.

    The project and capability token are carried in the payload because a
    Gateway connection can serve grants for more than one project. The token
    is used only for authorization and is removed before the platform event
    is persisted. Production agents should source it from a system secret
    store; the development CLI still accepts it as a configuration value and
    may keep it in the local recovery queue.
    """

    project_id: UUID
    project_token: str = Field(min_length=16, max_length=240)
    event: SessionIpcEvent


__all__ = [
    "GATEWAY_COMMAND_TYPES",
    "SESSION_IPC_MESSAGE_TYPES",
    "AgentHeartbeat",
    "AgentEventPayload",
    "GatewayCommandResult",
    "GatewayEnvelope",
    "GatewayEventAck",
    "GatewayReplayRequest",
    "SessionIpcEvent",
    "SessionIpcRequest",
    "SessionIpcResponse",
    "SessionPeer",
    "SessionRunContext",
    "SessionRunStartPayload",
    "SessionLifecycleTransition",
    "TerminalSize",
]
