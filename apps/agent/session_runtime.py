"""Runtime bridge between the session IPC policy and the local Runner.

``SessionWorkerBroker`` remains the authorization boundary.  This module only
turns an authorized request into a ``RunnerRequest``, supervises its lifecycle
on a private asyncio loop, and exposes completed process events for the future
IPC event stream.
"""

from __future__ import annotations

import asyncio
import platform
import threading
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Callable, Mapping
from uuid import uuid4

try:
    from packages.agent_protocol import (
        SessionIpcRequest,
        SessionIpcResponse,
        SessionIpcEvent,
        SessionPeer,
        SessionRunStartPayload,
        TerminalSize,
        SessionLifecycleTransition,
    )
except ModuleNotFoundError:  # Support direct script execution during development.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from packages.agent_protocol import SessionIpcRequest, SessionIpcResponse, SessionIpcEvent, SessionPeer, SessionRunStartPayload, TerminalSize, SessionLifecycleTransition

try:
    from .runner import ExecutionProfile, LocalRunner, RunnerRequest
    from .container_runner import ContainerRunner
    from .machine_service import ProcessResult
    from .session_worker import SessionWorkerBroker, SessionWorkerPolicy
    from .cli_adapters import CliProtocolError, NormalizedCliEvent
except ImportError:  # Support direct script execution during development.
    from runner import ExecutionProfile, LocalRunner, RunnerRequest
    from container_runner import ContainerRunner
    from machine_service import ProcessResult
    from session_worker import SessionWorkerBroker, SessionWorkerPolicy
    from cli_adapters import CliProtocolError, NormalizedCliEvent


@dataclass(frozen=True)
class SessionRuntimeConfig:
    worker_id: str
    user_session_id: str
    user_sid: str
    supported_execution_modes: tuple[str, ...] = ("USER_SESSION",)
    capabilities: tuple[str, ...] = ("session.run",)
    current_os: str | None = None

    def __post_init__(self) -> None:
        if not self.worker_id or not self.user_session_id or not self.user_sid:
            raise ValueError("session_runtime_identity_required")
        if not self.supported_execution_modes:
            raise ValueError("session_runtime_execution_modes_required")


