"""「转入项目生产」（W1.2 promote）契约测试。

口径（重新定位实施计划第二期）：
- 会话产出 → 任务输入附件 + 正式任务；产物只能从**本会话**轮次 outputs 里挑（防跨会话夹带）；
- 幂等键服务端派生（会话 + 请求指纹）：同会话同载荷重复提交返回**同一任务**，载荷变了才允许再建；
- `target_agent_id` 如实做成描述里的结构化交接块（校验存在性），**不假装任务被硬性钉给某个 Agent**
  ——任务系统按能力匹配领取，真正的派发钉定属多智能体编排期（W3.2）；
- 归属：只有会话本人能 promote。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import agent_chat, main
from app.contracts import (
    AgentChatConversationCreate,
    AgentChatMessageCreate,
    AgentChatTurnComplete,
    AgentChatTurnOutput,
    AgentConversationPromote,
    AgentRegister,
    ArtifactCreate,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request

MEMBER = "member-001"
DEVICE = "device-promote-001"


class AgentPromoteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="promote-agent", display_name="Promote Agent", owner_member_id=MEMBER)
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                self.agent.agent_id,
                DEVICE,
                device_name="Promote box",
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

    def conversation_with_output(self):
        conversation = agent_chat.create_conversation(
            self.store,
            MEMBER,
            AgentChatConversationCreate(project_id=self.project.id, device_id=self.device.device_id, model="m"),
        )
        turn = agent_chat.append_message(
            self.store, conversation.id, MEMBER, AgentChatMessageCreate(content="给我画一张图")
        )
        artifact = self.store.create_artifact(
            self.project.id, ArtifactCreate(name="result-figure.png", artifact_type="figure")
        )
        agent_chat.complete_turn(
            self.store,
            turn.id,
            self.agent.agent_id,
            AgentChatTurnComplete(
                success=True,
                content="图在这里",
                outputs=[AgentChatTurnOutput(artifact_id=artifact.id, name="result-figure.png", artifact_type="figure")],
            ),
        )
        return conversation, artifact

    def promote(self, conversation_id: UUID, payload: dict):
        response = self.client.post(f"/api/my-agent/conversations/{conversation_id}/promote", json=payload)
        return response

    # ---- 正路径 ----------------------------------------------------------

    def test_promote_creates_task_with_attached_outputs(self) -> None:
        conversation, artifact = self.conversation_with_output()
        response = self.promote(
            conversation.id,
            {
                "title": "把图整理进报告",
                "description": "基于会话里那张图写一节",
                "output_artifact_ids": [str(artifact.id)],
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["task"]["title"], "把图整理进报告")
        self.assertEqual(body["attached_artifact_ids"], [str(artifact.id)])
        self.assertEqual(body["handoff_note"], "")
        # 任务真实存在且挂着输入附件
        task = self.store.get_task(UUID(body["task"]["id"]))
        self.assertIn(str(artifact.id), [str(a) for a in (task.input_artifacts or [])])

    def test_promote_is_idempotent_per_conversation_and_payload(self) -> None:
        """同一会话同一载荷重复提交 → 同一任务（服务端派生幂等键）；改载荷 → 允许新建。"""

        conversation, artifact = self.conversation_with_output()
        payload = {"title": "转入任务", "output_artifact_ids": [str(artifact.id)]}
        first = self.promote(conversation.id, payload).json()
        second = self.promote(conversation.id, dict(payload)).json()
        self.assertEqual(first["task"]["id"], second["task"]["id"])
        changed = self.promote(conversation.id, {**payload, "title": "换了个意图"}).json()
        self.assertNotEqual(first["task"]["id"], changed["task"]["id"])

    # ---- 交接意图 --------------------------------------------------------

    def test_promote_with_target_agent_writes_handoff_block(self) -> None:
        conversation, _ = self.conversation_with_output()
        response = self.promote(
            conversation.id,
            {
                "title": "交给复核 Agent",
                "target_agent_id": self.agent.agent_id,
                "handoff_context": "已完成建模，请复核假设 2 与代码边界",
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertIn("agent:promote-agent", body["handoff_note"])
        task = self.store.get_task(UUID(body["task"]["id"]))
        self.assertIn("—— 来自对话转产 ——", task.description)
        self.assertIn("agent:promote-agent", task.description)
        self.assertIn("请复核假设 2", task.description)

    def test_unknown_target_agent_rejected(self) -> None:
        conversation, _ = self.conversation_with_output()
        response = self.promote(conversation.id, {"title": "转给不存在的人", "target_agent_id": "agent-ghost"})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "target_agent_not_found")

    # ---- 边界 ------------------------------------------------------------

    def test_output_outside_conversation_rejected(self) -> None:
        """防夹带：不在本会话 outputs 里的成果物不能挂成任务输入（400 校验拒绝）。"""

        conversation, _ = self.conversation_with_output()
        response = self.promote(conversation.id, {"title": "夹带测试", "output_artifact_ids": [str(uuid4())]})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "artifact_not_in_conversation")

    def test_promote_requires_ownership(self) -> None:
        conversation, _ = self.conversation_with_output()
        response = self.promote(
            conversation.id, {"title": "标题够长", "output_artifact_ids": []}
        )  # 开发模式回落 member-001，本人可提
        self.assertEqual(response.status_code, 201)
        with self.assertRaisesRegex(agent_chat.AgentChatError, "conversation_not_owned_by_member"):
            agent_chat.promote_conversation(
                self.store,
                conversation.id,
                "member-somebody-else",
                AgentConversationPromote(title="别人的会话"),
            )

    def test_title_too_short_rejected_by_contract(self) -> None:
        conversation, _ = self.conversation_with_output()
        response = self.promote(conversation.id, {"title": "短"})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
