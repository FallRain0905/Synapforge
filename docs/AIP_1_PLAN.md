# AIP-1 计划：能力卡 · 候选推荐 · 身份两段 · 意图两字段

> 状态：**已实施并上线**（2026-09-22，三期全部交付：AIP-1a/1b/1c 与 AIP-1d 各一次发布）
> 交接：`docs/handoffs/AIP_1AB_DESCRIPTION_DISCOVERY_HANDOFF.md`（能力卡/候选推荐/身份两段）、`docs/handoffs/AIP_1D_TASK_INTENT_HANDOFF.md`（预算/证据要求）
> 来源：`docs/AIP_COMPARISON.md` §5 可借鉴清单第 1–4 条（含"信誉分"一条）
> 一句话：把 AIP 的**描述 / 发现 / 身份 / 意图**四层里对我们真正有用的部分落到 synapforge，
> 治的是我们自己的三个老毛病——能力是自由字符串、派单只有"能/不能"、`agent_id` 把"哪种执行体"和"哪个实例"混在一起。

---

## 0 总览

| 期 | 内容 | 治的病 | 数据变更 | 前端落点 | 上线批次 |
| --- | --- | --- | --- | --- | --- |
| **AIP-1a** | 能力卡：技能名 + 版本 + 输入 + 输出 | `python`/`Python` 是两种能力；无版本；授权范围与技能混在一个袋子 | `agents` +1 列（迁移 021） | `/team` 能力目录 | 第 1 批 |
| **AIP-1b** | 发现给"候选列表 + 推荐" | 只有"能/不能领"，没有"这几台能跑，谁更该跑" | 无（纯读） | `/team` + 任务详情 + auto 调度器 | 第 1 批 |
| **AIP-1c** | 身份两段：`package_id` + `instance_id` | `agent_id` 混装"执行体种类"与"实例"，统计多实例靠猜 | `agents` +3 列（已在迁移 021 内，本期开始消费） | `/team` 程序包聚合 | 第 2 批 |
| **AIP-1d** | 意图两字段：预算 + 显式证据要求 | 任务只有约束/时限/边界，没有"这次最多花多少""必须附什么才算完成" | `tasks` +2 列（迁移 022） | 任务表单/详情 + 门禁 | 第 2 批 |

**为什么分两批**：a + b 是"修匹配 + 加展示"，不动执行语义，风险最低、立刻见效（演示与日常使用马上能看出差别）。
c 动身份、d 动领取与门禁语义，各自单独一期验证更稳。三次上线，各自独立可回退。

**取意不照抄**：AIP 标准正文与我们拿不到（`docs/AIP_COMPARISON.md` §1），所以下面所有字段形状都是**按我们已有事实重新设计**的，
凡是我们做不到的（例如 token 计量）一律显式标注"只记录、不强制"，不做看起来很像其实空的字段。

---

## 1 现状证据（每条都能指到代码）

| # | 现状 | 位置 |
| --- | --- | --- |
| 1 | 匹配是精确字符串、大小写敏感 | `store.py:3291` `_task_requirements_satisfied` → `all(str(item) in capabilities for item in required)` |
| 2 | 一处判定函数、四处判定点 | `_task_requirements_satisfied`（`store.py:3291`）被点名领取 `store.py:3803`、轮询跳过 `store.py:3862`、调度器 `store.py:3077` 调用 |
| 3 | **两套词表混在一个集合里**：`_agent_capability_set` = Agent 自报 tools ∪ `devices.capabilities` ∪ 心跳 `device_runtime_state.capabilities` | `store.py:3251` |
| 4 | 其中设备声明/心跳那一套是**授权范围**不是技能（值形如 `task.claim`、`artifact.write`） | `contracts.py:1308` `DeviceProjectGrantCreate.capabilities` 默认值；设备注册 `--capabilities` 默认 `["task.claim"]`（`agentd.py:1598`），心跳把它原样上报（`agentd.py:237`） |
| 5 | 没有版本：`required_capabilities` 与 `supported_tools` 都是纯名字 | `contracts.py:183`、`store.py:519` |
| 6 | 派单只有布尔过滤，没有候选排序 | `store.py:3060` `_auto_dispatch_candidate`（返回 `(member, agent, matched)` 或 `None`） |
| 7 | 成功率数据**已经在库里**，只是没用于派单 | `runs.status` + `runs.agent_id`（`store.py:481`），吞吐已按成员聚合（`store.py:2296`） |
| 8 | `agent_id` 混合两种身份：内核用 `agent-<device_id>`，CLI 由使用者自定 | `agentd.py:1273`、`--agent-id` |
| 9 | 任务无预算、无显式证据要求 | `TaskCreate` `contracts.py:183-203`；门禁规则只看 review/risk（`risk_rules.py:59` `gate_rules`） |

