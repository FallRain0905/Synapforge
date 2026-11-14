# 本地优先协作协议 · 实施计划

**文档状态**：Stage 1 计划 v1
**创建**：2026-10-20
**上位决策**：放弃托管网页平台形态，把 synapforge 的核心资产（**Agent 协作协议**）改为本地 **CLI + MCP + 技能**三层交付，先实现**同一台电脑上的多个 Agent 互通**。

> 这份文档是**决策留档**。上下文变长、会话更换时，以本文档为准，不要重新推导。

---

## 0. 决策摘要（只读这一节也够用）

| # | 决策 | 理由 |
|---|---|---|
| **D1** | **不做托管网页平台。** UI 改为**本地只读查看器**，按需启动，不是服务。 | 维护 web 是持续成本；用户是 Agent 不是人；原 web 24,735 行 / 25 页 / **零测试** / 还要服务器端 Next 构建 |
| **D2** | **协议层复用，不重写。** 丢 `main.py` / `postgres_repository.py` / `apps/web` / `apps/desktop`。 | 实测：协议核心 ≈13,700 行可原样用，且带约 1,000 项测试 |
| **D3** | **`Store`（SQLite）是唯一事实源。** 状态目录 `~/.synapforge/`。 | `Store(path, object_store=None)` 本来就是纯 stdlib 本地库 |
| **D4** | **不要常驻 broker / 守护进程。** CLI + 一个 SQLite 文件就是全部。 | 参与者各自带自己的循环；省掉端口、systemd、启动顺序、崩溃恢复 |
| **D5** | **命令只允许一套实现。** 若本地查看器日后加动作，必须调用与 CLI **完全相同的函数**。 | 不变量只在命令层强制一次，避免长出第二套权限/审批/状态机 |
| **D6** | **本地查看器物理只读**：SQLite 以 `mode=ro` 打开。 | 机制而非纪律——它**不可能**成为第二个事实源 |
| **D7** | **先 CLI，后 UI。** Stage 1 验收**不含** UI。 | 否则会建出第二个事实源。判据见 §6 护栏四 |
| **D8** | **能力令牌层退役**（设备令牌 / 项目授权 / 到期 / 撤销）；但 **`skill_match` 能力匹配保留**。 | 令牌管的是远程不可信多方；本地 OS 就是信任边界。能力匹配跑在 `store.py` 领取路径里，不依赖令牌 |
| **D9** | **`ask` 与 `handoff` 是两个不同对象，不合并。** | ask＝我缺信息才能继续；handoff＝我这块做完了交给你。各有自己的状态机 |
| **D10** | **Stage 1 执行体 = 内置最小直连 LLM 执行器**；外部产品执行体（opencode / codex / claude / zcode）属 Stage 2。 | 本机**没装任何**外部执行体（已盘点）；内置执行器 ~80 行，去掉全部外部依赖，让"协议是否成立"不被环境问题污染 |
| **D11** | **代码放仓库内 `apps/local/`**，不新建仓库。 | 复用即搬家；复制一份会立刻产生两份真源 |
| **D12** | **凭据只放仓库外** `~/.synapforge/config.json`。公开仓库零凭据。 | 仓库托管在 GitHub 且是**公开**的 |
| **D13** | **本地库开 WAL** + `busy_timeout`。 | 现状 `journal_mode=delete`，多进程并发写需要 WAL |
| **D14** | **区分两种 Agent 形态**（守护进程型 / 交互型）；Stage 1 **只做守护进程型**。 | 见 §9 风险 R1；这决定"互通"的真实含义 |

---

## 1. 为什么这么做

**问题不在后端做多了，在于后端只开了一个门，而且那个门是网页。**

仓库现状：`apps/api/app/main.py` 一个文件约 **300 条路由**，而其中**没有一条能被用户自己的 Agent 直接调用**。整个仓库 MCP 相关代码为**零**。结果是：手上有一个 300 接口的协作引擎，唯一的门是一个网页。

代价不是操作麻烦，是**上下文切换**：每次交接都要离开 Agent、开浏览器、找项目、找任务、点几下。而这个产品的使用者是 Agent——**只有人需要网页**。

旁证：项目已经为此造过 Electron 桌面壳把工作台包成应用（0.2.0），打的是症状（让网站像 app）而不是病因（网站不该在回路里）。且前端在项目记忆里留下了大量反复调试（网格空白、`min-width:0` 撑破、样式类名没接上）——**前端在这个产品里是成本中心，不是价值中心**。

**结论**：后端不是做多了，是**把"协作协议"实现成了"一个网站的功能"，而不是"一个 Agent 能调的能力"**。协议是对的，载体错了。

---

## 2. 已验证事实（探针证据，不要重新推导）

探针脚本：`数学建模\_probe_local_protocol.py`（仓库外，可重跑可删）。
运行：Python `C:\Users\19855\AppData\Local\Programs\Python\Python312\python.exe`，`PYTHONPATH=math-agent-platform;math-agent-platform/apps/api`。

**在临时 SQLite 文件上、零服务器、零端口、零守护进程，跑通了：**

| 验证项 | 结果 |
|---|---|
| 导入 `coordination.py` + `acceptance.py` 时拉起的 web/DB 模块 | **`NONE`**（fastapi/starlette/uvicorn/psycopg/multipart 都不在） |
| `Store(path)` 建表 | **43 张表**，含全部协议表 |
| 协议表 | `stage_reports` / `information_requests` / `feasibility_concerns` / `orchestration_decisions` / `project_workflow_runs` / `workflow_versions` |
| 信息请求闭环 | `OPEN`(`WAITING:information_requested`) → `ACKNOWLEDGED` → `ANSWERED` → `CONSUMER_ACKNOWLEDGED`(`RUNNING`) |
| 幂等重放 | 返回**原记录**，表内仍 1 行 |
| 诚实停止 | concern → `NEEDS_DECISION` + **任务真进 `BLOCKED` 且带 `blocked_reason`** → 决策 → `DECIDED` |
| 非法迁移 | **fail-closed**：`CONSUMER_ACKNOWLEDGED → OPEN` 被 `IllegalTransition` 拒绝 |
| 事件日志 | 11 条持久化事件落库（证据面本地成立） |

**合法迁移表规模**：`information_request` 17 条、`task` 17 条、`run` 7 条、`handoff` 6 条。

**可行性异议枚举（实测原值）**：
`missing_input` / `invalid_assumption` / `incompatible_contract` / `impossible_goal` / `insufficient_capability` / `budget_or_time` / `acceptance_conflict` / `unsafe_to_continue`

**编排建议枚举（实测原值）**：
`request_information` / `revise_task` / `change_agent` / `replan` / `escalate_human` / `stop` / `continue_with_assumption`

**ACK 合法取值（实测）**：`received` / `rejected` / `unable_to_execute`（`acknowledged` **不是**合法值）。

**两个反直觉但关键的事实**：
1. **`Store` 本来就是纯 stdlib 本地 SQLite 库**——`Store(path, object_store=None)`，无服务器依赖。
2. **Postgres 才是不完整的后端**——`postgres_repository.py` 只实现 291 个方法里的 **103** 个，且**完全没实现协作协议与工作流引擎那几张表**。协议层从来就跑在本地 SQLite 上；**要丢掉的服务器从来没承载过最宝贵的那部分**。

---

## 3. 保什么 / 丢什么（对账数字）

| 分类 | 行数 | 明细 |
|---|---|---|
| **可原样复用** | **≈13,700** | coordination 458 / coordination_store 839 / workflow_engine 321 / workflow_service 923 / acceptance 488 / progress_ledger 415 / event_catalog 187 / skill_match 217 / workflow_gate 133 / project_team 207 / store 6,913 / contracts 2,601 |
| **可无改动搬迁的纯测试** | **121 项** | test_coordination_protocol 32 / test_acceptance 34 / test_progress_ledger 28 / test_event_catalog 15 / test_skill_match 12 |
| **可丢弃** | **≈33,600** | main.py 5,537（302 条路由的薄包装）+ postgres_repository 2,050 + **apps/web 24,735（零测试）** + desktop 1,313 |

一句话：**停止维护约 3.4 万行（其中 2.5 万行是没测试的前端），保住约 1.37 万行且它已带近千项测试。**

