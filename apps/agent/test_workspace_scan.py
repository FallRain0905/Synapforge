"""工作区快照与差分的契约测试（D-CL-2）。

三件容易做错的事：
1. **剪枝**：依赖目录、构建产物、平台自己的运行文件不能当产出；
2. **判定**：(大小, mtime) 任一变化即修改；删除不算产出；
3. **上限**：文件数超限要如实置 `truncated`，不假装扫全了。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from apps.agent.workspace_scan import SnapshotDiff, diff, is_excluded, snapshot


class PruningTests(unittest.TestCase):
    def test_dependency_and_build_dirs_are_excluded(self) -> None:
        for relative in (
            "node_modules/pkg/index.js",
            ".git/HEAD",
            ".next/server/page.js",
            "dist/bundle.js",
            "dist-sidecar/kernel/app.exe",
            "apps/api/__pycache__/main.pyc",
            ".venv/lib/site.py",
            ".math-agent-platform/answers/run-1.md",
        ):
            with self.subTest(relative=relative):
                self.assertTrue(is_excluded(relative))

    def test_outputs_and_sources_are_not_excluded(self) -> None:
        for relative in (
            "paper/main.tex",
            "figures/trend.png",
            "code/solve.py",
            "results/summary.csv",
            "notes/answer.md",
        ):
            with self.subTest(relative=relative):
                self.assertFalse(is_excluded(relative))

    def test_local_state_files_and_logs_are_excluded(self) -> None:
        for relative in ("platform.json", "worker.json", "run.log", "data/cache.db", "tmp/x.tmp"):
            with self.subTest(relative=relative):
                self.assertTrue(is_excluded(relative))

    def test_platform_downloaded_inputs_are_not_outputs(self) -> None:
        """FM-0：平台下发的输入不算这一轮的产出。

        以前任务通道只排依赖/构建目录，`inputs/` 里的文件会被 diff 当成"新增"**重新上传成成果物**：
        同一份文件在项目里出现两次（原件 + "产物"），看起来像 Agent 又生成了一份东西。
        """

        for relative in ("inputs/题目原文.txt", "inputs/nested/data.csv", "user_data/seed.csv"):
            with self.subTest(relative=relative):
                self.assertTrue(is_excluded(relative))

    def test_only_top_level_input_dirs_are_excluded(self) -> None:
        """工具自己建的 `src/inputs/` 仍然是产出——排除只认顶层（平台下发的落点就是顶层）。"""

        for relative in ("src/inputs/data.csv", "code/inputs/seed.py", "outputs/inputs.csv"):
            with self.subTest(relative=relative):
                self.assertFalse(is_excluded(relative))


class SnapshotDiffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write(self, relative: str, content: str) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def test_created_and_modified_are_reported_deleted_is_not(self) -> None:
        self.write("keep.txt", "same")
        self.write("edit.txt", "before")
        self.write("gone.txt", "bye")
        before, before_truncated = snapshot(self.root)

        self.write("edit.txt", "after-longer")
        self.write("new/answer.md", "# 回答")
        (self.root / "gone.txt").unlink()

        after, after_truncated = snapshot(self.root)
        result = diff(before, after, before_truncated=before_truncated, after_truncated=after_truncated)

        self.assertEqual(result.created, ["new/answer.md"])
        self.assertEqual(result.modified, ["edit.txt"])
        self.assertEqual(result.deleted, ["gone.txt"])
        self.assertEqual(result.changed, ["edit.txt", "new/answer.md"])
        self.assertFalse(result.before_truncated or result.after_truncated)

    def test_same_size_rewrite_with_new_mtime_is_still_detected(self) -> None:
        """只比大小会漏掉"等长改写"——所以 mtime 也进指纹。"""

        path = self.write("equal.txt", "AAAA")
        before, _ = snapshot(self.root)
        path.write_text("BBBB", encoding="utf-8")
        os.utime(path, (path.stat().st_atime + 5, path.stat().st_mtime + 5))
        after, _ = snapshot(self.root)
        self.assertEqual(diff(before, after).modified, ["equal.txt"])

    def test_walk_prunes_excluded_directories(self) -> None:
        self.write("node_modules/pkg/index.js", "x" * 100)
        self.write(".git/objects/ab/cdef", "x")
        self.write("paper/main.tex", "\\documentclass{article}")
        stamps, _ = snapshot(self.root)
        self.assertEqual(sorted(stamps), ["paper/main.tex"])

    def test_inputs_downloaded_mid_run_do_not_become_outputs(self) -> None:
        """真流程的样子：先拍快照（此时还没有输入），跑之前平台把输入下到 inputs/，跑完再拍。

        产物只能是自己写出来的 `answer.md`——把输入也收进去就成了"Agent 生成了一份题目原文"。
        """

        before, _ = snapshot(self.root)
        self.write("inputs/题目原文.txt", "问题一……")
        self.write(".math-agent-platform/inputs-manifest.json", "{}")
        self.write("answer.md", "# 回答")

        after, _ = snapshot(self.root)
        self.assertEqual(diff(before, after).changed, ["answer.md"])

    def test_max_files_truncates_and_says_so(self) -> None:
        for index in range(12):
            self.write(f"notes/{index:02d}.md", "x")
        stamps, truncated = snapshot(self.root, max_files=5)
        self.assertEqual(len(stamps), 5)
        self.assertTrue(truncated)

    def test_missing_workspace_is_empty_not_an_error(self) -> None:
        stamps, truncated = snapshot(self.root / "does-not-exist")
        self.assertEqual(stamps, {})
        self.assertFalse(truncated)
        self.assertEqual(diff(stamps, stamps).changed, [])

    def test_empty_diff_serialises_without_lying(self) -> None:
        payload = SnapshotDiff().as_dict()
        self.assertEqual(payload["changed"], [])
        self.assertFalse(payload["before_truncated"])
        self.assertFalse(payload["after_truncated"])


if __name__ == "__main__":
    unittest.main()