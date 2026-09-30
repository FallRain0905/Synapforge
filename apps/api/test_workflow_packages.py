"""通用工作流包（W3.1）契约测试——按 docs/WORKFLOW_SCHEMA.md v1 定稿口径。

口径：
- 服务端按 schema §4 八条校验，**错误逐条列出**（id + 原因），绝不静默丢弃；
- `role_binding` 是互斥校验：auto/hybrid 必须有、manual 必须没有（§4 规则 6）；
- 版本一经发布只读：追加新版本，旧定义永不改写（§1 规则 2）；
- 运行物化 = 任务骨架（depends_on → 任务依赖；预算/能力从 role_binding 进任务），
  应用模板**不等于得到结果**（§1 规则 1，响应如实标注）；
- 组织隔离：工作流包按 organization_id 归属，跨组织不可见（对齐 RLS 口径）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import main, workflow_service
from app.store import Store

MEMBER = "member-001"


def valid_definition() -> dict:
    """最小合法定义（长文写作两节点：manual 前置 + auto 后置）。"""

    return {
        "schema_version": 1,
        "workflow": {"key": "longform-e2e", "name": "长文写作", "description": "大纲 → 成文"},
        "stages": [
            {"id": "outline", "title": "大纲"},
            {"id": "draft", "title": "成文"},
        ],
        "role_bindings": [
            {"id": "writer", "role_name": "写作", "capability_requirements": ["files.write"]},
        ],
        "nodes": [
            {
                "id": "collect_brief", "stage_id": "outline", "title": "整理写作简报",
                "goal": "确定主题与受众", "depends_on": [], "mode": "manual",
            },
            {
                "id": "write_outline", "stage_id": "outline", "title": "写大纲",
                "goal": "产出结构化大纲", "depends_on": ["collect_brief"], "mode": "auto",
                "role_binding": "writer",
                "prompt": {"task_template": "围绕 {{input.topic}} 写大纲"},
                "outputs": [{"name": "outline_doc", "artifact_type": "document", "path": "outputs/outline.md"}],
                "gate_policy": "outline-gate",
                "budget": {"max_attempts": 2},
            },
        ],
        "gate_policies": [
            {"id": "outline-gate", "spec": ["file:outputs/outline.md non-empty"], "on_block": "escalate_human"},
        ],
    }


class WorkflowPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.client.close()  # Windows：先关连接再清临时目录
        self.store.close()
        self.temp_dir.cleanup()

    # ---- 创建与校验 -------------------------------------------------------

    def test_create_valid_workflow(self) -> None:
        response = self.client.post("/api/workflows", json={"definition": valid_definition()})
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["key"], "longform-e2e")
        self.assertIsNotNone(body["current_version_id"])  # 创建即发布 v1
        self.assertIsNotNone(body.get("definition"))

    def test_validation_errors_listed_not_silent(self) -> None:
        """互斥校验 / 引用完整性 / 环 / 预算形状 / 非法 gate——错误逐条列出（§4 规则 8）。"""

        definition = valid_definition()
        definition["workflow"]["key"] = "broken-pack"
        definition["nodes"][0]["role_binding"] = "writer"  # manual 却带 binding（互斥违反）
        definition["nodes"][1]["mode"] = "auto"
        definition["nodes"][1].pop("role_binding")  # auto 却没有 binding（互斥违反）
        definition["nodes"][1]["depends_on"] = ["write_outline"]  # 自引用成环
        definition["nodes"][1]["budget"] = {"max_tokens": -5}  # 预算形状非法
        definition["gate_policies"][0]["spec"] = [{"xor": []}]  # 非法组合键
        response = self.client.post("/api/workflows", json={"definition": definition})
        self.assertEqual(response.status_code, 422, response.text)
        errors = response.json()["detail"]["errors"]
        joined = " ".join(errors)
        self.assertIn("manual 节点必须没有 role_binding", joined)
        self.assertIn("auto/hybrid 节点必须有 role_binding", joined)
        self.assertIn("引用了自己", joined)
        self.assertIn("budget", joined)
        self.assertIn("gate", joined)

    def test_two_node_cycle_detected(self) -> None:
        """§4 规则 4：真环（两节点互依）由 Kahn 判定报"环"。"""

        definition = valid_definition()
        definition["workflow"]["key"] = "cycle-pack"
        definition["nodes"][1]["depends_on"] = ["collect_brief"]
        definition["nodes"][0]["mode"] = "auto"
        definition["nodes"][0]["role_binding"] = "writer"
        definition["nodes"][0]["depends_on"] = ["write_outline"]
        errors = workflow_service.validate_definition(definition)
        self.assertTrue(any("环" in item for item in errors), errors)

    def test_gate_spec_undecidable_is_acceptable_but_garbage_is_not(self) -> None:
        """§4 规则 5：干跑 UNVERIFIED 可接受（运行期才知道文件在不在）；
        连解析都过不了的字符串才是模板非法。"""

        definition = valid_definition()
        definition["workflow"]["key"] = "gate-dryrun"
        definition["gate_policies"][0]["spec"] = ["file:outputs/never-written.md non-empty"]
        ok = self.client.post("/api/workflows", json={"definition": definition})
        self.assertEqual(ok.status_code, 201, ok.text)

        definition["workflow"]["key"] = "gate-bad-shape"
        definition["gate_policies"][0]["spec"] = [{"all": "not-a-list"}]
        bad = self.client.post("/api/workflows", json={"definition": definition})
        self.assertEqual(bad.status_code, 422)
        self.assertIn("gate", " ".join(bad.json()["detail"]["errors"]))

    # ---- 版本不可变 -------------------------------------------------------

    def test_version_append_only_and_old_definition_untouched(self) -> None:
        created = self.client.post("/api/workflows", json={"definition": valid_definition()}).json()
        workflow_id = created["id"]
        changed = valid_definition()
        changed["workflow"]["name"] = "长文写作 v2"
        changed["workflow"]["key"] = "longform-e2e"  # 同 key 追加版本
        second = self.client.post(f"/api/workflows/{workflow_id}/versions", json={"definition": changed})
        self.assertEqual(second.status_code, 201, second.text)
        body = second.json()
        self.assertEqual(body["name"], "长文写作 v2")

        # 旧版本定义原样保留（追加式，永不 UPDATE）
        rows = self.store.db.execute(
            "SELECT version, definition FROM workflow_versions WHERE workflow_id = ? ORDER BY version",
            (workflow_id,),
        ).fetchall()
        self.assertEqual([row["version"] for row in rows], [1, 2])
        self.assertEqual(json.loads(rows[0]["definition"])["workflow"]["name"], "长文写作")

    def test_key_conflict_rejected(self) -> None:
        self.client.post("/api/workflows", json={"definition": valid_definition()})
        response = self.client.post("/api/workflows", json={"definition": valid_definition()})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"]["code"], "workflow_key_exists")

    # ---- 组织隔离 ---------------------------------------------------------

    def test_org_isolation(self) -> None:
        """跨组织不可见（与 RLS 同口径；dev SQLite 由查询层过滤）。"""

        created = self.client.post("/api/workflows", json={"definition": valid_definition()}).json()
        with self.assertRaisesRegex(workflow_service.WorkflowError, "workflow_not_found"):
            workflow_service.get_workflow(self.store, UUID(created["id"]), "org-somebody-else")

    # ---- 运行物化 ---------------------------------------------------------

    def test_start_run_materializes_task_skeleton(self) -> None:
        created = self.client.post("/api/workflows", json={"definition": valid_definition()}).json()
        workflow_id = created["id"]
        response = self.client.post(
            f"/api/projects/{self.project.id}/workflow-runs",
            json={"workflow_id": workflow_id, "inputs": {"topic": "海冰厚度预测"}},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(len(body["tasks"]), 2)
        self.assertIn("骨架", body["note"])  # 如实标注：模板 ≠ 结果

        by_node = {item["node_id"]: item for item in body["tasks"]}
        # 依赖已接线：write_outline 挂在 collect_brief 的任务上
        outline_task = self.store.get_task(UUID(by_node["write_outline"]["task_id"]))
        self.assertEqual(
            [str(item) for item in outline_task.dependency_task_ids], [by_node["collect_brief"]["task_id"]]
        )
        # manual 节点没有能力要求（路由到成员/公共队列）；auto 节点带 role_binding 的能力
        brief_task = self.store.get_task(UUID(by_node["collect_brief"]["task_id"]))
        self.assertEqual(brief_task.required_capabilities, [])
        self.assertEqual(outline_task.required_capabilities, ["files.write"])
        # 预算进任务（TaskBudget 语义）
        self.assertEqual(outline_task.budget.max_attempts, 2)  # TaskBudget 模型
        # 每个节点都是真任务：project.task.created 事件已发（目录咽喉自动过）
        rows = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM events WHERE project_id = ? AND event_type = 'project.task.created'",
            (str(self.project.id),),
        ).fetchone()["c"]
        self.assertEqual(rows, 2)

    def test_start_run_binds_frozen_version(self) -> None:
        created = self.client.post("/api/workflows", json={"definition": valid_definition()}).json()
        changed = valid_definition()
        changed["workflow"]["name"] = "v2 改名"
        self.client.post(f"/api/workflows/{created['id']}/versions", json={"definition": changed})
        response = self.client.post(
            f"/api/projects/{self.project.id}/workflow-runs",
            json={"workflow_id": created["id"], "version_id": None, "inputs": {}},
        )
        body = response.json()
        runs = self.client.get(f"/api/projects/{self.project.id}/workflow-runs").json()
        self.assertEqual(runs[0]["workflow_version_id"], body["workflow_version_id"])

    def test_start_run_unknown_workflow_404(self) -> None:
        response = self.client.post(
            f"/api/projects/{self.project.id}/workflow-runs", json={"workflow_id": str(uuid4()), "inputs": {}}
        )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
