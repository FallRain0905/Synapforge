"""DP-2 后端契约测试（B1/B2/B3/B4）：心跳运行态落库、设备能力刷新、设备自视图与设备列表扩展。

覆盖：
- B1：`agent.heartbeat` 的 adapter_versions/capabilities/running_run_ids/local_queue_length/
  user_session_state/resource_summary 真的落库（此前全被丢弃），且同设备重复心跳是 upsert 而非新增行；
- B2：心跳会把 devices.capabilities / agent_version 刷新成设备自报值（装/卸执行体后无需重新注册）；
- B3：`GET /api/agent/me` 只认设备 Token，返回身份 + 运行态 + 授权项目 + 运行参数，且不含 Token 明文；
- B4：`GET /api/devices` 带出最近心跳运行态；没有心跳的设备 runtime 为 null。
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app import main
from app.contracts import (
    AgentHeartbeat,
    TaskResultSubmit,
    ArtifactCreate,
    AgentProjectGrant,
    ProjectCreate,
    RunComplete,
    RunCreate,
    TaskCreate,
    TaskStatus,
    AgentRegister,
    DevicePairingCreate,
    DeviceProjectGrantCreate,
    DeviceRegisterRequest,
    GatewayEnvelope,
)
from app.gateway import GatewayService
from app.store import DEV_ORG_ID, Store
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from device_test_support import registration_request


def _heartbeat(device_id: str, agent_id: str, session_id: str, connection_id: str, **overrides) -> AgentHeartbeat:
    values = {
        "device_id": device_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "connection_id": connection_id,
        "agent_version": "0.2.0",
        "sent_at": datetime.now(UTC),
    }
    values.update(overrides)
    return AgentHeartbeat(**values)


class DeviceRuntimeStateTests(unittest.TestCase):
    """B1/B2：心跳字段落库与设备能力刷新。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.agent = self.store.register_agent(AgentRegister(agent_id="agent-dp2", display_name="DP2 Agent"))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        self.credential = self.store.register_device(
            registration_request(
                pairing,
                Ed25519PrivateKey.generate(),
                self.agent.agent_id,
                "device-dp2-001",
                platform="windows",
                agent_version="0.1.0",
                capabilities=["task.claim"],
            )
        )
        self.device_id = self.credential.device.device_id
        self.session_id = "session-dp2-001"
        self.connection_id = "connection-dp2-001"
        self.gateway = GatewayService(self.store)
        self.context = self.gateway.authenticate(
            self.credential.device_token,
            device_id=self.device_id,
            session_id=self.session_id,
            connection_id=self.connection_id,
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _deliver(self, sequence: int, heartbeat: AgentHeartbeat) -> dict:
        envelope = GatewayEnvelope(
            device_id=self.device_id,
            agent_id=self.agent.agent_id,
            session_id=self.session_id,
            connection_id=self.connection_id,
            sequence=sequence,
            message_id=f"message-dp2-{sequence}",
            idempotency_key=f"idempotency-dp2-{sequence}",
            message_type="agent.heartbeat",
            sent_at=datetime.now(UTC),
            payload=heartbeat.model_dump(mode="json"),
        )
        return self.gateway.receive(self.context, envelope.model_dump(mode="json"))

    def _runtime(self, sequence: int = 1, **overrides) -> dict:
        response = self._deliver(sequence, _heartbeat(self.device_id, self.agent.agent_id, self.session_id, self.connection_id, **overrides))
        self.assertEqual(response.message_type, "gateway.ack")
        runtime = self.store.get_device_runtime_state(self.device_id)
        self.assertIsNotNone(runtime)
        return runtime

    def test_heartbeat_fields_are_persisted(self) -> None:
        runtime = self._runtime(
            1,
            agent_version="0.3.0",
            adapter_versions={"codex-cli": "0.154.0"},
            capabilities=["task.claim", "agent.inventory"],
            running_run_ids=["11111111-1111-1111-1111-111111111111"],
            local_queue_length=2,
            user_session_state="logged_in",
            resource_summary={"active_processes": 1},
        )
        self.assertEqual(runtime.device_id, self.device_id)
        self.assertEqual(runtime.connection_id, self.connection_id)
        self.assertEqual(runtime.agent_version, "0.3.0")
        self.assertEqual(runtime.adapter_versions, {"codex-cli": "0.154.0"})
        self.assertEqual(runtime.capabilities, ["task.claim", "agent.inventory"])
        self.assertEqual(runtime.running_run_ids, ["11111111-1111-1111-1111-111111111111"])
        self.assertEqual(runtime.local_queue_length, 2)
        self.assertEqual(runtime.user_session_state, "logged_in")
        self.assertEqual(runtime.resource_summary, {"active_processes": 1})
        self.assertIsNotNone(runtime.reported_at)

    def test_repeated_heartbeats_upsert_instead_of_appending(self) -> None:
        self._runtime(1, local_queue_length=5, adapter_versions={"codex-cli": "0.154.0"})
        runtime = self._runtime(2, local_queue_length=0, adapter_versions={})
        self.assertEqual(runtime.local_queue_length, 0)
        self.assertEqual(runtime.adapter_versions, {})
        rows = self.store.db.execute("SELECT COUNT(*) AS total FROM device_runtime_state").fetchone()
        self.assertEqual(int(rows["total"]), 1)

    def test_device_capabilities_and_version_follow_the_heartbeat(self) -> None:
        self._runtime(1, agent_version="0.4.0", capabilities=["task.claim", "codex.exec"])
        device = self.store.get_device(self.device_id)
        self.assertEqual(device.capabilities, ["task.claim", "codex.exec"])
        self.assertEqual(device.agent_version, "0.4.0")

    def test_empty_capabilities_do_not_erase_the_registered_ones(self) -> None:
        """老客户端不发 capabilities 时不能把注册时登记的能力清空。"""

        self._runtime(1, capabilities=[], agent_version="")
        device = self.store.get_device(self.device_id)
        self.assertEqual(device.capabilities, ["task.claim"])
        self.assertEqual(device.agent_version, "0.1.0")

    def test_runtime_state_is_scoped_to_the_device(self) -> None:
        self._runtime(1, local_queue_length=3)
        self.assertIsNone(self.store.get_device_runtime_state("device-other"))


class DeviceListRuntimeTests(unittest.TestCase):
    """B4：设备列表带出运行态。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _register(self, agent_id: str, device_id: str) -> str:
        agent = self.store.register_agent(AgentRegister(agent_id=agent_id, display_name=agent_id))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        return self.store.register_device(
            registration_request(pairing, Ed25519PrivateKey.generate(), agent.agent_id, device_id)
        )

    def test_listing_without_heartbeat_reports_null_runtime(self) -> None:
        self._register("agent-list-1", "device-list-1")
        devices = self.store.list_devices_for_member("member-001")
        self.assertEqual([device.device_id for device in devices], ["device-list-1"])
        self.assertIsNone(devices[0].runtime)

    def test_listing_after_heartbeat_reports_runtime(self) -> None:
        credential = self._register("agent-list-2", "device-list-2")
        session_id, connection_id = "session-list-2", "connection-list-2"
        gateway = GatewayService(self.store)
        context = gateway.authenticate(
            credential.device_token,
            device_id=credential.device.device_id,
            session_id=session_id,
            connection_id=connection_id,
        )
        envelope = GatewayEnvelope(
            device_id=credential.device.device_id,
            agent_id="agent-list-2",
            session_id=session_id,
            connection_id=connection_id,
            sequence=1,
            message_id="message-list-2",
            idempotency_key="idempotency-list-2",
            message_type="agent.heartbeat",
            sent_at=datetime.now(UTC),
            payload=_heartbeat(
                credential.device.device_id,
                "agent-list-2",
                session_id,
                connection_id,
                adapter_versions={"codex-cli": "0.154.0"},
                local_queue_length=1,
            ).model_dump(mode="json"),
        )
        gateway.receive(context, envelope.model_dump(mode="json"))

        devices = self.store.list_devices_for_member("member-001")
        self.assertEqual(len(devices), 1)
        runtime = devices[0].runtime
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime.adapter_versions, {"codex-cli": "0.154.0"})
        self.assertEqual(runtime.local_queue_length, 1)


class AgentSelfViewTests(unittest.TestCase):
    """B3：设备 Token 换自身配置。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.agent = self.store.register_agent(AgentRegister(agent_id="agent-self", display_name="Self Agent"))
        pairing = self.store.create_device_pairing(DevicePairingCreate(organization_id=UUID(DEV_ORG_ID)), "member-001")
        private_key = Ed25519PrivateKey.generate()
        self.credential = self.store.register_device(
            registration_request(pairing, private_key, self.agent.agent_id, "device-self-001")
        )
        self.project = self.store.list_projects()[0]
        self.grant = self.store.create_device_project_grant(
            self.project.id,
            DeviceProjectGrantCreate(device_id=self.credential.device.device_id, capabilities=["task.claim"]),
            "member-001",
        )
        self._original_store = main.store
        main.store = self.store

    def tearDown(self) -> None:
        main.store = self._original_store
        self.store.close()
        self.temp_dir.cleanup()

    @staticmethod
    def _request(token: str | None, scheme: str = "Bearer") -> "_FakeRequest":
        headers = {"Authorization": f"{scheme} {token}"} if token else {}
        return _FakeRequest(headers)

    def test_self_view_returns_identity_grants_and_policy(self) -> None:
        view = main.agent_self_view(self._request(self.credential.device_token))
        payload = view.model_dump(mode="json")
        self.assertEqual(payload["device"]["device_id"], "device-self-001")
        self.assertNotIn("device_token", payload["device"])
        self.assertIsNone(payload["runtime"])
        self.assertEqual([grant["project_id"] for grant in payload["grants"]], [str(self.project.id)])
        self.assertIn("codex", [payload["runtime_policy"]["executor"]])
        self.assertGreaterEqual(payload["runtime_policy"]["heartbeat_interval_seconds"], 1)
        self.assertTrue(payload["runtime_policy"]["allowed_sandboxes"])

    def test_self_view_rejects_missing_or_foreign_tokens(self) -> None:
        for token in (None, "dvc_not_a_real_token", ""):
            with self.subTest(token=token):
                with self.assertRaises(main.HTTPException) as context:
                    main.agent_self_view(self._request(token))
                self.assertEqual(context.exception.status_code, 401)
                self.assertEqual(context.exception.detail, "agent_device_token_required" if not token else "device_token_invalid")

    def test_self_view_rejects_project_capability_token(self) -> None:
        """项目能力 Token 不能冒充设备 Token（两条认证路径不能互相替代）。"""

        with self.assertRaises(main.HTTPException) as context:
            main.agent_self_view(self._request(self.grant.project_token))
        self.assertEqual(context.exception.status_code, 401)
        self.assertEqual(context.exception.detail, "device_token_invalid")

    def test_self_view_excludes_revoked_grants(self) -> None:
        self.store.revoke_device_project_grant(self.grant.grant.id, "member-001")
        view = main.agent_self_view(self._request(self.credential.device_token))
        self.assertEqual(view.grants, [])

    def test_self_view_reports_runtime_after_heartbeat(self) -> None:
        session_id, connection_id = "session-self-001", "connection-self-001"
        gateway = GatewayService(self.store)
        context = gateway.authenticate(
            self.credential.device_token,
            device_id=self.credential.device.device_id,
            session_id=session_id,
            connection_id=connection_id,
        )
        envelope = GatewayEnvelope(
            device_id=self.credential.device.device_id,
            agent_id=self.agent.agent_id,
            session_id=session_id,
            connection_id=connection_id,
            sequence=1,
            message_id="message-self-001",
            idempotency_key="idempotency-self-001",
            message_type="agent.heartbeat",
            sent_at=datetime.now(UTC),
            payload=_heartbeat(
                self.credential.device.device_id,
                self.agent.agent_id,
                session_id,
                connection_id,
                adapter_versions={"codex-cli": "0.154.0"},
            ).model_dump(mode="json"),
        )
        gateway.receive(context, envelope.model_dump(mode="json"))

        view = main.agent_self_view(self._request(self.credential.device_token))
        self.assertIsNotNone(view.runtime)
        self.assertEqual(view.runtime.adapter_versions, {"codex-cli": "0.154.0"})

    def test_runtime_policy_reads_environment_overrides(self) -> None:
        original = main.os.environ.get("MAP_AGENT_HEARTBEAT_SECONDS")
        main.os.environ["MAP_AGENT_HEARTBEAT_SECONDS"] = "45"
        main.os.environ["MAP_AGENT_ALLOWED_SANDBOXES"] = "read-only"
        try:
            policy = main.agent_runtime_policy()
        finally:
            if original is None:
                main.os.environ.pop("MAP_AGENT_HEARTBEAT_SECONDS", None)
            else:
                main.os.environ["MAP_AGENT_HEARTBEAT_SECONDS"] = original
            main.os.environ.pop("MAP_AGENT_ALLOWED_SANDBOXES", None)
        self.assertEqual(policy["heartbeat_interval_seconds"], 45)
        self.assertEqual(policy["allowed_sandboxes"], ["read-only"])

    def test_runtime_policy_ignores_invalid_environment_values(self) -> None:
        main.os.environ["MAP_AGENT_MAX_CONCURRENT_TASKS"] = "many"
        try:
            policy = main.agent_runtime_policy()
        finally:
            main.os.environ.pop("MAP_AGENT_MAX_CONCURRENT_TASKS", None)
        self.assertEqual(policy["max_concurrent_tasks"], 1)


class _FakeRequest:
    """只需要 headers 的最小替身，避免为了一个读取端点起 ASGI 应用。"""

    def __init__(self, headers: dict[str, str], member_id: str | None = None) -> None:
        self.headers = dict(headers)
        if member_id:
            self.headers["Authorization"] = f"Bearer dev-{member_id}"


if __name__ == "__main__":
    unittest.main()

class TaskExecutorPolicyTests(unittest.TestCase):
    """任务执行方式（PATCH 的 JSON 体）：模板包生成的任务必须能被"变成可执行"。

    没有这条路径，用户看到的就是"任务摆在那里、Agent 不动"——而 Agent 侧只会给出
    `executor_not_configured`，界面上完全看不到。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.create_project(ProjectCreate(name="执行方式验收项目"))
        self.task = self.store.create_task(self.project.id, TaskCreate(title="模板包生成的任务", stage="modeling"))

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_task_without_executor_is_not_executable(self) -> None:
        """模板包建的默认任务没有执行方式——这正是"Agent 领了就跑不起来"的原因。"""

        self.assertEqual(self.task.resource_policy, {})

    def test_assigning_codex_executor_and_prompt(self) -> None:
        updated = self.store.update_task(
            self.task.id,
            None,
            "agent-1",
            None,
            resource_policy={"worker_executor": "codex", "worker_prompt": "只回答 OK"},
        )
        self.assertEqual(updated.resource_policy["worker_executor"], "codex")
        self.assertEqual(updated.resource_policy["worker_prompt"], "只回答 OK")
        events = [event for event in self.store.list_events(self.project.id) if event.event_type == "task.updated"]
        self.assertTrue(any(event.payload.get("resource_policy") for event in events))

    def test_assigning_declarative_command(self) -> None:
        updated = self.store.update_task(
            self.task.id, None, None, None, resource_policy={"worker_command": [sys.executable, "-c", "print(1)"]}
        )
        self.assertEqual(updated.resource_policy["worker_command"][0], sys.executable)

    def test_rejects_unknown_executor_and_empty_policy(self) -> None:
        for policy, code in (
            ({"worker_executor": "cognition"}, "unsupported_worker_executor:cognition"),
            ({}, "task_executor_required"),
            ({"worker_command": []}, "worker_command_invalid"),
        ):
            with self.subTest(policy=policy):
                with self.assertRaises(ValueError) as raised:
                    self.store.update_task(self.task.id, None, None, None, resource_policy=policy)
                self.assertIn(code, str(raised.exception))

    def test_policy_is_preserved_when_only_status_changes(self) -> None:
        self.store.update_task(self.task.id, None, None, None, resource_policy={"worker_executor": "codex"})
        moved = self.store.update_task(self.task.id, TaskStatus.BLOCKED, None, "上游未就绪")
        self.assertEqual(moved.resource_policy["worker_executor"], "codex")

class LatestEventsTests(unittest.TestCase):
    """看板必须给**最近**事件：`list_events(limit=N)` 返回的是最早的 N 条。

    真实症状：项目跑了几十个事件，界面上一条新的都看不到，实时进度也没地方显示。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.create_project(ProjectCreate(name="事件顺序验收"))

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _add(self, count: int) -> None:
        for index in range(count):
            self.store.add_event(self.project.id, f"task.progress", "agent-1", {"index": index})

    def test_latest_events_returns_the_newest_ones_in_chronological_order(self) -> None:
        self._add(30)
        latest = self.store.list_latest_events(self.project.id, limit=5)
        # 建项目本身也会写一条 project.created，这里只看新加的这批
        self.assertEqual([event.payload.get("index") for event in latest], [25, 26, 27, 28, 29])
        # 升序返回：调用方直接渲染即可
        self.assertEqual([event.sequence for event in latest], sorted(event.sequence for event in latest))

    def test_plain_list_events_is_unchanged_for_incremental_readers(self) -> None:
        """增量读（after + limit）语义不变：补传队列与 WS 快照都依赖它。"""

        self._add(30)
        first = self.store.list_events(self.project.id, limit=6)
        self.assertEqual(first[0].event_type, "project.created")
        self.assertEqual([event.payload.get("index") for event in first[1:]], [0, 1, 2, 3, 4])
        after = self.store.list_events(self.project.id, after=first[-1].sequence, limit=5)
        self.assertEqual([event.payload.get("index") for event in after], [5, 6, 7, 8, 9])

    def test_latest_events_on_a_fresh_project_returns_the_creation_event(self) -> None:
        """新建项目不是"没有事件"：project.created 就是第一条，不能假装空。"""

        latest = self.store.list_latest_events(self.project.id, limit=5)
        self.assertEqual([event.event_type for event in latest], ["project.created"])


if __name__ == "__main__":
    unittest.main()

class RunDetailTests(unittest.TestCase):
    """单次执行详情：回答与原始输出要能完整读到，且不能越权读别的项目。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.create_project(ProjectCreate(name="运行详情验收"))
        self.agent = self.store.register_agent(AgentRegister(agent_id="agent-run-detail", display_name="Run Detail Agent"))
        self.store.grant_agent_project(
            AgentProjectGrant(project_id=self.project.id, agent_id=self.agent.agent_id, granted_by="member-001")
        )
        self.task = self.store.create_task(self.project.id, TaskCreate(title="写一段回答", stage="modeling"))
        self.run = self.store.create_run(
            self.project.id,
            RunCreate(task_id=self.task.id, agent_id=self.agent.agent_id, idempotency_key=f"run-detail-{self.task.id}"),
        )
        self._original_store = main.store
        main.store = self.store

    def tearDown(self) -> None:
        main.store = self._original_store
        self.store.close()
        self.temp_dir.cleanup()

    def test_answer_and_raw_output_are_stored_verbatim(self) -> None:
        answer = "第一段回答。\n\n第二段回答：结论是 42。"
        self.store.complete_run(
            self.run.id,
            RunComplete(
                success=True,
                idempotency_key="run-detail-complete-1",
                summary=f"{answer}\n\n---\ncodex exit=0 events=7",
                stdout='{"type": "item.completed", "item": {"type": "agent_message", "text": "第一段回答。"}}' * 3,
                stderr="",
            ),
        )
        detail = main.get_run(self.run.id, _FakeRequest({}))
        self.assertIn("结论是 42", detail.summary)
        self.assertIn("codex exit=0", detail.summary)
        self.assertGreater(len(detail.stdout), 0)

    def test_unknown_run_is_404(self) -> None:
        with self.assertRaises(main.HTTPException) as missing:
            main.get_run(uuid4(), _FakeRequest({}))
        self.assertEqual(missing.exception.status_code, 404)

    def test_unauthorized_caller_is_rejected(self) -> None:
        """端点不在 /api/projects 下，中间件看不到项目，所以必须自己挡住无名调用者。"""

        with self.assertRaises(main.HTTPException) as denied:
            main.get_run(self.run.id, _FakeRequest({"Authorization": "Bearer not-a-real-session"}))
        self.assertEqual(denied.exception.status_code, 401)
        self.assertEqual(denied.exception.detail, "invalid_session")

    def test_member_of_another_project_is_denied(self) -> None:
        """越权读别人的执行记录必须是 403（这里直接打授权层，避免为了造会话而绕远）。"""

        with self.assertRaises(PermissionError):
            self.store.authorize_member(self.project.id, "member-does-not-exist", "project.view")


if __name__ == "__main__":
    unittest.main()

class RunOutputArtifactTests(unittest.TestCase):
    """Run 要带上"这次产出了什么"：产出成果物 id 必须落库并可反查（CL-1-04）。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.create_project(ProjectCreate(name="产出回填验收"))
        self.agent = self.store.register_agent(AgentRegister(agent_id="agent-outputs", display_name="Output Agent"))
        self.store.grant_agent_project(
            AgentProjectGrant(project_id=self.project.id, agent_id=self.agent.agent_id, granted_by="member-001")
        )
        self.task = self.store.create_task(self.project.id, TaskCreate(title="产出文件", stage="modeling"))
        self.artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="notes/result.md", artifact_type="paper_source", status="PENDING_REVIEW"),
            created_by=self.agent.agent_id,
            created_by_kind="agent",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_completed_run_records_output_artifacts(self) -> None:
        run = self.store.create_run(
            self.project.id,
            RunCreate(task_id=self.task.id, agent_id=self.agent.agent_id, idempotency_key="run-outputs-1"),
        )
        completed = self.store.complete_run(
            run.id,
            RunComplete(
                success=True,
                summary="回答正文",
                output_artifact_ids=[self.artifact.id],
                idempotency_key="run-outputs-complete-1",
            ),
        )
        self.assertEqual([str(value) for value in completed.output_artifact_ids], [str(self.artifact.id)])
        # 重新读一遍：确认是落库而不只是返回值里带着
        reread = self.store.get_run(run.id)
        self.assertEqual([str(value) for value in reread.output_artifact_ids], [str(self.artifact.id)])

    def test_task_result_accepts_output_artifacts(self) -> None:
        """任务结果上报也能带产出：任务视图与 Run 视图口径一致。"""

        contract = TaskResultSubmit(
            agent_id=self.agent.agent_id,
            lease_token="lease-token-value-1234567890",
            success=True,
            summary="回答",
            output_artifact_ids=[self.artifact.id],
            idempotency_key="task-result-outputs-1",
        )
        self.assertEqual([str(value) for value in contract.output_artifact_ids], [str(self.artifact.id)])


