-- Stage 3 P3-23: retain a non-secret fingerprint for Gateway command replay.
-- The project capability token is excluded from the fingerprint by Gateway.
ALTER TABLE gateway_command_results
    ADD COLUMN IF NOT EXISTS request_hash text;
