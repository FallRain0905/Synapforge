#!/usr/bin/env python
"""桌面内核入口（PyInstaller 用）。

同一个包打出两个 exe：
- `synapforge-core.exe`：GUI 子系统，跑 studio 控制台；
- `synapforge-mcp.exe`：控制台子系统，给 Claude Code 当 MCP stdio 服务
  （GUI exe 没有 stdout，不能当 stdio 传输，所以必须分开）。

开发时仍然走 `python scripts/synapforge_studio.py` / `scripts/synapforge_mcp.py`。
"""

from __future__ import annotations

import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    _LOCAL = Path(__file__).resolve().parent
    if str(_LOCAL) not in sys.path:
        sys.path.insert(0, str(_LOCAL))

from synapforge.cli import main  # noqa: E402


def _mcp_main(argv: list[str]) -> int:
    import argparse

    # 冻结的控制台 exe 默认用系统 ANSI 代码页（cp936/cp1252），工具清单里的中文
    # 一旦写 stdout 就 UnicodeEncodeError；MCP 协议两侧约定 UTF-8，这里强制对齐。
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    from synapforge.mcp import McpServer, serve_stdio

    parser = argparse.ArgumentParser(prog="synapforge-mcp",
                                     description="synapforge 本地协作协议的 MCP 薄壳（stdio）")
    parser.add_argument("--agent", default=None, help="默认 Agent id")
    parser.add_argument("--project", default=None, help="默认项目 id")
    parser.add_argument("--plan", default=None, help="默认规划 id（规划会话专用）")
    parser.add_argument("--state", default=None, help="状态库路径")
    args = parser.parse_args(argv)
    server = McpServer(agent_id=args.agent, project_id=args.project,
                       store_path=args.state, plan_id=args.plan)
    try:
        return serve_stdio(server)
    finally:
        server.close()


if __name__ == "__main__":
    frozen = bool(getattr(sys, "frozen", False))
    prog = Path(sys.executable).stem.lower() if frozen else ""
    if frozen and prog.endswith("-mcp"):
        raise SystemExit(_mcp_main(sys.argv[1:]))
    raise SystemExit(main(["studio", *sys.argv[1:]]))
