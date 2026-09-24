# DP-2 常驻任务循环 + 本地 Agent 监测 交接

> 交接状态：`PASS`
>
> 日期：2026-09-16
>
> 对应阶段：DP-2（工作项定义见 `docs/DESKTOP_CLIENT_IMPLEMENTATION_PLAN.md` §5，决策追加见 §6.5b）
>
> 上一份交接：`docs/handoffs/DP_1_SHELL_AND_KERNEL_HANDOFF.md`

---

## 1. 本阶段目标

从实施计划 §5 DP-2 抄写：

> **目标**：安装后不用做任何事，有任务自动执行；平台能看到本机有哪些可用的 Agent。
>
> **入口条件**：DP-1 退出。
>
> **验收标准**
> 1. 装包后不改任何配置：平台建一个 `worker_executor=codex` 任务 → **一分钟内**自动领取并执行完成，`/runs` 摘要含 Codex 回复；
> 2. `/devices` 显示该设备"Codex 0.154 可用"与队列长度；在机器上装/卸一个 CLI 后（或点 rescan）清单随之更新；
> 3. 托盘"暂停领取"后新任务不被领取，恢复后立即领取；
> 4. 紧急停止生效时任务循环停止领取，且平台侧能看到该 Agent 掉线（复用 UX-3 的超时判定）；
> 5. **回归**：Agent 178 + API 291 全绿（新增测试计入数量）。
>
> **退出条件**：全自动闭环成立；B1/B2/B3/B4 均有契约测试；新增测试计入回归总数。
>
> **Do NOT**：不要实现多任务并发（DE12）；不要把 Gateway 改成服务端推送（DE5）；不要让 `daemon-run` 与 `worker-run` 同时连同一 `device_id`（DE11）。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| DP-2-01 任务循环 | 完成 | 新增 `apps/agent/task_loop.py`：`TaskLoop`（claim→progress→run-create→execute→run-complete→result，退避、暂停、`gate` 断线闸门、`snapshot()` 供心跳/状态页）。`machine_service.MachineAgentService` 注入 `task_loop` + `paused_provider`，连接握手成功才开闸（`_mark_connected` / `_may_claim`），断链立即关闸 |
| DP-2-02 合并常驻体 | 完成 | `agentd daemon-run`（契约 + 连接监督 + 任务循环 + 状态镜像）；`worker-run` 保留为前台调试入口并改为复用同一个 `TaskLoop`（`_build_task_loop`）；`sidecar-run` 保留为"只提供契约"形态；内核打包入口 `sidecar_entry.py` 默认走 `daemon-run`（`--contract-only` 可退回） |
| DP-2-03 本机清单 | 完成 | 新增 `apps/agent/agent_inventory.py`：`AgentInventory`（TTL 300s 缓存、`refresh()` 强制重探、只上报 `AVAILABLE` 的版本）、`default_probe()` 探测 codex + claude-code；`sidecar_api.default_agents_probe` 改为委托它；`/agents/rescan` 走 `agents_rescan`（强制真探测）；心跳携带 `adapter_versions` |
| DP-2-04 托盘与状态页 | 完成 | 壳 `src/main.js`：托盘提示增加"项目 / 队列 / 完成数 / 正在执行"，菜单增加"正在执行…"只读项与"查看日志"；`renderer/status.js` 增加 `reconnecting/unpaired/stopped/emergency_stopped` 状态文案与任务/队列行 |
| DP-2-05 后端 B1/B2 | 完成 | `store.py`：新增 `device_runtime_state` 表 + `_record_device_runtime_state()`（心跳 6 个字段落库，`ON CONFLICT` upsert）+ 能力/版本刷新（`devices.capabilities`、`devices.agent_version` 跟随心跳）；PG 侧同语义（`postgres_repository._write_device_runtime_state` + `migrations/015_device_runtime_state.sql`，RLS 同款策略） |
| DP-2-06 后端 B3 | 完成 | `GET /api/agent/me`（`main.py`）：设备 Token（Bearer，与 Gateway 同一凭据）换自身配置——身份、最近运行态、被授权项目清单、`runtime_policy`（心跳间隔/执行体/允许的 sandbox/并发/租约，可用 `MAP_AGENT_*` 环境变量覆盖）。人类会话与项目能力 Token 都会被拒（401/403） |
| DP-2-07 后端 B4 + 页面 | 完成 | `GET /api/devices` 返回 `runtime`（`contracts.DeviceRuntimeState`，列表端填充、单设备读取保持 `None`）；`/devices` 每台设备多一行 `执行体：… · 队列 N · 会话 … · 上报于 …`（`apps/web/app/devices/page.tsx` + `lib/api.ts` 的 `DeviceRuntimeState`） |
| **DP-2-08 授权深链（追加）** | 完成 | 契约追加 `POST /grant`（见 `docs/SIDECAR_CONTRACT.md` §4.5）；内核 `daemon_run` 的 `grant_handler` 运行期替换任务循环（`MachineAgentService.install_task_loop`，无需重启内核）；壳处理 `map://grant`；设备页授权弹窗新增「点这里把授权交给本机桌面端」（原命令行路径保留） |

