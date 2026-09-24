# 桌面端客户端实施计划书（一键接入 + 本地 Agent 自动监测）

> 日期：2026-09-16
>
> 状态：`PLAN`（§10 有 6 项待拍板，均已给默认值，不停工等待）
>
> 主线：把"下载一个安装包就能接入平台并自动干活"从规划推进到可交付
>
> 上游依据：`docs/DESKTOP_CLIENT_PLAN.md`（选型与架构，本文档是其执行级展开）、`docs/DEMO_1_0_IMPLEMENTATION_PLAN.md`（UX 主线，已收尾）、`docs/CODEX_EXECUTOR.md`（Codex 执行体）

---

## 0. 本文档的用法（防偏移的第一道闸）

本文档是**桌面端主线的单一事实来源**，与架构规划的分工是：

- `DESKTOP_CLIENT_PLAN.md` = **为什么这么选**（技术选型、架构图、协议设计）；
- 本文档 = **怎么做、做到什么算完成**（阶段、工作项、验收命令、禁区、交接）。

冲突时以本文档为准，但**选型理由以架构文档为准**——两者不是重复关系。

### 两条硬性使用规则

**规则一：开工前必读。** 任何一次续接（新会话、上下文压缩后、隔天回来），按顺序读：

1. 本文档 `§0`（用法）、`§3`（冻结决策）、`§4`（阶段总览）——约 3 分钟；
2. 当前阶段的 `§5.x` 小节**全文**（工作项表里每一行的文件锚点就是本批要动的范围）；
3. `docs/handoffs/` 下最近一份 `DP-*` 交接（若当前阶段已开工）；
4. `docs/DESKTOP_CLIENT_PLAN.md` 的 §4（选型）与 §5（sidecar 契约）——只在需要动壳/契约时读。

**规则二：先跑基线再动手。** 执行 `§7` 的基线命令，确认当前工作树是绿的（含 `scripts/demo-1.0.ps1` 的 17 项）。基线不绿时先修基线，不开始新工作项。

**不做的事**：不在本文档之外另起桌面端计划；不把决策记在聊天里；不因为"顺手"改动 Demo 1.0 已验收的行为（见 §3 DE9）。

---

## 1. 任务背景

### 1.1 为什么要做桌面端

Demo 1.0 已把平台做成"人只点界面就能干活"（UX-0…UX-7 全部退出，见 `docs/DEMO_1_0_ACCEPTANCE.md`），但**把本机 Agent 接进来这一步仍然需要**：仓库、Python 环境、PowerShell 脚本，外加手工复制命令与配对串。这挡住了"把平台给队友用"这件事——队友不一定有 Python，也不该学这套。

桌面端要解决的就是这一段：**下载 → 输入平台地址 → 网页点一下 → 之后不用管**。

### 1.2 现状盘点（结论：大半个桌面端已在仓库里）

可直接复用的资产：连接监督与退避（`machine_service.py` 的 `MachineAgentService`）、本地紧急停止、Windows 服务安装与故障恢复（`windows_service_adapter.py`）、用户会话 Worker 与 ConPTY（`session_runtime.py`/`conpty_runner.py`）、凭据管理器（`credential_store.py`）、本地补传队列（`local_state.py`）、ETW 访问观测、外部 CLI 探测与 Codex 执行体、平台侧配对与一次性 Token、Web 接入向导（`/devices`）。

**四个缺口**（本文档要补的）：

| # | 缺口 | 证据 |
| --- | --- | --- |
| G1 | 任务循环与连接监督是两套进程：`machine_service.py` 里没有任何 claim/task 逻辑；任务循环在 `agentd.py` 的 `worker-run`（前台命令） | 见 `DESKTOP_CLIENT_PLAN.md` 附录 A |
| G2 | 心跳里的本机 Agent 清单被平台丢弃：`record_agent_heartbeat()` 只更新 `last_seen`/`status` | `apps/api/app/store.py:1376-1394` |
| G3 | 无配对深链、无安装包与自动更新；接入依赖仓库 + Python | `scripts/connect-agent.ps1` |
| G4 | Agent 侧无法查询自身授权（靠 `--grant` 显式传入），配置不能集中管理 | `agentd._resolve_worker_identity` |

### 1.3 本机工具链事实（影响选型，已实测）

node `v24.19.0` / npm `11.17.0` 已安装；**cargo/rustc 未安装**；pyinstaller 未安装（可 pip 安装）。这是"默认 Electron 而非 Tauri"的直接依据。

---

## 2. 目标与非目标

### 2.1 目标（可验收口径）

