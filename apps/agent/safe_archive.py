"""内核侧安全解压（FM-3）：把工作区里的归档解到工作区内的目录，先验证、后落地。

与平台侧（`apps/api/app/archive.py`，FM-2）是**同一套规则的两个实现**——因为两边是不同的部署物
（平台是 FastAPI，内核是独立可执行），共享代码要走发包流程，这里选择"同一份规则写两遍 + 用同一组
攻击样本在两个测试套件里各测一遍"来防止漂移（样本见 `test_safe_archive.py` 与
`apps/api/test_drive_extraction.py` 的同名用例）。

计划 §7.2 的要求在文件系统上的落地方式：
- **先解压到临时目录**（工作区内的 `.math-agent-platform/tmp/<随机>`），全部校验通过后再 `os.replace`
  **原子移动**到目标目录 —— 失败时清理临时目录，用户永远看不到"解了一半"的目录；
- 拒绝：绝对路径、`..`、盘符、UNC、符号链接/硬链接/设备文件/FIFO、条目数、单文件、总大小、
  压缩比、目录深度、嵌套归档（跳过并计数）、同名冲突（默认不覆盖）。
"""

from __future__ import annotations

import io
import os
import re
import shutil
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from file_worker import FileWorkerError, validate_relative

SUPPORTED_SUFFIXES = (".tar.gz", ".tgz", ".zip", ".tar")
MAX_ENTRIES = 2000
MAX_SINGLE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_RATIO = 200.0
MAX_DEPTH = 32
JUNK_PREFIXES = ("__macosx/",)
JUNK_NAMES = {".ds_store", "thumbs.db"}
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass
class ScanResult:
    files: list[tuple[str, bytes]] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    skipped_nested: list[str] = field(default_factory=list)
    skipped_junk: int = 0
    total_bytes: int = 0


def archive_kind(name: str) -> str | None:
    lowered = str(name or "").lower()
    for suffix in SUPPORTED_SUFFIXES:
        if lowered.endswith(suffix):
            return "zip" if suffix == ".zip" else "tar"
    return None


def _normalize(raw: str) -> str:
    """成员路径 → 规范相对路径；可疑就抛（**不返回空**，避免"静默跳过"）。"""

    value = str(raw or "").replace("\\", "/")
    if value.startswith("/") or value.startswith("//"):
        raise FileWorkerError("archive_path_unsafe", "absolute")
    if _WINDOWS_DRIVE.match(value):
        raise FileWorkerError("archive_path_unsafe", "drive_letter")
    if "\x00" in value or any(ord(character) < 32 for character in value):
        raise FileWorkerError("archive_path_unsafe", "control_character")
    try:
        return validate_relative(value)
    except FileWorkerError as error:
        raise FileWorkerError("archive_path_unsafe", error.detail or "invalid") from error


def _is_nested(path: str) -> bool:
    return archive_kind(path) is not None


