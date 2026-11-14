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
import os
from pathlib import Path
from typing import Any

# 通用 CLI 通道的命令模板：`--format json` 是适配器解析的前提；`--auto` 让无人值守时不必等审批
# （`opencode run --help` 原文："auto-approve permissions that are not explicitly denied (dangerous!)"）。
# 模板必须含 `{prompt}`（平台校验 `cli_executor_template_requires_prompt_placeholder`）。
OPENCODE_WORKER_TEMPLATE = ("opencode", "run", "--format", "json", "--auto", "{prompt}")

# 平台渠道 provider 的 opencode 配置目录/文件名（方案 A，2026-10-02）。
# 每个项目一份（`providers/<project_id>.json`），经 `OPENCODE_CONFIG` 交给执行体——
# **项目隔离**：不同项目各指各的平台代理端点与令牌，互不串用（防跨项目泄漏/竞态）。
OPENCODE_PROVIDER_DIRNAME = "opencode-providers"

# 与文件工具清单保持一致：这些工具产生"文件被改"而不是"调了个工具"
_FILE_TOOLS = ("write", "edit")


def platform_provider_config_path(config_root: Path, project_id: str) -> Path:
    """某项目的平台渠道 provider 配置文件路径（`<root>/opencode-providers/<project_id>.json`）。

    `project_id` 必须非空且不带路径分隔符（uuid 形态天然满足）；防御性检查防止把
    恶意/损坏的 project_id 拼成路径逃逸。
    """

    key = str(project_id or "").strip()
    if not key or "/" in key or "\\" in key or ".." in key:
        raise ValueError("platform_provider_project_id_invalid")
    return Path(config_root) / OPENCODE_PROVIDER_DIRNAME / f"{key}.json"


def write_platform_provider_config(
    config_root: Path,
    project_id: str,
    *,
    provider: str,
    base_url: str,
    api_key: str,
    models: list[str],
) -> Path:
    """把平台渠道 provider 写成**项目隔离**的 opencode 配置（0600），返回文件路径。

    - 形状与 `deploy/cloud-agent/configure-opencode-provider.sh` 的合并写法一致
      （npm=@ai-sdk/openai-compatible、options.baseURL/apiKey、models 逐个登记）；
    - **合并而非覆盖**：文件已存在时只更新本 provider 条目，其它内容原样保留；
    - `models` 为空时抛错：没有渠道模型就不该写配置（**不静默回落**到全局配置的 provider）；
    - 文件 0600（Windows 上 os.chmod 只影响只读位，尽力而为；令牌本来就在本机凭据里，不新增暴露面）。
    """

    key = str(provider or "").strip()
    base = str(base_url or "").strip().rstrip("/")
    token = str(api_key or "").strip()
    cleaned = [str(item).strip() for item in (models or []) if str(item).strip()]
    if not key:
        raise ValueError("platform_provider_name_required")
    if not base.startswith(("http://", "https://")):
        raise ValueError("platform_provider_base_url_invalid")
    if not token:
        raise ValueError("platform_provider_api_key_required")
    if not cleaned:
        # 空模型清单 = 平台侧没有可用渠道：明确失败，不写一个"看起来配好了"的空壳
        raise ValueError("platform_provider_models_required")

    path = platform_provider_config_path(config_root, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    config: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                config = loaded
        except ValueError:
            config = {}  # 坏文件重建（0600、本机执行体用户属主，风险可控）
    config.setdefault("$schema", "https://opencode.ai/config.json")
    providers = config.setdefault("provider", {})
    if not isinstance(providers, dict):
        providers = {}
        config["provider"] = providers
    providers[key] = {
        "npm": "@ai-sdk/openai-compatible",
        "name": key,
        "options": {"baseURL": base, "apiKey": token},
        "models": {model: {"name": model} for model in cleaned},
    }
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows 的 chmod 语义受限；目录已在用户私有状态目录下
    return path


def read_platform_provider_models(config_root: Path, project_id: str) -> list[str]:
    """读某项目平台渠道 provider 已登记的模型名（探测/展示用；读不到就是空列表，不编）。"""

    path = platform_provider_config_path(config_root, project_id)
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entry = (config or {}).get("provider", {}).get("synapforge") if isinstance(config, dict) else None
    models = (entry or {}).get("models") if isinstance(entry, dict) else None
    if not isinstance(models, dict):
        return []
    return [str(name) for name in models if str(name).strip()]


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


__all__ = [
    "OPENCODE_PROVIDER_DIRNAME",
    "OPENCODE_WORKER_TEMPLATE",
    "parse_opencode_jsonl",
    "platform_provider_config_path",
    "read_platform_provider_models",
    "summarize_opencode_result",
    "write_platform_provider_config",
]