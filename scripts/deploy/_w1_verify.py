"""W-1 部署后验证：在服务器上就地检查工作区端点在**真实数据**上的表现。

做法：以 root 直接在库里给"首个在职成员"签发一个 300 秒的临时会话 → 用它打本机 API
（概览 / 聊天流 / 发言 / 卡片回填统计）→ 立刻删除该会话。用完即焚，不留在库里。

只读业务数据：除了"发一条群聊消息"（可删）之外不改任何东西；那条消息 id 会打印出来。
"""
import json
import sys
import urllib.request

sys.path.insert(0, "/opt/math-agent-platform/apps/api")
from app.accounts import hash_token
from app.contracts import ProjectMessageCreate, SessionCreate
from app.store import Store

DB = "/opt/math-agent-platform/apps/api/data/platform.db"
BASE = "http://127.0.0.1:8000"
store = Store(DB)

member_row = store.db.execute(
    "SELECT id, email, is_admin FROM human_members WHERE status = 'active' ORDER BY is_admin DESC, created_at LIMIT 1"
).fetchone()
print("验证身份:", member_row["email"], "is_admin=", member_row["is_admin"])
session = store.create_session(SessionCreate(member_id=member_row["id"], expires_in_seconds=300))
token = session.token


def call(path, body=None, method=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    request = urllib.request.Request(f"{BASE}{path}", headers=headers, data=json.dumps(body).encode() if body is not None else None, method=method)
    with urllib.request.urlopen(request) as response:
        return response.status, json.load(response)


try:
    status, projects = call("/api/projects")
    print(f"GET /api/projects → {status}，项目 {len(projects)} 个")
    if not projects:
        print("没有项目，无法验证工作区")
        raise SystemExit(0)
    project = projects[0]
    print("用第一个项目:", project["name"], "| goal:", project.get("goal"), "| task_mode:", project.get("task_mode"))

    status, overview = call(f"/api/projects/{project['id']}/workspace")
    viewer = overview["viewer"]
    print(f"GET /workspace → {status}")
    print("  viewer:", viewer)
    print("  成员:", [(m["display_name"], m["role"], m["open_tasks"], m["agents"]) for m in overview["members"]])
    print("  Agent:", [(a["display_name"], a["connected"], a["current_task_title"]) for a in overview["agents"]][:4], f"...共 {len(overview['agents'])}")
    print("  任务 by_status:", overview["tasks"]["by_status"], "| 未结束:", len(overview["tasks"]["open_items"]))
    print("  成果物:", {k: v for k, v in overview["artifacts"].items() if k != "recent"})
    print("  聊天(概览内):", len(overview["messages"]), "条")

    status, messages = call(f"/api/projects/{project['id']}/messages?limit=200")
    kinds = {}
    for message in messages:
        kinds[(message["sender_kind"], message["message_type"])] = kinds.get((message["sender_kind"], message["message_type"]), 0) + 1
    print(f"GET /messages → {status}，最近 {len(messages)} 条；分布 {kinds}")
    if messages:
        print("  样例:", [(m["seq"], m["sender_kind"], m["content"][:44]) for m in messages[-3:]])

    status, posted = call(
        f"/api/projects/{project['id']}/messages",
        ProjectMessageCreate(content="W-1 部署验证：这条是发布后自检发的，可删").model_dump(),
    )
    print(f"POST /messages → {status}，新消息 seq={posted['seq']} id={posted['id']}")

    total = store.db.execute("SELECT COUNT(*) AS c FROM project_messages").fetchone()["c"]
    cards = store.db.execute("SELECT COUNT(*) AS c FROM project_messages WHERE message_type = 'card'").fetchone()["c"]
    humans = store.db.execute("SELECT COUNT(*) AS c FROM project_messages WHERE message_type = 'text'").fetchone()["c"]
    watermarks = store.db.execute("SELECT COUNT(*) AS c FROM project_chat_watermarks").fetchone()["c"]
    print(f"库内统计：project_messages {total} 条（卡片 {cards} / 人类 {humans}），水位线 {watermarks} 个项目")
    print("回填样例（最早 2 条卡片）:", [(m["seq"], m["content"][:40]) for m in store.list_project_messages(project["id"], limit=2)])
finally:
    store.db.execute("DELETE FROM sessions WHERE token = ?", (hash_token(token),))
    store.db.commit()
    print("临时会话已删除:", store.db.execute("SELECT COUNT(*) AS c FROM sessions WHERE token = ?", (hash_token(token),)).fetchone()["c"] == 0)
    store.close()