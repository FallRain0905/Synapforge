"""集群规划：先和主 Agent 聊清楚，再生成任务卡，确认后才建集群。

设计要点：

- **规划对话复用单 Agent 链路**：每条用户消息就是一条 `local_agent_run`，
  多轮靠 `--resume` 串起来，SSE / 步骤 / 产物全部沿用。
- **计划是草稿**：存在 `local_cluster_plans`，确认前不建集群、不建任务。
- **解析容错**：主 Agent 的回复可能带围栏、说明文字、裸数组；解析失败
  如实返回错误和原文，让用户在界面上改，不静默吞掉。
- **确认即执行**：按 `(agent_id, model)` 分组建角色，每个条目预置一条任务，
  然后以 `kickoff=False` 启动，避免 lead 再拆一遍。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .. import commands as base
from ..paths import state_dir
from . import agents as studio_agents
from . import commands as studio_commands
from . import store as studio_store
from .commands import StudioError

MAX_ITEMS = 20

PLAN_INSTRUCTION = """现在请把上面的讨论整理成**可执行的任务列表**，只输出一个 JSON 数组，不要其它内容。

每个元素：
{
  "title": "任务标题（短，可识别）",
  "description": "具体要做什么、交付什么文件",
  "agent_name": "执行者名字（从下面列表里选）",
  "role_prompt": "给这个执行者的一句话补充要求（可空）"
}

可用的执行者：
{agents}

要求：
1. 任务之间尽量独立，能并行执行；
2. 每个任务都要有明确的产出文件；
3. 不要写"等待"、"依赖"这类无法并行的工作；
4. 只输出 JSON 数组。"""


# ---- 解析 -------------------------------------------------------------------


def _strip_fence(text: str) -> str:
    match = re.search(r"```(?:json)?\s*(.+?)```", text, re.S)
    return match.group(1).strip() if match else text


def _balanced_array(text: str) -> str:
    start = text.find("[")
    if start < 0:
        return ""
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""


def parse_items(text: str) -> dict[str, Any]:
    """从模型回复里提取任务数组；失败时返回 ok=False 和原文。"""

    raw = str(text or "").strip()
    if not raw:
        return {"ok": False, "error": "回复是空的", "raw": ""}
    candidates = [raw]
    fenced = _strip_fence(raw)
    if fenced != raw:
        candidates.append(fenced)
    balanced = _balanced_array(fenced if fenced != raw else raw)
    if balanced:
        candidates.append(balanced)

    last_error = ""
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except ValueError as error:
            last_error = str(error)
            continue
        items = data.get("tasks") if isinstance(data, dict) else data
        if not isinstance(items, list):
            last_error = "JSON 不是数组"
            continue
        cleaned = []
        for item in items[:MAX_ITEMS]:
            if isinstance(item, str):
                cleaned.append({"title": item[:120], "description": item,
                                "agent_name": "", "role_prompt": ""})
                continue
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("name") or "").strip()
            if not title:
                continue
            cleaned.append({
                "title": title[:120],
                "description": str(item.get("description") or item.get("detail") or "").strip()[:4000],
                "agent_name": str(item.get("agent_name") or item.get("agent") or "").strip(),
                "role_prompt": str(item.get("role_prompt") or "").strip()[:500],
                "model": str(item.get("model") or "").strip(),
            })
        if not cleaned:
            last_error = "数组里没有可用条目（每条至少要有 title）"
            continue
        return {"ok": True, "items": cleaned, "raw": raw[:2000]}
    hint = "回复里没有找到 JSON 任务数组" if "[" not in raw else last_error
    return {"ok": False, "error": hint or "没有找到 JSON 数组", "raw": raw[:2000]}


def _resolve_items(store: Any, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 agent_name 解析成真实 Agent；解析不到的标 invalid 并说明原因。"""

    agents = studio_store.list_agents(store)
    by_name = {a["name"]: a for a in agents}
    by_id = {a["id"]: a for a in agents}
    resolved: list[dict[str, Any]] = []
    for item in items:
        entry = dict(item)
        agent = by_id.get(str(entry.get("agent_id") or "")) or \
            by_name.get(str(entry.get("agent_name") or "").strip())
        if agent is None and len(agents) == 1:
            agent = agents[0]  # 只有一个 Agent 时不用挑
        if agent is None:
            entry["agent_id"] = ""
            entry["agent_name"] = str(entry.get("agent_name") or "")
            entry["invalid"] = f"找不到 Agent「{entry['agent_name'] or '未指定'}」"
        else:
            entry["agent_id"] = agent["id"]
            entry["agent_name"] = agent["name"]
            entry["invalid"] = ""
            if not entry.get("model"):
                entry["model"] = agent.get("model") or ""
        resolved.append(entry)
    return resolved


# ---- 规划生命周期 -----------------------------------------------------------


