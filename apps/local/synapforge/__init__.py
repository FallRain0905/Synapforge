"""synapforge 本地协作工具。

本包**复用** `apps/api` 里的协议实现（coordination / coordination_store / store），
不复制一份——复制会立刻产生两份真源。导入本包即把仓库路径装好，调用方不必自己拼
PYTHONPATH。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 桌面壳拿它做「关于」与 /health；与平台版本解耦，单独递增。
__version__ = "0.4.1"

_REPO_ROOT = Path(__file__).resolve().parents[3]
_API_ROOT = _REPO_ROOT / "apps" / "api"
# apps/agent 里有三个**纯净可复用**的模块：workspace_scan（快照/差分）、
# result_uploader.OutputDiscovery（哈希/MIME/类型推断）、receipts（RECEIPT_FORMAT v1）。
# 复用而不是复制——复制会立刻产生两份真源。
_AGENT_ROOT = _REPO_ROOT / "apps" / "agent"

for _path in (_AGENT_ROOT, _API_ROOT, _REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

__all__ = ["_AGENT_ROOT", "_API_ROOT", "_REPO_ROOT"]
