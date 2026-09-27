"""progress_ledger.py 的单测（工作包 C，第三期交付）。

覆盖：账本结构校验与 stall 记账（衰减规则逐字对齐 autogen）、
goal 判停四问与签名重复计数（对齐 deer-flow）、派发回退链五种来源与
名字归一化（对齐 autogen selector 回退）。自包含、无 IO、无时序依赖。
"""

from __future__ import annotations

import unittest

from app.progress_ledger import (
    CONTINUABLE_GOAL_BLOCKERS,
    LEDGER_REQUIRED_KEYS,
    MAX_CONTINUATIONS,
    MAX_ENTRY_CHARS,
    MAX_LEDGER_ENTRIES,
    MAX_NO_PROGRESS_CONTINUATIONS,
    DispatchCandidate,
    GoalTracker,
    LedgerUpdateError,
    ProgressLedger,
    decide_continuation,
    normalize_role_name,
    resolve_dispatch,
    visible_output_signature,
)

PARTICIPANTS = ("writer", "critic", "coder")


def _update(**overrides):
    base = {
        "is_request_satisfied": {"answer": False, "reason": "not yet"},
        "is_progress_being_made": {"answer": True, "reason": "moving"},
        "is_in_loop": {"answer": False, "reason": "linear"},
        "instruction_or_question": "continue with the draft",
        "next_speaker": "writer",
    }
    base.update(overrides)
    return base


class ProgressLedgerTests(unittest.TestCase):
    def test_fresh_ledger(self):
        ledger = ProgressLedger.new("写论文", facts=["题目已定"], plan=["先列大纲"])
        self.assertEqual((ledger.round, ledger.stall_count, ledger.done, ledger.needs_replan), (0, 0, False, False))
        self.assertEqual(ledger.facts, ("题目已定",))

    def test_apply_round_counts_and_keeps_speaker(self):
        ledger = ProgressLedger.new("t").apply_round(_update(), participant_names=PARTICIPANTS)
        self.assertEqual(ledger.round, 1)
        self.assertEqual(ledger.stall_count, 0)
        self.assertEqual(ledger.last_next_speaker, "writer")
        self.assertFalse(ledger.done)

    def test_missing_key_rejected(self):
        for key in LEDGER_REQUIRED_KEYS:
            update = _update()
            update.pop(key)
            with self.assertRaises(LedgerUpdateError, msg=key):
                ProgressLedger.new("t").apply_round(update, participant_names=PARTICIPANTS)

    def test_malformed_decision_rejected(self):
        with self.assertRaises(LedgerUpdateError):
            ProgressLedger.new("t").apply_round(
                _update(is_progress_being_made={"answer": "yes"}), participant_names=PARTICIPANTS
            )
        with self.assertRaises(LedgerUpdateError):
            ProgressLedger.new("t").apply_round(
                _update(is_request_satisfied={"answer": True, "reason": 3}), participant_names=PARTICIPANTS
            )

    def test_next_speaker_must_be_participant_unless_satisfied(self):
        with self.assertRaises(LedgerUpdateError):
            ProgressLedger.new("t").apply_round(_update(next_speaker="nobody"), participant_names=PARTICIPANTS)
        ok = ProgressLedger.new("t").apply_round(
            _update(next_speaker="nobody", is_request_satisfied={"answer": True, "reason": "done"}),
            participant_names=PARTICIPANTS,
        )
        self.assertTrue(ok.done)
        self.assertIsNone(ok.last_next_speaker)

    def test_stall_increments_on_no_progress_and_loop(self):
        stalled = ProgressLedger.new("t").apply_round(
            _update(is_progress_being_made={"answer": False, "reason": "stuck"}), participant_names=PARTICIPANTS
        )
        self.assertEqual(stalled.stall_count, 1)
        looping = stalled.apply_round(
            _update(is_progress_being_made={"answer": True, "reason": "ok"}, is_in_loop={"answer": True, "reason": "circular"}),
            participant_names=PARTICIPANTS,
        )
        self.assertEqual(looping.stall_count, 2)

    def test_stall_decays_but_never_below_zero(self):
        ledger = ProgressLedger.new("t").apply_round(
            _update(is_progress_being_made={"answer": False, "reason": "x"}), participant_names=PARTICIPANTS
        )
        ledger = ledger.apply_round(
            _update(is_progress_being_made={"answer": False, "reason": "x"}), participant_names=PARTICIPANTS
        )
        self.assertEqual(ledger.stall_count, 2)
        for _ in range(5):  # 衰减到 0 为止，不清零成负数
            ledger = ledger.apply_round(_update(), participant_names=PARTICIPANTS)
        self.assertEqual(ledger.stall_count, 0)

    def test_needs_replan_at_threshold_and_reset_by_satisfaction(self):
        ledger = ProgressLedger.new("t")
        for _ in range(2):
            ledger = ledger.apply_round(
                _update(is_progress_being_made={"answer": False, "reason": "x"}), participant_names=PARTICIPANTS
            )
        self.assertFalse(ledger.needs_replan)
        ledger = ledger.apply_round(
            _update(is_progress_being_made={"answer": False, "reason": "x"}), participant_names=PARTICIPANTS
        )
        self.assertTrue(ledger.needs_replan)  # stall 3 >= max_stalls 3
        done = ledger.apply_round(
            _update(is_request_satisfied={"answer": True, "reason": "finally"}), participant_names=PARTICIPANTS
        )
        self.assertTrue(done.done)
        self.assertFalse(done.needs_replan)  # 完成时不再要求重规划

    def test_facts_and_plan_are_bounded(self):
        facts = [f"fact-{i}" for i in range(MAX_LEDGER_ENTRIES + 10)]
        long_entry = "x" * (MAX_ENTRY_CHARS + 100)
        ledger = ProgressLedger.new("t", facts=facts, plan=[long_entry])
        self.assertEqual(len(ledger.facts), MAX_LEDGER_ENTRIES)
        self.assertEqual(ledger.facts[-1], f"fact-{MAX_LEDGER_ENTRIES + 9}")  # 保留最新的
        self.assertEqual(len(ledger.plan[0]), MAX_ENTRY_CHARS)

    def test_with_facts_immutability(self):
        original = ProgressLedger.new("t", facts=["a"])
        updated = original.with_facts(["b", "c"])
        self.assertEqual(original.facts, ("a",))
        self.assertEqual(updated.facts, ("b", "c"))
        self.assertEqual(updated.round, original.round)

    def test_json_roundtrip_and_lenient_load(self):
        ledger = ProgressLedger.new("t", facts=["f"], plan=["p"]).apply_round(_update(), participant_names=PARTICIPANTS)
        restored = ProgressLedger.from_json(ledger.to_json())
        self.assertEqual(restored, ledger)
        tolerant = ProgressLedger.from_json({**ledger.to_json(), "unknown_field": 1})
        self.assertEqual(tolerant, ledger)
        with self.assertRaises(LedgerUpdateError):
            ProgressLedger.from_json({"round": "not-an-int-ints-only"})  # 缺 task 必填


