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
- **Center point:** `TODO: lat, lng` (pick one, e.g. near Paona Bazar)
- **Radius:** `TODO: 10-12 km`
- Stored in a `cities` table so other cities are just new rows.

## 4. Phase 0 checklist
- [ ] Coverage check: search Google Maps and OSM for cafes in Imphal
  - Cafes found: `TODO`
  - With ratings: `TODO`
  - With hours: `TODO`
  - With price level: `TODO`
  - Only on Instagram / not on Google: `TODO`
- [ ] Center point + radius chosen
- [ ] Ranking approach confirmed (section 7)
- [ ] Tags and filters confirmed (sections 5 and 6)

## 5. Filters (v1)
| Filter | Notes |
|---|---|
| Distance from me | Browser geolocation, PostGIS radius query |
| Open now | Needs structured hours; compare in Asia/Kolkata time |
| Price | Own bands: ₹ under 150 per drink, ₹₹ 150-250, ₹₹₹ above 250 (Google's price level is often missing) |
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
- **cafes**: `id`, `city_id`, `name`, `address`, `location` (PostGIS point), `phone`, `website`, `price_band`, `opening_hours` (JSON), `status`, `google_place_id`, `osm_id`, `google_rating`, `google_review_count`, `curated_score`, `final_score`, `last_verified`
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

**Current phase:** 1 (steps 1–2 done: repo structure connection + migrations applied; step 3 next — NOT to be mixed into it)

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
| 2026-11-05 | Moved from Phase 0 to Phase 1 before Phase 0 checklist done | Owner instruction; coverage/center TODOs stay open, needed only by step 3 |
| 2026-11-05 | Schema approved: `cafes.location` NOT NULL; `price_band` smallint 1–3; `curated_score` numeric(3,2) 1–5; RLS on all 6 tables (no policies until Phase 4); `city_id` ON DELETE RESTRICT | Owner-reviewed plan |
| 2026-11-05 | Deps pinned: psycopg[binary] 3.3.8, python-dotenv 1.2.4 | Migration runner + later ingestion scripts |
| 2026-11-06 | Steps 1–2 applied to Supabase (7 migrations, migrations tracked with checksums in `migrations` schema, own schema not public) | Idempotent runner, per-file transaction |
