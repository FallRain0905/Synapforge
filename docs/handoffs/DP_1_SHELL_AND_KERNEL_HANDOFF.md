# DP-1 壳与内核骨架（最小可用配对） 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-16
>
> 对应阶段：DP-1（工作项定义见 `docs/DESKTOP_CLIENT_IMPLEMENTATION_PLAN.md` §5）
>
> 上一份交接：`docs/handoffs/DP_0_BASELINE_AND_CONTRACT_HANDOFF.md`

---

## 1. 本阶段目标

从实施计划 §5 DP-1 抄写：

> **目标**：装包 → 输入平台地址 → 网页点「接入这台电脑」→ 托盘变绿 → 平台 `/devices` 出现该设备。
>
> **退出条件**：一次安装能走完 §3 的 ①–③；壳与内核的崩溃互不拖死（各自可单独重启）。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| DP-1-01 Electron 壳骨架 | 完成 | `apps/desktop/`：`src/main.js`（单实例锁、托盘四态、状态窗口、深链、内核生命周期）、`src/preload.js`（contextBridge 最小面）、`renderer/index.html` + `status.js`（本机状态页：连接/身份/本机 Agent 清单/任务/日志、配对表单、暂停与紧急停止）；`assets/tray-{grey,yellow,green,red}.png` 与 `app-icon.png`（脚本生成，无字体依赖） |
| DP-1-02 sidecar 打包链路 | 完成 | `apps/agent/sidecar_entry.py`（打包入口）+ `scripts/build-desktop.ps1`：PyInstaller **ONEDIR**（实测 84.9MB）→ `dist-sidecar/math-agent-sidecar/`，再交给 electron-builder 作为 `extraResources` 打进安装包（`resources/sidecar/`，实测 11.5MB 的 exe + 依赖） |
| DP-1-03 壳↔内核契约实现 | 完成 | `apps/agent/sidecar_api.py` + `agentd sidecar-run`：仅监听 127.0.0.1、启动令牌、`/health` `/status` `/pair` `/agents/rescan` `/tasks/pause|resume` `/emergency-stop|clear` `/logs` `/shutdown`；壳读 `%LOCALAPPDATA%\MathAgentPlatform\sidecar.json` 后连接 |
| DP-1-04 配对深链 | 完成 | 壳侧：`setAsDefaultProtocolClient("map")` + `second-instance` 转交 argv；平台侧：`/devices` 配对弹窗新增「接入这台电脑」（`href="map://pair?platform=…&blob=…"`），**保留命令行方式二作为兜底**（DE10） |
| DP-1-05 路径与权限 | 完成 | 可写状态统一在 `%LOCALAPPDATA%\MathAgentPlatform\`（密钥、`agentd.db`、`worker.json`、`sidecar.json`）；`dist-sidecar/` 仅为构建产物 |
| DP-1-06 NSIS 安装包 | 完成 | `apps/desktop/dist/Math Agent Platform Setup 0.1.0.exe`（**137MB，未签名**）；electron-builder 配置含 `map://` 协议注册与 per-user 安装 |

**验收中修掉的五个真缺陷（全部由真实运行暴露）**：

1. **`/pair` 用错解码器**：复用了"项目授权串"的解码器（要求 `project_id`/`project_token`），真实配对报 `worker_grant_missing_project_id` 并 500。补专门的 `decode_pairing_blob()`（含 `expires_at` 过期校验）。
2. **`capabilities` 类型错**：`--capabilities` 是 `nargs="*"`，单个值时解析成字符串，平台 422 `list_type`。统一转 `list[str]`。
3. **POST 不读请求体导致 RST**：Windows 下客户端报 `ConnectionAborted/WinError 10053`。改为所有 POST 先排空请求体（含解释性注释）。
4. **一台机器只能注册一次 → 重装后永远接不进来**：平台按设计拒绝重复 `device_id`。现在内核**自动追加随机后缀重试一次**（`device-<host>` → `device-<host>-a1b2`）并回报最终标识；其他 409（如公钥重复）不重试。
5. **PS 5.1 的 EAP=Stop 会中止原生程序**：PyInstaller/electron-builder 的进度日志走 stderr，`$ErrorActionPreference='Stop'` 下即使 `*>&1` 也拦不住（`NativeCommandError`）。构建脚本改为 `Continue` + 每个原生调用显式检查 `$LASTEXITCODE`。

另外发现并登记：**Codex CLI 0.154 此前被判 `UNSUPPORTED`**（兼容矩阵只写了 `0.153.`）。按平台机制把 `0.154.` 登记为已测版本族（附实测依据），而不是绕过门禁——现在 `/status` 正确显示 `AVAILABLE · 0.154.0`。

## 3. 与计划的偏差

**有偏差，共 3 处：**

1. **DP-1 期间完成了 DP-1-03/DP-1-02 的核心**（已在 DP-0 交接说明）：先打通"契约 + 打包"这条不需要 Electron 的线，再让壳建立在已证实的接口上。
2. **新增 `scripts/verify-desktop-install.ps1`（计划外）**：为了验收"安装→启动→卸载无残留"这条 DP-1 验收项，写了可重复执行的验证脚本（静默安装到临时目录、检查随包内核、启动并查 `/health`、静默卸载并核对残留）。DP-6 的 `demo-desktop.ps1` 可复用它。
3. **`dist-sidecar/` 与 `apps/desktop/dist/` 留在仓库内**（分别 87MB / 138MB）：它们是构建与分发的产物，不是源码。**没有 .gitignore 可依赖（本目录不是 git 仓库）**——若日后纳管 git，这两个目录必须先忽略。

