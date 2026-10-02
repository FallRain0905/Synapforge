-- 040_feasibility_concerns.sql
-- 可行性异议（D3 收口，多 Agent 协作计划 §6.5.2）：Agent 质疑前提/目标不可行的
-- 结构化报告——协议形状权威 apps/api/app/coordination.py（validate_feasibility_concern）。
-- "无法完成"是有效交付事实：异议保留证据、进入 Orchestrator 决策（DECIDED 后原报告
-- 仍在 payload 里，否定结论也记录依据），不删除、不覆盖、不伪装成完成。

CREATE TABLE IF NOT EXISTS feasibility_concerns (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    project_id uuid NOT NULL REFERENCES projects(id),
    run_id text,
    node_id text,
    task_id uuid,
    reporter_agent_id text NOT NULL,
    concern_type text NOT NULL,
    claim text NOT NULL,
    status text NOT NULL DEFAULT 'NEEDS_DECISION',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS feasibility_concerns_project_idx
    ON feasibility_concerns(project_id, status, created_at DESC);

ALTER TABLE feasibility_concerns ENABLE ROW LEVEL SECURITY;
CREATE POLICY feasibility_concerns_tenant_policy ON feasibility_concerns
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
