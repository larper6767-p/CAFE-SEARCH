create table if not exists public.reports (
    id      bigint generated always as identity primary key,
    cafe_id bigint not null references public.cafes (id) on delete cascade,
    type    text not null check (type in ('closed', 'wrong_hours', 'other')),
    status  text not null default 'open'
            check (status in ('open', 'resolved')),
    -- Optional free text; makes 'other' reports usable.
    details text,
    created_at timestamptz not null default now()
);

-- Locked until the Phase 4 API defines policies. Scripts connect as the
-- postgres role, which bypasses RLS.
alter table public.reports enable row level security;
