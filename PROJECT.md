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
(Vocabulary CONFIRMED by the owner 2026-11-10: keep the current 10 for now.
These 10 columns are generated from the `tags` table in the tagging sheet; a
new tag is an INSERT into `tags` followed by a re-export. The ranking rubric
text is still pending from the owner — no rubric is recorded yet.)

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
- **cafes**: `id`, `city_id`, `name`, `address`, `location` (PostGIS point), `phone`, `website`, `price_band`, `price_band_source` ('google' | 'manual' — loader never overwrites manual bands), `opening_hours` (JSON), `status` (includes 'unknown' for purged Google stubs since migration 20261108090000), `google_place_id`, `osm_id`, `google_rating`, `google_review_count`, `curated_score`, `final_score`, `google_data_fetched_at` (batch fetch timestamp; 30-day staleness predicate uses THIS, not `last_verified`), `last_verified` (manual-verification semantics; the loader never writes it — insert default only); `notes` text, `instagram_url` text (owner-curated, manual CSV import only — loader never writes them, purge never touches them; added by applied migration 20261109100000)
- **tags**: `id`, `name`
- **cafe_tags**: `cafe_id`, `tag_id`
- **user_reviews** (later): `cafe_id`, `user_id`, `rating`, `text`, `created_at`
- **reports**: `cafe_id`, `type` (closed / wrong_hours / other), `status`, `created_at`

Indexes: GIST on `cafes.location`; btree on `city_id`, `final_score`.

**Standing rule (stub-filter, 2026-11-08):** every API/query that serves cafes
must exclude rows where `location IS NULL OR status = 'unknown'` — these are
purged Google stubs awaiting refresh (PROJECT.md section 15.5 purge design).
Manual rows always carry a location (import requirement), so they are never
hidden by this rule.

**Standing rule (manual-row coordinates, 2026-11-08):** coordinates for manual
rows MUST come from the owner's phone GPS or OpenStreetMap — NEVER copied from
Google Maps. The importer refuses nothing less; this keeps manual data safe to
display on non-Google map tiles under the section 15 terms boundary.

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
- All café-serving endpoints/queries apply the standing stub-filter `location IS NOT NULL AND status <> 'unknown'` (see section 9).
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

**Current phase:** 2 — **ranking + tagging.** Step 1 done (read-only stats). Steps 3–5 plan **approved with owner changes** (2026-11-10); coded in the owner's order, nothing committed to the DB: (a) **scoring job** `scripts/compute_final_score.py` + tracked `config/scoring.toml` (m=15.0, w=0.6 google / 0.4 curated, C computed excluding `closed_temporarily` per config flag, override mode supported; `--explain "<name>"` read-only) — dry-run on the 233 Google rows shown to the owner: C=4.251 (180 rated, closed excluded), branches google-only 187 / null 46 / blended 0 / curated-only 0 / stub 0, top-20 (4.74 Chai cafe King …) and bottom-20 (3.09 Ciao Bella …) printed, **no --commit**; (b) **migration** `supabase/migrations/20261110000000_add_cafe_tags_tag_source.sql` (provenance ladder own_visit/own_knowledge/review_derived/user, nullable, delete rule restricted to own_*) — **APPLIED 2026-11-10 on owner authorization after the additive-only re-check (nullable column + CHECK + comment, no defaults/data changes; 16 tracked)**; tagging DB tests activated (suite 140 OK, 1 justified skip: the refusal-path test is moot while the column exists); (c) **tagging exporter** `scripts/export_tagging_sheet.py` (working sheet → gitignored `data/processed/imphal_tagging.csv`, pre-filled round-trip, refuses clobber without --overwrite; **--overwrite DISCARDS un-imported sheet edits — workflow: import first, back up the sheet, then re-export; warned at runtime + --help**) + **tagging importer** `scripts/import_tagging.py` (blank=unknown, never "no"; deletes ONLY own_visit/own_knowledge links, every deletion listed; on --commit regenerates the tracked stripped record `data/decisions/imphal_tags.csv`) + **curated importer** `scripts/import_curated.py` (blank=unchanged, `clear`→NULL, optional `last_verified`; never touches `cafes.notes`). Suite **140 OK (1 justified skip** — the missing-column refusal test, moot while the migration is applied). **Real tagging sheet GENERATED 2026-11-10 (export only, no import): 233 rows × 14 columns, 0 prefilled links.** **No real import run yet** — owner's filled sheet pending. Owner to deliver: rubric text (the paste arrived as an unfilled placeholder); tag vocabulary CONFIRMED (§6: keep the current 10). Deadlines: refresh fetch 2026-12-01, purges 2026-12-06. Phase 1 only leftover: owner's manual CSV import (separate).

