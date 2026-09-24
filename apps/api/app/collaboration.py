"""协作编辑中继（阶段 7：多人同时编辑分析报告）。

平台已经有项目级 WebSocket 广播，这里只补一层**经过校验的 CRDT 帧中继**：
客户端把 Yjs 增量更新（base64）与在线状态帧发给服务端，服务端转发给同项目的
其他连接。服务端不理解 CRDT 内容，也不需要保存文档状态——收敛由客户端 Yjs 负责，
服务端只保证「同项目、按文档分房间、不回声、不放大」。
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

MAX_UPDATE_BYTES = 256 * 1024
UPDATE_FRAME = "document.update"
PRESENCE_FRAME = "document.presence"
ALLOWED_FRAMES = {UPDATE_FRAME, PRESENCE_FRAME}


class CollaborationError(ValueError):
    """稳定错误族：协作帧非法。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


@dataclass(frozen=True)
class CollaborationFrame:
    """一个已校验的协作帧。"""

    frame_type: str
    document_id: str
    payload: dict[str, Any]

    def as_message(self, *, sender: str | None = None) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": self.frame_type,
            "document_id": self.document_id,
            **self.payload,
        }
        if sender is not None:
            message["sender"] = sender
        return message


def parse_frame(raw: str) -> CollaborationFrame:
    """解析并校验客户端帧；非法输入 fail-closed。"""

    if not isinstance(raw, str) or not raw.strip():
        raise CollaborationError("collaboration_frame_required")
    if len(raw) > MAX_UPDATE_BYTES * 2:
        raise CollaborationError("collaboration_frame_too_large")
    try:
        message = json.loads(raw)
    except (TypeError, ValueError) as error:
        raise CollaborationError("collaboration_frame_invalid_json") from error
    if not isinstance(message, dict):
        raise CollaborationError("collaboration_frame_invalid_json")
    frame_type = message.get("type")
    if frame_type not in ALLOWED_FRAMES:
        raise CollaborationError("collaboration_frame_type_unsupported", str(frame_type))
    document_id = message.get("document_id")
    if not isinstance(document_id, str) or not document_id.strip():
        raise CollaborationError("collaboration_document_id_required")
    payload: dict[str, Any] = {}
    if frame_type == UPDATE_FRAME:
        update = message.get("update")
        if not isinstance(update, str):
            raise CollaborationError("collaboration_update_required")
        if not update:
            raise CollaborationError("collaboration_update_empty")
        try:
            decoded = base64.b64decode(update, validate=True)
        except (ValueError, TypeError) as error:
            raise CollaborationError("collaboration_update_not_base64") from error
        if len(decoded) > MAX_UPDATE_BYTES:
            raise CollaborationError("collaboration_update_too_large")
        if not decoded:
            raise CollaborationError("collaboration_update_empty")
        payload["update"] = update
    else:
        payload["peer"] = str(message.get("peer") or "")
        payload["state"] = str(message.get("state") or "active")
    return CollaborationFrame(str(frame_type), document_id.strip(), payload)


__all__ = [
    "ALLOWED_FRAMES",
    "MAX_UPDATE_BYTES",
    "PRESENCE_FRAME",
    "UPDATE_FRAME",
    "CollaborationError",
    "CollaborationFrame",
    "parse_frame",
]