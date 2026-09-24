"""工作区快照与差分：判断"这次执行到底产出了哪些文件"（D-CL-2）。

为什么不用 `git status`：工作区常常不是 git 仓库（平台自己也这么认为，见 Codex 适配器的
`--skip-git-repo-check`），而且执行体会在工作区里创建新文件——`git status` 只认得被跟踪的树。

判定方式：执行前后各扫一次（相对路径 → 大小 + mtime），差集就是产出。
- **只看得到"新增/修改"**：删除不算产出（内容生命周期不处理删除，D-CL-7）；
- **剪枝**：依赖目录、构建产物、缓存与本地状态库不扫（否则一次执行能产出上千个无关文件）；
- **有上限**：文件数超过 `max_files` 就停止扫描并置 `truncated=True`，如实上报——不假装扫全了。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 目录名：依赖、构建产物、缓存、IDE、平台自己的运行状态
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".next",
        ".turbo",
        ".nuxt",
        "dist",
        "dist-sidecar",
        "build",
        "out",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        ".cache",
        # 平台自己写的运行清单/回答文件（不是任务产出）
        ".math-agent-platform",
    }
)

# 扩展名：编译产物、日志、临时文件、本地数据库
EXCLUDED_SUFFIXES = frozenset(
    {
        ".pyc",
        ".pyo",
        ".pyd",
        ".log",
        ".tmp",
        ".temp",
        ".db",
        ".db-journal",
        ".db-wal",
        ".db-shm",
        ".lock",
        ".map",
        ".swp",
    }
)

# 文件名：本地状态与启动信息（含平台地址/设备标识，不该进内容库）
EXCLUDED_NAMES = frozenset({"sidecar.json", "platform.json", "worker.json", ".DS_Store", "Thumbs.db"})

DEFAULT_MAX_FILES = 20_000


def is_excluded(relative_path: str) -> bool:
    """是否属于"不该当产出"的路径（相对路径，正斜杠分隔）。"""

    parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
    if any(part in EXCLUDED_DIRS for part in parts[:-1]):
        return True
    name = parts[-1] if parts else ""
    if name in EXCLUDED_NAMES:
        return True
    return Path(name).suffix.lower() in EXCLUDED_SUFFIXES


def snapshot(
    workspace: str | Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    excluded_dirs: frozenset[str] = EXCLUDED_DIRS,
) -> tuple[dict[str, tuple[int, int]], bool]:
    """扫描工作区：`{相对路径: (大小, mtime_ns)}` + 是否因上限被截断。"""

    root = Path(workspace).expanduser().resolve()
    stamps: dict[str, tuple[int, int]] = {}
    truncated = False
    if not root.is_dir():
        return stamps, truncated

    for current, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = sorted(name for name in dirnames if name not in excluded_dirs)
        for name in sorted(filenames):
            if len(stamps) >= max_files:
                truncated = True
                return stamps, truncated
            path = Path(current) / name
            relative = os.path.relpath(path, root).replace(os.sep, "/")
            if is_excluded(relative):
                continue
            try:
                stat = path.stat()
            except OSError:
                # 扫的过程中文件被删/没权限：跳过而不是让整次采集失败
                continue
            stamps[relative] = (int(stat.st_size), int(stat.st_mtime_ns))
    return stamps, truncated


@dataclass(frozen=True)
class SnapshotDiff:
    created: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    before_truncated: bool = False
    after_truncated: bool = False

    @property
    def changed(self) -> list[str]:
        """产出候选（按路径排序，保证同一批上传顺序稳定、幂等键稳定）。"""

        return sorted({*self.created, *self.modified})

    def as_dict(self) -> dict[str, object]:
        return {
            "created": self.created,
            "modified": self.modified,
            "deleted": self.deleted,
            "changed": self.changed,
            "before_truncated": self.before_truncated,
            "after_truncated": self.after_truncated,
        }


def diff(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
    *,
    before_truncated: bool = False,
    after_truncated: bool = False,
) -> SnapshotDiff:
    """比较两次快照。(大小, mtime) 任一变化即视为修改。"""

    created = [path for path in after if path not in before]
    modified = [path for path in after if path in before and after[path] != before[path]]
    deleted = [path for path in before if path not in after]
    return SnapshotDiff(
        created=sorted(created),
        modified=sorted(modified),
        deleted=sorted(deleted),
        before_truncated=before_truncated,
        after_truncated=after_truncated,
    )


__all__ = [
    "DEFAULT_MAX_FILES",
    "EXCLUDED_DIRS",
    "EXCLUDED_NAMES",
    "EXCLUDED_SUFFIXES",
    "SnapshotDiff",
    "diff",
    "is_excluded",
    "snapshot",
]