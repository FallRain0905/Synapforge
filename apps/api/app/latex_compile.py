"""阶段 8：LaTeX 编译适配（真实 PDF 产出 + 页数解析）。

引擎解析顺序：PATH 中的 `xelatex` / `pdflatex` / `tectonic`，其次 Windows 常见
MiKTeX 用户级安装位置（无需管理员改 PATH）。引擎缺失时 fail-closed 返回
`latex_engine_missing`，绝不伪造编译成功。

页数解析是对 PDF 的轻量启发式（优先读 `/Type /Pages` 的 `/Count`，退化为
统计 `/Type /Page`），用于交付检查的页数项；不解析版式细节。
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

ENGINE_CANDIDATES = ("xelatex", "pdflatex", "tectonic")
WINDOWS_ENGINE_DIRS = (
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "MiKTeX" / "miktex" / "bin" / "x64",
    Path(os.environ.get("APPDATA", "")) / "MiKTeX" / "miktex" / "bin" / "x64",
    Path("C:/Program Files/MiKTeX/miktex/bin/x64"),
)
PAGE_COUNT_PATTERN = re.compile(rb"/Type\s*/Pages[^>]*?/Count\s+(\d+)", re.DOTALL)
SINGLE_PAGE_PATTERN = re.compile(rb"/Type\s*/Page[^s]")


class LatexError(RuntimeError):
    """稳定错误族：引擎缺失或编译失败。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code


@dataclass(frozen=True)
class CompileResult:
    status: str
    engine: str | None
    pages: int | None
    pdf_sha256: str | None
    pdf_bytes: bytes
    log_tail: str
    reason: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "compiled"

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "engine": self.engine,
            "pages": self.pages,
            "pdf_sha256": self.pdf_sha256,
            "pdf_bytes": len(self.pdf_bytes),
            "log_tail": self.log_tail,
            "reason": self.reason,
        }


def find_engine(
    *,
    which: Callable[[str], str | None] | None = None,
    candidates: Sequence[str] = ENGINE_CANDIDATES,
) -> str | None:
    """定位可用的 LaTeX 引擎（PATH 优先，其次 MiKTeX 常见安装目录）。"""

    resolver = which or shutil.which
    for name in candidates:
        found = resolver(name)
        if found:
            return found
    for directory in WINDOWS_ENGINE_DIRS:
        for name in candidates:
            candidate = directory / f"{name}.exe"
            if candidate.is_file():
                return str(candidate)
    return None


def pdf_page_count(pdf: bytes) -> int | None:
    """解析 PDF 页数：优先 pypdf，其次正则启发式；无法解析返回 None（不猜测）。

    真实 xelatex 输出常使用压缩对象流，纯正则读不到 `/Type /Pages`，因此
    安装 pypdf 时以它为准；两条路径都失败时明确返回 None，由调用方按 warn 处理。
    """

    try:
        import io

        from pypdf import PdfReader

        pages = len(PdfReader(io.BytesIO(pdf)).pages)
        if pages > 0:
            return pages
    except Exception:
        pass
    match = PAGE_COUNT_PATTERN.search(pdf)
    if match:
        try:
            value = int(match.group(1))
            if value > 0:
                return value
        except ValueError:
            return None
    pages = len(SINGLE_PAGE_PATTERN.findall(pdf))
    return pages or None


def compile_latex(
    source: str,
    *,
    engine: str | None = None,
    timeout_seconds: float = 180.0,
    run_twice: bool = True,
) -> CompileResult:
    """编译 LaTeX 源码为 PDF；引擎缺失或编译失败时 fail-closed。"""

    resolved = engine or find_engine()
    if not resolved:
        return CompileResult("not_compiled", None, None, None, b"", "", "latex_engine_missing")
    if not isinstance(source, str) or not source.strip():
        raise LatexError("latex_source_required")

    with tempfile.TemporaryDirectory(prefix="math-agent-latex-") as directory:
        workdir = Path(directory)
        tex_path = workdir / "paper.tex"
        tex_path.write_text(source, encoding="utf-8")
        command: list[str] = [
            resolved,
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            tex_path.name,
        ]
        log_tail = ""
        # 交叉引用与目录需要二次编译；第二次失败不影响首次成功的产物。
        passes = 2 if run_twice else 1
        for _ in range(passes):
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(workdir),
                    shell=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return CompileResult("not_compiled", resolved, None, None, b"", log_tail, f"latex_run_failed:{error}")
            log_tail = (completed.stdout or "")[-2000:] + (completed.stderr or "")[-500:]
            if completed.returncode != 0:
                return CompileResult("not_compiled", resolved, None, None, b"", log_tail, f"latex_exit_{completed.returncode}")
        pdf_path = workdir / "paper.pdf"
        if not pdf_path.is_file():
            return CompileResult("not_compiled", resolved, None, None, b"", log_tail, "latex_pdf_missing")
        pdf_bytes = pdf_path.read_bytes()
        if not pdf_bytes.startswith(b"%PDF"):
            return CompileResult("not_compiled", resolved, None, None, b"", log_tail, "latex_output_not_pdf")
        return CompileResult(
            "compiled",
            resolved,
            pdf_page_count(pdf_bytes),
            hashlib.sha256(pdf_bytes).hexdigest(),
            pdf_bytes,
            log_tail,
        )


__all__ = [
    "CompileResult",
    "ENGINE_CANDIDATES",
    "LatexError",
    "compile_latex",
    "find_engine",
    "pdf_page_count",
]