1. **零依赖安装**：在干净的 Windows 上只装一个安装包即可接入；不需要仓库、Python、PowerShell。
2. **一键配对**：平台网页点「接入这台电脑」→ 系统调起桌面端 → 自动完成配对；未安装时给出下载指引。
3. **自动连接**：开机自启、断线自动重连；离线由平台侧心跳超时判定（UX-3 已实现，保持复用）。
4. **自动监测**：本机可用 Agent（Codex/Claude/自研）与版本、会话状态、队列长度随心跳上报，平台 `/devices` 可见。
5. **自动干活**：常驻任务循环——有任务自动领取执行、无任务退避；托盘可见"执行中 N 个任务"并可一键紧急停止。
6. **不回归**：Demo 1.0 的既有验收（`scripts/demo-1.0.ps1` 17 项 + 三个回归套件）保持全绿。

### 2.2 非目标

- 首版不做 macOS/Linux（见 §5 D5，按需启动）；
- 不把平台 Web 整体搬进桌面端；桌面端只内嵌必要页面 + 本地状态页；
- 不改 Gateway 为服务端推送（沿用拉取，见 §3 DE5）；
- 不引入云端 Runner；
- 不做多任务并发调度（保持单并发，DE12）；
- 不做账号体系（沿用设备配对 + 项目授权）。

---

## 3. 冻结决策

本阶段期间视为冻结基线，变更必须走 `§6.5` 的追加流程。

| # | 决策 | 内容与理由 | 来源 |
| --- | --- | --- | --- |
| DE1 | 壳用 **Electron** | 本机/团队工具链在 TS 侧（node 已装，Rust 未装）；难点在 Python 内核而非壳。sidecar 契约独立（DE3），日后可换 Tauri 而不动内核 | 架构文档 P-1 默认值 |
| DE2 | 内核用 **现有 Python（agentd + machine_service）打包为 PyInstaller ONEDIR sidecar** | 服务安装、ConPTY、ETW、凭据、补传队列已实现且有测试覆盖（Agent 178 项）；重写等于重做最难的部分。ONEDIR 而非 onefile：启动快、便于排障、少被杀软误报 | 架构文档 §4.1 |
| DE3 | 壳内核用 **回环 HTTP + 每次启动生成的 Bearer 令牌**，契约冻结在 `docs/SIDECAR_CONTRACT.md`（DP-0 产出） | 最好调试；仅监听 `127.0.0.1`；令牌写临时文件（权限限当前用户）。Named Pipe 已有代码，作为后续加固选项而非首版 | 架构文档 §5 |
| DE4 | 首版**仅 Windows** | ConPTY/ETW/SCM/凭据管理器都是 Windows 专属且已实现；跨平台需降级方案（D5） | 架构文档 P-2 默认值 |
| DE5 | **不允许平台主动派活** | 沿用拉取模型（Demo 1.0 决策 D5）；改推送属协议级变更，需先走决策变更 | 继承 UX-5 D5 |
| DE6 | 凭据**只在 Windows Credential Manager**，壳与本地文件都不得持久化设备/项目 Token | 现有实现已如此（`credential_store.py`）；桌面端不得为了"方便"把它写进配置文件 | 本计划 |
| DE7 | 更新源由**平台托管**（MinIO/静态目录），客户端校验最低兼容协议版本 | 自托管部署一致；拒绝降级并提示升级 | 架构文档 P-4 默认值 |
| DE8 | 安装与状态路径固定：程序放 `Program Files`，**可写状态一律在 `%LOCALAPPDATA%\MathAgentPlatform\`** | 避免 UAC 提权与"家目录被污染"；也便于卸载清理 | 本计划 |
| DE9 | **不回归 Demo 1.0**：`demo-1.0.ps1` 17 项与三个回归套件必须保持全绿 | 桌面端是增量；接线期间不得改动已验收行为 | 本计划 |
| DE10 | 配对深链 `map://pair?blob=…` 由壳注册；网页必须保留**复制粘贴兜底** | 未安装/未注册深链时用户不至于卡死 | 架构文档 §4.1 |
| DE11 | **单实例**（单实例锁）+ **一设备一机器**（沿用"一公钥只能注册一台设备"） | 避免双开导致连接表冲突；公钥唯一是平台既有不变式 | 本计划 |
| DE12 | 任务循环默认**单并发** | 沿用 UX-5 决策；并发调度需要平台侧租约与队列设计 | 继承 UX-5 |
| DE13 | 壳与内核的**生命周期绑定但不共享内存**：壳负责启动、健康检查、异常重启；内核退出不影响已完成的运行台账 | 内核已有 durable queue，重启后补传 | 本计划 |
| DE14 | **紧急停止 = 停机，不是退出内核** | 内核收到 `/emergency-stop` 后停止领取并断开平台连接（平台侧看到掉线），但**保持契约服务存活**，因此托盘可「解除紧急停止」并自动恢复；若直接退出进程，壳会当成崩溃反复重启（最多 3 次后放弃），用户再也无法从界面恢复 | DP-2 追加（实测发现） |
| DE15 | **授权串可运行期交付**（`POST /grant` + `map://grant`） | 没有它，"下载桌面端就能干活"退化成"还要手工跑 PowerShell 命令"（违反 §2.1 目标）；授权串含项目 Token，只经本机回环 + 启动令牌传输且不写日志 | DP-2 追加 |
| DE16 | **每个连接会话换 `connection_id` 并把本地 outbox 序列对齐到 1** | 平台侧 `connection_id` 是主键、序列按连接计；复用 id 会撞唯一约束，只换 id 不重编号会陷入「要求重放→对不上→再要求重放」死循环（断线重连必然遇到） | DP-2 追加（实测发现） |
| DE17 | **Codex 的瞬断重试提示不算任务失败** | `Reconnecting... / stream disconnected` 是 Codex 自己的重试，退出码 0 且拿到最终回复就是成功；这类提示降级为摘要里的 `warnings`（见 `docs/CODEX_EXECUTOR.md` §3.4） | DP-2 追加（实测发现） |

