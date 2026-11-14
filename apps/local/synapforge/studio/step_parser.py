"""stream-json 解析：把 Claude Code 的 stdout 行变成 step 事件。

单 Agent 运行器和集群角色调用共用同一套解析，保证两边看到的步骤种类、
增量节流、去重规则完全一致。

调用方只需实现三个回调（sink）：

- `emit(kind, title, detail, payload)` → 新建一条 step，返回带 `id`/`seq` 的记录
- `update(step_id, title, detail)` → 覆盖已有 step（块结束时）
- `publish(event)` → 推给 SSE（`{"type": "step_update", ...}`）

解析器自己不碰数据库、不碰线程，纯状态机，方便单测。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

MAX_TEXT = 6000
THROTTLE_SECONDS = 0.15


def _text(value: Any, limit: int = MAX_TEXT) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[:limit] + "…"


def tool_input(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("file_path", "path", "command", "description", "pattern", "query"):
            if value.get(key):
                return str(value[key])
        return json.dumps(value, ensure_ascii=False)[:2000]
    return str(value or "")


def tool_result(value: Any) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts) or json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value or "")


class StepParser:
    """一行 stream-json → 0..n 条 step 事件。

    `emit` / `update` / `publish` 由调用方注入；解析器只维护
    「当前正在流式输出的块」和「是否已经逐块推过」这两个状态。
    """

    def __init__(self, *, emit: Callable[..., dict[str, Any]],
                 update: Callable[..., None],
                 publish: Callable[[dict[str, Any]], None],
                 now: Callable[[], float] | None = None) -> None:
        self._emit = emit
        self._update = update
        self._publish = publish
        self._now = now or time.time
        self.blocks: dict[int, dict[str, Any]] = {}
        self.streamed_blocks = 0
        self.session_id = ""
        self.model = ""
        self.result_text = ""
        self.ok = False
        self.session_announced = False
        self.usage: dict[str, Any] = {}
        self.new_session_id = ""
        self.on_session: Callable[[str], None] | None = None
        self.on_result: Callable[[], None] | None = None

    # ---- 对外入口 ----------------------------------------------------------

    def feed(self, line: str, *, backend: str = "claude") -> None:
        raw = (line or "").strip()
        if not raw:
            return
        if backend == "command":
            # 自定义命令多半是纯文本；但如果它自己也吐 stream-json，按结构化解析
            if not raw.startswith("{"):
                self._emit("output", "输出", _text(raw), {})
                return
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            if backend == "command":
                self._emit("output", "输出", _text(raw), {})
            else:
                self._emit("raw", "输出", _text(raw), {})
            return
        if not isinstance(event, dict):
            if backend == "command":
                self._emit("output", "输出", _text(raw), {})
            return
        self._handle_event(event)

    def reset_attempt(self) -> None:
        """换 key 重试时清掉本轮的流式状态，避免旧块串到新尝试。"""

        self.blocks.clear()
        self.streamed_blocks = 0
        self.session_announced = False
        self.result_text = ""
        self.ok = False
        self.usage = {}

    # ---- 事件分发 ----------------------------------------------------------

    def _handle_event(self, event: dict[str, Any]) -> None:
        kind = str(event.get("type") or "")
        if kind == "stream_event":
            self._stream_event(event.get("event") or {})
            return
        if kind == "system":
            self.session_id = str(event.get("session_id") or self.session_id)
            model = str(event.get("model") or self.model or "")
            if model:
                self.model = model
            if self.session_id and self.on_session:
                self.on_session(self.session_id)
            if not self.session_announced:
                self.session_announced = True
                self._emit("system", "会话开始",
                           f"model={model or '默认'}\nsession={self.session_id[:18]}", {})
            return
        if kind == "assistant":
            message = event.get("message") or {}
            usage = message.get("usage") or {}
            if usage:
                self._merge_usage(usage)
            streamed = bool(self.streamed_blocks)
            self.streamed_blocks = 0
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = str(block.get("type") or "")
                if btype == "text":
                    if streamed:
                        continue  # 已经逐块推过
                    text = _text(block.get("text"))
                    if text.strip():
                        self._emit("assistant", "Assistant", text, {})
                elif btype == "thinking":
                    if streamed:
                        continue
                    self._emit("thinking", "Thinking", _text(block.get("thinking")), {})
                elif btype == "tool_use":
                    name = str(block.get("name") or "tool")
                    self._emit("tool", f"Tool · {name}",
                               _text(tool_input(block.get("input")), 2000),
                               {"tool": name})
            return
        if kind == "user":
            message = event.get("message") or {}
            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                if str(block.get("type")) == "tool_result":
                    self._emit("tool_result", "Tool result",
                               _text(tool_result(block.get("content")), 4000),
                               {"is_error": bool(block.get("is_error"))})
            return
        if kind == "result":
            self.session_id = str(event.get("session_id") or self.session_id)
            self.result_text = _text(event.get("result") or event.get("error") or "")
            self.ok = not bool(event.get("is_error")) and \
                str(event.get("subtype") or "") in ("success", "")
            self._merge_usage(event.get("usage") or {}, event)
            self._emit("result", "完成" if self.ok else "失败", self.result_text,
                       {"cost_usd": event.get("total_cost_usd")})
            if self.on_result:
                self.on_result()
            return
        self._emit("raw", kind or "event", _text(json.dumps(event, ensure_ascii=False)), {})

    # ---- 逐块增量 ----------------------------------------------------------

    def _stream_event(self, event: dict[str, Any]) -> None:
        etype = str(event.get("type") or "")
        if etype == "content_block_start":
            block = event.get("content_block") or {}
            btype = str(block.get("type") or "")
            index = int(event.get("index") or 0)
            if btype == "thinking":
                step = self._emit("thinking", "思考中…", "", {"partial": True})
                self.blocks[index] = {"kind": "thinking", "step_id": step["id"],
                                      "seq": step.get("seq") or 0,
                                      "text": "", "written": 0.0}
                self.streamed_blocks += 1
            elif btype == "text":
                step = self._emit("assistant", "Assistant", "", {"partial": True})
                self.blocks[index] = {"kind": "assistant", "step_id": step["id"],
                                      "seq": step.get("seq") or 0,
                                      "text": "", "written": 0.0}
                self.streamed_blocks += 1
            return
        if etype == "content_block_delta":
            index = int(event.get("index") or 0)
            state = self.blocks.get(index)
            if state is None:
                return
            delta = event.get("delta") or {}
            dtype = str(delta.get("type") or "")
            chunk = ""
            if dtype == "text_delta":
                chunk = str(delta.get("text") or "")
            elif dtype == "thinking_delta":
                chunk = str(delta.get("thinking") or "")
            if not chunk:
                return
            state["text"] += chunk
            self._push(state, final=False)
            return
        if etype == "content_block_stop":
            index = int(event.get("index") or 0)
            state = self.blocks.pop(index, None)
            if state is not None:
                self._push(state, final=True)
            return

    def _push(self, state: dict[str, Any], *, final: bool) -> None:
        now = self._now()
        if not final and now - float(state.get("written") or 0) < THROTTLE_SECONDS:
            return
        state["written"] = now
        text = _text(state["text"])
        title = "Thinking" if state["kind"] == "thinking" else "Assistant"
        if final:
            self._update(state["step_id"], title, text)
        step = {"id": state["step_id"], "seq": int(state.get("seq") or 0),
                "kind": state["kind"], "title": title, "detail": text,
                "payload": {"partial": not final}, "created_at": ""}
        self._publish({"type": "step_update", "step": step})

    # ---- usage -------------------------------------------------------------

    def _merge_usage(self, usage: dict[str, Any],
                     result: dict[str, Any] | None = None) -> None:
        merged = dict(self.usage)
        for key in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                    "cache_creation_input_tokens"):
            value = usage.get(key)
            if value is not None:
                merged[key] = int(value)
        if result:
            model_usage = result.get("modelUsage") or {}
            if isinstance(model_usage, dict) and model_usage:
                first = next(iter(model_usage.values()))
                if isinstance(first, dict):
                    merged["context_window"] = first.get("contextWindow")
                    merged["canonical_model"] = first.get("canonicalModel")
            if result.get("total_cost_usd") is not None:
                merged["total_cost_usd"] = result.get("total_cost_usd")
            if result.get("duration_ms") is not None:
                merged["duration_ms"] = result.get("duration_ms")
        if self.model:
            merged["model"] = self.model
        self.usage = merged
