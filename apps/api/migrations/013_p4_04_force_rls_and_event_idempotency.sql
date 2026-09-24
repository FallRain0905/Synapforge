-- P4-04: make tenant isolation effective for the application database role.
-- ENABLE RLS alone still lets a table owner bypass policies.  The repository
-- runs as the schema owner in the development deployment, so FORCE is part of
-- the acceptance contract until a dedicated non-owner runtime role exists.

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'organizations', 'teams', 'human_members', 'memberships', 'projects',
        'project_memberships', 'agents', 'agent_project_grants',
        'git_repositories', 'git_file_indexes', 'sessions', 'invitations',
        'tasks', 'handoffs', 'runs', 'artifacts', 'artifact_multipart_uploads',
        'reviews', 'gates', 'evidence', 'events', 'event_outbox', 'task_leases',
        'idempotency_records', 'devices', 'device_pairings',
        'device_project_grants', 'agent_connections', 'gateway_command_results',
        'device_token_rotations', 'handoff_receipts', 'risks'
    ] LOOP
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', table_name);
    END LOOP;
END
$$;

-- These older tables predate the first RLS migration or do not carry an
-- organization_id directly.  Add their tenant predicates before forcing RLS.
ALTER TABLE agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE idempotency_records
    ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(id) ON DELETE CASCADE;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = current_schema() AND tablename = 'agents' AND policyname = 'agents_tenant_policy'
    ) THEN
        CREATE POLICY agents_tenant_policy ON agents
            USING (EXISTS (
                SELECT 1 FROM human_members m
                WHERE m.id = agents.owner_member_id AND m.organization_id = app.current_organization_id()
            ))
            WITH CHECK (EXISTS (
                SELECT 1 FROM human_members m
                WHERE m.id = agents.owner_member_id AND m.organization_id = app.current_organization_id()
            ));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = current_schema() AND tablename = 'sessions' AND policyname = 'sessions_tenant_policy'
    ) THEN
        CREATE POLICY sessions_tenant_policy ON sessions
            USING (EXISTS (
                SELECT 1 FROM human_members m
                WHERE m.id = sessions.member_id AND m.organization_id = app.current_organization_id()
            ))
            WITH CHECK (EXISTS (
                SELECT 1 FROM human_members m
                WHERE m.id = sessions.member_id AND m.organization_id = app.current_organization_id()
            ));
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = current_schema() AND tablename = 'idempotency_records' AND policyname = 'idempotency_records_tenant_policy'
    ) THEN
        CREATE POLICY idempotency_records_tenant_policy ON idempotency_records
            USING (organization_id = app.current_organization_id())
            WITH CHECK (organization_id = app.current_organization_id());
    END IF;
END
$$;

-- Existing unscoped idempotency rows cannot be safely assigned to a tenant.
-- They remain archived behind NULL and are ignored by org-scoped repositories.
CREATE INDEX IF NOT EXISTS idx_idempotency_records_organization
    ON idempotency_records(organization_id, key);

-- Event idempotency is project-scoped at the API boundary.  The partial index
-- prevents two concurrent workers from publishing the same keyed event.
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_project_idempotency
    ON events(project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