---

## 4. 阶段与依赖总览

编号规则：阶段 `DP-0` … `DP-6`，工作项 `DP-{阶段}-{序号}`。**注意与既有编号空间区分**：`UX-*` = Demo 1.0 主线（已收尾），`D1–D5`/`B1–B6` = 架构规划里的阶段与后端项（`DESKTOP_CLIENT_PLAN.md`）——本实施计划把它们展开为 `DP-*` 工作项，并在工作项里标注对应的 `D#`/`B#` 以便回溯。

```text
DP-0 基线冻结与契约冻结
  │
  └─> DP-1 壳与内核骨架（最小可用配对）
        │
        ├─> DP-2 常驻任务循环 + 本地 Agent 监测（含后端 B1/B2/B3）
        │     │
        │     └─> DP-3 本地可观测与诊断
        │           │
        │           ─> DP-4 分发与运维（签名 / 更新 / 静默安装 / 下载页）
        │
        └─> DP-6 交付验收（依赖 DP-1…DP-4 全部退出）

DP-5 跨平台（按需，默认不启动）
```

依赖是**入口条件**而非建议：DP-1 未退出不开始 DP-2（否则任务循环没有壳可承载）；DP-6 必须等 DP-1…DP-4 退出。DP-5 默认不启动（DE4）。

---

## 5. 阶段细则

### DP-0：基线冻结与契约冻结

**目标**：让后续每个阶段从同一条基线出发，并把壳↔内核的接口先钉死。

**入口条件**：无。

| 编号 | 工作项 | 对应 | 交付物 |
| --- | --- | --- | --- |
| DP-0-01 | 记录基线：后端 291 项、Agent 178 项、前端 17 页、`demo-1.0.ps1` 17 项 | — | 本文档 §7 与 `docs/IMPLEMENTATION_STATUS.md` |
| DP-0-02 | 冻结 sidecar 契约：把架构文档 §5 的端点表落成正式文档（含请求/响应形状、错误码、启动令牌约定） | D1-03 前置 | `docs/SIDECAR_CONTRACT.md` |
| DP-0-03 | 桌面端交接模板（沿用 UX 模板结构，增加"壳/内核版本对照"一节） | — | `docs/handoffs/_TEMPLATE_DP_HANDOFF.md` |
| DP-0-04 | 工具链自检脚本：检查 node/npm、Python 版本、PyInstaller、签名工具是否就绪 | — | `scripts/desktop-toolchain-check.ps1` |

**验收标准**

```powershell
cd apps\api;  python -X utf8 -m unittest discover -s . -p "test_*.py"   # 291 (skipped=13)
cd apps\agent; python -X utf8 -m unittest discover -s . -p "test_*.py"  # 178 (skipped=9)
cd apps\web;  npm run build                                             # 17 页
.\scripts\desktop-toolchain-check.ps1                                   # 逐项报告就绪状态
```

**退出条件**：契约文档存在且冻结；工具链检查脚本能在缺件时明确报出缺什么。

**Do NOT**：不要在这一阶段写 Electron/PyInstaller 代码。

---

### DP-1：壳与内核骨架（最小可用配对）

**目标**：装包 → 输入平台地址 → 网页点「接入这台电脑」→ 托盘变绿 → 平台 `/devices` 出现该设备。

**入口条件**：DP-0 退出。

