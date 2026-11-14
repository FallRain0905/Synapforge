#!/usr/bin/env python
"""本地 Agent 调度器：把"有人有活"变成"跑一轮 Agent"。

**这不是服务器。** 没有端口、不长期持有协议状态、不参与任何判断——它只是循环读
`inbox`，发现有活就 spawn 一个本机 Agent 进程（`claude -p`），跑完再看下一轮。
协议状态一直只在 `~/.synapforge/state.db` 里。

用法：
    python scripts/synapforge_run.py --project <pid> --agents agent-a,agent-b

每一轮：对一个 Agent 跑一次"处理收件箱里最优先的一项，然后停"。
Agent 自己决定要不要问、要不要写、要不要收尾；调度器不替它做决定。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_LOCAL = _ROOT / "apps" / "local"
_HERE = str(_LOCAL)
sys.path[:] = [entry for entry in sys.path if entry != _HERE]
sys.path.insert(0, _HERE)

from synapforge import commands  # noqa: E402
from synapforge.db import open_store  # noqa: E402
from synapforge.paths import state_dir  # noqa: E402

GENERIC_PROMPT = """\
你是通过 synapforge 协作协议工作的 Agent：{agent}。

现在只做**一件事**：处理收件箱里最优先的一项，做完立刻停。

1. 调用 synapforge_inbox，看这四块：
   assigned_to_me（派给我还没领）、in_flight_tasks（我领了在跑，带 lease_token）、
   to_answer（要我回答）、to_consume（等我消费的回复）。
2. 按优先级只处理**一项**：
   a) to_consume 非空 → synapforge_consume 消费，然后继续推进那个任务（见 3）
   b) to_answer 非空 → synapforge_answer 回答（答不上来用 unavailable，绝不编造），然后停
   c) assigned_to_me 非空 → synapforge_claim 领取，synapforge_start 置 RUNNING，然后见 3
   d) in_flight_tasks 非空 → 见 3
3. 推进任务时：
   - 先用 synapforge_say 在群聊里说一句你打算做什么（一句话即可）
   - 缺信息（不知道接口契约、字段名、上游产物）→ synapforge_ask 向对方询问，然后**立刻停**等回答
   - 做完了 → 把产出写进工作目录，synapforge_artifact 登记，synapforge_finish 收尾，
     再用 synapforge_say 汇报一句产出
4. 没有任何待办就停，不要自己造任务。

规矩：
- 不确定就问，绝不猜测或编造。
- 一次只推进一个任务，不要一口气做完所有事。
- 你的工作目录是 {workspace}；这个环境里 Bash 不可用，用 Write 工具写文件。
"""


def find_claude() -> str | None:
    """Claude Code 装在 VS Code 扩展里时不在 PATH 上。"""

    direct = shutil.which("claude")
    if direct:
        return direct
    base = Path.home() / ".vscode" / "extensions"
    if base.exists():
        candidates = sorted(base.glob("anthropic.claude-code-*/resources/native-binary/claude.exe"),
                            reverse=True)
        if candidates:
            return str(candidates[0])
    return None


def has_work(box: dict[str, Any]) -> bool:
    return bool(box.get("assigned_to_me") or box.get("in_flight_tasks")
                or box.get("to_answer") or box.get("to_consume"))


def describe(box: dict[str, Any]) -> str:
    parts = []
    if box.get("assigned_to_me"):
        parts.append(f"待领 {len(box['assigned_to_me'])}")
    if box.get("in_flight_tasks"):
        parts.append(f"在跑 {len(box['in_flight_tasks'])}")
    if box.get("to_answer"):
        parts.append(f"待答 {len(box['to_answer'])}")
    if box.get("to_consume"):
        parts.append(f"待消费 {len(box['to_consume'])}")
    return " · ".join(parts) or "空闲"


def write_mcp_config(path: Path, agent_id: str, project_id: str) -> None:
    config = {"mcpServers": {"synapforge": {
        "command": sys.executable,
        "args": [str(_ROOT / "scripts" / "synapforge_mcp.py"),
                 "--agent", agent_id, "--project", project_id],
    }}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def run_agent(claude: str, agent_id: str, project_id: str, workspace: Path,
              config: Path, timeout: int) -> tuple[int, str]:
    environment = dict(os.environ)
    environment["PYTHONUTF8"] = "1"
    environment["PYTHONIOENCODING"] = "utf-8"
    command = [
        claude, "-p", GENERIC_PROMPT.format(agent=agent_id, workspace=str(workspace)),
        "--mcp-config", str(config), "--strict-mcp-config",
        "--permission-mode", "acceptEdits",
        "--allowedTools", "mcp__synapforge,Write,Read",
    ]
    try:
        completed = subprocess.run(command, cwd=str(workspace), capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   timeout=timeout, env=environment)
    except subprocess.TimeoutExpired:
        return 124, "（超时）"
    return completed.returncode, (completed.stdout or completed.stderr or "").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="synapforge-run",
                                     description="本地 Agent 调度器（不是服务器）")
    parser.add_argument("--project", required=True)
    parser.add_argument("--agents", required=True, help="逗号分隔")
    parser.add_argument("--rounds", type=int, default=8, help="最多几轮")
    parser.add_argument("--timeout", type=int, default=480, help="单次运行超时秒")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--claude", default=None, help="claude 可执行文件路径")
    parser.add_argument("--state", default=None)
    args = parser.parse_args(argv)

    agents = [a.strip() for a in args.agents.split(",") if a.strip()]
    if not agents:
        print("至少要一个 Agent", file=sys.stderr)
        return 2

    claude = args.claude or find_claude()
    if not claude:
        print("找不到 claude 可执行文件；用 --claude 指定", file=sys.stderr)
        return 2

    base = state_dir() / "apps" / "run" / args.project[:8]
    store = open_store(args.state) if args.state else open_store()
    for agent_id in agents:
        commands.ensure_participant(store, project_id=args.project, agent_id=agent_id)
        write_mcp_config(base / f"mcp-{agent_id}.json", agent_id, args.project)
        (base / agent_id).mkdir(parents=True, exist_ok=True)

    print(f"项目 {args.project[:8]} · Agent {', '.join(agents)} · 最多 {args.rounds} 轮")
    print(f"claude: {claude}")
    print()

    for round_index in range(1, args.rounds + 1):
        busy = []
        for agent_id in agents:
            box = commands.inbox(store, agent_id=agent_id, project_id=args.project)
            if has_work(box):
                busy.append((agent_id, box, describe(box)))
        if not busy:
            print(f"[轮 {round_index}] 全部空闲，结束")
            break
        print(f"[轮 {round_index}] " + " | ".join(f"{a}: {d}" for a, _, d in busy))
        for agent_id, _, _ in busy:
            started = time.time()
            code, output = run_agent(claude, agent_id, args.project, base / agent_id,
                                     base / f"mcp-{agent_id}.json", args.timeout)
            elapsed = time.time() - started
            first_line = (output.splitlines() or [""])[0][:110]
            print(f"   {agent_id:<16} exit={code} {elapsed:5.1f}s  {first_line}")
            if code != 0:
                print(f"   ↳ {output[-400:]}")
            time.sleep(args.interval)
        print()

    print("最终状态：")
    for agent_id in agents:
        box = commands.inbox(store, agent_id=agent_id, project_id=args.project)
        print(f"  {agent_id:<16} {describe(box)}")
    print()
    for task in commands.status(store, project_id=args.project)["tasks"]:
        print(f"  [{task['status']:<14}] {task['title']}")
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
