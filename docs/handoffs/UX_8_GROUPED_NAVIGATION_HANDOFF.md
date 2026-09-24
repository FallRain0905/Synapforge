# UX-8 导航分组与快速跳转 交接

> 交接状态：`PASS`
>
> 日期：2026-09-16
>
> 对应阶段：UX-8（前端信息架构修正，用户提出的"页面太多、找来找去很麻烦"）
>
> 上一份交接：`docs/handoffs/CL_6_DOCUMENT_DRAFTS_HANDOFF.md`

---

## 1. 本阶段目标

从用户原话抄写：

> **目标**：优化前端显示。当前页面太多了，找来找去很麻烦，可不可以通过下拉框、侧边栏或者顶部导航栏的方式优化？**精简显示页面的数量但不减少显示内容。**

拆成可验收的四条：

1. 首屏平铺的导航入口数量显著下降（"精简显示页面的数量"）；
2. 每个页面仍然可达，且**任何一个都不需要记 URL**（"不减少显示内容"）；
3. 不靠眼睛找也能到页面（下拉框/搜索）；
4. 16 个路由与 URL 不变——本次只改导航呈现，不动页面本身，避免破坏既有链接、教案与验收脚本。

**Do NOT**：不合并/删除页面；不改路由；不改后端与 Agent；不引入新的状态管理依赖。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| 导航唯一数据源 | 完成 | 新增 `apps/web/lib/nav.ts`：7 个 `NavSection`（分组标签/说明/图标）+ 16 个 `NavItem`（href、标签、图标、`data-testid`、一句话说明、搜索别名、角标类型），另有 `PAGE_TITLES`（页面标题/副标题）、`findSection`、`findNavItem`、`pageMeta`、`searchNav`、`NAV_STORAGE_KEY` |
| 侧边栏分组化 | 完成 | `apps/web/components/shell.tsx`：分组头是 `<button>`（`aria-expanded` + `data-testid="nav-group-<id>"`），子项是 `<Link>`（原 `data-testid` 原样保留）；**折叠仅视觉**——子项仍在 DOM 的 `.nav-children` 容器里（`max-height:0; visibility:hidden`），所以每个路由的静态 HTML 依旧含全部入口 |
| 展开策略 | 完成 | 默认只展开"当前页所在分组"（`openSections[id] ?? id === activeSection.id`），用户手动开合写入 `localStorage["math-agent-platform.nav.open-sections"]`；首帧不读 localStorage，保证同一路由的静态 HTML 稳定 |
| 单页分组自动降级 | 完成 | 只含 1 个入口的分组（任务与流程、空间设置）不渲染折叠按钮，直接是普通链接——有分组不等于要塞一个分组头 |
| 快速跳转（下拉 + 搜索） | 完成 | 新增 `apps/web/components/command-palette.tsx`：空查询列出**全部 16 页**并按分组加小标题，输入后按"标签前缀 > 标签包含 > 搜索别名 > 说明"排序；↑↓ 选择、Enter 打开、Esc 关闭、鼠标点击与悬停同步；面板底部显示"命中数 / 总页数" |
| 三个入口都能开 | 完成 | 顶栏「快速跳转」按钮（`data-testid="quick-jump"`，带 `Ctrl K` 提示）、侧栏「搜索页面」（`quick-jump-sidebar`，移动端也可用）、全局 `Ctrl/Cmd + K` 开关 |
| 面包屑带分组 | 完成 | 顶栏面包屑从「页面 / 项目」改为「**分组 › 页面** / 项目」，用户能从路径反推页面归属（移动端隐藏分组段） |
| 角标归位 | 完成 | 角标从"一个 `attention` 变量给所有页用"改为按数据类型判断：`tasks`（BLOCKED/NEEDS_REVISION 数）、`review`（未通过门禁数）、`pack`（模板包未物化）、`projects`（项目数，仍是数字小标）；分组头在**折叠时**用数字小标说明内含几页，有需要注意的页面时点一个提醒点 |
| 样式与响应式 | 完成 | `apps/web/app/globals.css`：`.nav-group-button`/`.nav-children`（缩进 + 左侧引导线 + 展开动画 + 折叠时可聚焦性关闭）、`.nav-jump`、`.palette-*`（含暗色主题选中项覆盖）；760px 以下侧栏仍是抽屉，顶栏「快速跳转」只留图标，面板改为顶部 6vh 起 |

