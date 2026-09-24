-- 023_run_usage.sql
-- 执行用量回报（COST-1，见 docs/handoffs/AIP_1D_TASK_INTENT_HANDOFF.md §5 的"未做"第一条）：
-- 让 token 预算从"只记录"变成"可判定"。
--
-- 背景：AIP-1d 给任务加了 `budget.max_tokens`，但平台没有任何用量采集（runs 无用量字段、
-- RunComplete 无 usage），所以那时只能诚实标注"只记录不强制"。本迁移补上**执行体回报用量**的落点。
--
-- 语义：
--   * usage：`{input_tokens, output_tokens, total_tokens, turns, seconds, seconds_source, source}`
--       - tokens 由执行体回报（codex 的 JSONL 里 `turn.completed.usage`；通用 CLI 执行体一般没有）；
--       - seconds 优先取执行体自报，缺省由平台按 started_at/completed_at 观测值兜底；
--       - source 记 **token** 的出处（`codex-jsonl` / `agent-reported` / `platform-observed`），
--         seconds_source 记 **耗时** 的出处（`agent` / `platform`）——两者不是一回事，不混成一个字段。
--   * 判定发生在**完成 Run 时**（写一次性事件）与**批准任务时**（门禁 blocking finding），
--     平台不因为超预算就自动改任务状态——那会掩盖事实，判断权留给人工门禁。

ALTER TABLE runs ADD COLUMN IF NOT EXISTS usage jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS runs_usage_tokens_idx ON runs ((usage -> 'total_tokens'));