## 4. 测试与验证

```text
后端  apps/api   : Ran 291 tests, OK (skipped=13)
Agent apps/agent : Ran 208 tests, OK (skipped=9)   （含 30 项 sidecar 契约测试）
前端  apps/web   : npm run build → 19 个静态页；npx tsc --noEmit 无错误
构建  : .\scripts\build-desktop.ps1 -SkipInstall → 内核 84.9MB + 安装包 137MB
```

**安装包验收（`scripts/verify-desktop-install.ps1` 实测）**

| 验收点 | 结果 |
| --- | --- |
| 静默安装到指定目录 | 退出码 0；安装内容含 `Math Agent Platform.exe`（234.9MB）与 **`resources\sidecar\math-agent-sidecar.exe`（11.5MB，随包内核）** |
| 安装版启动后内核被拉起 | 30 秒内写出 `sidecar.json`（port=53270, contract=v1）；`/health` 返回 `ok`（version 0.1.0） |
| 静默卸载 | 安装目录残留 **0**、注册表卸载项残留 **0** |
| 凭据残留 | `cmdkey /list` 中 `MathAgentPlatform/*` **28 条**——这些是本次与历史测试配对写入的**真实凭据**（设备/项目 Token），**属 DP-4-04 的卸载清理范围**，本阶段未清理 |

**深链端到端（两条独立实测）**

1. `map://pair?platform=…&blob=…` → 运行中的壳经 `second-instance` 收到 argv → 转交内核 → 平台出现新设备（`device-fallrain-9ac1`，自动换标识生效）；
2. 平台 `/devices` 配对弹窗的「接入这台电脑」锚点 `href` 以 `map://pair?platform=…&blob=…` 开头，且命令行兜底仍在。

## 5. 尚未完成与边界

- **内核目前不连平台**：`/status` 显示 `connection.state=disconnected`。连接监督 + 任务循环的合并是 **DP-2-01**；DP-1 只有"配对 + 状态 + 本机探测"。
- **托盘图标是纯色圆点**（脚本生成，16×16），够用但不是设计稿；应用图标是渐变圆角方块（256×256）。
- **安装包未签名**：SmartScreen 会提示"更多信息 → 仍要运行"；代码签名是 **DP-4-01**。
- **卸载不清理凭据**（28 条残留）：DP-4-04 需要在 NSIS 里加 `deleteAppDataOnUninstall` 或自定义卸载钩子（注意：删凭据是破坏性动作，应让用户选）。
- **深链在未安装桌面端时无响应**：网页已用文案说明"未安装时浏览器不会响应"，但**下载页（DP-4-05）尚未做**，用户目前只能从仓库文档找到安装包。
- **`npm 11` 会拦截 electron 的 postinstall**：构建脚本已自动补跑 `node node_modules/electron/install.js`，手工构建时需注意。
- **内核与壳的崩溃互不拖死**：实测"杀掉内核 → 壳自动重启"（最多 3 次），但"壳崩溃后内核是否残留"未实测（`before-quit` 会发 `/shutdown`，强杀则不会）。

## 6. 下一步

- 下一阶段：**DP-2 常驻任务循环 + 本地 Agent 监测**（入口条件：DP-1 已退出）
- 建议的下一批工作项：`DP-2-01`（machine_service 增加任务循环）、`DP-2-02`（`daemon-run` 合并常驻体）、`DP-2-03`（本机 Agent 清单周期上报）、`DP-2-05`（后端 B1/B2：心跳字段落库 + 设备能力刷新）、`DP-2-06`（后端 B3：`GET /api/agent/me`）
- 提醒：DP-2 会动 `apps/api/app/store.py`（心跳落库）与 `apps/agent/machine_service.py`（任务循环），两者都属平台核心，**必须补契约测试并保持 §6.3 的不变式（I1 不回归 Demo 1.0）**。

## 7. 复现命令

```powershell
# 构建（内核 + 安装包）
.\scripts\build-desktop.ps1

# 安装包验收（静默装到临时目录 → 验证 → 卸载）
.\scripts\verify-desktop-install.ps1

# 开发态：内核
python -X utf8 apps/agent/agentd.py sidecar-run --state-dir "$env:TEMP\map-sidecar"

# 开发态：壳（会弹窗口与托盘）
cd apps\desktop; $env:MAP_STATE_DIR = "$env:TEMP\map-shell"; npx electron .

# 深链
npx electron . "map://pair?platform=http://127.0.0.1:8010&blob=<配对串>"

# 回归
cd ..\..\apps\agent; python -X utf8 -m unittest discover -s . -p "test_*.py"   # 208
cd ..\api;            python -X utf8 -m unittest discover -s . -p "test_*.py"   # 291
```

## 8. 壳/内核版本对照

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| 桌面端壳 | 0.1.0 | `Math Agent Platform Setup 0.1.0.exe`（137MB，未签名） |
| Python 内核 | 0.1.0 | ONEDIR 84.9MB；安装后位于 `resources/sidecar/` |
| sidecar 契约 | v1 | 冻结；实现 30 项测试；本阶段追加 `identity.last_pair_error` 与"设备标识冲突自动重试"行为 |
| 协议 | schema_version 1.0 | `packages/agent_protocol` 未改动 |
| Electron | 44.4.0 | npm 11 需手动补跑 postinstall |
| PyInstaller | 6.22.3 | 仅打包用 |