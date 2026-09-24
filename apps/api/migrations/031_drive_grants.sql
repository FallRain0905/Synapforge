-- 031_drive_grants.sql
-- 个人云盘 → Agent 的**文件访问授权**（FM-5）：Grant / Grant Node / Lease 三张表。
--
-- 为什么必须单独一套授权（而不是复用项目能力令牌）：项目令牌回答的是"这台设备能不能替这个项目干活"，
-- 不回答"它能读我云盘里的哪些文件"。云盘是**成员私有**资产，Agent 默认只该看到被显式授权的那部分。
--
-- 已拍板的口径（计划 §1 D1/D2）：
--   * Agent 默认能力只有 `drive.metadata.read` / `drive.file.read` / `drive.file.import`——
--     **没有**删除、移动、改名、覆盖；
--   * 授权粒度：单个文件 / 某个文件夹 / 用户明确选择的整盘；
--   * 授权必须绑定：所有者、Agent、设备、项目，以及可选的 任务/Run/对话轮次；带过期时间与撤销版本；
--   * 文件夹授权默认只覆盖**授权当时已有**的节点；用户明确勾选"包含以后新增"才动态覆盖。
--
-- 撤销的联动靠 `revocation_epoch`：撤销时把 epoch +1（不是删行），所有校验都比对 epoch；
-- 设备撤销 / 成员被移出项目 / Token 轮换都会推高 epoch，于是"撤销后新读取立即失败"是自然结果。
--
-- Lease 是**短期**访问凭证：明文只在签发时返回一次，库里只存哈希（与设备令牌同一口径，永不可回读）。

CREATE TABLE IF NOT EXISTS file_access_grants (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    owner_member_id text NOT NULL,
    agent_id text NOT NULL,
    device_id text,
    project_id text NOT NULL,
    task_id text,
    run_id text,
    conversation_id text,
    turn_id text,
    scope_type text NOT NULL,
    root_node_id text,
    include_future_nodes boolean NOT NULL DEFAULT false,
    capabilities text NOT NULL DEFAULT '[]',
    expires_at timestamptz NOT NULL,
    revocation_epoch integer NOT NULL DEFAULT 1,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoked_by text,
    revoke_reason text,
    renewed_at timestamptz
);
CREATE INDEX IF NOT EXISTS file_access_grants_owner_idx ON file_access_grants(owner_member_id, created_at);
CREATE INDEX IF NOT EXISTS file_access_grants_agent_idx ON file_access_grants(agent_id, expires_at);

CREATE TABLE IF NOT EXISTS file_access_grant_nodes (
    id text PRIMARY KEY,
    grant_id text NOT NULL REFERENCES file_access_grants(id),
    drive_node_id text NOT NULL,
    content_hash text,
    revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS file_access_grant_nodes_unique_idx
    ON file_access_grant_nodes(grant_id, drive_node_id);
CREATE INDEX IF NOT EXISTS file_access_grant_nodes_node_idx ON file_access_grant_nodes(drive_node_id);

CREATE TABLE IF NOT EXISTS file_access_leases (
    id text PRIMARY KEY,
    organization_id text NOT NULL,
    grant_id text NOT NULL REFERENCES file_access_grants(id),
    agent_id text NOT NULL,
    device_id text,
    project_id text NOT NULL,
    run_id text,
    token_hash text NOT NULL,
    revocation_epoch integer NOT NULL DEFAULT 1,
    expires_at timestamptz NOT NULL,
    used_at timestamptz,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS file_access_leases_token_idx ON file_access_leases(token_hash);
CREATE INDEX IF NOT EXISTS file_access_leases_grant_idx ON file_access_leases(grant_id, expires_at);

ALTER TABLE file_access_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE file_access_grant_nodes ENABLE ROW LEVEL SECURITY;
ALTER TABLE file_access_leases ENABLE ROW LEVEL SECURITY;

CREATE POLICY file_access_grants_tenant_policy ON file_access_grants
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
CREATE POLICY file_access_grant_nodes_tenant_policy ON file_access_grant_nodes
    USING (EXISTS (SELECT 1 FROM file_access_grants g WHERE g.id = file_access_grant_nodes.grant_id))
    WITH CHECK (EXISTS (SELECT 1 FROM file_access_grants g WHERE g.id = file_access_grant_nodes.grant_id));
CREATE POLICY file_access_leases_tenant_policy ON file_access_leases
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());