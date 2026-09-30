"""对话通道的产出采集：一轮跑完，把工作区里**新出现/被改过**的文件上传成成果物。

为什么要有它（用户 2026-09-24 的需求）：执行体在对话里生成文件之后，人应当能**下载**它，或
**一键转进个人云盘**——而平台此前只在"任务/运行"这条线上采集产出（CL-1），对话这条线什么都没有。

与任务通道的差别只有两点，其余全部复用既有链路：
1. **没有 Run**：成果物直接挂项目（`run_id=None`、`task_id=None`，状态 `PENDING_REVIEW` 走既有的审核门禁）；
2. **幂等键按轮次算**：`artifact-create:chat:{turn_id}:{sha(相对路径+内容哈希)}`——
   同一条路径在不同轮次写出不同内容时各成一个版本，不会因为"键相同、指纹不同"被平台判冲突。

口径（与 CL-1 的 I4 一致）：**上传失败不改变这一轮的成败**——记一条日志、产出留在工作区，下次再传。
`inputs/`（平台下发的输入文件）不算产出，显式跳过。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:  # 包内导入（正常运行）
    from .receipts import build_file_receipt
    from .result_uploader import AgentArtifactClient, DiscoveredOutput, OutputDiscovery
    from .workspace_scan import INPUT_DIR_NAMES, diff, is_excluded, snapshot
except ImportError:  # 直接脚本执行 / 测试以顶层模块导入
    from receipts import build_file_receipt  # type: ignore
    from result_uploader import AgentArtifactClient, DiscoveredOutput, OutputDiscovery  # type: ignore
    from workspace_scan import INPUT_DIR_NAMES, diff, is_excluded, snapshot  # type: ignore

# 一轮最多收这么多产出（对话不是流水线，防的是"模型把整个目录写爆"）
MAX_OUTPUTS_PER_TURN = 20
# 单文件上限：与成果物上传上限同量级；超了只记名字不传（如实告知，不截断）
MAX_OUTPUT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ChatOutput:
    """一条对话产出（给平台 `complete` 用；不带内容，内容在成果物里）。

    `receipt` 是产物溯源（RECEIPT_FORMAT v1，`docs/RECEIPT_FORMAT.md`）：
    平台契约落地前 `complete` 请求体里的该字段会被忽略，落地后由 A 校验落库。
    """

    artifact_id: str
    name: str
    size_bytes: int
    artifact_type: str
    mime_type: str
    relative_path: str = ""
    receipt: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        payload = {
            "artifact_id": self.artifact_id,
            "name": self.name,
            "size_bytes": int(self.size_bytes),
            "artifact_type": self.artifact_type,
            "mime_type": self.mime_type,
            "relative_path": self.relative_path,
        }
        if self.receipt is not None:
            payload["receipt"] = self.receipt
        return payload


@dataclass
class CollectResult:
    outputs: list[ChatOutput] = field(default_factory=list)
    skipped_too_large: list[str] = field(default_factory=list)
    skipped_over_limit: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def note(self) -> str:
        if not self.outputs and not self.skipped_too_large and not self.errors:
            return "这一轮没有产出文件"
        parts = [f"产出 {len(self.outputs)} 个文件"]
        if self.skipped_too_large:
            parts.append(f"{len(self.skipped_too_large)} 个超过上传上限（只登记未上传）")
        if self.skipped_over_limit:
            parts.append(f"{len(self.skipped_over_limit)} 个超过单轮数量上限（留在工作区）")
        if self.errors:
            parts.append(f"{len(self.errors)} 项上传失败")
        return " | ".join(parts)


class ChatOutputCollector:
    """对话轮次的产出采集器：`snapshot()` 在执行前、`collect()` 在执行后。"""

    def __init__(
        self,
        *,
        workspace: str | Path,
        url: str,
        project_id: str,
        agent_id: str,
        project_token: str,
        client: Any | None = None,
        max_outputs: int = MAX_OUTPUTS_PER_TURN,
        max_bytes: int = MAX_OUTPUT_BYTES,
        log: Callable[[str], None] = print,
    ) -> None:
        self.workspace = Path(workspace).expanduser().resolve()
        self.url = url
        self.project_id = project_id
        self.agent_id = agent_id
        self.project_token = project_token
        self.max_outputs = int(max_outputs)
        self.max_bytes = int(max_bytes)
        self.log = log
        self._client = client or AgentArtifactClient(url, project_token, agent_id)
        self._before: dict[str, tuple[int, int]] | None = None

    # ---- 执行前 -------------------------------------------------------------

    def snapshot(self) -> None:
        """记下执行前的工作区指纹（**失败不抛**：拿不到就不采这一轮，轮次照跑）。"""

        try:
            self._before, _truncated = snapshot(self.workspace)
        except OSError as error:
            self._before = None
            self.log(f"[chat] 产出采集：工作区快照失败（{type(error).__name__}），这一轮不采产出")

    # ---- 执行后 -------------------------------------------------------------

    def collect(self, turn_id: str) -> CollectResult:
        """差分 → 建成果物 → 传内容。**任何失败都只记不抛**（不改变这一轮的成败）。"""

        result = CollectResult()
        if self._before is None:
            return result
        try:
            after, _truncated = snapshot(self.workspace)
            candidates = [
                relative
                for relative in diff(self._before, after).changed
                if self._is_collectable(relative)
            ]
        except OSError as error:
            result.errors.append(f"scan:{type(error).__name__}")
            return result

        for index, relative in enumerate(candidates):
            if len(result.outputs) >= self.max_outputs:
                # 超过单轮上限的留在工作区（如实记账，不静默丢弃）
                result.skipped_over_limit.extend(candidates[index:])
                break
            path = self.workspace / relative
            try:
                if path.stat().st_size > self.max_bytes:
                    result.skipped_too_large.append(relative)
                    continue
                output = OutputDiscovery(self.workspace).discover([relative])[0]
            except (OSError, ValueError, IndexError) as error:
                result.errors.append(f"{relative}:{type(error).__name__}")
                continue
            try:
                result.outputs.append(self._upload(turn_id, output))
                self.log(f"[chat] 产出已上传：{relative}（{output.size_bytes} 字节）")
            except Exception as error:  # noqa: BLE001 - 上传失败不改这一轮的成败（与 CL-1 的 I4 同一口径）
                result.errors.append(f"{relative}:{type(error).__name__}")
                self.log(f"[chat] 产出上传失败（留在工作区，下次再传）：{relative} {type(error).__name__}")
        return result

    # ---- 内部 ---------------------------------------------------------------

    @staticmethod
    def _is_collectable(relative: str) -> bool:
        parts = [part for part in relative.replace("\\", "/").split("/") if part]
        if parts and parts[0] in INPUT_DIR_NAMES:
            return False  # 平台下发的输入不算产出
        return not is_excluded(relative)

    def _idempotency_key(self, turn_id: str, output: DiscoveredOutput) -> str:
        digest = hashlib.sha256(f"{output.relative_path}:{output.content_hash}".encode("utf-8")).hexdigest()[:32]
        return f"artifact-create:chat:{turn_id}:{digest}"

    def _upload(self, turn_id: str, output: DiscoveredOutput) -> ChatOutput:
        # 产物溯源（RECEIPT_FORMAT v1）：§5.2 口径——output_hash 对"写入文件的内容字节"算
        # （content_hash 就是该口径的完整 SHA-256），工具名如实写采集通道，调用 id 用轮次。
        # receipt 同时挂 artifact-create 载荷（C 契约的正典载体）与 complete 的 outputs[]（平台契约
        # 落地前都被忽略，落地后 A 校验落库）。
        receipt = build_file_receipt(
            tool_name="workspace_diff",
            tool_call_id=turn_id,
            args={"path": output.relative_path},
            content_sha256_hex=output.content_hash,
            output_bytes=output.size_bytes,
        )
        payload = {
            "name": output.name,
            "artifact_type": output.artifact_type,
            "description": f"对话产出 {output.relative_path}",
            "content_hash": output.content_hash,
            "source_path": output.path,
            "status": "PENDING_REVIEW",
            "mime_type": output.mime_type,
            "receipt": receipt,
        }
        created = self._client._request(  # noqa: SLF001 - 复用同一个鉴权/重试壳，只换幂等键
            "POST",
            f"/api/agent/projects/{self.project_id}/artifacts",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Idempotency-Key": self._idempotency_key(turn_id, output)},
        )
        artifact_id = str(created.get("id") or "")
        if not artifact_id:
            raise RuntimeError("artifact_create_no_id")
        self._client.upload_content(artifact_id, output)
        return ChatOutput(
            artifact_id=artifact_id,
            name=output.name,
            size_bytes=output.size_bytes,
            artifact_type=output.artifact_type,
            mime_type=output.mime_type,
            relative_path=output.relative_path,
            receipt=receipt,
        )


__all__ = ["ChatOutput", "ChatOutputCollector", "CollectResult", "MAX_OUTPUTS_PER_TURN"]