**第 3/4 条是真问题，不只是"不好看"**：今天一条任务只要写 `required_capabilities: ["artifact.write"]`，
就会被"已授权 artifact.write 的设备"满足——因为授权范围被当成技能算了。这是词表混淆直接导致的**误匹配**。

---

## 2 设计总则（四条硬约束）

1. **兼容优先，老内核不改也能接入**：新字段全部可选；旧内核只发 `supported_tools: ["codex"]` 的老写法继续有效；
   归一化在**读时**做，所以历史库里已有的 `Python` 也立刻能与 `python` 匹配（不需要数据回填）。
2. **协议禁区不动**：`packages/agent_protocol/` 的信封、序号、心跳语义一个字不改。
   能力卡只走我们自己的 HTTP 契约（`POST /api/agents/register`）。
3. **迁移成套**：新增 `apps/api/migrations/021_agent_description.sql`（第 1 批：`agents` 四列 + `package_id` 索引）与
   `022_task_intent.sql`（第 3 批：`tasks` 两列），同步 `store.py` 的 SQLite 建表 / `_ensure_columns`，
   并把文件名按批次加进 `apps/api/test_platform_contracts.py` 的迁移清单（否则红）。
4. **默认空值 = 零行为变化**：新字段缺省时不改变任何既有判定；凡是"新语义会改变既有结果"的地方（§3.3 的技能域收窄），
   单列出来、写回归测试、在交接里标注为显式行为变更。

---

## 3 AIP-1a 能力卡（技能名 + 版本 + 输入 + 输出）

### 3.1 数据形状

```python
class CapabilityCard(APIModel):
    skill: str = Field(min_length=2, max_length=80)          # 归一化后的技能 id，如 codex / python / doc.write
    version: str = Field(default="", max_length=40)          # 空 = 不声明版本
    inputs: list[str] = Field(default_factory=list)          # 吃进去什么（ArtifactType 词表 ∪ 自由串）
    outputs: list[str] = Field(default_factory=list)         # 产出什么
    description: str = Field(default="", max_length=400)
```

- 落库：`agents.capability_cards TEXT NOT NULL DEFAULT '[]'`（JSON 数组）。
- `agents.supported_tools` **保留**，语义收窄为"卡片里 skill 的扁平派生视图"：新内核发卡片 → 平台同时写 `supported_tools`（去重 skill 名），
  旧内核只发字符串 → 直接落 `supported_tools`，`capability_cards` 为空。
  这样 `_agent_capability_set`、`/team` 现有 chips、`test_capabilities.py` 全部照旧工作。
- `inputs` / `outputs` 用**我们已有的 `ArtifactType` 词表**（`contracts.py:103-125`：`data_profile`、`model_spec`、`code`、`result_table`、`figure`、`paper_source`…）加自由串；
  不认识的取值不拒绝，只在 `/team` 标成灰色（避免又造一套需要维护的枚举）。

### 3.2 归一化规则（治 `python` vs `Python`）

`normalize_skill_id(raw) -> str | None`：

| 步骤 | 规则 |
| --- | --- |
| 1 | `strip()` → `lower()` |
| 2 | 空白与 `_` → `-`；折叠连续分隔符（`doc__write` → `doc-write`） |
| 3 | 保留 `.`（我们的命名空间习惯，如 `doc.write`、`code.python`） |
| 4 | 校验 `^[a-z0-9][a-z0-9.:-]{1,79}$`；不合法返回 `None` → 写入侧 422 `capability_skill_invalid` |

