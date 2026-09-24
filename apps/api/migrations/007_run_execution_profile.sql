ALTER TABLE runs
    ADD COLUMN IF NOT EXISTS execution_profile jsonb NOT NULL DEFAULT '{"mode":"HEADLESS","requires_user_session":false,"allow_remote_terminal":false,"allow_desktop_control":false,"network_policy":"deny-by-default","auto_retry":false}'::jsonb;

UPDATE runs
SET execution_profile = jsonb_set(execution_profile, '{network_policy}', to_jsonb(network_policy), true)
WHERE network_policy <> 'deny-by-default'
  AND execution_profile->>'network_policy' = 'deny-by-default';
