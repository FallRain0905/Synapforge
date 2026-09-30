"""任务租约 fencing（W2.5）契约测试。

把既有执法面锁进测试（借鉴 deer-flow worker lease/heartbeat/fencing 语义，实施计划 §1 #8）：
- 租约过期：进度/结果写入一律拒绝（`_lease_for_task` 先过期清扫再校验 ACTIVE）；
- 接管：A 的租约过期后 B 可重新领取；A 的旧令牌此后**一切写入被拒**（fencing 生效）；
- 心跳：过期令牌心跳被拒并落 EXPIRED；拿别人的令牌心跳 → lease_agent_mismatch。
平台不新增机制——这些语义已存在，本测试防止将来被无声破坏。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import main
from app.contracts import (
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    TaskClaimRequest,
    TaskCreate,
    TaskProgressRequest,
    TaskResultSubmit,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request

MEMBER = "member-001"


class LeaseFencingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # ---- helpers ---------------------------------------------------------

    def agent_with_grant(self, agent_id: str, device_id: str):
        agent = self.store.register_agent(
            AgentRegister(agent_id=agent_id, display_name=agent_id.title(), owner_member_id=MEMBER)
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                agent.agent_id,
                device_id,
                device_name=f"box-{agent_id}",
                capabilities=["task.claim", "task.progress", "task.result", "task.lease"],
            )
        )
        self.store.create_device_project_grant(
            self.project.id, DeviceProjectGrantCreate(device_id=credential.device.device_id), MEMBER
        )
        return agent

    def claimed_by(self, agent_id: str, key: str):
        task = self.store.create_task(self.project.id, TaskCreate(title=f" fencing-{key}"))
        _task, lease = self.store.claim_task(
            task.id, TaskClaimRequest(agent_id=agent_id, lease_seconds=900, idempotency_key=f"fencing-{key}")
        )
        return task, lease

    def expire(self, lease) -> None:
        """把租约的到期时间拨到过去（模拟租约到期；清扫由 _expire_leases 在写入路径触发）。"""

        self.store.db.execute(
            "UPDATE task_leases SET expires_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(seconds=5)).isoformat(), str(lease.lease_id)),
        )
        self.store.db.commit()

    # ---- 过期即拒 ---------------------------------------------------------

    def test_expired_lease_rejects_progress_and_result(self) -> None:
        agent = self.agent_with_grant("fencing-a", "device-fencing-a")
        task, lease = self.claimed_by(agent.agent_id, "expired")
        self.expire(lease)
        with self.assertRaisesRegex(ValueError, "lease_not_active"):
            self.store.update_task_progress(
                task.id,
                TaskProgressRequest(
                    agent_id=agent.agent_id, lease_token=lease.lease_token, status="RUNNING", idempotency_key="fencing-prog-001"
                ),
            )
        with self.assertRaisesRegex(ValueError, "lease_not_active"):
            self.store.submit_task_result(
                task.id,
                TaskResultSubmit(
                    agent_id=agent.agent_id,
                    lease_token=lease.lease_token,
                    idempotency_key="fencing-result-01",
                    success=True,
                    output_artifact_ids=[],
                    summary="过期后还想交结果",
                ),
            )

    # ---- 接管与旧令牌失效 -------------------------------------------------

    def test_takeover_invalidates_old_holder(self) -> None:
        """A 过期 → B 接管成功；A 的旧令牌此后一切写入被拒（fencing 生效）。"""

        agent_a = self.agent_with_grant("fencing-old", "device-fencing-old")
        agent_b = self.agent_with_grant("fencing-new", "device-fencing-new")
        task, lease_a = self.claimed_by(agent_a.agent_id, "takeover")
        self.expire(lease_a)
        _task2, lease_b = self.store.claim_task(
            task.id, TaskClaimRequest(agent_id=agent_b.agent_id, lease_seconds=900, idempotency_key="takeover-b")
        )
        with self.assertRaisesRegex(ValueError, "lease_not_active"):
            self.store.update_task_progress(
                task.id,
                TaskProgressRequest(
                    agent_id=agent_a.agent_id, lease_token=lease_a.lease_token, status="RUNNING", idempotency_key="fencing-old-0001"
                ),
            )
        updated = self.store.update_task_progress(
            task.id,
            TaskProgressRequest(
                agent_id=agent_b.agent_id, lease_token=lease_b.lease_token, status="RUNNING", idempotency_key="fencing-new-0001"
            ),
        )
        self.assertEqual(str(updated.status.value if hasattr(updated.status, "value") else updated.status), "RUNNING")

    # ---- 心跳面 -----------------------------------------------------------

    def test_heartbeat_rejects_expired_token(self) -> None:
        agent = self.agent_with_grant("fencing-hb", "device-fencing-hb")
        _task, lease = self.claimed_by(agent.agent_id, "hb")
        self.expire(lease)
        with self.assertRaisesRegex(ValueError, "lease_expired"):
            self.store.heartbeat_lease(lease.lease_token, agent.agent_id, 300, self.project.id)

    def test_heartbeat_rejects_foreign_agent(self) -> None:
        """拿别人的令牌心跳 → lease_agent_mismatch（fencing 的身份面）。"""

        agent_a = self.agent_with_grant("fencing-hb-a", "device-fencing-hb-a")
        agent_b = self.agent_with_grant("fencing-hb-b", "device-fencing-hb-b")
        _task, lease = self.claimed_by(agent_a.agent_id, "hb-foreign")
        with self.assertRaisesRegex(PermissionError, "lease_agent_mismatch"):
            self.store.heartbeat_lease(lease.lease_token, agent_b.agent_id, 300, self.project.id)


if __name__ == "__main__":
    unittest.main()
