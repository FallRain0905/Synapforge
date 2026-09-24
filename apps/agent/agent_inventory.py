"""本机执行体清单：探测、缓存与上报（DP-2-03）。

桌面端要回答"这台机器上现在能用什么干活"，所以清单必须来自**真实探测**，
不是配置里的声明。探测会真的起子进程（`codex --version`、`claude --version`），
因此带 TTL 缓存：

- 默认 300 秒内复用上次结果（心跳每 15 秒一次，不能每次都探测）；
- `refresh()` 强制重探（托盘「立即重扫本机 Agent」、或内核 `/agents/rescan`）；
- 探测失败如实记 `state`（`NOT_INSTALLED` / `UNSUPPORTED` / `ERROR`），不谎报"可用"。

清单的两个消费方：心跳的 `adapter_versions`（平台 `/devices` 展示）与内核 `/status`
（本地状态页展示）。两者读同一份缓存，避免各探一次。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

DEFAULT_TTL_SECONDS = 300.0
# opencode 的模型探测要读它自己的配置与 provider 目录：**冷启动可能十几秒**（实测被 12 秒上限掐断过，
# 结果被缓存成空），所以给宽一点；失败仍如实留空，不编。
OPENCODE_MODELS_TIMEOUT_SECONDS = 30.0
# 角色（agent）探测比模型轻（不联网），给它 20 秒足够；失败同样如实留空。
OPENCODE_ROLES_TIMEOUT_SECONDS = 20.0

# 平台在「我的智能体」里额外暴露的**内置**可选模式：它们没有角色文件，但有明确用途。
# `build`（默认全工具）不在这里——页面上它就是「默认（无角色）」，重复出现只会让人困惑；
# `compaction` / `summary` / `title` / `explore` / `general` 是 opencode 的内部 agent，不该出现在用户的下拉里。
BUILTIN_SELECTABLE_AGENTS = {
    "plan": "计划模式 · 先出方案再动手，禁用编辑与写入（opencode 内置）",
}

# `opencode agent list` 的输出是 `名字 (模式)` 一行一个，后面跟权限 JSON（实测格式，逐行匹配）。
_AGENT_LIST_LINE = re.compile(r"^([A-Za-z0-9_.\-]+) \((?:primary|subagent|all)\)$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class AgentInventory:
    """本机可用执行体的探测结果与缓存。线程安全由调用方（内核 HTTP 线程/事件循环）保证。"""

    ttl_seconds: float = DEFAULT_TTL_SECONDS
    probe: Callable[[], list[dict[str, Any]]] | None = None
    clock: Callable[[], float] = time.monotonic
    _entries: list[dict[str, Any]] = field(default_factory=list)
    _checked_at: float = 0.0
    _checks: int = 0
    _warming: bool = False

    def __post_init__(self) -> None:
        if self.ttl_seconds < 0:
            raise ValueError("agent_inventory_ttl_invalid")
        if self.probe is None:
            self.probe = default_probe

    def entries(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        now = self.clock()
        stale = (now - self._checked_at) >= self.ttl_seconds
        if refresh or stale or not self._entries:
            self._entries = list(self.probe() or [])
            self._checked_at = now
            self._checks += 1
        return [dict(entry) for entry in self._entries]

    def adapter_versions(self, *, refresh: bool = False) -> dict[str, str]:
        """只上报**能真正执行任务**的执行体版本。

        `NOT_INSTALLED` / `UNSUPPORTED`（例如 Claude Code 的语义适配器尚未实现）/
        `ERROR` 都不上报：报了会让平台以为这台机器能接这类活。
        """

        versions: dict[str, str] = {}
        for entry in self.entries(refresh=refresh):
            adapter_id = str(entry.get("adapter_id") or "").strip()
            version = entry.get("version")
            if (
                adapter_id
                and entry.get("state") == "AVAILABLE"
                and isinstance(version, str)
                and version.strip()
            ):
                versions[adapter_id] = version.strip()
        return versions

    def warm(self) -> None:
        """后台预热：把清单先探一遍（含 `opencode models`，冷启动可能十几秒）。

        为什么要这么做：清单是**心跳**在读的（每 15 秒一次），而探测要起子进程——
        第一次探测若卡在心跳路径上，心跳会迟到，平台会以为设备掉线（实测踩过一次：
        探测超时 → 模型列表被缓存成空）。
        """

        if self._warming:
            return
        self._warming = True

        def run() -> None:
            try:
                self.entries(refresh=True)
            except Exception:  # noqa: BLE001 - 预热失败不影响任何功能，下次心跳照旧读缓存
                pass
            finally:
                self._warming = False

        threading.Thread(target=run, name="agent-inventory-warm", daemon=True).start()

    def models(self, *, refresh: bool = False) -> tuple[list[str], str]:
        """对话用的可用模型：`(models, default_model)`。

        只取**已探测成功**的 opencode 条目的自报值（探测失败或没装 → 空列表、空默认）；
        平台侧看到空列表就不会给出假选项（「我的智能体」的模型下拉据此渲染）。
        """

        for entry in self.entries(refresh=refresh):
            # 注意匹配方式：探测条目里的 `executable` 是**解析后的绝对路径**
            # （例如 /home/synapforge/.opencode/bin/opencode），拿它去比字面量 "opencode" 永远不相等
            # ——这一条被线上验证抓过（模型列表一直是空，但手工探测明明有 10 个模型）。
            executable = str(entry.get("executable") or "")
            adapter_id = str(entry.get("adapter_id") or "")
            if adapter_id != "opencode-cli" and Path(executable).name != "opencode":
                continue
            if entry.get("state") != "AVAILABLE":
                continue
            models = [str(item) for item in (entry.get("models") or []) if str(item).strip()]
            return models, str(entry.get("default_model") or "")
        return [], ""

    def roles(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """对话可选的角色（opencode 的 agent）：`[{name, description, executes}]`。

        与 `models()` 同一套口径：只取**探测成功**的 opencode 条目的自报值；探不到就是空列表——
        平台侧看到空列表就只显示「默认（无角色）」，**不给假选项**。
        """

        for entry in self.entries(refresh=refresh):
            executable = str(entry.get("executable") or "")
            adapter_id = str(entry.get("adapter_id") or "")
            if adapter_id != "opencode-cli" and Path(executable).name != "opencode":
                continue
            if entry.get("state") != "AVAILABLE":
                continue
            roles: list[dict[str, Any]] = []
            for item in entry.get("roles") or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                roles.append(
                    {
                        "name": name,
                        "description": str(item.get("description") or "").strip(),
                        "executes": bool(item.get("executes")),
                    }
                )
            return roles
        return []

    def summary(self, *, refresh: bool = False) -> dict[str, Any]:
        entries = self.entries(refresh=refresh)
        return {
            "checked_at": _now(),
            "count": len(entries),
            "available": [entry["adapter_id"] for entry in entries if entry.get("state") == "AVAILABLE"],
            "checks": self._checks,
            "entries": entries,
        }


# 通用 CLI 执行体（worker_executor == "cli"，提示词驱动命令模板）：装了就能用。
# 豆包桌面端不在其中——它是 GUI 应用，没有 CLI/自动化接口，接不进来（如实记录，不假装支持）。
# opencode 在其中的同时还有**协议适配**（`opencode run --format json` 的事件流有解析器，
# 见 opencode_executor.py）：探测仍只回答"装没装"，协议由 `agentd._event_protocol` 按模板首词决定。
GENERIC_CLI_EXECUTABLES = ("workbuddy", "zcode", "opencode")


def default_probe() -> list[dict[str, Any]]:
    """探测本机执行体：Codex（协议适配）、Claude Code（语义未实现，UNSUPPORTED）、
    通用 CLI（workbuddy / zcode / opencode，装了即可作为 worker_executor=cli 的执行体）。"""

    results: list[dict[str, Any]] = []
    results.append(_probe_codex())
    results.append(_probe_claude_code())
    for executable in GENERIC_CLI_EXECUTABLES:
        results.append(_probe_generic_cli(executable))
    return results


def _probe_codex() -> dict[str, Any]:
    checked_at = _now()
    try:
        from .codex_executor import detect_codex_cli
        from .cli_adapters import CodexAdapter
    except ImportError:  # 直接脚本执行（无包上下文）
        from codex_executor import detect_codex_cli  # type: ignore
        from cli_adapters import CodexAdapter  # type: ignore

    try:
        path = detect_codex_cli()
        probe = CodexAdapter(executable=path or "codex").probe()
        return {
            "adapter_id": probe.adapter_id,
            "state": probe.state,
            "version": probe.version,
            "executable": path or "codex",
            "capabilities": list(probe.capabilities or ()),
            "reason": probe.reason,
            "checked_at": checked_at,
        }
    except Exception as error:  # noqa: BLE001 - 探测异常如实记为 ERROR
        return {
            "adapter_id": "codex-cli",
            "state": "ERROR",
            "version": None,
            "executable": "codex",
            "checked_at": checked_at,
            "detail": f"{type(error).__name__}:{error}",
        }


def _probe_claude_code() -> dict[str, Any]:
    try:
        from .cli_adapters import ClaudeCodeAdapter
    except ImportError:
        from cli_adapters import ClaudeCodeAdapter  # type: ignore

    try:
        probe = ClaudeCodeAdapter().probe()
        return {
            "adapter_id": probe.adapter_id,
            "state": probe.state,
            "version": probe.version,
            "executable": probe.executable,
            "capabilities": list(probe.capabilities or ()),
            "reason": probe.reason,
            "checked_at": _now(),
        }
    except Exception as error:  # noqa: BLE001
        return {
            "adapter_id": "claude-code",
            "state": "ERROR",
            "version": None,
            "executable": "claude",
            "checked_at": _now(),
            "detail": f"{type(error).__name__}:{error}",
        }


def _resolve_for_probe(executable: str) -> str | None:
    """找可执行文件：PATH 优先，其次 opencode 官方安装器的落地位置（家目录）。

    为什么要兜底：官方安装器把 PATH 写进 `.bashrc`，systemd 单元里我们自己设了 PATH；
    但前台调试（`worker-run`）时未必有——找不到就返回 None，如实报 NOT_INSTALLED。
    """

    found = shutil.which(executable)
    if found:
        return found
    candidate = Path.home() / ".opencode" / "bin" / executable
    return str(candidate) if candidate.exists() else None


def _probe_opencode_models(executable: str) -> tuple[list[str], str]:
    """探测 opencode 的可用模型与默认模型（「我的智能体」模型切换的数据来源）。

    - `opencode models` 输出一行一个 `provider/model`；只收含 `/` 的行（其它是提示/日志噪声）；
    - 默认模型从它自己的配置里读（`~/.config/opencode/opencode.json` 的 `model` 字段）；
    - **cwd 固定在家目录**：opencode 会读"当前目录的项目配置"，在没权限的目录里跑会失败（实测）。
    - 任何失败都返回空——不编模型名（平台/界面宁可没有选项，也不要假选项）。
    """

    resolved = _resolve_for_probe(executable)
    if resolved is None:
        return [], ""
    try:
        completed = subprocess.run(
            [resolved, "models"],
            cwd=str(Path.home()),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=OPENCODE_MODELS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return [], ""
    if completed.returncode != 0:
        return [], ""
    models: list[str] = []
    for line in (completed.stdout or "").splitlines():
        name = line.strip()
        if name and "/" in name and " " not in name and name not in models:
            models.append(name)
    default_model = ""
    config_path = Path.home() / ".config" / "opencode" / "opencode.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if isinstance(config, dict):
            default_model = str(config.get("model") or "")
    except (OSError, ValueError):
        default_model = ""
    return models, default_model


def _frontmatter_description(text: str) -> str:
    """读角色文件 frontmatter 里的 `description`（`opencode agent list` **不输出**说明，只有名字与模式）。"""

    if not text.startswith("---"):
        return ""
    for line in text.splitlines()[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if stripped.startswith("description:"):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


# 决定"这个角色能不能动手"的三个工具（实测 `tools: {bash: false}` 真的禁用；**没写 = opencode 默认开启**）
EXECUTION_TOOLS = ("bash", "edit", "write")


def _frontmatter_tools(text: str) -> dict[str, bool]:
    """读角色文件 frontmatter 的 `tools:` 开关块（`bash: false` / `edit: true` 这类）。

    语义要点：**没列出的工具是开启的**（opencode 的默认）——所以"这个角色能不能改动工作目录"
    要看有没有被**显式关掉**，而不是看有没有写 `true`。
    """

    tools: dict[str, bool] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return tools
    inside = False
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.strip().startswith("tools:"):
            inside = True
            continue
        if not inside:
            continue
        if not line.startswith((" ", "\t")):  # 回到顶层的另一个键
            break
        key, _, value = line.strip().partition(":")
        name = key.strip()
        if name:
            tools[name] = value.strip().strip('"').strip("'").lower() not in {"false", "no", "0", "off"}
    return tools


def _role_executes(tools: dict[str, bool]) -> bool:
    """这个角色能不能执行命令或写文件（页面要如实标注"会改动工作目录"）。"""

    return any(tools.get(name, True) for name in EXECUTION_TOOLS)


def _parse_agent_names(stdout: str) -> list[str]:
    """从 `opencode agent list` 的输出里取角色名（纯函数，便于测试）。

    实测格式是 `名字 (模式)` 一行一个，后面跟权限 JSON——**只认整行匹配**，
    这样权限 JSON 里的内容不会被误当角色名。
    """

    names: list[str] = []
    for line in (stdout or "").splitlines():
        match = _AGENT_LIST_LINE.match(line.strip())
        if match and match.group(1) not in names:
            names.append(match.group(1))
    return names


def _opencode_agent_descriptions(config_dir: Path | None = None) -> dict[str, str]:
    """角色名 → 中文说明。

    来源三处（都不联网）：`<config>/agent/*.md` 与 `<config>/agents/*.md` 的 frontmatter、
    以及 `<config>/opencode.json` 的 `agent` 段（opencode 支持两种定义方式，两种都认）。
    **只认带 description 的**：没说明的（内部 agent / 我们没装的角色）不进用户下拉。
    """

    descriptions: dict[str, str] = {}
    base = Path(config_dir) if config_dir is not None else Path.home() / ".config" / "opencode"
    for dirname in ("agent", "agents"):
        try:
            files = sorted((base / dirname).glob("*.md"))
        except OSError:  # 目录不存在/不可读：如实当作"没有自定义角色"
            continue
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            description = _frontmatter_description(text)
            if description:
                descriptions.setdefault(path.stem, description)
    try:
        config = json.loads((base / "opencode.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    if isinstance(config, dict) and isinstance(config.get("agent"), dict):
        for name, spec in config["agent"].items():
            if isinstance(spec, dict):
                description = str(spec.get("description") or "").strip()
                if description:
                    descriptions.setdefault(str(name), description)
    return descriptions


def _opencode_agent_tools(config_dir: Path | None = None) -> dict[str, dict[str, bool]]:
    """角色名 → 工具开关（只从 `agent/` 与 `agents/` 下的 md 读；`opencode.json` 里定义的没这信息）。"""

    tools_by_role: dict[str, dict[str, bool]] = {}
    base = Path(config_dir) if config_dir is not None else Path.home() / ".config" / "opencode"
    for dirname in ("agent", "agents"):
        try:
            files = sorted((base / dirname).glob("*.md"))
        except OSError:
            continue
        for path in files:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            parsed = _frontmatter_tools(text)
            if parsed:
                tools_by_role.setdefault(path.stem, parsed)
    return tools_by_role


def _probe_opencode_roles(executable: str, *, config_dir: Path | None = None) -> list[dict[str, str]]:
    """探测对话可选角色：**名字以 `opencode agent list` 为准**（只有真被加载的才会出现），
    说明取自角色文件自己的 frontmatter（见 `_opencode_agent_descriptions`）。

    为什么不直接读文件当名单：文件写坏了 opencode 不会加载它——只有 `agent list` 才回答
    "到底哪些角色真能用"。反过来，`agent list` 也不含说明，所以两处合起来用。
    任何失败都返回空列表：平台侧看到空就只显示「默认」，**不显示点不动的假选项**。
    """

    resolved = _resolve_for_probe(executable)
    if resolved is None:
        return []
    try:
        completed = subprocess.run(
            [resolved, "agent", "list"],
            cwd=str(Path.home()),
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=OPENCODE_ROLES_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    names = _parse_agent_names(completed.stdout or "")
    if not names:
        return []
    described = _opencode_agent_descriptions(config_dir)
    tools_by_role = _opencode_agent_tools(config_dir)
    roles: list[dict[str, Any]] = []
    for name in names:
        description = described.get(name) or BUILTIN_SELECTABLE_AGENTS.get(name, "")
        if not description:
            continue  # 内部 agent（compaction/summary/title/explore/general）与默认 build 不进下拉
        # `executes`：这个角色能不能跑命令/写文件（页面据此标注"会改动工作目录"）。
        # 三种情况分开定，**不确定时按"能"报**（宁可多标一句提示，也不要让用户以为它是只读的）：
        #   ① 有角色文件的 → 按 frontmatter 的 tools 推（没写 = opencode 默认开启）；
        #   ② 已知的内置模式（plan）→ 只读（它的定义就是禁用编辑工具）；
        #   ③ 来源不明（只写在 opencode.json 里）→ 当作能。
        if name in tools_by_role:
            executes = _role_executes(tools_by_role[name])
        elif name in BUILTIN_SELECTABLE_AGENTS:
            executes = False
        else:
            executes = True
        roles.append({"name": name, "description": description, "executes": executes})
    return roles


def _probe_generic_cli(executable: str) -> dict[str, Any]:
    """通用 CLI 探测：装没装（--version 能跑即可用），不做协议级判断。

    这些 CLI 走 `worker_executor = "cli"`（命令模板 + {prompt}）：stdout 即结果、
    退出码即成败，与声明式命令同一套边界——所以探测只回答"在不在"，不猜语义。
    """

    try:
        from .cli_adapters import probe_cli
    except ImportError:
        from cli_adapters import probe_cli  # type: ignore

    try:
        status = probe_cli(
            adapter_id=f"{executable}-cli",
            executable=executable,
            capabilities=("cli.version",),
            accepted_version_prefixes=None,
            timeout_seconds=5.0,
        )
        reason = "可用作 worker_executor=cli 的执行体（命令模板带 {prompt}）" if status.state == "AVAILABLE" else status.reason
        entry = {
            "adapter_id": status.adapter_id,
            "state": status.state,
            "version": status.version,
            "executable": status.executable,
            "capabilities": list(status.capabilities or ()),
            "reason": reason,
            "checked_at": status.checked_at.isoformat(),
        }
        if executable == "opencode" and status.state == "AVAILABLE":
            # 对话页的模型下拉要真数据：装了 opencode 才多跑一次 `opencode models`（有 TTL 缓存，不会每跳心跳都探）
            models, default_model = _probe_opencode_models(executable)
            entry["models"] = models
            entry["default_model"] = default_model
            # 角色（agent）：同样只在装了 opencode 时探；探不到就是空列表（页面只显示「默认」）
            entry["roles"] = _probe_opencode_roles(executable)
        return entry
    except Exception as error:  # noqa: BLE001 - 探测异常如实记为 ERROR
        return {
            "adapter_id": f"{executable}-cli",
            "state": "ERROR",
            "version": None,
            "executable": executable,
            "checked_at": _now(),
            "detail": f"{type(error).__name__}:{error}",
        }


__all__ = ["AgentInventory", "DEFAULT_TTL_SECONDS", "default_probe", "GENERIC_CLI_EXECUTABLES"]