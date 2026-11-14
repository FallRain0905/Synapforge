"""MCP 薄壳测试。

要点：
- 工具数受上下文预算约束（这是稀缺资源，不是"越多越好"）；
- 每个列出来的工具都必须真的能被分发（列了却调不通 = 骗消费者）；
- 工具层错误是**结果**（`isError: true` + 稳定 code），不是协议错误——Agent 要按 code 分支；
- 协议错误（未知方法）才是 JSON-RPC error；
- stdio 传输用真子进程验一遍：stdout 只能有协议消息。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from synapforge import commands
from synapforge.db import open_store
from synapforge.mcp import TOOLS, McpServer, McpToolError, handle_message

TOOL_BUDGET = 17  # 15 → 16 加 synapforge_say；16 → 17 加 synapforge_contract（契约是核心能力）。
MCP_ENTRY = Path(__file__).resolve().parents[1] / ".." / ".." / "scripts" / "synapforge_mcp.py"


class ToolSurfaceTests(unittest.TestCase):
    def test_tool_count_within_context_budget(self) -> None:
        """工具 schema 会进模型上下文——300 条路由不能变成 300 个工具。"""

        self.assertLessEqual(len(TOOLS), TOOL_BUDGET,
                             f"工具数 {len(TOOLS)} 超过预算 {TOOL_BUDGET}：先收敛动词，不要加工具")

    def test_tool_names_are_unique_and_prefixed(self) -> None:
        names = [tool["name"] for tool in TOOLS]
        self.assertEqual(len(names), len(set(names)))
        for name in names:
            self.assertTrue(name.startswith("synapforge_"), name)

    def test_every_listed_tool_has_a_handler(self) -> None:
        server = McpServer()
        for tool in TOOLS:
            handler = getattr(server, f"_tool_{tool['name'].removeprefix('synapforge_')}", None)
            self.assertIsNotNone(handler, f"{tool['name']} 列在工具表里却没有处理器")

    def test_every_tool_declares_a_schema(self) -> None:
        for tool in TOOLS:
            self.assertIn("description", tool, tool["name"])
            schema = tool.get("inputSchema") or {}
            self.assertEqual(schema.get("type"), "object", tool["name"])


class JsonRpcTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-mcp-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "state.db"
        store = open_store(self.db)
        self.pid = commands.ensure_project(store, "mcp-proj")["id"]
        store.close()
        self.server = McpServer(agent_id="agent-mcp", project_id=self.pid, store_path=self.db)
        self.addCleanup(self.server.close)

    def _call(self, method: str, params: dict | None = None, request_id: int = 1) -> dict:
        return handle_message(self.server, {"jsonrpc": "2.0", "id": request_id,
                                            "method": method, "params": params or {}})

    def _tool(self, name: str, arguments: dict | None = None) -> dict:
        response = self._call("tools/call", {"name": name, "arguments": arguments or {}})
        result = response["result"]
        return {"is_error": result["isError"],
                "payload": json.loads(result["content"][0]["text"])}

    def test_initialize_echoes_protocol_version(self) -> None:
        response = self._call("initialize", {"protocolVersion": "2025-03-26"})
        result = response["result"]
        self.assertEqual(result["protocolVersion"], "2025-03-26")
        self.assertIn("tools", result["capabilities"])
        self.assertEqual(result["serverInfo"]["name"], "synapforge")

    def test_notifications_get_no_reply(self) -> None:
        self.assertIsNone(handle_message(self.server, {"jsonrpc": "2.0",
                                                       "method": "notifications/initialized"}))

    def test_tools_list_returns_all_tools(self) -> None:
        self.assertEqual(len(self._call("tools/list")["result"]["tools"]), len(TOOLS))

    def test_unknown_method_is_protocol_error(self) -> None:
        response = self._call("does/not/exist")
        self.assertIn("error", response)
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_tool_is_tool_error_result(self) -> None:
        outcome = self._tool("synapforge_nonexistent")
        self.assertTrue(outcome["is_error"])
        self.assertEqual(outcome["payload"]["code"], "unknown_tool")

    def test_missing_argument_is_tool_error_with_code(self) -> None:
        outcome = self._tool("synapforge_task_create", {})
        self.assertTrue(outcome["is_error"])
        self.assertEqual(outcome["payload"]["code"], "missing_argument")

    def test_status_tool_returns_project_view(self) -> None:
        outcome = self._tool("synapforge_status", {"event_limit": 5})
        self.assertFalse(outcome["is_error"])
        self.assertEqual(outcome["payload"]["project_id"], self.pid)

    def test_full_loop_through_mcp_tools(self) -> None:
        """A 问 → B 答 → A 消费：全程只经 MCP 工具，不经 CLI。"""

        worker = McpServer(agent_id="agent-worker", project_id=self.pid, store_path=self.db)
        self.addCleanup(worker.close)
        worker.store  # 触发默认参与者登记

        task = self._tool("synapforge_task_create", {"title": "MCP 闭环验证"})["payload"]

        claimed = self._tool("synapforge_claim")["payload"]
        self.assertTrue(claimed.get("claimed"), f"claim 必须真的领到任务：{claimed}")
        lease_token = claimed["lease"]["lease_token"]
        started = self._tool("synapforge_start", {"task_id": task["id"],
                                                 "lease_token": lease_token})["payload"]
        self.assertEqual(started["status"], "RUNNING")

        asked = self._tool("synapforge_ask", {
            "provider_agent_id": "agent-worker", "question": "哈希是多少？",
            "reason": "没有它无法收尾", "task_id": task["id"]})["payload"]
        self.assertEqual(asked["status"], "OPEN")
        # required 阻塞必须真的把节点移进 WAITING（不是只记一句备注）
        self.assertEqual(asked["node_effect_result"]["effect"], "WAITING")
        waiting = self.server.store.db.execute(
            "SELECT status FROM tasks WHERE id = ?", (str(task["id"]),)).fetchone()
        self.assertEqual(str(waiting["status"]), "WAITING")

        answered = worker.call("synapforge_answer", {
            "request_id": asked["request_id"], "summary": "abc123"})
        self.assertEqual(answered["status"], "ANSWERED")

        consumed = self._tool("synapforge_consume", {"request_id": asked["request_id"]})["payload"]
        self.assertEqual(consumed["status"], "CONSUMER_ACKNOWLEDGED")
        self.assertTrue(consumed["node_effect_result"]["task_resumed"])

        row = self.server.store.db.execute(
            "SELECT status FROM tasks WHERE id = ?", (str(task["id"]),)).fetchone()
        self.assertEqual(str(row["status"]), "RUNNING")

    def test_concern_requires_evidence(self) -> None:
        outcome = self._tool("synapforge_concern",
                             {"concern_type": "missing_input", "claim": "x"})
        self.assertTrue(outcome["is_error"])
        self.assertEqual(outcome["payload"]["code"], "missing_argument")

    def test_artifact_publish_and_read_via_mcp(self) -> None:
        workspace = self.tmp / "ws"
        workspace.mkdir()
        (workspace / "out.md").write_bytes("内容".encode("utf-8"))

        published = self._tool("synapforge_artifact", {
            "action": "publish", "workspace": str(workspace),
            "relative_path": "out.md"})["payload"]
        self.assertEqual(published["name"], "out.md")

        read_back = self._tool("synapforge_artifact", {
            "action": "read", "artifact_id": published["id"]})["payload"]
        self.assertEqual(read_back["text"], "内容")

        listed = self._tool("synapforge_artifact", {"action": "list"})["payload"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["receipt_version"], 1)


class StdioTransportTests(unittest.TestCase):
    """真子进程：stdout 必须只有协议消息（日志混进去就会污染协议流）。"""

    def test_stdio_roundtrip(self) -> None:
        entry = Path(__file__).resolve().parents[3] / "scripts" / "synapforge_mcp.py"
        tmp = Path(tempfile.mkdtemp(prefix="synapforge-mcp-stdio-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        db = tmp / "state.db"

        store = open_store(db)
        pid = commands.ensure_project(store, "stdio-proj")["id"]
        store.close()

        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "synapforge_status", "arguments": {"project_id": pid}}},
        ]
        stdin = "".join(json.dumps(message) + "\n" for message in messages)

        process = subprocess.run(
            [sys.executable, str(entry), "--agent", "agent-stdio",
             "--project", pid, "--state", str(db)],
            input=stdin, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(process.returncode, 0, process.stderr[-800:])

        lines = [line for line in process.stdout.splitlines() if line.strip()]
        # 4 条消息，其中 initialized 是通知不回包 → 3 条响应
        self.assertEqual(len(lines), 3, f"stdout 行数不对：{lines}")
        replies = [json.loads(line) for line in lines]
        for reply in replies:
            self.assertEqual(reply["jsonrpc"], "2.0")
            self.assertIn("id", reply)
        self.assertEqual(replies[0]["id"], 1)
        self.assertEqual(len(replies[1]["result"]["tools"]), len(TOOLS))
        status = json.loads(replies[2]["result"]["content"][0]["text"])
        self.assertEqual(status["project_id"], pid)

    def test_parse_error_does_not_kill_the_server(self) -> None:
        entry = Path(__file__).resolve().parents[3] / "scripts" / "synapforge_mcp.py"
        tmp = Path(tempfile.mkdtemp(prefix="synapforge-mcp-bad-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        stdin = "这不是 JSON\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n"
        process = subprocess.run(
            [sys.executable, str(entry), "--state", str(tmp / "state.db")],
            input=stdin, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(process.returncode, 0)
        lines = [json.loads(line) for line in process.stdout.splitlines() if line.strip()]
        self.assertEqual(lines[0]["error"]["code"], -32700)
        self.assertEqual(lines[1]["id"], 9, "坏消息之后服务必须继续处理后续消息")


if __name__ == "__main__":
    unittest.main()
