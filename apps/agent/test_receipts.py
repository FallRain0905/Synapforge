"""receipt 生成器的契约测试（`receipts.py`，口径全在 `docs/RECEIPT_FORMAT.md` §2）。

盯五件事：
1. 两个 hash 都是 SHA-256 前 16 位小写 hex（`^[0-9a-f]{16}$`）；
2. `args_hash` 对规范化 JSON：键序无关、中文不转义、同输入同输出；
3. `output_hash` 是内容完整 SHA-256 的**前 16 位**（大小写归一）；
4. 可选字段如实：`truncated` 只在为真时出现；`source_artifact_hashes` 只在给了时出现；
5. `created_at` 是带时区的 ISO-8601；`tool_name/tool_call_id` 超限截断到 128。
"""

from __future__ import annotations

import hashlib
import sys
import unittest
from datetime import datetime
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = AGENT_ROOT.parent.parent
for candidate in (str(REPOSITORY_ROOT), str(AGENT_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from receipts import RECEIPT_VERSION, build_file_receipt, canonical_json_hash, short_hash  # noqa: E402


class ShortHashTests(unittest.TestCase):
    def test_matches_sha256_prefix(self) -> None:
        self.assertEqual(short_hash(b"hello"), hashlib.sha256(b"hello").hexdigest()[:16])

    def test_is_lowercase_16_hex(self) -> None:
        digest = short_hash(b"\x00\xff")
        self.assertRegex(digest, r"^[0-9a-f]{16}$")


class CanonicalJsonHashTests(unittest.TestCase):
    def test_key_order_does_not_matter(self) -> None:
        self.assertEqual(canonical_json_hash({"a": 1, "b": 2}), canonical_json_hash({"b": 2, "a": 1}))

    def test_nested_and_unicode_are_stable(self) -> None:
        value = {"path": "成果物/新文件.md", "opts": {"sort": True, "n": 3}}
        self.assertEqual(canonical_json_hash(value), canonical_json_hash({"opts": {"n": 3, "sort": True}, "path": "成果物/新文件.md"}))
        self.assertRegex(canonical_json_hash(value), r"^[0-9a-f]{16}$")


class BuildFileReceiptTests(unittest.TestCase):
    def receipt(self, **overrides):
        kwargs = dict(
            tool_name="workspace_diff",
            tool_call_id="turn-abc",
            args={"path": "out/report.md"},
            content_sha256_hex=hashlib.sha256(b"file bytes").hexdigest(),
            output_bytes=10,
        )
        kwargs.update(overrides)
        return build_file_receipt(**kwargs)

    def test_shape_per_receipt_format_v1(self) -> None:
        receipt = self.receipt()
        self.assertEqual(receipt["receipt_version"], RECEIPT_VERSION)
        self.assertEqual(receipt["receipt_version"], 1)
        self.assertEqual(receipt["tool_name"], "workspace_diff")
        self.assertEqual(receipt["tool_call_id"], "turn-abc")
        self.assertEqual(receipt["output_bytes"], 10)
        self.assertEqual(receipt["status"], "success")
        for key in ("args_hash", "output_hash"):
            self.assertRegex(receipt[key], r"^[0-9a-f]{16}$")

    def test_output_hash_is_lowercased_prefix_of_content_sha256(self) -> None:
        full = hashlib.sha256(b"file bytes").hexdigest().upper()
        receipt = self.receipt(content_sha256_hex=full)
        self.assertEqual(receipt["output_hash"], full.lower()[:16])

    def test_created_at_is_timezone_aware_iso8601(self) -> None:
        parsed = datetime.fromisoformat(self.receipt()["created_at"])
        self.assertIsNotNone(parsed.tzinfo)

    def test_optional_fields_omitted_until_true_or_given(self) -> None:
        self.assertNotIn("truncated", self.receipt())
        self.assertNotIn("source_artifact_hashes", self.receipt())
        truncated = self.receipt(truncated=True, output_bytes=4, source_artifact_hashes=["a" * 16])
        self.assertTrue(truncated["truncated"])
        self.assertEqual(truncated["source_artifact_hashes"], ["a" * 16])

    def test_oversized_fields_are_truncated_to_128(self) -> None:
        receipt = self.receipt(tool_name="t" * 500, tool_call_id="c" * 500)
        self.assertEqual(len(receipt["tool_name"]), 128)
        self.assertEqual(len(receipt["tool_call_id"]), 128)


if __name__ == "__main__":
    unittest.main()
