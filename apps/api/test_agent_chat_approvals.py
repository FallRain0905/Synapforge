"""对话权限请求（待批准卡片，M-5c S-3）契约测试。

口径（真机探针实测过三遍，这里钉住）：
- 三档决定与 opencode 的回复一一对应：`once`/`always` → APPROVED、`reject` → DENIED；
- **`reject` 之后执行体确实不做那件事**（探针里文件没被写出来）——所以这是个真闸门；
- 幂等：同一个 request id 重报不产生第二张卡；**决策只认第一次**；等不到人批就 EXPIRED（= 不执行）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import agent_chat, main
from app.contracts import (
    AgentChatConversationCreate,
    AgentChatMessageCreate,
    AgentChatTurnApprovalRequest,
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request

MEMBER = "member-001"
DEVICE = "device-approval-001"


class ChatApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="approval-agent", display_name="Approval Agent", owner_member_id=MEMBER)
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential = self.store.register_device(
            registration_request(
                pairing, Ed25519PrivateKey.generate(), self.agent.agent_id, DEVICE, device_name="Box", capabilities=["chat.run"]
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

    def pending_turn(self):
        conversation = agent_chat.create_conversation(
            self.store,
            MEMBER,
            AgentChatConversationCreate(project_id=self.project.id, device_id=self.device.device_id, model="m"),
        )
        turn = agent_chat.append_message(self.store, conversation.id, MEMBER, AgentChatMessageCreate(content="改个文件"))
        agent_chat.claim_turn(self.store, self.agent.agent_id, self.project.id, 900)
        return turn

    def report(self, turn, request_id: str = "per_probe_1"):
        return agent_chat.request_approval(
            self.store,
            turn.id,
            self.agent.agent_id,
            AgentChatTurnApprovalRequest(
                request_id=request_id,
                permission="external_directory",
                patterns=["/tmp/*"],
                summary="写工作区外的文件：/tmp/oc-perm-probe.txt",
                tool="write",
                call_id="call_1",
            ),
        )

    # ---- 上报 -------------------------------------------------------------

    def test_report_creates_a_pending_card_with_fields(self) -> None:
        turn = self.pending_turn()
        card = self.report(turn)
        self.assertEqual(card.status, "PENDING")
        self.assertEqual(card.permission, "external_directory")
        self.assertEqual(card.patterns, ["/tmp/*"])
        self.assertIn("/tmp/oc-perm-probe.txt", card.summary)
        self.assertEqual(card.decided_by, None)

    def test_report_is_idempotent_by_request_id(self) -> None:
        turn = self.pending_turn()
        first = self.report(turn)
        second = self.report(turn)
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(agent_chat.list_turn_approvals(self.store, turn.id, MEMBER)), 1)

    # ---- 决策 -------------------------------------------------------------

    def test_member_decision_records_who_decided(self) -> None:
        turn = self.pending_turn()
        self.report(turn)
        decided = agent_chat.decide_approval(self.store, turn.id, "per_probe_1", MEMBER, "reject")
        self.assertEqual(decided.status, "DENIED")
        self.assertEqual(decided.decision, "reject")
        self.assertEqual(decided.decided_by, MEMBER)
        self.assertIsNotNone(decided.decided_at)

    def test_first_decision_wins(self) -> None:
        turn = self.pending_turn()
        self.report(turn)
        agent_chat.decide_approval(self.store, turn.id, "per_probe_1", MEMBER, "once")
        again = agent_chat.decide_approval(self.store, turn.id, "per_probe_1", MEMBER, "reject")
        self.assertEqual(again.status, "APPROVED")
        self.assertEqual(again.decision, "once")

    def test_always_is_approved_too(self) -> None:
        turn = self.pending_turn()
        self.report(turn)
        decided = agent_chat.decide_approval(self.store, turn.id, "per_probe_1", MEMBER, "always")
        self.assertEqual(decided.status, "APPROVED")
        self.assertEqual(decided.decision, "always")

    def test_expired_cannot_be_decided_afterwards(self) -> None:
        """没人批（超时）→ EXPIRED；之后人再点也不改写（它已经按"不执行"回复过执行体了）。"""

        turn = self.pending_turn()
        self.report(turn)
        expired = agent_chat.expire_approval(self.store, turn.id, "per_probe_1", self.agent.agent_id)
        self.assertEqual(expired.status, "EXPIRED")
        after = agent_chat.decide_approval(self.store, turn.id, "per_probe_1", MEMBER, "always")
        self.assertEqual(after.status, "EXPIRED")

    # ---- 执行体轮询 -------------------------------------------------------

    def test_agent_polling_reads_status_and_decision(self) -> None:
        turn = self.pending_turn()
        self.report(turn)
        pending = agent_chat.approval_state(self.store, turn.id, "per_probe_1", self.agent.agent_id)
        self.assertEqual((pending.status, pending.decision), ("PENDING", ""))
        agent_chat.decide_approval(self.store, turn.id, "per_probe_1", MEMBER, "always")
        decided = agent_chat.approval_state(self.store, turn.id, "per_probe_1", self.agent.agent_id)
        self.assertEqual((decided.status, decided.decision), ("APPROVED", "always"))

    def test_unknown_request_or_foreign_turn_is_404(self) -> None:
        turn = self.pending_turn()
        with self.assertRaisesRegex(agent_chat.AgentChatError, "approval_not_found"):
            agent_chat.get_approval(self.store, turn.id, "per_missing")
        with self.assertRaisesRegex(agent_chat.AgentChatError, "turn_not_found"):
            agent_chat.list_turn_approvals(self.store, turn.id, "member-somebody-else")

    # ---- HTTP 层 ----------------------------------------------------------

    def test_http_report_read_decide_round_trip(self) -> None:
        turn = self.pending_turn()
        reported = self.client.post(
            f"/api/agents/{self.agent.agent_id}/chat-turns/{turn.id}/approvals",
            json={
                "request_id": "per_http_1",
                "permission": "external_directory",
                "patterns": ["/tmp/*"],
                "summary": "写工作区外的文件",
            },
            headers={"X-Project-Capability-Token": self.grant.project_token, "X-Agent-Id": self.agent.agent_id},
        )
        self.assertEqual(reported.status_code, 201, reported.text)
        listed = self.client.get(f"/api/my-agent/turns/{turn.id}/approvals")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([item["id"] for item in listed.json()], ["per_http_1"])
        decided = self.client.post(f"/api/my-agent/turns/{turn.id}/approvals/per_http_1", json={"decision": "reject"})
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(decided.json()["status"], "DENIED")
        state = self.client.get(
            f"/api/agents/{self.agent.agent_id}/chat-turns/{turn.id}/approvals/per_http_1",
            headers={"X-Project-Capability-Token": self.grant.project_token, "X-Agent-Id": self.agent.agent_id},
        )
        self.assertEqual(state.json(), {"status": "DENIED", "decision": "reject"})

    def test_http_invalid_decision_is_rejected_by_contract(self) -> None:
        turn = self.pending_turn()
        self.report(turn)
        response = self.client.post(f"/api/my-agent/turns/{turn.id}/approvals/per_probe_1", json={"decision": "maybe"})
        self.assertEqual(response.status_code, 422)

    def test_deleting_a_conversation_cleans_up_its_approvals(self) -> None:
        """删会话要把挂在轮次上的**每张表**都清掉：S-3 就漏了审批表，结果带权限请求的会话删不掉。"""

        turn = self.pending_turn()
        self.report(turn)
        agent_chat.delete_conversation(self.store, turn.conversation_id, MEMBER)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM agent_turn_approvals").fetchone()["n"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS n FROM agent_turns").fetchone()["n"], 0)


if __name__ == "__main__":
    unittest.main()