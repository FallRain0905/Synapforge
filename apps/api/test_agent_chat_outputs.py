"""对话产出与「转入云盘」契约测试（2026-09-24）。

用户要的是：执行体在对话里生成文件之后，人能**下载**，也能**一键转进个人云盘**。
所以这里盯两件事：
1. 产出清单跟着轮次走（`complete` 上报 → 列表读得到），且**坏数据不拖垮整轮**；
2. 「转入云盘」是**复制**（成果物留在项目里），并且只有项目成员能取。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID

from fastapi.testclient import TestClient
from starlette.requests import Request

from app import agent_chat, main, personal_drive
from app.contracts import (
    AgentChatConversationCreate,
    AgentChatMessageCreate,
    AgentChatTurnComplete,
    AgentChatTurnOutput,
    AgentRegister,
    ArtifactCreate,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

MEMBER = "member-001"
DEVICE = "device-outputs-001"


class ChatOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="outputs-agent", display_name="Outputs Agent", owner_member_id=MEMBER)
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential = self.store.register_device(
            registration_request(pairing, Ed25519PrivateKey.generate(), self.agent.agent_id, DEVICE, device_name="Box", capabilities=["chat.run"])
        )
        self.device = credential.device
        self.store.create_device_project_grant(
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
        turn = agent_chat.append_message(self.store, conversation.id, MEMBER, AgentChatMessageCreate(content="写个文件"))
        agent_chat.claim_turn(self.store, self.agent.agent_id, self.project.id, 900)
        return turn

    def artifact(self, name: str = "notes.md", content: bytes | str = b"# hello\n") -> str:
        payload = content.encode("utf-8") if isinstance(content, str) else content
        created = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name=name, artifact_type="paper_source", mime_type="text/markdown"),
            created_by=MEMBER,
            created_by_kind="member",
        )
        self.store.store_artifact_content(created.id, payload, "text/markdown", None)
        return str(created.id)

    # ---- 产出清单 ---------------------------------------------------------

    def test_outputs_are_stored_on_the_turn_and_read_back(self) -> None:
        turn = self.pending_turn()
        artifact_id = self.artifact()
        agent_chat.complete_turn(
            self.store,
            turn.id,
            self.agent.agent_id,
            AgentChatTurnComplete(
                success=True,
                content="写好了",
                outputs=[
                    AgentChatTurnOutput(
                        artifact_id=UUID(artifact_id),
                        name="notes.md",
                        size_bytes=8,
                        artifact_type="paper_source",
                        mime_type="text/markdown",
                        relative_path="notes.md",
                    )
                ],
            ),
        )
        stored = agent_chat.get_turn(self.store, turn.id)
        self.assertEqual([item.name for item in stored.outputs], ["notes.md"])
        self.assertEqual(str(stored.outputs[0].artifact_id), artifact_id)
        # 列表接口（页面就读这个）也带着产出
        listed = self.client.get(f"/api/my-agent/conversations/{turn.conversation_id}/turns")
        # 没有会话令牌时是 development 模式下的 member-001：能读到自己那条
        self.assertEqual(listed.status_code, 200)
        payload = listed.json()
        self.assertEqual(payload[0]["outputs"][0]["relative_path"], "notes.md")

    def test_bad_output_rows_do_not_break_the_turn(self) -> None:
        """手改库/历史行里出现坏数据时：跳过坏的那条，整轮照样读得出来。"""

        turn = self.pending_turn()
        good = self.artifact()
        raw = json.dumps([{"name": "缺 id"}, {"artifact_id": good, "name": "ok.md", "size_bytes": 3}])
        self.store.db.execute("UPDATE agent_turns SET outputs = ? WHERE id = ?", (raw, str(turn.id)))
        self.store.db.commit()
        stored = agent_chat.get_turn(self.store, turn.id)
        self.assertEqual([item.name for item in stored.outputs], ["ok.md"])

    def test_repeated_complete_keeps_the_first_outputs(self) -> None:
        """幂等：网络重试不该把结果覆盖成空的（与内容/用量的口径一致）。"""

        turn = self.pending_turn()
        artifact_id = self.artifact()
        first = AgentChatTurnComplete(
            success=True,
            content="第一次",
            outputs=[AgentChatTurnOutput(artifact_id=UUID(artifact_id), name="notes.md", size_bytes=8)],
        )
        agent_chat.complete_turn(self.store, turn.id, self.agent.agent_id, first)
        agent_chat.complete_turn(self.store, turn.id, self.agent.agent_id, AgentChatTurnComplete(success=True, content="第二次"))
        stored = agent_chat.get_turn(self.store, turn.id)
        self.assertEqual(stored.content, "第一次")
        self.assertEqual([item.name for item in stored.outputs], ["notes.md"])

    # ---- 转入云盘 ---------------------------------------------------------

    def test_copy_artifact_to_drive_is_a_copy_with_content(self) -> None:
        artifact_id = self.artifact(content="# 内容\n正文")
        response = self.client.post(f"/api/drive/from-artifact/{artifact_id}")
        self.assertEqual(response.status_code, 200, response.text)
        entry = response.json()
        self.assertEqual(entry["name"], "notes.md")
        self.assertEqual(entry["source_artifact_id"], artifact_id)
        stored, content = personal_drive.read_drive_file(self.store, MEMBER, entry["id"])
        self.assertEqual(content, "# 内容\n正文".encode("utf-8"))
        # 成果物还在项目里（复制不是移动）
        self.assertEqual(self.store.get_artifact(UUID(artifact_id)).name, "notes.md")

    def test_copy_artifact_to_drive_is_deduplicated_by_content(self) -> None:
        artifact_id = self.artifact(content="同样的内容")
        first = self.client.post(f"/api/drive/from-artifact/{artifact_id}").json()
        second = self.client.post(f"/api/drive/from-artifact/{artifact_id}").json()
        self.assertEqual(first["id"], second["id"])  # 内容寻址：同内容复用同一条记录
        self.assertEqual(len(personal_drive.list_drive_files(self.store, MEMBER)), 1)

    def test_copy_artifact_to_drive_requires_project_membership(self) -> None:
        """只有项目成员能取：建一个**真实成员**（不在这个项目里）→ 403。"""

        from app.contracts import HumanMemberCreate

        artifact_id = self.artifact()
        outsider = self.store.create_member(
            HumanMemberCreate(organization_id=UUID(DEV_ORG_ID), email="outsider@example.test", display_name="局外人")
        )
        with mock.patch.object(main, "_request_member_id", return_value=outsider.id):
            response = self.client.post(f"/api/drive/from-artifact/{artifact_id}")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "project_membership_required")

    def test_copy_artifact_to_drive_unknown_artifact_is_404(self) -> None:
        response = self.client.post("/api/drive/from-artifact/00000000-0000-0000-0000-000000000000")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()