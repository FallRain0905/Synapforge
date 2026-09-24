"""阶段 7 协作编辑：中继帧校验与转发契约测试。"""

from __future__ import annotations

import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from app import collaboration, main
from app.store import Store


class FakeWebSocket:
    """记录发送内容的假连接。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def accept(self) -> None:
        return None

    async def send_json(self, message: dict) -> None:
        self.sent.append(message)


class CollaborationFrameTests(unittest.TestCase):
    def _update_frame(self, document_id: str, payload: bytes = b"crdt-bytes") -> str:
        return json.dumps(
            {
                "type": collaboration.UPDATE_FRAME,
                "document_id": document_id,
                "update": base64.b64encode(payload).decode("ascii"),
            }
        )

    def test_parses_update_frame(self) -> None:
        frame = collaboration.parse_frame(self._update_frame("doc-1"))
        self.assertEqual(frame.frame_type, collaboration.UPDATE_FRAME)
        self.assertEqual(frame.document_id, "doc-1")
        decoded = base64.b64decode(frame.payload["update"])
        self.assertEqual(decoded, b"crdt-bytes")
        message = frame.as_message(sender="peer-a")
        self.assertEqual(message["sender"], "peer-a")
        self.assertEqual(message["type"], collaboration.UPDATE_FRAME)

    def test_parses_presence_frame(self) -> None:
        raw = json.dumps(
            {"type": collaboration.PRESENCE_FRAME, "document_id": "doc-1", "peer": "member-002", "state": "active"}
        )
        frame = collaboration.parse_frame(raw)
        self.assertEqual(frame.frame_type, collaboration.PRESENCE_FRAME)
        self.assertEqual(frame.payload["peer"], "member-002")

    def test_rejects_unsupported_or_malformed_frames(self) -> None:
        cases = {
            "collaboration_frame_required": "",
            "collaboration_frame_invalid_json": "{not json",
            "collaboration_frame_type_unsupported": json.dumps({"type": "chat.message", "document_id": "d"}),
            "collaboration_document_id_required": json.dumps({"type": collaboration.UPDATE_FRAME, "update": "AA=="}),
            "collaboration_update_required": json.dumps({"type": collaboration.UPDATE_FRAME, "document_id": "d"}),
            "collaboration_update_not_base64": json.dumps(
                {"type": collaboration.UPDATE_FRAME, "document_id": "d", "update": "not base64!!"}
            ),
            "collaboration_update_empty": json.dumps(
                {"type": collaboration.UPDATE_FRAME, "document_id": "d", "update": base64.b64encode(b"").decode()}
            ),
        }
        for expected_code, raw in cases.items():
            with self.subTest(code=expected_code):
                with self.assertRaises(collaboration.CollaborationError) as caught:
                    collaboration.parse_frame(raw)
                self.assertEqual(caught.exception.code, expected_code)

    def test_rejects_oversized_update(self) -> None:
        big = base64.b64encode(b"x" * (collaboration.MAX_UPDATE_BYTES + 1)).decode("ascii")
        raw = json.dumps({"type": collaboration.UPDATE_FRAME, "document_id": "d", "update": big})
        with self.assertRaises(collaboration.CollaborationError) as caught:
            collaboration.parse_frame(raw)
        self.assertEqual(caught.exception.code, "collaboration_update_too_large")


class CollaborationRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project_id = self.store.list_projects()[0].id
        self.manager = main.ConnectionManager()
        self.alice = FakeWebSocket()
        self.bob = FakeWebSocket()
        asyncio.run(self.manager.connect(self.project_id, self.alice))  # type: ignore[arg-type]
        asyncio.run(self.manager.connect(self.project_id, self.bob))  # type: ignore[arg-type]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def test_relay_delivers_to_other_clients_without_echo(self) -> None:
        message = {"type": collaboration.UPDATE_FRAME, "document_id": "doc-1", "update": "AA=="}
        delivered = asyncio.run(self.manager.relay(str(self.project_id), message, exclude=self.alice))
        self.assertEqual(delivered, 1)
        self.assertEqual(self.alice.sent, [])
        self.assertEqual(self.bob.sent, [message])

    def test_relay_is_isolated_per_project(self) -> None:
        other_project = self.store.create_project(
            main.ProjectCreate(name="另一个项目", competition_pack="cumcm-2026", problem_code="C", description="x")
        )
        stranger = FakeWebSocket()
        asyncio.run(self.manager.connect(other_project.id, stranger))  # type: ignore[arg-type]
        asyncio.run(self.manager.relay(str(self.project_id), {"type": collaboration.UPDATE_FRAME}, exclude=self.alice))
        self.assertEqual(stranger.sent, [])

    def test_relay_drops_dead_connection(self) -> None:
        class BrokenSocket:
            async def accept(self) -> None:
                return None

            async def send_json(self, message: dict) -> None:
                raise RuntimeError("closed")

        broken = BrokenSocket()
        asyncio.run(self.manager.connect(self.project_id, broken))  # type: ignore[arg-type]
        asyncio.run(self.manager.relay(str(self.project_id), {"type": collaboration.UPDATE_FRAME}, exclude=self.alice))
        remaining = self.manager.connections[str(self.project_id)]
        self.assertNotIn(broken, remaining)
        self.assertIn(self.bob, remaining)


if __name__ == "__main__":
    unittest.main()