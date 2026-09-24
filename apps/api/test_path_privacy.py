"""FM-0：执行体自报的宿主机绝对路径不能出现在成员的响应里（`app/path_privacy.py`）。

这三处以前会把 Agent 那台机器的绝对路径原样发给任何登录成员：`agents.local_workspace`、
`runs.observed_input_files`、`artifacts.source_path`。库里真值不动（边界判定与审计要用它们），
只在**成员可见的响应**里收窄：工作区给稳定标识，文件只给文件名。

设备侧仍要拿到原值——执行体注册完要能回读自己上报的工作区来对账，收窄它就没法验证
"注册的工作区与实际 Runner cwd 一致"了。所以这里同时钉住"该收的收、该留的留"。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import main
from app.contracts import AgentProjectGrant, AgentRegister, ArtifactCreate, RunComplete, RunCreate, SessionCreate
from app.path_privacy import path_label, public_agent, public_artifact, public_run, workspace_identity
from app.store import Store

MEMBER = "member-001"
# 故意用 Windows 形状：真实上报的就是这种（云端 Agent 是 /srv/synapforge/<instance>）
HOST_WORKSPACE = r"C:\Users\someone\Documents\MathAgentWorkspace"
INPUT_PATH = HOST_WORKSPACE + r"\inputs\题目原文.txt"


def identity_of(raw: str) -> str:
    return "ws-" + hashlib.sha256(raw.replace("\\", "/").rstrip("/").encode("utf-8")).hexdigest()[:12]


class PathLabelTests(unittest.TestCase):
    def test_workspace_identity_is_stable_and_path_independent(self) -> None:
        first = workspace_identity(HOST_WORKSPACE)
        self.assertEqual(first, identity_of(HOST_WORKSPACE))
        self.assertTrue(first.startswith("ws-"))
        # 同一路径重复上报得到同一个标识（大小写与分隔符风格不影响同一台机器）
        self.assertEqual(workspace_identity(HOST_WORKSPACE.replace("\\", "/")), first)
        # 不同机器不同标识
        self.assertNotEqual(workspace_identity("/srv/synapforge/cloud-01"), first)
        self.assertIsNone(workspace_identity(None))
        self.assertIsNone(workspace_identity("   "))
        # 标识里不能残留路径痕迹
        self.assertNotIn("Users", first)
        self.assertNotIn("someone", first)

    def test_path_label_keeps_only_the_file_name(self) -> None:
        self.assertEqual(path_label(INPUT_PATH), "…/题目原文.txt")
        self.assertEqual(path_label("/srv/synapforge/cloud-01/out/answer.md"), "…/answer.md")
        self.assertEqual(path_label("single.csv"), "…/single.csv")
        self.assertIsNone(path_label(None))
        self.assertIsNone(path_label(""))


class MemberResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.client = TestClient(main.app)
        self.project = self.store.list_projects()[0]
        self.session = self.store.create_session(SessionCreate(member_id=MEMBER, expires_in_seconds=600))
        self.headers = {"Authorization": f"Bearer {self.session.token}"}
        # 运行记录必须挂在一台**已注册并有项目授权**的 Agent 上（否则 store 直接拒绝）
        self.store.register_agent(
            AgentRegister(agent_id="fm0-agent", display_name="FM0 Agent", owner_member_id=MEMBER)
        )
        self.store.grant_agent_project(
            AgentProjectGrant(agent_id="fm0-agent", project_id=self.project.id, granted_by=MEMBER)
        )

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.temp_dir.cleanup()

    def test_agent_list_hides_the_host_path_but_keeps_an_identity(self) -> None:
        self.client.post(
            "/api/agents/register",
            json={
                "agent_id": "fm0-agent",
                "display_name": "FM0 Agent",
                "owner_member_id": MEMBER,
                "local_workspace": HOST_WORKSPACE,
            },
        )
        response = self.client.get("/api/agents", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = next(item for item in response.json() if item["agent_id"] == "fm0-agent")
        self.assertIsNone(payload["local_workspace"])
        self.assertEqual(payload["workspace_identity"], identity_of(HOST_WORKSPACE))
        # 整个响应体里不许出现原路径（哪怕是别处顺手带出来的）
        self.assertNotIn("someone", response.text)
        self.assertNotIn("C:\\Users", response.text)
        # 库里仍然留真值：运维排障与 FM-3 的工作区台账要用
        stored = self.store.db.execute("SELECT local_workspace FROM agents WHERE agent_id = ?", ("fm0-agent",)).fetchone()
        self.assertEqual(stored["local_workspace"], HOST_WORKSPACE)

    def test_device_side_still_reads_back_what_it_reported(self) -> None:
        """注册/心跳是**设备侧**接口：执行体要回读自己上报的工作区才能对账。"""

        registered = self.client.post(
            "/api/agents/register",
            json={
                "agent_id": "fm0-agent-b",
                "display_name": "FM0 Agent B",
                "owner_member_id": MEMBER,
                "local_workspace": HOST_WORKSPACE,
            },
        )
        self.assertEqual(registered.status_code, 200)
        self.assertEqual(registered.json()["local_workspace"], HOST_WORKSPACE)
        heartbeat = self.client.post("/api/agents/fm0-agent-b/heartbeat")
        self.assertEqual(heartbeat.json()["local_workspace"], HOST_WORKSPACE)

    def test_run_detail_and_list_only_show_file_names(self) -> None:
        run = self.store.create_run(
            self.project.id,
            RunCreate(
                agent_id="fm0-agent",
                observed_input_files=[INPUT_PATH, "/srv/synapforge/cloud-01/inputs/data.csv"],
                idempotency_key="fm0-run-create-1",
            ),
        )
        self.store.complete_run(run.id, RunComplete(success=True, summary="ok"))

        detail = self.client.get(f"/api/runs/{run.id}", headers=self.headers)
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["observed_input_files"], ["…/题目原文.txt", "…/data.csv"])
        self.assertNotIn("someone", detail.text)
        # 边界结论里嵌着同一份原始路径（同一个泄漏的第二条通路）
        self.assertEqual(
            detail.json()["information_boundary"]["observed_input_files"], ["…/题目原文.txt", "…/data.csv"]
        )

        listing = self.client.get(f"/api/projects/{self.project.id}/runs")
        self.assertEqual(listing.status_code, 200)
        listed = next(item for item in listing.json() if item["id"] == str(run.id))
        self.assertEqual(listed["observed_input_files"], ["…/题目原文.txt", "…/data.csv"])
        # 真值仍在库里（边界判定 `boundary_gate` 比的是原始路径）
        self.assertEqual(self.store.get_run(run.id).observed_input_files[0], INPUT_PATH)

    def test_artifact_source_path_is_a_file_name_only(self) -> None:
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(
                name="结果表",
                artifact_type="result_table",
                content_hash="sha256:" + "a" * 64,
                source_path=HOST_WORKSPACE + r"\out\results.csv",
            ),
        )
        listing = self.client.get(f"/api/projects/{self.project.id}/artifacts")
        payload = next(item for item in listing.json() if item["id"] == str(artifact.id))
        self.assertEqual(payload["source_path"], "…/results.csv")
        self.assertNotIn("someone", listing.text)

        detail = self.client.get(f"/api/projects/{self.project.id}/artifacts/{artifact.id}/detail")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["artifact"]["source_path"], "…/results.csv")
        self.assertNotIn("someone", detail.text)
        self.assertEqual(self.store.get_artifact(artifact.id).source_path, HOST_WORKSPACE + r"\out\results.csv")


class HelperTests(unittest.TestCase):
    """helper 必须返回**新实例**：库里那份真值不能被就地改掉（下次读还是原路径）。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.project = self.store.list_projects()[0]
        self.store.register_agent(
            AgentRegister(agent_id="agent-x", display_name="Agent X", owner_member_id=MEMBER, local_workspace=HOST_WORKSPACE)
        )
        self.store.grant_agent_project(
            AgentProjectGrant(agent_id="agent-x", project_id=self.project.id, granted_by=MEMBER)
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_helpers_do_not_mutate_the_original(self) -> None:
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(
                name="x",
                artifact_type="code",
                content_hash="sha256:" + "b" * 64,
                source_path=HOST_WORKSPACE + r"\a.py",
            ),
        )
        self.assertEqual(public_artifact(artifact).source_path, "…/a.py")
        self.assertEqual(artifact.source_path, HOST_WORKSPACE + r"\a.py")

        run = self.store.create_run(
            self.project.id,
            RunCreate(agent_id="agent-x", observed_input_files=[INPUT_PATH], idempotency_key="fm0-helper-run"),
        )
        self.assertEqual(public_run(run).observed_input_files, ["…/题目原文.txt"])
        self.assertEqual(run.observed_input_files, [INPUT_PATH])

        agent = self.store.register_agent(
            AgentRegister(agent_id="agent-x", display_name="Agent X", owner_member_id=MEMBER, local_workspace=HOST_WORKSPACE)
        )
        redacted = public_agent(agent)
        self.assertIsNone(redacted.local_workspace)
        self.assertEqual(redacted.workspace_identity, identity_of(HOST_WORKSPACE))
        self.assertEqual(agent.local_workspace, HOST_WORKSPACE)


if __name__ == "__main__":
    unittest.main()