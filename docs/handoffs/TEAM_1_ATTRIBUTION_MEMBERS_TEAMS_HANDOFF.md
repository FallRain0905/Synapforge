# TEAM-1 执行归属 · 成员管理 · 多团队 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-17
>
> 上一份交接：`docs/handoffs/DISPATCH_1_MY_TASKS_HANDOFF.md`
>
> 已发布：`https://synapforge.top`（`server_verify.sh` 22/22）

---

## 1. 起因与范围

用户列了五条（都是我在 DISPATCH-1 交接里记的 P2），要求一起做：

1. 执行归属落库（runs 缺 device_id/member_id；事件 actor_kind 多为 system）
2. 项目级成员管理界面（加人/移出/改角色；新建项目不带老成员）
3. 成员目录 / 工作量视图（谁在忙什么）
4. 多组织 / 多团队（表与端点是半成品，注册与建项目落进硬编码单一组织）
5. `required_capabilities` / `deadline` 只是存储字段，claim 不消费

范围上有一处**明确收窄**并说明理由：第 4 条实现的是**多团队**，不是"多租户多组织"——
运行时是 SQLite（RLS 只在 PostgreSQL 仓储路径生效），多租户隔离需要先把 RLS 落到运行时，
属另一个量级的改动。本轮把组织做成可配置（`PLATFORM_ORG_ID` / `PLATFORM_ORG_NAME`），
组织内团队成为真正的协作单元。

## 2. 执行归属落库（迁移 `019_run_attribution.sql`）

设计文档要求"任务结果、进程事件和人工操作都必须可以追溯到设备、Agent、成员和 Run"，
此前 runs 只有 agent_id、事件 actor_kind 多为 system。

- **runs 增列** `device_id` / `member_id`（SQLite 用 `_ensure_columns` 补列）
- **归属推导 `execution_attribution(agent_id, device_id)`**：
  - `device_id` 以执行体自报为准，但**必须与该 Agent 对得上**，对不上就丢弃（防冒名机器）
  - 没带设备信息时（老版本执行体 / Gateway 命令）退回"该 Agent 最近一次活跃连接所属设备"
  - `member_id` **一律服务端推导**（设备归属优先、其次 Agent 归属）——不接受客户端自报的成员身份
- **客户端只需多带一个字段**：内核在 Run 登记时带上自己的 `device_id`（`task_loop` 已有该字段）；Gateway 路径不改协议（走连接兜底）
- **事件主体修正**：`task.claimed` / `task.progress` / `task.result_submitted` / `run.created|completed|failed|blocked` 现在写 `actor_kind="agent"`，run.created 的 payload 里还带 `device_id`/`member_id`；历史事件按同样规则**回填**（`actor LIKE 'agent-%'` 且事件类型在白名单内，才从 system 改成 agent）
- **可见面**：运行详情新增「执行者 · 设备 · 归属」chip；时间线因 actor_kind 修正后能正确显示为「Agent」

## 3. 项目成员管理

- `DELETE /api/projects/{pid}/members/{member_id}`（需 `project.admin`）：移出成员时**连带**
  - 撤销其名下设备在该项目的 `device_project_grants`（置 revoked_at）、删除 `agent_project_grants`
  - 释放派给他的未完成任务（回到"谁先轮到谁跑"）
  - 写 `project.member_removed` 事件（带撤销授权数、释放任务数、涉及设备）
  - **不允许移出最后一个 owner/project_lead**（否则项目失去管理员）
- `PATCH /api/projects/{pid}/members/{member_id}`：改项目内角色（白名单校验）
- **新建项目自动带上组织内在职成员**：创建者为 project_lead，其余为 contributor（用户明确指出的缺口）
- **权限修正**：中间件此前对含 `/members` 的**任何**请求都要求 `project.admin` —— 那会让 contributor 用不了派单选择器。
  现在 GET 走 `project.view`、写操作才要 `project.admin`（实测：contributor 读成员目录 200、改角色 403）。

## 4. 成员目录 / 工作量视图 + 团队界面（新页 `/team`）

