"""MY-AGENT 契约测试：「对话」会话与轮次（与任务体系分开的"单纯对话"）。

口径（用户拍板的）：**「项目工作」跑任务、「对话」是单纯对话**——所以这些测试同时盯住两件事：
它按预期工作（取活→回传→续会话），以及它**不碰任务体系**（不建 Task、不进任务板）。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import agent_chat, main
from app.contracts import (
    ArtifactCreate,
    AgentChatConversationCreate,
    AgentChatConversationUpdate,
    AgentChatMessageCreate,
    AgentChatTurnClaim,
    AgentChatTurnComplete,
    AgentChatTurnEventReport,
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    ProjectCreate,
    SessionCreate,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request

MEMBER = "member-001"
DEVICE = "device-chat-001"


class AgentChatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="chat-agent", display_name="Chat Agent", owner_member_id=MEMBER)
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                self.agent.agent_id,
                DEVICE,
                device_name="Chat box",
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

    def conversation(self):
        return agent_chat.create_conversation(
            self.store,
            MEMBER,
            AgentChatConversationCreate(
                project_id=self.project.id,
                device_id=self.device.device_id,
                model="deepseek/deepseek-v4.1-flash",
            ),
        )

    def say(self, conversation, text: str):
        return agent_chat.append_message(self.store, conversation.id, MEMBER, AgentChatMessageCreate(content=text))

    def claim(self):
        return agent_chat.claim_turn(self.store, self.agent.agent_id, self.project.id, 900)

    def finish(self, turn_id, *, content="好的", session_key="ses_real_001", usage=None, success=True):
        return agent_chat.complete_turn(
            self.store,
            turn_id,
            self.agent.agent_id,
            AgentChatTurnComplete(
                success=success,
                content=content,
                session_key=session_key,
                usage=usage or {"total_tokens": 100, "source": "opencode-jsonl"},
                error="" if success else "boom",
            ),
        )

    @staticmethod
    def _request(token: str | None = None, agent_id: str = "chat-agent") -> Request:
        headers = []
        if token is not None:
            headers.append((b"x-project-capability-token", token.encode("utf-8")))
            # 能力令牌之外还要 X-Agent-Id：_require_agent_capability 会核对它属于这台设备
            headers.append((b"x-agent-id", agent_id.encode("utf-8")))
        return Request({"type": "http", "method": "POST", "path": "/api/agent-chat", "headers": headers})

    # ---- 会话与轮次 -------------------------------------------------------

    def test_conversation_requires_a_grant_with_chat_capability(self) -> None:
        restricted = self.store.create_device_project_grant(
            self.project.id,
            DeviceProjectGrantCreate(device_id=self.device.device_id, capabilities=["task.claim"]),
            MEMBER,
        )
        self.assertIsNotNone(restricted.project_token)
        # 覆盖掉刚才那份带 chat.run 的授权，模拟"只授了任务能力"的设备
        self.store.db.execute("UPDATE device_project_grants SET revoked_at = ? WHERE id = ?", ("2026-01-01T00:00:00+00:00", str(self.grant.grant.id)))
        self.store.db.commit()
        with self.assertRaisesRegex(agent_chat.AgentChatError, "device_grant_missing_chat_capability"):
            self.conversation()

    def test_two_turns_keep_the_session_handle_and_do_not_touch_tasks(self) -> None:
        conversation = self.conversation()
        turns_before = self.store.db.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]

        first = self.say(conversation, "把 summary.md 的错别字改掉")
        self.assertEqual(first.status, "PENDING")
        claim = self.claim()
        self.assertIsNotNone(claim)
        self.assertEqual(str(claim.turn.id), str(first.id))
        self.assertIsNone(claim.turn.session_key)  # 第一轮没有句柄
        self.assertIn("{prompt}", " ".join(claim.command_template))
        self.finish(first.id)
        self.assertEqual(agent_chat.get_conversation(self.store, conversation.id, MEMBER).session_key, "ses_real_001")

        second = self.say(conversation, "再把数字口径统一一下")
        claim2 = self.claim()
        self.assertEqual(str(claim2.turn.id), str(second.id))
        self.assertEqual(claim2.turn.session_key, "ses_real_001")  # 句柄带出去了 → 执行体续上下文
        self.assertEqual(claim2.turn.model, "deepseek/deepseek-v4.1-flash")

        turns = agent_chat.list_turns(self.store, conversation.id, MEMBER)
        self.assertEqual([turn.status for turn in turns], ["DONE", "CLAIMED"])
        self.assertEqual(turns[0].content, "好的")
        self.assertEqual(turns[0].usage["source"], "opencode-jsonl")
        # 「对话」不该在任务板上留痕
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"], turns_before)

    def artifact(self, name: str = "报告.md", content: bytes = b"# report"):
        from app.contracts import ArtifactCreate as _ArtifactCreate

        created = self.store.create_artifact(
            self.project.id, _ArtifactCreate(name=name, artifact_type="problem_source", mime_type="text/markdown")
        )
        self.store.store_artifact_content(created.id, content)
        return created

    def test_attachments_ride_along_with_the_turn_and_reach_the_agent(self) -> None:
        """M-3：附件随轮次走，claim 时执行体拿到"要下什么"（id + 真名）。"""

        artifact = self.artifact()
        conversation = self.conversation()
        turn = agent_chat.append_message(
            self.store,
            conversation.id,
            MEMBER,
            AgentChatMessageCreate(content="把这份报告的错别字改掉", artifact_ids=[artifact.id]),
        )
        self.assertEqual([str(item.artifact_id) for item in turn.artifacts], [str(artifact.id)])
        self.assertEqual(turn.artifacts[0].name, "报告.md")
        claim = self.claim()
        self.assertEqual(len(claim.input_files), 1)
        self.assertEqual(claim.input_files[0].name, "报告.md")
        self.assertEqual(str(claim.input_files[0].artifact_id), str(artifact.id))

    def test_attachment_from_another_project_is_refused(self) -> None:
        other = self.store.create_project(ProjectCreate(name="别的项目", created_by=MEMBER))
        foreign = self.store.create_artifact(other.id, ArtifactCreate(name="别人的.md", artifact_type="problem_source"))
        conversation = self.conversation()
        with self.assertRaisesRegex(agent_chat.AgentChatError, "artifact_not_in_project"):
            agent_chat.append_message(
                self.store,
                conversation.id,
                MEMBER,
                AgentChatMessageCreate(content="看一眼", artifact_ids=[foreign.id]),
            )

    def test_agent_can_download_an_artifact_it_did_not_create(self) -> None:
        """下载与上传的鉴权**刻意不同**：输入通常是人上传的，只要求 artifact.read。"""

        artifact = self.artifact(name="输入.md", content=b"hello-input")
        response = main.download_agent_artifact_content(artifact.id, self._request(token=self.grant.project_token))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"hello-input")
        # 有 Agent 身份但**没有能力令牌** → 401（中间件在 required 模式下也会挡住；这里直接验鉴权函数）
        bare = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/agent/artifacts/x/content",
                "headers": [(b"x-agent-id", b"chat-agent")],
            }
        )
        with self.assertRaises(HTTPException) as caught:
            main.download_agent_artifact_content(artifact.id, bare)
        self.assertEqual(caught.exception.status_code, 401)
        self.assertIn("agent_project_token_required", str(caught.exception.detail))

    def test_agent_can_read_turn_status_for_cancellation(self) -> None:
        """执行体跑的过程中要能读到"被取消了"（M-4 真中断的前提）。"""

        conversation = self.conversation()
        turn = self.say(conversation, "跑一个很久的命令")
        request = self._request(token=self.grant.project_token)
        main.claim_chat_turn(
            self.agent.agent_id,
            AgentChatTurnClaim(agent_id=self.agent.agent_id, project_id=self.project.id),
            request,
        )
        read = main.read_chat_turn(self.agent.agent_id, turn.id, request)
        self.assertEqual(read.status, "CLAIMED")
        agent_chat.cancel_turn(self.store, turn.id, MEMBER)
        self.assertEqual(main.read_chat_turn(self.agent.agent_id, turn.id, request).status, "CANCELLED")
        # 没有能力令牌 → 401
        bare = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/agents/x/chat-turns/y",
                "headers": [(b"x-agent-id", b"chat-agent")],
            }
        )
        with self.assertRaises(HTTPException) as caught:
            main.read_chat_turn(self.agent.agent_id, turn.id, bare)
        self.assertEqual(caught.exception.status_code, 401)

    def test_title_comes_from_the_first_message(self) -> None:
        conversation = self.conversation()
        self.assertEqual(conversation.title, "")
        self.say(conversation, "帮我看看这份建模报告的结构")
        self.assertEqual(
            agent_chat.get_conversation(self.store, conversation.id, MEMBER).title, "帮我看看这份建模报告的结构"
        )

    def test_busy_conversation_refuses_a_second_inflight_message(self) -> None:
        conversation = self.conversation()
        self.say(conversation, "第一句")
        with self.assertRaisesRegex(agent_chat.AgentChatError, "conversation_busy"):
            self.say(conversation, "第二句")
        self.finish(agent_chat.list_turns(self.store, conversation.id, MEMBER)[0].id)
        self.say(conversation, "第二句")  # 上一轮结束后可以继续

    def test_turn_events_are_deduplicated_by_sequence(self) -> None:
        conversation = self.conversation()
        turn = self.say(conversation, "跑一下")
        self.claim()
        first = agent_chat.record_turn_event(
            self.store,
            turn.id,
            self.agent.agent_id,
            AgentChatTurnEventReport(event_type="agent.message", sequence=1, payload={"text": "在看"}),
        )
        again = agent_chat.record_turn_event(
            self.store,
            turn.id,
            self.agent.agent_id,
            AgentChatTurnEventReport(event_type="agent.message", sequence=1, payload={"text": "在看"}),
        )
        self.assertEqual(str(first.id), str(again.id))
        self.assertEqual(len(agent_chat.list_turn_events(self.store, turn.id, MEMBER)), 1)

    def test_failed_turn_keeps_the_error_and_can_be_retried(self) -> None:
        conversation = self.conversation()
        turn = self.say(conversation, "跑一下")
        self.claim()
        failed = self.finish(turn.id, success=False)
        self.assertEqual(failed.status, "FAILED")
        self.assertEqual(failed.error, "boom")
        self.say(conversation, "再试一次")

    def test_cancel_and_delete(self) -> None:
        conversation = self.conversation()
        turn = self.say(conversation, "别跑了")
        cancelled = agent_chat.cancel_turn(self.store, turn.id, MEMBER)
        self.assertEqual(cancelled.status, "CANCELLED")
        with self.assertRaisesRegex(agent_chat.AgentChatError, "turn_already_finished"):
            agent_chat.cancel_turn(self.store, turn.id, MEMBER)
        agent_chat.delete_conversation(self.store, conversation.id, MEMBER)
        with self.assertRaisesRegex(agent_chat.AgentChatError, "conversation_not_found"):
            agent_chat.get_conversation(self.store, conversation.id, MEMBER)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM agent_turns").fetchone()[0], 0)

    def test_claim_is_scoped_to_the_device_project(self) -> None:
        other = self.store.create_project(ProjectCreate(name="另一个项目", created_by=MEMBER))
        self.assertIsNone(agent_chat.claim_turn(self.store, self.agent.agent_id, other.id, 900))
        with self.assertRaisesRegex(agent_chat.AgentChatError, "agent_device_not_found"):
            agent_chat.claim_turn(self.store, "agent-does-not-exist", self.project.id, 900)

    def test_complete_from_another_agent_is_refused(self) -> None:
        conversation = self.conversation()
        turn = self.say(conversation, "跑一下")
        self.claim()
        with self.assertRaisesRegex(agent_chat.AgentChatError, "turn_claimed_by_other_agent"):
            agent_chat.complete_turn(
                self.store, turn.id, "agent-somebody-else", AgentChatTurnComplete(success=True, content="x")
            )

    def test_my_agents_lists_models_and_conversation_count(self) -> None:
        conversation = self.conversation()
        self.say(conversation, "问一句")
        self.store.db.execute(
            "INSERT INTO device_runtime_state (device_id, resource_summary, reported_at) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET resource_summary = excluded.resource_summary",
            (
                DEVICE,
                '{"models": ["deepseek/deepseek-v4.1-flash"], "default_model": "deepseek/deepseek-v4.1-flash", "executor": "opencode"}',
                "2026-09-23T00:00:00+00:00",
            ),
        )
        self.store.db.commit()
        endpoints = agent_chat.list_my_agents(self.store, MEMBER)
        self.assertEqual(len(endpoints), 1)
        entry = endpoints[0]
        self.assertEqual(entry.device_id, DEVICE)
        self.assertEqual(entry.models, ["deepseek/deepseek-v4.1-flash"])
        self.assertEqual(entry.default_model, "deepseek/deepseek-v4.1-flash")
        self.assertEqual(entry.executor, "opencode")
        self.assertEqual(entry.conversation_count, 1)
        # 别人名下的设备不该出现在我这里
        self.assertEqual(agent_chat.list_my_agents(self.store, "member-someone-else"), [])

    # ---- 角色（M-6）------------------------------------------------------

    def test_role_is_stored_on_the_turn_and_reaches_the_agent(self) -> None:
        """角色在**建这一轮**时定下来：历史里"这轮谁跑的"不会被后来的会话设置改写。"""

        conversation = agent_chat.create_conversation(
            self.store,
            MEMBER,
            AgentChatConversationCreate(
                project_id=self.project.id, device_id=self.device.device_id, model="m", role="mm-review"
            ),
        )
        self.assertEqual(conversation.role, "mm-review")
        turn = self.say(conversation, "帮我看这个结果")
        self.assertEqual(turn.role, "mm-review")
        claim = self.claim()
        self.assertEqual(claim.turn.role, "mm-review")  # 执行体据此插 `--agent mm-review`

        # 中途换角色：只影响下一轮，已经跑过的那一轮仍然是 mm-review
        agent_chat.update_conversation(
            self.store, conversation.id, MEMBER, AgentChatConversationUpdate(role="mm-paper-zh")
        )
        self.finish(turn.id)
        self.assertEqual(agent_chat.get_turn(self.store, turn.id).role, "mm-review")
        self.assertEqual(agent_chat.get_conversation(self.store, conversation.id, MEMBER).role, "mm-paper-zh")
        self.assertEqual(self.say(conversation, "再写一段").role, "mm-paper-zh")

    def test_variant_is_frozen_on_each_turn_and_reaches_the_agent(self) -> None:
        conversation = agent_chat.create_conversation(
            self.store,
            MEMBER,
            AgentChatConversationCreate(
                project_id=self.project.id,
                device_id=self.device.device_id,
                model="m",
                variant="high",
            ),
        )
        first = self.say(conversation, "仔细想")
        self.assertEqual(first.variant, "high")
        self.assertEqual(self.claim().turn.variant, "high")
        agent_chat.update_conversation(
            self.store, conversation.id, MEMBER, AgentChatConversationUpdate(variant="minimal")
        )
        self.finish(first.id)
        self.assertEqual(agent_chat.get_turn(self.store, first.id).variant, "high")
        self.assertEqual(self.say(conversation, "简短想").variant, "minimal")

    def test_empty_role_means_default_and_is_not_invented(self) -> None:
        """没选角色就是空串——**不能凭空塞一个角色名**（否则每一轮都会带 `--agent`）。"""

        conversation = self.conversation()
        self.assertEqual(conversation.role, "")
        self.assertEqual(self.say(conversation, "你好").role, "")
        self.assertEqual(self.claim().turn.role, "")

    def test_model_falls_back_the_same_way_as_the_page_does(self) -> None:
        """不带模型建会话时：心跳 → 注册自报，两级兜底与 `list_my_agents` 同一口径。

        实测踩到过：心跳里模型列表正好是空的（执行体重启后第一次探测超时），页面按注册值显示了一个默认模型，
        建出来的会话却是空模型——两条路径口径不一致，界面与行为对不上。
        """

        self.store.db.execute(
            "UPDATE agents SET model_name = ? WHERE agent_id = ?", ("deepseek/deepseek-v4.1-flash", "chat-agent")
        )
        self.store.db.commit()
        # 心跳里没有模型 → 用注册值
        conversation = agent_chat.create_conversation(
            self.store, MEMBER, AgentChatConversationCreate(project_id=self.project.id, device_id=DEVICE)
        )
        self.assertEqual(conversation.model, "deepseek/deepseek-v4.1-flash")
        # `unspecified` 是注册占位值，**不是模型**：两级都没有就如实留空（不编一个模型名）
        self.store.db.execute("UPDATE agents SET model_name = 'unspecified' WHERE agent_id = 'chat-agent'")
        self.store.db.commit()
        blank = agent_chat.create_conversation(
            self.store, MEMBER, AgentChatConversationCreate(project_id=self.project.id, device_id=DEVICE)
        )
        self.assertEqual(blank.model, "")

    def test_role_update_only_touches_the_fields_given_and_checks_ownership(self) -> None:
        conversation = self.conversation()
        updated = agent_chat.update_conversation(
            self.store, conversation.id, MEMBER, AgentChatConversationUpdate(role="mm-research")
        )
        self.assertEqual(updated.role, "mm-research")
        self.assertEqual(updated.model, "deepseek/deepseek-v4.1-flash")  # 没传的字段保持原样
        with self.assertRaises(agent_chat.AgentChatError):
            agent_chat.update_conversation(
                self.store, conversation.id, "member-someone-else", AgentChatConversationUpdate(role="mm-review")
            )

    def test_my_agents_reports_roles_from_the_executor_probe(self) -> None:
        """角色下拉的数据来源：执行体心跳里的 `roles`；探不到就是空列表（页面只显示「默认」）。"""

        self.store.db.execute(
            "INSERT INTO device_runtime_state (device_id, resource_summary, reported_at) VALUES (?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET resource_summary = excluded.resource_summary",
            (
                DEVICE,
                '{"models": ["m"], "default_model": "m", "executor": "opencode", "roles": ['
                '{"name": "mm-review", "description": "逻辑对抗复核", "executes": false},'
                '{"name": "mm-coding", "description": "编程实现", "executes": true},'
                '{"name": "", "description": "空名要丢"},'
                '{"name": "plan", "description": "计划模式"}, "垃圾数据"]}',
                "2026-09-23T00:00:00+00:00",
            ),
        )
        self.store.db.commit()
        roles = agent_chat.list_my_agents(self.store, MEMBER)[0].roles
        self.assertEqual([role.name for role in roles], ["mm-review", "mm-coding", "plan"])
        self.assertEqual(roles[0].description, "逻辑对抗复核")
        # 执行边界随角色一起上报（页面据此标注"会改动工作目录"）；老心跳没有这个字段就是 False
        self.assertEqual([role.executes for role in roles], [False, True, False])

        self.store.db.execute(
            "UPDATE device_runtime_state SET resource_summary = ? WHERE device_id = ?",
            ('{"models": ["m"], "default_model": "m"}', DEVICE),
        )
        self.store.db.commit()
        self.assertEqual(agent_chat.list_my_agents(self.store, MEMBER)[0].roles, [])

    # ---- HTTP 层（执行体那三条走能力令牌）--------------------------------

    def test_http_claim_requires_chat_run_capability(self) -> None:
        conversation = self.conversation()
        self.say(conversation, "跑一下")
        restricted = self.store.create_device_project_grant(
            self.project.id,
            DeviceProjectGrantCreate(device_id=self.device.device_id, capabilities=["task.claim"]),
            MEMBER,
        )
        with self.assertRaises(HTTPException) as caught:
            main.claim_chat_turn(
                self.agent.agent_id,
                AgentChatTurnClaim(agent_id=self.agent.agent_id, project_id=self.project.id),
                self._request(token=restricted.project_token),
            )
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIn("capability", str(caught.exception.detail))

        result = main.claim_chat_turn(
            self.agent.agent_id,
            AgentChatTurnClaim(agent_id=self.agent.agent_id, project_id=self.project.id),
            self._request(token=self.grant.project_token),
        )
        self.assertIsNotNone(result)
        self.assertEqual(str(result.turn.id), str(agent_chat.list_turns(self.store, conversation.id, MEMBER)[0].id))

    def test_http_complete_and_events_round_trip(self) -> None:
        conversation = self.conversation()
        turn = self.say(conversation, "跑一下")
        request = self._request(token=self.grant.project_token)
        main.claim_chat_turn(
            self.agent.agent_id,
            AgentChatTurnClaim(agent_id=self.agent.agent_id, project_id=self.project.id),
            request,
        )
        event = main.report_chat_turn_event(
            self.agent.agent_id,
            turn.id,
            AgentChatTurnEventReport(event_type="thinking", sequence=1, payload={"text": "先核对假设"}),
            request,
        )
        self.assertEqual(event.event_type, "thinking")
        history = agent_chat.list_turn_events(self.store, turn.id, MEMBER)
        self.assertEqual([(item.event_type, item.payload["text"]) for item in history], [("thinking", "先核对假设")])
        done = main.complete_chat_turn(
            self.agent.agent_id,
            turn.id,
            AgentChatTurnComplete(success=True, content="改完了", session_key="ses_http_001"),
            request,
        )
        self.assertEqual(done.status, "DONE")
        self.assertEqual(done.content, "改完了")
        self.assertEqual(
            agent_chat.get_conversation(self.store, conversation.id, MEMBER).session_key, "ses_http_001"
        )

    def test_http_patch_conversation_changes_role(self) -> None:
        """HTTP 层的角色设置（M-6 新增的唯一写路由）：带会话令牌能改、别人的会话 404、越界值 422。

        为什么要 HTTP 层测试：这套页面走的是会话令牌 + `_request_member_id(request)` 注入，
        store 层测试全绿也照样可能 500（W-2 踩过：路由签名漏了 `request: Request`）。
        """

        conversation = self.conversation()
        client = TestClient(main.app)
        self.addCleanup(client.close)
        headers = {"Authorization": f"Bearer {self.store.create_session(SessionCreate(member_id=MEMBER)).token}"}

        response = client.patch(
            f"/api/my-agent/conversations/{conversation.id}", json={"role": "mm-paper-zh"}, headers=headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["role"], "mm-paper-zh")
        self.assertEqual(agent_chat.get_conversation(self.store, conversation.id, MEMBER).role, "mm-paper-zh")

        # 没有会话令牌：required 模式下必须 401（而不是静默落回开发默认成员）
        with mock.patch.dict(os.environ, {"PLATFORM_AUTH_MODE": "required"}):
            self.assertEqual(
                client.patch(
                    f"/api/my-agent/conversations/{conversation.id}", json={"role": "mm-review"}
                ).status_code,
                401,
            )
        # 角色名超长：422（契约层就挡住，不写进库）
        self.assertEqual(
            client.patch(
                f"/api/my-agent/conversations/{conversation.id}", json={"role": "x" * 81}, headers=headers
            ).status_code,
            422,
        )
        # 不属于我的会话：404
        self.assertEqual(
            client.patch(
                f"/api/my-agent/conversations/{uuid4()}", json={"role": "mm-review"}, headers=headers
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()