**Phase 1 closing checklist (2026-11-09):**
- **Done:** schema live (15 migrations incl. `last_verified` relax, location-purge CHECK, name/status/final_score purge revision, notes + instagram_url); step-4 raw fetch (402 unique places, 78 Google calls ≈ ₹0.00); step-5 review decisions (include 233 / review 2 / exclude 167, tracked durably in `data/decisions/`); **step-6 real load** (233 Google rows, §15.5 verification green); **step-7 importer + template** (code committed, dry-run default, suite 106 OK); purge tooling ready (local `purge_expired_cache.py` + DB `purge_stale_google_data.py`, both dry-run default, `--commit` gated); standing rules recorded (stub-filter §9/§10; manual coords from phone GPS/OSM only).
- **NOT done (Phase 2 tagging/ranking):** `curated_score` values (all rows currently from loader defaults / NULL), `final_score` formula + `cafes.final_score` population for manual rows, tag curation beyond the 10 seeded v1 tags, open-now computation (`Asia/Kolkata`) and any display logic, API filter queries (Phase 3).
- **Deadlines:** **2026-12-01 — refresh fetch** (re-batch Google data while billing cap rules apply; new batch ts anchors the next 30-day cycle); **2026-12-06 — purge** (`purge_expired_cache.py --commit` for local `data/raw/`+`data/processed/`, then refresh re-load + `purge_stale_google_data.py --commit` for DB stubs; keep decisions in `data/decisions/imphal_decisions.csv`). Manual rows and curated fields are exempt from the DB purge by design.

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
| 2026-11-10 | **Step-2 partial: tag vocabulary CONFIRMED** — keep the current 10 v1 tags for now (annotated in §6; a new tag = INSERT into `tags` + re-export). Ranking rubric text still PENDING: the owner's paste arrived as an unfilled placeholder, so no rubric was recorded and none was invented. **Exporter warning added**: --overwrite prints (and --help states) that it DISCARDS un-imported sheet edits — workflow: import first, back up the sheet, then re-export. **Real tagging sheet GENERATED (export only, no import)**: `data/processed/imphal_tagging.csv` — 233 cafes × 14 columns (id, name, review_count, tag_source + the 10 tags), sorted review_count desc, 0 prefilled links (cafe_tags empty; tag_source column live), UTF-8-BOM all-quoted. Still zero writes beyond approved migrations; no scoring --commit; no imports | Owner items 1–4; rubric awaited |
| 2026-11-10 | **cafe_tags.tag_source migration APPLIED** after owner authorization + additive-only re-check (nullable text column, CHECK 4-token provenance, column comment; no defaults/data changes/drops; the only pending file; **16 tracked**) + the ranking print now shows `R=` (google_rating) beside `v=` on top/bottom-20 output (owner asked for both). Conditional tagging DB test activated and passing (add/delete/protected-link flow on the live schema; refusal-path test now a justified skip while the column exists). Suite 140 OK. Confirmed for the owner: tag sheet columns are GENERATED from the tags table (new tag = INSERT + re-export), prefill = DB links with refuse-without---overwrite; disclosed caveat: prefill reads DB state, so `--overwrite` of a sheet with un-imported edits WOULD drop those edits (merge-on-overwrite upgrade offered, not built) | Owner instruction items 1–4; still no --commit/import/real-export ran |
| 2026-11-10 | **Phase 2 started (owner order of steps 1–7; one step at a time).** Step 1 executed — READ-ONLY stats, SELECT queries only, no writes/migrations: `scripts/ranking_stats.py --city Imphal` (kept for step-6 audit reuse). Results: 233 google rows / 187 rated (46 no-rating); review_count min 1 / p25 4 / median 13 / p75 52 / p90 165 / mean 61.4 / max 871 with `<5`=55, `<10`=80, `<25`=114, `>100`=29; rating min 1.0 / max 5.0 / mean 4.271 (**owner candidate for C**) / median 4.4 / stddev 0.692 / ≥4.5 = 80; closed_temporarily: 24 rows, 7 rated, review max 94, rating mean 4.771 (small-n, ≥4.5 count 5). Agent m suggestion: **15** (between §7's 10–20; just above the median so the ~55% thinly-reviewed places lean on the prior while p75+ rows keep 78–98% of their own signal; tunable). Steps 3–5 plan delivered in chat — TAGGING export/import (id, name, review_count, one column per tag, tag_source; blank = unknown, never inferred as "no"); staged migration proposal `cafe_tags.tag_source` (own_visit / own_knowledge / review_derived / user) NOT applied; curated import `cafes.curated_score` 1.00–5.00; scoring job `final_score = w·WR + (1−w)·curated` with WR Bayesian (m from config), config in tracked TOML (stdlib tomllib), renormalized when one term missing, NULL when both missing or the row is a purged stub | Owner instruction; §7 shape unchanged |
| 2026-11-10 | **Steps 3–5 plan approved with owner changes; code written in the owner's order (a→b→c), zero DB writes and no --commit runs.** Owner changes honored: tagging working sheet exported to `data/processed/` (gitignored, purge-listed) with the tracked stripped record `data/decisions/imphal_tags.csv` (id, tag, tag_source) written by the importer on --commit; importer deletes ONLY links whose tag_source is own_visit/own_knowledge (never review_derived/user/NULL) and the dry-run lists every deletion; curated import: blank = unchanged, token `clear` → NULL, optional `last_verified` (YYYY-MM-DD) column stored only when provided, `cafes.notes` untouched; scoring: C excludes `closed_temporarily` (config flag ON), starts w = 0.6/0.4, m = 15, output prints C/m/w + per-branch counts (blended / google only / curated only / null), `--explain "<name>"` gives the single-cafe breakdown. Artifacts: `scripts/compute_final_score.py`, `config/scoring.toml` (tracked so weight edits are reviewable), `scripts/export_tagging_sheet.py`, `scripts/import_tagging.py`, `scripts/import_curated.py`, tests incl. rolled-back DB flows (suite 140 OK, 1 skip pending the staged migration). Scoring DRY-RUN on live data shown: C = 4.251 (computed, closed excluded), 187 google-only + 46 null + 0 stub. NEW staged migration `20261110000000_add_cafe_tags_tag_source.sql` awaiting approval (untracked): cafe_tags.tag_source nullable + CHECK 4-token provenance; loader/purge never touch cafe_tags | Owner approval + 5 change list; b-stage waits, c-code ready |
| 2026-11-09 | **notes/instagram_url migration APPLIED** on owner pre-approval after content check (additive-only: two nullable `text` columns, no defaults, no failing constraints — `20261109100000_add_notes_and_instagram.sql` promoted from staging, applied via `--only` as the only pending file; 15 tracked). Importer DB tests moved to the live schema (no conditional column handling left as default; the missing-column refusal stays as a defensive code path, unit-tested), and new coverage added: `notes`/`instagram_url` stored end-to-end, `last_verified` stored when provided and **NULL when the cell is empty** (owner decision: date column optional). Suite 106 OK. PROJECT.md: §12 Phase 1 closing checklist added (done / not-done boundary at Phase 2 tagging+ranking; refresh fetch by 2026-12-01, purge 2026-12-06). **No manual CSV imported — importer stays idle until the owner's real CSV is ready** | Owner instruction items 1–3 |
| 2026-11-08 | **Purge revision APPLIED**: `20261108090000_purge_revision_name_status.sql` promoted from staging to `supabase/migrations/` and applied via `--only` (the only pending file; 14 tracked). AFTER-proof in rolled-back txns: (a) google row → status='unknown'/name NULL/location NULL now **succeeds**, (b) manual row name=NULL still **rejected** — now by `cafes_name_live_guard_check`, (c) legacy statuses **work**; row count 233 unchanged. New `scripts/test_purge_stale_google_data.py` (rolled-back stub-shape: google band dropped, manual band survives, fresh row untouched, guard, legacy statuses, tags survive). Suite now 103 tests, OK. **Step 7 (manual CSV import) code written, planning→implementation, NO real import**: importer decisions from the owner — always `source='manual'`, `price_band_source='manual'` when a band given; manual rows ≤15 km from center with distance printed; light-syntax hours (day ranges, 'closed', multi-intervals, open<close, overnight = two clauses); intra-file duplicates (same name ≤50 m) REFUSED, re-run of an imported row SKIPPED, google near-match (same name ≤50 m) reported only (never merged); new columns approved for the template: optional `last_verified` (date), `instagram_url`, `notes`; `data/manual/` TRACKED in git; template file (`*_template*`) refuse-to-import; manual coordinates MUST come from phone GPS/OSM, never Google Maps (§9 standing rule). NEW staged migration `20261109100000_add_notes_and_instagram.sql` (adds `cafes.notes` + `cafes.instagram_url`, nullable, loader-never-writes/purge-never-touches) awaiting owner approval; importer refuses rows filling those cells while columns are missing | Owner approval of revision + step-7 decisions; template committed, no DB writes |
| 2026-11-08 | **Process fix (runner)**: `apply_migrations.py` NO LONGER auto-applies — default is list-only (pending + staged listing); explicit `--apply` (all pending) or `--only <file>` required; NEW `supabase/migrations_staged/` holds unapproved migrations and is NEVER scanned for applying; `20261108090000_purge_revision_name_status.sql` moved to staging pending owner approval. Constraint reality confirmed via pg_constraint: `cafes_status_check` is the real existing name (drop+re-add under it correct); `cafes_name_live_guard_check` does not exist yet (staged migration adds it); `location` already nullable with `cafes_location_purge_check` (from the applied 20261108000010). BEFORE-state proof in rolled-back txns: (a) google row → unknown/name NULL/location NULL **rejected** (`NotNullViolation` on name today; will pass after the staged migration), (b) manual row name=NULL **rejected** (stays rejected via the new guard), (c) manual row status='operational' **works**; 233 rows unaffected. Standing stub-filter rule added to PROJECT.md §9/§10: every café-serving API/query must exclude `location IS NULL OR status = 'unknown'` | Owner process input after the runner double-apply incident; approval for the staged migration still pending |
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


## Curated score rubric (v1)

**What it is:** my personal 1.0-5.0 rating of a cafe, based on at least one
visit. Half points allowed (e.g. 3.5). NULL = I haven't visited. I never
score a cafe from photos, hearsay, or Google reviews.

### Scale
| Score | Meaning |
|---|---|
| 5.0 | Best in Imphal. I'd go out of my way and bring visitors. |
| 4.0-4.5 | Very good. I'd recommend it to a friend without hesitation. |
| 3.0-3.5 | Solid. Fine if I'm nearby, nothing special. |
| 2.0-2.5 | Below average. Only if there's no alternative. |
| 1.0-1.5 | Would not return. |

### What I weigh (rough guide, not a formula)
| Factor | Weight | What I'm judging |
|---|---|---|
| Drinks and food  | 50% | Quality of coffee/tea/other drinks I actually ordered |
| Space and comfort | 30% | Seating, cleanliness, noise, atmosphere |
| Service and consistency | 10% | Speed, friendliness, same quality on repeat visits |
| Value | 10% | Is what I got worth what I paid? |

### Deliberately NOT part of the score
- **Wifi, outlets, quiet, outdoor seating, pet-friendly, etc.** These are
  tags. A cafe with no wifi can still be a 5.
- **Price level.** Handled by the price filter. Only "value" counts here.
- **Distance, popularity, Google rating.** Handled elsewhere.

### Rules for staying consistent
1. Visit at least once; order a signature drink; stay 20+ minutes if I can.
2. Record the visit date (last_verified) when I score.
3. Calibrate: most cafes should land between 3.0 and 4.0. If more than
   ~10% of my scores are 5.0, I'm inflating.
4. Score the first 5 cafes, then re-read them side by side before
   continuing, so the scale is anchored.
5. Re-score after 12 months, or sooner if a place changes owners, menu,
   or quality.
6. Keep sub-scores in a private spreadsheet if I want them. Only the
   overall number goes in the database.
