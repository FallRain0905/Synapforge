"""渠道、连接检测、本机路由扫描与导入。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import providers as studio_providers
from synapforge.studio import store as studio_store


class _FakeGateway(BaseHTTPRequestHandler):
    models = ["glm-5.3", "Qwen3.8-Max"]
    require_key = ""
    calls: list[tuple[str, str, str]] = []

    def log_message(self, fmt, *args):  # noqa: A003
        return

    def _auth_ok(self) -> bool:
        if not self.require_key:
            return True
        header = self.headers.get("Authorization") or ""
        return header == f"Bearer {self.require_key}"

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        type(self).calls.append(("GET", self.path, self.headers.get("Authorization") or ""))
        if self.path.rstrip("/") in ("/v1/models", "/models"):
            if not self._auth_ok():
                self._json(401, {"error": "bad key"})
                return
            self._json(200, {"data": [{"id": m} for m in self.models]})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        type(self).calls.append(("POST", self.path, self.headers.get("Authorization") or ""))
        if not self._auth_ok():
            self._json(401, {"error": "bad key"})
            return
        if self.path.rstrip("/") == "/v1/chat/completions":
            self._json(200, {"choices": [{"message": {"content": "pong"}}]})
            return
        self._json(404, {"error": "not found", "body": raw.decode("utf-8", "replace")})


class ProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-provider-")
        self.addCleanup(self.tmp.cleanup)
        self.store = open_store(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()

    def _gateway(self, *, require_key: str = "") -> tuple[ThreadingHTTPServer, int]:
        handler = type("_Handler", (_FakeGateway,), {"require_key": require_key,
                                                     "calls": []})
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        return httpd, httpd.server_address[1]

    def test_create_masks_key_and_catalog(self) -> None:
        provider = studio_providers.create_provider(
            self.store, name="本地网关", kind="openai",
            base_url="http://127.0.0.1:3425", api_key="sk-1234567890abcdef",
            models=["auto/glm-5.3", "auto/glm-5.3"], is_default=True)
        self.assertEqual(provider["models"], ["auto/glm-5.3"])
        self.assertTrue(provider["has_key"])
        self.assertEqual(provider["key_masked"], "sk-…cdef")
        self.assertNotIn("api_key", provider)

        catalog = studio_providers.catalog(self.store)
        ids = [m["id"] for m in catalog["models"]]
        self.assertIn("auto/glm-5.3", ids)
        self.assertEqual(catalog["default"], "auto/glm-5.3")
        channel = next(c for c in catalog["channels"] if c["id"] == provider["id"])
        self.assertTrue(channel["has_key"])

    def test_update_key_keep_and_clear(self) -> None:
        provider = studio_providers.create_provider(
            self.store, name="手动渠道", base_url="https://api.example.com",
            api_key="secret-key-123456")
        kept = studio_providers.update_provider(
            self.store, provider["id"], name="改名", api_key="")
        self.assertEqual(kept["name"], "改名")
        self.assertTrue(kept["has_key"])
        cleared = studio_providers.update_provider(
            self.store, provider["id"], clear_key=True)
        self.assertFalse(cleared["has_key"])

    def test_probe_and_deep_check(self) -> None:
        httpd, port = self._gateway(require_key="real-key")
        result = studio_providers.probe_endpoint(
            f"http://127.0.0.1:{port}", "openai", api_key="real-key")
        self.assertTrue(result["ok"])
        self.assertEqual(result["models"], ["glm-5.3", "Qwen3.8-Max"])
        self.assertEqual(result["base_url"], f"http://127.0.0.1:{port}")

        bad = studio_providers.probe_endpoint(
            f"http://127.0.0.1:{port}", "openai", api_key="wrong")
        self.assertFalse(bad["ok"])
        self.assertTrue(bad["auth_error"])

        deep = studio_providers.deep_check(
            f"http://127.0.0.1:{port}", "openai", api_key="real-key", model="glm-5.3")
        self.assertTrue(deep["ok"])
        self.assertEqual(deep["reply"], "pong")

    def test_test_provider_updates_last_check(self) -> None:
        httpd, port = self._gateway()
        provider = studio_providers.create_provider(
            self.store, name="探测渠道", kind="openai",
            base_url=f"http://127.0.0.1:{port}", models=["glm-5.3"])
        result = studio_providers.test_provider(self.store, provider["id"])
        self.assertTrue(result["ok"])
        saved = studio_store.get_provider(self.store, provider["id"])
        self.assertTrue(saved["last_check"]["ok"])
        self.assertEqual(saved["last_check"]["models"], ["glm-5.3", "Qwen3.8-Max"])

    def test_scan_finds_fake_port(self) -> None:
        httpd, port = self._gateway()
        routes = studio_providers.scan_local_routes(
            home=self.home, ports=((port, "测试网关", "openai", ""),), timeout=1.5)
        self.assertEqual(len(routes), 1)
        self.assertTrue(routes[0]["reachable"])
        self.assertEqual(routes[0]["models"], ["glm-5.3", "Qwen3.8-Max"])
        self.assertEqual(routes[0]["provider_id"], "")

    def test_import_route_reads_key_from_config(self) -> None:
        httpd, port = self._gateway(require_key="config-key")
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
            "ANTHROPIC_AUTH_TOKEN": "config-key",
            "ANTHROPIC_MODEL": "glm-5.3",
        }}), encoding="utf-8")
        routes = studio_providers.scan_local_routes(
            home=self.home, ports=(), timeout=1.5)
        self.assertTrue(routes[0]["reachable"])
        self.assertTrue(routes[0]["is_current"])
        self.assertNotIn("api_key", routes[0])

        provider = studio_providers.import_route(
            self.store, url=f"http://127.0.0.1:{port}", kind="anthropic",
            home=self.home, ports=())
        self.assertEqual(provider["name"], "Claude Code 当前路由")
        self.assertTrue(provider["has_key"])
        saved = studio_store.get_provider(self.store, provider["id"])
        self.assertEqual(saved["api_key"], "config-key")
        self.assertEqual(saved["models"], ["glm-5.3", "Qwen3.8-Max"])

    def test_import_claude_settings_is_idempotent(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"env": {
            "ANTHROPIC_BASE_URL": "https://api.example.com",
            "ANTHROPIC_AUTH_TOKEN": "token-123456789",
            "ANTHROPIC_MODEL": "glm-5.3",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1M]",
        }}), encoding="utf-8")
        first = studio_providers.import_claude_settings(self.store, home=self.home)
        second = studio_providers.import_claude_settings(self.store, home=self.home)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["models"], ["glm-5.3"])
        self.assertTrue(first["is_default"])
        self.assertEqual(len(studio_store.list_providers(self.store)), 1)

    def test_agent_rejects_openai_channel_for_claude_backend(self) -> None:
        provider = studio_providers.create_provider(
            self.store, name="OpenAI 渠道", kind="openai",
            base_url="https://api.example.com", models=["gpt-x"])
        with self.assertRaises(studio_agents.StudioError) as ctx:
            studio_agents.create_agent(
                self.store, name="Writer", provider_id=provider["id"],
                backend="claude", workspace=str(self.home / "ws-a"))
        self.assertEqual(ctx.exception.code, "provider_incompatible")

        agent = studio_agents.create_agent(
            self.store, name="Writer", provider_id=provider["id"],
            backend="command", command="my-agent --prompt {prompt}",
            workspace=str(self.home / "ws-b"))
        self.assertEqual(agent["backend"], "command")
        self.assertEqual(agent["provider_id"], provider["id"])

    def test_delete_channel_detaches_agents(self) -> None:
        provider = studio_providers.create_provider(
            self.store, name="临时渠道", base_url="https://api.example.com")
        agent = studio_agents.create_agent(
            self.store, name="Writer", provider_id=provider["id"],
            workspace=str(self.home / "ws-c"))
        result = studio_providers.delete_provider(self.store, provider["id"])
        self.assertEqual(result["detached_agents"], ["Writer"])
        refreshed = studio_store.get_agent(self.store, agent["id"])
        self.assertEqual(refreshed["provider_id"], "")


if __name__ == "__main__":
    unittest.main()
