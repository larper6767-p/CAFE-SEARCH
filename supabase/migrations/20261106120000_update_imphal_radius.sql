-- Radius correction, owner decision 2026-11-06: 100 km was wider than the
-- Imphal scope; back to the 10-12 km draft band at 12 km.
-- Radius changes go through NEW migration files (see PROJECT.md section 3):
-- editing an applied migration makes the runner fail on checksum mismatch.
update public.cities
set radius_m = 12000
where name = 'Imphal';
