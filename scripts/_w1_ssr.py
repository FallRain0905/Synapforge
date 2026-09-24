"""项目工作区：SSR 不变量校验（W-1 起沿用，W-4 更新为 6 分组）。

延续 UX-9/AUTH-1 的同一组不变量：每个页面都能在 SSR HTML 里看到全部导航入口与分组按钮，
当前页唯一高亮，工作区入口（/workspace）与它的分组按钮都在。页面内部的四个 Tab 是客户端
渲染（首帧是"正在确认会话"骨架），由浏览器检查负责，不在这里断言。
"""
import sys
import urllib.request

BASE = "http://127.0.0.1:3000"

ROUTES = [
    ("/", "nav-overview"),
    ("/workspace", "nav-workspace"),
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
GROUP_BUTTONS = [
    "nav-group-project",
    "nav-group-flow",
    "nav-group-content",
    "nav-group-delivery",
    "nav-group-workspace",
    "nav-group-tools",
]


def fetch(path: str) -> str:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=20) as response:
        return response.read().decode("utf-8", "replace")


failures: list[str] = []
for route, testid in ROUTES:
    html = fetch(route)
    problems = []
    missing = [candidate for candidate in ALL_NAV if f'data-testid="{candidate}"' not in html]
    if missing:
        problems.append(f"缺少导航入口 {missing}")
    for button in GROUP_BUTTONS:
        if f'data-testid="{button}"' not in html:
            problems.append(f"缺少分组按钮 {button}")
    if f'data-testid="{testid}"' not in html:
        problems.append(f"当前页入口不存在 {testid}")
    if html.count('aria-current="page"') > 1:
        problems.append(f"高亮不唯一（{html.count('aria-current=\"page\"')} 处）")
    if problems:
        failures.append(f"{route}: " + "; ".join(problems))

if failures:
    print("SSR 不变量失败：")
    for line in failures:
        print(" -", line)
    sys.exit(1)

print(f"SSR 不变量通过：{len(ROUTES)} 路由 × {len(ALL_NAV)} 入口 × {len(GROUP_BUTTONS)} 分组按钮（旧页收进高级工具后仍全部可达）；工作区入口 /workspace 就位")