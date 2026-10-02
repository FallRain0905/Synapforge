"""协作协议接线（D1）契约测试：阶段报告 / 信息请求全生命周期 / workflow-view。

验收对照（多 Agent 协作开发计划 D1）：
- 阶段报告：结构校验后落库并发事实事件；非法结构 422 逐条列出；报告不是批准结论；
- 信息请求全生命周期：创建（required 无截止时间 422、预算超限 429、幂等返回原结果）
  → ACK → 回复 → 消费（required 使节点效果 WAITING/BLOCKED）→ 事件逐笔可追溯；
- workflow-view：节点/边/运行时请求来自服务端权威对象，阻断链服务端计算。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from app import coordination_store, main
from app.contracts import (
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    TaskCreate,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request

MEMBER = "member-001"


def report_payload(**overrides) -> dict:
    base = {
        "status": "completed",
        "summary": "事实清单完成",
        "completed_items": ["题面事实"],
        "incomplete_items": [],
        "output_artifacts": [],
        "evidence_refs": ["event_0001"],
        "tests": {"passed": 3, "failed": 0, "skipped": 0, "commands": []},
        "blockers": [],
        "can_continue_safely": True,
        "continued_under_assumption": False,
        "assumptions": [],
    }
    base.update(overrides)
    return base


def request_payload(**overrides) -> dict:
    base = {
        "request_type": "context",
        "question": "请确认信息边界约束",
        "required_information": ["边界清单"],
        "blocking": "required",
        "reason": "没有边界清单无法安全开始",
        "response_deadline": "2026-10-02T13:00:00+00:00",
    }
    base.update(overrides)
    return base


class CoordinationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="coord-agent", display_name="Coord Agent", owner_member_id=MEMBER)
        )
        peer = self.store.register_agent(
            AgentRegister(agent_id="peer-agent", display_name="Peer Agent", owner_member_id=MEMBER)
        )
        self.peer = peer
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential = self.store.register_device(
            registration_request(
                pairing, Ed25519PrivateKey.generate(), self.agent.agent_id, "device-coord-001",
                device_name="Coord box", capabilities=["task.progress"],
            )
        )
        self.device = credential.device
        self.grant = self.store.create_device_project_grant(
            self.project.id, DeviceProjectGrantCreate(device_id=self.device.device_id), MEMBER
        )
        self.headers = {"X-Project-Capability-Token": self.grant.project_token, "X-Agent-Id": self.agent.agent_id}
        # 请求的 provider 侧（peer-agent）也要有自己的设备与授权——回复由 provider 给
        pairing2 = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential2 = self.store.register_device(
            registration_request(
                pairing2, Ed25519PrivateKey.generate(), peer.agent_id, "device-peer-001",
                device_name="Peer box", capabilities=["task.progress"],
            )
        )
        grant2 = self.store.create_device_project_grant(
            self.project.id, DeviceProjectGrantCreate(device_id=credential2.device.device_id), MEMBER
        )
        self.peer_headers = {"X-Project-Capability-Token": grant2.project_token, "X-Agent-Id": peer.agent_id}

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.client.close()
        self.store.close()
        self.temp_dir.cleanup()

    # ---- helpers ---------------------------------------------------------

    def new_task(self, title: str = "需要同伴信息的任务") -> UUID:
        task = self.store.create_task(self.project.id, TaskCreate(title=title))
        return task.id

    def events_of(self, family: str) -> list[str]:
        rows = self.store.db.execute(
            "SELECT event_type FROM events WHERE project_id = ? AND event_type LIKE ? ORDER BY sequence",
            (str(self.project.id), f"project.{family}.%"),
        ).fetchall()
        return [row["event_type"] for row in rows]

    # ---- 阶段报告 ---------------------------------------------------------

    def test_stage_report_submitted_and_event_emitted(self) -> None:
        task = self.new_task()
        response = self.client.post(
            f"/api/agents/{self.agent.agent_id}/tasks/{task}/stage-report",
            json={"report": report_payload(), "run_id": "run-x", "attempt": 2}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["attempt"], 2)
        self.assertIn("project.stage_report.submitted", self.events_of("stage_report"))

    def test_stage_report_invalid_structure_422_with_error_list(self) -> None:
        task = self.new_task()
        bad = report_payload(status="completed", incomplete_items=["还有一件事"], blockers=[])
        response = self.client.post(
            f"/api/agents/{self.agent.agent_id}/tasks/{task}/stage-report", json={"report": bad}, headers=self.headers
        )
        self.assertEqual(response.status_code, 422)
        detail = response.json()["detail"]
        self.assertEqual(detail["code"], "stage_report_invalid")
        self.assertTrue(any("incomplete_items" in item for item in detail["errors"]))

    def test_stage_report_requires_capability(self) -> None:
        task = self.new_task()
        response = self.client.post(
            f"/api/agents/{self.agent.agent_id}/tasks/{task}/stage-report", json={"report": report_payload()}
        )
        self.assertEqual(response.status_code, 401)

    # ---- 信息请求全生命周期 ------------------------------------------------

    def make_request(self, **overrides) -> dict:
        task = self.new_task()
        payload = {"request": request_payload(**overrides), "run_id": "run-coord-1",
                   "requester_node_id": "node_solve", "requester_task_id": str(task),
                   "provider_agent_id": "peer-agent", "idempotency_key": f"ireq-{uuid4()}"}
        response = self.client.post(
            f"/api/agents/{self.agent.agent_id}/information-requests?project_id={self.project.id}",
            json=payload, headers=self.headers,
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_information_request_lifecycle_full_pass(self) -> None:
        """创建（required → WAITING 效果）→ ACK → 回复 → 消费（RUNNING）→ 事件逐笔可追溯。"""

        created = self.make_request()
        self.assertEqual(created["status"], "OPEN")
        self.assertIn("WAITING", created["node_effect"])

        acked = self.client.post(
            f"/api/agents/{self.peer.agent_id}/information-requests/{created['request_id']}/ack",
            json={"outcome": "received"}, headers=self.peer_headers,
        )
        self.assertEqual(acked.status_code, 200, acked.text)
        self.assertEqual(acked.json()["status"], "ACKNOWLEDGED")

        responded = self.client.post(
            f"/api/agents/{self.peer.agent_id}/information-requests/{created['request_id']}/respond",
            json={"response": {"status": "answered", "answer_summary": "边界清单：A、B、C",
                               "facts": ["边界 A"], "artifact_refs": [], "evidence_refs": [], "assumptions": []}},
            headers=self.peer_headers,
        )
        self.assertEqual(responded.status_code, 200, responded.text)
        self.assertEqual(responded.json()["status"], "ANSWERED")

        consumed = self.client.post(
            f"/api/agents/{self.agent.agent_id}/information-requests/{created['request_id']}/consume",
            headers=self.headers,
        )
        self.assertEqual(consumed.status_code, 200, consumed.text)
        self.assertEqual(consumed.json()["node_effect_result"]["effect"], "RUNNING")

        self.assertEqual(
            self.events_of("information_request"),
            ["project.information_request.created", "project.information_request.acked",
             "project.information_request.answered", "project.information_request.consumed"],
        )

    def test_request_idempotency_returns_original(self) -> None:
        task = self.new_task()
        payload = {"request": request_payload(), "run_id": "run-idem", "idempotency_key": "ireq-idem-001"}
        first = self.client.post(
            f"/api/agents/{self.agent.agent_id}/information-requests?project_id={self.project.id}",
            json=payload, headers=self.headers,
        )
        second = self.client.post(
            f"/api/agents/{self.agent.agent_id}/information-requests?project_id={self.project.id}",
            json=payload, headers=self.headers,
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["request_id"], second.json()["request_id"])
        self.assertTrue(second.json().get("duplicate"))

    def test_required_without_deadline_422(self) -> None:
        task = self.new_task()
        response = self.client.post(
            f"/api/agents/{self.agent.agent_id}/information-requests?project_id={self.project.id}",
            json={"request": request_payload(response_deadline=None), "run_id": "run-nodeadline",
                  "idempotency_key": "ireq-nodeadline"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 422)
        self.assertTrue(any("response_deadline" in item for item in response.json()["detail"]["errors"]))

    def test_budget_exceeded_429(self) -> None:
        """通信预算：并发超限 → 429（防无限互问）。"""

        for index in range(5):
            self.make_request_over_http(f"ireq-budget-{index}")
        response = self.client.post(
            f"/api/agents/{self.agent.agent_id}/information-requests?project_id={self.project.id}",
            json={"request": request_payload(), "run_id": "run-coord-1", "idempotency_key": "ireq-budget-over"},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"]["code"], "information_request_budget_exceeded")

    def make_request_over_http(self, key: str) -> None:
        self.client.post(
            f"/api/agents/{self.agent.agent_id}/information-requests?project_id={self.project.id}",
            json={"request": request_payload(), "run_id": "run-coord-1", "idempotency_key": key},
            headers=self.headers,
        )

    def test_unanswered_blocked_effect_blocks_task_on_consume(self) -> None:
        """回复不可用 → 消费后节点效果 BLOCKED，任务进 BLOCKED 并带原因（不静默）。"""

        created = self.make_request()
        self.client.post(
            f"/api/agents/{self.peer.agent_id}/information-requests/{created['request_id']}/ack",
            json={"outcome": "received"}, headers=self.peer_headers,
        )
        responded = self.client.post(
            f"/api/agents/{self.peer.agent_id}/information-requests/{created['request_id']}/respond",
            json={"response": {"status": "unavailable", "answer_summary": "", "facts": [],
                               "artifact_refs": [], "evidence_refs": [], "assumptions": []}},
            headers=self.peer_headers,
        )
        self.assertEqual(responded.status_code, 200, responded.text)
        # 不可用回复没有"消费"步：BLOCKED 在 respond 时立即生效（协议语义）
        self.assertEqual(responded.json()["node_effect_result"], {"effect": "BLOCKED", "reason": responded.json()["node_effect"]})
        task_id = UUID(created["requester_task_id"])
        task = self.store.get_task(task_id)
        self.assertEqual(str(task.status.value if hasattr(task.status, "value") else task.status), "BLOCKED")
        self.assertIn("information_request", str(task.blocked_reason))

    # ---- workflow-view -----------------------------------------------------

    def test_workflow_view_aggregates_authoritative_objects(self) -> None:
        created = self.make_request()
        view_response = self.client.get(f"/api/projects/{self.project.id}/workflow-view")
        self.assertEqual(view_response.status_code, 200, view_response.text)
        view = view_response.json()
        self.assertIn("nodes", view)  # 无工作流运行时 nodes 可为空，但结构必须在
        self.assertTrue(any(r["request_id"] == created["request_id"] for r in view["runtime_requests"]))
        self.assertTrue(any("信息请求" in chain["reason"] for chain in view["blocking_chains"]))


if __name__ == "__main__":
    unittest.main()
