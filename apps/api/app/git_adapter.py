"""Small provider-neutral Git adapter used for reproducibility metadata."""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitFile:
    path: str
    content_hash: str
    size_bytes: int


class LocalGitProvider:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise ValueError("git_root_must_be_directory")

    def _run(self, *args: str) -> str:
        try:
            result = subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise ValueError("git_command_failed") from error
        return result.stdout.strip()

    def head(self) -> str:
        return self._run("rev-parse", "HEAD")

    def commit_exists(self, commit_sha: str) -> bool:
        try:
            self._run("cat-file", "-e", f"{commit_sha}^{{commit}}")
        except ValueError:
            return False
        return True

    def index_commit(self, commit_sha: str | None = None) -> list[GitFile]:
        commit = commit_sha or self.head()
        if not self.commit_exists(commit):
            raise ValueError("git_commit_not_found")
        paths = self._run("ls-tree", "-r", "--name-only", commit).splitlines()
        result: list[GitFile] = []
        for relative in paths:
            try:
                content = subprocess.run(["git", "show", f"{commit}:{relative}"], cwd=self.root, check=True, capture_output=True).stdout
            except (OSError, subprocess.CalledProcessError) as error:
                raise ValueError("git_file_read_failed") from error
            result.append(GitFile(path=relative, content_hash=hashlib.sha256(content).hexdigest(), size_bytes=len(content)))
        return result

