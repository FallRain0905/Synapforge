"""本地只读查看器：左侧 Agent 列表，右侧该 Agent 的活动流。

**不复用旧前端**（不复用 `apps/web` 的任何东西）——那是一万五千行 Next.js，而这里只需要
一个页面。用 stdlib `http.server` + 一个静态 HTML，**没有构建步骤、没有 node_modules**。

护栏一（机制而非纪律）：数据库连接以 `mode=ro` 打开，**物理上无法写**。
护栏三：按需启动、绑 127.0.0.1、关掉就没了，不进 systemd、不开机自启。
护栏四：关掉它协作照常跑——它只是把事件日志渲染出来，不参与任何决策。
"""

from __future__ import annotations

import json
import sqlite3
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .db import open_readonly_connection
from .paths import db_path

STATIC_DIR = Path(__file__).resolve().parent / "static"
DEFAULT_STREAM_LIMIT = 300


# ---- 数据读取（全部只读）---------------------------------------------------


def _rows(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict[str, Any]]:
    try:
        return [dict(row) for row in conn.execute(query, params).fetchall()]
    except sqlite3.Error as exc:
        # 查询写错就写错——不要静默变成"空列表"，那会让页面看起来正常而实际缺数据。
        print(f"[viewer] query failed: {exc} :: {' '.join(query.split())[:120]}", file=sys.stderr)
        return []


def list_agents(conn: sqlite3.Connection, only: list[str] | None = None) -> list[dict[str, Any]]:
    agents = _rows(conn, "SELECT agent_id, display_name, status, last_seen, package_source"
                         " FROM agents ORDER BY agent_id")
    if only:
        wanted = set(only)
        agents = [a for a in agents if a["agent_id"] in wanted]
    out: list[dict[str, Any]] = []
    for agent in agents:
        agent_id = agent["agent_id"]
        in_flight = _rows(
            conn,
            "SELECT COUNT(*) AS c FROM task_leases l JOIN tasks t ON t.id = l.task_id"
            " WHERE l.status = 'ACTIVE' AND l.agent_id = ?", (agent_id,))
        open_requests = _rows(
            conn,
            "SELECT COUNT(*) AS c FROM information_requests"
            " WHERE provider_agent_id = ? AND status IN ('OPEN','ROUTED')", (agent_id,))
        artifacts = _rows(
            conn, "SELECT COUNT(*) AS c FROM artifacts WHERE created_by = ?", (agent_id,))
        last = _rows(
            conn, "SELECT MAX(created_at) AS t FROM events WHERE actor = ?", (agent_id,))
        out.append({
            "agent_id": agent_id,
            "display_name": agent.get("display_name") or agent_id,
            "status": agent.get("status") or "unknown",
            "last_seen": agent.get("last_seen"),
            "last_event_at": (last[0]["t"] if last else None),
            "in_flight": int(in_flight[0]["c"]) if in_flight else 0,
            "open_requests": int(open_requests[0]["c"]) if open_requests else 0,
            "artifacts": int(artifacts[0]["c"]) if artifacts else 0,
        })
    return out


# 事件 → 人话。渲染放服务端，页面只负责排版。
_KIND = {
    "task.claimed": ("work", "领取任务"),
    "task.progress": ("work", "开始执行"),
    "task.result_submitted": ("work", "提交结果"),
    "project.task.created": ("system", "任务创建"),
    "project.information_request.created": ("ask", "提问"),
    "project.information_request.acked": ("ack", "已确认收到"),
    "project.information_request.answered": ("answer", "回答"),
    "project.information_request.consumed": ("consume", "收到回复，继续"),
    "project.feasibility_concern.raised": ("concern", "诚实停止"),
    "project.feasibility_concern.decided": ("system", "异议已裁决"),
    "project.stage_report.submitted": ("report", "阶段报告"),
    "artifact.created": ("artifact", "登记成果物"),
    "project.artifact.uploaded": ("artifact", "成果物入库"),
    "artifact.content_stored": ("artifact", "成果物内容落盘"),
    "project.created": ("system", "项目创建"),
    "project.message.posted": ("message", "消息"),
}


