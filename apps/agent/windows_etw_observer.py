"""Windows ETW access observation adapter for P5-06-REAL.

The adapter uses the Windows inbox ``logman`` and ``tracerpt`` tools instead
of a Python ETW package. Kernel events are collected to an ETL file,
converted to CSV after the child process exits, filtered to the Runner
process tree, and normalized into the platform observation contracts.

This module does not claim OS-level blocking. Failure to start, stop, parse,
or bind a process is reported as ``not_captured``/``partial`` so callers can
keep the information-boundary audit fail-closed.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol, Sequence

try:
    from .information_boundary import AccessObservationCapture, FileObservation, NetworkObservation
except ImportError:  # Support direct development execution.
    from information_boundary import AccessObservationCapture, FileObservation, NetworkObservation


class WindowsEtwError(RuntimeError):
    """Stable error family for unavailable or malformed Windows ETW capture."""


class EtwCommandRunner(Protocol):
    def run(self, command: Sequence[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]: ...


class SubprocessEtwCommandRunner:
    """Run ETW utilities without a shell and retain diagnostics."""

    def run(self, command: Sequence[str], timeout_seconds: float) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            list(command),
            shell=False,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )


class ProcessTreeProvider(Protocol):
    def pids_for(self, root_pid: int) -> set[int]: ...


class WindowsProcessTree:
    """Enumerate a Windows process tree through Toolhelp32Snapshot."""

    def pids_for(self, root_pid: int) -> set[int]:
        if os.name != "nt":
            return {root_pid}
        try:
            import ctypes
            from ctypes import wintypes

            class ProcessEntry32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
            if snapshot == ctypes.c_void_p(-1).value:
                return {root_pid}
            try:
                entry = ProcessEntry32W()
                entry.dwSize = ctypes.sizeof(ProcessEntry32W)
                parent_by_pid: dict[int, int] = {}
                first = kernel32.Process32FirstW
                first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
                first.restype = wintypes.BOOL
                next_process = kernel32.Process32NextW
                next_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
                next_process.restype = wintypes.BOOL
                if first(snapshot, ctypes.byref(entry)):
                    while True:
                        parent_by_pid[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                        if not next_process(snapshot, ctypes.byref(entry)):
                            break
                result = {root_pid}
                changed = True
                while changed:
                    changed = False
                    for pid, parent in parent_by_pid.items():
                        if parent in result and pid not in result:
                            result.add(pid)
                            changed = True
                return result
            finally:
                kernel32.CloseHandle(snapshot)
        except Exception:
            # A missing privilege or a short-lived process must not widen the
            # observation scope to unrelated processes.
            return {root_pid}


@dataclass(frozen=True)
class WindowsEtwConfig:
    """Collection settings kept explicit for Manifest/debugging."""

    file_keywords: int = 0x3F0
    network_keywords: int = 0x30
    level: int = 0x04
    command_timeout_seconds: float = 30.0
    output_directory: str | None = None
    include_network: bool = True

    def __post_init__(self) -> None:
        if self.file_keywords < 0 or self.network_keywords < 0:
            raise ValueError("windows_etw_keywords_invalid")
        if self.level < 0:
            raise ValueError("windows_etw_level_invalid")
        if self.command_timeout_seconds <= 0:
            raise ValueError("windows_etw_command_timeout_invalid")


@dataclass(frozen=True)
class _TraceSession:
    name: str
    etl_path: Path
    csv_path: Path
    temporary_directory: Path | None


class WindowsEtwAccessObservationProvider:
    """Capture file/network ETW events for one Runner process tree."""

    FILE_PROVIDER = "Microsoft-Windows-Kernel-File"
    NETWORK_PROVIDER = "Microsoft-Windows-Kernel-Network"

    def __init__(
        self,
        config: WindowsEtwConfig | None = None,
        *,
        command_runner: EtwCommandRunner | None = None,
        process_tree: ProcessTreeProvider | None = None,
        is_windows: Callable[[], bool] | None = None,
        which: Callable[[str], str | None] | None = None,
    ) -> None:
        self.config = config or WindowsEtwConfig()
        self.command_runner = command_runner or SubprocessEtwCommandRunner()
        self.process_tree = process_tree or WindowsProcessTree()
        self.is_windows = is_windows or (lambda: os.name == "nt")
        self.which = which or shutil.which
        self._sessions: list[_TraceSession] = []
        self._workspace_path: str | None = None
        self._root_pid: int | None = None
        self._bound_pids: set[int] = set()
        self._binding_error: str | None = None
        self._tree_lock = threading.Lock()
        self._tree_stop = threading.Event()
        self._tree_thread: threading.Thread | None = None

    def start(self, workspace_path: str, process_id: str | None = None) -> None:
        if not self.is_windows():
            raise WindowsEtwError("windows_etw_requires_windows")
        if self._sessions:
            raise WindowsEtwError("windows_etw_already_started")
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            raise WindowsEtwError("windows_etw_workspace_required")
        workspace = os.path.realpath(os.path.abspath(os.path.expanduser(workspace_path)))
        output_directory: Path
        temporary_directory: Path | None = None
        if self.config.output_directory:
            output_directory = Path(self.config.output_directory).expanduser().resolve()
            output_directory.mkdir(parents=True, exist_ok=True)
        else:
            temporary_directory = Path(tempfile.mkdtemp(prefix="math-agent-etw-"))
            output_directory = temporary_directory
        token = uuid.uuid4().hex[:12]
        name = f"MathAgentAccess-{os.getpid()}-{token}"
        try:
            logman = self._tool("logman")
        except Exception:
            self._cleanup_directory(temporary_directory)
            raise
        provider_specs = [("file", self.FILE_PROVIDER, self.config.file_keywords)]
        if self.config.include_network:
            provider_specs.append(("network", self.NETWORK_PROVIDER, self.config.network_keywords))
        sessions: list[_TraceSession] = []
        for suffix, provider, keywords in provider_specs:
            session_name = f"{name}-{suffix}"
            etl_path = output_directory / f"{session_name}.etl"
            csv_path = output_directory / f"{session_name}.csv"
            command = [
                logman,
                "create",
                "trace",
                session_name,
                "-o",
                str(etl_path),
                "-p",
                provider,
                f"0x{keywords:X}",
                f"0x{self.config.level:X}",
                "-ets",
                "-y",
            ]
            result = self.command_runner.run(command, self.config.command_timeout_seconds)
            if result.returncode != 0:
                for active in sessions:
                    try:
                        self.command_runner.run([logman, "stop", active.name, "-ets"], self.config.command_timeout_seconds)
                        self.command_runner.run([logman, "delete", active.name], self.config.command_timeout_seconds)
                    except Exception:
                        pass
                self._cleanup_directory(temporary_directory)
                raise WindowsEtwError(self._command_error("windows_etw_start_failed", result))
            sessions.append(_TraceSession(session_name, etl_path, csv_path, temporary_directory))
        self._sessions = sessions
        self._workspace_path = workspace
        self._root_pid = None
        self._bound_pids = set()
        self._binding_error = None
        if process_id is not None:
            self.bind_process(process_id)

    def bind_process(self, process_id: int | str | None) -> None:
        if not self._sessions:
            raise WindowsEtwError("windows_etw_not_started")
        try:
            pid = int(process_id) if process_id is not None else 0
        except (TypeError, ValueError) as error:
            self._binding_error = "windows_etw_process_id_invalid"
            raise WindowsEtwError(self._binding_error) from error
        if pid < 1:
            self._binding_error = "windows_etw_process_binding_missing"
            raise WindowsEtwError(self._binding_error)
        self._root_pid = pid
        with self._tree_lock:
            self._bound_pids = set(self.process_tree.pids_for(pid))
            self._bound_pids.add(pid)
        self._tree_stop.clear()
        self._tree_thread = threading.Thread(target=self._refresh_process_tree, name="math-agent-etw-pids", daemon=True)
        self._tree_thread.start()

    def stop(self) -> AccessObservationCapture:
        sessions = tuple(self._sessions)
        if not sessions:
            return AccessObservationCapture("not_captured", reason="windows_etw_not_started")
        self._sessions = []
        stop_errors: list[str] = []
        all_files: list[FileObservation] = []
        all_network: list[NetworkObservation] = []
        diagnostics = self._new_diagnostics(sessions)
        try:
            logman = self._tool("logman")
            with self._tree_lock:
                bound_pids = set(self._bound_pids)
            diagnostics["bound_process_ids"] = sorted(bound_pids)
            if self._root_pid is None or not bound_pids:
                diagnostics["stop_status"] = "failed"
                diagnostics["stop_errors"] = [self._binding_error or "windows_etw_process_binding_missing"]
                return AccessObservationCapture(
                    "not_captured",
                    reason=self._binding_error or "windows_etw_process_binding_missing",
                    diagnostics=diagnostics,
                )
            for session in sessions:
                session_diag = diagnostics["sessions"][session.name]
                result = self.command_runner.run([logman, "stop", session.name, "-ets"], self.config.command_timeout_seconds)
                session_diag["stop_returncode"] = result.returncode
                if result.returncode != 0:
                    error = self._command_error("windows_etw_stop_failed", result)
                    stop_errors.append(error)
                    session_diag["stop_error"] = error
                    continue
                tracerpt = self._tool("tracerpt")
                result = self.command_runner.run(
                    [tracerpt, str(session.etl_path), "-o", str(session.csv_path), "-of", "CSV", "-en", "Unicode", "-y", "-lr"],
                    self.config.command_timeout_seconds,
                )
                session_diag["convert_returncode"] = result.returncode
                if result.returncode != 0:
                    error = self._command_error("windows_etw_parse_failed", result)
                    stop_errors.append(error)
                    session_diag["convert_error"] = error
                    continue
                session_diag["etl_sha256"] = self._file_sha256(session.etl_path)
                session_diag["csv_sha256"] = self._file_sha256(session.csv_path)
                session_diag["etl_bytes"] = session.etl_path.stat().st_size if session.etl_path.exists() else None
                session_diag["csv_bytes"] = session.csv_path.stat().st_size if session.csv_path.exists() else None
                files, network, parse_error = self._parse_csv(session.csv_path, bound_pids)
                session_diag["file_events"] = len(files)
                session_diag["network_events"] = len(network)
                all_files.extend(files)
                all_network.extend(network)
                if parse_error is not None:
                    stop_errors.append(parse_error)
                    session_diag["parse_errors"] = parse_error
            files = self._deduplicate_files(all_files)
            network = self._deduplicate_network(all_network)
            diagnostics["file_events"] = len(files)
            diagnostics["network_events"] = len(network)
            if stop_errors:
                reason = ";".join(stop_errors[:10])
                diagnostics["stop_status"] = "failed"
                diagnostics["stop_errors"] = stop_errors[:10]
                return AccessObservationCapture(
                    "partial" if files or network else "not_captured",
                    files,
                    network,
                    reason,
                    diagnostics=diagnostics,
                )
            if not files and not network:
                diagnostics["stop_status"] = "no_scoped_events"
                return AccessObservationCapture("not_captured", reason="windows_etw_no_scoped_events", diagnostics=diagnostics)
            diagnostics["stop_status"] = "ok"
            return AccessObservationCapture("captured", files, network, diagnostics=diagnostics)
        except Exception as error:
            diagnostics["stop_status"] = "failed"
            diagnostics["stop_errors"] = [f"windows_etw_capture_failed:{error}"[:300]]
            return AccessObservationCapture("not_captured", reason=f"windows_etw_capture_failed:{error}", diagnostics=diagnostics)
        finally:
            self._tree_stop.set()
            if self._tree_thread is not None and self._tree_thread is not threading.current_thread():
                self._tree_thread.join(timeout=1.0)
            self._tree_thread = None
            cleanup_errors: list[str] = []
            try:
                logman = self._tool("logman")
                for session in sessions:
                    result = self.command_runner.run([logman, "delete", session.name], self.config.command_timeout_seconds)
                    if result.returncode != 0:
                        cleanup_errors.append(self._command_error("windows_etw_delete_failed", result))
            except Exception as error:
                cleanup_errors.append(f"windows_etw_cleanup_failed:{error}")
            if cleanup_errors:
                diagnostics["cleanup_errors"] = cleanup_errors[:10]
            diagnostics["cleanup_status"] = "ok" if not cleanup_errors else "failed"
            diagnostics["finished_at"] = datetime.now(UTC).isoformat()
            self._workspace_path = None
            self._root_pid = None
            with self._tree_lock:
                self._bound_pids = set()
            self._binding_error = None
            self._cleanup_directory(sessions[0].temporary_directory)

    def _new_diagnostics(self, sessions: Sequence[_TraceSession]) -> dict[str, object]:
        return {
            "schema_version": "1.0",
            "adapter": "windows-etw",
            "started_at": datetime.now(UTC).isoformat(),
            "file_provider": self.FILE_PROVIDER,
            "file_keywords": f"0x{self.config.file_keywords:X}",
            "network_provider": self.NETWORK_PROVIDER if self.config.include_network else None,
            "network_keywords": f"0x{self.config.network_keywords:X}" if self.config.include_network else None,
            "level": f"0x{self.config.level:X}",
            "command_timeout_seconds": self.config.command_timeout_seconds,
            "sessions": {
                session.name: {
                    "provider": self.FILE_PROVIDER if session.name.endswith("-file") else self.NETWORK_PROVIDER,
                    "etl_path": str(session.etl_path),
                    "csv_path": str(session.csv_path),
                    "retained": session.temporary_directory is None,
                }
                for session in sessions
            },
        }

    @staticmethod
    def _file_sha256(path: Path) -> str | None:
        try:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()
        except OSError:
            return None

    @staticmethod
    def _deduplicate_files(items: Sequence[FileObservation]) -> tuple[FileObservation, ...]:
        seen: set[tuple[str, str]] = set()
        result: list[FileObservation] = []
        for item in items:
            key = (item.path, item.access_mode)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return tuple(result)

    @staticmethod
    def _deduplicate_network(items: Sequence[NetworkObservation]) -> tuple[NetworkObservation, ...]:
        seen: set[tuple[str, int | None, str, str]] = set()
        result: list[NetworkObservation] = []
        for item in items:
            key = (item.host, item.port, item.protocol, item.direction)
            if key not in seen:
                seen.add(key)
                result.append(item)
        return tuple(result)

    def _refresh_process_tree(self) -> None:
        while not self._tree_stop.wait(0.1):
            root_pid = self._root_pid
            if root_pid is None:
                continue
            try:
                observed = self.process_tree.pids_for(root_pid)
            except Exception:
                continue
            with self._tree_lock:
                self._bound_pids.update(observed)

    def _parse_csv(self, csv_path: Path, bound_pids: set[int]) -> tuple[tuple[FileObservation, ...], tuple[NetworkObservation, ...], str | None]:
        if not csv_path.exists():
            return (), (), "windows_etw_csv_missing"
        files: list[FileObservation] = []
        network: list[NetworkObservation] = []
        errors: list[str] = []
        try:
            with csv_path.open("r", encoding="utf-16", errors="replace", newline="") as handle:
                for row in csv.DictReader(handle):
                    try:
                        self._parse_row(row, files, network, bound_pids)
                    except Exception as error:
                        errors.append(str(error))
        except UnicodeError:
            try:
                with csv_path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                    for row in csv.DictReader(handle):
                        try:
                            self._parse_row(row, files, network, bound_pids)
                        except Exception as error:
                            errors.append(str(error))
            except Exception as error:
                errors.append(str(error))
        except Exception as error:
            errors.append(str(error))
        return tuple(files), tuple(network), ";".join(errors[:10]) if errors else None

    def _parse_row(self, row: Mapping[str, str], files: list[FileObservation], network: list[NetworkObservation], bound_pids: set[int]) -> None:
        pid = self._pid(row)
        if pid is None or pid not in bound_pids:
            return
        event_text = " ".join(str(value or "") for value in row.values()).lower()
        provider = self._field(row, "provider", "provider name", "providername").lower()
        if self.FILE_PROVIDER.lower() in provider or "fileio" in event_text or "file io" in event_text:
            path = self._field(row, "path", "filename", "file name", "name", "file")
            if not path:
                path = self._extract_file_path(self._field(row, "description", "event description", "payload"))
            if path:
                files.append(FileObservation(self._normalise_path(path), self._file_access_mode(event_text), "system"))
        if self.config.include_network and (
            self.NETWORK_PROVIDER.lower() in provider or "network" in event_text or "tcp" in event_text or "udp" in event_text
        ):
            host = self._field(row, "remote host", "remotehost", "destination address", "destinationaddress", "remote address", "remoteaddress", "host", "address")
            if not host:
                host = self._extract_host(event_text)
            if host:
                network.append(
                    NetworkObservation(
                        host=host,
                        port=self._port(row, event_text),
                        protocol=self._field(row, "protocol", "transport") or self._protocol(event_text),
                        direction="egress",
                        observation_source="system",
                    )
                )

    @staticmethod
    def _field(row: Mapping[str, str], *names: str) -> str:
        normalized = {str(key).strip().lower().replace("_", " "): str(value or "").strip() for key, value in row.items()}
        for name in names:
            value = normalized.get(name.lower().replace("_", " "))
            if value:
                return value
        return ""

    def _pid(self, row: Mapping[str, str]) -> int | None:
        raw = self._field(row, "process id", "processid", "pid", "process")
        if not raw:
            return None
        match = re.search(r"0x[0-9a-f]+|[0-9]+", raw, re.IGNORECASE)
        if not match:
            return None
        value = match.group(0)
        return int(value, 16) if value.lower().startswith("0x") else int(value)

    @staticmethod
    def _file_access_mode(event_text: str) -> str:
        if "write" in event_text or "rename" in event_text or "delete" in event_text:
            return "write"
        return "read"

    @staticmethod
    def _port(row: Mapping[str, str], event_text: str) -> int | None:
        raw = WindowsEtwAccessObservationProvider._field(row, "remote port", "remoteport", "destination port", "destinationport", "port")
        if not raw:
            match = re.search(r":(\d{1,5})(?:\s|$)", event_text)
            raw = match.group(1) if match else ""
        try:
            value = int(raw)
            return value if 0 <= value <= 65535 else None
        except ValueError:
            return None

    @staticmethod
    def _protocol(event_text: str) -> str:
        return "udp" if "udp" in event_text else "tcp"

    @staticmethod
    def _extract_host(event_text: str) -> str:
        match = re.search(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])", event_text)
        return match.group(0) if match else ""

    @staticmethod
    def _extract_file_path(description: str) -> str:
        if not description:
            return ""
        match = re.search(
            r"(?:[A-Za-z]:\\[^\";,|]+|\\\\Device\\[^\";,|]+|\\\\\?\\[^\";,|]+)",
            description,
        )
        return match.group(0).strip() if match else ""

    def _normalise_path(self, path: str) -> str:
        cleaned = path.strip().strip('"')
        if cleaned.startswith("\\\\?\\"):
            cleaned = cleaned[4:]
        if cleaned.startswith("\\Device\\"):
            for drive in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                device = self._query_dos_device(f"{drive}:")
                if device and cleaned.lower().startswith(device.lower()):
                    cleaned = f"{drive}:" + cleaned[len(device):]
                    break
        if self._workspace_path and not os.path.isabs(cleaned):
            cleaned = os.path.join(self._workspace_path, cleaned)
        return os.path.realpath(os.path.abspath(cleaned))

    @staticmethod
    def _query_dos_device(drive: str) -> str | None:
        if os.name != "nt":
            return None
        try:
            import ctypes

            buffer = ctypes.create_unicode_buffer(1024)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            length = kernel32.QueryDosDeviceW(drive, buffer, len(buffer))
            return buffer.value if length else None
        except Exception:
            return None

    def _tool(self, name: str) -> str:
        value = self.which(name) or self.which(f"{name}.exe")
        if not value:
            raise WindowsEtwError(f"windows_etw_tool_not_found:{name}")
        return value

    @staticmethod
    def _command_error(prefix: str, result: subprocess.CompletedProcess[str]) -> str:
        detail = (result.stderr or result.stdout or "").strip().replace("\r", " ").replace("\n", " ")
        return f"{prefix}:{result.returncode}:{detail[:300]}"

    @staticmethod
    def _cleanup_directory(directory: Path | None) -> None:
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "EtwCommandRunner",
    "ProcessTreeProvider",
    "SubprocessEtwCommandRunner",
    "WindowsEtwAccessObservationProvider",
    "WindowsEtwConfig",
    "WindowsEtwError",
    "WindowsProcessTree",
]
