"""LLM 渠道与全员免费额度契约测试。

这一层的价值几乎全在"边界"上：

- 非管理员碰渠道接口 → 403；渠道清单里的 api_key **任何响应都不出现**；
- 模型路由：命中 / 未命中 / 渠道被禁用 / priority 冲突时的取舍；
- 额度：从默认值扣减、耗尽后 429 且拒绝原因明确、负数 = 不限量；
- 上游 4xx / 超时：如实记账并标 error，不假装成功；
- 检测与测速：成功与失败都回填，失败轮不冒充通过。

**测试一律用本机假上游（http.server），不出现任何真实 key 或真实渠道地址。**
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from app import llm_channels, main
from app.contracts import SessionCreate
from app.store import Store
from tests_support import ensure_member

ADMIN = "member-001"
PLAIN = "member-llm-plain"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeUpstream:
    """本机假上游：按 script 逐次应答，记录收到的请求体供断言。

    绝不使用真实凭据——这里连 key 都是假的。
    """

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
                try:
                    parsed = json.loads(raw) if raw else {}
                except ValueError:
                    parsed = {}
                upstream.requests.append(
                    {"path": self.path, "auth": self.headers.get("Authorization", ""), "body": parsed}
                )
                status, payload = upstream.next_response()
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def do_GET(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: object) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def next_response(self) -> tuple[int, dict]:
        if self.script:
            return self.script.pop(0)
        return 200, {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class LlmChannelTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        llm_channels.ensure_schema(self.store)
        ensure_member(self.store, PLAIN)  # 非管理员成员（is_admin 默认 0）
        # 管理员**显式置位**：库里 is_admin 默认 0，不依赖种子数据碰巧是管理员
        self.store.db.execute("UPDATE human_members SET is_admin = 1 WHERE id = ?", (ADMIN,))
        self.store.db.commit()
        self.admin_session = self.store.create_session(SessionCreate(member_id=ADMIN, expires_in_seconds=900))
        self.admin_headers = {"Authorization": f"Bearer {self.admin_session.token}"}
        self.plain_session = self.store.create_session(SessionCreate(member_id=PLAIN, expires_in_seconds=900))
        self.plain_headers = {"Authorization": f"Bearer {self.plain_session.token}"}
        self.upstreams: list[FakeUpstream] = []

    def tearDown(self) -> None:
        for upstream in self.upstreams:
            upstream.close()
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # -- 工具 ---------------------------------------------------------------
    def make_upstream(self, script: list[tuple[int, dict]] | None = None) -> FakeUpstream:
        upstream = FakeUpstream(script)
        self.upstreams.append(upstream)
        return upstream

    def create_channel(self, upstream: FakeUpstream, **overrides) -> dict:
        payload = {
            "name": overrides.pop("name", f"渠道-{uuid4().hex[:6]}"),
            "base_url": overrides.pop("base_url", upstream.base_url),
            "api_key": overrides.pop("api_key", "sk-fake-test-key-0001"),
            "models": overrides.pop("models", ["fake-model-a"]),
            "priority": overrides.pop("priority", 100),
            "enabled": overrides.pop("enabled", True),
            **overrides,
        }
        response = self.client.post("/api/admin/llm-channels", json=payload, headers=self.admin_headers)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()


class AdminAccessTest(LlmChannelTestBase):
    def test_non_admin_gets_403_on_channel_endpoints(self) -> None:
        upstream = self.make_upstream()
        channel = self.create_channel(upstream)
        # 注意：FastAPI 先做请求体校验再进 handler，所以这里每个端点都要给**合法体**，
        # 否则拿到的是 422（校验）而不是 403（鉴权），测不到想测的东西。
        calls = [
            ("get", "/api/admin/llm-channels", None),
            ("get", f"/api/admin/llm-channels/{channel['id']}", None),
            ("post", "/api/admin/llm-channels", {"name": "x", "base_url": "http://127.0.0.1:1/v1", "models": ["m"]}),
            ("patch", f"/api/admin/llm-channels/{channel['id']}", {"priority": 1}),
            ("delete", f"/api/admin/llm-channels/{channel['id']}", None),
            ("post", f"/api/admin/llm-channels/{channel['id']}/check", None),
            ("post", f"/api/admin/llm-channels/{channel['id']}/speed-test", {"rounds": 1}),
            ("get", "/api/admin/llm-usage", None),
            ("get", f"/api/admin/llm-quotas/{PLAIN}", None),
            ("put", f"/api/admin/llm-quotas/{PLAIN}", {"token_limit": 100}),
        ]
        for method, url, body in calls:
            kwargs: dict = {"headers": self.plain_headers}
            if body is not None:
                kwargs["json"] = body
            response = getattr(self.client, method)(url, **kwargs)
            self.assertEqual(response.status_code, 403, f"{method.upper()} {url} → {response.status_code}")

    def test_admin_can_list_and_members_cannot_see_channels(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream)
        listed = self.client.get("/api/admin/llm-channels", headers=self.admin_headers)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["channels"]), 1)
        self.assertEqual(listed.json()["default_quota_tokens"], llm_channels.DEFAULT_QUOTA_TOKENS)


class SecretLeakTest(LlmChannelTestBase):
    def test_api_key_never_appears_in_any_response(self) -> None:
        secret = "sk-fake-test-key-0001"
        upstream = self.make_upstream()
        channel = self.create_channel(upstream, api_key=secret)
        # key_hint 只给末 4 位
        self.assertEqual(channel["key_hint"], "0001")
        self.assertNotIn("api_key", channel)

        listed = self.client.get("/api/admin/llm-channels", headers=self.admin_headers)
        detail = self.client.get(f"/api/admin/llm-channels/{channel['id']}", headers=self.admin_headers)
        patched = self.client.patch(
            f"/api/admin/llm-channels/{channel['id']}", json={"priority": 5}, headers=self.admin_headers
        )
        for response in (listed, detail, patched):
            self.assertNotIn(secret, response.text, f"{response.request.url} 泄漏了明文 key")
            self.assertNotIn("api_key", response.text)

    def test_api_key_sent_upstream_but_not_returned(self) -> None:
        secret = "sk-fake-test-key-0001"
        upstream = self.make_upstream()
        self.create_channel(upstream, api_key=secret, models=["fake-model-a"])
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        # 上游确实收到了 Bearer（说明密钥被用上了），但响应体里没有它
        self.assertEqual(upstream.requests[0]["auth"], f"Bearer {secret}")
        self.assertNotIn(secret, response.text)

    def test_update_without_api_key_keeps_it(self) -> None:
        secret = "sk-fake-test-key-0001"
        upstream = self.make_upstream()
        channel = self.create_channel(upstream, api_key=secret)
        # 不传 api_key = 不变
        self.client.patch(
            f"/api/admin/llm-channels/{channel['id']}", json={"priority": 7}, headers=self.admin_headers
        )
        row = self.store.db.execute(
            "SELECT api_key FROM llm_channels WHERE id = ?", (channel["id"],)
        ).fetchone()
        self.assertEqual(row["api_key"], secret)
        # 传空串 = 清空
        self.client.patch(
            f"/api/admin/llm-channels/{channel['id']}", json={"api_key": ""}, headers=self.admin_headers
        )
        row = self.store.db.execute(
            "SELECT api_key FROM llm_channels WHERE id = ?", (channel["id"],)
        ).fetchone()
        self.assertEqual(row["api_key"], "")


class RoutingTest(LlmChannelTestBase):
    def test_route_hit_is_case_insensitive(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, models=["Fake-Model-A"])
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(upstream.requests[0]["body"]["model"], "fake-model-a")

    def test_no_route_returns_404_and_no_upstream_call(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, models=["fake-model-a"])
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "not-configured", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("llm_channel_no_route", response.json()["detail"])
        self.assertEqual(upstream.requests, [], "未命中路由不该打上游")

    def test_disabled_channel_not_routed(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, models=["fake-model-a"], enabled=False)
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(upstream.requests, [])

    def test_priority_conflict_prefers_lower_priority(self) -> None:
        low = self.make_upstream()
        high = self.make_upstream()
        # priority 小者优先：先建 priority=100 的，再建 priority=10 的
        self.create_channel(high, models=["shared-model"], priority=100, name="次级渠道")
        self.create_channel(low, models=["shared-model"], priority=10, name="优先渠道")
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "shared-model", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(low.requests), 1, "priority 小的渠道应被选中")
        self.assertEqual(len(high.requests), 0)

    def test_models_endpoint_lists_enabled_models_only(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, models=["fake-model-a", "fake-model-b"])
        self.create_channel(upstream, models=["hidden-model"], enabled=False, name="停用渠道")
        response = self.client.get("/api/llm/v1/models", headers=self.plain_headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(sorted(response.json()["models"]), ["fake-model-a", "fake-model-b"])


class QuotaTest(LlmChannelTestBase):
    def test_quota_lazy_created_at_default(self) -> None:
        response = self.client.get("/api/llm/quota", headers=self.plain_headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["token_limit"], llm_channels.DEFAULT_QUOTA_TOKENS)
        self.assertEqual(body["tokens_used"], 0)
        self.assertFalse(body["unlimited"])

    def test_usage_is_deducted_from_upstream_usage(self) -> None:
        upstream = self.make_upstream(
            [
                (
                    200,
                    {
                        "choices": [{"message": {"role": "assistant", "content": "pong"}}],
                        "usage": {"prompt_tokens": 30, "completion_tokens": 12, "total_tokens": 42},
                    },
                )
            ]
        )
        self.create_channel(upstream, models=["fake-model-a"])
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        quota = self.client.get("/api/llm/quota", headers=self.plain_headers).json()
        self.assertEqual(quota["tokens_used"], 42)
        self.assertEqual(quota["tokens_remaining"], llm_channels.DEFAULT_QUOTA_TOKENS - 42)
        row = self.store.db.execute(
            "SELECT prompt_tokens, completion_tokens, total_tokens, status, model FROM llm_usage_log"
        ).fetchone()
        self.assertEqual((row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]), (30, 12, 42))
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["model"], "fake-model-a")

    def test_exhausted_quota_returns_429_with_reason(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, models=["fake-model-a"])
        set_resp = self.client.put(
            f"/api/admin/llm-quotas/{PLAIN}", json={"token_limit": 10}, headers=self.admin_headers
        )
        self.assertEqual(set_resp.status_code, 200, set_resp.text)
        # 把已用量顶到上限
        with llm_channels._transaction(self.store):
            self.store.db.execute(
                "UPDATE llm_member_quotas SET tokens_used = 10 WHERE member_id = ?", (PLAIN,)
            )
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("llm_quota_exceeded", response.json()["detail"])
        self.assertEqual(upstream.requests, [], "额度耗尽应在打上游之前就拒绝")

    def test_negative_limit_means_unlimited(self) -> None:
        upstream = self.make_upstream(
            [(200, {"choices": [{"message": {"content": "pong"}}], "usage": {"prompt_tokens": 5000, "completion_tokens": 5000}})]
        )
        self.create_channel(upstream, models=["fake-model-a"])
        set_resp = self.client.put(
            f"/api/admin/llm-quotas/{PLAIN}", json={"token_limit": -1}, headers=self.admin_headers
        )
        self.assertEqual(set_resp.status_code, 200, set_resp.text)
        self.assertTrue(set_resp.json()["unlimited"])
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        quota = self.client.get("/api/llm/quota", headers=self.plain_headers).json()
        self.assertTrue(quota["unlimited"])
        self.assertIsNone(quota["tokens_remaining"])


class UpstreamFailureTest(LlmChannelTestBase):
    def test_upstream_4xx_recorded_as_error(self) -> None:
        upstream = self.make_upstream([(401, {"error": {"message": "invalid api key"}})])
        self.create_channel(upstream, models=["fake-model-a"])
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 502)
        self.assertIn("llm_upstream_error", response.json()["detail"])
        row = self.store.db.execute(
            "SELECT status, error_code, total_tokens FROM llm_usage_log"
        ).fetchone()
        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error_code"], "llm_upstream_error")
        self.assertEqual(row["total_tokens"], 0)
        # 失败不该扣额度
        quota = self.client.get("/api/llm/quota", headers=self.plain_headers).json()
        self.assertEqual(quota["tokens_used"], 0)

    def test_unreachable_upstream_recorded_as_error(self) -> None:
        port = _free_port()  # 没有服务在听
        response = self.client.post(
            "/api/admin/llm-channels",
            json={
                "name": f"不可达-{uuid4().hex[:6]}",
                "base_url": f"http://127.0.0.1:{port}/v1",
                "api_key": "sk-fake",
                "models": ["fake-model-a"],
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        chat = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}]},
            headers=self.plain_headers,
        )
        self.assertEqual(chat.status_code, 502)
        self.assertIn("llm_upstream_unreachable", chat.json()["detail"])
        row = self.store.db.execute("SELECT status, error_code FROM llm_usage_log").fetchone()
        self.assertEqual((row["status"], row["error_code"]), ("error", "llm_upstream_unreachable"))

    def test_streaming_proxies_and_records_usage(self) -> None:
        # SSE 假上游：逐行 data: 帧，最后一帧带 usage
        frames = [
            'data: {"choices":[{"delta":{"content":"po"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"ng"}}]}\n\n',
            'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n',
            "data: [DONE]\n\n",
        ]
        upstream = self._sse_upstream(frames)
        self.create_channel(upstream, models=["fake-model-a"])
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("po", response.text)
        self.assertIn("ng", response.text)
        quota = self.client.get("/api/llm/quota", headers=self.plain_headers).json()
        self.assertEqual(quota["tokens_used"], 10, "流式结束应按最后一个 usage 扣减")

    def test_streaming_requires_model_and_messages(self) -> None:
        response = self.client.post(
            "/api/llm/v1/chat/completions",
            json={"model": "fake-model-a"},
            headers=self.plain_headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("llm_messages_required", response.json()["detail"])

    def _sse_upstream(self, frames: list[str]) -> FakeUpstream:
        upstream = FakeUpstream()
        upstream.close()
        port = upstream.port
        captured = upstream.requests

        class SseHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                captured.append({"path": self.path, "body": json.loads(raw) if raw else {}})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                payload = "".join(frames).encode("utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        server = HTTPServer(("127.0.0.1", port), SseHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        upstream.server = server  # 复用 close() 关掉这个替换过的 server
        return upstream


class CheckAndSpeedTest(LlmChannelTestBase):
    def test_check_success_backfills_result(self) -> None:
        upstream = self.make_upstream([(200, {"choices": [{"message": {"content": "pong"}}]})])
        channel = self.create_channel(upstream, models=["fake-model-a"])
        response = self.client.post(
            f"/api/admin/llm-channels/{channel['id']}/check", headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(upstream.requests[0]["body"]["max_tokens"], 1)
        self.assertEqual(upstream.requests[0]["body"]["temperature"], 0)
        stored = self.client.get(
            f"/api/admin/llm-channels/{channel['id']}", headers=self.admin_headers
        ).json()
        self.assertTrue(stored["last_check_ok"])
        self.assertIsNotNone(stored["last_check_at"])
        self.assertIsNotNone(stored["last_latency_ms"])

    def test_check_failure_backfills_failure_truthfully(self) -> None:
        upstream = self.make_upstream([(500, {"error": {"message": "boom"}})])
        channel = self.create_channel(upstream, models=["fake-model-a"])
        response = self.client.post(
            f"/api/admin/llm-channels/{channel['id']}/check", headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["ok"])
        stored = self.client.get(
            f"/api/admin/llm-channels/{channel['id']}", headers=self.admin_headers
        ).json()
        self.assertFalse(stored["last_check_ok"])
        self.assertIn("llm_upstream_error", stored["last_check_detail"])

    def test_speed_test_reports_each_round(self) -> None:
        upstream = self.make_upstream()
        channel = self.create_channel(upstream, models=["fake-model-a"])
        response = self.client.post(
            f"/api/admin/llm-channels/{channel['id']}/speed-test",
            json={"rounds": 3},
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["ok_rounds"], 3)
        self.assertEqual(len(body["rounds"]), 3)
        self.assertEqual([item["round"] for item in body["rounds"]], [1, 2, 3])
        self.assertIsNotNone(body["avg_ms"])
        self.assertEqual(len(upstream.requests), 3)

    def test_speed_test_rounds_capped_at_ten(self) -> None:
        upstream = self.make_upstream()
        channel = self.create_channel(upstream, models=["fake-model-a"])
        response = self.client.post(
            f"/api/admin/llm-channels/{channel['id']}/speed-test",
            json={"rounds": 50},
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["rounds"]), 10)
        self.assertEqual(len(upstream.requests), 10)

    def test_speed_test_partial_failure_counted_honestly(self) -> None:
        upstream = self.make_upstream(
            [
                (200, {"choices": [{"message": {"content": "pong"}}]}),
                (503, {"error": {"message": "busy"}}),
                (200, {"choices": [{"message": {"content": "pong"}}]}),
            ]
        )
        channel = self.create_channel(upstream, models=["fake-model-a"])
        response = self.client.post(
            f"/api/admin/llm-channels/{channel['id']}/speed-test",
            json={"rounds": 3},
            headers=self.admin_headers,
        )
        body = response.json()
        self.assertEqual(body["ok_rounds"], 2)
        self.assertFalse(body["rounds"][1]["ok"])
        self.assertNotIn("avg_ms", [k for k, v in body.items() if v is None])


class UsageOverviewTest(LlmChannelTestBase):
    def test_usage_overview_aggregates_by_member(self) -> None:
        upstream = self.make_upstream(
            [
                (200, {"choices": [{"message": {"content": "a"}}], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}),
                (200, {"choices": [{"message": {"content": "b"}}], "usage": {"prompt_tokens": 20, "completion_tokens": 8}}),
            ]
        )
        self.create_channel(upstream, models=["fake-model-a"])
        for _ in range(2):
            self.client.post(
                "/api/llm/v1/chat/completions",
                json={"model": "fake-model-a", "messages": [{"role": "user", "content": "hi"}]},
                headers=self.plain_headers,
            )
        overview = self.client.get("/api/admin/llm-usage", headers=self.admin_headers)
        self.assertEqual(overview.status_code, 200, overview.text)
        body = overview.json()
        self.assertEqual(body["totals"]["requests"], 2)
        self.assertEqual(body["totals"]["ok_requests"], 2)
        self.assertEqual(body["totals"]["total_tokens"], 43)
        entry = next(item for item in body["members"] if item["member_id"] == PLAIN)
        self.assertEqual(entry["requests"], 2)
        self.assertEqual(entry["tokens_used"], 43)

    def test_admin_can_read_and_set_member_quota(self) -> None:
        got = self.client.get(f"/api/admin/llm-quotas/{PLAIN}", headers=self.admin_headers)
        self.assertEqual(got.status_code, 200, got.text)
        self.assertEqual(got.json()["token_limit"], llm_channels.DEFAULT_QUOTA_TOKENS)
        put = self.client.put(
            f"/api/admin/llm-quotas/{PLAIN}", json={"token_limit": 1234}, headers=self.admin_headers
        )
        self.assertEqual(put.status_code, 200, put.text)
        self.assertEqual(put.json()["token_limit"], 1234)


class ValidationTest(LlmChannelTestBase):
    def test_duplicate_name_rejected(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, name="唯一渠道名")
        response = self.client.post(
            "/api/admin/llm-channels",
            json={"name": "唯一渠道名", "base_url": upstream.base_url, "models": ["m"]},
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 409)
        self.assertIn("llm_name_taken", response.json()["detail"])

    def test_invalid_base_url_rejected(self) -> None:
        response = self.client.post(
            "/api/admin/llm-channels",
            json={"name": f"坏地址-{uuid4().hex[:6]}", "base_url": "not-a-url", "models": ["m"]},
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("llm_base_url_invalid", response.json()["detail"])

    def test_models_required(self) -> None:
        upstream = self.make_upstream()
        response = self.client.post(
            "/api/admin/llm-channels",
            json={"name": f"无模型-{uuid4().hex[:6]}", "base_url": upstream.base_url, "models": []},
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("llm_models_required", response.json()["detail"])

    def test_deleted_channel_disappears(self) -> None:
        upstream = self.make_upstream()
        channel = self.create_channel(upstream)
        deleted = self.client.delete(
            f"/api/admin/llm-channels/{channel['id']}", headers=self.admin_headers
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(
            self.client.get(
                f"/api/admin/llm-channels/{channel['id']}", headers=self.admin_headers
            ).status_code,
            404,
        )


if __name__ == "__main__":
    unittest.main()
