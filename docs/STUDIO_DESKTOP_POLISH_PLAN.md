# studio 桌面端「桌面感」美化计划（STUDIO-POLISH）

> 2026-11-16 · 状态：**计划待实施**
> 上游：`docs/STUDIO_DESKTOP_PLAN.md`（P0–P4 已交付）
> 目标：在不重写 UI 的前提下，把 studio 从"窗口里的网页"做成"一眼就是桌面软件"。

## 0 原则

1. **不重画系统控件**：最小化/最大化/关闭继续用原生按钮（WCO），不做无边框自绘三键——Snap Layouts、贴边、全屏、多屏 DPI 全都交给系统。
2. **业务零改动**：所有美化只动壳（`apps/studio-desktop`）和 `workbench.html` 的桌面态样式/交互；后端 API 除任务栏需要的现有端点外不加新协议。
3. **浏览器形态不受影响**：所有桌面专属样式挂在 `html.desktop-shell` 下，没有桥时一行都不生效。
4. **每个视觉实验都能一键回退**：`shell.json` 特性开关 + `SF_SHELL_SAFE=1` 安全模式。
5. **先实用、后视觉、最后第二界面**：顺序是 2 → 3 → 1 → 4 → 5 → 8 → 9（见 §3）。

## 1 现状与缺口

已经有的桌面能力（P0–P4）：托盘常驻/四色状态、系统通知、关窗到托盘、单实例、`synapforge://` 深链、
拖拽路径、开机自启、`Ctrl+Shift+Space` 唤起主窗、窗口尺寸记忆、诊断页、更新检查、NSIS 安装包。

看起来还不像桌面软件的地方：

| 缺口 | 现状 | 影响 |
| --- | --- | --- |
| 系统标题栏是 Windows 蓝 | 应用深色、标题栏系统色 | 最刺眼的一处"网页感" |
| 导入技能/插件要手敲路径 | 扩展库只有输入框 | 高频操作很别扭 |
| 没有右键菜单 | 只能点按钮 | 桌面软件的基本预期 |
| 任务栏无进度/无缩略图按钮 | 只能看窗口 | 长任务时缺少系统级反馈 |
| 没有命令面板 | 全靠鼠标点侧栏 | 键盘用户不顺手 |
| 侧栏不透明，没有 Mica | 整页纯色 | 精致度差一档 |
| 没有快速面板 | `Ctrl+Shift+Space` 直接开主窗 | 派一条任务要先等大窗起来 |

## 2 阶段 0 · 基线与回退开关（0.5 天）

**做什么**

- `workbench.html` 启动时：`if (window.synapforgeStudio) document.documentElement.classList.add("desktop-shell")`
- 新增设计令牌（`workbench.html` `:root`）：
  - `--titlebar-h: 40px`
  - `--surface-glass` / `--surface-glass-strong`（半透明表面，Mica 阶段用）
  - `--focus-ring: 0 0 0 2px color-mix(in srgb, var(--accent) 55%, transparent)`
  - `--window-shadow`
- 壳新增 `src/shell-config.js`：读 `%APPDATA%/synapforge-studio-desktop/shell.json`
  ```json
  {"titleBar":"wco","mica":"auto","quickPanel":true,"taskbar":true,"contextMenu":true}
  ```
  - `titleBar`: `wco | system`
  - `mica`: `auto | on | off`（auto = Win11 才开）
  - `SF_SHELL_SAFE=1` 时全部回落：系统标题栏、无 Mica、无面板、右键菜单仍可留
- 新增 `scripts/check_studio_polish.js`：断言 `#sfTitlebar`、`#commandPalette`、`sf-context` 等关键节点与桥方法存在（纯静态检查，不依赖 Electron）。

**验收**

- 浏览器打开 8903：DOM 上没有 `desktop-shell`，视觉零变化
- 桌面壳：有 `desktop-shell`，安全模式下与 0.1.1 完全一致
- `node scripts/check_studio_polish.js` 通过

