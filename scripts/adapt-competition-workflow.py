"""CLI：把外部 competition-workflow 插件适配清单导出为 JSON。

用法：
    python scripts/adapt-competition-workflow.py <plugin_root> [out.json]
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "apps" / "api"))

from app.competition_workflow_adapter import adapt_competition_workflow, write_adapter_report  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__.strip())
        return 2
    plugin_root = sys.argv[1]
    report = adapt_competition_workflow(plugin_root)
    if len(sys.argv) >= 3:
        path = write_adapter_report(report, sys.argv[2])
        print(f"适配清单已写入 {path}")
    else:
        print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    print(
        f"summary: skills={len(report.skills)} mapped={report.mapped_skill_count} "
        f"templates={len(report.templates)} checks={len(report.checks)} unmapped={len(report.unmapped)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