def scan_archive(name: str, payload: bytes) -> ScanResult:
    """扫一遍归档（不写任何东西）。"""

    kind = archive_kind(name)
    if kind is None:
        raise FileWorkerError("archive_format_unsupported", name)
    result = ScanResult()
    seen: set[str] = set()

    def accept(path: str, content_reader) -> None:
        if path in seen:
            raise FileWorkerError("archive_target_conflict", path)
        seen.add(path)
        lowered = path.lower()
        if lowered.startswith(JUNK_PREFIXES) or Path(lowered).name in JUNK_NAMES:
            result.skipped_junk += 1
            return
        if _is_nested(path):
            result.skipped_nested.append(path)
            return
        content = content_reader()
        if len(content) > MAX_SINGLE_BYTES:
            raise FileWorkerError("archive_uncompressed_size_exceeded", path)
        result.total_bytes += len(content)
        if result.total_bytes > MAX_TOTAL_BYTES:
            raise FileWorkerError("archive_uncompressed_size_exceeded", str(result.total_bytes))
        result.files.append((path, content))

    if kind == "zip":
        try:
            handle = zipfile.ZipFile(io.BytesIO(payload))
        except zipfile.BadZipFile as error:
            raise FileWorkerError("archive_format_unsupported", "bad_zip") from error
        infos = handle.infolist()
        if len(infos) > MAX_ENTRIES:
            raise FileWorkerError("archive_too_many_entries", str(len(infos)))
        for info in infos:
            if info.is_dir():
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                raise FileWorkerError("archive_path_unsafe", f"symlink:{info.filename}")
            path = _normalize(info.filename)
            accept(path, lambda info=info: handle.read(info))
    else:
        try:
            handle = tarfile.open(fileobj=io.BytesIO(payload), mode="r:*")
        except tarfile.TarError as error:
            raise FileWorkerError("archive_format_unsupported", "bad_tar") from error
        count = 0
        with handle:
            for member in handle:
                count += 1
                if count > MAX_ENTRIES:
                    raise FileWorkerError("archive_too_many_entries", str(count))
                if member.issym() or member.islnk() or member.ischr() or member.isblk() or member.isfifo():
                    raise FileWorkerError("archive_path_unsafe", f"link_or_special:{member.name}")
                if member.isdir():
                    result.directories.append(_normalize(member.name))
                    continue
                if not member.isfile():
                    raise FileWorkerError("archive_path_unsafe", f"unsupported:{member.name}")
                path = _normalize(member.name)
                extracted = handle.extractfile(member)
                accept(path, lambda extracted=extracted: extracted.read() if extracted is not None else b"")

    if not result.files and not result.directories:
        raise FileWorkerError("archive_empty", name)
    compressed = max(1, len(payload))
    if result.total_bytes / compressed > MAX_RATIO:
        raise FileWorkerError("archive_ratio_exceeded", f"{result.total_bytes}/{compressed}")
    return result


def _directories_for(files: list[tuple[str, bytes]], extra: list[str]) -> list[str]:
    dirs: set[str] = set(extra)
    for path, _content in files:
        parts = path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:index]))
    return sorted(dirs)


def extract_into_workspace(archive: Path, destination_dir: Path, root: Path) -> dict[str, object]:
    """把 `archive` 解到 `destination_dir/<归档名去扩展名>`（先临时目录、后原子移动）。"""

    payload = Path(archive).read_bytes()
    scan = scan_archive(Path(archive).name, payload)
    stem = Path(archive).name
    lowered = stem.lower()
    for suffix in SUPPORTED_SUFFIXES:
        if lowered.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = stem.strip() or "解压结果"
    try:
        target_name = validate_relative(stem, allow_empty=False)
    except FileWorkerError:
        target_name = "解压结果"
    target = Path(destination_dir) / target_name
    suffix_counter = 2
    while target.exists():
        target = Path(destination_dir) / f"{target_name}-{suffix_counter}"
        suffix_counter += 1
        if suffix_counter > 50:
            raise FileWorkerError("archive_target_conflict", target_name)

    staging_root = Path(root) / ".math-agent-platform" / "tmp" / f"extract-{uuid4().hex[:10]}"
    staging = staging_root / target_name
    try:
        staging.mkdir(parents=True, exist_ok=False)
        for relative in _directories_for(scan.files, scan.directories):
            (staging / relative).mkdir(parents=True, exist_ok=True)
        for relative, content in scan.files:
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                raise FileWorkerError("archive_target_conflict", relative)
            path.write_bytes(content)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staging, target)  # 原子移动：要么整棵出现，要么完全没有
    except Exception:
        shutil.rmtree(staging_root, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)
    return {
        "relative_path": str(target.relative_to(Path(root))).replace(os.sep, "/"),
        "files": len(scan.files),
        "bytes": scan.total_bytes,
        "directories": len(_directories_for(scan.files, scan.directories)),
        "skipped_nested": scan.skipped_nested,
        "skipped_junk": scan.skipped_junk,
    }


__all__ = [
    "MAX_ENTRIES",
    "MAX_RATIO",
    "MAX_SINGLE_BYTES",
    "MAX_TOTAL_BYTES",
    "SUPPORTED_SUFFIXES",
    "ScanResult",
    "archive_kind",
    "extract_into_workspace",
    "scan_archive",
]