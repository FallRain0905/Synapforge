# W-1 项目工作区（群聊 + 成员概览 + 成果入口）交接

> 交接状态：`PASS`
>
> 日期：2026-09-22
>
> 设计依据：`docs/PROJECT_WORKSPACE_DESIGN.md`（本轮开工前已拍板四条）
>
> 已发布：`https://synapforge.top`（`server_verify.sh` 22/22；域名入口 `/workspace` → 200，
> IP 入口 Basic 后门仍 401）

---

## 1. 范围

设计文档里的 **W-1 工作区骨架**：项目立项字段、项目内聊天流（人类消息 + Agent 事件卡片）、
右侧成员概览、成果空间入口、单页四 Tab 工作区。W-2（任务板派单 UI 内嵌 / 成果空间聚合面板）、
W-3（模板全自动调度）、W-4（旧页正式收进「高级工具」）**未做**，见 §7。

## 2. 后端

**迁移 `020_project_workspace.sql`**（新增迁移，SQLite 侧同步建表 + `_ensure_columns`，
`test_platform_contracts.py` 迁移清单已加）：

- `projects` 加 `goal` / `target_member_count` / `task_mode`（`manual|hybrid|auto`，默认 `manual`）
- 新表 `project_messages`：`seq`（项目内单调递增，聊天分页游标）、`sender_kind`（human/agent/system）、
  `sender_name`（写入时快照展示名）、`content`、`message_type`（text/card）、
  `ref_event_id` / `ref_artifact_id` / `ref_task_id`
- 新表 `project_chat_watermarks`：每项目"事件已检查到第几号"，**按已检查序号推进**而不是按最后一张卡片，
  否则中间夹着大量非卡事件时会漏扫

**事件 → 卡片的唯一实现点**：`Store._insert_event()` 里调 `_bridge_chat_event()`（同一事务，由调用方提交）。
所有事件路径（含直接调 `_insert_event` 的 handoff/artifact/review/gate/evidence）都被覆盖，
**Agent 侧零改动**——`packages/agent_protocol` 的信封与序号完全没碰。渲染只做"一句人话 + 引用"，
不复制业务内容；运维噪声（`agent.offline`、`artifact.content_stored`、`project.seeded/synced`）不进流。

**端点**（都走既有鉴权中间件）：

| 端点 | 权限 | 说明 |
| --- | --- | --- |
| `GET /api/projects/{id}/workspace` | `project.view` | 首屏聚合：`project` + **`viewer`**（我是谁/角色/can_chat/can_manage）+ `members` + `agents` + `tasks` + `artifacts` + 最近 50 条聊天 |
| `GET /api/projects/{id}/messages` | `project.view` | `?before=` 翻历史页 / `?after=` 增量 / 缺省最近 N 条，一律按 `seq` 升序返回 |
| `POST /api/projects/{id}/messages` | **`project.chat`**（新增：owner/project_lead/contributor/reviewer，observer 只读） | 发言后**立即**推 WS |
| `PATCH /api/projects/{id}` | `project.admin` | 扩展为 PATCH 语义：`team_id`（用 `model_fields_set` 区分"显式置空"与"不动"）、`goal`、`target_member_count`、`task_mode`；写 `project.task_mode_changed` 事件 |

**WS 推送**（`/ws/projects/{id}`）：

- 连上时的 `connected` 帧新增 `messages`（最近 50 条）与 `message_seq`（该连接已看到的序号），
  页面因此不用再单独拉一次历史；
- 新消息以 `{"type":"project.message","message":{…}}` 帧推送；
- 推送实现要点：**绝大多数路由是同步 `def`（跑在线程池里，没有运行中的事件循环）**，所以启动时在
  `lifespan` 抓住主循环，用 `run_coroutine_threadsafe` 调度；维护循环每 10 秒兜底
  `catch_up_project_messages()` + `publish_project_chat()`，兜住任何漏了即时桥接/推送的路径。

**启动回填**：`lifespan` 里跑一次全量 `catch_up_project_messages()`，老项目的历史事件一次性进聊天流
（线上实测：2 个项目、29 张卡片一次回填完成），并把推送基线对齐到当前最大 `seq`，历史不回放给 WS。

## 3. 前端

- **新页 `/workspace`**（第 25 个路由，静态预渲染）：单页四 Tab —— 聊天（默认）/ 任务 / 成果空间 / 概览，
  右侧常驻成员概览（成员 + Agent + 成果空间入口）。`?project=<id>` 直链支持（读 `window.location.search`，
  不打断静态预渲染）；新建项目成功后直接跳进工作区。
- **聊天流**：人类发言是气泡，Agent/系统动作是卡片（`card-badge` 区分主体，卡片带「查看任务/成果物」跳转）；
  Enter 发送、Shift+Enter 换行；顶部「读取更早的记录」翻历史页；新消息贴底滚动（用户上翻时不打扰）。
- **`lib/workspace.tsx`**：新增 `subscribeMessages()`，把 WS 帧里的消息分发给订阅者；
  **消息帧不再触发整盘刷新**（否则群里一活跃界面就会不停重取看板）。
- **`lib/nav.ts`**：项目分组首位新增「项目工作区」（`nav-workspace`，复用 `projects` 角标）；
  项目总览页页头加「项目工作区」入口。
- **新建项目表单**：加 `项目目标` / `目标人数` / `任务推进方式`（默认队长派单）。
- **CSS**：`.chat-*` / `.list-row` / `.kv*` / `.avatar-dot` 等 + 深色主题覆盖 + 980px/560px 断点
  （窄屏右栏落到下方、输入区纵向排列）。

## 4. 验收（全部实测）