## 3. 信息架构对照

| 旧（16 个平铺入口，4 个分组标题） | 新（7 个入口，5 个可折叠分组 + 2 个直达页） |
| --- | --- |
| 工作区：项目总览、任务与流程、建模模板包、文档版本 | **项目**：项目总览、建模模板包（默认展开） |
| 知识库与 AI：知识库、超图可视化、AI 问答 | **任务与流程**（直达） |
| 协作与交付：审核门禁、论文交付、个人云盘、成果物库、交接中心 | **内容与文档**：成果物库、文档版本、个人云盘 |
| 运行：运行控制台、设备与接入、项目时间线、空间设置 | **审核与交付**：审核门禁、论文交付、交接中心 |
| — | **知识库与 AI**：知识库、超图可视化、AI 问答 |
| — | **运行与设备**：运行控制台、设备与接入、项目时间线 |
| — | **空间设置**（直达） |

分组依据是"这页在流程里干什么"（项目 → 任务 → 内容 → 交付 → 知识 → 运行 → 设置），而不是原来的按实现模块划分。

## 4. 测试与验证

### 4.1 静态与构建

```
# 前端（新增 2 个文件、改 2 个文件）
apps/web$ npm run build
# → 19 个路由全部静态预渲染，Linting and checking validity of types 通过
```

**SSR 结构校验**（临时脚本逐路由抓 HTML，验证后已删除）：16 个路由 **每个**都满足

- 16 个 `data-testid="nav-*"` 入口全部存在（**折叠不让内容消失**）；
- 5 个 `nav-group-*` 按钮存在，且单页分组不出现分组按钮；
- `nav-item-active` 恰好出现 1 次且指向当前路由对应入口；
- 分组展开状态与当前页一致（只有当前页所在分组带 `is-open`）；
- 非当前分组虽然折叠，其子项的 `data-testid` 仍在 HTML 中。

### 4.2 实机（浏览器 + 真平台 API，`127.0.0.1:3000` + `:8000`）

| 场景 | 结果 |
| --- | --- |
| 首页侧边栏 | 7 行入口：项目（展开，含 2 子项）、任务与流程、内容与文档、审核与交付、知识库与 AI、运行与设备、空间设置 |
| 展开分组 | 点「知识库与 AI」→ 知识库/超图可视化/AI 问答 可见（折叠时 `isVisible=false`） |
| 搜索 | 「论文」→ 命中 2 条，`论文交付`（标签命中）排在 `文档版本`（说明命中）之前 |
| 回车跳转 | Enter → URL 变 `/delivery`，面包屑 `审核与交付 › 论文交付 / 测试`，审核与交付分组自动展开 |
| 状态持久化 | 刷新后 localStorage=`{"knowledge":true}`，手动展开的知识库分组仍在，当前页所在分组仍自动展开，未碰过的分组仍折叠 |
| Ctrl+K | 关闭态按一次 → 面板打开且焦点在搜索框（`activeElement` 为 `quick-jump-input`）；再按一次 → 关闭 |
| 布局几何 | 7 行 `top/bottom` 互不重叠、`scrollWidth<=clientWidth`（无截断）、面板 640×666 落在 1440×900 视口内 |
| 移动端 720×820 | 侧栏关闭时 `translateX(-244px)`，点「打开导航」后 `left=0`，抽屉内 16 个入口和 5 个分组按钮齐备；顶栏按钮退化为图标 |
| 暗色主题 | 面板底 `rgb(22,28,38)`、选中项 `rgb(30,43,60)`、正文 `rgb(230,237,243)`，与亮色主题取值不同且对比正常 |

