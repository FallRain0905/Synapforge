-- Stage 3 P3-20: device token rotation metadata and secret-free audit trail.
ALTER TABLE devices ADD COLUMN IF NOT EXISTS token_version bigint NOT NULL DEFAULT 1;
ALTER TABLE devices ADD COLUMN IF NOT EXISTS token_rotated_at timestamptz;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'devices'::regclass AND conname = 'devices_token_version_check'
    ) THEN
        ALTER TABLE devices ADD CONSTRAINT devices_token_version_check CHECK (token_version >= 1);
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS device_token_rotations (
    id uuid PRIMARY KEY,
    device_id text NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    organization_id uuid NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    rotated_by text NOT NULL REFERENCES human_members(id),
    reason text NOT NULL,
    token_version bigint NOT NULL CHECK (token_version >= 2),
    created_at timestamptz NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_device_token_rotations_device
    ON device_token_rotations(device_id, created_at DESC);

ALTER TABLE device_token_rotations ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_policies
        WHERE schemaname = current_schema()
          AND tablename = 'device_token_rotations'
          AND policyname = 'device_token_rotations_tenant_policy'
    ) THEN
        CREATE POLICY device_token_rotations_tenant_policy ON device_token_rotations
            USING (organization_id = app.current_organization_id())
            WITH CHECK (organization_id = app.current_organization_id());
    END IF;
END
$$;
