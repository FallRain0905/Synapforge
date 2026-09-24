"""M-6 角色验收（在**平台机**上跑）：角色真的从平台传到执行体、并且真的改变行为。

断言（都从真实数据来，不看角色自述）：
  1. **角色被发现**：`/api/my-agent/list_my_agents` 那条执行体上报的角色里含我们装的角色（含内置 `plan`）；
  2. **角色传下去了**：每个角色各真跑一轮，`agent_turns.role` 等于所选角色，且这一轮有 `process.started`（真起了进程）；
  3. **角色改变了输出**：回复呈现**该角色才有的形状**（关键词命中）；
  4. **工具边界生效**：只读角色那一轮**没有 `file.changed`**；`mm-coding` 必须真的**用过工具**（它的承诺就是写脚本跑脚本）；
  5. **不得越界**：`mm-paper-zh-docx`（Word 路径）的回复里**不许出现任何 LaTeX 命令**；
  6. **用量可读**：每轮回报 total_tokens > 0（角色变长会让每步输入变大，这个数就是代价的度量）。

用法：
  cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_m6_roles_verify.py
  ... --only mm-analysis,mm-critique     # 只跑指定角色（分批跑，一轮一轮串行，很慢）
跑完**自清理**（删掉自己建的会话；不碰用户已有的对话）。
"""

from __future__ import annotations

import json
import sys
import time
import uuid

sys.path.insert(0, "apps/api")

from app import agent_chat  # noqa: E402
from app.contracts import AgentChatConversationCreate, AgentChatMessageCreate  # noqa: E402
from app.store import Store  # noqa: E402

OWNER = "b696e695-0942-4ffd-8695-9ded3e442715"  # 用户账号（设备归属人）
DEVICE = "device-cloud-01"
PROJECT = "06f9df2b-264d-4d7d-9ea7-0bff0bbb6c2c"
WAIT_SECONDS = 300

# 只读角色（不应产生 file.changed）；其余是"能动手"的角色
READONLY_ROLES = {
    "mm-analysis",
    "mm-review",
    "mm-critique",
    "mm-paper-zh",
    "mm-paper-zh-docx",
    "mm-paper-en",
    "mm-research",
    "mm-topic",
}

# 每个角色配一句"只有它会这么答"的请求 + 期望命中的形状关键词（≥2 个命中即算）
CASES = [
    {
        "role": "mm-review",
        "prompt": "我算了个效率 η=1-Cout/Cin，测出来 Cout=100，题目说传感器量程上限就是 100，所以我取 η 的下界是 0.5。这样对吗？",
        "expect": ("findings", "fatal", "严重性", "上界", "下界", "验证"),
    },
    {
        "role": "mm-research",
        "prompt": "我论文引了这篇：「Zhang et al., 2019, UAV path planning with energy constraints, IEEE T-ITS」。帮我核一下能不能引。",
        "expect": ("未核验", "存疑", "核验", "DOI", "证据", "检索式"),
    },
    {
        "role": "mm-paper-zh",
        "prompt": "我的论文摘要怎么写？目前只有第二问的结果：最优成本 1.2e6 元。",
        "expect": ("摘要", "段", "问题", "数值", "骨架"),
    },
    {
        "role": "mm-analysis",
        "prompt": "某城市要建 3 个无人机配送站，使最大响应时间最小。请帮我拆题。",
        "expect": ("子问题", "对齐", "作用对象", "待确认", "能力", "给定"),
    },
    {
        "role": "mm-modeling",
        "prompt": "我要建一个选址优化模型：最小化最大响应时间，建 3 个站。假设怎么写？",
        "expect": ("假设", "参数化", "灵敏度", "声称", "must", "forbid", "口径"),
    },
    {
        "role": "mm-coding",
        "prompt": "用 python 算一下：候选点坐标 (0,0) (3,4) (6,0) (3,-4) (10,10)，选 3 个使最大覆盖半径最小。写代码跑出来给我数。",
        "expect": ("脚本", "python", "结果", "半径", "断言", "可选", ".py"),
    },
    {
        "role": "mm-figure",
        "prompt": "我有 5 个方案的 3 个指标对比数据，想放一张图说明哪个方案最好。该用什么图？",
        "expect": ("图型", "要证明", "图注", "解读", "数据", "不要用"),
    },
    {
        "role": "mm-critique",
        "prompt": "帮我评一下我的摘要：「本文研究了无人机配送站选址问题，建立了优化模型，通过求解得到了较好的结果，具有重要的理论意义和应用价值。」",
        "expect": ("维度", "权重", "得分", "返工", "证据", "数值"),
    },
    {
        "role": "mm-paper-zh-docx",
        "prompt": "我要交 Word 版，第三章（模型建立与求解）该怎么写？给我可以直接粘进 Word 的骨架。",
        "expect": ("章节", "粘", "Word", "骨架", "段落"),
        "forbid_substrings": ("\\begin{", "\\section{", "\\cite{", "\\input{", "\\ref{"),
    },
    {
        "role": "mm-paper-en",
        "prompt": "帮我写 Summary Sheet 的前两句：题目是无人机配送站选址，第二问最优覆盖半径 4.2 km。",
        "expect": ("Summary", "problem", "km", "数值", "一句话", "For Problem"),
    },
    {
        "role": "mm-compile",
        "prompt": "我的论文编不过，报 undefined control sequence，怎么办？",
        "expect": ("编译", "报错", "归零", "检查", "日志", "xelatex", "未验"),
    },
    {
        "role": "mm-topic",
        "prompt": "统计建模大赛，我想做「双碳」方向，能做什么题？",
        "expect": ("研究类型", "数据", "可得", "方法", "指标", "题目"),
    },
]


