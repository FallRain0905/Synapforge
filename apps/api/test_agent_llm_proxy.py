"""执行体侧 LLM 代理（方案 A：`/api/agent/llm/v1/...`）契约测试。

覆盖口径：

- 项目能力令牌（Bearer）必须带 **llm.invoke** 能力且属于路径上的 agent；
- 额度与用量记到 **agent 的 owner_member_id** 名下（不是令牌持有者）；
- 渠道路由/扣减/流式与成员代理同一套口径（no_route 404、上游故障 502）；
- 负向：坏令牌 401、无 llm.invoke 能力 403、agent 不匹配 403。

一律用本机假上游（http.server），不出现真实 key。
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import knowledge_base, llm_channels, main
from app.contracts import AgentRegister, DevicePairingCreate, DeviceProjectGrantCreate, SessionCreate
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request
from test_llm_channels import FakeUpstream

ADMIN = "member-001"
DEVICE = "device-llm-proxy"
AGENT = "agent-llm-proxy"


class AgentLlmProxyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        llm_channels.ensure_schema(self.store)
        knowledge_base.ensure_kb_tables(self.store)
        # member-001 提为管理员：渠道经 API 建出来（走真实建渠路径）
        self.store.db.execute("UPDATE human_members SET is_admin = 1 WHERE id = ?", (ADMIN,))
        self.store.db.commit()
        # agent 属于 ADMIN；设备由 ADMIN 配对（owner 一致），并绑定到该 agent
        self.agent = self.store.register_agent(
            AgentRegister(agent_id=AGENT, display_name="LLM Proxy Agent", owner_member_id=ADMIN)
        )
        pairing = self.store.create_device_pairing(
            DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), ADMIN
        )
        credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                AGENT,
                DEVICE,
                device_name="LLM proxy box",
                capabilities=["chat.run", "llm.invoke"],
            )
        )
        self.device = credential.device
        # 默认能力表已含 llm.invoke（contracts 的 DeviceProjectGrantCreate）
        self.grant_credential = self.store.create_device_project_grant(
            self.project_id(), DeviceProjectGrantCreate(device_id=DEVICE), ADMIN
        )
        self.admin_headers = {
            "Authorization": f"Bearer {self.store.create_session(SessionCreate(member_id=ADMIN, expires_in_seconds=900)).token}"
        }
        self.upstreams: list[FakeUpstream] = []

    def tearDown(self) -> None:
        for upstream in self.upstreams:
            upstream.close()
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # -- 工具 ---------------------------------------------------------------
    def project_id(self) -> UUID:
        return self.store.list_projects()[0].id

    def auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def make_upstream(self, script=None) -> FakeUpstream:
        upstream = FakeUpstream(script)
        self.upstreams.append(upstream)
        return upstream

    def create_channel(self, upstream: FakeUpstream, models: list[str]) -> dict:
        response = self.client.post(
            "/api/admin/llm-channels",
            json={
                "name": f"执行体渠道-{len(self.upstreams)}",
                "base_url": upstream.base_url,
                "api_key": "sk-fake-agent-0001",
                "models": models,
                "priority": 10,
                "enabled": True,
            },
            headers=self.admin_headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def proxy_chat(self, token: str, payload: dict, agent_id: str = AGENT) -> object:
        return self.client.post(
            f"/api/agent/llm/v1/{agent_id}/{self.project_id()}/chat/completions",
            json=payload,
            headers=self.auth(token),
        )

    def owner_quota(self) -> dict:
        org = self.store.db.execute(
            "SELECT organization_id FROM human_members WHERE id = ?", (ADMIN,)
        ).fetchone()["organization_id"]
        return llm_channels.get_quota(self.store, ADMIN, organization_id=str(org))


class ProxyAuthTest(AgentLlmProxyTest):
    def test_chat_via_proxy_deducts_owner_quota(self) -> None:
        upstream = self.make_upstream(
            [(200, {"choices": [{"message": {"content": "pong"}}], "usage": {"prompt_tokens": 20, "completion_tokens": 6}})]
        )
        self.create_channel(upstream, ["chan-model"])
        response = self.proxy_chat(
            self.grant_credential.project_token,
            {"model": "chan-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "pong")
        # 额度记到 agent 的 owner（ADMIN）名下
        self.assertEqual(self.owner_quota()["tokens_used"], 26)
        row = self.store.db.execute("SELECT member_id, status FROM llm_usage_log").fetchone()
        self.assertEqual((row["member_id"], row["status"]), (ADMIN, "ok"))

    def test_models_endpoint_lists_channel_models(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model", "spare-model"])
        response = self.client.get(
            f"/api/agent/llm/v1/{AGENT}/{self.project_id()}/models",
            headers=self.auth(self.grant_credential.project_token),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"], ["chan-model", "spare-model"])

    def test_bad_token_rejected(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model"])
        response = self.proxy_chat("prj_not-a-real-token", {"model": "chan-model", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.status_code, 401)

    def test_missing_capability_rejected(self) -> None:
        # 只授 chat.run 的旧式授权：没有 llm.invoke 就调不了渠道
        restricted = self.store.create_device_project_grant(
            self.project_id(),
            DeviceProjectGrantCreate(device_id=DEVICE, capabilities=["chat.run"]),
            ADMIN,
        )
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model"])
        response = self.proxy_chat(
            restricted.project_token,
            {"model": "chan-model", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("device_capability_denied", response.json()["detail"])
        self.assertEqual(upstream.requests, [])

    def test_agent_mismatch_rejected(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model"])
        response = self.proxy_chat(
            self.grant_credential.project_token,
            {"model": "chan-model", "messages": [{"role": "user", "content": "hi"}]},
            agent_id="agent-someone-else",
        )
        self.assertEqual(response.status_code, 403)
        self.assertIn("agent_project_agent_mismatch", response.json()["detail"])

    def test_no_route_returns_404(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model"])
        response = self.proxy_chat(
            self.grant_credential.project_token,
            {"model": "not-configured", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(response.status_code, 404)
        self.assertIn("llm_channel_no_route", response.json()["detail"])
        self.assertEqual(upstream.requests, [])


class ProxyStreamTest(AgentLlmProxyTest):
    def test_stream_passthrough_and_deduction(self) -> None:
        from test_llm_channels import _free_port

        frames = [
            'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n',
            'data: {"choices":[{"delta":{}}],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n\n',
            "data: [DONE]\n\n",
        ]

        class SseUpstream(FakeUpstream):
            def __init__(self) -> None:
                self.requests: list[dict] = []
                self.port = _free_port()
                upstream = self

                class Handler(BaseHTTPRequestHandler):
                    protocol_version = "HTTP/1.1"

                    def do_POST(self) -> None:  # noqa: N802
                        length = int(self.headers.get("Content-Length") or 0)
                        raw = self.rfile.read(length).decode("utf-8") if length else ""
                        upstream.requests.append({"body": json.loads(raw) if raw else {}})
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

        upstream = SseUpstream()
        self.upstreams.append(upstream)
        self.create_channel(upstream, ["chan-model"])
        response = self.proxy_chat(
            self.grant_credential.project_token,
            {"model": "chan-model", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("he", response.text)
        self.assertIn("[DONE]", response.text)
        self.assertEqual(self.owner_quota()["tokens_used"], 6)


if __name__ == "__main__":
    unittest.main()


class MyAgentPlatformModelsTest(AgentLlmProxyTest):
    """`GET /api/my-agent/agents` 的平台渠道字段（方案 A，2026-10-02）。

    口径：platform_models 只在"授权含 llm.invoke 且组织里至少一条启用渠道"时可用；
    渠道模型**不混进**本地 models；不可用要如实给 False（页面显示"配置不可用"，不静默回落）。
    """

    def my_agents(self) -> dict:
        """让设备出现在执行体列表里：`list_my_agents` 不要求心跳，只要求 owner + 活动授权。"""
        response = self.client.get("/api/my-agent/agents", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200, response.text)
        return {row["device_id"]: row for row in response.json()}

    def test_platform_models_listed_and_available(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model-a", "chan-model-b"])
        rows = self._agents()
        self.assertIn(DEVICE, rows)
        endpoint = rows[DEVICE]
        self.assertEqual(endpoint["platform_models"], [{"model": "chan-model-a"}, {"model": "chan-model-b"}])
        self.assertTrue(endpoint["platform_models_available"])
        self.assertEqual(endpoint["platform_provider"], "synapforge")
        # 这台设备没有心跳自报本地模型：本地 models 就是空——渠道模型不混进来（关键断言）
        self.assertEqual(endpoint["models"], [])

    def test_platform_models_unavailable_without_channel(self) -> None:
        upstream = self.make_upstream()  # 有上游但**没建渠道**
        rows = self._agents()
        endpoint = rows[DEVICE]
        self.assertEqual(endpoint["platform_models"], [])
        self.assertFalse(endpoint["platform_models_available"])
        self.assertEqual(endpoint["platform_provider"], "")

    def test_platform_models_unavailable_without_llm_invoke(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model-a"])
        # 撤掉带 llm.invoke 的授权，换成只授 chat.run 的旧式授权
        grants = self.store.db.execute(
            "SELECT id FROM device_project_grants WHERE device_id = ?", (DEVICE,)
        ).fetchall()
        for row in grants:
            self.store.revoke_device_project_grant(UUID(row["id"]), ADMIN)
        self.store.create_device_project_grant(
            self.project_id(), DeviceProjectGrantCreate(device_id=DEVICE, capabilities=["chat.run"]), ADMIN
        )
        rows = self._agents()
        endpoint = rows[DEVICE]
        self.assertEqual(endpoint["platform_models"], [{"model": "chan-model-a"}])  # 渠道在，能"看"
        self.assertFalse(endpoint["platform_models_available"])  # 但授权没有 llm.invoke ⇒ 不可配置
        self.assertEqual(endpoint["platform_provider"], "")

    def _agents(self) -> dict:
        response = self.client.get("/api/my-agent/agents", headers=self.admin_headers)
        self.assertEqual(response.status_code, 200, response.text)
        return {row["device_id"]: row for row in response.json()}


class ExecutorSetupTest(AgentLlmProxyTest):
    """`GET /api/agents/{agent_id}/llm-setup/{project_id}`（执行体内核专用，2026-10-02）。

    浏览器拿不到令牌；执行体用它已持有的授权令牌换取 provider 配置——
    apiKey 是**同一枚令牌原样回传**，平台不落库。
    """

    def setup_request(self, token: str, agent_id: str = AGENT) -> object:
        return self.client.get(
            f"/api/agents/{agent_id}/llm-setup/{self.project_id()}",
            headers=self.auth(token),
        )

    def test_setup_returns_provider_config_with_token_echo(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model-a", "chan-model-b"])
        token = self.grant_credential.project_token
        response = self.setup_request(token)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["provider"], "synapforge")
        self.assertTrue(body["base_url"].endswith(f"/api/agent/llm/v1/{AGENT}/{self.project_id()}"))
        # 令牌**原样回传**（执行体写进本机 0600 配置；浏览器/平台库都不持有）
        self.assertEqual(body["provider_api_key"], token)
        self.assertEqual(body["models"], ["chan-model-a", "chan-model-b"])
        self.assertTrue(body["issued_at"])

    def test_setup_without_channels_gives_empty_models(self) -> None:
        upstream = self.make_upstream()  # 不建渠道
        response = self.setup_request(self.grant_credential.project_token)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["models"], [])

    def test_setup_requires_valid_token(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model-a"])
        self.assertEqual(self.setup_request("prj_not-a-real-token").status_code, 401)

    def test_setup_requires_llm_invoke_capability(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model-a"])
        restricted = self.store.create_device_project_grant(
            self.project_id(), DeviceProjectGrantCreate(device_id=DEVICE, capabilities=["chat.run"]), ADMIN
        )
        response = self.setup_request(restricted.project_token)
        self.assertEqual(response.status_code, 403)
        self.assertIn("device_capability_denied", response.json()["detail"])

    def test_setup_rejects_agent_mismatch(self) -> None:
        upstream = self.make_upstream()
        self.create_channel(upstream, ["chan-model-a"])
        response = self.setup_request(self.grant_credential.project_token, agent_id="agent-someone-else")
        self.assertEqual(response.status_code, 403)
