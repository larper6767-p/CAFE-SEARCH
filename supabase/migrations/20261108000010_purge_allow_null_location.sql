-- Step 6 follow-up #2 (owner item 2 of the approved changes): DB-side purge design.
-- APPLY ONLY ON OWNER APPROVAL (owner asked to see the files first).
--
-- Purge contract (PROJECT.md section 15): Google-sourced field values older
-- than 30 days are physically removed; the row survives as a STUB identified
-- by google_place_id (exempt key, stored indefinitely) with all curated data
-- kept (curated_score, cafe_tags, ids, city). The loader re-fills the stub on
-- the next refresh (upsert on google_place_id).
--
-- The blocker was cafes.location being NOT NULL while purged Google rows must
-- lose their coordinates. Suggested by the owner: make location nullable with
-- a CHECK keeping location mandatory for any row with a live Google payload.
--
-- CHECK semantics: location must be present UNLESS
--   (a) google_data_fetched_at IS NULL  — no live Google payload (purged stub,
--       or a non-Google row), or
--   (b) the row is not Google-sourced (osm/manual rows never had a Google fix).
-- After the purge nulls location AND google_data_fetched_at together, (a)
-- holds. A refresh re-fill writes location + google_data_fetched_at in the
-- SAME statement (single upsert), so the constraint stays satisfied.
--
-- NOTE: cafes.name is also NOT NULL and also Google-derived. THIS migration
-- does NOT touch it — the purge script replaces name with the key stub
-- '[place:<google_place_id>]' (proposal pending owner confirmation).

alter table public.cafes
    alter column location drop not null;

alter table public.cafes
    drop constraint if exists cafes_location_purge_check;

alter table public.cafes
    add constraint cafes_location_purge_check
    check (location is not null
           or google_data_fetched_at is null
           or source <> 'google');

comment on constraint cafes_location_purge_check on public.cafes is
    'Google rows with a live payload must have coordinates; purged stubs (google_data_fetched_at null) may drop location for the 30-day cache contract';
