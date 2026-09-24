-- P4-04-RUN-PROD: dedicated application runtime role.
--
-- The development deployment runs the repository as the schema owner, which
-- can bypass RLS and run DDL.  Production requires a separate role that can
-- only read/write rows and can never bypass row-level security.
--
-- Passwords are never stored in migrations: the deployment sets the secret
-- and the authentication method (pg_hba) out of band.
--
-- The role is a cluster-wide object.  A migration identity that is not a
-- superuser (for example a per-tenant owner applying migrations in an
-- isolated database) legitimately cannot create or alter it; in that case the
-- migration keeps the grants that are within its power and records a notice
-- instead of failing.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        BEGIN
            CREATE ROLE app_runtime LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'app_runtime not created: migration identity lacks CREATEROLE';
        END;
    END IF;
END
$$;

-- Keep the role's security attributes correct even when it already existed
-- (a login-only role created by hand could otherwise be a superuser).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        BEGIN
            ALTER ROLE app_runtime NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'app_runtime attributes unchanged: migration identity lacks CREATEROLE';
        END;
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        BEGIN
            EXECUTE 'GRANT USAGE ON SCHEMA public TO app_runtime';
            IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'app') THEN
                EXECUTE 'GRANT USAGE ON SCHEMA app TO app_runtime';
                EXECUTE 'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA app TO app_runtime';
            END IF;
            EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_runtime';
            EXECUTE 'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_runtime';
            -- Future tables created by this migration identity stay usable.
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_runtime';
            EXECUTE 'ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO app_runtime';
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'app_runtime grants skipped: migration identity lacks grant authority';
        END;
    END IF;
END
$$;

-- The runtime role must never be able to disable the tenant policy.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_runtime') THEN
        BEGIN
            EXECUTE 'REVOKE CREATE ON SCHEMA public FROM app_runtime';
        EXCEPTION WHEN insufficient_privilege THEN
            RAISE NOTICE 'app_runtime revoke skipped: migration identity lacks grant authority';
        END;
    END IF;
END
$$;
