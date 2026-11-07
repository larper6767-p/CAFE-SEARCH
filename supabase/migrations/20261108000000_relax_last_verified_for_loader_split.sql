-- Step 6 follow-up #1 (owner item 3 of the approved changes): last_verified field split.
--
-- Approved split: google_data_fetched_at covers Google-field freshness; the
-- loader never writes last_verified. But last_verified is
-- `timestamptz not null default now()`, so every NEW google row still gets
-- stamped with its insert time — which is NOT a manual verification and
-- muddies the semantic.
--
-- This migration makes the schema honest: last_verified stays NULL until a
-- HUMAN verifies the row (report check / walk-in). No backfill needed — the
-- cafes table is still empty (0 rows as of 2026-11-08).
--
-- Effect on the pending real load: the 181 include rows will insert with
-- last_verified NULL (= "not yet verified"), freshness display uses
-- google_data_fetched_at (batch ts 2026-11-06T16:15:00.800Z).

alter table public.cafes
    alter column last_verified drop not null,
    alter column last_verified drop default;

comment on column public.cafes.last_verified is
    'When a human last verified this row''s data (manual check). NULL = not yet verified. Google-field freshness uses google_data_fetched_at instead.';
