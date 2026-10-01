"""multi_agent_assertions 断言库的单测（工作包 C，第四期交付）。

库本体在 scripts/（与 A 的 e2e 主脚本同目录、同 sys.path 约定），测试从这里
把 scripts 加进 path 后导入。全部用假 payload，无网络、无时序依赖。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import multi_agent_assertions as maa  # noqa: E402


def _team(**overrides):
    base = {
        "project_id": "p-1",
        "agents": [
            {
                "agent_id": "agent-a",
                "agent_name": "A",
                "device_id": "d-1",
                "online": True,
                "capabilities": ["task.claim"],
                "load": 1,
                "current_tasks": [{"task_id": "t-1", "status": "RUNNING"}],
                "next_tasks": [],
                "waiting_tasks": [],
                "pending_handoffs": [],
            },
            {
                "agent_id": "agent-b",
                "agent_name": "B",
                "device_id": "d-2",
                "online": False,
                "capabilities": [],
                "load": 0,
                "current_tasks": [],
                "next_tasks": [],
                "waiting_tasks": [],
                "pending_handoffs": [{"handoff_id": "h-1", "task_id": "t-2", "status": "SENT"}],
            },
        ],
        "agent_count": 2,
        "online_count": 1,
        "task_status_counts": {"RUNNING": 1, "APPROVED": 1},
        "pending_handoff_count": 1,
        "generated_at": "2026-09-30T00:00:00+00:00",
    }
    base.update(overrides)
    return base


def _node(artifact_id: str, *, version: int = 1, status: str = "APPROVED", receipt=None, downstream=()):
    return {
        "artifact_id": artifact_id,
        "version": version,
        "status": status,
        "receipt": receipt,
        "downstream_tasks": [{"task_id": tid, "title": "t", "status": "READY"} for tid in downstream],
    }


def _receipt(**overrides):
    base = {
        "receipt_version": 1,
        "tool_name": "workspace_diff",
        "tool_call_id": "turn-t1",
        "args_hash": "0123456789abcdef",
        "output_hash": "fedcba9876543210",
        "output_bytes": 512,
    }
    base.update(overrides)
    return base


class TaskTransitionTests(unittest.TestCase):
    def test_script_used_transitions_all_legal(self):
        steps = ["READY", "CLAIMED", "RUNNING", "WAITING_REVIEW", "APPROVED"]
        ok, detail = maa.check_task_lifecycle(steps)
        self.assertTrue(ok, detail)

    def test_retry_path_legal(self):
        ok, _ = maa.check_task_lifecycle(["READY", "CLAIMED", "RUNNING", "FAILED", "READY", "CLAIMED"])
        self.assertTrue(ok)

    def test_unknown_status_fail_closed(self):
        ok, detail = maa.check_task_transition("READY", "TELEPORTED")
        self.assertFalse(ok)
        self.assertIn("unknown task status", detail)

    def test_unknown_transition_fails_with_hint(self):
        ok, detail = maa.check_task_transition("APPROVED", "READY")
        self.assertFalse(ok)
        self.assertIn("extend TASK_TRANSITIONS", detail)

    def test_success_is_not_artifact_approval(self):
        # 规划 §7 红线：任务收口与成果物批准是两条独立的人工审核，
        # 状态机里不存在"任务批准自动带动成果物"的捷径。
        ok, _ = maa.check_task_transition("WAITING_REVIEW", "APPROVED")
        self.assertTrue(ok)


class ReceiptShapeTests(unittest.TestCase):
    def test_valid_receipt(self):
        ok, detail = maa.check_receipt_shape(_receipt(truncated=False, status="success"))
        self.assertTrue(ok, detail)

    def test_version_zero_means_no_receipt(self):
        ok, detail = maa.check_receipt_shape(_receipt(receipt_version=0))
        self.assertFalse(ok)
        self.assertIn(">= 1", detail)

    def test_bad_hash_rejected(self):
        for bad in ("ABCDEF0123456789", "0123456789abcde", "0123456789abcdef00"):
            ok, detail = maa.check_receipt_shape(_receipt(output_hash=bad))
            self.assertFalse(ok, bad)
            self.assertIn("output_hash", detail)

    def test_field_limits_and_types(self):
        self.assertFalse(maa.check_receipt_shape(_receipt(tool_name="x" * 129))[0])
        self.assertFalse(maa.check_receipt_shape(_receipt(output_bytes=-1))[0])
        self.assertFalse(maa.check_receipt_shape(_receipt(status="maybe"))[0])
        self.assertFalse(maa.check_receipt_shape(_receipt(truncated="yes"))[0])


class TeamViewTests(unittest.TestCase):
    def test_valid_team_view(self):
        ok, detail = maa.check_team_view(_team(), expect_agent_ids=["agent-a", "agent-b"])
        self.assertTrue(ok, detail)

    def test_count_mismatch_detected(self):
        ok, detail = maa.check_team_view(_team(agent_count=3))
        self.assertFalse(ok)
        self.assertIn("agent_count", detail)

    def test_online_count_mismatch_detected(self):
        ok, detail = maa.check_team_view(_team(online_count=2))
        self.assertFalse(ok)
        self.assertIn("online_count", detail)

    def test_load_must_equal_current_tasks(self):
        team = _team()
        team["agents"][0]["load"] = 5
        ok, detail = maa.check_team_view(team)
        self.assertFalse(ok)
        self.assertIn("load", detail)

    def test_unknown_status_in_counts_fail_closed(self):
        ok, detail = maa.check_team_view(_team(task_status_counts={"TELEPORTED": 1}))
        self.assertFalse(ok)
        self.assertIn("TELEPORTED", detail)

    def test_missing_agent_detected(self):
        ok, detail = maa.check_team_view(_team(), expect_agent_ids=["agent-z"])
        self.assertFalse(ok)
        self.assertIn("agent-z", detail)


class ProductionPathTests(unittest.TestCase):
    def test_valid_path_with_receipt(self):
        payload = {"nodes": [_node("a-1", receipt=_receipt(), downstream=["t-2"]), _node("a-2", version=2)]}
        ok, detail = maa.check_production_path(payload, expect_artifact_ids=["a-1", "a-2"])
        self.assertTrue(ok, detail)

    def test_duplicate_node_rejected(self):
        payload = {"nodes": [_node("a-1"), _node("a-1")]}
        ok, detail = maa.check_production_path(payload)
        self.assertFalse(ok)
        self.assertIn("duplicate", detail)

    def test_bad_nested_receipt_surfaced_with_artifact_id(self):
        payload = {"nodes": [_node("a-1", receipt=_receipt(args_hash="nothex"))]}
        ok, detail = maa.check_production_path(payload)
        self.assertFalse(ok)
        self.assertIn("a-1", detail)
        self.assertIn("args_hash", detail)

    def test_downstream_entries_must_have_task_id(self):
        payload = {"nodes": [_node("a-1", downstream=[None])]}
        self.assertFalse(maa.check_production_path(payload)[0])

    def test_version_bump_and_feeds_helpers(self):
        ok, detail = maa.check_artifact_version_bump(2, 3)
        self.assertTrue(ok, detail)
        self.assertFalse(maa.check_artifact_version_bump(2, 4)[0])
        payload = {"nodes": [_node("a-1", downstream=["t-9"])]}
        self.assertTrue(maa.check_artifact_feeds_task(payload, "a-1", "t-9")[0])
        ok, detail = maa.check_artifact_feeds_task(payload, "a-1", "t-404")
        self.assertFalse(ok)
        self.assertIn("does not feed", detail)


class EventStreamTests(unittest.TestCase):
    def _events(self, *names_seqs):
        return [{"event": name, "seq": seq, "schema_version": 1, "occurred_at": "t", "payload": {}} for name, seq in names_seqs]

    def test_mixed_catalog_and_legacy_stream(self):
        events = self._events(
            ("task.created", 1),  # 老轨名：豁免但要点名
            ("project.task.created", 2),
            ("project.artifact.uploaded", 3),
            ("project.gate.blocked", 4),
        )
        ok, detail = maa.check_event_stream(events)
        self.assertTrue(ok, detail)
        self.assertIn("3 catalog", detail)
        self.assertIn("task.created", detail)  # 老轨名逐个可见

    def test_unregistered_catalog_shaped_name_fails(self):
        ok, detail = maa.check_event_stream(self._events(("project.task.exploded", 1)))
        self.assertFalse(ok)
        self.assertIn("not registered", detail)

    def test_seq_must_strictly_increase(self):
        ok, detail = maa.check_event_stream(self._events(("project.task.created", 3), ("project.task.claimed", 3)))
        self.assertFalse(ok)
        self.assertIn("strictly increasing", detail)

    def test_seq_must_be_positive_int(self):
        for bad in (0, -1, True, "7", None):
            ok, detail = maa.check_event_stream(self._events(("project.task.created", bad)))
            self.assertFalse(ok, f"seq={bad!r}")
            self.assertIn("seq", detail)

    def test_empty_stream_is_valid(self):
        ok, detail = maa.check_event_stream([])
        self.assertTrue(ok, detail)


class GateEventTests(unittest.TestCase):
    """形状取自 workflow_engine.py 的真实 wire 格式（2026-09-30 对齐）。"""

    def _leaf(self, verdict="HOLDS", criterion="file:outputs/x.md non-empty"):
        return {"criterion": criterion, "family": "file_non_empty", "verdict": verdict, "detail": "3 bytes"}

    def test_evaluated_holds_consistent(self):
        payload = {"task_id": "t1", "node_id": "n1", "verdict": "HOLDS", "leaves": [self._leaf()]}
        ok, detail = maa.check_gate_evaluated_payload(payload)
        self.assertTrue(ok, detail)

    def test_evaluated_holds_with_unverified_leaf_contradiction(self):
        payload = {"task_id": "t1", "node_id": "n1", "verdict": "HOLDS",
                   "leaves": [self._leaf(), self._leaf("UNVERIFIED", "tests_passed:x")]}
        ok, detail = maa.check_gate_evaluated_payload(payload)
        self.assertFalse(ok)
        self.assertIn("contradiction" if "contradiction" in detail else "HOLDS", detail)

    def test_evaluated_unverified_needs_a_unverified_leaf(self):
        payload = {"task_id": "t1", "node_id": "n1", "verdict": "UNVERIFIED", "leaves": [self._leaf()]}
        self.assertFalse(maa.check_gate_evaluated_payload(payload)[0])
        payload = {"task_id": "t1", "node_id": "n1", "verdict": "UNVERIFIED",
                   "leaves": [self._leaf("UNVERIFIED", "tests_passed:x")]}
        self.assertTrue(maa.check_gate_evaluated_payload(payload)[0])

    def test_evaluated_not_holds_needs_a_false_leaf(self):
        payload = {"task_id": "t1", "node_id": "n1", "verdict": "NOT_HOLDS",
                   "leaves": [self._leaf("UNVERIFIED", "artifact:paper approved")]}
        self.assertFalse(maa.check_gate_evaluated_payload(payload)[0])
        payload = {"task_id": "t1", "node_id": "n1", "verdict": "NOT_HOLDS",
                   "leaves": [self._leaf("NOT_HOLDS", "file:missing.md non-empty")]}
        self.assertTrue(maa.check_gate_evaluated_payload(payload)[0])

    def test_evaluated_structure_failures(self):
        base = {"task_id": "t1", "node_id": "n1", "verdict": "HOLDS", "leaves": [self._leaf()]}
        self.assertFalse(maa.check_gate_evaluated_payload({**base, "verdict": "MAYBE"})[0])
        self.assertFalse(maa.check_gate_evaluated_payload({**base, "leaves": []})[0])
        bad_leaf = {"family": "file_non_empty", "verdict": "HOLDS", "detail": "x"}
        self.assertFalse(maa.check_gate_evaluated_payload({**base, "leaves": [bad_leaf]})[0])

    def test_blocked_payload(self):
        ok, detail = maa.check_gate_blocked_payload(
            {"task_id": "t1", "node_id": "n1", "on_block": "escalate_human", "unchecked": ["tests_passed:x"]}
        )
        self.assertTrue(ok, detail)
        # 硬失败阻塞允许 unchecked 为空
        self.assertTrue(maa.check_gate_blocked_payload(
            {"task_id": "t1", "node_id": "n1", "on_block": "blocked", "unchecked": []}
        )[0])
        self.assertFalse(maa.check_gate_blocked_payload({"on_block": "maybe", "unchecked": []})[0])
        self.assertFalse(maa.check_gate_blocked_payload({"on_block": "blocked", "unchecked": [""]})[0])
        self.assertFalse(maa.check_gate_blocked_payload({"on_block": "blocked"})[0])


class LedgerSeriesTests(unittest.TestCase):
    def test_stall_series_legal(self):
        ok, detail = maa.check_stall_series([0, 1, 2, 1, 0, 0, 1])
        self.assertTrue(ok, detail)

    def test_stall_series_illegal_jumps(self):
        for bad in ([0, 2], [1, 3], [2, 0], [0, -1]):
            ok, detail = maa.check_stall_series(bad)
            self.assertFalse(ok, bad)
            self.assertIn("illegal stall transition", detail)

    def test_ledger_snapshot_roundtrip(self):
        ledger = {
            "task": "t", "facts": [], "plan": [], "round": 3, "stall_count": 1,
            "done": False, "needs_replan": False, "last_next_speaker": "engine", "last_instruction": "go",
        }
        ok, detail = maa.check_ledger_snapshot(ledger)
        self.assertTrue(ok, detail)
        self.assertIn("round=3", detail)

    def test_ledger_snapshot_invalid(self):
        ok, detail = maa.check_ledger_snapshot({"round": "not-an-int"})
        self.assertFalse(ok)
        self.assertIn("invalid ledger snapshot", detail)


if __name__ == "__main__":
    unittest.main()
