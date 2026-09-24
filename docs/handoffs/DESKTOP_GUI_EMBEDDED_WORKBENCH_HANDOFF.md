# DESKTOP-GUI 交接：完整功能桌面客户端（内嵌工作台）· 已上线

> 2026-09-23 · 计划见 `docs/DESKTOP_CLIENT_FULL_GUI_PLAN.md`（本文档实现的是其中**路线 A**）
> 用户拍板：①形态＝内嵌远程工作台 + 可配地址 ②桌面能力＝通知/角标 + 开机自启常驻 + 内核面板并入主窗口 + 拖拽上传/深链/快捷键（全选）③更新＝检查更新 + 提示下载
> 安装包：`https://synapforge.top/downloads/synapforge-setup-0.2.0-x64.exe`（143.8MB，未签名）· sha256 `b534cf2b9db9f63fdd8890a7602d900169bbb5acc8a4a5b0b642656249520b6c`

## 1 变了什么（用户能感知的）

| 之前（0.1.1） | 现在（0.2.0） |
| --- | --- |
| 壳只有一个 720×560 的状态小窗；工作台靠 `shell.openExternal()` **丢给系统浏览器** | **主窗口就是工作台**（1280×860 起，尺寸位置记忆），不再跳浏览器 |
| 内核能力只在那个小窗里 | **本机 Agent 面板并入主窗口**（顶栏按钮 / `Ctrl+2` / 托盘"本机 Agent（窗口内）"），独立窗口仍保留 |
| 没有应用菜单、没有快捷键 | 应用菜单（文件/视图/窗口/帮助）+ 常用快捷键（刷新、放大缩小、全屏、开发者工具、`Ctrl+1/2`） |
| 无通知、无角标 | 系统通知（开始执行 / 任务完成 / 与平台断开·恢复，点击直达「我的任务」）+ 任务栏叠加角标（有待办时） |
| 手动启动 | **开机自启开关**（向导里 / 托盘勾选），托盘常驻后台领任务 |
| 服务器地址写死 | **可配服务器地址**（首次运行向导 + 托盘"服务器地址…"），默认 `https://synapforge.top` |
| 无更新提示 | **检查更新**：读 `/downloads/latest.json` 比版本，提示并打开下载页（无需签名证书） |
| 深链只有 `map://pair`、`map://grant` | 新增 **`map://task/<id>`** 直达任务详情（平台 `/tasks?task=<id>` 配套支持） |
| 上传只能点按钮 | **拖文件进聊天区即上传**（走既有"云盘 → 成果物 → 发消息"链路，最多 5 个/次） |
| —— | 离线页（连不上平台时给重试/改地址/看内核状态，纯本地文件） |

## 2 落点

