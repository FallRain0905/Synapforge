# DP-0 基线冻结与契约冻结 交接

> 交接状态：`PASS`
>
> 日期：2026-09-16
>
> 对应阶段：DP-0（工作项定义见 `docs/DESKTOP_CLIENT_IMPLEMENTATION_PLAN.md` §5）
>
> 上一份交接：`—`（本主线首份）

---

## 1. 本阶段目标

从实施计划 §5 DP-0 抄写：

> **目标**：让后续每个阶段从同一条基线出发，并把壳↔内核的接口先钉死。
>
> **退出条件**：契约文档存在且冻结；工具链检查脚本能在缺件时明确报出缺什么。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 交付物 |
| --- | --- | --- |
| DP-0-01 记录基线 | 完成 | 实测冻结：后端 **291** 项（13 skipped）、Agent **178** 项（9 skipped）、前端 17 页构建通过、`demo-1.0.ps1 -AutoApprove` **17/17 项通过**。已写入实施计划 §7 与本文件 §4 |
| DP-0-02 冻结 sidecar 契约 | 完成 | `docs/SIDECAR_CONTRACT.md`（**v1，冻结**）：传输与鉴权（仅回环 + 启动令牌）、启动握手、8 个端点、错误码表、演进规则（只能追加）、与平台侧通道的边界（Token 不跨通道）。**追加两处**：`identity.last_pair_error`（配对失败原因落状态，壳重启后仍可见）、设备标识冲突的自动重试行为 |
| DP-0-03 交接模板 | 完成 | `docs/handoffs/_TEMPLATE_DP_HANDOFF.md`（比 UX 模板多「壳/内核版本对照」一节） |
| DP-0-04 工具链自检 | 完成 | `scripts/desktop-toolchain-check.ps1`（只读检查 + 缺失项安装指引 + 阻塞项汇总，非零退出码）。实测：`通过 5/8 项；阻塞项 1 个`（PyInstaller 当时未装） |

## 3. 与计划的偏差

**有偏差，共 1 处：**

1. **DP-0 期间顺带完成了 DP-1-03 与 DP-1-02 的核心验证**（计划里属 DP-1）。理由：契约刚冻结，实现它是验证"契约是否可落地"的最快方式，且这段工作**不需要 Electron**——本机 Rust 缺失、Electron 需下载安装，把内核线先打通能让后续壳的工作建立在已证实的接口上。壳与安装包（DP-1-01/DP-1-06）仍在 DP-1 内。

## 4. 测试与验证

```text
后端  apps/api   : python -X utf8 -m unittest discover -s . -p "test_*.py"  → Ran 291 tests, OK (skipped=13)
Agent apps/agent : 同上                                                        → Ran 208 tests, OK (skipped=9)   （基线 178 + 新增 30）
前端  apps/web   : npm run build → 17 页；npx tsc --noEmit → 无错误
端到端           : .\scripts\demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove → 全部 17 项通过
工具链           : .\scripts\desktop-toolchain-check.ps1 → 5/8 通过（阻塞项：PyInstaller）
```

新增测试 `apps/agent/test_sidecar_api.py`（**30 项**）：契约形状（`/health` 免鉴权且不泄露、`/status` 字段齐全、404 稳定码）、鉴权（无/错令牌 401）、**只监听回环**（非回环地址连不上）、启动信息文件（含 contract 与权限）、配对校验（URL/配对串/过期）、暂停与紧急停止开关、日志截断、`/shutdown` 后服务停止、**配对串解码**（合法/缺字段/非法 base64/过期/无 expires_at）、**设备标识冲突自动换标识重试**（并断言其他 409 不重试）、host_slug 与脚本约定一致。

## 5. 尚未完成与边界

- **壳尚未打包**：`/devices` 深链入口（DP-1-04 的网页侧）还没做——目前深链只能手工构造（实测用的就是手工构造的 URL）；安装包（DP-1-06）未做。
- **内核目前没有"平台连接"**：`/status` 显示 `connection.state=disconnected`——因为任务循环与连接监督的合并是 DP-2-01 的工作；DP-1 阶段只有"配对 + 状态 + 本机探测"。
- **托盘图标是纯色圆点**（`assets/tray-{grey,green,yellow,red}.png`，脚本生成）：够用但不是设计稿。
- **`device_name` 在深链配对时等于主机名**：深链只带 blob 与 platform；`agent_name` 走 `/pair` 的显式字段（状态页里可填）。
- **Electron 的 npm postinstall 被拦**：本机 npm 11 默认不跑安装脚本，需手动 `node node_modules/electron/install.js` 才有二进制。已记录，DP-1-06 的打包脚本要处理这一点（或在文档里写明）。

## 6. 下一步

- 下一阶段：**DP-1**（剩余：DP-1-01 壳骨架已完成开发态、DP-1-04 网页侧深链入口、DP-1-05 路径统一（部分完成：状态目录已统一）、DP-1-06 NSIS 安装包）
- 入口条件是否满足：**是**（DP-0 已退出）
- 建议的下一批工作项：`DP-1-04`（`/devices` 页加「接入这台电脑」深链按钮）、`DP-1-06`（electron-builder 打包，把 `dist-sidecar` 作为 extraResources）、`DP-1-02` 收尾（把 PyInstaller 产物落到 `dist-sidecar/` 并写进构建脚本）

## 7. 复现命令

```powershell
# 基线
cd apps\api;   $env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"      # 291
cd ..\agent;   python -X utf8 -m unittest discover -s . -p "test_*.py"   # 208（含 30 项 sidecar 契约测试）
cd ..\web;     npm run build                                   # 17 页

# 工具链自检
.\scripts\desktop-toolchain-check.ps1

# 内核（开发态）
python -X utf8 apps/agent/agentd.py sidecar-run --state-dir "$env:TEMP\map-sidecar"

# 壳（开发态；会弹出窗口与托盘图标）
cd apps\desktop; $env:MAP_STATE_DIR = "$env:TEMP\map-shell"; npx electron .

# 深链（等价于安装后平台网页的「接入这台电脑」）
npx electron . "map://pair?platform=http://127.0.0.1:8010&blob=<配对串>"
```

## 8. 壳/内核版本对照

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| 桌面端壳 | 0.1.0 | 开发态运行（`npx electron .`）；安装包未产出 |
| Python 内核 | 0.1.0 | `agentd sidecar-run`；打包产物实测 87MB ONEDIR（exe 12MB） |
| sidecar 契约 | v1 | 冻结；实现 `apps/agent/sidecar_api.py`，测试 30 项 |
| 协议 | schema_version 1.0 | 未改动 `packages/agent_protocol` |