**不做同义词映射**（`py` 不等于 `python`）——同义词表要靠人维护且永远不全，猜错比不猜更坏。
两边（声明侧与要求侧）都在读时归一化，所以历史数据自动收敛。

### 3.3 技能域的收窄（显式行为变更）

把 `_agent_capability_set` 拆成两个域，函数名与返回值明确：

- `_agent_skill_set(agent_id)` —— **技能域**，只用于任务匹配：
  Agent 自报卡片 skill ∪ `supported_tools` ∪ 心跳/运行时能力里**不属于授权范围前缀**的值。
  授权范围前缀白名单：`task.`、`artifact.`、`run.`、`handoff.`、`review.`、`project.`、`session.`、`terminal.`（`agentd.py:1780` 的 session worker 默认值就是 `session.run` 这类）。
- `_agent_scope_set(agent_id)` —— **授权域**，只用于能力令牌判定（`_require_agent_capability`，`main.py:488`）与 `/team` 展示，**不再参与任务匹配**。

**影响面**：任何"把授权范围词写进 `required_capabilities`"的任务从此不再被满足——这正是我们要修的误匹配。
已知用例复核：`test_capabilities.py`、`test_agent_capability.py`、`test_p3.py` 用的都是真技能词（`codex`、`python`、`r-language`），预计零改动；
新增一条回归断言：声明 `artifact.write` 的执行体**不满足**要求 `artifact.write` 的任务。

### 3.4 版本匹配（最小语义，别做全套 semver）

要求侧允许在名字后写 `@` 约束：`required_capabilities: ["codex@>=1.2", "python"]`。

| required 写法 | 含义 | 匹配条件 |
| --- | --- | --- |
| `python` | 只要求有这个技能 | 任意版本（含未声明版本） |
| `codex@*` | 显式任意 | 任意 |
| `codex@1.2` / `codex@1.2.3` | 至少这个版本 | provided ≥ required（按数值段逐段比较，缺失段补 0） |
| `codex@>=1.2` | 显式下限 | 同上 |

- **不支持** `^ ~ -` 区间与预发布标签（`1.2.0-beta`）——写进边界；遇到不认识的写法**当字面量精确比较**并在 `/team` 标 `unparsed`，不静默放过。
- 提供方未声明版本（空）时：`required` 无约束 → 命中；`required` 有下限 → 不命中（缺版本即不满足下限，如实反映）。
- 实现落点：`apps/api/app/skill_match.py`（新，纯函数：`normalize_skill_id`、`parse_capability_ref`、`satisfies(requirement, provided_cards)`）。
  纯函数便于单测，也避免 `store.py` 继续膨胀。

### 3.5 落点清单

| 面 | 改动 |
| --- | --- |
| 迁移 | `021_agent_description.sql`：`agents` + `capability_cards`、`package_id`、`instance_id`、`package_source`（c 期一起落，见 §5）；索引 `agents (package_id)` |
| SQLite | 建表语句加列 + `_ensure_columns("agents", {...})`；**`register_agent` 改为显式列名 INSERT**（现在是 `INSERT INTO agents VALUES (12 个值)`，加列后位置插入会直接报错——必须改） |
| 契约 | `contracts.py`：`CapabilityCard`、`AgentRegister.capability_cards`、`Agent.capability_cards`（`Agent` 缺字段不会被 `_agent()` 报错，pydantic 默认忽略多余键，但**不加上就永远看不到**，所以必须显式加） |
| Store | `_agent_skill_set` / `_agent_scope_set`（替换 `_agent_capability_set`，保留旧名做别名以免漏改调用点）、`_task_requirements_satisfied` 走 `skill_match.satisfies`、`capability_catalog` 输出 cards |
| 端点 | `GET /api/team/capabilities` 每个执行体多返回 `cards`、`unregistered_skills`、`scope_capabilities`、`package_id`/`instance_id` |
| 前端 | `/team` 能力目录：技能 chip 带版本，行内可展开看输入/输出；"未登记技能"独立成组；授权范围单列一栏（不再与技能混排） |
| 内核（可选） | `agentd.py` 新增 `--capability-card <json|@file>`，与前缀收窄配套；不升级的内核完全照旧 |

