CREATE TABLE IF NOT EXISTS organizations (
    id uuid PRIMARY KEY,
    name text NOT NULL,
    slug text NOT NULL UNIQUE,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS teams (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    name text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS human_members (
    id text PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    team_id uuid REFERENCES teams(id),
    email text NOT NULL UNIQUE,
    display_name text NOT NULL,
    status text NOT NULL CHECK (status IN ('active', 'invited', 'suspended')),
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS memberships (
    member_id text NOT NULL REFERENCES human_members(id),
    team_id uuid NOT NULL REFERENCES teams(id),
    role text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (member_id, team_id)
);

CREATE TABLE IF NOT EXISTS projects (
    id uuid PRIMARY KEY,
    organization_id uuid REFERENCES organizations(id),
    team_id uuid REFERENCES teams(id),
    created_by text NOT NULL DEFAULT 'member-001',
    name text NOT NULL,
    competition_pack text NOT NULL,
    problem_code text,
    description text NOT NULL,
    stage text NOT NULL,
    progress integer NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 100),
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS project_memberships (
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    member_id text NOT NULL REFERENCES human_members(id),
    role text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (project_id, member_id)
);

CREATE TABLE IF NOT EXISTS agents (
    agent_id text PRIMARY KEY,
    display_name text NOT NULL,
    owner_member_id text NOT NULL,
    model_provider text NOT NULL,
    model_name text NOT NULL,
    supported_tools jsonb NOT NULL,
    supported_languages jsonb NOT NULL,
    max_concurrency integer NOT NULL CHECK (max_concurrency > 0),
    local_workspace text,
    network_policy text NOT NULL,
    status text NOT NULL,
    last_seen timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_project_grants (
    agent_id text NOT NULL REFERENCES agents(agent_id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    granted_by text NOT NULL,
    created_at timestamptz NOT NULL,
    PRIMARY KEY (agent_id, project_id)
);

CREATE TABLE IF NOT EXISTS git_repositories (
    project_id uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    provider text NOT NULL,
    remote_url text,
    local_path text NOT NULL
);

CREATE TABLE IF NOT EXISTS git_file_indexes (
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    commit_sha text NOT NULL,
    path text NOT NULL,
    content_hash text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    indexed_at timestamptz NOT NULL,
    PRIMARY KEY (project_id, commit_sha, path)
);

CREATE TABLE IF NOT EXISTS sessions (
    token text PRIMARY KEY,
    member_id text NOT NULL REFERENCES human_members(id) ON DELETE CASCADE,
    expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS invitations (
    id uuid PRIMARY KEY,
    organization_id uuid NOT NULL REFERENCES organizations(id),
    team_id uuid REFERENCES teams(id),
    email text NOT NULL,
    role text NOT NULL,
    token text NOT NULL UNIQUE,
    status text NOT NULL,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title text NOT NULL,
    description text NOT NULL,
    stage text NOT NULL,
    status text NOT NULL,
    assignee text NOT NULL,
    priority text NOT NULL,
    requires_review boolean NOT NULL,
    allow_future_data boolean NOT NULL,
    input_artifacts jsonb NOT NULL,
    input_handoff_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    output_types jsonb NOT NULL,
    parent_task_id uuid,
    dependency_task_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    acceptance_criteria jsonb NOT NULL DEFAULT '[]'::jsonb,
    required_capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    deadline timestamptz,
    information_boundary jsonb NOT NULL DEFAULT '{}'::jsonb,
    resource_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    requires_human_approval boolean NOT NULL DEFAULT true,
    blocked_reason text,
    updated_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS handoffs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id uuid NOT NULL REFERENCES tasks(id),
    sender_agent_id text NOT NULL,
    receiver jsonb NOT NULL,
    status text NOT NULL,
    objective text NOT NULL,
    completed jsonb NOT NULL,
    input_artifacts jsonb NOT NULL,
    output_artifacts jsonb NOT NULL,
    key_conclusions jsonb NOT NULL,
    assumptions jsonb NOT NULL,
    evidence_refs jsonb NOT NULL,
    open_questions jsonb NOT NULL,
    risks jsonb NOT NULL,
    next_actions jsonb NOT NULL,
    requires_human_approval boolean NOT NULL,
    schema_version text NOT NULL DEFAULT '1.0',
    handoff_type text NOT NULL DEFAULT 'RELAY',
    receipt_status text NOT NULL DEFAULT 'PENDING',
    received_by text,
    received_at timestamptz,
    idempotency_key text,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id uuid,
    agent_id text NOT NULL,
    status text NOT NULL,
    source_commit text,
    input_artifact_ids jsonb NOT NULL,
    environment_image_digest text,
    dependency_lock text,
    parameters jsonb NOT NULL,
    random_seed integer,
    model_provider text,
    model_name text,
    tool_versions jsonb NOT NULL,
    network_policy text NOT NULL,
    execution_profile jsonb NOT NULL DEFAULT '{"mode":"HEADLESS","requires_user_session":false,"allow_remote_terminal":false,"allow_desktop_control":false,"network_policy":"deny-by-default","auto_retry":false}'::jsonb,
    data_access_policy jsonb NOT NULL,
    observed_input_files jsonb NOT NULL,
    output_artifact_ids jsonb NOT NULL,
    stdout text NOT NULL,
    stderr text NOT NULL,
    summary text NOT NULL,
    information_boundary jsonb NOT NULL,
    started_at timestamptz NOT NULL,
    completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_runs_project ON runs(project_id, started_at DESC);

CREATE TABLE IF NOT EXISTS artifacts (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name text NOT NULL,
    artifact_type text NOT NULL,
    description text NOT NULL,
    content_hash text NOT NULL,
    version integer NOT NULL CHECK (version >= 1),
    status text NOT NULL,
    source_path text,
    task_id uuid,
    run_id uuid,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL,
    data_policy jsonb NOT NULL,
    input_artifact_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    git_commit text,
    snapshot_ref text,
    created_by_kind text NOT NULL DEFAULT 'member',
    approved_by text,
    approved_at timestamptz,
    downstream_allowed boolean NOT NULL DEFAULT false,
    storage_key text,
    size_bytes bigint CHECK (size_bytes IS NULL OR size_bytes >= 0),
    mime_type text,
    immutable boolean NOT NULL DEFAULT false,
    parent_artifact_id uuid,
    archived_at timestamptz
);

CREATE TABLE IF NOT EXISTS artifact_multipart_uploads (
    upload_id text PRIMARY KEY,
    artifact_id uuid NOT NULL REFERENCES artifacts(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    storage_key text NOT NULL,
    mime_type text,
    created_at timestamptz NOT NULL,
    status text NOT NULL DEFAULT 'ACTIVE',
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS reviews (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    target_type text NOT NULL,
    target_id uuid NOT NULL,
    verdict text NOT NULL,
    summary text NOT NULL,
    findings jsonb NOT NULL,
    reviewer text NOT NULL,
    reviewer_kind text NOT NULL DEFAULT 'agent',
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS gates (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    target_type text NOT NULL,
    target_id uuid,
    status text NOT NULL,
    required_human_approval boolean NOT NULL,
    blocking_findings jsonb NOT NULL,
    rules jsonb NOT NULL,
    approved_by text,
    approved_at timestamptz,
    UNIQUE (project_id, target_type, target_id)
);

CREATE TABLE IF NOT EXISTS evidence (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    claim text NOT NULL,
    evidence_type text NOT NULL,
    artifact_id uuid,
    run_id uuid,
    source_ref text,
    created_by text NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sequence bigint NOT NULL,
    event_type text NOT NULL,
    actor text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL,
    actor_kind text NOT NULL DEFAULT 'system',
    object_type text,
    object_id uuid,
    idempotency_key text,
    schema_version text NOT NULL DEFAULT '1.0',
    UNIQUE (project_id, sequence)
);

CREATE TABLE IF NOT EXISTS event_outbox (
    id uuid PRIMARY KEY,
    event_id uuid NOT NULL UNIQUE REFERENCES events(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'PROCESSING', 'DELIVERED', 'FAILED')),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL,
    locked_at timestamptz,
    lock_expires_at timestamptz,
    delivered_at timestamptz,
    last_error text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_outbox_pending ON event_outbox(status, available_at, created_at);

CREATE TABLE IF NOT EXISTS task_leases (
    id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent_id text NOT NULL REFERENCES agents(agent_id),
    lease_token text NOT NULL UNIQUE,
    status text NOT NULL,
    issued_at timestamptz NOT NULL,
    expires_at timestamptz NOT NULL,
    last_heartbeat timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_leases_active ON task_leases(task_id, status);

CREATE TABLE IF NOT EXISTS idempotency_records (
    key text PRIMARY KEY,
    operation text NOT NULL,
    response jsonb NOT NULL,
    request_hash text,
    created_at timestamptz NOT NULL
);
