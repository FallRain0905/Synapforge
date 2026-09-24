"""Device Gateway protocol boundary.

The Gateway owns connection identity and protocol sequencing. Task and Run
commands are dispatched through the repository only after project-scoped
capability validation; desktop commands remain outside this protocol.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from typing import Any
from uuid import UUID, uuid4

from packages.agent_protocol import AgentEventPayload, GATEWAY_COMMAND_TYPES

from .contracts import (
    AgentConnection,
    AgentHeartbeat,
    Device,
    GatewayCommandResult,
    GatewayEnvelope,
    GatewayEventAck,
    GatewayReplayRequest,
    ArtifactCreate,
    HandoffCreate,
    HandoffDecisionRequest,
    ReviewCreate,
    RunComplete,
    RunCreate,
    TaskClaimRequest,
    TaskLeaseHeartbeat,
    TaskProgressRequest,
    TaskResultSubmit,
)
from .repository import PlatformRepository


class GatewayProtocolError(ValueError):
    def __init__(self, code: str, message: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class GatewayContext:
    device: Device
    connection: AgentConnection


class GatewayService:
    COMMAND_TYPES = set(GATEWAY_COMMAND_TYPES)

    def __init__(self, repository: PlatformRepository) -> None:
        self.repository = repository

    def authenticate(
        self,
        device_token: str,
        *,
        device_id: str,
        session_id: str,
        connection_id: str,
        transport: str = "websocket",
    ) -> GatewayContext:
        device = self.repository.resolve_device_token(device_token)
        if device.device_id != device_id:
            raise GatewayProtocolError("gateway_device_path_mismatch")
        connection = self.repository.open_agent_connection(device_id, session_id, connection_id, transport)
        return GatewayContext(device=device, connection=connection)

    def _assert_context(self, context: GatewayContext) -> AgentConnection:
        connection = self.repository.get_agent_connection(context.connection.connection_id)
        if connection.device_id != context.device.device_id or connection.agent_id != context.device.agent_id:
            raise GatewayProtocolError("gateway_connection_identity_mismatch")
        if connection.status == "REVOKED":
            # A revoked connection cannot receive an error frame either; the
            # transport layer must close it instead of advancing its sequence.
            raise GatewayProtocolError("device_connection_revoked")
        return connection

    def _response(self, context: GatewayContext, message_type: str, payload: dict[str, Any], idempotency_key: str) -> GatewayEnvelope:
        connection = self._assert_context(context)
        sequence = self.repository.next_gateway_send_sequence(connection.connection_id)
        return GatewayEnvelope(
            device_id=context.device.device_id,
            agent_id=context.device.agent_id,
            session_id=connection.session_id,
            connection_id=connection.connection_id,
            sequence=sequence,
            message_id=f"gw-{uuid4().hex}",
            idempotency_key=idempotency_key,
            message_type=message_type,
            schema_version="1.0",
            sent_at=datetime.now(UTC),
            payload=payload,
        )

    def _error(self, context: GatewayContext, code: str, *, idempotency_key: str | None = None) -> GatewayEnvelope:
        return self._response(
            context,
            "gateway.error",
            {"code": code},
            idempotency_key or f"gateway-error:{code}:{uuid4().hex}",
        )

    def _ack(
        self,
        context: GatewayContext,
        envelope: GatewayEnvelope,
        highest_sequence: int,
        duplicate: bool = False,
        command_result: Any | None = None,
        command_result_present: bool = False,
    ) -> GatewayEnvelope:
        payload = GatewayEventAck(
            device_id=context.device.device_id,
            connection_id=context.connection.connection_id,
            highest_contiguous_sequence=highest_sequence,
            acknowledged_message_ids=[envelope.message_id],
            received_at=datetime.now(UTC),
        ).model_dump(mode="json")
        payload["duplicate"] = duplicate
        if command_result_present or command_result is not None:
            payload["command_result"] = {
                "message_type": envelope.message_type,
                "result": command_result,
            }
        return self._response(context, "gateway.ack", payload, f"gateway-ack:{envelope.message_id}")

    def _stored_command_response(
        self,
        context: GatewayContext,
        envelope: GatewayEnvelope,
        highest_sequence: int,
        stored: GatewayCommandResult,
    ) -> GatewayEnvelope:
        if stored.message_type != envelope.message_type or stored.idempotency_key != envelope.idempotency_key:
            return self._error(context, "gateway_command_result_identity_conflict", idempotency_key=f"gateway-error:{envelope.message_id}")
        expected_hash = self._command_request_hash(envelope)
        if stored.request_hash and stored.request_hash != expected_hash:
            return self._error(context, "gateway_command_request_mismatch", idempotency_key=f"gateway-error:{envelope.message_id}")
        if stored.response_type == "gateway.error":
            return self._error(context, stored.error_code or "gateway_command_failed", idempotency_key=f"gateway-error:{envelope.message_id}")
        return self._ack(
            context,
            envelope,
            highest_sequence,
            duplicate=True,
            command_result=stored.result,
            command_result_present=True,
        )

    def _save_command_success(
        self,
        context: GatewayContext,
        envelope: GatewayEnvelope,
        result: Any,
    ) -> GatewayCommandResult:
        return self.repository.save_gateway_command_result(
            GatewayCommandResult(
                id=uuid4(),
                connection_id=context.connection.connection_id,
                message_id=envelope.message_id,
                idempotency_key=envelope.idempotency_key,
                sequence=envelope.sequence,
                message_type=envelope.message_type,
                status="SUCCEEDED",
                response_type="gateway.ack",
                request_hash=self._command_request_hash(envelope),
                result=result,
                created_at=datetime.now(UTC),
            )
        )

    def _save_command_error(
        self,
        context: GatewayContext,
        envelope: GatewayEnvelope,
        error_code: str,
    ) -> GatewayCommandResult:
        return self.repository.save_gateway_command_result(
            GatewayCommandResult(
                id=uuid4(),
                connection_id=context.connection.connection_id,
                message_id=envelope.message_id,
                idempotency_key=envelope.idempotency_key,
                sequence=envelope.sequence,
                message_type=envelope.message_type,
                status="FAILED",
                response_type="gateway.error",
                request_hash=self._command_request_hash(envelope),
                error_code=error_code,
                created_at=datetime.now(UTC),
            )
        )

    def _replay_required(self, context: GatewayContext, envelope: GatewayEnvelope, expected_sequence: int) -> GatewayEnvelope:
        replay = GatewayReplayRequest(
            device_id=context.device.device_id,
            connection_id=context.connection.connection_id,
            after_sequence=max(0, expected_sequence - 1),
            limit=1000,
        )
        return self._response(
            context,
            "gateway.replay_required",
            replay.model_dump(mode="json"),
            f"gateway-replay:{envelope.message_id}",
        )

    def _validate_heartbeat(self, context: GatewayContext, envelope: GatewayEnvelope) -> AgentHeartbeat:
        try:
            heartbeat = AgentHeartbeat.model_validate(envelope.payload)
        except Exception as error:
            raise GatewayProtocolError("invalid_agent_heartbeat") from error
        if (
            heartbeat.device_id != envelope.device_id
            or heartbeat.agent_id != envelope.agent_id
            or heartbeat.session_id != envelope.session_id
            or heartbeat.connection_id != envelope.connection_id
            or heartbeat.device_id != context.device.device_id
            or heartbeat.agent_id != context.device.agent_id
        ):
            raise GatewayProtocolError("gateway_identity_mismatch")
        return heartbeat

    def _require_command_scope(self, context: GatewayContext, payload: dict[str, Any], capability: str) -> UUID:
        token = payload.get("project_token")
        project_value = payload.get("project_id")
        if not isinstance(token, str) or len(token) < 16 or not project_value:
            raise GatewayProtocolError("gateway_project_token_required")
        try:
            project_id = UUID(str(project_value))
        except (TypeError, ValueError) as error:
            raise GatewayProtocolError("invalid_gateway_project_id") from error
        try:
            grant = self.repository.resolve_device_project_token(token, project_id, capability)
        except PermissionError as error:
            raise GatewayProtocolError(str(error)) from error
        if grant.device_id != context.device.device_id or grant.agent_id != context.connection.agent_id:
            raise GatewayProtocolError("gateway_project_token_identity_mismatch")
        return project_id

    @staticmethod
    def _uuid(value: Any, error_code: str) -> UUID:
        try:
            return UUID(str(value))
        except (TypeError, ValueError) as error:
            raise GatewayProtocolError(error_code) from error

    @staticmethod
    def _command_request_hash(envelope: GatewayEnvelope) -> str:
        """Fingerprint command semantics without retaining the capability secret."""
        payload = dict(envelope.payload)
        payload.pop("project_token", None)
        encoded = json.dumps(
            {"message_type": envelope.message_type, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _execute_command(self, context: GatewayContext, envelope: GatewayEnvelope) -> Any:
        payload = dict(envelope.payload)
        if envelope.message_type == "agent.task.claim":
            project_id = self._require_command_scope(context, payload, "task.claim")
            try:
                data = TaskClaimRequest(
                    agent_id=context.connection.agent_id,
                    lease_seconds=payload.get("lease_seconds", 900),
                    idempotency_key=envelope.idempotency_key,
                )
                stages = payload.get("stages", [])
                if not isinstance(stages, list) or not all(isinstance(stage, str) for stage in stages):
                    raise GatewayProtocolError("invalid_gateway_stages")
                claim = self.repository.claim_next_task(data, project_id, stages)
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            if claim is None:
                return None
            task, lease = claim
            return {"task": task.model_dump(mode="json"), "lease": lease.model_dump(mode="json")}

        if envelope.message_type == "agent.task.lease.heartbeat":
            project_id = self._require_command_scope(context, payload, "task.lease")
            try:
                lease_token = payload["lease_token"]
                data = TaskLeaseHeartbeat(
                    agent_id=context.connection.agent_id,
                    project_id=project_id,
                    extend_seconds=payload.get("extend_seconds", 900),
                )
                lease = self.repository.heartbeat_lease(lease_token, data.agent_id, data.extend_seconds, project_id)
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return lease.model_dump(mode="json")

        if envelope.message_type == "agent.task.progress":
            project_id = self._require_command_scope(context, payload, "task.progress")
            task_id = self._uuid(payload.get("task_id"), "invalid_gateway_task_id")
            try:
                task = self.repository.get_task(task_id)
                if task.project_id != project_id:
                    raise GatewayProtocolError("gateway_project_resource_mismatch")
                data = TaskProgressRequest(
                    agent_id=context.connection.agent_id,
                    lease_token=payload["lease_token"],
                    status=payload["status"],
                    message=payload.get("message", ""),
                    idempotency_key=envelope.idempotency_key,
                )
                updated = self.repository.update_task_progress(task_id, data)
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return updated.model_dump(mode="json")

        if envelope.message_type == "agent.task.result":
            project_id = self._require_command_scope(context, payload, "task.result")
            task_id = self._uuid(payload.get("task_id"), "invalid_gateway_task_id")
            try:
                task = self.repository.get_task(task_id)
                if task.project_id != project_id:
                    raise GatewayProtocolError("gateway_project_resource_mismatch")
                data = TaskResultSubmit(
                    agent_id=context.connection.agent_id,
                    lease_token=payload["lease_token"],
                    success=payload["success"],
                    summary=payload.get("summary", ""),
                    output_artifact_ids=[self._uuid(value, "invalid_gateway_artifact_id") for value in payload.get("output_artifact_ids", [])],
                    handoff_id=self._uuid(payload["handoff_id"], "invalid_gateway_handoff_id") if payload.get("handoff_id") else None,
                    idempotency_key=envelope.idempotency_key,
                )
                result = self.repository.submit_task_result(task_id, data)
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return result.model_dump(mode="json")

        if envelope.message_type == "agent.run.create":
            project_id = self._require_command_scope(context, payload, "run.create")
            command_payload = {key: value for key, value in payload.items() if key not in {"project_id", "project_token"}}
            supplied_agent = command_payload.get("agent_id", context.connection.agent_id)
            if supplied_agent != context.connection.agent_id:
                raise GatewayProtocolError("gateway_agent_identity_mismatch")
            command_payload.update(agent_id=context.connection.agent_id, idempotency_key=envelope.idempotency_key)
            try:
                data = RunCreate.model_validate(command_payload)
                run = self.repository.create_run(project_id, data)
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return run.model_dump(mode="json")

        if envelope.message_type == "agent.run.complete":
            project_id = self._require_command_scope(context, payload, "run.complete")
            run_id = self._uuid(payload.get("run_id"), "invalid_gateway_run_id")
            try:
                run = self.repository.get_run(run_id)
                if run.project_id != project_id or run.agent_id != context.connection.agent_id:
                    raise GatewayProtocolError("gateway_project_resource_mismatch")
                command_payload = {key: value for key, value in payload.items() if key not in {"project_id", "project_token", "run_id"}}
                command_payload["idempotency_key"] = envelope.idempotency_key
                data = RunComplete.model_validate(command_payload)
                completed = self.repository.complete_run(run_id, data)
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return completed.model_dump(mode="json")

        if envelope.message_type == "agent.handoff.create":
            project_id = self._require_command_scope(context, payload, "handoff.create")
            command_payload = {
                key: value for key, value in payload.items()
                if key not in {"project_id", "project_token", "sender_agent_id"}
            }
            command_payload["idempotency_key"] = envelope.idempotency_key
            try:
                data = HandoffCreate.model_validate(command_payload)
                task = self.repository.get_task(data.task_id)
                if task.project_id != project_id:
                    raise GatewayProtocolError("gateway_project_resource_mismatch")
                handoff = self.repository.create_handoff(project_id, data, sender_agent_id=context.connection.agent_id)
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return handoff.model_dump(mode="json")

        if envelope.message_type == "agent.handoff.accept":
            project_id = self._require_command_scope(context, payload, "handoff.accept")
            handoff_id = self._uuid(payload.get("handoff_id"), "invalid_gateway_handoff_id")
            try:
                handoff = self.repository.get_handoff(handoff_id)
                if handoff.project_id != project_id:
                    raise GatewayProtocolError("gateway_project_resource_mismatch")
                if not any(item.receiver_type == "agent" and item.receiver_id == context.connection.agent_id for item in handoff.receipts):
                    raise GatewayProtocolError("gateway_handoff_receiver_mismatch")
                accepted = self.repository.accept_handoff(
                    handoff_id,
                    context.connection.agent_id,
                    actor_kind="agent",
                )
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return accepted.model_dump(mode="json")

        if envelope.message_type == "agent.handoff.reject":
            project_id = self._require_command_scope(context, payload, "handoff.reject")
            handoff_id = self._uuid(payload.get("handoff_id"), "invalid_gateway_handoff_id")
            try:
                handoff = self.repository.get_handoff(handoff_id)
                if handoff.project_id != project_id:
                    raise GatewayProtocolError("gateway_project_resource_mismatch")
                if not any(item.receiver_type == "agent" and item.receiver_id == context.connection.agent_id for item in handoff.receipts):
                    raise GatewayProtocolError("gateway_handoff_receiver_mismatch")
                command_payload = {key: value for key, value in payload.items() if key not in {"project_id", "project_token", "handoff_id"}}
                decision = HandoffDecisionRequest.model_validate(command_payload)
                rejected = self.repository.reject_handoff(
                    handoff_id,
                    context.connection.agent_id,
                    decision.reason,
                    decision.findings,
                    "agent",
                )
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return rejected.model_dump(mode="json")

        if envelope.message_type == "agent.review.submit":
            project_id = self._require_command_scope(context, payload, "review.submit")
            command_payload = {
                key: value for key, value in payload.items()
                if key not in {"project_id", "project_token", "reviewer", "reviewer_kind"}
            }
            command_payload.update(
                reviewer=context.connection.agent_id,
                reviewer_kind="agent",
                idempotency_key=envelope.idempotency_key,
            )
            try:
                review = self.repository.create_review(project_id, ReviewCreate.model_validate(command_payload))
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return review.model_dump(mode="json")

        if envelope.message_type == "agent.artifact.create":
            project_id = self._require_command_scope(context, payload, "artifact.write")
            command_payload = {
                key: value for key, value in payload.items()
                if key not in {"project_id", "project_token", "created_by", "created_by_kind"}
            }
            if payload.get("created_by") not in {None, context.connection.agent_id}:
                raise GatewayProtocolError("gateway_agent_identity_mismatch")
            if payload.get("created_by_kind") not in {None, "agent"}:
                raise GatewayProtocolError("gateway_agent_identity_mismatch")
            try:
                data = ArtifactCreate.model_validate(command_payload)
                if data.task_id:
                    task = self.repository.get_task(data.task_id)
                    if task.project_id != project_id:
                        raise GatewayProtocolError("gateway_project_resource_mismatch")
                if data.run_id:
                    run = self.repository.get_run(data.run_id)
                    if run.project_id != project_id or run.agent_id != context.connection.agent_id:
                        raise GatewayProtocolError("gateway_project_resource_mismatch")
                for artifact_id in data.input_artifact_ids:
                    artifact = self.repository.get_artifact(artifact_id)
                    if artifact.project_id != project_id:
                        raise GatewayProtocolError("gateway_project_resource_mismatch")
                artifact = self.repository.create_artifact(
                    project_id,
                    data,
                    created_by=context.connection.agent_id,
                    created_by_kind="agent",
                )
            except GatewayProtocolError:
                raise
            except Exception as error:
                raise GatewayProtocolError(str(error)) from error
            return artifact.model_dump(mode="json")

        raise GatewayProtocolError("gateway_message_type_not_supported")

    def _record_agent_event(self, context: GatewayContext, envelope: GatewayEnvelope) -> None:
        """Validate and persist a local runtime event without retaining the token."""
        try:
            payload = AgentEventPayload.model_validate(envelope.payload)
        except Exception as error:
            raise GatewayProtocolError("invalid_agent_event_payload") from error
        try:
            project_id = self._require_command_scope(
                context,
                {"project_id": str(payload.project_id), "project_token": payload.project_token},
                "run.event",
            )
        except GatewayProtocolError:
            raise
        except (KeyError, PermissionError, ValueError) as error:
            raise GatewayProtocolError(str(error)) from error
        event = payload.event
        if payload.project_id != project_id or event.project_id != str(project_id):
            raise GatewayProtocolError("gateway_project_resource_mismatch")
        object_id = None
        if event.run_id:
            run_id = self._uuid(event.run_id, "invalid_gateway_run_id")
            try:
                run = self.repository.get_run(run_id)
            except Exception as error:
                raise GatewayProtocolError("gateway_run_not_found") from error
            if run.project_id != project_id or run.agent_id != context.connection.agent_id:
                raise GatewayProtocolError("gateway_project_resource_mismatch")
            object_id = run_id
        fingerprint = hashlib.sha256(
            json.dumps(envelope.payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if self.repository.get_idempotent_response(envelope.idempotency_key, "agent.event", fingerprint) is not None:
            return
        platform_event = self.repository.add_event(
            project_id,
            f"agent.{event.event_type}",
            context.connection.agent_id,
            {
                "source": "agent-runtime",
                "event": event.model_dump(mode="json"),
            },
            actor_kind="agent",
            object_type="run" if object_id else None,
            object_id=object_id,
            idempotency_key=f"runtime-event:{context.connection.connection_id}:{envelope.idempotency_key}",
            schema_version=event.schema_version,
        )
        if event.event_type in {"run.completed", "run.failed"} and object_id is not None:
            completion = event.payload
            if not isinstance(completion, dict):
                raise GatewayProtocolError("invalid_agent_run_completion_payload")
            try:
                output_ids = [self._uuid(value, "invalid_gateway_artifact_id") for value in completion.get("output_artifact_ids", [])]
                observed_files = [str(value) for value in completion.get("observed_input_files", [])]
            except (TypeError, AttributeError) as error:
                raise GatewayProtocolError("invalid_agent_run_completion_payload") from error
            self.repository.complete_run(
                object_id,
                RunComplete(
                    success=event.event_type == "run.completed",
                    stdout=str(completion.get("stdout", "")),
                    stderr=str(completion.get("stderr", "")),
                    summary=str(completion.get("summary", "")),
                    output_artifact_ids=output_ids,
                    observed_input_files=observed_files,
                    idempotency_key=f"run-event-complete:{envelope.idempotency_key}",
                ),
            )
        self.repository.save_idempotent_response(
            envelope.idempotency_key,
            "agent.event",
            {"accepted": True, "event_id": str(platform_event.id), "run_id": str(object_id) if object_id else None},
            fingerprint,
        )

    def _handle_agent_event(self, context: GatewayContext, envelope: GatewayEnvelope) -> None:
        self._record_agent_event(context, envelope)

    def receive(self, context: GatewayContext, raw_message: dict[str, Any]) -> GatewayEnvelope:
        try:
            envelope = GatewayEnvelope.model_validate(raw_message)
        except Exception:
            return self._error(context, "invalid_gateway_envelope")

        connection = self._assert_context(context)
        if (
            envelope.device_id != context.device.device_id
            or envelope.agent_id != context.device.agent_id
            or envelope.session_id != connection.session_id
            or envelope.connection_id != connection.connection_id
        ):
            return self._error(context, "gateway_identity_mismatch", idempotency_key=f"gateway-error:{envelope.message_id}")

        heartbeat: AgentHeartbeat | None = None
        if envelope.message_type == "agent.heartbeat":
            try:
                heartbeat = self._validate_heartbeat(context, envelope)
            except GatewayProtocolError as error:
                return self._error(context, error.code, idempotency_key=f"gateway-error:{envelope.message_id}")
        elif envelope.message_type == "agent.event":
            try:
                AgentEventPayload.model_validate(envelope.payload)
            except Exception:
                return self._error(context, "invalid_agent_event_payload", idempotency_key=f"gateway-error:{envelope.message_id}")
        elif envelope.message_type not in self.COMMAND_TYPES:
            return self._error(context, "gateway_message_type_not_supported", idempotency_key=f"gateway-error:{envelope.message_id}")

        try:
            sequence_status, highest_sequence = self.repository.record_gateway_receive(connection.connection_id, envelope.sequence)
        except ValueError as error:
            parts = str(error).split(":")
            if len(parts) == 3 and parts[0] == "gateway_sequence_gap":
                return self._replay_required(context, envelope, int(parts[1]))
            return self._error(context, "gateway_sequence_invalid", idempotency_key=f"gateway-error:{envelope.message_id}")
        except PermissionError as error:
            return self._error(context, str(error), idempotency_key=f"gateway-error:{envelope.message_id}")

        if sequence_status == "DUPLICATE":
            if envelope.message_type == "agent.event":
                try:
                    self._handle_agent_event(context, envelope)
                except GatewayProtocolError as error:
                    return self._error(context, error.code, idempotency_key=f"gateway-error:{envelope.message_id}")
                except (KeyError, PermissionError, ValueError) as error:
                    return self._error(context, str(error), idempotency_key=f"gateway-error:{envelope.message_id}")
                return self._ack(context, envelope, highest_sequence, duplicate=True)
            if envelope.message_type in self.COMMAND_TYPES:
                stored = self.repository.get_gateway_command_result(
                    connection.connection_id,
                    message_id=envelope.message_id,
                )
                if stored is None:
                    stored = self.repository.get_gateway_command_result(
                        connection.connection_id,
                        idempotency_key=envelope.idempotency_key,
                    )
                if stored is not None:
                    return self._stored_command_response(context, envelope, highest_sequence, stored)
            return self._ack(context, envelope, highest_sequence, duplicate=True)
        if heartbeat is not None:
            try:
                self.repository.record_agent_heartbeat(connection.connection_id, heartbeat)
            except (PermissionError, KeyError) as error:
                return self._error(context, str(error), idempotency_key=f"gateway-error:{envelope.message_id}")
        if envelope.message_type == "agent.event":
            try:
                self._handle_agent_event(context, envelope)
            except GatewayProtocolError as error:
                return self._error(context, error.code, idempotency_key=f"gateway-error:{envelope.message_id}")
            except (KeyError, PermissionError, ValueError) as error:
                return self._error(context, str(error), idempotency_key=f"gateway-error:{envelope.message_id}")
            return self._ack(context, envelope, highest_sequence)
        if envelope.message_type in self.COMMAND_TYPES:
            previous = self.repository.get_gateway_command_result(
                connection.connection_id,
                idempotency_key=envelope.idempotency_key,
            )
            if previous is not None:
                return self._stored_command_response(context, envelope, highest_sequence, previous)
            try:
                command_result = self._execute_command(context, envelope)
            except GatewayProtocolError as error:
                try:
                    stored = self._save_command_error(context, envelope, error.code)
                except (KeyError, PermissionError, ValueError) as save_error:
                    return self._error(context, str(save_error), idempotency_key=f"gateway-error:{envelope.message_id}")
                if stored.message_id != envelope.message_id:
                    return self._stored_command_response(context, envelope, highest_sequence, stored)
                return self._error(context, error.code, idempotency_key=f"gateway-error:{envelope.message_id}")
            try:
                stored = self._save_command_success(context, envelope, command_result)
            except (KeyError, PermissionError, ValueError) as save_error:
                return self._error(context, str(save_error), idempotency_key=f"gateway-error:{envelope.message_id}")
            if stored.request_hash and stored.request_hash != self._command_request_hash(envelope):
                return self._error(context, "gateway_command_request_mismatch", idempotency_key=f"gateway-error:{envelope.message_id}")
            return self._ack(
                context,
                envelope,
                highest_sequence,
                command_result=stored.result,
                command_result_present=True,
            )
        return self._ack(context, envelope, highest_sequence)

    def connected_message(self, context: GatewayContext) -> GatewayEnvelope:
        return self._response(
            context,
            "gateway.connected",
            {"connection": context.connection.model_dump(mode="json")},
            f"gateway-connected:{context.connection.connection_id}",
        )

    def close(self, context: GatewayContext) -> AgentConnection:
        return self.repository.close_agent_connection(context.connection.connection_id)
