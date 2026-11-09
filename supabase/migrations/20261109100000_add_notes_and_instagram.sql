-- Step 7 (manual CSV import): owner-curated text columns.
-- Applied 2026-11-09 via `apply_migrations.py --only` (owner pre-approved:
-- additive-only = two nullable text columns, no defaults, no constraints).
--
-- adds two nullable, owner-owned columns:
--   notes          — free text (never written by the loader, never nulled by the purge;
--                    curated data survives the 2026-12-06 purge untouched)
--   instagram_url  — profile URL (same lifecycle: loader never writes it;
--                    purge never touches it)
-- The manual importer (scripts/import_manual.py) refuses rows that fill these
-- columns while they are missing.

alter table public.cafes
    add column if not exists notes text,
    add column if not exists instagram_url text;

comment on column public.cafes.notes is
    'Owner-curated free text (manual CSV import); loader never writes it, purge never touches it';
comment on column public.cafes.instagram_url is
    'Owner-curated Instagram profile URL (manual CSV import); loader never writes it, purge never touches it';
