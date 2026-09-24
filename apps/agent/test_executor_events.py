"""执行体过程上报的契约测试（`executor_events.ExecutorEventReporter`）。

覆盖三件容易做错的事：
1. **粒度**：只报"看得懂的过程"（回复/工具/文件/起停），不把每一行 stdout 都当事件；
2. **节流**：高频事件按时间间隔与条数上限收敛，并把"压掉了多少"如实统计出来；
3. **终态边界**：process.started / process.exited 不受节流；终态完成不走这里（由 HTTP 完成 Run）。
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from apps.agent.executor_events import ExecutorEventReporter


def _line(**item: object) -> str:
    return json.dumps({"type": "item.completed", "item": item}) + "\n"


class ReporterTests(unittest.TestCase):
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

    def test_stream_is_parsed_line_by_line_even_when_split_across_chunks(self) -> None:
        raw = _line(type="agent_message", text="先看题面")
        # 故意在 JSON 中间切开，模拟 TCP 分片
        self.reporter.feed(raw[:20])
        self.assertEqual(self.sent, [])
        self.reporter.feed(raw[20:])
        self.assertEqual(self.types(), ["agent.message"])
        self.assertEqual(self.sent[0][1]["text"], "先看题面")
        self.assertEqual(self.sent[0][1]["task_id"], "task-1")
        self.assertEqual(self.sent[0][1]["run_id"], "run-1")

    def test_maps_codex_items_to_platform_event_types(self) -> None:
        self.reporter.feed(_line(type="tool_call", name="shell"))
        self.now[0] = 10
        self.reporter.feed(_line(type="file_change", path="paper/main.tex"))
        self.now[0] = 20
        self.reporter.feed(_line(type="agent_message", text="结论如下"))
        self.assertEqual(self.types(), ["tool.completed", "file.changed", "agent.message"])
        self.assertEqual(self.sent[0][1]["tool"], "shell")
        self.assertEqual(self.sent[1][1]["path"], "paper/main.tex")

    def test_high_frequency_events_are_throttled_and_counted(self) -> None:
        for index in range(10):
            self.reporter.feed(_line(type="agent_message", text=f"片段 {index}"))
        # 同一瞬间只放行第一条，其余计入 suppressed
        self.assertEqual(self.types(), ["agent.message"])
        stats = self.reporter.stats()
        self.assertEqual(stats["sent_events"], 1)
        self.assertEqual(stats["suppressed_events"], 9)

    def test_max_events_caps_the_total(self) -> None:
        reporter = ExecutorEventReporter(
            emit=lambda event_type, payload: self.sent.append((event_type, payload)),
            min_interval_seconds=0,
            max_events=3,
        )
        for index in range(6):
            reporter.feed(_line(type="agent_message", text=f"片段 {index}"))
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(reporter.stats()["suppressed_events"], 3)

    def test_lifecycle_events_bypass_throttling(self) -> None:
        self.reporter.started(["codex", "exec", "--json"], "codex")
        self.reporter.feed(_line(type="agent_message", text="片段"))
        self.reporter.exited_code = None  # 终态由任务循环发，这里只验证 started 不受限
        self.assertIn("process.started", self.types())
        started = next(payload for event_type, payload in self.sent if event_type == "process.started")
        self.assertEqual(started["executor"], "codex")
        self.assertIn("codex exec", started["command"])

    def test_broken_lines_are_counted_not_silently_dropped(self) -> None:
        self.reporter.feed("not-json\n")
        self.reporter.feed('{"type": "turn.started"}\n')
        self.reporter.feed('{"type": "item.completed", "item": "not-an-object"}\n')
        self.assertEqual(self.sent, [])
        self.assertEqual(self.reporter.stats()["unparsed_lines"], 2)

    def test_no_sink_means_no_work(self) -> None:
        """worker-run（前台调试）不给 sink：不应该产生任何事件，也不应该报错。"""

        quiet = ExecutorEventReporter(emit=None)
        quiet.started(["codex"], "codex")
        quiet.feed(_line(type="agent_message", text="x"))
        quiet.flush()
        self.assertEqual(quiet.stats()["sent_events"], 0)

    def test_usage_is_accumulated_from_turn_completed(self) -> None:
        """用量（COST-1）从 turn.completed 里读：多轮累加，不当过程事件上报。"""

        self.reporter.feed(
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 120, "output_tokens": 30}})
            + "\n"
            + json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}})
            + "\n"
        )
        self.reporter.flush()
        self.assertEqual(
            self.reporter.usage(),
            {"input_tokens": 125, "output_tokens": 31, "total_tokens": 156, "turns": 2, "source": "codex-jsonl"},
        )
        # 用量不进过程事件流（群聊不该被用量刷屏）
        self.assertEqual(self.types(), [])

    def test_usage_is_none_without_a_report_and_parsed_without_sink(self) -> None:
        """没回报就是 None（不报 0）；没有 sink 也要解析出来——用量只在这条路径上能得到。"""

        self.assertIsNone(self.reporter.usage())
        quiet = ExecutorEventReporter(emit=None)
        quiet.feed(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 7, "output_tokens": 3}}) + "\n")
        quiet.flush()
        self.assertEqual(quiet.usage()["total_tokens"], 10)
        self.assertEqual(quiet.stats()["sent_events"], 0)

    def test_flush_handles_the_last_line_without_newline(self) -> None:
        self.reporter.feed(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "结尾"}}))
        self.assertEqual(self.sent, [])
        self.reporter.flush()
        self.assertEqual(self.types(), ["agent.message"])


if __name__ == "__main__":
    unittest.main()