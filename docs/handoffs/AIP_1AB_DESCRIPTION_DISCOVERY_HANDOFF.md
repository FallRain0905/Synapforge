# AIP-1 第 1 批交接：能力卡 · 候选推荐 · 身份两段（已上线）

> 2026-09-22 · 计划见 `docs/AIP_1_PLAN.md` §3/§4/§5，本期交付 **AIP-1a + AIP-1b + AIP-1c**（第 2 批 AIP-1d 见后续交接）
> 线上：https://synapforge.top · 发布记录 `docs/SERVER_DEPLOYMENT.md` §10

## 1 这一批做了什么（一句话各一条）

| 期 | 交付 | 关键文件 |
| --- | --- | --- |
| **1a 能力卡** | 技能从"自由字符串"升级为 `{技能名, 版本, 输入, 输出, 说明}`；**技能域与授权范围域拆开**；大小写/分隔符归一化；`名字@>=1.2` 版本下限 | `apps/api/app/skill_match.py`（新）、`store.py`、`contracts.py` |
| **1b 候选推荐** | `rank_task_candidates` 单实现：任务详情、`/team` 补位建议、auto 调度器**共用同一排序**（在线 → 平滑成功率 → 在手件数 → 稳定序） | `store.rank_task_candidates`、`GET /api/tasks/{id}/candidates` |
| **1c 身份两段** | `package_id`（执行体程序包）+ `instance_id`（实例）+ `package_source`（reported/inferred）；`/team` 加"执行体程序包 × 实例"聚合 | 迁移 `021_agent_description.sql`、`_derive_package`/`_backfill_agent_packages`/`_upgrade_agent_package` |

## 2 治了什么病（可复现的现状证据）

1. **`python` 与 `Python` 是两种能力** → 归一化在**读时**做，历史数据不用回填：`_task_requirements_satisfied` 走
   `skill_match.unsatisfied_requirements`，声明侧与要求侧都收敛（测试 `test_case_insensitive_matching_without_backfill`）。
2. **两套词表混在一个集合里（真 bug）**：`_agent_capability_set` 原来把 `devices.capabilities`（值是**授权范围**
   `task.claim`/`artifact.write`）也当技能算 → 一条任务写 `required_capabilities: ["artifact.write"]`
   就会被"已授权 artifact.write 的机器"满足。现在拆成 `_agent_skill_set`（技能，用于任务匹配）与
   `_agent_scope_set`（授权范围，用于能力令牌判定与展示）；`_agent_capability_set` 保留为技能域的兼容别名。
   **这是显式行为变更**：把授权范围词写进 `required_capabilities` 的任务不再被误判为"有人能跑"
   （回归断言 `test_scope_grant_is_not_a_skill`）。
3. **派单只有"能/不能"** → 现在有候选列表 + 理由（"全满足 · 成功率 0.83（12 次） · 在手 1 件 · 离线，开机即可接"），
   离线机器照样列出（补位建议），但**调度器只取在线候选**，且取的正是界面推荐的第一条。
   平滑成功率 `(成功+2)/(总数+4)`（Beta 先验，无记录 = 0.5 中性），界面**显示样本量**，避免把 1 次成功读成"可靠"。
4. **`agent_id` 混装"哪种执行体"与"哪个实例"** → `package_id`/`instance_id` 两段；`package_source` 标注来源。

## 3 几个刻意的取舍（防误读）

- **不做同义词映射**（`py` ≠ `python`）：同义词表要靠人维护且永远不全，猜错比不猜坏。
- **版本语义只做最小集合**（`""`/`*`/`X[.Y[.Z]]`/`>=X.Y.Z`）：`^ ~ -` 区间与预发布标签**按字面量比较**并可由界面标注，
  不假装支持全套 semver。**提供方未声明版本时不满足任何下限**（缺版本不能证明达标，如实判不满足）。
- **包身份未知就留空，不按 `agent_id` 前缀造 `legacy:xxx`**。计划里写的"前缀兜底"在实现时被否掉了：
  `agent-<uuid>`、`agent-mira` 的前缀不携带执行体信息，硬拼会把互不相干的执行体归成"同一个包"——假分组比"未知"更坏。
  当前取值优先级：内核上报（reported）> 设备运行态探测值（inferred）> 空（界面显示"—"）。
- **技能域收窄**只排除已知授权范围前缀（`task.`/`artifact.`/`run.`/`handoff.`/`review.`/`project.`/`organization.`/`device.`/`session.`/`terminal.`）。
- **老内核零改动**：只发 `supported_tools` 的注册照旧工作；能力卡与新身份字段全部可选。

## 4 落点清单

**后端**
- `apps/api/app/skill_match.py`（新，纯函数）：`normalize_skill_id`、`split_requirement`、`version_satisfies`、
  `satisfies`、`unsatisfied_requirements`、`card_versions`、`merge_version_maps`、`is_scope_token`。
- `apps/api/app/migrations/021_agent_description.sql`（新）：`agents` + `capability_cards`/`package_id`/`instance_id`/`package_source`、
  `agents(package_id)` 索引、`instance_id` 回填、只读视图 `agent_skill_declarations`。
  迁移清单已同步 `test_platform_contracts.py`（含列名/索引/回填断言）。
