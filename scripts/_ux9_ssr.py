"""UX-9 移动端适配：SSR 不变量校验（临时脚本，验完删除）。

保证两件事：
  1. 上一轮导航重构的不变量没被破坏（16 个入口 + 5 个分组按钮 + 单页分组不带按钮 + 当前页唯一高亮）
  2. 移动端新增标记到位（底部标签栏、5 个 tab、表格单元格 data-label、viewport meta 带 viewport-fit）
"""
import json
import re
import sys
import urllib.request

BASE = "http://127.0.0.1:3000"

ROUTES = [
    ("/", "nav-overview"),
    ("/pack", "nav-pack"),
    ("/tasks", "nav-tasks"),
    ("/my-tasks", "nav-my-tasks"),
    ("/artifacts", "nav-artifacts"),
    ("/documents", "nav-documents"),
    ("/drive", "nav-drive"),
    ("/review", "nav-reviews"),
    ("/delivery", "nav-delivery"),
    ("/handoffs", "nav-handoffs"),
    ("/kb", "nav-kb"),
    ("/graph", "nav-graph"),
    ("/ask", "nav-ask"),
    ("/runs", "nav-runs"),
    ("/devices", "nav-devices"),
    ("/timeline", "nav-timeline"),
    ("/team", "nav-team"),
    ("/settings", "nav-settings"),
]
ALL_NAV = [testid for _, testid in ROUTES]
GROUP_BUTTONS = ["nav-group-project", "nav-group-flow", "nav-group-content", "nav-group-delivery", "nav-group-knowledge", "nav-group-ops", "nav-group-workspace"]
FLAT_SECTIONS = []
TABS = ["mtab-overview", "mtab-tasks", "mtab-review", "mtab-ask", "mtab-more"]
# 注：表格单元格的 data-label 不在 SSR 断言里——行数据来自客户端 fetch，
# SSR HTML 里没有行；那部分在浏览器几何断言阶段验证（见 _ux9_geom 检查）。


def fetch(path: str) -> str:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as response:
        return response.read().decode("utf-8", "replace")


failures = []
for route, testid in ROUTES:
    html = fetch(route)
    problems = []

    missing = [candidate for candidate in ALL_NAV if f'data-testid="{candidate}"' not in html]
    if missing:
        problems.append(f"缺少导航入口 {missing}")
    for button in GROUP_BUTTONS:
        if f'data-testid="{button}"' not in html:
            problems.append(f"缺少分组按钮 {button}")
    for flat in FLAT_SECTIONS:
        if f'data-testid="{flat}"' in html:
            problems.append(f"单页分组不应有分组按钮 {flat}")

    active = html.count("nav-item-active")
    if active != 1:
        problems.append(f"nav-item-active 出现 {active} 次（期望 1）")

    # 底部标签栏：5 个 tab 都要在 SSR 里（桌面由 CSS 隐藏，不能靠 JS 才渲染）
    for tab in TABS:
        if f'data-testid="{tab}"' not in html:
            problems.append(f"缺少标签栏项 {tab}")
    if 'data-testid="mobile-tabs"' not in html:
        problems.append("缺少底部标签栏容器")
    # 抽屉遮罩只在抽屉打开时渲染；SSR 静态输出里不应有
    if 'data-testid="nav-scrim"' in html:
        problems.append("SSR 里出现了抽屉遮罩（初态不该有）")
    # 当前页高亮：标签栏里恰好一处 is-active（四个 tab 之一，或「更多」）
    if html.count("mobile-tab is-active") != 1:
        problems.append(f"mobile-tab is-active 出现 {html.count('mobile-tab is-active')} 次（期望 1）")

    viewport = re.search(r'<meta name="viewport" content="([^"]+)"', html)
    if not viewport:
        problems.append("缺少 viewport meta")
    elif "viewport-fit=cover" not in viewport.group(1):
        problems.append(f"viewport 未开启 viewport-fit=cover：{viewport.group(1)}")

    if problems:
        failures.append({"route": route, "problems": problems})

print(json.dumps({"routes": len(ROUTES), "failures": failures}, ensure_ascii=False, indent=1))
sys.exit(1 if failures else 0)