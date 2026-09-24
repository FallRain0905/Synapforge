"""竞赛 pack HTTP 端点契约测试（阶段 6 数学建模模板接入）。

沿用项目既有测试风格：替换 `main.store` 后直接调用路由函数，避免启动真实服务。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app import main, pack_api
from app.contracts import CompetitionPackApplyRequest, CompetitionPackReviewRequest
from app.store import Store


def make_request(method: str = "POST", path: str = "/api/projects/test/competition-pack/apply") -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": []})


class CompetitionPackApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # ---- 目录与详情 -------------------------------------------------------

    def test_list_packs_exposes_cumcm_summary(self) -> None:
        packs = main.list_competition_packs()
        self.assertTrue(packs)
        cumcm = next(item for item in packs if item["pack_id"] == "cumcm")
        self.assertEqual(cumcm["questions"], [1, 2, 3, 4])
        self.assertEqual(cumcm["template_count"], 9)
        self.assertGreater(cumcm["dag_task_count"], 0)
        self.assertIn("problem_analysis", cumcm["required_artifacts"])

    def test_pack_detail_includes_templates_dag_and_rules(self) -> None:
        detail = main.get_competition_pack("cumcm")
        self.assertEqual(detail["pack_id"], "cumcm")
        self.assertEqual(len(detail["templates"]), 9)
        self.assertTrue(detail["dag"])
        self.assertIn("required_artifacts", detail["validation_rules"])
        self.assertTrue(detail["upgrade_rules"])
        # 模板占位符已在详情中暴露，便于前端渲染表单。
        modeling = next(item for item in detail["templates"] if item["template_id"] == "modeling_report")
        self.assertIn("questions_block", modeling["placeholders"])

    def test_unknown_pack_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.get_competition_pack("no-such-pack")
        self.assertEqual(caught.exception.status_code, 404)

    def test_template_scaffold_export_is_downloadable(self) -> None:
        response = main.get_competition_pack_template("cumcm", "modeling_report")
        body = response.body.decode("utf-8")
        self.assertIn("{{questions_block}}", body)
        self.assertIn("MODELING_REPORT.md", response.headers["content-disposition"])

    def test_unknown_template_returns_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.get_competition_pack_template("cumcm", "no-such-template")
        self.assertEqual(caught.exception.status_code, 404)

    # ---- 项目视角 ---------------------------------------------------------

    def test_project_pack_reports_materialization_progress(self) -> None:
        before = main.get_project_competition_pack(self.project.id)
        self.assertEqual(before["competition_pack"], self.project.competition_pack)
        self.assertFalse(before["materialization"]["materialized"])
        # 种子项目自带少量同名成果物，因此进度不为零但未完成物化。
        self.assertLess(
            before["materialization"]["planned_present"], before["materialization"]["planned_total"]
        )

        main.apply_project_competition_pack(
            self.project.id,
            CompetitionPackApplyRequest(idempotency_key="pack-apply-progress-1"),
            make_request(),
        )
        after = main.get_project_competition_pack(self.project.id)
        self.assertEqual(after["materialization"]["task_present"], after["materialization"]["task_total"])
        self.assertGreater(after["materialization"]["progress"], 0.5)

    def test_apply_creates_tasks_and_scaffolds_and_is_idempotent(self) -> None:
        first = main.apply_project_competition_pack(
            self.project.id,
            CompetitionPackApplyRequest(idempotency_key="pack-apply-once-001"),
            make_request(),
        )
        self.assertGreater(first["created_task_count"], 0)
        self.assertGreater(first["created_artifact_count"], 0)

        second = main.apply_project_competition_pack(
            self.project.id,
            CompetitionPackApplyRequest(idempotency_key="pack-apply-twice-001"),
            make_request(),
        )
        self.assertEqual(second["created_task_count"], 0)
        self.assertEqual(second["created_artifact_count"], 0)

        # 同幂等键重放返回原响应。
        replay = main.apply_project_competition_pack(
            self.project.id,
            CompetitionPackApplyRequest(idempotency_key="pack-apply-once-001"),
            make_request(),
        )
        self.assertEqual(replay["created_task_count"], first["created_task_count"])

    def test_apply_requires_idempotency_key(self) -> None:
        with self.assertRaisesRegex(HTTPException, "idempotency_key_required"):
            main.apply_project_competition_pack(
                self.project.id, CompetitionPackApplyRequest(), make_request()
            )

    def test_apply_question_subset_creates_only_selected_branch(self) -> None:
        result = main.apply_project_competition_pack(
            self.project.id,
            CompetitionPackApplyRequest(questions=[2], idempotency_key="pack-apply-q2-0001"),
            make_request(),
        )
        self.assertEqual(result["questions"], [2])
        self.assertFalse([task for task in result["tasks"] if task["question"] == 1])

    def test_apply_rejects_unknown_project(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.apply_project_competition_pack(
                uuid4(), CompetitionPackApplyRequest(idempotency_key="pack-apply-missing-1"), make_request()
            )
        self.assertEqual(caught.exception.status_code, 404)

    # ---- 校验与模板导出 ---------------------------------------------------

    def test_validate_reports_missing_required_artifacts_before_apply(self) -> None:
        report = main.validate_project_competition_pack(self.project.id)
        self.assertIn(report["status"], {"BLOCKED", "NEEDS_REVISION"})
        self.assertTrue(report["missing_artifacts"])

    def test_validate_counts_artifacts_but_not_empty_scaffolds(self) -> None:
        main.apply_project_competition_pack(
            self.project.id,
            CompetitionPackApplyRequest(idempotency_key="pack-validate-0001"),
            make_request(),
        )
        report = main.validate_project_competition_pack(self.project.id)
        codes = {finding["code"] for finding in report["findings"]}
        # 骨架已登记：不应再报"必需成果物缺失"，也不应产生 major 误报。
        self.assertNotIn("missing_required_artifact", codes)
        self.assertEqual(report["missing_artifacts"], [])
        self.assertEqual(report["status"], "PASS_WITH_ASSUMPTIONS")
        # 未填写的骨架可读但不算交付内容，因此不为其记四问覆盖。
        self.assertEqual(report["coverage"]["per_artifact"]["result_table"], [])
        self.assertEqual(report["coverage"]["questions_covered"], [])

    def test_filled_scaffold_is_validated_as_deliverable(self) -> None:
        main.apply_project_competition_pack(
            self.project.id,
            CompetitionPackApplyRequest(idempotency_key="pack-fill-000001"),
            make_request(),
        )
        # 填写建模报告：写入含四问标记的真实内容后，该成果物不再算空骨架。
        artifact = next(item for item in self.store.list_artifacts(self.project.id) if item.name == "MODELING_REPORT.md")
        content = (
            "# 模型报告\n\n## 模型建立\n## 求解思路\n## 结果预期\n\n"
            "问题一 问题二 问题三 问题四\n" + "正文内容。" * 600
        ).encode("utf-8")
        self.store.store_artifact_content(artifact.id, content)

        report = main.validate_project_competition_pack(self.project.id)
        self.assertIn(1, report["coverage"]["per_artifact"]["model_spec"])
        self.assertEqual(report["coverage"]["per_artifact"]["model_spec"], [1, 2, 3, 4])

    def test_export_rendered_template_for_project(self) -> None:
        response = main.export_project_competition_pack_template(
            self.project.id, "paper_outline", questions="1,2"
        )
        body = response.body.decode("utf-8")
        self.assertNotIn("{{", body)
        self.assertIn(self.project.name, body)
        self.assertEqual(response.headers["x-pack-id"], "cumcm")
        self.assertEqual(response.headers["x-pack-version"], "1.1.0")

    def test_export_rejects_invalid_questions_query(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.export_project_competition_pack_template(self.project.id, "paper_outline", questions="1,x")
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()

class CompetitionPackReviewTests(unittest.TestCase):
    """机器 Review / 信息边界 Gate：把 pack 校验落成平台 Review + Gate。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        # 用全新项目而不是种子项目：种子自带 audit_report，会掩盖"缺必需成果物"的发现。
        self.project = self.store.create_project(
            main.ProjectCreate(
                name="模板审查项目",
                competition_pack="cumcm-2026",
                problem_code="C",
                description="机器 Review 端到端",
            )
        )
        main.apply_project_competition_pack(
            self.project.id,
            CompetitionPackApplyRequest(idempotency_key="pack-review-apply1"),
            make_request(),
        )

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def _review(self, **kwargs):
        payload = CompetitionPackReviewRequest(**kwargs)
        return main.review_project_competition_pack(self.project.id, payload)

    def test_machine_review_creates_review_and_gate_for_major_findings(self) -> None:
        result = self._review(idempotency_key="pack-review-000001")
        self.assertTrue(result["created"])
        self.assertEqual(result["verdict"], "NEEDS_REVISION")
        self.assertEqual(result["target_type"], "artifact")
        self.assertTrue(result["findings"])

        review = next(item for item in self.store.list_reviews(self.project.id) if str(item.id) == result["review_id"])
        self.assertEqual(str(review.verdict), "NEEDS_REVISION")
        self.assertEqual(str(review.reviewer_kind), "agent")
        codes = {finding["code"] for finding in review.findings}
        self.assertTrue(codes)

        gates = self.store.list_gates(self.project.id)
        target_gate = [gate for gate in gates if str(gate.target_id) == result["target_id"]]
        self.assertTrue(target_gate)
        self.assertEqual(str(target_gate[0].status), "FAILED")

    def test_machine_review_same_idempotency_key_returns_same_review(self) -> None:
        first = self._review(idempotency_key="pack-review-idem-01")
        second = self._review(idempotency_key="pack-review-idem-01")
        self.assertEqual(first["review_id"], second["review_id"])

    def test_machine_review_never_auto_approves_clean_project(self) -> None:
        result = self._review(scope="information_boundary", idempotency_key="pack-review-clean-1")
        # 信息边界范围下没有边界类发现：不得自动批准，也不创建 Review。
        self.assertFalse(result["created"])
        self.assertEqual(result["reason"], "machine_review_clean_requires_human_approval")
        self.assertIsNone(result["verdict"])

    def test_information_boundary_scope_only_reports_boundary_codes(self) -> None:
        result = self._review(scope="information_boundary", idempotency_key="pack-review-scope-1")
        for finding in result["findings"]:
            self.assertIn(finding["code"], pack_api_boundary_codes())

    def test_machine_review_rejects_invalid_scope(self) -> None:
        # 契约层用 Literal 拦住非法 scope；服务层也保留自己的守卫。
        with self.assertRaises(ValidationError):
            CompetitionPackReviewRequest(scope="not-a-scope")
        with self.assertRaises(pack_api.MachineReviewError) as caught:
            pack_api.create_machine_review(
                self.store,
                self.project,
                pack_api.pack_for_project(self.project),
                scope="not-a-scope",
            )
        self.assertEqual(caught.exception.code, "machine_review_scope_invalid")

    def test_machine_review_explicit_artifact_target_is_used(self) -> None:
        artifact = next(item for item in self.store.list_artifacts(self.project.id) if item.artifact_type == "result_table")
        result = self._review(target_type="artifact", target_id=artifact.id, idempotency_key="pack-review-target-1")
        self.assertTrue(result["created"])
        self.assertEqual(result["target_type"], "artifact")
        self.assertEqual(result["target_id"], str(artifact.id))

    def test_machine_review_requires_existing_target(self) -> None:
        empty = self.store.create_project(
            main.ProjectCreate(name="空项目模板审查", competition_pack="cumcm-2026", problem_code="C", description="no targets")
        )
        with self.assertRaises(HTTPException) as caught:
            main.review_project_competition_pack(
                empty.id, CompetitionPackReviewRequest(idempotency_key="pack-review-no-target")
            )
        self.assertEqual(caught.exception.status_code, 404)


def pack_api_boundary_codes() -> set[str]:
    return set(pack_api.BOUNDARY_CODES)
