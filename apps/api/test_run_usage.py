"""执行用量回报与预算判定（COST-1，接 AIP-1d 的"token 预算只记录"）。

覆盖：
  * 用量落库：total 由 in+out 推导；耗时优先执行体自报、缺省用平台观测（started_at/completed_at）；
  * 不报用量 = 没数据（`tokens_used=0` 且 `usage_reported_runs=0`），不是"花了 0"；
  * 超 token 预算：完成 Run 时写**一次性**事件；批准时被门禁拦下并留 blocking finding；
  * 耗时超上限有 10%/30 秒余量，不把进程收尾判成超时；
  * 没预算的任务/没回报用量的 Run：一切照旧（零行为变化）；
  * HTTP：`POST /api/runs/{id}/complete` 带 usage → 任务详情能看到累计用量；负数 422。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from device_test_support import registration_request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import main
from app.contracts import (
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    ProjectCreate,
    ReviewCreate,
    ReviewerKind,
    RunComplete,
    RunCreate,
    RunUsage,
    SessionCreate,
    TaskCreate,
)
from app.store import DEV_ORG_ID, Store


class UsageFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.addCleanup(self.store.close)
        self.lead = self.store.get_member("member-001")
        self.project = self.store.create_project(
            ProjectCreate(name="用量测试", competition_pack="cumcm-2026", problem_code="A", created_by=self.lead.id)
        )
        self.store.register_agent(AgentRegister(agent_id="agent-cost", display_name="用量执行体", owner_member_id=self.lead.id))
        self.store.db.execute(
            "INSERT OR REPLACE INTO agent_project_grants (agent_id, project_id, capabilities, granted_by, created_at) VALUES (?, ?, ?, ?, ?)",
            ("agent-cost", str(self.project.id), json.dumps(["task.claim", "run.create", "run.complete"]), self.lead.id, "2026-01-01T00:00:00+00:00"),
        )
        self.store.db.commit()

    def make_task(self, title: str, **overrides):
        return self.store.create_task(self.project.id, TaskCreate(title=title, description="测试", **overrides))

    def start_run(self, task_id: UUID | None = None):
        return self.store.create_run(
            self.project.id,
            RunCreate(agent_id="agent-cost", task_id=task_id, idempotency_key=uuid4().hex),
        )

    def finish(self, run_id, *, usage: RunUsage | None = None, success: bool = True):
        return self.store.complete_run(
            run_id,
            RunComplete(success=success, summary="完成", usage=usage, idempotency_key=uuid4().hex),
        )


class UsageStorageTests(UsageFixture):
    def test_total_is_derived_and_usage_is_stored(self) -> None:
        task = self.make_task("有回报")
        run = self.start_run(task.id)
        done = self.finish(run.id, usage=RunUsage(input_tokens=120, output_tokens=30, turns=2, source="codex-jsonl"))
        self.assertEqual(done.usage.total_tokens, 150)
        self.assertEqual(done.usage.input_tokens, 120)
        self.assertEqual(done.usage.turns, 2)
        self.assertEqual(done.usage.source, "codex-jsonl")
        # 耗时没自报 → 平台观测兜底（字段必须存在且 ≥ 0，且来源要标出来）
        self.assertIsNotNone(done.usage.seconds)
        self.assertEqual(done.usage.seconds_source, "platform")

    def test_reported_seconds_win_over_platform_observation(self) -> None:
        run = self.start_run()
        done = self.finish(run.id, usage=RunUsage(seconds=42.5, source="agent-reported"))
        self.assertEqual(done.usage.seconds, 42.5)
        self.assertEqual(done.usage.seconds_source, "agent")

    def test_no_usage_means_no_data_not_zero(self) -> None:
        task = self.make_task("没回报")
        run = self.start_run(task.id)
        done = self.finish(run.id)
        # 通用 CLI 执行体没有用量可报：token 字段留空，只有平台观测的耗时
        self.assertIsNone(done.usage.total_tokens)
        self.assertEqual(done.usage.source, "platform-observed")
        state = self.store._task_budget_state(self.store.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task.id),)).fetchone())
        self.assertEqual(state["tokens_used"], 0)
        self.assertEqual(state["usage_reported_runs"], 0)

    def test_task_usage_summary_aggregates_reported_runs(self) -> None:
        task = self.make_task("两次回报")
        for tokens in (100, 250):
            run = self.start_run(task.id)
            self.finish(run.id, usage=RunUsage(total_tokens=tokens))
        summary = self.store.task_usage_summary(task.id)
        self.assertEqual(summary["tokens_used"], 350)
        self.assertEqual(summary["usage_reported_runs"], 2)
        self.assertEqual(summary["runs_total"], 2)


class UsageBudgetTests(UsageFixture):
    def approve(self, task_id):
        return self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="task",
                target_id=task_id,
                reviewer=self.lead.id,
                reviewer_kind=ReviewerKind.MEMBER,
                verdict="APPROVED",
                summary="复核",
            ),
        )

    def test_over_token_budget_records_one_event_and_blocks_approval(self) -> None:
        task = self.make_task("超 token 预算", budget={"max_tokens": 100})
        run = self.start_run(task.id)
        self.finish(run.id, usage=RunUsage(total_tokens=250))
        events = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exceeded' AND idempotency_key = ?",
            (f"budget-exceeded:{run.id}",),
        ).fetchone()["c"]
        self.assertEqual(events, 1)
        # 再上报一次完成（幂等键不同、状态已终态会直接返回）不会重复写事件
        self.finish(run.id, usage=RunUsage(total_tokens=250))
        again = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exceeded'",
        ).fetchone()["c"]
        self.assertEqual(again, 1)
        # 批准被拦：门禁留痕
        self.store.db.execute("UPDATE tasks SET status = 'WAITING_REVIEW' WHERE id = ?", (str(task.id),))
        self.store.db.commit()
        with self.assertRaises(ValueError) as error:
            self.approve(task.id)
        self.assertIn("task_usage_budget_exceeded", str(error.exception))
        gate = self.store.db.execute(
            "SELECT status, blocking_findings FROM gates WHERE project_id = ? AND target_type = 'task' AND target_id = ?",
            (str(self.project.id), str(task.id)),
        ).fetchone()
        self.assertEqual(gate["status"], "FAILED")
        self.assertIn("task:usage_budget_exceeded", gate["blocking_findings"])
        self.assertEqual(self.store.get_task(task.id).status, "WAITING_REVIEW")

    def test_within_budget_approves_normally(self) -> None:
        task = self.make_task("没超", budget={"max_tokens": 1000})
        run = self.start_run(task.id)
        self.finish(run.id, usage=RunUsage(total_tokens=300))
        events = self.store.db.execute("SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exceeded'").fetchone()["c"]
        self.assertEqual(events, 0)
        self.store.db.execute("UPDATE tasks SET status = 'WAITING_REVIEW' WHERE id = ?", (str(task.id),))
        self.store.db.commit()
        self.approve(task.id)
        self.assertEqual(self.store.get_task(task.id).status, "APPROVED")

    def test_seconds_overrun_has_grace_and_flags_beyond_it(self) -> None:
        task = self.make_task("限时 60 秒", budget={"max_seconds": 60})
        # 65 秒：在 10% / 30 秒余量内，不算超
        run = self.start_run(task.id)
        self.finish(run.id, usage=RunUsage(seconds=65))
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exceeded'").fetchone()["c"], 0
        )
        # 300 秒：明确超限 → 留痕
        second = self.start_run(task.id)
        self.finish(second.id, usage=RunUsage(seconds=300))
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exceeded'").fetchone()["c"], 1
        )

    def test_no_budget_means_no_findings(self) -> None:
        task = self.make_task("没有预算")
        run = self.start_run(task.id)
        self.finish(run.id, usage=RunUsage(total_tokens=999_999, seconds=9999))
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) AS c FROM events WHERE event_type = 'task.budget_exceeded'").fetchone()["c"], 0
        )


class UsageHttpTests(UsageFixture):
    def setUp(self) -> None:
        super().setUp()
        # 完成 Run 是**执行体**的端点：鉴权走项目能力令牌（设备授权），不是人类会话令牌。
        # 这里用真实设备 + 真实授权串，走与 Agent 完全相同的路径，避免测出一个假的通过。
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), self.lead.id)
        credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                "agent-cost",
                "device-cost-001",
                device_name="用量工作站",
                capabilities=["task.claim", "run.create", "run.complete"],
            )
        )
        grant = self.store.create_device_project_grant(
            self.project.id, DeviceProjectGrantCreate(device_id=credential.device.device_id), self.lead.id
        )
        self.previous_store = main.store
        main.store = self.store
        self.addCleanup(self._restore)
        self.client = TestClient(main.app)
        self.headers = {"Authorization": f"Bearer {self.store.create_session(SessionCreate(member_id=self.lead.id)).token}"}
        self.agent_headers = {"X-Project-Capability-Token": grant.project_token, "X-Agent-Id": "agent-cost"}

    def _restore(self) -> None:
        main.store = self.previous_store
        self.client.close()

    def test_complete_with_usage_then_task_detail_shows_totals(self) -> None:
        task = self.make_task("HTTP 用量", budget={"max_tokens": 1000})
        run = self.start_run(task.id)
        response = self.client.post(
            f"/api/runs/{run.id}/complete",
            json={
                "success": True,
                "summary": "完成",
                "usage": {"input_tokens": 400, "output_tokens": 100, "turns": 1, "seconds": 12.5, "source": "codex-jsonl"},
                "idempotency_key": uuid4().hex,
            },
            headers=self.agent_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["usage"]["total_tokens"], 500)
        detail = self.client.get(f"/api/tasks/{task.id}", headers=self.headers)
        self.assertEqual(detail.status_code, 200, detail.text)
        body = detail.json()
        self.assertEqual(body["budget_state"]["tokens_used"], 500)
        self.assertEqual(body["budget_state"]["usage_reported_runs"], 1)
        self.assertEqual(body["task"]["budget"]["max_tokens"], 1000)

    def test_negative_tokens_rejected(self) -> None:
        run = self.start_run()
        response = self.client.post(
            f"/api/runs/{run.id}/complete",
            json={"success": True, "summary": "坏用量", "usage": {"input_tokens": -5}, "idempotency_key": uuid4().hex},
            headers=self.agent_headers,
        )
        self.assertEqual(response.status_code, 422)

    def test_complete_without_usage_still_works(self) -> None:
        run = self.start_run()
        response = self.client.post(
            f"/api/runs/{run.id}/complete",
            json={"success": True, "summary": "老客户端", "idempotency_key": uuid4().hex},
            headers=self.agent_headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["usage"]["source"], "platform-observed")
        self.assertIsNone(response.json()["usage"]["total_tokens"])


if __name__ == "__main__":
    unittest.main()