def _render(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    kind, label = _KIND.get(event["event_type"], ("system", event["event_type"]))
    text = ""
    meta: dict[str, Any] = {}
    if event["event_type"] == "project.information_request.created":
        text = str(payload.get("question") or "")
        meta = {"to": payload.get("provider_agent_id"), "blocking": payload.get("blocking")}
    elif event["event_type"] == "project.information_request.answered":
        text = str(payload.get("answer_summary") or payload.get("response_status") or "")
        meta = {"request": payload.get("request_id"), "effect": payload.get("node_effect")}
    elif event["event_type"] == "project.information_request.consumed":
        text = f"节点效果：{payload.get('node_effect') or ''}"
    elif event["event_type"] == "project.feasibility_concern.raised":
        text = str(payload.get("claim") or payload.get("concern_type") or "")
        meta = {"type": payload.get("concern_type")}
    elif event["event_type"] == "project.stage_report.submitted":
        text = str(payload.get("summary") or "")
        meta = {"status": payload.get("status")}
    elif event["event_type"] in ("artifact.created", "project.artifact.uploaded"):
        text = str(payload.get("name") or payload.get("artifact_id") or "")
        meta = {"receipt": payload.get("receipt_version")}
    elif event["event_type"] == "project.feasibility_concern.decided":
        text = f"决策：{payload.get('policy')}"
    elif event["event_type"] == "task.progress":
        text = str(payload.get("message") or payload.get("status") or "")
    elif event["event_type"] == "task.result_submitted":
        text = f"任务状态：{payload.get('status') or '—'}"
        meta = {"accepted": payload.get("accepted")}
    elif event["event_type"] == "task.claimed":
        text = "领取任务"
    elif event["event_type"] == "project.task.created":
        text = str(payload.get("title") or "")
        meta = {"stage": payload.get("stage")}
    elif event["event_type"] == "project.message.posted":
        text = str(payload.get("text") or "")
        meta = {"to": payload.get("to")}
    return {"kind": kind, "label": label, "text": text, "meta": meta}


def agent_stream(conn: sqlite3.Connection, agent_id: str,
                 limit: int = DEFAULT_STREAM_LIMIT) -> dict[str, Any]:
    """该 Agent 的活动流：以它为主语的事件（它说了什么、做了什么、产出了什么）。

    只读渲染事件日志——不对状态做任何推断，页面上看到的每一行都对应一条持久化事实。
    """

    rows = _rows(
        conn,
        "SELECT sequence, event_type, actor, created_at, payload, object_type, object_id"
        " FROM events WHERE actor = ? ORDER BY sequence DESC LIMIT ?",
        (agent_id, int(limit)),
    )
    stream: list[dict[str, Any]] = []
    for row in reversed(rows):
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError):
            payload = {}
        event = {**row, "payload": payload}
        stream.append({
            "sequence": row["sequence"],
            "at": row["created_at"],
            "event_type": row["event_type"],
            "object_type": row.get("object_type"),
            "object_id": row.get("object_id"),
            **_render(event),
        })

    # 待办摘要：让右栏顶部能显示"它在等什么 / 有什么要它做"
    # 注意列名是 id，不是 request_id（用错列名会静默返回空列表）
    pending = _rows(
        conn,
        "SELECT id AS request_id, question, requester_agent_id, status, blocking, created_at"
        " FROM information_requests WHERE provider_agent_id = ? AND status IN ('OPEN','ROUTED')"
        " ORDER BY created_at DESC", (agent_id,))
    waiting_on = _rows(
        conn,
        "SELECT id AS request_id, question, provider_agent_id, status, created_at"
        " FROM information_requests WHERE requester_agent_id = ?"
        " AND status IN ('OPEN','ROUTED','ANSWERED','PROVISIONAL')"
        " ORDER BY created_at DESC", (agent_id,))

    # 派给它、还没被领的任务。与 `commands.inbox.assigned_to_me` 同一口径——
    # 这里只是只读投影，事实源仍是 tasks 表，不构成第二套判断。
    owner = _rows(conn, "SELECT owner_member_id FROM agents WHERE agent_id = ?", (agent_id,))
    member_id = (owner[0]["owner_member_id"] if owner else None) or f"local-agent:{agent_id}"
    assigned_to_me = _rows(
        conn,
        "SELECT t.id, t.title, t.status, t.stage, t.updated_at FROM tasks t"
        " WHERE t.assignee_member_id = ? AND t.status IN ('READY','NEEDS_REVISION')"
        " AND NOT EXISTS (SELECT 1 FROM task_leases l"
        "                 WHERE l.task_id = t.id AND l.status = 'ACTIVE')"
        " ORDER BY (t.deadline IS NULL), t.deadline ASC, t.updated_at ASC", (member_id,))

    return {"agent_id": agent_id, "member_id": member_id, "events": stream,
            "pending": pending, "waiting_on": waiting_on, "assigned_to_me": assigned_to_me}