**Agent 侧**：`apps/agent` 非测试 18,180 行 + 测试 10,216 行 / 489 项——执行体那套基本活着，变的是它**指向哪里**。

---

## 4. 架构

```
   Claude Code        ZCode         opencode(守护)      codex
        │               │                │               │
        └─── CLI / MCP ─┴───────┬────────┴───────────────┘
                               │  读写
                    ~/.synapforge/state.db        ← 唯一事实源（SQLite, WAL）
                    ~/.synapforge/config.json     ← 凭据（仓库外）
                    ~/.synapforge/artifacts/      ← 成果物
                    ~/.synapforge/workspace/      ← 共享工作区
                               │
                               │  只读（file:...?mode=ro）
                    synapforge ui → http://127.0.0.1:PORT   ← 只是"看"
```

### 4.1 Agent 之间不"互相通信"，而是共享状态

**这是最容易搞错的一点，必须写死：** CLI 不是两个 Agent 之间的通道，**CLI 是每个 Agent 对同一份共享状态的操作手柄**。Agent A 不"发消息给" B——A 往 SQLite 写一条信息请求；**B 下次运行时读到它**。

四个后果：
1. **A 不需要 B 正在运行**——两个 Agent 可以永远不同时活着，协作照样推进。
2. **任一方崩溃都不丢东西**——请求/回答/交接在库里，也在事件日志里。
3. **审计白送**——本来就走事件日志，不是聊天记录。
4. **2 个 Agent 和 20 个 Agent 同一套代码**——没有注册中心、没有寻址、没有连接管理。

### 4.2 两个协作对象

| 对象 | 含义 | 已有状态机 |
|---|---|---|
| **`ask` / `answer`**（信息请求） | "我缺一个信息才能继续"（会阻塞或降级） | `information_request` 17 条合法迁移 |
| **`handoff`**（交接） | "我这块做完了，上下文/证据/未决问题归你"（带接收回执） | `handoff` 6 条合法迁移 |

**不合并。** 两个对象都已实现。

---

## 5. 动词表（CLI 表面，约 15 个）

唯一真源在 `apps/local/`；MCP 与本地查看器都建在它上面（D5）。

| 组 | 动词 | 说明 |
|---|---|---|
| **看** | `status` | 我在哪、有哪些 Agent、有什么活 |
| | `inbox` | 待我处理的（任务 / 信息请求 / 交接 / 待复核） |
| | `watch` | 跟着事件流看（终端里盯） |
| | `ui` | 起本地**只读**查看器（Stage 3，不在 Stage 1） |
| **任务** | `task create` | 建任务 |
| | `claim` / `heartbeat` | 领活（拿租约）/ 续租 |
| | `report` | 提交 StageReport（含 `can_continue_safely`） |
| | `finish` | 完成 + 成果物 |
| **协作** | `ask` / `answer` / `consume` | 信息请求全生命周期 |
| | `handoff create/accept/reject` | 交接全生命周期 |
| **诚实停止** | `concern` | 提交可行性异议（八类之一 + 非空 `basis`） |

命令行接口调用的是**已有协议函数**（`coordination_store.*`），不重写逻辑。

---

## 6. 四条护栏

本地 web 是**最容易长回平台**的东西。今天三个只读视图 → 一个月后加"批准"按钮 → 然后"编辑任务" → "新建项目" → 要账号 → 又有平台了。

**护栏一：架构上只读，不是纪律上只读。**
SQLite 以 `file:...?mode=ro` 打开——查看器的连接**物理上无法写**。比"约定不要写"强一个量级。

**护栏二：命令只允许一套实现。**
若日后查看器要加动作（合理，例如看 diff 时想批准），那个按钮必须调用**与 CLI 完全相同的函数**。不变量只在命令层强制一次。

**护栏三：按需启动，不是服务。**
`synapforge ui` 起一个绑 `127.0.0.1` 的进程 + 开浏览器，关掉就没了。不进 systemd、不开机自启、不常驻。前端用原生 HTML/JS，**不要框架、不要构建步骤、不要 node_modules**。

**护栏四（可测量的判据）：关掉本地 web，协作能不能照常跑？**
能 → 它是查看器。不能 → 它已经是平台了。
**这条判据从第一天就要能通过**——所以查看器必须等功能跑通再造。

---

## 7. 前置修复（Stage 1 开工第一件事）

不修这两处，闭环是**假的**。

**F1：本地库开 WAL。**
现状（实测）：`journal_mode = delete`，`busy_timeout = 5000`（Python sqlite3 默认）。
多进程并发写需要 `PRAGMA journal_mode=WAL`。**另：多进程并发写尚未证明**——探针那次第二连接写失败是**探针自身**漏了 `events.sequence` NOT NULL 列，不是锁冲突，需要单独补测。

**F2：`blocking=required` 的阻塞没有落到任务状态。**（已修复，见 §12.2）

实测真相（比第一版描述更精确）：
- `create_information_request` 只在行里**记录** `node_effect: WAITING:information_requested`，**完全不碰任务状态**——没有任何代码会把任务移进 WAITING。
- `coordination.TRANSITIONS["task"]` **有** `("RUNNING","WAITING")` 与 `("WAITING","RUNNING")`，但 `contracts.TaskStatus` **没有** `WAITING` —— 迁移表预期了一个领域模型从未定义的状态。
- 合法源状态只有 `RUNNING`（没有 `READY → WAITING`）：设计意图是 Agent 已 claim 并进入 RUNNING、发现缺口时进 WAITING。

结论：这是**真缺口**，不是探针假象。必须在协议层施加效果 + 把 `WAITING` 补进枚举。

---

## 8. 分期与验收

### Stage 1（当前，全部赌注）——本机双 Agent 协议闭环

**范围**：`apps/local/` CLI 动词层 + 最小本地 Agent 运行时 + 内置 LLM 执行器 + F1/F2 修复。
**不含**：UI、MCP、技能、外部执行体适配。

**验收标准**（不是"CLI 存在"，是**协议真的闭环**）：

1. `task create` → 任务落库
2. Agent A `claim` 拿到租约
3. A 发现信息缺口 → `ask --to B` → `OPEN` + **任务进 WAITING**（F2 生效）
4. B 的循环取到 → `answer` 带证据 → `ANSWERED`
5. A `consume` → `CONSUMER_ACKNOWLEDGED`，节点恢复
6. A 产出成果物 + receipt
7. A `report` → StageReport（含 `can_continue_safely`）
8. **另一条路径**：A `concern` → 任务进 `BLOCKED`
9. `status` 从事件日志还原整条链
10. **协议层零网络调用**（只有执行体调 LLM 走网）

> 第 10 条是重点。跑通它，整个论点被证明，剩下全是打包。跑不通，任何 MCP 或技能都救不了。

### Stage 2——MCP 薄壳
工具 = §5 同一批动词。薄适配器，不另开认证路径。

### Stage 3——本地只读查看器
三个视图 + 事件时间线（见 §6 护栏）。**必须建在 Stage 1 之后。**

### Stage 4——通用协作技能
现在只有 `math-modeling-competition` 这种**领域**技能，缺的是"何时交接、报告必须含什么、何时必须停下"这种**跟领域无关**的纪律。

### Stage 5——外部执行体适配 + 打包成"插件"
装一次 = 技能 + MCP 配置 + CLI。**插件不是第四种技术，是外壳。**

### Stage 6（可选）——跨机器
跨机器是唯一真的会断的（云端执行体失去归属）。协议不挡路——"状态存在哪里"本来就不在协议语义里。**但不承诺免费。**

---

## 9. 已知风险

**R1：Agent 形态决定"互通"的含义（最容易做错）。**

- **守护进程型**（`opencode run`、`codex exec`、通用 CLI 模板）——永远活着、自己轮询，交接**真自动**。
- **交互型**（你开着的 Claude Code / ZCode / Cursor 窗口）——它不在"运行"，是一段需要人参与的对话；交给它的东西只能等**下一轮**才被看到。

组合含义：两个守护 = 全自动；守护+交互 = 半自动靠人推动；两个交互 = 靠人传话。
**这不是缺陷，是特性**（可按 Agent 选"无人值守"或"我在时才干活"）。但**必须现在决定首批支持哪种**，否则会做出一个假装能自动、实则卡住的系统。
→ Stage 1 只做**守护进程型**（D14）。

