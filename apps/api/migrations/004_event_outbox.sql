-- Add the transactional event outbox to databases that already applied 001-003.
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

-- Existing events were written before the outbox existed. Re-enqueue them so a
-- new dispatcher can establish delivery without making the events table the bus.
INSERT INTO event_outbox (id, event_id, project_id, status, attempts, available_at, created_at, updated_at)
SELECT e.id, e.id, e.project_id, 'PENDING', 0, e.created_at, e.created_at, e.created_at
FROM events e
WHERE NOT EXISTS (SELECT 1 FROM event_outbox o WHERE o.event_id = e.id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = current_schema()
          AND tablename = 'event_outbox'
          AND policyname = 'event_outbox_tenant_policy'
    ) THEN
        EXECUTE $policy$
            CREATE POLICY event_outbox_tenant_policy ON event_outbox
                USING (EXISTS (SELECT 1 FROM projects p WHERE p.id = event_outbox.project_id AND p.organization_id = app.current_organization_id()))
                WITH CHECK (EXISTS (SELECT 1 FROM projects p WHERE p.id = event_outbox.project_id AND p.organization_id = app.current_organization_id()))
        $policy$;
    END IF;
END
$$;

ALTER TABLE event_outbox ENABLE ROW LEVEL SECURITY;

ALTER TABLE event_outbox
    ADD COLUMN IF NOT EXISTS lock_expires_at timestamptz;
