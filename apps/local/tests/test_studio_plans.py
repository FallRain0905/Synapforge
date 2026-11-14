"""集群规划：解析、草稿、确认建集群 + 预置任务。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import plans as studio_plans
from synapforge.studio import store as studio_store


class PlanParseTests(unittest.TestCase):
    def test_fenced_json(self) -> None:
        text = '说明\n```json\n[{"title":"A","description":"d","agent_name":"Writer"}]\n```'
        result = studio_plans.parse_items(text)
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["title"], "A")

    def test_bare_array_with_prose(self) -> None:
        text = '好的，计划如下： [{"title":"B","agent_name":"W"}] 以上。'
        result = studio_plans.parse_items(text)
        self.assertTrue(result["ok"])
        self.assertEqual(result["items"][0]["title"], "B")

    def test_wrapped_in_object(self) -> None:
        text = json.dumps({"tasks": [{"title": "C"}]})
        result = studio_plans.parse_items(text)
        self.assertTrue(result["ok"])

    def test_no_array_fails_honestly(self) -> None:
        result = studio_plans.parse_items("我先看看情况，稍后再说。")
        self.assertFalse(result["ok"])
        self.assertIn("没有找到", result["error"])
        self.assertTrue(result["raw"])

    def test_items_without_title_dropped(self) -> None:
        text = json.dumps([{"description": "no title"}, {"title": "keep"}])
        result = studio_plans.parse_items(text)
        self.assertTrue(result["ok"])
        self.assertEqual(len(result["items"]), 1)

    def test_string_items_become_tasks(self) -> None:
        result = studio_plans.parse_items(json.dumps(["写简介", "画图"]))
        self.assertTrue(result["ok"])
        self.assertEqual([i["title"] for i in result["items"]], ["写简介", "画图"])


class PlanFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="sf-plan-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = open_store(self.root / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.lead = studio_agents.create_agent(
            self.store, name="Planner", model="glm-5.3",
            workspace=str(self.root / "planner-ws"))
        self.writer = studio_agents.create_agent(
            self.store, name="Writer", model="auto/glm-5.3-flash",
            workspace=str(self.root / "writer-ws"))
        self.worker = studio_agents.create_agent(
            self.store, name="Worker", model="glm-5.3",
            workspace=str(self.root / "worker-ws"))

    def test_create_and_send_message_creates_run(self) -> None:
        plan = studio_plans.create_plan(
            self.store, lead_agent_id=self.lead["id"], objective="验证规划")
        self.assertEqual(plan["status"], "CHAT")
        result = studio_plans.send_message(self.store, plan["id"], "帮我做一件事")
        self.assertEqual(result["run"]["status"], "QUEUED")
        refreshed = studio_plans.plan_state(self.store, plan["id"])
        self.assertEqual(refreshed["chat_run_id"], result["run"]["id"])

    def test_generate_plan_resolves_agents(self) -> None:
        plan = studio_plans.create_plan(
            self.store, lead_agent_id=self.lead["id"], objective="验证解析")
        text = json.dumps([
            {"title": "写说明", "description": "写一份说明", "agent_name": "Writer"},
            {"title": "做另一件", "agent_name": "Worker", "model": "glm-5.3"},
        ])
        result = studio_plans.generate_plan(self.store, plan["id"], text=text)
        self.assertTrue(result["ok"])
        items = result["items"]
        self.assertEqual(items[0]["agent_id"], self.writer["id"])
        self.assertEqual(items[0]["model"], "auto/glm-5.3-flash")
        self.assertEqual(items[1]["agent_id"], self.worker["id"])
        self.assertFalse(items[0]["invalid"])
        self.assertEqual(result["plan"]["status"], "READY")

    def test_unknown_agent_marks_invalid(self) -> None:
        plan = studio_plans.create_plan(
            self.store, lead_agent_id=self.lead["id"], objective="验证无效")
        text = json.dumps([{"title": "X", "agent_name": "不存在的Agent"}])
        result = studio_plans.generate_plan(self.store, plan["id"], text=text)
        self.assertTrue(result["ok"])
        self.assertTrue(result["items"][0]["invalid"])

    def test_confirm_builds_cluster_and_tasks(self) -> None:
        plan = studio_plans.create_plan(
            self.store, lead_agent_id=self.lead["id"], objective="验证确认")
        text = json.dumps([
            {"title": "任务一", "description": "写 A", "agent_name": "Writer"},
            {"title": "任务二", "description": "写 B", "agent_name": "Worker"},
        ])
        studio_plans.generate_plan(self.store, plan["id"], text=text)
        result = studio_plans.confirm_plan(self.store, plan["id"])
        cluster = result["cluster"]
        self.assertEqual(cluster["status"], "READY")
        self.assertEqual(len(result["tasks"]), 2)
        # lead + 2 个 worker 组
        self.assertGreaterEqual(len(cluster["roles"]), 2)
        confirmed = studio_store.get_cluster_plan(self.store, plan["id"])
        self.assertEqual(confirmed["status"], "CONFIRMED")
        self.assertEqual(confirmed["cluster_id"], cluster["id"])
        self.assertEqual(len(studio_store.list_roles(self.store, cluster["id"])),
                         len(cluster["roles"]))

    def test_confirm_rejects_invalid_items(self) -> None:
        plan = studio_plans.create_plan(
            self.store, lead_agent_id=self.lead["id"], objective="验证拒绝")
        text = json.dumps([{"title": "X", "agent_name": "不存在"}])
        studio_plans.generate_plan(self.store, plan["id"], text=text)
        with self.assertRaises(studio_plans.StudioError) as ctx:
            studio_plans.confirm_plan(self.store, plan["id"])
        self.assertEqual(ctx.exception.code, "plan_invalid_items")

    def test_update_items_and_discard(self) -> None:
        plan = studio_plans.create_plan(
            self.store, lead_agent_id=self.lead["id"], objective="验证编辑")
        result = studio_plans.update_items(self.store, plan["id"], [
            {"title": "手工任务", "agent_id": self.writer["id"], "model": "glm-5.3"},
        ])
        self.assertEqual(result["items"][0]["agent_name"], "Writer")
        discarded = studio_plans.discard_plan(self.store, plan["id"])
        self.assertTrue(discarded["discarded"])
        self.assertEqual(studio_store.get_cluster_plan(
            self.store, plan["id"])["status"], "DISCARDED")

    def test_plan_prompt_lists_agents(self) -> None:
        plan = studio_plans.create_plan(
            self.store, lead_agent_id=self.lead["id"], objective="验证提示")
        prompt = studio_plans.plan_prompt(self.store, plan["id"])
        self.assertIn("Writer", prompt)
        self.assertIn("JSON", prompt)


if __name__ == "__main__":
    unittest.main()