def create_plan(store: Any, *, lead_agent_id: str, title: str = "",
                objective: str = "") -> dict[str, Any]:
    lead = studio_store.get_agent(store, str(lead_agent_id))
    if lead is None:
        raise StudioError("agent_not_found", [str(lead_agent_id)])
    plan = studio_store.create_cluster_plan(
        store, lead_agent_id=lead["id"], title=str(title or "").strip(),
        objective=str(objective or "").strip())
    return plan_state(store, plan["id"])


def plan_state(store: Any, plan_id: str) -> dict[str, Any]:
    plan = studio_store.get_cluster_plan(store, plan_id)
    if plan is None:
        raise StudioError("plan_not_found", [str(plan_id)])
    lead = studio_store.get_agent(store, plan["lead_agent_id"])
    runs = studio_store.list_agent_runs(store, plan["lead_agent_id"], limit=30)
    chat_runs = [r for r in runs if str(r.get("session_id") or "") ==
                 str((lead or {}).get("session_id") or "")] if lead else []
    latest = chat_runs[0] if chat_runs else (runs[0] if runs else None)
    steps = studio_store.list_agent_steps(store, latest["id"]) if latest else []
    return {
        **plan,
        "lead": lead,
        "lead_name": (lead or {}).get("name") or "",
        "run": latest,
        "steps": steps,
        "items": _resolve_items(store, plan["items"] or []),
        "agents": [{"id": a["id"], "name": a["name"], "model": a.get("model") or "",
                    "provider_id": a.get("provider_id") or "",
                    "provider_name": a.get("provider_name") or ""}
                   for a in studio_store.list_agents(store)],
    }


