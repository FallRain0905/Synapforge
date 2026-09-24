-- 022_task_intent.sql
-- 意图对象的两个缺项（AIP-1d，见 docs/AIP_1_PLAN.md §6）：**预算**与**显式证据要求**。
--
-- 背景：任务此前已有约束（acceptance_criteria）、时限（deadline）、信息边界（information_boundary），
--   但没有"这次最多花多少"（预算）与"必须附哪类证据才算完成"（证据要求）。
--   AIP 把这些统称"意图对象"的表达要素；本迁移只补两个字段，且**默认空值 = 零行为变化**。
--
-- 语义：
--   * budget：`{max_seconds, max_attempts, max_tokens}`
--       - max_seconds：单次执行的墙钟上限，**强制**——领取时把租约到期时间压到 min(请求值, max_seconds)；
--       - max_attempts：整条任务允许的领取次数，**强制**——领取前数 task_leases（每次领取一行）；超限拒绝领取；
--       - max_tokens：**只记录不强制**——平台没有 token 计量（runs 无 token 字段、RunComplete 无 usage），
--         写进 Run 参数供执行体参考，界面明确标注"未强制"。要真强制得先让执行体回报用量。
--   * evidence_requirements：`[{evidence_type, min_count, note}]`，evidence_type 复用既有词表
--     （artifact / run / event / external_source）。**读时**算缺口用于展示；门禁规则
--     `task:evidence_requirements` 只在字段非空时生效（不自动批准，与既有"门禁一律人工"一致）。

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS budget jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS evidence_requirements jsonb NOT NULL DEFAULT '[]'::jsonb;

-- 便于运维核对"哪些任务设了预算/证据要求"；两个字段都不参与过滤，故只建表达式索引用于排查。
CREATE INDEX IF NOT EXISTS tasks_has_budget_idx ON tasks ((budget <> '{}'::jsonb));
CREATE INDEX IF NOT EXISTS tasks_has_evidence_requirements_idx ON tasks ((evidence_requirements <> '[]'::jsonb));