截图留存在浏览器会话产物目录（首屏折叠态、展开知识库分组、搜索"论文"、跳转后的 `/delivery`、移动端抽屉、暗色面板）。

### 4.3 回归

```
apps/api$   python -X utf8 -m unittest discover -s . -p "test_*.py"   # Ran 330 tests, OK (skipped=14)
apps/agent$ python -X utf8 -m unittest discover -s . -p "test_*.py"   # Ran 281 tests, OK (skipped=9)
apps/web$   npm run build                                            # 19 页
```

本次未改后端与 Agent，两条套件跑一遍确认"没有关联回归"；`demo-1.0.ps1` 只断言 API 项目列表，不依赖 Web 定位器（已核对脚本内容），导航重构不影响其 17 项。

## 5. 不与哪些约束冲突

- **I1（Demo 1.0 不回归）**：API 330 / Agent 281 / 前端 19 页均不低于基线；`demo-1.0.ps1` 触及面未变。
- **禁区**：未触碰 `collaboration.py`、`gateway.py`、`packages/agent_protocol/`、`packages/competition_packs/`、`kb_gateway.py`、`infra/docker-compose.yml`、既有迁移脚本。
- **既有 `data-testid`**：16 个 `nav-*` 名称逐字保留（历史验收记录里的定位器仍然有效），只**新增** `nav-group-*`、`quick-jump`、`quick-jump-sidebar`、`quick-jump-panel`、`quick-jump-input`、`jump-*`。

## 6. 尚未完成与边界

1. **页面没有合并**：16 个路由与 URL 一字未改。若后续要把 `/kb`+`/graph`、`/artifacts`+`/handoffs` 这类相邻页做成同页标签，属于"改页面"而不是"改导航"，需要单独排期并检查外部链接（总览页快捷卡、模板包页、时间线的 `?artifact=`）。
2. **搜索是别名 + 子串匹配**，不做拼音首字母（"rwjf" → 任务与流程）与模糊纠错；别名表在 `nav.ts` 里手工维护，新增页面记得补 `keywords`。
3. **折叠状态是本机偏好**（localStorage），不随成员/项目同步；换设备后回到"只展开当前分组"。
4. **界面提示只写 `Ctrl K`**，实际同时支持 macOS 的 ⌘K。
5. **折叠分组的内容对屏幕阅读器不可见**（`visibility:hidden`，这是刻意的可聚焦性保护），需要无障碍全量朗读时应展开分组或改用跳转面板。

## 7. 复现命令

```powershell
# 构建 + 起服务（README「前端改动后必须重建并重启 Web 进程」）
cd apps\web; npm run build
node .\node_modules\next\dist\bin\next start -p 3000

# 浏览器验证要点
# 1) 首页侧栏应只有 7 行入口（5 个带数字小标的分组 + 2 个直达页）
# 2) 点「知识库与 AI」→ 出现 3 个子项；刷新页面后仍是展开的（localStorage）
# 3) 顶栏「快速跳转」或 Ctrl+K → 输入「论文」→ 第一条是「论文交付」→ 回车进入
# 4) 进入 /delivery 后确认左侧「审核与交付」自动展开、面包屑显示「审核与交付 › 论文交付」
```

## 8. 组件版本对照

| 组件 | 版本/位置 | 本次是否改动 |
| --- | --- | --- |
| Next.js | 15.5.25（`apps/web/package.json`） | 否 |
| `apps/web/lib/nav.ts` | 新增 | 是 |
| `apps/web/components/command-palette.tsx` | 新增 | 是 |
| `apps/web/components/shell.tsx` | 导航/面包屑/角标重写 | 是 |
| `apps/web/app/globals.css` | 新增导航分组与面板样式 | 是 |
| 后端 / Agent / 契约 | — | **否** |