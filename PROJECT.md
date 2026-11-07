# Cafe Tracker: PROJECT.md

> Living doc. Update it as decisions change. Agents: read this fully before starting any task.

## 1. Pitch
A mobile-first web app that helps people find the best cafes near them, starting with **Imphal, Manipur**, and built so new cities can be added without code changes.

## 2. Goals and non-goals

**Goals (v1)**
- Find good cafes by distance, price, rating, and amenities
- Accurate, self-refreshing data (hours, closures)
- Fast on phones and weak connections

**Non-goals (v1)**
- User accounts, user reviews, photo uploads
- Native mobile app
- Monetization
- Scraping Instagram or other social platforms

## 3. Target area (v1)
- **City:** Imphal (Imphal West + Imphal East)
- **Center point:** `24.807779351178766, 93.93915469958242` (owner-provided 2026-11-06)
- **Radius:** `12 km` (`radius_m = 12000`; corrected 2026-11-06 from an interim 100 km via `20261106120000_update_imphal_radius.sql` — back to the 10–12 km draft, scoped to Imphal West/East.)
- To change the radius later: **never edit an applied migration** (the runner fails on checksum mismatch). Add a new file, e.g. `supabase/migrations/<date>_update_imphal_radius.sql` containing `update public.cities set radius_m = <new> where name = 'Imphal';` and run `scripts/apply_migrations.py`.
- Stored in a `cities` table so other cities are just new rows.

## 4. Phase 0 checklist
- [ ] Coverage check: search Google Maps and OSM for cafes in Imphal (measured 2026-11-06)
  - Cafes found: 402 unique on Google (385 within the 12 km radius), 9 on OSM
  - With ratings: 331/402 (82%)
  - With hours: 261/402 (64%)
  - With price level: 142/402 (35%)
  - Only on Instagram / not on Google: `TODO`
- [x] Center point + radius chosen
- [ ] Ranking approach confirmed (section 7)
- [ ] Tags and filters confirmed (sections 5 and 6)

