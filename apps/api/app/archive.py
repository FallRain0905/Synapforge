"""安全解压（FM-2）：把云盘里的归档解成一个目录树，**先全量校验、再落库**。

为什么这件事必须单独写一层：解压是"用用户给的数据去写文件系统"的经典入口，攻击面全在路径与
元数据上（Zip Slip、绝对路径、盘符、UNC、symlink/hardlink、设备文件、zip bomb）。计划 §7.2 定的
规则是：**先把归档目录完整扫一遍，全部通过才开始落库**；任何一条不满足就整份拒绝，不留半个目录。

本实现与"先解压到临时目录再原子移动"是同一个意图在节点树上的等价物：
- 校验阶段**不写任何东西**（不落节点、不写对象）；
- 写入阶段逐份落库，中途失败就**补偿清理**（把已经建出来的节点 trash + purge，对象按引用计数删）；
所以用户能看到的要么是完整的解压结果，要么什么都没有——不存在"解了一半"的目录。

第一期只支持 `.zip` / `.tar` / `.tar.gz` / `.tgz`：`.7z`/`.rar`/`.bz2`/`.xz` 只识别与下载
（没有受控解压器就不猜）。
"""

from __future__ import annotations

import io
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

from . import drive
from .drive import DriveActor, DriveError

# 第一期允许的格式（扩展名 → 解压器）
SUPPORTED_SUFFIXES = (".tar.gz", ".tgz", ".zip", ".tar")

MAX_ENTRIES = 2000
MAX_SINGLE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_RATIO = 200.0
MAX_DEPTH = 32
MAX_NAME_LENGTH = 240
# 归档里的噪声：macOS 元数据目录与文件（解出来只是垃圾，如实计数但不落地）
JUNK_PREFIXES = ("__macosx/",)
JUNK_NAMES = {".ds_store", "thumbs.db"}
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


@dataclass
class Member:
    """归档里的一个成员（已经过校验，可以直接落库）。"""

    path: str  # 相对路径，正斜杠分隔，已规范化
    content: bytes
    is_directory: bool = False


@dataclass
class ScanResult:
    members: list[Member] = field(default_factory=list)
    directories: list[str] = field(default_factory=list)
    skipped_nested: list[str] = field(default_factory=list)
    skipped_junk: int = 0
    total_bytes: int = 0


def archive_kind(name: str) -> str | None:
    """扩展名 → 解压器；不支持的返回 None（**不猜**：7z/rar/bz2/xz 只下载不解压）。"""

    lowered = str(name or "").lower()
    for suffix in SUPPORTED_SUFFIXES:
        if lowered.endswith(suffix):
            return "zip" if suffix == ".zip" else "tar"
    return None


def _normalize_member_path(raw: str) -> str:
    """成员路径 → 规范化相对路径；只要有一点可疑就拒绝（返回空串表示"不安全"）。"""

    value = str(raw or "").replace("\\", "/")
    if not value or value.endswith("/") and value.strip("/") == "":
        return ""
    if "\x00" in value or any(ord(character) < 32 for character in value):
        return ""
    if value.startswith("/") or value.startswith("//"):
        return ""
    if _WINDOWS_DRIVE.match(value):
        return ""
    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            return ""
        if len(part) > MAX_NAME_LENGTH:
            return ""
        parts.append(part)
    if not parts:
        return ""
    if len(parts) > MAX_DEPTH:
        return ""
    candidate = "/".join(parts)
    # 逐段做一次与云盘同规格的名字校验（保留名、尾随点等），但**不**因扩展名而变
    for part in parts:
        try:
            drive.validate_name(part)
        except DriveError:
            return ""
    return candidate


def _check_member(path: str, size: int) -> None:
    if size > MAX_SINGLE_BYTES:
        raise DriveError("archive_uncompressed_size_exceeded", f"{path}:{size}")


def _scan_zip(payload: bytes) -> ScanResult:
    result = ScanResult()
    try:
        handle = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as error:
        raise DriveError("archive_format_unsupported", "bad_zip") from error
    infos = handle.infolist()
    if len(infos) > MAX_ENTRIES:
        raise DriveError("archive_too_many_entries", str(len(infos)))
    seen: set[str] = set()
    for info in infos:
        if info.is_dir():
            continue
        # symlink：ZIP 用外部属性里的 Unix 模式位标记（0o120000）
        unix_mode = (info.external_attr >> 16) & 0o170000
        if unix_mode == 0o120000:
            raise DriveError("archive_path_unsafe", f"symlink:{info.filename}")
        path = _normalize_member_path(info.filename)
        if not path:
            raise DriveError("archive_path_unsafe", str(info.filename)[:120])
        if path in seen:
            raise DriveError("archive_target_conflict", path)
        seen.add(path)
        lowered = path.lower()
        if lowered.startswith(JUNK_PREFIXES) or PurePosixPath(lowered).name in JUNK_NAMES:
            result.skipped_junk += 1
            continue
        if _is_nested_archive(path):
            result.skipped_nested.append(path)
            continue
        _check_member(path, info.file_size)
        result.total_bytes += info.file_size
        if result.total_bytes > MAX_TOTAL_BYTES:
            raise DriveError("archive_uncompressed_size_exceeded", str(result.total_bytes))
        with handle.open(info) as member:
            content = member.read(MAX_SINGLE_BYTES + 1)
        if len(content) > MAX_SINGLE_BYTES:
            raise DriveError("archive_uncompressed_size_exceeded", f"{path}:actual")
        result.members.append(Member(path=path, content=content))
    if len(result.members) + len(result.skipped_nested) > MAX_ENTRIES:
        raise DriveError("archive_too_many_entries", str(len(result.members)))
    return result


