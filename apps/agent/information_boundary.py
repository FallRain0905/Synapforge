"""Information-boundary audit contracts for reproducible Agent runs.

The module is deliberately independent from an operating-system tracing
backend.  A tracing adapter can provide ``FileObservation`` values later; the
audit itself remains deterministic and fail-closed when required observations
are missing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Mapping, Protocol, Sequence


AccessMode = Literal["read", "write", "read_write"]
ObservationSource = Literal["system", "declared", "imported", "unknown"]
NetworkDirection = Literal["egress", "ingress"]


@dataclass(frozen=True)
class FileObservation:
    path: str
    access_mode: AccessMode = "read"
    observation_source: ObservationSource = "system"
    available_at: datetime | None = None
    data_time_end: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("file_observation_path_required")
        if self.access_mode not in {"read", "write", "read_write"}:
            raise ValueError("file_observation_access_mode_invalid")
        if self.observation_source not in {"system", "declared", "imported", "unknown"}:
            raise ValueError("file_observation_source_invalid")


@dataclass(frozen=True)
class NetworkObservation:
    """One normalized network connection observed during a Run."""

    host: str
    port: int | None = None
    protocol: str = "tcp"
    direction: NetworkDirection = "egress"
    observation_source: ObservationSource = "system"

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host.strip():
            raise ValueError("network_observation_host_required")
        if self.port is not None and (not isinstance(self.port, int) or isinstance(self.port, bool) or not 0 <= self.port <= 65535):
            raise ValueError("network_observation_port_invalid")
        if not isinstance(self.protocol, str) or not self.protocol.strip():
            raise ValueError("network_observation_protocol_invalid")
        if self.direction not in {"egress", "ingress"}:
            raise ValueError("network_observation_direction_invalid")
        if self.observation_source not in {"system", "declared", "imported", "unknown"}:
            raise ValueError("network_observation_source_invalid")


class FileAccessObservationProvider(Protocol):
    """Future OS-specific adapter contract."""

    def start(self, workspace_path: str) -> None: ...

    def stop(self) -> Sequence[FileObservation]: ...


@dataclass(frozen=True)
class ObservationCapture:
    status: Literal["captured", "partial", "not_captured"]
    observations: tuple[FileObservation, ...]
    reason: str | None = None


class FailClosedObservationController:
    """Run an optional provider without treating unavailable tracing as success."""

    @staticmethod
    def capture(provider: FileAccessObservationProvider | None, workspace_path: str, *, required: bool) -> ObservationCapture:
        if provider is None:
            return ObservationCapture("not_captured", (), "file_access_observer_unavailable") if required else ObservationCapture("not_captured", ())
        try:
            provider.start(workspace_path)
            observations = tuple(provider.stop())
        except Exception as error:
            return ObservationCapture("not_captured", (), f"file_access_observer_failed:{error}")
        if required and not observations:
            return ObservationCapture("not_captured", (), "file_access_observer_returned_no_observations")
        return ObservationCapture("captured", observations)


@dataclass(frozen=True)
class AccessObservationCapture:
    """Combined file/network capture returned by a P5-06 adapter."""

    status: Literal["captured", "partial", "not_captured"]
    file_observations: tuple[FileObservation, ...] = ()
    network_observations: tuple[NetworkObservation, ...] = ()
    reason: str | None = None
    # Per-run adapter diagnostics such as trace session names, provider
    # configuration, trace file hashes, and command return codes.  Diagnostics
    # are audit evidence only and are excluded from the reproducibility hash:
    # trace artefacts legitimately differ between otherwise identical runs.
    diagnostics: Mapping[str, Any] | None = None


class AccessObservationProvider(Protocol):
    """Process-scoped file/network observation adapter boundary."""

    def start(self, workspace_path: str, process_id: str | None = None) -> None: ...

    def stop(self) -> AccessObservationCapture: ...


class FailClosedAccessObservationController:
    """Invoke a combined observer without treating missing tracing as success."""

    @staticmethod
    def capture(
        provider: AccessObservationProvider | None,
        workspace_path: str,
        *,
        process_id: str | None = None,
        required_files: bool,
        required_network: bool,
    ) -> AccessObservationCapture:
        if provider is None:
            if required_files or required_network:
                return AccessObservationCapture("not_captured", reason="access_observer_unavailable")
            return AccessObservationCapture("not_captured")
        try:
            provider.start(workspace_path, process_id)
            capture = provider.stop()
        except Exception as error:
            return AccessObservationCapture("not_captured", reason=f"access_observer_failed:{error}")
        if not isinstance(capture, AccessObservationCapture):
            return AccessObservationCapture("not_captured", reason="access_observer_invalid_capture")
        if required_files and not capture.file_observations:
            return AccessObservationCapture(
                "not_captured",
                capture.file_observations,
                capture.network_observations,
                "file_access_observations_missing",
            )
        if required_network and not capture.network_observations:
            return AccessObservationCapture(
                "not_captured",
                capture.file_observations,
                capture.network_observations,
                "network_access_observations_missing",
            )
        return capture


class InformationBoundaryAudit:
    """Evaluate path, availability-time, and dataset-time boundaries."""

    @classmethod
    def evaluate(
        cls,
        workspace_path: str,
        declared_inputs: Sequence[str],
        observations: Sequence[FileObservation],
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        policy = dict(policy or {})
        workspace = cls._normalise(workspace_path)
        declared = [str(value) for value in declared_inputs]
        declared_map = {
            cls._normalise(value if os.path.isabs(value) else os.path.join(workspace, value)): value
            for value in declared
        }
        decision_time = cls._parse_datetime(policy.get("decision_time"))
        data_cutoff = cls._parse_datetime(policy.get("data_cutoff"))
        allow_future_data = bool(policy.get("allow_future_data", False))
        require_temporal_metadata = bool(policy.get("require_temporal_metadata", False))
        observation_mode = str(policy.get("observation_mode", "declared"))
        runtime_roots = cls._runtime_dependency_roots(policy)
        violations: list[dict[str, Any]] = []
        observed_records: list[dict[str, Any]] = []
        undeclared: list[str] = []
        outside_workspace: list[str] = []
        future_data: list[str] = []
        runtime_dependencies: list[str] = []
        reads = [item for item in observations if item.access_mode in {"read", "read_write"}]

        for item in reads:
            normalised = cls._normalise(item.path if os.path.isabs(item.path) else os.path.join(workspace, item.path))
            is_runtime_dependency = cls._inside_any(normalised, runtime_roots) and normalised not in declared_map
            record = {
                "path": item.path,
                "normalised_path": normalised,
                "access_mode": item.access_mode,
                "observation_source": item.observation_source,
                "available_at": item.available_at.isoformat() if item.available_at else None,
                "data_time_end": item.data_time_end.isoformat() if item.data_time_end else None,
                "category": "runtime_dependency" if is_runtime_dependency else "task_input",
            }
            observed_records.append(record)
            if is_runtime_dependency:
                # Interpreter/library/temp reads are tool runtime dependencies,
                # not competition inputs: they are recorded but do not require
                # task-level declaration and do not constitute data leakage.
                runtime_dependencies.append(item.path)
                continue
            if not cls._inside(normalised, workspace):
                outside_workspace.append(item.path)
                violations.append({"severity": "fatal", "code": "observed_input_outside_workspace", "path": item.path})
                continue
            if normalised not in declared_map:
                undeclared.append(item.path)
                violations.append({"severity": "major", "code": "undeclared_input_file", "path": item.path})
            if require_temporal_metadata and item.available_at is None and item.data_time_end is None:
                violations.append({"severity": "major", "code": "temporal_metadata_missing", "path": item.path})
            if not allow_future_data and decision_time is not None and item.available_at is not None and item.available_at > decision_time:
                future_data.append(item.path)
                violations.append({"severity": "fatal", "code": "future_data_available_after_decision", "path": item.path})
            if not allow_future_data and data_cutoff is not None and item.data_time_end is not None and item.data_time_end > data_cutoff:
                if item.path not in future_data:
                    future_data.append(item.path)
                violations.append({"severity": "fatal", "code": "future_data_beyond_cutoff", "path": item.path})

        capture_status = "captured" if reads else "not_captured"
        if observation_mode == "system" and declared_inputs and not reads:
            violations.append({"severity": "major", "code": "input_observation_not_captured"})
        elif observation_mode == "system" and reads and any(item.observation_source != "system" for item in reads):
            capture_status = "partial"
            violations.append({"severity": "major", "code": "system_observation_incomplete"})
        elif observation_mode == "not_captured" and declared_inputs:
            violations.append({"severity": "major", "code": "input_observation_not_captured"})

        return {
            "schema_version": "1.1",
            "allowed": not any(item["severity"] in {"fatal", "major"} for item in violations),
            "audit_status": capture_status,
            "declared": declared,
            "observed": observed_records,
            "undeclared": undeclared,
            "outside_workspace": outside_workspace,
            "future_data": future_data,
            "runtime_dependencies": runtime_dependencies,
            "violations": violations,
        }

    @staticmethod
    def _runtime_dependency_roots(policy: Mapping[str, Any]) -> list[str]:
        """Normalised roots that a policy declares as tool runtime dependencies."""

        roots: list[str] = []
        for raw in policy.get("runtime_dependency_roots", ()) or ():
            if isinstance(raw, str) and raw.strip():
                expanded = InformationBoundaryAudit._normalise(
                    os.path.expanduser(os.path.expandvars(raw.strip()))
                )
                if expanded not in roots:
                    roots.append(expanded)
        return roots

    @classmethod
    def _inside_any(cls, path: str, roots: Sequence[str]) -> bool:
        return any(cls._inside(path, root) for root in roots)

    @classmethod
    def observations_from_inputs(
        cls,
        workspace_path: str,
        observed_input_files: Sequence[str],
        policy: Mapping[str, Any] | None = None,
    ) -> tuple[FileObservation, ...]:
        metadata_by_path: dict[str, Mapping[str, Any]] = {}
        for raw in (policy or {}).get("observed_files", []):
            if not isinstance(raw, Mapping) or not isinstance(raw.get("path"), str):
                continue
            path = str(raw["path"])
            key = cls._normalise(path if os.path.isabs(path) else os.path.join(workspace_path, path))
            metadata_by_path[key] = raw
        result: list[FileObservation] = []
        for path in observed_input_files:
            key = cls._normalise(path if os.path.isabs(path) else os.path.join(workspace_path, path))
            metadata = metadata_by_path.get(key, {})
            result.append(
                FileObservation(
                    path=str(path),
                    access_mode=str(metadata.get("access_mode", "read")),  # type: ignore[arg-type]
                    observation_source=str(metadata.get("observation_source", "unknown")),  # type: ignore[arg-type]
                    available_at=cls._parse_datetime(metadata.get("available_at")),
                    data_time_end=cls._parse_datetime(metadata.get("data_time_end")),
                )
            )
        return tuple(result)

    @classmethod
    def network_observations_from_records(cls, records: Sequence[Any]) -> tuple[NetworkObservation, ...]:
        result: list[NetworkObservation] = []
        for raw in records:
            if isinstance(raw, NetworkObservation):
                result.append(raw)
                continue
            if not isinstance(raw, Mapping):
                continue
            try:
                result.append(
                    NetworkObservation(
                        host=str(raw.get("host") or raw.get("remote_host") or ""),
                        port=raw.get("port"),
                        protocol=str(raw.get("protocol", "tcp")),
                        direction=str(raw.get("direction", "egress")),  # type: ignore[arg-type]
                        observation_source=str(raw.get("observation_source", "system")),  # type: ignore[arg-type]
                    )
                )
            except (TypeError, ValueError):
                continue
        return tuple(result)

    @classmethod
    def evaluate_network(
        cls,
        observations: Sequence[NetworkObservation],
        policy: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        policy = dict(policy or {})
        network_policy = str(policy.get("network_policy", "deny-by-default"))
        allowed_hosts = {str(value).strip().lower() for value in policy.get("allowed_network_hosts", []) if str(value).strip()}
        violations: list[dict[str, Any]] = []
        observed = [
            {
                "host": item.host,
                "port": item.port,
                "protocol": item.protocol,
                "direction": item.direction,
                "observation_source": item.observation_source,
            }
            for item in observations
        ]
        for item in observations:
            host = item.host.strip().lower()
            if network_policy == "deny-by-default" and item.direction == "egress":
                violations.append({"severity": "fatal", "code": "network_egress_not_allowed", "host": item.host, "port": item.port})
            elif network_policy == "allow-listed" and item.direction == "egress" and host not in allowed_hosts:
                violations.append({"severity": "fatal", "code": "network_host_not_allowlisted", "host": item.host, "port": item.port})
            if item.observation_source != "system":
                violations.append({"severity": "major", "code": "system_network_observation_incomplete", "host": item.host, "port": item.port})
        observation_mode = str(policy.get("network_observation_mode", "declared"))
        if observation_mode == "system" and not observations:
            violations.append({"severity": "major", "code": "network_observation_not_captured"})
        return {
            "schema_version": "1.0",
            "allowed": not any(item["severity"] in {"fatal", "major"} for item in violations),
            "audit_status": "captured" if observations else "not_captured",
            "policy": network_policy,
            "observed": observed,
            "violations": violations,
        }

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)

    @staticmethod
    def _normalise(path: str) -> str:
        return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))

    @classmethod
    def _inside(cls, path: str, root: str) -> bool:
        try:
            return os.path.commonpath([path, cls._normalise(root)]) == cls._normalise(root)
        except ValueError:
            return False


__all__ = [
    "AccessObservationCapture",
    "AccessObservationProvider",
    "FailClosedObservationController",
    "FailClosedAccessObservationController",
    "FileAccessObservationProvider",
    "FileObservation",
    "InformationBoundaryAudit",
    "NetworkObservation",
    "ObservationCapture",
]
