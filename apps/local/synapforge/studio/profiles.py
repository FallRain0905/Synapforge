"""方案（Profile）：把一组 Agent 的执行体 / 渠道 / 模型 / 角色卡存成一个名字。

借鉴 magpie 的 profile：不是备份整个库，只存"每个 Agent 用哪个渠道、哪个模型、
什么角色卡、挂哪些扩展"，一键把所有 Agent 切到同一套配置。

应用时只改存在的 Agent（按名字匹配）；方案里多出来的 Agent 不会被创建，
快照里不存在的 Agent 保持原样——避免"切方案把别人的 Agent 删了"。
"""

from __future__ import annotations

from typing import Any

from . import store as studio_store
from .commands import StudioError

FIELDS = ("backend", "provider_id", "model", "command", "system_prompt",
          "allow_bash", "allow_web", "skills", "mcp", "mcp_managed", "plugins")


def snapshot(store: Any, note: str = "") -> dict[str, Any]:
    agents: list[dict[str, Any]] = []
    providers = {item["id"]: item["name"] for item in studio_store.list_providers(store)}
    for agent in studio_store.list_agents(store):
        agents.append({
            "id": agent["id"],
            "name": agent["name"],
            "provider_name": providers.get(agent["provider_id"], "本机默认"),
            **{key: agent.get(key) for key in FIELDS},
        })
    return {"agents": agents, "note": str(note or "")}


def _public(profile: dict[str, Any], store: Any) -> dict[str, Any]:
    agents = (profile.get("snapshot") or {}).get("agents") or []
    names = {item["name"] for item in studio_store.list_agents(store)}
    return {
        "id": profile["id"],
        "name": profile["name"],
        "note": profile["note"],
        "agents": [
            {"name": item.get("name"), "model": item.get("model"),
             "provider_name": item.get("provider_name"),
             "backend": item.get("backend"),
             "present": item.get("name") in names}
            for item in agents
        ],
        "count": len(agents),
        "created_at": profile["created_at"],
        "updated_at": profile["updated_at"],
    }


def list_profiles(store: Any) -> list[dict[str, Any]]:
    return [_public(item, store) for item in studio_store.list_profiles(store)]


def _provider_id(store: Any, provider_name: str) -> str:
    name = str(provider_name or "").strip()
    if not name or name == "本机默认":
        return ""
    provider = studio_store.get_provider_by_name(store, name)
    return provider["id"] if provider else ""


def save_profile(store: Any, *, name: str, note: str = "",
                 profile_id: str = "") -> dict[str, Any]:
    label = str(name or "").strip()
    if len(label) < 2:
        raise StudioError("profile_name_required", ["方案名至少 2 个字符"])
    if len(label) > 60:
        raise StudioError("profile_name_too_long", ["方案名不能超过 60 个字符"])
    other = studio_store.get_profile_by_name(store, label)
    if other and other["id"] != profile_id:
        raise StudioError("profile_name_exists", [f"{label}: 已有同名方案"])
    data = snapshot(store, note)
    if profile_id:
        current = studio_store.get_profile(store, profile_id)
        if current is None:
            raise StudioError("profile_not_found", [profile_id])
        updated = studio_store.update_profile(
            store, profile_id, name=label, note=str(note or ""), snapshot=data)
        return _public(updated, store)  # type: ignore[arg-type]
    created = studio_store.create_profile(store, name=label, note=str(note or ""),
                                          snapshot=data)
    return _public(created, store)


def update_profile_meta(store: Any, profile_id: str, *,
                        name: str | None = None, note: str | None = None) -> dict[str, Any]:
    current = studio_store.get_profile(store, profile_id)
    if current is None:
        raise StudioError("profile_not_found", [profile_id])
    fields: dict[str, Any] = {}
    if name is not None:
        label = str(name).strip()
        if len(label) < 2:
            raise StudioError("profile_name_required", ["方案名至少 2 个字符"])
        other = studio_store.get_profile_by_name(store, label)
        if other and other["id"] != profile_id:
            raise StudioError("profile_name_exists", [f"{label}: 已有同名方案"])
        fields["name"] = label
    if note is not None:
        fields["note"] = str(note)
    updated = studio_store.update_profile(store, profile_id, **fields)
    return _public(updated, store)  # type: ignore[arg-type]


def delete_profile(store: Any, profile_id: str) -> dict[str, Any]:
    current = studio_store.get_profile(store, profile_id)
    if current is None:
        raise StudioError("profile_not_found", [profile_id])
    studio_store.delete_profile(store, profile_id)
    return {"deleted": True, "profile_id": profile_id, "name": current["name"]}


def apply_profile(store: Any, profile_id: str) -> dict[str, Any]:
    profile = studio_store.get_profile(store, profile_id)
    if profile is None:
        raise StudioError("profile_not_found", [profile_id])
    agents = (profile.get("snapshot") or {}).get("agents") or []
    by_name = {item["name"]: item for item in studio_store.list_agents(store)}
    applied: list[str] = []
    skipped: list[str] = []
    for item in agents:
        agent = by_name.get(str(item.get("name")))
        if agent is None:
            skipped.append(str(item.get("name")))
            continue
        fields: dict[str, Any] = {}
        for key in FIELDS:
            if key not in item:
                continue
            fields[key] = item[key]
        provider_id = _provider_id(store, str(item.get("provider_name") or ""))
        if provider_id or str(item.get("provider_name") or "") in ("", "本机默认"):
            fields["provider_id"] = provider_id
        studio_store.update_agent(store, agent["id"], **fields)
        applied.append(agent["name"])
    return {"profile": _public(profile, store), "applied": applied, "skipped": skipped}
