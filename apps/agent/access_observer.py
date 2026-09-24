"""Normalized file and network observation adapters for P5-06.

The platform does not pretend that parsing a caller-supplied list is an OS
trace.  Real ETW/eBPF/container bridges must feed this recorder with events
marked as ``system`` and remain responsible for process-tree scoping.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from typing import Any, Callable, Iterable, Mapping

try:
    from .information_boundary import AccessObservationCapture, FileObservation, NetworkObservation
except ImportError:  # Support direct development execution.
    from information_boundary import AccessObservationCapture, FileObservation, NetworkObservation


class AccessTraceError(ValueError):
    """Stable error family for invalid or unsupported trace events."""


class AccessTraceEventNormalizer:
    """Convert bridge-neutral JSON events into audit domain observations."""

    FILE_TYPES = {"file.read": "read", "file.write": "write", "file.read_write": "read_write"}
    NETWORK_TYPES = {"network.connect", "network.accept"}

    @classmethod
    def normalize(cls, event: Mapping[str, Any], workspace_path: str) -> FileObservation | NetworkObservation:
        event_type = str(event.get("type") or "")
        source = str(event.get("observation_source", "system"))
        if event_type in cls.FILE_TYPES:
            raw_path = event.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                raise AccessTraceError("trace_file_path_required")
            path = raw_path if os.path.isabs(raw_path) else os.path.join(workspace_path, raw_path)
            return FileObservation(
                path=path,
                access_mode=cls.FILE_TYPES[event_type],  # type: ignore[arg-type]
                observation_source=source,  # type: ignore[arg-type]
                available_at=cls._parse_datetime(event.get("available_at")),
                data_time_end=cls._parse_datetime(event.get("data_time_end")),
            )
        if event_type in cls.NETWORK_TYPES:
            host = event.get("host") or event.get("remote_host")
            if not isinstance(host, str) or not host:
                raise AccessTraceError("trace_network_host_required")
            return NetworkObservation(
                host=host,
                port=event.get("port"),
                protocol=str(event.get("protocol", "tcp")),
                direction="ingress" if event_type == "network.accept" else str(event.get("direction", "egress")),  # type: ignore[arg-type]
                observation_source=source,  # type: ignore[arg-type]
            )
        raise AccessTraceError(f"trace_event_type_unsupported:{event_type or 'missing'}")

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if not isinstance(value, str):
            raise AccessTraceError("trace_datetime_invalid")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise AccessTraceError("trace_datetime_invalid") from error
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class AccessObservationRecorder:
    """Thread-safe sink used by ETW/eBPF/container bridge callbacks."""

    def __init__(self) -> None:
        self._workspace_path: str | None = None
        self._process_id: str | None = None
        self._files: list[FileObservation] = []
        self._network: list[NetworkObservation] = []
        self._errors: list[str] = []
        self._lock = threading.Lock()

    def start(self, workspace_path: str, process_id: str | None = None) -> None:
        if not isinstance(workspace_path, str) or not workspace_path.strip():
            raise AccessTraceError("trace_workspace_required")
        with self._lock:
            self._workspace_path = os.path.realpath(os.path.abspath(os.path.expanduser(workspace_path)))
            self._process_id = str(process_id) if process_id is not None else None
            self._files.clear()
            self._network.clear()
            self._errors.clear()

    def feed(self, event: str | Mapping[str, Any]) -> None:
        with self._lock:
            workspace_path = self._workspace_path
        if workspace_path is None:
            raise AccessTraceError("trace_recorder_not_started")
        try:
            parsed = json.loads(event) if isinstance(event, str) else dict(event)
            if not isinstance(parsed, Mapping):
                raise AccessTraceError("trace_event_must_be_object")
            event_process_id = parsed.get("process_id")
            if self._process_id is not None and event_process_id is not None and str(event_process_id) != self._process_id:
                return
            observation = AccessTraceEventNormalizer.normalize(parsed, workspace_path)
        except Exception as error:
            with self._lock:
                self._errors.append(str(error))
            raise
        with self._lock:
            if isinstance(observation, FileObservation):
                self._files.append(observation)
            else:
                self._network.append(observation)

    def stop(self) -> AccessObservationCapture:
        with self._lock:
            if self._workspace_path is None:
                return AccessObservationCapture("not_captured", reason="trace_recorder_not_started")
            files = tuple(self._files)
            network = tuple(self._network)
            errors = tuple(self._errors)
            self._workspace_path = None
            self._process_id = None
        if errors:
            return AccessObservationCapture("partial", files, network, ";".join(errors[:10]))
        if not files and not network:
            return AccessObservationCapture("not_captured", reason="trace_recorder_returned_no_events")
        return AccessObservationCapture("captured", files, network)


class JsonlAccessObservationProvider:
    """Adapter for an external ETW/eBPF/strace bridge that emits JSONL."""

    def __init__(self, event_source: Callable[[], Iterable[str | Mapping[str, Any]]]) -> None:
        self.event_source = event_source
        self.recorder = AccessObservationRecorder()

    def start(self, workspace_path: str, process_id: str | None = None) -> None:
        self.recorder.start(workspace_path, process_id)

    def stop(self) -> AccessObservationCapture:
        try:
            events: Iterable[str | Mapping[str, Any]] = self.event_source()
            for event in events:
                self.recorder.feed(event)
        except Exception as error:
            capture = self.recorder.stop()
            return AccessObservationCapture(
                "partial" if capture.file_observations or capture.network_observations else "not_captured",
                capture.file_observations,
                capture.network_observations,
                f"trace_event_source_failed:{error}",
            )
        return self.recorder.stop()


__all__ = [
    "AccessObservationRecorder",
    "AccessTraceError",
    "AccessTraceEventNormalizer",
    "JsonlAccessObservationProvider",
]