### 3.6 验收（AIP-1a）

1. 单测：归一化（大小写/下划线/非法字符）、版本比较（≥/<、缺失段、`*`、unparsed 字面量）、技能域与授权域分离（含 §3.3 的回归断言）。
2. 兼容：只发 `supported_tools` 的老注册路径，`/team` 与领取行为与今天完全一致（同一组既有用例不改动通过）。
3. HTTP 层：`POST /api/agents/register` 带卡片 → `GET /api/team/capabilities` 能读到 cards 与 `package_id`；
   非法技能名 422 `capability_skill_invalid`。
4. 界面：`/team` 展开/收起、未登记技能分组、授权范围独立栏（浏览器实机 + 手机视口）。

---

## 4 AIP-1b 候选列表 + 推荐

### 4.1 目标

把"能/不能领"变成"**这几台能跑，按推荐顺序排列，并给出理由**"。
同一条排序函数同时服务三处：任务详情展示、`/team` 能力目录的补位建议、auto 调度器的选人。

### 4.2 排序函数（单实现）

`Store.rank_task_candidates(project_id, task_row) -> list[Candidate]`：

1. **硬过滤**：只在项目成员里、其执行体对该项目有授权（`agent_project_grants`）、技能满足（`skill_match.satisfies`）。
   **在线不是过滤条件**：离线机器照样列出来并标注"离线，开机就能接"（这正是"没人能跑"告警的下一步动作）；
   但**调度器只取 `online` 的候选**（保持今天的选人范围不变，`store.py:3052` 的 `WHERE a.status = 'online'`）。
2. **分层**：`satisfied`（全满足）在前，`partial`（缺技能，列出 `missing`）在后——不隐藏"差一点"的机器，因为"给它补一个技能"往往比"重新找机器"更快。
3. **排序键**（全部可解释、完全确定）：
   `(online 降序, 平滑成功率 降序, 该成员在项目内的未结任务数 升序, member_id 升序, agent_id 升序)`
   - 平滑成功率 = `(succeeded + 2) / (total + 4)`（Beta 先验；无执行记录 = 0.5 中性，不因为"跑过一次成功"就排到有 50 次经验的前面）。
   - `total = succeeded + failed + blocked`（`runs.status`），数据来源见 §1 第 7 条。
4. 每条候选带 `reason`（"全满足 · 成功率 0.83（12 次）· 在手 1 件"）与 `runs_total`，**界面必须显示样本量**，避免把 1 次成功读成"很可靠"。

### 4.3 端点与前端

| 面 | 改动 |
| --- | --- |
| 端点（新） | `GET /api/tasks/{task_id}/candidates`（`project.view` 权限）→ `{task_id, required_capabilities, satisfied: [...], partial: [...]}` |
| 端点（扩） | `GET /api/team/capabilities` 的 `unmet_tasks` 每条补 `candidates`（最多 5 条 + 总数） |
| 调度器 | `_auto_dispatch_candidate` 改为调用 `rank_task_candidates` 并取**第一条 satisfied**；「每 tick 每项目只推进一件事」「不自动批准门禁」「切回 manual 立即停手」三条不变量原样保留 |
| 前端 | ①`/team` "没人能跑"旁边给"这几台能跑"；②工作区任务详情加「推荐执行体」区块（点击可复制 agent_id / 一键指派给该成员名下执行体）；③条目显示成功率与样本量 |

**性能**：成功率用**一次** `GROUP BY agent_id` 的分组查询（按 `project_id` 过滤），不做逐 agent 查询；
候选上限（默认 20）先截断再算分，避免大组织下把整张 runs 表拉进内存。

### 4.4 不做（边界）

- **不改派单口径**：manual 模式下仍旧只有 owner/project_lead 能改负责人（`store._dispatch_policy` 不动）；推荐只是"给人看的建议 + auto 模式的选人依据"。
- **不改领取资格**：`claim_next_task` 的先到先得语义不变（候选排序不影响谁先领到）；是否让"推荐"影响领取顺序属于二期。

