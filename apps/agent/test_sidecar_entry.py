"""内核打包入口的编码护栏（桌面端 0.2.0 首次运行验证发现的真 bug）。

现象：打包成 exe 后，Windows 上 stdout 默认是 cp1252。内核的日志**到处是中文**
（例如未配对时的 `no_project_grant` 提示），`print()` 一抛 `UnicodeEncodeError`
进程就退出 → 壳看到"内核反复崩溃"→ 重启 3 次后放弃 → 首装用户永远配不上对。

修法：`sidecar_entry._force_utf8_stdio()` 在 `main()` 最前面把 stdout/stderr 钉成 UTF-8。
这里用一个 cp1252 的流复现"修复前会炸、修复后能打"。
"""

from __future__ import annotations

import io
import sys
import unittest

import sidecar_entry


class StdioEncodingTests(unittest.TestCase):
    def test_force_utf8_stdio_makes_chinese_logs_safe(self) -> None:
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", write_through=True)
        original = sys.stdout
        sys.stdout = stream
        try:
            # 修复前：中文日志直接抛 UnicodeEncodeError（打包 exe 上就是"内核崩了"）
            with self.assertRaises(UnicodeEncodeError):
                print("未配对提示：先执行带 --grant 的首次接入")
            sidecar_entry._force_utf8_stdio()
            self.assertEqual(sys.stdout.encoding.lower().replace("-", ""), "utf8")
            print("未配对提示：先执行带 --grant 的首次接入")  # 不该再抛
            sys.stdout.flush()
        finally:
            sys.stdout = original
        self.assertIn("未配对提示", raw.getvalue().decode("utf-8"))

    def test_force_utf8_stdio_is_idempotent(self) -> None:
        sidecar_entry._force_utf8_stdio()
        sidecar_entry._force_utf8_stdio()
        self.assertTrue(sys.stdout.encoding)


if __name__ == "__main__":
    unittest.main()