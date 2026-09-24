"""Machine-level Agent service primitives.

The service owns reconnect supervision and local safety state. It deliberately
does not implement Windows Service installation, desktop automation, or a
language-specific Runner; those remain separate adapters and later slices.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import UUID, uuid4

try:
    from packages.agent_protocol import AgentHeartbeat
except ModuleNotFoundError:  # Support direct script execution during development.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from packages.agent_protocol import AgentHeartbeat

try:
    from .gateway_client import DurableGatewayClient, GatewayIdentity
    from .local_state import LocalAgentState
except ImportError:  # Support ``python apps/agent/agentd.py`` during development.
    from gateway_client import DurableGatewayClient, GatewayIdentity
    from local_state import LocalAgentState


TokenProvider = Callable[[], str | Awaitable[str]]
HeartbeatProvider = Callable[[], dict[str, Any] | AgentHeartbeat | Awaitable[dict[str, Any] | AgentHeartbeat]]
OutputCallback = Callable[["ProcessSpec", str, str], None]


def _is_uuid(value: Any) -> bool:
    """`running_run_ids` 在协议里是 UUID 列表，非 UUID 的值只能丢弃而不是让整个心跳失败。"""

    try:
        UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True


@dataclass(frozen=True)
class MachineServiceConfig:
    gateway_uri: str
    agent_version: str = "unknown"
    capabilities: tuple[str, ...] = ()
    heartbeat_interval_seconds: float = 15.0
    send_poll_interval_seconds: float = 0.25
    retry_after_seconds: int = 5
    reconnect_base_seconds: float = 1.0
    reconnect_max_seconds: float = 60.0
    control_poll_interval_seconds: float = 0.25
    process_stop_timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.gateway_uri:
            raise ValueError("machine_service_gateway_uri_required")
        if self.heartbeat_interval_seconds <= 0:
            raise ValueError("machine_service_heartbeat_interval_invalid")
        if self.send_poll_interval_seconds <= 0:
            raise ValueError("machine_service_send_poll_interval_invalid")
        if self.retry_after_seconds < 0:
            raise ValueError("machine_service_retry_after_invalid")
        if self.reconnect_base_seconds <= 0 or self.reconnect_max_seconds <= 0:
            raise ValueError("machine_service_backoff_invalid")
        if self.reconnect_base_seconds > self.reconnect_max_seconds:
            raise ValueError("machine_service_backoff_range_invalid")
        if self.control_poll_interval_seconds <= 0:
            raise ValueError("machine_service_control_poll_invalid")


@dataclass(frozen=True)
class ProcessSpec:
    run_id: str
    command: tuple[str, ...]
    project_id: str
    task_id: str | None
    agent_id: str
    device_id: str
    workspace_id: str
    workspace_path: str
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    inherit_environment: bool = False
    timeout_seconds: float | None = None
    max_output_bytes: int = 1_000_000
    stdin_enabled: bool = False
    terminal_columns: int = 80
    terminal_rows: int = 24
    terminal_backend: str = "PIPE"

    def __post_init__(self) -> None:
        if not self.run_id:
            raise ValueError("process_run_id_required")
        for field_name in ("project_id", "agent_id", "device_id", "workspace_id", "workspace_path"):
            if not isinstance(getattr(self, field_name), str) or not getattr(self, field_name).strip():
                raise ValueError(f"process_{field_name}_required")
        if self.task_id is not None and (not isinstance(self.task_id, str) or not self.task_id.strip()):
            raise ValueError("process_task_id_invalid")
        if not self.command or not all(isinstance(part, str) and part for part in self.command):
            raise ValueError("process_command_required")
        if not isinstance(self.inherit_environment, bool):
            raise ValueError("process_inherit_environment_invalid")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("process_timeout_invalid")
        if self.max_output_bytes < 1:
            raise ValueError("process_output_limit_invalid")
        if not isinstance(self.stdin_enabled, bool):
            raise ValueError("process_stdin_enabled_invalid")
        if self.terminal_backend not in {"PIPE", "CONPTY"}:
            raise ValueError("process_terminal_backend_invalid")
        if not isinstance(self.terminal_columns, int) or isinstance(self.terminal_columns, bool) or not 1 <= self.terminal_columns <= 1000:
            raise ValueError("process_terminal_columns_invalid")
        if not isinstance(self.terminal_rows, int) or isinstance(self.terminal_rows, bool) or not 1 <= self.terminal_rows <= 1000:
            raise ValueError("process_terminal_rows_invalid")


@dataclass(frozen=True)
class ProcessResult:
    process_id: str
    run_id: str
    status: str
    exit_code: int | None
    stdout: str
    stderr: str
    observed_input_files: tuple[str, ...] = ()
    observed_network_connections: tuple[Mapping[str, object], ...] = ()
    access_observation_status: str | None = None
    access_observation_reason: str | None = None
    access_observation_diagnostics: Mapping[str, object] | None = None


@dataclass
class _ProcessHandle:
    process_id: str
    spec: ProcessSpec
    process: asyncio.subprocess.Process
    completion: asyncio.Task[ProcessResult]
    stop_requested: bool = False


class LocalProcessSupervisor:
    """Start and supervise child processes without invoking a shell."""

    def __init__(
        self,
        state: LocalAgentState,
        stop_timeout_seconds: float = 5.0,
        output_callback: OutputCallback | None = None,
    ) -> None:
        if stop_timeout_seconds <= 0:
            raise ValueError("process_stop_timeout_invalid")
        self.state = state
        self.stop_timeout_seconds = stop_timeout_seconds
        self._handles: dict[str, _ProcessHandle] = {}
        self._output_callback = output_callback

    def set_output_callback(self, callback: OutputCallback | None) -> None:
        self._output_callback = callback

    def supports_terminal_backend(self, backend: str) -> bool:
        return backend == "PIPE"

    async def start(self, spec: ProcessSpec) -> _ProcessHandle:
        if spec.terminal_backend != "PIPE":
            raise PermissionError("conpty_backend_unavailable")
        if self.state.is_emergency_stopped():
            raise PermissionError("local_emergency_stop_active")
        process_id = f"process-{uuid4().hex}"
        environment = None
        if spec.env is not None or not spec.inherit_environment:
            environment = dict(spec.env or {})
            if spec.inherit_environment:
                environment = {**os.environ, **environment}
        try:
            process = await asyncio.create_subprocess_exec(
                *spec.command,
                cwd=spec.cwd,
                env=environment,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # 不允许交互时给 /dev/null（即 EOF），**不是继承父进程的 stdin**：
                # 继承会让"我们没打算交互"的执行体读到守护进程的输入流并永远等下去
                # （实测：`opencode run` 在 stdin 是打开着的管道时会挂住不返回，见 CLOUD-1 交接）。
                stdin=asyncio.subprocess.PIPE if spec.stdin_enabled else asyncio.subprocess.DEVNULL,
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
            self.state.update_process(
                process_id,
                "FAILED",
                stderr=str(error),
            )
            self.state.save_run_state(
                spec.run_id,
                "FAILED",
                {
                    "process_id": process_id,
                    "command": list(spec.command),
                    "error": str(error),
                    "project_id": spec.project_id,
                    "task_id": spec.task_id,
                    "agent_id": spec.agent_id,
                    "device_id": spec.device_id,
                    "workspace_id": spec.workspace_id,
                    "workspace_path": spec.workspace_path or spec.cwd,
                    "terminal_backend": spec.terminal_backend,
                    "terminal_columns": spec.terminal_columns,
                    "terminal_rows": spec.terminal_rows,
                },
            )
            raise
        self.state.register_process(
            process_id,
            spec.run_id,
            list(spec.command),
            spec.cwd,
            process.pid,
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
            {
                "process_id": process_id,
                "command": list(spec.command),
                "pid": process.pid,
                "project_id": spec.project_id,
                "task_id": spec.task_id,
                "agent_id": spec.agent_id,
                "device_id": spec.device_id,
                "workspace_id": spec.workspace_id,
                "workspace_path": spec.workspace_path or spec.cwd,
                "terminal_backend": spec.terminal_backend,
                "terminal_columns": spec.terminal_columns,
                "terminal_rows": spec.terminal_rows,
            },
        )
        completion = asyncio.create_task(self._watch(process_id, spec, process))
        handle = _ProcessHandle(process_id, spec, process, completion)
        self._handles[process_id] = handle
        return handle

    async def wait(self, process_id: str) -> ProcessResult:
        handle = self._handles.get(process_id)
        if handle is None:
            raise KeyError("managed_process_not_found")
        result = await handle.completion
        self._handles.pop(process_id, None)
        return result

    async def stop(self, process_id: str) -> ProcessResult:
        handle = self._handles.get(process_id)
        if handle is None:
            raise KeyError("managed_process_not_found")
        await self.request_stop(process_id)
        return await self.wait(process_id)

    async def request_stop(self, process_id: str) -> None:
        """Request termination without consuming the completion result."""

        handle = self._handles.get(process_id)
        if handle is None:
            raise KeyError("managed_process_not_found")
        handle.stop_requested = True
        await self._terminate(handle.process)

    async def write_stdin(self, process_id: str, data: str) -> None:
        handle = self._handles.get(process_id)
        if handle is None:
            raise KeyError("managed_process_not_found")
        if handle.process.stdin is None:
            raise PermissionError("process_stdin_not_enabled")
        encoded = data.encode("utf-8")
        if not encoded:
            raise ValueError("process_stdin_empty")
        handle.process.stdin.write(encoded)
        await handle.process.stdin.drain()

    async def stop_all(self) -> list[ProcessResult]:
        handles = list(self._handles.values())
        for handle in handles:
            handle.stop_requested = True
        await asyncio.gather(*(self._terminate(handle.process) for handle in handles), return_exceptions=True)
        if not handles:
            return []
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
        return int(handle.process.pid) if handle is not None and handle.process.pid is not None else None

    def recover_orphan_records(self) -> int:
        """Mark processes from a previous service instance as unknown/abandoned."""
        return self.state.mark_managed_processes_abandoned()

    async def _watch(
        self,
        process_id: str,
        spec: ProcessSpec,
        process: asyncio.subprocess.Process,
    ) -> ProcessResult:
        assert process.stdout is not None
        assert process.stderr is not None
        output_budget = [spec.max_output_bytes]
        stdout_task = asyncio.create_task(self._read_limited(process.stdout, output_budget, spec, "stdout"))
        stderr_task = asyncio.create_task(self._read_limited(process.stderr, output_budget, spec, "stderr"))
        timed_out = False
        try:
            if spec.timeout_seconds is None:
                exit_code = await process.wait()
            else:
                exit_code = await asyncio.wait_for(process.wait(), timeout=spec.timeout_seconds)
        except asyncio.TimeoutError:
            timed_out = True
            await self._terminate(process)
            exit_code = process.returncode
        except asyncio.CancelledError:
            await self._terminate(process)
            stdout_task.cancel()
            stderr_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            raise
        stdout, stderr = await asyncio.gather(stdout_task, stderr_task)
        handle = self._handles.get(process_id)
        stopped = bool(handle and handle.stop_requested)
        if stopped:
            status = "CANCELLED"
        elif timed_out:
            status = "TIMED_OUT"
        else:
            status = "SUCCEEDED" if exit_code == 0 else "FAILED"
        result = ProcessResult(process_id, spec.run_id, status, exit_code, stdout, stderr)
        self.state.update_process(process_id, status, exit_code=exit_code, stdout=stdout, stderr=stderr)
        self.state.save_run_state(
            spec.run_id,
            status,
            {
                "process_id": process_id,
                "command": list(spec.command),
                "exit_code": exit_code,
                "stdout": stdout,
                "stderr": stderr,
                "project_id": spec.project_id,
                "task_id": spec.task_id,
                "agent_id": spec.agent_id,
                "device_id": spec.device_id,
                "workspace_id": spec.workspace_id,
                "workspace_path": spec.workspace_path or spec.cwd,
                "terminal_backend": spec.terminal_backend,
                "terminal_columns": spec.terminal_columns,
                "terminal_rows": spec.terminal_rows,
            },
        )
        return result

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=self.stop_timeout_seconds)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _read_limited(
        self,
        stream: asyncio.StreamReader,
        output_budget: list[int],
        spec: ProcessSpec,
        stream_name: str,
    ) -> str:
        captured = bytearray()
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            accepted = b""
            if output_budget[0] > 0:
                accepted = chunk[: output_budget[0]]
                captured.extend(accepted)
                output_budget[0] -= len(accepted)
            if accepted and self._output_callback is not None:
                try:
                    self._output_callback(spec, stream_name, accepted.decode("utf-8", errors="replace"))
                except Exception:
                    # Telemetry must not change the child process outcome.
                    pass
        return bytes(captured).decode("utf-8", errors="replace")


class MachineAgentService:
    """Reconnect supervisor and local safety boundary for one device Agent."""

    def __init__(
        self,
        client: DurableGatewayClient,
        token_provider: TokenProvider,
        config: MachineServiceConfig,
        *,
        heartbeat_provider: HeartbeatProvider | None = None,
        process_supervisor: LocalProcessSupervisor | None = None,
        task_loop: Any | None = None,
        paused_provider: Callable[[], bool] | None = None,
        gateway_uri_provider: Callable[[str], str] | None = None,
        connection_id_provider: Callable[[], str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.client = client
        self.token_provider = token_provider
        self.config = config
        self.heartbeat_provider = heartbeat_provider or self._default_heartbeat
        self.process_supervisor = process_supervisor or LocalProcessSupervisor(
            client.state,
            config.process_stop_timeout_seconds,
        )
        # 任务循环是可选注入的（dack-typed：需要 run_forever(stop=..., gate=...)）：
        # 只做连接监督的部署（例如服务账号下的老路径）不需要它。
        self.task_loop = task_loop
        self._task_loop_task: asyncio.Task[None] | None = None
        self._paused_provider = paused_provider or (lambda: False)
        # 每次连接用新的 connection_id（平台侧主键）+ 与之匹配的 URI；
        # 不给 provider 时沿用固定 URI（老路径，单次会话）。
        self._gateway_uri_provider = gateway_uri_provider
        self._connection_id_provider = connection_id_provider
        self._active_uri: str | None = None
        self._last_renumber: dict[str, int] | None = None
        self._sleep = sleep
        self._stop_event = asyncio.Event()
        self._connected = asyncio.Event()
        self._connection_task: asyncio.Task[None] | None = None
        self._status = "STOPPED"
        self._last_error: str | None = None
        self._reconnect_attempt = 0
        self._running_run_ids: set[str] = set()

    @property
    def status(self) -> str:
        return self._status

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def connected(self) -> bool:
        """平台连接是否已就绪（握手确认过）。任务循环用它当闸门。"""

        return self._connected.is_set()

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "last_error": self._last_error,
            "reconnect_attempt": self._reconnect_attempt,
            "connected": self.connected,
            "connection_id": self.client.identity.connection_id,
            "sequence_renumber": self._last_renumber,
            "active_process_ids": self.process_supervisor.active_process_ids(),
            "emergency_stop": self.client.state.emergency_stop_state(),
            "task_loop": self.task_loop.snapshot() if self.task_loop is not None and hasattr(self.task_loop, "snapshot") else None,
        }

    def set_running_run_ids(self, run_ids: Sequence[str]) -> None:
        self._running_run_ids = {str(run_id) for run_id in run_ids}

    async def run_forever(self) -> str:
        if self.client.state.is_emergency_stopped():
            self._status = "EMERGENCY_STOPPED"
            return self._status
        self._stop_event.clear()
        self._connected.clear()
        # 本进程刚起来：上一进程留下的"在跑的 Run"不再成立，别把它报成还在跑
        self._running_run_ids = set()
        self._last_error = None
        self._reconnect_attempt = 0
        self.process_supervisor.recover_orphan_records()
        control_task = asyncio.create_task(self._control_loop())
        self._task_loop_task = asyncio.create_task(self._task_loop_supervisor())
        task_loop_task = self._task_loop_task
        try:
            while not self._stop_event.is_set():
                if self.client.state.is_emergency_stopped():
                    self._status = "EMERGENCY_STOPPED"
                    break
                self._status = "CONNECTING"
                heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                try:
                    token = await self._resolve_token()
                    self._rotate_connection_identity()
                    self._connection_task = asyncio.create_task(
                        self.client.run_once(
                            self._active_uri or self.config.gateway_uri,
                            token,
                            on_connected=self._mark_connected,
                            send_poll_interval=self.config.send_poll_interval_seconds,
                            retry_after_seconds=self.config.retry_after_seconds,
                        )
                    )
                    await self._connection_task
                    if not self._stop_event.is_set():
                        self._status = "DISCONNECTED"
                except asyncio.CancelledError:
                    if not self._stop_event.is_set():
                        raise
                except Exception as error:  # Reconnect is the service's recovery boundary.
                    self._last_error = str(error)
                    self._status = "RECONNECT_WAIT"
                finally:
                    self._connection_task = None
                    # 断链后闸门立即关闭：任务循环不再领取，直到下一次握手成功。
                    self._connected.clear()
                    heartbeat_task.cancel()
                    await asyncio.gather(heartbeat_task, return_exceptions=True)
                if self._stop_event.is_set():
                    break
                self._reconnect_attempt += 1
                self._status = "RECONNECT_WAIT"
                await self._wait_or_stop(
                    self.backoff_delay(
                        self._reconnect_attempt,
                        base=self.config.reconnect_base_seconds,
                        maximum=self.config.reconnect_max_seconds,
                    )
                )
        finally:
            control_task.cancel()
            task_loop_task.cancel()
            if self._task_loop_task is not None and self._task_loop_task is not task_loop_task:
                self._task_loop_task.cancel()
            await asyncio.gather(
                control_task,
                task_loop_task,
                *( [self._task_loop_task] if self._task_loop_task is not None and self._task_loop_task is not task_loop_task else [] ),
                return_exceptions=True,
            )
            await self.process_supervisor.stop_all()
            if self.client.state.is_emergency_stopped():
                self._status = "EMERGENCY_STOPPED"
            elif self._status != "EMERGENCY_STOPPED":
                self._status = "STOPPED"
        return self._status

    async def _mark_connected(self) -> None:
        self._status = "CONNECTED"
        self._connected.set()

    def _rotate_connection_identity(self) -> None:
        """每次连接换一个 connection_id，并把本地 outbox 的序列对齐到新连接。

        两件事必须一起做：平台侧 `connection_id` 是主键（复用会撞唯一约束），且
        序列号是按连接计的（新连接要求从 1 开始）。只换 id 不重编号，就会出现
        "平台要求重放、重放又对不上"的死循环。
        """

        if self._connection_id_provider is None:
            return
        connection_id = self._connection_id_provider()
        identity = self.client.identity
        self.client.identity = GatewayIdentity(
            device_id=identity.device_id,
            agent_id=identity.agent_id,
            session_id=identity.session_id,
            connection_id=connection_id,
        )
        self._active_uri = self._gateway_uri_provider(connection_id) if self._gateway_uri_provider else None
        renumber = getattr(self.client.state, "renumber_pending_events", None)
        if callable(renumber):
            self._last_renumber = renumber()

    async def _task_loop_supervisor(self) -> None:
        """驱动任务循环：连接到平台后才领取，断开/暂停时只等待（循环本身不退出）。

        只负责"当前这一个"循环；`install_task_loop` 会在授权变更时替换并重启它。
        """

        loop = self.task_loop
        if loop is None:
            return
        try:
            await loop.run_forever(stop=self._stop_event, gate=self._may_claim)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 任务循环崩了不能带走常驻体
            self._last_error = f"task_loop_failed:{type(error).__name__}:{error}"

    async def install_task_loop(self, task_loop: Any) -> None:
        """运行期安装/替换任务循环（设备刚拿到项目授权时用，不必重启内核）。"""

        self.task_loop = task_loop
        current = self._task_loop_task
        if current is not None and not current.done():
            current.cancel()
            await asyncio.gather(current, return_exceptions=True)
        if not self._stop_event.is_set():
            self._task_loop_task = asyncio.create_task(self._task_loop_supervisor())

    async def _may_claim(self) -> bool:
        """领取闸门：连接就绪 + 未被暂停 + 无紧急停止。"""

        return self.connected and not self._paused_provider() and not self.client.state.is_emergency_stopped()

    async def stop(self) -> None:
        self._stop_event.set()
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
        await self.process_supervisor.stop_all()

    async def emergency_stop(self, reason: str = "local_emergency_stop") -> None:
        self.client.state.set_emergency_stop(reason)
        await self.stop()
        self._status = "EMERGENCY_STOPPED"

    def clear_emergency_stop(self) -> None:
        self.client.state.clear_emergency_stop()
        if self._status == "EMERGENCY_STOPPED":
            self._status = "STOPPED"

    @staticmethod
    def backoff_delay(attempt: int, base: float = 1.0, maximum: float = 60.0) -> float:
        if attempt < 1:
            raise ValueError("machine_service_backoff_attempt_invalid")
        if base <= 0 or maximum <= 0 or base > maximum:
            raise ValueError("machine_service_backoff_range_invalid")
        return min(maximum, base * (2 ** (attempt - 1)))

    async def _resolve_token(self) -> str:
        token = self.token_provider()
        if inspect.isawaitable(token):
            token = await token
        if not isinstance(token, str) or not token:
            raise ValueError("machine_service_device_token_required")
        return token

    async def _heartbeat_loop(self) -> None:
        await self._queue_heartbeat()
        while not self._stop_event.is_set():
            await self._sleep(self.config.heartbeat_interval_seconds)
            if self._stop_event.is_set() or self.client.state.is_emergency_stopped():
                return
            await self._queue_heartbeat()

    async def _queue_heartbeat(self) -> None:
        payload = self.heartbeat_provider()
        if inspect.isawaitable(payload):
            payload = await payload
        if isinstance(payload, AgentHeartbeat):
            encoded = payload.model_dump(mode="json")
        elif isinstance(payload, dict):
            encoded = payload
        else:
            raise TypeError("machine_service_heartbeat_payload_invalid")
        self.client.queue_event(
            "agent.heartbeat",
            encoded,
            idempotency_key=f"heartbeat:{self.client.identity.connection_id}:{uuid4().hex}",
        )

    async def _control_loop(self) -> None:
        while not self._stop_event.is_set():
            if self.client.state.is_emergency_stopped():
                self._status = "EMERGENCY_STOPPED"
                self._stop_event.set()
                if self._connection_task and not self._connection_task.done():
                    self._connection_task.cancel()
                await self.process_supervisor.stop_all()
                return
            await self._sleep(self.config.control_poll_interval_seconds)

    async def _wait_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return

    def _default_heartbeat(self) -> AgentHeartbeat:
        loop_snapshot = self.task_loop.snapshot() if self.task_loop is not None and hasattr(self.task_loop, "snapshot") else {}
        running_run_ids = list(loop_snapshot.get("running_run_ids") or self._running_run_ids)
        local_queue_length = int(loop_snapshot.get("local_queue_length") or 0)
        return AgentHeartbeat(
            device_id=self.client.identity.device_id,
            agent_id=self.client.identity.agent_id,
            session_id=self.client.identity.session_id,
            connection_id=self.client.identity.connection_id,
            agent_version=self.config.agent_version,
            capabilities=list(self.config.capabilities),
            running_run_ids=[run_id for run_id in running_run_ids if _is_uuid(run_id)],
            local_queue_length=local_queue_length,
            user_session_state="unknown",
            resource_summary={
                "active_processes": len(self.process_supervisor.active_process_ids()),
                "pending_gateway_events": len(self.client.state.pending_events(limit=1000)),
            },
            sent_at=datetime.now(UTC),
        )


__all__ = [
    "LocalProcessSupervisor",
    "MachineAgentService",
    "MachineServiceConfig",
    "ProcessResult",
    "ProcessSpec",
]