### 4.5 验收（AIP-1b）

1. 单测：排序键（无记录中性、样本量、负载、确定性——同样输入两次调用结果一致）；`partial` 分层；技能不满足者不进 `satisfied`。
2. 调度器回归：既有 `test_workspace_auto.py` 全绿（选人结果在这批数据上与"负载最轻"一致），新增一条"成功率影响选人"的用例。
3. HTTP：候选端点权限（contributor 可读、非项目成员 403）、任务不存在 404。
4. 界面：任务详情推荐区块 + `/team` 补位建议（浏览器实机）。
5. 线上：`server_verify.sh` 22/22 不回归。

---

## 5 AIP-1c 身份两段（程序包序列号 + 实例序列号）

### 5.1 语义定义

| 字段 | 含义 | 取值示例 | 来源 |
| --- | --- | --- | --- |
| `package_id` | **哪种执行体程序包**（跨机器相同） | `codex@0.9.3`、`cli:workbuddy@0.4`、`agentd@0.1.1` | 内核上报（新）/ 平台推断（老） |
| `instance_id` | **哪个实例**（同一包的不同机器/进程互不相同） | `agent-3f9c…`、DIY 名字 | 等于今天的 `agent_id`（我们的 `agent_id` 本来就是实例身份） |
| `package_source` | 该 `package_id` 是上报的还是推断的 | `reported` / `inferred` | 平台 |

- **`agent_id` 不变**：它是对外标识与所有既有外键的锚点，不改名、不换值（改它就是全库迁移，收益为零）。
  两段拆分的价值在于**统计与展示不必再猜**：`SELECT package_id, COUNT(*) ... GROUP BY package_id` 即可回答"同一执行体几台在跑"。
- `package_source` 是**诚实字段**：推断值标 `inferred`，界面显示为斜体/灰字（"推断：codex@0.9"），避免把猜测当事实。

### 5.2 派生规则

1. **新内核（reported）**：注册体新增可选 `executor: {kind, version, package}`（kind ∈ `codex|cli|command|unspecified`，与 `agentd.py::_executor_kind` 对齐）；
   平台拼 `package_id = f"{kind}@{version}"`（无版本则 `f"{kind}@unknown"`）。内核侧取值来自既有探测（`agent_inventory.py` 的 `adapter_versions`），不新增探测。
2. **老行（inferred）**：一次性幂等回填 —— 优先用该 Agent 最近连接设备的 `device_runtime_state.adapter_versions`（探测得来、比自报可信）取第一个非空键值 → `kind@version`；
   取不到则按 `agent_id` 前缀兜底 `legacy:<prefix>`（前缀 = 第一个 `-` 之前）。回填只写 `package_source='inferred'` 的行，重复执行不覆盖已上报值。
3. `instance_id = agent_id`（同表冗余一列，目的是让语义显式；未来若一个 `agent_id` 下要挂多实例进程再扩展）。

### 5.3 聚合视图与前端

- `GET /api/team/capabilities` 新增 `packages`: `[{package_id, source, instances, instances_online, skills, succeeded, failed, success_rate, members}]`。
- `/team`：能力目录上方加"执行体程序包"面板——一个包一行，展开看实例清单（哪台机器、谁的、在线否、成功率）。
  这一栏直接回答今天要靠字符串猜的问题（"我装了 3 台 codex，平台能不能看出来是同一个执行体"）。

### 5.4 验收（AIP-1c）

1. 单测：`reported` 优先于 `inferred`；回填幂等（跑两次结果一致）；设备 `adapter_versions` → package 的推断；前缀兜底。
2. 契约：老内核（不发 `executor`）注册后 `/team` 仍能看到包（标 `inferred`）。
3. 界面：程序包面板 + 实例展开（浏览器实机）。
4. 既有回归：`test_agent_health.py`、`test_devices.py`、`test_agent_capability.py` 全绿（改动只在新增列与展示）。

---

