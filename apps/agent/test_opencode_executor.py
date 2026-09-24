"""CLOUD-1 方案 B 契约测试：opencode 执行体（通用 CLI 通道的协议适配）。

样本不是编的：2026-09-23 在云端执行体服务器（154.219.99.75，Ubuntu 22.04）上装 opencode **1.18.32**，
用 `opencode run --format json` 实跑采到的**原文**（下面两个常量逐字来自 `/tmp/oc.jsonl` 与
`/tmp/oc2.jsonl`，见 `docs/handoffs/CLOUD_1_OPENCODE_ADAPTER_HANDOFF.md`）。

覆盖四件事：
1. **解析**：回复文本、工具与文件动作、用量与花费（跨 step 累加）、坏行计数；
2. **摘要**：成功判定以退出码为准（错误事件形状未采到样本，不假装能判）；答案在最前面、诊断在后；
3. **上报**：opencode 协议把事件映射成 `agent.message` / `tool.completed` / `file.changed`，
   用量出处标 `opencode-jsonl`（不是 codex 的）；
4. **跨任务隔离**：常驻体里 reporter 是进程级复用的，第二次执行必须清零——
   否则第二个任务会报出第一个任务的用量（那是错数字）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
API_ROOT = REPOSITORY_ROOT / "apps" / "api"
AGENT_ROOT = REPOSITORY_ROOT / "apps" / "agent"
for candidate in (str(REPOSITORY_ROOT), str(API_ROOT), str(AGENT_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from executor_events import ExecutorEventReporter  # noqa: E402  （agent 根目录进 sys.path 后导入）
from opencode_executor import (  # noqa: E402
    OPENCODE_WORKER_TEMPLATE,
    parse_opencode_jsonl,
    summarize_opencode_result,
)

# ---- 真实样本（逐字原文，未做任何改写）------------------------------------------

# 样本 1：`opencode run --format json --auto -m opencode/mimo-v2.6-flash-free "Reply with exactly: OK"`
SAMPLE_TEXT_ONLY = """{"type":"step_start","timestamp":1790124141815,"sessionID":"ses_f34497679ffedlefYLlJSlO3JZ","part":{"id":"prt_0cbb6acef001ctuBHNchpZque6","messageID":"msg_0cbb69235001JYabGnjTCS0mbQ","sessionID":"ses_f34497679ffedlefYLlJSlO3JZ","type":"step-start"}}
{"type":"text","timestamp":1790124142054,"sessionID":"ses_f34497679ffedlefYLlJSlO3JZ","part":{"id":"prt_0cbb6ad98001004GOSCi70XCjk","messageID":"msg_0cbb69235001JYabGnjTCS0mbQ","sessionID":"ses_f34497679ffedlefYLlJSlO3JZ","type":"text","text":"OK","time":{"start":1790124141976,"end":1790124141991}}}
{"type":"step_finish","timestamp":1790124142055,"sessionID":"ses_f34497679ffedlefYLlJSlO3JZ","part":{"id":"prt_0cbb6adba001SyinQO4cZBJgpJ","reason":"stop","messageID":"msg_0cbb69235001JYabGnjTCS0mbQ","sessionID":"ses_f34497679ffedlefYLlJSlO3JZ","type":"step-finish","tokens":{"total":7683,"input":52,"output":3,"reasoning":12,"cache":{"write":0,"read":7616}},"cost":0}}
"""

# 样本 2：让它写文件再跑 shell（两步、两次工具调用、两段 step_finish）
SAMPLE_WITH_TOOLS = """{"type":"step_start","timestamp":1790124216048,"sessionID":"ses_f3448502affeEKnBw719Vc7Kob","part":{"id":"prt_0cbb7cee80014jzql2L19TCK3Y","messageID":"msg_0cbb7b78e001pA54c1qZur3Wj7","sessionID":"ses_f3448502affeEKnBw719Vc7Kob","type":"step-start"}}
{"type":"tool_use","timestamp":1790124216616,"sessionID":"ses_f3448502affeEKnBw719Vc7Kob","part":{"type":"tool","tool":"write","callID":"call_9c69314f7a754e4e9a45205b","state":{"status":"completed","input":{"filePath":"/tmp/oc-sample2/hello.txt","content":"OK"},"output":"Wrote file successfully.","metadata":{"diagnostics":{},"filepath":"/tmp/oc-sample2/hello.txt","exists":false,"truncated":false},"title":"tmp/oc-sample2/hello.txt","time":{"start":1790124216556,"end":1790124216604}},"id":"prt_0cbb7d034001MOgmoRKDn2SyuS","sessionID":"ses_f3448502affeEKnBw719Vc7Kob","messageID":"msg_0cbb7b78e001pA54c1qZur3Wj7"}}
{"type":"tool_use","timestamp":1790124217255,"sessionID":"ses_f3448502affeEKnBw719Vc7Kob","part":{"type":"tool","tool":"bash","callID":"call_6481a542473f4b7dbbfb8934","state":{"status":"completed","input":{"command":"cat hello.txt","workdir":"/tmp/oc-sample2"},"output":"OK","metadata":{"output":"OK","exit":0,"truncated":false},"title":"cat hello.txt","time":{"start":1790124216886,"end":1790124217095}},"id":"prt_0cbb7d17a001bGFPLFGAvFHbAA","sessionID":"ses_f3448502affeEKnBw719Vc7Kob","messageID":"msg_0cbb7b78e001pA54c1qZur3Wj7"}}
{"type":"step_finish","timestamp":1790124217255,"sessionID":"ses_f3448502affeEKnBw719Vc7Kob","part":{"id":"prt_0cbb7d3120019bO0jxTDteWx0i","reason":"tool-calls","messageID":"msg_0cbb7b78e001pA54c1qZur3Wj7","sessionID":"ses_f3448502affeEKnBw719Vc7Kob","type":"step-finish","tokens":{"total":7776,"input":1546,"output":66,"reasoning":20,"cache":{"write":0,"read":6144}},"cost":0}}
{"type":"step_start","timestamp":1790124219827,"sessionID":"ses_f3448502affeEKnBw719Vc7Kob","part":{"id":"prt_0cbb7dda7001d2Lx2ycuaptb7T","messageID":"msg_0cbb7d338001Xka2awzYYHO5DQ","sessionID":"ses_f3448502affeEKnBw719Vc7Kob","type":"step-start"}}
{"type":"text","timestamp":1790124220307,"sessionID":"ses_f3448502affeEKnBw719Vc7Kob","part":{"id":"prt_0cbb7df4a001uCYXvqdrm3P3yq","messageID":"msg_0cbb7d338001Xka2awzYYHO5DQ","sessionID":"ses_f3448502affeEKnBw719Vc7Kob","type":"text","text":"OK","time":{"start":1790124220234,"end":1790124220253}}}
{"type":"step_finish","timestamp":1790124220307,"sessionID":"ses_f3448502affeEKnBw719Vc7Kob","part":{"id":"prt_0cbb7df660012lFbdn835riAma","reason":"stop","messageID":"msg_0cbb7d338001Xka2awzYYHO5DQ","sessionID":"ses_f3448502affeEKnBw719Vc7Kob","type":"step-finish","tokens":{"total":7808,"input":113,"output":3,"reasoning":12,"cache":{"write":0,"read":7680}},"cost":0}}
"""


class ParseTests(unittest.TestCase):
    """解析：把人看的回复与可判定的用量从事件流里取出来。"""

    def test_reply_tools_and_files_are_counted(self) -> None:
        parsed = parse_opencode_jsonl(SAMPLE_WITH_TOOLS)
        self.assertEqual(parsed["final_message"], "OK")
        self.assertEqual(parsed["messages"], ["OK"])
        # write → 文件动作；bash → 工具动作
        self.assertEqual(parsed["file_events"], 1)
        self.assertEqual(parsed["tool_events"], 1)
        self.assertEqual(parsed["event_count"], 7)
        self.assertEqual(parsed["unparsed"], 0)

    def test_usage_accumulates_across_steps(self) -> None:
        parsed = parse_opencode_jsonl(SAMPLE_WITH_TOOLS)
        # 两次 step_finish：input 1546+113、output 66+3（opencode 的 tokens.total 含 cache 读取，不并进来）
        self.assertEqual(
            parsed["usage"],
            {
                "input_tokens": 1659,
                "output_tokens": 69,
                "total_tokens": 1728,
                "turns": 2,
                "source": "opencode-jsonl",
            },
        )
        self.assertEqual(parsed["cost"], 0.0)

    def test_single_step_sample(self) -> None:
        parsed = parse_opencode_jsonl(SAMPLE_TEXT_ONLY)
        self.assertEqual(parsed["final_message"], "OK")
        self.assertEqual(parsed["usage"]["total_tokens"], 55)  # 52 + 3
        self.assertEqual(parsed["usage"]["turns"], 1)
        self.assertEqual(parsed["tool_events"], 0)
        self.assertEqual(parsed["file_events"], 0)

    def test_bad_lines_are_counted_not_silently_dropped(self) -> None:
        parsed = parse_opencode_jsonl('not-json\n[1, 2]\n{"type":"step_start"}\n')
        self.assertEqual(parsed["unparsed"], 2)
        self.assertEqual(parsed["event_count"], 1)
        self.assertEqual(parsed["usage"], {})

    def test_empty_output_has_no_usage(self) -> None:
        parsed = parse_opencode_jsonl("")
        self.assertEqual(parsed["final_message"], "")
        self.assertEqual(parsed["usage"], {})
        self.assertIsNone(parsed["cost"])
        self.assertEqual(parsed["session_id"], "")

    def test_session_id_is_extracted_for_multi_turn_continuity(self) -> None:
        """「我的智能体」的多轮上下文靠它：每行事件顶层的 `sessionID`（下一个 --session 的值）。"""

        self.assertEqual(parse_opencode_jsonl(SAMPLE_WITH_TOOLS)["session_id"], "ses_f3448502affeEKnBw719Vc7Kob")
        self.assertEqual(parse_opencode_jsonl(SAMPLE_TEXT_ONLY)["session_id"], "ses_f34497679ffedlefYLlJSlO3JZ")


class SummarizeTests(unittest.TestCase):
    def test_answer_comes_first_and_success_needs_exit_zero(self) -> None:
        parsed = parse_opencode_jsonl(SAMPLE_WITH_TOOLS)
        success, summary = summarize_opencode_result(0, parsed, "run-abc")
        self.assertTrue(success)
        self.assertTrue(summary.startswith("OK\n\n---\n"))
        self.assertIn("opencode exit=0 run=run-abc", summary)
        self.assertIn("tools=1 files=1", summary)
        self.assertIn("tokens=1728（opencode-jsonl）", summary)
        self.assertIn("cost=0", summary)

        failed, failed_summary = summarize_opencode_result(1, parsed, "run-abc")
        self.assertFalse(failed)
        # 失败也保留回复原文（界面要看得见它到底说了什么）
        self.assertTrue(failed_summary.startswith("OK\n\n---\n"))

    def test_no_reply_text_is_stated_plainly(self) -> None:
        success, summary = summarize_opencode_result(0, parse_opencode_jsonl(""), "run-empty")
        self.assertTrue(success)
        self.assertTrue(summary.startswith("（本次执行没有产出回复文本）"))

    def test_unparsed_lines_are_reported(self) -> None:
        _, summary = summarize_opencode_result(0, parse_opencode_jsonl("garbage\n"), "run-bad")
        self.assertIn("unparsed_lines=1", summary)


class ReporterProtocolTests(unittest.TestCase):
    """上报：协议决定 stdout 怎么读；用量出处必须标对。"""

    def setUp(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.now = [0.0]
        self.reporter = ExecutorEventReporter(
            emit=lambda event_type, payload: self.sent.append((event_type, payload)),
            task_id="task-1",
            run_id="run-1",
            clock=lambda: self.now[0],
        )

    def types(self) -> list[str]:
        return [event_type for event_type, _ in self.sent]

    def feed_sample(self, sample: str) -> None:
        for index, line in enumerate(sample.splitlines()):
            self.now[0] = index * 10.0
            self.reporter.feed(line + "\n")
        self.reporter.flush()

    def test_opencode_events_map_to_platform_event_types(self) -> None:
        self.reporter.started(["opencode", "run", "--format", "json"], "cli", protocol="opencode")
        self.sent.clear()
        self.feed_sample(SAMPLE_WITH_TOOLS)
        self.assertEqual(self.types(), ["file.changed", "tool.completed", "agent.message"])
        self.assertEqual(self.sent[0][1]["path"], "/tmp/oc-sample2/hello.txt")
        self.assertEqual(self.sent[1][1]["tool"], "bash")
        self.assertEqual(self.sent[2][1]["text"], "OK")
        # 生命周期事件不受节流，但 started 已经单独发过一条
        self.assertEqual(self.sent[2][1]["task_id"], "task-1")

    def test_opencode_usage_is_labelled_with_its_own_source(self) -> None:
        self.reporter.started(["opencode", "run"], "cli", protocol="opencode")
        self.feed_sample(SAMPLE_WITH_TOOLS)
        usage = self.reporter.usage()
        self.assertEqual(usage["total_tokens"], 1728)
        self.assertEqual(usage["source"], "opencode-jsonl")

    def test_unfinished_tool_calls_are_skipped_and_finished_ones_deduplicated(self) -> None:
        self.reporter.reset_for_run("opencode")
        running = (
            '{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"call_x",'
            '"state":{"status":"running","input":{"command":"ls"}}}}'
        )
        self.reporter.feed(running + "\n")
        self.assertEqual(self.types(), [])
        # 同一次调用的完成态（真实样本里 callID 相同）只报一次
        self.feed_sample(SAMPLE_WITH_TOOLS)
        self.reporter.feed(
            '{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"call_6481a542473f4b7dbbfb8934",'
            '"state":{"status":"completed","input":{"command":"cat hello.txt"}}}}\n'
        )
        self.reporter.flush()
        self.assertEqual(self.types().count("tool.completed"), 1)

    def test_codex_protocol_still_reads_codex_shapes(self) -> None:
        """默认协议不能被改坏：codex 的 turn.completed / item.completed 照旧。"""

        self.reporter.started(["codex", "exec", "--json"], "codex")
        self.now[0] = 10.0  # 推进时钟：过程事件有最小间隔节流，刚起进程那一刻的发不出去
        self.reporter.feed('{"type":"turn.completed","usage":{"input_tokens":120,"output_tokens":30}}\n')
        self.reporter.feed('{"type":"item.completed","item":{"type":"agent_message","text":"好了"}}\n')
        self.reporter.flush()
        self.assertEqual(self.reporter.usage()["source"], "codex-jsonl")
        self.assertIn("agent.message", self.types())

    def test_none_protocol_does_not_parse_stdout(self) -> None:
        """声明式命令的 stdout 不是事件流：不解析，也不该把每行都记成坏行。"""

        self.reporter.started(["python", "run.py"], "command", protocol="none")
        self.reporter.feed("普通输出第一行\n普通输出第二行\n")
        self.reporter.flush()
        self.assertEqual(self.reporter.stats()["unparsed_lines"], 0)
        self.assertIsNone(self.reporter.usage())
        self.assertEqual([item for item in self.types() if item != "process.started"], [])

    def test_started_clears_the_previous_run(self) -> None:
        """常驻体里 reporter 跨任务复用：第二次执行必须只报第二次的用量。"""

        self.reporter.started(["codex", "exec", "--json"], "codex")
        self.reporter.feed('{"type":"turn.completed","usage":{"input_tokens":1000,"output_tokens":500}}\n')
        self.reporter.flush()
        self.assertEqual(self.reporter.usage()["total_tokens"], 1500)

        self.reporter.started(["opencode", "run"], "cli", protocol="opencode")
        self.assertIsNone(self.reporter.usage())
        self.feed_sample(SAMPLE_WITH_TOOLS)
        self.assertEqual(self.reporter.usage()["total_tokens"], 1728)
        self.assertEqual(self.reporter.usage()["source"], "opencode-jsonl")
        # 事件计数也按次清零（否则 process.exited 的统计会把上一轮算进来）
        self.assertEqual(self.reporter.stats()["unparsed_lines"], 0)


class EventProtocolSelectionTests(unittest.TestCase):
    """Agent 侧：这次执行按哪种协议解析 stdout。"""

    @classmethod
    def setUpClass(cls) -> None:
        import agentd

        cls.agentd = agentd

    def test_codex_executor_is_always_codex_protocol(self) -> None:
        task = {"resource_policy": {"worker_executor": "codex"}}
        self.assertEqual(self.agentd._event_protocol(task), "codex")

    def test_opencode_template_is_recognised_by_first_word(self) -> None:
        task = {
            "resource_policy": {
                "worker_executor": "cli",
                "worker_command": list(OPENCODE_WORKER_TEMPLATE),
            }
        }
        self.assertEqual(self.agentd._event_protocol(task), "opencode")

    def test_other_generic_cli_stays_unparsed(self) -> None:
        task = {
            "resource_policy": {
                "worker_executor": "cli",
                "worker_command": ["workbuddy", "exec", "{prompt}"],
            }
        }
        self.assertEqual(self.agentd._event_protocol(task), "none")

    def test_declared_protocol_wins_over_the_template(self) -> None:
        declared = {
            "resource_policy": {
                "worker_executor": "cli",
                "worker_command": ["my-wrapper", "{prompt}"],
                "worker_events": "opencode",
            }
        }
        self.assertEqual(self.agentd._event_protocol(declared), "opencode")

        opted_out = {
            "resource_policy": {
                "worker_executor": "cli",
                "worker_command": list(OPENCODE_WORKER_TEMPLATE),
                "worker_events": "none",
            }
        }
        self.assertEqual(self.agentd._event_protocol(opted_out), "none")

    def test_absolute_path_and_declarative_command(self) -> None:
        absolute = {
            "resource_policy": {
                "worker_executor": "cli",
                "worker_command": ["/usr/local/bin/opencode", "run", "--format", "json", "{prompt}"],
            }
        }
        self.assertEqual(self.agentd._event_protocol(absolute), "opencode")
        declarative = {"resource_policy": {"worker_command": ["python", "run.py"]}}
        self.assertEqual(self.agentd._event_protocol(declarative), "none")


class ResourcePolicyValidationTests(unittest.TestCase):
    """API 侧：`worker_events` 只接受平台真正支持的取值。"""

    @staticmethod
    def validate(policy: dict) -> dict:
        from app.store import Store

        return Store._validated_resource_policy(policy)

    def test_opencode_events_with_cli_executor_is_accepted(self) -> None:
        policy = {
            "worker_executor": "cli",
            "worker_command": list(OPENCODE_WORKER_TEMPLATE),
            "worker_events": "opencode",
        }
        self.assertEqual(self.validate(policy), policy)

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.validate(
                {
                    "worker_executor": "cli",
                    "worker_command": ["opencode", "run", "{prompt}"],
                    "worker_events": "opencode-v2",
                }
            )
        self.assertIn("unsupported_worker_events:opencode-v2", str(caught.exception))

    def test_protocol_must_match_the_executor(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self.validate({"worker_executor": "codex", "worker_events": "opencode"})
        self.assertIn("worker_events_opencode_requires_cli_executor", str(caught.exception))

        with self.assertRaises(ValueError) as caught:
            self.validate(
                {
                    "worker_executor": "cli",
                    "worker_command": ["opencode", "run", "{prompt}"],
                    "worker_events": "codex",
                }
            )
        self.assertIn("worker_events_codex_requires_codex_executor", str(caught.exception))

    def test_codex_keeps_working_without_the_new_field(self) -> None:
        self.assertEqual(self.validate({"worker_executor": "codex"}), {"worker_executor": "codex"})


if __name__ == "__main__":
    unittest.main()