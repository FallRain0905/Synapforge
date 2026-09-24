"""技能词表归一化与版本匹配的单元测试（AIP-1a，见 docs/AIP_1_PLAN.md §3）。

覆盖：大小写/分隔符归一化、非法值拒绝、授权范围识别、`名字@约束` 拆分、
版本比较（≥ / 缺失段 / `*` / 缺失版本 / 认不出的约束按字面量）、能力卡版本汇总。
"""

from __future__ import annotations

import unittest

from app import skill_match as sm


class NormalizeSkillTests(unittest.TestCase):
    def test_case_and_separator_folding(self) -> None:
        for raw in ["Python", " python ", "PYTHON"]:
            self.assertEqual(sm.normalize_skill_id(raw), "python")
        self.assertEqual(sm.normalize_skill_id("doc__write"), "doc-write")
        self.assertEqual(sm.normalize_skill_id("code.python"), "code.python")
        self.assertEqual(sm.normalize_skill_id("R-Language"), "r-language")
        self.assertEqual(sm.normalize_skill_id("codex-v2"), "codex-v2")

    def test_invalid_values_rejected(self) -> None:
        for raw in ["", "   ", None, "-", ".", "a", "有中文", "skill/name", "skill name/"]:
            self.assertIsNone(sm.normalize_skill_id(raw), raw)

    def test_scope_tokens_recognized(self) -> None:
        for value in ["task.claim", "TASK.CLAIM", "artifact.write", "session.run", "terminal.resize"]:
            self.assertTrue(sm.is_scope_token(value), value)
        for value in ["codex", "python", "doc.write", "r-language"]:
            self.assertFalse(sm.is_scope_token(value), value)

    def test_requirement_normalization_keeps_constraint(self) -> None:
        self.assertEqual(sm.normalize_requirement("Python"), "python")
        self.assertEqual(sm.normalize_requirement("Codex@>=1.2"), "codex@1.2")
        self.assertEqual(sm.normalize_requirement("codex@*"), "codex@*")
        self.assertIsNone(sm.normalize_requirement("有中文"))
        self.assertEqual(sm.normalize_capability_list(["Python", "python", "Codex@>=1.2", "有中文"]), ["python", "codex@1.2"])


class VersionMatchTests(unittest.TestCase):
    def test_no_constraint_accepts_any_version(self) -> None:
        self.assertTrue(sm.version_satisfies("", ""))
        self.assertTrue(sm.version_satisfies("", "1.2.3"))
        self.assertTrue(sm.version_satisfies("*", ""))
        self.assertTrue(sm.version_satisfies("*", "9.9"))

    def test_minimum_version_semantics(self) -> None:
        self.assertTrue(sm.version_satisfies("1.2", "1.2"))
        self.assertTrue(sm.version_satisfies("1.2", "1.2.0"))
        self.assertTrue(sm.version_satisfies("1.2", "1.10"))
        self.assertTrue(sm.version_satisfies("1.2.3", "1.2.4"))
        self.assertFalse(sm.version_satisfies("1.2", "1.1.9"))
        self.assertFalse(sm.version_satisfies("1.2.3", "1.2"))
        self.assertTrue(sm.version_satisfies("1.2", "2"))

    def test_missing_provided_version_fails_minimum(self) -> None:
        self.assertFalse(sm.version_satisfies("1.2", ""))
        self.assertTrue(sm.version_satisfies("", ""))

    def test_unparsed_constraint_compares_literally(self) -> None:
        self.assertTrue(sm.version_satisfies("^1.2", "^1.2", unparsed=True))
        self.assertFalse(sm.version_satisfies("^1.2", "1.2.9", unparsed=True))

    def test_split_requirement_marks_unparsed(self) -> None:
        self.assertEqual(sm.split_requirement("codex"), ("codex", "", False))
        self.assertEqual(sm.split_requirement("codex@>=1.2"), ("codex", "1.2", False))
        self.assertEqual(sm.split_requirement("codex@*"), ("codex", "*", False))
        name, constraint, unparsed = sm.split_requirement("codex@^1.2")
        self.assertEqual((name, constraint), ("codex", "^1.2"))
        self.assertTrue(unparsed)


class SatisfiesTests(unittest.TestCase):
    def test_requirement_satisfied_by_versioned_provider(self) -> None:
        provided = {"python": "3.11.4", "codex": "0.9.3"}
        self.assertTrue(sm.satisfies("python", provided))
        self.assertTrue(sm.satisfies("Python", provided))
        self.assertTrue(sm.satisfies("codex@>=0.9", provided))
        self.assertFalse(sm.satisfies("codex@>=1.0", provided))
        self.assertFalse(sm.satisfies("r-language", provided))

    def test_unsatisfied_requirements_lists_original_wording(self) -> None:
        provided = {"python": "3.11.4"}
        missing = sm.unsatisfied_requirements(["Python", "codex@>=1.0", "有中文"], provided)
        self.assertEqual(missing, ["codex@>=1.0", "有中文"])

    def test_card_versions_picks_highest_duplicate(self) -> None:
        cards = [
            {"skill": "Python", "version": "3.10"},
            {"skill": "python", "version": "3.12.1"},
            {"skill": "codex", "version": ""},
            {"skill": "有中文", "version": "1.0"},
        ]
        self.assertEqual(sm.card_versions(cards), {"python": "3.12.1", "codex": ""})


if __name__ == "__main__":
    unittest.main()