| 编号 | 工作项 | 对应 | 关键文件 |
| --- | --- | --- | --- |
| DP-1-01 | Electron 壳骨架：主进程、托盘（状态灯：灰/黄/绿/红）、窗口、单实例锁 | D1-01、DE11 | `apps/desktop/`（新建） |
| DP-1-02 | sidecar 打包链路：PyInstaller ONEDIR + 壳内启动/健康检查/异常重启 | D1-02、DE2、DE13 | `apps/desktop/sidecar/`、`apps/desktop/build/` |
| DP-1-03 | 壳↔内核实现：`/health` `/pair` `/status` `/shutdown`（按 `docs/SIDECAR_CONTRACT.md`） | D1-03、DE3 | `apps/agent/sidecar_api.py`（新建） |
| DP-1-04 | 配对深链：壳注册 `map://`、平台网页生成深链；保留复制粘贴兜底 | D1-04、DE10 | `apps/web/app/devices/page.tsx` |
| DP-1-05 | 路径与权限：可写状态统一到 `%LOCALAPPDATA%\MathAgentPlatform\`；程序目录只读 | D1-05、DE8 | 壳与 sidecar 的路径常量 |
| DP-1-06 | NSIS 安装包（未签名，标注内测） | D1-06 | electron-builder 配置 |

**验收标准**

1. 干净 Windows（无 Python、无仓库）安装后：输入平台地址 → 网页点接入 → 托盘变绿 → `/devices` 出现该设备且 active；
2. 从任务管理器杀掉 sidecar 进程，壳在 10 秒内自动拉起并恢复连接；
3. 断链重连：拔网线 30 秒后插回，托盘状态自动回到绿；
4. **回归**：`demo-1.0.ps1` 17 项仍全过（桌面端不得改变既有接入路径的行为）。

**退出条件**：一次安装能走完 §3 的 ①–③；壳与内核的崩溃互不拖死（各自可单独重启）。

**Do NOT**：不要在这一阶段接任务循环（DP-2）；不要为"顺路"改造 `/devices` 页既有交互（只加深链入口）。

---

### DP-2：常驻任务循环 + 本地 Agent 监测

**目标**：安装后不用做任何事，有任务自动执行；平台能看到本机有哪些可用的 Agent。

**入口条件**：DP-1 退出。

| 编号 | 工作项 | 对应 | 关键文件 |
| --- | --- | --- | --- |
| DP-2-01 | `machine_service` 增加任务循环：连接就绪后 claim→执行→上报，断线暂停、重连恢复 | D2-01、G1、DE12 | `apps/agent/machine_service.py` |
| DP-2-02 | 合并常驻体：`daemon-run`（连接监督 + 任务循环）；保留 `worker-run` 前台模式用于调试 | D2-02 | `apps/agent/agentd.py` |
| DP-2-03 | 本地 Agent 清单探测与周期上报（复用 `detect_codex_cli` + `cli_adapters` 探针），支持手动 rescan | D2-03 | `apps/agent/codex_executor.py`、`apps/agent/agentd.py` |
| DP-2-04 | 托盘菜单：暂停/恢复领取、紧急停止、查看日志、打开本地状态页 | D2-04 | 壳 |
| DP-2-05 | **后端 B1/B2**：心跳字段落库（`adapter_versions`/`capabilities`/`running_run_ids`/`local_queue_length`/`user_session_state`/`resource_summary`）与设备能力刷新 | B1、B2、G2 | `apps/api/app/store.py:1376`、`apps/api/app/main.py` |
| DP-2-06 | **后端 B3**：`GET /api/agent/me`——设备 Token 换自身配置（授权项目与能力、执行体偏好、心跳间隔、允许的 sandbox 上限） | B3、G4 | `apps/api/app/main.py` |
| DP-2-07 | **后端 B4**：`GET /api/devices` 扩展返回最近能力上报与队列长度；`/devices` 页展示"本机可用 Agent" | B4 | `apps/api/app/main.py`、`apps/web/app/devices/page.tsx` |
| DP-2-08 | **授权深链**：内核 `POST /grant` + 壳处理 `map://grant` + 设备页「把授权交给本机桌面端」（DE15） | — | `apps/agent/sidecar_api.py`、`apps/desktop/src/main.js`、`apps/web/app/devices/page.tsx` |

**验收标准**

1. 装包后不改任何配置：平台建一个 `worker_executor=codex` 任务 → **一分钟内**自动领取并执行完成，`/runs` 摘要含 Codex 回复；
2. `/devices` 显示该设备"Codex 0.154 可用"与队列长度；在机器上装/卸一个 CLI 后（或点 rescan）清单随之更新；
3. 托盘"暂停领取"后新任务不被领取，恢复后立即领取；
4. 紧急停止生效时任务循环停止领取，且平台侧能看到该 Agent 掉线（复用 UX-3 的超时判定）；
5. **回归**：Agent 178 + API 291 全绿（新增测试计入数量）。