**R2：范围回弹。** 见 §6 四条护栏。

**R3：现有在线部署怎么办。** 建议**别删**，让它停着。数据迁移不是问题——平台库本来就是 SQLite，`platform.db` 拷到 `~/.synapforge/state.db` 即迁移。

---

## 10. 已决策、不得重复打开

- 不做托管网页平台（D1）。
- 不重写协议层（D2）——`coordination.py` / `acceptance.py` 等实测可原样用。
- 不建常驻 broker / 守护进程（D4）。
- 不新建仓库（D11）——代码进 `apps/local/`。
- 不把凭据写进仓库（D12）——仓库是**公开**的。
- 不合并 `ask` 与 `handoff`（D9）。
- 不在 Stage 1 做 UI / MCP / 技能 / 外部执行体（D7、D10）。
- 不重建能力令牌层（D8）。
- **不要重新推导"协议能不能脱离服务器"**——§2 已有探针证据。

---

## 11. 执行体配置（仓库外，不写入本文档）

- 位置：`~/.synapforge/config.json`
- 端点：OpenAI 兼容 `https://api.treeapi.online/v1`，模型 `glm-5.3-flash`
- **实测注意**：该模型是**推理模型**，`max_tokens` 必须留足——实测 `max_tokens=16` 时 13 个 token 被思考吃掉，回复被截断。执行器默认应给 ≥2000。
- **凭据绝不可复制进本仓库**（公开仓库）。

---

## 12. Stage 1 实施结果（2026-10-20 已完成）

### 12.1 交付的文件

| 文件 | 作用 |
|---|---|
| `apps/local/synapforge/__init__.py` | 路径引导（复用 `apps/api` 的协议实现，不复制） |
| `apps/local/synapforge/paths.py` | 状态目录与配置解析（凭据只在仓库外） |
| `apps/local/synapforge/db.py` | `open_store()`：WAL + busy_timeout + 完整协议表 + **不播种的 LocalStore** |
| `apps/local/synapforge/commands.py` | **动词层**：协议的唯一命令入口（护栏二） |
| `apps/local/synapforge/cli.py` | 20 个子命令，JSON 默认输出（Agent 消费者优先） |
| `apps/local/synapforge/llm.py` | 最小 OpenAI 兼容客户端 + 测试用 `ScriptedLlm` |
| `apps/local/synapforge/runtime.py` | 守护进程型 Agent 循环（消费 → 回答 → 推进） |
| `apps/local/tests/test_local_protocol.py` | 15 项：闭环 / 诚实停止 / 零网络 / 领约 / 解析 |
| `apps/local/tests/test_local_concurrency.py` | 1 项：4 个真子进程并发写 |
| `scripts/synapforge.py` | CLI 启动器 |
| `scripts/synapforge_demo.py` | 真实模型端到端演示 |

### 12.2 对共享代码的三处改动（都已通过平台全量回归）

1. **`coordination_store.create_information_request`**：`blocking=required` 且任务处于 `RUNNING` 时，
   经 `check_transition` 施加 `RUNNING → WAITING`；源状态不匹配时**如实返回 `node_effect_result.effect="NONE"`
   与原因**，不静默假装已生效。
2. **`coordination_store.consume_information_request`**：回复可消费且任务处于 `WAITING` 时施加
   `WAITING → RUNNING`，返回 `task_resumed` 标志（这是"等到了就继续"的可观测证据）。
3. **`contracts.TaskStatus` 增加 `WAITING`**：迁移表早就有 `RUNNING ↔ WAITING`，枚举此前落后于它。
   加法改动。

**回归证据**：平台全量 `1010 项测试 OK（14 skipped）`；协调协议/契约/租约 fencing/多 agent 断言
六件套 `91 项 OK`。

### 12.3 实施中发现的两个真问题（都已修）

**P1：`Store._seed()` 会污染本地事实源，并且静默绕过项目隔离。**
`Store.__init__` 每次打开都会跑 `_seed()`；其中 `_seed_identity()` 有一条：

```sql
INSERT OR IGNORE INTO agent_project_grants
SELECT a.agent_id, p.id, ... FROM agents a CROSS JOIN projects p
```

**CROSS JOIN agents × projects** —— 把**每个 Agent 授权到每个项目**。本地库里表现为凭空出现
`agent-nova` / `agent-mira` / `agent-aster`，而且"谁能访问哪个项目"这个本应由
`agent_project_grants` 行表达的事实被冲成"人人有权"。
→ 修法：`db.py` 里的 `LocalStore` 覆盖 `_seed` / `_seed_identity`：**不播演示数据**，只保留
组织/团队/成员身份行（`create_project` 要求 team 属于 organization，没有团队行建不了项目）。

**P2：授权写法的 check-then-insert 竞态。**
`grant_project_access` 原本先 SELECT 再 INSERT，多进程下理论可重复插入。
→ 本机实测 4 进程各写 6 次没有触发（每进程 agent_id 不同、无同键争用），但写法已改为
先查再插并返回 `duplicate` 标志，仍是单写者语义——**若将来出现同键并发，需要 `INSERT ... WHERE NOT EXISTS` 或唯一索引**。
（注：`agent_project_grants` 当前**没有**唯一约束。）

### 12.4 本地授权口径（D8 的落地）

`Store._assert_agent_project_access` 只认 `agent_project_grants` 一行 —— 无令牌、无到期、无撤销。
**这正是本地形态该保留的那一半**：能力声明保留（`skill_match` 派发要用），
`device_project_grants`（带 `token_hash` / `expires_at` / `revoked_at`）是远程令牌层，本地不使用。

### 12.5 F1 结果：多进程并发写**已证明**

- `journal_mode = wal`，`busy_timeout = 15000`。
- `test_local_concurrency`：4 个**真子进程**各开自己的连接、各写 6 个任务 → 24/24 落库，
  全部退出码 0，无 `SQLITE_BUSY`。
- 探针阶段该失败是**探针自身**漏了 `events.sequence` 的 NOT NULL，不是锁冲突——现已澄清。

### 12.6 真实模型端到端（证据）

`python scripts/synapforge_demo.py` —— 两个本机 Agent、真实模型（`glm-5.3-flash`）、零服务器：

```
轮 1：A 领活并尝试完成
  A 的动作：ask
  A 问 agent-facts         节点效果：{'effect': 'WAITING', 'reason': 'information_requested'}
  [轮1 之后] 任务状态 = WAITING
轮 2：B 取到请求并回答
  B 回答：synapforge 是一个本机运行的多 Agent 协作协议工具…（用的是它自己角色里的事实，没有编造）
轮 3：A 消费回复并继续
  A 消费 → {'effect': 'RUNNING', 'task_resumed': True}
  A 的动作：finish
  收尾信息：基于 agent-facts 提供的定位信息，写出简介：synapforge：本机运行的多 Agent 协作协议…
  [轮3 之后] 任务状态 = WAITING_REVIEW

持久化事件：project.created → task.created → project.task.created → task.claimed → task.progress
→ project.information_request.created → .acked → .answered → .consumed → task.result_submitted
```

**状态轨迹 `RUNNING → WAITING → RUNNING → WAITING_REVIEW` 是本阶段的核心证据**：
"等到了就继续"不是一句备注，是任务状态机里真的发生了。

### 12.7 Stage 1 验收对照（计划 §8）

| # | 验收项 | 状态 |
|---|---|---|
| 1 | `task create` 落库 | ✅ |
| 2 | Agent A `claim` 拿租约 | ✅ |
| 3 | A `ask` → OPEN + **任务进 WAITING** | ✅（F2 修复后） |
| 4 | B 取到 → `answer` 带证据 | ✅ |
| 5 | A `consume` → CONSUMER_ACKNOWLEDGED，节点恢复 | ✅（`task_resumed=true`） |
| 6 | A 产出成果物 + receipt | ⬜ **未做**——演示只到 StageReport |
| 7 | A `report` → StageReport | ✅（测试覆盖，演示未走） |
| 8 | `concern` → 任务进 BLOCKED | ✅（测试覆盖） |
| 9 | `status` 从事件日志还原整条链 | ✅ |
| 10 | **协议层零网络** | ✅（`socket.socket` 被桩掉整条闭环照样跑通） |

**未完成项**：第 6 项（成果物与 receipt 落盘）留给 Stage 1.5。

