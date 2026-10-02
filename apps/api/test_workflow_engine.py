"""工作流推进器（W3.2）与门禁运行器（W3.3）契约测试。

口径（实施计划第五期；schema §1 规则 4 + §5 事件映射）：
- 门禁在节点交付待审时评估：verdict holds → project.gate.passed；不 holds → blocked，
  on_block=escalate_human 再发 escalated；**门禁不替代人工审核**（红线原样保留）；
- 文件探针以成果物库为平台侧文件现实（agent 产出只有成为 artifact 平台才见得到），
  平台看不到的文件如实 UNVERIFIED；tests_passed v1 一律 UNVERIFIED（显式口径）；
- 有界重试：on_fail=retry 的失败节点放回 READY 并发 project.task.retried（带尝试序号），
  预算 max_attempts 是硬顶——到顶保持 FAILED，升级人工看得到；
- 账本：确定性信号记账（本轮状态迁移=进步），stall 达标 → 运行 STALLED；
  全部节点 APPROVED → COMPLETED；完成后再推进 → 409。
测试里的任务状态迁移用条件 UPDATE 直接播种（单测已有专项覆盖迁移合法性）。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import main, workflow_service
from app.contracts import ArtifactCreate, ReviewCreate, ReviewerKind
from app.store import Store

MEMBER = "member-001"


def engine_definition() -> dict:
    """两节点：collect（manual，无门禁）→ solve（auto，带门禁与重试）。"""

    return {
        "schema_version": 1,
        "workflow": {"key": "engine-pack", "name": "推进器验收包"},
        "stages": [{"id": "facts", "title": "事实"}, {"id": "solve", "title": "求解"}],
        "role_bindings": [{"id": "solver", "role_name": "求解", "capability_requirements": ["files.write"]}],
        "nodes": [
            {"id": "collect", "stage_id": "facts", "title": "整理事实", "goal": "整理", "depends_on": [], "mode": "manual"},
            {
                "id": "solve", "stage_id": "solve", "title": "求解", "goal": "求解并产出结果表",
                "depends_on": ["collect"], "mode": "auto", "role_binding": "solver",
                "outputs": [{"name": "result_table", "artifact_type": "result_table", "path": "outputs/result.csv"}],
                "gate_policy": "solve-gate",
                "budget": {"max_attempts": 2},
                "on_fail": "retry",
                "retry_policy": {"backoff_seconds": 0},
            },
        ],
        "gate_policies": [
            {"id": "solve-gate", "spec": ["artifact:result_table approved"], "on_block": "escalate_human"},
        ],
    }


class WorkflowEngineTests(unittest.TestCase):
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

    # ---- helpers ---------------------------------------------------------

    def start_run(self) -> dict:
        created = self.client.post("/api/workflows", json={"definition": engine_definition()}).json()
        response = self.client.post(
            f"/api/projects/{self.project.id}/workflow-runs", json={"workflow_id": created["id"], "inputs": {}}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def node_task(self, run: dict, node_id: str) -> UUID:
        return UUID(next(item["task_id"] for item in run["tasks"] if item["node_id"] == node_id))

    def set_status(self, task_id: UUID, status: str) -> None:
        self.store.db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, str(task_id)))
        self.store.db.commit()

    def gate_events(self) -> list[str]:
        rows = self.store.db.execute(
            "SELECT event_type FROM events WHERE project_id = ? AND event_type LIKE 'project.gate%' ORDER BY sequence",
            (str(self.project.id),),
        ).fetchall()
        return [row["event_type"] for row in rows]

    def advance(self, run: dict) -> dict:
        response = self.client.post(f"/api/projects/{self.project.id}/workflow-runs/{run['run_id']}/advance")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    # ---- 门禁（W3.3）------------------------------------------------------

    def test_gate_blocked_and_escalated_when_delivered_without_approval(self) -> None:
        """交付待审 + 门禁不 holds → evaluated + blocked + escalated（on_block=escalate_human）。"""

        run = self.start_run()
        solve_task = self.node_task(run, "solve")
        self.set_status(solve_task, "WAITING_REVIEW")
        body = self.advance(run)
        # artifact:… approved 平台可判定：查过、没有 → NOT_HOLDS（不是 UNVERIFIED）
        self.assertEqual(body["gates"][0]["verdict"], "NOT_HOLDS")
        self.assertEqual(self.gate_events(), ["project.gate.evaluated", "project.gate.blocked", "project.gate.escalated"])

    def test_gate_passes_when_artifact_approved(self) -> None:
        """产物获批后：artifact:result_table approved → holds → project.gate.passed。"""

        run = self.start_run()
        solve_task = self.node_task(run, "solve")
        self.set_status(solve_task, "WAITING_REVIEW")
        artifact = self.store.create_artifact(
            self.project.id, ArtifactCreate(name="result.csv", artifact_type="result_table", task_id=solve_task), created_by=MEMBER
        )
        self.store.create_review(
            self.project.id,
            ReviewCreate(target_type="artifact", target_id=artifact.id, verdict="APPROVED", summary="ok", reviewer=MEMBER, reviewer_kind=ReviewerKind.MEMBER, idempotency_key="gate-review-1"),
        )
        body = self.advance(run)
        self.assertEqual(body["gates"][0]["verdict"], "HOLDS", json.dumps(body["gates"], ensure_ascii=False))
        self.assertIn("project.gate.passed", self.gate_events())

    # ---- 有界重试 ---------------------------------------------------------

    def test_failed_node_retried_within_budget_then_stops(self) -> None:
        """on_fail=retry：失败 → 放回 READY 并发 retried；预算 max_attempts=2 → 第二次不再重试。"""

        run = self.start_run()
        solve_task = self.node_task(run, "solve")
        self.set_status(self.node_task(run, "collect"), "APPROVED")
        self.set_status(solve_task, "FAILED")

        first = self.advance(run)
        self.assertEqual(first["retried"], ["solve"])
        events = self.store.db.execute(
            "SELECT payload FROM events WHERE project_id = ? AND event_type = 'project.task.retried'", (str(self.project.id),)
        ).fetchall()
        self.assertEqual(json.loads(events[0]["payload"])["attempt"], 2)
        observed = self.store.get_task(solve_task).status
        self.assertEqual(str(observed.value if hasattr(observed, "value") else observed), "READY")

        self.set_status(solve_task, "FAILED")  # 再次失败
        second = self.advance(run)
        self.assertEqual(second["retried"], [])  # attempt 2 == max_attempts 2 → 停

    # ---- 账本与完成 -------------------------------------------------------

    def test_advance_without_change_stalls_eventually(self) -> None:
        """无状态变化的多轮推进：签名不变 → stall 累计 → STALLED（防空转）。"""

        run = self.start_run()
        statuses = []
        body: dict = {}
        for _ in range(6):
            body = self.advance(run)
            statuses.append(body["status"])
            if body["status"] == "STALLED":
                break
        self.assertEqual(body["status"], "STALLED", statuses)
        self.assertTrue(body["needs_replan"])

    def test_all_nodes_approved_completes_run(self) -> None:
        run = self.start_run()
        for node in ("collect", "solve"):
            self.set_status(self.node_task(run, node), "APPROVED")
        body = self.advance(run)
        self.assertEqual(body["status"], "COMPLETED")

    def test_advance_unknown_or_finished_run(self) -> None:
        response = self.client.post(f"/api/projects/{self.project.id}/workflow-runs/{uuid4()}/advance")
        self.assertEqual(response.status_code, 404)
        run = self.start_run()
        for node in ("collect", "solve"):
            self.set_status(self.node_task(run, node), "APPROVED")
        self.advance(run)
        again = self.client.post(f"/api/projects/{self.project.id}/workflow-runs/{run['run_id']}/advance")
        self.assertEqual(again.status_code, 409)  # COMPLETED 后不可再推进


if __name__ == "__main__":
    unittest.main()
