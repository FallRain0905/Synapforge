"""Local output discovery, manifest creation, and resumable Artifact upload."""

from __future__ import annotations

try:
    from .input_fetcher import InputFetchError
except ImportError:  # 直接脚本执行 / 顶层模块导入
    from input_fetcher import InputFetchError  # type: ignore

import hashlib
import json
import mimetypes
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

try:
    from .local_state import LocalAgentState
    from .information_boundary import InformationBoundaryAudit
except ImportError:  # Support direct development execution.
    from local_state import LocalAgentState
    from information_boundary import InformationBoundaryAudit


@dataclass(frozen=True)
class DiscoveredOutput:
    path: str
    name: str
    relative_path: str
    content_hash: str
    size_bytes: int
    mime_type: str
    artifact_type: str = "result_table"


@dataclass(frozen=True)
class UploadResult:
    upload_id: str
    artifact_id: str
    path: str


def _normalise(path: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(str(path)))))


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _artifact_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".svg", ".pdf"}:
        return "figure" if suffix != ".pdf" else "compiled_pdf"
    if suffix in {".py", ".r", ".jl", ".m", ".ipynb"}:
        return "code"
    if suffix in {".md", ".tex", ".docx"}:
        return "paper_source"
    if suffix in {".json", ".csv", ".xlsx", ".xls"}:
        return "result_table"
    return "run_manifest"


class OutputDiscovery:
    def __init__(self, workspace: str | Path) -> None:
        self.workspace = _normalise(workspace)

    def discover(self, paths: Iterable[str | Path]) -> list[DiscoveredOutput]:
        result: list[DiscoveredOutput] = []
        seen: set[str] = set()
        for raw_path in paths:
            path = _normalise(raw_path if os.path.isabs(str(raw_path)) else Path(self.workspace) / str(raw_path))
            if path in seen:
                continue
            seen.add(path)
            if not _inside(path, self.workspace):
                raise ValueError("output_path_outside_workspace")
            candidate = Path(path)
            if not candidate.exists():
                raise FileNotFoundError(f"output_file_not_found:{raw_path}")
            if not candidate.is_file():
                raise ValueError(f"output_path_not_file:{raw_path}")
            digest = hashlib.sha256()
            size = 0
            with candidate.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            relative = os.path.relpath(path, self.workspace).replace(os.sep, "/")
            result.append(
                DiscoveredOutput(
                    path=path,
                    name=candidate.name,
                    relative_path=relative,
                    content_hash=digest.hexdigest(),
                    size_bytes=size,
                    mime_type=mimetypes.guess_type(candidate.name)[0] or "application/octet-stream",
                    artifact_type=_artifact_type(candidate),
                )
            )
        return result


