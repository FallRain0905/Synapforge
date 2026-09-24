-- 020_project_workspace.sql
-- 项目工作区（W-1）：项目立项目标与人数、任务推进模式、以及项目内聊天/事件卡片流。
--
-- 背景：平台后端能力已完整（派单、能力匹配、事件广播、模板包），但页面多而散，
-- 没有"主工作区域"。设计见 docs/PROJECT_WORKSPACE_DESIGN.md：
--   * 中间是项目聊天流——人类消息落 project_messages；Agent 的动作由服务端从既有
--     事件派生为卡片（引用 ref_event_id，不复制内容），Agent 侧协议零改动。
--   * 右侧是成员概览（读时聚合，不落库）。
--
-- 语义：
--   * goal / target_member_count：立项信息。target_member_count 只作建队参考，不做加入拦截。
--   * task_mode：manual（默认，队长派单）/ hybrid（允许成员认领）/ auto（模板全自动推进，
--     opt-in，队长可随时暂停）。W-1 只落字段与界面，auto 调度在 W-3。
--   * project_messages.seq：项目内单调递增，聊天分页游标（与 events.sequence 各自独立）。
--   * sender_name：写入时快照的展示名（Agent 改名或成员更名后历史消息仍可读）。
--   * 队长角色沿用既有 project_memberships.role = 'project_lead'，不新增枚举值。

ALTER TABLE projects ADD COLUMN IF NOT EXISTS goal text;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS target_member_count integer;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS task_mode text NOT NULL DEFAULT 'manual';

CREATE TABLE IF NOT EXISTS project_messages (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    seq bigint NOT NULL,
    sender_kind text NOT NULL,
    sender_member_id text REFERENCES human_members(id),
    sender_agent_id text,
    sender_name text NOT NULL DEFAULT '',
    content text NOT NULL,
    message_type text NOT NULL DEFAULT 'text',
    ref_event_id bigint,
    ref_artifact_id uuid,
    ref_task_id uuid,
    created_at timestamptz NOT NULL,
    UNIQUE (project_id, seq)
);

CREATE INDEX IF NOT EXISTS project_messages_project_seq_idx ON project_messages (project_id, seq DESC);

-- 聊天桥水位线：记录"某项目的事件已检查到第几号"。按已检查序号推进（而不是最后一张卡片的
-- 序号），这样中间隔着大量非卡事件也不会漏扫。表很小，一个项目一行。
CREATE TABLE IF NOT EXISTS project_chat_watermarks (
    project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    last_event_sequence bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL
);