class GoalContinuationTests(unittest.TestCase):
    def test_signature_is_stable_and_whitespace_insensitive(self):
        self.assertEqual(visible_output_signature("  hello world \n"), visible_output_signature("hello world"))
        self.assertNotEqual(visible_output_signature("v1"), visible_output_signature("v2"))

    def test_decide_four_stop_reasons(self):
        # 满足 → 停
        self.assertFalse(decide_continuation({"satisfied": True, "blocker": "none"}, continuation_count=0, no_progress_count=0).should_continue)
        # blocker 不可续 → 停（全部非 goal_not_met_yet 的 blocker 都要停）
        for blocker in ("missing_evidence", "needs_user_input", "run_failed", "external_wait"):
            decision = decide_continuation({"satisfied": False, "blocker": blocker}, continuation_count=0, no_progress_count=0)
            self.assertFalse(decision.should_continue, blocker)
            self.assertIn("not continuable", decision.reason)
        # 预算耗尽 → 停
        decision = decide_continuation(
            {"satisfied": False, "blocker": "goal_not_met_yet"},
            continuation_count=MAX_CONTINUATIONS, no_progress_count=0,
        )
        self.assertFalse(decision.should_continue)
        self.assertIn("budget", decision.reason)
        # 无进展超限 → 停
        decision = decide_continuation(
            {"satisfied": False, "blocker": "goal_not_met_yet"},
            continuation_count=0, no_progress_count=MAX_NO_PROGRESS_CONTINUATIONS,
        )
        self.assertFalse(decision.should_continue)
        self.assertIn("no visible progress", decision.reason)
        # 其余情况 → 续
        ok = decide_continuation(
            {"satisfied": False, "blocker": "goal_not_met_yet"}, continuation_count=1, no_progress_count=1,
        )
        self.assertTrue(ok.should_continue)
        self.assertEqual(CONTINUABLE_GOAL_BLOCKERS, {"goal_not_met_yet"})

    def test_tracker_counts_repeats_of_same_output(self):
        tracker = GoalTracker()
        tracker.observe_output("同一版产出")
        self.assertEqual(tracker.state.signature_repeat_count, 1)
        tracker.observe_output("同一版产出  ")  # 仅空白差异仍算同一版
        self.assertEqual(tracker.state.signature_repeat_count, 2)
        tracker.observe_output("新的产出")
        self.assertEqual(tracker.state.signature_repeat_count, 1)

    def test_tracker_blocker_change_resets_repeat(self):
        tracker = GoalTracker()
        tracker.note_blocker("goal_not_met_yet")
        tracker.observe_output("same")
        tracker.observe_output("same")
        self.assertEqual(tracker.state.signature_repeat_count, 2)
        tracker.note_blocker("goal_not_met_yet")  # 同 blocker 不重置
        self.assertEqual(tracker.state.signature_repeat_count, 2)
        tracker.note_blocker("missing_evidence")  # blocker 变化 → 重新计数
        self.assertEqual(tracker.state.signature_repeat_count, 0)

    def test_tracker_unknown_blocker_rejected(self):
        with self.assertRaises(ValueError):
            GoalTracker().note_blocker("just_because")

    def test_tracker_full_flow_stops_on_stall(self):
        tracker = GoalTracker().note_blocker("goal_not_met_yet")
        tracker.observe_output("论文 v1")
        self.assertTrue(tracker.decide({"satisfied": False, "blocker": "goal_not_met_yet"}).should_continue)
        # 字节级相同的重复产出才算原地踏步；评估器的措辞每次都会变，所以判停只看可见产出签名。
        tracker.observe_output("论文 v1")
        self.assertEqual(tracker.state.signature_repeat_count, 2)
        decision = tracker.decide({"satisfied": False, "blocker": "goal_not_met_yet"})
        self.assertFalse(decision.should_continue)
        tracker.mark_continuation()
        self.assertEqual(tracker.state.continuation_count, 1)

    def test_tracker_json_roundtrip(self):
        tracker = GoalTracker().note_blocker("goal_not_met_yet")
        tracker.observe_output("out")
        tracker.mark_continuation()
        restored = GoalTracker.from_json(tracker.to_json())
        self.assertEqual(restored.state, tracker.state)


