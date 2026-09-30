"""第三期（A 侧）契约测试：事件目录接 outbox（W2.1）+ 可信归属（W2.2）+ 团队/生产路径（W2.3）。

口径：
- `project.*` 事件必须在 event_catalog 注册且信封合法才允许落库（咽喉在 store._insert_event），
  未注册名直接拒绝——发出侧拦截，绝不发出去让消费方猜；老轨事件名不受影响（只增不改）；
- 归属服务端注入：竞 pack 物化等入口的客户端自报 created_by 一律忽略，不信任自报字段；
- 团队视图/生产路径是读时聚合：数据全部来自权威表，不知道的不编（探不到就如实空/offline）。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from starlette.requests import Request

from app import main
from app.contracts import (
    AgentRegister,
    ArtifactCreate,
    CompetitionPackApplyRequest,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    TaskClaimRequest,
    TaskCreate,
)
from app.store import DEV_ORG_ID, Store
from device_test_support import registration_request

MEMBER = "member-001"


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/api/projects/test/competition-pack/apply", "headers": []})


class ProjectEventCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.client.close()  # 不先关连接，Windows 上 tempdir 清理会撞文件锁（WinError 32）
        self.store.close()
        self.temp_dir.cleanup()

    def events_named(self, event_type: str) -> list:
        return self.store.db.execute(
            "SELECT * FROM events WHERE project_id = ? AND event_type = ? ORDER BY sequence ASC",
            (str(self.project.id), event_type),
        ).fetchall()

    # ---- W2.1 目录咽喉 ----------------------------------------------------

    def test_unregistered_project_event_rejected(self) -> None:
        """未注册的 project.* 事件名直接拒绝；已注册的照常落库（进 events + outbox）。"""

        with self.assertRaisesRegex(ValueError, "project_event_catalog_rejected"):
            self.store.add_event(self.project.id, "project.task.exploded", MEMBER, {"task_id": "x"})
        with self.assertRaisesRegex(ValueError, "project_event_catalog_rejected"):
            self.store.add_event(self.project.id, "project.something.new", MEMBER, {})

        event = self.store.add_event(
            self.project.id,
            "project.task.created",
            MEMBER,
            {"task_id": str(uuid4()), "title": "合法事件"},
            actor_kind="member",
            object_type="task",
            object_id=uuid4(),
        )
        rows = self.events_named("project.task.created")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], MEMBER)
        outbox = self.store.db.execute("SELECT * FROM event_outbox WHERE event_id = ?", (str(event.id),)).fetchone()
        self.assertIsNotNone(outbox)

    def test_task_create_emits_catalog_event_with_server_actor(self) -> None:
        """建任务：老轨 `task.created` 保留，新族 `project.task.created` 同步发出，actor 来自会话。"""

        response = self.client.post(
            f"/api/projects/{self.project.id}/tasks",
            json={"title": "写模型假设", "stage": "modeling", "priority": "high"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        task_id = response.json()["id"]
        self.assertEqual(len(self.events_named("task.created")), 1)  # 老轨在
        catalog_rows = self.events_named("project.task.created")
        self.assertEqual(len(catalog_rows), 1)
        import json as _json

        payload = _json.loads(catalog_rows[0]["payload"])
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["stage"], "modeling")
        self.assertEqual(catalog_rows[0]["actor"], MEMBER)  # 开发模式回落 member-001，不是硬编码假身份
        self.assertEqual(catalog_rows[0]["object_type"], "task")

    def test_artifact_upload_emits_catalog_event_with_hash(self) -> None:
        """成果物上传：`project.artifact.uploaded` 带内容哈希（平台侧 receipt 对应物）。"""

        artifact = self.store.create_artifact(
            self.project.id, ArtifactCreate(name="assumptions.png", artifact_type="figure"), created_by=MEMBER
        )
        rows = self.events_named("project.artifact.uploaded")
        self.assertEqual(len(rows), 1)
        import json as _json

        payload = _json.loads(rows[0]["payload"])
        self.assertEqual(payload["artifact_id"], str(artifact.id))
        self.assertTrue(payload["content_hash"])
        self.assertEqual(rows[0]["actor"], MEMBER)

    # ---- W2.2 可信归属 ----------------------------------------------------

    def test_pack_apply_ignores_forged_created_by(self) -> None:
        """竞 pack 物化：请求体自报 created_by="forger" 被忽略，产物归属登录会话（member-001）。"""

        result = main.apply_project_competition_pack(
            self.project.id,
            CompetitionPackApplyRequest(idempotency_key="pack-forge-check-1", created_by="forger"),
            make_request(),
        )
        self.assertGreater(result["created_artifact_count"], 0)
        forged = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM artifacts WHERE project_id = ? AND created_by IN ('forger', 'pack-materializer')",
            (str(self.project.id),),
        ).fetchone()["c"]
        self.assertEqual(forged, 0)
        member_owned = self.store.db.execute(
            "SELECT COUNT(*) AS c FROM artifacts WHERE project_id = ? AND created_by = ?",
            (str(self.project.id), MEMBER),
        ).fetchone()["c"]
        self.assertGreater(member_owned, 0)

    # ---- W2.3 团队视图与生产路径 -----------------------------------------

    def _agent_with_grant(self):
        agent = self.store.register_agent(
            AgentRegister(agent_id="team-agent", display_name="Team Agent", owner_member_id=MEMBER)
        )
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), MEMBER)
        credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                agent.agent_id,
                "device-team-001",
                device_name="Team box",
                capabilities=["chat.run", "task.run"],
            )
        )
        grant = self.store.create_device_project_grant(
            self.project.id, DeviceProjectGrantCreate(device_id=credential.device.device_id), MEMBER
        )
        return agent, credential.device, grant

    def test_team_overview_reports_agent_load_and_tasks(self) -> None:
        agent, device, _grant = self._agent_with_grant()
        task = self.store.create_task(self.project.id, TaskCreate(title="跑第一问", stage="coding"))
        self.store.claim_task(task.id, TaskClaimRequest(agent_id=agent.agent_id, idempotency_key="team-claim-1"))

        response = self.client.get(f"/api/projects/{self.project.id}/team")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        entry = next(item for item in body["agents"] if item["agent_id"] == agent.agent_id)
        self.assertEqual(entry["device_id"], device.device_id)
        self.assertIn("task.claim", entry["capabilities"])  # 真实能力词表（领取任务的能力名是 task.claim）
        self.assertEqual(entry["load"], 1)
        self.assertEqual(entry["current_tasks"][0]["task_id"], str(task.id))
        self.assertIn(entry["online"], (True, False))  # 如实报告，不保证在线
        self.assertGreaterEqual(body["task_status_counts"].get("CLAIMED", 0), 1)

    def test_production_path_links_artifact_to_source_and_downstream(self) -> None:
        source = self.store.create_task(self.project.id, TaskCreate(title="产出一张图", stage="coding"))
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="figure-1.png", artifact_type="figure", task_id=source.id),
            created_by=MEMBER,
        )
        downstream = self.store.create_task(
            self.project.id, TaskCreate(title="把图写进论文", stage="paper", input_artifacts=[str(artifact.id)])
        )
        response = self.client.get(f"/api/projects/{self.project.id}/production-path")
        self.assertEqual(response.status_code, 200, response.text)
        node = next(item for item in response.json()["nodes"] if item["artifact_id"] == str(artifact.id))
        self.assertEqual(node["source"]["task_id"], str(source.id))
        self.assertIn(str(downstream.id), [item["task_id"] for item in node["downstream_tasks"]])

    def test_team_and_path_unknown_project_404(self) -> None:
        """不可达项目：聚合接口被访问门挡下（403 无权 / 404 不存在，与兄弟接口同口径）。"""

        for path in ("team", "production-path"):
            response = self.client.get(f"/api/projects/{uuid4()}/{path}")
            self.assertIn(response.status_code, (403, 404), f"{path}: {response.text}")


if __name__ == "__main__":
    unittest.main()