# DISPATCH-1 派单模式与个人任务中心 交接

> 交接状态：`PASS`
>
> 日期：2026-09-17
>
> 上一份交接：`docs/handoffs/AUTH_1_ACCOUNTS_HANDOFF.md`
>
> 已发布：`https://synapforge.top`（`server_verify.sh` 22/22）

---

## 1. 起因

用户提问："团队系统以及团队的 agent 协作——不同用户的 agent 在一个团队内协作的场景，我们是否考虑到了？"

三路代码审计的结论：**设计在、运行语义不在**。当时的实际缺口有八条，其中最贵的是"任务没有指派语义"（同项目两台不同成员的机器就是抢同一队列）与"执行归属不可追溯"。用户拍板了三条口径，本阶段落实第一条，并把审计中"现在就有问题"的两条一起修掉。

## 2. 拍板的三个口径（已固化在代码与测试里）

| 口径 | 取值 | 落地方式 |
| --- | --- | --- |
| 任务指派 | **派单模式**：指派给成员，并**在个人任务中心显示** | `tasks.assignee_member_id` + 领取时按成员过滤 + `/my-tasks` 页 |
| 复核 | **允许**批准自己 Agent 的产出（不做职责分离约束） | 不加产出者校验；用测试锁定该行为 |
| 可见性 | **所有内容可见**（项目成员都能看到草稿/待审） | 保持现状；用测试锁定该行为 |

## 3. 顺手修掉的两个"现在就有问题"（P0）

### 3.1 `worker-run` 在强制鉴权下 401（我引入的回归）

接入向导打印的 `worker-run` 走 HTTP 领取 `/api/agents/{id}/tasks/claim`，而该路径不在中间件免鉴权清单里（只豁免了 `/api/agent/*` 与两个 register 端点）——切 `PLATFORM_AUTH_MODE=required` 后必被拦：

```
修前：POST /api/agents/agent-x/tasks/claim（带 X-Project-Capability-Token）→ 401 authentication_required
修后：同一请求 → 422（平台的参数校验），即已到达 handler
```

修法：中间件加一条与 nginx 同构的规则——**带 `X-Project-Capability-Token` 的请求交给 handler 按能力校验**（每个相关 handler 本来就调用 `_require_agent_capability`，放行不等于开洞）。常驻内核走 Gateway WebSocket 不受影响，所以上线冒烟（在切换 required 之前做的）没暴露它。

### 3.2 组织/团队/成员端点匿名可达

`main.py` 的中间件白名单精确豁免了 `/api/organizations|/api/teams|/api/members`，而 handler 不校验身份：

```
修前：GET /api/organizations → 200（匿名读到组织）；POST /api/teams|/api/members → 422（进到 handler = 无鉴权）
修后：三者匿名 → 401
```

危害不止垃圾数据：`POST /api/members` 能建出**占位成员**，而注册按邮箱唯一判定——别人可以用它抢注队友邮箱、阻断真人注册。现在：读取需会话，写入需管理员；`GET /api/members/{id}/projects` 也改成"本人或管理员"。

## 4. 派单模式（P1-1）

**数据与语义**

- 迁移 `018_task_dispatch.sql`：`tasks.assignee_member_id`（NULL = 未指派）。SQLite 侧用既有 `_ensure_columns` 补列。
- **领取过滤**：`claim_next_task` 只挑"指派给我"或"未指派"的；`claim_task` 对被派给别人的任务直接拒绝（`task_assigned_to_another_member`）。
- **成员解析**：谁在领？→ `agents.owner_member_id`（HTTP 领取与 Gateway 都走这一个来源）。
- **校验**：派单目标必须是项目成员（`assignee_not_project_member`）；取消指派 = 空串。
- 原有的 `assignee` 文本列保留为"执行者标注"（领取时写 agent_id），不再承担指派语义。

**新增端点**

| 端点 | 说明 |
| --- | --- |
| `GET /api/projects/{id}/members` | 项目成员目录（id/角色/显示名/邮箱/状态），派单选择器用 |
| `GET /api/tasks/mine` | 个人任务中心：`assigned`（派给我的未结束）/ `running`（我的 Agent 在执行）/ `recent`（我的 Agent 最近完成） |
| `PATCH /api/tasks/{id}` | 请求体可带 `assignee_member_id`（出现才生效；空串=取消指派） |
| `POST /api/projects/{id}/tasks` | 建任务时可直接派单 |

