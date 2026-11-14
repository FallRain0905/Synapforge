#!/usr/bin/env python
"""synapforge MCP server（stdio）。

给 Agent 宿主（Claude Code / Cursor / ZCode 等）配置的启动命令：
    python <repo>/scripts/synapforge_mcp.py --agent <agent_id> --project <project_id>

stdout 只放 JSON-RPC 协议消息；日志一律走 stderr。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LOCAL = _ROOT / "apps" / "local"
# 脚本目录是 sys.path[0]，里面的 .py 会遮蔽同名包；把包路径**强制置顶**。
_HERE = str(_LOCAL)
sys.path[:] = [entry for entry in sys.path if entry != _HERE]
sys.path.insert(0, _HERE)

from synapforge.mcp import serve_stdio, McpServer  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synapforge-mcp",
                                     description="synapforge 本地协作协议的 MCP 薄壳（stdio）")
    parser.add_argument("--agent", default=None, help="默认 Agent id")
    parser.add_argument("--project", default=None, help="默认项目 id")
    parser.add_argument("--plan", default=None, help="默认规划 id（规划会话专用）")
    parser.add_argument("--state", default=None, help="状态库路径（默认 ~/.synapforge/state.db）")
    args = parser.parse_args(argv)

    server = McpServer(agent_id=args.agent, project_id=args.project,
                       store_path=args.state, plan_id=args.plan)
    try:
        return serve_stdio(server)
    finally:
        server.close()


if __name__ == "__main__":
    sys.exit(main())
