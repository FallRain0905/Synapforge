"""阶段 7 最小切片：文档三层版本（草稿/提交/批准）与证据链契约测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.contracts import (
    ArtifactCreate,
    DocumentReviseRequest,
    DocumentSubmitRequest,
    EvidenceCreate,
    ReviewCreate,
    ReviewerKind,
)
from app.store import Store


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


class DocumentLayerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]
        self.document = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(
                name="PAPER_OUTLINE.md",
                artifact_type="paper_source",
                description="论文草稿",
                git_commit="abc1234",
            ),
            created_by="member-001",
            created_by_kind="member",
        )

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def _evidence(self, claim: str = "问题三结论可复算"):
        return self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim=claim, evidence_type="artifact", artifact_id=self.document.id),
        )

    def _submit(self):
        return main.submit_document_revision(
            self.project.id, self.document.id, DocumentSubmitRequest(), make_request()
        )

    def _review(self, verdict: str, summary: str = "复核结论"):
        return self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="artifact",
                target_id=self.document.id,
                verdict=verdict,
                summary=summary,
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )

    def test_layer_definitions_expose_three_layers_and_rules(self) -> None:
        layers = main.get_document_layers()
        self.assertEqual([item["layer"] for item in layers["layers"]], ["draft", "submitted", "approved"])
        self.assertTrue(layers["rules"]["submit_requires_evidence"])
        self.assertTrue(layers["rules"]["approval_requires_human_review"])
        self.assertTrue(layers["rules"]["draft_cannot_be_downstream_input"])
        self.assertEqual(layers["status_to_layer"]["PENDING_REVIEW"], "submitted")

    def test_new_document_starts_as_draft(self) -> None:
        timeline = main.get_document_timeline(self.project.id, self.document.id)
        self.assertEqual(timeline["current_layer"], "draft")
        self.assertEqual(timeline["revision_count"], 1)
        self.assertTrue(timeline["revisions"][0]["editable"])
        self.assertFalse(timeline["revisions"][0]["downstream_allowed"])

    def test_submit_requires_evidence(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            self._submit()
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("document_evidence_required", str(caught.exception.detail))
        self.assertEqual(main.get_document_timeline(self.project.id, self.document.id)["current_layer"], "draft")

    def test_submit_with_evidence_freezes_document(self) -> None:
        self._evidence()
        result = self._submit()
        self.assertEqual(result["layer"], "submitted")
        self.assertTrue(result["changed"])

        timeline = main.get_document_timeline(self.project.id, self.document.id)
        latest = timeline["revisions"][-1]
        self.assertEqual(latest["layer"], "submitted")
        self.assertFalse(latest["editable"])
        self.assertFalse(latest["downstream_allowed"])
        self.assertEqual(len(latest["evidence"]), 1)

    def test_submit_is_idempotent_for_already_submitted(self) -> None:
        self._evidence()
        self._submit()
        again = self._submit()
        self.assertFalse(again["changed"])

    def test_submit_rejects_unlinked_evidence(self) -> None:
        other = self.store.create_artifact(
            self.project.id, ArtifactCreate(name="OTHER.md", artifact_type="paper_source"), created_by="member-001"
        )
        foreign = self.store.create_evidence(
            self.project.id, EvidenceCreate(claim="别的证据", evidence_type="artifact", artifact_id=other.id)
        )
        with self.assertRaises(HTTPException) as caught:
            main.submit_document_revision(
                self.project.id,
                self.document.id,
                DocumentSubmitRequest(evidence_ids=[foreign.id]),
                make_request(),
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_agent_cannot_approve_document(self) -> None:
        self._evidence()
        self._submit()
        with self.assertRaises(PermissionError):
            self.store.create_review(
                self.project.id,
                ReviewCreate(
                    target_type="artifact",
                    target_id=self.document.id,
                    verdict="APPROVED",
                    summary="agent 尝试批准",
                    reviewer="modular-audit",
                    reviewer_kind=ReviewerKind.AGENT,
                ),
            )

    def test_human_review_approves_and_unlocks_downstream(self) -> None:
        self._evidence()
        self._submit()
        self._review("APPROVED", "人工批准论文草稿")
        timeline = main.get_document_timeline(self.project.id, self.document.id)
        latest = timeline["revisions"][-1]
        self.assertEqual(latest["layer"], "approved")
        self.assertTrue(latest["downstream_allowed"])
        self.assertTrue(latest["immutable"])
        self.assertEqual(latest["traceability"]["approved_by"], "member-001")

    def test_submitted_document_is_frozen_until_reviewed(self) -> None:
        self._evidence()
        self._submit()
        submitted = self.store.get_artifact(self.document.id)
        self.assertEqual(str(submitted.status), "PENDING_REVIEW")
        self.assertFalse(submitted.downstream_allowed)

    def test_revision_creates_new_draft_and_keeps_history(self) -> None:
        self._evidence()
        self._submit()
        self._review("NEEDS_REVISION", "需要补灵敏度分析")
        result = main.revise_document_revision(
            self.project.id, self.document.id, DocumentReviseRequest(description="补充灵敏度"), make_request()
        )
        self.assertEqual(result["layer"], "draft")
        self.assertEqual(result["parent_artifact_id"], str(self.document.id))

        timeline = main.get_document_timeline(self.project.id, UUID(result["artifact_id"]))
        self.assertEqual(timeline["revision_count"], 2)
        self.assertEqual([item["layer"] for item in timeline["revisions"]], ["draft", "draft"])
        self.assertEqual(timeline["revisions"][0]["artifact_id"], str(self.document.id))

    def test_revision_does_not_mutate_approved_version(self) -> None:
        self._evidence()
        self._submit()
        self._review("APPROVED", "批准")
        result = main.revise_document_revision(
            self.project.id, self.document.id, DocumentReviseRequest(), make_request()
        )
        approved = self.store.get_artifact(self.document.id)
        self.assertEqual(str(approved.status), "APPROVED")
        self.assertTrue(approved.immutable)
        self.assertNotEqual(result["artifact_id"], str(self.document.id))

    def test_timeline_is_traceable_to_member_kind_and_git_commit(self) -> None:
        evidence = self._evidence()
        self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="来自运行的结果", evidence_type="external_source", source_ref="run-log-7"),
        )
        main.submit_document_revision(
            self.project.id,
            self.document.id,
            DocumentSubmitRequest(evidence_ids=[evidence.id]),
            make_request(),
        )
        timeline = main.get_document_timeline(self.project.id, self.document.id)
        trace = timeline["revisions"][-1]["traceability"]
        self.assertEqual(trace["created_by"], "member-001")
        self.assertEqual(trace["created_by_kind"], "member")
        self.assertEqual(trace["git_commit"], "abc1234")

    def test_evidence_chain_query_lists_claims(self) -> None:
        self._evidence("问题一约束残差为 0")
        self._evidence("问题二成本下降 3.1%")
        chain = main.get_document_evidence(self.project.id, self.document.id)
        self.assertEqual(chain["evidence_count"], 2)
        claims = {entry["claim"] for entry in chain["evidence"]}
        self.assertIn("问题一约束残差为 0", claims)
        self.assertEqual(chain["evidence"][0]["artifact_id"], str(self.document.id))

    def test_timeline_rejects_unknown_document(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.get_document_timeline(self.project.id, uuid4())
        self.assertEqual(caught.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()