class DispatchChainTests(unittest.TestCase):
    def _candidates(self):
        return (
            DispatchCandidate("r1", "审题", capabilities=frozenset({"files.read"})),
            DispatchCandidate("r2", "Code_Writer", capabilities=frozenset({"files.read", "files.write", "shell.run"})),
            DispatchCandidate("r3", "复核", capabilities=frozenset({"files.read"}), available=False),
        )

    def test_requested_hit_by_name_with_normalization(self):
        decision = resolve_dispatch(
            candidates=self._candidates(), required_capabilities=["files.write"], requested_role="code writer"
        )
        self.assertEqual((decision.role_id, decision.source), ("r2", "requested"))

    def test_requested_hit_by_role_id(self):
        decision = resolve_dispatch(candidates=self._candidates(), requested_role="R1")
        self.assertEqual((decision.role_id, decision.source), ("r1", "requested"))

    def test_requested_but_caps_missing_falls_to_capability(self):
        decision = resolve_dispatch(
            candidates=self._candidates(), required_capabilities=["shell.run"], requested_role="审题"
        )
        self.assertEqual((decision.role_id, decision.source), ("r2", "capability"))

    def test_unavailable_requested_skipped(self):
        candidates = (DispatchCandidate("x", "复核", available=False), DispatchCandidate("y", "写作"))
        decision = resolve_dispatch(candidates=candidates, requested_role="复核")
        self.assertEqual((decision.role_id, decision.source), ("y", "capability"))

    def test_previous_with_caps_outranks_declaration_order(self):
        # 上一角色能力齐时优先续用（连续性 > 声明顺序）
        candidates = (
            DispatchCandidate("a", "甲", capabilities=frozenset({"files.write"})),
            DispatchCandidate("b", "code_writer", capabilities=frozenset({"files.read", "files.write", "shell.run"})),
        )
        decision = resolve_dispatch(
            candidates=candidates, required_capabilities=["files.write"], previous_role="code_writer"
        )
        self.assertEqual((decision.role_id, decision.source), ("b", "previous"))

    def test_capability_and_previous_miss_falls_to_first_with_waiver(self):
        # 没人有 web.search；上一角色（code_writer）也不具备 → 跳过 previous，落首个可用并豁免
        decision = resolve_dispatch(
            candidates=self._candidates(),
            required_capabilities=["web.search"],
            previous_role="code_writer",
        )
        self.assertEqual((decision.role_id, decision.source), ("r1", "first"))
        self.assertIn("waived", decision.reason)

    def test_previous_ignored_when_caps_missing_then_first_with_waiver(self):
        decision = resolve_dispatch(
            candidates=(DispatchCandidate("a", "审题", capabilities=frozenset({"files.read"})),),
            required_capabilities=["shell.run"],
            previous_role="审题",
        )
        self.assertEqual((decision.role_id, decision.source), ("a", "first"))
        self.assertIn("waived", decision.reason)

    def test_no_available_candidate_escalates(self):
        decision = resolve_dispatch(
            candidates=(DispatchCandidate("x", "复核", available=False),)
        )
        self.assertIsNone(decision.role_id)
        self.assertEqual(decision.source, "none")

    def test_capability_match_respects_declaration_order(self):
        candidates = (
            DispatchCandidate("a", "A", capabilities=frozenset({"w"})),
            DispatchCandidate("b", "B", capabilities=frozenset({"w"})),
        )
        decision = resolve_dispatch(candidates=candidates, required_capabilities=["w"])
        self.assertEqual(decision.role_id, "a")

    def test_normalize_role_name(self):
        self.assertEqual(normalize_role_name("Code_Writer"), normalize_role_name("code writer"))
        self.assertEqual(normalize_role_name("mm-coding"), normalize_role_name("mm coding"))
        self.assertEqual(normalize_role_name("  复核  "), "复核")


if __name__ == "__main__":
    unittest.main()
