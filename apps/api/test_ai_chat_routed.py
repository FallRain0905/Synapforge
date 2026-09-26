"""/ask 渠道优先路由（`ai_chat.chat_routed` + `POST /api/ai/chat`）契约测试。

覆盖口径（与 ai_chat.chat_routed 的文档一致）：

- 渠道接得住：成员**零配置**即可用（via=channel），额度按上游 usage 扣减、记流水；
- 渠道优先：成员配了自己的 key、模型相同 → 仍走渠道，成员自己的上游**零调用**；
- 回退：渠道不接的模型 → 走成员自配（via=own_key）；
- **不静默回落**：额度耗尽 → 429、渠道上游 500 → 502，成员自己的上游零调用（不悄悄花钱）；
- 无渠道且无凭据 → 400 ai_credentials_missing；
- 流式：渠道路径产出 delta（首块带 via）+ [DONE]，结束后扣额度。

一律用本机假上游（http.server），不出现真实 key。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

from app import knowledge_base, llm_channels, main
from app.contracts import SessionCreate
from app.store import Store
from tests_support import ensure_member
from test_llm_channels import FakeUpstream, _free_port

ADMIN = "member-001"
PLAIN = "member-chat-plain"


class SseUpstream:
    """SSE 版假上游：逐帧 data:，最后一帧带 usage（供流式路径断言扣减）。"""

    def __init__(self, frames: list[str], usage: dict | None = None) -> None:
        self.requests: list[dict] = []
        self.port = _free_port()
        upstream = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                upstream.requests.append({"path": self.path, "body": json.loads(raw) if raw else {}})
                payload = "".join(frames).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class ChatRoutedTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        llm_channels.ensure_schema(self.store)
        knowledge_base.ensure_kb_tables(self.store)
        ensure_member(self.store, PLAIN)
        # MEMBER 提为管理员：渠道经 API 建出来（走真实建渠路径）
        self.store.db.execute("UPDATE human_members SET is_admin = 1 WHERE id = ?", (ADMIN,))
        self.store.db.commit()
        self.admin_headers = {
            "Authorization": f"Bearer {self.store.create_session(SessionCreate(member_id=ADMIN, expires_in_seconds=900)).token}"
        }
        self.member_headers = {
            "Authorization": f"Bearer {self.store.create_session(SessionCreate(member_id=PLAIN, expires_in_seconds=900)).token}"
        }
        self.upstreams: list = []

    def tearDown(self) -> None:
        for upstream in self.upstreams:
            upstream.close()
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # -- 工具 ---------------------------------------------------------------
    def make_upstream(self, script=None) -> FakeUpstream:
        upstream = FakeUpstream(script)
        self.upstreams.append(upstream)
        return upstream

    def create_channel(self, upstream: FakeUpstream, models: list[str]) -> dict:
        response = self.client.post(
            "/api/admin/llm-channels",
            json={
                "name": f"路由渠道-{len(self.upstreams)}",
                "base_url": upstream.base_url,
                "api_key": "sk-fake-routed-0001",
                "models": models,
                "priority": 10,
                "enabled": True,
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def set_own_credentials(self, upstream: FakeUpstream, model: str) -> None:
        knowledge_base.save_ai_settings(
            self.store,
            PLAIN,
            {"llm_api_key": "sk-fake-own-0001", "llm_base_url": upstream.base_url, "llm_model": model},
        )

    def exhaust_quota(self) -> None:
        org = self.store.db.execute("SELECT organization_id FROM human_members WHERE id = ?", (PLAIN,)).fetchone()["organization_id"]
        quota = llm_channels.get_quota(self.store, PLAIN, organization_id=str(org))
        with llm_channels._transaction(self.store):
            self.store.db.execute(
                "UPDATE llm_member_quotas SET tokens_used = token_limit WHERE member_id = ?", (PLAIN,)
            )

    def ask(self, member_headers: dict, **payload) -> object:
        body = {"messages": [{"role": "user", "content": "hi"}], **payload}
        return self.client.post("/api/ai/chat", json=body, headers=member_headers)


class ChannelFirstTest(ChatRoutedTestBase):
    def test_channel_serves_model_member_needs_no_credentials(self) -> None:
        upstream = self.make_upstream(
            [(200, {"choices": [{"message": {"content": "pong"}}], "usage": {"prompt_tokens": 30, "completion_tokens": 12}})]
        )
        self.create_channel(upstream, ["chan-model", "spare-model"])
        response = self.ask(self.member_headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["via"], "channel")
        self.assertEqual(body["content"], "pong")
        # 缺省模型 = 优先级最高的渠道的第一个模型
        self.assertEqual(upstream.requests[0]["body"]["model"], "chan-model")
        # 额度按 usage 扣减、记流水
        quota = self.client.get("/api/llm/quota", headers=self.member_headers).json()
        self.assertEqual(quota["tokens_used"], 42)
        row = self.store.db.execute("SELECT member_id, status FROM llm_usage_log").fetchone()
        self.assertEqual((row["member_id"], row["status"]), (PLAIN, "ok"))

    def test_channel_preferred_over_own_credentials(self) -> None:
        upstream = self.make_upstream()
        own = self.make_upstream()
        self.create_channel(upstream, ["chan-model"])
        self.set_own_credentials(own, "chan-model")  # 模型相同，成员也配了自己的 key
        response = self.ask(self.member_headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["via"], "channel")
        self.assertEqual(own.requests, [], "渠道接得住时不应打成员自己的上游")

    def test_fallback_to_own_credentials_when_no_route(self) -> None:
        upstream = self.make_upstream()
        own = self.make_upstream()
        self.create_channel(upstream, ["chan-model"])
        self.set_own_credentials(own, "own-model")  # 渠道不接这个模型
        response = self.ask(self.member_headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["via"], "own_key")
        self.assertEqual(len(own.requests), 1)
        self.assertEqual(upstream.requests, [], "回退时不应打渠道上游")

    def test_no_channel_and_no_credentials_missing(self) -> None:
        response = self.ask(self.member_headers)
        self.assertEqual(response.status_code, 400)
        self.assertIn("ai_credentials_missing", response.json()["detail"])

    def test_quota_exceeded_no_silent_fallback(self) -> None:
        upstream = self.make_upstream()
        own = self.make_upstream()
        self.create_channel(upstream, ["chan-model"])
        self.set_own_credentials(own, "chan-model")  # 成员有自己的 key，但也不该被悄悄花
        self.exhaust_quota()
        response = self.ask(self.member_headers)
        self.assertEqual(response.status_code, 429, response.text)
        self.assertIn("llm_quota_exceeded", response.json()["detail"])
        self.assertEqual(upstream.requests, [], "额度闸门应在打上游之前")
        self.assertEqual(own.requests, [], "额度耗尽绝不能回落到成员自配 key")

    def test_channel_upstream_error_no_fallback(self) -> None:
        upstream = self.make_upstream([(500, {"error": {"message": "boom"}})])
        own = self.make_upstream()
        self.create_channel(upstream, ["chan-model"])
        self.set_own_credentials(own, "chan-model")
        response = self.ask(self.member_headers)
        self.assertEqual(response.status_code, 502, response.text)
        self.assertIn("llm_upstream_error", response.json()["detail"])
        self.assertEqual(own.requests, [], "上游 500 也不能回落到成员自配 key")
        row = self.store.db.execute("SELECT status, error_code FROM llm_usage_log").fetchone()
        self.assertEqual((row["status"], row["error_code"]), ("error", "llm_upstream_error"))


class ChannelStreamTest(ChatRoutedTestBase):
    def test_stream_via_channel(self) -> None:
        frames = [
            'data: {"choices":[{"delta":{"content":"你"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"好"}}]}\n\n',
            'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n',
            "data: [DONE]\n\n",
        ]
        upstream = SseUpstream(frames)
        self.upstreams.append(upstream)
        self.create_channel(upstream, ["chan-model"])
        response = self.ask(self.member_headers, stream=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn('"via": "channel"', response.text)
        self.assertIn("你", response.text)
        self.assertIn("[DONE]", response.text)
        quota = self.client.get("/api/llm/quota", headers=self.member_headers).json()
        self.assertEqual(quota["tokens_used"], 8, "流式结束后应按最后 usage 扣减")


if __name__ == "__main__":
    unittest.main()
