-- 025_agent_chat_roles.sql
-- 「我的智能体」的角色（MY-AGENT M-6，见 docs/MY_AGENT_M6_AGENT_ROLES_EXECUTION_PLAN.md）。
--
-- 背景：执行体（opencode）支持**自定义 agent**——一个 md 文件就是一个角色（frontmatter 写说明与
-- 工具开关，正文就是系统提示），`opencode run --agent <名>` 选中它。平台这一侧只需要记住"这个会话
-- 选了哪个角色"，**不存提示词正文**（提示词只存在仓库 `deploy/cloud-agent/roles/*.md`，单一真源，
-- 避免"平台改一版、执行体跑另一版"）。
--
-- 两列的分工（都不可省）：
--   * agent_conversations.role —— 会话设置（空 = 默认，不传 `--agent`）；中途可改，**只影响下一轮**；
--   * agent_turns.role        —— 这一轮**实际**用的角色，建轮次时从会话抄下来。
--     分开记的原因：会话上换了角色之后，历史里"上一轮是谁跑的"必须仍然是真的，不能被新设置覆盖。
--
-- 名字的合法性不在平台校验：可选角色来自执行体心跳上报的 `resource_summary.roles`
-- （由 `opencode agent list` 真探测得到）。名字在那边不存在时，opencode 自己会报错，
-- 内核把退出码与错误如实回传到该轮次的 FAILED——平台不猜、也不假装成功。

ALTER TABLE agent_conversations ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT '';
ALTER TABLE agent_turns ADD COLUMN IF NOT EXISTS role text NOT NULL DEFAULT '';