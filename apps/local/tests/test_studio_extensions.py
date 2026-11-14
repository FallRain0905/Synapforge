"""按 Agent 的技能挂载、MCP 选择与记忆文件。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import capabilities as studio_capabilities
from synapforge.studio import store as studio_store


class ExtensionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-ext-")
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.store = open_store(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.agent = studio_agents.create_agent(
            self.store, name="Writer", workspace=str(Path(self.tmp.name) / "ws"))
        self.skill_dir = self.home / ".agents" / "skills" / "demo-skill"
        self.skill_dir.mkdir(parents=True)
        (self.skill_dir / "SKILL.md").write_text(
            "---\ndescription: 演示技能\n---\n# demo\n", encoding="utf-8")
        (self.skill_dir / "run.py").write_text("print('ok')\n", encoding="utf-8")
        (self.home / ".claude.json").write_text(json.dumps({
            "mcpServers": {
                "demo-mcp": {"command": "python", "args": ["server.py"],
                             "env": {"DEMO_TOKEN": "secret"}},
            }
        }), encoding="utf-8")

    def test_skill_inventory_and_mount(self) -> None:
        data = studio_agents.agent_extensions(self.store, self.agent["id"],
                                              home=self.home)
        skill = next(item for item in data["skills"] if item["name"] == "demo-skill")
        self.assertTrue(skill["id"].startswith("agents:"))
        self.assertEqual(skill["description"], "演示技能")

        updated = studio_agents.update_agent_extensions(
            self.store, self.agent["id"], skills=[skill["id"]], home=self.home)
        self.assertEqual(updated["agent"]["skills"], [skill["id"]])

        workspace = Path(self.agent["workspace"])
        report = studio_capabilities.sync_workspace_skills(
            workspace, self.home, [skill["id"]])
        self.assertEqual(report["mounted"], ["demo-skill"])
        self.assertTrue((workspace / ".claude" / "skills" / "demo-skill" / "SKILL.md").exists())
        manifest = json.loads((workspace / ".claude" / "skills" /
                               ".synapforge-managed.json").read_text(encoding="utf-8"))
        self.assertIn("demo-skill", manifest["skills"])

        report = studio_capabilities.sync_workspace_skills(workspace, self.home, [])
        self.assertEqual(report["mounted"], [])
        self.assertFalse((workspace / ".claude" / "skills" / "demo-skill").exists())

    def test_mcp_inventory_selection_and_payload(self) -> None:
        data = studio_agents.agent_extensions(self.store, self.agent["id"],
                                              home=self.home)
        mcp = next(item for item in data["mcp"] if item["name"] == "demo-mcp")
        self.assertNotIn("secret", json.dumps(data))
        updated = studio_agents.update_agent_extensions(
            self.store, self.agent["id"], mcp=[mcp["id"]], mcp_managed=True,
            home=self.home)
        self.assertTrue(updated["agent"]["mcp_managed"])
        payload = studio_capabilities.mcp_payload(
            self.home, updated["agent"]["mcp"], [updated["agent"]])
        self.assertIn("demo-mcp", payload["mcpServers"])
        self.assertEqual(payload["mcpServers"]["demo-mcp"]["env"]["DEMO_TOKEN"], "secret")
        self.assertEqual(payload["missing"], [])

    def test_memory_write_and_read(self) -> None:
        saved = studio_agents.save_agent_memory(
            self.store, self.agent["id"], name="CLAUDE.md",
            content="项目约定：先给结论。\n")
        self.assertTrue(saved["file"]["exists"])
        data = studio_agents.agent_memory(self.store, self.agent["id"])
        claude = next(item for item in data["files"] if item["name"] == "CLAUDE.md")
        self.assertIn("先给结论", claude["content"])
        context = studio_capabilities.memory_context(self.agent["workspace"])
        self.assertIn("先给结论", context)
        with self.assertRaises(studio_agents.StudioError):
            studio_agents.save_agent_memory(
                self.store, self.agent["id"], name="../evil.md", content="x")

    def test_update_extensions_ignores_unknown_ids(self) -> None:
        result = studio_agents.update_agent_extensions(
            self.store, self.agent["id"], skills=["missing:skill"],
            mcp=["mcp-missing"], home=self.home)
        self.assertEqual(result["agent"]["skills"], [])
        self.assertEqual(result["agent"]["mcp"], [])
        self.assertEqual(sorted(result["ignored"]), ["mcp-missing", "missing:skill"])


if __name__ == "__main__":
    unittest.main()
