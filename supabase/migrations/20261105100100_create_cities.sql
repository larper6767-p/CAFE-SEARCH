create table if not exists public.cities (
    id         bigint generated always as identity primary key,
    name       text not null unique,
    center     extensions.geography(point, 4326) not null,
    radius_m   integer not null check (radius_m > 0),
    timezone   text not null,
    created_at timestamptz not null default now()
);

comment on table public.cities is 'One row per launch city. No city values are hardcoded anywhere in code; ingestion scripts take a city row as input.';

-- Locked until the Phase 4 API defines policies. Scripts connect as the
-- postgres role, which bypasses RLS.
alter table public.cities enable row level security;
