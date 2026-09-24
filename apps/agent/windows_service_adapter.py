"""Windows Service and Session 0 adapters for the local Agent.

The module keeps policy in ``MachineServiceLifecycle`` and isolates Win32
effects behind small replaceable interfaces.  Tests can therefore exercise
service/session orchestration without installing a Windows service or starting
an interactive process on the development machine.
"""

from __future__ import annotations

import ctypes
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence, Literal

from ctypes import wintypes

try:
    from .service_lifecycle import LifecycleAction, LifecycleResult, MachineServiceLifecycle
    from packages.agent_protocol import SessionIpcRequest, SessionLifecycleTransition, SessionPeer
except ImportError:  # Support direct script execution on Windows.
    from service_lifecycle import LifecycleAction, LifecycleResult, MachineServiceLifecycle
    from packages.agent_protocol import SessionIpcRequest, SessionLifecycleTransition, SessionPeer


class WindowsServiceError(RuntimeError):
    """Stable error family for service and Session 0 adapter failures."""


class WindowsServiceUnavailable(WindowsServiceError):
    """Raised when a Windows-only adapter is used on another operating system."""


ServiceStartType = Literal["auto", "demand", "disabled"]
ServiceStatus = Literal["STOPPED", "START_PENDING", "STOP_PENDING", "RUNNING", "UNKNOWN"]
SessionState = Literal["logged_in", "locked", "logged_out"]
SessionEvent = Literal[
    "session.logged_in",
    "session.locked",
    "session.unlocked",
    "session.logged_out",
]


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsServiceUnavailable("windows_service_requires_windows")


def _win_error(operation: str) -> WindowsServiceError:
    error = ctypes.get_last_error()
    return WindowsServiceError(f"{operation}:win32_error:{error}")


def _quote_command(command: Sequence[str]) -> str:
    if not command or any(not isinstance(part, str) or not part for part in command):
        raise ValueError("windows_service_command_required")
    return subprocess.list2cmdline(list(command))


@dataclass(frozen=True)
class ServiceInstallSpec:
    service_name: str
    display_name: str
    command: tuple[str, ...]
    description: str = "Math Agent Platform machine service"
    start_type: ServiceStartType = "auto"
    service_account: str = "LocalSystem"
    dependencies: tuple[str, ...] = ()
    failure_recovery: "FailureRecoveryPolicy" = field(default_factory=lambda: FailureRecoveryPolicy())

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{2,256}", self.service_name):
            raise ValueError("windows_service_name_invalid")
        if not self.display_name.strip():
            raise ValueError("windows_service_display_name_required")
        if not self.command:
            raise ValueError("windows_service_command_required")
        if self.start_type not in {"auto", "demand", "disabled"}:
            raise ValueError("windows_service_start_type_invalid")
        if not self.service_account.strip():
            raise ValueError("windows_service_account_required")
        if any(not value.strip() for value in self.dependencies):
            raise ValueError("windows_service_dependency_invalid")

    @property
    def binary_path(self) -> str:
        return _quote_command(self.command)


@dataclass(frozen=True)
class FailureRecoveryPolicy:
    """SCM restart policy for unexpected service termination."""

    restart_delays_seconds: tuple[int, ...] = (60, 300, 900)
    reset_period_seconds: int = 86_400

    def __post_init__(self) -> None:
        if not self.restart_delays_seconds or any(delay < 0 for delay in self.restart_delays_seconds):
            raise ValueError("service_failure_restart_delays_invalid")
        if self.reset_period_seconds < 0:
            raise ValueError("service_failure_reset_period_invalid")


@dataclass(frozen=True)
class SessionSnapshot:
    """A user session observed by the Session 0 service."""

    session_id: str
    windows_session_id: int
    user_sid: str
    state: SessionState
    username: str | None = None

    def __post_init__(self) -> None:
        if len(self.session_id) < 2 or not isinstance(self.windows_session_id, int) or self.windows_session_id < 0:
            raise ValueError("session_snapshot_identity_invalid")
        if not re.fullmatch(r"S-\d-\d+(?:-\d+)+", self.user_sid, re.IGNORECASE):
            raise ValueError("session_snapshot_sid_invalid")
        if self.state not in {"logged_in", "locked", "logged_out"}:
            raise ValueError("session_snapshot_state_invalid")


@dataclass(frozen=True)
class WorkerLaunchRequest:
    worker_id: str
    session: SessionSnapshot
    command: tuple[str, ...]
    cwd: str
    pipe_name: str
    environment: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.worker_id) < 2 or not self.command:
            raise ValueError("worker_launch_identity_or_command_invalid")
        if not Path(self.cwd).is_absolute():
            raise ValueError("worker_launch_cwd_must_be_absolute")
        if not self.pipe_name.startswith("\\\\.\\pipe\\"):
            raise ValueError("worker_launch_pipe_name_invalid")
        if any(not isinstance(key, str) or not key or "=" in key for key in self.environment):
            raise ValueError("worker_launch_environment_key_invalid")
        if any(not isinstance(value, str) for value in self.environment.values()):
            raise ValueError("worker_launch_environment_value_invalid")


@dataclass(frozen=True)
class WorkerHandle:
    worker_id: str
    session_id: str
    windows_session_id: int
    process_id: int
    pipe_name: str
    process_handle: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if len(self.worker_id) < 2 or len(self.session_id) < 2 or self.process_id < 1:
            raise ValueError("worker_handle_invalid")


@dataclass(frozen=True)
class WorkerLaunchAudit:
    """Immutable binding between a Worker process and its user session."""

    worker_id: str
    process_id: int
    session_id: str
    windows_session_id: int
    user_sid: str
    pipe_name: str
    command: tuple[str, ...]
    cwd: str
    occurred_at: str

    @classmethod
    def from_request(cls, request: WorkerLaunchRequest, handle: WorkerHandle) -> "WorkerLaunchAudit":
        return cls(
            worker_id=request.worker_id,
            process_id=handle.process_id,
            session_id=request.session.session_id,
            windows_session_id=request.session.windows_session_id,
            user_sid=request.session.user_sid,
            pipe_name=request.pipe_name,
            command=request.command,
            cwd=request.cwd,
            occurred_at=datetime.now(UTC).isoformat(),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "process_id": self.process_id,
            "session_id": self.session_id,
            "windows_session_id": self.windows_session_id,
            "user_sid": self.user_sid,
            "pipe_name": self.pipe_name,
            "command": list(self.command),
            "cwd": self.cwd,
            "occurred_at": self.occurred_at,
        }


