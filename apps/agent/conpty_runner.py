"""ConPTY-backed process supervision for Windows interactive runs.

The dependency is optional at import time.  A request explicitly selecting
CONPTY fails closed when Windows or ``pywinpty`` is unavailable; it never falls
back to ordinary pipes because that would change the terminal semantics.
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4


_SPAWN_LOCK = threading.Lock()
_MIN_CONPTY_BUILD = 17763

try:
    import winpty
except ImportError:  # pragma: no cover - exercised by dependency-free installs.
    winpty = None  # type: ignore[assignment]

try:
    from .local_state import LocalAgentState
    from .machine_service import OutputCallback, ProcessResult, ProcessSpec
except ImportError:  # Support direct script execution on Windows.
    from local_state import LocalAgentState
    from machine_service import OutputCallback, ProcessResult, ProcessSpec


class ConPtyError(RuntimeError):
    """Stable error family for PTY capability and lifecycle failures."""


class ConPtyUnavailable(ConPtyError):
    """Raised when the host cannot provide a real ConPTY backend."""


def conpty_capability() -> dict[str, Any]:
    """Return a diagnostic capability record without creating a process."""

    build = None
    if os.name == "nt":
        try:
            build = int(sys.getwindowsversion().build)
        except (AttributeError, OSError):
            build = None
    available = os.name == "nt" and winpty is not None and build is not None and build >= _MIN_CONPTY_BUILD
    return {
        "os": os.name,
        "platform": platform.platform(),
        "windows_build": build,
        "minimum_windows_build": _MIN_CONPTY_BUILD,
        "available": available,
        "provider": "pywinpty" if winpty is not None else None,
        "provider_version": getattr(winpty, "__version__", None),
        "backend": "ConPTY" if available else None,
    }


@dataclass
class _PtyHandle:
    process_id: str
    spec: ProcessSpec
    process: Any
    completion: asyncio.Task[ProcessResult]
    stop_requested: bool = False
    started_at: float = field(default_factory=time.monotonic)


class ConPtyProcessSupervisor:
    """Supervise pywinpty processes with stdin, merged output and resize."""

    def __init__(
        self,
        state: LocalAgentState,
        stop_timeout_seconds: float = 5.0,
        output_callback: OutputCallback | None = None,
    ) -> None:
        if stop_timeout_seconds <= 0:
            raise ValueError("conpty_stop_timeout_invalid")
        self.state = state
        self.stop_timeout_seconds = stop_timeout_seconds
        self._output_callback = output_callback
        self._handles: dict[str, _PtyHandle] = {}

    def set_output_callback(self, callback: OutputCallback | None) -> None:
        self._output_callback = callback

    def supports_terminal_backend(self, backend: str) -> bool:
        return backend == "CONPTY" and conpty_capability()["available"]

    async def start(self, spec: ProcessSpec) -> _PtyHandle:
        if spec.terminal_backend != "CONPTY":
            raise ConPtyError("conpty_requires_conpty_process_spec")
        if not self.supports_terminal_backend("CONPTY"):
            raise ConPtyUnavailable("conpty_backend_unavailable")
        if self.state.is_emergency_stopped():
            raise PermissionError("local_emergency_stop_active")
        process_id = f"conpty-process-{uuid4().hex}"
        environment = None
        if spec.env is not None or not spec.inherit_environment:
            environment = dict(spec.env or {})
            if spec.inherit_environment:
                environment = {**os.environ, **environment}
        try:
            process = await asyncio.to_thread(
                self._spawn,
                spec,
                environment,
            )
        except Exception as error:
            self.state.register_process(
                process_id,
                spec.run_id,
                list(spec.command),
                spec.cwd,
                None,
                "FAILED",
                project_id=spec.project_id,
                task_id=spec.task_id,
                agent_id=spec.agent_id,
                device_id=spec.device_id,
                workspace_id=spec.workspace_id,
                workspace_path=spec.workspace_path or spec.cwd,
            )
            self.state.update_process(process_id, "FAILED", stderr=str(error))
            self.state.save_run_state(
                spec.run_id,
                "FAILED",
                self._run_payload(spec, process_id=process_id, error=str(error)),
            )
            raise
        self.state.register_process(
            process_id,
            spec.run_id,
            list(spec.command),
            spec.cwd,
            int(getattr(process, "pid", 0) or 0) or None,
            "RUNNING",
            project_id=spec.project_id,
            task_id=spec.task_id,
            agent_id=spec.agent_id,
            device_id=spec.device_id,
            workspace_id=spec.workspace_id,
            workspace_path=spec.workspace_path or spec.cwd,
        )
        self.state.save_run_state(
            spec.run_id,
            "RUNNING",
            self._run_payload(spec, process_id=process_id, pid=getattr(process, "pid", None)),
        )
        completion = asyncio.create_task(self._watch(process_id, spec, process))
        handle = _PtyHandle(process_id, spec, process, completion)
        self._handles[process_id] = handle
        return handle

    async def wait(self, process_id: str) -> ProcessResult:
        handle = self._handles.get(process_id)
        if handle is None:
            raise KeyError("conpty_process_not_found")
        result = await handle.completion
        self._handles.pop(process_id, None)
        return result

    async def request_stop(self, process_id: str) -> None:
        handle = self._handles.get(process_id)
        if handle is None:
            raise KeyError("conpty_process_not_found")
        handle.stop_requested = True
        await self._terminate(handle.process)

    async def stop(self, process_id: str) -> ProcessResult:
        await self.request_stop(process_id)
        return await self.wait(process_id)

    async def write_stdin(self, process_id: str, data: str) -> None:
        handle = self._handles.get(process_id)
        if handle is None:
            raise KeyError("conpty_process_not_found")
        if not isinstance(data, str) or not data:
            raise ValueError("process_stdin_empty")
        if not await asyncio.to_thread(handle.process.isalive):
            raise BrokenPipeError("conpty_process_not_alive")
        await asyncio.to_thread(handle.process.write, data)

    async def resize(self, process_id: str, columns: int, rows: int, pixel_width: int | None = None, pixel_height: int | None = None) -> None:
        del pixel_width, pixel_height
        handle = self._handles.get(process_id)
        if handle is None:
            raise KeyError("conpty_process_not_found")
        if not 1 <= columns <= 1000 or not 1 <= rows <= 1000:
            raise ValueError("conpty_terminal_size_invalid")
        if not await asyncio.to_thread(handle.process.isalive):
            raise BrokenPipeError("conpty_process_not_alive")
        # pywinpty exposes rows, cols while the wire contract uses cols, rows.
        await asyncio.to_thread(handle.process.setwinsize, rows, columns)

    async def stop_all(self) -> list[ProcessResult]:
        handles = list(self._handles.values())
        for handle in handles:
            handle.stop_requested = True
        await asyncio.gather(*(self._terminate(handle.process) for handle in handles), return_exceptions=True)
        results = await asyncio.gather(*(handle.completion for handle in handles), return_exceptions=True)
        completed: list[ProcessResult] = []
        for handle, result in zip(handles, results):
            self._handles.pop(handle.process_id, None)
            if isinstance(result, ProcessResult):
                completed.append(result)
        return completed

    def active_process_ids(self) -> list[str]:
        return [process_id for process_id, handle in self._handles.items() if not handle.completion.done()]

    def operating_system_pid(self, process_id: str) -> int | None:
        """Return the real child PID for OS-scoped observers."""

        handle = self._handles.get(process_id)
        pid = getattr(handle.process, "pid", None) if handle is not None else None
        return int(pid) if pid is not None else None

    def recover_orphan_records(self) -> int:
        return self.state.mark_managed_processes_abandoned()

    def _spawn(self, spec: ProcessSpec, environment: dict[str, str] | None) -> Any:
        assert winpty is not None
        command = list(spec.command)
        executable = shutil.which(command[0], path=os.environ.get("PATH", os.defpath))
        if executable is None:
            raise FileNotFoundError(f"conpty_executable_not_found:{command[0]}")
        command[0] = executable
        # pywinpty uses ``os.environ`` when passed an empty environment.  Keep
        # the child fail-closed by passing a non-empty minimal environment.
        child_environment = dict(environment or {})
        child_environment.setdefault("PATH", os.defpath)
        with _SPAWN_LOCK:
            previous_blocking = os.environ.get("PYWINPTY_BLOCK")
            # Blocking mode is required: pywinpty's non-blocking wrapper
            # reports a false EOF while a child is still alive.
            os.environ["PYWINPTY_BLOCK"] = "1"
            try:
                return winpty.PtyProcess.spawn(
                    command,
                    cwd=spec.cwd,
                    env=child_environment,
                    dimensions=(spec.terminal_rows, spec.terminal_columns),
                    backend=winpty.Backend.ConPTY,
                )
            finally:
                if previous_blocking is None:
                    os.environ.pop("PYWINPTY_BLOCK", None)
                else:
                    os.environ["PYWINPTY_BLOCK"] = previous_blocking

    async def _watch(self, process_id: str, spec: ProcessSpec, process: Any) -> ProcessResult:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()
        stop_reader = threading.Event()

        def read_loop() -> None:
            try:
                while not stop_reader.is_set():
                    chunk = process.read(64 * 1024)
                    if chunk:
                        asyncio.run_coroutine_threadsafe(queue.put(chunk), loop).result()
                    if not process.isalive():
                        break
            except Exception:
                pass
            finally:
                try:
                    asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()
                except Exception:
                    pass

        reader = threading.Thread(target=read_loop, name=f"conpty-reader-{process_id}", daemon=True)
        reader.start()
        captured = bytearray()
        output_budget = spec.max_output_bytes
        timed_out = False
        transport_closed_while_alive = False
        exit_code: int | None = None
        try:
            while True:
                wait_seconds = 0.1
                if spec.timeout_seconds is not None:
                    remaining = spec.timeout_seconds - (time.monotonic() - self._started_at(process_id))
                    if remaining <= 0 and not timed_out:
                        timed_out = True
                        await self._terminate(process)
                    wait_seconds = min(wait_seconds, max(0.01, remaining)) if not timed_out else 0.1
                try:
                    chunk = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
                except asyncio.TimeoutError:
                    if not await asyncio.to_thread(process.isalive) and queue.empty():
                        break
                    continue
                if chunk is None:
                    # pywinpty can close its forwarding socket a little before
                    # the child publishes its exit status.  Do not interpret
                    # that transport EOF as permission to kill a live child.
                    if await asyncio.to_thread(process.isalive):
                        transport_closed_while_alive = True
                        await self._terminate(process)
                    break
                raw = chunk.encode("utf-8", errors="replace") if isinstance(chunk, str) else chunk
                encoded = raw[:output_budget] if output_budget > 0 else b""
                if encoded:
                    captured.extend(encoded)
                    output_budget -= len(encoded)
                    if self._output_callback is not None:
                        try:
                            self._output_callback(spec, "stdout", encoded.decode("utf-8", errors="replace"))
                        except Exception:
                            pass
            if await asyncio.to_thread(process.isalive):
                await self._terminate(process)
            try:
                # Wait once more before reading exitstatus.  On ConPTY a
                # resize can make isalive() turn false slightly before the
                # exit code is published.
                exit_code = await asyncio.to_thread(process.wait)
            except Exception:
                exit_code = getattr(process, "exitstatus", None)
        except asyncio.CancelledError:
            # A cancelled watcher must not leave the ConPTY child behind.
            await self._terminate(process)
            raise
        finally:
            stop_reader.set()
            await asyncio.to_thread(self._close_process_transport, process)
            await asyncio.to_thread(reader.join, min(0.25, self.stop_timeout_seconds))
            internal_reader = getattr(process, "_thread", None)
            if internal_reader is not None:
                await asyncio.to_thread(internal_reader.join, min(0.25, self.stop_timeout_seconds))
            await asyncio.to_thread(reader.join, self.stop_timeout_seconds)
            if internal_reader is not None:
                await asyncio.to_thread(internal_reader.join, self.stop_timeout_seconds)
        handle = self._handles.get(process_id)
        stopped = bool(handle and handle.stop_requested)
        status = (
            "CANCELLED"
            if stopped
            else "TIMED_OUT"
            if timed_out
            else "FAILED"
            if transport_closed_while_alive or exit_code != 0
            else "SUCCEEDED"
        )
        stdout = bytes(captured).decode("utf-8", errors="replace")
        result = ProcessResult(process_id, spec.run_id, status, exit_code, stdout, "")
        self.state.update_process(process_id, status, exit_code=exit_code, stdout=stdout)
        self.state.save_run_state(
            spec.run_id,
            status,
            self._run_payload(
                spec,
                process_id=process_id,
                exit_code=exit_code,
                stdout=stdout,
                transport_error="conpty_output_channel_closed" if transport_closed_while_alive else None,
            ),
        )
        return result

    def _started_at(self, process_id: str) -> float:
        # A monotonic timestamp avoids wall-clock changes affecting timeout.
        handle = self._handles.get(process_id)
        return float(getattr(handle, "started_at", time.monotonic()))

    async def _terminate(self, process: Any) -> None:
        if not await asyncio.to_thread(process.isalive):
            return
        try:
            await asyncio.to_thread(process.terminate)
        except Exception:
            pass
        deadline = time.monotonic() + self.stop_timeout_seconds
        while time.monotonic() < deadline:
            if not await asyncio.to_thread(process.isalive):
                return
            await asyncio.sleep(0.02)
        try:
            await asyncio.to_thread(process.terminate, True)
        except Exception:
            try:
                await asyncio.to_thread(process.kill)
            except Exception:
                pass

    @staticmethod
    def _close_process_transport(process: Any) -> None:
        """Close pywinpty sockets even after ``isalive`` marked it closed."""

        try:
            if not getattr(process, "closed", False):
                process.close()
        except Exception:
            pass
        for name in ("fileobj", "_server"):
            resource = getattr(process, name, None)
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass
        try:
            process.closed = True
            process.fd = -1
        except Exception:
            pass

    @staticmethod
    def _run_payload(spec: ProcessSpec, **extra: Any) -> dict[str, Any]:
        return {
            "process_id": extra.pop("process_id", None),
            "command": list(spec.command),
            "project_id": spec.project_id,
            "task_id": spec.task_id,
            "agent_id": spec.agent_id,
            "device_id": spec.device_id,
            "workspace_id": spec.workspace_id,
            "workspace_path": spec.workspace_path or spec.cwd,
            "terminal_backend": spec.terminal_backend,
            "terminal_columns": spec.terminal_columns,
            "terminal_rows": spec.terminal_rows,
            **extra,
        }


class HybridProcessSupervisor:
    """Route PIPE and CONPTY specs to their exact backend."""

    def __init__(self, pipe: Any, conpty: ConPtyProcessSupervisor | None = None) -> None:
        self.pipe = pipe
        self.conpty = conpty
        self._process_backend: dict[str, Any] = {}

    @property
    def state(self) -> LocalAgentState:
        return self.pipe.state

    def set_output_callback(self, callback: OutputCallback | None) -> None:
        self.pipe.set_output_callback(callback)
        if self.conpty is not None:
            self.conpty.set_output_callback(callback)

    def supports_terminal_backend(self, backend: str) -> bool:
        if backend == "PIPE":
            return self.pipe.supports_terminal_backend(backend)
        return self.conpty is not None and self.conpty.supports_terminal_backend(backend)

    async def start(self, spec: ProcessSpec) -> Any:
        backend = self.conpty if spec.terminal_backend == "CONPTY" else self.pipe
        if backend is None:
            raise ConPtyUnavailable("conpty_backend_unavailable")
        handle = await backend.start(spec)
        self._process_backend[handle.process_id] = backend
        return handle

    async def wait(self, process_id: str) -> ProcessResult:
        backend = self._process_backend.get(process_id)
        if backend is None:
            raise KeyError("managed_process_not_found")
        try:
            return await backend.wait(process_id)
        finally:
            self._process_backend.pop(process_id, None)

    async def request_stop(self, process_id: str) -> None:
        backend = self._process_backend.get(process_id)
        if backend is None:
            raise KeyError("managed_process_not_found")
        await backend.request_stop(process_id)

    async def write_stdin(self, process_id: str, data: str) -> None:
        backend = self._process_backend.get(process_id)
        if backend is None:
            raise KeyError("managed_process_not_found")
        await backend.write_stdin(process_id, data)

    async def resize(self, process_id: str, columns: int, rows: int, pixel_width: int | None = None, pixel_height: int | None = None) -> None:
        backend = self._process_backend.get(process_id)
        if backend is not self.conpty or backend is None:
            raise ConPtyError("terminal_resize_runtime_unavailable")
        await backend.resize(process_id, columns, rows, pixel_width, pixel_height)

    async def stop_all(self) -> list[ProcessResult]:
        results = await self.pipe.stop_all()
        if self.conpty is not None:
            results.extend(await self.conpty.stop_all())
        self._process_backend.clear()
        return results

    def active_process_ids(self) -> list[str]:
        result = list(self.pipe.active_process_ids())
        if self.conpty is not None:
            result.extend(self.conpty.active_process_ids())
        return result

    def operating_system_pid(self, process_id: str) -> int | None:
        backend = self._process_backend.get(process_id)
        if backend is None:
            return None
        resolver = getattr(backend, "operating_system_pid", None)
        if not callable(resolver):
            return None
        pid = resolver(process_id)
        return int(pid) if pid is not None else None

    def recover_orphan_records(self) -> int:
        recovered = self.pipe.recover_orphan_records()
        # A normal deployment shares one LocalAgentState, in which case the
        # first call already covers both backends.  Keep the second state
        # recoverable for tests and embedders that intentionally separate them.
        if self.conpty is not None and self.conpty.state is not self.pipe.state:
            recovered += self.conpty.recover_orphan_records()
        return recovered


__all__ = ["ConPtyError", "ConPtyProcessSupervisor", "ConPtyUnavailable", "HybridProcessSupervisor", "conpty_capability"]
