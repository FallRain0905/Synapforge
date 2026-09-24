"""Codex 执行体的契约测试。

不打真实网络、不依赖本机是否装了 Codex：
- JSONL 解析覆盖正常事件、工具/文件事件、错误事件、坏行与 usage；
- CLI 探测用临时目录伪造解包结构（含"取最新版本"）；
- 命令组装断言安全约束（提示词在最后、拒绝 danger-full-access、拒绝危险 flag）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from apps.agent.cli_adapters import CliAdapterError, CodexAdapter
from apps.agent.codex_executor import (
    build_codex_command,
    detect_codex_cli,
    parse_codex_jsonl,
    summarize_codex_result,
)


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event) for event in events)


class ParseCodexJsonlTests(unittest.TestCase):
    def test_extracts_final_message_and_counts(self) -> None:
        raw = _jsonl(
            {"type": "thread.started", "thread_id": "t1"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": "先看文件"}},
            {"type": "item.completed", "item": {"id": "i1", "type": "tool_call", "name": "shell"}},
            {"type": "item.completed", "item": {"id": "i2", "type": "file_change", "path": "a.py"}},
            {"type": "item.completed", "item": {"id": "i3", "type": "agent_message", "text": "PLATFORM_CODEX_OK"}},
            {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}},
        )
        parsed = parse_codex_jsonl(raw)
        self.assertEqual(parsed["final_message"], "PLATFORM_CODEX_OK")
        self.assertEqual(parsed["messages"], ["先看文件", "PLATFORM_CODEX_OK"])
        self.assertEqual(parsed["tool_events"], 1)
        self.assertEqual(parsed["file_events"], 1)
        self.assertEqual(parsed["event_count"], 7)
        self.assertEqual(parsed["unparsed"], 0)
        self.assertEqual(parsed["usage"]["input_tokens"], 12)

    def test_error_events_are_split_into_fatal_and_transient(self) -> None:
        """Codex 自己会重试的瞬断提示不能算失败，但也不能被丢掉——分开记。"""

        raw = _jsonl(
            {"type": "error", "message": "Reconnecting... waiting for network"},
            {"type": "error", "message": "stream disconnected before completion: stream closed"},
            {"type": "turn.failed", "error": "connection failed"},
        )
        parsed = parse_codex_jsonl(raw)
        self.assertEqual(parsed["error_events"], ["connection failed"])
        self.assertEqual(
            parsed["transient_events"],
            [
                "Reconnecting... waiting for network",
                "stream disconnected before completion: stream closed",
            ],
        )
        self.assertEqual(parsed["final_message"], "")

    def test_recovered_transient_error_keeps_the_answer(self) -> None:
        raw = _jsonl(
            {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion)"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "答复已拿到"}},
            {"type": "turn.completed", "usage": {"input_tokens": 3}},
        )
        parsed = parse_codex_jsonl(raw)
        self.assertEqual(parsed["final_message"], "答复已拿到")
        self.assertEqual(parsed["error_events"], [])
        self.assertTrue(parsed["transient_events"])

    def test_unparsed_lines_are_counted_not_dropped(self) -> None:
        raw = "not-json\n" + _jsonl({"type": "turn.started"}) + "\n{broken"
        parsed = parse_codex_jsonl(raw)
        self.assertEqual(parsed["unparsed"], 2)
        self.assertEqual(parsed["event_count"], 1)

    def test_empty_output_is_safe(self) -> None:
        parsed = parse_codex_jsonl("")
        self.assertEqual(parsed["event_count"], 0)
        self.assertEqual(parsed["final_message"], "")


class DetectCodexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_explicit_path_wins_when_exists(self) -> None:
        exe = self.root / "codex.exe"
        exe.write_bytes(b"stub")
        self.assertEqual(detect_codex_cli(str(exe)), str(exe))

    def test_explicit_path_is_ignored_when_missing(self) -> None:
        with patch.object(os, "environ", {}):
            self.assertIsNone(detect_codex_cli(str(self.root / "nope.exe")))

    def test_env_var_and_desktop_bin_dir(self) -> None:
        env_exe = self.root / "env-codex.exe"
        env_exe.write_bytes(b"stub")
        with patch.object(os, "environ", {"CODEX_CLI_PATH": str(env_exe)}):
            self.assertEqual(detect_codex_cli(), str(env_exe))

        # 桌面端解包目录：LOCALAPPDATA/OpenAI/Codex/bin/<hash>/codex.exe，取最新
        older = self.root / "OpenAI" / "Codex" / "bin" / "aaa"
        newer = self.root / "OpenAI" / "Codex" / "bin" / "bbb"
        for directory in (older, newer):
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "codex.exe").write_bytes(b"stub")
        os.utime(older / "codex.exe", (1_600_000_000, 1_600_000_000))
        os.utime(newer / "codex.exe", (1_700_000_000, 1_700_000_000))
        with patch.object(os, "environ", {"LOCALAPPDATA": str(self.root)}), patch(
            "shutil.which", return_value=None
        ):
            self.assertEqual(detect_codex_cli(), str(newer / "codex.exe"))

    def test_returns_none_when_absent(self) -> None:
        with patch.object(os, "environ", {}), patch("shutil.which", return_value=None):
            self.assertIsNone(detect_codex_cli())


class BuildCodexCommandTests(unittest.TestCase):
    def test_prompt_stays_last_and_flags_are_safe(self) -> None:
        command = build_codex_command("codex.exe", "Reply with exactly: OK")
        self.assertEqual(command[0], "codex.exe")
        self.assertEqual(command[1], "exec")
        self.assertEqual(command[-1], "Reply with exactly: OK")
        self.assertIn("--json", command)
        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)
        self.assertIn("--skip-git-repo-check", command)
        # 安全约束：绝不出现危险的沙箱/审批绕过或路径逃逸参数
        for forbidden in (
            "--dangerously-bypass-approvals-and-sandbox",
            "--dangerously-bypass-hook-trust",
            "--approve-for-me",
            "--cd",
            "-C",
            "--add-dir",
            "-c",
            "--config",
        ):
            self.assertNotIn(forbidden, command)

    def test_danger_full_access_is_rejected(self) -> None:
        with self.assertRaises(CliAdapterError):
            build_codex_command("codex.exe", "x", sandbox="danger-full-access")

    def test_workspace_write_is_allowed(self) -> None:
        command = build_codex_command("codex.exe", "x", sandbox="workspace-write")
        self.assertIn("workspace-write", command)

    def test_adapter_rejects_unsafe_flags_in_supplied_command(self) -> None:
        """直接把带危险 flag 的命令交给 adapter 也必须被拒（防线不只在 worker 侧）。"""

        adapter = CodexAdapter(executable="codex.exe")
        with self.assertRaises(CliAdapterError):
            adapter.build_command(["codex.exe", "exec", "--dangerously-bypass-approvals-and-sandbox", "x"])


class WorkerCodexBranchTests(unittest.TestCase):
    def test_task_executor_detection_and_prompt(self) -> None:
        sys.path.insert(0, str(_REPOSITORY_ROOT / "apps" / "agent"))
        import agentd

        codex_task = {"resource_policy": {"worker_executor": "codex", "worker_prompt": "do it"}}
        self.assertTrue(agentd._is_codex_executor(codex_task))
        self.assertEqual(agentd._codex_prompt(codex_task), "do it")

        plain = {"resource_policy": {"worker_command": ["python", "-c", "print(1)"]}}
        self.assertFalse(agentd._is_codex_executor(plain))

        # 未声明 worker_prompt 时用标题+说明+完成标准兜底
        fallback = {
            "title": "写一份摘要",
            "description": "基于数据",
            "acceptance_criteria": ["含结论"],
            "resource_policy": {"worker_executor": "codex"},
        }
        prompt = agentd._codex_prompt(fallback)
        self.assertIn("写一份摘要", prompt)
        self.assertIn("含结论", prompt)

    def test_executor_environment_is_allowlisted_and_present(self) -> None:
        sys.path.insert(0, str(_REPOSITORY_ROOT / "apps" / "agent"))
        import agentd

        env = agentd._executor_environment()
        # Codex 需要 USERPROFILE 才能找到 ~/.codex；白名单里必须有它
        self.assertIn("USERPROFILE", agentd._EXECUTOR_ENVIRONMENT_KEYS)
        self.assertTrue(set(env).issubset(set(agentd._EXECUTOR_ENVIRONMENT_KEYS)))
        if os.environ.get("USERPROFILE"):
            self.assertIn("USERPROFILE", env)


if __name__ == "__main__":
    unittest.main()

class CodexOutcomeTests(unittest.TestCase):
    """成功判定：致命错误算失败；Codex 自己的瞬断重试不算。"""

    def test_fatal_error_fails_even_with_zero_exit_code(self) -> None:
        parsed = parse_codex_jsonl(_jsonl({"type": "turn.failed", "error": "usage limit reached"}))
        success, summary = summarize_codex_result(0, parsed, "run-1")
        self.assertFalse(success)
        self.assertIn("errors: usage limit reached", summary)
        # 没有回复时也要说清楚，而不是留一段空白
        self.assertIn("没有产出回复文本", summary)

    def test_recovered_transient_error_is_success_with_a_warning(self) -> None:
        parsed = parse_codex_jsonl(
            _jsonl(
                {"type": "error", "message": "Reconnecting... 1/5 (stream disconnected before completion)"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": "DP2 复现成功。"}},
                {"type": "turn.completed", "usage": {"input_tokens": 5}},
            )
        )
        success, summary = summarize_codex_result(0, parsed, "run-2")
        self.assertTrue(success, summary)
        # 回答在最前面：界面直接展示这段文本（复制按钮、任务详情都取它）
        self.assertTrue(summary.startswith("DP2 复现成功。"), summary)
        self.assertIn("---", summary)
        self.assertIn("warnings: Reconnecting...", summary)
        self.assertNotIn("errors:", summary)

    def test_nonzero_exit_fails_regardless_of_messages(self) -> None:
        parsed = parse_codex_jsonl(_jsonl({"type": "item.completed", "item": {"type": "agent_message", "text": "半句话"}}))
        success, summary = summarize_codex_result(3, parsed, "run-3")
        self.assertFalse(success)
        self.assertIn("exit=3", summary)


if __name__ == "__main__":
    unittest.main()
