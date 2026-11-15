"""集群任务 / 集群 Agent / 会话导入 / 打开文件夹。

这几个模块的共同点：都在**碰别人的数据或系统的自由度**，所以测试重点不是
快乐路径，而是边界——只读打开、路径白名单、状态机不许绕过。
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from synapforge import commands as base
from synapforge.db import open_store
from synapforge.studio import agents as studio_agents
from synapforge.studio import external
from synapforge.studio import reveal
from synapforge.studio import store as studio_store
from synapforge.studio import tasks_view
from synapforge.studio.commands import StudioError


def _zcode_fixture(root: Path) -> Path:
    """造一个最小 ZCode 会话库（session / message / part 三张表）。"""

    db = root / ".zcode" / "cli" / "db" / "db.sqlite"
    db.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE session (
            id TEXT PRIMARY KEY, title TEXT, directory TEXT, parent_id TEXT,
            time_created INTEGER, time_updated INTEGER, time_archived INTEGER
        );
        CREATE TABLE message (
            id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
            sequence INTEGER, data TEXT
        );
        CREATE TABLE part (
            id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
            time_created INTEGER, sequence INTEGER, data TEXT
        );
    """)
    connection.execute(
        "INSERT INTO session VALUES (?,?,?,?,?,?,?)",
        ("sess_abc123", "旧项目的收尾", "D:/work/old", "", 1700000000000,
         1700000900000, 0))
    rows = [
        ("m1", "user", '{"role":"user","semantics":{"origin":"real_user"}}',
         '{"type":"text","text":"把收尾做完"}'),
        ("m2", "assistant", '{"role":"assistant"}',
         '{"type":"text","text":"好的，我先看现状。"}'),
        # 框架注入的噪声，导进来会误导新 Agent
        ("m3", "user", '{"role":"user","semantics":{"origin":"real_user"}}',
         '{"type":"text","text":"# Environment\\n - Primary working directory: X"}'),
        # 工具回灌的 user 消息，不是人说的
        ("m4", "user", '{"role":"user","semantics":{"origin":"tool"}}',
         '{"type":"text","text":"tool output should not appear"}'),
    ]
    for index, (mid, role, meta, part) in enumerate(rows):
        connection.execute("INSERT INTO message VALUES (?,?,?,?,?)",
                           (mid, "sess_abc123", 1700000000000 + index, index, meta))
        connection.execute("INSERT INTO part VALUES (?,?,?,?,?,?)",
                           (f"p{index}", mid, "sess_abc123", 1700000000000 + index,
                            index, part))
    connection.commit()
    connection.close()
    return root


class ZCodeSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sf-zcode-")
        self.addCleanup(tmp.cleanup)
        self.home = _zcode_fixture(Path(tmp.name))

    def test_list_sessions(self) -> None:
        items = external.list_zcode_sessions(home=self.home)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "旧项目的收尾")
        self.assertEqual(items[0]["directory"], "D:/work/old")
        self.assertFalse(items[0]["resumable"])
        self.assertEqual(items[0]["source"], "zcode")

    def test_transcript_filters_noise(self) -> None:
        turns = external.zcode_transcript(session_id="sess_abc123", home=self.home)
        texts = [t["text"] for t in turns]
        self.assertEqual(texts, ["把收尾做完", "好的，我先看现状。"])
        # 环境说明与工具回灌都不该出现
        joined = "\n".join(texts)
        self.assertNotIn("# Environment", joined)
        self.assertNotIn("tool output", joined)

    def test_missing_db_returns_empty(self) -> None:
        empty = Path(tempfile.mkdtemp(prefix="sf-nodb-"))
        self.addCleanup(lambda: None)
        self.assertFalse(external.zcode_available(home=empty))
        self.assertEqual(external.list_zcode_sessions(home=empty), [])
        self.assertEqual(external.zcode_transcript(session_id="x", home=empty), [])

    def test_secrets_are_counted(self) -> None:
        turns = [{"role": "user", "text": "密码 abc12345 还有 sk-abcdefghijklmnopqrst"}]
        self.assertGreaterEqual(external.scan_secrets(turns), 2)


class ZCodeImportTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sf-import-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = _zcode_fixture(self.root / "home")
        self.db = self.root / "state.db"
        self.store = open_store(self.db)
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)

    def test_import_creates_agent_and_context_file(self) -> None:
        result = external.import_zcode_session(
            self.store, session_id="sess_abc123", home=self.home)
        agent = result["agent"]
        self.assertEqual(result["turns"], 2)
        self.assertFalse(result["resumable"])
        self.assertIn("IMPORTED-SESSION.md", result["context_file"])
        context = Path(result["context_file"])
        self.assertTrue(context.exists())
        body = context.read_text(encoding="utf-8")
        self.assertIn("把收尾做完", body)
        self.assertIn("sess_abc123", body)
        # 角色卡必须说明"你不是 ZCode 本身"
        self.assertIn("不是 ZCode 进程本身", agent["system_prompt"])
        self.assertIn("IMPORTED-SESSION.md", agent["system_prompt"])

    def test_import_rejects_empty_session(self) -> None:
        with self.assertRaises(ValueError):
            external.import_zcode_session(self.store, session_id="sess_missing",
                                         home=self.home)


class ClusterTaskViewTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sf-taskview-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = open_store(self.root / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.agent = studio_agents.create_agent(
            self.store, name="写手", workspace=str(self.root / "ws"))
        self.cluster = studio_store.create_cluster(
            self.store, name="演示集群", objective="验证任务看板",
            workspace_root=str(self.root / "cluster"),
            project_id=self.agent["project_id"])
        studio_store.add_role(
            self.store, cluster_id=self.cluster["id"], name="写手",
            agent_id="role-0001", local_agent_id=self.agent["id"],
            workspace=str(self.root / "ws"))

    def test_task_list_carries_role_and_artifact_path(self) -> None:
        task = base.create_task(self.store, project_id=self.agent["project_id"],
                                title="写说明", description="产出说明.md")
        self.store.db.execute(
            "UPDATE tasks SET assignee = ? WHERE id = ?", ("role-0001", str(task["id"])))
        self.store.db.commit()
        workspace = Path(self.agent["workspace"])
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "说明.md").write_text("内容", encoding="utf-8")
        base.publish_artifact(self.store, project_id=self.agent["project_id"],
                              workspace=str(workspace), relative_path="说明.md",
                              agent_id=self.agent["id"], task_id=task["id"])

        result = tasks_view.list_cluster_tasks(self.store)
        self.assertEqual(result["summary"]["tasks"], 1)
        row = result["tasks"][0]
        self.assertEqual(row["role_name"], "写手")
        self.assertEqual(row["cluster_name"], "演示集群")
        self.assertEqual(len(row["artifacts"]), 1)
        artifact = row["artifacts"][0]
        self.assertTrue(artifact["local_path"], "成果必须带本地路径")
        self.assertTrue(Path(artifact["local_path"]).exists())

    def test_edit_task_text_does_not_touch_status(self) -> None:
        task = base.create_task(self.store, project_id=self.agent["project_id"],
                                title="旧标题", description="旧描述")
        before = self.store.get_task(task["id"])
        result = tasks_view.update_task_text(self.store, str(task["id"]),
                                             title="新标题", description="新描述")
        self.assertEqual(result["task"]["title"], "新标题")
        self.assertEqual(result["task"]["description"], "新描述")
        after = self.store.get_task(task["id"])
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.assignee, before.assignee)

    def test_edit_rejects_status_and_assignee(self) -> None:
        task = base.create_task(self.store, project_id=self.agent["project_id"],
                               title="任务", description="")
        for field in ("status", "assignee", "priority"):
            with self.assertRaises(StudioError) as ctx:
                tasks_view.update_task_text(self.store, str(task["id"]), **{field: "x"})
            self.assertEqual(ctx.exception.code, "task_field_not_editable")

    def test_edit_rejects_empty_title(self) -> None:
        task = base.create_task(self.store, project_id=self.agent["project_id"],
                                title="任务", description="")
        with self.assertRaises(StudioError) as ctx:
            tasks_view.update_task_text(self.store, str(task["id"]), title="   ")
        self.assertEqual(ctx.exception.code, "task_title_required")

    def test_unknown_artifact_path_reports_error(self) -> None:
        # 未知 id 必须是 400 级业务错误（artifact_not_found），不能冒成 500
        with self.assertRaises(StudioError) as ctx:
            tasks_view.artifact_local_path(self.store, "11111111-1111-1111-1111-111111111111")
        self.assertEqual(ctx.exception.code, "artifact_not_found")


class RevealTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="sf-reveal-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = open_store(self.root / "state.db")
        self.addCleanup(self.store.close)
        studio_store.ensure_schema(self.store)
        self.agent = studio_agents.create_agent(
            self.store, name="写手", workspace=str(self.root / "ws"))

    def test_enabled_only_for_local_desktop(self) -> None:
        self.assertTrue(reveal.reveal_enabled(host="127.0.0.1", base_path=""))
        # 反代部署：子路径或非回环绑定，一律关掉
        self.assertFalse(reveal.reveal_enabled(host="127.0.0.1", base_path="/studio"))
        self.assertFalse(reveal.reveal_enabled(host="0.0.0.0", base_path=""))

    def test_rejects_api_path_outside_workspaces(self) -> None:
        outside = self.root / "outside.txt"
        outside.write_text("x", encoding="utf-8")
        with self.assertRaises(StudioError) as ctx:
            reveal.reveal(self.store, str(outside), host="127.0.0.1")
        self.assertEqual(ctx.exception.code, "path_not_allowed")

    def test_rejects_missing_path(self) -> None:
        with self.assertRaises(StudioError) as ctx:
            reveal.reveal(self.store, str(self.root / "nope"), host="127.0.0.1")
        self.assertEqual(ctx.exception.code, "path_not_found")

    def test_rejects_when_deployment_mode(self) -> None:
        ws = Path(self.agent["workspace"])
        ws.mkdir(parents=True, exist_ok=True)
        with self.assertRaises(StudioError) as ctx:
            reveal.reveal(self.store, str(ws), host="127.0.0.1", base_path="/studio")
        self.assertEqual(ctx.exception.code, "reveal_unavailable")


if __name__ == "__main__":
    unittest.main()
