"""阶段 7 最小切片：文档版本 Diff 与合并确认契约测试。"""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from uuid import UUID

from fastapi import HTTPException
from starlette.requests import Request

from app import main
from app.contracts import (
    ArtifactCreate,
    DocumentMergeRequest,
    DocumentSubmitRequest,
    EvidenceCreate,
    ReviewCreate,
    ReviewerKind,
)
from app.store import Store


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


class DocumentDiffAndMergeTests(unittest.TestCase):
    """Git 分支/Diff/合并确认最小切片。"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp_dir.name) / "platform.db")
        self.previous_store = main.store
        main.store = self.store
        self.project = self.store.list_projects()[0]
        self.document = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="MODELING_REPORT.md", artifact_type="model_spec", description="建模报告"),
            created_by="member-001",
            created_by_kind="member",
        )
        self.store.store_artifact_content(
            self.document.id, "# 模型报告\n\n## 模型建立\n初步模型。\n".encode("utf-8")
        )
        self.latest = self.document

    def tearDown(self) -> None:
        main.store = self.previous_store
        self.store.close()
        self.temp_dir.cleanup()

    def _new_version(self, text: str, git_commit: str | None = None, actor: str = "agent-7") -> None:
        draft = ArtifactCreate(
            name="MODELING_REPORT.md",
            artifact_type="model_spec",
            description="建模报告",
            git_commit=git_commit,
        )
        artifact = self.store.revise_artifact(
            self.project.id, self.latest.id, draft, actor=actor, actor_kind="agent"
        )
        self.store.store_artifact_content(artifact.id, text.encode("utf-8"))
        self.latest = artifact

    def test_diff_reports_content_and_trace_changes(self) -> None:
        self._new_version("# 模型报告\n\n## 模型建立\n改进模型，加入灵敏度。\n", git_commit="def5678")
        diff = main.get_document_diff(self.project.id, self.latest.id)
        self.assertEqual(diff["from_revision"], 1)
        self.assertEqual(diff["to_revision"], 2)
        self.assertTrue(diff["content_changed"])
        self.assertFalse(diff["identical"])
        self.assertGreaterEqual(diff["stats"]["added_lines"], 1)
        self.assertGreaterEqual(diff["stats"]["removed_lines"], 1)
        self.assertTrue(any(line.startswith("@@") for line in diff["unified_diff"]))
        self.assertTrue(diff["git_commit"]["changed"])
        self.assertEqual(diff["git_commit"]["to"], "def5678")
        self.assertTrue(diff["author"]["changed"])
        self.assertEqual(diff["author"]["to"], "agent-7")

    def test_diff_of_identical_content_is_marked_identical(self) -> None:
        self._new_version("# 模型报告\n\n## 模型建立\n初步模型。\n")
        diff = main.get_document_diff(self.project.id, self.latest.id)
        self.assertTrue(diff["identical"])
        self.assertFalse(diff["content_changed"])
        self.assertEqual(diff["stats"]["added_lines"], 0)
        self.assertEqual(diff["stats"]["removed_lines"], 0)

    def test_diff_explicit_range_and_out_of_range(self) -> None:
        self._new_version("# 模型报告\n\n第二版。\n")
        self._new_version("# 模型报告\n\n第三版。\n")
        diff = main.get_document_diff(self.project.id, self.latest.id, from_revision=1, to_revision=3)
        self.assertEqual((diff["from_revision"], diff["to_revision"]), (1, 3))
        with self.assertRaises(HTTPException) as caught:
            main.get_document_diff(self.project.id, self.latest.id, from_revision=1, to_revision=9)
        self.assertEqual(caught.exception.status_code, 400)

    def test_diff_fails_closed_when_content_missing(self) -> None:
        bare = self.store.create_artifact(
            self.project.id, ArtifactCreate(name="BARE.md", artifact_type="paper_source"), created_by="member-001"
        )
        with self.assertRaises(HTTPException) as caught:
            main.get_document_diff(self.project.id, bare.id)
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("document_content_unavailable", str(caught.exception.detail))

    def test_merge_confirm_derives_draft_from_source_revision(self) -> None:
        first_content = "# 模型报告\n\n## 模型建立\n初步模型。\n"
        self._new_version("# 模型报告\n\n分歧版本（别人写的）。\n", git_commit="def5678")
        result = main.merge_document_revision(
            self.project.id,
            self.latest.id,
            DocumentMergeRequest(source_revision=1, note="采用基线版本合并"),
            make_request(),
        )
        self.assertEqual(result["source_revision"], 1)
        self.assertEqual(result["layer"], "draft")
        self.assertEqual(result["content_hash"], hashlib.sha256(first_content.encode()).hexdigest())

        timeline = main.get_document_timeline(self.project.id, UUID(result["artifact_id"]))
        self.assertEqual(timeline["revision_count"], 3)
        self.assertEqual([item["layer"] for item in timeline["revisions"]][:2], ["draft", "draft"])

    def test_merge_confirm_rejects_approved_target(self) -> None:
        self.store.create_evidence(
            self.project.id,
            EvidenceCreate(claim="结论可复算", evidence_type="artifact", artifact_id=self.latest.id),
        )
        main.submit_document_revision(self.project.id, self.latest.id, DocumentSubmitRequest(), make_request())
        self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="artifact",
                target_id=self.latest.id,
                verdict="APPROVED",
                summary="批准",
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        with self.assertRaises(HTTPException) as caught:
            main.merge_document_revision(
                self.project.id, self.latest.id, DocumentMergeRequest(source_revision=1), make_request()
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("document_approved_is_immutable", str(caught.exception.detail))

    def test_merge_confirm_rejects_out_of_range_revision(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.merge_document_revision(
                self.project.id, self.latest.id, DocumentMergeRequest(source_revision=7), make_request()
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("document_revision_out_of_range", str(caught.exception.detail))

    def test_merge_confirm_validates_git_commit_when_repository_registered(self) -> None:
        repo = str(Path(self.temp_dir.name) / "repo")
        Path(repo).mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "t@example.test"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "tester"], cwd=repo, check=True)
        Path(repo, "a.txt").write_text("x", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()

        self.store.register_git_repository(
            main.GitRepositoryCreate(project_id=self.project.id, provider="local", local_path=repo)
        )
        self._new_version("# 模型报告\n\n第二版。\n")
        ok = main.merge_document_revision(
            self.project.id,
            self.latest.id,
            DocumentMergeRequest(source_revision=1, git_commit=head),
            make_request(),
        )
        self.assertEqual(ok["git_commit"], head)

        with self.assertRaises(HTTPException) as caught:
            main.merge_document_revision(
                self.project.id,
                self.latest.id,
                DocumentMergeRequest(source_revision=1, git_commit="0" * 40),
                make_request(),
            )
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("git_commit_not_found", str(caught.exception.detail))


if __name__ == "__main__":
    unittest.main()