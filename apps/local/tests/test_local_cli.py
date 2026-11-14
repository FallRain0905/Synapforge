"""CLI 行为测试。

重点锁住一个 argparse 陷阱：`--state` / `--human` 定义在共享 parent 上，
子解析器会用**自身默认值**覆盖写在子命令之前的值——必须用 `SUPPRESS` 才能两个位置都生效。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from synapforge import commands
from synapforge.cli import main
from synapforge.db import open_store


class CliFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-cli-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "state.db"
        store = open_store(self.db)
        self.pid = commands.ensure_project(store, "cli-proj")["id"]
        commands.ensure_participant(store, project_id=self.pid, agent_id="agent-cli")
        commands.create_task(store, project_id=self.pid, title="给 cli 的活",
                             assignee_agent_id="agent-cli")
        store.close()

    def _run(self, argv: list[str]) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = main(argv)
        return code, buffer.getvalue()

    def _inbox_args(self) -> list[str]:
        return ["inbox", "--agent", "agent-cli", "--project", self.pid]

    def test_human_flag_works_before_subcommand(self) -> None:
        code, out = self._run(["--state", str(self.db), "--human", *self._inbox_args()])
        self.assertEqual(code, 0)
        self.assertIn("派给我（还没领）", out)
        self.assertNotIn('"agent_id"', out)

    def test_human_flag_works_after_subcommand(self) -> None:
        code, out = self._run(["--state", str(self.db), *self._inbox_args(), "--human"])
        self.assertEqual(code, 0)
        self.assertIn("派给我（还没领）", out)

    def test_default_output_is_json(self) -> None:
        code, out = self._run(["--state", str(self.db), *self._inbox_args()])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["agent_id"], "agent-cli")
        self.assertEqual([t["title"] for t in payload["assigned_to_me"]], ["给 cli 的活"])

    def test_state_flag_works_before_subcommand(self) -> None:
        """`--state` 同样会被子解析器默认值吃掉——这条一起锁住。"""

        code, out = self._run(["--state", str(self.db), "status", "--project", self.pid])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["project_id"], self.pid)

    def test_task_create_accepts_assignee_agent(self) -> None:
        code, out = self._run(["--state", str(self.db), "task-create",
                               "--project", self.pid, "--title", "新派的活",
                               "--assignee-agent", "agent-cli"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["assignee"], "agent-cli")
        self.assertEqual(payload["assignee_member_id"], commands.agent_member_id("agent-cli"))

    def test_error_is_json_with_code(self) -> None:
        code, out = self._run(["--state", str(self.db), "claim",
                               "--project", "not-a-uuid", "--agent", "agent-cli"])
        self.assertNotEqual(code, 0)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertIn("code", payload)


if __name__ == "__main__":
    unittest.main()