## 6 AIP-1d 意图两字段（预算 / 显式证据要求）

### 6.1 数据形状

```python
class TaskBudget(APIModel):
    max_seconds: int | None = Field(default=None, ge=30, le=86400)   # 单次执行墙钟上限（强制）
    max_attempts: int | None = Field(default=None, ge=1, le=50)      # 整条任务允许的领取次数（强制）
    max_tokens: int | None = Field(default=None, ge=1)               # 只记录（见 6.2）

class EvidenceRequirement(APIModel):
    evidence_type: Literal["artifact", "run", "event", "external_source"]  # 复用既有词表
    min_count: int = Field(default=1, ge=1, le=20)
    note: str = Field(default="", max_length=200)
```

- 落库：`tasks.budget TEXT NOT NULL DEFAULT '{}'`、`tasks.evidence_requirements TEXT NOT NULL DEFAULT '[]'`。
- `deadline` 继续承担"时限"，**不**重复进预算（AIP 的意图对象里时限/预算本就是两项）。

### 6.2 哪些真强制、哪些只记录（诚实表）

| 字段 | 消费点 | 强度 |
| --- | --- | --- |
| `max_seconds` | `claim_task`（`store.py:3793`）里把租约上限压到 `min(请求值, max_seconds)` | **强制**（用既有租约机制，不新增计时器） |
| `max_attempts` | `claim_task` 领取前数 `task_leases`（每次领取一行，已确认为既有行为）；超限抛 `task_budget_exhausted`；`claim_next_task` 循环里跳过（catch 该错，不整体失败） | **强制** |
| `max_tokens` | 写进 Run 的 `parameters`/`execution_profile` 供执行体参考；平台**不计量**（无 token 采集） | 只记录：`/team` 与任务详情标「未强制（执行体不回报用量）」 |
| `evidence_requirements` | ①任务详情/交付页**读时**算缺口并展示（非阻塞，默认）；②门禁规则 `task:evidence_requirements`（仅当字段非空时生效）出 blocking finding | 展示强制；门禁按需（不自动批准） |

**为什么不做 token 强制**：我们没有任何用量采集（`runs` 表无 token 字段，`RunComplete` 无 usage）。
要做真强制，得先让执行体在 `RunComplete` 里回报用量（codex 的 JSONL 有 usage，cli 执行体没有）——
那是独立的下一步，写在这里但不进本期（避免造一个"看起来在管成本、其实没人看"的字段）。

### 6.3 消费点细节

- **领取**：`claim_task` 是**唯一咽喉**（`claim_next_task` 也走它），所以强制逻辑只写一处；
  超限时写一次性事件 `task.budget_exhausted`（幂等同 `task.auto_unmatched` 的写法，`store.py` 里既有先例），并在项目聊天里出一条卡片。
- **门禁**：`risk_rules.gate_rules()` 增加 `task:evidence_requirements`，`create_gate` 时把"缺证据"落成 finding；
  默认（`evidence_requirements` 为空）**规则不产生任何 finding**，既有任务与模板包行为零变化；不自动批准（与现有"门禁一律人工"一致）。
- **展示**：任务详情显示"预算：≤600s / ≤2 次尝试 / tokens（未强制）"与"证据缺口：run ×1 缺"；交付页在任务行加缺口角标。

### 6.4 落点

| 面 | 改动 |
| --- | --- |
| 契约 | `TaskBudget`、`EvidenceRequirement`；`TaskCreate`、`Task.budget`/`evidence_requirements`；`TaskUpdateRequest` 加两字段（**"出现才生效"**，沿用 `assignee_member_id`/`deadline` 的既有约定，空值 = 清空/不设） |
| Store | `create_task` 写入 + 校验（`budget_invalid` / `evidence_requirements_invalid`）；`_task_budget_state(row)`；`claim_task` 强制两项；`task_evidence_gaps(task_id)`（读时算缺口，供详情与门禁共用） |
| 端点 | `PATCH /api/tasks/{id}` 支持两字段（**必须声明 `request: Request`** 并按既有惯例配 HTTP 层测试——这是踩过的坑）；任务详情端点返回缺口 |
| 门禁 | `risk_rules.py` + `create_gate` 的规则消费 |
| 前端 | 工作区任务详情（预算/证据要求表单 + 缺口提示）、`/tasks` 列表字段、交付页缺口角标 |

