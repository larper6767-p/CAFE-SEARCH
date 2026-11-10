-- Phase 2, step 3 (tagging): record WHERE a tag came from.
-- Applied 2026-11-10 via `apply_migrations.py --only` (owner authorized after
-- the additive-only check: nullable column + CHECK, no defaults, no data).
--
-- Every cafe_tags row keeps provenance in one nullable column; the four tokens
-- are the confidence ladder (owner decision, 2026-11-10):
--   own_visit      — owner visited the place personally
--   own_knowledge  — owner knows the fact another way (no visit)
--   review_derived — extracted from review text/Google reviews
--   user           — user-reported (later phases)
-- NULL = link predates tagging (e.g. created by the manual CSV importer).
-- Lifecycle: the loader NEVER writes cafe_tags; the purge NEVER touches
-- cafe_tags; the tagging importer SETS tag_source on links it creates and
-- DELETES only own_visit/own_knowledge links (never review_derived/user).

alter table public.cafe_tags
    add column if not exists tag_source text;

alter table public.cafe_tags
    add constraint cafe_tags_tag_source_check
    check (tag_source in
           ('own_visit', 'own_knowledge', 'review_derived', 'user'));

comment on column public.cafe_tags.tag_source is
    'Provenance of the tag: own_visit / own_knowledge / review_derived / user; NULL = not yet curated';