- 端点 `GET /api/team/workload`：逐成员聚合「派单未完成 / 执行中 / 累计完成 / Agent 数 / 设备数与在线数 / 参与项目数 / 所属团队」
- 端点 `GET /api/teams/{id}/members`、`POST/DELETE /api/teams/{id}/members/{member_id}`（需管理员）：
  **加入团队 = 获得团队身份 + 自动加入该团队的所有项目**；移出团队 = 连带移出这些项目（复用 `remove_project_member` 的授权撤销）
- 新页 `/team`（导航「团队与空间」分组，18 个导航入口）：成员工作量表 + 当前项目成员管理（角色下拉、移出）+ 团队管理（建队、加人、移出）+ 口径提醒
- 新建项目弹窗新增「归属团队」选择（项目归队后，后续入队成员自动获得该项目）

## 5. 领取时消费能力与截止时间

- `_agent_capability_set(agent_id)`：Agent 自报 tools ∪ 最近连接设备声明的 capabilities ∪ 心跳上报 capabilities
- **能力不满足的任务不会被领取**（轮询跳过；点名领取 `task_capabilities_not_satisfied`），避免"领下来必然失败、白耗一次执行"
- **已过截止时间的任务不再自动领取**（`task_deadline_passed`），需要人工改期或取消
- **排序改为"有截止时间的优先（最早在前）"**，再按优先级与更新时间
- 补上用得上的入口：建任务表单可设截止时间、任务行显示「⚠ 已过期」、任务详情可改期（空串 = 清除）
- 个人任务中心对已过期的派单标 `deadline_passed`（界面会给提示）

## 6. 验证

- **后端新增 19 项测试**（`test_capabilities.py` 7 项 + `test_team.py` 12 项）：能力不足拦截与声明后放行、设备声明能力同样算数、过期不领取、截止时间排序、改期与清除、时间线标记；
  新建项目带成员、移出成员撤销授权并释放派单、最后一个负责人不可移除、入队即入项目、退队连带退项目、跨组织拒绝、工作量计数、**Run 归属（含伪造设备被丢弃）**、Agent 事件 actor_kind、组织可由环境变量配置。
- 全量：API **387 项通过**（14 skipped）、Agent **281 项通过**、前端 **24 页**构建、SSR 不变量（18 路由 × 18 入口 × 7 分组按钮）通过。
- 浏览器实机（本地，required 模式）：`/team` 工作量表（派单/执行中/机器数/项目数）→ 项目成员管理（2 行、角色下拉、移出按钮）→ 建团队「2026 国赛 A 队」→ 加人成功（提示"同时加入该团队 0 个项目"，非零场景由单测覆盖）→ 任务页建带截止时间的任务 → 行内显示「⚠ 已过期」。
- 发布：`https://synapforge.top`，`server_verify.sh` 22/22。

## 7. 尚未完成与边界

1. **多租户隔离未做**（本轮只做多团队）：一个部署 = 一个组织；组织内可多团队。跨组织隔离要求 RLS 落到运行时（当前运行时是 SQLite），属独立议题。
2. **项目归队只能建项目时选**：还没有"把已有项目改归到某团队"的界面/端点（可加 `PATCH /api/projects/{id}`）。
3. **工作量是计数视图**，没有按时间段的趋势、没有工时统计。
4. **能力词表没有统一枚举**：`required_capabilities` 是自由字符串数组，靠 Agent/设备声明对齐（谁声明得准谁被选中）；没有"能力目录"页面。
5. Gateway 路径的 Run 归属依赖"最近活跃连接"兜底（协议未改），若设备从未建立过连接（纯 HTTP 领取）则 `device_id` 为空，只有 `member_id`。
6. 移出成员/退队**不可撤销**（要重新加入需管理员操作），也没有导出"谁被移出过"的审计视图（事件流里有 `project.member_removed`）。

## 8. 复现命令

```bash
cd apps/api
PYTHONPATH="<repo>;<repo>/apps/api" python -X utf8 -m unittest test_team test_capabilities -v

# 迁移清单已含 019；服务器发布后自检（带会话断言）
MAP_VERIFY_EMAIL=you@example.com MAP_VERIFY_PASSWORD=<口令> bash /root/deploy/server_verify.sh
```