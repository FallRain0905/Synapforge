-- 028_agent_chat_variants.sql
-- 会话级推理强度：会话保存下一轮设置，轮次冻结实际值；执行体据此插入 `--variant`。
-- 当前 opencode serve 会静默忽略 message.model.variant，因此非默认值由内核明确走 CLI 通道。

ALTER TABLE agent_conversations ADD COLUMN IF NOT EXISTS variant text NOT NULL DEFAULT '';
ALTER TABLE agent_turns ADD COLUMN IF NOT EXISTS variant text NOT NULL DEFAULT '';