**退出条件**：全自动闭环成立；B1/B2/B3/B4 均有契约测试；新增测试计入回归总数。

**Do NOT**：不要实现多任务并发（DE12）；不要把 Gateway 改成服务端推送（DE5）；不要让 `daemon-run` 与 `worker-run` 同时连同一 `device_id`（DE11）。

---

### DP-3：本地可观测与诊断

**目标**：任务失败时用户能在本地页面看到原因，而不是去翻 JSONL。

**入口条件**：DP-2 退出。

| 编号 | 工作项 | 对应 |
| --- | --- | --- |
| DP-3-01 | 本地状态页：连接状态、当前任务、队列长度、最近 50 条日志、本机 CLI 清单 | D3-01 |
| DP-3-02 | 一键导出诊断包（`agentd.db` 摘要、日志、版本清单、**脱敏后的**配置） | D3-02、DE6 |
| DP-3-03 | 失败可视化：把 `parse_codex_jsonl` 的 `error_events`/`unparsed` 呈现在界面上 | D3-03 |

**验收标准**

1. 触发一次失败任务（如故意让 Codex 连不上服务商）→ 本地页面显示可读原因；
2. 诊断包里**不含**任何 Token 明文（脚本化断言：搜索 `dvc_`/`prj_`/`sk-` 前缀应无命中）；
3. 日志尾随窗口在任务执行期间能实时更新。

**退出条件**：三条验收有可复现步骤；诊断包脱敏有自动化断言。

**Do NOT**：不要把诊断包上传到平台（首版只导出到本地）。

---

### DP-4：分发与运维

**目标**：能像正常软件一样分发、升级、静默安装与干净卸载。

**入口条件**：DP-3 退出。

| 编号 | 工作项 | 对应 |
| --- | --- | --- |
| DP-4-01 | 代码签名与 SmartScreen 说明（内测版明确标注） | D4-01、P-3 |
| DP-4-02 | 自动更新：electron-updater + 平台更新源（**后端 B6**：`GET /api/client/latest` 返回版本/下载地址/最低兼容协议版本） | D4-02、B6、DE7 |
| DP-4-03 | 开机自启策略：默认装 Windows 服务（复用 `service-install`），可选"仅登录后运行" | D4-03、P-5 |
| DP-4-04 | 静默安装参数 + 卸载清理（凭据、服务、本地库） | D4-04 |
| DP-4-05 | 平台下载页（`/devices` 或 `/settings` 提供安装包与版本说明） | D4-05 |

**验收标准**

1. 平台发布新版本后，旧客户端能自更新并保持连接；
2. 客户端拒绝与"最低兼容协议版本"不符的升级路径（或反之，阻止过旧客户端连接并提示升级）；
3. 卸载后：无残留服务、无残留凭据（`cmdkey /list` 无 `MathAgentPlatform/*`）、无 `%LOCALAPPDATA%` 残留目录；
4. 静默安装参数在无交互环境下能一次成功。

**退出条件**：四条验收通过；下载页可访问且版本信息与安装包一致。

**Do NOT**：不要在没有签名的情况下对外发布（内测除外，需在下载页标注）。

---

### DP-5：跨平台（按需，默认不启动）

**目标**：在 macOS/Linux 上提供同等的接入与执行能力（含明确的能力降级说明）。

**入口条件**：DP-4 退出 **且** 明确决定投入（默认不启动）。

| 编号 | 工作项 | 对应 |
| --- | --- | --- |
| DP-5-01 | 凭据抽象：macOS Keychain / Linux Secret Service（现有实现 Windows 专属且 fail-closed） | D5-01 |
| DP-5-02 | 服务化替代：launchd（macOS）/ systemd user service（Linux） | D5-02 |
| DP-5-03 | 能力降级矩阵：无 ConPTY/ETW 时的终端与观测行为，并在界面明确标注 | D5-03 |
| DP-5-04 | 打包：dmg / AppImage / deb | D5-04 |

**验收标准**：三平台各跑一遍 §2.1 的 1–5 条；降级能力在界面有明确标注（不谎报能力）。

**Do NOT**：不要在未完成 DP-5-03 的情况下声称跨平台"功能对等"。

---

### DP-6：交付验收

**目标**：把"下载即用"的完成定义变成可出示的证据。

**入口条件**：DP-1 至 DP-4 全部退出。

