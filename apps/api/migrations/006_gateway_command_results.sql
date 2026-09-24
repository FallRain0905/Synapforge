-- Stage 3 P3-07: durable Gateway command outcomes for ACK-loss recovery.
-- Results are scoped by connection because a connection owns the message and
-- idempotency sequence; tenant scope is inherited through the device.
CREATE TABLE IF NOT EXISTS gateway_command_results (
    id uuid PRIMARY KEY,
    connection_id text NOT NULL REFERENCES agent_connections(connection_id) ON DELETE CASCADE,
    message_id text NOT NULL,
    idempotency_key text NOT NULL,
    sequence bigint NOT NULL CHECK (sequence >= 1),
    message_type text NOT NULL,
    status text NOT NULL CHECK (status IN ('SUCCEEDED', 'FAILED')),
    response_type text NOT NULL CHECK (response_type IN ('gateway.ack', 'gateway.error')),
    result jsonb,
    error_code text,
    created_at timestamptz NOT NULL,
    UNIQUE (connection_id, message_id),
    UNIQUE (connection_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_gateway_command_results_message
    ON gateway_command_results(connection_id, message_id);
CREATE INDEX IF NOT EXISTS idx_gateway_command_results_idempotency
    ON gateway_command_results(connection_id, idempotency_key);

ALTER TABLE gateway_command_results ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = current_schema()
          AND tablename = 'gateway_command_results'
          AND policyname = 'gateway_command_results_tenant_policy'
    ) THEN
        EXECUTE $policy$
            CREATE POLICY gateway_command_results_tenant_policy ON gateway_command_results
                USING (EXISTS (
                    SELECT 1
                    FROM agent_connections c
                    JOIN devices d ON d.device_id = c.device_id
                    WHERE c.connection_id = gateway_command_results.connection_id
                      AND d.organization_id = app.current_organization_id()
                ))
                WITH CHECK (EXISTS (
                    SELECT 1
                    FROM agent_connections c
                    JOIN devices d ON d.device_id = c.device_id
                    WHERE c.connection_id = gateway_command_results.connection_id
                      AND d.organization_id = app.current_organization_id()
                ))
        $policy$;
    END IF;
END
$$;