### 6.5 验收（AIP-1d）

1. 单测：`max_attempts` 用尽后领取被拒且事件只写一次；租约被压到 `max_seconds`；清空预算后行为恢复；空 `evidence_requirements` 的门禁不产生 finding。
2. HTTP：`PATCH` 两个字段生效 + 非法值 422；任务详情缺口字段。
3. 兼容：`test_store.py`、`test_dispatch.py`、`test_workspace*.py`、`test_p3.py` 全绿（默认值下行为不变）。
4. 界面：任务详情设置预算与证据要求 → 触发一次超限 → 聊天卡片与任务详情状态正确（浏览器实机）。
5. 线上：`server_verify.sh` 22/22；演示项目（`_demo_doc_writing.py`）不受影响。

---

## 7 迁移与兼容总表

**两个迁移**：`021_agent_description.sql`（第 1 批上线，含 `agents` 四列；其中身份三列在第 2 批开始消费——带缺省的列先落地不影响任何行为）
与 `022_task_intent.sql`（第 3 批上线，`tasks` 两列）。

| 迁移 | 表 | 列 | 缺省 | 谁写 |
| --- | --- | --- | --- | --- |
| 021 | `agents` | `capability_cards` | `'[]'` | 注册（新内核）/ 空（老内核） |
| 021 | `agents` | `package_id` | `NULL` | 注册上报 / 回填推断 |
| 021 | `agents` | `instance_id` | `NULL` → 回填为 `agent_id` | 注册 / 回填 |
| 021 | `agents` | `package_source` | `'inferred'` 默认（老行）/ `'reported'`（新上报） | 注册 / 回填 |
| 022 | `tasks` | `budget` | `'{}'` | 建单 / PATCH |
| 022 | `tasks` | `evidence_requirements` | `'[]'` | 建单 / PATCH |

- 索引：`agents (package_id)`（021）；`tasks` 不加索引（预算/证据不用于过滤）。
- SQLite：建表语句同步 + `_ensure_columns`（含幂等回填 SQL）；**`register_agent` 改显式列名 INSERT**（§3.5 的坑）。
- PG：只做列与索引（PG 运行时读 `agents`，写入仍在 SQLite `Store`；`postgres_repository.py` 只读该表）。
- 迁移清单：`test_platform_contracts.py` 的期望文件名数组按批次追加 `021_agent_description.sql`、`022_task_intent.sql`。
- 回填走 `_ensure_columns` 之后的幂等 UPDATE（只在 `package_source='inferred' AND package_id IS NULL` 时写）。

---

## 8 测试、验收与发布口径（每期都跑）

1. **后端全量**：当前基线 API **455 项**（2 项 LaTeX 环境失败与代码无关）+ 本期新增；Agent 侧基线 **293 项（9 skipped）**（改 `agentd.py` 时）。
2. **前端**：`next build`（当前 25 页）+ `scripts/_w1_ssr.py`（当前 19×19×6）不变量；**改完必须重建并重启 `next start`**。
3. **后端改动必须重启 uvicorn**（踩过的坑：旧进程会让新行为"看起来没生效"）。
4. **线上**：`scripts/deploy/pack-source.sh`（先转 LF）→ `remote.py put` → `server_release.sh` → `server_verify.sh` 22/22；
   `/root/deploy` 下脚本变更需单独上传。
5. **浏览器实机**：每期至少一条端到端链路（a：注册带卡片 → 目录显示；b：任务详情推荐 → 一键指派；c：程序包面板；d：预算超限卡片）。
6. **登记**：`docs/IMPLEMENTATION_STATUS.md` 逐期登记 + `docs/SERVER_DEPLOYMENT.md` §10 增量发布记录；每期一份 `docs/handoffs/AIP_1x_*.md`。

---

## 9 风险与回退

