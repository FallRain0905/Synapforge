-- 018_task_dispatch.sql
-- 派单模式（P1-1）：任务可以指派给某个成员，领取时按成员过滤。
--
-- 语义：
--   * assignee_member_id 为 NULL  → 谁先轮到谁跑（保持原有拉取语义）
--   * assignee_member_id 有值     → 只有该成员名下的设备/Agent 能领取
--   * 原有的 assignee 文本列保留为"执行者标注"（领取时平台写 agent_id），
--     指派信息只认 assignee_member_id，不再用自由文本表达指派关系。
--
-- 不加外键约束：与其余"归属类"列保持一致（human_members 删除是不允许的，
-- member-001 也保留为 suspended），校验放在应用层（必须是项目成员）。

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assignee_member_id text;

CREATE INDEX IF NOT EXISTS tasks_assignee_member_idx ON tasks (assignee_member_id);