"""本机能力清单：Skills / MCP / 记忆 / 插件。

只读探测，不修改任何配置。用途是让“模型与智能体”页面能回答：
这个 Agent 除了模型之外，还能用哪些技能、连了哪些 MCP、记得什么。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any

MAX_SKILLS = 200
MAX_PLUGINS = 200
MAX_MEMORY_BYTES = 512 * 1024
MEMORY_FILES = ("CLAUDE.md", "AGENTS.md", "MEMORY.md")
MANAGED_SKILLS = ".synapforge-managed.json"
SECRET_ARG = re.compile(r"(sk-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9]{8,}|Bearer\s+\S+)", re.I)
SAFE_SKILL_NAME = re.compile(r"[^A-Za-z0-9._\-\u4e00-\u9fff]+")


def _read_text(path: Path, limit: int = 200_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _skill_description(path: Path) -> str:
    text = _read_text(path, 20_000)
    lines = text.splitlines()
    # frontmatter: description: ...
    if lines and lines[0].strip() == "---":
        for line in lines[1:60]:
            if line.strip() == "---":
                break
            match = re.match(r"\s*description\s*:\s*(.+)", line)
            if match:
                return match.group(1).strip().strip("\"'")[:240]
    for line in lines[:80]:
        value = line.strip()
        if value and not value.startswith("#") and not value.startswith("---"):
            return value[:240]
    return ""


def _skill(name: str, path: Path, source: str, skill_id: str) -> dict[str, Any]:
    return {"id": skill_id, "name": name, "description": _skill_description(path),
            "path": str(path), "source": source}


def _skill_dir(base: Path, source: str, prefix: str,
               limit: int = MAX_SKILLS) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        entries = sorted(base.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return out
    for entry in entries:
        if len(out) >= limit:
            break
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if skill_md.exists():
            out.append(_skill(entry.name, skill_md, source,
                              f"{prefix}:{entry.name}"))
    return out


def find_skills(home: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for base, source, prefix in (
        (home / ".claude" / "skills", "Claude Code", "claude"),
        (home / ".agents" / "skills", "本机技能库", "agents"),
        (home / ".claude" / "plugins", "Claude 插件", "plugin"),
    ):
        if base.is_dir() and base.name == "plugins":
            # 插件里的 skills 通常多一层：<plugin>/skills/<name>/SKILL.md
            try:
                for plugin in sorted(base.iterdir(), key=lambda item: item.name.lower()):
                    skills = plugin / "skills"
                    if skills.is_dir():
                        out.extend(_skill_dir(skills, f"插件 · {plugin.name}",
                                              f"plugin:{plugin.name}"))
            except OSError:
                continue
        elif base.is_dir():
            out.extend(_skill_dir(base, source, prefix))
    return out


def find_skill(home: Path, skill_id: str) -> dict[str, Any] | None:
    return next((item for item in find_skills(home) if item["id"] == skill_id), None)


def _safe_url(url: str) -> str:
    raw = str(url or "")
    if "?" in raw:
        raw = raw.split("?", 1)[0]
    return SECRET_ARG.sub("***", raw)


def _safe_command(value: Any) -> str:
    if isinstance(value, list):
        parts = [SECRET_ARG.sub("***", str(part)) for part in value]
        return " ".join(parts)[:300]
    return SECRET_ARG.sub("***", str(value or ""))[:300]


def _mcp_id(source: str, name: str) -> str:
    return "mcp-" + hashlib.sha1(f"{source}|{name}".encode("utf-8")).hexdigest()[:10]


def _mcp_entry(name: str, config: Any, source: str) -> dict[str, Any]:
    if not isinstance(config, dict):
        return {"id": _mcp_id(source, name), "name": name, "transport": "unknown",
                "target": "", "source": source, "enabled": True, "env_keys": []}
    transport = str(config.get("type") or config.get("transport") or "").strip()
    url = str(config.get("url") or config.get("endpoint") or "").strip()
    command = config.get("command")
    args = config.get("args")
    target = _safe_url(url) if url else _safe_command(
        " ".join([str(command or "")] + ([str(a) for a in args] if isinstance(args, list) else [])))
    env = config.get("env") or {}
    env_keys = sorted(env.keys())[:30] if isinstance(env, dict) else []
    if not transport:
        transport = "http" if url else ("stdio" if command else "unknown")
    return {"id": _mcp_id(source, name), "name": name, "transport": transport,
            "target": target, "source": source,
            "enabled": config.get("enabled") is not False, "env_keys": env_keys}


def find_mcp_raw(home: Path, agents: list[dict[str, Any]] | None = None
                 ) -> list[dict[str, Any]]:
    """完整 MCP 配置（含 env / headers），只在服务端用于生成本次运行的 mcp-config。"""

    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(servers: Any, source: str) -> None:
        if not isinstance(servers, dict):
            return
        for name, config in servers.items():
            key = (str(name), source)
            if key in seen:
                continue
            seen.add(key)
            out.append({"id": _mcp_id(source, str(name)), "name": str(name),
                        "source": source, "config": config})

    try:
        data = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
        add(data.get("mcpServers"), "~/.claude.json")
        projects = data.get("projects")
        if isinstance(projects, dict):
            for path_key, value in list(projects.items())[:40]:
                if isinstance(value, dict):
                    add(value.get("mcpServers"), f"项目 · {Path(path_key).name}")
    except (OSError, ValueError):
        pass
    for agent in (agents or []):
        workspace = Path(str(agent.get("workspace") or ""))
        for name in (".mcp.json", ".claude/settings.json"):
            path = workspace / name
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            add(payload.get("mcpServers"), f"{agent.get('name')} · {name}")
    return out


def find_mcp(home: Path, agents: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    return [_mcp_entry(item["name"], item["config"], item["source"])
            for item in find_mcp_raw(home, agents)]


def resolve_mcp(home: Path, mcp_ids: list[str],
                agents: list[dict[str, Any]] | None = None) -> tuple[dict[str, Any], list[str]]:
    """按 id 取完整配置；返回 ({name: config}, missing_ids)。重名时后者覆盖前者。"""

    wanted = {str(item) for item in (mcp_ids or [])}
    servers: dict[str, Any] = {}
    found: set[str] = set()
    for item in find_mcp_raw(home, agents):
        if item["id"] in wanted:
            servers[item["name"]] = item["config"]
            found.add(item["id"])
    return servers, sorted(wanted - found)


def _memory_entry(path: Path, *, agent: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        stat = path.stat()
        updated = stat.st_mtime
    except OSError:
        updated = 0
    return {
        "name": path.name,
        "path": str(path),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "updated_at": updated,
        "agent_id": (agent or {}).get("id", ""),
        "agent_name": (agent or {}).get("name", "全局"),
    }


def find_memory(home: Path, agents: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for name in ("CLAUDE.md", "AGENTS.md"):
        path = home / ".claude" / name
        if path.is_file():
            out.append(_memory_entry(path))
    for agent in (agents or []):
        workspace = Path(str(agent.get("workspace") or ""))
        for name in ("CLAUDE.md", "AGENTS.md", "MEMORY.md"):
            path = workspace / name
            if path.is_file():
                out.append(_memory_entry(path, agent=agent))
    return out


def find_plugins(home: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    base = home / ".claude" / "plugins"
    if not base.is_dir():
        return out
    try:
        entries = sorted(base.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return out
    for entry in entries:
        if len(out) >= MAX_PLUGINS:
            break
        if entry.name.startswith(".") or entry.name == "config.json":
            continue
        if entry.is_dir() and (entry / "skills").is_dir():
            out.append({"name": entry.name, "path": str(entry), "source": "Claude 插件"})
    return out


def _memory_path(workspace: str | Path, name: str) -> Path:
    if name not in MEMORY_FILES:
        raise ValueError(f"只支持 {', '.join(MEMORY_FILES)}")
    root = Path(workspace).expanduser().resolve()
    path = (root / name).resolve()
    if root != path.parent and root not in path.parents:
        raise ValueError("路径越界")
    return path


def workspace_memory(workspace: str | Path) -> list[dict[str, Any]]:
    root = Path(workspace).expanduser()
    out: list[dict[str, Any]] = []
    for name in MEMORY_FILES:
        path = Path(workspace) / name
        try:
            stat = path.stat()
            exists = True
        except OSError:
            exists = False
        out.append({
            "name": name,
            "path": str((root / name).resolve()),
            "exists": exists,
            "size_bytes": stat.st_size if exists else 0,
            "updated_at": stat.st_mtime if exists else 0,
            "content": _read_text(path, MAX_MEMORY_BYTES) if exists else "",
        })
    return out


def read_memory(workspace: str | Path, name: str) -> dict[str, Any]:
    path = _memory_path(workspace, name)
    text = _read_text(path, MAX_MEMORY_BYTES)
    return {"name": name, "path": str(path), "exists": path.exists(),
            "content": text, "size_bytes": len(text.encode("utf-8"))}


def write_memory(workspace: str | Path, name: str, content: str) -> dict[str, Any]:
    path = _memory_path(workspace, name)
    text = str(content or "")
    raw = text.encode("utf-8")
    if len(raw) > MAX_MEMORY_BYTES:
        raise ValueError(f"记忆文件不能超过 {MAX_MEMORY_BYTES // 1024} KB")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"name": name, "path": str(path), "exists": True,
            "size_bytes": len(raw), "content": text}


def memory_context(workspace: str | Path, limit: int = 8000) -> str:
    """给自定义命令执行体注入的长期记忆；Claude Code 自己会读 CLAUDE.md。"""

    parts: list[str] = []
    for name in ("CLAUDE.md", "AGENTS.md", "MEMORY.md"):
        path = Path(workspace) / name
        text = _read_text(path, limit).strip()
        if text:
            parts.append(f"[{name}]\n{text}")
        if sum(len(part) for part in parts) >= limit:
            break
    return "\n\n".join(parts)[:limit]


def sync_workspace_skills(workspace: str | Path, home: Path,
                          skill_ids: list[str],
                          extra_skills: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """把选中的技能复制到工作区 .claude/skills；只删自己 manifest 里登记过的目录。"""

    target = Path(workspace) / ".claude" / "skills"
    manifest_path = target / MANAGED_SKILLS
    previous: dict[str, Any] = {}
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    managed = previous.get("skills") if isinstance(previous.get("skills"), dict) else {}
    available = {item["id"]: item for item in find_skills(home)}
    selected = []
    missing = []
    for skill_id in dict.fromkeys(str(item) for item in (skill_ids or [])):
        skill = available.get(skill_id)
        if skill is None:
            missing.append(skill_id)
        else:
            selected.append(skill)
    for extra in extra_skills or []:
        path = str(extra.get("path") or "")
        name = str(extra.get("name") or "")
        if not path or not name:
            continue
        selected.append({"id": str(extra.get("id") or f"extra:{name}"),
                         "name": name, "path": path,
                         "source": str(extra.get("plugin") or "插件")})

    mounted: list[str] = []
    skipped: list[dict[str, str]] = []
    names: dict[str, str] = {}
    if selected:
        target.mkdir(parents=True, exist_ok=True)
    selected_names = set()
    for skill in selected:
        name = SAFE_SKILL_NAME.sub("-", skill["name"]).strip(".-") or "skill"
        if name in names:
            skipped.append({"name": name, "reason": "duplicate"})
            continue
        names[name] = skill["id"]
        selected_names.add(name)
    # 清掉上次挂载、这次没选中的目录
    for name in list(managed):
        if name in selected_names:
            continue
        path = target / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
    for skill in selected:
        name = SAFE_SKILL_NAME.sub("-", skill["name"]).strip(".-") or "skill"
        if names.get(name) != skill["id"]:
            continue
        dest = target / name
        source = Path(skill["path"])
        if not source.is_dir():
            source = source.parent
        if not (source / "SKILL.md").exists():
            skipped.append({"name": name, "reason": "no_skill_md"})
            continue
        if dest.exists():
            if name not in managed:
                skipped.append({"name": name, "reason": "exists_not_managed"})
                continue
            shutil.rmtree(dest, ignore_errors=True)
        try:
            shutil.copytree(source, dest)
            mounted.append(name)
        except OSError as error:
            skipped.append({"name": name, "reason": str(error)[:120]})
    if target.exists():
        manifest_path.write_text(json.dumps(
            {"managed_by": "synapforge-studio", "skills": names},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return {"mounted": mounted, "skipped": skipped, "missing": missing,
            "target": str(target)}


def mcp_payload(home: Path, mcp_ids: list[str],
                agents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    servers, missing = resolve_mcp(home, mcp_ids, agents)
    return {"mcpServers": servers, "missing": missing}


def import_skill(folder: str | Path, home: Path | None = None) -> dict[str, Any]:
    """把本地技能目录复制进 `~/.agents/skills/<name>`（不联网）。"""

    source = Path(folder).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"不是目录：{source}")
    if not (source / "SKILL.md").exists():
        raise ValueError(f"{source} 里没有 SKILL.md，不是技能目录")
    base = Path(home).expanduser() if home else Path.home()
    base = base / ".agents" / "skills"
    name = SAFE_SKILL_NAME.sub("-", source.name).strip(".-") or "skill"
    destination = base / name
    index = 2
    while destination.exists():
        candidate = base / f"{name}-{index}"
        if candidate.exists():
            index += 1
            continue
        destination = candidate
        break
    base.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination,
                    ignore=shutil.ignore_patterns(".git", "__pycache__", ".DS_Store"))
    return {"name": destination.name, "path": str(destination / "SKILL.md"),
            "id": f"agents:{destination.name}", "source": "本地导入"}


def import_plugin_folder(folder: str | Path, home: Path | None = None) -> dict[str, Any]:
    """把一个插件目录里的 skills 全部导入本机技能库（不安装插件本体）。"""

    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"不是目录：{root}")
    skills_dir = root / "skills"
    candidates = [root] if (root / "SKILL.md").exists() else []
    if skills_dir.is_dir():
        candidates += [child for child in sorted(skills_dir.iterdir()) if child.is_dir()]
    imported: list[dict[str, Any]] = []
    skipped: list[str] = []
    for candidate in candidates:
        if not (candidate / "SKILL.md").exists():
            continue
        try:
            imported.append(import_skill(candidate, home=home))
        except (OSError, ValueError):
            skipped.append(candidate.name)
    if not imported:
        raise ValueError(f"{root} 里没有找到技能（SKILL.md）")
    return {"imported": imported, "skipped": skipped}


def inventory(home: Path | None = None,
              agents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    base = home or Path.home()
    skills = find_skills(base)
    mcp = find_mcp(base, agents)
    memory = find_memory(base, agents)
    plugins = find_plugins(base)
    return {
        "skills": skills,
        "mcp": mcp,
        "memory": memory,
        "plugins": plugins,
        "counts": {"skills": len(skills), "mcp": len(mcp),
                   "memory": len(memory), "plugins": len(plugins)},
        "home": str(base),
    }
