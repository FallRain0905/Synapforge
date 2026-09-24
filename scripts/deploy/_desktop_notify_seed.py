"""桌面通知的线上验证数据（DESKTOP-NOTIFY）：给临时探针账号造"派给我 + 待我复核"两件事。

做三件事（都用平台自己的 API，不伪造 SQL 语义）：
  1. 建临时探针账号（开放注册）；
  2. 在演示项目里给探针一个**派给他的任务**（assignee_member_id = 探针成员）；
  3. 把探针在该项目的角色提到 reviewer（这样 WAITING_REVIEW 的任务算"待他复核"），
     并造一条 WAITING_REVIEW 的任务；
输出探针邮箱 + 项目/任务 id，供 `_desktop_notify_check` 用。

用法：cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_desktop_notify_seed.py
之后必须跑 `_desktop_notify_cleanup.py`（或带 --cleanup 一起跑）。
"""

from __future__ import annotations

import json
import sys
from uuid import uuid4

sys.path.insert(0, "apps/api")

from app.contracts import RegisterRequest, TaskCreate  # noqa: E402
from app.store import Store  # noqa: E402

DEMO_NAME = "演示 · 多 Agent 文档撰写"


def main() -> None:
    store = Store("apps/api/data/platform.db")
    project = next((p for p in store.list_projects() if p.name == DEMO_NAME), None)
    if project is None:
        raise SystemExit("没找到演示项目")

    email = f"notify-probe-{uuid4().hex[:10]}@example.com"
    password = f"Notify-{uuid4().hex[:12]}"
    session, account = store.register_account(
        RegisterRequest(email=email, password=password, display_name="通知探针"),
        allow_open_registration=True,
    )
    member_id = account.member.id
    store.add_project_member(project.id, member_id, "reviewer")

    assigned = store.create_task(
        project.id,
        TaskCreate(title="[通知验证] 派给我的任务", description="桌面通知验证用，稍后删除", assignee_member_id=member_id),
    )
    waiting = store.create_task(project.id, TaskCreate(title="[通知验证] 待我复核的任务", description="桌面通知验证用，稍后删除"))
    store.db.execute("UPDATE tasks SET status = 'WAITING_REVIEW' WHERE id = ?", (str(waiting.id),))
    store.db.commit()

    print(
        json.dumps(
            {
                "email": email,
                "password": password,
                "member_id": member_id,
                "project_id": str(project.id),
                "assigned_task_id": str(assigned.id),
                "review_task_id": str(waiting.id),
                "token": session.token,
            },
            ensure_ascii=False,
        )
    )
    store.close()


if __name__ == "__main__":
    main()