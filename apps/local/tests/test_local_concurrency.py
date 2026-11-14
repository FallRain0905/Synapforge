"""多进程并发写：本地形态的前提。

探针阶段这一项**没有证明**（那次失败是探针自身漏了 `events.sequence` 的 NOT NULL，
不是锁冲突）。这里用真子进程补上：多个 Agent 各开自己的连接、自己的进程写同一个
状态文件，全部必须成功。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from synapforge import commands
from synapforge.db import open_store

WORKER = Path(__file__).resolve().parent / "_concurrency_worker.py"
WORKERS = 4
ITERATIONS = 6


class MultiProcessWriteTests(unittest.TestCase):
    def test_concurrent_writers_all_succeed(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="synapforge-conc-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        db_path = tmp / "state.db"
        store = open_store(db_path)
        pid = commands.ensure_project(store, "concurrency")["id"]
        store.close()

        processes = [
            subprocess.Popen(
                [sys.executable, str(WORKER), str(db_path), str(pid), f"agent-{index}", str(ITERATIONS)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for index in range(WORKERS)
        ]
        results = [(p.wait(timeout=120), *p.communicate()) for p in processes]

        for code, out, err in results:
            self.assertEqual(code, 0, f"worker failed:\nstdout={out}\nstderr={err}")

        store = open_store(db_path)
        self.addCleanup(store.close)
        rows = store.db.execute(
            "SELECT COUNT(*) AS c FROM tasks WHERE stage = 'concurrency'").fetchone()
        self.assertEqual(int(rows["c"]), WORKERS * ITERATIONS,
                         "并发的每个写都必须落库，不许有静默丢失")

        granted = {r["agent_id"] for r in store.db.execute(
            "SELECT agent_id FROM agent_project_grants WHERE project_id = ?",
            (str(pid),)).fetchall()}
        self.assertEqual(granted, {f"agent-{i}" for i in range(WORKERS)},
                         "授权必须只包含真实参与的 Agent——不许有播种进来的 phantom 授权")

        known_agents = {r["agent_id"] for r in store.db.execute(
            "SELECT agent_id FROM agents").fetchall()}
        self.assertEqual(known_agents, {f"agent-{i}" for i in range(WORKERS)},
                         "本地库不许被平台演示数据播种（agent-nova/mira/aster 不应出现）")


if __name__ == "__main__":
    unittest.main()
