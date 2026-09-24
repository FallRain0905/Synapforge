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
import tempfile
import unittest
from pathlib import Path

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


class WorkspaceArgumentTests(unittest.TestCase):
    """FM-0：工作目录必须能被显式传给内核，不能在入口里硬写成 `Path.cwd()`。

    修复前的行为：`_daemon_args` 无条件写 `workspace=str(Path.cwd())`。打包成 exe 后进程的 cwd 是
    安装目录（`resources\\sidecar`），于是"工作区"落在程序安装目录里——只读、用户也找不到自己的文件；
    而且因为命令行上永远"有值"，`daemon_run` 里"读 platform.json 记住的工作区"那条路等于死代码。
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.state_dir = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _args(self, workspace: str | None) -> object:
        import argparse

        return argparse.Namespace(
            url=None,
            state_dir=str(self.state_dir),
            contract_only=False,
            device_token=None,
            workspace=workspace,
            port=None,
        )

    def test_cli_workspace_wins_and_is_forwarded(self) -> None:
        chosen = str(self.state_dir / "我的工作区")
        daemon_args = sidecar_entry._daemon_args(self._args(chosen))
        self.assertEqual(daemon_args.workspace, chosen)

    def test_remembered_workspace_is_used_when_the_shell_gives_none(self) -> None:
        remembered = str(self.state_dir / "remembered")
        (self.state_dir / "platform.json").write_text(
            '{"url": "https://synapforge.top", "device_id": "d1", "workspace": "%s"}' % remembered.replace("\\", "\\\\"),
            encoding="utf-8",
        )
        daemon_args = sidecar_entry._daemon_args(self._args(None))
        self.assertEqual(daemon_args.workspace, remembered)

    def test_nothing_known_leaves_it_to_the_kernel(self) -> None:
        """两边都不知道时不在这里编一个值：让 `daemon_run` 按"命令行 > platform.json > 当前目录"决定。"""

        daemon_args = sidecar_entry._daemon_args(self._args(None))
        self.assertIsNone(daemon_args.workspace)

    def test_workspace_flag_exists_on_the_parser(self) -> None:
        """入口的参数解析必须认 `--workspace`（壳就是用它把用户选的目录传进来的）。"""

        source = Path(sidecar_entry.__file__).read_text(encoding="utf-8")
        self.assertIn('"--workspace"', source)


if __name__ == "__main__":
    unittest.main()