| 编号 | 工作项 |
| --- | --- |
| DP-6-01 | `docs/DESKTOP_ACCEPTANCE.md`：逐条映射 §2.1 的目标到实测证据（干净机器安装、自动连、自动干活、监测可见、卸载干净、不回归） |
| DP-6-02 | 端到端演示脚本 `scripts/demo-desktop.ps1`：从安装包开始走完整旅程并逐项断言（沿用 `demo-1.0.ps1` 的"自动断言 + 检查点"风格） |
| DP-6-03 | 文档收尾：README 增「桌面端（下载即用）」一节并替换当前的"关于下载即用的桌面端"规划指引；`IMPLEMENTATION_STATUS.md` 归档本主线 |

**验收标准**：`demo-desktop.ps1` 在干净机器一次通过；验收文档每条目标都有证据链接。

**Do NOT**：不要在验收阶段引入新功能；发现的问题记入后续清单。

---

## 6. 防偏移机制

上下文一长最容易发生的四种偏移：**目标漂移**（做着做着去优化别的）、**决策漂移**（同一问题两次不同答案）、**范围漂移**（顺手改坏稳定模块）、**事实漂移**（把"子代理说的"当成"我验证过的"）。

### 6.1 上下文压缩后的恢复协议

1. 读本文档 `§0`、`§3`、`§4`；
2. 读当前阶段的 `§5.x` 全文；
3. 读最近一份 `DP-*` 交接的 `§2 实际完成内容` 与 `§5 尚未完成与边界`；
4. 跑 `§7` 基线（含 `demo-1.0.ps1`）；
5. 在回复里**用一句话复述**："当前阶段是 DP-x，本批工作项是 DP-x-yy，退出条件是……"。复述不出来说明上下文没恢复。

### 6.2 术语与编号冻结

- 编号空间：`UX-*`（Demo 1.0，已收尾、不可复活）、`D#`/`B#`（架构规划，仅用于回溯引用）、`DP-*`（本主线，唯一可新增的编号）；
- 新增编号只能追加，不得复用或重排既有编号；
- 术语沿用 `docs/DOMAIN_GLOSSARY.md`；桌面端新增术语（壳/内核/sidecar/深链）在 `docs/SIDECAR_CONTRACT.md` 里给出定义，避免同义词扩散。

### 6.3 不变式（每个阶段退出前必须成立）

| # | 不变式 |
| --- | --- |
| I1 | `demo-1.0.ps1` 17 项与三个回归套件保持全绿（DE9） |
| I2 | 设备 Token / 项目 Token 绝不出现在：日志、诊断包、本地配置文件、壳的持久化状态（DE6） |
| I3 | sidecar 只监听 `127.0.0.1`，且所有写操作要求启动令牌（DE3） |
| I4 | `packages/agent_protocol` 既有字段语义不变；新增字段必须可选且有默认值 |
| I5 | 一个 `device_id` 只有一个活动连接（DE11） |
| I6 | 平台侧迁移（如 B1 新表/新列）必须有对应的 SQLite 开发版与 PostgreSQL 迁移路径 |
| I7 | 不改 `hyper-rag-service`、不改 Gateway 为推送、不动 Demo 1.0 已验收的页面行为 |

### 6.4 禁区清单（本主线期间禁止改动）

- `packages/agent_protocol` 的信封/序号/幂等语义（只能追加可选字段）；
- `apps/api/app/gateway.py` 的逐帧应答模型（DE5）；
- `apps/api/app/collaboration.py` 的中继语义；
- `packages/competition_packs/` 内置包内容；
- `apps/api/app/kb_gateway.py` 的错误码映射；
- `infra/docker-compose.yml`（除 DP-4 明确需要新增下载页静态挂载）；
- Demo 1.0 已验收页面的交互语义（`/devices` 只允许**新增**深链入口与能力展示）。

### 6.5 决策变更流程

1. **不允许**修改 §3 已有决策的结论；要变更，追加 `DE14`、`DE15`…，内容含：变更内容、原因、影响面、迁移方案、生效阶段；
2. 在原决策行"来源"列末尾标注 `（被 DEx 取代）`，保留原行；
3. 若变更影响已完成阶段，必须在 `docs/IMPLEMENTATION_STATUS.md` 追加"回归影响"并重跑 §7 基线。

### 6.5b DP-2 决策追加与实测修正（2026-09-16）

DP-2 执行期间发现四处**必须留痕**的偏差/修正，均已按 §6.5 追加为 `DE14`–`DE17`：

