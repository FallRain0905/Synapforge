-- 038_coordination_storage.sql
-- 协作协议存储（D1，多 Agent 协作开发计划；协议形状权威 apps/api/app/coordination.py，D0 冻结）。
--
-- 三张表：
--   * stage_reports            阶段报告：执行 Agent 的结构化交付材料。"报告不是业务状态
--                              本身"——它进入接收方审核，不直接当批准结论（计划 §阶段 4）。
--   * information_requests     运行时信息请求：Agent 执行中主动向同伴请求上下文的正式
--                              消息（计划 §6.5）。状态生命周期由 coordination.TRANSITIONS
--                              的 information_request 表约束；临时依赖不改已发布工作流定义。
--   * orchestration_decisions  编排决策：唯一 Orchestrator 的可审计记录——为什么派给谁、
--                              为什么重试/阻塞/停止（计划 §阶段 6；所有全局决策可回放）。
--
-- 三张表都带 organization_id，RLS 与 033/036 同口径。dev SQLite 由
-- workflow_engine.ensure_schema 建运行时表。

CREATE TABLE IF NOT EXISTS stage_reports (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    project_id uuid NOT NULL REFERENCES projects(id),
    task_id uuid NOT NULL REFERENCES tasks(id),
    run_id text,
    attempt integer NOT NULL DEFAULT 1,
    status text NOT NULL,
    summary text NOT NULL DEFAULT '',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS stage_reports_task_idx ON stage_reports(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS information_requests (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    project_id uuid NOT NULL REFERENCES projects(id),
    run_id text,
    requester_node_id text,
    requester_task_id uuid,
    requester_agent_id text NOT NULL,
    provider_agent_id text,
    provider_capability text,
    request_type text NOT NULL,
    question text NOT NULL,
    blocking text NOT NULL,
    reason text NOT NULL DEFAULT '',
    node_effect text NOT NULL DEFAULT '',
    redirect_count integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'OPEN',
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    correlation_id text,
    idempotency_key text,
    response_deadline timestamptz,
    responded_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS information_requests_project_idx
    ON information_requests(project_id, status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS information_requests_idem_idx
    ON information_requests(idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS orchestration_decisions (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    project_id uuid NOT NULL REFERENCES projects(id),
    run_id text,
    node_id text,
    task_id uuid,
    policy text NOT NULL,
    basis jsonb NOT NULL DEFAULT '[]'::jsonb,
    expected_events jsonb NOT NULL DEFAULT '[]'::jsonb,
    stop_reason text,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS orchestration_decisions_run_idx
    ON orchestration_decisions(project_id, created_at DESC);

ALTER TABLE stage_reports ENABLE ROW LEVEL SECURITY;
CREATE POLICY stage_reports_tenant_policy ON stage_reports
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

ALTER TABLE information_requests ENABLE ROW LEVEL SECURITY;
CREATE POLICY information_requests_tenant_policy ON information_requests
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

ALTER TABLE orchestration_decisions ENABLE ROW LEVEL SECURITY;
CREATE POLICY orchestration_decisions_tenant_policy ON orchestration_decisions
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
