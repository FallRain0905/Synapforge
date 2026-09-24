CREATE SCHEMA IF NOT EXISTS app;

CREATE OR REPLACE FUNCTION app.current_organization_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT NULLIF(current_setting('app.organization_id', true), '')::uuid
$$;

ALTER TABLE organizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE teams ENABLE ROW LEVEL SECURITY;
ALTER TABLE human_members ENABLE ROW LEVEL SECURITY;
ALTER TABLE memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE project_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_project_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE git_repositories ENABLE ROW LEVEL SECURITY;
ALTER TABLE git_file_indexes ENABLE ROW LEVEL SECURITY;
ALTER TABLE invitations ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE handoffs ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE artifact_multipart_uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE reviews ENABLE ROW LEVEL SECURITY;
ALTER TABLE gates ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE event_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE task_leases ENABLE ROW LEVEL SECURITY;

CREATE POLICY organizations_tenant_policy ON organizations
    USING (id = app.current_organization_id())
    WITH CHECK (id = app.current_organization_id());
CREATE POLICY teams_tenant_policy ON teams
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
CREATE POLICY human_members_tenant_policy ON human_members
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
CREATE POLICY memberships_tenant_policy ON memberships
    USING (EXISTS (SELECT 1 FROM teams t WHERE t.id = memberships.team_id AND t.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM teams t WHERE t.id = memberships.team_id AND t.organization_id = app.current_organization_id()));
CREATE POLICY projects_tenant_policy ON projects
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());
CREATE POLICY project_memberships_tenant_policy ON project_memberships
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = project_memberships.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = project_memberships.project_id AND p.organization_id = app.current_organization_id()));

CREATE POLICY agent_project_grants_tenant_policy ON agent_project_grants
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = agent_project_grants.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = agent_project_grants.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY invitations_tenant_policy ON invitations
    USING (organization_id = app.current_organization_id())
    WITH CHECK (organization_id = app.current_organization_id());

CREATE POLICY git_repositories_tenant_policy ON git_repositories
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = git_repositories.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = git_repositories.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY git_file_indexes_tenant_policy ON git_file_indexes
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = git_file_indexes.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = git_file_indexes.project_id AND p.organization_id = app.current_organization_id()));

CREATE POLICY tasks_tenant_policy ON tasks
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = tasks.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = tasks.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY handoffs_tenant_policy ON handoffs
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = handoffs.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = handoffs.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY runs_tenant_policy ON runs
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = runs.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = runs.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY artifacts_tenant_policy ON artifacts
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = artifacts.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = artifacts.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY artifact_multipart_tenant_policy ON artifact_multipart_uploads
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = artifact_multipart_uploads.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = artifact_multipart_uploads.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY reviews_tenant_policy ON reviews
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = reviews.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = reviews.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY gates_tenant_policy ON gates
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = gates.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = gates.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY evidence_tenant_policy ON evidence
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = evidence.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = evidence.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY events_tenant_policy ON events
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = events.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = events.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY event_outbox_tenant_policy ON event_outbox
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = event_outbox.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = event_outbox.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY task_leases_tenant_policy ON task_leases
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = task_leases.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = task_leases.project_id AND p.organization_id = app.current_organization_id()));
