-- P4-01: explicit handoff revision/aggregation and review-gate evidence lineage.
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS input_handoff_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS revision_of_handoff_id UUID;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS revision_number INTEGER NOT NULL DEFAULT 1;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS decision_reason TEXT;
ALTER TABLE handoffs ADD COLUMN IF NOT EXISTS decision_findings JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE reviews ADD COLUMN IF NOT EXISTS evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE gates ADD COLUMN IF NOT EXISTS review_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE gates ADD COLUMN IF NOT EXISTS evidence_ids JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE gates ADD COLUMN IF NOT EXISTS risk_summary JSONB NOT NULL DEFAULT '{}'::jsonb;
