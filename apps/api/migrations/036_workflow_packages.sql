-- 036_workflow_packages.sql
-- 通用垂直工作流包（W3.1，实施计划第五期；D4 冻结：定义存 JSON 列，校验靠服务端）。
--
-- 三张表：
--   * workflows             工作流包本体（key 全局唯一稳定不变；current_version_id 指向已发布版本）
--   * workflow_versions     版本，**一经发布只读**（追加式，永不 UPDATE 定义）——
--                           运行绑定 workflow_version_id，模板后续修改不改写历史运行的解释
--                           （规划 §5.4 硬要求；WORKFLOW_SCHEMA §1 规则 2）
--   * project_workflow_runs 运行实例：项目 + 冻结版本 + 输入；节点实例化就是普通任务
--                           （depends_on → 任务依赖，见 WORKFLOW_SCHEMA §5 映射表），
--                           因此不需要独立的节点实例表——Task/Run/Artifact 沿用既有对象
--
-- 定义本身不落独立列：stages/nodes/role_bindings/gate_policies/handoff_contracts/
-- delivery_adapters 全在 definition JSON 里，形状由 docs/WORKFLOW_SCHEMA.md v1 权威定义，
-- 服务端校验清单见同文档 §4（apps/api/app/workflow_service.py 实现）。
--
-- RLS 与 033 同口径：三张表都带 organization_id，严格租户隔离。

CREATE TABLE IF NOT EXISTS workflows (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    key text NOT NULL,
    name text NOT NULL,
    description text NOT NULL DEFAULT '',
    current_version_id text,
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS workflows_key_idx ON workflows(key);

CREATE TABLE IF NOT EXISTS workflow_versions (
    id text PRIMARY KEY,
    workflow_id text NOT NULL REFERENCES workflows(id),
    version integer NOT NULL,
    definition jsonb NOT NULL,
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS workflow_versions_workflow_version_idx
    ON workflow_versions(workflow_id, version);

CREATE TABLE IF NOT EXISTS project_workflow_runs (
    id text PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id),
    organization_id text NOT NULL,
    workflow_id text NOT NULL,
    workflow_version_id text NOT NULL,
    status text NOT NULL DEFAULT 'RUNNING',
    inputs jsonb NOT NULL DEFAULT '{}',
    created_by text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS project_workflow_runs_project_idx
    ON project_workflow_runs(project_id, created_at DESC);

ALTER TABLE workflows ENABLE ROW LEVEL SECURITY;
CREATE POLICY workflows_tenant_policy ON workflows
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

ALTER TABLE workflow_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY workflow_versions_tenant_policy ON workflow_versions
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

ALTER TABLE project_workflow_runs ENABLE ROW LEVEL SECURITY;
CREATE POLICY project_workflow_runs_tenant_policy ON project_workflow_runs
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
