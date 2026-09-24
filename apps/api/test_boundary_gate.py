"""信息边界审计 Gate 契约测试：pack 规则 × 运行事实 → Review/Gate。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.contracts import (
    AgentRegister,
    ArtifactCreate,
    BoundaryGateRequest,
    RunComplete,
    RunCreate,
    TaskCreate,
)
from app.store import Store


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


class BoundaryGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]
        self.store.register_agent(
            AgentRegister(agent_id="agent-001", display_name="边界 Agent", owner_member_id="member-001")
        )
        self.store.grant_agent_project(
            main.AgentProjectGrant(
                project_id=self.project.id, agent_id="agent-001", granted_by="member-001", capabilities=["run.create"]
            )
        )
        self.task = self.store.create_task(self.project.id, TaskCreate(title="边界审计任务"))

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def _run(self, *, policy: dict | None = None, observed: list[str] | None = None):
        run = self.store.create_run(
            self.project.id,
            RunCreate(
                task_id=self.task.id,
                agent_id="agent-001",
                summary="边界审计运行",
                idempotency_key=f"boundary-run-{len(self.store.list_runs(self.project.id)) + 1:04d}",
            ),
        )
        # 直接写入运行事实：数据访问策略、观察到的输入与边界审计结果。
        self.store.db.execute(
            "UPDATE runs SET data_access_policy = ?, observed_input_files = ? WHERE id = ?",
            (
                __import__("json").dumps(policy or {"observation_mode": "system"}, ensure_ascii=False),
                __import__("json").dumps(observed if observed is not None else ["input/data.csv"]),
                str(run.id),
            ),
        )
        self.store.db.commit()
        return self.store.get_run(run.id)

    def _set_boundary(self, run_id: UUID, boundary: dict) -> None:
        self.store.db.execute(
            "UPDATE runs SET information_boundary = ? WHERE id = ?",
            (__import__("json").dumps(boundary, ensure_ascii=False), str(run_id)),
        )
        self.store.db.commit()

    # ---- 只读审计 ---------------------------------------------------------

    def test_clean_run_passes_without_creating_review(self) -> None:
        self._run()
        audit = main.get_project_boundary_audit(self.project.id)
        self.assertTrue(audit["allowed"])
        self.assertIsNone(audit["verdict"])
        self.assertEqual(audit["run_count"], 1)
        self.assertEqual([item["code"] for item in audit["findings"]], [])

        result = main.create_project_boundary_gate(
            self.project.id, BoundaryGateRequest(task_id=self.task.id), make_request()
        )
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "boundary_audit_clean_requires_human_approval")

    def test_no_run_records_minor_finding(self) -> None:
        audit = main.get_project_boundary_audit(self.project.id)
        self.assertEqual([item["code"] for item in audit["findings"]], ["boundary_audit_no_run"])
        self.assertTrue(audit["allowed"])

    # ---- 违规 → BLOCKED ---------------------------------------------------

    def test_boundary_violation_blocks_and_creates_gate(self) -> None:
        run = self._run()
        self._set_boundary(
            run.id,
            {"allowed": False, "violations": [{"severity": "fatal", "code": "observed_input_outside_workspace", "message": "工作区外读取"}]},
        )
        audit = main.get_project_boundary_audit(self.project.id, task_id=self.task.id)
        self.assertEqual(audit["verdict"], "BLOCKED")
        codes = {item["code"] for item in audit["findings"]}
        self.assertIn("information_boundary_violation", codes)
        self.assertIn("observed_input_outside_workspace", codes)

        result = main.create_project_boundary_gate(
            self.project.id,
            BoundaryGateRequest(task_id=self.task.id, idempotency_key="boundary-gate-0001"),
            make_request(),
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["verdict"], "BLOCKED")
        self.assertEqual(result["target_type"], "task")
        self.assertEqual(result["gate_status"], "BLOCKED")

        review = next(item for item in self.store.list_reviews(self.project.id) if str(item.id) == result["review_id"])
        self.assertEqual(str(review.verdict), "BLOCKED")
        self.assertEqual(str(review.reviewer_kind), "agent")

    # ---- 观察缺失/模式不符 → NEEDS_REVISION -------------------------------

    def test_missing_system_observation_needs_revision(self) -> None:
        self._run(observed=[])
        audit = main.get_project_boundary_audit(self.project.id)
        self.assertEqual(audit["verdict"], "NEEDS_REVISION")
        self.assertIn("input_observation_not_captured", {item["code"] for item in audit["findings"]})
        result = main.create_project_boundary_gate(
            self.project.id, BoundaryGateRequest(idempotency_key="boundary-gate-0002"), make_request()
        )
        self.assertTrue(result["created"])
        self.assertEqual(result["verdict"], "NEEDS_REVISION")
        self.assertEqual(result["gate_status"], "FAILED")

    def test_observation_mode_mismatch_needs_revision(self) -> None:
        self._run(policy={"observation_mode": "declared"})
        audit = main.get_project_boundary_audit(self.project.id)
        self.assertIn("observation_mode_mismatch", {item["code"] for item in audit["findings"]})

    def test_future_data_policy_conflict_blocks(self) -> None:
        self._run(policy={"observation_mode": "system", "allow_future_data": True})
        audit = main.get_project_boundary_audit(self.project.id)
        self.assertEqual(audit["verdict"], "BLOCKED")
        self.assertIn("future_data_allowed", {item["code"] for item in audit["findings"]})

    # ---- 任务级规则冲突 ---------------------------------------------------

    def test_task_allow_future_data_conflicts_with_deny_rule(self) -> None:
        task = self.store.create_task(
            self.project.id, TaskCreate(title="允许未来数据的任务", allow_future_data=True)
        )
        audit = main.get_project_boundary_audit(self.project.id, task_id=task.id)
        self.assertEqual(audit["verdict"], "BLOCKED")
        self.assertIn("task_future_data_allowed", {item["code"] for item in audit["findings"]})

    # ---- 幂等与目标选择 ---------------------------------------------------

    def test_gate_creation_is_idempotent(self) -> None:
        self._run(observed=[])
        first = main.create_project_boundary_gate(
            self.project.id, BoundaryGateRequest(idempotency_key="boundary-idem-0001"), make_request()
        )
        second = main.create_project_boundary_gate(
            self.project.id, BoundaryGateRequest(idempotency_key="boundary-idem-0001"), make_request()
        )
        self.assertEqual(first["review_id"], second["review_id"])

    def test_unknown_task_returns_404(self) -> None:
        from uuid import uuid4

        with self.assertRaises(HTTPException) as caught:
            main.get_project_boundary_audit(self.project.id, task_id=uuid4())
        self.assertEqual(caught.exception.status_code, 404)

    def test_gate_target_missing_returns_404(self) -> None:
        # 有违规但没有可挂载目标：运行未绑定任务、没有产出成果物、项目也没有结果表槽位。
        empty_project = self.store.create_project(
            main.ProjectCreate(name="无目标项目", competition_pack="cumcm-2026", problem_code="C", description="x")
        )
        self.store.grant_agent_project(
            main.AgentProjectGrant(
                project_id=empty_project.id, agent_id="agent-001", granted_by="member-001", capabilities=["run.create"]
            )
        )
        run = self.store.create_run(
            empty_project.id,
            RunCreate(task_id=None, agent_id="agent-001", summary="无目标运行", idempotency_key="boundary-empty-run1"),
        )
        self._set_boundary(run.id, {"allowed": False, "violations": [{"severity": "fatal", "code": "future_data", "message": "未来数据"}]})
        audit = main.get_project_boundary_audit(empty_project.id)
        self.assertEqual(audit["verdict"], "BLOCKED")
        with self.assertRaises(HTTPException) as caught:
            main.create_project_boundary_gate(
                empty_project.id, BoundaryGateRequest(idempotency_key="boundary-empty-01"), make_request()
            )
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("boundary_gate_target_missing", str(caught.exception.detail))


if __name__ == "__main__":
    unittest.main()