def _scan_tar(payload: bytes) -> ScanResult:
    result = ScanResult()
    try:
        handle = tarfile.open(fileobj=io.BytesIO(payload), mode="r:*")
    except tarfile.TarError as error:
        raise DriveError("archive_format_unsupported", "bad_tar") from error
    count = 0
    seen: set[str] = set()
    with handle:
        for member in handle:
            count += 1
            if count > MAX_ENTRIES:
                raise DriveError("archive_too_many_entries", str(count))
            # 链接与特殊文件一律拒绝：它们在解压时能指到归档外面去（tar 的经典攻击面）
            if member.issym() or member.islnk():
                raise DriveError("archive_path_unsafe", f"link:{member.name}")
            if member.ischr() or member.isblk() or member.isfifo():
                raise DriveError("archive_path_unsafe", f"special:{member.name}")
            if member.isdir():
                path = _normalize_member_path(member.name)
                if not path:
                    raise DriveError("archive_path_unsafe", str(member.name)[:120])
                result.directories.append(path)
                continue
            if not member.isfile():
                raise DriveError("archive_path_unsafe", f"unsupported:{member.name}")
            path = _normalize_member_path(member.name)
            if not path:
                raise DriveError("archive_path_unsafe", str(member.name)[:120])
            if path in seen:
                raise DriveError("archive_target_conflict", path)
            seen.add(path)
            lowered = path.lower()
            if lowered.startswith(JUNK_PREFIXES) or PurePosixPath(lowered).name in {name.lower() for name in JUNK_NAMES}:
                result.skipped_junk += 1
                continue
            if _is_nested_archive(path):
                result.skipped_nested.append(path)
                continue
            _check_member(path, member.size)
            result.total_bytes += member.size
            if result.total_bytes > MAX_TOTAL_BYTES:
                raise DriveError("archive_uncompressed_size_exceeded", str(result.total_bytes))
            extracted = handle.extractfile(member)
            content = extracted.read(MAX_SINGLE_BYTES + 1) if extracted is not None else b""
            if len(content) > MAX_SINGLE_BYTES:
                raise DriveError("archive_uncompressed_size_exceeded", f"{path}:actual")
            result.members.append(Member(path=path, content=content))
    return result


def _is_nested_archive(path: str) -> bool:
    return archive_kind(path) is not None


def scan_archive(name: str, payload: bytes) -> ScanResult:
    """扫一遍归档（**不写任何东西**）：把所有攻击面在这一步拦掉。"""

    kind = archive_kind(name)
    if kind is None:
        raise DriveError("archive_format_unsupported", name)
    result = _scan_zip(payload) if kind == "zip" else _scan_tar(payload)
    if not result.members and not result.directories:
        raise DriveError("archive_empty", name)
    compressed = max(1, len(payload))
    if result.total_bytes / compressed > MAX_RATIO:
        raise DriveError("archive_ratio_exceeded", f"{result.total_bytes}/{compressed}")
    return result


def _directory_name_for(name: str) -> str:
    """归档名 → 解出来的目录名（去掉归档扩展名，并过一遍云盘的名字校验）。"""

    lowered = name.lower()
    for suffix in SUPPORTED_SUFFIXES:
        if lowered.endswith(suffix):
            stem = name[: -len(suffix)]
            break
    else:
        stem = name
    stem = stem.strip() or "解压结果"
    try:
        return drive.validate_name(stem)
    except DriveError:
        return "解压结果"


def _unique_directory_name(store: Any, actor: DriveActor, parent_id: str, desired: str) -> str:
    candidate = desired
    counter = 2
    while store.db.execute(
        "SELECT 1 FROM drive_nodes WHERE owner_member_id = ? AND parent_id = ? AND name_key = ? AND deleted_at IS NULL AND purged_at IS NULL",
        (actor.member_id, parent_id, drive.name_key(candidate)),
    ).fetchone():
        candidate = f"{desired}-{counter}"
        counter += 1
        if counter > 50:
            raise DriveError("file_name_conflict", desired)
    return candidate


