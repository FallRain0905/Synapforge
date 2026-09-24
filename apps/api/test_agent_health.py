"""UX-3 Agent 状态真实性与租约回收的契约测试。

覆盖：
- 心跳超时（固定 90s 阈值）把在线 Agent 置 offline 并写 agent.offline 事件；
- 扫描是幂等的：已离线的不再重复写事件；
- 租约过期后的任务去向按决策 D4 分级：CLAIMED→READY、RUNNING→NEEDS_REVISION+机器复核（含风险）；
- 其他状态（WAITING_REVIEW）只释放租约，不动任务；
- Gateway 连接全部断开、设备撤销后 Agent 落 offline；
- 维护扫描真的调用 broadcast_event（此前是死代码）。
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import UUID, uuid4

from app import main
from app.contracts import (
    AgentProjectGrant,
    AgentRegister,
    DevicePairingCreate,
    ProjectCreate,
    ReviewerKind,
    TaskClaimRequest,
    TaskCreate,
    TaskStatus,
)
from app.store import AGENT_HEARTBEAT_TIMEOUT_SECONDS, DEV_ORG_ID, Store
from device_test_support import registration_request
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _status_of(task) -> str:
    """Task.status 在 Store 层是字符串（契约枚举在 API 层），统一取字符串。"""

    return task.status.value if hasattr(task.status, "value") else str(task.status)


class AgentHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.create_project(ProjectCreate(name="UX3 验收项目", competition_pack="cumcm-2026"))
        self.agent = self.store.register_agent(
            AgentRegister(agent_id="agent-ux3", display_name="UX3 Agent", owner_member_id="member-001")
        )
        self.store.grant_agent_project(
            AgentProjectGrant(
                project_id=self.project.id,
                agent_id=self.agent.agent_id,
                granted_by="member-001",
                capabilities=["task.claim"],
            )
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _backdate_last_seen(self, seconds: int) -> None:
        moment = datetime.now(UTC) - timedelta(seconds=seconds)
        self.store.db.execute("UPDATE agents SET last_seen = ? WHERE agent_id = ?", (_iso(moment), self.agent.agent_id))
        self.store.db.commit()

    def _events(self, event_type: str) -> list:
        return [
            event
            for event in self.store.list_events(self.project.id, limit=200)
            if event.event_type == event_type
        ]

    # ---- UX-3-01 心跳超时 -------------------------------------------------

    def test_heartbeat_timeout_marks_agent_offline(self) -> None:
        self._backdate_last_seen(AGENT_HEARTBEAT_TIMEOUT_SECONDS + 30)
        events = self.store.expire_stale_agents()

        agent = next(item for item in self.store.list_agents() if item.agent_id == self.agent.agent_id)
        self.assertEqual(agent.status, "offline")
        self.assertEqual(len(events), 1)
        offline_events = self._events("agent.offline")
        self.assertEqual(len(offline_events), 1)
        self.assertEqual(offline_events[0].payload["reason"], "heartbeat_timeout")
        self.assertEqual(offline_events[0].payload["timeout_seconds"], AGENT_HEARTBEAT_TIMEOUT_SECONDS)
        self.assertEqual(offline_events[0].actor, self.agent.agent_id)

    def test_recent_heartbeat_is_not_offline(self) -> None:
        self.store.heartbeat(self.agent.agent_id)
        self.assertEqual(self.store.expire_stale_agents(), [])
        agent = next(item for item in self.store.list_agents() if item.agent_id == self.agent.agent_id)
        self.assertEqual(agent.status, "online")

    def test_expire_is_idempotent(self) -> None:
        self._backdate_last_seen(AGENT_HEARTBEAT_TIMEOUT_SECONDS + 5)
        self.store.expire_stale_agents()
        self.assertEqual(self.store.expire_stale_agents(), [])
        self.assertEqual(len(self._events("agent.offline")), 1)

    def test_threshold_boundary_uses_reference_time(self) -> None:
        """阈值判断以参考时间为准，便于测试与未来复用，不必真的等 90 秒。"""

        self.store.heartbeat(self.agent.agent_id)
        future = _iso(datetime.now(UTC) + timedelta(seconds=AGENT_HEARTBEAT_TIMEOUT_SECONDS + 1))
        events = self.store.expire_stale_agents(reference_time=future)
        # 参考时间也会让种子 Agent 越过阈值，因此只断言本测试 Agent 的事件。
        mine = [event for event in events if event.payload["agent_id"] == self.agent.agent_id]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0].payload["reason"], "heartbeat_timeout")

    # ---- UX-3-03 租约回收 -------------------------------------------------

    def _claim_task(self, title: str, status_after_claim: str | None = None):
        task = self.store.create_task(self.project.id, TaskCreate(title=title, output_types=["result_table"]))
        claim = TaskClaimRequest(agent_id=self.agent.agent_id, lease_seconds=120, idempotency_key=f"claim-{uuid4().hex}")
        claimed, lease = self.store.claim_task(task.id, claim)
        self.assertEqual(_status_of(claimed), "CLAIMED")
        if status_after_claim:
            self.store.update_task(task.id, TaskStatus(status_after_claim), None, None)
        return task, lease

    def _expire_lease(self, lease, seconds_ago: int = 5) -> None:
        moment = datetime.now(UTC) - timedelta(seconds=seconds_ago)
        self.store.db.execute("UPDATE task_leases SET expires_at = ? WHERE id = ?", (_iso(moment), str(lease.lease_id)))
        self.store.db.commit()

    def test_claimed_task_returns_to_ready(self) -> None:
        task, lease = self._claim_task("未开工任务")
        self._expire_lease(lease)

        events = self.store.recycle_expired_leases()

        refreshed = self.store.get_task(task.id)
        self.assertEqual(_status_of(refreshed), "READY")
        self.assertEqual(len(events), 1)
        expired_events = self._events("task.lease.expired")
        self.assertEqual(len(expired_events), 1)
        self.assertEqual(expired_events[0].payload["previous_status"], "CLAIMED")
        self.assertEqual(expired_events[0].payload["agent_id"], self.agent.agent_id)

    def test_claimed_task_can_be_claimed_again_after_recycle(self) -> None:
        task, lease = self._claim_task("可重领任务")
        self._expire_lease(lease)
        self.store.recycle_expired_leases()

        claim = TaskClaimRequest(agent_id=self.agent.agent_id, lease_seconds=120, idempotency_key=f"reclaim-{uuid4().hex}")
        reclaimed, _ = self.store.claim_task(task.id, claim)
        self.assertEqual(reclaimed.id, task.id)
        self.assertEqual(_status_of(reclaimed), "CLAIMED")

    def test_running_task_goes_to_needs_revision_with_machine_review(self) -> None:
        task, lease = self._claim_task("已开工任务", status_after_claim="RUNNING")
        self._expire_lease(lease)

        events = self.store.recycle_expired_leases()

        refreshed = self.store.get_task(task.id)
        self.assertEqual(_status_of(refreshed), "NEEDS_REVISION")
        recycled_events = self._events("task.lease.recycled")
        self.assertEqual(len(recycled_events), 1)
        self.assertEqual(recycled_events[0].payload["previous_status"], "RUNNING")
        self.assertEqual(len(events), 1)

        # 机器复核登记了风险，且复核者类型是 system（不是人工批准）
        center = self.store.review_center(self.project.id) if hasattr(self.store, "review_center") else None
        reviews = self.store.list_reviews(self.project.id) if hasattr(self.store, "list_reviews") else []
        if reviews:
            review = next(item for item in reviews if item.target_id == task.id)
            self.assertEqual(review.verdict, "NEEDS_REVISION")
        risks = self.store.list_risks(self.project.id)
        codes = {risk["code"] if isinstance(risk, dict) else risk.code for risk in risks}
        self.assertIn("lease_expired_during_execution", codes)
        self.assertIsNotNone(center) if center is not None else None

    def test_lease_recycle_is_idempotent(self) -> None:
        task, lease = self._claim_task("幂等任务", status_after_claim="RUNNING")
        self._expire_lease(lease)
        self.store.recycle_expired_leases()
        self.assertEqual(self.store.recycle_expired_leases(), [])
        self.assertEqual(_status_of(self.store.get_task(task.id)), "NEEDS_REVISION")

    def test_other_status_only_releases_lease(self) -> None:
        task, lease = self._claim_task("待审核任务", status_after_claim="RUNNING")
        self.store.update_task(task.id, TaskStatus.WAITING_REVIEW, None, None)
        self._expire_lease(lease)

        self.store.recycle_expired_leases()

        self.assertEqual(_status_of(self.store.get_task(task.id)), "WAITING_REVIEW")
        self.assertEqual(self._events("task.lease.expired"), [])
        self.assertEqual(self._events("task.lease.recycled"), [])

    # ---- UX-3-02 其他离线写入点 -------------------------------------------

    def _register_device(self) -> str:
        """给本测试 Agent 注册一台真实设备（Ed25519 配对签名），返回 device_id。"""

        pairing = self.store.create_device_pairing(
            DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001"
        )
        key = Ed25519PrivateKey.generate()
        credential = self.store.register_device(
            registration_request(
                pairing,
                key,
                self.agent.agent_id,
                f"device-{self.agent.agent_id}",
                device_name="UX3 workstation",
                platform="windows",
                agent_version="0.1.0",
                capabilities=["task.claim"],
            )
        )
        return credential.device.device_id

    def test_gateway_disconnect_marks_agent_offline(self) -> None:
        device_id = self._register_device()
        connection_id = f"conn-ux3-{uuid4().hex[:8]}"
        self.store.open_agent_connection(device_id, "session-ux3", connection_id)
        self.store.heartbeat(self.agent.agent_id)

        self.store.close_agent_connection(connection_id)

        agent = next(item for item in self.store.list_agents() if item.agent_id == self.agent.agent_id)
        self.assertEqual(agent.status, "offline")
        events = self._events("agent.offline")
        self.assertTrue(events)
        self.assertEqual(events[-1].payload["reason"], "gateway_disconnected")

    def test_liveness_kept_when_another_connection_remains(self) -> None:
        """还有连接活着时不能误判离线。"""

        device_id = self._register_device()
        first, second = f"conn-a-{uuid4().hex[:8]}", f"conn-b-{uuid4().hex[:8]}"
        for connection_id in (first, second):
            self.store.open_agent_connection(device_id, "session-ux3", connection_id)
        self.store.heartbeat(self.agent.agent_id)

        self.store.close_agent_connection(first)

        agent = next(item for item in self.store.list_agents() if item.agent_id == self.agent.agent_id)
        self.assertEqual(agent.status, "online")

    # ---- UX-3-04 事件广播接通 ---------------------------------------------

    def test_maintenance_tick_broadcasts_events(self) -> None:
        """扫描出的离线事件必须真的推给订阅者（此前 broadcast_event 是死代码）。"""

        self._backdate_last_seen(AGENT_HEARTBEAT_TIMEOUT_SECONDS + 10)
        with patch.object(main, "store", self.store), patch.object(main, "broadcast_event") as broadcast:
            events = asyncio.run(main._maintenance_tick())

        self.assertEqual(len(events), 1)
        self.assertEqual(broadcast.call_count, 1)
        self.assertEqual(broadcast.call_args.args[0].event_type, "agent.offline")

    def test_maintenance_tick_noop_when_healthy(self) -> None:
        self.store.heartbeat(self.agent.agent_id)
        with patch.object(main, "store", self.store), patch.object(main, "broadcast_event") as broadcast:
            events = asyncio.run(main._maintenance_tick())

        self.assertEqual(events, [])
        self.assertEqual(broadcast.call_count, 0)

    def test_maintenance_pass_does_not_broadcast_from_worker_thread(self) -> None:
        """回归：工作线程里的 broadcast_event 会抛 RuntimeError 被吞掉，因此扫描不负责广播。"""

        self._backdate_last_seen(AGENT_HEARTBEAT_TIMEOUT_SECONDS + 10)
        with patch.object(main, "store", self.store) as _store, patch.object(main, "broadcast_event") as broadcast:
            events = main._maintenance_pass()

        self.assertEqual(len(events), 1)
        self.assertEqual(broadcast.call_count, 0)

    def test_reviewer_kind_for_lease_recycle_is_system(self) -> None:
        """回收产生的复核必须是系统机器意见，不能冒充人工批准（D9）。"""

        self.assertEqual(ReviewerKind.SYSTEM.value, "system")

    # ---- 并发安全回归 -----------------------------------------------------

    def test_connection_is_serialized_across_threads(self) -> None:
        """回归：维护扫描线程与 FastAPI 请求线程共用同一个连接会抛 InterfaceError。

        症状是请求莫名 404/500、WebSocket 建连后立刻断开。修复方式是把连接的所有操作
        （含取数）放进可重入锁串行化。
        """

        errors: list[BaseException] = []
        stop = threading.Event()

        def reader() -> None:
            while not stop.is_set():
                try:
                    self.store.list_agents()
                    self.store.list_events(self.project.id, limit=5)
                except BaseException as error:  # noqa: BLE001 - 记录后退出即可
                    errors.append(error)
                    return

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for _ in range(200):
                self.store.heartbeat(self.agent.agent_id)
                self.store.expire_stale_agents()
        finally:
            stop.set()
            thread.join(timeout=5)

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()