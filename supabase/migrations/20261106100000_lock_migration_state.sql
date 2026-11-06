-- Defense in depth (step 1–2 review): migration state lives in the dedicated
-- 'migrations' schema — created by the runner bootstrap, never in public —
-- and the Supabase Data API does not expose it by default. Lock it anyway:
-- enable RLS (runner connects as postgres, which bypasses RLS) and strip any
-- Data API role grants.
alter table migrations.schema_migrations enable row level security;
revoke all on migrations.schema_migrations from anon, authenticated, service_role;
revoke usage on schema migrations from anon, authenticated, service_role;