### 12.8 已知边界（诚实口径）

- **在飞任务只存在进程内存里**（`LocalAgent._inflight`）。跨进程恢复需要持久化——属后续工作。
- **`WAITING` 没有专用原因列**：原因在 `information_requests.node_effect` 里（那本来就是事实源），
  `tasks.blocked_reason` 对 WAITING 保持为空。若需要内联原因，属加法迁移。
- **执行体是直连 LLM**，不是 opencode/codex/claude 适配器（本机没装）。外部产品适配属 Stage 2。
- **`agent_project_grants` 无唯一约束**：同键并发授权仍是理论竞态（P2）。

---

## 13. Stage 1.5 实施结果（2026-10-20）：成果物与溯源回执

补齐了 §8 验收表里唯一未过的第 6 项。

### 13.1 新增文件

| 文件 | 作用 |
|---|---|
| `apps/local/synapforge/artifacts.py` | 工作区快照/差分 + 成果物登记 + `RECEIPT_FORMAT v1` 回执 |
| `apps/local/tests/test_local_artifacts.py` | 6 项：回执字段、内容往返、越界拒绝、待审状态、差分识别 |

### 13.2 复用而不是复制（重要）

三个**纯净可复用**的模块直接从 `apps/agent` 引过来，没有复制第二份：

| 模块 | 提供 | 纯净度 |
|---|---|---|
| `workspace_scan` | `snapshot` / `diff`：执行前后差分识别产出 | 只依赖 os/dataclasses/pathlib |
| `result_uploader.OutputDiscovery` | 哈希 / MIME / artifact_type 推断 | 只需 mimetypes |
| `receipts.build_file_receipt` | `RECEIPT_FORMAT v1` 回执生成 | 只依赖 hashlib/json/datetime |

区别只在**通道**：平台侧经 HTTP 上传 + 本地补传队列（要扛断网）；本地侧直接写同一个 `Store`
的成果物表——**没有网络这一层，就不需要把失败伪装成"待补传"**。

采集点遵循 `RECEIPT_FORMAT §5.3`：通道名 `workspace_diff`，`tool_call_id` 用任务 id。

### 13.3 产出靠差分识别，不靠 Agent 自报

```
执行前快照 → 执行 → 执行后快照 → 差集就是本轮产出
```

Agent 在 `finish` 里声明的 `artifacts: [{path, content}]` 只是**写文件**；**能被登记成成果物的
依据是差分结果**，不是它报的清单。`test_finish_without_artifacts_is_honest` 锁住了这一点：
没声明产出就不会凭空多出成果物。

越界路径（`../../escape.md`）被拒绝，且**必须写进摘要**——不许静默当成"已完成且无产出"。

### 13.4 本轮抓到的两个真问题

**P3（本轮最重要）：事件号分配跨进程会撞号。**

`store._insert_event` 原本是：

```python
next_sequence = SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE project_id=?
INSERT INTO events (..., sequence, ...) VALUES (...)
```

`MAX+1` 与 `INSERT` 是**两条语句**，中间没有写锁。WAL 只保证写是串行的，两条语句之间另一个
进程仍可能提交同一号 → `UNIQUE constraint failed: events.project_id, events.sequence`。

**平台永远撞不到**：uvicorn 单进程，`_SerializedConnection` 的进程内锁就够。**本地多进程才是常态**，
所以这个 bug 只在本地暴露——`test_local_concurrency` 第一次是靠运气过的，全量套件里才复现。
→ 修法：有界重试（`_EVENT_SEQUENCE_RETRIES = 8`），重算后重插；修后并发测试连续 5 次通过。

**这不是"本地才有的新 bug"，是"单进程架构长期掩盖的既有 bug"。**

**P4：`Path.write_text` 在 Windows 上把 `\n` 翻成 `\r\n`，让哈希不可复现。**

`RECEIPT_FORMAT §2` 定义 `output_hash` 对**文件内容字节**计算。用 `write_text` 写产出会让磁盘字节
与作者给的正文不一致 → 哈希对不上算出来的内容哈希。
→ 修法：产出与测试一律 `write_bytes(content.encode("utf-8"))`，绝不让平台翻译换行。

### 13.5 端到端证据（真实模型）

`python scripts/synapforge_demo.py`：

```
轮 3：A 消费回复 → task_resumed: True → finish
  写入工作区：['intro.md']  （工作区 ~/.synapforge/workspace/agent-writer）
  成果物 intro.md  输出哈希 11953cd2ecad2010  108 bytes
  收尾信息：已根据 peer 提供的定位信息，写出一句 28 字的简介并落成 intro.md。

成果物清单（含溯源回执）：
  intro.md  status=PENDING_REVIEW  v1  receipt=v1  channel=workspace_diff
    output_hash=11953cd2ecad2010  bytes=108
    content_hash=11953cd2ecad2010df4bcc70bf284d54bc01a46f005f04f72913405a1553c767

读回成果物内容（来自状态库的对象存储，不是工作区文件）：
  'synapforge：让多个 AI Agent 在本机通过协议互相协作、交接的本地多 Agent 协作工具。'

事件：… → project.information_request.consumed → artifact.created
      → project.artifact.uploaded → artifact.content_stored → task.result_submitted
```

要点：`output_hash` 是完整 `content_hash` 的**前 16 位**（与 `RECEIPT_FORMAT §2` 一致）；
`status=PENDING_REVIEW` —— **执行体只报告，不判断，不自己批准自己**。

### 13.6 验收表更新

§8 验收第 6 项（成果物 + receipt）**已过**。Stage 1 十项验收现在**全部通过**。

### 13.7 新增边界（诚实口径）

- **成果物一律 `PENDING_REVIEW`**：本地没有人工审核界面，批准仍走命令行/后续 UI。**未批准
  的成果物不能被下游消费**——这个不变量现在只靠状态字段表达，没有强制点（平台侧由门禁强制）。
- **单文件上限 64MB**（`MAX_ARTIFACT_BYTES`）：本地直写没有传输约束，但仍要闸门，避免把整盘
  工作区灌进成果物表。
- **差分靠 (size, mtime)**：同尺寸同 mtime 的内容改写识别不出来——这是平台既有 `workspace_scan`
  的口径，不是本轮引入的。

---

## 14. Stage 2 实施结果（2026-10-20）：MCP 薄壳

### 14.1 新增文件

| 文件 | 作用 |
|---|---|
| `apps/local/synapforge/mcp.py` | MCP server（stdio，换行分隔 JSON-RPC 2.0）+ 15 个工具 |
| `scripts/synapforge_mcp.py` | MCP 启动器（Agent 宿主配置用） |
| `apps/local/tests/test_local_mcp.py` | 16 项：工具表纪律、JSON-RPC、真子进程 stdio |

启动命令（给 Agent 宿主的 MCP 配置）：

```
python <repo>/scripts/synapforge_mcp.py --agent <agent_id> --project <project_id>
```

### 14.2 "薄"的实际含义

**没有第二套实现。** 每个工具直接调用 `commands.<动词>`，不变量仍在命令层强制一次（护栏二）。
MCP 只做两件事：协议翻译 + 错误归一。

### 14.3 工具表（15 个，受上下文预算约束）

| 工具 | 说明 |
|---|---|
| `synapforge_status` / `synapforge_inbox` | 总览 / 待我处理 |
| `synapforge_task_create` / `claim` / `heartbeat` / `start` / `report` / `finish` | 任务生命周期 |
| `synapforge_ask` / `answer` / `consume` | 信息请求 |
| `synapforge_concern` | 诚实停止 |
| `synapforge_handoff_create` / `handoff_decide` | 交接（accept/reject 合成一个"决策"） |
| `synapforge_artifact` | 成果物（list / read / publish） |

**工具数是硬预算**，有测试锁住：`assertLessEqual(len(TOOLS), 15)`。
理由：工具 schema 会进模型上下文，这是稀缺资源。平台有约 300 条路由——
**不要让 300 条路由变成 300 个工具**，这正是"动词层"这一步存在的意义。
要加工具，先收敛动词。

### 14.4 两条设计决定

**D15：工具层错误是"结果"，不是协议错误。**
`tools/call` 出错时返回 `isError: true` + 稳定 `code`（`missing_argument` / `unknown_tool` / 领域码），
而不是 JSON-RPC error。理由：Agent 需要**读到 code 才能分支处理**——协议级错误对模型是不可读的。
协议级错误只留给真正的协议问题（未知方法 `-32601`、解析失败 `-32700`）。