- `store.py`：
  - **`register_agent` 改成显式列名 INSERT**（原来是 `INSERT INTO agents VALUES (12 个值)`——加列即 `table agents has 16 columns but 12 values were supplied`，
    这是本期真踩到的坑；`_seed` 与快照恢复里两处同类位置插入也一并修了）。
  - `_normalized_cards` / `_merge_skill_names` / `_agent_cards` / `_agent_skill_versions` / `_agent_skill_names` / `_agent_device_row`。
  - `capability_catalog` 重写：每个执行体给 `cards`/`skill_versions`/`scope_capabilities`/包实例身份/成功率；
    新增 `packages` 聚合；`unmet_tasks` 用归一化+版本判定并附 `candidates`（最多 5 条）。
  - `rank_task_candidates`（新）+ `_agent_reliability`（一次 `GROUP BY agent_id` 分组查询，可被 tick 复用）；
    `_auto_dispatch_candidate` 改为调用它；`auto_dispatch_tick` 每拍只查一次成功率。
  - `_derive_package` / `_infer_package_from_runtime` / `_backfill_agent_packages` / `_upgrade_agent_package`
    （设备上报运行态后自动把"未知"补成探测到的版本）。
  - `_seed` 的三个示例执行体改为声明**技能卡**（原来是 `["handoff","artifact.read","artifact.write"]`，
    后半两个是授权范围，收窄后会全部落空）。
- `contracts.py`：`CapabilityCard`、`AgentExecutor`、`TaskCandidate`、`TaskCandidates`、`CapabilityPackage`；
  `AgentRegister.capability_cards`/`executor`（非法技能名 422 `capability_skill_invalid`）、`Agent` 新字段、`CapabilityCatalog.packages`。
- `main.py`：`GET /api/tasks/{task_id}/candidates`（`project.view`，走既有中间件解析项目）；
  `GET /api/team/capabilities` 显式回填 `packages`（该 handler 是逐字段构造响应，漏了字段不会报错但会静默丢失——踩过）。

**Agent 侧（可选，未重建桌面端）**
- `agentd.py`：`register --capability-card`（内联 JSON / `@文件` / `技能@版本` 简写，可重复）、`--executor-kind`/`--executor-version`；
  解析失败**报错而不是静默丢弃**。桌面端 sidecar 未重建 —— 老内核照旧工作，包身份由设备探测值推断，效果相同。

**前端**
- `/team` 能力目录：技能芯片带 `@版本`、`详情` 展开看能力卡的输入/产出、授权范围单列、
  "执行体程序包（同一执行体多实例）"聚合、"没人能跑"旁边给候选（同名机器合并显示）。
- 工作区任务板：每行 `推荐执行体` → 展开候选面板（推荐 + 理由 + 「派给 TA」）。
- `lib/api.ts`：`CapabilityCard`/`TaskCandidate`/`TaskCandidates`/`CapabilityPackage` 类型 + `getTaskCandidates`。

**部署与验证脚本**
- `scripts/deploy/_aip1_enrich_agents.py`（新，幂等）：给**示例**执行体补能力卡（只碰 `agent-demo-*`/`agent-mira`/`agent-aster`/`agent-nova`，
  不动真实接入的机器）。上线后已在服务器执行：5 个示例执行体补卡完成。
- `scripts/deploy/_aip1_verify.py`（新）：线上走真实 store 代码路径验证版本下限/大小写归一化/候选排序/调度器一致性，跑完自清理临时任务。

## 5 验收（全部实测）

| 项 | 结果 |
| --- | --- |
| 后端全量 | **483 项**（+12 `test_skill_match` / +18 `test_agent_description`），仅 2 项既有 LaTeX 环境失败 |
| Agent 套件 | **298 项（9 skipped）**（+5 `test_capability_cards`） |
| 前端 | `tsc --noEmit` 干净、`next build` **25 页**；SSR 不变量 **19×19×6** |
| 浏览器实机（本地） | `/team` 技能芯片 `python@3.12`、详情展开出"输入 data_profile · 产出 model_spec、code"与授权范围分栏；任务板 `推荐执行体` 展开候选面板 |
| 线上发布 | `server_release.sh` 成功；`server_verify.sh` **22/22** |
| 线上功能 | 注册探针账号读 `/api/team/capabilities`：6 个执行体带版本技能、`unmet` 正常；`/api/tasks/{id}/candidates` 返回 6 候选带理由；探针账号**已清理**（含会话与成员关系） |
| 线上版本语义 | `_aip1_verify.py` 四项全 PASS：`Latex@>=0.5` 判"没人能跑"（Nova 声明 latex 未带版本）、`Markdown@>=0.5` 判"有 `markdown@1.0` 的机器能跑"、推荐排序带样本量、调度器与推荐一致 |

**顺带修掉的界面坑**：候选面板最初被塞进任务行的 flex 布局里，把标题挤成竖排（截图发现）→
`.list-row.is-task { flex-wrap: wrap }` + `.task-candidates { flex: 1 1 100% }` 让面板独占一行。

## 6 本期待观察 / 未做

- **桌面端安装包未重建**：`agentd` 的新参数要等下次封包才对已装用户生效（不影响功能，只是包身份从"上报"变成"探测推断"）。
- `/team` 里同一台机器名重复（演示验收遗留的多个 `Demo 工作站`）会让候选列表看起来啰嗦，前端已按展示名合并，但数据本身没清。
- 能力卡的 `inputs`/`outputs` 目前**只用于展示**，没有参与匹配（按计划，匹配只看技能名 + 版本）。
- 「未登记技能」目前不做区分显示（计划 §3.4 的 registry 未实现）——技能名不做硬校验，只做归一化与字符集校验；
  要不要引入"登记表"等第 2 批（AIP-1d）之后按需再定。