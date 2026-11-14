#!/usr/bin/env python
"""synapforge CLI 启动器。

用法：python scripts/synapforge.py <动词> ...
例如：python scripts/synapforge.py status --human
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LOCAL = _ROOT / "apps" / "local"
# 脚本目录是 sys.path[0]，里面的 .py 会遮蔽同名包；把包路径**强制置顶**。
_HERE = str(_LOCAL)
sys.path[:] = [entry for entry in sys.path if entry != _HERE]
sys.path.insert(0, _HERE)

from synapforge.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
