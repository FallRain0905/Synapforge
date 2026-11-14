"""会话视图：把同一个 Claude session 的多次 run 聚合成一段对话。

会话统计从 `local_agent_runs` 实时聚合；`local_agent_sessions` 只存标题/归档，
避免再复制一份 run 数据。命令执行体没有 Claude session，不进入会话列表。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from . import store as studio_store
from .commands import StudioError

MAX_RUNS = 50
MAX_STEPS = 4000


def _tokens(usage: dict[str, Any]) -> int:
    return sum(int(usage.get(key) or 0) for key in
               ("input_tokens", "output_tokens",
                "cache_read_input_tokens", "cache_creation_input_tokens"))


def _group_runs(store: Any, agent_id: str | None = None,
                limit: int = 2000) -> dict[str, list[dict[str, Any]]]:
    runs = studio_store.list_all_agent_runs(store, limit=limit)
    providers = {item["id"]: item["name"] for item in studio_store.list_providers(store)}
    for run in runs:
        run["provider_name"] = providers.get(str(run.get("provider_id") or ""), "本机默认")
    groups: dict[str, list[dict[str, Any]]] = {}
    for run in runs:
        sid = str(run.get("session_id") or "").strip()
        if not sid:
            continue
        if agent_id and str(run.get("agent_id")) != str(agent_id):
            continue
        groups.setdefault(sid, []).append(run)
    for values in groups.values():
        values.sort(key=lambda item: str(item.get("started_at") or ""))
    return groups


def _session_summary(session_id: str, runs: list[dict[str, Any]],
                     meta: dict[str, Any] | None) -> dict[str, Any]:
    first = runs[0]
    last = runs[-1]
    latest_status = str(last.get("status") or "")
    active = [r for r in runs if str(r.get("status")) in ("RUNNING", "QUEUED")]
    status = str(active[-1].get("status")) if active else latest_status
    tokens = sum(_tokens(r.get("usage") or {}) for r in runs)
    cost = sum(float((r.get("usage") or {}).get("total_cost_usd") or 0) for r in runs)
    title = str((meta or {}).get("title") or "").strip()
    if not title:
        title = str(first.get("task_text") or "").strip().replace("\n", " ")[:80]
    return {
        "id": session_id,
        "agent_id": str(first.get("agent_id") or (meta or {}).get("agent_id") or ""),
        "agent_name": first.get("agent_name") or "",
        "title": title or session_id[:18],
        "archived": bool((meta or {}).get("archived")),
        "status": status,
        "runs": len(runs),
        "tokens": tokens,
        "cost": round(cost, 6),
        "provider_name": last.get("provider_name") or "本机默认",
        "model": last.get("model") or "",
        "last_task": str(last.get("task_text") or ""),
        "started_at": first.get("started_at"),
        "updated_at": last.get("finished_at") or last.get("started_at"),
    }


def list_sessions(store: Any, *, agent_id: str | None = None, query: str = "",
                  include_archived: bool = False) -> list[dict[str, Any]]:
    groups = _group_runs(store, agent_id)
    metas = {item["id"]: item for item in studio_store.list_agent_sessions(store, agent_id)}
    out: list[dict[str, Any]] = []
    needle = str(query or "").strip().lower()
    for session_id, runs in groups.items():
        item = _session_summary(session_id, runs, metas.get(session_id))
        if item["archived"] and not include_archived:
            continue
        if needle:
            haystack = " ".join([item["title"], item["agent_name"],
                                 item["last_task"], item["model"]]).lower()
            if needle not in haystack:
                continue
        out.append(item)
    out.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    return out


def _agent_for_session(store: Any, session_id: str,
                       runs: list[dict[str, Any]]) -> str:
    meta = studio_store.get_agent_session(store, session_id)
    if meta and meta.get("agent_id"):
        return str(meta["agent_id"])
    return str(runs[0].get("agent_id") or "") if runs else ""


def session_detail(store: Any, session_id: str) -> dict[str, Any]:
    sid = str(session_id)
    meta = studio_store.get_agent_session(store, sid)
    runs = _group_runs(store).get(sid, [])
    if not runs:
        raise StudioError("session_not_found", [sid])
    runs = runs[:MAX_RUNS]
    detail_runs: list[dict[str, Any]] = []
    total_steps = 0
    for run in runs:
        steps = studio_store.list_agent_steps(store, run["id"])
        if total_steps + len(steps) > MAX_STEPS:
            steps = steps[:max(0, MAX_STEPS - total_steps)]
        total_steps += len(steps)
        detail_runs.append({**run, "steps": steps})
    summary = _session_summary(sid, runs, meta)
    agent = studio_store.get_agent(store, summary["agent_id"])
    summary["agent_name"] = (agent or {}).get("name") or summary["agent_name"]
    return {"session": summary, "runs": detail_runs,
            "agent": agent, "meta": meta}


def rename_session(store: Any, session_id: str, title: str) -> dict[str, Any]:
    sid = str(session_id)
    label = str(title or "").strip()
    if not label:
        raise StudioError("session_title_required", ["会话标题不能为空"])
    if len(label) > 120:
        raise StudioError("session_title_too_long", ["会话标题不能超过 120 个字符"])
    meta = studio_store.get_agent_session(store, sid)
    if meta is None:
        groups = _group_runs(store)
        runs = groups.get(sid, [])
        if not runs:
            raise StudioError("session_not_found", [sid])
        meta = studio_store.ensure_agent_session(
            store, session_id=sid, agent_id=_agent_for_session(store, sid, runs),
            title=label)
    updated = studio_store.update_agent_session(store, sid, title=label)
    return updated or meta  # type: ignore[return-value]


def toggle_archive(store: Any, session_id: str,
                   archived: bool | None = None) -> dict[str, Any]:
    meta = studio_store.get_agent_session(store, str(session_id))
    if meta is None:
        raise StudioError("session_not_found", [session_id])
    target = (not meta["archived"]) if archived is None else bool(archived)
    updated = studio_store.update_agent_session(store, str(session_id), archived=target)
    return updated  # type: ignore[return-value]


def activate_session(store: Any, session_id: str) -> dict[str, Any]:
    """把 Agent 的当前 session 指到这个会话；下一个任务会 --resume 它。"""

    sid = str(session_id)
    runs = [run for run in studio_store.list_all_agent_runs(store, limit=2000)
            if str(run.get("session_id") or "") == sid]
    if not runs:
        raise StudioError("session_not_found", [sid])
    agent_id = _agent_for_session(store, sid, runs)
    agent = studio_store.get_agent(store, agent_id)
    if agent is None:
        raise StudioError("agent_not_found", [agent_id])
    updated = studio_store.update_agent(store, agent_id, session_id=sid)
    studio_store.ensure_agent_session(
        store, session_id=sid, agent_id=agent_id,
        title=str(runs[0].get("task_text") or "")[:80])
    return updated  # type: ignore[return-value]


def _stamp(value: Any) -> str:
    text = str(value or "")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return text[:16]


def export_markdown(store: Any, session_id: str) -> tuple[str, str]:
    detail = session_detail(store, session_id)
    session = detail["session"]
    safe = re.sub(r"[\\/:*?\"<>|]+", "-", session["title"])[:60] or "session"
    lines = [
        f"# {session['title']}",
        "",
        f"- Agent：{session['agent_name']}",
        f"- 渠道 / 模型：{session['provider_name']} / {session['model'] or '—'}",
        f"- 会话：`{session['id']}`",
        f"- 运行：{session['runs']} 次 · {session['tokens']} tokens · ${session['cost']:.3f}",
        f"- 时间：{_stamp(session['started_at'])} → {_stamp(session['updated_at'])}",
        "",
        "---",
        "",
    ]
    for index, run in enumerate(detail["runs"], start=1):
        lines += [f"## {index}. {str(run.get('task_text') or '').strip()}", "",
                  f"状态：{run.get('status')} · 模型：{run.get('model') or '—'} · "
                  f"耗时：{_stamp(run.get('started_at'))}", ""]
        for step in run.get("steps") or []:
            kind = str(step.get("kind") or "")
            if kind in ("task", "raw"):
                continue
            title = str(step.get("title") or kind)
            detail_text = str(step.get("detail") or "").strip()
            lines.append(f"### {title}" + (f" · {kind}" if kind else ""))
            if detail_text:
                lines += ["", "```text", detail_text, "```"]
            lines.append("")
        lines += ["---", ""]
    filename = f"{safe}-{session['id'][:8]}.md"
    return filename, "\n".join(lines).strip() + "\n"
