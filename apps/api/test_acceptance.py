"""acceptance.py 判定器的全套三态单测（工作包 C，第一期交付）。

覆盖：五类条件语法 × HOLDS/NOT_HOLDS/UNVERIFIED 三态、fail-closed 规则、
测试输出三形状（fail 优先 / zero 否决 / 无摘要 UNVERIFIED）、
PathGuardProbe 越界防护、all/any 组合的三值（Kleene）语义、spec 结构校验。
自包含：无网络、无固定端口、临时目录自清理。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from app import acceptance
from app.acceptance import (
    FileStat,
    GateSpecError,
    PathGuardProbe,
    classify_test_output,
    evaluate_all,
    evaluate_criterion,
    evaluate_gate,
)


class FakeProbe:
    """内存文件探针：files 映射 path -> bytes 或 "unreadable" 哨兵。"""

    def __init__(self, files: dict[str, bytes] | None = None, unreadable: set[str] | None = None):
        self.files = files or {}
        self.unreadable = unreadable or set()

    def stat(self, path: str) -> FileStat | None:
        if path in self.files:
            return FileStat(exists=True, size=len(self.files[path]))
        if path in self.unreadable:
            return FileStat(exists=True, size=None)
        return FileStat(exists=False, size=None)

    def read(self, path: str, max_bytes: int) -> bytes | None:
        if path in self.unreadable:
            return None
        data = self.files.get(path)
        if data is None:
            return None
        return data[:max_bytes]


class FakeTests:
    def __init__(self, runs: dict[str, str] | None = None):
        self.runs = runs or {}

    def lookup(self, command: str) -> str | None:
        return self.runs.get(command)


class FakeDecisions:
    def __init__(
        self,
        artifacts: dict[str, bool] | None = None,
        reviews: dict[str, bool] | None = None,
    ):
        self.artifacts = artifacts or {}
        self.reviews = reviews or {}

    def artifact_approved(self, artifact_type: str) -> bool | None:
        return self.artifacts.get(artifact_type)

    def review_concluded(self, role: str) -> bool | None:
        return self.reviews.get(role)


class SyntaxTests(unittest.TestCase):
    def _family(self, criterion: str, **kwargs) -> str:
        kwargs.setdefault("probe", FakeProbe())
        kwargs.setdefault("tests", FakeTests())
        kwargs.setdefault("decisions", FakeDecisions())
        return evaluate_criterion(criterion, **kwargs).family

    def test_syntax_is_case_insensitive(self):
        self.assertEqual(self._family("FILE:report.pdf EXISTS"), "file_exists")
        self.assertEqual(self._family("File:report.pdf Non-Empty"), "file_non_empty")
        self.assertEqual(self._family("FILE_WRITTEN:out/paper.tex"), "file_written")
        self.assertEqual(self._family("TESTS_PASSED:python -m pytest -q"), "tests_passed")
        self.assertEqual(self._family("Artifact:paper Approved"), "artifact_approved")
        self.assertEqual(self._family("Review:critique Concluded"), "review_concluded")

    def test_unknown_syntax_is_undecidable_even_with_all_sources(self):
        result = evaluate_criterion(
            "model:deepseek agrees",
            probe=FakeProbe(),
            tests=FakeTests(),
            decisions=FakeDecisions(),
        )
        self.assertEqual(result.family, "undecidable")
        self.assertFalse(result.checked)
        self.assertEqual(result.verdict, acceptance.VERDICT_UNVERIFIED)

    def test_empty_and_whitespace_criteria_are_undecidable(self):
        for text in ("", "   ", None):
            self.assertEqual(evaluate_criterion(text).family, "undecidable")

    def test_missing_source_configuration_is_undecidable_for_that_family(self):
        self.assertEqual(evaluate_criterion("file:a.txt exists").family, "undecidable")
        self.assertEqual(evaluate_criterion("tests_passed:x").family, "undecidable")
        self.assertEqual(evaluate_criterion("artifact:paper approved").family, "undecidable")


class FileCriterionTests(unittest.TestCase):
    def test_exists_holds_and_not_holds(self):
        probe = FakeProbe(files={"a.txt": b"hello"})
        held = evaluate_criterion("file:a.txt exists", probe=probe)
        self.assertTrue(held.checked and held.holds)
        missing = evaluate_criterion("file:missing.txt exists", probe=probe)
        self.assertTrue(missing.checked and not missing.holds)
        self.assertIn("does not exist", missing.detail)

    def test_non_empty_rejects_empty_file(self):
        probe = FakeProbe(files={"empty.txt": b"", "full.txt": b"data"})
        held = evaluate_criterion("file:full.txt non-empty", probe=probe)
        self.assertTrue(held.holds)
        empty = evaluate_criterion("file:empty.txt non-empty", probe=probe)
        self.assertTrue(empty.checked and not empty.holds)
        self.assertIn("empty", empty.detail)

    def test_size_unknown_degrades_to_unverified(self):
        probe = FakeProbe(unreadable={"mystery.bin"})
        result = evaluate_criterion("file:mystery.bin non-empty", probe=probe)
        self.assertFalse(result.checked)
        self.assertEqual(result.verdict, acceptance.VERDICT_UNVERIFIED)

    def test_written_requires_read_back(self):
        probe = FakeProbe(files={"ok.bin": b"\x00\x01"}, unreadable={"mode000.bin"})
        held = evaluate_criterion("file_written:ok.bin", probe=probe)
        self.assertTrue(held.checked and held.holds)
        self.assertIn("read-back ok", held.detail)
        blocked = evaluate_criterion("file_written:mode000.bin", probe=probe)
        self.assertFalse(blocked.checked)
        self.assertEqual(blocked.verdict, acceptance.VERDICT_UNVERIFIED)

    def test_written_missing_file_is_checked_false_holds(self):
        probe = FakeProbe()
        result = evaluate_criterion("file_written:nope.txt", probe=probe)
        self.assertTrue(result.checked and not result.holds)


class TestOutputClassificationTests(unittest.TestCase):
    def test_pass_shapes(self):
        for output in (
            "5 passed in 0.12s",
            "OK",
            "test result: ok. 12 passed",
            "ok  \tmymodule/tests",
            "BUILD SUCCESSFUL",
            "all tests passed, nice",
        ):
            self.assertEqual(classify_test_output(output), "pass", output)

    def test_fail_shapes(self):
        for output in (
            "1 failed, 4 passed in 0.5s",
            "2 errors during collection",
            "FAILED (failures=1)",
            "ERROR tests/unit/test_auth.py",
            "test result: FAILED. 3 passed",
            "FAIL\tpkg/path",
            "BUILD FAILURE",
        ):
            self.assertEqual(classify_test_output(output), "fail", output)

    def test_zero_shapes_veto_pass(self):
        for output in ("0 passed", "Ran 0 tests", "[no test files]"):
            self.assertEqual(classify_test_output(output), "none", output)

    def test_no_summary_is_none(self):
        self.assertEqual(classify_test_output("hello world, all done!"), "none")

    def tests_passed_criterion_three_states(self):
        recorded = FakeTests({
            "good": "3 passed in 0.1s",
            "bad": "2 failed",
            "zero": "Ran 0 tests",
            "muted": "it seems fine",
        })
        held = evaluate_criterion("tests_passed:good", tests=recorded)
        self.assertTrue(held.checked and held.holds)
        failed = evaluate_criterion("tests_passed:bad", tests=recorded)
        self.assertTrue(failed.checked and not failed.holds)
        for cmd in ("zero", "muted"):
            result = evaluate_criterion(f"tests_passed:{cmd}", tests=recorded)
            self.assertFalse(result.checked, cmd)
            self.assertEqual(result.verdict, acceptance.VERDICT_UNVERIFIED, cmd)
        unrecorded = evaluate_criterion("tests_passed:never-ran", tests=recorded)
        self.assertFalse(unrecorded.checked)
        self.assertIn("no recorded run", unrecorded.detail)


class DecisionCriterionTests(unittest.TestCase):
    def test_artifact_approved_three_states(self):
        decisions = FakeDecisions(artifacts={"paper": True, "figure": False})
        held = evaluate_criterion("artifact:paper approved", decisions=decisions)
        self.assertTrue(held.checked and held.holds)
        rejected = evaluate_criterion("artifact:figure approved", decisions=decisions)
        self.assertTrue(rejected.checked and not rejected.holds)
        unknown = evaluate_criterion("artifact:model approved", decisions=decisions)
        self.assertFalse(unknown.checked)
        self.assertEqual(unknown.verdict, acceptance.VERDICT_UNVERIFIED)

    def test_review_concluded_three_states(self):
        decisions = FakeDecisions(reviews={"critique": True, "modeling": False})
        held = evaluate_criterion("review:critique concluded", decisions=decisions)
        self.assertTrue(held.checked and held.holds)
        pending = evaluate_criterion("review:modeling concluded", decisions=decisions)
        self.assertTrue(pending.checked and not pending.holds)
        unregistered = evaluate_criterion("review:nobody concluded", decisions=decisions)
        self.assertFalse(unregistered.checked)
        self.assertEqual(unregistered.verdict, acceptance.VERDICT_UNVERIFIED)
        no_service = evaluate_criterion("review:critique concluded")
        self.assertEqual(no_service.family, "undecidable")


class PathGuardProbeTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "outputs").mkdir()
        (self.root / "outputs" / "paper.pdf").write_bytes(b"%PDF-1.4 fake")
        (self.root / "secret.txt").write_bytes(b"top secret")
        self.probe = PathGuardProbe(str(self.root), _RealProbe())

    def test_relative_path_within_root_holds(self):
        result = evaluate_criterion("file:outputs/paper.pdf non-empty", probe=self.probe)
        self.assertTrue(result.checked and result.holds, result.detail)

    def test_traversal_escape_is_unverified(self):
        result = evaluate_criterion("file:../secret.txt exists", probe=self.probe)
        self.assertFalse(result.checked)
        self.assertEqual(result.verdict, acceptance.VERDICT_UNVERIFIED)

    def test_absolute_path_outside_root_is_unverified(self):
        outside = self.root.parent / "elsewhere.txt"
        result = evaluate_criterion(f"file:{outside} exists", probe=self.probe)
        self.assertFalse(result.checked)

    def test_absolute_path_inside_root_holds(self):
        target = self.root / "secret.txt"
        result = evaluate_criterion(f"file:{target} exists", probe=self.probe)
        self.assertTrue(result.checked and result.holds, result.detail)

    def test_null_byte_path_is_unverified(self):
        result = evaluate_criterion("file:out\x00put/x exists", probe=self.probe)
        self.assertFalse(result.checked)


class _RealProbe:
    """基于真实文件系统的探针（配合 PathGuardProbe 的集成测试用）。"""

    def stat(self, path: str) -> FileStat | None:
        if not os.path.exists(path):
            return FileStat(exists=False, size=None)
        if os.path.isfile(path):
            return FileStat(exists=True, size=os.path.getsize(path))
        return FileStat(exists=True, size=None)

    def read(self, path: str, max_bytes: int) -> bytes | None:
        try:
            with open(path, "rb") as handle:
                return handle.read(max_bytes)
        except OSError:
            return None


class CompositeGateTests(unittest.TestCase):
    def setUp(self):
        self.probe = FakeProbe(files={"a.txt": b"x"})
        self.decisions = FakeDecisions(artifacts={"paper": True})

    def test_flat_list_is_implicit_all(self):
        report = evaluate_all(
            ["file:a.txt exists", "artifact:paper approved"],
            probe=self.probe,
            decisions=self.decisions,
        )
        self.assertEqual(report.verdict, acceptance.VERDICT_HOLDS)
        self.assertTrue(report.all_hold)
        self.assertEqual(report.unchecked, [])

    def test_all_with_unknown_is_unverified(self):
        report = evaluate_gate(
            ["file:a.txt exists", "file:missing.txt exists"],
            probe=self.probe,
        )
        self.assertTrue(report.leaves[0].holds)
        self.assertTrue(report.leaves[1].checked and not report.leaves[1].holds)
        self.assertEqual(report.verdict, acceptance.VERDICT_NOT_HOLDS)

    def test_all_with_unknown_and_true_is_unverified(self):
        report = evaluate_gate(
            {"all": ["file:a.txt exists", "artifact:paper approved"]},
            probe=self.probe,  # 未提供 decisions → 第二个条件 UNVERIFIED
        )
        self.assertEqual(report.verdict, acceptance.VERDICT_UNVERIFIED)
        self.assertEqual(report.unchecked, ["artifact:paper approved"])
        self.assertFalse(report.all_hold)

    def test_any_true_short_circuits_unknown(self):
        report = evaluate_gate(
            {"any": ["file:a.txt exists", "artifact:paper approved"]},
            probe=self.probe,  # TRUE OR UNKNOWN = TRUE
        )
        self.assertEqual(report.verdict, acceptance.VERDICT_HOLDS)

    def test_any_all_false_is_not_holds(self):
        report = evaluate_gate(
            {"any": ["file:missing.txt exists", "file:also-missing.txt exists"]},
            probe=self.probe,
        )
        self.assertEqual(report.verdict, acceptance.VERDICT_NOT_HOLDS)

    def test_any_false_with_unknown_is_unverified(self):
        report = evaluate_gate(
            {"any": ["file:missing.txt exists", "artifact:paper approved"]},
            probe=self.probe,
        )
        self.assertEqual(report.verdict, acceptance.VERDICT_UNVERIFIED)

    def test_nested_spec(self):
        report = evaluate_gate(
            {
                "all": [
                    "file:a.txt exists",
                    {"any": ["artifact:paper approved", "review:critique concluded"]},
                ]
            },
            probe=self.probe,
            decisions=FakeDecisions(artifacts={"paper": False}),  # FALSE OR UNKNOWN = UNKNOWN
        )
        self.assertEqual(report.verdict, acceptance.VERDICT_UNVERIFIED)
        self.assertEqual(len(report.leaves), 3)

    def test_all_hold_stricter_than_verdict_with_any(self):
        report = evaluate_gate(
            {"any": ["file:a.txt exists", "file:missing.txt exists"]},
            probe=self.probe,
        )
        self.assertEqual(report.verdict, acceptance.VERDICT_HOLDS)
        self.assertFalse(report.all_hold)  # 一个叶子没 hold，严格口径不通过

    def test_invalid_specs_raise(self):
        for bad in ([], {"all": [], "any": []}, {"both": ["x"]}, {"all": "not-a-list"}, 42):
            with self.assertRaises(GateSpecError):
                evaluate_gate(bad)

    def test_unchecked_list_reports_exactly_the_unverified_leaves(self):
        report = evaluate_all(
            ["file:a.txt exists", "tests_passed:x", "model:y"],
            probe=self.probe,
            tests=FakeTests(),
        )
        self.assertEqual(report.unchecked, ["tests_passed:x", "model:y"])


if __name__ == "__main__":
    unittest.main()