# 群聊只显示"人说的话"与"人做的事"；内容落盘、播种这类机械事件不进对话流。
_GROUP_EVENTS = frozenset({
    "project.task.created", "task.claimed", "task.progress", "task.result_submitted",
    "project.information_request.created", "project.information_request.acked",
    "project.information_request.answered", "project.information_request.consumed",
    "project.feasibility_concern.raised", "project.feasibility_concern.decided",
    "project.stage_report.submitted",
    "artifact.created", "project.artifact.uploaded",
    "project.message.posted",
})


def _agent_names(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["agent_id"]: (row.get("display_name") or row["agent_id"])
            for row in _rows(conn, "SELECT agent_id, display_name FROM agents")}


def latest_project_id(conn: sqlite3.Connection) -> str | None:
    """最近有事件的项目。"最近创建的"未必是"正在协作的"。

    **不能按 `sequence` 排**——`sequence` 是按项目各自编号的（`UNIQUE(project_id, sequence)`），
    不是全局时钟，按它排会永远选中事件最多的那个项目。
    """

    recent = _rows(conn, "SELECT project_id FROM events ORDER BY created_at DESC LIMIT 1")
    if recent:
        return str(recent[0]["project_id"])
    rows = _rows(conn, "SELECT id FROM projects ORDER BY updated_at DESC LIMIT 1")
    return str(rows[0]["id"]) if rows else None


def group_stream(conn: sqlite3.Connection, project_id: str | None = None,
                 limit: int = DEFAULT_STREAM_LIMIT) -> dict[str, Any]:
    """群聊：把项目里所有 Agent 的事件合成一条时间线，读起来像一场多方对话。

    与单 Agent 视图的区别只在**聚合维度**：事实源仍是同一张 events 表，
    这里不做任何状态推断，只补充"谁对谁说"。
    """

    if not project_id:
        project_id = latest_project_id(conn)
    if not project_id:
        return {"project_id": None, "project_name": None, "agents": [], "events": []}
    row = _rows(conn, "SELECT id, name FROM projects WHERE id = ?", (project_id,))
    project_name = row[0].get("name") if row else None

    names = _agent_names(conn)
    requests = {r["id"]: r for r in _rows(
        conn,
        "SELECT id, requester_agent_id, provider_agent_id, question, status"
        " FROM information_requests WHERE project_id = ?", (project_id,))}
    tasks = {r["id"]: r for r in _rows(
        conn, "SELECT id, title, stage FROM tasks WHERE project_id = ?", (project_id,))}

    rows = _rows(
        conn,
        "SELECT sequence, event_type, actor, created_at, payload, object_type, object_id"
        " FROM events WHERE project_id = ? ORDER BY sequence DESC LIMIT ?",
        (project_id, int(limit) * 3),
    )
    events: list[dict[str, Any]] = []
    for row in reversed(rows):
        if row["event_type"] not in _GROUP_EVENTS:
            continue
        try:
            payload = json.loads(row.get("payload") or "{}")
        except (TypeError, ValueError):
            payload = {}
        rendered = _render({**row, "payload": payload})
        sender = row["actor"]
        recipient = None
        if row["event_type"] == "project.information_request.created":
            sender = str(payload.get("requester_agent_id") or sender)
            recipient = payload.get("provider_agent_id")
        elif row["event_type"] == "project.information_request.answered":
            # 回答事件的 actor 就是回答方，接收方是提问方
            req = requests.get(str(payload.get("request_id")))
            if req:
                sender = row["actor"]
                recipient = req["requester_agent_id"]
        elif row["event_type"] == "project.information_request.consumed":
            req = requests.get(str(payload.get("request_id")))
            if req:
                sender = str(req["requester_agent_id"])
                recipient = req["provider_agent_id"]
        events.append({
            "sequence": row["sequence"],
            "at": row["created_at"],
            "event_type": row["event_type"],
            "actor": row["actor"],
            "actor_name": names.get(str(row["actor"]), str(row["actor"])),
            "from": sender,
            "from_name": names.get(str(sender), str(sender)),
            "to": recipient,
            "to_name": names.get(str(recipient), str(recipient)) if recipient else None,
            "subject": (tasks.get(str(payload.get("task_id")))
                        or tasks.get(str(row.get("object_id") or "")) or {}).get("title"),
            "sender_kind": payload.get("sender_kind"),
            **rendered,
        })
        if len(events) >= int(limit):
            break

    # 只列这个项目的参与者——全局 Agent 列表会把别的项目的 Agent 也混进来
    participants = {r["agent_id"] for r in _rows(
        conn, "SELECT agent_id FROM agent_project_grants WHERE project_id = ?", (project_id,))}
    agents = [{"agent_id": aid, "display_name": names.get(aid, aid)}
              for aid in sorted(participants) if aid in names]
    return {"project_id": project_id, "project_name": project_name,
            "agents": agents, "events": events}


