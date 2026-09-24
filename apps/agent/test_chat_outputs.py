"""对话产出采集的契约测试（`chat_outputs.py`）。

盯四件事：
1. **只收这一轮新出/改过的文件**（快照差分；未变的文件不重复上传）；
2. **输入目录不算产出**（`inputs/` 是平台下发的，不该被当成"执行体生成的东西"回传）；
3. **上限如实记账**（超大小只登记不上传、超过单轮数量留在工作区），不静默丢弃；
4. **上传失败不改这一轮的成败**（与 CL-1 的 I4 同一口径）：只记错误。
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

from chat_outputs import ChatOutputCollector  # noqa: E402


class FakeArtifactClient:
    """假成果物客户端：记下调用，可按名字注入失败。"""

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.created: list[dict] = []
        self.uploaded: list[tuple[str, str]] = []
        self.keys: list[str] = []
        self.fail_on = fail_on or set()

    def _request(self, method: str, path: str, *, body=None, headers=None):  # noqa: ANN001
        payload = __import__("json").loads(body.decode("utf-8"))
        key = (headers or {}).get("Idempotency-Key", "")
        self.keys.append(key)
        if payload["name"] in self.fail_on:
            raise RuntimeError("artifact_http_500")
        self.created.append({"path": path, "payload": payload})
        return {"id": f"artifact-{len(self.created)}"}

    def upload_content(self, artifact_id: str, output) -> dict:  # noqa: ANN001
        self.uploaded.append((artifact_id, output.relative_path))
        return {"id": artifact_id}


class ChatOutputCollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name)
        (self.workspace / "inputs").mkdir()
        (self.workspace / "inputs" / "seed.md").write_text("输入", encoding="utf-8")
        (self.workspace / "old.md").write_text("旧文件", encoding="utf-8")
        self.client = FakeArtifactClient()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def collector(self, **kwargs) -> ChatOutputCollector:
        return ChatOutputCollector(
            workspace=self.workspace,
            url="http://platform.test",
            project_id="p1",
            agent_id="a1",
            project_token="t" * 24,
            log=lambda _m: None,
            **{"client": self.client, **kwargs},
        )

    def test_collects_only_the_new_and_changed_files(self) -> None:
        collector = self.collector()
        collector.snapshot()
        (self.workspace / "new.md").write_text("新文件", encoding="utf-8")
        (self.workspace / "old.md").write_text("改了", encoding="utf-8")
        result = collector.collect("turn-1")

        self.assertEqual(sorted(item.name for item in result.outputs), ["new.md", "old.md"])
        self.assertEqual(len(self.client.created), 2)
        self.assertEqual(sorted(item.relative_path for item in result.outputs), ["new.md", "old.md"])
        # 幂等键按轮次 + 路径 + 内容算：不同轮次各成一份，不会被判成重复键冲突
        self.assertTrue(all(key.startswith("artifact-create:chat:turn-1:") for key in self.client.keys))
        payloads = {item["payload"]["name"]: item["payload"] for item in self.client.created}
        self.assertIsNone(payloads["new.md"].get("run_id"))
        self.assertEqual(payloads["new.md"]["status"], "PENDING_REVIEW")

    def test_unchanged_files_are_not_re_uploaded(self) -> None:
        collector = self.collector()
        collector.snapshot()
        result = collector.collect("turn-1")
        self.assertEqual(result.outputs, [])
        self.assertEqual(self.client.created, [])

    def test_input_files_and_excluded_paths_are_not_outputs(self) -> None:
        collector = self.collector()
        collector.snapshot()
        # 输入目录里的文件被改动、以及被排除的产物，都不该当产出
        (self.workspace / "inputs" / "seed.md").write_text("执行体动了输入文件", encoding="utf-8")
        (self.workspace / "render.log").write_text("日志", encoding="utf-8")
        (self.workspace / "__pycache__").mkdir()
        (self.workspace / "__pycache__" / "x.pyc").write_bytes(b"\x00")
        result = collector.collect("turn-1")
        self.assertEqual(result.outputs, [])

    def test_too_large_files_are_recorded_not_uploaded(self) -> None:
        collector = self.collector(max_bytes=4)
        collector.snapshot()
        (self.workspace / "big.bin").write_bytes(b"0123456789")
        result = collector.collect("turn-1")
        self.assertEqual(result.outputs, [])
        self.assertEqual(result.skipped_too_large, ["big.bin"])
        self.assertIn("超过上传上限", result.note())

    def test_turn_output_count_is_capped_and_recorded(self) -> None:
        collector = self.collector(max_outputs=2)
        collector.snapshot()
        for index in range(4):
            (self.workspace / f"out-{index}.md").write_text(f"内容 {index}", encoding="utf-8")
        result = collector.collect("turn-1")
        self.assertEqual(len(result.outputs), 2)
        self.assertEqual(sorted(result.skipped_over_limit), ["out-2.md", "out-3.md"])
        self.assertIn("单轮数量上限", result.note())

    def test_upload_failure_is_recorded_and_does_not_raise(self) -> None:
        failing = FakeArtifactClient(fail_on={"boom.md"})
        collector = self.collector(client=failing)
        collector.snapshot()
        (self.workspace / "boom.md").write_text("会失败", encoding="utf-8")
        (self.workspace / "ok.md").write_text("会成功", encoding="utf-8")
        result = collector.collect("turn-1")
        self.assertEqual([item.name for item in result.outputs], ["ok.md"])
        self.assertEqual(len(result.errors), 1)
        self.assertIn("boom.md", result.errors[0])

    def test_snapshot_failure_disables_collection_without_raising(self) -> None:
        collector = ChatOutputCollector(
            workspace=Path(self.temp_dir.name) / "not-a-dir",
            url="http://platform.test",
            project_id="p1",
            agent_id="a1",
            project_token="t" * 24,
            client=self.client,
            log=lambda _m: None,
        )
        collector.snapshot()  # 目录不存在：`snapshot` 返回空指纹，collect 不该炸
        result = collector.collect("turn-1")
        self.assertEqual(result.outputs, [])
        self.assertEqual(result.errors, [])


if __name__ == "__main__":
    unittest.main()