class RunManifestBuilder:
    """Create a serialisable manifest from a request, result, and outputs."""

    @staticmethod
    def build(request: Any, process_result: Any, outputs: Iterable[DiscoveredOutput]) -> dict[str, Any]:
        declared_inputs = [str(path) for path in getattr(request, "input_paths", ())]
        observed_inputs = [str(path) for path in getattr(request, "observed_input_files", ())]
        boundary = getattr(request, "information_boundary", None)
        if not isinstance(boundary, dict) or "allowed" not in boundary:
            audit_policy = dict(getattr(request, "data_access_policy", {}) or {})
            audit_policy.setdefault("observation_mode", "system")
            observations = InformationBoundaryAudit.observations_from_inputs(
                str(request.workspace_path), observed_inputs, audit_policy
            )
            boundary = InformationBoundaryAudit.evaluate(
                str(request.workspace_path), declared_inputs, observations, audit_policy
            )
        undeclared_inputs = [str(value) for value in boundary.get("undeclared", [])]
        profile = getattr(request, "profile", {})
        observation_diagnostics = getattr(request, "access_observation_diagnostics", None)
        if observation_diagnostics is None:
            observation_diagnostics = getattr(process_result, "access_observation_diagnostics", None)
        execution_profile = getattr(profile, "__dict__", profile if isinstance(profile, dict) else {})
        observed_network = getattr(request, "observed_network_connections", ())
        network_policy = dict(getattr(request, "data_access_policy", {}) or {})
        network_policy.setdefault("network_policy", getattr(profile, "network_policy", "deny-by-default"))
        network_boundary = InformationBoundaryAudit.evaluate_network(
            InformationBoundaryAudit.network_observations_from_records(observed_network),
            network_policy,
        )
        if not network_boundary["allowed"]:
            boundary = dict(boundary)
            boundary["allowed"] = False
            boundary["violations"] = [*list(boundary.get("violations", [])), *network_boundary["violations"]]
        output_records = [output.__dict__ for output in outputs]
        manifest = {
            "schema_version": "1.1",
            "run_id": str(request.run_id),
            "project_id": str(request.project_id),
            "task_id": str(request.task_id) if request.task_id else None,
            "agent_id": str(request.agent_id),
            "device_id": str(request.device_id),
            "workspace_id": str(request.workspace_id),
            "workspace_path": str(request.workspace_path),
            "command": list(request.command),
            "execution_profile": dict(execution_profile),
            "environment_keys": sorted(str(key) for key in request.environment),
            "input_paths": declared_inputs,
            "output_paths": list(request.output_paths),
            "timeout_seconds": request.timeout_seconds,
            "max_output_bytes": request.max_output_bytes,
            "terminal_size": {
                "columns": getattr(request, "terminal_size", (80, 24))[0],
                "rows": getattr(request, "terminal_size", (80, 24))[1],
            },
            "terminal_backend": getattr(request, "terminal_backend", "PIPE"),
            "execution_backend": getattr(request, "execution_backend", "host"),
            "execution": dict(getattr(request, "execution_details", {}) or {}),
            "reproducibility": {
                "source_commit": getattr(request, "source_commit", None),
                "environment_image_digest": getattr(request, "environment_image_digest", None),
                "dependency_lock": getattr(request, "dependency_lock", None),
                "parameters": dict(getattr(request, "parameters", {}) or {}),
                "random_seed": getattr(request, "random_seed", None),
                "model_provider": getattr(request, "model_provider", None),
                "model_name": getattr(request, "model_name", None),
                "tool_versions": dict(getattr(request, "tool_versions", {}) or {}),
                "network_policy": getattr(profile, "network_policy", "deny-by-default"),
                "data_access_policy": dict(getattr(request, "data_access_policy", {}) or {}),
            },
            "input_access": {
                "declared": declared_inputs,
                "observed": observed_inputs,
                "undeclared": undeclared_inputs,
            },
            "information_boundary": boundary,
            "network_access": network_boundary,
            "process": {
                "process_id": str(process_result.process_id),
                "status": str(process_result.status),
                "exit_code": process_result.exit_code,
                "stdout_sha256": hashlib.sha256(process_result.stdout.encode("utf-8")).hexdigest(),
                "stderr_sha256": hashlib.sha256(process_result.stderr.encode("utf-8")).hexdigest(),
                "stdout_bytes": len(process_result.stdout.encode("utf-8")),
                "stderr_bytes": len(process_result.stderr.encode("utf-8")),
            },
            "access_observation": {
                "status": getattr(process_result, "access_observation_status", None),
                "reason": getattr(process_result, "access_observation_reason", None),
                "diagnostics": dict(observation_diagnostics) if observation_diagnostics else None,
            },
            "outputs": output_records,
        }
        manifest["reproducibility_hash"] = reproducibility_hash(manifest)
        canonical = json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest["manifest_sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return manifest


def reproducibility_hash(manifest: Mapping[str, Any]) -> str:
    """Hash deterministic run facts while excluding process/run-local identity."""

    workspace_path = _normalise(str(manifest.get("workspace_path") or ""))
    stable = {
        key: value
        for key, value in manifest.items()
        if key not in {"run_id", "workspace_path", "process", "manifest_sha256", "reproducibility_hash"}
    }
    execution = stable.get("execution")
    if isinstance(execution, dict):
        stable["execution"] = {
            key: value for key, value in execution.items() if key not in {"workspace_path", "command"}
        }
    access_observation = stable.get("access_observation")
    if isinstance(access_observation, dict):
        # Trace session names, ETL/CSV hashes and timestamps legitimately vary
        # between identical runs; only status/reason are deterministic facts.
        stable["access_observation"] = {
            key: value for key, value in access_observation.items() if key != "diagnostics"
        }
    stable = _canonicalise_reproducibility_value(stable, workspace_path)
    canonical = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


_PATH_LIST_KEYS = {
    "declared",
    "observed",
    "undeclared",
    "outside_workspace",
    "future_data",
    "input_paths",
    "output_paths",
}
_PATH_VALUE_KEYS = {"path", "normalised_path", "source_path"}
_COMMAND_KEYS = {"command", "workload_command"}


def _canonicalise_workspace_path(value: str, workspace_path: str) -> str:
    if not workspace_path or not os.path.isabs(value):
        return value
    normalised = _normalise(value)
    if not _inside(normalised, workspace_path):
        return value
    relative = os.path.relpath(normalised, workspace_path)
    return "." if relative == "." else relative.replace(os.sep, "/")


def _canonicalise_reproducibility_value(value: Any, workspace_path: str, *, key: str | None = None) -> Any:
    """Remove machine-local workspace spelling from otherwise stable facts."""

    if isinstance(value, Mapping):
        return {
            str(item_key): _canonicalise_reproducibility_value(item_value, workspace_path, key=str(item_key))
            for item_key, item_value in value.items()
            if str(item_key) != "workspace_path"
        }
    if isinstance(value, (list, tuple)):
        if key in _PATH_LIST_KEYS:
            return [
                _canonicalise_workspace_path(item, workspace_path)
                if isinstance(item, str)
                else _canonicalise_reproducibility_value(item, workspace_path)
                for item in value
            ]
        if key in _COMMAND_KEYS:
            return [
                _canonicalise_workspace_path(item, workspace_path) if isinstance(item, str) else item
                for item in value
            ]
        return [_canonicalise_reproducibility_value(item, workspace_path) for item in value]
    if isinstance(value, str) and key in _PATH_VALUE_KEYS:
        return _canonicalise_workspace_path(value, workspace_path)
    return value


def _filename_from_disposition(value: str) -> str:
    """从 `Content-Disposition` 里取文件名：**必须优先 RFC 5987 的 `filename*=`**。

    踩过的坑：平台的响应头形如 `attachment; filename="artifact.md"; filename*=UTF-8''%E9%94%99...`——
    中文名的 ASCII 兜底（去掉非 ASCII 后只剩扩展名，补成 `artifact.md`）排在前面，
    按顺序取就会把**真名换成 artifact.md**（实测：中文名文件落地后变成 artifact.md）。
    """

    if not value:
        return ""
    pieces = [part.strip() for part in str(value).split(";")]
    raw = next((piece for piece in pieces if piece.lower().startswith("filename*=")), "")
    if raw:
        value_part = raw.split("=", 1)[1].strip().strip('"')
        if "''" in value_part:
            value_part = value_part.split("''", 1)[1]
        try:
            decoded = urllib.parse.unquote(value_part)
            if decoded:
                return decoded
        except Exception:  # noqa: BLE001 - 解码失败退回 ASCII 兜底
            pass
    plain = next((piece for piece in pieces if piece.lower().startswith("filename=")), "")
    return plain.split("=", 1)[1].strip().strip('"') if plain else ""


class AgentArtifactClient:
    """Small urllib client for the Agent-only Artifact endpoints."""

    def __init__(
        self,
        base_url: str,
        project_token: str,
        agent_id: str,
        *,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.project_token = project_token
        self.agent_id = agent_id
        self.opener = opener

    def _request(self, method: str, path: str, *, body: bytes | None = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={"X-Project-Capability-Token": self.project_token, "X-Agent-Id": self.agent_id, **(headers or {})},
        )
        try:
            with self.opener(request, timeout=30) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"artifact_http_{error.code}:{detail[:500]}") from error
        if not isinstance(value, dict):
            raise RuntimeError("artifact_response_must_be_object")
        return value

    def create(
        self,
        project_id: str,
        output: DiscoveredOutput,
        *,
        task_id: str | None,
        run_id: str,
        status: str = "DRAFT",
    ) -> dict[str, Any]:
        """创建成果物。`status` 由调用方决定：常驻任务循环的产出用 `PENDING_REVIEW`（D-CL-1）。"""

        payload = {
            "name": output.name,
            "artifact_type": output.artifact_type,
            "description": f"Runner output {output.relative_path}",
            "content_hash": output.content_hash,
            "source_path": output.path,
            "task_id": task_id,
            "run_id": run_id,
            "status": status,
            "mime_type": output.mime_type,
        }
        key_suffix = hashlib.sha256(output.relative_path.encode("utf-8")).hexdigest()[:32]
        return self._request(
            "POST",
            f"/api/agent/projects/{project_id}/artifacts",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Idempotency-Key": f"artifact-create:{run_id}:{key_suffix}"},
        )

    def download(self, artifact_id: str, *, max_bytes: int = 64 * 1024 * 1024) -> tuple[bytes, str]:
        """下载一个成果物的内容（Agent 专用路由，项目能力令牌鉴权）：返回 `(内容, 文件名)`。

        为什么连文件名一起返回：平台的任务契约只给 `input_artifacts` 的 id（没名字），而下载响应头里
        `Content-Disposition` 带着真名——从那里取比再扩一次契约省事，也不用让 SWE 看到 `input-3f9a.bin`。

        超过 `max_bytes` 直接拒绝，不把超大文件读进内存（宁可失败也不截断——截断会让执行体看到半个文档）。
        """

        request = urllib.request.Request(
            f"{self.base_url}/api/agent/artifacts/{artifact_id}/content",
            method="GET",
            headers={"X-Project-Capability-Token": self.project_token, "X-Agent-Id": self.agent_id},
        )
        try:
            with self.opener(request, timeout=60) as response:
                filename = _filename_from_disposition(response.headers.get("Content-Disposition", "") if hasattr(response, "headers") else "")
                content = response.read(max_bytes + 1)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise InputFetchError(f"http_{error.code}:{detail[:120]}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise InputFetchError(f"下载失败:{type(error).__name__}") from error
        if len(content) > max_bytes:
            raise InputFetchError(f"超过 {max_bytes // (1024 * 1024)}MB 上限")
        return content, filename

    def upload_content(self, artifact_id: str, output: DiscoveredOutput) -> dict[str, Any]:
        content = Path(output.path).read_bytes()
        if len(content) > 8 * 1024 * 1024:
            return self.upload_multipart(artifact_id, output, content)
        return self._upload_form(artifact_id, output, content)

    @staticmethod
    def _form_body(filename: str, mime_type: str, content: bytes) -> tuple[bytes, str]:
        boundary = "----MathAgentUpload" + secrets.token_hex(12)
        disposition = f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        body = (
            f"--{boundary}\r\n{disposition}Content-Type: {mime_type}\r\n\r\n".encode("utf-8")
            + content
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )
        return body, boundary

    def _upload_form(self, artifact_id: str, output: DiscoveredOutput, content: bytes) -> dict[str, Any]:
        body, boundary = self._form_body(output.name, output.mime_type, content)
        return self._request(
            "POST",
            f"/api/agent/artifacts/{artifact_id}/content",
            body=body,
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Content-SHA256": output.content_hash,
                "Idempotency-Key": f"artifact-content:{artifact_id}:{output.content_hash}",
            },
        )

    def upload_multipart(self, artifact_id: str, output: DiscoveredOutput, content: bytes | None = None) -> dict[str, Any]:
        content = content if content is not None else Path(output.path).read_bytes()
        init = self._request(
            "POST",
            f"/api/agent/artifacts/{artifact_id}/multipart?mime_type={urllib.parse.quote(output.mime_type)}",
            headers={"Idempotency-Key": f"artifact-multipart-init:{artifact_id}:{output.content_hash}"},
        )
        upload_id = str(init["upload_id"])
        part_size = 8 * 1024 * 1024
        for index, offset in enumerate(range(0, len(content), part_size), start=1):
            part = content[offset : offset + part_size]
            body, boundary = self._form_body(f"part-{index}", "application/octet-stream", part)
            self._request(
                "PUT",
                f"/api/agent/artifacts/{artifact_id}/multipart/{upload_id}/parts/{index}",
                body=body,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                    "Idempotency-Key": f"artifact-multipart-part:{upload_id}:{index}:{hashlib.sha256(part).hexdigest()}",
                },
            )
        return self._request(
            "POST",
            f"/api/agent/artifacts/{artifact_id}/multipart/{upload_id}/complete",
            headers={
                "X-Content-SHA256": output.content_hash,
                "Idempotency-Key": f"artifact-multipart-complete:{upload_id}:{output.content_hash}",
            },
        )


