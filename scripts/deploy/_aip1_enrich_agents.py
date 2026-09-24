"""AIP-1a 演示数据补齐：给示例执行体声明能力卡（技能名 + 版本 + 输入 + 输出）。

为什么单独一个脚本：`_demo_doc_writing.py` 是"项目不存在才种"的，已经种过的库不会重跑；
而 AIP-1a 上线后能力卡是新的展示面（`/team` 能力目录 + "这几台能跑"候选），
演示库里那几个**示例执行体**（写手/复核/模型/仿真/写作 Agent）需要补上卡片才能体现出来。

幂等：只写 `capability_cards = '[]'`（或与目标不同）的示例执行体，且**不动真实接入的机器**
（只处理 `agent-demo-*`、`agent-mira`、`agent-aster`、`agent-nova` 这些示例标识）。

用法：
  本地：  PYTHONPATH="apps/api;." python scripts/deploy/_aip1_enrich_agents.py
  服务器：cd /opt/math-agent-platform && PYTHONPATH=apps/api:. venv/bin/python /tmp/_aip1_enrich_agents.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DB = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].endswith(".db") else "apps/api/data/platform.db"

# 演示执行体的技能卡（示例数据，不是探测结果）
DEMO_CARDS: dict[str, list[tuple[str, str, list[str], list[str]]]] = {
    "agent-demo-writer": [
        ("markdown", "1.0", ["result_table", "figure"], ["paper_source"]),
        ("write", "", ["problem_analysis"], ["paper_source"]),
    ],
    "agent-demo-checker": [
        ("review", "", ["paper_source"], ["review_report"]),
        ("markdown", "1.0", ["paper_source"], ["audit_report"]),
    ],
    "agent-mira": [
        ("python", "3.12", ["data_profile"], ["model_spec", "code"]),
        ("scipy", "", ["problem_facts"], ["result_table"]),
    ],
    "agent-aster": [
        ("python", "3.12", ["model_spec"], ["result_table", "figure"]),
        ("pandas", "", ["data_profile"], ["result_table"]),
    ],
    "agent-nova": [
        ("markdown", "", ["result_table", "figure"], ["paper_source"]),
        ("latex", "", ["paper_source"], ["compiled_pdf"]),
    ],
}


def main() -> None:
    path = Path(DB)
    if not path.exists():
        raise SystemExit(f"数据库不存在：{path}")
    import sqlite3

    db = sqlite3.connect(str(path))
    updated = 0
    for agent_id, cards in DEMO_CARDS.items():
        row = db.execute("SELECT capability_cards FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
        if row is None:
            continue
        payload = [
            {"skill": skill, "version": version, "inputs": inputs, "outputs": outputs, "description": ""}
            for skill, version, inputs, outputs in cards
        ]
        encoded = json.dumps(payload, ensure_ascii=False)
        if row[0] == encoded:
            continue
        db.execute(
            "UPDATE agents SET capability_cards = ?, supported_tools = ? WHERE agent_id = ?",
            (encoded, json.dumps([card[0] for card in cards]), agent_id),
        )
        updated += 1
        print(f"补卡片：{agent_id} → {[card[0] for card in cards]}")
    db.commit()
    db.close()
    print(f"完成：更新 {updated} 个示例执行体（其余未动）")


if __name__ == "__main__":
    main()