## 3 阶段顺序（对应建议 2 → 3 → 1 → 4 → 5 → 8 → 9）

| 阶段 | 内容 | 对应建议 | 工作量 |
| --- | --- | --- | --- |
| 1 | 原生文件夹选择器 | 2 | 0.3 天 |
| 2 | 右键菜单 | 3 | 0.5 天 |
| 3 | WCO 自绘标题栏 | 1 | 1 天 |
| 4 | 任务栏进度 + 缩略图按钮 + Jump List | 4 | 0.5 天 |
| 5 | Ctrl+K 命令面板 | 5 | 0.5–1 天 |
| 6 | Mica 半透明 | 8 | 1 天 |
| 7 | 独立快速面板 | 9 | 1–2 天 |

### 阶段 1 · 原生文件夹选择器（0.3 天）

**做什么**

- 壳：`ipcMain.handle("dialog:pickDirectory", ...)` → `dialog.showOpenDialog(win, {properties:["openDirectory","createDirectory"]})`
- preload：`pickDirectory({title}) => ipcRenderer.invoke(...)`
- `workbench.html` 在这些输入框右侧加「选择…」按钮（仅桌面壳显示）：
  - 扩展库：技能目录、插件目录（`#libSkillPath` / `#libPluginPath`）
  - Agent 扩展弹窗：技能目录、插件目录（`#extSkillPath` / `#extPluginPath`）
  - 新建 Agent：工作区（`#fWorkspace`）
  - 集群向导目前没有工作区字段（留空自动创建），暂不加；以后加字段时复用同一个按钮
- 按钮点击 → 选中目录 → 填值并触发 `input` 事件（保持现有校验逻辑不变）

**验收**

- 点「选择…」弹出系统文件夹对话框；选中后输入框有值
- 浏览器形态不出现按钮
- 取消对话框不改原值

### 阶段 2 · 右键菜单（0.5 天）

**做什么**

- 桥：`sfContextMenu(spec) => Promise<actionId|null>`
  - 主进程用 `Menu.buildFromTemplate` + `menu.popup({window, callback})`；点击项 resolve action id，关闭 resolve null
  - 菜单项只传 `{id, label, enabled, type, accelerator}`，不传回调（避免跨进程函数序列化）
- 各处的菜单项：
  - **成果物**（交付表、集群任务行、抽屉、弹窗、对话卡片）：
    `预览` / `打开所在文件夹` / `在默认应用打开` / `复制路径` / `复制文件名` / `批准交付`（PENDING_REVIEW 时）
  - **任务行**：`编辑任务细节` / `打开工作区` / `复制任务 id` / `批准` / `退回`（待复核时）
  - **Agent 卡**：`打开工作区` / `新会话` / `复制 Agent id` / `查看运行记录`
  - **运行记录行**：`打开详情` / `复制 run id` / `停止`（RUNNING 时）/ `重跑`
  - **对话正文**：`复制` / `全选`（用 role）
- 纯前端动作（预览、编辑）由渲染进程拿到 action id 后自己调；文件动作走主进程 `shell.openPath` / `showItemInFolder` / `clipboard.writeText`

**验收**

- 右键任一成果物出现 5–6 项；「打开所在文件夹」在资源管理器选中文件
- 「复制路径」进剪贴板；Esc / 点空白关闭菜单
- 浏览器里右键保持原生网页菜单（不接管）

### 阶段 3 · WCO 自绘标题栏（1 天）

**做什么**

- 主窗：`titleBarStyle: "hidden"` + `titleBarOverlay: { color, symbolColor, height: 40 }`
  - `color` 跟随主题（深 `#16181d` / 浅 `#f4f4f6`），`symbolColor` 跟随 `--fg`
  - 原生最小化/最大化/关闭保留在右上角
