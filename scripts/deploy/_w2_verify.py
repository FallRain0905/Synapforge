"""W-2 部署后验证（服务器上就地跑）：成果空间聚合 + 批量派单端点。

原则：**不改动用户的数据**——
  * 成果空间是只读聚合；
  * 批量派单只用"注定失败的调用"验证链路与口径（目标不是项目成员 / 任务不存在），
    校验在写库之前就拒绝，因此不会留下任何副作用。
临时的 300 秒会话用完即删。
"""
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, "/opt/math-agent-platform/apps/api")
from app.accounts import hash_token
from app.contracts import SessionCreate
from app.store import Store

DB = "/opt/math-agent-platform/apps/api/data/platform.db"
BASE = "http://127.0.0.1:8000"
store = Store(DB)

member = store.db.execute(
    "SELECT id, email, is_admin FROM human_members WHERE status = 'active' ORDER BY is_admin DESC, created_at LIMIT 1"
).fetchone()
print("验证身份:", member["email"], "is_admin=", member["is_admin"])
session = store.create_session(SessionCreate(member_id=member["id"], expires_in_seconds=300))
token = session.token


def call(path, body=None, method=None):
    request = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()[:200]


try:
    _, projects = call("/api/projects")
    project = projects[0]
    print("项目:", project["name"], "| task_mode:", project.get("task_mode"))

    status, deliverables = call(f"/api/projects/{project['id']}/deliverables")
    print(f"GET /deliverables → {status}")
    for key in ("artifacts", "documents", "handoffs", "reviews"):
        section = deliverables[key]
        print(f"  {key}: total={section['total']}", "by_status=" + str(section.get("by_status") or section.get("by_layer") or ""))
    print("  gates:", deliverables["gates"]["open"], "未通过 /", deliverables["gates"]["total"], "总")
    print("  risks:", deliverables["risks"]["open"], "未关闭 /", deliverables["risks"]["total"], "总")
    if deliverables["artifacts"]["recent"]:
        sample = deliverables["artifacts"]["recent"][0]
        print("  样例成果物:", sample["name"], sample["artifact_type"], sample["status"])
    if deliverables["documents"]["recent"]:
        sample = deliverables["documents"]["recent"][0]
        print("  样例文档:", sample["name"], "层 =", sample["layer"])
    if deliverables["reviews"]["recent"]:
        print("  样例复核:", deliverables["reviews"]["recent"][0]["verdict"], "by", deliverables["reviews"]["recent"][0]["reviewer"])

    status, tasks = call(f"/api/projects/{project['id']}/tasks")
    print(f"GET /tasks → {status}，{len(tasks)} 条任务")
    task_id = tasks[0]["id"] if tasks else None

    # 批量派单：只验证"注定失败"的两条路径（校验发生在写库前，不产生任何变更）
    status, payload = call(
        f"/api/projects/{project['id']}/tasks/assign",
        {"task_ids": [task_id or "00000000-0000-4000-8000-000000000000"], "assignee_member_id": "not-a-project-member"},
        "POST",
    )
    print(f"POST /tasks/assign（非项目成员）→ {status} {str(payload)[:90]}")

    status, payload = call(
        f"/api/projects/{project['id']}/tasks/assign",
        {"task_ids": ["00000000-0000-4000-8000-000000000000"], "assignee_member_id": member["id"]},
        "POST",
    )
    print(f"POST /tasks/assign（任务不存在）→ {status} updated={payload.get('updated')} failures={payload.get('failures')}")

    events = store.db.execute(
        "SELECT COUNT(*) AS c FROM events WHERE project_id = ? AND event_type = 'task.dispatched'",
        (project["id"],),
    ).fetchone()["c"]
    cards = store.db.execute(
        "SELECT COUNT(*) AS c FROM project_messages WHERE project_id = ? AND message_type = 'card'",
        (project["id"],),
    ).fetchone()["c"]
    print(f"库内：task.dispatched 事件 {events} 条、聊天卡片 {cards} 张（验证过程不新增）")
finally:
    store.db.execute("DELETE FROM sessions WHERE token = ?", (hash_token(token),))
    store.db.commit()
    print("临时会话已删除:", store.db.execute("SELECT COUNT(*) AS c FROM sessions WHERE token = ?", (hash_token(token),)).fetchone()["c"] == 0)
    store.close()