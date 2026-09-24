"""桌面通知验证数据的清理（DESKTOP-NOTIFY）：按外键顺序删干净，不留痕迹。

顺序：临时任务（先 leases/evidence/门禁/事件/outbox/聊天卡片）→ 探针账号（会话/成员关系）。

用法：cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_desktop_notify_cleanup.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "apps/api")

from app.store import Store  # noqa: E402


def main() -> None:
    store = Store("apps/api/data/platform.db")
    tasks = [row["id"] for row in store.db.execute("SELECT id FROM tasks WHERE title LIKE '[通知验证]%'")]
    for task_id in tasks:
        store.db.execute("DELETE FROM task_leases WHERE task_id = ?", (task_id,))
        store.db.execute("DELETE FROM project_messages WHERE ref_task_id = ?", (task_id,))
        store.db.execute("DELETE FROM gates WHERE target_type = 'task' AND target_id = ?", (task_id,))
        store.db.execute(
            "DELETE FROM event_outbox WHERE event_id IN (SELECT id FROM events WHERE object_id = ? OR payload LIKE ?)",
            (task_id, f"%{task_id}%"),
        )
        store.db.execute("DELETE FROM events WHERE object_id = ? OR payload LIKE ?", (task_id, f"%{task_id}%"))
        store.db.execute("DELETE FROM runs WHERE task_id = ?", (task_id,))
        store.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    members = [row["id"] for row in store.db.execute("SELECT id FROM human_members WHERE email LIKE 'notify-probe-%'")]
    for member_id in members:
        store.db.execute("DELETE FROM sessions WHERE member_id = ?", (member_id,))
        store.db.execute("DELETE FROM project_memberships WHERE member_id = ?", (member_id,))
        store.db.execute("DELETE FROM memberships WHERE member_id = ?", (member_id,))
        store.db.execute("DELETE FROM human_members WHERE id = ?", (member_id,))
    store.db.commit()
    left_tasks = store.db.execute("SELECT COUNT(*) FROM tasks WHERE title LIKE '[通知验证]%'").fetchone()[0]
    left_members = store.db.execute("SELECT COUNT(*) FROM human_members WHERE email LIKE 'notify-probe-%'").fetchone()[0]
    print(f"清理完成：临时任务 {len(tasks)} → 剩 {left_tasks}；探针账号 {len(members)} → 剩 {left_members}")
    store.close()
    raise SystemExit(0 if left_tasks == 0 and left_members == 0 else 1)


if __name__ == "__main__":
    main()