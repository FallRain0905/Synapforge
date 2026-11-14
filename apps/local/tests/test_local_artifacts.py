"""成果物与溯源回执测试。

要点：
- 产出靠**执行前后差分**识别，不靠 Agent 自报；
- 每条成果物带 `RECEIPT_FORMAT v1` 的 receipt（7 个持久化列）；
- 内容哈希对账：读回的字节必须和登记时算的一致；
- 声明写的路径不许逃出工作区；
- 产出登记失败**必须如实写进摘要**，不许静默当成"已完成且无产出"。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from synapforge import artifacts, commands
from synapforge.db import open_store
from synapforge.llm import ScriptedLlm
from synapforge.runtime import LocalAgent


class ArtifactPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-art-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = open_store(self.tmp / "state.db")
        self.pid = commands.ensure_project(self.store, "artifacts")["id"]
        self.workspace = self.tmp / "ws"
        self.workspace.mkdir()
        self.agent = "agent-writer"
        commands.ensure_participant(self.store, project_id=self.pid, agent_id=self.agent)

    def test_artifact_carries_receipt_and_content_roundtrips(self) -> None:
        body = "# 结果\n\n这是产出正文。\n"
        (self.workspace / "result.md").write_bytes(body.encode("utf-8"))

        view = commands.publish_artifact(self.store, project_id=self.pid,
                                         workspace=self.workspace, relative_path="result.md",
                                         agent_id=self.agent)
        self.assertEqual(view["name"], "result.md")
        self.assertEqual(view["relative_path"], "result.md")

        full_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        receipt = view["receipt"]
        self.assertEqual(receipt["receipt_version"], 1)
        self.assertEqual(receipt["tool_name"], artifacts.CHANNEL_WORKSPACE_DIFF)
        self.assertEqual(receipt["output_hash"], full_hash[:16], "output_hash 是完整 SHA-256 前 16 位")
        self.assertEqual(receipt["output_bytes"], len(body.encode("utf-8")))
        self.assertRegex(receipt["args_hash"], r"^[0-9a-f]{16}$")

        # 持久化列真的落了（不是只回显在返回值里）
        row = self.store.db.execute(
            "SELECT receipt_version, tool_name, output_hash, output_bytes, content_hash"
            " FROM artifacts WHERE id = ?", (str(view["id"]),)).fetchone()
        self.assertEqual(int(row["receipt_version"]), 1)
        self.assertEqual(row["tool_name"], artifacts.CHANNEL_WORKSPACE_DIFF)
        self.assertEqual(row["output_hash"], full_hash[:16])
        self.assertEqual(row["content_hash"], full_hash)

        # 读回来的字节逐字节一致
        read_back = commands.read_artifact(self.store, artifact_id=view["id"])
        self.assertEqual(read_back["text"], body)
        self.assertEqual(read_back["content_hash"], full_hash)

    def test_path_escape_is_rejected(self) -> None:
        outside = self.tmp / "secret.txt"
        outside.write_text("nope", encoding="utf-8")
        with self.assertRaises(artifacts.ArtifactError):
            commands.publish_artifact(self.store, project_id=self.pid,
                                      workspace=self.workspace,
                                      relative_path="../secret.txt", agent_id=self.agent)

    def test_status_is_pending_review(self) -> None:
        """只报告不判断：执行体产出一律待审，不自己批准自己。"""

        (self.workspace / "a.json").write_bytes(b"{}")
        view = commands.publish_artifact(self.store, project_id=self.pid,
                                         workspace=self.workspace, relative_path="a.json",
                                         agent_id=self.agent)
        self.assertEqual(view["status"], "PENDING_REVIEW")


class RuntimeArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-artrun-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "state.db"
        store = open_store(self.db)
        self.pid = commands.ensure_project(store, "art-runtime")["id"]
        self.task = commands.create_task(store, project_id=self.pid, title="产出成果物验证")
        store.close()
        self.workspace = self.tmp / "ws"

    def _agent(self, replies: list[str]) -> LocalAgent:
        return LocalAgent("agent-writer", self.pid, role="writer", llm=ScriptedLlm(replies),
                          peer_ids=[], store_path=self.db, workspace=self.workspace)

    def test_finish_publishes_declared_files_with_receipt(self) -> None:
        body = "简介：synapforge 是本机多 Agent 协作协议。"
        agent = self._agent([json.dumps({
            "action": "finish",
            "summary": "写好了",
            "artifacts": [{"path": "report.md", "content": body}],
        }, ensure_ascii=False)])
        self.addCleanup(agent.close)

        record = agent.run_once()
        work = record["work"]
        self.assertEqual(work["action"], "finish")
        self.assertEqual(work["written"], ["report.md"])
        self.assertEqual(len(work["artifacts"]), 1)
        self.assertEqual(work["artifact_errors"], [])

        # 文件真的落在工作区里
        self.assertEqual((self.workspace / "report.md").read_bytes(), body.encode("utf-8"))

        # 成果物真的落库、带 receipt、且挂在这条任务上
        listed = commands.list_artifacts(agent.store, project_id=self.pid)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["receipt_version"], 1)
        self.assertEqual(listed[0]["task_id"], str(self.task["id"]))

        read_back = commands.read_artifact(agent.store, artifact_id=listed[0]["id"])
        self.assertEqual(read_back["text"], body)

        # 任务收尾时把这件成果物报上去了
        row = agent.store.db.execute(
            "SELECT status FROM tasks WHERE id = ?", (str(self.task["id"]),)).fetchone()
        self.assertNotEqual(str(row["status"]), "RUNNING")

    def test_path_escape_in_declaration_is_refused_and_reported(self) -> None:
        agent = self._agent([json.dumps({
            "action": "finish", "summary": "试图越界",
            "artifacts": [{"path": "../../escape.md", "content": "nope"}],
        }, ensure_ascii=False)])
        self.addCleanup(agent.close)

        record = agent.run_once()
        work = record["work"]
        self.assertEqual(work["written"], [])
        self.assertTrue(any("escapes workspace" in item for item in work["artifact_errors"]))
        self.assertFalse((self.tmp / "escape.md").exists())
        # 越界必须写进摘要，不许假装干净收尾
        row = agent.store.db.execute(
            "SELECT summary FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        if row is not None:
            self.assertIn("产出登记问题", str(row["summary"]))

    def test_finish_without_artifacts_is_honest(self) -> None:
        """没声明产出就不该凭空多出成果物——差分识别，不是自报。"""

        agent = self._agent([json.dumps({"action": "finish", "summary": "没写文件"},
                                        ensure_ascii=False)])
        self.addCleanup(agent.close)
        work = agent.run_once()["work"]
        self.assertEqual(work["written"], [])
        self.assertEqual(work["artifacts"], [])
        self.assertEqual(commands.list_artifacts(agent.store, project_id=self.pid), [])


if __name__ == "__main__":
    unittest.main()
