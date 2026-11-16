"""Studio HTTP 控制面：token、建集群、状态、Origin 防护、UI 脚本语法。"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from synapforge.studio.server import STATIC_DIR, build_server


class StudioServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-http-")
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "state.db"
        self.httpd, self.manager, self.token, self.port = build_server(
            port=0, state_path=self.db, token="test-token",
            skills_home=Path(self.tmp.name) / "home")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.base = f"http://127.0.0.1:{self.port}"

    def _stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.manager.shutdown()
        getattr(self.httpd, "agent_manager").shutdown()

    def _call(self, path: str, *, method: str = "GET", body: dict | None = None,
              token: str | None = None, headers: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request_headers = {"Content-Type": "application/json", "Connection": "close"}
        if token is not None:
            request_headers["X-Studio-Token"] = token
        request_headers.update(headers or {})
        request = urllib.request.Request(self.base + path, data=data, method=method,
                                         headers=request_headers)

        def attempt() -> tuple[int, dict]:
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as error:
                return error.code, json.loads(error.read().decode("utf-8"))

        try:
            return attempt()
        except (ConnectionAbortedError, ConnectionResetError, urllib.error.URLError):
            # Windows 上偶发 10053（服务端线程收尾与客户端读竞争）；测试侧重语义，重试一次。
            time.sleep(0.15)
            return attempt()

    def _raw_get(self, path: str) -> tuple[int, bytes]:
        last: Exception | None = None
        for _ in range(3):
            request = urllib.request.Request(self.base + path,
                                             headers={"Connection": "close"})
            try:
                with urllib.request.urlopen(request, timeout=10) as response:
                    return response.status, response.read()
            except (ConnectionAbortedError, ConnectionResetError,
                    urllib.error.URLError) as error:
                last = error
                time.sleep(0.15)
        raise last  # type: ignore[misc]

    def test_index_served_and_token_required(self) -> None:
        status, body = self._raw_get("/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("synapforge studio", html)
        status, payload = self._call("/api/studio/state")
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "unauthorized")
        status, payload = self._call("/api/studio/state", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertIn("clusters", payload)

    def test_create_cluster_and_read_state(self) -> None:
        status, payload = self._call(
            "/api/studio/clusters", method="POST", token=self.token, body={
                "name": "HTTP 中文集群", "objective": "验证控制面",
                "lead": {"name": "Lead", "backend": "claude", "system_prompt": "lead"},
                "roles": [{"name": "Worker", "backend": "claude", "system_prompt": "worker"}],
                "policy": {"max_rounds": 8},
            })
        self.assertEqual(status, 201)
        cluster = payload["cluster"]
        self.assertEqual(cluster["name"], "HTTP 中文集群")
        self.assertEqual(len(cluster["roles"]), 2)
        self.assertEqual(cluster["status"], "READY")

        status, payload = self._call(
            f"/api/studio/state?cluster_id={cluster['id']}", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["cluster"]["name"], "HTTP 中文集群")
        self.assertEqual(payload["cluster"]["stats"]["roles"], 2)

        status, payload = self._call("/api/studio/instructions", method="POST",
                                     token=self.token,
                                     body={"cluster_id": cluster["id"], "text": "先写提纲"})
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["broadcast"]["tasks"]), 2)

    def test_origin_rejected(self) -> None:
        status, payload = self._call(
            "/api/studio/messages", method="POST", token=self.token,
            body={"cluster_id": "missing", "text": "x"},
            headers={"Origin": "https://evil.example"})
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "origin_rejected")

    def test_agent_registry_endpoints(self) -> None:
        status, payload = self._call("/api/studio/agents", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["agents"], [])

        status, payload = self._call(
            "/api/studio/agents", method="POST", token=self.token, body={
                "name": "Writer", "model": "glm-5.3", "system_prompt": "你是写手",
                "workspace": str(Path(self.tmp.name) / "agent-ws"),
            })
        self.assertEqual(status, 201)
        agent = payload["agent"]
        self.assertEqual(agent["name"], "Writer")

        status, payload = self._call(f"/api/studio/agents/{agent['id']}", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["agent"]["name"], "Writer")
        self.assertEqual(payload["agent"]["steps"], [])

        status, payload = self._call("/api/studio/models", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["models"])

    def test_provider_endpoints_and_agent_channel(self) -> None:
        status, payload = self._call(
            "/api/studio/providers", method="POST", token=self.token, body={
                "name": "测试渠道", "kind": "anthropic",
                "base_url": "http://127.0.0.1:9", "api_key": "sk-test-1234567890",
                "models": ["glm-5.3"], "is_default": True,
            })
        self.assertEqual(status, 201)
        provider = payload["provider"]
        self.assertTrue(provider["has_key"])
        self.assertEqual(provider["key_masked"], "sk-…7890")
        self.assertNotIn("api_key", provider)

        status, payload = self._call("/api/studio/models", token=self.token)
        self.assertEqual(status, 200)
        ids = [item["id"] for item in payload["models"]]
        self.assertIn("glm-5.3", ids)
        self.assertTrue(any(c["id"] == provider["id"] for c in payload["channels"]))

        status, payload = self._call(
            "/api/studio/agents", method="POST", token=self.token, body={
                "name": "ChannelAgent", "provider_id": provider["id"],
                "model": "glm-5.3", "workspace": str(Path(self.tmp.name) / "ch-ws"),
            })
        self.assertEqual(status, 201)
        self.assertEqual(payload["agent"]["provider_id"], provider["id"])
        self.assertEqual(payload["agent"]["provider_name"], "测试渠道")

        status, payload = self._call(
            f"/api/studio/providers/{provider['id']}/test", method="POST",
            token=self.token, body={"deep": False})
        self.assertEqual(status, 200)
        self.assertFalse(payload["check"]["ok"])

        status, payload = self._call("/api/studio/capabilities", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("counts", payload)

        status, payload = self._call(
            f"/api/studio/providers/{provider['id']}", method="DELETE", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["detached_agents"], ["ChannelAgent"])

    def test_memory_and_extension_endpoints(self) -> None:
        status, payload = self._call(
            "/api/studio/agents", method="POST", token=self.token, body={
                "name": "ExtAgent", "workspace": str(Path(self.tmp.name) / "ext-ws"),
            })
        self.assertEqual(status, 201)
        agent_id = payload["agent"]["id"]

        status, payload = self._call(
            f"/api/studio/agents/{agent_id}/extensions", token=self.token)
        self.assertEqual(status, 200)
        for key in ("skills", "mcp", "memory", "selected_skills"):
            self.assertIn(key, payload)

        status, payload = self._call(
            f"/api/studio/agents/{agent_id}/memory", method="PUT", token=self.token,
            body={"name": "CLAUDE.md", "content": "长期约定：先给结论。\n"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["file"]["exists"])

        status, payload = self._call(
            f"/api/studio/agents/{agent_id}/memory", token=self.token)
        self.assertEqual(status, 200)
        claude = next(item for item in payload["files"] if item["name"] == "CLAUDE.md")
        self.assertIn("先给结论", claude["content"])

        status, payload = self._call(
            f"/api/studio/agents/{agent_id}/extensions", method="PUT", token=self.token,
            body={"skills": [], "mcp": [], "mcp_managed": True})
        self.assertEqual(status, 200)
        self.assertTrue(payload["agent"]["mcp_managed"])

        status, payload = self._call("/api/studio/sessions", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["sessions"], [])

        status, payload = self._call("/api/studio/sessions/missing", token=self.token)
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "session_not_found")

    def test_plugin_endpoints(self) -> None:
        plugin_dir = Path(self.tmp.name) / "source-plugin"
        (plugin_dir / ".claude-plugin").mkdir(parents=True)
        (plugin_dir / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": "http-plugin", "version": "0.1"}),
            encoding="utf-8")
        skill = plugin_dir / "skills" / "hello"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("# hello\n", encoding="utf-8")

        status, payload = self._call(
            "/api/studio/plugins", method="POST", token=self.token,
            body={"path": str(plugin_dir)})
        self.assertEqual(status, 201)
        plugin = payload["plugin"]
        self.assertEqual(plugin["name"], "http-plugin")
        self.assertEqual(plugin["counts"]["skills"], 1)

        status, payload = self._call("/api/studio/plugins", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["plugins"]], [plugin["id"]])

        status, payload = self._call(
            "/api/studio/agents", method="POST", token=self.token,
            body={"name": "PluginAgent",
                  "workspace": str(Path(self.tmp.name) / "plugin-ws")})
        agent_id = payload["agent"]["id"]
        status, payload = self._call(
            f"/api/studio/agents/{agent_id}/extensions", method="PUT",
            token=self.token, body={"plugins": [plugin["id"]]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["agent"]["plugins"], [plugin["id"]])

        status, payload = self._call(
            f"/api/studio/plugins/{plugin['id']}", method="DELETE", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["detached_agents"], ["PluginAgent"])

    def test_profile_endpoints(self) -> None:
        status, payload = self._call(
            "/api/studio/agents", method="POST", token=self.token, body={
                "name": "ProfileAgent", "model": "glm-5.3",
                "workspace": str(Path(self.tmp.name) / "prof-ws"),
            })
        self.assertEqual(status, 201)
        agent_id = payload["agent"]["id"]

        status, payload = self._call(
            "/api/studio/profiles", method="POST", token=self.token,
            body={"name": "基线", "note": "测试"})
        self.assertEqual(status, 201)
        profile = payload["profile"]
        self.assertEqual(profile["count"], 1)
        self.assertEqual(profile["agents"][0]["name"], "ProfileAgent")

        status, payload = self._call("/api/studio/profiles", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["profiles"]), 1)

        self._call(f"/api/studio/agents/{agent_id}", method="PATCH",
                   token=self.token, body={"model": "changed-model"})
        status, payload = self._call(
            f"/api/studio/profiles/{profile['id']}/apply", method="POST",
            token=self.token, body={})
        self.assertEqual(status, 200)
        self.assertEqual(payload["applied"], ["ProfileAgent"])
        status, payload = self._call(f"/api/studio/agents/{agent_id}", token=self.token)
        self.assertEqual(payload["agent"]["model"], "glm-5.3")

        status, payload = self._call(
            f"/api/studio/profiles/{profile['id']}", method="PATCH",
            token=self.token, body={"name": "改名后"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["profile"]["name"], "改名后")

        status, payload = self._call(
            f"/api/studio/profiles/{profile['id']}", method="DELETE", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["deleted"])

    def test_key_pool_and_skill_import_endpoints(self) -> None:
        status, payload = self._call(
            "/api/studio/providers", method="POST", token=self.token, body={
                "name": "池渠道", "kind": "openai",
                "base_url": "http://127.0.0.1:9", "api_key": "main-key-1",
                "models": ["m"],
            })
        provider_id = payload["provider"]["id"]

        status, payload = self._call(
            f"/api/studio/providers/{provider_id}/keys", method="POST",
            token=self.token, body={"api_key": "pool-key-2", "label": "备用"})
        self.assertEqual(status, 201)
        self.assertEqual(payload["total"], 2)
        key_id = payload["keys"][1]["id"]

        status, payload = self._call(
            f"/api/studio/providers/{provider_id}/keys", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["available"], 2)

        status, payload = self._call(
            f"/api/studio/providers/{provider_id}/keys/{key_id}",
            method="PATCH", token=self.token, body={"enabled": False})
        self.assertEqual(status, 200)
        self.assertEqual(payload["available"], 1)

        status, payload = self._call(
            f"/api/studio/providers/{provider_id}/keys/{key_id}",
            method="DELETE", token=self.token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 1)

        skill_dir = Path(self.tmp.name) / "local-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\ndescription: 本地\n---\n# x\n",
                                            encoding="utf-8")
        status, payload = self._call(
            "/api/studio/skills/import", method="POST", token=self.token,
            body={"path": str(skill_dir)})
        self.assertEqual(status, 201)
        self.assertEqual(payload["imported"][0]["name"], "local-skill")

    def test_library_and_usage_endpoints(self) -> None:
        status, payload = self._call("/api/studio/library", token=self.token)
        self.assertEqual(status, 200)
        for key in ("skills", "mcp", "plugins", "agents", "counts"):
            self.assertIn(key, payload)
        self.assertIn("skills", payload["counts"])

        status, payload = self._call("/api/studio/usage", token=self.token)
        self.assertEqual(status, 200)
        usage = payload["usage"]
        for key in ("by_agent", "by_model", "by_channel", "by_day",
                    "cache_rate", "cost_per_run", "input", "output"):
            self.assertIn(key, usage)

    def test_ui_script_is_valid_javascript(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        for name in ("studio.html", "workbench.html"):
            html = (STATIC_DIR / name).read_text(encoding="utf-8")
            blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
            self.assertTrue(blocks, name)
            for index, block in enumerate(blocks):
                script = Path(self.tmp.name) / f"{name}-{index}.js"
                script.write_text(block, encoding="utf-8")
                subprocess.run([node, "--check", str(script)], check=True,
                               capture_output=True, text=True)

    def test_markdown_renderer(self) -> None:
        """Markdown 渲染器：语法子集 + XSS 两条线都要过。

        用独立脚本而不是在这里内联，是为了本地能单独跑（`node
        scripts/deploy/check_markdown.js`），调试时比看 unittest 输出快。
        """

        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        script = (STATIC_DIR.parents[4] / "scripts" / "deploy" / "check_markdown.js")
        if not script.exists():
            self.skipTest(f"缺少 {script}")
        result = subprocess.run([node, str(script)], capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class ReverseProxyTests(unittest.TestCase):
    """反代部署形态：Origin 白名单 + 子路径前缀。

    默认形态（只绑回环、只认回环 Origin）不能因为多两个开关就放松，
    所以这里同时断言"不给白名单时公网 Origin 仍被拒"。
    """

    def _serve(self, **kwargs) -> str:
        tmp = tempfile.TemporaryDirectory(prefix="sf-studio-proxy-")
        self.addCleanup(tmp.cleanup)
        self.httpd, self.manager, self.token, self.port = build_server(
            port=0, state_path=Path(tmp.name) / "state.db", token="proxy-token",
            skills_home=Path(tmp.name) / "home", **kwargs)
        thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        thread.start()

        def stop() -> None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.manager.shutdown()
            getattr(self.httpd, "agent_manager").shutdown()

        self.addCleanup(stop)
        return f"http://127.0.0.1:{self.port}"

    def _post(self, base: str, path: str, *, origin: str) -> tuple[int, dict]:
        request = urllib.request.Request(
            base + path,
            data=json.dumps({"cluster_id": "missing", "text": "x"}).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json", "Connection": "close",
                     "X-Studio-Token": "proxy-token", "Origin": origin})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read().decode("utf-8"))

    def test_public_origin_rejected_without_whitelist(self) -> None:
        base = self._serve()
        status, payload = self._post(base, "/api/studio/messages",
                                    origin="https://studio.example.com")
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "origin_rejected")

    def test_whitelisted_origin_allowed(self) -> None:
        base = self._serve(allowed_origins=("https://studio.example.com",))
        status, payload = self._post(base, "/api/studio/messages",
                                    origin="https://studio.example.com")
        # 过了 Origin 校验，才会走到"集群不存在"的业务错误
        self.assertNotEqual(payload.get("code"), "origin_rejected")
        self.assertFalse(payload.get("ok"))

    def test_base_path_serves_ui_and_api(self) -> None:
        base = self._serve(base_path="/studio",
                           allowed_origins=["https://studio.example.com"])
        with urllib.request.urlopen(base + "/studio/", timeout=10) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("synapforge studio", response.read().decode("utf-8"))
        # 反代常把 /studio 的尾斜杠省掉
        with urllib.request.urlopen(base + "/studio", timeout=10) as response:
            self.assertEqual(response.status, 200)
        status, payload = self._post(base, "/studio/api/studio/messages",
                                    origin="https://studio.example.com")
        # 去掉前缀后是正常的 API 路由（集群不存在 → 业务错误，而不是 not_found 兜底）
        self.assertNotIn(payload.get("code"), {"not_found", "origin_rejected"})
        # 页面在子路径下拿到的是同一份单页（token 走 query，和本地形态一致）
        with urllib.request.urlopen(base + "/studio/?token=proxy-token", timeout=10) as response:
            self.assertIn("sf.studio.token", response.read().decode("utf-8"))

    def test_host_binding_defaults_to_loopback(self) -> None:
        self._serve()
        self.assertEqual(self.httpd.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
