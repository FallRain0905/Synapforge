-- 034_agent_turn_stop_reason.sql
-- 「我的智能体」轮次终止原因（W1.1，重新定位实施计划第一期）。
--
-- 为什么要这一列：`status` 只能说"怎么结束的"（DONE / FAILED / CANCELLED），说不清"为什么"。
-- 预算触顶、审批没人批、平台超时……这些真实结局以前全被压平成一个 DONE/FAILED，
-- 页面想如实展示（"预算触顶，请调整后重试"）没有材料。`status` 说怎么结束，`stop_reason` 说为什么：
--
--   * 平台侧可判定的：cancelled（成员停止）、completed / failed（按执行体回报的 success 兜底）；
--   * 执行体回报的：token_capped / turn_capped / timeout / permission_timeout
--     （执行体知道自己怎么停的就如实报；平台侧超时门随后续门禁落地）；
--   * 契约集合见 app/contracts.py 的 AGENT_TURN_STOP_REASONS；集合外的值平台一律归一化为
--     unknown——宁可诚实说"不知道为什么结束"，也不编一个像模像样的值。
--
-- 默认 ''：历史行不回填猜测值，读取层把空串显示为"未记录"。

ALTER TABLE agent_turns ADD COLUMN IF NOT EXISTS stop_reason text NOT NULL DEFAULT '';
