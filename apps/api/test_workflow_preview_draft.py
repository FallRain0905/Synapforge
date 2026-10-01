"""阶段 4 收尾（W4.3 试运行 / W4.4 反向草稿）契约测试。

口径：
- 试运行只展开任务图，**不创建任何对象**（无任务、无运行、无事件）——防"误以为已得到结果"；
  warnings 收集运行期才会变成问题的事（输入缺失、manual 节点、tests_passed v1 边界、交付适配器）；
- 反向草稿从真实任务图抽取（取消任务不计入；历史批准的产物类型 → 建议门禁；agent → 角色
  并集能力），草稿必须能过 schema §4 校验——它是给编辑器补全的起点，不是直接可发布的模板。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import main, workflow_service
from app.contracts import ArtifactCreate, AgentRegister, ProjectCreate, ReviewCreate, ReviewerKind, TaskCreate
from app.store import Store

MEMBER = "member-001"


def two_node_definition() -> dict:
    return {
        "schema_version": 1,
        "workflow": {"key": "preview-pack", "name": "预览验收包"},
        "stages": [{"id": "facts", "title": "事实"}, {"id": "solve", "title": "求解"}],
        "role_bindings": [{"id": "solver", "role_name": "求解", "capability_requirements": ["files.write"]}],
        "nodes": [
            {"id": "collect", "stage_id": "facts", "title": "整理事实", "goal": "整理",
             "depends_on": [], "mode": "manual"},
            {"id": "solve", "stage_id": "solve", "title": "求解", "goal": "求解",
             "depends_on": ["collect"], "mode": "auto", "role_binding": "solver",
             "prompt": {"task_template": "围绕 {{input.topic}} 求解第 {{input.q}} 问"},
             "outputs": [{"name": "result_table", "artifact_type": "result_table", "path": "outputs/result.csv"}],
             "gate_policy": "solve-gate", "budget": {"max_attempts": 2}, "on_fail": "retry"},
        ],
        "gate_policies": [{"id": "solve-gate", "spec": ["artifact:result_table approved"], "on_block": "escalate_human"}],
    }


class WorkflowPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        workflow_service.ensure_schema(self.store)  # 零副作用断言要直查运行表
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.client.close()
        self.store.close()
        self.temp_dir.cleanup()

    # ---- W4.3 试运行 ------------------------------------------------------

    def test_preview_expands_plan_without_creating_anything(self) -> None:
        """试运行只展开：插值落了、依赖边在、manual 节点标需要人工；库零副作用。"""

        events_before = self.store.db.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"]
        tasks_before = self.store.db.execute("SELECT COUNT(*) AS c FROM tasks WHERE project_id = ?", (str(self.project.id),)).fetchone()["c"]
        response = self.client.post(
            "/api/workflows/preview",
            json={"definition": two_node_definition(), "inputs": {"topic": "海冰", "q": "3"}},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["valid"])
        solve = next(n for n in body["plan"]["nodes"] if n["node_id"] == "solve")
        self.assertEqual(solve["resolved_prompt"], "围绕 海冰 求解第 3 问")
        self.assertEqual(body["plan"]["edges"], [["collect", "solve"]])
        self.assertTrue(next(n for n in body["plan"]["nodes"] if n["node_id"] == "collect")["requires_human"])
        warnings = " ".join(body["warnings"])
        self.assertIn("manual", warnings)
        # 零副作用：没有任务、没有运行、没有新事件
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM tasks WHERE project_id = ?", (str(self.project.id),)).fetchone()["c"], tasks_before)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM project_workflow_runs").fetchone()["c"], 0)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) AS c FROM events").fetchone()["c"], events_before)

    def test_preview_warns_on_missing_inputs(self) -> None:
        """模板引用了未提供的输入 → warning 点名（运行时会保留占位符原样，不猜）。"""

        response = self.client.post(
            "/api/workflows/preview", json={"definition": two_node_definition(), "inputs": {"topic": "海冰"}}
        )
        body = response.json()
        self.assertTrue(body["valid"])
        self.assertTrue(any("q" in item for item in body["warnings"]), body["warnings"])

    def test_preview_invalid_definition_lists_errors(self) -> None:
        broken = two_node_definition()
        broken["nodes"][1].pop("role_binding")
        response = self.client.post("/api/workflows/preview", json={"definition": broken, "inputs": {}})
        self.assertEqual(response.status_code, 200)  # 试运行不因定义非法而 4xx：错误进结果体
        body = response.json()
        self.assertFalse(body["valid"])
        self.assertTrue(any("role_binding" in item for item in body["errors"]))
        self.assertIsNone(body["plan"])

    def test_preview_by_workflow_id_with_org_isolation(self) -> None:
        created = self.client.post("/api/workflows", json={"definition": two_node_definition()}).json()
        response = self.client.post(
            "/api/workflows/preview", json={"workflow_id": created["id"], "inputs": {"topic": "x", "q": "1"}}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["workflow_version_id"], created["current_version_id"])
        with self.assertRaisesRegex(workflow_service.WorkflowError, "workflow_not_found"):
            workflow_service.preview_workflow_version(
                self.store, "org-somebody-else", UUID(created["id"]), {}
            )

    # ---- W4.4 反向草稿 ----------------------------------------------------

    def test_draft_from_project_tasks_with_deps_roles_and_gate_suggestions(self) -> None:
        agent = self.store.register_agent(
            AgentRegister(agent_id="draft-agent", display_name="Draft Solver", owner_member_id=MEMBER)
        )
        first = self.store.create_task(self.project.id, TaskCreate(title="整理事实", stage="problem_analysis", description="事实清单"))
        second = self.store.create_task(
            self.project.id,
            TaskCreate(title="求解", stage="modeling", required_capabilities=["files.write"], dependency_task_ids=[first.id]),
        )
        self.store.db.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (agent.agent_id, str(second.id)))
        self.store.db.commit()
        artifact = self.store.create_artifact(
            self.project.id, ArtifactCreate(
                name="result.csv", artifact_type="result_table", task_id=second.id
            ), created_by=MEMBER
        )
        self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="artifact", target_id=artifact.id, verdict="APPROVED", summary="ok",
                reviewer=MEMBER, reviewer_kind=ReviewerKind.MEMBER, idempotency_key="draft-review-1",
            ),
        )
        self.store.db.execute("UPDATE tasks SET status = 'APPROVED' WHERE id = ?", (str(second.id),))
        self.store.db.commit()

        response = self.client.get(f"/api/projects/{self.project.id}/workflow-draft")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        definition = body["definition"]
        # 草稿必须能过 schema §4 校验——它是给编辑器补全的起点，不是半成品
        self.assertEqual(body["validation_errors"], [], body["validation_errors"])
        # 样例项目自带 5 个种子任务——草稿如实收全部未取消任务，这里定向断言自己建的两个
        self.assertGreaterEqual(len(definition["nodes"]), 2)
        solve_node = next(n for n in definition["nodes"] if n["title"] == "求解")
        self.assertEqual(solve_node["depends_on"], [f"task-{str(first.id)[:8]}"])
        self.assertEqual(solve_node["role_binding"], "draft-agent")
        # 历史批准 → 建议门禁
        self.assertEqual(solve_node["gate_policy"], "gate-" + solve_node["id"])
        self.assertIn("artifact:result_table approved", " ".join(json.dumps(g) for g in definition["gate_policies"]))
        # 无 agent 领取的任务 → manual 且无 role_binding（互斥校验）
        facts_node = next(n for n in definition["nodes"] if n["title"] == "整理事实")
        self.assertEqual(facts_node["mode"], "manual")
        self.assertNotIn("role_binding", facts_node)

    def test_draft_empty_project_rejected(self) -> None:
        empty_project = self.store.create_project(ProjectCreate(name="空项目"))
        response = self.client.get(f"/api/projects/{empty_project.id}/workflow-draft")
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["detail"]["code"], "workflow_draft_empty")


if __name__ == "__main__":
    unittest.main()
