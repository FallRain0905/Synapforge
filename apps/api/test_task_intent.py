"""意图对象的两个缺项（AIP-1d，见 docs/AIP_1_PLAN.md §6）：预算 与 显式证据要求。

覆盖：
  * 默认空值 = 零行为变化（不设预算不设证据要求时，领取与门禁与以前完全一样）；
  * `max_attempts` **强制**：用尽后点名领取报 `task_budget_exhausted`、轮询跳过、一次性事件只写一条；
  * `max_seconds` **强制**：租约被压到预算以内，续租也不能越过上限；
  * `max_tokens` **只记录**：不影响领取（平台没有 token 计量）；
  * 证据要求：读时算缺口；**批准时**才校验，缺证据 → 门禁 FAILED + 拒绝批准（不自动批准）；
  * HTTP：`PATCH /api/tasks/{id}` 的"出现才生效"语义（含清除）、证据类型非法 422、详情端点结构。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import main
from app.contracts import (
    AgentRegister,
    EvidenceCreate,
    ProjectCreate,
    ReviewCreate,
    ReviewerKind,
    SessionCreate,
    TaskClaimRequest,
    TaskCreate,
    TaskUpdateRequest,
)
from app.store import DEV_ORG_ID, Store


class IntentFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.store.close)
        self.lead = self.store.get_member("member-001")
        self.project = self.store.create_project(
            ProjectCreate(name="意图对象测试", competition_pack="cumcm-2026", problem_code="A", created_by=self.lead.id)
        )
        self.store.register_agent(AgentRegister(agent_id="agent-intent", display_name="意图执行体", owner_member_id=self.lead.id))
        self.store.db.execute(
            "INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES (?, ?, ?, ?, ?)",
            ("agent-intent", str(self.project.id), json.dumps(["task.claim"]), self.lead.id, "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.commit()

    def make_task(self, title: str, **overrides):
        return self.store.create_task(self.project.id, TaskCreate(title=title, description="测试", **overrides))

    def claim(self, task_id, *, seconds: int = 600, key: str | None = None):
        return self.store.claim_task(
            task_id, TaskClaimRequest(agent_id="agent-intent", lease_seconds=seconds, idempotency_key=key or uuid4().hex)
        )


class BudgetEnforcementTests(IntentFixture):
    def test_default_empty_budget_changes_nothing(self) -> None:
        task = self.make_task("没有预算")
        self.assertIsNone(task.budget)
        claimed, lease = self.claim(task.id, seconds=900)
        self.assertEqual(claimed.status, "CLAIMED")
        # 没有 max_seconds 时租约原样（900 秒），不被悄悄改小
        self.assertGreaterEqual((lease.expires_at - lease.issued_at).total_seconds(), 890)

    def test_max_seconds_caps_lease_and_heartbeat(self) -> None:
        task = self.make_task("限时 60 秒", budget={"max_seconds": 60})
        _, lease = self.claim(task.id, seconds=3600)
        self.assertLessEqual((lease.expires_at - lease.issued_at).total_seconds(), 61)
        # 续租也不能越过上限
        extended = self.store.heartbeat_lease(lease.lease_token, "agent-intent", 3600)
        ceiling = lease.issued_at + timedelta(seconds=60)
        self.assertLessEqual(extended.expires_at, ceiling + timedelta(seconds=1))

    def test_max_attempts_blocks_claim_and_records_one_event(self) -> None:
        task = self.make_task("只允许一次", budget={"max_attempts": 1})
        self.claim(task.id)
        # 释放租约后回到 READY，但预算已用尽：点名领取被拒
        self.store.db.execute("UPDATE task_leases SET status = 'RELEASED' WHERE task_id = ?", (str(task.id),))
        self.store.db.execute("UPDATE tasks SET status = 'READY' WHERE id = ?", (str(task.id),))
        self.store.db.commit()
        with self.assertRaises(ValueError) as error:
            self.claim(task.id)
        self.assertEqual(str(error.exception), "task_budget_exhausted")
        events = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exhausted' AND idempotency_key = ?",
            (f"budget-exhausted:{task.id}",),
        ).fetchone()
        self.assertEqual(events["c"], 1)
        # 再试一次不会重复写事件（幂等）
        with self.assertRaises(ValueError):
            self.claim(task.id)
        again = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exhausted' AND idempotency_key = ?",
            (f"budget-exhausted:{task.id}",),
        ).fetchone()
        self.assertEqual(again["c"], 1)

    def test_polling_skips_budget_exhausted_task_instead_of_failing(self) -> None:
        first = self.make_task("用尽预算的任务", budget={"max_attempts": 1})
        self.claim(first.id)
        self.store.db.execute("UPDATE task_leases SET status = 'RELEASED' WHERE task_id = ?", (str(first.id),))
        self.store.db.execute("UPDATE tasks SET status = 'READY' WHERE id = ?", (str(first.id),))
        self.store.db.commit()
        second = self.make_task("还有预算的任务")
        claimed = self.store.claim_next_task(
            TaskClaimRequest(agent_id="agent-intent", lease_seconds=600, idempotency_key=uuid4().hex),
            project_id=self.project.id,
        )
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed[0].id, second.id)

    def test_max_tokens_is_recorded_only(self) -> None:
        task = self.make_task("只记 token", budget={"max_tokens": 1000})
        claimed, _ = self.claim(task.id)
        self.assertEqual(claimed.status, "CLAIMED")
        state = self.store._task_budget_state(self.store.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task.id),)).fetchone())
        self.assertEqual(state["max_tokens"], 1000)
        self.assertFalse(state["exhausted"])


class EvidenceRequirementTests(IntentFixture):
    def approve(self, task_id, *, verdict: str = "APPROVED"):
        task = self.store.get_task(task_id)
        return self.store.create_review(
            UUID(str(task.project_id)),
            ReviewCreate(
                target_type="task",
                target_id=task_id,
                reviewer=self.lead.id,
                reviewer_kind=ReviewerKind.MEMBER,
                verdict=verdict,
                summary="复核",
            ),
        )

    def test_empty_requirements_do_not_affect_approval(self) -> None:
        task = self.make_task("没证据要求")
        self.store.db.execute("UPDATE tasks SET status = 'WAITING_REVIEW' WHERE id = ?", (str(task.id),))
        self.store.db.commit()
        self.approve(task.id)
        self.assertEqual(self.store.get_task(task.id).status, "APPROVED")

    def test_missing_evidence_blocks_approval_and_marks_gate(self) -> None:
        task = self.make_task("要 run 证据", evidence_requirements=[{"evidence_type": "run", "min_count": 1}])
        self.store.db.execute("UPDATE tasks SET status = 'WAITING_REVIEW' WHERE id = ?", (str(task.id),))
        self.store.db.commit()
        gaps = self.store.task_evidence_gaps(task.id)
        self.assertEqual(gaps[0]["missing"], 1)
        with self.assertRaises(ValueError) as error:
            self.approve(task.id)
        self.assertIn("task_evidence_requirements_unmet", str(error.exception))
        # 任务没被批准，且门禁留下了可审计的 blocking finding
        self.assertEqual(self.store.get_task(task.id).status, "WAITING_REVIEW")
        gate = self.store.db.execute(
            "SELECT status, blocking_findings FROM gates WHERE project_id = ? AND target_type = 'task' AND target_id = ?",
            (str(task.project_id), str(task.id)),
        ).fetchone()
        self.assertEqual(gate["status"], "FAILED")
        self.assertIn("task:evidence_requirements", gate["blocking_findings"])

    def test_evidence_satisfied_allows_approval(self) -> None:
        task = self.make_task("要一个外部来源证据", evidence_requirements=[{"evidence_type": "external_source", "min_count": 1}])
        self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="外部来源已核对", evidence_type="external_source", source_ref="https://example.com/ref", created_by=self.lead.id),
        )
        self.store.db.execute("UPDATE tasks SET status = 'WAITING_REVIEW' WHERE id = ?", (str(task.id),))
        self.store.db.commit()
        self.assertEqual(self.store.task_evidence_gaps(task.id)[0]["missing"], 0)
        self.approve(task.id)
        self.assertEqual(self.store.get_task(task.id).status, "APPROVED")

    def test_invalid_evidence_type_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self.make_task("坏的证据类型", evidence_requirements=[{"evidence_type": "video", "min_count": 1}])


class IntentHttpTests(IntentFixture):
    def setUp(self) -> None:
        super().setUp()
        self.previous_store = main.store
        main.store = self.store
        self.addCleanup(self._restore)
        self.client = TestClient(main.app)
        self.headers = {"Authorization": f"Bearer {self.store.create_session(SessionCreate(member_id=self.lead.id)).token}"}

    def _restore(self) -> None:
        main.store = self.previous_store
        self.client.close()

    def test_patch_budget_and_evidence_are_opt_in(self) -> None:
        task = self.make_task("打补丁")
        # 只改预算 → 证据要求保持空
        response = self.client.patch(
            f"/api/tasks/{task.id}", json={"budget": {"max_attempts": 2}}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["budget"]["max_attempts"], 2)
        self.assertIsNone(body["budget"]["max_seconds"])
        self.assertEqual(body["evidence_requirements"], [])
        # 只改证据要求 → 预算保持上一次的值
        response = self.client.patch(
            f"/api/tasks/{task.id}",
            json={"evidence_requirements": [{"evidence_type": "run", "min_count": 2, "note": "必须有运行记录"}]},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["budget"]["max_attempts"], 2)
        self.assertIsNone(body["budget"]["max_seconds"])
        self.assertEqual(body["evidence_requirements"][0]["min_count"], 2)
        # 显式 null / 空列表 = 清除
        response = self.client.patch(f"/api/tasks/{task.id}", json={"budget": None, "evidence_requirements": []}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()["budget"])
        self.assertEqual(response.json()["evidence_requirements"], [])

    def test_patch_invalid_evidence_type_returns_422(self) -> None:
        task = self.make_task("坏证据")
        response = self.client.patch(
            f"/api/tasks/{task.id}", json={"evidence_requirements": [{"evidence_type": "video"}]}, headers=self.headers
        )
        self.assertEqual(response.status_code, 422)

    def test_task_detail_exposes_budget_state_and_gaps(self) -> None:
        task = self.make_task(
            "详情",
            budget={"max_attempts": 3, "max_seconds": 120},
            evidence_requirements=[{"evidence_type": "run", "min_count": 1, "note": "运行记录"}],
        )
        response = self.client.get(f"/api/tasks/{task.id}", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["task"]["id"], str(task.id))
        self.assertEqual(body["budget_state"]["max_attempts"], 3)
        self.assertEqual(body["budget_state"]["attempts"], 0)
        self.assertFalse(body["budget_state"]["exhausted"])
        self.assertEqual(body["evidence_gaps"][0]["evidence_type"], "run")
        self.assertEqual(body["evidence_gaps"][0]["missing"], 1)
        missing = self.client.get(f"/api/tasks/{uuid4()}", headers=self.headers)
        self.assertEqual(missing.status_code, 404)


if __name__ == "__main__":
    unittest.main()