**D16：启动时只登记**默认** Agent 参与默认项目。**
本地单机场景下默认 Agent 就是"我自己的 Agent"，让它零仪式可用是合理的；
但单次调用里覆盖的 `agent_id`/`project_id` **不做隐式授权**——那会变成悄悄放权。
（这条是被测试逼出来的：第一版没有登记参与者，`claim` 被 `agent_project_access_denied`
挡掉，而测试没检查 `claim` 的返回值，于是静默失败。）

### 14.5 本轮抓到的真问题

**P5：启动器文件名遮蔽包名。**
`scripts/synapforge.py` 与包 `synapforge` **同名**。Python 把脚本所在目录放在 `sys.path[0]`，
于是 `import synapforge` 解析到的是那个启动器文件，不是包 →
`ModuleNotFoundError: No module named 'synapforge.cli'; 'synapforge' is not a package`。

→ 两处修：启动器改名 `synapforge.py` → **`synapforge_cli.py`**；并且三个启动器统一
**把包路径强制置顶**（先移除再 `insert(0, ...)`，而不是"不在路径里才插入"——后者在
PYTHONPATH 已含相对路径时会跳过，仍然被 `scripts/` 压住）。

### 14.6 验收证据

- `apps/local/tests/test_local_mcp.py` **16 项全过**，含：
  - 工具表纪律（数量预算、名字前缀、**每个列出的工具都必须真的有处理器**）
  - JSON-RPC 形状（initialize 回声协议版本、通知不回包、未知方法 `-32601`）
  - 工具错误形态（`isError: true` + code）
  - **全链路只经 MCP 工具**跑通：claim → start(RUNNING) → ask(→WAITING) → answer → consume(→RUNNING)
  - **真子进程 stdio**：4 条消息（含 1 条通知）→ 恰好 3 条响应，stdout 零污染
  - 坏 JSON 之后服务继续存活（下一条消息正常应答）
- `apps/local` 全量 **38 项测试连续 2 轮 OK**
- 平台全量回归 **1010 项 OK（14 skipped）**

### 14.7 当前交付总览

| 层 | 状态 | 位置 |
|---|---|---|
| 协议核心（复用，未改动语义） | ✅ | `apps/api/app/{coordination,coordination_store,acceptance,progress_ledger,event_catalog,store,...}.py` |
| 动词层 | ✅ | `apps/local/synapforge/commands.py` |
| CLI（20+ 子命令） | ✅ | `scripts/synapforge_cli.py` |
| 本地 Agent 运行时 | ✅ | `apps/local/synapforge/runtime.py` |
| 成果物 + 溯源回执 | ✅ | `apps/local/synapforge/artifacts.py` |
| MCP 薄壳（15 工具） | ✅ | `apps/local/synapforge/mcp.py`、`scripts/synapforge_mcp.py` |
| 本地只读查看器 | ⬜ Stage 3 | — |
| 通用协作技能 | ⬜ Stage 4 | — |
| 打包成"插件" | ⬜ Stage 5 | — |
| 跨机器 | ⬜ Stage 6（可选） | — |

**代码量**：`apps/local/` 共 **2,973 行**（含测试）。对照 §3：可丢弃的平台侧约 33,600 行。

### 14.8 已知边界（Stage 2 之后）

- **MCP 是"拉"不是"推"**：别人家 Agent 干十分钟这件事没法当一次 tool call 等着。落法是
  "调用返回 WAITING + 句柄，稍后再查"——CLI 与 MCP 都是 `inbox` 模型。
- **stdio 传输没有并发工具调用保护**：`McpServer` 单线程顺序处理消息，这符合 stdio 语义，
  但要在同一进程里并发调用需要另外设计。
- **未验证真实宿主**：测试用的是自造 JSON-RPC 客户端与真子进程。**没有在 Claude Code /
  Cursor / ZCode 里实际挂载过这个 MCP server**——"能被真实宿主识别"尚未证明。

---

## 15. Stage 2 收尾：真实宿主挂载已验证（2026-10-20）

§14.8 里"未在真实宿主里挂载过"这一条**已关闭**。

### 15.1 宿主信息

Claude Code **不是**常驻 PATH 的独立安装，而是**安装在 VS Code 扩展里**：

```
C:\Users\19855\.vscode\extensions\anthropic.claude-code-2.1.226-win32-x64\
    resources\native-binary\claude.exe          (287 MB)
```

版本 `2.1.226 (Claude Code)`。这解释了"记得装过但不在 PATH 里"——VS Code 扩展不会把二进制加进 PATH。
数据目录 `~/.claude`（首次启动 2026-08-06），MCP 配置存 `~/.claude.json`。

### 15.2 挂载命令（已验证）

```bash
claude mcp add synapforge --scope user -- \
  "C:\Users\19855\AppData\Local\Programs\Python\Python312\python.exe" \
  "<repo>\scripts\synapforge_mcp.py" --agent agent-claude --project <project_id>
```

`claude mcp list` → **`synapforge: ✔ Connected`**

### 15.3 端到端证据链（逐级）

| 级别 | 证据 |
|---|---|
| 启动 | `MCP server "synapforge": Successfully connected (transport: stdio) in 442ms` |
| 能力 | `Connection established with capabilities: {"hasTools":true,...,"serverVersion":{"name":"synapforge","version":"0.1.0"}}` |
| **工具枚举** | Claude Code 把工具注册成 `mcp__synapforge__synapforge_status` —— 这个命名说明它真的消费了 `tools/list`（`hasTools` 只反映我们声明的能力，**不能**作为枚举证据；这条区分很重要） |
| 模型选择 | 模型**主动**决定调用该工具 |
| 执行 | `[Stall] tool_dispatch_end tool=mcp__synapforge__synapforge_status outcome=ok durationMs=109` |
| 结果 | 模型回复 `0ef33046-df4d-44d3-83bd-e67b54a5d79f` —— **本机 SQLite 里的真实 project_id** |

**含义**：一个真实 AI Agent，经真实宿主，调用本地协作协议，读到本机真实状态，**全程零服务器**。
Stage 1 的核心命题至此从"CLI 能跑"升级为"真实宿主能用"。

### 15.4 途中发现并修掉的宿主侧配置 bug（不是本项目代码的问题）

第一次挂载后模型调用报：

```
There's an issue with the selected model (zai-org/GLM-5.3-Flash[1m]).
It may not exist or you may not have access to it.
```

**这是一句误导性的错误信息。** 调试日志给出的真因是：

```
[API REQUEST] /v1/v1/messages
[ERROR] API error (attempt 1/11): 404 404 Not Found
```

`~/.claude/settings.json` 里 `ANTHROPIC_BASE_URL = "https://api.siliconflow.cn/v1"`——
Claude Code 会再拼一次 `/v1/messages`，于是路径变成 `/v1/v1/messages` → 404。

- **诊断证据**：直连该端点用 `zai-org/GLM-5.3-Flash` 发 Anthropic 形状请求（含 tools、含 stream）
  均返回 **HTTP 200**；`/v1/models` 也确认该模型在账号可用列表里。
  → 模型名与凭据都没问题，**只是 base_url 多了一段 `/v1`**。
- **修法**：`ANTHROPIC_BASE_URL` → `https://api.siliconflow.cn`（去掉尾缀 `/v1`）。
- 已备份原文件为 `settings.json.bak-20261020-123451`，只改这一个键，其余未动。

**教训（通用）**：宿主报"模型不存在/无权限"时要看调试日志里的**实际请求路径**，
不要顺着它的措辞去改模型名——这里模型名一直是对的。

### 15.5 真实宿主带来的一个新约束

Claude Code 对每个 MCP 工具有**独立权限门**：

```
Claude requested permissions to use mcp__synapforge__synapforge_status,
but you haven't granted it yet.
[DEBUG] mcp__synapforge__synapforge_status tool permission denied
```

工具被枚举、被模型选中，但**默认不执行**。要真正用起来，用户需要在会话里 `/permissions` 放行，
或在 `~/.claude/settings.json` 加：

```json
"permissions": { "allow": ["mcp__synapforge"] }
```