**验收中修掉的真缺陷（全部由真机运行暴露，已登记为 DE14–DE17）**：

1. **事件循环饿死**：`claim→执行→上报` 全程同步完成时，`await` 不真正让出控制权，同一条事件循环上的心跳与控制任务被饿死。
   修法：每轮结束显式 `await asyncio.sleep(0)`（`task_loop.TaskLoop.run_forever`）。测试表现为"某个用例挂住"。
2. **重连序列错位（DE16）**：换了 `connection_id` 却复用本地自增序列 → 平台 `gateway_sequence_gap` → 要求重放 → 重放又对不上，死循环。
   修法：新增 `LocalAgentState.renumber_pending_events()`，每个新连接会话开始时把未确认事件重编号为从 1 开始、已确认的直接删除；`MachineAgentService._rotate_connection_identity()` 负责"换 id + 重编号"两件事一起做。
3. **紧急停止退出进程（DE14）**：原实现让机器服务 `run_forever` 返回，内核随即退出——壳当崩溃重启 3 次后放弃，托盘「解除紧急停止」永远点不到。
   修法：常驻体在紧急停止期间保持契约服务存活，只停领取 + 断连；`clear` 后自动重连并恢复领取。
4. **授权能力不足的表现**：授权串缺 `task.result`/`run.complete` 时，任务在本地跑完了但平台永远停在 `RUNNING`（只在本地日志里看到 403）。
   修法：`missing_task_loop_capabilities()` 在启动时校验必要能力并打印/记日志缺哪几项（不阻断，只提示）。
5. **Codex 瞬断被当成失败（DE17）**：`Reconnecting... / stream disconnected before completion` 是 Codex 自己的重试，退出码 0 且已给出最终回复，原判定把它算失败。
   修法：`parse_codex_jsonl` 把错误事件分成 `error_events`（致命）与 `transient_events`（瞬断），新增 `summarize_codex_result()` 作为唯一成功判定入口（可单测）；瞬断降级为摘要里的 `warnings`。

**未配对也能启动（DP-1 启动顺序的必然要求）**：壳会在配对**之前**拉起内核，因此 `daemon-run` 先起契约服务，
再等"设备 Token 在凭据管理器里"（`_await_device_credentials`）；未配对时 `connection.state = unpaired`，用户点「接入这台电脑」后自动开始连接，不需要重启内核。

## 3. 与计划的偏差

偏差共四处，均按 §6.5 追加为决策（DE14–DE17），并新增工作项 `DP-2-08`：

1. **新增 `DP-2-08`（授权深链）**：计划里授权只经 `connect-agent.ps1`/命令行交付，但 §2.1 的目标是"下载桌面端就能干活"——
   没有运行期授权通道，用户仍要手工跑 PowerShell 命令。故补齐 `POST /grant` + `map://grant` + 设备页入口（DE15）。
   影响面：契约追加一个端点（`v1` 允许追加），壳与设备页各加一处；不影响任何既有字段语义。
2. **紧急停止语义修正（DE14）**：计划 §5 DP-2-04 只写"紧急停止生效时任务循环停止领取，且平台侧能看到掉线"，
   未规定内核是否退出；实测发现"退出内核"会让托盘无法恢复，故明确为"停机但不退出"。
