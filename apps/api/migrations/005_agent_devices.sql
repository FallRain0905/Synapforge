-- Stage 3 P3-01: device identity, pairing, scoped project credentials and
-- persisted connection metadata. Device secrets are stored as hashes only.
CREATE TABLE IF NOT EXISTS devices (
    device_id text PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    agent_id text NOT NULL REFERENCES agents(agent_id),
    owner_member_id text NOT NULL REFERENCES human_members(id),
    device_name text NOT NULL,
    public_key text NOT NULL,
    public_key_fingerprint text NOT NULL UNIQUE,
    device_token_hash text NOT NULL UNIQUE,
    platform text NOT NULL,
    agent_version text NOT NULL,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked')),
    created_at timestamptz NOT NULL,
    last_seen timestamptz,
    revoked_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_devices_organization ON devices(organization_id, created_at DESC);

CREATE TABLE IF NOT EXISTS device_pairings (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    created_by text NOT NULL REFERENCES human_members(id),
    code_hash text NOT NULL UNIQUE,
    challenge_hash text NOT NULL,
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'CONSUMED', 'EXPIRED', 'REVOKED')),
    expires_at timestamptz NOT NULL,
    device_id text REFERENCES devices(device_id),
    consumed_at timestamptz,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_pairings_pending ON device_pairings(status, expires_at);

CREATE TABLE IF NOT EXISTS device_project_grants (
    id uuid PRIMARY KEY,
    device_id text NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    agent_id text NOT NULL REFERENCES agents(agent_id),
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    token_hash text NOT NULL UNIQUE,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    granted_by text NOT NULL REFERENCES human_members(id),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_project_grants_scope ON device_project_grants(device_id, project_id, expires_at);

CREATE TABLE IF NOT EXISTS agent_connections (
    connection_id text PRIMARY KEY,
    device_id text NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    agent_id text NOT NULL REFERENCES agents(agent_id),
    session_id text NOT NULL,
    transport text NOT NULL CHECK (transport IN ('websocket', 'long_poll')),
    status text NOT NULL CHECK (status IN ('CONNECTING', 'CONNECTED', 'DISCONNECTED', 'REVOKED')),
    last_received_sequence bigint NOT NULL DEFAULT 0 CHECK (last_received_sequence >= 0),
    last_sent_sequence bigint NOT NULL DEFAULT 0 CHECK (last_sent_sequence >= 0),
    connected_at timestamptz NOT NULL,
    last_heartbeat_at timestamptz NOT NULL,
    disconnected_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_agent_connections_device ON agent_connections(device_id, status);

ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_pairings ENABLE ROW LEVEL SECURITY;
ALTER TABLE device_project_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_connections ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'devices' AND policyname = 'devices_tenant_policy') THEN
        CREATE POLICY devices_tenant_policy ON devices
            USING (organization_id = app.current_organization_id())
            WITH CHECK (organization_id = app.current_organization_id());
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'device_pairings' AND policyname = 'device_pairings_tenant_policy') THEN
        CREATE POLICY device_pairings_tenant_policy ON device_pairings
            USING (organization_id = app.current_organization_id())
            WITH CHECK (organization_id = app.current_organization_id());
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'device_project_grants' AND policyname = 'device_project_grants_tenant_policy') THEN
        CREATE POLICY device_project_grants_tenant_policy ON device_project_grants
            USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = device_project_grants.project_id AND p.organization_id = app.current_organization_id()))
            WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = device_project_grants.project_id AND p.organization_id = app.current_organization_id()));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'agent_connections' AND policyname = 'agent_connections_tenant_policy') THEN
        CREATE POLICY agent_connections_tenant_policy ON agent_connections
            USING (EXISTS (SELECT 1 FROM devices d WHERE d.device_id = agent_connections.device_id AND d.organization_id = app.current_organization_id()))
            WITH CHECK (EXISTS (SELECT 1 FROM devices d WHERE d.device_id = agent_connections.device_id AND d.organization_id = app.current_organization_id()));
    END IF;
END
$$;
