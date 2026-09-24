-- P4-02: independent Fanout receipts, durable risk records and Gate snapshots.

ALTER TABLE gates ADD COLUMN IF NOT EXISTS input_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE gates ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ;
ALTER TABLE gates ADD COLUMN IF NOT EXISTS invalidation_reason TEXT;

CREATE TABLE IF NOT EXISTS handoff_receipts (
    id UUID PRIMARY KEY,
    handoff_id UUID NOT NULL REFERENCES handoffs(id),
    project_id UUID NOT NULL REFERENCES projects(id),
    receiver_type TEXT NOT NULL,
    receiver_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    received_by TEXT,
    received_at TIMESTAMPTZ,
    decision_reason TEXT,
    decision_findings JSONB NOT NULL DEFAULT '[]'::jsonb,
    idempotency_key TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE(handoff_id, receiver_type, receiver_id)
);
CREATE INDEX IF NOT EXISTS idx_handoff_receipts_handoff ON handoff_receipts(handoff_id);

CREATE TABLE IF NOT EXISTS risks (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id),
    review_id UUID NOT NULL REFERENCES reviews(id),
    target_type TEXT NOT NULL,
    target_id UUID NOT NULL,
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    evidence_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    owner TEXT,
    resolution_reason TEXT,
    resolved_by TEXT,
    resolved_at TIMESTAMPTZ,
    closure_evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risks_target ON risks(project_id, target_type, target_id);

ALTER TABLE handoff_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE risks ENABLE ROW LEVEL SECURITY;

CREATE POLICY handoff_receipts_tenant_policy ON handoff_receipts
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = handoff_receipts.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = handoff_receipts.project_id AND p.organization_id = app.current_organization_id()));
CREATE POLICY risks_tenant_policy ON risks
    USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = risks.project_id AND p.organization_id = app.current_organization_id()))
    WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = risks.project_id AND p.organization_id = app.current_organization_id()));

-- Backfill one receipt for legacy RELAY rows. New Fanout rows are expanded by
-- the repository in the same transaction as the Handoff insert.
INSERT INTO handoff_receipts
    (id, handoff_id, project_id, receiver_type, receiver_id, status,
     received_by, received_at, decision_reason, decision_findings, created_at)
SELECT md5(h.id::text || ':legacy-receipt')::uuid, h.id, h.project_id,
       CASE WHEN jsonb_typeof(h.receiver) = 'object'
            THEN COALESCE(h.receiver->>'type', h.receiver->>'kind', 'team')
            ELSE 'team' END,
       CASE WHEN jsonb_typeof(h.receiver) = 'object'
            THEN h.receiver->>'id'
            ELSE trim(both '"' from h.receiver::text) END,
       h.receipt_status, h.received_by, h.received_at, h.decision_reason,
       h.decision_findings, h.created_at
FROM handoffs h
WHERE h.handoff_type <> 'FANOUT'
  AND NOT EXISTS (SELECT 1 FROM handoff_receipts r WHERE r.handoff_id = h.id)
  AND (jsonb_typeof(h.receiver) <> 'object' OR h.receiver->>'id' IS NOT NULL);