class ServiceControlBackend(Protocol):
    def install(self, spec: ServiceInstallSpec) -> None: ...

    def update(self, spec: ServiceInstallSpec) -> None: ...

    def uninstall(self, service_name: str) -> None: ...

    def start(self, service_name: str) -> None: ...

    def stop(self, service_name: str, timeout_seconds: float = 10.0) -> None: ...

    def status(self, service_name: str) -> ServiceStatus: ...

    def configure_recovery(self, service_name: str, policy: FailureRecoveryPolicy) -> None: ...


class SessionProcessBackend(Protocol):
    def enumerate_sessions(self) -> list[SessionSnapshot]: ...

    def launch_worker(self, request: WorkerLaunchRequest) -> WorkerHandle: ...

    def stop_worker(self, handle: WorkerHandle, timeout_seconds: float = 5.0) -> None: ...

    def worker_is_running(self, handle: WorkerHandle) -> bool: ...

    def release_worker_handle(self, handle: WorkerHandle) -> None: ...

    def wait_worker_ready(
        self,
        handle: WorkerHandle,
        *,
        peer_id: str,
        process_id: int | None,
        windows_session_id: int,
        timeout_seconds: float = 10.0,
    ) -> None: ...


class WindowsServiceInstaller:
    """Install or update a service and apply its recovery policy."""

    def __init__(self, backend: ServiceControlBackend) -> None:
        self.backend = backend

    def install_or_update(self, spec: ServiceInstallSpec) -> Literal["INSTALLED", "UPDATED"]:
        try:
            self.backend.install(spec)
            result: Literal["INSTALLED", "UPDATED"] = "INSTALLED"
        except WindowsServiceError as error:
            if str(error) != "service_already_exists":
                raise
            self.backend.update(spec)
            result = "UPDATED"
        self.backend.configure_recovery(spec.service_name, spec.failure_recovery)
        return result