def main() -> int:
    args = sys.argv[1:]
    only: set[str] | None = None
    if "--only" in args:
        only = {item.strip() for item in args[args.index("--only") + 1].split(",") if item.strip()}
    cases = [case for case in CASES if only is None or case["role"] in only]

    store = Store("apps/api/data/platform.db")
    ok = True
    created: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        nonlocal ok
        print(("  [PASS] " if condition else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))
        ok = ok and condition

    print("== 1. 执行体上报的角色 ==")
    endpoints = agent_chat.list_my_agents(store, OWNER)
    endpoint = next((item for item in endpoints if item.device_id == DEVICE), None)
    if endpoint is None:
        print("  [FAIL] 没找到云端执行体（设备未在线/未授权）")
        store.close()
        return 1
    names = [role.name for role in endpoint.roles]
    executing = {role.name: role.executes for role in endpoint.roles}
    print(f"  上报角色（{len(names)}）：{names}")
    for role in [case["role"] for case in cases] + ["plan"]:
        check(f"角色 {role} 出现在下拉数据里", role in names)
    if only is None:
        for role in ("mm-coding", "mm-figure", "mm-compile", "mm-modeling"):
            check(f"{role} 被标为可执行", executing.get(role) is True, f"executes={executing.get(role)}")
        for role in ("mm-review", "mm-analysis", "mm-critique", "mm-topic"):
            check(f"{role} 被标为只读", executing.get(role) is False, f"executes={executing.get(role)}")

    print(f"== 2-6. {len(cases)} 个角色各真跑一轮 ==")
    for case in cases:
        role = case["role"]
        conversation = agent_chat.create_conversation(
            store,
            OWNER,
            AgentChatConversationCreate(
                project_id=PROJECT, device_id=DEVICE, role=role, title=f"[M6 验证] {role}",
            ),
        )
        created.append(str(conversation.id))
        check(f"{role}: 会话记住了角色", conversation.role == role)
        turn = agent_chat.append_message(
            store, conversation.id, OWNER, AgentChatMessageCreate(content=case["prompt"])
        )
        check(f"{role}: 轮次带上角色（历史里「谁跑的」如实）", turn.role == role)
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            current = agent_chat.get_turn(store, turn.id)
            if current.status in {"DONE", "FAILED", "CANCELLED"}:
                break
            time.sleep(5)
        current = agent_chat.get_turn(store, turn.id)
        check(f"{role}: 一轮跑完", current.status == "DONE", current.status + " " + current.error[:160])
        content = current.content or ""
        hits = [word for word in case["expect"] if word in content]
        check(f"{role}: 回复呈现该角色的形状", len(hits) >= 2, "命中 " + "/".join(hits) + f"（{len(content)} 字）")
        forbidden = [word for word in case.get("forbid_substrings", ()) if word in content]
        if case.get("forbid_substrings"):
            check(f"{role}: 没有越界内容（禁项 0 命中）", not forbidden, "出现 " + "/".join(forbidden))
        events = agent_chat.list_turn_events(store, turn.id, OWNER)
        kinds = [event.event_type for event in events]
        check(f"{role}: 有 process.started（真起了进程）", "process.started" in kinds, ",".join(sorted(set(kinds))))
        if role in READONLY_ROLES:
            check(f"{role}: 只读角色没有改文件", "file.changed" not in kinds)
        if role == "mm-coding":
            check(f"{role}: 真用过工具（它的承诺是写脚本跑脚本）", "tool.completed" in kinds, ",".join(sorted(set(kinds))))
        total = int((current.usage or {}).get("total_tokens") or 0)
        check(f"{role}: 用量可读", total > 0, f"{total} tokens = {json.dumps(current.usage, ensure_ascii=False)[:70]}")

    print("== 清理 ==")
    for conversation_id in created:
        agent_chat.delete_conversation(store, uuid.UUID(conversation_id), OWNER)
    leftovers = store.db.execute(
        "SELECT COUNT(*) FROM agent_conversations WHERE title LIKE '[M6 验证]%'"
    ).fetchone()[0]
    check("临时会话已删除", leftovers == 0)

    store.close()
    print("\n=== 结果：" + ("全部通过" if ok else "有失败项") + " ===")
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()