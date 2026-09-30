-- 037_workflow_engine.sql
-- 工作流推进器（W3.2，实施计划第五期）的两列：
--
--   * node_tasks：node id → task id 的实例化映射（start_workflow_run 物化时写入）。
--     推进器靠它把"定义里的节点"对应到"平台里的任务"；没有它，每次推进都要
--     反查任务标题猜映射——那是猜，不是状态。
--   * ledger：推进账本（Magentic-One 语义的确定性子集：round/stall_count/needs_replan
--     + 每节点尝试序号）。每轮 advance 后快照写回，账本不可变对象按轮整体替换。
--
-- 两列都是 JSON；形状由 workflow_engine 权威（docs/WORKFLOW_SCHEMA.md §5 的运行侧延伸）。

ALTER TABLE project_workflow_runs ADD COLUMN IF NOT EXISTS node_tasks jsonb NOT NULL DEFAULT '{}';
ALTER TABLE project_workflow_runs ADD COLUMN IF NOT EXISTS ledger jsonb NOT NULL DEFAULT '{}';