| 项 | 结果 |
| --- | --- |
| 后端契约测试 | 新增 `apps/api/test_workspace.py` **16 项**（立项字段与队长角色、PATCH 语义、消息游标、权限、卡片派生与噪声过滤、水位线幂等、**老事件回填**、非卡事件不阻塞后续卡片、概览聚合、**viewer 身份**） |
| 后端全量 | **410 项**，仅 2 项失败且都是环境问题（本机没有 pdflatex/xelatex → `latex_engine_missing`，与 W-1 无关；服务器有 TeX 时通过） |
| 前端构建 | **25 页**全静态预渲染通过 |
| SSR 不变量 | `scripts/_w1_ssr.py`：**19 路由 × 19 导航入口 × 7 分组按钮**，当前页唯一高亮 |
| 浏览器实机（本地 `required` 模式） | 打开工作区 → 历史 27 张卡片（项目创建/任务/领取/进度/运行/复核/交接）；发言落库并回显；**Agent 侧**用项目能力令牌 claim + progress，两张卡片**经 WS 实时**出现在页面（无需刷新）；任务推进模式切换 → 群聊出现「任务推进模式已切换为…」卡片；四 Tab 与右侧成员栏逐项核对；新建项目带目标/人数/自动模式 → 直接进工作区且概览显示正确 |
| 手机视口 | 390×844：无横向溢出（scrollWidth 375 = clientWidth 375）、聊天/输入框可用、底部标签栏正常 |
| 线上 | `server_verify.sh` **22/22**；域名 `/workspace` → 200 且 SSR 里有 `nav-workspace`；IP 入口 Basic 仍 401；发布后另用**临时 300 秒会话**（用完即删）在服务器上就地打真实数据：`/workspace` 200 且 `viewer` 角色正确（project_lead/can_manage）、成员与 Agent 聚合正确、`/messages` 200、`POST /messages` 201、库内 30 条消息（29 卡片 + 1 人类）与 2 条水位线 |

## 5. 本轮修掉的三个缺陷（都是测试/实机发现的）

1. **`project_workspace` 里的变量遮蔽**（严重）：成员列表循环写成了 `member_id = item["member_id"]`，
   把函数参数覆盖成"列表里最后一个人"。结果 `viewer` 的"我是谁"是错的——队长拿不到模式开关、
   普通成员可能看到管理权。已改名 `entry_member_id`，并补了 `test_viewer_identity_is_the_requesting_member`。
2. **`run.*` 卡片显示「任务」而不是任务名**：run 事件 payload 只有 `run_id`，渲染器没顺着
   `runs.task_id` 找回标题。已加 `_run_task_title()`，并把线上/本地已生成的 60 条卡片按同一渲染器**就地重写**
   （只改内容，不动 `seq` 与顺序）。
3. **窄屏顶栏面包屑逐字竖排**：`.breadcrumbs strong`（页面标题）在 760px 以下没有 nowrap/省略号，
   "项目工作区"会一个字一行。已在两个移动断点里补 `nowrap + ellipsis + max-width`。

另外两处不是缺陷但记一笔：旧版 `PATCH /api/projects/{id}` 需要**平台管理员**，
W-1 起改为**项目管理员**（队长即可改自己项目的设置），与"队长派单"的定位一致；
`GET /api/projects/{id}/workspace` 的权限由中间件按路径判定（`GET → project.view`）。

## 6. 边界（明确没做）

- **Agent 不会"发言"**：聊天流里的 Agent 内容 100% 是服务端从事件派生的卡片，
  `agent_protocol` 零改动；@Agent 下指令是二期。
- **@指令 / 未读标记 / 消息编辑删除 / 表情回应**：都没做。
- **批量路径不写成员事件**（既有边界延续）：注册自动入项目、入队即入项目、建项目自动带成员
  这些"系统带入"在聊天流里看不到。
- **卡片内容不随对象改名回溯更新**（除本次的一次性重写）；卡片只是引用 + 快照文本。
- **`task_mode=auto` 只落了字段与界面**：真正的模板自动推进调度在 W-3，界面上已写明。
- 成员概览的"在线"以 `agent_connections` 为准；人类成员没有在线心跳。

## 7. 下一步（W-2 / W-3 / W-4）

- **W-2**：任务板 Tab 内嵌队长派单 UI 与 hybrid 认领；成果空间把 artifacts + handoffs + documents + reviews
  聚合成完整面板（现在只有成果物摘要 + 三个深页入口）。
- **W-3**：`task_mode=auto` 的调度（物化 → 能力匹配 → 执行 → 复核 → 下一阶段）+ 队长暂停接管。
- **W-4**：侧栏把 graph / kb / ask / delivery / timeline / runs 等深页收进「高级工具」分组（只隐藏不删除）。

## 8. 复现命令

```bash
# 后端
PYTHONPATH="apps/api;." python -m unittest discover -s apps/api -p "test_workspace.py" -t apps/api
# 前端
cd apps/web && npm run build && npm run start     # 改完前端必须重建并重启
# SSR 不变量
python scripts/_w1_ssr.py
# 线上发布后（服务器上，可选：带会话断言）
MAP_VERIFY_EMAIL=<账号> MAP_VERIFY_PASSWORD=<口令> bash /root/deploy/server_verify.sh --expect-public-url https://synapforge.top
# 发布后就地验证工作区端点（临时会话，用完即删）
scp scripts/deploy/_w1_verify.py root@<IP>:/tmp/ && \
  ssh root@<IP> "cd /opt/math-agent-platform && PYTHONPATH=/opt/math-agent-platform/apps/api:/opt/math-agent-platform \
  /opt/math-agent-platform/venv/bin/python /tmp/w1_verify.py"
```