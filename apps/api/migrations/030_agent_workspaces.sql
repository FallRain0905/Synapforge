-- 030_agent_workspaces.sql
-- Agent 工作区文件服务（FM-3）：把"Agent 的工作目录"变成平台能安全浏览/操作的东西。
--
-- 三条边界（计划 §4/§6.4/§7.1）：
--   1. 平台**不**入站连接任何人家的电脑：所有文件操作由 Agent **主动领取**（下面的
--      `workspace_operations` 就是队列），平台只负责入队、路由与记账；
--   2. 工作区路径**只用相对路径**，且每次都回到 Agent 侧的 WorkspacePolicy 重新校验；
--      `agent_workspaces.workspace_identity` 是**路径的哈希**（不是绝对路径）——平台不持有、
--      也不展示宿主机绝对路径（与 FM-0 的 `path_privacy` 同一口径）；
--   3. 大文件不进 WebSocket/Gateway 帧：走 `file_transfer_sessions` + 对象存储。
--
-- 与 Gateway 的关系：**零改动**。Gateway 的命令列表（11 种）是冻结的，文件操作走这一套独立的、
-- 版本化的轮询契约（`/api/agent/workspace-operations/*`）。
--
-- 幂等：同一个 `idempotency_key` 且 `request_hash` 相同 → 返回既有操作；同 key 不同 hash → 409
-- （"同 key 不同内容必须冲突"，安全要求第 12 条）。完成后重复提交（complete）只认第一次。

CREATE TABLE IF NOT EXISTS agent_workspaces (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    agent_id text NOT NULL,
    device_id text,
    project_id text,
    display_name text NOT NULL,
    -- 相对工作区根**没有绝对路径**：平台存的是路径的 sha256 前缀（FM-0 的 ws-xxxxxxxxxxxx）
    workspace_identity text NOT NULL,
    kind text NOT NULL DEFAULT 'desktop',
    status text NOT NULL DEFAULT 'offline',
    policy_version text NOT NULL DEFAULT '1',
    -- Agent 自报的保护目录（相对路径），删除/移动这些目录会被拒绝
    protected_paths text NOT NULL DEFAULT '[]',
    last_seen_at timestamptz,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_workspaces_identity_idx
    ON agent_workspaces(agent_id, workspace_identity);
CREATE INDEX IF NOT EXISTS agent_workspaces_org_idx ON agent_workspaces(organization_id, status);

CREATE TABLE IF NOT EXISTS workspace_operations (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    workspace_id text NOT NULL REFERENCES agent_workspaces(id),
    project_id text,
    requested_by_member_id text NOT NULL,
    agent_id text NOT NULL,
    device_id text,
    operation_type text NOT NULL,
    relative_path text NOT NULL DEFAULT '',
    arguments text NOT NULL DEFAULT '{}',
    expected_revision text,
    status text NOT NULL DEFAULT 'queued',
    idempotency_key text NOT NULL,
    request_hash text NOT NULL,
    claimed_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    heartbeat_at timestamptz,
    result text,
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS workspace_operations_idempotency_idx
    ON workspace_operations(workspace_id, idempotency_key);
CREATE INDEX IF NOT EXISTS workspace_operations_queue_idx
    ON workspace_operations(workspace_id, status, created_at);
CREATE INDEX IF NOT EXISTS workspace_operations_agent_idx
    ON workspace_operations(agent_id, status, created_at);

CREATE TABLE IF NOT EXISTS file_transfer_sessions (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    operation_id text,
    workspace_id text,
    source_type text NOT NULL,
    source_id text,
    source_hash text,
    target_type text NOT NULL,
    target_id text,
    storage_key text NOT NULL,
    expected_size bigint NOT NULL DEFAULT 0,
    expected_hash text,
    uploaded_size bigint NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'initialized',
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS file_transfer_sessions_status_idx ON file_transfer_sessions(status, expires_at);
CREATE INDEX IF NOT EXISTS file_transfer_sessions_operation_idx ON file_transfer_sessions(operation_id);

CREATE TABLE IF NOT EXISTS workspace_audit (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    workspace_id text,
    operation_id text,
    member_id text,
    agent_id text,
    action text NOT NULL,
    relative_path_hash text,
    capability text NOT NULL DEFAULT '',
    decision text NOT NULL DEFAULT 'allow',
    reason text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS workspace_audit_workspace_idx ON workspace_audit(workspace_id, created_at);

ALTER TABLE agent_workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE file_transfer_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE workspace_audit ENABLE ROW LEVEL SECURITY;

CREATE POLICY agent_workspaces_tenant_policy ON agent_workspaces
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
CREATE POLICY workspace_operations_tenant_policy ON workspace_operations
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
CREATE POLICY file_transfer_sessions_tenant_policy ON file_transfer_sessions
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
CREATE POLICY workspace_audit_tenant_policy ON workspace_audit
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());