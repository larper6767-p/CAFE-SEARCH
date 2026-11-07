-- Purge-design REVISION migration (owner decisions, 2026-11-08) — apply only on approval.
--
-- Base for the revision: 20261108000010 already relaxed `location` and added
-- cafes_location_purge_check (checksum-locked; not editable). This NEW migration
-- adds the owner's remaining purge decisions:
--
--  (a) name becomes NULLABLE — purged Google stubs drop the name entirely
--      (no '[place:<id>]' placeholder). Rows with source = 'manual' always
--      keep their name, enforced by the guard below.
--  (b) status gains 'unknown'; the purge script sets status = 'unknown' on
--      stubs. The 'operational' insert default is unchanged, and the loader
--      only ever writes the three live statuses, so refresh re-fills real
--      values.
--  (c) final_score is nulled by the purge script (column is already nullable —
--      no schema change needed here).
--  (d) name guard: `name is not null or source = 'google'` — osm/manual rows
--      must carry a name; only Google rows may be nameless stubs.

alter table public.cafes
    alter column name drop not null;

alter table public.cafes
    drop constraint if exists cafes_status_check;
alter table public.cafes
    add constraint cafes_status_check
    check (status in ('operational', 'closed_temporarily', 'closed_permanently', 'unknown'));

alter table public.cafes
    drop constraint if exists cafes_name_live_guard_check;
alter table public.cafes
    add constraint cafes_name_live_guard_check
    check (name is not null or source = 'google');

comment on constraint cafes_name_live_guard_check on public.cafes is
    'Only Google rows may be nameless stubs after purge; osm/manual rows always keep a name';
