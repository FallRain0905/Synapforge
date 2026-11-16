"""执行体适配：把角色的配置变成一条可执行的命令 + 一段给 Agent 的 prompt。

第一版支持：
- `claude`：Claude Code 的 `-p + --mcp-config`（本仓库已实测）；
- `command`：用户填的通用命令模板，占位符 `{prompt}` `{mcp_config}` `{workspace}`
  `{agent_id}` `{project_id}`；不启用 shell。

新增执行体只需在这里加一个 `build_*` 分支；supervisor 不关心具体 CLI。
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import _REPO_ROOT
from . import providers as studio_providers

MCP_SCRIPT = _REPO_ROOT / "scripts" / "synapforge_mcp.py"


def mcp_launch(*, agent_id: str, project_id: str,
               state_path: str | None) -> tuple[str, list[str]]:
    """MCP stdio 服务的启动命令。

    源码形态用 `python scripts/synapforge_mcp.py`；冻结后脚本不在磁盘上，
    改用同目录的 `synapforge-mcp.exe`（控制台子系统，GUI exe 没有 stdout）。
    """
    base = ["--agent", agent_id, "--project", project_id]
    if state_path:
        base += ["--state", str(state_path)]
    if getattr(sys, "frozen", False):
        mcp_exe = Path(sys.executable).with_name(
            "synapforge-mcp.exe" if os.name == "nt" else "synapforge-mcp")
        if mcp_exe.exists():
            return str(mcp_exe), base
    return sys.executable, [str(MCP_SCRIPT), *base]


class BackendError(RuntimeError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Invocation:
    command: list[str]
    cwd: str
    env: dict[str, str]
    timeout_seconds: int
    backend: str


@dataclass(frozen=True)
class AgentProbe:
    id: str
    name: str
    available: bool
    path: str = ""
    version: str = ""
    note: str = ""


def find_claude() -> str | None:
    """Claude Code 可能装在 VS Code 扩展里，不在 PATH 上。"""

    direct = shutil.which("claude")
    if direct:
        return direct
    base = Path.home() / ".vscode" / "extensions"
    if base.exists():
        candidates = sorted(base.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"),
                            reverse=True)
        if candidates:
            return str(candidates[0])
    return None


def _version_of(executable: str, args: tuple[str, ...] = ("--version",)) -> str:
    try:
        completed = subprocess.run([executable, *args], capture_output=True, text=True,
                                   encoding="utf-8", errors="replace", timeout=8)
        return (completed.stdout or completed.stderr or "").strip().splitlines()[0][:120]
    except Exception:  # noqa: BLE001 — 探测失败不应影响 studio 启动
        return ""


def probe_agents() -> list[AgentProbe]:
    """已安装/可用的执行体清单；command 总是可用（用户自己填模板）。"""

    known = [
        ("claude", "Claude Code", find_claude),
        ("codex", "Codex", lambda: shutil.which("codex")),
        ("opencode", "opencode", lambda: shutil.which("opencode")),
        ("zcode", "ZCode", lambda: shutil.which("zcode")),
        ("kimi", "Kimi Code", lambda: shutil.which("kimi")),
        ("workbuddy", "WorkBuddy", lambda: shutil.which("workbuddy")),
    ]
    out: list[AgentProbe] = []
    for agent_id, name, resolver in known:
        try:
            path = resolver()
        except Exception:  # noqa: BLE001
            path = None
        out.append(AgentProbe(id=agent_id, name=name, available=bool(path),
                              path=path or "",
                              version=_version_of(path) if path else "",
                              note="" if path else "未在 PATH 中找到"))
    out.append(AgentProbe(id="command", name="自定义命令模板", available=True,
                          note="填一条带 {prompt} 的命令；适合 ZCode/WorkBuddy/Kimi 等"))
    return out


def write_mcp_config(path: Path, *, agent_id: str, project_id: str,
                     state_path: str | None,
                     extra_servers: dict[str, Any] | None = None) -> Path:
    """写本次调用的 MCP 配置：`synapforge`（任务板）必需，可再合并角色自己的服务器。"""

    command, args = mcp_launch(agent_id=agent_id, project_id=project_id,
                               state_path=state_path)
    servers: dict[str, Any] = {"synapforge": {
        "command": command,
        "args": args,
    }}
    for name, config in (extra_servers or {}).items():
        if name == "synapforge":
            continue
        servers[str(name)] = config
    config = {"mcpServers": servers}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _fill(template: str, values: dict[str, str]) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def _split_command(tokens: list[str]) -> list[str]:
    """单个字符串模板按 shell 规则拆；列表原样使用。不用 shell=True。"""

    if len(tokens) == 1 and any(ch.isspace() for ch in tokens[0]):
        raw = tokens[0]
        parts = shlex.split(raw, posix=(os.name != "nt"))
        if os.name == "nt":
            parts = [p.strip('"') for p in parts]
        return parts
    return list(tokens)


def _base_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    if extra:
        env.update(extra)
    return env


def _timeout_for(role: dict[str, Any], policy: dict[str, Any]) -> int:
    budget = role.get("budget") or {}
    value = budget.get("timeout_seconds") or policy.get("role_timeout_seconds") or 1800
    try:
        return max(30, int(value))
    except (TypeError, ValueError):
        return 1800


def _write_claude_settings(role: dict[str, Any], provider: dict[str, Any],
                           model: str, api_key: str) -> Path | None:
    """把渠道的 ANTHROPIC_* 写进一份临时 settings，供 `--settings` 覆盖本机默认。"""

    run_dir = str(role.get("_run_dir") or "").strip()
    if not run_dir:
        return None
    path = Path(run_dir) / f"claude-settings-{role.get('id') or 'role'}.json"
    try:
        env = studio_providers.env_for(provider, model, api_key=api_key)
        payload = {"env": {key: value for key, value in env.items()
                           if key.startswith("ANTHROPIC_")}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path
    except OSError:
        return None


def build_invocation(role: dict[str, Any], *, prompt: str, mcp_config: Path,
                     workspace: str, agent_id: str, project_id: str,
                     policy: dict[str, Any], state_path: str | None) -> Invocation:
    backend = str(role.get("backend") or "claude")
    timeout = _timeout_for(role, policy)
    extra_args = [str(a) for a in (role.get("args") or [])]
    env_extra = {
        "SYNAPFORGE_AGENT_ID": agent_id,
        "SYNAPFORGE_PROJECT_ID": project_id,
        "SYNAPFORGE_MCP_CONFIG": str(mcp_config),
        "SYNAPFORGE_WORKSPACE": workspace,
    }
    if state_path:
        env_extra["SYNAPFORGE_STATE"] = str(state_path)
    # 第三方模型名（glm-5.3 等）Claude Code 不认识，默认会按 200k 上下文提醒；
    # 关掉这条提示，避免把噪声写进每次日志。也避免它走「等 API 确认窗口」而卡住。
    env_extra.setdefault("CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT", "1")
    env_extra.setdefault("CLAUDE_CODE_MAX_CONTEXT_TOKENS", "1000000")
    # 集群角色选了渠道时，Claude Code 会合并 ~/.claude/settings.json 的 env，
    # 必须显式覆盖，否则会继续打到本机默认地址。
    provider = role.get("_provider")
    if provider:
        env_extra.update({key: value for key, value in
                          studio_providers.env_for(provider, str(role.get("model") or ""),
                                                   api_key=str(role.get("_api_key") or "")).items()
                          if key.startswith("ANTHROPIC_")})

    if backend == "claude":
        executable = find_claude()
        if not executable:
            raise BackendError("claude_not_found",
                               "未找到 claude；安装 Claude Code 或改用 command 执行体")
        allow_bash = bool(policy.get("allow_bash", False)) or bool(
            (role.get("budget") or {}).get("allow_bash", False))
        tools = "mcp__synapforge,Write,Read,Edit" + (",Bash" if allow_bash else "")
        command = [executable]
        if provider:
            # 与单 Agent 路径一致：写一份临时 settings，用 --settings 覆盖
            # ~/.claude/settings.json 的 env（否则渠道切换不生效，会打到旧地址）
            settings_path = _write_claude_settings(
                role, provider, str(role.get("model") or ""), str(role.get("_api_key") or ""))
            if settings_path:
                command += ["--settings", str(settings_path)]
        if role.get("model"):
            command += ["--model", str(role["model"])]
        command += [
            "-p", prompt,
            "--mcp-config", str(mcp_config),
            "--strict-mcp-config",
            "--permission-mode", "acceptEdits",
            "--allowedTools", tools,
            # 集群角色也要结构化步骤流，才能和单 Agent 一样逐块显示
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
        command += extra_args
        return Invocation(command=command, cwd=workspace,
                          env=_base_env(env_extra), timeout_seconds=timeout,
                          backend=backend)

    if backend == "command":
        template = _split_command(extra_args)
        if not template:
            raise BackendError("command_template_missing", "command 执行体需要命令模板")
        values = {"prompt": prompt, "mcp_config": str(mcp_config), "workspace": workspace,
                  "agent_id": agent_id, "project_id": project_id,
                  "state": str(state_path or "")}
        command = [_fill(token, values) for token in template]
        if not any("{prompt}" in token for token in template):
            command.append(prompt)
        return Invocation(command=command, cwd=workspace,
                          env=_base_env(env_extra), timeout_seconds=timeout,
                          backend=backend)

    raise BackendError("backend_unknown", f"未知执行体：{backend}")


def build_prompt(*, cluster: dict[str, Any], role: dict[str, Any],
                 roles: list[dict[str, Any]], tasks: list[dict[str, Any]],
                 events: list[dict[str, Any]]) -> str:
    is_lead = str(role.get("id")) == str(cluster.get("lead_role_id"))
    role_by_agent = {str(r.get("agent_id")): r for r in roles}
    roster = "\n".join(
        f"- {r['name']}（{'lead' if str(r['id']) == str(cluster.get('lead_role_id')) else 'worker'}，"
        f"agent_id={r['agent_id']}）"
        for r in roles)
    board_lines = []
    for task in tasks[:24]:
        assignee = role_by_agent.get(str(task.get("assignee") or ""))
        board_lines.append(
            f"- [{task.get('status')}] {task.get('title')}"
            f" → {assignee['name'] if assignee else (task.get('assignee') or '未指派')}"
            f"（id={str(task.get('id'))[:8]}）")
    event_lines = [f"- {e.get('event_type')} · {e.get('actor')}" for e in events[-10:]]

    duties = str(role.get("system_prompt") or "").strip() or (
        "按 lead 的派发执行任务，产出写到工作区并登记成果物。" if not is_lead
        else "拆解目标、给 worker 派活，并在最后做终审。")
    origin = ""
    if role.get("local_agent_id"):
        origin = (f"\n（这个角色复用了本地 Agent「{role.get('local_agent_id')}」的渠道、"
                  "角色卡与扩展配置。）")

    lines = [
        f"你是本地 Agent 集群「{cluster.get('name')}」中的角色「{role.get('name')}」。",
        f"集群目标：{cluster.get('objective')}",
        f"你的职责：{duties}{origin}",
        f"执行体：{role.get('backend')}" + (f" · model={role.get('model')}" if role.get("model") else ""),
        f"工作区：{role.get('workspace')}",
        "",
        "角色名册：",
        roster,
        "",
        "当前任务板（最近 24 条）：",
        "\n".join(board_lines) or "（空）",
        "",
        "最近事件（最近 10 条）：",
        "\n".join(event_lines) or "（空）",
        "",
        "规则（必须遵守）：",
        "1. 先调用 synapforge_inbox，看 assigned_to_me / in_flight_tasks / to_answer / to_consume。",
        "2. 一次只处理最优先的一项；处理完立刻停，不要空转、不要臆造任务。",
        "3. 缺信息 → synapforge_ask 向对应角色提问（说清 reason），然后停；"
        "别人问你 → synapforge_answer，不确定就用 unavailable。",
        "4. 产出必须写成工作区文件，用 synapforge_artifact(action=publish) 登记；"
        "然后 synapforge_finish 收尾。",
        "5. 无法按原口径完成 → synapforge_concern，不要伪造成功。",
        "6. 不要直接改别人的工作区；跨角色协作走 ask / handoff / 任务板。",
    ]
    if is_lead:
        lines += [
            "",
            "lead 额外要求：",
            "- 如果任务板为空或 worker 没有可做的任务：把目标拆成 2-4 个可独立完成的任务，"
            "用 synapforge_task_create 指派给具体 worker（assignee_agent_id）。",
            "- 不要替 worker 写具体产出；你的产出是计划、分工、终审结论。",
            "- 收到「集群终审」任务时，检查成果物是否覆盖目标，给出明确结论并 finish。",
        ]
    else:
        lines += [
            "",
            "worker 额外要求：",
            "- 只做派给你的任务；完成后写文件、登记成果物、finish。",
            "- 需要别的角色提供接口/事实/上游产物时，先 ask，不要猜。",
        ]
    return "\n".join(lines)


def role_process_env(role: dict[str, Any], *, state_path: str | None) -> dict[str, str]:
    env = _base_env()
    env["SYNAPFORGE_ROLE_ID"] = str(role.get("id") or "")
    env["SYNAPFORGE_ROLE_NAME"] = str(role.get("name") or "")
    if state_path:
        env["SYNAPFORGE_STATE"] = str(state_path)
    return env


@dataclass
class PromptContext:
    """给 supervisor 的只读快照，避免它把 Store 传进 backends。"""

    cluster: dict[str, Any]
    role: dict[str, Any]
    roles: list[dict[str, Any]]
    tasks: list[dict[str, Any]]
    events: list[dict[str, Any]] = field(default_factory=list)
