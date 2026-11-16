"""单 Agent 工作台：Agent 注册、角色卡、任务下发、会话状态。

与集群模式共用同一个 SQLite 和成果物/事件表；区别是不需要 lead/worker，
一个 Agent 直接接一个任务，跑完给交付。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any
from uuid import UUID

from .. import commands as base
from ..paths import state_dir
from . import capabilities as studio_capabilities
from . import plugins as studio_plugins
from . import providers as studio_providers
from . import store as studio_store
from .commands import StudioError

BACKENDS = ("claude", "command")


def _slug(text: str, fallback: str) -> str:
    raw = re.sub(r"[^a-zA-Z0-9_-]+", "-", (text or "").strip()).strip("-").lower()
    return (raw or fallback)[:40]


def model_options(store: Any | None = None) -> dict[str, Any]:
    return studio_providers.catalog(store)


def _workspace(raw: str, name: str) -> Path:
    if raw.strip():
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = state_dir() / path
    else:
        path = state_dir() / "agents" / f"{_slug(name, 'agent')}-{os.urandom(2).hex()}"
    path = path.resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _project(store: Any, name: str) -> dict[str, Any]:
    return base.ensure_project(store, f"Agent · {name}", task_mode="manual")


def _provider_or_error(store: Any, provider_id: str) -> dict[str, Any] | None:
    value = str(provider_id or "").strip()
    if not value:
        return None
    provider = studio_store.get_provider(store, value)
    if provider is None:
        raise StudioError("provider_not_found", [value])
    if not provider["enabled"]:
        raise StudioError("provider_disabled", [f"{provider['name']}: 渠道已停用"])
    return provider


def _validate_backend(backend: str, provider: dict[str, Any] | None,
                      command: str) -> str:
    value = str(backend or "claude").strip().lower()
    if value not in BACKENDS:
        raise StudioError("backend_invalid", [f"未知执行体：{backend}（可选 claude / command）"])
    if value == "command" and not str(command or "").strip():
        raise StudioError("command_required", ["自定义命令执行体需要命令模板"])
    if value == "claude" and provider is not None and provider["kind"] != "anthropic":
        raise StudioError(
            "provider_incompatible",
            [f"{provider['name']} 是 OpenAI 协议，Claude Code 不能直接用；"
             "请把执行体换成自定义命令，或改用 Anthropic 协议网关"])
    return value


def create_agent(store: Any, *, name: str, model: str = "", system_prompt: str = "",
                 workspace: str = "", allow_bash: bool = False,
                 allow_web: bool = True, backend: str = "claude",
                 provider_id: str = "", command: str = "",
                 skills: list[str] | None = None, mcp: list[str] | None = None,
                 mcp_managed: bool = False,
                 plugins: list[str] | None = None) -> dict[str, Any]:
    label = str(name or "").strip()
    if len(label) < 2:
        raise StudioError("agent_name_required", ["Agent 名至少 2 个字符"])
    if len(label) > 60:
        raise StudioError("agent_name_too_long", ["Agent 名不能超过 60 个字符"])
    if any(a["name"] == label for a in studio_store.list_agents(store)):
        raise StudioError("agent_name_exists", [f"{label}: 已有同名 Agent"])
    provider = _provider_or_error(store, provider_id)
    protocol = _validate_backend(backend, provider, command)
    path = _workspace(str(workspace or ""), label)
    project = _project(store, label)
    agent = studio_store.create_agent(
        store, name=label, backend=protocol, model=str(model or "").strip(),
        provider_id=provider["id"] if provider else "",
        command=str(command or "").strip(),
        system_prompt=str(system_prompt or "").strip(), workspace=str(path),
        allow_bash=bool(allow_bash), allow_web=bool(allow_web),
        skills=[str(item) for item in (skills or [])],
        mcp=[str(item) for item in (mcp or [])], mcp_managed=bool(mcp_managed),
        plugins=[str(item) for item in (plugins or [])],
        project_id=str(project["id"]))
    base.ensure_participant(store, project_id=project["id"], agent_id=agent["id"],
                            display_name=label)
    return {**agent, "project_name": project["name"],
            "provider_name": provider["name"] if provider else "本机默认"}


def get_agent_or_error(store: Any, agent_id: str) -> dict[str, Any]:
    agent = studio_store.get_agent(store, agent_id)
    if agent is None:
        raise StudioError("agent_not_found", [str(agent_id)])
    return agent


def update_agent(store: Any, agent_id: str, **fields: Any) -> dict[str, Any]:
    agent = get_agent_or_error(store, agent_id)
    if "name" in fields:
        name = str(fields["name"] or "").strip()
        if len(name) < 2:
            raise StudioError("agent_name_required", ["Agent 名至少 2 个字符"])
        fields["name"] = name
    if "workspace" in fields:
        fields["workspace"] = str(_workspace(str(fields["workspace"] or ""),
                                             str(fields.get("name") or agent["name"])))
    for key in ("skills", "mcp", "plugins"):
        if key in fields:
            value = fields[key]
            if not isinstance(value, list):
                raise StudioError("agent_extension_invalid", [f"{key} 必须是列表"])
            fields[key] = list(dict.fromkeys(str(item) for item in value if str(item)))[:100]
    if "mcp_managed" in fields:
        fields["mcp_managed"] = bool(fields["mcp_managed"])
    provider_id = fields.get("provider_id", agent["provider_id"])
    provider = _provider_or_error(store, provider_id)
    protocol = _validate_backend(str(fields.get("backend") or agent["backend"]),
                                 provider,
                                 str(fields.get("command", agent["command"]) or ""))
    fields["backend"] = protocol
    fields["provider_id"] = provider["id"] if provider else ""
    return studio_store.update_agent(store, agent_id, **fields)  # type: ignore[return-value]


def delete_agent(store: Any, agent_id: str) -> dict[str, Any]:
    agent = get_agent_or_error(store, agent_id)
    run = studio_store.latest_agent_run(store, agent_id)
    if run and run["status"] == "RUNNING":
        raise StudioError("agent_busy", ["Agent 正在跑任务，先停止再删除"])
    studio_store.delete_agent(store, agent_id)
    return {"deleted": True, "agent_id": agent_id, "name": agent["name"]}


def start_task(store: Any, agent_id: str, task_text: str, *,
               plan_id: str = "") -> dict[str, Any]:
    agent = get_agent_or_error(store, agent_id)
    text = str(task_text or "").strip()
    if not text:
        raise StudioError("task_required", ["任务内容不能为空"])
    provider = _provider_or_error(store, agent["provider_id"])
    if agent["backend"] == "command" and not agent["command"]:
        raise StudioError("command_required", ["自定义命令执行体需要命令模板"])
    if agent["backend"] == "claude" and provider and provider["kind"] != "anthropic":
        raise StudioError("provider_incompatible",
                          [f"{provider['name']} 不是 Anthropic 协议"])
    task = base.create_task(
        store, project_id=agent["project_id"],
        title=(text[:120] if len(text) >= 2 else f"任务：{text}"),
        description=text, stage="agent_task",
        assignee_agent_id=agent_id, requires_review=False)
    catalog = model_options(store)
    default_model = ((provider["models"][0] if provider and provider["models"] else "")
                     or catalog["default"])
    run = studio_store.create_agent_run(
        store, agent_id=agent_id, project_id=agent["project_id"],
        task_id=str(task["id"]), task_text=text,
        model=agent["model"] or default_model,
        backend=agent["backend"], provider_id=agent["provider_id"],
        workspace=agent["workspace"], status="QUEUED", plan_id=str(plan_id or ""))
    return {"run": run, "task": task, "agent": agent,
            "provider_name": provider["name"] if provider else "本机默认",
            "queue_position": studio_store.queue_position(store, agent_id)}


def retry_run(store: Any, run_id: str) -> dict[str, Any]:
    run = studio_store.get_agent_run(store, run_id)
    if run is None:
        raise StudioError("run_not_found", [run_id])
    if run["status"] in ("QUEUED", "RUNNING"):
        raise StudioError("run_busy", ["这个任务还在排队或运行中"])
    return start_task(store, run["agent_id"], run["task_text"])


def new_session(store: Any, agent_id: str) -> dict[str, Any]:
    agent = get_agent_or_error(store, agent_id)
    updated = studio_store.update_agent(store, agent_id, session_id="")
    return updated  # type: ignore[return-value]


def list_agents(store: Any) -> list[dict[str, Any]]:
    providers = {p["id"]: p for p in studio_store.list_providers(store)}
    out: list[dict[str, Any]] = []
    for agent in studio_store.list_agents(store):
        run = studio_store.latest_agent_run(store, agent["id"])
        artifacts = base.list_artifacts(store, project_id=agent["project_id"])
        provider = providers.get(agent["provider_id"])
        out.append({**agent, "run": run, "artifact_count": len(artifacts),
                    "queue_count": studio_store.queue_position(store, agent["id"]),
                    "skills_count": len(agent.get("skills") or []),
                    "mcp_count": len(agent.get("mcp") or []),
                    "plugins_count": len(agent.get("plugins") or []),
                    "provider_name": provider["name"] if provider else "本机默认",
                    "provider_kind": provider["kind"] if provider else "anthropic",
                    "provider_missing": bool(agent["provider_id"] and not provider)})
    return out


def agent_extensions(store: Any, agent_id: str, *,
                     home: Path | None = None) -> dict[str, Any]:
    agent = get_agent_or_error(store, agent_id)
    base_home = home or Path.home()
    skills = studio_capabilities.find_skills(base_home)
    mcp = studio_capabilities.find_mcp(base_home, [agent])
    skill_ids = {item["id"] for item in skills}
    mcp_ids = {item["id"] for item in mcp}
    selected_skills = [item for item in agent["skills"] if item in skill_ids]
    selected_mcp = [item for item in agent["mcp"] if item in mcp_ids]
    installed_plugins = studio_plugins.list_plugins(store)
    plugin_map = {item["id"]: item for item in installed_plugins}
    selected_plugins = [item for item in agent.get("plugins") or [] if item in plugin_map]
    parts = studio_plugins.agent_plugin_parts(
        store, agent, getattr(store, "path", None))
    return {
        "agent": agent,
        "skills": skills,
        "mcp": mcp,
        "memory": studio_capabilities.workspace_memory(agent["workspace"]),
        "selected_skills": selected_skills,
        "selected_mcp": selected_mcp,
        "missing_skills": [item for item in agent["skills"] if item not in skill_ids],
        "missing_mcp": [item for item in agent["mcp"] if item not in mcp_ids],
        "mcp_managed": agent["mcp_managed"],
        "plugins": installed_plugins,
        "selected_plugins": selected_plugins,
        "plugin_parts": {
            "skills": parts["skills"], "mcp": sorted(parts["mcp"]),
            "subagents": parts["subagents"], "missing": parts["missing"],
        },
    }


def update_agent_extensions(store: Any, agent_id: str, *,
                            skills: Any = None, mcp: Any = None,
                            mcp_managed: Any = None, plugins: Any = None,
                            home: Path | None = None) -> dict[str, Any]:
    agent = get_agent_or_error(store, agent_id)
    base_home = home or Path.home()
    fields: dict[str, Any] = {}
    ignored: list[str] = []
    if skills is not None:
        if not isinstance(skills, list):
            raise StudioError("agent_extension_invalid", ["skills 必须是列表"])
        available = {item["id"] for item in studio_capabilities.find_skills(base_home)}
        wanted = list(dict.fromkeys(str(item) for item in skills if str(item)))
        ignored += [item for item in wanted if item not in available]
        fields["skills"] = [item for item in wanted if item in available]
    if mcp is not None:
        if not isinstance(mcp, list):
            raise StudioError("agent_extension_invalid", ["mcp 必须是列表"])
        available = {item["id"] for item in
                     studio_capabilities.find_mcp(base_home, [agent])}
        wanted = list(dict.fromkeys(str(item) for item in mcp if str(item)))
        ignored += [item for item in wanted if item not in available]
        fields["mcp"] = [item for item in wanted if item in available]
    if mcp_managed is not None:
        fields["mcp_managed"] = bool(mcp_managed)
    if plugins is not None:
        if not isinstance(plugins, list):
            raise StudioError("agent_extension_invalid", ["plugins 必须是列表"])
        available = {item["id"] for item in studio_store.list_plugins(store)}
        wanted = list(dict.fromkeys(str(item) for item in plugins if str(item)))
        ignored += [item for item in wanted if item not in available]
        fields["plugins"] = [item for item in wanted if item in available]
    updated = studio_store.update_agent(store, agent_id, **fields)
    return {"agent": updated, "ignored": ignored}


def agent_memory(store: Any, agent_id: str, *,
                 home: Path | None = None) -> dict[str, Any]:
    agent = get_agent_or_error(store, agent_id)
    return {"agent": agent,
            "files": studio_capabilities.workspace_memory(agent["workspace"])}


def save_agent_memory(store: Any, agent_id: str, *, name: str,
                      content: str) -> dict[str, Any]:
    agent = get_agent_or_error(store, agent_id)
    try:
        saved = studio_capabilities.write_memory(agent["workspace"], name, content)
    except ValueError as error:
        raise StudioError("memory_invalid", [str(error)]) from error
    studio_store.update_agent(store, agent_id)  # 刷新 updated_at
    return {"agent": agent, "file": saved}


def agent_state(store: Any, agent_id: str, *, after_seq: int = 0) -> dict[str, Any]:
    agent = get_agent_or_error(store, agent_id)
    run = studio_store.latest_agent_run(store, agent_id)
    steps = studio_store.list_agent_steps(store, run["id"], after_seq=after_seq) if run else []
    # 导入的 Agent 可能没有合法 project_id；不要让它连详情页都打不开。
    project_id = str(agent.get("project_id") or "").strip()
    try:
        UUID(project_id)
    except (ValueError, TypeError, AttributeError):
        tasks, artifacts = [], []
    else:
        tasks = store.list_tasks(project_id)
        artifacts = base.list_artifacts(store, project_id=project_id)
    runs = studio_store.list_agent_runs(store, agent_id, limit=30)
    provider = studio_store.get_provider(store, agent["provider_id"]) if agent["provider_id"] else None
    return {
        **agent,
        "provider": studio_providers.public_provider(provider) if provider else None,
        "provider_name": provider["name"] if provider else "本机默认",
        "provider_missing": bool(agent["provider_id"] and provider is None),
        "run": run,
        "runs": runs,
        "queue": [r for r in runs if r["status"] == "QUEUED"],
        "steps": steps,
        "tasks": [t.model_dump(mode="json") for t in tasks],
        "artifacts": artifacts,
        "models": model_options(store),
    }


def list_runs(store: Any, agent_id: str) -> list[dict[str, Any]]:
    return studio_store.list_agent_runs(store, agent_id)


def all_runs(store: Any, limit: int = 200) -> list[dict[str, Any]]:
    providers = {p["id"]: p for p in studio_store.list_providers(store)}
    out = studio_store.list_all_agent_runs(store, limit=limit)
    for run in out:
        provider = providers.get(str(run.get("provider_id") or ""))
        run["provider_name"] = provider["name"] if provider else "本机默认"
    return out


def all_artifacts(store: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for agent in studio_store.list_agents(store):
        project_id = str(agent.get("project_id") or "").strip()
        try:
            UUID(project_id)
        except (ValueError, TypeError, AttributeError):
            # 导入的 Agent 可能没有合法 project_id；不能让一个坏行把整个交付页打成 500。
            continue
        for item in base.list_artifacts(store, project_id=project_id):
            artifact_id = str(item.get("id") or "")
            if artifact_id and artifact_id in seen:
                continue  # 多个 Agent 共用一个项目时，成果物只出一次
            if artifact_id:
                seen.add(artifact_id)
            out.append({**item, "agent_id": agent["id"], "agent_name": agent["name"]})
    out.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return out


def _run_tokens(run: dict[str, Any]) -> dict[str, int]:
    usage = run.get("usage") or {}
    return {
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
    }


def usage_summary(store: Any) -> dict[str, Any]:
    """用量总览：总计 / 按 Agent / 按模型 / 按渠道 / 按天 / 缓存命中。"""

    runs = all_runs(store, limit=2000)
    providers = {item["id"]: item["name"] for item in studio_store.list_providers(store)}
    by_agent: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_channel: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}
    totals = {"runs": 0, "tokens": 0, "input": 0, "output": 0,
              "cache_read": 0, "cache_write": 0, "cost": 0.0,
              "duration_ms": 0, "failed": 0, "done": 0}

    def bucket(table: dict[str, dict[str, Any]], key: str,
               label: str, run: dict[str, Any], parts: dict[str, int],
               cost: float) -> None:
        item = table.setdefault(key, {"id": key, "name": label, "runs": 0,
                                      "tokens": 0, "input": 0, "output": 0,
                                      "cache_read": 0, "cache_write": 0,
                                      "cost": 0.0, "duration_ms": 0})
        item["runs"] += 1
        item["tokens"] += sum(parts.values())
        item["input"] += parts["input"]
        item["output"] += parts["output"]
        item["cache_read"] += parts["cache_read"]
        item["cache_write"] += parts["cache_write"]
        item["cost"] += cost
        item["duration_ms"] += int((run.get("usage") or {}).get("duration_ms") or 0)

    for run in runs:
        parts = _run_tokens(run)
        tokens = sum(parts.values())
        cost = float((run.get("usage") or {}).get("total_cost_usd") or 0)
        duration = int((run.get("usage") or {}).get("duration_ms") or 0)
        totals["runs"] += 1
        totals["tokens"] += tokens
        totals["cost"] += cost
        totals["duration_ms"] += duration
        for key in ("input", "output", "cache_read", "cache_write"):
            totals[key] += parts[key]
        status = str(run.get("status") or "")
        if status == "DONE":
            totals["done"] += 1
        elif status == "FAILED":
            totals["failed"] += 1

        agent_id = str(run.get("agent_id") or "")
        bucket(by_agent, agent_id, run.get("agent_name") or agent_id, run, parts, cost)
        model = str(run.get("model") or "未记录")
        bucket(by_model, model, model, run, parts, cost)
        provider_id = str(run.get("provider_id") or "")
        channel = run.get("provider_name") or providers.get(provider_id, "本机默认")
        bucket(by_channel, provider_id or "local", channel, run, parts, cost)
        stamp = str(run.get("started_at") or "")[:10]
        if stamp:
            bucket(by_day, stamp, stamp, run, parts, cost)

    read = totals["cache_read"]
    billed_input = totals["input"] + read + totals["cache_write"]
    cache_rate = round(read / billed_input * 100, 1) if billed_input else 0.0
    totals["cost"] = round(totals["cost"], 6)
    for table in (by_agent, by_model, by_channel, by_day):
        for item in table.values():
            item["cost"] = round(item["cost"], 6)
            billed = item["input"] + item["cache_read"] + item["cache_write"]
            item["cache_rate"] = round(item["cache_read"] / billed * 100, 1) if billed else 0.0
    return {
        **totals,
        "cache_rate": cache_rate,
        "cost_per_run": round(totals["cost"] / totals["runs"], 6) if totals["runs"] else 0.0,
        "tokens_per_run": int(totals["tokens"] / totals["runs"]) if totals["runs"] else 0,
        "by_agent": sorted(by_agent.values(), key=lambda item: item["cost"], reverse=True),
        "by_model": sorted(by_model.values(), key=lambda item: item["tokens"], reverse=True),
        "by_channel": sorted(by_channel.values(), key=lambda item: item["cost"], reverse=True),
        "by_day": sorted(by_day.values(), key=lambda item: item["name"]),
    }


def overview(store: Any) -> dict[str, Any]:
    return {
        "agents": list_agents(store),
        "runs": all_runs(store, limit=200),
        "artifacts": all_artifacts(store),
        "usage": usage_summary(store),
        "models": model_options(store),
        "providers": studio_providers.list_providers(store),
    }
