# W-2 任务板派单 · 成果空间聚合 交接

> 交接状态：`PASS`
>
> 日期：2026-09-22
>
> 上游：`docs/handoffs/W1_PROJECT_WORKSPACE_HANDOFF.md`（W-1 工作区骨架）
>
> 设计依据：`docs/PROJECT_WORKSPACE_DESIGN.md` 场景 3（任务推进三模式）与场景 2（成果空间）
>
> 已发布：`https://synapforge.top`（`server_verify.sh` 22/22；发布后就地验证见 §5）

---

## 1. 范围

W-2：把 W-1 里"占位"的两个 Tab 补成能干活的——**任务板**（队长派单 / 成员认领 / 批量派单 / 筛选）
与**成果空间**（成果物 + 文档三层版本 + 交接单 + 复核门禁，一个面板看到底）。

## 2. 派单口径（本轮的核心决定）

用户口径："**不应该默认进行全自动化设计，任务需要队长派遣**，同时成员自己可以看到自己的任务。"

落到代码里就是 `Store._dispatch_policy()`：

| 角色 | manual（默认） | hybrid / auto |
| --- | --- | --- |
| 队长（owner / project_lead） | 派给谁、改派、收回，都行 | 同左 |
| 普通成员 | **只能看**，改负责人被拒 `task_self_claim_disabled_in_manual_mode` | 可认领**还没有负责人**的任务（把自己设为负责人），也可释放自己认领的 |
| 任何人 | 不能把任务派给别人（`task_dispatch_requires_lead`） | 同左 |
| 任何人 | 不能动别人负责的任务（`task_assigned_to_another_member`） | 同左 |
| 观察者（observer） | 连 `project.write` 都没有 → 中间件 403 | 同左 |

派单目标必须是**项目成员**（`assignee_not_project_member`，409）——否则会出现"派给一个看不到这个项目的人"。

**批量派单**：`POST /api/projects/{id}/tasks/assign`（`{task_ids, assignee_member_id}`，空串 = 全部收回）。
逐条走同一套策略，**不做"全成功或全失败"**：18 条里有一条被别人认领了，其余照常派出，
失败项按 `task_id + reason` 返回（否则批量操作会退化成人工排除体力活）。

**派单进群聊**：每次负责人变化都写 `task.dispatched` 事件（带 `action`：`dispatch` / `self_claim` / `release`），
经 W-1 的事件→卡片桥渲染成人话："队长把任务「问题一建模」派给了 小王" / "小王 认领了任务「…」" /
"任务「…」回到未指派（谁先轮到谁跑）"。批量派单只在一次批量后推一次 WS（不逐条推）。

## 3. 成果空间聚合

`GET /api/projects/{id}/deliverables`（`project.view`）——读时聚合，**不落新表**：

| 面板 | 内容 |
| --- | --- |
| 成果物 | 全部版本 + 按状态计数（`by_status`）+ 最近 25 条（名称/类型/版本/产出者/时间/状态） |
| 文档 | 文本类成果物（`DOCUMENT_ARTIFACT_TYPES` 白名单）的**三层版本**：草稿（有 draft 或未提交）/ 已提交 / 已批准，带 `has_draft` 标记 |
| 交接单 | 按状态计数 + 最近 12 条（目标、发出者、首条关键结论） |
| 复核与门禁 | 门禁列表（状态 + 阻断意见数 + 批准人）、最近 8 次复核（verdict / 复核人 / 摘要）、未关闭风险数 |

`reviewer` 字段在返回前过一遍 `_display_name()`：老数据里存的是成员 id，界面上直接显示成名字。

## 4. 前端（`/workspace` 的两个 Tab）

- **任务板**：状态/负责人筛选、行内派单下拉（队长）、`认领`/`释放` 按钮（成员，仅 hybrid/auto）、
  **勾选 + 批量派单条**（含"收回指派"）、刷新按钮；任务数据用 `dashboard.tasks`（全量），
  排序与后端队列一致（有截止时间的优先）。
- **成果空间**：四个面板（成果物 / 文档三层 / 交接单 / 复核与门禁）+ 四个深页入口；切到该 Tab 时懒加载。
- 窄屏（≤560px）：行内派单选择器换行独占一行，批量条自动换行。

## 5. 验收

