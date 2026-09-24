# W-3 全自动调度器（task_mode=auto） 交接

> 交接状态：`PASS`
>
> 日期：2026-09-22
>
> 上游：`docs/handoffs/W2_TASK_BOARD_DELIVERABLES_HANDOFF.md`
>
> 设计依据：`docs/PROJECT_WORKSPACE_DESIGN.md` 场景 3（任务推进三模式）
>
> 已发布：`https://synapforge.top`（`server_verify.sh` 22/22；发布后就地验证见 §5）

---

## 1. 粒度决定（本轮唯一需要拍板的一项）

设计阶段留的问题："auto 模式一次推进一个阶段，还是一路跑到需要人工复核为止？"

**采用的是前者**（我按推荐执行）：**每个 tick 每个项目只推进一件事**——派一条任务，
或提示一次"没人能跑"。理由：这是平台上唯一会"自己动起来"的东西，一次一件事能让队长在群聊里
看清每一步、随时切回「队长派单」停手；"一路跑到底"需要更复杂的护栏，而 4 人 4G 的部署上
没人盯着时的自动化是最贵的那种错误。队长随时切回 manual，已派出去的活不受影响。

## 2. 调度器做什么（只做两件低风险、可逆的事）

`Store.auto_dispatch_tick(project_id, limit=1)`（`apps/api/app/store.py`）：

1. **能力匹配自动派单**：挑出"依赖已满足、还没有负责人、未过期"的 `READY/NEEDS_REVISION` 任务，
   在**项目成员**里找有**在线且对项目有授权**的 Agent、且其能力集合满足任务 `required_capabilities`
   的人，按"当前未结束任务最少"挑一个派过去。
   不这么做的话，未指派任务会被任意一台机器"先到先得"抢走，分工就没有意义了。
2. **派不出去就提示一次**：没有在线执行体能满足要求时写 `task.auto_unmatched` 事件
   （幂等键 `auto-unmatched:{task_id}`，只提示一次，不会每 10 秒刷屏），卡片文案直接说缺什么能力。

**明确不做**：自动批准门禁（批准必须由人，沿用既有口径）、自动改任务状态、自动复核。

## 3. 驱动方式（三个入口）

| 入口 | 说明 |
| --- | --- |
| 维护循环（每 10 秒一拍） | `_maintenance_pass()` 里对每个项目跑 `limit=1` 的 tick，事件由 `_maintenance_tick` 广播；单个项目失败不影响整轮 |
| 切到 auto 的那一刻 | `PATCH /api/projects/{id}` 里 `task_mode=auto` 时立刻跑一次 tick + 推群聊——队长马上能看到调度器在干活，不用等下一拍 |
| 物化模板包之后 | `POST /api/projects/{id}/competition-pack/apply` 里，若项目是 auto 就顺手推进一件（任务刚建出来就该按能力派出去） |

调度器的 actor 是 `platform-auto`（`actor_kind=system`）；派单策略里有显式例外：
调度器不是成员，不受"成员不能派给别人"的约束——但它的目标选择仍受能力/在线/授权的硬约束。

## 4. 群聊卡片

- `全自动：把任务「用 Python 跑灵敏度分析」派给了 队长（能力匹配：python）`
- `全自动：没有在线执行体能跑「需要 GPU 集群的训练」（需要 cuda），已停下等你处理`
- 切模式本身也有卡片：`任务推进模式已切换为「模板全自动」` / `「队长派单」`

前端「概览」Tab 的 auto 文案已同步更新（原文写的是"调度在 W-3 上线"），并新增一行说明：
调度器只做派单与提示两件事、**不会**自动批准门禁。

## 5. 验收

| 项 | 结果 |
| --- | --- |
| 契约测试 | 新增 `apps/api/test_workspace_auto.py` **15 项**：能力匹配派给对的人、每拍只派一条、无人可跑只提示一次（幂等）、离线执行体不用、无能力要求的任务直接派、manual/hybrid 不动手、**切回 manual 立即停手**、负载均衡挑最闲的成员、过期任务跳过、依赖未满足跳过、已指派不重派、observer 名下的执行体不作为目标、无项目授权的执行体不作为目标、维护扫描确实会推进、卡片文案正确 |
| 后端全量 | **447 项**，仅 2 项 LaTeX 环境失败（与 W-3 无关） |
| 前端 | 构建 25 页通过；SSR 不变量 19×19×7 通过 |
| 浏览器实机 | 新项目（1 条 `python` 任务 + 1 条 `cuda` 任务 + 1 个在线且声明 `python` 的执行体）→ 概览切「模板全自动」→ **立刻**出现"全自动：把任务「用 Python 跑灵敏度分析」派给了 队长（能力匹配：python）"；等下一拍（10 秒）出现"没有在线执行体能跑「需要 GPU 集群的训练」（需要 cuda）"；切回「队长派单」后再建任务，**跨过一拍 13 秒仍是未指派**且无新卡片 |
| 线上 | `server_verify.sh` 22/22；发布后就地验证：部署里 `auto_dispatch_tick` 可调用、线上两个项目都是 `manual` → tick 动作"无"、**事件 46→46 / 消息 29→29（零副作用）**、三个服务 active |

## 6. 边界（明确没做）

- **不做自动批准/自动复核**：门禁与复核仍然要人（`/review`）；机器复核（pack 的审计脚本）没有接进调度器。
- **不自动物化下一阶段**：pack 的 17 条任务是一次性物化的，阶段推进靠任务的 `dependency_task_ids`
  自然阻塞领取（依赖没满足的任务不会被派、也领不到），调度器只负责"把已经能做的活分给对的人"。
- **不做优先级抢占**：已经在跑的活不会被调度器改派。
- **没有可视化"调度器日志"页**：动作都在群聊里（`task.auto_assigned` / `task.auto_unmatched`），
  时间线（`/timeline`）里也能查到；没有单独的调度面板。
- **能力词表是自由字符串**（既有边界延续）：调度器按 `required_capabilities` 与执行体声明的能力集合
  精确匹配，"python" 与 "Python" 会被当成两种能力——写能力时要一致（`/team` 的能力目录可以核对）。

## 7. 下一步（只剩 W-4）

- **W-4 导航收敛**：把 graph / kb / ask / delivery / timeline / runs / devices 等深页收进侧栏
  「高级工具」分组（**只隐藏不删除**），让侧栏回到"工作区 + 我的任务 + 团队 + 账号 + 高级工具"。

## 8. 复现命令

```bash
# 契约测试
PYTHONPATH="apps/api;." python -m unittest discover -s apps/api -p "test_workspace_auto.py" -t apps/api
# 线上零副作用检查（服务器上）
scp scripts/deploy/_w3_verify.py root@<IP>:/tmp/ && ssh root@<IP> \
  "cd /opt/math-agent-platform && PYTHONPATH=/opt/math-agent-platform/apps/api:/opt/math-agent-platform \
   /opt/math-agent-platform/venv/bin/python /tmp/w3_verify.py"
```