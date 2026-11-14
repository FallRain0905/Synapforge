"""Stage 1 协议闭环测试。

分两层：
- **协议层零网络**：把 `socket.socket` 换成会抛异常的桩，整条闭环照样跑通。
- **执行体用 ScriptedLlm**：协议断言必须是确定性的，不能依赖真实模型输出。
"""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import unittest
from pathlib import Path

from synapforge import commands
from synapforge.db import journal_mode, open_store
from synapforge.llm import ScriptedLlm
from synapforge.runtime import LocalAgent, parse_json_reply


class LocalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = open_store(self.tmp / "state.db")

    def test_wal_enabled(self) -> None:
        """多进程并发写的前提：WAL 不是 delete。"""

        self.assertEqual(journal_mode(self.store).lower(), "wal")

    def test_state_lives_in_one_file(self) -> None:
        tables = {r["name"] for r in self.store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for required in ("stage_reports", "information_requests", "feasibility_concerns",
                         "orchestration_decisions", "events", "tasks", "agents"):
            self.assertIn(required, tables)


class ProtocolLoopTests(unittest.TestCase):
    """F2 回归：required 阻塞必须真的把节点移进 WAITING，消费回复必须真的移回 RUNNING。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = open_store(self.tmp / "state.db")
        self.project = commands.ensure_project(self.store, "stage1-loop")
        self.pid = self.project["id"]
        self.analyst, self.worker = "agent-analyst", "agent-worker"
        for agent_id in (self.analyst, self.worker):
            commands.ensure_participant(self.store, project_id=self.pid, agent_id=agent_id)
        self.task = commands.create_task(self.store, project_id=self.pid,
                                         title="闭环验证任务", stage="analysis")

    def _status(self) -> str:
        row = self.store.db.execute("SELECT status FROM tasks WHERE id = ?",
                                    (str(self.task["id"]),)).fetchone()
        return str(row["status"])

    def test_required_request_moves_node_to_waiting_then_back_to_running(self) -> None:
        lease = commands.claim(self.store, project_id=self.pid, agent_id=self.analyst)
        self.assertTrue(lease["claimed"])
        token = lease["lease"]["lease_token"]
        commands.mark_running(self.store, task_id=self.task["id"], agent_id=self.analyst,
                              lease_token=token)
        self.assertEqual(self._status(), "RUNNING")

        asked = commands.ask(self.store, project_id=self.pid, agent_id=self.analyst,
                             provider_agent_id=self.worker, request_type="fact",
                             question="上游摘要的 sha256 是多少？",
                             reason="没有它无法安全写入交付文件", blocking="required",
                             task_id=self.task["id"])
        self.assertEqual(asked["status"], "OPEN")
        self.assertEqual(asked["node_effect_result"]["effect"], "WAITING")
        self.assertEqual(self._status(), "WAITING", "required 阻塞必须真的落到任务状态")

        answered = commands.answer(self.store, request_id=asked["request_id"], agent_id=self.worker,
                                   response={"status": "answered", "answer_summary": "sha256=abc123",
                                             "facts": [{"key": "sha256", "value": "abc123"}],
                                             "artifact_refs": [], "evidence_refs": ["receipt:x"],
                                             "assumptions": []})
        self.assertEqual(answered["status"], "ANSWERED")

        consumed = commands.consume(self.store, request_id=asked["request_id"], agent_id=self.analyst)
        self.assertEqual(consumed["status"], "CONSUMER_ACKNOWLEDGED")
        self.assertTrue(consumed["node_effect_result"]["task_resumed"])
        self.assertEqual(self._status(), "RUNNING", "消费回复必须真的把节点移回 RUNNING")

    def test_request_from_unstarted_task_reports_honest_reason(self) -> None:
        """任务还没 claim（READY）时不该假装进了 WAITING——如实说明源状态不合法。"""

        asked = commands.ask(self.store, project_id=self.pid, agent_id=self.analyst,
                             provider_agent_id=self.worker, request_type="fact",
                             question="q", reason="r", blocking="required",
                             task_id=self.task["id"])
        self.assertEqual(asked["node_effect_result"]["effect"], "NONE")
        self.assertIn("READY", asked["node_effect_result"]["reason"])
        self.assertEqual(self._status(), "READY")

    def test_idempotent_ask_does_not_duplicate(self) -> None:
        first = commands.ask(self.store, project_id=self.pid, agent_id=self.analyst,
                             provider_agent_id=self.worker, request_type="fact", question="q",
                             reason="r", blocking="optional", idempotency_key="dup-key-0001")
        again = commands.ask(self.store, project_id=self.pid, agent_id=self.analyst,
                             provider_agent_id=self.worker, request_type="fact", question="q",
                             reason="r", blocking="optional", idempotency_key="dup-key-0001")
        self.assertEqual(first["request_id"], again["request_id"])
        self.assertTrue(again.get("duplicate"))
        count = self.store.db.execute("SELECT COUNT(*) AS c FROM information_requests").fetchone()["c"]
        self.assertEqual(count, 1)

    def test_illegal_transition_is_fail_closed(self) -> None:
        from app import coordination

        with self.assertRaises(coordination.IllegalTransition):
            coordination.check_transition("information_request", "CONSUMER_ACKNOWLEDGED", "OPEN")


class HonestStopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = open_store(self.tmp / "state.db")
        self.pid = commands.ensure_project(self.store, "stage1-stop")["id"]
        self.worker = "agent-worker"
        commands.ensure_participant(self.store, project_id=self.pid, agent_id=self.worker)
        self.task = commands.create_task(self.store, project_id=self.pid, title="诚实停止验证")

    def test_concern_blocks_task_and_orchestrator_decides(self) -> None:
        concern = commands.raise_concern(
            self.store, project_id=self.pid, agent_id=self.worker,
            concern_type="missing_input",
            claim="上游成果物没有内容哈希，无法验证回执",
            basis=["event:project.task.created", "artifact:artifact-x"],
            task_id=self.task["id"])
        self.assertEqual(concern["status"], "NEEDS_DECISION")
        self.assertEqual(concern["node_effect"], "BLOCKED")
        row = self.store.db.execute("SELECT status, blocked_reason FROM tasks WHERE id = ?",
                                    (str(self.task["id"]),)).fetchone()
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIn("feasibility_concern", row["blocked_reason"])

        decided = commands.decide_concern(
            self.store, concern_id=concern["concern_id"], policy="request_information",
            rationale="先向上游索取哈希再放行",
            basis=["concern:" + str(concern["concern_id"])],
            expected_events=["project.feasibility_concern.decided"])
        self.assertEqual(decided["status"], "DECIDED")

    def test_evidence_is_mandatory(self) -> None:
        with self.assertRaises(Exception) as ctx:
            commands.raise_concern(self.store, project_id=self.pid, agent_id=self.worker,
                                   concern_type="missing_input", claim="x", basis=[],
                                   task_id=self.task["id"])
        self.assertIn("basis", str(ctx.exception))


class NoNetworkTests(unittest.TestCase):
    """协议层不许碰网络：整条闭环在 socket 被禁用的前提下必须照样跑通。"""

    def test_full_protocol_loop_without_sockets(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="synapforge-test-"))
        self.addCleanup(shutil.rmtree, tmp, True)

        real_socket = socket.socket

        def _forbidden(*args, **kwargs):  # pragma: no cover — 只会在违规时被调用
            raise AssertionError("protocol layer attempted network access")

        socket.socket = _forbidden
        try:
            store = open_store(tmp / "state.db")
            pid = commands.ensure_project(store, "no-net")["id"]
            for agent_id in ("agent-a", "agent-b"):
                commands.ensure_participant(store, project_id=pid, agent_id=agent_id)
            task = commands.create_task(store, project_id=pid, title="无网验证")
            lease = commands.claim(store, project_id=pid, agent_id="agent-a")
            token = lease["lease"]["lease_token"]
            commands.mark_running(store, task_id=task["id"], agent_id="agent-a", lease_token=token)
            asked = commands.ask(store, project_id=pid, agent_id="agent-a", provider_agent_id="agent-b",
                                 request_type="fact", question="q", reason="r",
                                 task_id=task["id"])
            commands.answer(store, request_id=asked["request_id"], agent_id="agent-b",
                            response={"status": "answered", "answer_summary": "yes", "facts": [],
                                      "artifact_refs": [], "evidence_refs": [], "assumptions": []})
            commands.consume(store, request_id=asked["request_id"], agent_id="agent-a")
            commands.submit_report(store, project_id=pid, task_id=task["id"], agent_id="agent-a",
                                   report={"status": "completed", "summary": "ok",
                                           "completed_items": ["x"], "incomplete_items": [],
                                           "output_artifacts": [], "evidence_refs": [], "blockers": [],
                                           "tests": {"passed": 1, "failed": 0, "skipped": 0},
                                           "can_continue_safely": True})
            row = store.db.execute("SELECT status FROM tasks WHERE id = ?",
                                   (str(task["id"]),)).fetchone()
            self.assertEqual(str(row["status"]), "RUNNING",
                             "消费回复后节点必须回到 RUNNING（WAITING 是等待，不是终态）")
            store.close()
        finally:
            socket.socket = real_socket


class RuntimeLoopTests(unittest.TestCase):
    """两个 Agent 真跑一遍：A 问 → B 答 → A 续租接着做 → 收尾。"""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="synapforge-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = self.tmp / "state.db"
        store = open_store(self.db)
        self.pid = commands.ensure_project(store, "stage1-runtime")["id"]
        self.task = commands.create_task(store, project_id=self.pid,
                                         title="双 Agent 交接验证", description="需要上游提供哈希")
        store.close()

    def _agent(self, agent_id: str, replies: list[str], peer: str) -> LocalAgent:
        return LocalAgent(agent_id, self.pid, role=f"role-{agent_id}",
                          llm=ScriptedLlm(replies), peer_ids=[peer], store_path=self.db)

    def test_two_agents_close_the_loop(self) -> None:
        analyst = self._agent("agent-analyst", [
            json.dumps({"action": "ask", "to": "agent-worker", "request_type": "fact",
                        "question": "sha256?", "reason": "no hash to verify"},
                       ensure_ascii=False),
            json.dumps({"action": "finish", "summary": "done with hash"},
                       ensure_ascii=False),
        ], "agent-worker")
        worker = self._agent("agent-worker", [
            json.dumps({"status": "answered", "summary": "sha256 is abc123",
                        "facts": [{"key": "sha256", "value": "abc123"}],
                        "evidence_refs": ["receipt:x"]}, ensure_ascii=False),
        ], "agent-analyst")
        self.addCleanup(analyst.close)
        self.addCleanup(worker.close)

        statuses: list[str] = []

        def snapshot() -> str:
            row = analyst.store.db.execute("SELECT status FROM tasks WHERE id = ?",
                                           (str(self.task["id"]),)).fetchone()
            value = str(row["status"])
            statuses.append(value)
            return value

        # 轮一：A 领活、进 RUNNING、发现缺信息 → ask → 任务进 WAITING
        record = analyst.run_once()
        self.assertEqual(record["work"]["action"], "ask")
        self.assertEqual(record["work"]["node_effect_result"]["effect"], "WAITING")
        self.assertEqual(snapshot(), "WAITING")
        self.assertIsNotNone(analyst.inflight, "问完之后任务应保持在飞")

        # 轮二：B 取到请求并回答
        worker_record = worker.run_once()
        self.assertEqual(len(worker_record["answered"]), 1)
        self.assertEqual(worker_record["answered"][0]["status"], "ANSWERED")

        # 轮三：A 消费回复（WAITING→RUNNING）→ 续租 → 接着做 → 收尾
        resumed = analyst.run_once()
        self.assertEqual(len(resumed["consumed"]), 1)
        # task_resumed 只在 WAITING→RUNNING 真的被施加时置真，是恢复的可观测证据
        self.assertTrue(resumed["consumed"][0]["node_effect_result"]["task_resumed"])
        self.assertEqual(resumed["work"]["action"], "finish")
        self.assertIsNone(analyst.inflight, "收尾后不应还挂着在飞任务")

        final = snapshot()
        self.assertNotEqual(final, "RUNNING")
        # 状态轨迹：RUNNING →（ask 前置）WAITING →（consume 前置）RUNNING → 终态
        self.assertIn("WAITING", statuses)

        events = [e["event_type"] for e in commands.recent_events(analyst.store, limit=50)]
        for expected in ("project.information_request.created",
                         "project.information_request.answered",
                         "project.information_request.consumed"):
            self.assertIn(expected, events)

    def test_agent_reports_honest_stop_instead_of_faking_success(self) -> None:
        worker = self._agent("agent-solo", [
            json.dumps({"action": "concern", "concern_type": "missing_input",
                        "claim": "没有上游输入，无法完成", "basis": ["event:project.task.created"]},
                       ensure_ascii=False),
        ], "agent-peer")
        self.addCleanup(worker.close)
        record = worker.run_once()
        self.assertEqual(record["work"]["action"], "concern")
        self.assertEqual(record["work"]["node_effect"], "BLOCKED")
        row = worker.store.db.execute("SELECT status, blocked_reason FROM tasks WHERE id = ?",
                                      (str(self.task["id"]),)).fetchone()
        self.assertEqual(row["status"], "BLOCKED")
        self.assertIn("feasibility_concern", row["blocked_reason"])


class ReplyParsingTests(unittest.TestCase):
    def test_plain_json(self) -> None:
        self.assertEqual(parse_json_reply('{"action":"finish"}'), {"action": "finish"})

    def test_fenced_json(self) -> None:
        self.assertEqual(parse_json_reply('前言\n```json\n{"action":"ask"}\n```\n后记'),
                         {"action": "ask"})

    def test_json_with_surrounding_prose(self) -> None:
        self.assertEqual(parse_json_reply('好的：{"action":"concern"} 以上'), {"action": "concern"})

    def test_non_json_raises(self) -> None:
        with self.assertRaises(ValueError):
            parse_json_reply("我做不到")


if __name__ == "__main__":
    unittest.main()
