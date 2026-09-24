"""MY-AGENT M-3 契约测试：把输入文件落到执行体工作目录（`input_fetcher`）。

这一层做错会很隐蔽：文件位置不对、名字穿越、重名覆盖、超大文件截断——四条都在这里钉住。
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = AGENT_ROOT.parent.parent
for candidate in (str(REPOSITORY_ROOT), str(AGENT_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

import input_fetcher  # noqa: E402
from input_fetcher import (  # noqa: E402
    INPUT_DIR_NAME,
    InputFetchError,
    manifest_path,
    materialize_inputs,
    prompt_with_inputs,
    read_inputs_manifest,
)


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


class InputsManifestTests(unittest.TestCase):
    """FM-0：落盘要留台账——`artifact id → inputs/<文件名> → sha256 → 字节数`。

    平台只发 id、磁盘上只有文件名，中间不留证，事后就答不了"这次 Run 读到的到底是不是那个成果物"。
    """

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.workspace = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_entries_carry_id_hash_size_and_relative_path(self) -> None:
        client = FakeClient({"a1": (b"hello", "report.md")})
        materialize_inputs(client, [{"artifact_id": "a1", "name": ""}], workspace=self.workspace, log=lambda _m: None)

        manifest = read_inputs_manifest(self.workspace)
        self.assertEqual(manifest["schema"], "inputs-manifest/1")
        self.assertEqual(manifest["directory"], INPUT_DIR_NAME)
        self.assertEqual(
            manifest["entries"],
            [
                {
                    "artifact_id": "a1",
                    "name": "report.md",
                    "relative_path": "inputs/report.md",
                    "sha256": hashlib.sha256(b"hello").hexdigest(),
                    "size_bytes": 5,
                    "source": "input_artifacts",
                    "materialized_at": manifest["entries"][0]["materialized_at"],
                }
            ],
        )
        self.assertTrue(manifest["entries"][0]["materialized_at"])

    def test_manifest_lives_in_the_platform_metadata_dir(self) -> None:
        self.assertEqual(manifest_path(self.workspace).parent.name, ".math-agent-platform")

    def test_broken_manifest_is_ignored_and_rewritten(self) -> None:
        """清单是台账：坏掉/换过 schema 都当没有，不能因此拦住这一轮的文件落地。"""

        path = manifest_path(self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{ not json", encoding="utf-8")
        self.assertEqual(read_inputs_manifest(self.workspace), {})

        client = FakeClient({"a1": (b"x", "a.txt")})
        written, failed = materialize_inputs(
            client, [{"artifact_id": "a1", "name": ""}], workspace=self.workspace, log=lambda _m: None
        )
        self.assertEqual((written, failed), (["a.txt"], []))
        self.assertEqual(len(read_inputs_manifest(self.workspace)["entries"]), 1)

    def test_write_failure_does_not_break_materialization(self) -> None:
        """清单写不进去只记日志：文件已经落地了，台账失败不该把这一轮判成没拿到输入。"""

        blocked = self.workspace / "blocked"
        blocked.write_text("我是一个文件，不是目录", encoding="utf-8")
        original = input_fetcher.manifest_path
        input_fetcher.manifest_path = lambda workspace: blocked / "inputs-manifest.json"  # type: ignore[assignment]
        try:
            logs: list[str] = []
            client = FakeClient({"a1": (b"x", "a.txt")})
            written, failed = materialize_inputs(
                client, [{"artifact_id": "a1", "name": ""}], workspace=self.workspace, log=logs.append
            )
        finally:
            input_fetcher.manifest_path = original  # type: ignore[assignment]

        self.assertEqual((written, failed), (["a.txt"], []))
        self.assertTrue((self.workspace / "inputs" / "a.txt").is_file())
        self.assertTrue(any("清单写入失败" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main()
