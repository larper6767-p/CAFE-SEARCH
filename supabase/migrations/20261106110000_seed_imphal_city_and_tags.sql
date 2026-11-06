-- Seed data, Phase 1 step 3 (PROJECT.md sections 3 and 6).
-- Idempotent: upserts keyed on unique names; re-running cannot create
-- duplicates. The seed file is what holds the Imphal values — no ingestion
-- script hardcodes them; scripts read the city row from the cities table.
--
-- NOTE (postGIS convention): longitude comes FIRST in st_makepoint(lng, lat).
-- Center: 24.807779351178766, 93.93915469958242 (owner-provided 2026-11-06).
-- Radius: 100 km (100000 m) per owner decision 2026-11-06.

insert into public.cities (name, center, radius_m, timezone)
values
    ('Imphal',
     extensions.st_setsrid(
         extensions.st_makepoint(93.93915469958242, 24.807779351178766),
         4326
     )::extensions.geography,
     100000,
     'Asia/Kolkata')
on conflict (name) do update
    set center   = excluded.center,
        radius_m = excluded.radius_m,
        timezone = excluded.timezone;

-- v1 tag list, PROJECT.md section 6.
insert into public.tags (name)
values
    ('wifi'),
    ('outlets'),
    ('outdoor_seating'),
    ('quiet'),
    ('group_friendly'),
    ('late_night'),
    ('vegetarian_friendly'),
    ('pet_friendly'),
    ('specialty_coffee'),
    ('desserts_bakery')
on conflict (name) do update set name = excluded.name;
