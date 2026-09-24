# 项目工作区（Project Workspace）设计

> 状态：设计已拍板（2026-09-22），待实施。分期 W-1 ～ W-4 见文末。
> 背景：平台后端能力已较完整（派单、能力匹配、事件广播、模板包），但页面多而散，
> 缺一个「主工作区域」作为人机协作的反馈主轴。本设计把项目内日常操作收拢进单一工作区。

## 一、使用场景

### 场景 1：立项（队长视角）
创建项目填写：名称、模板（`competition_pack`，已有）、简介（`description`，已有）、
**项目目标**（新增 `goal`）、**目标人数**（新增 `target_member_count`，仅作建队参考，不做强校验）。
创建者自动获得该项目 `leader` 角色 —— 这是「队长派遣」的角色基础。
立项后通过邀请链接（invitations，已有）拉成员，通过设备配对（已有）接入 Agent。

### 场景 2：项目工作台（所有成员视角）
进入项目后只有一个主工作区域 `/projects/[id]/workspace`：

- **中间：项目聊天流**（默认 tab）。人类成员发消息；Agent 的动作（领任务、阶段推进、
  产出成果物、请求复核、任务完成）由服务端自动转为**事件卡片**插入聊天流，
  卡片可跳转到运行/成果物详情。
- **右侧：成员概览栏（常驻）**。每个成员与每个 Agent 一行：在线状态（agent_connections）、
  当前在跑什么（runs 归属）、负载（workload）。下方挂**成果空间入口**。
- **成果空间**（tab）：artifacts + 交接文档（handoffs）+ 论文草稿（documents）+
  复核记录（reviews）的聚合视图，后端全部现成。

### 场景 3：任务推进三模式（项目级 `task_mode`，默认 manual）
- **manual（默认）**：队长在任务板派单；成员在「我的任务」查看；未指派任务先到先得（现有逻辑）。
- **hybrid**：派单之外，允许成员自己认领。
- **auto（opt-in）**：按模板包阶段结构自动推进——物化阶段任务 → 按能力目录匹配 Agent
  → 执行 → 复核 → 推进下一阶段。队长可随时暂停接管。

## 二、已拍板的决定

| 决定点 | 结论 |
| --- | --- |
| Agent 在聊天流的角色 | 一期只做**事件卡片**（服务端从事件派生，Agent 不发消息）；@Agent 触发任务放二期 |
| 工作区形态 | **单页四 Tab**：聊天（默认）/ 任务板 / 成果空间 / 设置概览；右侧成员栏常驻 |
| 自动化粒度 | **项目级三档开关** manual/hybrid/auto，不做按阶段粒度 |
| 旧 24 页处理 | **收进「高级工具」侧栏分组**，只隐藏不删除（与老轨 HTTP 轨道同一原则） |

## 三、数据模型（迁移 020，需同步 SQLite 建表与 test_platform_contracts.py 清单）

```
projects 加列：
  goal                text
  target_member_count integer
  task_mode           text NOT NULL DEFAULT 'manual'

新表 project_messages：
  id               uuid PRIMARY KEY
  project_id       uuid REFERENCES projects(id) ON DELETE CASCADE
  seq              integer NOT NULL            -- 项目内单调递增，分页游标
  sender_kind      text NOT NULL               -- human | agent | system
  sender_member_id text REFERENCES human_members(id)
  sender_agent_id  text REFERENCES agents(agent_id)
  content          text NOT NULL               -- 卡片时为人可读摘要
  message_type     text NOT NULL DEFAULT 'text'  -- text | card
  ref_event_id     bigint                      -- 卡片引用，不复制内容
  ref_artifact_id  uuid
  ref_task_id      uuid
  created_at       timestamptz NOT NULL
  UNIQUE (project_id, seq)

角色：project_memberships.role 枚举补 leader（创建者默认）。
```

## 四、关键架构约束

1. **Agent 不发聊天消息**：`packages/agent_protocol` 的信封与序号是 README 禁区，不能动，
   老轨 agent 也不会新增“发言”能力。聊天流里的 agent 内容一律由服务端派生：
   run 推进 → 既有事件落库 → 在 broadcast_event 的旁路把**人可读事件**写一行
   `project_messages`（sender_kind=agent、message_type=card、引用 event_id）→ 同一条 WS 广播。
2. **聊天不复用 collaboration 帧中继**：`collaboration.py` 的中继语义（纯转发不落库）是禁区；
   人类消息走 REST 落库 + `manager.broadcast`，与中继并存互不影响。
3. WS 鉴权沿用 `main.py` 现状（`?token=` 查询参数 + project.view，nginx 对该路径关访问日志）。
4. 自动推进调度挂在 run 完成事件的既有路径上，`task_mode=auto` 才触发；批量路径照旧不写
   成员事件（既有边界，不在本期扩大）。

## 五、端点（增量，全部走现有鉴权中间件）

| 端点 | 说明 |
| --- | --- |
| `POST /api/projects` | 扩展 goal / target_member_count；创建者自动 leader |
| `GET /api/projects/{id}/messages` | 分页拉取（seq 游标，倒序） |
| `POST /api/projects/{id}/messages` | 发人类消息（project.contribute） |
| `GET /api/projects/{id}/overview` | 工作区首屏聚合：成员+Agent 状态、任务摘要、最近消息、成果统计 |
| `PATCH /api/projects/{id}` | 已有端点扩展 task_mode / goal / target_member_count |
| `POST /api/projects/{id}/tasks/materialize` | 从模板包物化阶段任务（auto 启动 / 队长手动调用） |

## 六、前端信息架构

- 新页 `/projects/[id]/workspace`：四 tab + 常驻右侧成员栏；聊天 tab 长连 `/ws/projects/{id}`。
- 侧栏重组：项目列表为主入口 + 我的任务 + 团队 + 账号/设置 + **「高级工具」分组**
  （graph / kb / ask / delivery / timeline / runs 等深页收编于此，页面本身不改不删）。
- 移动端沿用 UX-9 断点；聊天输入区在 320px 可用。

## 七、分期交付

| 期 | 内容 | 验收要点 |
| --- | --- | --- |
| W-1 工作区骨架 | 迁移 020 + 消息表 + REST/WS 聊天（人类 + 事件卡片）+ 工作区页（聊天 tab、成员概览、成果入口） | 契约测试（消息分页/权限/卡片派生）；`required` 模式下 WS 与 Agent 接入回归 |
| W-2 任务与成果 | 任务板 tab（队长派单 UI、hybrid 认领）+ 成果空间聚合页 | 派单/认领全链路在单页内闭环 |
| W-3 模板自动化 | task_mode 三档 + 物化 + 自动推进 + 队长暂停接管 | auto 模式端到端跑通一个阶段；暂停后回到 manual 不丢状态 |
| W-4 导航收敛 | 侧栏重组，深页降级「高级工具」 | SSR 不变量更新；旧页路由全部仍可达 |

## 八、边界（明确不做）

- @Agent 指令（聊天即指挥）：二期，本期不实现。
- 不改 agent_protocol 信封、不删任何旧页、不做按阶段自动化粒度。
- 成员概览的“在线状态”以 agent_connections 为准，人类成员不做在线心跳。
- target_member_count 只做展示，不做加入拦截。
