"""执行体过程上报：把执行体的 JSON 事件流变成平台可见的进度事件。

为什么单独一个模块：`agentd._execute_task` 已经不短，而"什么该上报、多快上报"
是一条独立规则，必须能单独测——否则很容易变成"每条 stdout 都发一次"，把事件表灌爆。

粒度与节流（与 `docs/CODEX_EXECUTOR.md` §3.5 一致）：

- **生命周期**：`process.started`（起进程）、`process.exited`（退出码 + 摘要）——一次一条，不节流；
- **过程**：`agent.message`（执行体的中间回复）、`tool.completed`（工具调用）、`file.changed`（文件修改）
  —— 每条最多 1 次、每次至少间隔 `min_interval_seconds`、整体不超过 `max_events` 条；
- **终态不在这里发**：`run.completed`/`run.failed` 会经 Gateway 完成 Run，而完成任务是 HTTP
  `/api/runs/{id}/complete` 的职责，两条路径同时走会把同一个 Run 完成两次。

两个事件协议（`event_protocol`），都是**按行**的 JSON、都按行解析，坏行计入 `unparsed`
（不静默丢弃）：

- `codex`（默认，`codex exec --json`）：`turn.completed` 带用量、`item.completed` 带过程；
- `opencode`（`opencode run --format json`，形状取自 1.18.32 的真实输出样本）：
  `text` 带回复文本（`part.text`）、`tool_use` 带工具与文件动作（`part.tool`/`part.state`）、
  `step_finish` 带用量（`part.tokens`）与花费（`part.cost`）、`step_start` 无内容；
- `none`：不解析（声明式命令的 stdout 通常不是 JSON），stdout 只当结果用。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

EmitCallable = Callable[[str, dict], None]

DEFAULT_MIN_INTERVAL_SECONDS = 1.5
DEFAULT_MAX_EVENTS = 40

# 事件协议：`codex` / `opencode` / `none`（不解析）。默认 codex——历史调用方全都按 codex 形状喂数据。
DEFAULT_EVENT_PROTOCOL = "codex"
EVENT_PROTOCOLS = ("codex", "opencode", "none")

# opencode 里"这是文件动作"的工具名（`write` 写整份、`edit` 改片段）；
# 其余（bash/read/grep…）按普通工具上报。
_OPENCODE_FILE_TOOLS = ("write", "edit")


@dataclass
class ExecutorEventReporter:
    """按行消费执行体输出，节流后上报为平台事件。"""

    emit: EmitCallable | None
    task_id: str | None = None
    run_id: str | None = None
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    max_events: int = DEFAULT_MAX_EVENTS
    clock: Callable[[], float] = time.monotonic
    event_protocol: str = DEFAULT_EVENT_PROTOCOL
    _buffer: str = ""
    _sent: int = 0
    # 初值必须是"很久以前"：否则第一条过程事件会因为距 0 秒不足最小间隔而被压掉
    _last_sent_at: float = field(default_factory=lambda: float("-inf"))
    _unparsed: int = 0
    _suppressed: int = 0
    # 用量（COST-1）：从执行体输出里读到的 token 计数；没有回报就是 0 轮、不假装有数
    _usage_input: int = 0
    _usage_output: int = 0
    _usage_turns: int = 0
    # opencode 会在一次工具调用上重复发事件（pending → running → completed），按 callID 去重
    _reported_calls: set[str] = field(default_factory=set)

    # ---- 生命周期 -------------------------------------------------------

    def started(
        self,
        command: list[str] | tuple[str, ...],
        executor: str,
        protocol: str | None = None,
    ) -> None:
        """一次执行开始：**先清零上一次的计数**（常驻体里 reporter 是进程级复用的，
        不归零会让第二个任务报到第一个任务的用量——那是错数字，不是"多一点"），再发 `process.started`。
        """

        self.reset_for_run(protocol)
        self._send("process.started", {
            "executor": executor,
            "command": " ".join(str(part) for part in command[:3]),
        })

    def reset_for_run(self, protocol: str | None = None) -> None:
        """把按次统计清零：缓冲、用量、事件计数、工具去重集合、节流时间戳。"""

        if protocol:
            self.event_protocol = protocol if protocol in EVENT_PROTOCOLS else "none"
        self._buffer = ""
        self._sent = 0
        self._unparsed = 0
        self._suppressed = 0
        self._last_sent_at = float("-inf")
        self._usage_input = 0
        self._usage_output = 0
        self._usage_turns = 0
        self._reported_calls = set()

    def usage(self) -> dict[str, Any] | None:
        """这次执行读到的用量（token）。没读到就返回 None——不报 0，避免"0 用量"被当真。"""

        if not self._usage_turns:
            return None
        return {
            "input_tokens": self._usage_input,
            "output_tokens": self._usage_output,
            "total_tokens": self._usage_input + self._usage_output,
            "turns": self._usage_turns,
            "source": "opencode-jsonl" if self.event_protocol == "opencode" else "codex-jsonl",
        }

    def stats(self) -> dict[str, int]:
        """给"进程退出"那条事件用：节流掉了多少、坏行多少——不假装什么都报上去了。"""

        return {"sent_events": self._sent, "suppressed_events": self._suppressed, "unparsed_lines": self._unparsed}

    # ---- 输出流 ---------------------------------------------------------

    def feed(self, chunk: str) -> None:
        """喂入一段 stdout（可能不是完整行）。

        **没有 sink 也要解析**：用量（token 计数）是我们唯一能拿到它的地方，
        而 `emit=None` 只是"不发过程事件"（前台调试路径），不该顺带把用量也丢掉。
        """

        self._buffer += chunk
        while "\n" in self._buffer:
            line, _, rest = self._buffer.partition("\n")
            self._buffer = rest
            self._handle_line(line)

    def flush(self) -> None:
        if not self._buffer.strip():
            self._buffer = ""
            return
        line, self._buffer = self._buffer, ""
        self._handle_line(line)

    # ---- 内部 -----------------------------------------------------------

    def _handle_line(self, line: str) -> None:
        text = line.strip()
        if not text:
            return
        if self.event_protocol == "none":
            # 不解析的协议：stdout 只当结果用（声明式命令的边界与今天完全一致）
            return
        try:
            event = json.loads(text)
        except ValueError:
            self._unparsed += 1
            return
        if not isinstance(event, dict):
            self._unparsed += 1
            return
        if self.event_protocol == "opencode":
            self._handle_opencode_event(event)
            return
        self._handle_codex_event(event)

    def _handle_codex_event(self, event: dict) -> None:
        """codex `--json`：`turn.completed` 带用量、`item.completed` 带过程。"""

        event_type = str(event.get("type") or "")
        if event_type == "turn.completed":
            # 用量在这一类事件里（codex 的 JSONL）：只累计，不当过程事件上报
            usage = event.get("usage")
            if isinstance(usage, dict):
                self._usage_input += int(usage.get("input_tokens") or 0)
                self._usage_output += int(usage.get("output_tokens") or 0)
                self._usage_turns += 1
            return
        if event_type != "item.completed":
            # 其它事件类型（thread/turn.started…）是预期内的，不算坏行
            return
        item = event.get("item")
        item_type = str(item.get("type") or "") if isinstance(item, dict) else ""
        if not item_type:
            # 结构不对的 item.completed：算坏行，别静默丢掉
            self._unparsed += 1
            return
        if item_type == "agent_message":
            message = str(item.get("text") or "").strip()
            if message:
                self._send("agent.message", {"text": message[:500]})
        elif item_type in {"tool_call", "command_execution", "function_call"}:
            self._send("tool.completed", {"tool": str(item.get("name") or item_type)[:80]})
        elif item_type in {"file_change", "patch_apply", "file_write"}:
            self._send("file.changed", {"path": str(item.get("path") or "")[:200]})

    def _handle_opencode_event(self, event: dict) -> None:
        """opencode `--format json`：`text` 回复、`tool_use` 工具、`step_finish` 用量。

        形状取自 opencode 1.18.32 的真实输出（`docs/handoffs/CLOUD_1_OPENCODE_ADAPTER_HANDOFF.md` 有原文样本）：

        - `{"type":"text","part":{"type":"text","text":"OK"}}`
        - `{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":…,"state":{"status":"completed",…}}}`
        - `{"type":"step_finish","part":{"type":"step-finish","tokens":{"input":5,"output":1,…},"cost":0}}`
        """

        event_type = str(event.get("type") or "")
        part = event.get("part")
        part = part if isinstance(part, dict) else {}
        if event_type == "step_finish":
            # 用量只累计，不上报（群聊不该被用量刷屏）——与 codex 同一口径
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                self._usage_input += int(tokens.get("input") or 0)
                self._usage_output += int(tokens.get("output") or 0)
                self._usage_turns += 1
            return
        if event_type == "text":
            message = str(part.get("text") or "").strip()
            if message:
                self._send("agent.message", {"text": message[:500]})
            return
        if event_type == "tool_use":
            self._handle_opencode_tool(part)
            return
        # `step_start` 等其它类型是预期内的（生命周期由 process.started/exited 表达）
        return

    def _handle_opencode_tool(self, part: dict) -> None:
        state = part.get("state")
        state = state if isinstance(state, dict) else {}
        # 只在完成态上报：pending/running 只说明"正在做"，没有结果可讲
        if str(state.get("status") or "") != "completed":
            return
        call_id = str(part.get("callID") or "")
        if call_id:
            if call_id in self._reported_calls:
                return
            self._reported_calls.add(call_id)
        tool = str(part.get("tool") or "")[:80]
        if not tool:
            self._unparsed += 1
            return
        if tool in _OPENCODE_FILE_TOOLS:
            data = state.get("input")
            path = str(data.get("filePath") or "") if isinstance(data, dict) else ""
            self._send("file.changed", {"path": path[:200]})
            return
        self._send("tool.completed", {"tool": tool})

    def progress(self, event_type: str, payload: dict[str, Any]) -> None:
        """直接投递一条过程事件（常驻 server 模式 M-5c S-1 用）。

        那条通道的事件不是"一行一段 JSONL"，而是 SSE 的对象（`message.part.*`），
        翻译放在 `opencode_server.py` 里；这里只借用同一套**节流、上限与计数**口径，
        避免两条通道各写一份节流规则。
        """

        self._send(event_type, payload)

    def _send(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.emit is None:
            return
        if self._sent >= self.max_events:
            self._suppressed += 1
            return
        now = self.clock()
        # 生命周期事件（起停）不受节流影响：它们是"到底有没有在跑"的唯一证据
        if event_type not in {"process.started", "process.exited"}:
            if now - self._last_sent_at < self.min_interval_seconds:
                self._suppressed += 1
                return
        self._last_sent_at = now
        self._sent += 1
        body = dict(payload)
        if self.task_id:
            body["task_id"] = self.task_id
        if self.run_id:
            body["run_id"] = self.run_id
        try:
            self.emit(event_type, body)
        except Exception:  # noqa: BLE001 - 上报失败不能影响任务执行
            self._suppressed += 1


__all__ = ["ExecutorEventReporter", "DEFAULT_MAX_EVENTS", "DEFAULT_MIN_INTERVAL_SECONDS"]