## 5. Filters (v1)
| Filter | Notes |
|---|---|
| Distance from me | Browser geolocation, PostGIS radius query |
| Open now | Needs structured hours (format decided step 5: per-weekday interval arrays, `[]` = closed day, missing days = closed, NULL = unknown; 24/7 and after-midnight wraps handled); compare in Asia/Kolkata time |
| Price | Own bands: ₹ (Rs) under 150 per drink, ₹₹ (RsRs) 150-250, ₹₹₹ (RsRsRs) above 250 (Google's price level is often missing) |
| Rating | Based on `final_score` |
| Tags | See below |

## 6. Tags (v1)
`wifi`, `outlets`, `outdoor_seating`, `quiet`, `group_friendly`, `late_night`, `vegetarian_friendly`, `pet_friendly`, `specialty_coffee`, `desserts_bakery`

Tags live in a join table, so adding one later means adding a row, not a column.

## 7. Ranking
`final_score` is computed in the ingestion job (not at query time) and stored.

Base: Bayesian-weighted rating
```
score = (v / (v + m)) * R + (m / (v + m)) * C
```
- `R` = cafe's average rating
- `v` = number of ratings
- `m` = minimum-votes threshold (**start at 10-20**, since Imphal review counts are low)
- `C` = mean rating across all cafes in the city

Then blend in (weights `TODO`):
- Own curated score (for cafes personally visited)
- Later: user votes

"Best nearby" sort = blend of score and distance, so a far-away 4.8 doesn't beat a close 4.6.

## 8. Data sources
| Source | Use | Status |
|---|---|---|
| Google Places API | Base list, ratings, hours, status, price level | Primary |
| OpenStreetMap (Overpass) | Cross-check, discover missing cafes | Secondary |
| Own data | Curated scores, tags, manual additions | Essential for Imphal |
| SerpApi / Outscraper | Deeper review text, for tag extraction | Later, optional |
| Yelp / Foursquare | Second rating | Check Imphal coverage first |

**Rules**
- Use official APIs where possible; do not scrape Google, Instagram, or TikTok directly.
- Google terms restrict caching of place data: store `google_place_id` long-term, refresh other fields on a schedule, and check current terms before changing storage.
- Set a **billing cap** on the Google Cloud project.

## 9. Draft data model (confirm after Phase 0)
- **cities**: `id`, `name`, `center` (point), `radius_m`, `timezone`
- **cafes**: `id`, `city_id`, `name`, `address`, `location` (PostGIS point), `phone`, `website`, `price_band`, `price_band_source` ('google' | 'manual' — loader never overwrites manual bands), `opening_hours` (JSON), `status`, `google_place_id`, `osm_id`, `google_rating`, `google_review_count`, `curated_score`, `final_score`, `google_data_fetched_at` (batch fetch timestamp; 30-day staleness predicate uses THIS, not `last_verified`), `last_verified` (manual-verification semantics; the loader never writes it — insert default only)
- **tags**: `id`, `name`
- **cafe_tags**: `cafe_id`, `tag_id`
- **user_reviews** (later): `cafe_id`, `user_id`, `rating`, `text`, `created_at`
- **reports**: `cafe_id`, `type` (closed / wrong_hours / other), `status`, `created_at`

Indexes: GIST on `cafes.location`; btree on `city_id`, `final_score`.

## 10. Architecture
```
Data sources → Ingestion jobs → Postgres + PostGIS → Backend API → Web app (PWA)
```
- **Database/auth/API:** Supabase (Postgres + PostGIS)
- **Jobs:** GitHub Actions or Supabase cron running Python/Node scripts
- **Frontend:** Next.js on Vercel
- **Map:** Leaflet + OpenStreetMap tiles
- **Caching:** none at first

**API (planned)**
- `GET /cafes?lat&lng&radius&price&tags&open_now&sort`
- `GET /cafes/:id`
- `POST /reports`

## 11. Imphal-specific notes
- Mobile-first, lightweight pages; consider PWA with cached cafe list for weak connectivity.
- Small cafes often have wrong or missing online hours: show `last_verified` and a "Report a problem" button prominently.
- Some cafes will only exist on Instagram or word of mouth: support manual entry.
- English UI for v1; Meiteilon labels later.

## 12. Phases
| Phase | Goal | Done when |
|---|---|---|
| 0 | Define scope | This doc is filled in |
| 1 | Data foundation | DB holds 100+ real Imphal cafes |
| 2 | Ranking + tagging | Top results look right |
| 3 | Backend API | Filter queries return correct results |
| 4 | Frontend MVP | Usable on a phone, finds nearby cafes |
| 5 | Freshness | Data refreshes itself weekly |
| 6 | Deploy + soft launch | 10-20 real users giving feedback |
| 7 | Users + community | Accounts, reviews, favorites |
| 8 | Growth | More cities, second sources, monetization |

**Current phase:** 1 — **step 6 COMPLETE (real load committed: 233 Google rows in `cafes`; all verification queries green).** Applied migrations: 13 tracked. Pending owner approval: `20261108090000_purge_revision_name_status.sql` + revised `scripts/purge_stale_google_data.py` (name NULL / status 'unknown' / final_score NULL stubs). Next: step 7 (manual CSV import) and step 8 (verification). Purges trigger 2026-12-06 (local `scripts/purge_expired_cache.py` + DB `purge_stale_google_data.py`, dry-run defaults). Step 4 complete: 402 unique places under `data/raw/`, 78 Google calls ≈ ₹0.00. Decisions tracked in `data/decisions/imphal_decisions.csv` (include 233 / review 2 / exclude 167)

## 13. Working agreements (for me and the agent)
- One phase at a time; don't build ahead.
- Every ingestion script takes a city as input; no hardcoded Imphal values.
- Never commit API keys; use environment variables.
- Prefer simple over clever; ask before adding a dependency.
- Update this file when a decision changes.

## 14. Decisions log
| Date | Decision | Reason |
|---|---|---|
| | Start with Imphal only | Focused launch, data quality |
| 2026-11-08 | Owner re-marked the review CSV (same filename): **include 233 / review 2 / exclude 167** — 69 decisions changed vs the cleaner prefill (51 review→include, 17 review→exclude, 1 exclude→include override of `type_exclude` for Mellow Imphal, 2 reviews left open). Loader gate caught 3 invalid tokens (typos `incldue`×1, `excldue`×2 — hard errors per design); owner approved automatic correction (backup `step5_review_20261106T170726Z_pre_typofix.bak` kept in `data/processed/`). Decisions CSV regenerated from the edited file (same id/decision/notes columns, 402 rows; same distribution). Dry-run re-run: 233 include rows validated (0 errors), **inserted 233 / updated 0 (match: True)**, rolled back, `cafes` still 0 rows | Owner re-marking; typo fixes owner-approved |
| 2026-11-08 | **Real load COMMITTED** (`load_places.py --city Imphal --commit`): **233 Google rows inserted / 0 updated (match: True)**, 209 operational + 24 `closed_temporarily`. Verification §15.5 all green: duplicates 0; missing/stale fetch ts 0 (min=max=2026-11-06T16:15:00.8Z); 233/233 within 15 km via ST_DWithin, axis order ok 233; hours 137, price_band 89 (manual 0), rating 187, reviews 187; extras: `last_verified NOT NULL` = 0 (split verified), `google_data_fetched_at = batch ts` = 233/233, `price_band_source='google'` = 89. Migration `20261108000000` applied (last_verified now nullable, no default). **Runner incident (disclosed):** apply_migrations applies ALL pending files, so `20261108000010` (location nullable + `cafes_location_purge_check`) got applied in the same run even though the owner had said not yet — impact none (0 rows at the time; matches the owner's original formulation); the checksum-locked history cannot be un-applied, so the purge revisions continue in NEW migration `20261108090000_purge_revision_name_status.sql` (STAGED, not applied): name nullable + CHECK `name is not null or source='google'` + status CHECK gains 'unknown'; purge script revised: stub sets name NULL, status 'unknown', final_score NULL, keeps google_place_id/curated/tags/last_verified; awaiting owner approval to apply | Owner instruction sequence; incident disclosed not hidden |
| 2026-11-07 | **Step 6 approved with owner changes**; executed: migration `20261107000000_add_google_data_fetched_at_and_price_band_source.sql` applied via the checksum runner (11 tracked). Loader `scripts/load_places.py` written: dry-run DEFAULT + `--commit` gated on owner approval; single transaction; upsert on `google_place_id` (arbiter `cafes_google_place_id_key`); writes name/address/location(lng first)/phone/website/opening_hours/status/rating/review_count + `price_band` filled ONLY when existing NULL or `price_band_source='google'` (never overwrites manual; incoming NULL replaces a google-sourced band) + `price_band_source`; `google_data_fetched_at` = batch manifest timestamp (2026-11-06T16:15:00.800Z), never `now()`; loader NEVER writes `last_verified` (insert default only) and never touches curated_score/tags/final_score/source/city_id/id. phone/website sourced from the raw cache by place id (the marked review CSV has no such columns; owner's CSV untouched). `RETURNING (xmax = 0)` insert/update discrimination verified against a pre-snapshot (fallback check in place). Decisions survive purges in `data/decisions/imphal_decisions.csv` (tracked in git; columns id/decision/notes only, 402 rows: include 181 / review 70 / exclude 151). Local purge script `scripts/purge_expired_cache.py` written: dry-run default, deletes only under `data/raw/` + `data/processed/`, lists candidates, refuses `--commit` when no fresh batch exists unless `--confirm-without-replacement`. Tests extended for loader + purge (suite 70, all pass). DB-side purge design (make `location` nullable w/ CHECK + `purge_stale_google_data.py`) is a PROPOSAL awaiting owner approval — NOT implemented. Dry-run executed: gate integrity OK, 181 include rows validated (0 errors), **inserted 181 / updated 0 (match: True)**, transaction ROLLED BACK — `cafes` still 0 rows (verified separately). NO `--commit` run. | Owner-approved plan + changes; dry-run shown to owner; real load awaits explicit "commit" |
| 2026-11-06 | Step 5 run under approved plan + changes: bakery/cake_shop default EXCLUDE (`bakery_default_exclude`), review only if `types` has a cafe-family type or keyword name (cafe, café, coffee, tea, chai, espresso, brew, roast, roastery, bistro, patisserie, dessert, bakehouse, lounge); cafe-family 12–15 km review, >15 km exclude for everything; hours JSON per-weekday intervals approved (`[]`=closed, missing days closed, NULL=unknown, 24/7-no-close + sat→sun wrap handled); price_level→band 1/2/3, missing→NULL; dedupe same-name ≤50 m flags both rows; review CSV UTF-8-BOM, all-quoted, decision prefilled, sorted review_count desc; loader gate `csv_gate.py`: count+id integrity check, non-include rows refused (not ingested, invalid decision tokens are hard errors); whitespace/NFC normalization for names+addresses; `data/processed/` gitignored + on purge list; tests stdlib unittest | Owner-approved with changes |
| 2026-11-05 | Moved from Phase 0 to Phase 1 before Phase 0 checklist done | Owner instruction; coverage/center TODOs stay open, needed only by step 3 |
| 2026-11-05 | Schema approved: `cafes.location` NOT NULL; `price_band` smallint 1–3; `curated_score` numeric(3,2) 1–5; RLS on all 6 tables (no policies until Phase 4); `city_id` ON DELETE RESTRICT | Owner-reviewed plan |
| 2026-11-05 | Deps pinned: psycopg[binary] 3.3.8, python-dotenv 1.2.4 | Migration runner + later ingestion scripts |
| 2026-11-06 | Steps 1–2 applied to Supabase (7 migrations, migrations tracked with checksums in `migrations` schema, own schema not public) | Idempotent runner, per-file transaction |
| 2026-11-06 | Imphal center 24.807779351178766, 93.93915469958242; radius 100000 m (≈100 km — confirmed over a briefly-written 80 km; revisit before bulk Google fetch) + 10 v1 tags seeded via `20261106110000_seed_imphal_city_and_tags.sql` (upsert, owner decision) | Owner values; seeds idempotent by design |
| 2026-11-06 | Imphal radius corrected to 12000 m (12 km) via `20261106120000_update_imphal_radius.sql` | 100 km exceeded the Imphal area and inflated Google fetch cost; aligned with the 10–12 km draft |
| 2026-11-06 | Step 4 approved: 3×3 grid + one split per saturated cell; text queries cafe/coffee shop/bakery/tea house/cafe restaurant; OSM Overpass check now (local JSON only); stdlib-only HTTP (no new deps); dry-run default, `--live` gated on `GOOGLE_BILLING_CONFIRMED` + `PLACES_MAX_CALLS` cap | Owner decisions; see section 15 for Google data terms |
| 2026-11-06 | First Google live run executed (owner "go" after confirming GCP billing cap, 50/day quota, Places API (New)-only key): 40/40 calls, **252 unique places** saved to local JSON (719 entries incl. cross-cell dupes), 230 OPERATIONAL / 22 CLOSED_TEMPORARILY; fields on unique: rating 83%, hours 65%, phone 57%, priceLevel 38%, website 17%. Coverage incomplete — 28/40 responses saturated, depth-1 split insufficient, 5 text nextPageTokens un-followed; cap aborted before call 41 as designed | Real demand for continuation estimated at ~60–100 more calls (≈₹0.00 within 7,000 free events/mo); awaiting owner decision (continue deeper splits / accept 252 / hybrid) |
| 2026-11-06 | Coverage continuation approved and run: fetcher upgraded to depth-2 splits with **growth-based stop** (`PLACES_MIN_NEW_PER_CALL=8`), cap 120 (default stays 40), text pages 2–3 followed. Run 2: 52 queue calls = 26 live + 26 free cache replays. **Union of both runs: 398 unique places** (1,085 entries; 369 OPERATIONAL / 29 CLOSED_TEMPORARILY; first-seen nearby 270 vs text 128; field coverage: rating 82%, hours 65%, phone 53%, priceLevel 35%, website 15%). Total Google calls across both runs: **66 ≈ ₹0.00** | Text search exhausted at Google's 60-results/query × 5 queries; 4 dense 2-km core cells still saturate under POPULARITY (c09s3s3 +0) so deeper chasing is low-yield; Phase-1 target (100+) exceeded ~4×; owner to confirm coverage stop and review raw JSON |
| 2026-11-06 | Final depth-3 pass run (`PLACES_SPLITS=3`, cap 90): 64/90 budget = 12 live + 52 cache replays. Outcome: **+4 places only** (branches c02s1s1s* +1, c02s1s3s* +3, c03s1s1s* +0; c09s3s3 refused by growth-stop). POPULARITY ranking returns the same ~20 places even at 1 km radius — tail confirmed exhausted. **Step 4 COMPLETE. Final union: 402 unique places** (1,171 entries; 372 OPERATIONAL / 30 CLOSED_TEMPORARILY), 78 Google calls total ≈ ₹0.00, all raw JSON + manifests on disk, zero DB writes | Depth-3 yield decision data proves stopping is right; Phase-1 target exceeded 4×; next = step 5 planning only (cleaning + tests + upsert design, no code until plan approved) |

## 15.5. Post-load verification queries (fixed 2026-11-07)

All scoped to Imphal's city row; the inside-radius count uses `ST_DWithin`
against the city center (geography, meters) — NOT baked-in constants; axis
order checked via ST_X/ST_Y (point stores lng first).

```sql
-- 1. totals by source/status (Imphal only)
select source, status, count(*)
from public.cafes
where city_id = (select id from public.cities where name = 'Imphal')
group by 1, 2 order by 3 desc;

-- 2. duplicate guard on the upsert key
select google_place_id, count(*)
from public.cafes
where source = 'google'
  and city_id = (select id from public.cities where name = 'Imphal')
group by 1 having count(*) > 1;

-- 3. freshness: 30-day compliance
select count(*) filter (where google_data_fetched_at is null)      as missing_ts,
       count(*) filter (where google_data_fetched_at < now() - interval '30 days') as stale,
       min(google_data_fetched_at) as oldest, max(google_data_fetched_at) as newest
from public.cafes
where source = 'google'
  and city_id = (select id from public.cities where name = 'Imphal');

-- 4. coords within 15 km via ST_DWithin + lng/lat sanity
select count(*) as inside_radius,
       count(*) filter (where extensions.st_y(location::extensions.geometry) between 23 and 25
                          and extensions.st_x(location::extensions.geometry) between 93 and 95) as axis_order_ok
from public.cafes c
join public.cities ci on ci.name = 'Imphal' and c.city_id = ci.id
where c.source = 'google'
  and st_dwithin(c.location, ci.center, 15000);

-- 5. fields sanity (hours/price/rating)
select count(*) filter (where opening_hours is not null)              as with_hours,
       count(*) filter (where price_band is not null)                 as with_price_band,
       count(*) filter (where price_band_source = 'manual')           as manual_bands,
       count(*) filter (where google_rating is not null)              as with_rating,
       count(*) filter (where google_review_count is not null)        as with_reviews
from public.cafes
where source = 'google'
  and city_id = (select id from public.cities where name = 'Imphal');
```

## 15. Google data terms
> Read 2026-11-06 from the Places API policies page and the Google Maps Platform Service Specific Terms (A.3, §14, last modified 2026-10-28).
- `google_place_id` may be stored **indefinitely** (place IDs are exempt from caching restrictions).
- Cache **purge/re-verify list for the 2026-12-06 deadline**: `data/raw/` (raw fetch responses incl. coords) and `data/processed/` (derived review CSVs carrying Google-sourced values).
- Google-sourced lat/lng may be cached at most **30 consecutive calendar days**. Other Google fields (name, address, rating, review count, hours, phone, website, price level) have **no indefinite storage grant** — treat them as refreshable cache tied to `google_data_fetched_at` (column added 2026-11-07; loader stamps it with the BATCH fetch timestamp, never `now()`, and never touches `last_verified`). **First fetch date was 2026-11-06 → cached Google coords must be re-verified or dropped by 2026-12-06.**
- **Purge mechanics (2026-11-07)**: local purge = `scripts/purge_expired_cache.py` (dry-run default; deletes only under `data/raw/` + `data/processed/`; lists candidates; refuses `--commit` with no fresh batch unless `--confirm-without-replacement`). DB-side purge design (proposal, awaiting owner approval): migration to make `cafes.location` nullable with a CHECK (`location is not null unless google_data_fetched_at is null or the row is not Google-sourced`) + `scripts/purge_stale_google_data.py` nulling Google-derived fields older than 30 days, keeping `google_place_id` and all curated data. Owner decisions are preserved outside the purge zone in `data/decisions/imphal_decisions.csv` (tracked in git, id/decision/notes only).
- Google Places data must **not be displayed on a non-Google map** (Service Specific Terms §14.2). The Phase 4 map decision is **OPEN**: option A (Google Maps), option B (Leaflet with own/OSM coordinates), option C (hybrid). **Do not build any map until decided.**
- OSM data (Overpass fetches) is © OpenStreetMap contributors, licensed under ODbL — attribution required wherever OSM-derived fields are shown.
- Dev raw responses are cached locally under `data/raw/` (gitignored) so re-runs don't re-bill; raw caching is a development-time convenience per AGENTS.md, and the refresh rule above is the long-term compliance plan.
