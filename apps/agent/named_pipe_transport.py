"""Windows Named Pipe transport for the User Session Worker protocol.

The transport owns framing, Windows ACL construction, and OS-level peer
identity discovery.  It deliberately does not make workflow decisions: an
authenticated ``SessionPeer`` is passed to ``SessionWorkerBroker`` (or another
caller-supplied handler) for protocol authorization.

The implementation uses synchronous Win32 calls behind a small blocking API.
Callers that run an asyncio service can place ``serve_once``/``request`` in a
worker thread.  This keeps the Windows-specific layer independent from the
event loop while preserving one framing and authentication implementation.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import struct
import time
from dataclasses import dataclass
from threading import Event
from typing import Any, Callable, Literal

try:
    from packages.agent_protocol import SessionIpcRequest, SessionIpcResponse, SessionPeer
except ModuleNotFoundError:  # Support direct script execution during development.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from packages.agent_protocol import SessionIpcRequest, SessionIpcResponse, SessionPeer


class NamedPipeError(RuntimeError):
    """Stable error family for Windows Named Pipe failures."""


class NamedPipeUnavailable(NamedPipeError):
    """Raised when the transport is used on a non-Windows host."""


class FrameError(NamedPipeError):
    """Raised for malformed, truncated, or oversized IPC frames."""


MAX_FRAME_BYTES = 1024 * 1024
PIPE_PREFIX = "\\\\.\\pipe\\"
_SID_PATTERN = re.compile(r"^S-\d-\d+(?:-\d+)+$", re.IGNORECASE)


@dataclass(frozen=True)
class PipePeerPolicy:
    """Expected identity of the process allowed to use one pipe endpoint."""

    peer_id: str
    peer_kind: Literal["machine_service", "user_session_worker", "desktop_ui"]
    allowed_sid: str | None = None
    allowed_sids: tuple[str, ...] = ()
    allowed_session_ids: tuple[int, ...] = ()
    max_frame_bytes: int = MAX_FRAME_BYTES

    def __post_init__(self) -> None:
        if len(self.peer_id) < 2:
            raise ValueError("pipe_peer_id_required")
        trusted_sids = self.trusted_sids
        if not trusted_sids or any(not _SID_PATTERN.fullmatch(sid) for sid in trusted_sids):
            raise ValueError("pipe_allowed_sid_invalid")
        if not self.allowed_session_ids or any(session_id < 0 for session_id in self.allowed_session_ids):
            raise ValueError("pipe_allowed_session_ids_required")
        if self.max_frame_bytes < 1024 or self.max_frame_bytes > MAX_FRAME_BYTES:
            raise ValueError("pipe_max_frame_bytes_invalid")

    @property
    def trusted_sids(self) -> tuple[str, ...]:
        legacy_sid = (self.allowed_sid,) if self.allowed_sid else ()
        return tuple(dict.fromkeys((*self.allowed_sids, *legacy_sid)))


class JsonFrameCodec:
    """Length-prefixed UTF-8 JSON codec used over byte-mode Named Pipes."""

    HEADER = struct.Struct("<I")

    @classmethod
    def encode(cls, payload: dict[str, Any], *, max_frame_bytes: int = MAX_FRAME_BYTES) -> bytes:
        try:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise FrameError("pipe_payload_not_json_serializable") from error
        if not body or len(body) > max_frame_bytes:
            raise FrameError("pipe_frame_size_invalid")
        return cls.HEADER.pack(len(body)) + body

    @classmethod
    def decode(cls, frame: bytes, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> dict[str, Any]:
        if len(frame) < cls.HEADER.size:
            raise FrameError("pipe_frame_header_truncated")
        (body_size,) = cls.HEADER.unpack(frame[: cls.HEADER.size])
        if body_size == 0 or body_size > max_frame_bytes:
            raise FrameError("pipe_frame_size_invalid")
        expected_size = cls.HEADER.size + body_size
        if len(frame) != expected_size:
            raise FrameError("pipe_frame_length_mismatch")
        try:
            payload = json.loads(frame[cls.HEADER.size :].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise FrameError("pipe_frame_json_invalid") from error
        if not isinstance(payload, dict):
            raise FrameError("pipe_frame_object_required")
        return payload


if os.name == "nt":
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    HANDLE = wintypes.HANDLE
    LPVOID = ctypes.c_void_p

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    _kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_SecurityAttributes),
    ]
    _kernel32.CreateNamedPipeW.restype = HANDLE
    _kernel32.ConnectNamedPipe.argtypes = [HANDLE, LPVOID]
    _kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    _kernel32.DisconnectNamedPipe.argtypes = [HANDLE]
    _kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        HANDLE,
    ]
    _kernel32.CreateFileW.restype = HANDLE
    _kernel32.ReadFile.argtypes = [HANDLE, LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), LPVOID]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [HANDLE, LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), LPVOID]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.FlushFileBuffers.argtypes = [HANDLE]
    _kernel32.FlushFileBuffers.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetNamedPipeClientProcessId.argtypes = [HANDLE, ctypes.POINTER(wintypes.ULONG)]
    _kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = HANDLE
    _kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    _kernel32.SetNamedPipeHandleState.argtypes = [HANDLE, ctypes.POINTER(wintypes.DWORD), LPVOID, LPVOID]
    _kernel32.SetNamedPipeHandleState.restype = wintypes.BOOL
    _kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    _kernel32.GetLastError.restype = wintypes.DWORD
    _kernel32.LocalFree.argtypes = [LPVOID]
    _kernel32.LocalFree.restype = LPVOID

    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(LPVOID),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [HANDLE, wintypes.DWORD, ctypes.POINTER(HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [HANDLE, wintypes.DWORD, LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL

    _INVALID_HANDLE_VALUE = HANDLE(-1).value
    _ERROR_PIPE_CONNECTED = 535
    _ERROR_PIPE_BUSY = 231
    _ERROR_BROKEN_PIPE = 109
    _ERROR_NO_DATA = 232
    _ERROR_PIPE_NOT_CONNECTED = 233
    _ERROR_MORE_DATA = 234
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_SEM_TIMEOUT = 121
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _PIPE_ACCESS_DUPLEX = 0x00000003
    _PIPE_TYPE_BYTE = 0x00000000
    _PIPE_READMODE_BYTE = 0x00000000
    _PIPE_WAIT = 0x00000000
    _PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
    _PIPE_UNLIMITED_INSTANCES = 255
    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _SECURITY_DESCRIPTOR_REVISION = 1
else:
    HANDLE = Any  # type: ignore[misc,assignment]


def _require_windows() -> None:
    if os.name != "nt":
        raise NamedPipeUnavailable("windows_named_pipe_requires_windows")


def _win_error(operation: str) -> NamedPipeError:
    error = ctypes.get_last_error()
    return NamedPipeError(f"{operation}: win32_error_{error}")


def _handle_value(handle: HANDLE) -> int:
    value = getattr(handle, "value", handle)
    return int(value or 0)


def _valid_pipe_name(name: str) -> str:
    if not name.startswith(PIPE_PREFIX):
        name = PIPE_PREFIX + name.strip("\\/")
    suffix = name[len(PIPE_PREFIX) :]
    if not suffix or "\\" in suffix or "/" in suffix or len(name) > 256:
        raise ValueError("pipe_name_invalid")
    return name


def _sid_from_token(token: HANDLE) -> str:
    _require_windows()
    required = wintypes.DWORD(0)
    _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(required))
    if required.value <= 0:
        raise _win_error("GetTokenInformation.size")
    buffer = ctypes.create_string_buffer(required.value)
    if not _advapi32.GetTokenInformation(token, _TOKEN_USER, buffer, required, ctypes.byref(required)):
        raise _win_error("GetTokenInformation")
    sid_pointer = ctypes.cast(buffer, ctypes.POINTER(LPVOID))[0]
    sid_string = wintypes.LPWSTR()
    if not _advapi32.ConvertSidToStringSidW(sid_pointer, ctypes.byref(sid_string)):
        raise _win_error("ConvertSidToStringSid")
    try:
        return sid_string.value
    finally:
        _kernel32.LocalFree(sid_string)


def current_user_sid() -> str:
    """Return the SID of the current process token."""

    _require_windows()
    token = HANDLE()
    if not _advapi32.OpenProcessToken(HANDLE(-1), _TOKEN_QUERY, ctypes.byref(token)):
        raise _win_error("OpenProcessToken")
    try:
        return _sid_from_token(token)
    finally:
        _kernel32.CloseHandle(token)


def process_user_sid(process_id: int) -> str:
    """Return the SID of a connected pipe client's process token."""

    _require_windows()
    process = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, process_id)
    if not process or _handle_value(process) == _INVALID_HANDLE_VALUE:
        raise _win_error("OpenProcess")
    token = HANDLE()
    try:
        if not _advapi32.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
            raise _win_error("OpenProcessToken.client")
        try:
            return _sid_from_token(token)
        finally:
            _kernel32.CloseHandle(token)
    finally:
        _kernel32.CloseHandle(process)