| 发现 | 现象 | 处理 |
| --- | --- | --- |
| 事件循环饿死 | 任务循环里 `claim→执行→上报` 若全程同步完成，`await` 不会真正让出控制权，同一条事件循环上的心跳/控制任务被饿死（测试里表现为"卡住"） | 每轮结束时显式 `await asyncio.sleep(0)`（`task_loop.run_forever`） |
| 重连序列错位 | 换 `connection_id` 后复用本地自增序列 → 平台要求重放、重放又对不上 | `LocalAgentState.renumber_pending_events()`：新会话把未确认事件重编号为从 1 开始，已确认的直接清掉（DE16） |
| 紧急停止退出进程 | 内核退出后壳反复重启，托盘「解除紧急停止」失效 | 常驻体在紧急停止期间保持存活，只停领取 + 断连（DE14） |
| 授权能力不足的表现 | 授权串缺 `task.result`/`run.complete` 时，任务干完了但平台永远停在 RUNNING，只在运行日志里看到 403 | 内核启动时校验必要能力并提示缺哪几项（`missing_task_loop_capabilities`）；按设备页默认能力集授权即可 |

**DP-2 验收证据（实机，2026-09-16）**：平台建 `worker_executor=codex` 任务后，内核 **3–10 秒领取、30–60 秒完成**，
任务进 `WAITING_REVIEW`、Run `SUCCEEDED` 且摘要含 Codex 回复；`/devices` 显示
`执行体：codex-cli 0.154.0 · 队列 N · 会话 logged_in`；暂停期间任务保持 `READY`、恢复后立即被领取；
紧急停止后平台侧 Agent 变 `offline` 且不领取任务；`GET /api/agent/me` 返回 13 项能力与运行参数。

**回归基线（DP-2 退出时）**：Agent **247** 项 OK、API **305** 项 OK、`demo-1.0.ps1` **17/17**、
前端 `next build` 19 页（`/devices` 6.42 kB）。

### 6.6 阶段交接文档模板

沿用 `docs/handoffs/_TEMPLATE_UX_HANDOFF.md` 的结构；桌面端另加一节：

```markdown
## 8. 壳/内核版本对照
| 组件 | 版本 | 说明 |
| --- | --- | --- |
| 桌面端壳 | x.y.z | electron-builder 产物 |
| Python 内核 | x.y.z | agentd 版本（与 agent_version 一致） |
| 协议 | schema_version | 与平台最低兼容版本比对结果 |
```

### 6.7 禁止的"顺手"行为

- 顺手升级 Electron/Next/Python 依赖；
- 顺手把 `worker-run` 前台模式删掉（它是调试入口，DP-2-02 明确保留）；
- 顺手给桌面端加"自动更新平台 AI 凭据"这类未规划的能力；
- 顺手删除"看起来没人用"的探测/观测代码（ETW 等属生产验收项）；
- 顺手把 `demo-1.0.ps1` 改成"跳过界面步骤"以便跑绿（那会削弱它作为验收证据的效力）。

---

## 7. 测试与验收基线

**后端**（`apps/api`）：

```powershell
$env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"     # 基线：291 tests, OK (skipped=13)
```

**Agent**（`apps/agent`，同上 PYTHONPATH 形式）：

```powershell
python -X utf8 -m unittest discover -s . -p "test_*.py"     # 基线：178 tests, OK (skipped=9)
```

**前端**：

```powershell
cd apps\web; npm run build                                    # 基线：17 个页面静态预渲染；npx tsc --noEmit 无错误
```

**端到端（不回归）**：

```powershell
.\scripts\demo-1.0.ps1 -Api http://127.0.0.1:8010 -AutoApprove   # 基线：17/17 项通过（需先起空库实例）
```

**桌面端（本主线新增，随阶段生长）**：

```powershell
.\scripts\desktop-toolchain-check.ps1        # DP-0
.\scripts\demo-desktop.ps1                   # DP-6
```

---

## 8. 风险与回滚

| 风险 | 触发信号 | 应对 |
| --- | --- | --- |
| Python sidecar 被杀软误报/拦截 | 装完就被隔离 | ONEDIR + 代码签名（DP-4-01）+ 白名单说明；必要时把 CLI 探测前移进壳 |
| 深链未注册时点"接入"无反应 | 用户报"点了没反应" | 网页必须在无响应时给出下载与粘贴兜底（DE10） |
| 服务模式与用户会话权限冲突 | ConPTY/桌面控制在服务进程不可用 | 沿用"服务 + 用户会话 Worker"（已有 Session 0 适配器） |
| 自更新导致版本错配 | 老客户端连不上 | 客户端与平台双向校验最低兼容版本（DP-4-02） |
| B1 新表/新列影响既有回归 | API 测试变红 | 迁移必须双写兼容（I6）；先加列后改读 |
| sidecar 与壳的令牌泄露 | 本地其他进程调用 | 令牌仅临时文件（限当前用户）+ 回环监听（I3） |
| 上下文压缩导致阶段错位 | 接手者说不清当前阶段 | §6.1 的复述动作是强制项 |

