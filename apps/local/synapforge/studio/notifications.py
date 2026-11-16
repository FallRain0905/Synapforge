"""桌面通知源：最近完成/失败的 run 与集群。

壳按游标轮询。游标取「已返回的最后一条时间」而不是 now()——如果取 now()，
查询与取时间之间完成的条目会被永久跳过。
"""

from __future__ import annotations

from typing import Any

from .store import now_iso

RUN_TERMINAL = ("DONE", "FAILED", "STOPPED")
CLUSTER_TERMINAL = ("DONE", "STOPPED")


def _clip(text: Any, limit: int = 90) -> str:
    value = " ".join(str(text or "").split())
    return value[:limit]


def recent_events(store: Any, *, since: str = "", limit: int = 20) -> dict[str, Any]:
    count = max(1, min(int(limit or 20), 50))
    cursor = str(since or "").strip()[:40] or now_iso()
    items: list[dict[str, Any]] = []

    run_marks = ", ".join("?" for _ in RUN_TERMINAL)
    runs = store.db.execute(
        f"""
        SELECT r.id, r.agent_id, r.status, r.task_text, r.finished_at,
               COALESCE(a.name, '') AS agent_name
          FROM local_agent_runs AS r
          LEFT JOIN local_agents AS a ON a.id = r.agent_id
         WHERE r.status IN ({run_marks})
           AND r.finished_at IS NOT NULL
           AND r.finished_at > ?
         ORDER BY r.finished_at ASC
         LIMIT ?
        """,
        (*RUN_TERMINAL, cursor, count),
    ).fetchall()
    for row in runs:
        items.append({
            "kind": "run",
            "id": str(row["id"]),
            "status": str(row["status"]),
            "title": _clip(row["task_text"]) or "任务",
            "agent_id": str(row["agent_id"]),
            "agent_name": str(row["agent_name"] or ""),
            "at": str(row["finished_at"]),
        })

    cluster_marks = ", ".join("?" for _ in CLUSTER_TERMINAL)
    clusters = store.db.execute(
        f"""
        SELECT id, name, status, updated_at
          FROM local_clusters
         WHERE status IN ({cluster_marks})
           AND updated_at > ?
         ORDER BY updated_at ASC
         LIMIT ?
        """,
        (*CLUSTER_TERMINAL, cursor, count),
    ).fetchall()
    for row in clusters:
        items.append({
            "kind": "cluster",
            "id": str(row["id"]),
            "status": str(row["status"]),
            "title": _clip(row["name"]) or "集群",
            "at": str(row["updated_at"]),
        })

    items.sort(key=lambda item: item["at"])
    items = items[:count]
    return {
        "ok": True,
        "cursor": items[-1]["at"] if items else cursor,
        "items": items,
    }
