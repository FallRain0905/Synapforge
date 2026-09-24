"""MY-AGENT M-3 契约测试：把输入文件落到执行体工作目录（`input_fetcher`）。

这一层做错会很隐蔽：文件位置不对、名字穿越、重名覆盖、超大文件截断——四条都在这里钉住。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = AGENT_ROOT.parent.parent
for candidate in (str(REPOSITORY_ROOT), str(AGENT_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from input_fetcher import INPUT_DIR_NAME, InputFetchError, materialize_inputs, prompt_with_inputs  # noqa: E402


class FakeClient:
    """替身：按 artifact_id 返回内容（也可以在名字里指定报错）。"""

    def __init__(self, payloads: dict[str, tuple[bytes, str] | Exception]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def download(self, artifact_id: str, *, max_bytes: int = 0):
        self.calls.append(artifact_id)
        value = self.payloads[artifact_id]
        if isinstance(value, Exception):
            raise value
        return value


class MaterializeInputsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def inputs_dir(self) -> Path:
        return self.workspace / INPUT_DIR_NAME

    def test_platform_name_wins_over_the_served_one(self) -> None:
        """平台给的名字优先：响应头里的名字可能只是 ASCII 兜底（中文名会退化成 artifact.md）。"""

        client = FakeClient({"a1": (b"hello", "artifact.md")})
        written, failed = materialize_inputs(
            client, [{"artifact_id": "a1", "name": "错别字样例.md"}], workspace=self.workspace, log=lambda _m: None
        )
        self.assertEqual((written, failed), (["错别字样例.md"], []))
        self.assertEqual((self.inputs_dir() / "错别字样例.md").read_bytes(), b"hello")

    def test_served_name_is_used_when_the_platform_has_none(self) -> None:
        """任务侧平台不给名字（只有 id）——这时才用响应头里的真名。"""

        client = FakeClient({"a1": (b"hello", "报告.md")})
        written, _ = materialize_inputs(
            client, [{"artifact_id": "a1", "name": ""}], workspace=self.workspace, log=lambda _m: None
        )
        self.assertEqual(written, ["报告.md"])

    def test_same_name_does_not_overwrite(self) -> None:
        client = FakeClient({"a1": (b"one", "notes.md"), "a2": (b"two", "notes.md")})
        written, failed = materialize_inputs(
            client,
            [{"artifact_id": "a1", "name": "notes.md"}, {"artifact_id": "a2", "name": "notes.md"}],
            workspace=self.workspace,
            log=lambda _m: None,
        )
        self.assertEqual(failed, [])
        self.assertEqual(sorted(written), ["notes-2.md", "notes.md"])
        self.assertEqual((self.inputs_dir() / "notes.md").read_bytes(), b"one")
        self.assertEqual((self.inputs_dir() / "notes-2.md").read_bytes(), b"two")

    def test_path_traversal_is_stripped(self) -> None:
        client = FakeClient({"a1": (b"x", "")})
        written, _ = materialize_inputs(
            client, [{"artifact_id": "a1", "name": "../../etc/passwd"}], workspace=self.workspace, log=lambda _m: None
        )
        self.assertEqual(written, ["passwd"])
        self.assertTrue((self.inputs_dir() / "passwd").exists())
        self.assertFalse((self.workspace.parent / "etc").exists())

    def test_missing_name_falls_back_to_the_id(self) -> None:
        client = FakeClient({"abcdef1234567890": (b"x", "")})
        written, _ = materialize_inputs(
            client, [{"artifact_id": "abcdef1234567890", "name": ""}], workspace=self.workspace, log=lambda _m: None
        )
        self.assertEqual(written, ["input-abcdef12"])

    def test_failures_are_reported_not_hidden(self) -> None:
        client = FakeClient({"a1": InputFetchError("http_404"), "a2": (b"ok", "fine.md")})
        written, failed = materialize_inputs(
            client,
            [{"artifact_id": "a1", "name": "gone.md"}, {"artifact_id": "a2", "name": "fine.md"}],
            workspace=self.workspace,
            log=lambda _m: None,
        )
        self.assertEqual(written, ["fine.md"])
        self.assertEqual(failed, ["gone.md: http_404"])

    def test_prompt_mentions_inputs_and_missing_files(self) -> None:
        text = prompt_with_inputs("把错别字改掉", ["报告.md"], ["gone.md: http_404"])
        self.assertIn("inputs/", text)
        self.assertIn("报告.md", text)
        self.assertIn("没有取到", text)
        self.assertTrue(text.endswith("把错别字改掉"))
        self.assertEqual(prompt_with_inputs("原样", [], []), "原样")


if __name__ == "__main__":
    unittest.main()