| 项 | 结果 |
| --- | --- |
| 契约测试 | 新增 `apps/api/test_workspace_dispatch.py` **22 项**：7 条口径（队长派/改派/收回、成员 manual 拒、hybrid 认领与释放、抢别人任务拒、释放别人任务拒、auto 也允许认领、目标必须在项目内）+ 4 条批量（成功/部分失败/任务不存在/目标非法）+ 6 条成果聚合（五段结构、文档层随状态变化、空项目、风险计数）+ **5 条 HTTP 层**（TestClient，`required` 模式） |
| 后端全量 | **432 项**，仅 2 项 LaTeX 环境失败（与 W-2 无关） |
| 前端 | 构建 25 页通过；SSR 不变量 19 路由 × 19 入口 × 7 分组按钮通过 |
| 浏览器实机 | 队长视角：任务板 18 行 + 批量条可用 → 勾 2 条派给「队友小王」→ **未指派 18→16** 且群聊实时出现两张"队长把任务「…」派给了 队友小王"卡片；成果空间四面板显示真实数据（9 份成果物 / 文档草稿 8 / 交接单 1 张 / 3 次复核 + 2 个门禁未通过 + 2 个未关闭风险） |
| HTTP 实测（本地） | 成员 token：manual 认领 → **403**；切 hybrid 后认领 → **200**；队长收回 → 200；批量派 3 条 → `updated=3, failures=[]`；批量收回 3 条 → 200 |
| 线上 | `server_verify.sh` 22/22；发布后就地验证（临时 300 秒会话，用完即删）：`/deliverables` → 200 且五段结构完整、`/tasks/assign` 非项目成员 → **409 `assignee_not_project_member`**、任务不存在 → `200 + failures=[task_not_found]`，**验证过程 0 新增事件/卡片** |

## 6. 本轮修掉的两个缺陷

1. **`PATCH /api/tasks/{id}` 少声明 `request: Request`** → 我一加 `_request_member_id(request)` 就 500
   （`NameError: name 'request' is not defined`）。**store 层测试全绿也发现不了**——这正是本轮补 5 条
   HTTP 层断言的直接原因（TestClient + `required` 模式，覆盖队长派单、成员认领、批量端点、成果端点鉴权、观察者被拒）。
2. **批量失败原因带引号**：`str(KeyError("task_not_found"))` 是 `"'task_not_found'"`，错误码不可比对。
   改为取 `error.args[0]`。

顺带把 `PATCH /api/tasks/{id}` 的审计主体从写死的 `member-001` 改成真实会话成员——此前"谁改的任务"在时间线上是错的。

## 7. 边界（明确没做）

- **auto（模板全自动）的调度器仍未实现**（W-3）：`task_mode=auto` 现在只影响"成员能否认领"，不会自动派单/推进。
- **任务看板无拖拽**；批量只支持"派给同一人 / 全部收回"，不支持按阶段批量筛选后一次派（可用筛选 + 勾选代替）。
- **成果空间不做下载/预览**：成果物内容仍在 `/artifacts` 与文档页（这里只给聚合视图与跳转）。
- **门禁不能在这里批准**：批准仍走 `/review`（门禁批准是"人工复核"的唯一入口，沿用既有口径）。
- 交接单面板只展示，不接受/退回仍在 `/handoffs`。

## 8. 下一步（W-3 / W-4）

- **W-3**：`task_mode=auto` 的模板自动推进——按 pack 阶段物化任务 → 能力匹配派给能跑的 Agent →
  执行 → 复核 → 下一阶段；队长可随时暂停切回人工。
- **W-4**：侧栏重组，把 graph / kb / ask / delivery / timeline / runs 等深页收进「高级工具」分组（只隐藏不删除）。

## 9. 复现命令

```bash
# 契约测试（含 HTTP 层）
PYTHONPATH="apps/api;." python -m unittest discover -s apps/api -p "test_workspace_dispatch.py" -t apps/api
# 线上发布后（服务器上跑，只读 + 零副作用验证）
scp scripts/deploy/_w2_verify.py root@<IP>:/tmp/ && ssh root@<IP> \
  "cd /opt/math-agent-platform && PYTHONPATH=/opt/math-agent-platform/apps/api:/opt/math-agent-platform \
   /opt/math-agent-platform/venv/bin/python /tmp/w2_verify.py"
```