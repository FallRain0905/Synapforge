"""并发写工作进程：由 test_local_concurrency 用 subprocess 拉起。

不是一个"演示脚本"——它必须真的开自己的进程、自己的连接，才能证明多进程写成立。
"""

from __future__ import annotations

import sys
from pathlib import Path

_LOCAL_ROOT = Path(__file__).resolve().parents[1]
if str(_LOCAL_ROOT) not in sys.path:
    sys.path.insert(0, str(_LOCAL_ROOT))

from synapforge import commands  # noqa: E402
from synapforge.db import open_store  # noqa: E402


def main() -> int:
    db_path = sys.argv[1]
    project_id = sys.argv[2]
    agent_id = sys.argv[3]
    iterations = int(sys.argv[4])

    store = open_store(db_path)
    try:
        commands.ensure_participant(store, project_id=project_id, agent_id=agent_id)
        for index in range(iterations):
            commands.create_task(store, project_id=project_id, stage="concurrency",
                                 title=f"{agent_id}-{index}")
    finally:
        store.close()
    print(f"ok {agent_id} {iterations}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
