"""MCP submit_plan 工具：主 Agent 通过工具提交结构化计划。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from synapforge import mcp
from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import plans as studio_plans
from synapforge.studio import store as studio_store


class SubmitPlanToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-mcp-plan-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "state.db"
        store = open_store(self.db)
        studio_store.ensure_schema(store)
        self.lead = studio_agents.create_agent(
            store, name="Planner", workspace=str(self.root / "planner"))
        self.writer = studio_agents.create_agent(
            store, name="Writer", model="glm-5.3", workspace=str(self.root / "writer"))
        self.plan = studio_plans.create_plan(
            store, lead_agent_id=self.lead["id"], objective="验证工具提交")
        self.plan_id = self.plan["id"]
        store.close()

    def _server(self, plan_id: str | None = None) -> mcp.McpServer:
        server = mcp.McpServer(agent_id=self.lead["id"],
                               project_id=self.lead["project_id"],
                               store_path=self.db, plan_id=plan_id or self.plan_id)
        self.addCleanup(server.close)
        return server

    def test_tool_registered(self) -> None:
        names = [tool["name"] for tool in mcp.TOOLS]
        self.assertIn("synapforge_submit_plan", names)
        tool = next(t for t in mcp.TOOLS if t["name"] == "synapforge_submit_plan")
        self.assertIn("tasks", tool["inputSchema"]["properties"])
        self.assertEqual(tool["inputSchema"]["required"], ["tasks"])

    def test_submit_writes_items(self) -> None:
        server = self._server()
        result = server.call("synapforge_submit_plan", {
            "title": "两份说明",
            "objective": "写两份技术说明",
            "tasks": [
                {"title": "写向量检索", "description": "产出 vector.md",
                 "agent_name": "Writer"},
                {"title": "写混合检索", "description": "产出 hybrid.md",
                 "agent_name": "Writer", "role_prompt": "重点讲 RRF"},
            ],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["item_count"], 2)
        store = open_store(self.db)
        try:
            plan = studio_store.get_cluster_plan(store, self.plan_id)
        finally:
            store.close()
        self.assertEqual(plan["status"], "READY")
        self.assertEqual(len(plan["items"]), 2)
        self.assertEqual(plan["items"][0]["agent_id"], self.writer["id"])
        self.assertEqual(plan["title"], "两份说明")

    def test_unknown_agent_marked_invalid(self) -> None:
        server = self._server()
        result = server.call("synapforge_submit_plan", {
            "tasks": [{"title": "X", "agent_name": "不存在"}],
        })
        self.assertTrue(result["ok"])
        self.assertTrue(result["items"][0]["invalid"])

    def test_empty_tasks_rejected(self) -> None:
        server = self._server()
        with self.assertRaises(mcp.McpToolError) as ctx:
            server.call("synapforge_submit_plan", {"tasks": []})
        self.assertEqual(ctx.exception.code, "missing_argument")

    def test_missing_plan_rejected(self) -> None:
        server = mcp.McpServer(agent_id=self.lead["id"], store_path=self.db)
        self.addCleanup(server.close)
        with self.assertRaises(mcp.McpToolError) as ctx:
            server.call("synapforge_submit_plan", {"tasks": [{"title": "X"}]})
        self.assertEqual(ctx.exception.code, "plan_required")

    def test_generate_prefers_tool_result(self) -> None:
        server = self._server()
        server.call("synapforge_submit_plan", {
            "tasks": [{"title": "工具提交的任务", "agent_name": "Writer"}],
        })
        store = open_store(self.db)
        try:
            # 没有 assistant 文本，也应能从工具写入的 items 里拿到
            result = studio_plans.generate_plan(store, self.plan_id)
        finally:
            store.close()
        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "tool")
        self.assertEqual(result["items"][0]["title"], "工具提交的任务")

    def test_confirm_after_tool_submit(self) -> None:
        server = self._server()
        server.call("synapforge_submit_plan", {
            "tasks": [
                {"title": "任务一", "agent_name": "Writer"},
                {"title": "任务二", "agent_name": "Planner"},
            ],
        })
        store = open_store(self.db)
        try:
            result = studio_plans.confirm_plan(store, self.plan_id)
        finally:
            store.close()
        self.assertEqual(len(result["tasks"]), 2)
        self.assertGreaterEqual(len(result["cluster"]["roles"]), 2)


class RunPlanIdTests(unittest.TestCase):
    def test_run_carries_plan_id(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sf-run-plan-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        store = open_store(root / "state.db")
        self.addCleanup(store.close)
        studio_store.ensure_schema(store)
        agent = studio_agents.create_agent(
            store, name="Planner", workspace=str(root / "ws"))
        result = studio_agents.start_task(store, agent["id"], "聊一聊", plan_id="plan-abc")
        self.assertEqual(result["run"]["plan_id"], "plan-abc")
        runs = studio_store.list_agent_runs(store, agent["id"])
        self.assertEqual(runs[0]["plan_id"], "plan-abc")

    def test_plan_mcp_config_includes_plan_server(self) -> None:
        """规划会话的 mcp-config 必须多挂一个带 --plan 的 synapforge 服务。"""

        from synapforge.studio.agent_runtime import AgentRunWorker, RunBroker

        tmp = tempfile.TemporaryDirectory(prefix="sf-run-mcp-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        state = root / "state.db"
        worker = AgentRunWorker("arun-test", state, RunBroker(), plan_id="plan-xyz")
        path = worker._write_mcp_config({"id": "agent-1", "mcp": [], "mcp_managed": False})
        payload = json.loads(path.read_text(encoding="utf-8"))
        servers = payload["mcpServers"]
        self.assertIn("synapforge_plan", servers)
        args = servers["synapforge_plan"]["args"]
        self.assertIn("--plan", args)
        self.assertIn("plan-xyz", args)
        self.assertIn("--state", args)
        worker._cleanup_settings()

    def test_no_plan_server_without_plan(self) -> None:
        from synapforge.studio.agent_runtime import AgentRunWorker, RunBroker

        tmp = tempfile.TemporaryDirectory(prefix="sf-run-noplan-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        worker = AgentRunWorker("arun-test2", root / "state.db", RunBroker())
        path = worker._write_mcp_config({"id": "agent-1", "mcp": [], "mcp_managed": False})
        payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertNotIn("synapforge_plan", payload["mcpServers"])
        worker._cleanup_settings()


if __name__ == "__main__":
    unittest.main()