| 风险 | 影响 | 处置 |
| --- | --- | --- |
| 技能域收窄误伤真实用法 | 某些任务突然"没人能跑" | 收窄只排除**已知授权范围前缀**；`/team` 会立刻显示"没人能跑"与缺失项；一条 UPDATE 即可回退（把被排掉的值加回技能集合） |
| 版本比较语义被误当成全套 semver | 用户写 `^1.2` 得不到预期 | 不支持的写法当字面量并在界面标 `unparsed`；文档与界面都写明支持的最小集合 |
| 平滑成功率所需 runs 数据稀疏 | 推荐看起来随机 | 显示样本量 + 无记录取中性 0.5 + 排序键完全确定（同输入同输出） |
| `package_id` 推断错误 | 统计分组错 | `package_source='inferred'` 显式标注；新内核上报后自动覆盖（只覆盖 `inferred`） |
| `max_attempts` 让任务卡死 | 无人能再领 | 一次性事件 + 聊天卡片可见；队长清除预算即可恢复；`max_attempts` 只限制"领取次数"，不影响人工改期/取消/改派 |
| 加列后位置 INSERT 报错 | 注册直接 500 | §3.5 已列为必改项；契约测试 + 注册 HTTP 测试覆盖 |

---

## 10 明确不做（本期边界）

- **不改 Agent 协议**（`packages/agent_protocol/` 禁区）：要对外讲 AIP 的话，正确做法是加 AIP 网关做翻译（`docs/AIP_COMPARISON.md` §6），不是改内核。
- **不做 CA 三方分离**（注册/发行/验证）与**可计价/清算**：单组织闭环与未商业化前收益为零。
- **不做 token/成本计量**：没有用量采集，只能记录（§6.2）；要强制先做用量回报。
- **不做硬词表门禁**：未登记技能只标灰、不拒绝注册（拒绝会打断既有接入的机器）。
- **不改 `agent_id`**、不改派单口径（`_dispatch_policy`）、不改领取先到先得语义。
- **不做同义词映射**（`py` → `python`）。

---

## 11 交付顺序

1. **第 1 批（AIP-1a + 1b）**：`skill_match.py` + 迁移 021（四列随批落地，身份列先不用）→ 契约/store/端点 → `/team` 能力卡与候选展示 → 调度器共用排序 → 回归 + 浏览器 → 上线。
2. **第 2 批（AIP-1c）**：`package_id`/`instance_id`/`package_source` 上报与回填 + 程序包聚合面板（无新迁移）→ 上线。
3. **第 3 批（AIP-1d）**：迁移 022 + 预算与证据字段 + 领取强制 + 门禁规则（非空才生效）+ 任务表单/详情 → 上线。
4. 每批：交接文档 + `IMPLEMENTATION_STATUS` + `SERVER_DEPLOYMENT §10` 登记。

---

## 12 需要你拍板的三个点（已给推荐）

1. **技能域收窄（§3.3）现在做还是缓一缓**：推荐**现在做**——它是"词表混淆"的直接后果，且只排除授权范围前缀，回退成本一行 SQL。
2. **token 预算"只记录不强制"能否接受**：推荐**接受**——界面明确标"未强制"，把"执行体回报用量"作为独立下一步（不假装在管成本）。
3. **上线节奏**：推荐**三次上线**（a+b / c / d），而不是一次性四期合并——a+b 立刻见效，c、d 各自单独回归更安全。

---

## 附：与 AIP 条目的对应

| AIP 条目（`AIP_COMPARISON.md` §5） | 本计划 |
| --- | --- |
| 1 能力卡（技能名+版本+输入+输出） | AIP-1a |
| 2 发现给候选列表 + 推荐 | AIP-1b |
| 3 身份拆包序列号 / 实例序列号 | AIP-1c |
| 4 意图对象补预算与显式证据要求 | AIP-1d |
| 5 信誉分做派单权重 | AIP-1b §4.2（平滑成功率进排序键） |
| 9 交互层报文：不做照搬，改走 AIP 网关 | §10 明确不做 |
| 10 CA 三方分离、11 可计价清算 | §10 明确不做 |