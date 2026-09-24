-- 024_agent_conversations.sql
-- 「我的智能体」的「对话」（MY-AGENT M-1，见 docs/MY_AGENT_CONSOLE_PLAN.md）。
--
-- 用户拍板：对话分两类——「项目工作」跑任务、「对话」是**单纯对话**。所以这套表与任务体系完全分开：
-- 不建 Task、不进任务板、不走复核，也不产生 Run/成果物。一轮对话 = 一行 agent_turns（用户消息 + 回复），
-- 执行中的过程事件另存 agent_turn_events（供页面流式渲染）。
--
-- 语义要点：
--   * 会话绑「成员 + 设备 + 项目」：设备决定谁跑（执行体按会话轮询取活），项目决定能力令牌与授权范围；
--   * session_key 是**执行体侧的会话句柄**（opencode 的 ses_…）：平台不拼历史、不存模型 key，
--     多轮上下文由执行体维护，平台只负责把句柄带回去（实测跨进程有效）；
--   * turn.status：PENDING（待领取）→ CLAIMED（已领取，带租约）→ DONE / FAILED / CANCELLED；
--     租约过期可被重新领取，避免执行体崩了把会话卡死；
--   * agent_turn_events.sequence 是幂等键：重传同一序号不产生重复卡片。

CREATE TABLE IF NOT EXISTS agent_conversations (
    id text PRIMARY KEY,
    member_id text NOT NULL REFERENCES human_members(id),
    project_id uuid NOT NULL REFERENCES projects(id),
    device_id text NOT NULL REFERENCES devices(device_id),
    agent_id text REFERENCES agents(agent_id),
    title text NOT NULL DEFAULT '',
    model text NOT NULL DEFAULT '',
    command_template jsonb NOT NULL DEFAULT '[]'::jsonb,
    session_key text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    archived_at timestamptz
);

CREATE INDEX IF NOT EXISTS agent_conversations_member_idx
    ON agent_conversations (member_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_turns (
    id text PRIMARY KEY,
    conversation_id text NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE,
    seq integer NOT NULL,
    status text NOT NULL CHECK (status IN ('PENDING', 'CLAIMED', 'DONE', 'FAILED', 'CANCELLED')),
    prompt text NOT NULL DEFAULT '',
    content text NOT NULL DEFAULT '',
    model text NOT NULL DEFAULT '',
    session_key text,
    usage jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text NOT NULL DEFAULT '',
    claimed_by text,
    claim_expires_at timestamptz,
    created_at timestamptz NOT NULL,
    claimed_at timestamptz,
    completed_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS agent_turns_conversation_seq_idx
    ON agent_turns (conversation_id, seq);
CREATE INDEX IF NOT EXISTS agent_turns_pending_idx
    ON agent_turns (status, created_at);

CREATE TABLE IF NOT EXISTS agent_turn_events (
    id text PRIMARY KEY,
    turn_id text NOT NULL REFERENCES agent_turns(id) ON DELETE CASCADE,
    sequence integer NOT NULL,
    event_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS agent_turn_events_turn_sequence_idx
    ON agent_turn_events (turn_id, sequence);