if __name__ == "__main__":
    unittest.main()

class ArtifactDetailTests(unittest.TestCase):
    """成果物全貌（CL-3）：来源、版本谱系、被谁引用、孤儿标注。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.create_project(ProjectCreate(name="成果物详情验收"))
        self.task = self.store.create_task(self.project.id, TaskCreate(title="产出这份内容", stage="modeling"))
        self.first = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="notes/answer.md", artifact_type="agent_answer", task_id=self.task.id),
            created_by="agent-detail",
            created_by_kind="agent",
        )
        self._original_store = main.store
        main.store = self.store

    def tearDown(self) -> None:
        main.store = self._original_store
        self.store.close()
        self.temp_dir.cleanup()

    def test_lineage_source_and_orphan_flag(self) -> None:
        second = self.store.create_artifact_version(
            self.project.id,
            self.first.id,
            ArtifactCreate(
                name=self.first.name,
                artifact_type=self.first.artifact_type,
                status="PENDING_REVIEW",
                # 版本是否继承来源由调用方决定：界面「提交新版本」会把父版本的 task/run 传下来
                task_id=self.first.task_id,
            ),
            created_by="member-001",
        )
        detail = main.get_artifact_detail(self.project.id, second.id)
        self.assertEqual([entry.version for entry in detail.lineage], [1, 2])
        self.assertTrue(detail.lineage[1].is_current, "当前版本要标出来")
        self.assertEqual(detail.source_task["title"], "产出这份内容")
        self.assertFalse(detail.orphan)

        # 不带来源的版本：谱系仍在，但来源为空——不编造
        bare = self.store.create_artifact_version(
            self.project.id,
            self.first.id,
            ArtifactCreate(name=self.first.name, artifact_type=self.first.artifact_type),
            created_by="member-001",
        )
        bare_detail = main.get_artifact_detail(self.project.id, bare.id)
        self.assertIsNone(bare_detail.source_task)
        self.assertEqual(len(bare_detail.lineage), 3)

    def test_consumers_and_404_for_foreign_project(self) -> None:
        consumer = self.store.create_task(
            self.project.id,
            TaskCreate(title="引用这份内容", stage="modeling", input_artifacts=[str(self.first.id)]),
        )
        detail = main.get_artifact_detail(self.project.id, self.first.id)
        self.assertEqual([item.task_id for item in detail.consumers], [consumer.id])

        other = self.store.create_project(ProjectCreate(name="别的项目"))
        with self.assertRaises(main.HTTPException) as missing:
            main.get_artifact_detail(other.id, self.first.id)
        self.assertEqual(missing.exception.status_code, 404)
        self.assertEqual(missing.exception.detail, "artifact_not_in_project")

    def test_orphan_is_flagged_not_deleted(self) -> None:
        """来源任务被删掉的内容只标注「孤儿」，内容本身保留（D-CL-7）。"""

        self.store.db.execute("DELETE FROM tasks WHERE id = ?", (str(self.task.id),))
        self.store.db.commit()
        detail = main.get_artifact_detail(self.project.id, self.first.id)
        self.assertTrue(detail.orphan)
        self.assertIn("来源任务", detail.orphan_reason or "")
        self.assertIsNotNone(self.store.get_artifact(self.first.id))


if __name__ == "__main__":
    unittest.main()

class DocumentDraftTests(unittest.TestCase):
    """CL-6：协作草稿落库、修订号冲突与不可变守卫。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.create_project(ProjectCreate(name="协作草稿验收"))
        self.doc = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="paper/main.tex", artifact_type="paper_source", status="DRAFT"),
            created_by="member-001",
        )

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_first_save_then_load_then_revision_increments(self) -> None:
        self.assertIsNone(self.store.get_document_draft(self.project.id, self.doc.id), "还没有草稿就是 None，不编造")
        first = self.store.save_document_draft(self.project.id, self.doc.id, "第一版草稿", base_revision=0, updated_by="member-001")
        self.assertEqual(first.revision, 1)
        self.assertEqual(first.content, "第一版草稿")

        second = self.store.save_document_draft(self.project.id, self.doc.id, "第二版草稿", base_revision=1, updated_by="member-002")
        self.assertEqual(second.revision, 2)
        self.assertEqual(second.updated_by, "member-002")
        self.assertEqual(self.store.get_document_draft(self.project.id, self.doc.id).content, "第二版草稿")

    def test_stale_revision_is_a_conflict_not_a_silent_overwrite(self) -> None:
        """乙先保存（→rev1）；甲还停留在"没有草稿"（base 0）时保存必须冲突。

        注意 base_revision 的语义是"我上次看到的修订号"：与其相等表示我知道最新版本，允许写入；
        落后才冲突。断言用的是落后 0 → 服务端 1。
        """

        self.store.save_document_draft(self.project.id, self.doc.id, "别人的编辑", base_revision=0, updated_by="member-002")
        with self.assertRaises(ValueError) as raised:
            self.store.save_document_draft(self.project.id, self.doc.id, "我的编辑", base_revision=0, updated_by="member-001")
        self.assertIn("document_draft_conflict", str(raised.exception))
        # 冲突后服务端内容不被覆盖
        self.assertEqual(self.store.get_document_draft(self.project.id, self.doc.id).content, "别人的编辑")

    def test_approved_content_cannot_be_drafted(self) -> None:
        # 不能直接建 APPROVED（服务端拒绝），这里把不可变标志直接置上以验证守卫
        approved = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="paper/approved.tex", artifact_type="paper_source", status="PENDING_REVIEW"),
            created_by="member-001",
        )
        self.store.db.execute(
            "UPDATE artifacts SET status = 'APPROVED', immutable = 1, downstream_allowed = 1 WHERE id = ?",
            (str(approved.id),),
        )
        self.store.db.commit()
        with self.assertRaises(PermissionError) as raised:
            self.store.save_document_draft(self.project.id, approved.id, "偷改", base_revision=0, updated_by="member-001")
        self.assertIn("approved_artifact_is_immutable", str(raised.exception))

    def test_foreign_project_is_rejected(self) -> None:
        other = self.store.create_project(ProjectCreate(name="别的项目甲乙"))
        with self.assertRaises(ValueError) as raised:
            self.store.save_document_draft(other.id, self.doc.id, "x", base_revision=0, updated_by="member-001")
        self.assertIn("artifact_not_in_project", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
