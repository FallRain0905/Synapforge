from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from app.contracts import GitRepositoryCreate
from app.store import Store


class GitIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.store = Store(self.root / "platform.db")
        self.project = self.store.list_projects()[0]
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self._git("init")
        self._git("config", "user.email", "test@example.local")
        self._git("config", "user.name", "Contract Test")

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def _git(self, *args: str) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    def test_local_git_commit_is_indexed(self) -> None:
        (self.repo / "solve.py").write_text("print('ok')\n", encoding="utf-8")
        self._git("add", "solve.py")
        self._git("commit", "-m", "initial")
        repository = self.store.register_git_repository(GitRepositoryCreate(project_id=self.project.id, local_path=str(self.repo)))
        self.assertTrue(repository.head_commit)
        indexed = self.store.index_git_repository(self.project.id)
        self.assertEqual(len(indexed), 1)
        self.assertEqual(indexed[0].path, "solve.py")
        self.assertEqual(len(indexed[0].content_hash), 64)
        self.assertEqual(self.store.list_git_index(self.project.id)[0].commit_sha, repository.head_commit)


if __name__ == "__main__":
    unittest.main()