**前端**

- 新页 `/my-tasks`（个人任务中心，三组视图 + 使用说明），导航里「任务与流程」因此从单页变成分组（17 个导航入口）。
- 任务页：建任务表单加「指派给（派单）」选择器；任务详情弹窗加派单改派；任务行显示「→ 某人」。
- 修掉一个**真 bug**：`/my-tasks` 与 `/account` 的加载 effect 早于 Provider 恢复会话（React 子组件 effect 先跑），**硬刷新时请求不带令牌 → 401 → 页面显示成空**。现在两页都等 `ready && authenticated` 再加载。

## 5. 验收

- 后端新增 `apps/api/test_dispatch.py` **10 项**：派单只被目标成员领取（且租约仍防重复）、未指派保持先到先得、指派目标必须是项目成员、PATCH 的"不动/改派/取消"三态、租约过期回收后派单关系仍在、成员目录、个人任务中心三组聚合、**自批允许**、**全部可见**（后两项为口径锁定测试）。
- 全量：API **368 项通过**（14 skipped）、前端 **23 页**构建通过、SSR 不变量（17 路由 × 17 入口 × 6 分组按钮）通过。
- 浏览器实机（本地，`PLATFORM_AUTH_MODE=required`）：管理员建任务时选择「队友小王」→ 任务行显示「→ 队友小王」→ 队友登录（硬刷新）在个人任务中心看到「指派给我 1 · 只有我的 Agent 能领取」。
- 线上：`server_verify.sh` **22/22**（新增账号面与接入面断言，含"能力令牌请求到达 handler"这一条回归探针）。

## 6. 尚未完成（审计里的 P2，未拍板/未做）

1. **执行归属落库**：`runs` 仍只有 `agent_id`，没有 `device_id`/`member_id`；事件 `actor_kind` 仍多为 `system`。设计文档要求的"可追溯到设备、Agent、成员、Run"还没达成——这是多成员协作里"谁家的机器干的活"的直接依据。
2. **项目级成员管理界面**（把某人加入/移出某项目、改项目内角色）：仍是后端兜底（`POST /api/projects/{id}/members`，需 project.admin），没有界面；新建项目不会自动带上老成员。
3. **成员目录 / 工作量视图**：只有项目内的成员选择器，没有"谁在忙什么"的视图。
4. **多组织/多团队**：表和端点仍是半成品（现在写入需管理员，但注册/建项目全部落进硬编码的单一组织）。
5. **按能力/负载推荐执行体**：`required_capabilities`、`deadline` 仍只是存储字段，claim 不消费；派单解决了"指定人"，"指定后由哪台机器跑"仍是该成员名下设备先到先得。
6. **并发**：内核仍是单并发（DE12），多成员并行的吞吐依赖"多台机器各跑各的"。

## 7. 复现命令

```bash
cd apps/api
PYTHONPATH="<repo>;<repo>/apps/api" python -X utf8 -m unittest test_dispatch -v

# 线上自检（带会话断言：注册首个账号后可用）
MAP_VERIFY_EMAIL=you@example.com MAP_VERIFY_PASSWORD=<口令> bash /root/deploy/server_verify.sh
```

## 8. 一个工具链教训

用 Windows 上的 Python 脚本改 `.sh`/`.conf` 时会把行尾写成 CRLF（`write_text` 默认按 `os.linesep`），上传到 Linux 的 bash 会报 `invalid option name`；而"在 bash 里写 `$'\r'` 检测"本身又会因为转义与自匹配踩更多坑。现在的做法：

- 所有部署脚本用 `newline="\n"` 写；
- `pack-source.sh` 里加了一道**用 `bytes([13])`（不写转义序列）**的字节级防呆，打包前直接拒绝含 CR 的文件。这一次 `server_verify.sh`/`server_bootstrap.sh`/`nginx-site.conf` 都曾被 CRLF 污染，已全部规范化并重新上传。