def list_plans(store: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for plan in studio_store.list_cluster_plans(store):
        lead = studio_store.get_agent(store, plan["lead_agent_id"])
        out.append({
            **plan,
            "lead_name": (lead or {}).get("name") or "",
            "item_count": len(plan["items"] or []),
        })
    return out


def send_message(store: Any, plan_id: str, text: str) -> dict[str, Any]:
    """用户发一句话：建一条 run（续接 lead 会话），由 AgentWorker 执行。"""

    plan = studio_store.get_cluster_plan(store, plan_id)
    if plan is None:
        raise StudioError("plan_not_found", [str(plan_id)])
    body = str(text or "").strip()
    if not body:
        raise StudioError("message_required", ["消息不能为空"])
    result = studio_agents.start_task(store, plan["lead_agent_id"], body)
    studio_store.update_cluster_plan(
        store, plan_id, chat_run_id=str(result["run"]["id"]), status="CHAT")
    return {"plan": studio_store.get_cluster_plan(store, plan_id), **result}


def _last_assistant_text(store: Any, run_id: str) -> str:
    if not run_id:
        return ""
    steps = studio_store.list_agent_steps(store, str(run_id))
    for step in reversed(steps):
        if str(step.get("kind")) in ("assistant", "result"):
            text = str(step.get("detail") or "").strip()
            if text:
                return text
    return ""


def generate_plan(store: Any, plan_id: str, *, text: str = "") -> dict[str, Any]:
    """从主 Agent 最后一条回复里提取任务卡；也可直接传入文本（重试）。"""

    plan = studio_store.get_cluster_plan(store, plan_id)
    if plan is None:
        raise StudioError("plan_not_found", [str(plan_id)])
    source = str(text or "").strip() or _last_assistant_text(store, plan["chat_run_id"])
    parsed = parse_items(source)
    if not parsed["ok"]:
        return {"ok": False, "plan": plan, **parsed}
    items = _resolve_items(store, parsed["items"])
    updated = studio_store.update_cluster_plan(
        store, plan_id, items=items, status="READY")
    return {"ok": True, "plan": updated, "items": items, "raw": parsed["raw"]}


def update_items(store: Any, plan_id: str,
                 items: list[dict[str, Any]]) -> dict[str, Any]:
    plan = studio_store.get_cluster_plan(store, plan_id)
    if plan is None:
        raise StudioError("plan_not_found", [str(plan_id)])
    if not isinstance(items, list):
        raise StudioError("items_invalid", ["items 必须是数组"])
    cleaned = []
    for item in items[:MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        cleaned.append({
            "title": title[:120],
            "description": str(item.get("description") or "").strip()[:4000],
            "agent_id": str(item.get("agent_id") or "").strip(),
            "agent_name": str(item.get("agent_name") or "").strip(),
            "model": str(item.get("model") or "").strip(),
            "role_prompt": str(item.get("role_prompt") or "").strip()[:500],
        })
    resolved = _resolve_items(store, cleaned)
    updated = studio_store.update_cluster_plan(store, plan_id, items=resolved)
    return {"plan": updated, "items": resolved}


def plan_prompt(store: Any, plan_id: str) -> str:
    """让主 Agent 输出任务卡的那段指令（附可用执行者名单）。"""

    agents = studio_store.list_agents(store)
    listing = "\n".join(
        f"- {a['name']}（模型 {a.get('model') or '默认'}）" for a in agents) or "（没有 Agent）"
    return PLAN_INSTRUCTION.replace("{agents}", listing)


def _unique_role_name(existing: set[str], base_name: str) -> str:
    name = base_name[:56]
    if name not in existing:
        return name
    index = 2
    while f"{name}-{index}" in existing:
        index += 1
    return f"{name}-{index}"


def confirm_plan(store: Any, plan_id: str,
                 items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """确认计划：按 (agent, model) 建角色 → 预置任务 → 启动集群（不 kickoff）。"""

    plan = studio_store.get_cluster_plan(store, plan_id)
    if plan is None:
        raise StudioError("plan_not_found", [str(plan_id)])
    if items is not None:
        update_items(store, plan_id, items)
        plan = studio_store.get_cluster_plan(store, plan_id)
    entries = _resolve_items(store, (plan or {}).get("items") or [])
    if not entries:
        raise StudioError("plan_empty", ["计划里还没有任务"])
    invalid = [item for item in entries if item.get("invalid")]
    if invalid:
        raise StudioError("plan_invalid_items",
                          [f"{item['title']}: {item['invalid']}" for item in invalid])
    if plan["status"] == "CONFIRMED" and plan["cluster_id"]:
        return {"plan": plan, "cluster": studio_commands.cluster_detail(
            store, plan["cluster_id"]), "already": True}

    # 按 (agent_id, model) 分组：同一执行者同一模型共用一个角色
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for item in entries:
        key = (item["agent_id"], item.get("model") or "")
        groups.setdefault(key, {"agent_id": item["agent_id"],
                                "agent_name": item["agent_name"],
                                "model": item.get("model") or "",
                                "items": []})
        groups[key]["items"].append(item)

    ordered = list(groups.values())
    lead_group = ordered[0]
    worker_groups = ordered[1:] or ordered[:1]
    if not worker_groups:
        worker_groups = [lead_group]

    objective = plan["objective"] or "；".join(item["title"] for item in entries[:4])
    title = plan["title"] or (entries[0]["title"][:40])
    workspace_root = str(Path(state_dir()) / "workspace" / f"plan-{plan_id[-8:]}")

    role_specs: list[dict[str, Any]] = []
    existing_names: set[str] = set()
    for index, group in enumerate([lead_group] + worker_groups):
        name = _unique_role_name(existing_names, group["agent_name"])
        existing_names.add(name)
        prompts = [item.get("role_prompt") or item["description"] for item in group["items"]]
        role_specs.append({
            "agent_id": group["agent_id"],
            "name": name,
            "model": group["model"],
            "role_prompt": ("；".join(p for p in prompts if p)[:400]),
            "lead": index == 0,
        })

    lead_spec = role_specs[0]
    roles_payload = [{k: v for k, v in spec.items() if k != "lead"}
                     for spec in role_specs[1:]] or [
        {k: v for k, v in lead_spec.items() if k != "lead"}]

    detail = studio_commands.create_cluster(
        store, name=f"计划 · {title}"[:60], objective=objective,
        workspace_root=workspace_root,
        lead={k: v for k, v in lead_spec.items() if k != "lead"},
        roles=roles_payload,
        policy={"auto_approve": True, "max_parallel_workers": max(1, len(role_specs)),
                "human_review": False})

    cluster_roles = detail["roles"]
    role_by_agent: dict[str, dict[str, Any]] = {}
    for role in cluster_roles:
        role_by_agent.setdefault(str(role["local_agent_id"]), role)
    # 同 (agent, model) 可能被拆成多个角色；按出现顺序消费
    pending = list(cluster_roles)
    assigned: dict[tuple[str, str], dict[str, Any]] = {}
    for key, group in groups.items():
        match = next((r for r in pending
                      if str(r["local_agent_id"]) == key[0]
                      and str(r["model"] or "") == key[1]), None)
        if match is None:
            match = next((r for r in pending if str(r["local_agent_id"]) == key[0]), None)
        if match is not None:
            pending.remove(match)
            assigned[key] = match

    created_tasks: list[dict[str, Any]] = []
    for item in entries:
        role = assigned.get((item["agent_id"], item.get("model") or ""))
        if role is None:
            continue
        task = base.create_task(
            store, project_id=detail["project_id"], title=item["title"][:120],
            description=item.get("description") or item["title"],
            stage="plan", assignee_agent_id=role["agent_id"],
            requires_review=False)
        created_tasks.append(task)

    studio_store.update_cluster_plan(
        store, plan_id, status="CONFIRMED", cluster_id=detail["id"])
    return {"plan": studio_store.get_cluster_plan(store, plan_id),
            "cluster": studio_commands.cluster_detail(store, detail["id"]),
            "tasks": created_tasks,
            "roles": cluster_roles}


def discard_plan(store: Any, plan_id: str) -> dict[str, Any]:
    plan = studio_store.get_cluster_plan(store, plan_id)
    if plan is None:
        raise StudioError("plan_not_found", [str(plan_id)])
    studio_store.update_cluster_plan(store, plan_id, status="DISCARDED")
    return {"discarded": True, "plan_id": plan_id}
