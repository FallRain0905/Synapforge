#!/usr/bin/env python
"""Stage 1 端到端演示：两个本机 Agent，真实模型，零服务器。

跑法：
    python scripts/synapforge_demo.py

它做的事：
1. 在本地状态库里建一个演示项目与两个 Agent（都只授权到该项目）；
2. 给 Agent A 一个必须依赖外部事实才能完成的任务；
3. A 发现自己缺信息 → `ask` 向 B；B 取到 → `answer`；A `consume` 后继续；
4. 打印任务状态轨迹与持久化事件。

凭据从 `~/.synapforge/config.json` 读（仓库外；本仓库是公开的）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_LOCAL = _ROOT / "apps" / "local"
# 脚本目录是 sys.path[0]，里面的 .py 会遮蔽同名包；把包路径**强制置顶**。
_HERE = str(_LOCAL)
sys.path[:] = [entry for entry in sys.path if entry != _HERE]
sys.path.insert(0, _HERE)

from synapforge import commands  # noqa: E402
from synapforge.db import open_store  # noqa: E402
from synapforge.llm import OpenAICompatLlm  # noqa: E402
from synapforge.paths import db_path, llm_config  # noqa: E402
from synapforge.runtime import LocalAgent  # noqa: E402

PROJECT_NAME = "本机协作演示"
AGENT_A = "agent-writer"
AGENT_B = "agent-facts"

ROLE_A = (
    "你是写作 Agent。你**没有**任何关于 synapforge 产品定位的信息，"
    "凡是涉及定位、目标、核心价值的事实都必须向 peer 索取，绝不许编造。"
)
ROLE_B = (
    "你是事实提供 Agent。你掌握的事实如下（除这些之外你不知道别的）：\n"
    "  - synapforge 是一个运行在本机的多 Agent 协作协议工具。\n"
    "  - 它让同一台电脑上的多个 AI Agent 通过 CLI / MCP 互相请求信息与交接工作。\n"
    "  - 核心资产是协作协议本身（阶段模型、证据纪律、诚实停止），不是网页平台。"
)

TASK_TITLE = "写一句 synapforge 的简介并落成文件"
TASK_DESC = (
    "用不超过 30 个字介绍 synapforge，并把结果**写进工作区文件** `intro.md`"
    "（该文件只放这句简介，不要放别的）。定位必须准确，不确定就向 peer 求证，不要编造。"
)


def main() -> int:
    cfg = llm_config()
    if not cfg["api_key"]:
        print("缺少 LLM 凭据：请在 ~/.synapforge/config.json 里配置 llm.{base_url,api_key,model}",
              file=sys.stderr)
        return 2

    store = open_store()
    project = commands.ensure_project(store, PROJECT_NAME, task_mode="manual")
    pid = project["id"]
    for agent_id in (AGENT_A, AGENT_B):
        commands.ensure_participant(store, project_id=pid, agent_id=agent_id)
    task = commands.create_task(store, project_id=pid, title=TASK_TITLE,
                                description=TASK_DESC, stage="demo",
                                assignee_agent_id=AGENT_A)
    store.close()

    print(f"项目 {PROJECT_NAME} ({pid[:8]})")
    print(f"任务 {task['id'][:8]}  {TASK_TITLE}")
    print(f"  指派给 {task.get('assignee')} —— 别的 Agent 领不到（Agent 级归属）")
    print()

    llm_a = OpenAICompatLlm(cfg["base_url"], cfg["api_key"], cfg["model"])
    llm_b = OpenAICompatLlm(cfg["base_url"], cfg["api_key"], cfg["model"])

    agent_a = LocalAgent(AGENT_A, pid, role=ROLE_A, llm=llm_a, peer_ids=[AGENT_B])
    agent_b = LocalAgent(AGENT_B, pid, role=ROLE_B, llm=llm_b, peer_ids=[AGENT_A])
    timeline: list[tuple[str, str]] = []

    def snapshot(label: str) -> None:
        row = agent_a.store.db.execute("SELECT status FROM tasks WHERE id = ?",
                                       (str(task["id"]),)).fetchone()
        status = str(row["status"]) if row else "?"
        timeline.append((label, status))
        print(f"  [{label}] 任务状态 = {status}")

    try:
        print("轮 1：A 领活并尝试完成")
        record = agent_a.run_once()
        work = record.get("work") or {}
        print(f"  A 的动作：{work.get('action')}")
        if work.get("action") == "ask":
            print(f"  A 问 {work.get('to')}：{work.get('request_id', '')[:8]}")
            print(f"  节点效果：{work.get('node_effect_result')}")
        else:
            print(f"  完整记录：{json.dumps(work, ensure_ascii=False)}")
        snapshot("轮1 之后")

        print()
        print("轮 2：B 取到请求并回答")
        record_b = agent_b.run_once()
        for item in record_b.get("answered") or []:
            print(f"  B 回答：{item.get('summary')}")
        if not record_b.get("answered"):
            print("  （B 没有待回答的请求）")

        print()
        print("轮 3：A 消费回复并继续")
        record_a2 = agent_a.run_once()
        for item in record_a2.get("consumed") or []:
            print(f"  A 消费：{item.get('question')} → {item.get('node_effect_result')}")
        work2 = record_a2.get("work") or {}
        print(f"  A 的动作：{work2.get('action')}")
        if work2.get("written"):
            print(f"  写入工作区：{work2['written']}  （工作区 {agent_a.workspace}）")
        for item in work2.get("artifacts") or []:
            print(f"  成果物 {item['name']}  输出哈希 {item.get('output_hash')}  "
                  f"{item.get('size_bytes')} bytes")
        if work2.get("artifact_errors"):
            print(f"  产出登记问题：{work2['artifact_errors']}")
        if work2.get("message"):
            print(f"  收尾信息：{work2.get('message')}")
        snapshot("轮3 之后")

        print()
        print("成果物清单（含溯源回执）：")
        artifacts_list = commands.list_artifacts(agent_a.store, project_id=pid)
        for item in artifacts_list:
            print(f"  {item['name']}  status={item['status']}  v{item['version']}  "
                  f"receipt=v{item['receipt_version']}  channel={item['tool_name']}")
            print(f"    output_hash={item.get('output_hash')}  bytes={item.get('output_bytes')}")
            print(f"    content_hash={item.get('content_hash')}")
        if artifacts_list:
            read_back = commands.read_artifact(agent_a.store, artifact_id=artifacts_list[0]["id"])
            print()
            print("读回成果物内容（来自状态库的对象存储，不是工作区文件）：")
            print(f"  {read_back['name']}  {read_back['size_bytes']} bytes")
            print(f"  {read_back['text']!r}")

        print()
        print("持久化事件（事实源）：")
        for event in commands.recent_events(agent_a.store, project_id=pid, limit=40):
            print(f"  {event['sequence']:>4}  {event['event_type']}")
    finally:
        agent_a.close()
        agent_b.close()

    print()
    print("状态轨迹：")
    for label, status in timeline:
        print(f"  {label}: {status}")
    print()
    print(f"本地状态库：{db_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
