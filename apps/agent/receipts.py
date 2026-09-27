"""产物溯源 receipt 生成（`docs/RECEIPT_FORMAT.md` §2，receipt_version=1）。

职责分工（C 契约）：**agentd 在把产物上传为 artifact 时生成 receipt，随上传一并提交**；
平台（A）校验结构后落库溯源字段并在 `project.artifact.uploaded` 事件里携带。

与 deer-flow `tool_receipt.py` 的两处差异（C 在文档 §2 已纠正，这里照做）：
1. 字段名用 `*_hash`——deer-flow 叫 `*_sha256` 但实为 SHA-256 前 16 位 hex，名实不符；
2. 哈希对象明确：`args_hash` 对参数的规范化 JSON（UTF-8、键排序、无空白），
   `output_hash` 对输出**原始字节**。

§5.2 口径（本模块的实现前提，接了 C 留给 B 的问题 2）：opencode 侧拿不到"工具调用的
原始输出流"——对话通道的产物经工作区快照差分采集，是**派生视图**。因此
`output_hash` 对**写入文件的内容字节**计算（`OutputDiscovery.content_hash` 即该口径
的完整 SHA-256），`tool_name` 如实写采集通道名（`workspace_diff`），
`tool_call_id` 用轮次 id（这轮对话就是执行体侧的调用标识）。
任务 Run 通道的 receipt 接入是第四期（W2.4）工作，复用本模块的生成函数。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

RECEIPT_VERSION = 1
_HASH_LEN = 16  # RECEIPT_FORMAT §2：两个 hash 都是 SHA-256 前 16 位小写 hex
_MAX_FIELD = 128  # tool_name / tool_call_id 的长度上限，超限截断（生成侧兜底）


def short_hash(data: bytes) -> str:
    """SHA-256 前 16 位小写 hex（`*_hash` 的口径）。"""
    return hashlib.sha256(data).hexdigest()[:_HASH_LEN]


def canonical_json_hash(args: Mapping[str, Any]) -> str:
    """对参数的规范化 JSON（UTF-8、键排序、无空白）取 `args_hash`。"""
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return short_hash(canonical.encode("utf-8"))


def build_file_receipt(
    *,
    tool_name: str,
    tool_call_id: str,
    args: Mapping[str, Any],
    content_sha256_hex: str,
    output_bytes: int,
    status: str = "success",
    truncated: bool = False,
    source_artifact_hashes: list[str] | None = None,
) -> dict[str, Any]:
    """按 RECEIPT_FORMAT §2 生成一条文件产物的 receipt。

    `content_sha256_hex` 传**文件内容的完整 SHA-256 hex**（大写也会被归一成小写），
    本函数取前 16 位作为 `output_hash`；`output_bytes` 必须是**计入哈希的字节数**
    （截断时是保留段的长度，且 `truncated=True`）。
    """

    receipt: dict[str, Any] = {
        "receipt_version": RECEIPT_VERSION,
        "tool_name": str(tool_name)[:_MAX_FIELD],
        "tool_call_id": str(tool_call_id)[:_MAX_FIELD],
        "args_hash": canonical_json_hash(args),
        "output_hash": str(content_sha256_hex).lower()[:_HASH_LEN],
        "output_bytes": int(output_bytes),
        "status": status,
        # 执行体时钟、ISO-8601 UTC（带 +00:00 偏移）；平台侧以事件 occurred_at 为准复核
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    if truncated:
        receipt["truncated"] = True
    if source_artifact_hashes:
        receipt["source_artifact_hashes"] = [str(item) for item in source_artifact_hashes]
    return receipt


__all__ = ["RECEIPT_VERSION", "build_file_receipt", "canonical_json_hash", "short_hash"]
