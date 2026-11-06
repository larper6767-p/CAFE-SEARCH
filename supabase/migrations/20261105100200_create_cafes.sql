create table if not exists public.cafes (
    id                 bigint generated always as identity primary key,
    city_id            bigint not null
                       references public.cities (id) on delete restrict,
    name               text not null,
    address            text,
    -- geography(point, 4326): real-world points queried by distance/radius
    -- in meters (ST_DWithin/ST_Distance) without SRID casts. Longitude is
    -- passed FIRST everywhere (lng, lat).
    location           extensions.geography(point, 4326) not null,
    phone              text,
    website            text,
    -- 1 = budget, 2 = mid, 3 = premium; UI maps to rupee bands later
    -- (PROJECT.md section 5). Nullable until data is cleaned.
    price_band         smallint check (price_band between 1 and 3),
    -- Format to be proposed in step 5 BEFORE any data is inserted.
    opening_hours      jsonb,
    -- Lowercased from Google businessStatus values.
    status             text not null default 'operational'
                       check (status in (
                           'operational',
                           'closed_temporarily',
                           'closed_permanently'
                       )),
    source             text not null
                       check (source in ('google', 'osm', 'manual')),
    -- Upsert key (PROJECT.md/AGENTS.md: ingestion matches on google_place_id).
    google_place_id    text,
    osm_id             bigint, -- OSM node id (nodes only in v1)
    google_rating      numeric(3, 2) check (google_rating between 0 and 5),
    google_review_count integer check (google_review_count >= 0),
    -- Own curated score (PROJECT.md section 7), scale 1.00-5.00.
    curated_score      numeric(3, 2) check (curated_score between 1 and 5),
    -- Bayesian score, computed in the ingestion job (step 6), not at query time.
    final_score        double precision,
    last_verified      timestamptz not null default now(),
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now(),

    -- google_place_id is required when the row came from Google.
    constraint cafes_google_requires_place_id
        check (source <> 'google' or google_place_id is not null)
);

-- UNIQUE on google_place_id: the upsert key (nullable rows are allowed for
-- osm/manual sources; postgres unique indexes allow multiple nulls).
create unique index if not exists cafes_google_place_id_key
    on public.cafes (google_place_id);

-- Partial unique on osm_id (nullable column chosen by user).
create unique index if not exists cafes_osm_id_key
    on public.cafes (osm_id)
    where osm_id is not null;

-- Spatial index for the radius/distance filter (PROJECT.md section 9).
create index if not exists cafes_location_gist
    on public.cafes using gist (location);

create index if not exists cafes_city_id_idx
    on public.cafes (city_id);

create index if not exists cafes_final_score_idx
    on public.cafes (final_score);

-- updated_at is refreshed on every UPDATE.
create or replace function public.set_updated_at() returns trigger
    language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists cafes_set_updated_at on public.cafes;
create trigger cafes_set_updated_at
    before update on public.cafes
    for each row execute function public.set_updated_at();

-- Locked until the Phase 4 API defines policies. Scripts connect as the
-- postgres role, which bypasses RLS.
alter table public.cafes enable row level security;