**桌面端（Electron）**
- `apps/desktop/src/main.js`（309 → 700+ 行）：配置 `desktop.json`（地址/自启/窗口尺寸）、工作台窗口（`partition: persist:synapforge` 持久登录态）、应用菜单与快捷键、外链外部打开、`did-fail-load` → 离线页、下载完成通知、系统通知与状态跳变检测、任务栏角标、`app.setLoginItemSettings`、深链（pair/grant/**task**）、`checkUpdate`（版本比较 + 提示）、IPC 扩到 17 个句柄、**自检模式**（见 §4）。
- `apps/desktop/src/preload.js`：暴露 `window.synapforgeShell`（20 个方法；旧名 `shell` 保留兼容本地页）。
- `apps/desktop/renderer/setup.html`（重写）：首次运行向导 = 服务器地址 + 开机自启 + 可选配对串 + 内核状态/执行体/日志/重扫/暂停/紧急停止；黑灰白配色。
- `apps/desktop/renderer/offline.html`（新）：离线页。
- `apps/desktop/package.json`：`version 0.2.0`。

**前端（同一份 UI 跑在浏览器与桌面端；没有桥时桌面专属入口自动不出现）**
- `apps/web/lib/desktop.ts`（新）：桥的类型与 `desktopShell()` / `isDesktopShell()`。
- `apps/web/components/local-agent-panel.tsx`（新）：本机 Agent 面板（连接状态、正在执行、已完成、本地队列、执行体清单、日志、重扫/暂停/紧急停止、检查更新、打开独立窗口）。
- `apps/web/components/shell.tsx`：顶栏 `local-agent-toggle`（仅桌面端）+ 挂载面板 + 响应壳的"打开面板"事件。
- `apps/web/app/workspace/page.tsx`：聊天区拖拽上传（`.drop-active` 高亮）。
- `apps/web/app/tasks/page.tsx`：`?task=<id>` 深链打开任务详情（**读 `window.location.search`，不用 `useSearchParams()`**——后者会让页面转动态渲染，本项目全静态预渲染）。
- 4 处下载链接 → `synapforge-setup-0.2.0-x64.exe`。

**内核（Python）**
- `apps/agent/sidecar_entry.py`：`_force_utf8_stdio()`（见 §5 的真 bug）。

## 3 验收（全部实测）

| 项 | 结果 |
| --- | --- |
| 前端 | `tsc --noEmit` 干净、`next build` **25 页全静态**、SSR 不变量 **19×19×6** |
| Agent 套件 | **304 项（9 skipped）**（+2 `test_sidecar_entry`） |
| **打包应用自检（对生产）** | `SELFCHECK {"ok":true,"version":"0.2.0","url":"https://synapforge.top/workspace","panel":true,"panel_open":true,"kernel_contract":"v1","kernel_agents":4}`，进程退出码 0 —— 用**真实安装包解包目录**、真实线上站点、真实内核跑的，不是 dev 模式 |
| 打包应用自检（对本地） | 同上 `ok:true`（本地 web + 本地 API） |
| 线上发布 | `server_release.sh` 成功；`server_verify.sh` **22/22** |
| 线上产物 | `latest.json`（version/sha256/size 与本地一致）、`synapforge-setup-0.2.0-x64.exe` 200 且字节数一致、`/login` 页面已指向 0.2.0 |
| 探针清理 | 自检用的线上临时账号（连会话/成员关系）已删除 |

**自检怎么用**（发布前必跑，也可用于排障）：
```bash
MAP_STATE_DIR=<空目录> MAP_DESKTOP_SELFCHECK=1 \
MAP_DESKTOP_SELFCHECK_TOKEN=<平台会话令牌> MAP_DESKTOP_PLATFORM_URL=https://synapforge.top \
apps/desktop/dist/win-unpacked/synapforge.exe    # 退出码 0 = 桥在、面板能开、内核契约 v1
```
断言的是"工作台在窗口里加载 + preload 桥 20 个方法齐 + 顶栏入口存在 + 点开后 `[data-testid=local-agent-panel]` 真在 + 内核 `snapshot().shell.contract == 'v1'`"——
只断言"按钮在"太弱（按钮在、面板打不开也会通过）。

## 4 设计取舍（写下来免得以后来回改）

- **不重画 UI**：同一份前端跑两处（浏览器 / 桌面端），靠 `window.synapforgeShell` 是否存在来显隐桌面专属入口。理由：25 页重画一遍的维护成本翻倍，收益为零。
- **登录态放应用分区**（`persist:synapforge`），**设备/项目令牌仍然只在 Windows 凭据管理器**（既有硬约束，preload 不暴露任何令牌）。
- **不改前端到"自带静态副本"**（计划里的路线 B）：那会让前端每次改动都要重发安装包。当前形态下前端升级 = 服务器部署即生效。
- **通知只来自本机内核状态跳变**（开始执行/完成/断连），不是"派给我的任务/待复核"——后者需要前端在收到事件时主动请求壳通知，那是前端配合的下一步（桥里已经留好 `notify`）。
- **角标在 Windows 用任务栏叠加图标**（`app.setBadgeCount` 在 Windows 是 no-op，仅 mac/linux 生效）。

## 5 首次运行验证抓到的真 bug（0.1.1 也有）

**现象**：全新机器（未配对）启动桌面端 → 内核反复崩溃 → 壳重启 3 次后放弃 → 用户**永远配不上对**。
**根因**：打包成 exe 后 Windows 上 stdout 默认 cp1252；内核未配对时会打一行中文提示
（`no_project_grant` 的 hint），`print()` 抛 `UnicodeEncodeError` → 进程退出。
**修法**：`sidecar_entry._force_utf8_stdio()` 在 `main()` 最前面把 stdout/stderr 钉成 UTF-8
（`errors="replace"`，任何情况下不再因为日志崩）；壳 spawn 时另加 `PYTHONUTF8=1` / `PYTHONIOENCODING=utf-8` 作双保险（老内核也受益）。
**回归测试**：`apps/agent/test_sidecar_entry.py` 用 cp1252 流复现"修复前会炸、修复后能打"。
**验证**：修复后在同一台机器上用**空状态目录**跑打包应用，`kernel_agents: 4`、无崩溃、退出码 0。

## 6 边界与未做

- **安装包未签名**：首装会看到 SmartScreen"更多信息 → 仍要运行"；静默自动更新需证书（用户选了"检查更新 + 提示下载"）。
- **通知不含"派给我的任务/待复核"**（见 §4）；前端侧主动通知是下一步。
- **工作台需要网络**：平台是服务器端应用，离线时只有离线页 + 本机内核面板可用（路线 C 本地平台明确不做）。
- **未做**：0.1.1 安装包仍留在 `/downloads/`（老用户升级到 0.2.0 由"检查更新"提示）；侧栏「本机 Agent」标签页（现在是从顶栏弹出的抽屉式面板）。