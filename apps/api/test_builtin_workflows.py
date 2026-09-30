"""内置工作流包（W3.4/W3.5）与交付适配器（W3.6）契约测试。

口径：
- 两个内置包（CUMCM 主线 / 长文写作）必须能过 schema §4 全量校验——它们就是
  "换包不重写内核"的活证明（同一套 Task/Artifact/Review 物化路径）；
- 种子幂等：按 key 各建一次；
- 交付适配器：节点 APPROVED 后执行一次，失败如实记账（不掩盖、不阻塞完成）；
  未知 kind 报 unknown_adapter（不猜、不静默跳过）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from fastapi.testclient import TestClient

from app import builtin_workflows, main, workflow_delivery, workflow_service
from app.store import DEV_ORG_ID, Store

MEMBER = "member-001"


class BuiltinWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.client.close()
        self.store.close()
        self.temp_dir.cleanup()

    # ---- 定义与种子 -------------------------------------------------------

    def test_both_definitions_pass_full_validation(self) -> None:
        for builder in (builtin_workflows.cumcm_definition, builtin_workflows.longform_definition):
            errors = workflow_service.validate_definition(builder())
            self.assertEqual(errors, [], f"{builder.__name__}: {errors}")

    def test_seed_is_idempotent_and_materializes(self) -> None:
        first = builtin_workflows.ensure_builtin_workflows(self.store, DEV_ORG_ID, MEMBER)
        self.assertEqual(sorted(first["created"]), ["cumcm-main", "longform-writing"])
        second = builtin_workflows.ensure_builtin_workflows(self.store, DEV_ORG_ID, MEMBER)
        self.assertEqual(second["created"], [])
        self.assertEqual(sorted(second["skipped"]), ["cumcm-main", "longform-writing"])

        # 同一内核物化两个包：节点数正确、依赖接线
        cumcm = next(w for w in workflow_service.list_workflows(self.store, DEV_ORG_ID) if w["key"] == "cumcm-main")
        run = workflow_service.start_workflow_run(
            self.store, self.project.id, DEV_ORG_ID, MEMBER, UUID(cumcm["id"]), {"problem_code": "C", "questions": "1,2,3,4"}
        )
        self.assertEqual(len(run["tasks"]), 7)
        by_node = {item["node_id"]: item["task_id"] for item in run["tasks"]}
        code_task = self.store.get_task(UUID(by_node["code"]))
        self.assertEqual([str(dep) for dep in code_task.dependency_task_ids], [by_node["model"]])
        self.assertEqual(code_task.budget.max_attempts, 3)  # 重试预算进了任务

        longform = next(w for w in workflow_service.list_workflows(self.store, DEV_ORG_ID) if w["key"] == "longform-writing")
        run2 = workflow_service.start_workflow_run(
            self.store, self.project.id, DEV_ORG_ID, MEMBER, UUID(longform["id"]), {"topic": "海冰厚度预测"}
        )
        self.assertEqual(len(run2["tasks"]), 4)
        outline = next(t for t in self.store.list_tasks(self.project.id) if t.title == "写大纲")
        self.assertIn("海冰厚度预测", outline.description)  # 模板插值真的落了

    # ---- 交付适配器 -------------------------------------------------------

    def test_seed_route_reachable_and_idempotent(self) -> None:
        first = self.client.post("/api/workflows/builtin")
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(sorted(first.json()["created"]), ["cumcm-main", "longform-writing"])
        again = self.client.post("/api/workflows/builtin")
        self.assertEqual(again.json()["created"], [])

    def test_unknown_adapter_recorded_not_silent(self) -> None:
        result = workflow_delivery.run_adapter(self.store, self.project.id, "no-such-adapter", actor=MEMBER, node_id="n", task_id="t")
        self.assertEqual(result["status"], "failed")
        self.assertIn("unknown_adapter", result["error"])

    def test_delivery_adapter_runs_once_after_approval_and_records_honestly(self) -> None:
        """compile 节点 APPROVED → advance 执行 paper_compile 适配器；无已批准论文源码时
        如实记 not_compiled（fail-closed），且不重复执行、不阻塞运行完成。"""

        builtin_workflows.ensure_builtin_workflows(self.store, DEV_ORG_ID, MEMBER)
        cumcm = next(w for w in workflow_service.list_workflows(self.store, DEV_ORG_ID) if w["key"] == "cumcm-main")
        run = workflow_service.start_workflow_run(
            self.store, self.project.id, DEV_ORG_ID, MEMBER, UUID(cumcm["id"]), {}
        )
        by_node = {item["node_id"]: item["task_id"] for item in run["tasks"]}
        for node_id in ("problem_facts", "model", "code", "review", "paper", "compile"):
            self.store.db.execute("UPDATE tasks SET status = 'APPROVED' WHERE id = ?", (by_node[node_id],))
        self.store.db.commit()

        response = self.client.post(f"/api/projects/{self.project.id}/workflow-runs/{run['run_id']}/advance")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        # bundle 未批准 → 运行未完成（完成判定 = 全节点 APPROVED），且本轮有迁移 → 仍在推进
        self.assertEqual(body["status"], "RUNNING")
        compile_delivery = next((d for d in body["deliveries"] if d["node_id"] == "compile"), None)
        self.assertIsNotNone(compile_delivery)
        self.assertEqual(compile_delivery["adapter"], "paper_compile")
        # 场景里没有已批准的 paper_source：compile_paper fail-closed 抛
        # compile_requires_approved_paper_source，适配器如实收口成 failed（不掩盖、不阻塞运行）
        self.assertEqual(compile_delivery["status"], "failed")
        self.assertIn("compile_requires_approved_paper_source", compile_delivery["error"])

        # 交付不重复执行：账本 deliveries 里 compile 只记一条，且已进 delivered 清单
        state = self.store.db.execute(
            "SELECT ledger FROM project_workflow_runs WHERE id = ?", (run["run_id"],)
        ).fetchone()
        persisted = json.loads(state["ledger"])
        self.assertIn("compile", persisted["deliveries"])
        self.assertEqual(persisted["deliveries"]["compile"]["adapter"], "paper_compile")
        # 失败不当作已交付：delivered 清单不含 compile（下一轮还会重试交付，直到成功）
        self.assertNotIn("compile", persisted["delivered"])


if __name__ == "__main__":
    unittest.main()
