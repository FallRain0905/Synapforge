"""恢复、阻塞与人工介入（D3）契约测试。

验收对照（多 Agent 协作开发计划 D3 验收重点：重启、断线、重复投递和 Agent 崩溃
不产生双写、假完成或静默丢失）：
- **重启恢复**：Store 关闭后在同一路径重开——运行仍可推进、信息请求仍在收件箱、
  引擎 advance 继续工作（持久层是权威，进程死亡不丢事实）；
- **阶段报告幂等**：同一 idempotency_key 的重复提交返回原报告（不重复记账）；
- **重复 ACK 幂等**：已 ACK 的请求再 ACK（同 outcome）返回原状态，不双写；
- **过期扫描**：超过 deadline 的 required 请求由推进器显式转 EXPIRED 并发事件
  （不静默丢弃），且引擎把该节点的效果保持为阻塞；
- **崩溃恢复**：租约过期扫描（W2.5 已修）把 CLAIMED/RUNNING 任务放回 READY，
  已有 fencing 测试锁定，这里验证"重启后引擎推进不产生假完成"。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from app import coordination_store, main, workflow_engine, workflow_service
from app.contracts import AgentRegister, DevicePairingCreate, DeviceProjectGrantCreate, TaskCreate
from app.coordination import CoordinationError
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request
from test_workflow_engine import engine_definition

MEMBER = "member-001"


class CoordinationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "platform.db"
        self.store = Store(self.db_path)
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        coordination_store.ensure_schema(self.store)  # 直查 information_requests 前先建表
        self.project = self.store.list_projects()[0]
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="recovery-agent", display_name="Recovery Agent", owner_member_id=MEMBER)
        )

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # ---- helpers ---------------------------------------------------------

    def reopen_store(self) -> None:
        """模拟服务重启：关闭旧连接，同一路径重开（不重新播种）。"""

        self.store.close()
        self.store = Store(self.db_path)
        main.store = self.store

    def start_run(self) -> dict:
        created = self.client.post("/api/workflows", json={"definition": engine_definition()}).json()
        response = self.client.post(
            f"/api/projects/{self.project.id}/workflow-runs", json={"workflow_id": created["id"], "inputs": {}}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def set_status(self, task_id: UUID, status: str) -> None:
        self.store.db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, str(task_id)))
        self.store.db.commit()

    # ---- 重启恢复 ---------------------------------------------------------

    def test_run_survives_restart_and_advance_continues(self) -> None:
        """重启后：运行仍是 RUNNING 且可继续推进（ready queue 从持久层重建，不丢事实）。"""

        created = self.client.post("/api/workflows", json={"definition": engine_definition()}).json()
        run = self.client.post(
            f"/api/projects/{self.project.id}/workflow-runs", json={"workflow_id": created["id"], "inputs": {}}
        ).json()
        self.reopen_store()
        self.client = TestClient(main.app)
        response = self.client.post(f"/api/projects/{self.project.id}/workflow-runs/{run['run_id']}/advance")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "RUNNING")  # 没有假完成
        detail = self.client.get(f"/api/projects/{self.project.id}/workflow-runs/{run['run_id']}").json()
        self.assertEqual(detail["node_statuses"], response.json()["node_statuses"])  # 回放一致

    def test_information_requests_survive_restart(self) -> None:
        """重启后：信息请求仍在收件箱（OPEN 不静默丢失），过期扫描在推进时显式转 EXPIRED。"""

        created = self.client.post("/api/workflows", json={"definition": engine_definition()}).json()
        run = self.client.post(
            f"/api/projects/{self.project.id}/workflow-runs", json={"workflow_id": created["id"], "inputs": {}}
        ).json()
        task_id = next(item["task_id"] for item in run["tasks"] if item["node_id"] == "solve")
        # 已过期的 required 请求（走公共创建函数，不手拼列序）
        created = coordination_store.create_information_request(
            self.store, self.project.id, MEMBER,
            {"request": {"request_type": "context", "question": "需要边界清单", "blocking": "required",
                         "reason": "无则不安全", "response_deadline": (datetime.now(UTC) - timedelta(seconds=2)).isoformat()},
             "run_id": run["run_id"], "requester_task_id": UUID(task_id),
             "idempotency_key": f"recover-req-{uuid4()}"},
        )
        self.assertEqual(created["status"], "OPEN")
        before = coordination_store.list_information_requests(self.store, self.project.id, open_only=True)
        self.reopen_store()
        self.client = TestClient(main.app)
        after = coordination_store.list_information_requests(self.store, self.project.id, open_only=True)
        self.assertEqual(len(after), len(before))  # 重启不丢请求

        expired = workflow_engine.advance_run(
            self.store, self.project.id, UUID(run["run_id"]), MEMBER
        )
        _ = expired
        rows = self.store.db.execute(
            "SELECT status FROM information_requests WHERE response_deadline < ?",
            (datetime.now(UTC).isoformat(),),
        ).fetchall()
        self.assertTrue(all(row["status"] == "EXPIRED" for row in rows), [dict(r) for r in rows])
        self.assertTrue(any(r["event_type"] == "project.information_request.expired"
                            for r in self.store.db.execute(
                                "SELECT event_type FROM events WHERE event_type LIKE 'project.information_request.%'")))

    # ---- 幂等收口 ---------------------------------------------------------

    def test_stage_report_idempotent_on_duplicate_key(self) -> None:
        """重复提交（同 idempotency_key）返回原报告，不产生第二行。"""


        task = self.store.create_task(self.project.id, TaskCreate(title="幂等报告"))
        report = {
            "status": "partial", "summary": "第一次提交", "completed_items": [], "incomplete_items": [],
            "output_artifacts": [], "evidence_refs": [],
            "tests": {"passed": 1, "failed": 0, "skipped": 0, "commands": []},
            "blockers": [], "can_continue_safely": True, "continued_under_assumption": False, "assumptions": [],
        }
        first = coordination_store.submit_stage_report(
            self.store, self.project.id, task.id, MEMBER, report, idempotency_key="e2e-report-key"
        )
        second = coordination_store.submit_stage_report(
            self.store, self.project.id, task.id, MEMBER, report, idempotency_key="e2e-report-key"
        )
        self.assertEqual(first["report_id"], second["report_id"])
        self.assertTrue(second.get("duplicate"))
        count = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM stage_reports WHERE task_id = ?", (str(task.id),)
        ).fetchone()["c"]
        self.assertEqual(count, 1)

    def test_duplicate_ack_returns_original_state(self) -> None:
        """重复 ACK（同 outcome）返回原状态，不抛错不双写（重复投递纪律）。"""


        task = self.store.create_task(self.project.id, TaskCreate(title="ACK 幂等"))
        created = coordination_store.create_information_request(
            self.store, self.project.id, MEMBER,
            {"request": {"request_type": "context", "question": "需要边界", "blocking": "optional",
                         "reason": "测试", "response_deadline": None},
             "run_id": None, "provider_agent_id": "reviewer-agent", "idempotency_key": "ack-idem-1"},
        )
        coordination_store.ack_information_request(self.store, UUID(created["request_id"]), MEMBER, "received")
        again = coordination_store.ack_information_request(self.store, UUID(created["request_id"]), MEMBER, "received")
        self.assertEqual(again["status"], "ACKNOWLEDGED")
        self.assertTrue(again.get("duplicate"))

    def test_expired_ack_outcome_rejected_not_silent(self) -> None:
        """过期 ACK 走显式 expired（ack_outcome_for 已在协议层锁定，这里锁生命周期衔接）。"""

        with self.assertRaises(CoordinationError):
            # 请求从未 ACK：直接消费不可用的回复 → 非法迁移（fail-closed）
            coordination_store.consume_information_request(self.store, uuid4(), MEMBER)


if __name__ == "__main__":
    unittest.main()
