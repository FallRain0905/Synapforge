"""M-5c S-1 契约测试：常驻 `opencode serve` 通道（`opencode_server.py` + 对话循环的通道切换）。

覆盖四件事：
1. **协议事实**：模型串拆分、Basic 认证（用户名固定 `opencode`）、v1 路径与请求体形状——都按 S-0 真机样本写；
2. **生命周期**：起、等就绪、探活失败重启、起不来**如实降级**（不抛异常）；`popen` 注入假进程，不需要真装 opencode；
3. **一轮闭环**：SSE 的 `message.part.delta`/`part.updated`/`file.edited`/`permission.asked` 翻译成平台事件，
   正文与用量从 `POST /session/{id}/message` 的响应里取（serve 的 token 账映射成平台口径）；
4. **取消语义**：serve 模式下不是杀进程，而是 `POST /session/{id}/abort`；取消**不算成功**。

假 server 用 `http.server` 真起在回环端口上（带 Basic 校验），所以这是"真 HTTP"而不是打桩函数。
"""

from __future__ import annotations

import base64
import json
import queue
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = AGENT_ROOT.parent.parent
for candidate in (str(REPOSITORY_ROOT), str(AGENT_ROOT)):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from executor_events import ExecutorEventReporter  # noqa: E402
from opencode_server import (  # noqa: E402
    OpenCodeServer,
    OpenCodeServerConfig,
    ServerClient,
    run_serve_turn,
    split_model_ref,
)

PASSWORD = "test-password-123"