class SessionWorkerRuntime:
    """Execute authorized session requests and retain a local event stream."""

    def __init__(
        self,
        config: SessionRuntimeConfig,
        runner: LocalRunner,
        *,
        broker: SessionWorkerBroker | None = None,
        policy: SessionWorkerPolicy | None = None,
        container_runners: Mapping[str, ContainerRunner] | None = None,
        on_event: Callable[[SessionIpcEvent], None] | None = None,
        on_transition: Callable[[SessionLifecycleTransition], None] | None = None,
        on_run_complete: Callable[[RunnerRequest, ProcessResult], list[str]] | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.container_runners = dict(container_runners or {})
        if broker is None and policy is None and self.container_runners:
            policy = SessionWorkerPolicy(
                allowed_execution_modes=("HEADLESS", "USER_SESSION", "INTERACTIVE_DESKTOP")
            )
        self.broker = broker or SessionWorkerBroker(policy)
        if on_transition is not None:
            self.broker.set_transition_handler(on_transition)
        self.worker = self.broker.register_worker(
            config.worker_id,
            config.user_session_id,
            config.user_sid,
            supported_execution_modes=config.supported_execution_modes,
            capabilities=config.capabilities,
        )
        self._run_futures: dict[str, Future[Any]] = {}
        self._run_backends: dict[str, Any] = {}
        self._run_parsers: dict[str, Any] = {}
        self._parser_terminal_events: dict[str, str] = {}
        self._parser_failures: dict[str, str] = {}
        self._events: deque[SessionIpcEvent] = deque()
        self._event_sequence = 0
        self._lock = threading.Lock()
        self._on_event = on_event
        self._on_run_complete = on_run_complete
        self._closed = False
        self.runner.supervisor.set_output_callback(self._on_process_output)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name=f"session-worker-{config.worker_id}", daemon=True)
        self._thread.start()

    def handle(self, request: SessionIpcRequest, authenticated_peer: SessionPeer) -> SessionIpcResponse:
        """Handle one IPC request synchronously for a Named Pipe Handler."""

        if request.message_type == "session.run.start":
            response = self.broker.handle(request, authenticated_peer)
            if response.status != "ACCEPTED":
                return response
            assert request.context is not None
            if request.context.execution_mode == "INTERACTIVE_DESKTOP":
                self.broker.complete_run(request.worker_id, request.context.run_id)
                failed = self._failed_response(
                    request,
                    "session_interactive_desktop_runtime_unavailable",
                    "ConPTY or desktop executor is not configured",
                )
                self.broker.replace_idempotent_response(request, failed)
                return failed
            try:
                payload = SessionRunStartPayload.model_validate(request.payload)
            except Exception as error:
                assert request.context is not None
                self.broker.complete_run(request.worker_id, request.context.run_id)
                failed = self._failed_response(request, "session_run_payload_invalid", str(error))
                self.broker.replace_idempotent_response(request, failed)
                return failed
            try:
                self._submit_start(request, payload).result(timeout=10)
            except Exception as error:
                assert request.context is not None
                self.broker.complete_run(request.worker_id, request.context.run_id)
                failed = self._failed_response(request, "session_run_start_failed", str(error))
                self.broker.replace_idempotent_response(request, failed)
                return failed
            return response

        if request.message_type == "session.run.stop":
            assert request.context is not None
            response = self.broker.handle(request, authenticated_peer)
            if response.status != "COMPLETED":
                return response
            try:
                self._submit_stop(request.context.run_id).result(timeout=10)
            except Exception as error:
                self.broker.restore_run(request.worker_id, request.context.run_id)
                failed = self._failed_response(request, "session_run_stop_failed", str(error))
                self.broker.replace_idempotent_response(request, failed)
                return failed
            return response

        if request.message_type == "session.terminal.input":
            assert request.context is not None
            response = self.broker.handle(request, authenticated_peer)
            if response.status != "ACCEPTED":
                return response
            try:
                input_value = request.payload.get("input")
                if not isinstance(input_value, str):
                    raise ValueError("session_terminal_input_required")
                self._submit_stdin(request.context.run_id, input_value).result(timeout=10)
            except Exception as error:
                failed = self._failed_response(request, "session_terminal_input_failed", str(error))
                self.broker.replace_idempotent_response(request, failed)
                return failed
            return response

        if request.message_type == "session.terminal.resize":
            assert request.context is not None
            response = self.broker.handle(request, authenticated_peer)
            if response.status != "ACCEPTED":
                return response
            try:
                size = TerminalSize.model_validate(request.payload)
                self._submit_resize(request.context.run_id, size).result(timeout=10)
            except Exception as error:
                failed = self._failed_response(request, "session_terminal_resize_failed", str(error))
                self.broker.replace_idempotent_response(request, failed)
                return failed
            self._emit(
                "terminal.resized",
                request.context.project_id,
                request.context.run_id,
                {"columns": size.columns, "rows": size.rows, "pixel_width": size.pixel_width, "pixel_height": size.pixel_height},
            )
            return response

        if request.message_type == "session.desktop.control":
            response = self.broker.handle(request, authenticated_peer)
            if response.status != "ACCEPTED":
                return response
            failed = self._failed_response(
                request,
                "session_desktop_control_runtime_unavailable",
                "desktop control executor is not configured",
            )
            self.broker.replace_idempotent_response(request, failed)
            return failed

        if request.message_type == "session.worker.shutdown":
            active_run_ids = tuple(self.runner.active_run_ids())
            response = self.broker.handle(request, authenticated_peer)
            if response.status != "COMPLETED":
                return response
            for run_id in active_run_ids:
                try:
                    self._submit_stop(run_id).result(timeout=10)
                except Exception:
                    self._emit("run.failed", run_id, {"error": "session_shutdown_run_stop_failed"})
            self.close()
            return response

        return self.broker.handle(request, authenticated_peer)

    def serve_forever(self, pipe_server: Any, *, stop_event: threading.Event) -> None:
        """Run a configured Named Pipe server with this runtime as its handler."""

        pipe_server.serve_forever(self.handle, stop_event=stop_event)

    def poll_events(self, limit: int = 100) -> list[SessionIpcEvent]:
        if limit < 1:
            raise ValueError("session_event_limit_invalid")
        with self._lock:
            events: list[SessionIpcEvent] = []
            while self._events and len(events) < limit:
                events.append(self._events.popleft())
            return events

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        active_run_ids = self._active_run_ids()
        for run_id in active_run_ids:
            try:
                self._submit_stop(run_id).result(timeout=10)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5)

    def _submit_start(self, request: SessionIpcRequest, payload: SessionRunStartPayload) -> Future[Any]:
        assert request.context is not None
        context = request.context
        profile = ExecutionProfile(
            mode=context.execution_mode,
            requires_user_session=context.execution_mode != "HEADLESS",
            allow_remote_terminal=context.allow_remote_terminal,
            allow_desktop_control=context.allow_desktop_control,
            network_policy=payload.network_policy,
        )
        runner_request = RunnerRequest(
            project_id=context.project_id,
            task_id=context.task_id,
            run_id=context.run_id,
            agent_id=context.agent_id,
            device_id=context.device_id,
            workspace_id=context.workspace_id,
            workspace_path=context.workspace_path,
            command=tuple(payload.command),
            profile=profile,
            environment=payload.environment,
            input_paths=tuple(payload.input_paths),
            output_paths=tuple(payload.output_paths),
            timeout_seconds=payload.timeout_seconds,
            max_output_bytes=payload.max_output_bytes,
            terminal_size=(payload.terminal_size.columns, payload.terminal_size.rows),
            terminal_backend=payload.terminal_backend,
            execution_backend=payload.execution_backend,
            container_runtime=payload.container_runtime,
            container_image=payload.container_image,
            source_commit=payload.source_commit,
            environment_image_digest=payload.environment_image_digest,
            dependency_lock=payload.dependency_lock,
            parameters=payload.parameters,
            random_seed=payload.random_seed,
            model_provider=payload.model_provider,
            model_name=payload.model_name,
            tool_versions=payload.tool_versions,
            data_access_policy=payload.data_access_policy,
            observed_input_files=tuple(payload.observed_input_files),
            observed_network_connections=tuple(payload.observed_network_connections),
        )
        return asyncio.run_coroutine_threadsafe(self._start_and_watch(payload.adapter_id, runner_request), self._loop)

    async def _start_and_watch(self, adapter_id: str, request: RunnerRequest) -> None:
        backend = self._backend_for(request)
        effective_request = request
        invocation_manifest: dict[str, Any] = {}
        if request.execution_backend == "container":
            invocation = backend.build_invocation(request.container_image or "", request)
            effective_request = replace(request, execution_details=invocation.as_manifest())
            handle = await backend.start_invocation(invocation, effective_request)
            invocation_manifest = invocation.as_manifest()
            parser = None
        else:
            parser = self.runner.new_event_parser(adapter_id)
            handle = await backend.start(adapter_id, effective_request)
        self._run_backends[request.run_id] = backend
        if parser is not None:
            self._run_parsers[request.run_id] = parser
        process_payload: dict[str, Any] = {
            "process_id": handle.process_id,
            "command": list(request.command),
            "execution_backend": request.execution_backend,
        }
        if invocation_manifest:
            process_payload["execution"] = invocation_manifest
        self._emit("process.started", request.project_id, request.run_id, process_payload)
        future = asyncio.create_task(self._watch_run(effective_request))
        self._run_futures[request.run_id] = future
        return

    async def _watch_run(self, request: RunnerRequest) -> None:
        try:
            backend = self._run_backends.get(request.run_id, self.runner)
            result = await backend.wait(request.run_id)
            effective_request = request
            observed_input_files = tuple(getattr(result, "observed_input_files", ()))
            observed_network_connections = tuple(getattr(result, "observed_network_connections", ()))
            observation_diagnostics = getattr(result, "access_observation_diagnostics", None)
            if observed_input_files or observed_network_connections or observation_diagnostics:
                effective_request = replace(
                    request,
                    observed_input_files=observed_input_files,
                    observed_network_connections=observed_network_connections,
                    access_observation_diagnostics=observation_diagnostics,
                )
            parser = self._run_parsers.get(request.run_id)
            if parser is not None:
                try:
                    self._emit_cli_events(request, parser.finish())
                except CliProtocolError as error:
                    self._record_parser_failure(request, error)
            self._emit(
                "process.exited",
                request.project_id,
                request.run_id,
                {"process_id": result.process_id, "status": result.status, "exit_code": result.exit_code},
            )
            output_artifact_ids: list[str] = []
            upload_error: str | None = None
            if self._on_run_complete is not None:
                try:
                    output_artifact_ids = [
                        str(value)
                        for value in await asyncio.to_thread(self._on_run_complete, effective_request, result)
                    ]
                except Exception as error:
                    upload_error = str(error)
            parser_failure = self._parser_failures.get(request.run_id)
            success = result.status == "SUCCEEDED" and upload_error is None and parser_failure is None
            completion_payload = {
                "status": result.status,
                "exit_code": result.exit_code,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "summary": "process completed" if upload_error is None else "output upload failed",
                "observed_input_files": list(effective_request.observed_input_files),
                "output_artifact_ids": output_artifact_ids,
                "execution_backend": request.execution_backend,
            }
            if upload_error is not None:
                completion_payload["upload_error"] = upload_error
            terminal_event = self._parser_terminal_events.get(request.run_id)
            expected_terminal = "run.completed" if success else "run.failed"
            self.broker.complete_run(self.config.worker_id, request.run_id)
            if terminal_event != expected_terminal:
                if parser_failure is not None:
                    completion_payload["cli_protocol_error"] = parser_failure
                self._emit(expected_terminal, request.project_id, request.run_id, completion_payload)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._emit("run.failed", request.project_id, request.run_id, {"error": str(error)})
            self.broker.complete_run(self.config.worker_id, request.run_id)
        finally:
            self._run_futures.pop(request.run_id, None)
            self._run_backends.pop(request.run_id, None)
            self._run_parsers.pop(request.run_id, None)
            self._parser_terminal_events.pop(request.run_id, None)
            self._parser_failures.pop(request.run_id, None)

    async def _stop_run(self, run_id: str) -> None:
        backend = self._run_backends.get(run_id, self.runner)
        completion = self._run_futures.get(run_id)
        if completion is not None:
            if run_id in backend.active_run_ids():
                await backend.request_stop(run_id)
            await completion
            return
        await backend.request_stop(run_id)
        await backend.wait(run_id)

    async def _write_stdin(self, run_id: str, data: str) -> None:
        await self._run_backends.get(run_id, self.runner).write_stdin(run_id, data)

    def _submit_stop(self, run_id: str) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(self._stop_run(run_id), self._loop)

    def _submit_stdin(self, run_id: str, data: str) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(self._write_stdin(run_id, data), self._loop)

    async def _resize_terminal(self, run_id: str, size: TerminalSize) -> None:
        await self._run_backends.get(run_id, self.runner).resize_terminal(
            run_id,
            size.columns,
            size.rows,
            pixel_width=size.pixel_width,
            pixel_height=size.pixel_height,
        )

    def _submit_resize(self, run_id: str, size: TerminalSize) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(self._resize_terminal(run_id, size), self._loop)

    def _on_process_output(self, spec: Any, stream_name: str, text: str) -> None:
        event_type = "process.stdout" if stream_name == "stdout" else "process.stderr"
        self._emit(event_type, spec.project_id, spec.run_id, {"text": text})
        if stream_name != "stdout":
            return
        parser = self._run_parsers.get(spec.run_id)
        if parser is None or spec.run_id in self._parser_failures:
            return
        try:
            self._emit_cli_events_for_run(spec.project_id, spec.run_id, parser.feed(text))
        except CliProtocolError as error:
            self._record_parser_failure_for_run(spec.project_id, spec.run_id, error)
            try:
                asyncio.create_task(self.runner.request_stop(spec.run_id))
            except RuntimeError:
                pass

    def _emit_cli_events(self, request: RunnerRequest, events: list[NormalizedCliEvent]) -> None:
        self._emit_cli_events_for_run(request.project_id, request.run_id, events)

    def _emit_cli_events_for_run(self, project_id: str, run_id: str, events: list[NormalizedCliEvent]) -> None:
        for event in events:
            self._emit(event.event_type, project_id, run_id, {"source_type": event.source_type, **event.payload})
            if event.event_type in {"run.completed", "run.failed"}:
                self._parser_terminal_events[run_id] = event.event_type
                if event.event_type == "run.failed":
                    self._parser_failures.setdefault(run_id, "cli_reported_failure")

    def _record_parser_failure(self, request: RunnerRequest, error: CliProtocolError) -> None:
        self._record_parser_failure_for_run(request.project_id, request.run_id, error)

    def _record_parser_failure_for_run(self, project_id: str, run_id: str, error: CliProtocolError) -> None:
        detail = str(error)
        if run_id in self._parser_failures:
            return
        self._parser_failures[run_id] = detail

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def _backend_for(self, request: RunnerRequest) -> Any:
        if request.execution_backend == "host":
            return self.runner
        if request.container_runtime is None:
            raise ValueError("container_runtime_required")
        backend = self.container_runners.get(request.container_runtime)
        if backend is None:
            raise ValueError(f"container_runtime_not_configured:{request.container_runtime}")
        return backend

    def _active_run_ids(self) -> tuple[str, ...]:
        active: set[str] = set(self.runner.active_run_ids())
        for backend in self.container_runners.values():
            active.update(backend.active_run_ids())
        return tuple(sorted(active))

    def _emit(self, event_type: str, project_id: str, run_id: str, payload: dict[str, Any]) -> None:
        with self._lock:
            self._event_sequence += 1
            event = SessionIpcEvent(
                event_id=f"event-{uuid4().hex}",
                worker_id=self.config.worker_id,
                event_type=event_type,
                sequence=self._event_sequence,
                project_id=project_id,
                run_id=run_id,
                occurred_at=datetime.now(UTC),
                payload=payload,
            )
            self._events.append(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                # A durable event sink is best effort here; process supervision
                # and the in-memory event stream remain authoritative.
                pass

    @staticmethod
    def _failed_response(request: SessionIpcRequest, error_code: str, detail: str) -> SessionIpcResponse:
        return SessionIpcResponse(
            request_id=request.request_id,
            idempotency_key=request.idempotency_key,
            worker_id=request.worker_id,
            status="FAILED",
            error_code=error_code,
            responded_at=datetime.now(UTC),
            payload={"detail": detail},
        )


__all__ = ["SessionRuntimeConfig", "SessionWorkerRuntime"]