def process_session_id(process_id: int) -> int:
    """Return the Windows Terminal Services session ID for a process."""

    _require_windows()
    session_id = wintypes.DWORD(0)
    if not _kernel32.ProcessIdToSessionId(process_id, ctypes.byref(session_id)):
        raise _win_error("ProcessIdToSessionId")
    return int(session_id.value)


class _SecurityDescriptor:
    def __init__(self, allowed_sids: tuple[str, ...]) -> None:
        _require_windows()
        if not allowed_sids or any(not _SID_PATTERN.fullmatch(sid) for sid in allowed_sids):
            raise ValueError("pipe_allowed_sid_invalid")
        # SYSTEM and Administrators retain operational access; the configured
        # endpoint SIDs receive only generic read/write access.
        endpoint_aces = "".join(f"(A;;GRGW;;;{sid})" for sid in allowed_sids)
        self.sddl = f"D:P(A;;GA;;;SY)(A;;GA;;;BA){endpoint_aces}"
        descriptor = LPVOID()
        size = wintypes.ULONG(0)
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            self.sddl,
            _SECURITY_DESCRIPTOR_REVISION,
            ctypes.byref(descriptor),
            ctypes.byref(size),
        ):
            raise _win_error("ConvertStringSecurityDescriptorToSecurityDescriptor")
        self.descriptor = descriptor
        self.attributes = _SecurityAttributes(
            nLength=ctypes.sizeof(_SecurityAttributes),
            lpSecurityDescriptor=descriptor,
            bInheritHandle=False,
        )

    def close(self) -> None:
        if getattr(self, "descriptor", None):
            _kernel32.LocalFree(self.descriptor)
            self.descriptor = None

    def __enter__(self) -> "_SecurityDescriptor":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class NamedPipeConnection:
    """Blocking, byte-mode JSON framed connection over one pipe handle."""

    def __init__(self, handle: HANDLE, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        _require_windows()
        self.handle = handle
        self.max_frame_bytes = max_frame_bytes
        self._closed = False

    def send_json(self, payload: dict[str, Any]) -> None:
        self._write_all(JsonFrameCodec.encode(payload, max_frame_bytes=self.max_frame_bytes))
        if not _kernel32.FlushFileBuffers(self.handle):
            raise _win_error("FlushFileBuffers")

    def receive_json(self) -> dict[str, Any]:
        header = self._read_exact(JsonFrameCodec.HEADER.size)
        (body_size,) = JsonFrameCodec.HEADER.unpack(header)
        if body_size == 0 or body_size > self.max_frame_bytes:
            raise FrameError("pipe_frame_size_invalid")
        body = self._read_exact(body_size)
        return JsonFrameCodec.decode(header + body, max_frame_bytes=self.max_frame_bytes)

    def send_model(self, model: Any) -> None:
        if hasattr(model, "model_dump"):
            self.send_json(model.model_dump(mode="json"))
        elif isinstance(model, dict):
            self.send_json(model)
        else:
            raise TypeError("pipe_model_or_dict_required")

    def receive_request(self) -> SessionIpcRequest:
        return SessionIpcRequest.model_validate(self.receive_json())

    def receive_response(self) -> SessionIpcResponse:
        return SessionIpcResponse.model_validate(self.receive_json())

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _kernel32.CloseHandle(self.handle)

    def _read_exact(self, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            buffer = ctypes.create_string_buffer(remaining)
            read = wintypes.DWORD(0)
            success = _kernel32.ReadFile(self.handle, buffer, remaining, ctypes.byref(read), None)
            if not success:
                error = ctypes.get_last_error()
                if error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED}:
                    raise NamedPipeError("pipe_connection_closed")
                if error == _ERROR_MORE_DATA and read.value:
                    chunks.append(buffer.raw[: read.value])
                    remaining -= read.value
                    continue
                raise _win_error("ReadFile")
            if read.value == 0:
                raise FrameError("pipe_frame_truncated")
            chunks.append(buffer.raw[: read.value])
            remaining -= read.value
        return b"".join(chunks)

    def _write_all(self, payload: bytes) -> None:
        offset = 0
        while offset < len(payload):
            chunk = payload[offset:]
            buffer = ctypes.create_string_buffer(chunk)
            written = wintypes.DWORD(0)
            if not _kernel32.WriteFile(self.handle, buffer, len(chunk), ctypes.byref(written), None):
                error = ctypes.get_last_error()
                if error in {_ERROR_BROKEN_PIPE, _ERROR_NO_DATA, _ERROR_PIPE_NOT_CONNECTED}:
                    raise NamedPipeError("pipe_connection_closed")
                raise _win_error("WriteFile")
            if written.value == 0:
                raise NamedPipeError("pipe_write_no_progress")
            offset += written.value


class NamedPipeServer:
    """One-client-at-a-time Named Pipe server with OS peer authentication."""

    def __init__(self, name: str, peer_policy: PipePeerPolicy) -> None:
        _require_windows()
        self.name = _valid_pipe_name(name)
        self.peer_policy = peer_policy
        self._closed = False
        self._handle: HANDLE | None = None

    def serve_once(
        self,
        handler: Callable[[SessionIpcRequest, SessionPeer], SessionIpcResponse],
        *,
        stop_event: Event | None = None,
    ) -> None:
        """Accept one authenticated client and serve requests until it disconnects."""

        self._handle = self._create_handle()
        try:
            if stop_event is not None and stop_event.is_set():
                return
            connected = _kernel32.ConnectNamedPipe(self._handle, None)
            if not connected and ctypes.get_last_error() != _ERROR_PIPE_CONNECTED:
                raise _win_error("ConnectNamedPipe")
            peer = self._authenticate_client(self._handle)
            connection = NamedPipeConnection(self._handle, max_frame_bytes=self.peer_policy.max_frame_bytes)
            while stop_event is None or not stop_event.is_set():
                try:
                    payload = connection.receive_request()
                except NamedPipeError as error:
                    if str(error) == "pipe_connection_closed":
                        break
                    raise
                response = handler(payload, peer)
                connection.send_model(response)
        finally:
            if self._handle:
                _kernel32.DisconnectNamedPipe(self._handle)
                _kernel32.CloseHandle(self._handle)
                self._handle = None

    def serve_forever(
        self,
        handler: Callable[[SessionIpcRequest, SessionPeer], SessionIpcResponse],
        *,
        stop_event: Event,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        while not stop_event.is_set() and not self._closed:
            try:
                self.serve_once(handler, stop_event=stop_event)
            except (NamedPipeError, ValueError) as error:
                if stop_event.is_set() or self._closed:
                    break
                if on_error is None:
                    raise
                on_error(error)
                time.sleep(0.05)

    def close(self) -> None:
        self._closed = True
        if self._handle:
            _kernel32.CloseHandle(self._handle)
            self._handle = None

    def _create_handle(self) -> HANDLE:
        with _SecurityDescriptor(self.peer_policy.trusted_sids) as security:
            # The security descriptor is copied by CreateNamedPipeW while the
            # call runs; retaining the server handle is enough afterwards.
            handle = _kernel32.CreateNamedPipeW(
                self.name,
                _PIPE_ACCESS_DUPLEX,
                _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT | _PIPE_REJECT_REMOTE_CLIENTS,
                1,
                self.peer_policy.max_frame_bytes,
                self.peer_policy.max_frame_bytes,
                0,
                ctypes.byref(security.attributes),
            )
        if not handle or _handle_value(handle) == _INVALID_HANDLE_VALUE:
            raise _win_error("CreateNamedPipeW")
        return handle

    def _authenticate_client(self, handle: HANDLE) -> SessionPeer:
        process_id = wintypes.ULONG(0)
        if not _kernel32.GetNamedPipeClientProcessId(handle, ctypes.byref(process_id)):
            raise _win_error("GetNamedPipeClientProcessId")
        actual_sid = process_user_sid(int(process_id.value))
        if actual_sid.casefold() not in {sid.casefold() for sid in self.peer_policy.trusted_sids}:
            raise NamedPipeError("pipe_peer_sid_mismatch")
        actual_session_id = process_session_id(int(process_id.value))
        if actual_session_id not in self.peer_policy.allowed_session_ids:
            raise NamedPipeError("pipe_peer_windows_session_mismatch")
        return SessionPeer(
            peer_id=self.peer_policy.peer_id,
            peer_kind=self.peer_policy.peer_kind,
            process_id=int(process_id.value),
            windows_session_id=actual_session_id,
            user_sid=None if self.peer_policy.peer_kind == "machine_service" else actual_sid,
        )


class NamedPipeClient:
    """Connect to a server pipe and preserve the configured frame limit."""

    def __init__(self, name: str, *, max_frame_bytes: int = MAX_FRAME_BYTES) -> None:
        _require_windows()
        if max_frame_bytes < 1024 or max_frame_bytes > MAX_FRAME_BYTES:
            raise ValueError("pipe_max_frame_bytes_invalid")
        self.name = _valid_pipe_name(name)
        self.max_frame_bytes = max_frame_bytes

    def connect(self, *, timeout_seconds: float = 10.0) -> NamedPipeConnection:
        if timeout_seconds <= 0:
            raise ValueError("pipe_connect_timeout_invalid")
        deadline = time.monotonic() + timeout_seconds
        while True:
            handle = _kernel32.CreateFileW(
                self.name,
                _GENERIC_READ | _GENERIC_WRITE,
                0,
                None,
                _OPEN_EXISTING,
                _FILE_ATTRIBUTE_NORMAL,
                None,
            )
            if handle and _handle_value(handle) != _INVALID_HANDLE_VALUE:
                mode = wintypes.DWORD(_PIPE_READMODE_BYTE)
                if not _kernel32.SetNamedPipeHandleState(handle, ctypes.byref(mode), None, None):
                    _kernel32.CloseHandle(handle)
                    raise _win_error("SetNamedPipeHandleState")
                return NamedPipeConnection(handle, max_frame_bytes=self.max_frame_bytes)
            error = ctypes.get_last_error()
            if error not in {_ERROR_PIPE_BUSY, _ERROR_FILE_NOT_FOUND, _ERROR_SEM_TIMEOUT} or time.monotonic() >= deadline:
                raise _win_error("CreateFileW.pipe")
            _kernel32.WaitNamedPipeW(self.name, 100)

    def request(self, request: SessionIpcRequest, *, timeout_seconds: float = 10.0) -> SessionIpcResponse:
        connection = self.connect(timeout_seconds=timeout_seconds)
        try:
            connection.send_model(request)
            return connection.receive_response()
        finally:
            connection.close()


__all__ = [
    "FrameError",
    "JsonFrameCodec",
    "NamedPipeClient",
    "NamedPipeConnection",
    "NamedPipeError",
    "NamedPipeServer",
    "NamedPipeUnavailable",
    "PipePeerPolicy",
    "current_user_sid",
    "process_session_id",
    "process_user_sid",
]