**回滚粒度**：按工作项。每个工作项应能独立回退。**不提供整阶段回滚**——阶段间有依赖，回滚一个阶段等于回滚后续所有。

**平台侧改动的回滚**：B1–B4 属新增列/新端点，回滚只需停止读取新字段（旧字段仍在）。

---

## 9. 工作量估算

单位人日，含自测与文档，不含等待评审。

| 阶段 | 内容 | 估算 |
| --- | --- | --- |
| DP-0 | 基线冻结与契约冻结 | 1.5 |
| DP-1 | 壳与内核骨架（MVP 配对） | 8 |
| DP-2 | 常驻任务循环 + 监测（含后端 B1/B2/B3/B4） | 7 |
| DP-3 | 本地可观测与诊断 | 4 |
| DP-4 | 分发与运维 | 6 |
| DP-6 | 交付验收 | 2 |
| 合计（DP-0…DP-4 + DP-6） | | **28.5** |
| DP-5 | 跨平台（按需） | 10 |

**最小可用（DP-0…DP-2）≈ 16.5 人日**，即可交付"下载→配对→自动连→自动监测→自动干活"。
关键路径：DP-0 → DP-1 → DP-2 → DP-3 → DP-4 → DP-6。

---

## 10. 待拍板事项（均已给默认值，按默认即可开工）

| # | 事项 | 默认值（已按此执行） | 影响 |
| --- | --- | --- | --- |
| P-1 | 壳技术 Electron / Tauri | **Electron**（DE1） | 换 Tauri：+3 人日 + Rust 工具链；内核契约不变故可后换 |
| P-2 | 首版是否跨平台 | **仅 Windows**（DE4） | 跨平台 +10 人日，且需降级矩阵 |
| P-3 | 代码签名时机 | **DP-1 不签、DP-4 补**（DE7 相关） | 不签则用户需手动"仍要运行"，企业分发受限 |
| P-4 | 更新源位置 | **平台托管**（DE7） | 走外部 CDN 需额外网络与权限规划 |
| P-5 | 服务 vs 托盘常驻默认 | **Windows 服务为默认 + 托盘可选** | 影响安装是否需要管理员权限 |
| P-6 | 是否允许平台主动派活 | **不允许**（DE5） | 允许则需改 Gateway 为推送，属协议级变更 |

---

## 附录 A：关键文件地图

| 区域 | 文件 | 本主线动作 |
| --- | --- | --- |
| 桌面壳（新建） | `apps/desktop/**`（主进程、托盘、深链、builder 配置） | DP-1 起逐步建立 |
| 内核 sidecar API（新建） | `apps/agent/sidecar_api.py` | DP-1-03 |
| 内核常驻体 | `apps/agent/machine_service.py`、`agentd.py` | DP-2-01/02 |
| 本地探测 | `apps/agent/codex_executor.py`、`cli_adapters.py` | DP-2-03 |
| 后端 | `apps/api/app/main.py`、`apps/api/app/store.py` | DP-2-05/06/07、DP-4-02 |
| 前端 | `apps/web/app/devices/page.tsx`、`settings/page.tsx` | DP-1-04、DP-2-07、DP-4-05 |
| 契约与文档 | `docs/SIDECAR_CONTRACT.md`、`docs/DESKTOP_ACCEPTANCE.md`、`docs/handoffs/_TEMPLATE_DP_HANDOFF.md` | DP-0-02/03、DP-6-01 |
| 脚本 | `scripts/desktop-toolchain-check.ps1`、`scripts/demo-desktop.ps1` | DP-0-04、DP-6-02 |

## 附录 B：上游证据索引

| 结论 | 证据 |
| --- | --- |
| 大半个桌面端已在仓库内 | `docs/DESKTOP_CLIENT_PLAN.md` §1.1 的资产表 |
| 任务循环与连接监督分离（G1） | `machine_service.py` 无 claim/task 定义；任务循环在 `agentd.py` 的 `worker-run` |
| 心跳字段被丢弃（G2） | `apps/api/app/store.py:1376-1394` |
| 设备能力是注册快照 | `devices` 表的 `capabilities TEXT NOT NULL`，仅注册时写入 |
| 配对/凭据/服务/会话 Worker 均已实现 | `packages/device_identity`、`credential_store.py`、`windows_service_adapter.py:246`、`session_runtime.py` |
| Codex 执行体已打通 | `docs/CODEX_EXECUTOR.md` §5 实测两条 |
| 本机工具链 | node v24.19.0 / npm 11.17.0 已装；cargo/rustc 未装；pyinstaller 未装 |
| Demo 1.0 已收尾（不得回归） | `docs/DEMO_1_0_ACCEPTANCE.md`、`scripts/demo-1.0.ps1`（17/17） |