- `workbench.html` 在 `.app` 之前插入一条**全宽**标题栏（仅 `desktop-shell`）：
  ```
  ┌ #sfTitlebar (40px, -webkit-app-region: drag) ─────────────────────────┐
  │ [S] synapforge   Agent 工作台 / 写手        ● 运行中 3/8        ─ □ ✕ │
  └───────────────────────────────────────────────────────────────────────┘
  ```
  - 左侧：品牌块 + 当前视图/Agent 面包屑
  - 中间/右侧：运行状态 chip（来自 `renderHeader` 的状态）、内核连接点
  - 右侧预留 `padding-right: 150px`（WCO 按钮区；或用 `env(titlebar-area-width)`）
- 布局：`desktop-shell .app { height: calc(100vh - var(--titlebar-h)); }`；
  侧栏 `.side-top` 的品牌块在桌面态隐藏（品牌移到标题栏），导航整体上移
- 交互：标题栏空白处 `drag`；所有按钮/下拉/chip 设 `-webkit-app-region: no-drag`
- 双击标题栏最大化、拖到屏幕边缘 Snap，均由 WCO 原生提供
- 回退：`shell.json titleBar:"system"` 或 `SF_SHELL_SAFE=1` 时恢复现状

**验收**

- 1280×860、最大化、100%/125%/150% DPI 下：标题栏高 40px，按钮不遮挡内容，拖动/双击最大化正常
- 原生三键 hover/点击正常；Snap Layouts（Win11）可用
- 深/浅主题切换时标题栏颜色跟着变（`nativeTheme.on("updated")` 重建 overlay 颜色）
- 浏览器形态不出现标题栏，布局不变

**风险**

- `.topbar` 目前只在主列，全宽标题栏要新增一层；侧栏品牌块移动会牵动 `.side-top` 高度
- `drag` 区域覆盖输入框/按钮会导致点不动——必须逐个 `no-drag`

### 阶段 4 · 任务栏进度 + 缩略图按钮 + Jump List（0.5 天）

**做什么**

- **进度**：`win.setProgressBar(ratio)`；运行中没有总步数时用 `2`（不确定态），集群按 `已完成任务/总任务` 传 0–1；结束/失败 `-1` 清除
- **叠加图标**：失败时 `win.setOverlayIcon(redDot, "有任务失败")`，有新完成时 `greenDot`，查看后清除
- **缩略图工具栏**：`win.setThumbarButtons([{icon, tooltip, click}, ...])`
  - `暂停集群` / `恢复集群` / `停止当前运行` / `打开主窗`
  - 按钮可用性由前端通过 `taskbar:state` 上报的 active cluster/run 决定
  - 主进程直接调内核现有端点：`POST /clusters/{id}/pause|resume`、`POST /runs/{id}/stop`
- **Jump List**：`app.setJumpList` 最近 5 个集群（名字 + id），点击走 `sfOpen({cluster})`；最近列表存 `shell.json`
- 前端 → 壳：`taskbar:state({progress, kind, clusterId, runId, status})`（节流 1s）

**验收**

- 跑一条任务：任务栏图标出现进度；结束后消失
- 悬停任务栏缩略图出现 4 个按钮；点「暂停集群」后集群真的 PAUSED（刷新界面确认）
- 右键任务栏图标出现最近集群，点击落到集群任务页

### 阶段 5 · Ctrl+K 命令面板（0.5–1 天）

**做什么**

- `workbench.html` 增加 `#commandPalette`（居中 640px、背景模糊、键盘操作）
- `Ctrl/Cmd+K` 打开；`↑/↓` 选择、`Enter` 执行、`Esc` 关闭
- 数据源（全部来自现有 `S`）：
  - 视图：集群工作 / 集群任务 / 集群 Agent / Agent 工作台 / 运行记录 / 会话 / 交付 / 扩展库 / 用量 / 设置
  - Agent（选中即 `selectAgent`）
  - 集群（`sfOpen({cluster})`）
  - 最近 5 条运行（`sfOpen({run})`）
  - 动作：新建集群、导入技能、安装插件、打开诊断页、检查更新
