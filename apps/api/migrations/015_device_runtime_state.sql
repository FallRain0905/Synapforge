-- DP-2-05（B1）：把心跳里的运行态落库。此前只更新时间戳，
-- adapter_versions / capabilities / running_run_ids / local_queue_length /
-- user_session_state / resource_summary 全部被丢弃，平台侧看不到设备上有什么执行体。
-- 这些列只用于展示与诊断，不参与任何授权判定。
CREATE TABLE IF NOT EXISTS device_runtime_state (
    device_id text PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
    connection_id text,
    agent_version text,
    adapter_versions jsonb NOT NULL DEFAULT '{}'::jsonb,
    capabilities jsonb NOT NULL DEFAULT '[]'::jsonb,
    running_run_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    local_queue_length integer NOT NULL DEFAULT 0 CHECK (local_queue_length >= 0),
    user_session_state text NOT NULL DEFAULT 'unknown',
    resource_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    reported_at timestamptz NOT NULL
);

ALTER TABLE device_runtime_state ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname = current_schema() AND tablename = 'device_runtime_state' AND policyname = 'device_runtime_state_tenant_policy') THEN
        CREATE POLICY device_runtime_state_tenant_policy ON device_runtime_state
            USING (EXISTS (SELECT 1 FROM devices d WHERE d.device_id = device_runtime_state.device_id AND d.organization_id = app.current_organization_id()))
            WITH CHECK (EXISTS (SELECT 1 FROM devices d WHERE d.device_id = device_runtime_state.device_id AND d.organization_id = app.current_organization_id()));
    END IF;
END
$$;