def overview(conn: sqlite3.Connection) -> dict[str, Any]:
    tasks = _rows(conn, "SELECT id, title, stage, status, assignee FROM tasks ORDER BY updated_at DESC LIMIT 50")
    artifacts = _rows(conn, "SELECT id, name, status, receipt_version, size_bytes, created_by,"
                            " output_hash FROM artifacts ORDER BY created_at DESC LIMIT 50")
    return {"tasks": tasks, "artifacts": artifacts, "db_path": str(db_path())}


# ---- HTTP -------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    server_version = "synapforge-viewer/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003 — 安静，别刷屏
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _agent_filter(self) -> list[str] | None:
        return getattr(self.server, "agent_filter", None)

    def do_GET(self) -> None:  # noqa: N802 — stdlib 约定
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            index = STATIC_DIR / "index.html"
            if not index.exists():
                self._send(500, b"viewer asset missing", "text/plain; charset=utf-8")
                return
            self._send(200, index.read_bytes(), "text/html; charset=utf-8")
            return
        if path == "/api/agents":
            conn = open_readonly_connection()
            try:
                self._json({"agents": list_agents(conn, self._agent_filter())})
            finally:
                conn.close()
            return
        if path.startswith("/api/agents/") and path.endswith("/stream"):
            agent_id = path[len("/api/agents/"):-len("/stream")]
            query = parse_qs(parsed.query)
            try:
                limit = int((query.get("limit") or [DEFAULT_STREAM_LIMIT])[0])
            except ValueError:
                limit = DEFAULT_STREAM_LIMIT
            self._json(agent_stream(open_readonly_connection(), agent_id, limit))
            return
        if path == "/api/group/stream":
            query = parse_qs(parsed.query)
            project_id = (query.get("project_id") or [None])[0]
            try:
                limit = int((query.get("limit") or [DEFAULT_STREAM_LIMIT])[0])
            except ValueError:
                limit = DEFAULT_STREAM_LIMIT
            self._json(group_stream(open_readonly_connection(), project_id, limit))
            return
        if path == "/api/overview":
            self._json(overview(open_readonly_connection()))
            return
        self._json({"error": "not_found", "path": path}, 404)

    def do_POST(self) -> None:  # noqa: N802 — stdlib 约定
        """唯一的写入口：人类在群聊里说一句话。

        护栏怎么守住的：
        - **读仍然只读**：渲染路径上的所有查询都走 `mode=ro` 连接；
        - **写只有一条实现**：这里调用的是 `commands.post_message`，和 CLI 完全同一个函数，
          不存在第二套写逻辑（护栏二）；
        - **不改变事实源**：消息落成 `project.message.posted` 事件，和 Agent 说的话进同一条时间线。
        """

        parsed = urlparse(self.path)
        if parsed.path != "/api/messages":
            self._json({"error": "not_found", "path": parsed.path}, 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            self._json({"error": "invalid_json"}, 400)
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            self._json({"error": "text_required"}, 400)
            return

        project_id = payload.get("project_id")
        if not project_id:
            conn = open_readonly_connection()
            try:
                project_id = latest_project_id(conn)
            finally:
                conn.close()
        if not project_id:
            self._json({"error": "project_not_found"}, 400)
            return

        from . import commands
        from .db import open_store

        store = open_store()
        try:
            result = commands.post_message(store, project_id=project_id,
                                           sender=str(payload.get("sender") or "你"),
                                           text=text, sender_kind="member")
        except Exception as error:  # noqa: BLE001 — 归一成可读错误，不把异常抛给浏览器
            self._json({"error": getattr(error, "code", type(error).__name__),
                        "detail": str(error)}, 400)
            return
        finally:
            store.close()
        self._json({"ok": True, **result})


def serve(*, port: int = 8765, open_browser: bool = True, label: str = "",
          agents: list[str] | None = None) -> None:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    # 只展示这些 Agent（None = 全部）。侧栏保持干净，不必删数据。
    httpd.agent_filter = [a for a in agents if a] if agents else None  # type: ignore[attr-defined]
    actual = httpd.server_address[1]
    url = f"http://127.0.0.1:{actual}/"
    print(f"synapforge 本地查看器（只读）：{url}")
    print(f"  状态库：{db_path()}")
    if label:
        print(f"  {label}")
    print("  关闭：Ctrl-C")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()


__all__ = ["agent_stream", "group_stream", "list_agents", "overview", "serve"]
