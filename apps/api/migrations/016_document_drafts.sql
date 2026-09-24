-- CL-6：协作草稿持久化（未保存的编辑刷新后不丢）。
-- 只存文本与修订号；协作帧语义不变（apps/api/app/collaboration.py 未改动）。
CREATE TABLE IF NOT EXISTS document_drafts (
    artifact_id uuid PRIMARY KEY REFERENCES artifacts(id) ON DELETE CASCADE,
    project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content text NOT NULL,
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_document_drafts_project ON document_drafts(project_id, updated_at DESC);

ALTER TABLE document_drafts ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'document_drafts' AND policyname = 'document_drafts_tenant_policy') THEN
        CREATE POLICY document_drafts_tenant_policy ON document_drafts
            USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = document_drafts.project_id AND p.organization_id = app.current_organization_id()))
            WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = document_drafts.project_id AND p.organization_id = app.current_organization_id()));
    END IF;
END
$$;
