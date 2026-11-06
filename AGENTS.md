# AGENTS.md: Instructions for the coding agent

(This file is picked up automatically by agents that look for `AGENTS.md`. If a tool expects a different name, e.g. `CLAUDE.md`, copy this content into it.)

## Start of every session
1. Read `PROJECT.md` fully. It is the source of truth for scope, schema, and the current phase.
2. Work only on the **current phase**. Do not build features from later phases.
3. If a request conflicts with `PROJECT.md`, stop and ask instead of picking one silently.

## How to work
- **Plan before coding.** For any task bigger than a small fix, write a short plan (files to change, approach, risks) and wait for approval.
- **Small steps.** One task per change. Keep diffs small and reviewable; no unrelated refactors or "while I'm here" edits.
- **Ask when unsure.** If requirements, data shapes, or API behavior are unclear, ask. Never guess and never invent API fields or endpoints. Check the official docs for the Google Places API, Supabase, PostGIS, and Leaflet before using them.
- **Verify, don't assume.** After each change, run it and show the result (script output, query results, test output). Say clearly what you ran and what you did not.
- **Be honest about failures.** If something doesn't work or you're unsure it's correct, say so. Do not claim a task is done unless you verified it.

## Code rules
- Keep it simple. Prefer plain, readable code over clever abstractions.
- Ask before adding any new dependency, and say why it's needed.
- **No hardcoded city values.** Every ingestion script takes a city (from the `cities` table) as input.
- Never hardcode or commit secrets. Use environment variables and keep `.env` in `.gitignore`. Provide a `.env.example` with placeholder names.
- Validate and clean external data (missing fields, nulls, duplicates) before saving. Never trust API responses blindly.
- Handle API errors, rate limits, and timeouts with retries and clear logs.
- Make scripts **idempotent**: running an ingestion twice must not create duplicate cafes. Match on `google_place_id`.
- Timestamps in UTC in the database; use `Asia/Kolkata` only when evaluating "open now" for display.

## Database rules
- All schema changes go through **migration files**, never manual edits in the dashboard.
- Never run destructive commands (`DROP`, `TRUNCATE`, bulk `DELETE`, resetting data) without asking first, and say exactly what will be affected.
- Test queries on a small sample before running on the full dataset.
- Keep the GIST index on `cafes.location`; check query plans for the distance filter.

## External APIs and cost
- Google Cloud billing cap must be set before any bulk API run.
- Before a large fetch, estimate the number of API calls and cost, and get approval.
- Cache or store raw responses during development so re-runs don't re-bill.
- Respect provider terms: no scraping Google, Instagram, or TikTok directly. Do not store Google data beyond what `PROJECT.md` allows.

## Testing
- Write tests for the ranking formula, filter queries, "open now" logic, and data-cleaning functions.
- For the distance and filter endpoint, test known cases: expected cafes inside and outside the radius, price filters, tag filters, closed cafes excluded.
- Run existing tests before saying a task is complete.

## Git
- Work on a feature branch, not directly on `main`.
- Small commits with clear messages (`feat:`, `fix:`, `chore:`).
- Do not push or merge without being asked.

## Finishing a task
End every task with a short summary:
1. What changed (files)
2. What was run and the result
3. Anything unverified or risky
4. Suggested next step

Then update `PROJECT.md` (current phase, decisions log) if anything changed.

## Never do
- Build ahead of the current phase
- Invent data, fields, or API behavior
- Commit secrets or `.env` files
- Run destructive database commands without approval
- Add dependencies silently
- Mark something done without verifying it
