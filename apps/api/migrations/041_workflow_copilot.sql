-- 041_workflow_copilot.sql
-- 工作流 Copilot（会话式工作流搭建助手）：持久化 owner + org/project 作用域的
-- 会话 / 轮次 / 结构化决策，三张表。
--
--   * workflow_copilot_sessions  会话本体：归属成员（owner）+ 组织；可选绑定项目
--                                （绑定即做项目成员校验）。当前草稿（definition JSON）
--                                与 draft_revision 存会话行——发布/运行都以"当前修订"
--                                做过期（stale）判定；发布回执（workflow_id + revision
--                                + 定义哈希）也在会话行上，重复发布幂等。
--   * workflow_copilot_turns     轮次：一次用户输入 + 一次助手回复（含修复轮的最终
--                                内容）。草稿只落会话行；轮次保留 via/model/usage
--                                （chat_routed 计费口径）与失败错误（校验错误逐条）。
--   * workflow_copilot_decisions 结构化决策记录：synapforge-choice 的**机器可读**
--                                回答（answers JSON + 当时的问题原文），一问一答，
--                                每轮最多一条（唯一索引）。自由文本不进决策——
--                                只有服务端校验通过的结构化回答才记录。
--
-- 边界：AI 永远不能发布/运行——publish/run 只存在于需要人工 confirmed=true 的
-- HTTP 路由（main.py），本表结构不承载任何"AI 已批准"语义。
--
-- RLS 与 036/038 同口径：organization_id = app.current_organization_id()。
-- dev SQLite 由 workflow_copilot.ensure_schema 建同名表（TEXT + JSON 字符串）。

CREATE TABLE IF NOT EXISTS workflow_copilot_sessions (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    project_id uuid REFERENCES projects(id),
    member_id text NOT NULL,
    title text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'active',
    draft jsonb NOT NULL DEFAULT '{}'::jsonb,
    draft_revision integer NOT NULL DEFAULT 0,
    draft_hash text NOT NULL DEFAULT '',
    draft_valid integer NOT NULL DEFAULT 0,
    published_workflow_id text,
    published_revision integer NOT NULL DEFAULT 0,
    published_hash text NOT NULL DEFAULT '',
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS workflow_copilot_sessions_member_idx
    ON workflow_copilot_sessions(organization_id, member_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS workflow_copilot_sessions_project_idx
    ON workflow_copilot_sessions(project_id, updated_at DESC) WHERE project_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS workflow_copilot_turns (
    id text PRIMARY KEY,
    session_id text NOT NULL REFERENCES workflow_copilot_sessions(id),
    organization_id text NOT NULL,
    seq integer NOT NULL,
    status text NOT NULL DEFAULT 'running',
    prompt text NOT NULL DEFAULT '',
    content text NOT NULL DEFAULT '',
    model text NOT NULL DEFAULT '',
    via text NOT NULL DEFAULT '',
    usage jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text NOT NULL DEFAULT '',
    errors jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS workflow_copilot_turns_session_idx
    ON workflow_copilot_turns(session_id, seq);

CREATE TABLE IF NOT EXISTS workflow_copilot_decisions (
    id text PRIMARY KEY,
    session_id text NOT NULL REFERENCES workflow_copilot_sessions(id),
    turn_id text NOT NULL REFERENCES workflow_copilot_turns(id),
    organization_id text NOT NULL,
    choice_id text NOT NULL DEFAULT '',
    answers jsonb NOT NULL DEFAULT '{}'::jsonb,
    questions jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS workflow_copilot_decisions_turn_idx
    ON workflow_copilot_decisions(turn_id);

ALTER TABLE workflow_copilot_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY workflow_copilot_sessions_tenant_policy ON workflow_copilot_sessions
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

ALTER TABLE workflow_copilot_turns ENABLE ROW LEVEL SECURITY;
CREATE POLICY workflow_copilot_turns_tenant_policy ON workflow_copilot_turns
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

ALTER TABLE workflow_copilot_decisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY workflow_copilot_decisions_tenant_policy ON workflow_copilot_decisions
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
