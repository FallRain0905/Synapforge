"""工作流 Copilot（会话式搭建助手）HTTP 契约测试。

覆盖口径（与 workflow_copilot.py 文档一致）：
- 草稿唯一合法来源是 ```workflow-draft 内部围栏：没有围栏/JSON 坏 → 有界修复后如实失败；
- 修复有界（首答 + ≤2 轮）：持续输出非法定义的模型在修复轮耗尽后报 copilot_draft_unparseable，
  且 LLM 调用次数 = 1 + MAX_AUTO_REPAIR_ROUNDS；
- synapforge-choice：合法问题围栏被接受；回答必须过服务端校验（未知问题/未知选项 → 422，
  且**不落决策、不调模型**）；合法回答落机器可读 decisions 记录；
- 会话 owner + org 作用域：跨组织读取 404；
- 预览零副作用（无任务/无运行/无事件），复用 workflow_service.preview_definition；
- 发布必须 confirmed=true + 当前修订号：缺确认 422、stale 409、非法定义 422；
- 重复发布幂等（同一草稿返回既有 workflow，不建第二个包）；
- 运行是第二次显式确认：未发布 409、confirmed=false 422、无项目权限 403（服务端检查，
  不依赖中间件）、正常路径复用 workflow_service.start_workflow_run 物化任务骨架。

一律用本机假上游（http.server），不出现真实 key；ai_chat.chat_routed 计费口径复用
test_ai_chat_routed 的 FakeUpstream 模式。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socket import socket

from fastapi.testclient import TestClient

from app import main, workflow_copilot, workflow_service
from app.contracts import ProjectCreate, SessionCreate
from app.store import Store
from tests_support import ensure_member

MEMBER = "member-001"


def _free_port() -> int:
    with socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeUpstream:
    """本机假上游：按 script 逐次应答，记录请求体供断言。"""

    def __init__(self, script: list[tuple[int, dict]] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict] = []
        self.port = _free_port()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                upstream.requests.append(json.loads(raw) if raw else {})
                status, payload = upstream.next_response()
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def next_response(self) -> tuple[int, dict]:
        if self.script:
            return self.script.pop(0)
        return 500, {"error": {"message": "script exhausted"}}

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def draft_content(text: str = "两节点工作流") -> str:
    return (
        f"好的，这是草稿：{text}\n\n"
        "```workflow-draft\n"
        + json.dumps(two_node_definition(), ensure_ascii=False)
        + "\n```\n"
    )


def two_node_definition() -> dict:
    return {
        "schema_version": 1,
        "workflow": {"key": "copilot-pack", "name": "Copilot 验收包"},
        "stages": [{"id": "facts", "title": "事实"}, {"id": "solve", "title": "求解"}],
        "role_bindings": [{"id": "solver", "role_name": "求解", "capability_requirements": ["files.write"]}],
        "nodes": [
            {"id": "collect", "stage_id": "facts", "title": "整理事实", "goal": "整理",
             "depends_on": [], "mode": "manual"},
            {"id": "solve", "stage_id": "solve", "title": "求解", "goal": "求解",
             "depends_on": ["collect"], "mode": "auto", "role_binding": "solver",
             "prompt": {"task_template": "围绕 {{input.topic}} 求解"},
             "outputs": [{"name": "result_table", "artifact_type": "result_table", "path": "outputs/result.csv"}],
             "gate_policy": "solve-gate"},
        ],
        "gate_policies": [{"id": "solve-gate", "spec": ["artifact:result_table approved"], "on_block": "escalate_human"}],
    }


def choice_content() -> str:
    return (
        "先确认范围：\n\n"
        "```synapforge-choice\n"
        + json.dumps({
            "id": "scope-check",
            "title": "确认工作流范围",
            "questions": [
                {"id": "depth", "label": "流程深度", "type": "single", "required": True,
                 "options": [{"value": "two", "label": "两节点"}, {"value": "three", "label": "三节点"}]},
                {"id": "notes", "label": "补充", "type": "text", "required": False},
            ],
        }, ensure_ascii=False)
        + "\n```\n"
    )


class CopilotTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        workflow_copilot.ensure_schema(self.store)
        workflow_service.ensure_schema(self.store)
        self.project = self.store.list_projects()[0]
        self.upstreams: list[FakeUpstream] = []

    def tearDown(self) -> None:
        for upstream in self.upstreams:
            upstream.close()
        main.store = self.previous_store
        self.client.close()
        self.store.close()
        self.temp_dir.cleanup()

    def _install_channel(self, script: list[tuple[int, dict]] | None = None) -> FakeUpstream:
        """给开发组织装一条指向假上游的启用渠道（chat_routed 会走渠道路径）。"""

        from app import llm_channels

        llm_channels.ensure_schema(self.store)
        upstream = FakeUpstream(script=script or [])
        self.upstreams.append(upstream)
        llm_channels.create_channel(
            self.store, name=f"copilot-fake-{upstream.port}", base_url=upstream.base_url,
            api_key="fake-key-not-real", models=["copilot-test-model"], priority=10,
            enabled=True, actor_member_id=MEMBER, organization_id="00000000-0000-4000-8000-000000000001",
        )
        return upstream

    def _make_session(self, *, project_id=None) -> dict:
        response = self.client.post(
            "/api/workflows/copilot/sessions",
            json={"title": "测试会话", **({"project_id": str(project_id)} if project_id else {})},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()


class CopilotTurnTests(CopilotTestBase):
    def test_turn_parses_fenced_draft_and_persists_revision(self) -> None:
        """围栏草稿被解析落库：draft_revision 推进、draft_valid 置真、via=channel。"""

        upstream = self._install_channel()
        upstream.script = [(200, {"choices": [{"message": {"content": draft_content()}}], "usage": {"total_tokens": 88}})]
        session = self._make_session()
        response = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "建一个两节点工作流"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["turn"]["status"], "completed")
        self.assertEqual(body["turn"]["via"], "channel")
        self.assertTrue(body["draft_valid"])
        self.assertEqual(body["draft_revision"], 1)
        self.assertEqual(body["draft"]["workflow"]["key"], "copilot-pack")
        self.assertEqual(body["validation_errors"], [])
        # chat_routed 渠道路径确实被调用（billing 语义复用）
        self.assertEqual(len(upstream.requests), 1)
        # usage 如实记账到轮次
        self.assertEqual(body["turn"]["usage"].get("total_tokens"), 88)

    def test_repair_loop_is_bounded(self) -> None:
        """修复有界：模型持续输出非法定义 → 1 + MAX_AUTO_REPAIR_ROUNDS 次调用后如实失败。"""

        broken = two_node_definition()
        broken["nodes"][1].pop("role_binding")  # auto 节点缺 role_binding：校验必失败
        upstream = self._install_channel()
        upstream.script = [
            (200, {"choices": [{"message": {"content": "```workflow-draft\n" + json.dumps(broken) + "\n```"}}]}) for _ in range(3)
        ]
        session = self._make_session()
        response = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "建包"})
        self.assertEqual(response.status_code, 502, response.text)
        body = response.json()
        self.assertEqual(body["detail"]["code"], "copilot_draft_unparseable")
        self.assertTrue(any("role_binding" in item for item in body["detail"]["errors"]))
        self.assertEqual(len(upstream.requests), 1 + workflow_copilot.MAX_AUTO_REPAIR_ROUNDS)
        # 会话草稿不被半成品污染
        refreshed = self.client.get(f"/api/workflows/copilot/sessions/{session['id']}").json()
        self.assertEqual(refreshed["draft_revision"], 0)

    def test_repair_succeeds_on_second_attempt(self) -> None:
        """首答非法、修复轮合法 → 草稿落库（修复真实工作，不是摆设）。"""

        broken = two_node_definition()
        broken["nodes"][1].pop("role_binding")
        good = two_node_definition()
        upstream = self._install_channel()
        upstream.script = [
            (200, {"choices": [{"message": {"content": "```workflow-draft\n" + json.dumps(broken) + "\n```"}}]}),
            (200, {"choices": [{"message": {"content": "```workflow-draft\n" + json.dumps(good) + "\n```"}}]}),
        ]
        session = self._make_session()
        response = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "建包"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["draft_valid"])
        self.assertEqual(len(upstream.requests), 2)
        # 修复轮确实把校验错误回喂给了模型
        second_messages = upstream.requests[1]["messages"]
        self.assertTrue(any("role_binding" in str(message.get("content")) for message in second_messages))

    def test_no_fence_is_rejected_not_accepted_as_prose(self) -> None:
        """没有围栏的自然语言不背接受为草稿：修复轮耗尽后失败（拒绝任意自然语言）。"""

        upstream = self._install_channel()
        upstream.script = [
            (200, {"choices": [{"message": {"content": f"这个流程大概是：先收集再求解。{index}"}}]}) for index in range(3)
        ]
        session = self._make_session()
        response = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "建包"})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body_code(response), "copilot_draft_unparseable")

    def test_choice_round_is_accepted_and_answer_validated(self) -> None:
        """提问轮：合法 synapforge-choice 被接受；回答缺省时 409 要求先答。"""

        upstream = self._install_channel()
        upstream.script = [(200, {"choices": [{"message": {"content": choice_content()}}]})]
        session = self._make_session()
        response = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "我想建个包"})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue("synapforge-choice" in body["turn"]["content"])
        # 未回答选择就继续 → 409（服务端强制，不靠前端自觉）
        followup = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "继续"})
        self.assertEqual(followup.status_code, 409)
        self.assertEqual(body_code(followup), "copilot_choice_required")
        self.assertEqual(len(upstream.requests), 1)  # 没调模型（回答校验前置）

    def test_invalid_structured_answer_is_rejected_and_not_recorded(self) -> None:
        """未知问题/未知选项 → 422；决策不落库、模型不被调用（机器可读回答，不猜）。"""

        upstream = self._install_channel()
        upstream.script = [(200, {"choices": [{"message": {"content": choice_content()}}]})]
        session = self._make_session()
        self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "我想建个包"})

        bad_unknown_question = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/turns",
            json={"message": "回答", "structured_answer": {"nonsense": "two"}},
        )
        self.assertEqual(bad_unknown_question.status_code, 422)
        self.assertEqual(body_code(bad_unknown_question), "copilot_choice_invalid")

        bad_unknown_option = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/turns",
            json={"message": "回答", "structured_answer": {"depth": "four"}},
        )
        self.assertEqual(bad_unknown_option.status_code, 422)
        self.assertEqual(body_code(bad_unknown_option), "copilot_choice_invalid")

        # 两条都失败了：决策仍是未回答占位，模型零调用
        detail = self.client.get(f"/api/workflows/copilot/sessions/{session['id']}").json()
        pending = [turn for turn in detail["turns"] if turn["status"] == "completed"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(len(upstream.requests), 1)

    def test_valid_structured_answer_is_recorded_machine_readable(self) -> None:
        """合法回答：落机器可读 decisions（answers JSON），确认上下文进下一轮。"""

        upstream = self._install_channel()
        upstream.script = [
            (200, {"choices": [{"message": {"content": choice_content()}}]}),
            (200, {"choices": [{"message": {"content": draft_content()}}]}),
        ]
        session = self._make_session()
        self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "我想建个包"})
        answer = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/turns",
            json={"message": "按选择继续", "structured_answer": {"depth": "two", "notes": "以两节点为准"}},
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        # 决策记录机器可读
        row = self.store.db.execute(
            "SELECT answers FROM workflow_copilot_decisions WHERE session_id = ? ORDER BY created_at",
            (session["id"],),
        ).fetchall()
        recorded = [json.loads(item["answers"]) for item in row]
        self.assertTrue(any(item.get("depth") == "two" for item in recorded), recorded)
        # 回答上下文确实喂给了第二轮模型
        second_messages = upstream.requests[1]["messages"]
        self.assertTrue(any("结构化选择回答" in str(m.get("content")) and "two" in str(m.get("content")) for m in second_messages))


class CopilotScopeTests(CopilotTestBase):
    def test_session_scoped_to_owner_and_organization(self) -> None:
        """owner + org 作用域：别的成员（同组织不建会话）与别的组织都看不到。"""

        session = self._make_session()
        # 同组织其他成员不是 owner：列表看不到
        ensure_member(self.store, "member-copilot-b")
        self.store.create_session(SessionCreate(member_id="member-copilot-b"))  # 拿到会话令牌才能以 B 身份请求
        token_b = self.store.create_session(SessionCreate(member_id="member-copilot-b")).token
        listed = self.client.get(
            "/api/workflows/copilot/sessions", headers={"Authorization": f"Bearer {token_b}"}
        ).json()
        self.assertEqual([item["id"] for item in listed if item["id"] == session["id"]], [])
        # 跨组织成员：404（存在性不泄露）
        from app.contracts import HumanMemberCreate, OrganizationCreate, TeamCreate

        organization = self.store.create_organization(OrganizationCreate(name="Copilot 第二组织", slug="copilot-second"))
        team = self.store.create_team(TeamCreate(organization_id=organization.id, name="Copilot 第二队伍"))
        member = self.store.create_member(HumanMemberCreate(organization_id=organization.id, team_id=team.id, email="copilot-second@example.local", display_name="Copilot 第二成员", role="owner"))
        token_other = self.store.create_session(SessionCreate(member_id=member.id)).token
        response = self.client.get(
            f"/api/workflows/copilot/sessions/{session['id']}", headers={"Authorization": f"Bearer {token_other}"}
        )
        self.assertEqual(response.status_code, 404)


class CopilotPreviewTests(CopilotTestBase):
    def _session_with_draft(self) -> dict:
        upstream = self._install_channel()
        upstream.script = [(200, {"choices": [{"message": {"content": draft_content()}}]})]
        session = self._make_session()
        response = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "建包"})
        self.assertEqual(response.status_code, 200)
        return session

    def test_preview_has_no_side_effects(self) -> None:
        """预览零副作用：无任务、无运行、无事件；plan 复用 workflow_service 形状。"""

        session = self._session_with_draft()
        tasks_before = self.store.db.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
        events_before = self.store.db.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        runs_before = self.store.db.execute("SELECT COUNT(*) AS c FROM project_workflow_runs").fetchone()["c"]
        response = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/preview")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["preview"]["valid"])
        self.assertTrue(any(node["node_id"] == "solve" for node in body["preview"]["plan"]["nodes"]))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"], tasks_before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"], events_before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM project_workflow_runs").fetchone()["c"], runs_before)

    def test_preview_empty_session_is_422(self) -> None:
        session = self._make_session()
        response = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/preview")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(body_code(response), "copilot_draft_empty")


class CopilotPublishRunTests(CopilotTestBase):
    def _session_with_valid_draft(self) -> dict:
        upstream = self._install_channel()
        upstream.script = [(200, {"choices": [{"message": {"content": draft_content()}}]})]
        session = self._make_session(project_id=self.project.id)
        response = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/turns", json={"message": "建包"})
        self.assertEqual(response.status_code, 200)
        # 轮次推进了 draft_revision：返回最新快照（创建响应里的 0 已过期）
        return self.client.get(f"/api/workflows/copilot/sessions/{session['id']}").json()

    def test_publish_requires_explicit_confirmation(self) -> None:
        """confirmed=false → 422；发布后 published_workflow_id 回执落在会话上。"""

        session = self._session_with_valid_draft()
        denied = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/publish",
            json={"confirmed": False, "draft_revision": session["draft_revision"]},
        )
        self.assertEqual(denied.status_code, 422)
        self.assertEqual(body_code(denied), "copilot_publish_not_confirmed")
        ok = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/publish",
            json={"confirmed": True, "draft_revision": session["draft_revision"]},
        )
        self.assertEqual(ok.status_code, 200, ok.text)
        body = ok.json()
        self.assertFalse(body["idempotent"])
        self.assertTrue(body["workflow"]["id"])
        self.assertEqual(body["session"]["published_workflow_id"], body["workflow"]["id"])

    def test_publish_rejects_stale_revision(self) -> None:
        """过期修订 → 409 copilot_draft_stale（防"看过就确认"的竞态）。"""

        session = self._session_with_valid_draft()
        stale = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/publish",
            json={"confirmed": True, "draft_revision": session["draft_revision"] - 1},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(body_code(stale), "copilot_draft_stale")

    def test_publish_is_idempotent_for_same_draft(self) -> None:
        """重复发布同一草稿：幂等返回既有 workflow（不建第二个包）。"""

        session = self._session_with_valid_draft()
        payload = {"confirmed": True, "draft_revision": session["draft_revision"]}
        first = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/publish", json=payload)
        second = self.client.post(f"/api/workflows/copilot/sessions/{session['id']}/publish", json=payload)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(first.json()["idempotent"])
        self.assertTrue(second.json()["idempotent"])
        self.assertEqual(first.json()["workflow"]["id"], second.json()["workflow"]["id"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM workflows WHERE key = 'copilot-pack'").fetchone()["c"], 1)

    def test_run_requires_published_version_and_second_confirmation(self) -> None:
        """运行：未发布 → 409；已发布但 confirmed=false → 422；二次确认后物化任务骨架。"""

        session = self._make_session()
        unpublished = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/run",
            json={"project_id": str(self.project.id), "confirmed": True, "inputs": {}},
        )
        self.assertEqual(unpublished.status_code, 409)
        self.assertEqual(body_code(unpublished), "copilot_not_published")

        session = self._session_with_valid_draft()
        self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/publish",
            json={"confirmed": True, "draft_revision": session["draft_revision"]},
        )
        not_confirmed = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/run",
            json={"project_id": str(self.project.id), "confirmed": False, "inputs": {}},
        )
        self.assertEqual(not_confirmed.status_code, 422)
        self.assertEqual(body_code(not_confirmed), "copilot_run_not_confirmed")

        confirmed = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/run",
            json={"project_id": str(self.project.id), "confirmed": True, "inputs": {"topic": "海冰"}},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        body = confirmed.json()
        self.assertEqual(len(body["tasks"]), 2)
        self.assertIn("骨架", body["note"])

    def test_run_checks_project_permission_server_side(self) -> None:
        """服务端项目权限检查：非项目成员即使带合法会话也 403（不走中间件旁路）。"""

        ensure_member(self.store, "member-copilot-outsider")
        token = self.store.create_session(SessionCreate(member_id="member-copilot-outsider")).token
        headers = {"Authorization": f"Bearer {token}"}
        # outsider 自己建会话 → 出草稿 → 发布（用自己的 org 上下文，不绑开发项目）
        created = self.client.post("/api/workflows/copilot/sessions", json={"title": "外部会话"}, headers=headers)
        self.assertEqual(created.status_code, 201, created.text)
        # 直接用 member-001 的会话试跑项目：member-copilot-outsider 不在项目里 → 403
        session = self._session_with_valid_draft()
        self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/publish",
            json={"confirmed": True, "draft_revision": session["draft_revision"]},
        )
        response = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/run",
            json={"project_id": str(self.project.id), "confirmed": True, "inputs": {}},
            headers=headers,
        )
        self.assertEqual(response.status_code, 403)

    def test_run_rejects_stale_published_revision(self) -> None:
        """运行携带过期 published_revision → 409。"""

        session = self._session_with_valid_draft()
        published = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/publish",
            json={"confirmed": True, "draft_revision": session["draft_revision"]},
        ).json()
        response = self.client.post(
            f"/api/workflows/copilot/sessions/{session['id']}/run",
            json={"project_id": str(self.project.id), "confirmed": True, "published_revision": published["session"]["published_revision"] + 5, "inputs": {}},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(body_code(response), "copilot_draft_stale")


class CopilotDraftEditTests(CopilotTestBase):
    def test_manual_draft_save_validates_and_bumps_revision(self) -> None:
        """人工编辑保存：立即重校验（非法 → draft_valid=false + errors）并推进修订号。"""

        session = self._make_session()
        broken = two_node_definition()
        broken["nodes"][1].pop("role_binding")
        response = self.client.put(f"/api/workflows/copilot/sessions/{session['id']}/draft", json={"definition": broken})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["session"]["draft_valid"])
        self.assertTrue(any("role_binding" in item for item in body["validation_errors"]))
        self.assertEqual(body["session"]["draft_revision"], 1)


def body_code(response) -> str:
    detail = response.json().get("detail")
    return detail.get("code") if isinstance(detail, dict) else str(detail)


if __name__ == "__main__":
    unittest.main()
