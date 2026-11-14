"""Claude Code 插件库：一个插件目录 = skills + MCP + subagents 的打包。

借鉴 magpie 的 library 做法：安装时把插件复制到自己的目录，读取
`.claude-plugin/plugin.json` / `skills/*/SKILL.md` / `.mcp.json` / `agents/*.md`；
启用时把这几部分按 Agent 挂载，卸载时只删自己复制的那份。

V1 不携带 hooks / slash commands / LSP；它们会出现在 `left` 里，明确告诉用户
“看到了但没接”，而不是静默丢弃。
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import store as studio_store
from .commands import StudioError

PLUGIN_MANIFEST = Path(".claude-plugin") / "plugin.json"
PLUGIN_MARKERS = ("skills", "agents", "commands", "hooks", ".mcp.json", "lsp.json")
SLUG = re.compile(r"[^A-Za-z0-9_-]+")
MAX_PLUGIN_FILES = 5000


@dataclass(frozen=True)
class PluginParts:
    name: str
    description: str
    version: str
    root: str
    skills: list[dict[str, str]]
    mcp: list[str]
    agents: list[str]
    left: list[dict[str, str]]


def plugin_root(state_path: str | Path | None) -> Path:
    base = (Path(state_path).expanduser().resolve().parent
            if state_path else Path.home() / ".synapforge")
    return base / "studio" / "plugins"


def slug(value: str, fallback: str = "plugin") -> str:
    text = SLUG.sub("-", str(value or "").strip()).strip("-_")[:40]
    return text or fallback


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _paths(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _inside(root: Path, relative: str) -> Path | None:
    path = (root / relative).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    return path


def is_plugin(folder: Path) -> bool:
    if not folder.is_dir():
        return False
    if (folder / PLUGIN_MANIFEST).exists():
        return True
    return any((folder / marker).exists() for marker in PLUGIN_MARKERS)


def _skill_dirs(folder: Path, manifest: dict[str, Any],
                left: list[dict[str, str]]) -> list[dict[str, str]]:
    roots = ["skills"] + _paths(manifest.get("skills"))
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for relative in roots:
        root = _inside(folder, relative)
        if root is None:
            left.append({"what": f"skills {relative}", "why": "路径越界"})
            continue
        if not root.is_dir():
            continue
        candidates = [root] if (root / "SKILL.md").exists() else [
            child for child in sorted(root.iterdir()) if child.is_dir()]
        for candidate in candidates:
            skill_md = candidate / "SKILL.md"
            if not skill_md.exists():
                continue
            name = candidate.name
            if name in seen:
                continue
            seen.add(name)
            out.append({"name": name, "path": str(candidate)})
    return out


def _agent_files(folder: Path, manifest: dict[str, Any],
                 left: list[dict[str, str]]) -> list[str]:
    roots = ["agents"] + _paths(manifest.get("agents"))
    out: list[str] = []
    for relative in roots:
        root = _inside(folder, relative)
        if root is None or not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            if path.is_file() and path.stem not in out:
                out.append(path.stem)
    return out


def _read_parts(folder: Path) -> PluginParts:
    manifest = _read_json(folder / PLUGIN_MANIFEST)
    name = slug(str(manifest.get("name") or folder.name))
    description = str(manifest.get("description") or "").strip()
    version = str(manifest.get("version") or "").strip()
    left: list[dict[str, str]] = []
    skills = _skill_dirs(folder, manifest, left)
    agents = _agent_files(folder, manifest, left)
    mcp: list[str] = []
    mcp_file = folder / ".mcp.json"
    if mcp_file.exists():
        payload = _read_json(mcp_file)
        servers = payload.get("mcpServers")
        if isinstance(servers, dict):
            mcp = [str(key) for key in servers]
    for what, why in (
        ("hooks", "V1 只挂载 skills / MCP / subagents，hooks 未携带"),
        ("commands", "slash commands 未携带"),
        ("lsp", "LSP servers 未携带"),
    ):
        marker = "hooks/hooks.json" if what == "hooks" else (
            "commands" if what == "commands" else "lsp.json")
        if (folder / marker).exists():
            left.append({"what": what, "why": why})
    return PluginParts(name=name, description=description, version=version,
                       root=str(folder), skills=skills, mcp=mcp, agents=agents,
                       left=left)


def read_plugin(folder: str | Path) -> PluginParts:
    path = Path(folder).expanduser().resolve()
    if not path.is_dir():
        raise StudioError("plugin_path_invalid", [f"不是目录：{path}"])
    if not is_plugin(path):
        raise StudioError(
            "plugin_invalid",
            [f"{path} 不是 Claude Code 插件：缺少 .claude-plugin/plugin.json、"
             "skills、agents、hooks 或 .mcp.json"])
    return _read_parts(path)


def _copy_plugin(source: Path, destination: Path) -> None:
    if destination.exists():
        if destination.resolve() == source.resolve():
            raise StudioError("plugin_source_is_library",
                              ["源目录就是插件库自身，不能重复安装"])
        shutil.rmtree(destination, ignore_errors=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination,
                    ignore=shutil.ignore_patterns(".git", "node_modules",
                                                  "__pycache__", ".DS_Store"))


def _public(plugin: dict[str, Any], store: Any | None = None) -> dict[str, Any]:
    users = studio_store.agents_using_plugin(store, plugin["id"]) if store else []
    return {
        "id": plugin["id"],
        "name": plugin["name"],
        "description": plugin["description"],
        "version": plugin["version"],
        "source": plugin["source"],
        "path": plugin["path"],
        "skills": plugin["skills"],
        "mcp": plugin["mcp"],
        "agents": plugin["agents"],
        "left": plugin["left"],
        "installed_at": plugin["installed_at"],
        "updated_at": plugin["updated_at"],
        "enabled_agents": users,
        "counts": {"skills": len(plugin["skills"]), "mcp": len(plugin["mcp"]),
                   "agents": len(plugin["agents"])},
    }


def list_plugins(store: Any) -> list[dict[str, Any]]:
    return [_public(item, store) for item in studio_store.list_plugins(store)]


def install_plugin(store: Any, source: str | Path, *, state_path: str | Path | None,
                   update: bool = True) -> dict[str, Any]:
    origin = Path(source).expanduser().resolve()
    parts = read_plugin(origin)
    destination = plugin_root(state_path) / parts.name
    existing = studio_store.get_plugin_by_name(store, parts.name)
    if existing and not update:
        raise StudioError("plugin_exists", [f"{parts.name}: 插件已安装"])
    _copy_plugin(origin, destination)
    copied = _read_parts(destination)
    if existing:
        updated = studio_store.update_plugin(
            store, existing["id"], description=copied.description,
            version=copied.version, source=str(origin), path=str(destination),
            skills=copied.skills, mcp=copied.mcp, agents=copied.agents,
            left=copied.left)
        return _public(updated, store)  # type: ignore[arg-type]
    created = studio_store.create_plugin(
        store, name=copied.name, description=copied.description,
        version=copied.version, source=str(origin), path=str(destination),
        skills=copied.skills, mcp=copied.mcp, agents=copied.agents,
        left=copied.left)
    return _public(created, store)


def update_plugin(store: Any, plugin_id: str, *, state_path: str | Path | None) -> dict[str, Any]:
    plugin = studio_store.get_plugin(store, plugin_id)
    if plugin is None:
        raise StudioError("plugin_not_found", [plugin_id])
    if not plugin["source"]:
        raise StudioError("plugin_source_missing", [plugin["name"]])
    return install_plugin(store, plugin["source"], state_path=state_path, update=True)


def delete_plugin(store: Any, plugin_id: str, *, state_path: str | Path | None) -> dict[str, Any]:
    plugin = studio_store.get_plugin(store, plugin_id)
    if plugin is None:
        raise StudioError("plugin_not_found", [plugin_id])
    users = studio_store.agents_using_plugin(store, plugin_id)
    folder = Path(plugin["path"])
    root = plugin_root(state_path).resolve()
    try:
        folder.relative_to(root)
    except ValueError:
        folder = None  # 只删插件库里的副本，不碰用户源目录
    if folder and folder.exists():
        shutil.rmtree(folder, ignore_errors=True)
    studio_store.delete_plugin(store, plugin_id)
    return {"deleted": True, "plugin_id": plugin_id, "name": plugin["name"],
            "detached_agents": users}


def _substitute(value: Any, plugin: dict[str, Any], state_path: str | Path | None) -> Any:
    data_dir = plugin_root(state_path) / "plugin-data" / plugin["name"]
    if isinstance(value, str):
        return (value.replace("${CLAUDE_PLUGIN_ROOT}", plugin["path"])
                .replace("$CLAUDE_PLUGIN_ROOT", plugin["path"])
                .replace("${CLAUDE_PLUGIN_DATA}", str(data_dir))
                .replace("$CLAUDE_PLUGIN_DATA", str(data_dir)))
    if isinstance(value, list):
        return [_substitute(item, plugin, state_path) for item in value]
    if isinstance(value, dict):
        return {key: _substitute(item, plugin, state_path) for key, item in value.items()}
    return value


def plugin_mcp_servers(plugin: dict[str, Any],
                       state_path: str | Path | None) -> dict[str, Any]:
    mcp_file = Path(plugin["path"]) / ".mcp.json"
    payload = _read_json(mcp_file)
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {str(name): _substitute(config, plugin, state_path)
            for name, config in servers.items()}


def agent_plugin_parts(store: Any, agent: dict[str, Any],
                       state_path: str | Path | None) -> dict[str, Any]:
    """Agent 启用的插件展开成运行时可挂载的 skills / MCP / subagents。"""

    plugins: list[dict[str, Any]] = []
    missing: list[str] = []
    for plugin_id in agent.get("plugins") or []:
        plugin = studio_store.get_plugin(store, str(plugin_id))
        if plugin is None:
            missing.append(str(plugin_id))
        else:
            plugins.append(plugin)
    skills: list[dict[str, str]] = []
    mcp: dict[str, Any] = {}
    subagents: list[dict[str, str]] = []
    for plugin in plugins:
        for skill in plugin["skills"]:
            skills.append({"id": f"pluginpart:{plugin['name']}:{skill['name']}",
                           "name": skill["name"], "path": skill["path"],
                           "plugin": plugin["name"]})
        mcp.update(plugin_mcp_servers(plugin, state_path))
        for name in plugin["agents"]:
            path = Path(plugin["path"]) / "agents" / f"{name}.md"
            if path.exists():
                subagents.append({"name": name, "path": str(path),
                                  "plugin": plugin["name"]})
    return {"plugins": plugins, "skills": skills, "mcp": mcp,
            "subagents": subagents, "missing": missing}


def sync_plugin_subagents(workspace: str | Path,
                          subagents: list[dict[str, str]]) -> dict[str, Any]:
    target = Path(workspace) / ".claude" / "agents"
    manifest_path = target / ".synapforge-managed.json"
    try:
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = {}
    managed = previous.get("agents") if isinstance(previous.get("agents"), dict) else {}
    names: dict[str, str] = {}
    for item in subagents:
        name = slug(f"{item.get('plugin', '')}--{item.get('name', '')}", "agent")
        names[name] = item["path"]
    for name in list(managed):
        if name not in names:
            path = target / f"{name}.md"
            if path.exists():
                path.unlink()
    mounted: list[str] = []
    if names:
        target.mkdir(parents=True, exist_ok=True)
    for name, source in names.items():
        destination = target / f"{name}.md"
        try:
            shutil.copy2(source, destination)
            mounted.append(name)
        except OSError:
            continue
    if target.exists():
        manifest_path.write_text(json.dumps(
            {"managed_by": "synapforge-studio", "agents": names},
            ensure_ascii=False, indent=2), encoding="utf-8")
    return {"mounted": mounted, "target": str(target)}
