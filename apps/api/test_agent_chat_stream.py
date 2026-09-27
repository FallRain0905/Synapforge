"""my-agent 实时流（SSE，W1.3）契约测试。

口径（消费工作包 C 的 stream_bridge，接线说明见其期二交付）：
- 帧格式 event/data/id，`id:` 即游标（turn 流 = agent_turn_events.sequence）；
- 断线续传：`?after=` 或 SSE 标准 Last-Event-ID 头，从桥重放增量；
- 桥被裁剪 → `__gap__` 帧 + 立即关闭——客户端必须回权威接口（GET .../events）全量重同步，
  绝不把残缺回放当完整流用；
- 正常结束 → `__end__` 帧后关闭；最终 content/stop_reason 以轮次行为准；
- 会话流只承载轮次生命周期信号（turn.created/finished/cancelled），不镜像内容。
测试全部订阅"已结束"的话题（publish_end 后有界），不阻塞。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import agent_chat, main, stream_bridge
from app.contracts import (
    AgentChatConversationCreate,
    AgentChatMessageCreate,
    AgentChatTurnComplete,
    AgentChatTurnEventReport,
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request

MEMBER = "member-001"
DEVICE = "device-stream-001"


def parse_sse(text: str) -> list[dict[str, str]]:
    """把 SSE 文本拆成帧字典（注释帧记在 comment 键）。"""

    frames: list[dict[str, str]] = []
    for block in text.split(chr(10) * 2):
        block = block.strip(chr(10) + chr(13) + " ")
        if not block:
            continue
        frame: dict[str, str] = {}
        for line in block.splitlines():
            if line.startswith(":"):
                frame["comment"] = line
                continue
            key, _, value = line.partition(": ")
            frame[key] = value
        frames.append(frame)
    return frames


class AgentStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="stream-agent", display_name="Stream Agent", owner_member_id=MEMBER)
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                self.agent.agent_id,
                DEVICE,
                device_name="Stream box",
                capabilities=["chat.run"],
            )
        )
        self.device = credential.device
        self.grant = self.store.create_device_project_grant(
            self.project.id, DeviceProjectGrantCreate(device_id=self.device.device_id), MEMBER
        )

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # ---- helpers ---------------------------------------------------------

    def new_turn(self):
        conversation = agent_chat.create_conversation(
            self.store,
            MEMBER,
            AgentChatConversationCreate(project_id=self.project.id, device_id=self.device.device_id, model="m"),
        )
        turn = agent_chat.append_message(self.store, conversation.id, MEMBER, AgentChatMessageCreate(content="跑一遍"))
        agent_chat.claim_turn(self.store, self.agent.agent_id, self.project.id, 900)
        return conversation, turn

    def record(self, turn, sequence: int, event_type: str = "delta", payload: dict | None = None):
        return agent_chat.record_turn_event(
            self.store,
            turn.id,
            self.agent.agent_id,
            AgentChatTurnEventReport(sequence=sequence, event_type=event_type, payload=payload or {"text": f"part{sequence}"}),
        )

    def get_stream(self, url: str, *, after: int | None = None, last_event_id: str | None = None) -> list[dict[str, str]]:
        if after is not None:
            url += ("&" if "?" in url else "?") + f"after={after}"
        headers: dict[str, str] = {}
        if last_event_id is not None:
            headers["Last-Event-ID"] = last_event_id
        with self.client.stream("GET", url, headers=headers) as response:
            self.assertEqual(response.status_code, 200, f"stream failed: {response.status_code}")
            self.assertTrue((response.headers.get("content-type") or "").startswith("text/event-stream"))
            text = "".join(response.iter_text())
        return parse_sse(text)

    # ---- turn 流 ---------------------------------------------------------

    def test_turn_stream_replays_then_ends(self) -> None:
        """全量订阅：重放已落库事件（id=sequence）→ __end__。帧格式 event/data/id 三段。"""

        conversation, turn = self.new_turn()
        self.record(turn, 1)
        self.record(turn, 2, payload={"text": "done"})
        agent_chat.complete_turn(
            self.store, turn.id, self.agent.agent_id, AgentChatTurnComplete(success=True, content="完了")
        )
        frames = self.get_stream(f"/api/my-agent/turns/{turn.id}/events/stream")
        self.assertEqual(
            [(f.get("id"), f.get("event")) for f in frames],
            [("1", "delta"), ("2", "delta"), (None, "__end__")],
        )
        self.assertEqual(json.loads(frames[0]["data"]), {"text": "part1"})
        # 结束后的最终状态以权威行为准：stop_reason 已在轮次行上
        self.assertEqual(agent_chat.get_turn(self.store, turn.id).stop_reason, "completed")

    def test_turn_stream_resumes_from_cursor(self) -> None:
        """断线续传：after=1（等价 Last-Event-ID: 1）只重放 seq>1 的增量 + __end__。"""

        conversation, turn = self.new_turn()
        self.record(turn, 1)
        self.record(turn, 2)
        agent_chat.complete_turn(
            self.store, turn.id, self.agent.agent_id, AgentChatTurnComplete(success=True, content="完了")
        )
        frames = self.get_stream(f"/api/my-agent/turns/{turn.id}/events/stream", after=1)
        self.assertEqual([(f.get("id"), f.get("event")) for f in frames], [("2", "delta"), (None, "__end__")])
        frames = self.get_stream(f"/api/my-agent/turns/{turn.id}/events/stream", last_event_id="1")
        self.assertEqual([(f.get("id"), f.get("event")) for f in frames], [("2", "delta"), (None, "__end__")])

    def test_turn_stream_gap_when_bridge_trimmed(self) -> None:
        """桥被裁剪（retention=1，第二条把第一条挤掉）→ __gap__ 帧 + 立即关闭，不冒充完整回放。"""

        self.addCleanup(setattr, agent_chat, "_BRIDGE", agent_chat._BRIDGE)
        agent_chat._BRIDGE = stream_bridge.StreamBridge(retention=1)
        conversation, turn = self.new_turn()
        self.record(turn, 1)
        self.record(turn, 2)
        agent_chat.complete_turn(
            self.store, turn.id, self.agent.agent_id, AgentChatTurnComplete(success=True, content="完了")
        )
        frames = self.get_stream(f"/api/my-agent/turns/{turn.id}/events/stream")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].get("event"), "__gap__")
        gap = json.loads(frames[0]["data"])
        self.assertEqual(gap["requested_seq"], 0)
        self.assertEqual(gap["earliest_available"], 2)

    # ---- 会话流 ----------------------------------------------------------

    def test_conversation_stream_carries_lifecycle(self) -> None:
        """会话流：turn.created / turn.finished 生命周期信号（不镜像内容），payload 带轮次与结局。"""

        conversation, turn = self.new_turn()
        self.record(turn, 1)
        agent_chat.complete_turn(
            self.store, turn.id, self.agent.agent_id, AgentChatTurnComplete(success=False, error="执行体炸了")
        )
        # 会话话题不会自动结束（会话还能有下一轮）：测试里手动收尾使订阅有界
        agent_chat.bridge().publish_end(agent_chat.conversation_topic(conversation.id))
        frames = self.get_stream(f"/api/my-agent/conversations/{conversation.id}/stream")
        self.assertEqual(
            [f.get("event") for f in frames],
            ["turn.created", "turn.finished", "__end__"],
        )
        created = json.loads(frames[0]["data"])
        self.assertEqual(created["turn_id"], str(turn.id))
        finished = json.loads(frames[1]["data"])
        self.assertEqual(finished["status"], "FAILED")
        self.assertEqual(finished["stop_reason"], "failed")
        # 会话流自增 seq：created=1, finished=2（与 turn 流的 sequence 口径独立，契约允许）
        self.assertEqual([f.get("id") for f in frames[:2]], ["1", "2"])

    def test_delete_conversation_cleans_bridge_topics(self) -> None:
        """会话删除 → 桥上话题清理成墓碑（ended），订阅者收 END 而不是永远等。"""

        conversation, turn = self.new_turn()
        agent_chat.delete_conversation(self.store, conversation.id, MEMBER)
        self.assertTrue(agent_chat.bridge().read_since(agent_chat.conversation_topic(conversation.id), 0).ended)
        self.assertTrue(agent_chat.bridge().read_since(agent_chat.turn_topic(turn.id), 0).ended)

    # ---- 鉴权与归属 ------------------------------------------------------

    def test_stream_requires_membership(self) -> None:
        """HTTP 层：无效会话令牌 401（鉴权在开流之前完成，因此有界）；别人的轮次 404。"""

        conversation, turn = self.new_turn()
        response = self.client.get(
            f"/api/my-agent/turns/{turn.id}/events/stream", headers={"Authorization": "Bearer not-a-real-token"}
        )
        self.assertEqual(response.status_code, 401)
        response = self.client.get(
            f"/api/my-agent/conversations/{conversation.id}/stream",
            headers={"Authorization": "Bearer not-a-real-token"},
        )
        self.assertEqual(response.status_code, 401)
        with self.assertRaisesRegex(agent_chat.AgentChatError, "turn_not_found"):
            agent_chat.ensure_turn_owned(self.store, turn.id, "member-somebody-else")


if __name__ == "__main__":
    unittest.main()
