-- Step 6 (loader): freshness + price-band provenance.
-- google_data_fetched_at: timestamp of the fetch BATCH that produced the
--   Google-sourced field values in this row (NOT the ingest time). The
--   30-day staleness predicate (PROJECT.md section 15/16) uses this column,
--   never last_verified.
-- price_band_source: provenance of price_band — 'google' (loader-filled)
--   or 'manual' (owner-curated; the loader NEVER overwrites manual bands).
--   NULL means "no band" (price_band is NULL too).
-- Both columns are additive and nullable; no existing data is modified.

alter table public.cafes
    add column if not exists google_data_fetched_at timestamptz,
    add column if not exists price_band_source text
        check (price_band_source in ('google', 'manual'));

comment on column public.cafes.google_data_fetched_at is
    'UTC ts of the fetch batch this row''s Google-sourced values came from; 30-day staleness predicate uses it';

comment on column public.cafes.price_band_source is
    'price_band provenance: google or manual; loader never overwrites manual bands';