**这是宿主的权限模型，不是我们的缺陷**——而且它与我们的护栏方向一致：
默认不放行，人决定是否授予。**我没有替用户加这条**（自动放行工具调用是安全决策，应由用户做）。

### 15.6 边界更新

- ✅ 真实宿主挂载与工具调用：**已验证**（Claude Code 2.1.226）
- ⬜ 其他宿主（Cursor / ZCode / Codex）尚未验证——MCP 是标准协议，但"标准"不等于"实现一致"，
  每个宿主都该像这次一样实测一次
- ⬜ 写类工具（`ask` / `concern` / `finish`）在真实宿主里的行为未实测——本次只跑了只读的 `status`

---

## 16. 双 Claude Code Agent 真实协作实验（2026-10-20）

### 16.1 实验设计：制造真实的契约依赖

任务是「协作看板」前后端：后端实现 HTTP API，前端实现单页。**关键在于不给任何一方完整信息**：

- 后端拿到的任务说"字段名、端点路径、状态取值**由你决定**，前端会来问你"
- 前端拿到的任务说"**你不掌握 API 契约**，必须先向 agent-backend 询问，不要猜"

于是信息缺口是真实的，`ask → answer → consume` 不是被提示词脚本化的。

**协作是跨进程、跨时间的**：每轮 `claude -p` 是一个全新会话（上一次的记忆不存在）。
这恰好验证了架构主张——**Agent 不需要同时活着**，状态在 SQLite 里。

### 16.2 实际发生的协作链

| 轮 | Agent | 动作 | 协议事实 |
|---|---|---|---|
| 1 | 后端 | 领任务（**误领了前端任务**）、置 RUNNING、写 `server.py`、登记成果物 | `task.claimed` / `artifact.created` |
| 2 | 后端 | 从 `inbox` 拿回租约、`finish` | 任务 → `WAITING_REVIEW` |
| 3 | 前端 | 领任务（限制 stage=frontend）、置 RUNNING、**提出 8 项契约问题**、**停下不写代码** | 任务 → **`WAITING`**，请求 `OPEN` |
| 4 | 后端 | **读自己的 `server.py`** 后回答（不是凭记忆） | 请求 → `ANSWERED` |
| 5 | 前端 | `consume`（`task_resumed=true`）、按契约写 `web/index.html`、登记、`finish` | 任务 → `WAITING_REVIEW` |

**第 4 轮是这次实验最有价值的一步**：后端的上一个会话已经结束，它对代码没有任何记忆，
只能读文件。而它**第一轮提交的 summary 其实写错了**（描述成 `/cards` 契约，代码是 `/api/tasks`）——
这正是"报告不是状态、证据才是事实"的现场例证。

### 16.3 产出与验收

| 成果物 | receipt | 字节 | output_hash |
|---|---|---|---|
| `server.py` | v1 | 10,803 | `3b48098114482743` |
| `index.html` | v1 | 9,244 | `d337337560447f31` |

**HTTP 联调（真实起服务）**：

```
GET  /                     → 200, 9244 bytes, text/html
GET  /api/tasks            → {"tasks":[{id,title,status},...]}
POST /api/tasks            → 201 {"id":"task-5","title":"中文标题联调","status":"todo"}   ← UTF-8 正确
PATCH /api/tasks/task-5    → 200 {"status":"done"}
GET  /api/tasks            → 该任务状态确为 done
PATCH /api/tasks/nope      → 404 {"error": "task not found"}
```

**浏览器实机**：三列看板（待办/进行中/已完成）、任务卡片、新建输入框、状态切换按钮。
点「→ 进行中」后 task-1 从待办移到进行中，计数 4→3 / 0→1，提示条显示后端返回结果。

**结论**：两个互不相识、不同进程、不同时间的 Claude Code 会话，只经本地协议交换信息，
产出了一个能跑的全栈应用。

### 16.4 本地查看器（Stage 3）

`apps/local/synapforge/viewer.py` + `static/index.html`，启动：`python scripts/synapforge_cli.py ui`

**刻意不复用旧前端**（`apps/web` 一行没用）：旧的是 24,735 行 Next.js + 构建步骤；这里是一个 HTML 文件 +
stdlib `http.server`，**零构建、零 node_modules**。实测页面 12 KB。

形态按用户要求：**左侧 Agent 列表 + 右侧会话流**。左栏显示头像、名称、最近活动、"N 成果物 / N 待答"角标；
右栏把该 Agent 的事件渲染成对话卡片，按 `kind` 上色（提问/回答/成果物/诚实停止/执行），
顶部显示"等它回答"与"它在等别人"。

护栏落地：
- **只读是机制**：连接用 `file:...?mode=ro` 打开，物理上无法写。
- `ui` 动词在**打开读写库之前**处理——查看器绝不会顺手建库或播种。
- 按需启动、绑 127.0.0.1、关掉就没了。

### 16.5 本轮发现的两个缺口

**P6：`claim` 没有 Agent 级归属（已修，见 §17）。** 平台按**人**归属（`assignee_member_id`），本地多 Agent 场景下
两个 Agent 共享同一个 owner member，于是后端 Agent 顺手把前端任务也领走了（实测发生）。
当时的缓解是 `stage` 过滤；正确修法已落地，`stage` 过滤保留为可叠加的第二层。

**P7：`inbox` 不返回在途任务的租约令牌（已修）。** `-p` 一次性会话结束后进程内存没了，
新会话拿不到 `lease_token` 就无法 `finish`。现在 `inbox.in_flight_tasks` 会 join `task_leases`
带回 `lease_token` 与 `lease_expires_at`，并且**只返回我自己持有租约的任务**（之前返回项目里所有
CLAIMED/RUNNING 任务，会误导 Agent 去动别人的活）。

### 16.6 一个环境事实（影响后续测试方式）

Claude Code 在这个环境里**Bash 不可用**：调试日志 `Git Bash not found; BashTool will be unavailable`。
所以 Agent 不能自己跑测试或起服务——它们只负责写代码，**验收必须由外部做**。
这不影响协作实验，但意味着"让 Agent 自证能跑"这条路走不通。

### 16.7 复现

```bash
# 两个身份各一份严格 MCP 配置（互不可见）
~/.synapforge/apps/board/mcp-agent-{backend,frontend}.json

claude -p "<该轮指令>" \
  --mcp-config ~/.synapforge/apps/board/mcp-agent-backend.json \
  --strict-mcp-config --permission-mode acceptEdits \
  --allowedTools "mcp__synapforge,Write,Read"
```

`--strict-mcp-config` 是关键：每个会话只看到自己那个身份的服务，不会串号。

---

## 17. P6 修复：Agent 级任务归属（2026-10-20）

### 17.1 根因

平台的归属算子是**人**：`member_for_agent(agent_id)` 读 `agents.owner_member_id`，
`claim_next_task` 用它过滤 `(t.assignee_member_id IS NULL OR t.assignee_member_id = ?)`。
本地所有 Agent 都用 `owner_member_id="local-owner"`，于是这条过滤对所有 Agent 恒真——
**谁会领到谁的任务，全看谁先调 `claim`**。另外任务创建时 `assignee_member_id` 恒为 NULL
（"未指派 = 先到先得"），进一步放大了问题。

### 17.2 修法（复用既有过滤，不新增第二套判断）

1. **每个 Agent 一个独立归属成员**：`agent_member_id(agent_id) = "local-agent:" + agent_id`。
   `ensure_agent` 每次调用都补正 `agents.owner_member_id`（历史数据统一挂在 `local-owner` 上）。
2. **落成真实成员行**：写 `human_members`（身份锚点，避免悬空引用）+ `project_memberships`
   （`create_task` 的 `_validate_dispatch_target` 要求指派目标必须是项目成员）。
3. **`create_task(assignee_agent_id=...)`**：把归属写进 `assignee_member_id`，
   于是 `claim_next_task` 现成的 SQL 过滤就生效了——**没有新增一行判断逻辑**。
4. **指派即参与**：给定 `assignee_agent_id` 时自动 `ensure_participant`，
   避免"指派给一个还不在项目里的人"。

语义说明：平台的 `member` 是"可问责的人"。本地形态里 Agent 自己就是参与方，
这里复用该表作为身份锚点，**不是把 Agent 说成人**。

### 17.3 接口变化

