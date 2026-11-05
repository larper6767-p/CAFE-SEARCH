create table if not exists public.cafe_tags (
    cafe_id bigint not null references public.cafes (id) on delete cascade,
    tag_id  bigint not null references public.tags (id) on delete cascade,
    primary key (cafe_id, tag_id)
);

-- Supports "cafes with tag X" lookups from the tag side.
create index if not exists cafe_tags_tag_id_idx
    on public.cafe_tags (tag_id);

-- Locked until the Phase 4 API defines policies. Scripts connect as the
-- postgres role, which bypasses RLS.
alter table public.cafe_tags enable row level security;
