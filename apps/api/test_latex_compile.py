"""阶段 8：LaTeX 编译适配与页数解析契约测试。"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import unittest.mock
from pathlib import Path

from fastapi import HTTPException
from starlette.requests import Request

from app import latex_compile, main
from app.contracts import ArtifactCreate, DeliveryCompileRequest, EvidenceCreate, ReviewCreate, ReviewerKind
from app.store import Store

ENGINE = latex_compile.find_engine()


def make_request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/x", "headers": []})


class FakeCommandRunner:
    """伪造编译命令：写出一份最小合法 PDF，用于验证接线逻辑。"""

    def __init__(self, pages: int = 3) -> None:
        self.pages = pages
        self.commands: list[list[str]] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        workdir = Path(kwargs["cwd"])
        pdf = b"%PDF-1.4\n" + b"/Type /Page\n" * self.pages + b"%%EOF"
        (workdir / "paper.pdf").write_bytes(pdf)
        return subprocess.CompletedProcess(list(command), 0, "ok", "")


class PageCountTests(unittest.TestCase):
    def test_page_count_prefers_pages_node(self) -> None:
        pdf = b"%PDF-1.5\n1 0 obj<</Type /Pages /Count 7>>endobj\n"
        self.assertEqual(latex_compile.pdf_page_count(pdf), 7)

    def test_page_count_falls_back_to_page_objects(self) -> None:
        pdf = b"%PDF-1.5\n" + b"<</Type /Page>>" * 4
        self.assertEqual(latex_compile.pdf_page_count(pdf), 4)

    def test_page_count_returns_none_when_unparsable(self) -> None:
        self.assertIsNone(latex_compile.pdf_page_count(b"not a pdf"))


class EngineResolutionTests(unittest.TestCase):
    def test_find_engine_prefers_path(self) -> None:
        resolved = latex_compile.find_engine(which=lambda name: f"/fake/{name}" if name == "pdflatex" else None)
        self.assertEqual(resolved, "/fake/pdflatex")

    def test_find_engine_missing_returns_none(self) -> None:
        self.assertIsNone(latex_compile.find_engine(which=lambda name: None, candidates=("nope-engine",)))


class CompileDriverTests(unittest.TestCase):
    def test_missing_engine_fails_closed(self) -> None:
        with unittest.mock.patch.object(latex_compile, "find_engine", return_value=None):
            result = latex_compile.compile_latex("\\documentclass{article}")
        self.assertEqual(result.status, "not_compiled")
        self.assertEqual(result.reason, "latex_engine_missing")

    def test_empty_source_is_rejected(self) -> None:
        with self.assertRaises(latex_compile.LatexError):
            latex_compile.compile_latex("   ", engine="xelatex")

    def test_compile_with_stubbed_runner_produces_pdf_and_pages(self) -> None:
        import unittest.mock as mock

        with mock.patch("subprocess.run", FakeCommandRunner(pages=3)):
            result = latex_compile.compile_latex("\\documentclass{article}\\begin{document}x\\end{document}", engine="xelatex")
        self.assertEqual(result.status, "compiled")
        self.assertEqual(result.pages, 3)
        self.assertTrue(result.pdf_bytes.startswith(b"%PDF"))
        self.assertEqual(len(result.pdf_sha256 or ""), 64)

    def test_nonzero_exit_is_reported_with_log(self) -> None:
        import unittest.mock as mock

        def failing(command, **kwargs):
            return subprocess.CompletedProcess(list(command), 1, "! LaTeX Error: something bad", "")

        with mock.patch("subprocess.run", failing):
            result = latex_compile.compile_latex("\\documentclass{article}", engine="xelatex")
        self.assertEqual(result.status, "not_compiled")
        self.assertIn("latex_exit_1", result.reason or "")
        self.assertIn("LaTeX Error", result.log_tail)


class DeliveryCompileTests(unittest.TestCase):
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

    def _approved_paper(self, text: str):
        artifact = self.store.create_artifact(
            self.project.id,
            ArtifactCreate(name="PAPER.md", artifact_type="paper_source", description="论文"),
            created_by="member-001",
        )
        self.store.store_artifact_content(artifact.id, text.encode("utf-8"))
        self.store.create_evidence(
            self.project.id, EvidenceCreate(claim="论文可复算", evidence_type="artifact", artifact_id=artifact.id)
        )
        self.store.submit_artifact_for_review(artifact.id, actor="member-001")
        self.store.create_review(
            self.project.id,
            ReviewCreate(
                target_type="artifact",
                target_id=artifact.id,
                verdict="APPROVED",
                summary="批准论文",
                reviewer="member-001",
                reviewer_kind=ReviewerKind.MEMBER,
            ),
        )
        return artifact

    def test_compile_requires_approved_paper_source(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            main.compile_delivery_paper(self.project.id, DeliveryCompileRequest(), make_request())
        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn("compile_requires_approved_paper_source", str(caught.exception.detail))

    def test_compile_registers_compiled_pdf_and_page_count(self) -> None:
        import unittest.mock as mock

        self._approved_paper("\\documentclass{article}\\begin{document}hello\\end{document}")
        with mock.patch("subprocess.run", FakeCommandRunner(pages=5)):
            report = main.compile_delivery_paper(self.project.id, DeliveryCompileRequest(), make_request())
        self.assertEqual(report["status"], "compiled")
        self.assertEqual(report["pages"], 5)
        self.assertTrue(report["pdf_sha256"])

        stored = next(
            item for item in self.store.list_artifacts(self.project.id) if str(item.artifact_type) == "compiled_pdf"
        )
        self.assertEqual(stored.name, "main.pdf")
        self.assertEqual(str(stored.status), "DRAFT")
        self.assertEqual(self.store.get_artifact_content(stored.id)[:4], b"%PDF")

        # 交付检查的页数项应读到真实页数。
        checklist = main.run_delivery_checklist(self.project.id, main.DeliveryChecklistRequest())
        page_check = next(item for item in checklist["checks"] if item["code"] == "page_count")
        self.assertEqual(page_check["status"], "pass")
        self.assertIn("5 页", page_check["detail"])

    def test_compile_inline_source_without_approval(self) -> None:
        import unittest.mock as mock

        with mock.patch("subprocess.run", FakeCommandRunner(pages=2)):
            report = main.compile_delivery_paper(
                self.project.id, DeliveryCompileRequest(source="\\documentclass{article}", artifact_name="preview.pdf"), make_request()
            )
        self.assertEqual(report["status"], "compiled")
        self.assertEqual(report["artifact_name"], "preview.pdf")
        self.assertIsNone(report["source_artifact_id"])

    @unittest.skipUnless(ENGINE, "本机未安装 LaTeX 引擎")
    def test_real_engine_compiles_chinese_paper(self) -> None:
        source = (
            "\\documentclass[12pt]{ctexart}\n"
            "\\begin{document}\n"
            "\\section{模型建立}\n本文给出风光储能协同优化模型。\n"
            "\\end{document}\n"
        )
        result = latex_compile.compile_latex(source, engine=ENGINE, timeout_seconds=300)
        self.assertEqual(result.status, "compiled", result.reason)
        self.assertTrue((result.pages or 0) >= 1)
        self.assertTrue(result.pdf_bytes.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()