| 层 | 变化 |
|---|---|
| `commands.create_task` | 新增 `assignee_agent_id` |
| `commands.agent_member_id` | 新增，归属成员 id 的唯一算法 |
| `commands.ensure_agent` | 补正 `owner_member_id`；`owner_member_id` 默认值从 `local-owner` 改为按 Agent 派生 |
| `commands.ensure_participant` | 返回值新增 `membership` |
| CLI | `task-create --assignee-agent` |
| MCP | `synapforge_task_create` 新增 `assignee_agent_id` |
| `claim` | **无改动**——归属过滤在既有 SQL 里 |

### 17.4 测试

`apps/local/tests/test_local_ownership.py` **8 项**：

- 两个 Agent 归属成员不同（共用 = 过滤失效）
- 归属成员行与项目成员行都存在（不悬空、指派校验能过）
- **核心回归**：指派给 beta 的任务，alpha 领不到；beta 领得到
- 归属真的写进了 `assignee_member_id` 与 `assignee`
- 未指派任务仍是先到先得（不破坏原语义）
- 指派给新 Agent 会自动注册
- 历史共用归属会被补正，补正后归属过滤立刻生效
- `stage` 过滤仍可与归属过滤叠加

`apps/local` 全量 **46 项 OK**。

### 17.5 真实库验证

```
=== 迁移前 ===
  agent-backend      local-owner
  agent-frontend     local-owner

=== 迁移后 ===
  agent-backend      local-agent:agent-backend
  agent-frontend     local-agent:agent-frontend

=== 归属验证 ===
  建任务: [归属验证] 只给后端的活 | assignee: agent-backend
  前端领取 -> False no_ready_task
  后端领取 -> True
```

### 17.6 一条教训：事件日志不可删

清理验证任务时我一度尝试 `DELETE FROM events ...`，被外键挡住。**这是对的**——
事件是只追加的事实源，删事件等于篡改历史。正确做法是**改状态**（`CLAIMED → CANCELLED`，
迁移表允许），历史事件原样保留。以后"清理测试数据"一律走状态机，不碰 `events`。

---

## 18. 任务指派接进所有入口（2026-10-20）

### 18.1 入口盘点（先查全，再动手）

| 入口 | 位置 | 状态 |
|---|---|---|
| 命令层 | `commands.create_task(assignee_agent_id=...)` | §17 已接 |
| CLI | `task-create --assignee-agent` | §17 已接 |
| MCP 工具 | `synapforge_task_create { assignee_agent_id }` | §17 已接 |
| 演示脚本 | `scripts/synapforge_demo.py` | **本轮接上** |
| 显式领取 `claim_task(task_id)` | 平台侧 | **无需改**：`_assert_dispatch_allows` 已拒绝 `task_assigned_to_another_member` |
| 批量领取 `claim_next_task` | 平台侧 | **无需改**：归属过滤在既有 SQL 里 |
| 工作流引擎建任务 | `workflow_service.start_workflow_run` | **本地层尚未暴露工作流动词**，不构成入口 |
| `scripts/deploy/*` 验收脚本 | 平台线上验证 | 不是本地产品面，不在范围内 |

### 18.2 补上的另一半：`inbox.assigned_to_me`

**只写 `assignee_member_id` 是半个功能**：它保证了"别人领不到"，但被指派方根本**发现不了**
有活派给自己——没有事件落在它名下（创建事件的 actor 是建任务的人），`in_flight_tasks` 只列已领取的。

现在 `inbox` 返回：

```
assigned_to_me    派给我、还没被任何租约占住的可领任务（status ∈ READY/NEEDS_REVISION）
in_flight_tasks   我领了在跑的任务 + lease_token
to_answer         要我回答的信息请求
to_consume        等我消费的回复
handoffs          待我收的交接
```

顺带返回 `member_id`，让"我是以哪个成员身份被指派的"可核对。

**归属成员以库为准**：`agent_member_id_of()` 读 `agents.owner_member_id`，缺失才按
`local-agent:<id>` 派生——不在命令层自造一套映射。

### 18.3 三处同步

- **CLI**：`inbox --human` 增加「派给我（还没领）」分区
- **MCP**：`synapforge_inbox` 描述写明 `assigned_to_me`
- **查看器**：右栏顶部新增「派给它 · 还没领」分区（只读投影，同一口径，不构成第二套判断）

### 18.4 测试

`test_local_ownership.py` 扩展到 **12 项**，新增 4 项覆盖指派可见性：

- 指派的任务出现在**被指派方**的 `assigned_to_me`，不出现在别人的
- 领取后从 `assigned_to_me` 移到 `in_flight_tasks`，且带上 `lease_token`
- 未指派任务不进任何人的 `assigned_to_me`（保持"先到先得"语义）
- `inbox` 正确报告 `member_id`

`apps/local` 全量 **50 项 OK**。

### 18.5 真实库验证

```
建任务：[指派验证] 只有后端该看到   assignee=agent-backend
agent-backend    member=local-agent:agent-backend    待领=['[指派验证] 只有后端该看到']
agent-frontend   member=local-agent:agent-frontend   待领=[]
```

查看器实机确认「派给它 · 还没领」分区渲染出该任务（READY · assign-demo）。

### 18.6 仍然存在的一个不对称（有意保留）

`claim` 只能"领下一个"，不能"领某一条"。被指派方如果同时有多条任务，只能按
deadline/priority/updated_at 的顺序拿——不能指定先做哪条。这是沿用平台的领取语义；如果以后
需要"指定领取"，应在 `commands.claim` 加可选的 `task_id`，走 `claim_task`（它已有归属校验），
而不是在 `claim_next_task` 上加过滤参数。

### 18.7 顺带修掉的 CLI 陷阱

`--state` / `--human` 定义在共享 parent parser 上，**写在子命令之前时不生效**：
argparse 会先在顶层解析一次，再在子解析器里解析一次，子解析器的默认值（`None` / `False`）
把顶层已解析的值覆盖掉了。

修法：两者的默认值改成 `argparse.SUPPRESS`（未传就不设属性），读取用
`getattr(args, "human", False)` / `getattr(args, "state", None)`。
新增 `apps/local/tests/test_local_cli.py`（6 项）锁住"前置/后置都生效""默认输出是 JSON"
"错误也走 JSON + code"。

### 18.8 测试总量

| 文件 | 项数 |
|---|---|
| `test_local_protocol.py` | 15 |
| `test_local_mcp.py` | 16 |
| `test_local_ownership.py` | 12 |
| `test_local_artifacts.py` | 6 |
| `test_local_cli.py` | 6 |
| `test_local_concurrency.py` | 1 |
| **合计** | **56 项 OK** |

---

## 19. 群聊视图与实时协作演示（2026-10-20）

### 19.1 成果物到底在哪（三层，都要知道）

一个成果物不是"一个文件"，而是**三处并存的事实**：

| 层 | 位置 | 作用 |
|---|---|---|
| **Agent 工作区**（源码本体） | `~/.synapforge/apps/<项目>/<agent>/…` | Agent 自己写文件的地方；差分识别的输入 |
| **对象存储**（内容副本） | `~/.synapforge/objects/projects/<pid>/artifacts/<aid>/v<ver>` | `store_artifact_content()` 落盘的实际字节 |
| **数据库登记**（元数据） | `artifacts` 表 | 名称、类型、状态、`content_hash`、`storage_key`、receipt 七列 |

协作看板那次实证：

```
server.py   10,803B  receipt=v1  agent-backend
  storage_key : projects/b7e374c5…/artifacts/eb589bf1…/v1
  content_hash: 3b480981144827439caeb277e1d78b740b8d0f6f964626af48b5f5c912f649d5
  output_hash : 3b48098114482743  channel=workspace_diff

index.html   9,244B  receipt=v1  agent-frontend
  content_hash: d337337560447f3107e32b5c10c088b72199b0be78c713ce974b5067774b3601
```

**`content_hash` 是完整 SHA-256；`output_hash` 是它的前 16 位**（`RECEIPT_FORMAT §2`）。
`GET` 回来的内容来自**对象存储**，不是工作区文件——工作区可以改，成果物不会跟着变。

### 19.2 群聊视图

单 Agent 视图回答"它在干什么"；群聊回答"**它们之间发生了什么**"。两者是同一张 `events` 表的
两种投影，不构成两个事实源。

