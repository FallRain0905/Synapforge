# UX-3 Agent 状态真实性 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-16
>
> 对应阶段：UX-3（工作项定义见 `docs/DEMO_1_0_IMPLEMENTATION_PLAN.md` §5 UX-3）
>
> 上一份交接：`docs/handoffs/UX_2_TASK_AND_PACK_CLOSURE_HANDOFF.md`

---

## 1. 本阶段目标

从计划书 §5 UX-3 抄写：

> **目标**：让界面上关于 Agent 的一切都与事实一致——在线就是在线，掉线就是掉线，掉线后任务可被回收。
>
> **入口条件**：UX-1 退出（可并行于 UX-2）。
>
> **退出条件**：全仓不存在"只写 online、从不写 offline"的路径；租约回收有对应的自动化测试。

## 2. 实际完成内容

| 工作项 | 状态 | 实际做法 / 关键改动位置 |
| --- | --- | --- |
| UX-3-01 心跳超时扫描 | 完成 | `apps/api/app/store.py` 新增模块常量 `AGENT_HEARTBEAT_TIMEOUT_SECONDS = 90`（固定值，不做环境变量，决策 D3）、`_mark_agent_offline()`、`expire_stale_agents(timeout_seconds=90, *, reference_time=None)`。Agent 不挂项目，离线事件按 `agent_project_grants` 分发到各项目；无授权的 Agent 只改状态不写事件 |
| UX-3-02 补全 offline 写入点 | 完成 | `close_agent_connection()` 断连后若该 Agent 已无 `CONNECTED` 连接则置 offline（`_agent_offline_if_disconnected`，reason=`gateway_disconnected`）；`revoke_device()` 撤销设备后对受影响 Agent 做同样处理。原实现只改连接状态，Agent 会永远显示在线 |
| UX-3-03 租约回收 | 完成 | 新增 `recycle_expired_leases(*, reference_time=None)`，按决策 D4 分级：`CLAIMED`（未开工）→ `READY` 并写 `task.lease.expired`；`RUNNING`（已开工）→ 走 `create_review(verdict=NEEDS_REVISION, reviewer_kind=SYSTEM)` 交回 `NEEDS_REVISION`、登记 `lease_expired_during_execution` 风险（`major`）、写 `task.lease.recycled`；其他状态只释放租约。复核用 `idempotency_key=f"lease-recycle:{lease_id}"` 保证重复扫描不产生重复复核 |
| UX-3-04 接通 broadcast_event | 完成 | `apps/api/app/main.py` 新增 `MAINTENANCE_INTERVAL_SECONDS = 10`、`_maintenance_pass()`（纯数据库工作，跑在线程里）、`_maintenance_tick()`（在事件循环线程里广播）、`_maintenance_loop()`，并由 `lifespan` 启停。`broadcast_event`（此前全仓无调用方）现在有真实调用方 |
| UX-3-05 界面联动 | 完成 | 前端无需改动即可生效：`apps/web/lib/workspace.tsx` 的 `/ws/projects/{id}` 订阅本来就是"收到任意消息即 refresh"，此前因为服务端从不推送而形同虚设 |
| UX-3-06 在线判定可解释 | 完成 | `apps/web/app/runs/page.tsx` 增加 `HEARTBEAT_TIMEOUT_SECONDS = 90` 常量、`secondsSince()` 工具与「最近心跳 X（N 秒前[，已超过判定阈值]）」行（`agent-last-seen-<agent_id>`），面板副标题改为"服务身份、最近心跳与在线判定依据"，并在面板顶部写明判定口径 |

**计划外但必要的修复（2 处，都在验收中发现）**：

1. **广播跑错线程**：初版把 `broadcast_event` 放在 `_maintenance_pass` 里，而该函数通过 `asyncio.to_thread` 在工作线程执行——`broadcast_event` 依赖 `asyncio.get_running_loop()`，线程里必然抛 `RuntimeError` 并被 `except RuntimeError: pass` 静默吞掉，界面因此不会自动更新。现拆成 `_maintenance_pass`（仅数据库）与 `_maintenance_tick`（循环线程广播），并留了回归用例 `test_maintenance_pass_does_not_broadcast_from_worker_thread`。
2. **SQLite 连接被跨线程并发使用**：维护线程与 FastAPI 线程池请求共用同一个 `sqlite3` 连接，触发 `sqlite3.InterfaceError: bad parameter or other API misuse`——表现为请求莫名 404/500、`/ws/projects/{id}` 建连后立刻断开（顶栏一直显示"等待连接"）。修复：在 `store.py` 增加 `_SerializedConnection` / `_SerializedCursor` 包装（可重入锁串行化 `execute`/`executemany`/`executescript`/取数/`commit`/`rollback`/`close`/`with`），`Store.__init__` 统一使用包装连接。留了回归用例 `test_connection_is_serialized_across_threads`。

## 3. 与计划的偏差

**有偏差，共 2 处：**

1. **"登记风险"通过机器复核实现，而不是直接插风险行。** 计划书 D4 写的是"回 `NEEDS_REVISION` 并登记风险"，但 `risks.review_id` 是 `NOT NULL` 且外键指向 `reviews`，无法裸插风险。因此改为走既有复核机制：`create_review(verdict=NEEDS_REVISION, reviewer_kind=SYSTEM)` 会同时产生 Review、Gate 与 Risks，语义更正确（"平台机器意见"，不是人工批准，符合 D9）。副作用是回收会在 `/review` 留下一条待处理门禁。
2. **计划书写的是"store.py（新增扫描）、main.py（启动期注册）"，实际多改了 `_SerializedConnection` 与 `close_agent_connection`/`revoke_device`。** 前者是为修复验收中暴露的并发缺陷（见 §2 修复 2），后者是 UX-3-02 明确要求的写入点。两者都在本阶段范围内，未触碰禁区清单中的任何文件。