- 简单模糊匹配（子序列匹配 + 高亮命中字符），不做分词
- 壳：全局快捷键 `Ctrl+Shift+Space` 唤起主窗后 `executeJavaScript("sfCommandPalette()")`（阶段 7 之前）
- 无障碍：`role="dialog"`、`aria-activedescendant`、焦点锁在面板内

**验收**

- `Ctrl+K` → 输入「写手」→ Enter → 切到该 Agent
- 输入「交付」→ Enter → 进交付页
- Esc 关闭后焦点回到原元素；鼠标点击也能用
- 浏览器形态同样可用（这是纯前端能力）

### 阶段 6 · Mica 半透明（1 天，依赖阶段 3）

**做什么**

- 主窗：`backgroundMaterial: "mica"` + 页面透明根
- 仅当 `process.platform === "win32"` 且系统 build ≥ 22000（`os.release()`）才启用；否则纯色
- CSS（`desktop-shell` + `shell.json mica != off`）：
  ```css
  html.desktop-shell, html.desktop-shell body { background: transparent; }
  html.desktop-shell .app { background: transparent; }
  html.desktop-shell .sidebar { background: color-mix(in srgb, var(--bg) 80%, transparent); }
  html.desktop-shell .session,
  html.desktop-shell .inspector,
  html.desktop-shell .modal-card,
  html.desktop-shell .drawer { background: var(--bg); }
  ```
- 标题栏颜色与 `backgroundMaterial` 对齐；`nativeTheme.on("updated")` → `win.setBackgroundMaterial("mica")` 并重设 overlay 颜色
- 浅色主题单独校一遍对比度（半透明侧栏上的 `--muted` 最容易发灰）

**验收**

- 彩色壁纸下侧栏透出模糊壁纸，主内容区保持不透明、文字对比度 ≥ 4.5:1
- 切浅色/深色、最大/还原都不闪白
- Win10 或 `mica:"off"` 时回落纯色，无透明残影
- 关闭 Mica 后与阶段 3 视觉一致

**风险**

- 透明根会影响所有全屏背景（弹窗/抽屉/滚动条），必须逐个补不透明底
- 截图/录屏里 Mica 效果受壁纸影响，验收要用固定壁纸

### 阶段 7 · 独立快速面板（1–2 天）

**做什么**

- 新窗 `renderer/panel.html`：780×520、`frame:false`、`alwaysOnTop:true`、`skipTaskbar:true`、`transparent:true`、圆角 + 阴影
- 内容（只做三件事，避免第二个完整 UI）：
  1. 搜索框（Agent / 集群 / 视图）
  2. 「给当前 Agent 派任务」单行输入（Enter 直接发）
  3. 最近 5 条运行/集群（点击打开主窗并跳转）
- 快捷键：`Ctrl+Shift+Space` 开关面板；主窗唤起改到 `Ctrl+Shift+M`（托盘/双击任务栏图标仍可）
- 定位：`screen.getCursorScreenPoint()` → `getDisplayNearestPoint()`，在该屏顶部居中，距顶 80px
- 隐藏：失焦、Esc、再次按快捷键
- 数据：面板直接加载内核 URL（同源 token），复用 `/api/studio/state`、`/agents`、`/runs`；派任务走 `POST /agents/{id}/tasks`
- 回退：`shell.json quickPanel:false` 时快捷键仍唤起主窗

**验收**

- 快捷键 → 面板 150ms 内出现，不占任务栏；输入任务 Enter 后主窗能看到新 run
- 点面板外任意位置 / Esc 立即隐藏；多屏时出现在鼠标所在屏
- 主窗关闭到托盘时面板仍可用（内核还在跑）

## 4 跨阶段工程约束

### 4.1 回退

