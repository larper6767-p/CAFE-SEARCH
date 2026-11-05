create table if not exists public.tags (
    id   bigint generated always as identity primary key,
    name text not null unique
);

-- Locked until the Phase 4 API defines policies. Scripts connect as the
-- postgres role, which bypasses RLS.
alter table public.tags enable row level security;
