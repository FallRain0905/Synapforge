"""Durable client-side protocol adapter for the Agent Gateway."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import UUID

try:
    from packages.agent_protocol import AgentEventPayload, GatewayEnvelope, GatewayEventAck, GatewayReplayRequest, SessionIpcEvent
except ImportError:  # Support direct ``python apps/agent/agentd.py`` execution.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from packages.agent_protocol import AgentEventPayload, GatewayEnvelope, GatewayEventAck, GatewayReplayRequest, SessionIpcEvent

try:
    from .local_state import LocalAgentState
except ImportError:  # Support ``python apps/agent/agentd.py`` during development.
    from local_state import LocalAgentState


@dataclass(frozen=True)
class GatewayIdentity:
    device_id: str
    agent_id: str
    session_id: str
    connection_id: str


class RuntimeEventUploader:
    """Adapt local SessionIpcEvent objects to durable Gateway events."""

    def __init__(self, client: "DurableGatewayClient") -> None:
        self.client = client
        self._project_tokens: dict[str, str] = {}

    def bind_project(self, project_id: str | UUID, project_token: str) -> None:
        if len(project_token) < 16:
            raise ValueError("runtime_event_project_token_invalid")
        self._project_tokens[str(project_id)] = project_token

    def enqueue(self, event: SessionIpcEvent) -> dict[str, Any]:
        project_id = str(event.project_id)
        token = self._project_tokens.get(project_id)
        if token is None:
            raise KeyError("runtime_event_project_not_bound")
        payload = AgentEventPayload(project_id=UUID(project_id), project_token=token, event=event)
        return self.client.queue_event(
            "agent.event",
            payload.model_dump(mode="json"),
            idempotency_key=f"runtime-event:{event.worker_id}:{event.sequence}",
            message_id=event.event_id,
        )


class DurableGatewayClient:
    """Build and consume Gateway frames while persisting recovery state.

    The device token is deliberately supplied by the caller and is never
    written to ``LocalAgentState``. A future desktop service can supply it
    from an OS credential vault.
    """

    def __init__(self, state: LocalAgentState, identity: GatewayIdentity) -> None:
        self.state = state
        self.identity = identity
        self._command_waiters: dict[str, asyncio.Future[dict[str, Any]]] = {}

    def queue_event(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        message_id: str | None = None,
    ) -> dict[str, Any]:
        return self.state.enqueue_event(
            message_type,
            payload,
            idempotency_key=idempotency_key,
            message_id=message_id,
        )

    # 信封里会带身份的字段名（心跳的 payload 里也有同一组）
    _IDENTITY_FIELDS = ("device_id", "agent_id", "session_id", "connection_id")

    def _restamp_identity(self, payload: Any) -> Any:
        """发送时把 payload 里的身份字段统一成**当前**连接身份。

        事件是在入队时构造的，而每个会话都会换 `connection_id`（断线重连、进程重启都换）。
        入队时盖的旧 connection_id 会随事件一起留在 payload 里，平台收到后判为
        `gateway_identity_mismatch` 并拒绝整帧——积压的心跳因此永远发不出去、
        还会因为序号没被记录而把后续事件一起卡住。
        """

        if not isinstance(payload, dict):
            return payload
        current = {
            "device_id": self.identity.device_id,
            "agent_id": self.identity.agent_id,
            "session_id": self.identity.session_id,
            "connection_id": self.identity.connection_id,
        }
        restamped = dict(payload)
        for field in self._IDENTITY_FIELDS:
            if field in restamped:
                restamped[field] = current[field]
        return restamped

    def _envelope(self, event: dict[str, Any]) -> GatewayEnvelope:
        return GatewayEnvelope(
            device_id=self.identity.device_id,
            agent_id=self.identity.agent_id,
            session_id=self.identity.session_id,
            connection_id=self.identity.connection_id,
            sequence=int(event["sequence"]),
            message_id=event["message_id"],
            idempotency_key=event["idempotency_key"],
            message_type=event["message_type"],
            schema_version="1.0",
            sent_at=datetime.now(UTC),
            payload=self._restamp_identity(event["payload"]),
        )

    def pending_envelopes(self, limit: int = 100, retry_after_seconds: int = 5) -> list[GatewayEnvelope]:
        events = self.state.pending_events(limit)
        envelopes: list[GatewayEnvelope] = []
        for event in events:
            self.state.mark_sent(int(event["sequence"]), retry_after_seconds=retry_after_seconds)
            envelopes.append(self._envelope(self.state.get_outbox_event(int(event["sequence"]))))
        return envelopes

    def handle_server_message(self, raw_message: dict[str, Any]) -> dict[str, Any]:
        try:
            envelope = GatewayEnvelope.model_validate(raw_message)
        except Exception as error:
            raise ValueError("invalid_gateway_server_envelope") from error
        if (
            envelope.device_id != self.identity.device_id
            or envelope.agent_id != self.identity.agent_id
            or envelope.session_id != self.identity.session_id
            or envelope.connection_id != self.identity.connection_id
        ):
            raise PermissionError("gateway_server_identity_mismatch")

        if envelope.message_type == "gateway.ack":
            try:
                ack = GatewayEventAck.model_validate(envelope.payload)
            except Exception as error:
                raise ValueError("invalid_gateway_ack") from error
            count = self.state.acknowledge(
                ack.highest_contiguous_sequence,
                ack.acknowledged_message_ids,
            )
            result: dict[str, Any] = {
                "type": "ack",
                "acked_count": count,
                "sequence": envelope.sequence,
                "acknowledged_message_ids": ack.acknowledged_message_ids,
                "message_id": envelope.message_id,
            }
            if "command_result" in envelope.payload:
                result["command_result"] = envelope.payload["command_result"]
            return result
        if envelope.message_type == "gateway.replay_required":
            try:
                replay = GatewayReplayRequest.model_validate(envelope.payload)
            except Exception as error:
                raise ValueError("invalid_gateway_replay_request") from error
            count = self.state.requeue_after(replay.after_sequence)
            return {"type": "replay_required", "requeued_count": count, "sequence": envelope.sequence}
        if envelope.message_type == "gateway.connected":
            return {"type": "connected", "sequence": envelope.sequence}
        if envelope.message_type == "gateway.error":
            return {
                "type": "error",
                "payload": envelope.payload,
                "sequence": envelope.sequence,
                "message_id": envelope.message_id,
                "idempotency_key": envelope.idempotency_key,
                "original_message_id": envelope.idempotency_key.removeprefix("gateway-error:"),
            }
        raise ValueError("unsupported_gateway_server_message")

    async def execute_command(
        self,
        message_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str,
        timeout_seconds: float = 30,
    ) -> Any:
        """Queue a Gateway command and await its ACK business result.

        ``run_once`` must be running in the same event loop. The command is
        durable before waiting, so a timeout or disconnect leaves it eligible
        for replay on the next connection.
        """
        if timeout_seconds <= 0:
            raise ValueError("gateway_command_timeout_invalid")
        loop = asyncio.get_running_loop()
        event = self.queue_event(message_type, payload, idempotency_key=idempotency_key)
        message_id = str(event["message_id"])
        waiter: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._command_waiters[message_id] = waiter
        try:
            result = await asyncio.wait_for(waiter, timeout=timeout_seconds)
            if result.get("type") == "error":
                raise RuntimeError(str(result.get("payload", {}).get("code", "gateway_command_failed")))
            command_result = result.get("command_result")
            if not isinstance(command_result, dict) or "result" not in command_result:
                raise RuntimeError("gateway_command_result_missing")
            return command_result["result"]
        finally:
            self._command_waiters.pop(message_id, None)

    async def create_run(
        self,
        project_id: str,
        project_token: str,
        *,
        task_id: str | None = None,
        source_commit: str | None = None,
        input_artifact_ids: list[str] | None = None,
        idempotency_key: str,
        **manifest: Any,
    ) -> Any:
        payload = {
            "project_id": project_id,
            "project_token": project_token,
            "task_id": task_id,
            "source_commit": source_commit,
            "input_artifact_ids": input_artifact_ids or [],
            **manifest,
        }
        return await self.execute_command("agent.run.create", payload, idempotency_key=idempotency_key)

    async def complete_run(
        self,
        project_id: str,
        project_token: str,
        run_id: str,
        *,
        success: bool,
        output_artifact_ids: list[str] | None = None,
        summary: str = "",
        stdout: str = "",
        stderr: str = "",
        observed_input_files: list[str] | None = None,
        idempotency_key: str,
    ) -> Any:
        payload = {
            "project_id": project_id,
            "project_token": project_token,
            "run_id": run_id,
            "success": success,
            "output_artifact_ids": output_artifact_ids or [],
            "summary": summary,
            "stdout": stdout,
            "stderr": stderr,
            "observed_input_files": observed_input_files or [],
        }
        return await self.execute_command("agent.run.complete", payload, idempotency_key=idempotency_key)

    async def create_handoff(
        self,
        project_id: str,
        project_token: str,
        task_id: str,
        *,
        objective: str,
        receiver: str | dict[str, Any] | list[dict[str, Any]] = "Team",
        handoff_type: str = "RELAY",
        input_handoff_ids: list[str] | None = None,
        revision_of_handoff_id: str | None = None,
        completed: list[str] | None = None,
        input_artifacts: list[str] | None = None,
        output_artifacts: list[str] | None = None,
        key_conclusions: list[str] | None = None,
        assumptions: list[str] | None = None,
        evidence_refs: list[str] | None = None,
        open_questions: list[str] | None = None,
        risks: list[dict[str, Any]] | None = None,
        next_actions: list[str] | None = None,
        requires_human_approval: bool = True,
        idempotency_key: str,
    ) -> Any:
        payload = {
            "project_id": project_id,
            "project_token": project_token,
            "task_id": task_id,
            "receiver": receiver,
            "handoff_type": handoff_type,
            "input_handoff_ids": input_handoff_ids or [],
            "revision_of_handoff_id": revision_of_handoff_id,
            "objective": objective,
            "completed": completed or [],
            "input_artifacts": input_artifacts or [],
            "output_artifacts": output_artifacts or [],
            "key_conclusions": key_conclusions or [],
            "assumptions": assumptions or [],
            "evidence_refs": evidence_refs or [],
            "open_questions": open_questions or [],
            "risks": risks or [],
            "next_actions": next_actions or [],
            "requires_human_approval": requires_human_approval,
        }
        return await self.execute_command("agent.handoff.create", payload, idempotency_key=idempotency_key)

    async def accept_handoff(self, project_id: str, project_token: str, handoff_id: str, *, idempotency_key: str) -> Any:
        return await self.execute_command(
            "agent.handoff.accept",
            {"project_id": project_id, "project_token": project_token, "handoff_id": handoff_id},
            idempotency_key=idempotency_key,
        )

    async def reject_handoff(
        self,
        project_id: str,
        project_token: str,
        handoff_id: str,
        *,
        reason: str,
        findings: list[dict[str, Any]] | None = None,
        idempotency_key: str,
    ) -> Any:
        return await self.execute_command(
            "agent.handoff.reject",
            {
                "project_id": project_id,
                "project_token": project_token,
                "handoff_id": handoff_id,
                "reason": reason,
                "findings": findings or [],
            },
            idempotency_key=idempotency_key,
        )

    async def submit_review(
        self,
        project_id: str,
        project_token: str,
        *,
        target_type: str,
        target_id: str,
        verdict: str,
        summary: str,
        findings: list[dict[str, Any]] | None = None,
        idempotency_key: str,
    ) -> Any:
        return await self.execute_command(
            "agent.review.submit",
            {
                "project_id": project_id,
                "project_token": project_token,
                "target_type": target_type,
                "target_id": target_id,
                "verdict": verdict,
                "summary": summary,
                "findings": findings or [],
            },
            idempotency_key=idempotency_key,
        )

    def _resolve_command_waiter(self, result: dict[str, Any]) -> None:
        message_ids = list(result.get("acknowledged_message_ids", []))
        if result.get("type") == "error" and result.get("original_message_id"):
            message_ids.append(str(result["original_message_id"]))
        for message_id in message_ids:
            waiter = self._command_waiters.get(str(message_id))
            if waiter is not None and not waiter.done():
                waiter.set_result(result)

    def recover_after_disconnect(self) -> int:
        return self.state.recover_unacked()

    async def run_once(
        self,
        uri: str,
        device_token: str,
        *,
        on_message: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        send_poll_interval: float = 0.25,
        retry_after_seconds: int = 5,
    ) -> None:
        """Run one WebSocket session.

        Reconnect/backoff belongs to the machine service supervisor. This
        method marks frames as SENT before transmission so a process crash can
        recover them on the next session.

        ``on_connected`` fires once the server has confirmed the handshake —
        the machine service uses it to tell "连接就绪" from "正在连接"，任务循环只在
        前者领取任务。
        """
        try:
            import websockets
        except ImportError as error:
            raise RuntimeError("websockets_dependency_required") from error

        self.state.recover_unacked()
        headers = {"Authorization": f"Bearer {device_token}"}
        try:
            connection = websockets.connect(uri, additional_headers=headers)
        except TypeError:
            # Compatibility with older websockets releases.
            connection = websockets.connect(uri, extra_headers=headers)
        async with connection as socket:
            connected = await socket.recv()
            connected_message = self.handle_server_message(_decode_json(connected))
            if connected_message["type"] != "connected":
                raise RuntimeError("gateway_connection_not_confirmed")
            if on_connected is not None:
                await on_connected()
            for envelope in self.pending_envelopes(retry_after_seconds=retry_after_seconds):
                await socket.send(envelope.model_dump_json())

            async def send_loop() -> None:
                while True:
                    await asyncio.sleep(max(0.05, send_poll_interval))
                    for envelope in self.pending_envelopes(retry_after_seconds=retry_after_seconds):
                        await socket.send(envelope.model_dump_json())

            async def receive_loop() -> None:
                async for raw_message in socket:
                    result = self.handle_server_message(_decode_json(raw_message))
                    self._resolve_command_waiter(result)
                    if on_message is not None:
                        await on_message(result)

            sender_task = asyncio.create_task(send_loop())
            receiver_task = asyncio.create_task(receive_loop())
            try:
                done, _ = await asyncio.wait(
                    {sender_task, receiver_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done:
                    task.result()
            finally:
                for task in (sender_task, receiver_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(sender_task, receiver_task, return_exceptions=True)


def _decode_json(raw_message: str | bytes) -> dict[str, Any]:
    import json

    if isinstance(raw_message, bytes):
        raw_message = raw_message.decode("utf-8")
    value = json.loads(raw_message)
    if not isinstance(value, dict):
        raise ValueError("gateway_message_must_be_object")
    return value
