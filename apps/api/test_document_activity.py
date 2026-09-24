"""阶段 7：文档评论/建议、快照与「结论-图表-运行-段落」关系契约测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.contracts import (
    AgentRegister,
    ArtifactCreate,
    DocumentCommentRequest,
    DocumentRelationRequest,
    DocumentSnapshotRequest,
    RunCreate,
)
from app.store import Store


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


class DocumentActivityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]
        self.document = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="PAPER_OUTLINE.md", artifact_type="paper_source", description="论文"),
            created_by="member-001",
        )
        self.store.store_artifact_content(self.document.id, "# 论文\n\n## 摘要\n结果如下。\n".encode("utf-8"))
        self.store.register_agent(
            AgentRegister(agent_id="agent-001", display_name="测试 Agent", owner_member_id="member-001")
        )
        self.store.grant_agent_project(
            main.AgentProjectGrant(
                project_id=self.project.id,
                agent_id="agent-001",
                granted_by="member-001",
                capabilities=["run.create"],
            )
        )
        self.figure = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="fig_cost.png", artifact_type="figure", description="成本曲线"),
            created_by="member-001",
        )

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    # ---- 评论与建议 -------------------------------------------------------

    def test_comment_and_suggestion_are_recorded_with_anchor(self) -> None:
        main.add_document_comment(
            self.project.id,
            self.document.id,
            DocumentCommentRequest(body="摘要需要补关键数值", anchor="摘要"),
            make_request(),
        )
        main.add_document_comment(
            self.project.id,
            self.document.id,
            DocumentCommentRequest(body="建议补灵敏度分析", kind="suggestion", anchor="模型建立与求解"),
            make_request(),
        )
        listing = main.list_document_comments(self.project.id, self.document.id)
        self.assertEqual(listing["comment_count"], 1)
        self.assertEqual(listing["suggestion_count"], 1)
        bodies = {item["body"] for item in listing["comments"]}
        self.assertIn("摘要需要补关键数值", bodies)
        anchored = next(item for item in listing["comments"] if item["body"].startswith("摘要需要"))
        self.assertEqual(anchored["anchor"], "摘要")
        self.assertEqual(anchored["layer"], "draft")

    def test_comment_requires_body(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.add_document_comment(
                self.project.id, self.document.id, DocumentCommentRequest(body=" "), make_request()
            )
        self.assertEqual(caught.exception.status_code, 400)

    def test_comment_on_unknown_document_is_404(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.add_document_comment(
                self.project.id, uuid4(), DocumentCommentRequest(body="幽灵文档"), make_request()
            )
        self.assertEqual(caught.exception.status_code, 404)

    # ---- 快照 -------------------------------------------------------------

    def test_snapshot_records_current_revision_hash(self) -> None:
        snapshot = main.create_document_snapshot(
            self.project.id, self.document.id, DocumentSnapshotRequest(label="初稿检查点"), make_request()
        )
        self.assertEqual(snapshot["revision"], 1)
        self.assertEqual(snapshot["label"], "初稿检查点")
        self.assertEqual(snapshot["content_hash"], self.store.get_artifact(self.document.id).content_hash)

        listing = main.list_document_snapshots(self.project.id, self.document.id)
        self.assertEqual(listing["snapshot_count"], 1)
        self.assertEqual(listing["snapshots"][0]["content_hash"], snapshot["content_hash"])

    def test_snapshot_tracks_new_revision_after_revise(self) -> None:
        main.create_document_snapshot(self.project.id, self.document.id, DocumentSnapshotRequest(), make_request())
        revised = main.revise_document_revision(
            self.project.id,
            self.document.id,
            main.DocumentReviseRequest(description="第二稿"),
            make_request(),
        )
        main.create_document_snapshot(
            self.project.id, revised["artifact_id"], DocumentSnapshotRequest(label="二稿"), make_request()
        )
        listing = main.list_document_snapshots(self.project.id, revised["artifact_id"])
        self.assertEqual(listing["snapshot_count"], 1)
        self.assertEqual(listing["snapshots"][0]["revision"], 2)

    # ---- 关系与影响面 -----------------------------------------------------

    def test_link_relation_to_figure_and_lookup_impact(self) -> None:
        main.link_document_relation(
            self.project.id,
            self.document.id,
            DocumentRelationRequest(
                target_type="figure",
                target_id=self.figure.id,
                paragraph="模型建立与求解",
                note="成本曲线支撑结论",
            ),
            make_request(),
        )
        relations = main.list_document_relations(self.project.id, self.document.id)
        self.assertEqual(relations["relation_count"], 1)
        self.assertEqual(relations["relations"][0]["target_type"], "figure")

        impact = main.get_impact_lookup(
            self.project.id, target_type="figure", target_id=str(self.figure.id)
        )
        self.assertEqual(impact["affected_count"], 1)
        self.assertEqual(impact["paragraphs"], ["模型建立与求解"])
        self.assertEqual(impact["affected_artifacts"], [str(self.document.id)])

    def test_link_relation_to_run(self) -> None:
        run = self.store.create_run(
            self.project.id,
            RunCreate(task_id=None, agent_id="agent-001", summary="问题三复算", idempotency_key="run-impact-0001"),
        )
        main.link_document_relation(
            self.project.id,
            self.document.id,
            DocumentRelationRequest(target_type="run", target_id=run.id, paragraph="检验", note="运行支撑"),
            make_request(),
        )
        impact = main.get_impact_lookup(self.project.id, target_type="run", target_id=str(run.id))
        self.assertEqual(impact["affected_count"], 1)
        self.assertEqual(impact["paragraphs"], ["检验"])

    def test_relation_rejects_foreign_target_and_bad_type(self) -> None:
        other_project = self.store.create_project(
            main.ProjectCreate(name="别的项目", competition_pack="cumcm-2026", problem_code="C", description="x")
        )
        foreign = self.store.create_artifact(
            other_project.id, ArtifactCreate(name="other.png", artifact_type="figure"), created_by="member-001"
        )
        with self.assertRaises(HTTPException) as caught:
            main.link_document_relation(
                self.project.id,
                self.document.id,
                DocumentRelationRequest(target_type="figure", target_id=foreign.id, paragraph="结论"),
                make_request(),
            )
        self.assertEqual(caught.exception.status_code, 404)

        with self.assertRaises(HTTPException) as caught:
            main.get_impact_lookup(self.project.id, target_type="not-a-type", target_id=str(self.figure.id))
        self.assertEqual(caught.exception.status_code, 400)

    def test_impact_requires_target_id(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.get_impact_lookup(self.project.id, target_type="figure", target_id="")
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()