-- Stage 3 P3-18: proof that the registering device controls its Ed25519 key.
-- Existing pairings may have NULL here and must not be used for registration.
ALTER TABLE device_pairings ADD COLUMN IF NOT EXISTS challenge_hash text;

COMMENT ON COLUMN device_pairings.challenge_hash IS
    'SHA-256 of the one-time registration challenge; raw challenge is never persisted';
