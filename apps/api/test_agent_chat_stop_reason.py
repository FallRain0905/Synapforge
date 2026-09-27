"""MY-AGENT 轮次终止原因（stop_reason，W1.1）契约测试。

口径（重新定位实施计划第一期）：
- `status` 只说"怎么结束"（DONE/FAILED/CANCELLED），`stop_reason` 说"为什么"；
- 平台侧可判定的：cancelled（成员停止）与 completed/failed（按执行体回报的 success 兜底，兜底不掩盖失败）；
- 执行体回报的契约集合内值**原样落库**（token_capped/permission_timeout…——执行体知道自己怎么停的）；
- 集合外的值一律归一化为 unknown，绝不编造；
- 被取消的轮次不因执行体迟到回报改写结局（与 complete 的幂等口径一致）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import agent_chat, main
from app.contracts import (
    AgentChatConversationCreate,
    AgentChatMessageCreate,
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request

MEMBER = "member-001"
DEVICE = "device-stopreason-001"


class AgentTurnStopReasonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="stopreason-agent", display_name="Stop Reason Agent", owner_member_id=MEMBER)
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                self.agent.agent_id,
                DEVICE,
                device_name="Stop reason box",
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

    def agent_headers(self) -> dict[str, str]:
        return {"X-Project-Capability-Token": self.grant.project_token, "X-Agent-Id": self.agent.agent_id}

    def claimed_turn(self, text: str = "帮我改一下这段代码"):
        conversation = agent_chat.create_conversation(
            self.store,
            MEMBER,
            AgentChatConversationCreate(project_id=self.project.id, device_id=self.device.device_id, model="m"),
        )
        turn = agent_chat.append_message(self.store, conversation.id, MEMBER, AgentChatMessageCreate(content=text))
        agent_chat.claim_turn(self.store, self.agent.agent_id, self.project.id, 900)
        return turn

    def complete(self, turn_id: UUID, payload: dict) -> dict:
        response = self.client.post(
            f"/api/agents/{self.agent.agent_id}/chat-turns/{turn_id}/complete", json=payload, headers=self.agent_headers()
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ---- 回报与兜底 -------------------------------------------------------

    def test_complete_without_stop_reason_defaults_by_success(self) -> None:
        """老执行体不带这个字段：按结局兜底——成功 completed、失败 failed，失败不被美化。"""

        done = self.complete(self.claimed_turn().id, {"success": True, "content": "搞定"})
        self.assertEqual((done["status"], done["stop_reason"]), ("DONE", "completed"))
        failed = self.complete(self.claimed_turn().id, {"success": False, "error": "executor boom"})
        self.assertEqual((failed["status"], failed["stop_reason"]), ("FAILED", "failed"))

    def test_reported_stop_reason_passes_through(self) -> None:
        """执行体知道自己怎么停的就如实落库：预算触顶、审批没人批都原样保留。"""

        capped = self.complete(self.claimed_turn().id, {"success": True, "content": "partial", "stop_reason": "token_capped"})
        self.assertEqual((capped["status"], capped["stop_reason"]), ("DONE", "token_capped"))
        expired = self.complete(
            self.claimed_turn().id, {"success": False, "error": "nobody approved", "stop_reason": "permission_timeout"}
        )
        self.assertEqual((expired["status"], expired["stop_reason"]), ("FAILED", "permission_timeout"))

    def test_unknown_reported_value_becomes_unknown(self) -> None:
        """集合外的值归一化为 unknown：诚实说"不知道为什么结束"，不编。"""

        turn = self.complete(self.claimed_turn().id, {"success": True, "content": "ok", "stop_reason": "exploded_spectacularly"})
        self.assertEqual(turn["stop_reason"], "unknown")
        # 大小写与首尾空白不改变语义：契约集合内的值以小写归一化后命中
        sloppy = self.complete(self.claimed_turn().id, {"success": True, "content": "ok", "stop_reason": "  Token_Capped  "})
        self.assertEqual(sloppy["stop_reason"], "token_capped")

    # ---- 成员停止 ---------------------------------------------------------

    def test_member_stop_marks_cancelled(self) -> None:
        turn = self.claimed_turn()
        response = self.client.post(f"/api/my-agent/turns/{turn.id}/stop")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["status"], body["stop_reason"]), ("CANCELLED", "cancelled"))

    def test_late_complete_does_not_overwrite_cancelled(self) -> None:
        """取消后执行体才回报完成：结局不被改写（幂等口径），stop_reason 仍是 cancelled。"""

        turn = self.claimed_turn()
        stopped = self.client.post(f"/api/my-agent/turns/{turn.id}/stop").json()
        late = self.complete(turn.id, {"success": True, "content": "其实跑完了"})
        self.assertEqual((late["status"], late["stop_reason"]), (stopped["status"], "cancelled"))

    def test_stop_requires_valid_session(self) -> None:
        """HTTP 层：无效会话令牌 401；服务层：别人的轮次 turn_not_found。"""

        turn = self.claimed_turn()
        response = self.client.post(
            f"/api/my-agent/turns/{turn.id}/stop", headers={"Authorization": "Bearer not-a-real-token"}
        )
        self.assertEqual(response.status_code, 401)
        with self.assertRaisesRegex(agent_chat.AgentChatError, "turn_not_found"):
            agent_chat.cancel_turn(self.store, turn.id, "member-somebody-else")

    def test_complete_requires_capability(self) -> None:
        """HTTP 层：完成回报必须带项目能力令牌（chat.run），匿名回报进不来（缺令牌 = 401）。"""

        turn = self.claimed_turn()
        response = self.client.post(
            f"/api/agents/{self.agent.agent_id}/chat-turns/{turn.id}/complete",
            json={"success": True, "content": "sneaky"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "agent_project_token_required")


if __name__ == "__main__":
    unittest.main()
