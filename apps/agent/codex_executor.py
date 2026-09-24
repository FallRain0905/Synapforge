"""把 Codex CLI 接成平台执行体（agentd worker 侧）。

三件事：
1. `detect_codex_cli()`：从显式参数、CODEX_CLI_PATH、ChatGPT 桌面端解包目录、PATH 里找 codex；
2. `parse_codex_jsonl()`：把 `codex exec --json` 的事件流解析成可读摘要（最终回复 + 工具/文件事件计数），
   而不是把整段 JSONL 原样塞进运行台账；
3. `_execute_task` 里新增 `worker_executor="codex"` 分支：用平台自己的 `CodexAdapter` 组命令
   （安全校验随之内建：拒绝 --dangerously-*、路径逃逸与配置覆盖类参数）。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from apps.agent.cli_adapters import CliAdapterError, CodexAdapter  # noqa: E402


def detect_codex_cli(explicit: str | None = None) -> str | None:
    """按可靠性排序探测 codex CLI：显式参数 → CODEX_CLI_PATH → 桌面端解包目录 → PATH。"""

    candidates: list[str] = []
    if explicit:
        candidates.append(explicit)
    env_path = os.environ.get("CODEX_CLI_PATH")
    if env_path:
        candidates.append(env_path)

    # ChatGPT 桌面端会把 CLI 解包到 %LOCALAPPDATA%\OpenAI\Codex\bin\<hash>\codex.exe
    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        bin_root = Path(local_appdata) / "OpenAI" / "Codex" / "bin"
        if bin_root.is_dir():
            versioned = [entry / "codex.exe" for entry in bin_root.iterdir() if entry.is_dir()]
            versioned = [path for path in versioned if path.exists()]
            versioned.sort(key=lambda path: path.stat().st_mtime, reverse=True)
            candidates.extend(str(path) for path in versioned)

    # WindowsApps 里的别名（商店包注册的 ExecutionAlias 会出现在 WindowsApps 目录）
    if local_appdata:
        alias = Path(local_appdata) / "Microsoft" / "WindowsApps" / "codex.exe"
        if alias.exists():
            candidates.append(str(alias))

    on_path = shutil.which("codex")
    if on_path:
        candidates.append(on_path)

    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def parse_codex_jsonl(raw: str) -> dict:
    """解析 `codex exec --json` 的输出。

    返回：{"final_message", "messages", "tool_events", "file_events", "error_events", "event_count", "usage"}
    无法解析的行计入 `unparsed`（不静默丢弃）。
    """

    messages: list[str] = []
    tool_events = 0
    file_events = 0
    error_events: list[str] = []
    transient_events: list[str] = []
    usage: dict = {}
    event_count = 0
    unparsed = 0

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            unparsed += 1
            continue
        event_count += 1
        event_type = str(event.get("type", ""))
        if event_type == "item.completed":
            item = event.get("item") or {}
            item_type = str(item.get("type", ""))
            if item_type == "agent_message":
                text = str(item.get("text", "")).strip()
                if text:
                    messages.append(text)
            elif item_type in {"tool_call", "command_execution", "function_call"}:
                tool_events += 1
            elif item_type in {"file_change", "patch_apply", "file_write"}:
                file_events += 1
        elif event_type in {"error", "turn.failed"}:
            message = str(event.get("message") or event.get("error") or event_type)
            if _is_transient_error(message):
                transient_events.append(message)
            else:
                error_events.append(message)
        elif event_type == "turn.completed":
            if isinstance(event.get("usage"), dict):
                usage = event["usage"]

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
    }


# Codex 自己会重试的瞬断提示：出现它们不代表这一轮失败——退出码 0 且拿到最终回复时，
# 任务仍然是成功的（否则每次网络抖动都会把已经完成的任务判成 FAILED）。
_TRANSIENT_ERROR_PATTERNS = (
    "reconnecting",
    "stream disconnected before completion",
    "stream closed before response.completed",
)


def _is_transient_error(message: str) -> bool:
    lowered = message.lower()
    return any(pattern in lowered for pattern in _TRANSIENT_ERROR_PATTERNS)


def summarize_codex_result(exit_code: int | None, parsed: dict, run_id: str) -> tuple[bool, str]:
    """把一次 Codex 执行压成 `(success, summary)`——判定规则集中在这里，便于测试与解释。

    成功 = 退出码 0 且没有**致命**错误事件；Codex 自己的瞬断重试提示（`Reconnecting...` 等）
    只作为 warnings 出现在摘要里，不会把已经拿到答案的任务判成失败。
    """

    diagnostics = (
        f"codex exit={exit_code} run={run_id} events={parsed.get('event_count', 0)}"
        f" tools={parsed.get('tool_events', 0)} files={parsed.get('file_events', 0)}"
    )
    if parsed.get("error_events"):
        diagnostics += f" | errors: {'; '.join(parsed['error_events'])[:300]}"
    if parsed.get("transient_events"):
        diagnostics += f" | warnings: {'; '.join(parsed['transient_events'])[:300]}"
    if parsed.get("unparsed"):
        diagnostics += f" | unparsed_lines={parsed['unparsed']}"

    # 回答放在**最前面**：界面（列表、详情、复制按钮）都直接展示这段文本，
    # 夹在诊断信息中间时会被各种截断切掉半句话。原文保留，不做二次截断——
    # 截断由 `_complete_run` 的字段上限统一负责。
    final_message = str(parsed.get("final_message") or "").strip()
    if final_message:
        summary = f"{final_message}\n\n---\n{diagnostics}"
    else:
        summary = f"（本次执行没有产出回复文本）\n\n---\n{diagnostics}"

    success = exit_code == 0 and not parsed.get("error_events")
    return success, summary


def build_codex_command(codex_path: str, prompt: str, *, sandbox: str = "read-only", ephemeral: bool = True) -> list[str]:
    """用平台 CodexAdapter 组命令（内含安全校验），再补 sandbox/ephemeral 这类运行参数。

    注意：`--sandbox` / `--ephemeral` 不在 adapter 的拒绝清单里，但 `danger-full-access`
    会被 adapter 显式拒绝——所以这里永远只走 read-only / workspace-write。
    """

    if sandbox not in {"read-only", "workspace-write"}:
        raise CliAdapterError("codex_sandbox_mode_not_allowed")
    adapter = CodexAdapter(executable=codex_path)
    command = list(adapter.build_command(prompt, ephemeral=ephemeral))
    # 附加参数必须插在 PROMPT 之前：codex exec 是 [OPTIONS] [PROMPT]，把参数跟在
    # positional 之后有被当成尾随参数的风险（提示词会被污染）。
    extras: list[str] = []
    if "--sandbox" not in command and "-s" not in command:
        extras.extend(["--sandbox", sandbox])
    if ephemeral and "--ephemeral" not in command:
        extras.append("--ephemeral")
    if "--skip-git-repo-check" not in command:
        # 任务工作区常常不是 git 仓库；不跳过会直接失败
        extras.append("--skip-git-repo-check")
    command[2:2] = extras
    adapter._reject_unsafe_flags(command)  # 补参数后再校验一次
    return command


__all__ = [
    "build_codex_command",
    "detect_codex_cli",
    "parse_codex_jsonl",
    "summarize_codex_result",
]