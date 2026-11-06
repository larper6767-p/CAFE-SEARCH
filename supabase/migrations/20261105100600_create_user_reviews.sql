-- User accounts arrive in Phase 7 (PROJECT.md section 12); this table is
-- created now per the phase 1 plan but is NOT used by anything yet.
-- user_id deliberately has no FK to auth.users yet: the auth schema is not
-- part of this phase, and inventing a reference would be a guess.
create table if not exists public.user_reviews (
    id      bigint generated always as identity primary key,
    cafe_id bigint not null references public.cafes (id) on delete cascade,
    user_id uuid not null,
    rating  integer not null check (rating between 1 and 5),
    body    text,
    created_at timestamptz not null default now()
);

-- Locked until the Phase 4/7 phases define policies. Scripts connect as the
-- postgres role, which bypasses RLS.
alter table public.user_reviews enable row level security;
