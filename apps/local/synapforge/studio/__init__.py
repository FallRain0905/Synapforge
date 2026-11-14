"""synapforge studio：本地 Agent 集群工作台。

与只读查看器（`synapforge ui`）分开：studio 是控制面，负责集群/角色/运行、
进程调度与长周期状态；协议事实仍只写进 `~/.synapforge/state.db`。
"""

from __future__ import annotations

__all__ = ["store", "commands", "backends", "supervisor", "server"]
