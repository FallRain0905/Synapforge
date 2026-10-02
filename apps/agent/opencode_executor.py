"""opencode 执行体（通用 CLI 通道的协议适配层）。

为什么需要它：`worker_executor="cli"` 的边界是"stdout 即结果"——`opencode run --format json`
的 stdout 是**事件流**而不是结果文本，直接当结果会把一整串 JSON 塞进 Run 摘要和成果物。
所以这一层做两件事：**从事件里取出人看的摘要**，以及**把事件里的用量读出来**（COST-1 的 token
预算判定只在这条路径上拿得到数）。

形状来自实测样本（不是猜的）：2026-09-23 在云端执行体服务器（XX.XX.XX.XX，Ubuntu 22.04）
上装 opencode **1.18.32**，`opencode run --format json` 的真实输出（原文见
`docs/handoffs/CLOUD_1_OPENCODE_ADAPTER_HANDOFF.md`）：

```json
{"type":"step_start","part":{"type":"step-start",…}}
{"type":"text","part":{"type":"text","text":"OK",…}}
{"type":"tool_use","part":{"type":"tool","tool":"bash","callID":"call_…","state":{"status":"completed","input":{…},"output":"OK"}}}
{"type":"step_finish","part":{"type":"step-finish","reason":"stop","tokens":{"total":7776,"input":1546,"output":66,"reasoning":20,"cache":{"write":0,"read":6144}},"cost":0}}
```

**没有采到的部分（如实说明）**：错误事件的形状。用坏模型名跑时它**挂住不报错**（无输出、无退出码，
只能靠外层超时杀掉），所以本模块**不按任何错误事件类型判失败**——成功判定以退出码为准，
未知事件类型按 codex 解析器同口径跳过。opencode 还报了 `reasoning` 与 `cache` 明细，
平台的用量契约（`RunUsage`）目前只收 input/output/total，这些暂时丢弃（不虚报）。
"""

from __future__ import annotations

import json
from typing import Any

# 通用 CLI 通道的命令模板：`--format json` 是适配器解析的前提；`--auto` 让无人值守时不必等审批
# （`opencode run --help` 原文："auto-approve permissions that are not explicitly denied (dangerous!)"）。
# 模板必须含 `{prompt}`（平台校验 `cli_executor_template_requires_prompt_placeholder`）。
OPENCODE_WORKER_TEMPLATE = ("opencode", "run", "--format", "json", "--auto", "{prompt}")

# 与 executor_events._OPENCODE_FILE_TOOLS 保持一致：这些工具产生"文件被改"而不是"调了个工具"
_FILE_TOOLS = ("write", "edit")


def parse_opencode_jsonl(raw: str) -> dict:
    """解析 `opencode run --format json` 的输出。

    返回：{"final_message", "messages", "tool_events", "file_events", "error_events",
           "transient_events", "event_count", "unparsed", "usage", "cost", "session_id"}
    无法解析的行计入 `unparsed`（不静默丢弃）。

    `session_id` 是 opencode 自己的会话句柄（每行事件顶层的 `sessionID`）——「我的智能体」的多轮上下文
    靠它续接（下一轮把 `--session <它>` 交给执行体），所以必须从这里取出来。
    """

    messages: list[str] = []
    tool_events = 0
    file_events = 0
    error_events: list[str] = []
    transient_events: list[str] = []
    event_count = 0
    unparsed = 0
    usage_input = 0
    usage_output = 0
    usage_turns = 0
    cost = 0.0
    saw_cost = False
    session_id = ""

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            unparsed += 1
            continue
        if not isinstance(event, dict):
            unparsed += 1
            continue
        event_count += 1
        if not session_id:
            candidate = event.get("sessionID")
            if isinstance(candidate, str) and candidate.strip():
                session_id = candidate.strip()
        event_type = str(event.get("type") or "")
        part = event.get("part")
        part = part if isinstance(part, dict) else {}

        if event_type == "text":
            text = str(part.get("text") or "").strip()
            if text:
                messages.append(text)
        elif event_type == "tool_use":
            state = part.get("state")
            state = state if isinstance(state, dict) else {}
            if str(state.get("status") or "") != "completed":
                continue  # pending/running 不是结果
            if str(part.get("tool") or "") in _FILE_TOOLS:
                file_events += 1
            else:
                tool_events += 1
        elif event_type == "step_finish":
            tokens = part.get("tokens")
            if isinstance(tokens, dict):
                usage_input += int(tokens.get("input") or 0)
                usage_output += int(tokens.get("output") or 0)
                usage_turns += 1
            if isinstance(part.get("cost"), (int, float)):
                cost += float(part["cost"])
                saw_cost = True
        # `step_start` 等其它类型是预期内的，不算坏行

    usage: dict[str, Any] = {}
    if usage_turns:
        usage = {
            "input_tokens": usage_input,
            "output_tokens": usage_output,
            "total_tokens": usage_input + usage_output,
            "turns": usage_turns,
            "source": "opencode-jsonl",
        }

    return {
        "final_message": messages[-1] if messages else "",
        "messages": messages,
        "tool_events": tool_events,
        "file_events": file_events,
        "error_events": error_events,
        "transient_events": transient_events,
        "event_count": event_count,
        "unparsed": unparsed,
        "usage": usage,
        "cost": cost if saw_cost else None,
        "session_id": session_id,
    }


def summarize_opencode_result(exit_code: int | None, parsed: dict, run_id: str) -> tuple[bool, str]:
    """把一次 opencode 执行压成 `(success, summary)`——判定规则集中在这里，便于测试与解释。

    成功 = **退出码 0**。没有错误事件可判（形状未采到样本，见模块 docstring），所以不假装能判；
    坏行、用量、花费都写进摘要，让人看得见"这次读到了什么"。
    """

    usage = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
    diagnostics = (
        f"opencode exit={exit_code} run={run_id} events={parsed.get('event_count', 0)}"
        f" tools={parsed.get('tool_events', 0)} files={parsed.get('file_events', 0)}"
    )
    if usage:
        diagnostics += f" | tokens={usage.get('total_tokens', 0)}（{usage.get('source', '')}）"
    cost = parsed.get("cost")
    if cost is not None:
        diagnostics += f" | cost={cost:g}"
    if parsed.get("error_events"):
        diagnostics += f" | errors: {'; '.join(str(item) for item in parsed['error_events'])[:300]}"
    if parsed.get("transient_events"):
        diagnostics += f" | warnings: {'; '.join(str(item) for item in parsed['transient_events'])[:300]}"
    if parsed.get("unparsed"):
        diagnostics += f" | unparsed_lines={parsed['unparsed']}"

    # 回答放在最前面（与 codex 摘要同一约定：界面直接展示这段文本，夹在诊断里会被截断切掉半句）
    final_message = str(parsed.get("final_message") or "").strip()
    if final_message:
        summary = f"{final_message}\n\n---\n{diagnostics}"
    else:
        summary = f"（本次执行没有产出回复文本）\n\n---\n{diagnostics}"

    return exit_code == 0, summary


__all__ = ["OPENCODE_WORKER_TEMPLATE", "parse_opencode_jsonl", "summarize_opencode_result"]