- `%APPDATA%/synapforge-studio-desktop/shell.json` 四个开关
- `SF_SHELL_SAFE=1` → `titleBar:system`、`mica:off`、`quickPanel:false`、`taskbar:false`
- 每个阶段单独提交，出问题 `git revert` 单笔即可

### 4.2 测试

- 每阶段：`node --check apps/studio-desktop/src/*.js`、`node scripts/check_studio_shell.js`、`node scripts/check_studio_polish.js`
- 涉及内核/前端：`apps/local` 全量（当前 231 项）必须保持全绿
- UI 用 CDP 断言 + 截图（深/浅、1280×860、150% DPI、最大化）

### 4.3 无障碍与动效

- 所有新交互键盘可达；焦点环用 `--focus-ring`
- 面板/命令面板有 `role`/`aria-*`
- 尊重 `prefers-reduced-motion`（标题栏/面板动画降级为淡入）

### 4.4 平台

- Windows 11：WCO + Mica + Snap + Thumbar 全开
- Windows 10：WCO 可用、Mica 自动回落、Snap Layouts 不可用（系统限制）
- macOS：交通灯 + `vibrancy` 另立项；本计划不承诺
- 纯浏览器：只保留阶段 5（命令面板），其余全部不生效

## 5 里程碑与版本

| 里程碑 | 内容 | 版本 | 工作量 |
| --- | --- | --- | --- |
| M1 | 阶段 0+1+2（基线、文件夹选择、右键菜单） | 0.2.1 | 1.3 天 |
| M2 | 阶段 3+4（WCO 标题栏、任务栏） | 0.3.0 | 1.5 天 |
| M3 | 阶段 5（命令面板） | 0.3.1 | 0.5–1 天 |
| M4 | 阶段 6+7（Mica、快速面板） | 0.4.0 | 2–3 天 |

合计约 **5.5–7 个工作日**。

## 6 明确不做

- 不做无边框自绘三键（保留原生 WCO 按钮）
- 不做主题市场/皮肤系统
- 不引入 React/Vue/构建步骤（继续单文件 vanilla）
- 不做移动端/平板布局
- 不改内核业务协议（任务栏/面板只用现有端点）
- 不做 macOS 专属效果（另立项）

## 7 文件影响面

| 文件 | 阶段 | 改动 |
| --- | --- | --- |
| `apps/studio-desktop/src/main.js` | 0–7 | WCO、对话框、右键菜单、任务栏、面板窗口、快捷键 |
| `apps/studio-desktop/src/preload.js` | 0–7 | `pickDirectory` / `sfContextMenu` / `taskbar:state` / `commandPalette` |
| `apps/studio-desktop/src/shell-config.js` | 0 | 特性开关与安全模式 |
| `apps/studio-desktop/renderer/panel.html` | 7 | 快速面板 |
| `apps/local/synapforge/studio/static/workbench.html` | 0–6 | `desktop-shell`、标题栏、右键菜单挂载、命令面板、选择按钮 |
| `scripts/check_studio_polish.js` | 0–6 | 静态契约检查 |
| `scripts/build-studio-desktop.ps1` | 交付 | 不变（继续复用） |

## 8 视觉验收清单（每个里程碑跑一遍）

- [ ] 浏览器与桌面壳截图对比：浏览器零变化
- [ ] 标题栏 40px，三键不重叠，拖拽/双击最大化正常
- [ ] 深色/浅色、100%/125%/150% DPI 无错位
- [ ] 右键菜单在 5 类对象上都出得来，Esc 能关
- [ ] 任务栏进度/缩略图按钮/最近集群真实可用
- [ ] `Ctrl+K` 全键盘可用，无鼠标依赖
- [ ] Mica 开/关、Win10 回落都无残影
- [ ] 快速面板 150ms 内出现、失焦即隐、多屏定位正确
- [ ] `apps/local` 全量测试与三个 JS 检查全绿