class FakeServeState:
    """假 serve 的服务端状态：记录收到的请求、推给 SSE 的事件、响应体。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, str]] = []
        self.permission_replies: list[tuple[str, str, dict]] = []
        self.aborts: list[str] = []
        self.events: queue.Queue[dict] = queue.Queue()
        self.event_connected = threading.Event()
        self.session_response: dict = {"id": "ses_fake_created"}
        self.message_response: dict = {
            "info": {
                "id": "msg_1",
                "sessionID": "ses_fake_created",
                "tokens": {"total": 7846, "input": 7793, "output": 37, "reasoning": 16, "cache": {"write": 0, "read": 2048}},
            },
            "parts": [{"type": "text", "text": "第一段正文"}, {"type": "step-finish", "tokens": {}}],
        }
        self.message_status = 200
        self.message_body_override: dict | None = None
        self.message_calls: list[dict] = []
        self.abort_seen = threading.Event()
        self.wait_for_abort = False


def make_handler(state: FakeServeState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args) -> None:  # 测试里不要访问日志
            return

        def _authorized(self) -> bool:
            header = self.headers.get("Authorization") or ""
            if not header.startswith("Basic "):
                return False
            decoded = base64.b64decode(header[6:]).decode("utf-8")
            return decoded == f"opencode:{PASSWORD}"

        def _read_body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            try:
                return json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                return {}

        def _send_json(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定名
            state.requests.append(("GET", self.path, {}, self.headers.get("Authorization") or ""))
            if not self._authorized():
                self._send_json(401, {"_tag": "UnauthorizedError"})
                return
            if self.path == "/api/health":
                self._send_json(200, {"healthy": True})
                return
            if self.path == "/event":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                state.event_connected.set()
                while True:
                    try:
                        event = state.events.get(timeout=5)
                    except queue.Empty:
                        break
                    if event is None:
                        break
                    chunk = f"data: {json.dumps(event)}\n\n".encode("utf-8")
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                return
            self._send_json(404, {"_tag": "NotFound"})

        def do_POST(self) -> None:  # noqa: N802
            body = self._read_body()
            state.requests.append(("POST", self.path, body, self.headers.get("Authorization") or ""))
            if not self._authorized():
                self._send_json(401, {"_tag": "UnauthorizedError"})
                return
            if self.path == "/session":
                self._send_json(200, state.session_response)
                return
            if self.path.endswith("/message"):
                state.message_calls.append(body)
                if state.message_status != 200:
                    self._send_json(state.message_status, {"_tag": "SessionNotFound"})
                    return
                while not state.events.empty():  # 上一轮残留清掉
                    state.events.get_nowait()
                    state.events.task_done()
                for event in state.pending_events if hasattr(state, "pending_events") else []:
                    state.events.put(event)
                if state.wait_for_abort and state.abort_seen.wait(timeout=5) is False:
                    state.events.put(None)
                state.events.put(None)
                self._send_json(200, state.message_body_override or state.message_response)
                return
            if "/permissions/" in self.path:
                state.permission_replies.append(("POST", self.path, body))
                self._send_json(200, True)
                return
            if self.path.endswith("/abort"):
                state.aborts.append(self.path)
                state.abort_seen.set()
                self._send_json(200, True)
                return
            self._send_json(404, {"_tag": "NotFound"})

    return Handler


class FakeServe:
    def __init__(self) -> None:
        self.state = FakeServeState()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class CannedReporter(ExecutorEventReporter):
    """把事件收在内存里，方便逐条断言。"""

    def __init__(self) -> None:
        super().__init__(emit=None, min_interval_seconds=0.0)
        self.events: list[tuple[str, dict]] = []

    def progress(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))

    def started(self, command, executor: str, protocol: str | None = None) -> None:  # noqa: ANN001
        self.events.append(("process.started", {"command": " ".join(command[:3])}))


class SplitModelRefTests(unittest.TestCase):
    def test_splits_provider_and_model(self) -> None:
        self.assertEqual(
            split_model_ref("deepseek/deepseek-v4.1-flash"),
            {"providerID": "deepseek", "modelID": "deepseek-v4.1-flash"},
        )

    def test_keeps_slashes_inside_the_model_id(self) -> None:
        self.assertEqual(split_model_ref("vendor/org/model"), {"providerID": "vendor", "modelID": "org/model"})

    def test_rejects_shapes_it_cannot_trust(self) -> None:
        for value in ["", None, "noshash", "/model", "provider/"]:
            self.assertIsNone(split_model_ref(value), value)


class ServerClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeServe()
        self.client = ServerClient(self.fake.base_url, PASSWORD, log=lambda _m: None)

    def tearDown(self) -> None:
        self.fake.stop()

    def test_health_uses_http_basic_with_fixed_user(self) -> None:
        """实测结论：用户名固定 `opencode`；Bearer 与自定义头都是 401。"""

        self.assertTrue(self.client.health())
        wrong = ServerClient(self.fake.base_url, "wrong-password", log=lambda _m: None)
        self.assertFalse(wrong.health())

    def test_create_session_sends_agent_but_never_model(self) -> None:
        """实测：v1 `POST /session` 带 `model` 会整条 400；模型只在发消息时带（见下一个用例）。"""

        session_id = self.client.create_session(
            model={"providerID": "deepseek", "modelID": "deepseek-v4.1-flash"},
            agent="mm-review",
        )
        self.assertEqual(session_id, "ses_fake_created")
        body = next(body for method, path, body, _auth in self.fake.state.requests if path == "/session")
        self.assertNotIn("model", body)
        self.assertEqual(body["agent"], "mm-review")

    def test_send_message_uses_parts_and_returns_response(self) -> None:
        code, body = self.client.send_message(
            "ses_x",
            prompt="你好",
            model={"providerID": "deepseek", "modelID": "deepseek-v4.1-flash"},
            agent="mm-topic",
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["info"]["tokens"]["total"], 7846)
        sent = self.fake.state.message_calls[-1]
        self.assertEqual(sent["parts"], [{"type": "text", "text": "你好"}])
        self.assertEqual(sent["agent"], "mm-topic")

    def test_permission_reply_and_abort_paths(self) -> None:
        self.assertTrue(self.client.reply_permission("ses_x", "per_1", "always"))
        self.assertTrue(self.client.abort("ses_x"))
        self.assertEqual(self.fake.state.permission_replies[-1][2], {"response": "always"})
        self.assertTrue(self.fake.state.aborts)


class ServerLifecycleTests(unittest.TestCase):
    class FakeProcess:
        def __init__(self, *, alive: bool = True) -> None:
            self.returncode: int | None = None if alive else 1
            self.terminated = False
            self.killed = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

    def make_server(self, *, healthy_after: int = 0, spawn_error: Exception | None = None):
        created: list[ServerLifecycleTests.FakeProcess] = []
        health_calls = {"count": 0}

        def popen(*_args, **_kwargs):
            if spawn_error is not None:
                raise spawn_error
            process = self.FakeProcess()
            created.append(process)
            return process

        client = ServerClient("http://127.0.0.1:1", PASSWORD, log=lambda _m: None)

        def health() -> bool:
            health_calls["count"] += 1
            return health_calls["count"] > healthy_after

        client.health = health  # type: ignore[method-assign]
        # 假时钟：每问一次时间就前进 0.01s——否则 `sleep` 被替成空操作后，"就绪超时"要靠真时间等 5 秒
        clock_state = {"now": 0.0}

        def clock() -> float:
            clock_state["now"] += 0.01
            return clock_state["now"]

        server = OpenCodeServer(
            OpenCodeServerConfig(workspace=Path("."), port=4199, startup_timeout_seconds=5.0, health_interval_seconds=0.01),
            log=lambda _m: None,
            popen=popen,
            clock=clock,
            sleep=lambda _s: None,
            client=client,
        )
        return server, created, health_calls

    def test_spawns_and_waits_until_healthy(self) -> None:
        server, created, health_calls = self.make_server(healthy_after=2)
        self.assertTrue(server.ensure_running())
        self.assertEqual(len(created), 1)
        self.assertGreater(health_calls["count"], 2)
        self.assertTrue(server.snapshot()["chat_server_alive"])

    def test_returns_false_when_spawn_fails(self) -> None:
        server, _created, _health = self.make_server(spawn_error=OSError("no such binary"))
        self.assertFalse(server.ensure_running())
        self.assertIn("启动失败", server.snapshot()["chat_server_error"])

    def test_times_out_and_stops_the_process(self) -> None:
        server, created, _health = self.make_server(healthy_after=10_000)
        self.assertFalse(server.ensure_running())
        self.assertIn("超时", server.snapshot()["chat_server_error"])
        self.assertTrue(created[0].terminated)

    def test_restarts_when_alive_but_unhealthy(self) -> None:
        server, created, health_calls = self.make_server()
        self.assertTrue(server.ensure_running())
        health_calls["count"] = -10_000  # 之后反复探活都失败
        self.assertFalse(server.ensure_running())
        self.assertTrue(created[0].terminated)
        self.assertEqual(server.snapshot()["chat_server_restarts"], 0)
        health_calls["count"] = 10_000
        self.assertTrue(server.restart())
        self.assertEqual(server.snapshot()["chat_server_restarts"], 1)
        # 三次拉起：① 首次成功 ② 探活失败后的重启（这次没就绪、被收掉）③ restart() 真正拉起的那个
        self.assertEqual(len(created), 3)
        self.assertTrue(server.snapshot()["chat_server_alive"])


class FakeApprovals:
    """假审批通道（S-3）：按脚本给决定；不给就模拟"没人批"（wait 返回 None）。"""

    def __init__(self, decision: str | None = None) -> None:
        self.decision = decision
        self.reported: list[dict] = []
        self.expired: list[str] = []
        self.waited: list[tuple[str, float]] = []

    def report(self, payload: dict) -> None:
        self.reported.append(payload)

    def wait(self, request_id: str, *, timeout: float, cancel_event=None) -> str | None:  # noqa: ANN001
        self.waited.append((request_id, timeout))
        return self.decision

    def expire(self, request_id: str) -> None:
        self.expired.append(request_id)


class ServeTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeServe()
        self.client = ServerClient(self.fake.base_url, PASSWORD, log=lambda _m: None)
        self.reporter = CannedReporter()
        self.deltas: list[dict] = []

    def tearDown(self) -> None:
        self.fake.stop()

    def run_turn(self, *, deltas: bool = True, **kwargs):
        state = self.fake.state
        state.pending_events = [
            # 真实顺序：先 `part.updated` 声明这一段的类型（`text` / `reasoning`），再来它的增量。
            # **实测教训**：思考与正文的增量都是 `field=text`，只看 field 会把思考混进气泡。
            {"type": "message.part.updated", "properties": {"part": {"id": "prt_reason", "type": "reasoning", "text": ""}}},
            {"type": "message.part.delta", "properties": {"partID": "prt_reason", "field": "text", "delta": "The user wants a"}},
            {"type": "message.part.updated", "properties": {"part": {"id": "prt_1", "type": "text", "text": ""}}},
            {"type": "message.part.delta", "properties": {"partID": "prt_1", "field": "text", "delta": "第一"}},
            {"type": "message.part.delta", "properties": {"partID": "prt_1", "field": "text", "delta": "段正文"}},
            {"type": "message.part.delta", "properties": {"partID": "prt_1", "field": "reasoning", "delta": "想一下"}},
            {"type": "message.part.updated", "properties": {"part": {"type": "tool", "tool": "bash", "state": {"status": "completed"}}}},
            {"type": "message.part.updated", "properties": {"part": {"type": "step-finish", "tokens": {"total": 10}}}},
            {"type": "file.edited", "properties": {"file": "/tmp/x.txt"}},
            {
                "type": "permission.asked",
                "properties": {"id": "per_1", "sessionID": "ses_fake_created", "permission": "external_directory", "patterns": ["/tmp/*"]},
            },
            {"type": "session.idle", "properties": {"sessionID": "ses_fake_created"}},
        ]
        if deltas:
            kwargs.setdefault("emit_delta", self.deltas.append)
        return run_serve_turn(
            self.client,
            prompt="你好",
            reporter=self.reporter,
            timeout=15.0,
            cancel_poll_seconds=0.05,
            log=kwargs.pop("log", lambda _m: None),
            **kwargs,
        )

    def test_reasoning_deltas_never_leak_into_the_answer(self) -> None:
        """S-0/S-1 都漏掉的一条：思考与正文的增量**长得一模一样**（`field=text`），
        只能靠 `part.updated` 声明的 `part.id → type` 区分；思考必须被丢掉。"""

        outcome = self.run_turn()
        self.assertTrue(outcome.ok, outcome.error)
        streamed = "".join(item["text"] for item in self.deltas)
        self.assertEqual(streamed, "第一段正文")
        self.assertNotIn("The user wants a", streamed)
        # 兜底口径：没丢成正文的未知类型增量要能看见（正常为 0）
        self.assertEqual(self.reporter.events[-1][1]["unknown_deltas"], 0)

    def test_turn_translates_events_and_maps_usage(self) -> None:
        outcome = self.run_turn(model="deepseek/deepseek-v4.1-flash", agent="mm-review")

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(outcome.text, "第一段正文")
        self.assertEqual(outcome.session_key, "ses_fake_created")
        usage = outcome.usage
        self.assertEqual(usage["input_tokens"], 7793)
        self.assertEqual(usage["output_tokens"], 37)
        self.assertEqual(usage["total_tokens"], 7846)
        self.assertEqual(usage["turns"], 1)
        self.assertEqual(usage["source"], "opencode-serve")
        self.assertEqual(usage["reasoning_tokens"], 16)
        self.assertEqual(usage["cache_read_tokens"], 2048)

        kinds = [kind for kind, _payload in self.reporter.events]
        self.assertIn("process.started", kinds)
        self.assertIn("tool.completed", kinds)
        self.assertIn("file.changed", kinds)
        self.assertEqual(kinds[-1], "process.exited")
        self.assertIn("delta_events", self.reporter.events[-1][1])
        # 思考增量不进正文（field=reasoning 被忽略）；正文按 delta 拼起来正好是完整一段
        self.assertEqual("".join(item["text"] for item in self.deltas), "第一段正文")
        # 常驻通道的正文只走 delta：不再重复发整段 agent.message
        self.assertEqual([kind for kind, _p in self.reporter.events if kind == "agent.message"], [])
        self.assertEqual(self.fake.state.permission_replies[-1][2], {"response": "always"})

    def test_without_delta_channel_text_goes_as_whole_segments(self) -> None:
        """没配 delta 通道时退回 S-1 的老口径：每段一条 `agent.message`（模块可独立使用）。"""

        outcome = self.run_turn(deltas=False)
        self.assertTrue(outcome.ok, outcome.error)
        messages = [payload["text"] for kind, payload in self.reporter.events if kind == "agent.message"]
        self.assertEqual(messages, ["第一段正文"])

    def test_delta_throttle_never_loses_text(self) -> None:
        """增量事件有节奏上限，但**一个字都不能丢**：封顶之后的正文在收尾那一条里补发。"""

        self.fake.state.pending_events = [
            {"type": "message.part.updated", "properties": {"part": {"id": "prt_1", "type": "text", "text": ""}}},
            *[
                {"type": "message.part.delta", "properties": {"partID": "prt_1", "field": "text", "delta": f"{index:02d}"}}
                for index in range(12)
            ],
            {"type": "session.idle", "properties": {"sessionID": "ses_fake_created"}},
        ]
        outcome = run_serve_turn(
            self.client,
            prompt="你好",
            reporter=self.reporter,
            log=lambda _m: None,
            emit_delta=self.deltas.append,
            delta_interval_seconds=0.0,  # 每条都够格发：这里考验的是**上限**而不是节流
            max_delta_events=5,
            session_key="ses_fake_created",
            timeout=15.0,
        )
        self.assertTrue(outcome.ok, outcome.error)
        self.assertLessEqual(len(self.deltas), 6)  # 5 条封顶 + 收尾补发的那一条
        self.assertEqual("".join(item["text"] for item in self.deltas), "".join(f"{index:02d}" for index in range(12)))

    def test_message_body_carries_model_and_agent(self) -> None:
        self.run_turn(model="deepseek/deepseek-v4.1-flash", agent="mm-topic")
        sent = self.fake.state.message_calls[-1]
        self.assertEqual(sent["model"], {"providerID": "deepseek", "modelID": "deepseek-v4.1-flash"})
        self.assertEqual(sent["agent"], "mm-topic")

    def test_missing_session_is_recreated_once(self) -> None:
        """平台带来的会话句柄在 server 上不存在（上一轮可能走的是 CLI 通道）→ 新建一个再来一次。"""

        self.fake.state.message_status = 404
        outcome = self.run_turn(session_key="ses_from_cli_channel")
        self.assertFalse(outcome.ok)
        # 第一次用旧句柄、第二次用新建的句柄：共两次消息调用 + 一次建会话
        self.assertEqual(len(self.fake.state.message_calls), 2)
        self.assertTrue(any(path == "/session" for _m, path, _b, _a in self.fake.state.requests))

    def test_provider_error_is_reported_not_hidden(self) -> None:
        self.fake.state.message_body_override = {
            "info": {"id": "msg_2", "error": {"message": "Provider request failed with HTTP 401"}},
            "parts": [],
        }
        outcome = self.run_turn()
        self.assertFalse(outcome.ok)
        self.assertIn("401", outcome.error)

    def test_cancel_aborts_the_session_instead_of_killing_a_process(self) -> None:
        """S-0 结论：serve 模式的取消是 `POST /session/{id}/abort`，不是杀进程。"""

        self.fake.state.wait_for_abort = True
        outcome = self.run_turn(session_key="ses_fake_created", cancel_check=lambda: True)
        self.assertTrue(outcome.cancelled)
        self.assertFalse(outcome.ok)
        self.assertTrue(self.fake.state.aborts)
        self.assertTrue(self.fake.state.aborts[0].endswith("/abort"))


    def test_permission_card_is_reported_and_member_decision_wins(self) -> None:
            """S-3：权限请求变成平台卡片，回复按人的决定走（真机实测 `reject` 之后确实没执行）。"""

            approvals = FakeApprovals(decision="reject")
            outcome = self.run_turn(approvals=approvals, approval_timeout_seconds=30.0)

            self.assertTrue(outcome.ok, outcome.error)
            self.assertEqual(len(approvals.reported), 1)
            card = approvals.reported[0]
            self.assertEqual(card["permission"], "external_directory")
            self.assertEqual(card["patterns"], ["/tmp/*"])
            self.assertIn("/tmp/*", card["summary"])
            # 回复给 opencode 的就是人的决定
            self.assertEqual(self.fake.state.permission_replies[-1][2], {"response": "reject"})
            kinds = [kind for kind, _payload in self.reporter.events]
            self.assertIn("approval.requested", kinds)
            self.assertIn("approval.decided", kinds)
            decided = next(payload for kind, payload in self.reporter.events if kind == "approval.decided")
            self.assertEqual(decided["decision"], "reject")
            self.assertEqual(decided["by"], "member")

    def test_permission_timeout_means_nobody_approved_so_it_is_rejected(self) -> None:
        """没人批 = 不执行：等不到决定就 reject 并标过期（有界等待，不把轮次挂死）。"""

        approvals = FakeApprovals(decision=None)
        outcome = self.run_turn(approvals=approvals, approval_timeout_seconds=5.0)

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(approvals.expired, ["per_1"])
        self.assertEqual(self.fake.state.permission_replies[-1][2], {"response": "reject"})
        decided = next(payload for kind, payload in self.reporter.events if kind == "approval.decided")
        self.assertEqual(decided["by"], "timeout")

    def test_approval_report_failure_falls_back_without_hanging(self) -> None:
        """上报失败不能让这一轮卡住：退回自动策略（这里是放行）并如实记一条日志。"""

        class BrokenApprovals(FakeApprovals):
            def report(self, payload: dict) -> None:
                raise RuntimeError("platform_unreachable")

        logs: list[str] = []
        outcome = self.run_turn(approvals=BrokenApprovals(), log=logs.append)

        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(self.fake.state.permission_replies[-1][2], {"response": "always"})
        self.assertTrue(any("上报失败" in line for line in logs))

    def test_permission_without_channel_rejects_when_auto_is_off(self) -> None:
        """没接审批通道且关掉 `--auto` 等价语义时，宁可拒绝也不擅自放行（绝不挂着不回）。"""

        outcome = self.run_turn(deltas=False, approvals=None, auto_approve_permissions=False)
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(self.fake.state.permission_replies[-1][2], {"response": "reject"})


if __name__ == "__main__":
    unittest.main()