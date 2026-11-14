"""本地成果物与溯源回执。

**复用平台既有实现，不复制**：
- `workspace_scan` —— 执行前后快照与差分（判断"这次到底产出了哪些文件"）
- `result_uploader.OutputDiscovery` —— 哈希 / MIME / 类型推断
- `receipts.build_file_receipt` —— `docs/RECEIPT_FORMAT.md` v1 的 receipt 生成

差别只在**通道**：平台侧经 HTTP 上传并带本地补传队列（要能扛断网）；本地侧直接写同一个
`Store` 的成果物表——没有网络这一层，写不成功就是写不成功，不需要伪装成"待补传"。

采集点遵循 `RECEIPT_FORMAT §5.3`：只为**产生文件产物**的动作生成 receipt，
通道名 `workspace_diff`；`tool_call_id` 用任务 id（本地一轮任务就是执行体侧的调用标识）。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import receipts as _receipts
import workspace_scan as _scan
from result_uploader import OutputDiscovery

from .paths import workspace_dir

# 采集通道名（RECEIPT_FORMAT §5.3 定稿）
CHANNEL_WORKSPACE_DIFF = "workspace_diff"
# 执行体产出进"待审"——只报告，不判断（平台 OutputCollector 同口径）
STATUS_PENDING_REVIEW = "PENDING_REVIEW"

# 单文件上限：本地直写没有传输约束，但仍要给个闸门，避免把工作区整盘灌进成果物表
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024


class ArtifactError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class WorkspaceDiff:
    changed: list[str]
    before_truncated: bool
    after_truncated: bool


def agent_workspace(agent_id: str) -> Path:
    """每个 Agent 一个工作区：产物落在这里，差分才认得出来是谁写的。"""

    safe = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in agent_id)
    return workspace_dir() / safe


def snapshot(workspace: str | Path) -> tuple[dict[str, tuple[int, int]], bool]:
    return _scan.snapshot(Path(workspace))


def diff(before: dict[str, tuple[int, int]], after: dict[str, tuple[int, int]],
         *, before_truncated: bool = False, after_truncated: bool = False) -> WorkspaceDiff:
    result = _scan.diff(before, after, before_truncated=before_truncated,
                        after_truncated=after_truncated)
    return WorkspaceDiff(changed=list(result.changed), before_truncated=before_truncated,
                         after_truncated=after_truncated)


def receipt_model(raw: dict[str, Any]) -> Any:
    """只取 `ArtifactReceipt` 契约认的字段。

    `build_file_receipt` 还带 `status` / `created_at` / `source_artifact_hashes`——
    `RECEIPT_FORMAT §2.1` 明确说不落这些列，让契约层剥离即可，不要求生成侧少给。
    """

    from app.contracts import ArtifactReceipt

    return ArtifactReceipt(
        receipt_version=int(raw.get("receipt_version", 1)),
        tool_name=str(raw["tool_name"]),
        tool_call_id=str(raw.get("tool_call_id") or ""),
        args_hash=str(raw["args_hash"]),
        output_hash=str(raw["output_hash"]),
        output_bytes=int(raw.get("output_bytes", 0)),
        truncated=bool(raw.get("truncated", False)),
    )


def write_artifact(store: Any, *, project_id: Any, workspace: str | Path, relative_path: str,
                   created_by: str, task_id: Any = None, status: str = STATUS_PENDING_REVIEW,
                   artifact_type: str | None = None,
                   max_bytes: int = MAX_ARTIFACT_BYTES) -> dict[str, Any]:
    """把一个工作区文件登记成成果物，带溯源 receipt。"""

    from app.contracts import ArtifactCreate

    workspace_path = Path(workspace)
    try:
        output = OutputDiscovery(workspace_path).discover([relative_path])[0]
    except (ValueError, FileNotFoundError) as exc:
        raise ArtifactError("artifact_source_unreadable", str(exc)) from exc
    if output.size_bytes > max_bytes:
        raise ArtifactError("artifact_too_large",
                            f"{output.relative_path} = {output.size_bytes} bytes > {max_bytes}")

    content = Path(output.path).read_bytes()
    receipt = _receipts.build_file_receipt(
        tool_name=CHANNEL_WORKSPACE_DIFF,
        tool_call_id=str(task_id or ""),
        args={"path": output.relative_path, "size_bytes": output.size_bytes},
        content_sha256_hex=output.content_hash,
        output_bytes=output.size_bytes,
    )

    project_uuid = project_id if hasattr(project_id, "hex") else __import__("uuid").UUID(str(project_id))
    task_uuid = None
    if task_id:
        task_uuid = task_id if hasattr(task_id, "hex") else __import__("uuid").UUID(str(task_id))

    artifact = store.create_artifact(
        project_uuid,
        ArtifactCreate(
            name=output.name,
            artifact_type=artifact_type or output.artifact_type,
            task_id=task_uuid,
            status=status,
            receipt=receipt_model(receipt),
        ),
        created_by=created_by,
        created_by_kind="agent",
    )
    # 内容哈希对账：读回的字节必须和发现阶段算的一致
    store.store_artifact_content(artifact.id, content, output.mime_type,
                                 expected_hash=output.content_hash)
    view = artifact.model_dump(mode="json")
    view["receipt"] = receipt
    view["relative_path"] = output.relative_path
    # 摘要也用得上的几个字段摊平（`size_bytes` 此时还没写回，用发现阶段的字节数）
    view["output_hash"] = receipt["output_hash"]
    view["output_bytes"] = receipt["output_bytes"]
    view["size_bytes"] = output.size_bytes
    return view


def receipt_for_bytes(*, tool_name: str, tool_call_id: str, args: dict[str, Any],
                      content: bytes) -> Any:
    """为"内存里的一段内容"生成 receipt（契约正文这类没有工作区文件的产物用）。"""

    raw = _receipts.build_file_receipt(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        args=args,
        content_sha256_hex=hashlib.sha256(content).hexdigest(),
        output_bytes=len(content),
    )
    return receipt_model(raw)


def read_artifact_bytes(store: Any, artifact_id: Any, *, max_bytes: int = MAX_ARTIFACT_BYTES) -> bytes:
    content = store.get_artifact_content(_as_uuid(artifact_id))
    if content is None:
        raise ArtifactError("artifact_content_missing", f"artifact {artifact_id} has no stored content")
    if len(content) > max_bytes:
        raise ArtifactError("artifact_too_large", f"{len(content)} bytes")
    return content


def _as_uuid(value: Any) -> Any:
    import uuid

    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def artifact_brief(artifact: Any) -> dict[str, Any]:
    """摘要视图。receipt 在契约里是**子对象**（`Artifact.receipt`），不是扁平的列。"""

    data = artifact.model_dump(mode="json") if hasattr(artifact, "model_dump") else dict(artifact)
    receipt = data.get("receipt") or {}
    keys = ("id", "name", "artifact_type", "status", "version", "size_bytes",
            "content_hash", "task_id", "created_at")
    brief = {key: data.get(key) for key in keys if key in data}
    brief["receipt_version"] = receipt.get("receipt_version", 0)
    brief["tool_name"] = receipt.get("tool_name")
    brief["output_hash"] = receipt.get("output_hash")
    brief["output_bytes"] = receipt.get("output_bytes")
    return brief


__all__ = [
    "ArtifactError", "CHANNEL_WORKSPACE_DIFF", "MAX_ARTIFACT_BYTES", "STATUS_PENDING_REVIEW",
    "WorkspaceDiff", "agent_workspace", "artifact_brief", "diff", "read_artifact_bytes",
    "snapshot", "write_artifact",
]