def _ensure_directory(store: Any, actor: DriveActor, parent_id: str, path: str) -> str:
    """按需创建中间目录，返回最深一级的节点 id（同名已存在的目录直接复用）。"""

    current = parent_id
    for segment in [part for part in path.split("/") if part]:
        existing = store.db.execute(
            "SELECT id, kind FROM drive_nodes WHERE owner_member_id = ? AND parent_id = ? AND name_key = ? AND deleted_at IS NULL AND purged_at IS NULL",
            (actor.member_id, current, drive.name_key(segment)),
        ).fetchone()
        if existing is not None:
            if str(existing["kind"]) != "directory":
                # 归档里既有文件 `a` 又有目录 `a/`：算法上无法同时成立，明确拒绝
                raise DriveError("archive_target_conflict", segment)
            current = str(existing["id"])
            continue
        node = drive.create_directory(store, actor, current, segment)
        current = node["id"]
    return current


def _rollback(store: Any, actor: DriveActor, created: list[str]) -> None:
    """补偿清理：把这次已经建出来的节点删干净（先子后父），对象按引用计数删。

    为什么需要它：写入阶段是逐份落库的（复用 `drive.put_file` 的去重与配额判定）。中途失败时
    用户不该看到一个"解了一半"的目录——宁可什么都不留，并把失败如实抛出去。
    """

    ordered = sorted(created, key=lambda node_id: len(drive.breadcrumb(store, actor, node_id)), reverse=True)
    for node_id in ordered:
        try:
            row = drive._raw(store, node_id)
            if row is None:
                continue
            if not drive._field(row, "deleted_at"):
                drive.trash(store, actor, node_id)
            drive.purge(store, actor, node_id)
        except DriveError:
            # 清理失败的节点留在回收站里（如实留痕，不假装删掉了）
            continue


def extract_archive(
    store: Any,
    actor: DriveActor,
    node_id: str,
    *,
    target_parent_id: str | None = None,
    directory_name: str | None = None,
) -> dict[str, Any]:
    """把云盘里的归档解成一个新目录。返回 `{node, files, bytes, directories, skipped_*}`。"""

    drive.ensure_schema(store)
    node, payload = drive.read_content(store, actor, node_id)
    if str(node["kind"]) != "file":
        raise DriveError("archive_format_unsupported", "not_a_file")

    scan = scan_archive(str(node["name"]), payload)

    parent = drive.resolve_parent(store, actor, target_parent_id or node["parent_id"])
    parent_id = str(parent["id"])
    # 配额：整份解压要一次性过闸（逐份判断会出现"解到一半配额不够"）
    usage = drive.usage(store, actor)
    fresh_bytes = sum(len(member.content) for member in scan.members)
    if usage["used_bytes"] + fresh_bytes > usage["quota_bytes"]:
        raise DriveError("file_quota_exceeded", f"{usage['used_bytes'] + fresh_bytes}/{usage['quota_bytes']}")

    desired = drive.validate_name(directory_name) if directory_name else _directory_name_for(str(node["name"]))
    target_name = _unique_directory_name(store, actor, parent_id, desired)
    root = drive.create_directory(store, actor, parent_id, target_name)
    created: list[str] = [root["id"]]
    try:
        for path in scan.directories:
            created.append(_ensure_directory(store, actor, root["id"], path))
        for member in scan.members:
            # 注意：`PurePosixPath("a.txt").parent` 是 "."——拿它去建目录会被名字校验拒掉
            # （"dot_segment"）。这里自己切段，空串就表示"直接放在解压根目录下"。
            container_path = "/".join(member.path.split("/")[:-1])
            container = _ensure_directory(store, actor, root["id"], container_path)
            if container not in created:
                created.append(container)
            uploaded = drive.put_file(store, actor, container, member.path.split("/")[-1], member.content)
            created.append(uploaded["id"])
    except Exception:
        _rollback(store, actor, created)
        raise

    with drive._transaction(store):
        drive.audit(
            store,
            actor,
            "extract",
            node=drive._raw(store, root["id"]),
            capability="drive.file.extract",
            size_bytes=scan.total_bytes,
            reason=f"{len(scan.members)} 个文件",
        )
    directories = store.db.execute(
        """
        WITH RECURSIVE subtree(id, kind) AS (
            SELECT id, kind FROM drive_nodes WHERE id = ? AND purged_at IS NULL
            UNION ALL
            SELECT n.id, n.kind FROM drive_nodes n JOIN subtree s ON n.parent_id = s.id WHERE n.purged_at IS NULL
        )
        SELECT COUNT(*) AS c FROM subtree WHERE kind = 'directory'
        """,
        (root["id"],),
    ).fetchone()["c"]
    return {
        "node": drive.get_node(store, actor, root["id"]),
        "archive_id": str(node_id),
        "files": len(scan.members),
        "bytes": scan.total_bytes,
        "directories": int(directories),
        "skipped_nested": scan.skipped_nested,
        "skipped_junk": scan.skipped_junk,
        "usage": drive.usage(store, actor),
    }


__all__ = [
    "MAX_ENTRIES",
    "MAX_RATIO",
    "MAX_SINGLE_BYTES",
    "MAX_TOTAL_BYTES",
    "Member",
    "SUPPORTED_SUFFIXES",
    "ScanResult",
    "archive_kind",
    "extract_archive",
    "scan_archive",
]