## 4. 测试与验证

**基线命令（计划书 §7）**

```text
后端：python -X utf8 -m unittest discover -s . -p "test_*.py"（apps/api）
      → Ran 282 tests, OK (skipped=13)，33.4s（基线上限 266 → 新增 16 项）
前端：cd apps/web && npm run build
      → 16 条路由全部静态预渲染；/runs 1.48 kB → 1.82 kB
```

新增 `apps/api/test_agent_health.py`（16 项）：心跳超时置离线与事件字段、近期心跳不误判、重复扫描幂等、参考时间边界、CLAIMED→READY（含可被重新领取）、RUNNING→NEEDS_REVISION + 风险 + 系统复核、回收幂等、其他状态只释放租约、Gateway 断连置离线、还有连接时不误判、扫描广播、健康时不广播、工作线程不广播（回归）、系统 reviewer_kind、跨线程连接串行化（回归）。

**端到端验收（真实前端 + 真实后端 + 独立临时库）**

夹具同前两阶段（`scripts/acceptance_empty_api.py` 于 8010，`next dev` 于 3014）。

| 验收标准 | 操作 | 结果 |
| --- | --- | --- |
| Agent 掉线后 90–100 秒内界面自动变离线，无需手动刷新 | 注册 Agent（HTTP 轨）并授项目权限 → 打开 `/runs` 确认 online → **直接改库**把 `last_seen` 置为 5 分钟前 → 等 24 秒 → **不刷新页面**读 DOM | 界面自行更新为 `offline`，并显示「最近心跳 09/15 22:51（308 秒前，已超过判定阈值）」；指标变成「在线 Agent 3 / 共 4 个已登记」；顶栏连接状态"已同步"（WebSocket 正常） |
| 租约过期后未开工任务可被重新领取 | 在真实库插入过期租约 + `CLAIMED` 任务 → 等维护扫描 | 任务变 `READY`，事件 `task.lease.expired`，租约 `EXPIRED` |
| 租约过期后已开工任务回 `NEEDS_REVISION` 并有风险记录 | 同上，任务状态为 `RUNNING` | 任务变 `NEEDS_REVISION`；`risks` 出现 `lease_expired_during_execution`(major)；`reviews` 出现 `verdict=NEEDS_REVISION, reviewer=platform-maintenance, reviewer_kind=system`；事件 `task.lease.recycled`；`/tasks` 页面显示两个任务的新状态 |
| `broadcast_event` 有真实调用方 | 代码 + 运行时双向确认 | `grep` 命中 `main._maintenance_tick`；运行时事件确实推达浏览器（上一行的自动更新即证据） |

## 5. 尚未完成与边界

- **`idle` 状态仍无人写入**：阈值扫描只区分 online/offline，契约里的 `idle` 仍是未使用值。
- **离线事件按授权项目分发**：没有项目授权的 Agent 掉线时只改状态、不产生事件（因此不会触发任何项目的界面刷新）。真实接入的 Agent 一定有授权，所以这不是 bug，但它意味着"未授权 Agent 掉线在界面上无痕"。
- **扫描发现离线的最坏延迟约 100 秒**（90s 阈值 + ≤10s 扫描间隔）。演示时可以接受，但"秒级感知掉线"需要更短阈值或事件驱动。
- **维护扫描只在 API 进程内**：多实例部署时每个实例都会扫（写操作幂等，事件会重复广播，界面只是多刷新几次）。真正多实例需要分布式锁或独立调度，属后续工作。
- **`_SerializedConnection` 是粗粒度串行**：牺牲并发度换正确性。demo 规模无感，但高并发场景应改为连接池 + WAL。
- **未覆盖**：Gateway 心跳 `record_agent_heartbeat` 路径下的 offline 判定（真实 WebSocket 心跳超时属于服务端连接层，本阶段只做了 HTTP 心跳与断连）。
- **UX-3-05 的"任务回收"界面联动**只在 `/tasks` 导航后验证到状态正确；"停留在页面上等回收自动更新"这条与 Agent 掉线是同一条推送链路（已验证），未单独截图。

## 6. 下一步

- 下一阶段：**UX-4 接入向导与设备管理**（入口条件 UX-3 已满足）
- 建议的下一批工作项：`UX-4-01`（`agentd keygen`）、`UX-4-02`（配对向导页）、`UX-4-04`（`scripts/connect-agent.ps1`）
- 提醒：UX-4 要在 Web 上暴露一次性 `device_token`，务必遵守"明文只显示一次"的语义（服务端只存哈希，无法回显）；测试里不要把它写进日志或前端状态持久化。

## 7. 复现命令

```powershell
# 1) 基线
cd apps\api
$env:PYTHONPATH = "$(Resolve-Path '..\..');$(Resolve-Path '.')"
python -X utf8 -m unittest discover -s . -p "test_*.py"     # Ran 282 tests, OK (skipped=13)
cd ..\web
npm run build                                                # 16 条路由；/runs 1.82 kB

# 2) 端到端验收
python -X utf8 -m uvicorn scripts.acceptance_empty_api:app --host 127.0.0.1 --port 8010
cd apps\web; $env:NEXT_PUBLIC_API_URL = "http://127.0.0.1:8010"; npx next dev -p 3014

# 3) 走查要点
#    注册 Agent 并授予项目权限 → /runs 确认 online
#    把 agents.last_seen 改成 5 分钟前 → 25 秒内界面（不刷新）应变 offline
#    插入过期租约 + CLAIMED/RUNNING 任务 → 25 秒内分别变 READY / NEEDS_REVISION，并在 /review 看到机器复核与风险
```