**接口**：`GET /api/group/stream?project_id=&limit=`

- 默认项目取**最近有事件**的那个。**不能按 `sequence` 排**——`sequence` 是按项目各自编号的
  （`UNIQUE(project_id, sequence)`），不是全局时钟，按它排会永远选中事件最多的项目（本轮实测踩到）。
- 只显示"人说的话 + 人做的事"，机械事件（`artifact.content_stored`、`project.seeded`）过滤掉。
- 每条补 `from` / `to`（谁对谁说）+ `subject`（关联任务标题），否则读不出一场对话。
- `agents` 只列**本项目参与者**（按 `agent_project_grants`），不混入别的项目的 Agent。

**页面**：侧栏顶部固定一项「群聊 · 全部 Agent」，右栏按发送方渲染头像与颜色，
提问/回答带 `→ 对方` 箭头。

**可链接**：选择写进地址栏（`#__group__` / `#agent-xxx`），刷新和分享都能回到同一视图。

### 19.3 实时协作演示（真实模型、真实进程）

两个全新 Agent，`实时协作演示` 项目：

```
17:10:43  agent-live-a  领取任务
17:10:46  agent-live-a  开始执行：任务需要平台准确名称，我并不知道，先向 agent-live-b 询问，不编造。
17:10:49  agent-live-a → agent-live-b  提问：这个平台叫什么名字？   blocking=required
17:12:37  agent-live-b  已确认收到
17:12:37  agent-live-b → agent-live-a  回答：这个平台叫 synapforge，是一个运行在本机的多 Agent 协作协议工具。
17:14:41  agent-live-a → agent-live-b  收到回复，继续   节点效果：RUNNING
17:14:47  agent-live-a  登记成果物 intro.md
17:14:51  agent-live-a  提交结果   任务状态：WAITING_REVIEW
```

**状态轨迹 `RUNNING → WAITING →（B 回答）→ RUNNING`**，成果物 `intro.md`（166B，receipt v1，
`output_hash=5223530b91bfc60f`）。

关键点：**发起方先停住了**。它没有猜、没有编，明确说"我并不知道，先问，不编造"，把节点置为
WAITING 后就结束了那一轮会话。两分钟后另一个进程、另一个会话把答案写了进来。

### 19.4 本轮抓到的两个 bug

**P8：`sequence` 不是全局时钟。** 群聊默认项目用了 `ORDER BY sequence DESC`，而 `sequence` 是
按项目各自编号的 → 永远选中事件数最多的项目。改用 `created_at`。

**P9：`const initial` 与 `function initial()` 同名 → 整页白屏。**
脚本在解析阶段就失败，于是 `state` 未定义、页面停在初始文案，而 **HTTP 仍然 200**，没有任何显式报错。
新增 `ViewerAssetTests`：把 `<script>` 抽出来跑 `node --check`，并检查页面里必须存在
`/api/agents`、`/api/group/stream`、`__group__` 三个标记。

### 19.5 测试总量

`apps/local` 全量 **64 项 OK**（新增 `test_local_viewer.py` 8 项：群聊聚合 4 + 只读护栏 2 + 静态资源 2）。

---

## 20. 契约成果物 + 版本引用校验（2026-10-20）

### 20.1 要解决的问题（来自一次真实翻车）

云盘协作那次：前端问了契约，后端答了，前端按**答案**开工；后端后来收到前端的追问，
把契约**改了一版**并照着新版实现。两边都"觉得自己确认过了"，产物只有 `GET /api/files`
一条重合。协议把过程全记下来了，但**没有任何机制把这两条线对到一起**。

根因不是沟通不足，是**"同意"不是一等对象**：一条 `ask/answer` 消息没有身份、没有版本、
没有约束力，任何人随时可以换一个理解继续走。

### 20.2 设计：三个对象，三处强制

| 对象 | 承载 | 复用/新建 |
|---|---|---|
| **契约正文** | 规范化 JSON 字节 | **复用 artifact**：进对象存储、有完整 `content_hash`、有 receipt |
| **契约身份** | `key → version + hash + artifact_id` | 新建（artifact 每次改写都是新 id，没有稳定身份） |
| **绑定** | `(contract, agent) → version + hash` | 新建（原来完全缺失的那一环） |
| **同意留痕** | `project.contract.published` / `.bound` 事件 | 复用事件目录（v5 追加 `contract` 族） |

**为什么复用 artifact 而不另造一套文件体系**：内容寻址、对象存储、receipt、内容哈希对账
全都现成；契约只是"有稳定身份的 artifact"。

**为什么必须有 `key → 当前版本` 的映射**：实测里后端改契约产生的是**一条新 artifact**，
前端手里那个旧 id 依然"有效"——没有任何东西能说"你引用的版本已经过期"。artifact 模型
天然缺"同名同族"的稳定身份。

### 20.3 三处强制点

1. **发布时（`contract_publish`）**：新版本一落库，立刻算出**谁被这次改动甩下了**，
   在返回值和 `project.contract.published` 事件里都带上 `stale_bindings`。
   → 发布者当场知道"我刚刚让 A 失效了"。这是原来完全缺失的信号。

2. **收尾时（`finish`）——硬闸门**：任务声明了 `required_contracts` 时，
   校验"我绑定的版本 == 最新版本 且 hash 一致"。不满足 → **拒绝 finish**，
   报 `contract_drift`，并写清 `绑定 v1 / 最新 v2`。
   → **为什么是拒绝而不是警告**：警告会被忽略（这轮已经证明 Agent 会忽略指令）。
   而拒绝收尾意味着它必须重新 `contract_show` + `contract_bind`——
   **绑定动作本身就是"我重新看过了"的证明**。

3. **开工时（`inbox`）**：直接给出每个契约的 `my_version` / `latest_version` / `in_sync`，
   让 Agent 在动手前就知道自己是否同步，而不是等到收尾才被拦。

### 20.4 两条不可绕过的规则

- **只能绑当前版本**：`bind(version=旧版)` 直接报 `contract_version_not_current`。
  绑旧版本等于"明知不同步还继续"，没有意义。
- **正文没变不造新版本**：同 hash 重复发布不递增版本号，否则版本号失去意义。

### 20.5 接口

| 层 | 形态 |
|---|---|
| 命令层 | `publish_contract` / `show_contract` / `bind_contract` / `contract_status` |
| CLI | `contract-publish --body/--file`、`contract-show`、`contract-bind`、`contract-status`；`task-create --required-contract` |
| MCP | `synapforge_contract`，action = `publish / show / bind / status`（工具预算 16 → 17） |
| 任务声明 | `create_task(required_contracts=[...])` → 写进 `resource_policy.contracts`（**无需迁移**） |

### 20.6 实测（CLI 端到端，非单测）

```
1) 后端发布 v1                    → v1  hash=219013fb653bfad3…
2) 前端绑定 v1                    → bound v1  in_sync=True
3) 后端改契约 → v2                 → stale_bindings=[{agent_id: agent-front, version: 1}]
4) 前端看状态                      → my_v=1  latest_v=2  in_sync=False
5) 未同步就收尾                    → {"code":"contract_drift","errors":["api: drifted（绑定 v1 / 最新 v2）"]}
6) 重新绑定后收尾                  → accepted=True  status=WAITING_REVIEW
```

测试：`test_local_contracts.py` **13 项**（发布/版本/绑定/闸门/留痕/inbox 同步态/artifact hash 对账）。
`apps/local` 全量 **77 项 OK**。

### 20.7 它不解决什么（诚实边界）

- **不校验语义正确性**：契约里写 `POST /api/upload`、代码里写成 `/api/uploads`，
  绑定照样"同步"。它保证的是**双方对同一份文本负责**，不是这份文本对。
- **不强制"双方都绑"**：`finish` 只校验**收尾者自己**的绑定。作者发布即可视为其同意；
  另一方是否绑过，由 `contract.bound` 事件和 `inbox` 呈现，但不阻断。
  （想做对称校验，需要任务声明"对方是谁"——目前任务只有一个 assignee。）
- **不做语义 diff**：只说"版本变了"，不说"哪里变了"。给 Agent 看 diff 属于后续工作。
- **契约是 opt-in**：没声明 `required_contracts` 的任务完全不受约束。这是有意的——
  不能给所有任务强加流程；但反过来，**忘了声明就没有保护**。
