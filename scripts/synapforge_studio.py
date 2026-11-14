#!/usr/bin/env python
"""启动本地 Agent 集群控制台（synapforge studio）。

用法：python scripts/synapforge_studio.py [--port 0] [--no-browser] [--state <db>]
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LOCAL = _ROOT / "apps" / "local"
# 脚本目录是 sys.path[0]，强制把包路径置顶，避免同名 .py 遮蔽。
_HERE = str(_LOCAL)
sys.path[:] = [entry for entry in sys.path if entry != _HERE]
sys.path.insert(0, _HERE)

from synapforge.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main(["studio", *sys.argv[1:]]))
