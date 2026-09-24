"""知识库 Phase A 契约测试：双形态、分享、文档、凭据、会话与 Gateway 代理。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from app import kb_gateway, knowledge_base, main
from app.contracts import (
    AiSettingsUpdate,
    ConversationCreate,
    KbCreate,
    KbDocumentCreate,
    KbIndexRequest,
    KbQueryRequest,
    KbShareCreate,
    MessageCreate,
)
from app.store import Store


def make_request(member: str = "member-001") -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/kb", "headers": []})


def other_member_session(store: Store, member_id: str = "member-002") -> Request:
    """创建另一个成员并签发真实会话（走 Bearer 认证路径）。"""

    from app.contracts import SessionCreate
    from datetime import UTC, datetime

    try:
        store.get_member(member_id)
    except KeyError:
        from uuid import uuid4 as uuid

        organization_id, team_id = uuid(), uuid()
        now = datetime.now(UTC).isoformat()
        store.db.execute("INSERT INTO organizations (id, name, slug, created_at) VALUES (?, ?, ?, ?)", (str(organization_id), f"KB 隔离 {member_id}", f"kb-{member_id}", now))
        store.db.execute("INSERT INTO teams (id, organization_id, name, created_at) VALUES (?, ?, ?, ?)", (str(team_id), str(organization_id), "KB 队伍", now))
        store.db.execute("INSERT INTO human_members (id, organization_id, team_id, email, display_name, status, created_at) VALUES (?, ?, ?, ?, ?, 'active', ?)", (member_id, str(organization_id), str(team_id), f"{member_id}@example.test", member_id, now))
        store.db.execute("INSERT INTO memberships (member_id, team_id, role, created_at) VALUES (?, ?, 'owner', ?)", (member_id, str(team_id), now))
        store.db.commit()
    session = store.create_session(SessionCreate(member_id=member_id, expires_in_seconds=600))
    return Request({"type": "http", "method": "POST", "path": "/api/kb", "headers": [(b"authorization", f"Bearer {session.token}".encode())]})


class KbBasicTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir) if hasattr(self.temp_dir, "name") is False else Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def test_create_project_kb_and_personal_kb(self) -> None:
        project_kb = main.create_kb(KbCreate(name="项目库", project_id=self.project.id), make_request())
        self.assertIsNotNone(project_kb["project_id"])
        personal = main.create_kb(KbCreate(name="个人库"), make_request())
        self.assertIsNone(personal["project_id"])

        project_list = main.list_project_kbs(self.project.id, make_request())
        self.assertTrue(any(item["id"] == project_kb["id"] for item in project_list))
        member_list = main.list_member_kbs(make_request())
        ids = {item["id"] for item in member_list}
        self.assertIn(personal["id"], ids)

    def test_personal_kb_sharing(self) -> None:
        personal = main.create_kb(KbCreate(name="分享库"), make_request())
        shared = main.share_kb(personal["id"], KbShareCreate(member_id="member-002"), make_request())
        self.assertEqual(shared["visibility"], "shared")
        self.assertEqual(shared["shares"][0]["member_id"], "member-002")

        other = other_member_session(self.store)
        listing = main.list_member_kbs(other)
        self.assertTrue(any(item["id"] == personal["id"] for item in listing))

    def test_share_requires_owner(self) -> None:
        personal = main.create_kb(KbCreate(name="owner-only"), make_request())
        other = other_member_session(self.store)
        with self.assertRaises(HTTPException) as caught:
            main.share_kb(personal["id"], KbShareCreate(member_id="member-003"), other)
        self.assertEqual(caught.exception.status_code, 403)

    def test_documents_and_access_control(self) -> None:
        kb = main.create_kb(KbCreate(name="文档库"), make_request())
        doc = main.add_kb_document(kb["id"], KbDocumentCreate(title="数据说明", content_md="# 数据\n附件字段 12 个。"), make_request())
        self.assertEqual(doc["index_status"], "not_indexed")
        self.assertTrue(doc["content_hash"])

        listing = main.get_kb_documents(kb["id"], make_request())
        self.assertEqual(listing[0]["content_md"], "")
        self.assertGreater(listing[0]["content_md_bytes"], 0)

        other = other_member_session(self.store)
        with self.assertRaises(HTTPException) as caught:
            main.get_kb_documents(kb["id"], other)
        self.assertEqual(caught.exception.status_code, 403)

    def test_ai_settings_masked_and_saved(self) -> None:
        initial = main.get_member_ai_settings(make_request())
        self.assertEqual(initial["llm_api_key"], "")

        saved = main.update_member_ai_settings(
            AiSettingsUpdate(
                llm_api_key="sk-test", llm_base_url="https://api.deepseek.com/v1", llm_model="deepseek-chat",
                embedding_api_key="emb-key", embedding_base_url="https://api.example.com", embedding_model="text-embedding",
                mineru_api_key="mineru-key",
            ),
            make_request(),
        )
        self.assertEqual(saved["llm_api_key"], "configured")
        # 保存响应也必须脱敏：密钥明文不出服务器（此前 PUT 会把明文回显给浏览器）
        self.assertNotIn("sk-test", json.dumps(saved, ensure_ascii=False))
        masked = main.get_member_ai_settings(make_request())
        self.assertEqual(masked["llm_api_key"], "configured")
        self.assertEqual(masked["embedding_api_key"], "configured")
        # 凭据可用性以库里的事实为准，而不是响应体
        stored = knowledge_base.get_ai_settings(main.store, "member-001")
        self.assertTrue(knowledge_base.ai_credentials_ready(stored))


class KbConversationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def test_multi_conversation_lifecycle(self) -> None:
        first = main.create_ai_conversation(ConversationCreate(title="数学建模问答"), make_request())
        second = main.create_ai_conversation(ConversationCreate(title="论文润色"), make_request())
        self.assertNotEqual(first["id"], second["id"])

        listing = main.list_ai_conversations(make_request())
        self.assertEqual(len(listing), 2)

        message = main.append_ai_message(
            first["id"], MessageCreate(role="user", content="问题一约束是什么"), make_request()
        )
        self.assertEqual(message["role"], "user")
        reply = main.append_ai_message(
            first["id"], MessageCreate(role="assistant", content="根据检索结果…", sources=[{"title": "数据说明"}]), make_request()
        )
        self.assertEqual(reply["sources"], [{"title": "数据说明"}])

        self.assertTrue(main.delete_ai_conversation(first["id"], make_request()))
        self.assertEqual(len(main.list_ai_conversations(make_request())), 1)
        with self.assertRaises(HTTPException) as caught:
            main.append_ai_message(first["id"], MessageCreate(role="user", content="gone"), make_request())
        self.assertEqual(caught.exception.status_code, 404)

    def test_rag_conversation_requires_kb_access(self) -> None:
        kb = knowledge_base.create_kb(self.store, KbCreate(name="受限库"), "member-001")
        conversation = main.create_ai_conversation(
            ConversationCreate(title="检索会话", mode="rag", kb_id=kb["id"]), make_request()
        )
        self.assertEqual(conversation["mode"], "rag")

        other = other_member_session(self.store)
        with self.assertRaises(HTTPException) as caught:
            main.create_ai_conversation(ConversationCreate(title="越权检索", mode="rag", kb_id=kb["id"]), other)
        self.assertEqual(caught.exception.status_code, 403)


class KbGatewayProxyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.kb = knowledge_base.create_kb(self.store, KbCreate(name="代理库"), "member-001")
        self.doc = knowledge_base.add_kb_document(
            self.store, self.kb["id"], KbDocumentCreate(title="测试文档", content_md="# 测试\n知识库内容。"), "member-001"
        )

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def test_query_requires_credentials_and_proxies(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.query_kb(self.kb["id"], KbQueryRequest(question="测试问题"), make_request())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("ai_credentials_missing", str(caught.exception.detail))

        main.update_member_ai_settings(
            AiSettingsUpdate(llm_api_key="k", llm_base_url="https://api.test", llm_model="m",
                             embedding_api_key="e", embedding_base_url="https://emb.test", embedding_model="emb"),
            make_request(),
        )
        fake_result = {"response": "答案", "entities": [], "hyperedges": [], "text_units": []}
        with mock.patch.object(kb_gateway, "query", return_value=fake_result):
            result = main.query_kb(self.kb["id"], KbQueryRequest(question="测试问题"), make_request())
        self.assertEqual(result["response"], "答案")

    def test_query_service_unavailable_is_502(self) -> None:
        main.update_member_ai_settings(
            AiSettingsUpdate(llm_api_key="k", embedding_api_key="e"), make_request()
        )
        with mock.patch.object(kb_gateway, "query", side_effect=kb_gateway.HyperRagError("hyper_rag_unavailable")):
            with self.assertRaises(HTTPException) as caught:
                main.query_kb(self.kb["id"], KbQueryRequest(question="q"), make_request())
        self.assertEqual(caught.exception.status_code, 502)

    def test_index_documents_marks_status(self) -> None:
        main.update_member_ai_settings(
            AiSettingsUpdate(llm_api_key="k", embedding_api_key="e"), make_request()
        )
        fake = {"results": [{"doc_id": self.doc["id"], "success": True}]}
        with mock.patch.object(kb_gateway, "_post", return_value=fake):
            result = main.index_kb_documents(self.kb["id"], KbIndexRequest(doc_ids=[self.doc["id"]]), make_request())
        self.assertEqual(result["indexed"], 1)
        status = knowledge_base.list_kb_documents(self.store, self.kb["id"], "member-001", include_content=True)[0]["index_status"]
        self.assertEqual(status, "indexed")

    def test_graph_endpoints_require_access(self) -> None:
        other = other_member_session(self.store)
        with self.assertRaises(HTTPException) as caught:
            main.get_kb_graph_entities(self.kb["id"], other)
        self.assertEqual(caught.exception.status_code, 403)

        fake = {"entities": [], "total": 0}
        with mock.patch.object(kb_gateway, "entities", return_value=fake):
            result = main.get_kb_graph_entities(self.kb["id"], make_request())
        self.assertEqual(result["total"], 0)

    def test_live_service_health(self) -> None:
        try:
            result = kb_gateway.health()
        except kb_gateway.HyperRagError:
            self.skipTest("hyper-rag-service 未运行，跳过在线健康检查")
        self.assertEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()