3. **重连身份/序列（DE16）**：计划未涉及；属于 DP-1"断链重连"验收在真实平台连接下必然踩到的坑（DP-1 当时只验证了壳侧状态）。
4. **Codex 成功判定（DE17）**：UX-5/DP-1 的判定规则是"退出码 0 且无错误事件"，实机遇到瞬断重试即判失败；改为分级。

其余工作项与计划一致（逐条比对 §5 工作项表：DP-2-01…DP-2-07 的范围、文件与验收口径均未变）。

## 4. 测试与验证

```text
# Agent 回归（新增 39 项：task_loop 11 + agent_daemon 12 + sidecar 新增 10 + local_state 2 + codex_outcome 4）
cd apps/agent
PYTHONPATH=<repo>;<repo>/apps/agent python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 247 tests, OK (skipped=9)          # DP-1 退出时为 208

# API 回归（新增 14 项 B1/B2/B3/B4 契约测试）
cd apps/api
PYTHONPATH=<repo>;<repo>/apps/api python -X utf8 -m unittest discover -s . -p "test_*.py"
→ Ran 305 tests, OK (skipped=14)         # DP-1 退出时为 291

# 前端构建
cd apps/web && NEXT_PUBLIC_API_URL=http://127.0.0.1:8010 npx next build
→ 19 页；/devices 6.42 kB

# Demo 1.0 主链路（不变式 I1）
pwsh -File scripts/demo-1.0.ps1 -AutoApprove
→ 17 项全过
```

**实机验收（2026-09-16，Windows，平台 127.0.0.1:8010，Python 3.12）**

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| ① 不改配置自动领取并执行 | **通过** | `[daemon] 领取任务 …（3–10s）` → `[daemon] 任务 … 结果：成功 · codex exit=0 … reply: DP2 验收通过。`；平台侧任务 `WAITING_REVIEW`、Run `SUCCEEDED` |
| ② 重扫立即生效（不重启内核） | **通过** | 实机：TTL=600s 时连续两次 `/status` 的 `checked_at` 相同（走缓存），`POST /agents/rescan` 返回全新探测时间（真探测）：`codex-cli AVAILABLE 0.154.0`、`claude-code NOT_INSTALLED` |
| ② `/devices` 显示执行体与队列 | **通过** | 接口：`runtime.adapter_versions={"codex-cli":"0.154.0"}`、`local_queue_length`、`user_session_state="logged_in"`；浏览器实测文本：`执行体：codex-cli 0.154.0 · 队列 1 · 会话 logged_in · 上报于 09/16 10:32` |
| ③ 暂停/恢复 | **通过** | 暂停期间任务保持 `READY`（8s 采样），`/tasks/resume` 后立即被领取（`claimed_after_resume=true`） |
| ④ 紧急停止 | **通过** | 停止后新建任务保持 `READY`、`/api/agents` 中该 Agent `offline`；内核存活、`/clear-emergency-stop` 后自动重连并领取 |
| B3 自视图 | **通过** | `GET /api/agent/me`（设备 Token）返回 1 个授权项目、13 项能力、`runtime_policy`；响应体不含 `device_token` |
| 心跳落库/能力刷新（B1/B2） | **通过** | 心跳后 `devices.capabilities` 变为授权时的 13 项、`devices.agent_version` 跟随上报 |

复现脚本（不写入仓库凭据，跑完请删除产物）：

```powershell
python -X utf8 scripts/dp2_acceptance_setup.py    # 配对 + 建项目 + 授权 + 建 codex 任务 → dp2_acceptance.json
python -u -X utf8 apps/agent/agentd.py daemon-run --url http://127.0.0.1:8010 --grant <blob> --workspace <repo>
python -X utf8 scripts/dp2_acceptance_grant.py    # 追加一轮（默认能力集）
python -X utf8 scripts/dp2_acceptance_verify.py   # 暂停/恢复、紧急停止、B3 → dp2_verify.json
```

> 注意：`dp2_acceptance.json` 里含**项目 Token**（授权串），验证完必须删除，不要提交。

## 5. 尚未完成与边界

用户视角"现在还不能做什么"：

