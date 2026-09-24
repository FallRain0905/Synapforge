# P3 成员管理收尾 · 能力目录 · 工时趋势 · 组织收口 交接

> 交接状态：`PASS_WITH_ASSUMPTIONS`
>
> 日期：2026-09-17
>
> 上一份交接：`docs/handoffs/TEAM_1_ATTRIBUTION_MEMBERS_TEAMS_HANDOFF.md`
>
> 已发布：`https://synapforge.top`（`server_verify.sh` 22/22）

---

## 1. 范围

用户要求"继续 P3"，即 TEAM-1 交接里列的五条收尾项。本轮落地四条 + 一条的范围说明：

| 项 | 状态 |
| --- | --- |
| 移出成员可撤销 + 成员变更审计 | 完成 |
| 项目改归团队（后端 + 界面） | 完成 |
| 能力目录（谁能跑什么 + 没人能跑的任务） | 完成 |
| 工时趋势（近 N 天吞吐） | 完成 |
| 多租户隔离 | **未实施**，出了设计方案并做了两处读泄漏收口（见 §5） |

## 2. 成员变更审计与恢复

- `add_project_member` 现在写事件：首次加入 → `project.member_added`、已在项目内 → `project.member_role_changed`（带 `previous_role`）
- `project.member_removed` 的 payload **带上被移除时的角色**，界面据此提供"按原角色恢复"
- `/team` 新增「成员变更审计」面板：列出项目最近的加入/改角色/移出（谁、什么时候、改了什么角色、
  移除时撤销了多少授权与释放了多少派单），被移出且当前不在项目内的成员给「按原角色恢复」按钮
  （姓名解析：事件 payload → 成员工作量表 → 退回 id）
- 恢复走既有 `POST /api/projects/{pid}/members/{member_id}?role=`

## 3. 项目改归团队

- `PATCH /api/projects/{project_id}`（需 `project.admin`）：只改 `projects.team_id`，**不会自动把团队成员塞进项目**
  （界面与文档都写明这一点，避免误解）；写 `project.team_changed` 事件；跨组织团队被拒（`team_not_in_project_organization`）
- `/team` 的项目面板头部新增「归属团队」选择器（管理员可见）

## 4. 能力目录与工时趋势

- `GET /api/team/capabilities`：逐个执行体列出**能力集合**（Agent 自报 tools ∪ 设备声明 ∪ 心跳上报）、
  设备数与在线数、归属成员；并列出**"没人能跑"的任务**——声明了 `required_capabilities`、当前没有任何执行体满足
  （带 `missing_capabilities`，界面直接提示"要么给机器声明这些能力，要么改任务要求"）
- `GET /api/team/throughput?days=14`：按天（成功/失败）与按成员的执行完成数，数据来自 `runs`
  （`019` 之后有 `member_id`，所以能按人统计）
- `/team` 新增两个面板：能力目录（含未满足告警条）、吞吐图（纯 CSS/SVG 柱状，无新依赖；红段为失败）

## 5. 多租户：只做收口 + 出方案

`docs/MULTI_TENANT_DESIGN.md`（状态 PLAN）：给出现状、三个方案（A 部署隔离 / B 切 PostgreSQL + 强制 RLS /
C 应用层显式过滤）、各自成本与验收要求，并明确**当前推荐 A**（组织已可配置；多团队已是真正的协作单元）。

本轮顺手修掉两处"多租户半成品里最容易漏的读泄漏"：

- `GET /api/agents` 此前**全局可见**（任何登录成员都能看到所有 Agent，含本地工作区路径）→ 按会话成员的组织过滤
- `DELETE /api/agents/{agent_id}` 此前**零授权** → 改为"本人（其所有者）或管理员"
- 过程中发现并删除了一处**重复路由**（我新加的带鉴权版本与旧的无鉴权版本同路径同方法，
  FastAPI 保留先注册的那个 → 旧版本仍生效）；现在启动时无 duplicate operation id 警告

## 6. 顺带修掉的一个真 bug（影响时间线）

前端 `listProjectEvents()` 打的是 `GET /api/projects/{pid}/events?limit=N`，而该端点用的是
`list_events`（`sequence ASC`）——**取最早的 N 条**。项目事件一多，时间线和刚做的成员审计就都看不到最近发生的事
（这与当初看板"永远看不到新事件"是同一类 bug，当时只修了看板）。

- 后端：`/events` 新增 `latest=true` → 走 `store.list_latest_events`
- 前端：新参数 `listProjectEvents(projectId, limit, latest)`；**时间线改为取最近 300 条**、成员审计取最近 60 条
- 实测：移出成员后审计立刻出现「移出 队友小王（复核人）· 撤销 0 项授权 · 释放 1 个派单 · 按原角色恢复」；
  点击恢复后库里 `role=reviewer`、并写入 `project.member_added`（原角色）

## 7. 验证

- 新增 `apps/api/test_p3.py` **7 项**：成员变更三类事件与角色、按原角色恢复、项目改归团队与跨组织拒绝、
  能力目录与未满足任务、吞吐按天与按成员（含窗口外不计）、Agent 列表按组织收口
- 全量：API **394 项通过**（14 skipped）、前端 **24 页**构建、SSR 不变量（18 路由 × 18 入口 × 7 分组按钮）通过
- 浏览器实机：`/team` 四个新面板齐备（审计/能力目录 10 行/吞吐图/项目归队选择器）；
  移出 → 审计出现 → 按原角色恢复 → 库里角色回到 reviewer
- 线上：`server_verify.sh` 22/22

## 8. 尚未完成与边界

1. **多租户隔离未实施**（设计见 `docs/MULTI_TENANT_DESIGN.md`；跨组织唯一性约束、成员多组织、
   kb/settings/drive 三组表补迁移是前置项）。
2. 成员变更审计只覆盖**显式**的加入/改角色/移出；批量路径（注册自动入项目、入队即入项目、建项目自动带成员）
   目前不写成员事件，因此审计面板看不到这些"系统带入"的记录。
3. 吞吐统计基于 `runs`：纯人工推进（不走执行体）的进度不计入；窗口最大 90 天，无聚合归档。
4. 能力目录的 `required_capabilities` 仍是自由字符串，没有统一词表/校验，靠声明对齐。
5. 恢复成员会重新加入但**不会自动恢复其设备/Agent 的项目授权**（需要重新授权或让 Agent 重新连接）——
   这是刻意的（授权是独立决策），但界面上没有提示，属已知粗糙点。