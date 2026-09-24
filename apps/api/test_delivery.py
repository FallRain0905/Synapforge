"""阶段 8 交付契约测试：装配、检查、提交包冻结与跨部署恢复。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.contracts import (
    ArtifactCreate,
    DeliveryBundleRequest,
    DeliveryChecklistRequest,
    EvidenceCreate,
    ReviewCreate,
    ReviewerKind,
)
from app.store import Store


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


def approved_document(store: Store, project_id, name: str, artifact_type: str, text: str):
    """创建并走完草稿→提交→人工批准的成果物。"""

    artifact = store.create_artifact(
        project_id,
        ArtifactCreate(name=name, artifact_type=artifact_type, description=name, content_hash=None),
        created_by="member-001",
    )
    store.store_artifact_content(artifact.id, text.encode("utf-8"))
    store.create_evidence(
        project_id, EvidenceCreate(claim=f"{name} 可复算", evidence_type="artifact", artifact_id=artifact.id)
    )
    store.submit_artifact_for_review(artifact.id, actor="member-001")
    store.create_review(
        project_id,
        ReviewCreate(
            target_type="artifact",
            target_id=artifact.id,
            verdict="APPROVED",
            summary=f"批准 {name}",
            reviewer="member-001",
            reviewer_kind=ReviewerKind.MEMBER,
        ),
    )
    return store.get_artifact(artifact.id)


class DeliveryAssemblyTests(unittest.TestCase):
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

    def test_assembly_excludes_unapproved_artifacts(self) -> None:
        approved_document(self.store, self.project.id, "MODELING_REPORT.md", "model_spec", "# 模型\n约束残差为 0。\n")
        self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="DRAFT_NOTES.md", artifact_type="paper_source", description="未批准草稿"),
            created_by="member-001",
        )
        assembly = main.get_delivery_assembly(self.project.id)
        self.assertTrue(assembly["generatable"])
        types = {section["artifact_type"] for section in assembly["sections"]}
        self.assertIn("model_spec", types)
        # 未批准成果物被排除且显式列出。
        excluded = {item["name"] for item in assembly["excluded_unapproved"]}
        self.assertIn("DRAFT_NOTES.md", excluded)
        self.assertNotIn("DRAFT_NOTES.md", {section["source_name"] for section in assembly["sections"]})

    def test_assembly_reports_blocked_sections_and_traceability(self) -> None:
        approved_document(self.store, self.project.id, "PAPER.md", "paper_source", "# 论文\n## 摘要\n结论可用。\n")
        assembly = main.get_delivery_assembly(self.project.id)
        section = next(item for item in assembly["sections"] if item["title"] == "摘要")
        self.assertEqual(section["approved_by"], "member-001")
        self.assertTrue(section["content_hash"])
        # 缺少已批准素材的章节必须给出可读原因。
        self.assertTrue(assembly["blocked"])
        self.assertTrue(any("missing_approved_source" in reason for reason in assembly["blocked_reasons"]))

    def test_slides_generated_from_approved_sources(self) -> None:
        approved_document(self.store, self.project.id, "MODELING_REPORT.md", "model_spec", "# 模型\n结论：成本下降 3%。\n")
        response = main.get_delivery_slides(self.project.id)
        body = response.body.decode("utf-8")
        self.assertIn("marp: true", body)
        self.assertIn("## 模型建立与求解", body)
        self.assertIn("批准人 member-001", body)


class DeliveryChecklistTests(unittest.TestCase):
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

    def _check(self, text: str):
        return main.run_delivery_checklist(self.project.id, DeliveryChecklistRequest(paper_text=text))

    def test_checklist_flags_dangling_citations_and_labels(self) -> None:
        report = self._check("# 论文\n\\cite{ref1} \\eqref{eq:1}\n")
        by_code = {item["code"]: item for item in report["checks"]}
        self.assertEqual(by_code["citation_keys"]["status"], "fail")
        self.assertEqual(by_code["equation_labels"]["status"], "fail")
        self.assertFalse(report["passed"])
        self.assertGreaterEqual(report["blocking_count"], 2)

    def test_checklist_flags_anonymity_leak(self) -> None:
        report = self._check("# 论文\n学校：某某大学 队号：2026001\n")
        by_code = {item["code"]: item for item in report["checks"]}
        self.assertEqual(by_code["anonymity"]["status"], "fail")
        self.assertIn("anonymity", by_code)

    def test_checklist_reports_missing_attachments(self) -> None:
        report = self._check("# 论文\n")
        by_code = {item["code"]: item for item in report["checks"]}
        self.assertEqual(by_code["attachments"]["status"], "fail")
        self.assertIn("compiled_pdf", by_code["attachments"]["detail"])

    def test_checklist_passes_with_complete_deliverables(self) -> None:
        approved_document(self.store, self.project.id, "PAPER.md", "paper_source", "# 论文\n")
        approved_document(self.store, self.project.id, "result1.xlsx", "result_table", "问题一\n")
        approved_document(self.store, self.project.id, "main.pdf", "compiled_pdf", "%PDF-1.4")
        report = self._check("# 论文\n\\cite{r1}\n\\bibitem{r1} 文献\n\\label{eq:1} \\eqref{eq:1}\n")
        by_code = {item["code"]: item for item in report["checks"]}
        self.assertEqual(by_code["attachments"]["status"], "pass")
        self.assertEqual(by_code["citation_keys"]["status"], "pass")
        self.assertEqual(by_code["equation_labels"]["status"], "pass")
        self.assertTrue(report["passed"])


class SubmissionBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = Store(self.root / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def _seed_approved(self):
        approved_document(self.store, self.project.id, "MODELING_REPORT.md", "model_spec", "# 模型\n结论：可行。\n")
        approved_document(self.store, self.project.id, "result1.xlsx", "result_table", "问题一\n")

    def test_bundle_requires_approved_sources(self) -> None:
        # 用全新项目：种子项目自带已批准素材，无法验证"无批准素材"路径。
        self.project = self.store.create_project(
            main.ProjectCreate(name="空交付项目", competition_pack="cumcm-2026", problem_code="C", description="x")
        )
        with self.assertRaises(HTTPException) as caught:
            main.create_delivery_submission_bundle(
                self.project.id, DeliveryBundleRequest(idempotency_key="bundle-empty-0001"), make_request()
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("submission_bundle_no_approved_sources", str(caught.exception.detail))

    def test_bundle_assembles_and_submits_for_freeze(self) -> None:
        self._seed_approved()
        result = main.create_delivery_submission_bundle(
            self.project.id, DeliveryBundleRequest(idempotency_key="bundle-00000001"), make_request()
        )
        self.assertEqual(result["layer"], "submitted")
        self.assertEqual(result["status"], "PENDING_REVIEW")
        self.assertGreaterEqual(result["source_count"], 2)
        self.assertTrue(result["manifest_hash"])

        # 提交待审 → 人工批准后冻结为不可变版本。
        self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="artifact",
                target_id=__import__("uuid").UUID(result["artifact_id"]),
                verdict="APPROVED",
                summary="批准提交包",
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        frozen = self.store.get_artifact(__import__("uuid").UUID(result["artifact_id"]))
        self.assertEqual(str(frozen.status), "APPROVED")
        self.assertTrue(frozen.immutable)

    def test_bundle_verification_detects_hash_drift(self) -> None:
        self._seed_approved()
        result = main.create_delivery_submission_bundle(
            self.project.id, DeliveryBundleRequest(idempotency_key="bundle-drift-0001"), make_request()
        )
        ok = main.verify_delivery_submission_bundle(
            self.project.id, __import__("uuid").UUID(result["artifact_id"])
        )
        self.assertTrue(ok["verified"])
        self.assertEqual(ok["mismatches"], [])

        # 改动一个已批准来源的内容哈希 → 校验必须报出漂移（模拟素材被替换）。
        source = next(
            item for item in self.store.list_artifacts(self.project.id) if item.name == "MODELING_REPORT.md"
        )
        self.store.db.execute("UPDATE artifacts SET content_hash = ? WHERE id = ?", ("deadbeef" * 8, str(source.id)))
        self.store.db.commit()
        drift = main.verify_delivery_submission_bundle(
            self.project.id, __import__("uuid").UUID(result["artifact_id"])
        )
        self.assertFalse(drift["verified"])
        self.assertTrue(any("hash_changed" in item for item in drift["mismatches"]))

    def test_bundle_restores_on_second_deployment(self) -> None:
        self._seed_approved()
        result = main.create_delivery_submission_bundle(
            self.project.id, DeliveryBundleRequest(idempotency_key="bundle-restore-1"), make_request()
        )
        second = Store(self.root / "second-deployment.db", object_store=main.create_object_store(self.root / "second-objects"))
        try:
            report = main.verify_delivery_submission_bundle(
                self.project.id, __import__("uuid").UUID(result["artifact_id"]), restore_check=False
            )
            self.assertTrue(report["verified"])

            from app import delivery as delivery_module

            restore_report = delivery_module.verify_submission_bundle(
                self.store,
                self.project.id,
                __import__("uuid").UUID(result["artifact_id"]),
                restore_store=second,
            )
            self.assertIsNotNone(restore_report["restore"])
            self.assertTrue(restore_report["restore"]["restored"])
            self.assertEqual(restore_report["restore"]["checked"], restore_report["checked_sources"])
        finally:
            second.close()

    def test_bundle_is_idempotent(self) -> None:
        self._seed_approved()
        first = main.create_delivery_submission_bundle(
            self.project.id, DeliveryBundleRequest(idempotency_key="bundle-idem-0001"), make_request()
        )
        second = main.create_delivery_submission_bundle(
            self.project.id, DeliveryBundleRequest(idempotency_key="bundle-idem-0001"), make_request()
        )
        self.assertEqual(first["artifact_id"], second["artifact_id"])


if __name__ == "__main__":
    unittest.main()