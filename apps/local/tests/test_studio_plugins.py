"""Claude Code 插件：安装、解析、按 Agent 挂载、卸载。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import capabilities as studio_capabilities
from synapforge.studio import plugins as studio_plugins
from synapforge.studio import store as studio_store


class PluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-studio-plugin-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state.db"
        self.store = open_store(self.state)
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.agent = studio_agents.create_agent(
            self.store, name="Writer", workspace=str(self.root / "ws"))
        self.plugin_dir = self.root / "source-plugin"
        self._make_plugin(self.plugin_dir)

    def _make_plugin(self, folder: Path) -> None:
        (folder / ".claude-plugin").mkdir(parents=True)
        (folder / ".claude-plugin" / "plugin.json").write_text(json.dumps({
            "name": "demo-plugin",
            "description": "演示插件",
            "version": "1.2.3",
        }), encoding="utf-8")
        skill = folder / "skills" / "hello"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\ndescription: 打招呼\n---\n# hello\n",
                                        encoding="utf-8")
        agents = folder / "agents"
        agents.mkdir()
        (agents / "reviewer.md").write_text("---\nname: reviewer\n---\n审查代码。\n",
                                            encoding="utf-8")
        (folder / ".mcp.json").write_text(json.dumps({
            "mcpServers": {
                "plugin-mcp": {
                    "command": "python",
                    "args": ["${CLAUDE_PLUGIN_ROOT}/server.py"],
                    "env": {"PLUGIN_ROOT": "${CLAUDE_PLUGIN_ROOT}",
                            "PLUGIN_DATA": "${CLAUDE_PLUGIN_DATA}"},
                }
            }
        }), encoding="utf-8")
        (folder / "hooks").mkdir()
        (folder / "hooks" / "hooks.json").write_text("{}", encoding="utf-8")
        (folder / "commands").mkdir()
        (folder / "commands" / "do.md").write_text("# do\n", encoding="utf-8")

    def test_install_reads_parts_and_copies_to_library(self) -> None:
        plugin = studio_plugins.install_plugin(
            self.store, self.plugin_dir, state_path=self.state)
        self.assertEqual(plugin["name"], "demo-plugin")
        self.assertEqual(plugin["version"], "1.2.3")
        self.assertEqual(plugin["counts"], {"skills": 1, "mcp": 1, "agents": 1})
        library = self.root / "studio" / "plugins" / "demo-plugin"
        self.assertTrue((library / "skills" / "hello" / "SKILL.md").exists())
        self.assertTrue(plugin["path"].startswith(str(library)))
        left = {item["what"] for item in plugin["left"]}
        self.assertIn("hooks", left)
        self.assertIn("commands", left)

    def test_agent_parts_substitute_plugin_root(self) -> None:
        plugin = studio_plugins.install_plugin(
            self.store, self.plugin_dir, state_path=self.state)
        studio_agents.update_agent_extensions(
            self.store, self.agent["id"], plugins=[plugin["id"]])
        agent = studio_store.get_agent(self.store, self.agent["id"])
        parts = studio_plugins.agent_plugin_parts(
            self.store, agent, self.state)
        self.assertEqual(len(parts["skills"]), 1)
        self.assertIn("plugin-mcp", parts["mcp"])
        config = parts["mcp"]["plugin-mcp"]
        self.assertIn(plugin["path"], config["args"][0])
        self.assertIn(plugin["path"], config["env"]["PLUGIN_ROOT"])
        self.assertIn("plugin-data", config["env"]["PLUGIN_DATA"])
        self.assertEqual(len(parts["subagents"]), 1)

    def test_mount_skills_and_subagents(self) -> None:
        plugin = studio_plugins.install_plugin(
            self.store, self.plugin_dir, state_path=self.state)
        studio_agents.update_agent_extensions(
            self.store, self.agent["id"], plugins=[plugin["id"]])
        agent = studio_store.get_agent(self.store, self.agent["id"])
        parts = studio_plugins.agent_plugin_parts(
            self.store, agent, self.state)
        workspace = Path(self.agent["workspace"])
        report = studio_capabilities.sync_workspace_skills(
            workspace, Path.home(), [], extra_skills=parts["skills"])
        self.assertIn("hello", report["mounted"])
        self.assertTrue((workspace / ".claude" / "skills" / "hello" / "SKILL.md").exists())
        sub = studio_plugins.sync_plugin_subagents(workspace, parts["subagents"])
        self.assertIn("demo-plugin--reviewer", sub["mounted"])
        self.assertTrue((workspace / ".claude" / "agents" /
                         "demo-plugin--reviewer.md").exists())

    def test_agent_extensions_lists_plugin(self) -> None:
        plugin = studio_plugins.install_plugin(
            self.store, self.plugin_dir, state_path=self.state)
        studio_agents.update_agent_extensions(
            self.store, self.agent["id"], plugins=[plugin["id"]])
        data = studio_agents.agent_extensions(self.store, self.agent["id"])
        self.assertEqual([item["name"] for item in data["plugins"]], ["demo-plugin"])
        self.assertEqual(data["selected_plugins"], [plugin["id"]])
        self.assertEqual(len(data["plugin_parts"]["skills"]), 1)
        self.assertIn("plugin-mcp", data["plugin_parts"]["mcp"])

    def test_delete_detaches_and_removes_copy(self) -> None:
        plugin = studio_plugins.install_plugin(
            self.store, self.plugin_dir, state_path=self.state)
        studio_agents.update_agent_extensions(
            self.store, self.agent["id"], plugins=[plugin["id"]])
        result = studio_plugins.delete_plugin(
            self.store, plugin["id"], state_path=self.state)
        self.assertEqual(result["detached_agents"], ["Writer"])
        agent = studio_store.get_agent(self.store, self.agent["id"])
        self.assertEqual(agent["plugins"], [])
        self.assertFalse(Path(plugin["path"]).exists())

    def test_invalid_folder_rejected(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        with self.assertRaises(studio_plugins.StudioError) as ctx:
            studio_plugins.install_plugin(self.store, empty, state_path=self.state)
        self.assertEqual(ctx.exception.code, "plugin_invalid")


if __name__ == "__main__":
    unittest.main()
