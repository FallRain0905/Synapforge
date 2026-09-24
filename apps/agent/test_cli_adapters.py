from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from cli_adapters import (
    ClaudeCodeAdapter,
    CliAdapterError,
    CliProtocolError,
    CodexAdapter,
    CodexJsonlEventParser,
    probe_cli,
)
from runner import ExecutionProfile, RunnerRequest


class CliAdapterTests(unittest.TestCase):
    def test_codex_command_is_constructed_with_machine_readable_mode(self) -> None:
        adapter = CodexAdapter()
        command = adapter.build_command("分析问题一", model="test-model", output_schema="schema.json")
        self.assertEqual(command[0:2], ("codex", "exec"))
        self.assertIn("--json", command)
        self.assertIn("--color", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--model", command)
        self.assertIn("--output-schema", command)
        self.assertEqual(command[-1], "分析问题一")

    def test_codex_rejects_unsafe_approval_and_sandbox_flags(self) -> None:
        adapter = CodexAdapter()
        with self.assertRaisesRegex(CliAdapterError, "unsafe_approval"):
            adapter.build_command(("codex", "exec", "--approve-for-me", "prompt"))
        with self.assertRaisesRegex(CliAdapterError, "unrestricted_sandbox"):
            adapter.build_command(("codex", "exec", "--sandbox", "danger-full-access", "prompt"))
        with self.assertRaisesRegex(CliAdapterError, "path_escape"):
            adapter.build_command(("codex", "exec", "--cd", "C:/outside", "prompt"))

    def test_codex_builds_a_runner_process_spec_from_an_explicit_invocation(self) -> None:
        adapter = CodexAdapter()
        request = RunnerRequest(
            project_id="project-001",
            task_id="task-001",
            run_id="run-codex-001",
            agent_id="agent-001",
            device_id="device-001",
            workspace_id="workspace-001",
            workspace_path="C:/workspace",
            command=("codex", "exec", "prompt"),
            profile=ExecutionProfile(mode="HEADLESS"),
        )
        spec = adapter.build_process_spec(request)
        self.assertEqual(spec.command[0:2], ("codex", "exec"))
        self.assertIn("--json", spec.command)
        self.assertFalse(spec.stdin_enabled)

    def test_codex_output_schema_must_stay_inside_the_workspace(self) -> None:
        adapter = CodexAdapter()
        request = RunnerRequest(
            project_id="project-001",
            task_id=None,
            run_id="run-codex-schema-001",
            agent_id="agent-001",
            device_id="device-001",
            workspace_id="workspace-001",
            workspace_path="C:/workspace",
            command=("codex", "exec", "--output-schema", "C:/outside/schema.json", "prompt"),
            profile=ExecutionProfile(mode="HEADLESS"),
        )
        with self.assertRaisesRegex(CliAdapterError, "outside_workspace"):
            adapter.build_process_spec(request)

    def test_codex_process_execution_requires_a_compatible_capability_probe(self) -> None:
        adapter = CodexAdapter("definitely-missing-codex")
        status = adapter.probe()
        self.assertEqual(status.state, "NOT_INSTALLED")

    def test_codex_parser_handles_chunked_tool_message_and_completion(self) -> None:
        parser = CodexJsonlEventParser()
        first = parser.feed('{"type":"thread.started","thread_id":"thread-1"}\n{"type":"item.started","item":')
        self.assertEqual([event.event_type for event in first], ["run.started"])
        second = parser.feed('{"type":"command_execution","command":"python x.py","status":"in_progress"}}\n')
        self.assertEqual([event.event_type for event in second], ["tool.started"])
        third = parser.feed('{"type":"item.completed","item":{"type":"agent_message","text":"完成"}}\n{"type":"turn.completed","usage":{"input_tokens":1}}\n')
        self.assertEqual([event.event_type for event in third], ["agent.message", "run.completed"])

    def test_codex_parser_emits_approval_event(self) -> None:
        parser = CodexJsonlEventParser()
        events = parser.feed('{"type":"item.updated","item":{"type":"command_execution","status":"awaiting_approval"}}\n')
        self.assertEqual(events[0].event_type, "approval.requested")

    def test_codex_parser_fails_closed_on_unknown_protocol(self) -> None:
        with self.assertRaisesRegex(CliProtocolError, "event_type_unsupported"):
            CodexJsonlEventParser().feed('{"type":"future.codex.event"}\n')
        with self.assertRaisesRegex(CliProtocolError, "item_type_unsupported"):
            CodexJsonlEventParser().feed('{"type":"item.started","item":{"type":"future_item"}}\n')
        with self.assertRaisesRegex(CliProtocolError, "jsonl_invalid"):
            CodexJsonlEventParser().feed("not-json\n")

    def test_probe_reports_available_version_using_no_shell(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "version_probe.py"
            script.write_text("import sys; print('codex-cli 0.153.4')\n", encoding="utf-8")
            status = probe_cli(
                adapter_id="test-cli",
                executable=sys.executable,
                capabilities=("cli.version",),
                accepted_version_prefixes=("0.153.",),
                version_args=(str(script), "--version"),
            )
        self.assertEqual(status.state, "AVAILABLE")
        self.assertEqual(status.version, "0.153.4")

    def test_probe_reports_unsupported_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "version_probe.py"
            script.write_text("print('codex-cli 0.999.1')\n", encoding="utf-8")
            status = probe_cli(
                adapter_id="test-cli",
                executable=sys.executable,
                capabilities=(),
                accepted_version_prefixes=("0.153.",),
                version_args=(str(script),),
            )
        self.assertEqual(status.state, "UNSUPPORTED")

    def test_claude_is_not_claimed_as_supported(self) -> None:
        status = ClaudeCodeAdapter("definitely-missing-claude-code").probe()
        self.assertEqual(status.state, "NOT_INSTALLED")
        with self.assertRaisesRegex(CliAdapterError, "semantic_protocol_not_implemented"):
            ClaudeCodeAdapter().build_process_spec(
                RunnerRequest(
                    project_id="project-001",
                    task_id=None,
                    run_id="run-claude-001",
                    agent_id="agent-001",
                    device_id="device-001",
                    workspace_id="workspace-001",
                    workspace_path="C:/workspace",
                    command=("claude", "prompt"),
                    profile=ExecutionProfile(mode="HEADLESS"),
                )
            )


if __name__ == "__main__":
    unittest.main()
