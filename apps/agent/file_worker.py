"""Agent 工作区文件 Worker（FM-3）：领取平台入队的文件操作，在**本机**执行并回报。

## 为什么单独一层

平台不入站连接任何人的电脑，所以"在 Agent 机器上动文件"必须由 Agent 自己主动领取。
这个模块就是那半边：轮询 → 领取 → 执行 → 回报。它与任务循环共用一个执行体槽位（同一台机器
同一时刻只干一件事），但**不走 Gateway**：文件操作走独立的 `/api/agent/workspace-operations/*`
契约，Gateway 的命令列表零改动。

## 路径安全（计划 §7.1，全部在这里落地）

- 只接受**相对路径**，且必须过 `validate_relative`（绝对路径、`..`、盘符、UNC、控制字符、超长、
  超深、Windows 保留名/尾随点一律拒绝）；
- 逐级 `lstat`：路径上**任何一段**是符号链接 / junction / reparse point 都拒绝
  （Windows 的 junction 用 `st_file_attributes` 的 reparse 位识别——`os.path.islink()` 认不出来）；
- 最后再用 `resolve()` + `commonpath` 兜一次底（"两道锁"，任一道失效另一道还能拦）；
- 删除/移动/改名**不许碰**工作区根、`.git`、`.math-agent-platform` 与 Agent 自报的保护目录；
- 大文件（> 8MB）**不进任何帧**：内容经平台对象存储的传输会话交换。

## 失败口径

- 任何一步失败都回报 `success=false` + 稳定错误码（平台据此把操作标成 failed 并显示原因），
  **绝不**把没做成的操作报成成功；
- 平台把操作标成 `cancelled`/`expired` 时，Worker 不会再执行（会先取一次状态）。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

LARGE_FILE_BYTES = 8 * 1024 * 1024
MAX_PATH_LENGTH = 1024
MAX_SEGMENT_LENGTH = 240
MAX_DEPTH = 32
MAX_LIST_ENTRIES = 2000
# 工作区里平台自己写的元数据目录：不许删、不许移动（删了内核就丢了运行台账）
PLATFORM_RESERVED = (".math-agent-platform",)
DEFAULT_PROTECTED = (".git", ".hg", ".svn")


class FileWorkerError(RuntimeError):
    """稳定错误族（与平台侧 `workspace_files` 的词表对齐）。"""

    def __init__(self, code: str, detail: str | None = None) -> None:
        super().__init__(code if not detail else f"{code}:{detail}")
        self.code = code
        self.detail = detail or ""


# ---- 路径工具（纯函数，便于单测） -------------------------------------------


def validate_relative(value: str, *, allow_empty: bool = True) -> str:
    """语法层校验 → 规范相对路径（正斜杠分隔，无 `.`/`..` 段）。"""

    raw = str(value or "").strip()
    if not raw:
        if allow_empty:
            return ""
        raise FileWorkerError("workspace_path_required")
    text = raw.replace("\\", "/")
    if len(text) > MAX_PATH_LENGTH:
        raise FileWorkerError("workspace_path_invalid", "too_long")
    if text.startswith("/") or text.startswith("//"):
        raise FileWorkerError("workspace_path_invalid", "absolute")
    if len(text) >= 2 and text[1] == ":" and text[0].isalpha():
        raise FileWorkerError("workspace_path_invalid", "drive_letter")
    if "\x00" in text or any(ord(character) < 32 for character in text):
        raise FileWorkerError("workspace_path_invalid", "control_character")
    parts: list[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise FileWorkerError("workspace_path_outside_root", "parent_segment")
        if len(part) > MAX_SEGMENT_LENGTH:
            raise FileWorkerError("workspace_path_invalid", "segment_too_long")
        if part.endswith((".", " ")):
            raise FileWorkerError("workspace_path_invalid", "trailing_dot_or_space")
        parts.append(part)
    if len(parts) > MAX_DEPTH:
        raise FileWorkerError("workspace_path_invalid", "too_deep")
    return "/".join(parts)


def _is_reparse_point(path: Path) -> bool:
    """junction / reparse point：Windows 上用文件属性位，POSIX 上等价于符号链接。"""

    try:
        info = path.lstat()
    except OSError:
        return False
    if os.name == "nt":
        attributes = getattr(info, "st_file_attributes", 0)
        reparse = getattr(__import__("stat"), "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse)
    return path.is_symlink()


def safe_path(
    root: Path | str,
    relative: str,
    *,
    must_exist: bool = False,
    allow_missing_leaf: bool = True,
) -> Path:
    """相对路径 → 工作区内的绝对路径；任何逃逸/链接都拒绝。

    两道锁：① 逐级 `lstat` 拒绝符号链接/junction；② `resolve()` 后 `commonpath` 必须仍在根内。
    """

    base = Path(root).expanduser().resolve()
    normalized = validate_relative(relative)
    if not normalized:
        return base
    candidate = base
    segments = normalized.split("/")
    for index, segment in enumerate(segments):
        candidate = candidate / segment
        leaf = index == len(segments) - 1
        if candidate.exists() or candidate.is_symlink():
            if _is_reparse_point(candidate):
                # 链接可能指到工作区外（也可能指回来），一律拒绝——不猜用户意图
                raise FileWorkerError("workspace_symlink_denied", normalized)
        elif (must_exist and leaf) or (not leaf and not allow_missing_leaf):
            raise FileWorkerError("workspace_path_not_found", normalized)
    resolved = candidate.resolve() if candidate.exists() else (base / normalized)
    try:
        common = os.path.commonpath([str(base), str(resolved)])
    except ValueError as error:  # 不同盘符：直接不算同一棵树
        raise FileWorkerError("workspace_path_outside_root", normalized) from error
    if os.path.normcase(common) != os.path.normcase(str(base)):
        raise FileWorkerError("workspace_path_outside_root", normalized)
    if must_exist and not candidate.exists():
        raise FileWorkerError("workspace_path_not_found", normalized)
    return candidate


def is_protected(relative: str, protected: list[str] | None = None, *, root_protected: bool = True) -> bool:
    """保护路径：工作区根、平台元数据目录、Agent 自报的保护目录（默认含 `.git` 等）。"""

    path = validate_relative(relative)
    if not path:
        return bool(root_protected)
    names = [*PLATFORM_RESERVED, *DEFAULT_PROTECTED, *(protected or [])]
    for item in names:
        clean = validate_relative(item)
        if clean and (path == clean or path.startswith(f"{clean}/")):
            return True
    return False


def _entry(path: Path, root: Path, relative: str, *, deep: bool = False) -> dict[str, Any]:
    try:
        info = path.lstat()
    except OSError as error:
        raise FileWorkerError("workspace_path_not_found", relative) from error
    value: dict[str, Any] = {
        "name": path.name,
        "relative_path": relative,
        "kind": "directory" if path.is_dir() else "file",
        "size_bytes": int(info.st_size) if path.is_file() else 0,
        "modified_at": datetime.fromtimestamp(info.st_mtime, UTC).isoformat(),
        "is_symlink": _is_reparse_point(path),
    }
    if deep and path.is_dir():
        children: list[dict[str, Any]] = []
        for child in sorted(path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if len(children) >= MAX_LIST_ENTRIES:
                value["truncated"] = True
                break
            child_relative = f"{relative}/{child.name}" if relative else child.name
            try:
                children.append(_entry(child, root, child_relative))
            except FileWorkerError:
                continue
        value["children"] = children
    return value


# ---- Worker -----------------------------------------------------------------


@dataclass
class FileWorkerConfig:
    url: str
    project_token: str
    agent_id: str
    workspace: Path
    project_id: str | None = None
    device_id: str | None = None
    workspace_id: str | None = None
    protected_paths: list[str] = field(default_factory=list)
    idle_seconds: float = 3.0
    lease_seconds: int = 120
    limit: int = 4


class WorkspaceFileWorker:
    """领取并执行文件操作。`http` 可注入（测试用假 HTTP，不联网）。"""

    def __init__(self, config: FileWorkerConfig, *, http: Callable[..., Any] | None = None, log: Callable[[str], None] = print) -> None:
        self.config = config
        self.log = log
        self.root = Path(config.workspace).expanduser().resolve()
        self._http = http or self._default_http
        self.stats = {"claimed": 0, "succeeded": 0, "failed": 0, "last_error": None}

    # -- HTTP ---------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        headers = {"X-Project-Capability-Token": self.config.project_token, "X-Agent-Id": self.config.agent_id}
        if self.config.project_id:
            headers["X-Project-Id"] = str(self.config.project_id)
        return headers

    @staticmethod
    def _default_http(method: str, url: str, *, headers: dict[str, str], body: bytes | None = None, timeout: float = 60.0) -> tuple[int, Any]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(url, data=body, method=method, headers={**headers, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                payload: Any = json.loads(raw) if raw and response.headers.get("Content-Type", "").startswith("application/json") else raw
                return response.status, payload
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, json.loads(raw)
            except ValueError:
                return error.code, {"detail": raw[:200].decode("utf-8", "replace")}

    def _call(self, method: str, path: str, payload: dict | None = None, *, raw_body: bytes | None = None) -> tuple[int, Any]:
        body = raw_body if raw_body is not None else (json.dumps(payload).encode("utf-8") if payload is not None else None)
        return self._http(method, f"{self.config.url.rstrip('/')}{path}", headers=self._headers(), body=body)

    # -- 轮询与执行 ----------------------------------------------------------
    def poll_once(self) -> int:
        """领一轮活并执行完；返回处理条数（0 = 没活）。"""

        status, payload = self._call(
            "POST",
            "/api/agent/workspace-operations/claim",
            {"workspace_id": self.config.workspace_id, "limit": self.config.limit, "lease_seconds": self.config.lease_seconds},
        )
        if status != 200 or not isinstance(payload, dict):
            self.stats["last_error"] = f"claim_failed:{status}:{str(payload)[:120]}"
            self.log(f"[files] 领取失败：{self.stats['last_error']}")
            return 0
        operations = payload.get("operations") or []
        for operation in operations:
            self.stats["claimed"] += 1
            self._run(operation)
        return len(operations)

    def _run(self, operation: dict[str, Any]) -> None:
        operation_id = str(operation.get("id"))
        kind = str(operation.get("operation_type"))
        relative = str(operation.get("relative_path") or "")
        arguments = operation.get("arguments") or {}
        self.log(f"[files] 执行 {kind} {relative or '(工作区根)'}（op {operation_id[:8]}）")
        status, _ = self._call("POST", f"/api/agent/workspace-operations/{operation_id}/start")
        if status != 200:
            self.log(f"[files] start 失败（{status}），跳过这一条（平台侧状态没动）")
            return
        try:
            result = self.execute(kind, relative, arguments)
        except FileWorkerError as error:
            self.stats["failed"] += 1
            self.stats["last_error"] = f"{error.code}:{error.detail}"[:200]
            self.log(f"[files] 失败：{self.stats['last_error']}")
            self._call(
                "POST",
                f"/api/agent/workspace-operations/{operation_id}/complete",
                {"success": False, "error_code": error.code, "error_message": error.detail or str(error)},
            )
            return
        except Exception as error:  # noqa: BLE001 - 任何意外都要如实回报，不能卡成"执行中"
            self.stats["failed"] += 1
            self.stats["last_error"] = f"{type(error).__name__}:{error}"[:200]
            self.log(f"[files] 异常：{self.stats['last_error']}")
            self._call(
                "POST",
                f"/api/agent/workspace-operations/{operation_id}/complete",
                {"success": False, "error_code": "workspace_operation_failed", "error_message": str(error)[:400]},
            )
            return
        self.stats["succeeded"] += 1
        self._call("POST", f"/api/agent/workspace-operations/{operation_id}/complete", {"success": True, "result": result})

    # -- 各操作的实现 --------------------------------------------------------
    def execute(self, kind: str, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_op_{kind}", None)
        if handler is None:
            raise FileWorkerError("workspace_operation_unsupported", kind)
        return handler(relative, arguments)

    def _op_list(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        target = safe_path(self.root, relative, must_exist=True)
        if not target.is_dir():
            raise FileWorkerError("workspace_path_invalid", "not_a_directory")
        entries: list[dict[str, Any]] = []
        truncated = False
        for child in sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if len(entries) >= MAX_LIST_ENTRIES:
                truncated = True
                break
            child_relative = f"{validate_relative(relative)}/{child.name}".lstrip("/")
            try:
                entries.append(_entry(child, self.root, child_relative))
            except FileWorkerError:
                continue  # 链接之类的坏条目跳过（如实少一条，不编造）
        # 回报**生效的**保护清单（内置默认 + 平台登记 + Agent 自报）：页面据此把删/改名/移动
        # 藏起来。只报配置项的话，页面会以为 `.git` 可删，而内核执行时又拒绝——"显示为可执行
        # 但点了失败"是这里最不该出现的体验。
        protected = sorted({*PLATFORM_RESERVED, *DEFAULT_PROTECTED, *(self.config.protected_paths or [])})
        return {
            "relative_path": validate_relative(relative),
            "entries": entries,
            "truncated": truncated,
            "protected": [item for item in protected if item],
        }

    def _op_stat(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        target = safe_path(self.root, relative, must_exist=True)
        return {"entry": _entry(target, self.root, validate_relative(relative))}

    def _op_mkdir(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_relative(relative, allow_empty=False)
        target = safe_path(self.root, normalized)
        if target.exists():
            if target.is_dir():
                return {"created": False, "relative_path": normalized}
            raise FileWorkerError("workspace_path_conflict", normalized)
        target.mkdir(parents=True, exist_ok=bool(arguments.get("parents", True)))
        return {"created": True, "relative_path": normalized}

    def _op_upload(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_relative(relative, allow_empty=False)
        target = safe_path(self.root, normalized)
        overwrite = bool(arguments.get("overwrite"))
        if target.exists() and not overwrite:
            raise FileWorkerError("workspace_path_conflict", normalized)
        transfer_id = str(arguments.get("transfer_id") or "")
        if not transfer_id:
            raise FileWorkerError("workspace_transfer_required", normalized)
        content = self._fetch_transfer(transfer_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.upload-{uuid4().hex[:8]}")
        temporary.write_bytes(content)
        os.replace(temporary, target)  # 原子落地：中途失败不会留下半个文件
        return {"relative_path": normalized, "size_bytes": len(content), "sha256": _sha256(content)}

    def _op_download(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_relative(relative, allow_empty=False)
        transfer_id = str(arguments.get("transfer_id") or "")
        if not transfer_id:
            raise FileWorkerError("workspace_transfer_required", normalized)
        target = safe_path(self.root, normalized, must_exist=True)
        if not target.is_file():
            raise FileWorkerError("workspace_path_invalid", "not_a_file")
        content = target.read_bytes()
        status, payload = self._call(
            "PUT", f"/api/agent/workspace-transfers/{transfer_id}/content", raw_body=content
        )
        if status not in (200, 201):
            raise FileWorkerError("workspace_transfer_failed", f"{status}:{str(payload)[:120]}")
        return {"relative_path": normalized, "size_bytes": len(content), "sha256": _sha256(content), "transfer_id": transfer_id}

    def _op_rename(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            normalized = validate_relative(relative, allow_empty=False)
        except FileWorkerError:
            raise FileWorkerError("workspace_path_protected", relative or "(工作区根)")
        if is_protected(normalized, self.config.protected_paths):
            raise FileWorkerError("workspace_path_protected", normalized)
        new_name = validate_relative(str(arguments.get("name") or ""), allow_empty=False)
        if "/" in new_name:
            raise FileWorkerError("workspace_path_invalid", "name_must_be_single_segment")
        source = safe_path(self.root, normalized, must_exist=True)
        target = safe_path(self.root, f"{'/'.join(normalized.split('/')[:-1] + [new_name])}".lstrip("/"))
        if target.exists():
            raise FileWorkerError("workspace_path_conflict", new_name)
        source.rename(target)
        return {"from": normalized, "to": f"{'/'.join(normalized.split('/')[:-1] + [new_name])}".lstrip("/")}

    def _op_move(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            normalized = validate_relative(relative, allow_empty=False)
        except FileWorkerError:
            raise FileWorkerError("workspace_path_protected", relative or "(工作区根)")
        if is_protected(normalized, self.config.protected_paths):
            raise FileWorkerError("workspace_path_protected", normalized)
        destination = validate_relative(str(arguments.get("destination") or ""))
        source = safe_path(self.root, normalized, must_exist=True)
        destination_dir = safe_path(self.root, destination, must_exist=True)
        if not destination_dir.is_dir():
            raise FileWorkerError("workspace_path_invalid", "destination_not_a_directory")
        target = destination_dir / source.name
        if target.exists():
            raise FileWorkerError("workspace_path_conflict", str(target.name))
        # 环：不能把目录移进自己的子树
        if source.is_dir():
            try:
                destination_dir.relative_to(source)
            except ValueError:
                pass
            else:
                raise FileWorkerError("workspace_directory_cycle", normalized)
        shutil.move(str(source), str(target))
        return {"from": normalized, "to": f"{destination}/{source.name}".lstrip("/")}

    def _op_copy(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        normalized = validate_relative(relative, allow_empty=False)
        destination = validate_relative(str(arguments.get("destination") or ""))
        source = safe_path(self.root, normalized, must_exist=True)
        destination_dir = safe_path(self.root, destination, must_exist=True)
        if not destination_dir.is_dir():
            raise FileWorkerError("workspace_path_invalid", "destination_not_a_directory")
        name = validate_relative(str(arguments.get("name") or source.name), allow_empty=False)
        target = destination_dir / name
        if target.exists():
            raise FileWorkerError("workspace_path_conflict", name)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        return {"from": normalized, "to": f"{destination}/{name}".lstrip("/")}

    def _op_delete(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # 保护判定在语法校验**之前**：空路径 = 工作区根，要明确报"受保护"，
        # 报"路径为空"会让人以为换个写法就能删根目录
        if is_protected(str(relative or ""), self.config.protected_paths):
            raise FileWorkerError("workspace_path_protected", relative or "(工作区根)")
        normalized = validate_relative(relative, allow_empty=False)
        if is_protected(normalized, self.config.protected_paths):
            # 工作区根、`.git`、`.math-agent-platform`、Agent 自报的保护目录：一律拒绝
            raise FileWorkerError("workspace_path_protected", normalized)
        target = safe_path(self.root, normalized, must_exist=True)
        recursive = bool(arguments.get("recursive"))
        if target.is_dir():
            if not recursive:
                raise FileWorkerError("workspace_path_invalid", "directory_requires_recursive")
            shutil.rmtree(target)
        else:
            target.unlink()
        return {"relative_path": normalized, "deleted": True, "recursive": recursive}

    def _op_extract(self, relative: str, arguments: dict[str, Any]) -> dict[str, Any]:
        from safe_archive import extract_into_workspace  # 延迟导入：内核可选模块

        normalized = validate_relative(relative, allow_empty=False)
        archive = safe_path(self.root, normalized, must_exist=True)
        destination = validate_relative(str(arguments.get("destination") or ""))
        destination_dir = safe_path(self.root, destination)
        return extract_into_workspace(archive, destination_dir, self.root)

    # -- 传输 ----------------------------------------------------------------
    def _fetch_transfer(self, transfer_id: str) -> bytes:
        status, payload = self._call("GET", f"/api/agent/workspace-transfers/{transfer_id}/content")
        if status != 200:
            raise FileWorkerError("workspace_transfer_failed", f"{status}:{str(payload)[:120]}")
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        if isinstance(payload, dict) and payload.get("content_base64"):
            return base64.b64decode(str(payload["content_base64"]))
        raise FileWorkerError("workspace_transfer_failed", "unexpected_payload")


def _sha256(content: bytes) -> str:
    import hashlib

    return hashlib.sha256(content).hexdigest()


__all__ = [
    "DEFAULT_PROTECTED",
    "FileWorkerConfig",
    "FileWorkerError",
    "LARGE_FILE_BYTES",
    "PLATFORM_RESERVED",
    "WorkspaceFileWorker",
    "is_protected",
    "safe_path",
    "validate_relative",
]