ALTER TABLE idempotency_records
    ADD COLUMN IF NOT EXISTS request_hash text;
