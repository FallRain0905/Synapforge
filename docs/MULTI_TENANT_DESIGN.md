# 多租户（多组织）方案设计

> 状态：`PLAN`（本轮只落地了"组织可配置 + 非项目域资源按组织收口"，隔离本身未实施）
>
> 日期：2026-09-17
>
> 相关：`docs/handoffs/TEAM_1_ATTRIBUTION_MEMBERS_TEAMS_HANDOFF.md`、`docs/POSTGRES_REPOSITORY.md`

## 1. 现状（为什么现在还不是多租户）

| 事实 | 位置 | 影响 |
| --- | --- | --- |
| 运行时用 SQLite（`Store`），**RLS 只在 PostgreSQL 仓储路径生效** | `apps/api/app/main.py` 的 `store = Store(...)`；`postgres_repository.py` 的 `set_config('app.organization_id')` | 数据库层没有强制的组织边界 |
| 注册、建项目、恢复包都落进**同一个组织**（默认种子组织，现可由 `PLATFORM_ORG_ID`/`PLATFORM_ORG_NAME` 指定） | `_create_account`、`create_project` | 一个部署只服务一个组织 |
| 组织边界靠**项目成员关系**间接实现（`list_projects_for_member` join `project_memberships`） | `store.list_projects_for_member` | 项目域内是安全的；非项目域要看具体查询 |
| 知识库 / AI 凭据 / 个人云盘是 **SQLite 运行时建表**，迁移里根本没有对应表 | `knowledge_base.py`、`personal_drive.py` | 上 PG 时既无表也无 RLS，必须先补迁移 |
| 成员级资源已隔离：AI 凭据按 `member_id`、个人云盘按 `owner`、个人库按 `owner_member_id` | 同上 | 这部分天然不跨成员 |

本轮补的两处"读泄漏"（多租户半成品里最容易漏的）：

- `GET /api/agents` 此前**全局可见**（任何登录成员都能看到全部 Agent，含本地工作区路径）→ 改为按会话成员的组织过滤
- `DELETE /api/agents/{agent_id}` 此前**零授权** → 改为"本人（其所有者）或管理员"

## 2. 三个方案

### 方案 A：部署隔离（当前推荐）

一个团队一套部署；组织身份由 `PLATFORM_ORG_ID` / `PLATFORM_ORG_NAME` 指定（或在部署后注册首个管理员接管）。

- 成本：0（已经具备）
- 优点：隔离是物理的，没有"漏一处就串租户"的风险
- 缺点：升级要逐个部署、资源（数据库/对象存储）不能共享；成员跨组织协作需要两套账号

### 方案 B：切换运行时到 PostgreSQL + 强制 RLS（真正的多租户）

要点：

1. **把 SQLite Store 的能力搬到 PG 仓储**：当前 `PostgresRepository` 只覆盖部分对象（不含成员/会话/知识库等），要么补齐方法，要么把 `Store` 改造成可插拔的后端（SQLite 方言 → PG 方言），后者改动面更大但更彻底。
2. **补迁移**：知识库、AI 凭据、个人云盘三组表在迁移里不存在——先补 `020_kb_and_settings.sql`（含 RLS 策略）。
3. **会话 → 组织绑定**：中间件解析会话后把 `organization_id` 放进请求上下文；所有数据访问在事务里 `set_config('app.organization_id', …, true)`（`postgres_repository.transaction()` 已是这个模式）。
4. **成员可属于多个组织**：现在是 `human_members.organization_id` 单值。多租户要么加 `member_organizations` 关联表（推荐），要么一个邮箱一个组织。
5. **迁移期**：现有数据回填 `organization_id`（已是 `NOT NULL`）；跨组织唯一性约束（设备指纹、邮箱、slug）要逐个复核——它们现在多是**全局唯一**，多租户下需要改成 `(organization_id, …)` 复合唯一。
6. **验收**：跨租户访问矩阵测试（A 组织成员的每个端点 × B 组织资源），以及"忘记 set_config 时必须失败"的负向测试。

工作量：大（后端仓储改造 + 迁移 + 全部端点回归），建议作为独立阶段排期，而不是顺手做。

### 方案 C：应用层显式 org 过滤（不上 RLS）

所有 store 查询显式带 `organization_id`，中间件绑定会话组织。

- 优点：不需要 PG
- 缺点：**安全性靠"每处都记得写"**，漏一处就是跨租户泄漏；需要额外做自动化审计（为每个 store 方法强制要求 org 参数 + 全端点矩阵测试），长期维护成本高于方案 B

## 3. 建议

1. **短期用方案 A**：当前团队规模（一个建模组、若干队伍）不需要多租户；组织已可配置，多团队已是真正的协作单元。
2. **真要多租户就走方案 B**：RLS 是数据库层强制，比应用层过滤更不容易漏。前置清单见 §2 的 1–6，按独立阶段排期。
3. 无论走哪条，**先修"非项目域"的读泄漏**（本轮已修 agents；后续类似的有：跨组织的设备列表、事件流按项目兜底但项目归属要复核）。

## 4. 场景判断（什么时候需要多租户）

| 场景 | 需要多租户吗 |
| --- | --- |
| 一个建模组、多个队伍（如 2026 国赛 A/B 队） | **不需要**——用团队（已实现） |
| 同一机构多个互不可见的系/实验室共用一个部署 | 需要（方案 B） |
| 对外提供 SaaS（不同客户各自的数据） | 需要（方案 B + 计费/配额分离） |
| 队友分散在不同组织但协作同一个项目 | 需要方案 B 的"成员多组织"部分，或方案 A 下共享一套部署 |