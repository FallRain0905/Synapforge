"""CUMCM 领域包（阶段 6 数学建模模板）契约测试。

覆盖 pack 加载与渲染、四问 DAG 物化、幂等性、以及校验器的四问覆盖与
官方结果表规则。
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from packages.competition_packs import (
    CompetitionPackError,
    PackMaterializer,
    PackValidator,
    load_pack,
    resolve_pack_id,
)
from app.store import Store


PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_variables(pack) -> dict[str, str]:
    return {
        "project_name": "测试项目",
        "competition": "CUMCM",
        "problem_code": "C",
        "problem_id": "2026-C",
        "generated_at": "2026-09-15",
        "pack_version": pack.version,
        "n_questions": 4,
        "questions_block": "### 问题1\n",
    }


def compliant_documents() -> dict[str, str]:
    def markdown(sections, body: int = 600, extra: str = "") -> str:
        return "# 文档\n\n" + "".join(f"## {section}\n" for section in sections) + "\n" + extra + "正文内容。" * body

    questions = "问题一 问题二 问题三 问题四\n"
    documents = {
        "PROBLEM_ANALYSIS.md": markdown(["子问题拆解", "变量与符号", "图表清单"]),
        "MODELING_REPORT.md": markdown(["模型建立", "求解思路", "结果预期"], extra=questions),
        "EXPERIMENT_PLAN.md": markdown(["论证主张映射", "实验块", "灵敏度与鲁棒性"]),
        "AUDIT_REPORT.md": markdown(["约束检查", "灵敏度"], extra=questions),
        "COMP_REVIEW.md": markdown(["独立复算", "结论与限制"]),
        "PAPER.md": markdown(["摘要", "问题重述", "模型建立与求解", "检验", "推广"], body=1200),
        "code/q1_solve.py": "code/q1_solve.py 读取 user_data/ 输出 output/result1.xlsx\n" * 40,
        "DATA_PROFILE.json": json.dumps(
            {"datasets": [{"name": "d", "path": "user_data/d.csv"}], "information_boundary": {"future_data": "deny"}}
        ),
        "PROBLEM_FACTS.json": json.dumps(
            {"_meta": {"problem_id": "2026-C", "source_files": [{"path": "p", "sha256": "a" * 64}]}, "entities": [{"id": "e"}]}
        ),
        "result1.xlsx": "问题一\n" * 80,
        "result2.xlsx": "问题二\n" * 80,
        "result3.xlsx": "问题三\n" * 80,
        "result4.xlsx": "问题四\n" * 80,
    }
    return documents


COMPLIANT_ARTIFACTS = [
    {"name": name, "artifact_type": artifact_type, "content_hash": "a" * 64}
    for artifact_type, name in [
        ("problem_analysis", "PROBLEM_ANALYSIS.md"),
        ("model_spec", "MODELING_REPORT.md"),
        ("experiment_plan", "EXPERIMENT_PLAN.md"),
        ("audit_report", "AUDIT_REPORT.md"),
        ("review_report", "COMP_REVIEW.md"),
        ("paper_source", "PAPER.md"),
        ("result_table", "result1.xlsx"),
        ("result_table", "result2.xlsx"),
        ("result_table", "result3.xlsx"),
        ("result_table", "result4.xlsx"),
        ("code", "code/q1_solve.py"),
        ("data_profile", "DATA_PROFILE.json"),
        ("problem_facts", "PROBLEM_FACTS.json"),
    ]
]


class CumcmPackLoadingTests(unittest.TestCase):
    def test_pack_loads_with_templates_dag_and_questions(self) -> None:
        pack = load_pack("cumcm")
        self.assertEqual(pack.pack_id, "cumcm")
        self.assertEqual(pack.questions(), (1, 2, 3, 4))
        self.assertEqual(len(pack.manifest.templates), 9)
        self.assertTrue(pack.dag())
        self.assertEqual(resolve_pack_id("cumcm-2026"), "cumcm")
        self.assertEqual(PackValidator(pack).validate_pack().status, "PASS")

    def test_templates_only_use_declared_placeholders(self) -> None:
        pack = load_pack("cumcm")
        allowed = set(render_variables(pack))
        for spec in pack.manifest.templates:
            self.assertTrue(set(pack.placeholders(spec.template_id)).issubset(allowed), spec.template_id)

    def test_render_fails_closed_and_json_templates_stay_valid(self) -> None:
        pack = load_pack("cumcm")
        with self.assertRaises(CompetitionPackError) as caught:
            pack.render_template("modeling_report", {"project_name": "x"})
        self.assertEqual(caught.exception.code, "pack_template_variables_missing")

        variables = render_variables(pack)
        for spec in pack.manifest.templates:
            text = pack.render_template(spec.template_id, variables)
            self.assertFalse(PLACEHOLDER.search(text), spec.template_id)
            if spec.filename.endswith(".json"):
                json.loads(text)

    def test_upgrade_plan_reports_compatible_rename(self) -> None:
        plan = load_pack("cumcm").manifest.plan_upgrade("1.0.0")
        self.assertEqual(plan.to_version, "1.1.0")
        self.assertFalse(plan.requires_review)
        self.assertIn("capability_checklist", plan.added_artifacts)
        self.assertIn(("review.md", "COMP_REVIEW.md"), plan.renamed_artifacts)

    def test_unknown_template_is_rejected(self) -> None:
        with self.assertRaises(CompetitionPackError) as caught:
            load_pack("cumcm").template_text("no_such_template")
        self.assertEqual(caught.exception.code, "pack_template_not_found")


class PackMaterializationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = Store(self.root / "platform.db")
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def test_materialize_creates_tasks_with_dependencies_and_scaffolds(self) -> None:
        workspace = self.root / "workspace"
        result = PackMaterializer(self.store).apply(self.project.id, output_dir=workspace)
        self.assertTrue(result.tasks)
        self.assertTrue(result.artifacts)

        tasks = {task.title: task for task in self.store.list_tasks(self.project.id)}
        code_task = tasks["问题一代码与计算"]
        modeling_task = tasks["问题一建模"]
        self.assertIn(modeling_task.id, code_task.dependency_task_ids)

        # 所有模板骨架都会落到工作区，包括项目里已存在同名成果物的模板。
        pack = load_pack("cumcm")
        for spec in pack.manifest.templates:
            self.assertTrue((workspace / spec.filename).is_file(), spec.filename)

    def test_materialize_is_idempotent(self) -> None:
        workspace = self.root / "workspace"
        PackMaterializer(self.store).apply(self.project.id, output_dir=workspace)
        again = PackMaterializer(self.store).apply(self.project.id, output_dir=workspace)
        self.assertEqual(again.created_task_count, 0)
        self.assertEqual(again.created_artifact_count, 0)

    def test_question_subset_only_materializes_selected_questions(self) -> None:
        result = PackMaterializer(self.store).apply(self.project.id, questions=[1], output_dir=self.root / "ws")
        self.assertEqual(result.questions, (1,))
        self.assertFalse([task for task in result.tasks if task.question == 2])


class PackValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = PackValidator(load_pack("cumcm"))

    def test_compliant_project_passes(self) -> None:
        report = self.validator.validate(
            artifacts=COMPLIANT_ARTIFACTS, documents=compliant_documents(), project_id="compliant"
        )
        self.assertEqual(report.status, "PASS")
        self.assertTrue(report.allowed)
        self.assertEqual(report.findings, ())
        self.assertEqual(report.coverage["questions_covered"], [1, 2, 3, 4])

    def test_missing_blocking_artifact_is_fatal(self) -> None:
        artifacts = [item for item in COMPLIANT_ARTIFACTS if item["artifact_type"] != "paper_source"]
        report = self.validator.validate(artifacts=artifacts, documents=compliant_documents())
        self.assertEqual(report.status, "BLOCKED")
        self.assertIn("paper_source", report.missing_artifacts)

    def test_question_coverage_accepts_arabic_and_chinese_markers(self) -> None:
        documents = compliant_documents()
        documents["AUDIT_REPORT.md"] = "# 审计\n\n## 约束检查\n## 灵敏度\n" + "问题1 问题 2 Q3 Question 4\n" + "正文。" * 300
        report = self.validator.validate(artifacts=COMPLIANT_ARTIFACTS, documents=documents)
        self.assertEqual(report.coverage["per_artifact"]["audit_report"], [1, 2, 3, 4])

    def test_missing_question_coverage_is_reported_as_major(self) -> None:
        documents = compliant_documents()
        documents["MODELING_REPORT.md"] = "# 文档\n\n## 模型建立\n## 求解思路\n## 结果预期\n" + "正文。" * 600
        report = self.validator.validate(artifacts=COMPLIANT_ARTIFACTS, documents=documents)
        self.assertEqual(report.status, "NEEDS_REVISION")
        codes = {finding.code for finding in report.findings}
        self.assertIn("question_coverage_incomplete", codes)

    def test_scaffold_is_exempt_from_official_filename_rule(self) -> None:
        # pack 下发的模板骨架（带 template_id 标记）不算交付结果表。
        artifacts = [
            {
                "name": "RESULT_TABLE_TEMPLATE.md",
                "artifact_type": "result_table",
                "content_hash": "b" * 64,
                "data_policy": {"source": "competition_pack", "template_id": "result_table"},
            }
        ]
        report = self.validator.validate(artifacts=artifacts, documents={})
        self.assertNotIn("official_result_filename_mismatch", {finding.code for finding in report.findings})

    def test_pack_managed_result_table_needs_official_name(self) -> None:
        artifacts = [
            {
                "name": "problem_3_results.json",
                "artifact_type": "result_table",
                "content_hash": "c" * 64,
                "data_policy": {"source": "competition_pack"},
            }
        ]
        report = self.validator.validate(artifacts=artifacts, documents={})
        findings = [finding for finding in report.findings if finding.code == "official_result_filename_mismatch"]
        self.assertEqual([finding.severity for finding in findings], ["major"])

    def test_legacy_result_table_is_only_a_minor_note(self) -> None:
        artifacts = [{"name": "problem_3_results.json", "artifact_type": "result_table", "content_hash": "d" * 64}]
        report = self.validator.validate(artifacts=artifacts, documents={})
        findings = [finding for finding in report.findings if finding.code == "official_result_filename_mismatch"]
        self.assertEqual([finding.severity for finding in findings], ["minor"])


if __name__ == "__main__":
    unittest.main()