class CtypesServiceControlBackend:
    """Small SCM wrapper; no shell or ``sc.exe`` parsing is involved."""

    _SC_MANAGER_ALL_ACCESS = 0xF003F
    _SERVICE_ALL_ACCESS = 0xF01FF
    _SERVICE_WIN32_OWN_PROCESS = 0x00000010
    _SERVICE_AUTO_START = 0x00000002
    _SERVICE_DEMAND_START = 0x00000003
    _SERVICE_DISABLED = 0x00000004
    _SERVICE_ERROR_NORMAL = 0x00000001
    _SERVICE_STOP = 0x0020
    _SERVICE_QUERY_STATUS = 0x0004
    _SERVICE_START = 0x0010
    _SC_STATUS_PROCESS_INFO = 0
    _SC_MANAGER_CONNECT = 0x0001
    _SERVICE_CONTROL_STOP = 0x00000001
    _ERROR_SERVICE_EXISTS = 1073
    _ERROR_SERVICE_NOT_FOUND = 1060
    _ERROR_SERVICE_NOT_ACTIVE = 1062
    _SERVICE_STOPPED = 0x00000001
    _SERVICE_START_PENDING = 0x00000002
    _SERVICE_STOP_PENDING = 0x00000003
    _SERVICE_RUNNING = 0x00000004
    _SERVICE_CONFIG_FAILURE_ACTIONS = 2
    _SERVICE_CONFIG_DESCRIPTION = 1
    _SC_ACTION_RESTART = 1

    def __init__(self) -> None:
        _require_windows()
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_api()

    def _configure_api(self) -> None:
        w = wintypes
        self._advapi32.OpenSCManagerW.argtypes = [w.LPCWSTR, w.LPCWSTR, w.DWORD]
        self._advapi32.OpenSCManagerW.restype = w.HANDLE
        self._advapi32.CloseServiceHandle.argtypes = [w.HANDLE]
        self._advapi32.CloseServiceHandle.restype = w.BOOL
        self._advapi32.CreateServiceW.argtypes = [
            w.HANDLE, w.LPCWSTR, w.LPCWSTR, w.DWORD, w.DWORD, w.DWORD, w.DWORD,
            w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(w.DWORD), w.LPCWSTR, w.LPCWSTR, w.LPCWSTR,
        ]
        self._advapi32.CreateServiceW.restype = w.HANDLE
        self._advapi32.OpenServiceW.argtypes = [w.HANDLE, w.LPCWSTR, w.DWORD]
        self._advapi32.OpenServiceW.restype = w.HANDLE
        self._advapi32.ChangeServiceConfigW.argtypes = [
            w.HANDLE, w.DWORD, w.DWORD, w.DWORD, w.LPCWSTR, w.LPCWSTR, ctypes.POINTER(w.DWORD),
            w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, w.LPCWSTR,
        ]
        self._advapi32.ChangeServiceConfigW.restype = w.BOOL
        self._ScAction = type("ScAction", (ctypes.Structure,), {"_fields_": [("type", w.DWORD), ("delay", w.DWORD)]})
        self._ServiceFailureActions = type("ServiceFailureActions", (ctypes.Structure,), {"_fields_": [("reset_period", w.DWORD), ("reboot_message", w.LPWSTR), ("command", w.LPWSTR), ("action_count", w.DWORD), ("actions", ctypes.POINTER(self._ScAction))]})
        self._ServiceDescription = type("ServiceDescription", (ctypes.Structure,), {"_fields_": [("description", w.LPWSTR)]})
        self._advapi32.ChangeServiceConfig2W.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p]
        self._advapi32.ChangeServiceConfig2W.restype = w.BOOL
        self._advapi32.DeleteService.argtypes = [w.HANDLE]
        self._advapi32.DeleteService.restype = w.BOOL
        self._advapi32.StartServiceW.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.LPCWSTR)]
        self._advapi32.StartServiceW.restype = w.BOOL
        self._advapi32.ControlService.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p]
        self._advapi32.ControlService.restype = w.BOOL
        self._advapi32.QueryServiceStatusEx.argtypes = [
            w.HANDLE, w.DWORD, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD),
        ]
        self._advapi32.QueryServiceStatusEx.restype = w.BOOL

    def _open_manager(self, access: int) -> Any:
        handle = self._advapi32.OpenSCManagerW(None, None, access)
        if not handle:
            raise _win_error("OpenSCManagerW")
        return handle

    def _open_service(self, service_name: str, access: int) -> tuple[Any, Any]:
        manager = self._open_manager(self._SC_MANAGER_CONNECT)
        service = self._advapi32.OpenServiceW(manager, service_name, access)
        if not service:
            self._advapi32.CloseServiceHandle(manager)
            raise _win_error("OpenServiceW")
        return manager, service

    def _configure_description(self, service: Any, description: str) -> None:
        value = self._ServiceDescription(description)
        if not self._advapi32.ChangeServiceConfig2W(
            service,
            self._SERVICE_CONFIG_DESCRIPTION,
            ctypes.byref(value),
        ):
            raise _win_error("ChangeServiceConfig2W.description")

    @staticmethod
    def _service_account_name(value: str) -> str | None:
        if value.casefold() in {"localsystem", "system", "nt authority\\localsystem"}:
            return None
        return value

    def install(self, spec: ServiceInstallSpec) -> None:
        manager = self._open_manager(self._SC_MANAGER_ALL_ACCESS)
        start_type = {
            "auto": self._SERVICE_AUTO_START,
            "demand": self._SERVICE_DEMAND_START,
            "disabled": self._SERVICE_DISABLED,
        }[spec.start_type]
        dependency_blob = "\0".join(spec.dependencies) + "\0\0" if spec.dependencies else None
        service = None
        try:
            service = self._advapi32.CreateServiceW(
                manager,
                spec.service_name,
                spec.display_name,
                self._SERVICE_ALL_ACCESS,
                self._SERVICE_WIN32_OWN_PROCESS,
                start_type,
                self._SERVICE_ERROR_NORMAL,
                spec.binary_path,
                None,
                None,
                dependency_blob,
                self._service_account_name(spec.service_account),
                None,
            )
            if not service:
                error = ctypes.get_last_error()
                if error == self._ERROR_SERVICE_EXISTS:
                    raise WindowsServiceError("service_already_exists")
                raise _win_error("CreateServiceW")
            self._configure_description(service, spec.description)
        finally:
            if service:
                self._advapi32.CloseServiceHandle(service)
            self._advapi32.CloseServiceHandle(manager)

    def update(self, spec: ServiceInstallSpec) -> None:
        manager, service = self._open_service(spec.service_name, self._SERVICE_ALL_ACCESS)
        start_type = {
            "auto": self._SERVICE_AUTO_START,
            "demand": self._SERVICE_DEMAND_START,
            "disabled": self._SERVICE_DISABLED,
        }[spec.start_type]
        try:
            if not self._advapi32.ChangeServiceConfigW(
                service,
                self._SERVICE_WIN32_OWN_PROCESS,
                start_type,
                self._SERVICE_ERROR_NORMAL,
                spec.binary_path,
                None,
                None,
                None,
                self._service_account_name(spec.service_account),
                None,
                spec.display_name,
            ):
                raise _win_error("ChangeServiceConfigW")
            self._configure_description(service, spec.description)
        finally:
            self._advapi32.CloseServiceHandle(service)
            self._advapi32.CloseServiceHandle(manager)

    def uninstall(self, service_name: str) -> None:
        manager, service = self._open_service(service_name, self._SERVICE_ALL_ACCESS)
        try:
            if not self._advapi32.DeleteService(service):
                raise _win_error("DeleteService")
        finally:
            self._advapi32.CloseServiceHandle(service)
            self._advapi32.CloseServiceHandle(manager)

    def start(self, service_name: str) -> None:
        manager, service = self._open_service(service_name, self._SERVICE_START | self._SERVICE_QUERY_STATUS)
        try:
            if not self._advapi32.StartServiceW(service, 0, None):
                error = ctypes.get_last_error()
                if error != 1056:  # ERROR_SERVICE_ALREADY_RUNNING
                    raise _win_error("StartServiceW")
        finally:
            self._advapi32.CloseServiceHandle(service)
            self._advapi32.CloseServiceHandle(manager)

    def stop(self, service_name: str, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("service_stop_timeout_invalid")
        manager, service = self._open_service(service_name, self._SERVICE_STOP | self._SERVICE_QUERY_STATUS)
        class ServiceStatusStruct(ctypes.Structure):
            _fields_ = [("service_type", wintypes.DWORD), ("current_state", wintypes.DWORD), ("controls", wintypes.DWORD), ("win32_exit", wintypes.DWORD), ("service_exit", wintypes.DWORD), ("check_point", wintypes.DWORD), ("wait_hint", wintypes.DWORD)]
        try:
            status = ServiceStatusStruct()
            if not self._advapi32.ControlService(service, self._SERVICE_CONTROL_STOP, ctypes.byref(status)):
                error = ctypes.get_last_error()
                if error not in {self._ERROR_SERVICE_NOT_ACTIVE, 1061}:
                    raise _win_error("ControlService")
                return
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                if self.status(service_name) == "STOPPED":
                    return
                time.sleep(0.1)
            raise WindowsServiceError("service_stop_timeout")
        finally:
            self._advapi32.CloseServiceHandle(service)
            self._advapi32.CloseServiceHandle(manager)

    def status(self, service_name: str) -> ServiceStatus:
        manager, service = self._open_service(service_name, self._SERVICE_QUERY_STATUS)
        class ServiceStatusProcess(ctypes.Structure):
            _fields_ = [("service_type", wintypes.DWORD), ("current_state", wintypes.DWORD), ("controls", wintypes.DWORD), ("win32_exit", wintypes.DWORD), ("service_exit", wintypes.DWORD), ("check_point", wintypes.DWORD), ("wait_hint", wintypes.DWORD), ("process_id", wintypes.DWORD), ("flags", wintypes.DWORD)]
        try:
            value = ServiceStatusProcess()
            returned = wintypes.DWORD()
            if not self._advapi32.QueryServiceStatusEx(service, self._SC_STATUS_PROCESS_INFO, ctypes.byref(value), ctypes.sizeof(value), ctypes.byref(returned)):
                raise _win_error("QueryServiceStatusEx")
            return {
                self._SERVICE_STOPPED: "STOPPED",
                self._SERVICE_START_PENDING: "START_PENDING",
                self._SERVICE_STOP_PENDING: "STOP_PENDING",
                self._SERVICE_RUNNING: "RUNNING",
            }.get(value.current_state, "UNKNOWN")
        finally:
            self._advapi32.CloseServiceHandle(service)
            self._advapi32.CloseServiceHandle(manager)

    def configure_recovery(self, service_name: str, policy: FailureRecoveryPolicy) -> None:
        manager, service = self._open_service(service_name, self._SERVICE_ALL_ACCESS)
        action_values = (self._ScAction * len(policy.restart_delays_seconds))(
            *(self._ScAction(self._SC_ACTION_RESTART, delay * 1000) for delay in policy.restart_delays_seconds)
        )
        actions = self._ServiceFailureActions(
            policy.reset_period_seconds,
            None,
            None,
            len(policy.restart_delays_seconds),
            action_values,
        )
        try:
            if not self._advapi32.ChangeServiceConfig2W(
                service,
                self._SERVICE_CONFIG_FAILURE_ACTIONS,
                ctypes.byref(actions),
            ):
                raise _win_error("ChangeServiceConfig2W")
        finally:
            self._advapi32.CloseServiceHandle(service)
            self._advapi32.CloseServiceHandle(manager)


class WindowsSessionProcessBackend:
    """WTS + CreateProcessAsUser backend used by a Session 0 service."""

    _WTS_CURRENT_SERVER_HANDLE = ctypes.c_void_p(0)
    _WTS_ACTIVE = 0
    _WTS_CONNECTED = 1
    _WTS_DISCONNECTED = 4
    _WTS_INFO_CLASS_USERNAME = 5
    _TOKEN_USER = 1
    _TOKEN_DUPLICATE = 0x0002
    _TOKEN_QUERY = 0x0008
    _TOKEN_ASSIGN_PRIMARY = 0x0001
    _SECURITY_IMPERSONATION = 2
    _TOKEN_PRIMARY = 1
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _CREATE_NEW_PROCESS_GROUP = 0x00000200
    _PROCESS_TERMINATE = 0x0001
    _SYNCHRONIZE = 0x00100000
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 258
    _INFINITE = 0xFFFFFFFF

    def __init__(self) -> None:
        _require_windows()
        self._wtsapi32 = ctypes.WinDLL("wtsapi32", use_last_error=True)
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._userenv = ctypes.WinDLL("userenv", use_last_error=True)
        class StartupInfo(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("reserved", wintypes.LPWSTR),
                ("desktop", wintypes.LPWSTR),
                ("title", wintypes.LPWSTR),
                ("x", wintypes.DWORD),
                ("y", wintypes.DWORD),
                ("x_size", wintypes.DWORD),
                ("y_size", wintypes.DWORD),
                ("x_count_chars", wintypes.DWORD),
                ("y_count_chars", wintypes.DWORD),
                ("fill_attribute", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("show_window", wintypes.WORD),
                ("reserved2", wintypes.WORD),
                ("reserved2_ptr", ctypes.POINTER(ctypes.c_ubyte)),
                ("stdin", wintypes.HANDLE),
                ("stdout", wintypes.HANDLE),
                ("stderr", wintypes.HANDLE),
            ]
        class ProcessInformation(ctypes.Structure):
            _fields_ = [
                ("process", wintypes.HANDLE),
                ("thread", wintypes.HANDLE),
                ("process_id", wintypes.DWORD),
                ("thread_id", wintypes.DWORD),
            ]
        self._StartupInfo = StartupInfo
        self._ProcessInformation = ProcessInformation
        self._configure_api()

    def _configure_api(self) -> None:
        w = wintypes
        class WtsSessionInfo(ctypes.Structure):
            _fields_ = [("session_id", w.DWORD), ("station_name", w.LPWSTR), ("state", ctypes.c_int)]
        self._WtsSessionInfo = WtsSessionInfo
        self._wtsapi32.WTSEnumerateSessionsW.argtypes = [w.HANDLE, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.POINTER(WtsSessionInfo)), ctypes.POINTER(w.DWORD)]
        self._wtsapi32.WTSEnumerateSessionsW.restype = w.BOOL
        self._wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
        self._wtsapi32.WTSFreeMemory.restype = None
        self._wtsapi32.WTSQuerySessionInformationW.argtypes = [w.HANDLE, w.DWORD, ctypes.c_int, ctypes.POINTER(w.LPWSTR), ctypes.POINTER(w.DWORD)]
        self._wtsapi32.WTSQuerySessionInformationW.restype = w.BOOL
        self._wtsapi32.WTSQueryUserToken.argtypes = [w.DWORD, ctypes.POINTER(w.HANDLE)]
        self._wtsapi32.WTSQueryUserToken.restype = w.BOOL
        self._advapi32.DuplicateTokenEx.argtypes = [w.HANDLE, w.DWORD, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.POINTER(w.HANDLE)]
        self._advapi32.DuplicateTokenEx.restype = w.BOOL
        self._advapi32.GetTokenInformation.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD, ctypes.POINTER(w.DWORD)]
        self._advapi32.GetTokenInformation.restype = w.BOOL
        self._advapi32.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(w.LPWSTR)]
        self._advapi32.ConvertSidToStringSidW.restype = w.BOOL
        self._advapi32.CreateProcessAsUserW.argtypes = [w.HANDLE, w.LPCWSTR, w.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, w.BOOL, w.DWORD, ctypes.c_void_p, w.LPCWSTR, ctypes.POINTER(self._StartupInfo), ctypes.POINTER(self._ProcessInformation)]
        self._advapi32.CreateProcessAsUserW.restype = w.BOOL
        self._userenv.CreateEnvironmentBlock.argtypes = [ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.BOOL]
        self._userenv.CreateEnvironmentBlock.restype = w.BOOL
        self._userenv.DestroyEnvironmentBlock.argtypes = [ctypes.c_void_p]
        self._userenv.DestroyEnvironmentBlock.restype = w.BOOL
        self._kernel32.CloseHandle.argtypes = [w.HANDLE]
        self._kernel32.CloseHandle.restype = w.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p
        self._kernel32.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self._kernel32.OpenProcess.restype = w.HANDLE
        self._kernel32.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        self._kernel32.TerminateProcess.restype = w.BOOL
        self._kernel32.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        self._kernel32.WaitForSingleObject.restype = w.DWORD

    def enumerate_sessions(self) -> list[SessionSnapshot]:
        pointer = ctypes.POINTER(self._WtsSessionInfo)()
        count = wintypes.DWORD()
        if not self._wtsapi32.WTSEnumerateSessionsW(self._WTS_CURRENT_SERVER_HANDLE, 0, 1, ctypes.byref(pointer), ctypes.byref(count)):
            raise _win_error("WTSEnumerateSessionsW")
        results: list[SessionSnapshot] = []
        try:
            for index in range(count.value):
                item = pointer[index]
                if item.state not in {self._WTS_ACTIVE, self._WTS_CONNECTED, self._WTS_DISCONNECTED}:
                    continue
                username = self._query_username(item.session_id)
                token = wintypes.HANDLE()
                if not self._wtsapi32.WTSQueryUserToken(item.session_id, ctypes.byref(token)):
                    continue
                try:
                    sid = self._token_sid(token)
                finally:
                    self._kernel32.CloseHandle(token)
                if sid is None:
                    continue
                state: SessionState = "locked" if item.state == self._WTS_DISCONNECTED else "logged_in"
                results.append(SessionSnapshot(f"windows-session-{item.session_id}", int(item.session_id), sid, state, username))
        finally:
            self._wtsapi32.WTSFreeMemory(pointer)
        return results

    def _query_username(self, session_id: int) -> str | None:
        value = wintypes.LPWSTR()
        size = wintypes.DWORD()
        if not self._wtsapi32.WTSQuerySessionInformationW(self._WTS_CURRENT_SERVER_HANDLE, session_id, self._WTS_INFO_CLASS_USERNAME, ctypes.byref(value), ctypes.byref(size)):
            return None
        try:
            return value.value or None
        finally:
            self._wtsapi32.WTSFreeMemory(value)

    def _token_sid(self, token: Any) -> str | None:
        size = wintypes.DWORD()
        self._advapi32.GetTokenInformation(token, self._TOKEN_USER, None, 0, ctypes.byref(size))
        if size.value == 0:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if not self._advapi32.GetTokenInformation(token, self._TOKEN_USER, buffer, size.value, ctypes.byref(size)):
            return None
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        result = wintypes.LPWSTR()
        if not self._advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(result)):
            return None
        try:
            return result.value
        finally:
            self._kernel32.LocalFree(result)

    @staticmethod
    def _environment_block(values: Mapping[str, str]) -> ctypes.Array[ctypes.c_wchar]:
        entries = [f"{key}={value}" for key, value in sorted(values.items())]
        return ctypes.create_unicode_buffer("\0".join(entries) + "\0\0")

    def launch_worker(self, request: WorkerLaunchRequest) -> WorkerHandle:
        token = wintypes.HANDLE()
        if not self._wtsapi32.WTSQueryUserToken(request.session.windows_session_id, ctypes.byref(token)):
            raise _win_error("WTSQueryUserToken")
        primary = wintypes.HANDLE()
        try:
            token_access = self._TOKEN_ASSIGN_PRIMARY | self._TOKEN_DUPLICATE | self._TOKEN_QUERY
            if not self._advapi32.DuplicateTokenEx(token, token_access, None, self._SECURITY_IMPERSONATION, self._TOKEN_PRIMARY, ctypes.byref(primary)):
                raise _win_error("DuplicateTokenEx")
            base_environment = self._read_environment_block(primary)
            base_environment.update(request.environment)
            environment = self._environment_block(base_environment)
            startup = self._StartupInfo()
            startup.cb = ctypes.sizeof(startup)
            # CreateProcessAsUser starts from Session 0 unless the interactive
            # window station is selected explicitly.
            startup.desktop = "winsta0\\default"
            process_info = self._ProcessInformation()
            command_line = ctypes.create_unicode_buffer(_quote_command(request.command))
            if not self._advapi32.CreateProcessAsUserW(
                primary,
                None,
                command_line,
                None,
                None,
                False,
                self._CREATE_UNICODE_ENVIRONMENT | self._CREATE_NEW_PROCESS_GROUP,
                ctypes.cast(environment, ctypes.c_void_p),
                request.cwd,
                ctypes.byref(startup),
                ctypes.byref(process_info),
            ):
                raise _win_error("CreateProcessAsUserW")
            self._kernel32.CloseHandle(process_info.thread)
            # Keep the process handle as the identity anchor.  Polling and
            # stopping this Worker must never silently switch to a reused PID.
            return WorkerHandle(
                request.worker_id,
                request.session.session_id,
                request.session.windows_session_id,
                int(process_info.process_id),
                request.pipe_name,
                process_info.process,
            )
        finally:
            if primary:
                self._kernel32.CloseHandle(primary)
            self._kernel32.CloseHandle(token)

    def _read_environment_block(self, token: Any) -> dict[str, str]:
        block = ctypes.c_void_p()
        if not self._userenv.CreateEnvironmentBlock(ctypes.byref(block), token, False):
            raise _win_error("CreateEnvironmentBlock")
        try:
            pointer = ctypes.cast(block, ctypes.POINTER(ctypes.c_wchar))
            values: dict[str, str] = {}
            offset = 0
            while True:
                value = ctypes.wstring_at(ctypes.addressof(pointer.contents) + offset * ctypes.sizeof(ctypes.c_wchar))
                if not value:
                    break
                if "=" in value:
                    key, item = value.split("=", 1)
                    values[key] = item
                offset += len(value) + 1
            return values
        finally:
            self._userenv.DestroyEnvironmentBlock(block)

    def stop_worker(self, handle: WorkerHandle, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("worker_stop_timeout_invalid")
        process = handle.process_handle
        owns_process = process is None
        if process is None:
            process = self._kernel32.OpenProcess(self._PROCESS_TERMINATE | self._SYNCHRONIZE, False, handle.process_id)
            if not process:
                error = ctypes.get_last_error()
                if error in {5, 87, 1168}:  # access denied, invalid parameter, not found
                    return
                raise _win_error("OpenProcess")
        try:
            if not self._kernel32.TerminateProcess(process, 1):
                raise _win_error("TerminateProcess")
            result = self._kernel32.WaitForSingleObject(process, int(timeout_seconds * 1000))
            if result != self._WAIT_OBJECT_0:
                raise WindowsServiceError("worker_stop_timeout")
        finally:
            if owns_process or handle.process_handle is not None:
                self._kernel32.CloseHandle(process)

    def worker_is_running(self, handle: WorkerHandle) -> bool:
        process = handle.process_handle
        owns_process = process is None
        if process is None:
            process = self._kernel32.OpenProcess(self._SYNCHRONIZE, False, handle.process_id)
            if not process:
                return False
        try:
            return self._kernel32.WaitForSingleObject(process, 0) == self._WAIT_TIMEOUT
        finally:
            if owns_process:
                self._kernel32.CloseHandle(process)

    def release_worker_handle(self, handle: WorkerHandle) -> None:
        if handle.process_handle is not None:
            self._kernel32.CloseHandle(handle.process_handle)

    def wait_worker_ready(
        self,
        handle: WorkerHandle,
        *,
        peer_id: str,
        process_id: int | None,
        windows_session_id: int,
        timeout_seconds: float = 10.0,
    ) -> None:
        if not peer_id or windows_session_id < 0 or timeout_seconds <= 0:
            raise ValueError("worker_readiness_probe_arguments_invalid")
        try:
            from .named_pipe_transport import NamedPipeClient
        except ImportError:
            from named_pipe_transport import NamedPipeClient
        request = SessionIpcRequest(
            request_id=f"readiness-request-{handle.worker_id}-{time.time_ns()}",
            idempotency_key=f"readiness-{handle.worker_id}-{time.time_ns()}",
            message_type="session.hello",
            peer=SessionPeer(
                peer_id=peer_id,
                peer_kind="machine_service",
                process_id=process_id,
                windows_session_id=windows_session_id,
            ),
            worker_id=handle.worker_id,
            user_session_id=handle.session_id,
            sent_at=datetime.now(UTC),
            payload={"readiness_probe": True},
        )
        response = NamedPipeClient(handle.pipe_name).request(request, timeout_seconds=timeout_seconds)
        if response.status != "COMPLETED" or response.payload.get("state") != "READY":
            raise WindowsServiceError(f"worker_readiness_failed:{response.error_code or response.status}")


@dataclass
class SessionWorkerSlot:
    lifecycle: MachineServiceLifecycle
    session: SessionSnapshot
    worker: WorkerHandle | None = None


class SessionWorkerCoordinator:
    """Drive lifecycle policy from real or fake Win32 observations."""

    def __init__(
        self,
        backend: SessionProcessBackend,
        *,
        worker_command: Sequence[str] | None = None,
        worker_command_factory: Callable[[SessionSnapshot, str], Sequence[str]] | None = None,
        worker_cwd: str,
        pipe_name_factory: Callable[[SessionSnapshot], str],
        worker_environment: Mapping[str, str] | None = None,
        on_worker_launch: Callable[[WorkerLaunchAudit], None] | None = None,
        on_lifecycle_transition: Callable[[SessionLifecycleTransition], None] | None = None,
        machine_peer_id: str = "machine-service",
        machine_process_id: int | None = None,
        machine_windows_session_id: int = 0,
        worker_readiness_timeout_seconds: float = 10.0,
    ) -> None:
        if not worker_command and worker_command_factory is None:
            raise ValueError("coordinator_worker_command_required")
        if not Path(worker_cwd).is_absolute():
            raise ValueError("coordinator_worker_cwd_must_be_absolute")
        self.backend = backend
        self.worker_command = tuple(worker_command or ())
        self.worker_command_factory = worker_command_factory
        self.worker_cwd = worker_cwd
        self.pipe_name_factory = pipe_name_factory
        self.worker_environment = dict(worker_environment or {})
        self.on_worker_launch = on_worker_launch
        self.on_lifecycle_transition = on_lifecycle_transition
        if not machine_peer_id or machine_windows_session_id < 0 or worker_readiness_timeout_seconds <= 0:
            raise ValueError("coordinator_readiness_configuration_invalid")
        self.machine_peer_id = machine_peer_id
        self.machine_process_id = machine_process_id
        self.machine_windows_session_id = machine_windows_session_id
        self.worker_readiness_timeout_seconds = worker_readiness_timeout_seconds
        self._slots: dict[str, SessionWorkerSlot] = {}
        self._launch_audits: list[WorkerLaunchAudit] = []

    @property
    def slots(self) -> dict[str, SessionWorkerSlot]:
        return dict(self._slots)

    def launch_audits(self) -> list[WorkerLaunchAudit]:
        return list(self._launch_audits)

    def reconcile(self) -> list[LifecycleResult]:
        observed = {item.session_id: item for item in self.backend.enumerate_sessions()}
        results: list[LifecycleResult] = []
        for session_id, slot in list(self._slots.items()):
            if session_id not in observed:
                results.append(self._handle_event(slot, "session.logged_out"))
                self._slots.pop(session_id, None)
                continue
            session = observed[session_id]
            # WTS enumeration reports an active session for a locked desktop;
            # only the explicit WTS unlock event can change this remembered
            # state back to logged_in.
            if session.state == "logged_in" and slot.lifecycle.session_state == "locked":
                session = replace(session, state="locked")
            slot.session = session
            results.append(self._apply_session_state(slot, session.state))
            if slot.worker is None and slot.lifecycle.state in {"DEGRADED", "FAILED"} and session.state != "logged_out":
                recovery = slot.lifecycle.apply("recovery.requested", reason="session_reconcile")
                results.append(recovery)
                results.extend(self._execute_actions(slot, recovery.actions))
        for session_id, session in observed.items():
            if session_id in self._slots:
                continue
            lifecycle = MachineServiceLifecycle(
                worker_id=f"worker-{session_id}",
                user_session_id=session.session_id,
                session_state=session.state,
                on_transition=self.on_lifecycle_transition,
            )
            slot = SessionWorkerSlot(lifecycle, session)
            self._slots[session_id] = slot
            started = lifecycle.apply("service.start", reason="session_reconcile")
            results.append(started)
            results.extend(self._execute_actions(slot, started.actions))
        return results

    def handle_event(self, session_id: str, event: SessionEvent) -> list[LifecycleResult]:
        slot = self._slots.get(session_id)
        if slot is None:
            return []
        result = self._handle_event(slot, event)
        return [result]

    def handle_wts_event(self, windows_session_id: int, event_code: int) -> list[LifecycleResult]:
        """Handle SERVICE_CONTROL_SESSIONCHANGE without trusting its payload."""

        if windows_session_id < 0:
            raise ValueError("windows_session_id_invalid")
        event = session_event_from_wts(event_code)
        if event is None:
            return []
        session_id = f"windows-session-{windows_session_id}"
        if event == "session.logged_in":
            # The authoritative SID and state come from WTS enumeration, not
            # from the service notification's numeric session ID alone.
            return self.reconcile()
        return self.handle_event(session_id, event)


    def stop_all(self) -> list[LifecycleResult]:
        results: list[LifecycleResult] = []
        for slot in list(self._slots.values()):
            result = slot.lifecycle.apply("service.stop", reason="coordinator_stop")
            results.append(result)
            results.extend(self._execute_actions(slot, result.actions))
            if slot.lifecycle.state == "STOPPING":
                stopped = slot.lifecycle.apply("worker.stopped", reason="coordinator_stop_complete")
                results.append(stopped)
        self._slots.clear()
        return results

    def poll_worker_exits(self) -> list[LifecycleResult]:
        """Convert an observed Worker process exit into a lifecycle event."""

        results: list[LifecycleResult] = []
        for slot in self._slots.values():
            if slot.worker is None or self.backend.worker_is_running(slot.worker):
                continue
            self.backend.release_worker_handle(slot.worker)
            slot.worker = None
            results.append(slot.lifecycle.apply("worker.exited", reason="worker_process_exited"))
        return results

    def _apply_session_state(self, slot: SessionWorkerSlot, state: SessionState) -> LifecycleResult:
        signal: SessionEvent = {
            "logged_in": "session.logged_in",
            "locked": "session.locked",
            "logged_out": "session.logged_out",
        }[state]
        if state == "logged_in" and slot.lifecycle.session_state == "logged_in":
            if slot.lifecycle.state == "WORKER_READY":
                return LifecycleResult(True, signal, slot.lifecycle.state, slot.lifecycle.state, state, tuple(sorted(slot.lifecycle.active_run_ids)), actions=())
            signal = "session.unlocked"
        return self._handle_event(slot, signal)

    def _handle_event(self, slot: SessionWorkerSlot, event: SessionEvent) -> LifecycleResult:
        result = slot.lifecycle.apply(event, session_id=slot.session.session_id, reason="windows_session_event")
        self._execute_actions(slot, result.actions)
        return result

    def _execute_actions(self, slot: SessionWorkerSlot, actions: Sequence[LifecycleAction]) -> list[LifecycleResult]:
        results: list[LifecycleResult] = []
        for action in actions:
            if action.kind == "start_worker":
                pipe_name = self.pipe_name_factory(slot.session)
                command = (
                    tuple(self.worker_command_factory(slot.session, pipe_name))
                    if self.worker_command_factory is not None
                    else self.worker_command
                )
                request = WorkerLaunchRequest(
                    worker_id=slot.lifecycle.worker_id,
                    session=slot.session,
                    command=command,
                    cwd=self.worker_cwd,
                    pipe_name=pipe_name,
                    environment=self.worker_environment,
                )
                try:
                    slot.worker = self.backend.launch_worker(request)
                    self.backend.wait_worker_ready(
                        slot.worker,
                        peer_id=self.machine_peer_id,
                        process_id=self.machine_process_id,
                        windows_session_id=self.machine_windows_session_id,
                        timeout_seconds=self.worker_readiness_timeout_seconds,
                    )
                    audit = WorkerLaunchAudit.from_request(request, slot.worker)
                    self._launch_audits.append(audit)
                    if self.on_worker_launch is not None:
                        try:
                            self.on_worker_launch(audit)
                        except Exception:
                            # Audit sinks are best effort; a successful Worker
                            # must not be launched a second time on retry.
                            pass
                    results.append(slot.lifecycle.apply("worker.ready", reason="worker_process_ready"))
                except Exception as error:
                    if slot.worker is not None:
                        try:
                            self.backend.stop_worker(slot.worker)
                        except Exception:
                            pass
                        slot.worker = None
                    results.append(slot.lifecycle.apply("worker.failed", reason=f"worker_start_failed:{error}"))
            elif action.kind == "stop_worker" and slot.worker is not None:
                try:
                    self.backend.stop_worker(slot.worker)
                finally:
                    slot.worker = None
            elif action.kind == "stop_runs":
                # Run termination is owned by SessionWorkerRuntime.  This
                # adapter only records the requested cleanup IDs in lifecycle.
                continue
        return results


class ServiceDispatcherBackend(Protocol):
    def run(
        self,
        service_name: str,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_session_event: Callable[[int, SessionEvent], None],
    ) -> None: ...


class CtypesServiceDispatcherBackend:
    """Native SCM dispatcher with session-change notifications."""

    _SERVICE_WIN32_OWN_PROCESS = 0x00000010
    _SERVICE_START_PENDING = 0x00000002
    _SERVICE_STOP_PENDING = 0x00000003
    _SERVICE_RUNNING = 0x00000004
    _SERVICE_STOPPED = 0x00000001
    _SERVICE_ACCEPT_STOP = 0x00000001
    _SERVICE_ACCEPT_SHUTDOWN = 0x00000004
    _SERVICE_ACCEPT_SESSIONCHANGE = 0x00000080
    _SERVICE_CONTROL_STOP = 0x00000001
    _SERVICE_CONTROL_SHUTDOWN = 0x00000005
    _SERVICE_CONTROL_SESSIONCHANGE = 0x0000000E
    _SERVICE_CONTROL_PRESHUTDOWN = 0x0000000F

    def __init__(self) -> None:
        _require_windows()
        self._advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self._stop_event = None
        self._on_start: Callable[[], None] | None = None
        self._on_stop: Callable[[], None] | None = None
        self._on_session_event: Callable[[int, SessionEvent], None] | None = None
        self._status_handle: Any = None
        self._callbacks: list[Any] = []
        self._configure_api()

    def _configure_api(self) -> None:
        w = wintypes

        class ServiceStatus(ctypes.Structure):
            _fields_ = [
                ("service_type", w.DWORD),
                ("current_state", w.DWORD),
                ("controls_accepted", w.DWORD),
                ("win32_exit_code", w.DWORD),
                ("service_specific_exit_code", w.DWORD),
                ("check_point", w.DWORD),
                ("wait_hint", w.DWORD),
            ]

        self._ServiceStatus = ServiceStatus
        class SessionNotification(ctypes.Structure):
            _fields_ = [("size", w.DWORD), ("session_id", w.DWORD)]

        self._SessionNotification = SessionNotification
        self._handler_type = ctypes.WINFUNCTYPE(w.DWORD, w.DWORD, w.DWORD, w.LPVOID, w.LPVOID)
        self._main_type = ctypes.WINFUNCTYPE(None, w.DWORD, ctypes.POINTER(w.LPWSTR))
        self._entry_type = type(
            "ServiceTableEntry",
            (ctypes.Structure,),
            {"_fields_": [("service_name", w.LPWSTR), ("service_main", self._main_type)]},
        )
        self._advapi32.RegisterServiceCtrlHandlerExW.argtypes = [w.LPCWSTR, self._handler_type, w.LPVOID]
        self._advapi32.RegisterServiceCtrlHandlerExW.restype = w.HANDLE
        self._advapi32.SetServiceStatus.argtypes = [w.HANDLE, ctypes.POINTER(ServiceStatus)]
        self._advapi32.SetServiceStatus.restype = w.BOOL
        self._advapi32.StartServiceCtrlDispatcherW.argtypes = [ctypes.POINTER(self._entry_type)]
        self._advapi32.StartServiceCtrlDispatcherW.restype = w.BOOL

    def run(
        self,
        service_name: str,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_session_event: Callable[[int, SessionEvent], None],
    ) -> None:
        if not service_name:
            raise ValueError("service_dispatcher_name_required")
        self._on_start = on_start
        self._on_stop = on_stop
        self._on_session_event = on_session_event
        self._service_name = service_name
        self._stop_event = threading.Event()
        main_callback = self._main_type(self._service_main)
        handler_callback = self._handler_type(self._control_handler)
        self._callbacks = [main_callback, handler_callback]
        service_name_buffer = ctypes.create_unicode_buffer(service_name)
        table = (self._entry_type * 2)()
        table[0].service_name = ctypes.cast(service_name_buffer, wintypes.LPWSTR)
        table[0].service_main = main_callback
        table[1].service_name = None
        table[1].service_main = self._main_type()
        if not self._advapi32.StartServiceCtrlDispatcherW(table):
            error = ctypes.get_last_error()
            if error == 1063:  # ERROR_FAILED_SERVICE_CONTROLLER_CONNECT
                raise WindowsServiceError("service_dispatcher_not_started_by_scm")
            raise _win_error("StartServiceCtrlDispatcherW")

    def _set_status(self, state: int, controls: int = 0, exit_code: int = 0) -> None:
        status = self._ServiceStatus(
            self._SERVICE_WIN32_OWN_PROCESS,
            state,
            controls,
            exit_code,
            0,
            0,
            3000,
        )
        if not self._advapi32.SetServiceStatus(self._status_handle, ctypes.byref(status)):
            raise _win_error("SetServiceStatus")

    def _service_main(self, _argc: int, _argv: Any) -> None:
        self._status_handle = self._advapi32.RegisterServiceCtrlHandlerExW(
            self._service_name_from_table(),
            self._callbacks[1],
            None,
        )
        if not self._status_handle:
            return
        try:
            self._set_status(self._SERVICE_START_PENDING)
            assert self._on_start is not None
            self._on_start()
            self._set_status(
                self._SERVICE_RUNNING,
                self._SERVICE_ACCEPT_STOP | self._SERVICE_ACCEPT_SHUTDOWN | self._SERVICE_ACCEPT_SESSIONCHANGE,
            )
            assert self._stop_event is not None
            self._stop_event.wait()
            self._set_status(self._SERVICE_STOP_PENDING)
            if self._on_stop is not None:
                self._on_stop()
            self._set_status(self._SERVICE_STOPPED)
        except Exception:
            try:
                self._set_status(self._SERVICE_STOPPED, exit_code=1)
            except Exception:
                pass

    def _service_name_from_table(self) -> str:
        # The SCM passes the service name in argv[0].  The dispatcher keeps the
        # mutable callback independent from command-line parsing by storing it
        # in the run closure before StartServiceCtrlDispatcherW.
        return self._service_name

    def _control_handler(self, control: int, event_type: int, _event_data: Any, _context: Any) -> int:
        if control in {self._SERVICE_CONTROL_STOP, self._SERVICE_CONTROL_SHUTDOWN, self._SERVICE_CONTROL_PRESHUTDOWN}:
            if self._stop_event is not None:
                self._stop_event.set()
            return 0
        if control == self._SERVICE_CONTROL_SESSIONCHANGE:
            notification = ctypes.cast(_event_data, ctypes.POINTER(self._SessionNotification)).contents if _event_data else None
            if notification is not None:
                event = session_event_from_wts(int(event_type))
                if event is not None and self._on_session_event is not None:
                    self._on_session_event(int(notification.session_id), event)
            return 0
        return 0


class WindowsServiceHost:
    """Poll-based Session 0 host suitable for a thin SCM service wrapper."""

    def __init__(
        self,
        coordinator: SessionWorkerCoordinator,
        *,
        reconcile_interval_seconds: float = 5.0,
        dispatcher: ServiceDispatcherBackend | None = None,
    ) -> None:
        if reconcile_interval_seconds <= 0:
            raise ValueError("service_host_reconcile_interval_invalid")
        self.coordinator = coordinator
        self.reconcile_interval_seconds = reconcile_interval_seconds
        self.dispatcher = dispatcher
        self._monitor_stop = threading.Event()
        self._monitor_thread: threading.Thread | None = None

    def run_once(self) -> list[LifecycleResult]:
        results = self.coordinator.reconcile()
        results.extend(self.coordinator.poll_worker_exits())
        return results

    def run_forever(self, stop_event: Any) -> None:
        while not stop_event.is_set():
            self.run_once()
            stop_event.wait(self.reconcile_interval_seconds)

    def run_as_windows_service(self, service_name: str) -> None:
        dispatcher = self.dispatcher or CtypesServiceDispatcherBackend()
        dispatcher.run(service_name, self._on_service_start, self._on_service_stop, self._on_session_event)

    def _on_service_start(self) -> None:
        self._monitor_stop.clear()
        self._monitor_thread = threading.Thread(
            target=self.run_forever,
            args=(self._monitor_stop,),
            name="math-agent-session-monitor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _on_service_stop(self) -> None:
        self._monitor_stop.set()
        if self._monitor_thread is not None:
            self._monitor_thread.join(timeout=max(1.0, self.reconcile_interval_seconds + 1.0))
            self._monitor_thread = None
        self.coordinator.stop_all()

    def _on_session_event(self, windows_session_id: int, event: SessionEvent) -> None:
        event_code = {
            "session.logged_in": 5,
            "session.logged_out": 6,
            "session.locked": 7,
            "session.unlocked": 8,
        }[event]
        self.coordinator.handle_wts_event(windows_session_id, event_code)


def make_session_worker_command(
    agentd_command: Sequence[str],
    *,
    pipe_name: str,
    worker_id: str,
    user_session_id: str,
    user_sid: str,
    workspace: str,
    allowed_peer_id: str,
    allowed_sids: Sequence[str],
    allowed_session_ids: Sequence[int],
    state_path: str,
    adapter_id: str = "python-user-session",
    conpty_adapter_id: str | None = None,
    python_executable: str | None = None,
) -> tuple[str, ...]:
    """Build a fully bound Worker command for one discovered user session."""

    values = [
        *agentd_command,
        "session-worker-run",
        "--pipe-name", pipe_name,
        "--worker-id", worker_id,
        "--user-session-id", user_session_id,
        "--user-sid", user_sid,
        "--allowed-peer-id", allowed_peer_id,
        "--allowed-sids", *allowed_sids,
        "--allowed-session-ids", *(str(value) for value in allowed_session_ids),
        "--workspace", workspace,
        "--state-path", state_path,
        "--adapter-id", adapter_id,
    ]
    if conpty_adapter_id:
        values.extend(["--conpty-adapter-id", conpty_adapter_id])
    if python_executable:
        values.extend(["--python-executable", python_executable])
    return tuple(values)

def session_event_from_wts(event_code: int) -> SessionEvent | None:
    """Map WTS_SESSION_NOTIFICATION values to the shared lifecycle signals."""

    return {
        5: "session.logged_in",   # WTS_SESSION_LOGON
        6: "session.logged_out",  # WTS_SESSION_LOGOFF
        7: "session.locked",      # WTS_SESSION_LOCK
        8: "session.unlocked",    # WTS_SESSION_UNLOCK
    }.get(event_code)


__all__ = [
    "CtypesServiceControlBackend",
    "CtypesServiceDispatcherBackend",
    "FailureRecoveryPolicy",
    "ServiceControlBackend",
    "ServiceDispatcherBackend",
    "ServiceInstallSpec",
    "ServiceStatus",
    "SessionEvent",
    "SessionProcessBackend",
    "SessionSnapshot",
    "SessionWorkerCoordinator",
    "WindowsSessionProcessBackend",
    "WindowsServiceError",
    "WindowsServiceUnavailable",
    "WorkerHandle",
    "WorkerLaunchRequest",
    "WorkerLaunchAudit",
    "WindowsServiceHost",
    "WindowsServiceInstaller",
    "session_event_from_wts",
    "make_session_worker_command",
]