class ResultUploader:
    """Queue and upload explicit Runner outputs with durable local state."""

    def __init__(
        self,
        state: LocalAgentState,
        client: AgentArtifactClient,
        *,
        create_status: str = "DRAFT",
    ) -> None:
        self.state = state
        self.client = client
        # 执行产出默认进"待审"：内容能否进下游/交付必须有人签字（D-CL-1）
        self.create_status = create_status

    def queue_outputs(self, project_id: str, run_id: str, request: Any, outputs: Iterable[DiscoveredOutput]) -> list[str]:
        upload_ids: list[str] = []
        for output in outputs:
            upload_id = f"output:{run_id}:{output.relative_path}"
            self.state.save_upload(
                output.path,
                upload_id=upload_id,
                content_hash=output.content_hash,
                status="PENDING",
                project_id=project_id,
                task_id=str(request.task_id) if request.task_id else None,
                run_id=run_id,
                mime_type=output.mime_type,
                size_bytes=output.size_bytes,
                relative_path=output.relative_path,
                artifact_type=output.artifact_type,
            )
            upload_ids.append(upload_id)
        return upload_ids

    def upload_pending(self, *, task_id: str | None, run_id: str) -> list[UploadResult]:
        results: list[UploadResult] = []
        for item in self.state.list_uploads(["PENDING", "FAILED", "UPLOADING"]):
            if item.get("run_id") != run_id:
                continue
            candidate = Path(item["local_path"])
            output = OutputDiscovery(candidate.parent).discover([candidate])[0]
            if item.get("relative_path"):
                output = replace(output, relative_path=str(item["relative_path"]))
            if item.get("artifact_type"):
                output = replace(output, artifact_type=str(item["artifact_type"]))
            if item.get("content_hash") and output.content_hash != item["content_hash"]:
                raise ValueError("output_file_changed_after_queue")
            self.state.mark_upload_attempt(item["upload_id"])
            try:
                artifact_id = str(item["artifact_id"]) if item.get("artifact_id") else None
                if artifact_id is None:
                    artifact = self.client.create(
                        str(item["project_id"]), output, task_id=task_id, run_id=run_id, status=self.create_status
                    )
                    artifact_id = str(artifact["id"])
                    self.state.attach_upload_artifact(item["upload_id"], artifact_id)
                self.client.upload_content(artifact_id, output)
                self.state.complete_upload(item["upload_id"], artifact_id)
                results.append(UploadResult(item["upload_id"], artifact_id, output.path))
            except Exception as error:
                self.state.fail_upload(item["upload_id"], str(error))
                raise
        return results

    def upload_run_outputs(self, request: Any, process_result: Any) -> list[str]:
        outputs = OutputDiscovery(request.workspace_path).discover(request.output_paths)
        manifest = RunManifestBuilder.build(request, process_result, outputs)
        manifest_dir = Path(request.workspace_path) / ".math-agent-platform" / "run-manifests"
        manifest_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = manifest_dir / f"{request.run_id}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest_output = OutputDiscovery(request.workspace_path).discover([manifest_path])[0]
        manifest_output = replace(manifest_output, artifact_type="run_manifest", relative_path=f".math-agent-platform/run-manifests/{request.run_id}.json")
        all_outputs = [*outputs, manifest_output]
        upload_ids = self.queue_outputs(request.project_id, request.run_id, request, all_outputs)
        previous_ids = [
            str(item["artifact_id"])
            for item in self.state.list_uploads()
            if item["upload_id"] in upload_ids and item.get("status") == "SUCCEEDED" and item.get("artifact_id")
        ]
        new_results = self.upload_pending(task_id=request.task_id, run_id=request.run_id)
        return [*previous_ids, *[item.artifact_id for item in new_results]]


__all__ = ["AgentArtifactClient", "DiscoveredOutput", "OutputDiscovery", "ResultUploader", "RunManifestBuilder", "UploadResult", "reproducibility_hash"]
