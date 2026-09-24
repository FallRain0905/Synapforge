"""W-9 契约测试：通用 CLI 执行体（worker_executor = "cli"）。

口径：cli = 提示词驱动的命令模板（{prompt} 占位符），stdout 即结果、退出码即成败——
语义与声明式命令一致，只是提示词来自任务（worker_prompt / 标题+说明+完成标准）。
workbuddy / zcode 都走这条路；各自的正确调用方式由任务模板声明，平台不猜协议。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

API_ROOT = Path(__file__).resolve().parent.parent.parent / "apps" / "api"
AGENT_ROOT = Path(__file__).resolve().parent.parent.parent / "apps" / "agent"

sys.path.insert(0, str(API_ROOT))
sys.path.insert(0, str(AGENT_ROOT))


class CliExecutorPolicyTests(unittest.TestCase):
    """API 侧：`_validated_resource_policy` 对 cli 执行体的判定。"""

    @staticmethod
    def validate(policy: dict) -> dict:
        from app.store import Store

        return Store._validated_resource_policy(policy)

    def test_cli_with_prompt_placeholder_is_accepted(self) -> None:
        policy = {
            "worker_executor": "cli",
            "worker_command": ["workbuddy", "exec", "{prompt}", "--out", "result.md"],
            "worker_prompt": "写一份周报",
        }
        self.assertEqual(self.validate(policy), policy)

    def test_cli_without_command_template_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.validate({"worker_executor": "cli"})
        self.assertEqual(str(caught.exception), "cli_executor_requires_command_template")

    def test_cli_without_prompt_placeholder_is_rejected(self) -> None:
        # 没有 {prompt} 就等于声明式命令——该走 command 路径，不该标 cli
        with self.assertRaises(ValueError) as caught:
            self.validate({"worker_executor": "cli", "worker_command": ["workbuddy", "exec", "fixed"]})
        self.assertEqual(str(caught.exception), "cli_executor_template_requires_prompt_placeholder")

    def test_unknown_executor_still_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.validate({"worker_executor": "doubao"})
        self.assertIn("unsupported_worker_executor:doubao", str(caught.exception))

    def test_codex_and_plain_command_unchanged(self) -> None:
        codex = {"worker_executor": "codex"}
        self.assertEqual(self.validate(codex), codex)
        command = {"worker_command": ["cmd", "/c", "echo", "hi"]}
        self.assertEqual(self.validate(command), command)


class CliExecutorCommandTests(unittest.TestCase):
    """Agent 侧：模板换提示词、执行体类型判定。"""

    @classmethod
    def setUpClass(cls) -> None:
        import agentd

        cls.agentd = agentd

    def test_executor_kind(self) -> None:
        self.assertEqual(self.agentd._executor_kind({"resource_policy": {"worker_executor": "codex"}}), "codex")
        self.assertEqual(
            self.agentd._executor_kind({"resource_policy": {"worker_executor": "cli", "worker_command": ["workbuddy", "{prompt}"]}}),
            "cli",
        )
        self.assertEqual(self.agentd._executor_kind({"resource_policy": {"worker_command": ["echo", "hi"]}}), "command")
        self.assertEqual(self.agentd._executor_kind({}), "unspecified")

    def test_cli_command_substitutes_prompt(self) -> None:
        task = {
            "title": "写周报",
            "description": "汇总本周进展",
            "acceptance_criteria": ["含风险清单"],
            "resource_policy": {"worker_executor": "cli", "worker_command": ["workbuddy", "exec", "{prompt}"]},
        }
        command = self.agentd._cli_command(task, None)
        self.assertEqual(command[:2], ["workbuddy", "exec"])
        self.assertIn("写周报", command[2])
        self.assertIn("汇总本周进展", command[2])
        self.assertIn("含风险清单", command[2])
        self.assertNotIn("{prompt}", command[2])

    def test_cli_command_honors_declared_prompt(self) -> None:
        task = {
            "title": "t",
            "resource_policy": {
                "worker_executor": "cli",
                "worker_command": ["zcode", "run", "--prompt", "{prompt}"],
                "worker_prompt": "只回答一句话",
            },
        }
        command = self.agentd._cli_command(task, None)
        self.assertEqual(command, ["zcode", "run", "--prompt", "只回答一句话"])

    def test_cli_command_without_template_falls_back(self) -> None:
        task = {"title": "t", "resource_policy": {"worker_executor": "cli"}}
        self.assertEqual(self.agentd._cli_command(task, ["fallback", "{prompt}"]), ["fallback", "t"])

    def test_codex_detection_unchanged(self) -> None:
        self.assertTrue(self.agentd._is_codex_executor({"resource_policy": {"worker_executor": "codex"}}))
        self.assertFalse(self.agentd._is_codex_executor({"resource_policy": {"worker_executor": "cli", "worker_command": ["x", "{prompt}"]}}))


class GenericCliInventoryTests(unittest.TestCase):
    """Agent 侧：workbuddy / zcode 的安装探测（不猜协议，只回答装没装）。"""

    def test_probe_reports_not_installed_when_missing(self) -> None:
        from agent_inventory import _probe_generic_cli

        status = _probe_generic_cli("definitely-not-installed-xyz")
        self.assertEqual(status["adapter_id"], "definitely-not-installed-xyz-cli")
        self.assertEqual(status["state"], "NOT_INSTALLED")

    def test_default_probe_includes_generic_clis(self) -> None:
        from agent_inventory import default_probe

        ids = [entry["adapter_id"] for entry in default_probe()]
        self.assertIn("codex-cli", ids)
        self.assertIn("claude-code", ids)
        self.assertIn("workbuddy-cli", ids)
        self.assertIn("zcode-cli", ids)


if __name__ == "__main__":
    unittest.main()