1. **只认一个项目**：常驻体按 `worker.json` 里的那一个项目领任务。给同一台设备授权第二个项目时，需要再交付一次授权串（会覆盖为最新项目），
   多项目同时服务留待后续（`/api/agent/me` 已经能返回多项目清单，缺的是"多循环 + 每项目 Token"的调度）。
2. **单并发**：一次只跑一个任务（DE12），队列里第二个任务要等前一个结束。
3. **没有本地诊断页**：任务失败时只能看 `/logs`（纯文本），没有 DP-3 的可视化与诊断包导出。
4. **托盘不能切换"仅登录后运行"**、**没有开机自启开关**、**没有自动更新**（都属于 DP-4）。
5. **卸载不会清理凭据管理器里的 `MathAgentPlatform/*` 条目**（DP-4-04 的破坏性动作，本阶段未做）。
6. **Claude Code 探测到但不可执行**：`claude-code` 会显示 `UNSUPPORTED`（语义适配器未实现），因此不进 `adapter_versions`。
7. **未锁屏检测**：`user_session_state` 只区分 `logged_in` / `unknown`，不谎报 `locked`/`logged_out`。

9. **同一台机器可以同时存在两个常驻体**（实测踩到）：开发态 `daemon-run` 与已安装桌面端的内核各连一条 WebSocket，各自领取任务——平台按 `connection_id` 区分连接、按租约保证同一个任务不会被执行两次，但会消耗两份执行体调用（两份 Codex 额度）。外壳的单实例锁只防「同一个壳开两次」，防不了「壳 + 手跑的内核」。测试与排障时请只留一个常驻体。

## 6. 下一步

- 下一阶段：**DP-3 本地可观测与诊断**
- 入口条件是否满足：**是**（DP-2 退出条件全部满足：全自动闭环成立、B1/B2/B3/B4 有契约测试、新增测试计入回归总数）
- 建议的下一批工作项：`DP-3-01`（本地状态页：连接/当前任务/队列/最近 50 条日志/CLI 清单）、`DP-3-02`（一键导出脱敏诊断包）、`DP-3-03`（失败可视化：`error_events`/`unparsed` 上界面）
- 交接给 DP-3 的两个现成抓手：`TaskLoop.snapshot()` 已有 `last_result`/`last_claim_error`/`failure` 计数；`sidecar.logs` 已是可读文本缓冲（`GET /logs?tail=`）

## 7. 复现命令

```powershell
# 平台 + 前端
powershell -File scripts/start-api.ps1                     # 或 uvicorn app.main:app --port 8010
cd apps/web; $env:NEXT_PUBLIC_API_URL="http://127.0.0.1:8010"; npx next build; npx next start -p 3000

# 配对（网页 /devices → 生成配对 → 接入这台电脑），或走脚本
python -X utf8 scripts/dp2_acceptance_setup.py

# 内核常驻体（未配对也能启动：先起契约，配对成功后自动连接）
python -u -X utf8 apps/agent/agentd.py daemon-run --url http://127.0.0.1:8010 --workspace <repo>

# 只提供契约（排障）
python -u -X utf8 apps/agent/agentd.py sidecar-run

# 前台调试一轮
python -u -X utf8 apps/agent/agentd.py worker-run --grant <blob> --once

# 回归
cd apps/agent; python -X utf8 -m unittest discover -s . -p "test_*.py"
cd apps/api;   python -X utf8 -m unittest discover -s . -p "test_*.py"
pwsh -File scripts/demo-1.0.ps1 -AutoApprove
```

## 8. 壳/内核版本对照

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| 桌面端壳 | 0.1.0 | `apps/desktop/package.json`；本阶段改了 `src/main.js`、`renderer/status.js`，**未重新打包安装包**（DP-4 再出签名包） |
| Python 内核 | 0.1.0（sidecar `SIDECAR_VERSION`） | 心跳 `agent_version` 与之一致 |
| sidecar 契约 | v1（追加） | 新增 `POST /grant`、`platform.json`、`connection.state` 新取值、`identity.project_id`、`queue.local_queue_length`；无既有字段语义变化 |
| 协议 | `schema_version=1.0` | 心跳新增 `adapter_versions` 的实际填充与 `running_run_ids` 的真实值（字段早已存在，本阶段才开始用） |