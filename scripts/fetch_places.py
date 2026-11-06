"""Fetch Google Places (New) data for one city to local JSON files.

- Reads the city row (center/radius) from the cities table by --city name;
  no city values are hardcoded here, and NO database writes happen.
- Dry-run is the DEFAULT: prints the full request plan, makes zero HTTP calls.
- --live makes real billed calls. Requires GOOGLE_PLACES_API_KEY in .env and
  GOOGLE_BILLING_CONFIRMED=true (set it after checking the GCP billing cap).
- Hard cap on live HTTP calls: env var PLACES_MAX_CALLS (default 40); budget
  aborts the script when reached (cache hits do not consume budget).
- Every live call is appended to this run's manifest.jsonl; every response is
  saved raw under data/raw/ and cached by params-hash so re-runs don't re-bill.
  Defaults reuse the cache; --refetch forces fresh calls.

Approach (approved plan):
- Nearby Search (New): 3x3 overlapping-circle grid over the city radius; cell
  radius = 2/3 of city radius, spacing = 1/2 of city radius (geometry, not
  city-specific). Max 20 results per call, no pagination. A saturated cell
  (exactly 20 results) splits into 4 sub-cells (radius halved), up to
  NEARBY_GRID_SPLITS levels deep (e.g. 2 -> r 8km, 4km, 2km). Growth-based
  stop: split only when the result still contributes at least
  PLACES_MIN_NEW_PER_CALL (default 8) NEW place ids, so the queue terminates
  on the dense-overlap tail instead of running unbounded.
- Text Search (New): categorical queries ("cafe in <city>", coffee shop,
  bakery, tea house, cafe restaurant) with a circle locationBias; paginates up
  to 3 pages (max 60 results/query per the docs) via nextPageToken.
- Dedupe later happens by places.id (google_place_id) in the load step.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from db import connect, get_connection_string, masked_host
from raw_store import (cache_file_for, load_cached, log_call, new_run_dir,
                       params_hash, save_response, write_cache)

BASE = "https://places.googleapis.com/v1"
ENDPOINT_NEARBY = f"{BASE}/places:searchNearby"
ENDPOINT_TEXT = f"{BASE}/places:searchText"

NEARBY_TYPES = ["cafe", "coffee_shop", "bakery"]
TEXT_TERMS = ["cafe", "coffee shop", "bakery", "tea house", "cafe restaurant"]

MASK_BASE = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.rating,places.userRatingCount,places.regularOpeningHours,"
    "places.websiteUri,places.internationalPhoneNumber,places.priceLevel,"
    "places.businessStatus,places.types,places.primaryType"
)
MASK_NEARBY = MASK_BASE
MASK_TEXT = MASK_BASE + ",nextPageToken"

PAGE_SIZE = 20
MAX_TEXT_PAGES = 3  # Google caps Text Search at 60 results total
NEARBY_GRID_SPLITS = int(os.getenv("PLACES_SPLITS", "2"))  # max split depth; override at run time
NEW_IDS_MIN = int(os.getenv("PLACES_MIN_NEW_PER_CALL", "8"))  # growth-based split stop
MIN_DELAY_S = 0.3  # politeness delay between live calls
BACKOFF_S = [1.0, 2.0, 4.0, 8.0, 16.0]


class CallBudget:
    """Hard cap on LIVE HTTP calls. Cache hits don't count."""

    def __init__(self, cap: int):
        self.cap = cap
        self.used = 0

    def room(self) -> int:
        return self.cap - self.used

    def tick(self, label: str) -> None:
        self.used += 1
        print(f"[budget] live call {self.used}/{self.cap}: {label}")
        if self.used > self.cap:
            raise SystemExit(
                f"ABORT: PLACES_MAX_CALLS={self.cap} exceeded at {label!r} "
                "— stopping before sending further requests."
            )


def load_city(city_name: str) -> dict:
    url = get_connection_string()
    print(f"DB host: {masked_host(url)}")
    with connect(url) as conn:
        row = conn.execute(
            """
            select id, name,
                   extensions.st_x(center::extensions.geometry) as lng,
                   extensions.st_y(center::extensions.geometry) as lat,
                   radius_m, timezone
            from public.cities
            where name = %s
            """,
            (city_name,),
        ).fetchone()
    if not row:
        raise SystemExit(f"City {city_name!r} not found in cities table.")
    return {
        "id": row[0], "name": row[1], "lng": float(row[2]),
        "lat": float(row[3]), "radius_m": row[4], "timezone": row[5],
    }


def deg_offsets(dx_m: float, dy_m: float, lat0: float) -> tuple[float, float]:
    return dy_m / 111_320.0, dx_m / (111_320.0 * math.cos(math.radians(lat0)))


def build_grid(radius_m: int, lat0: float, lng0: float) -> list[dict]:
    """3x3 grid: cell radius 2/3 R, spacing R/2. Pure geometry from the city row."""
    half = radius_m / 2.0
    cell_radius = int(radius_m * 2 / 3)
    cells: list[dict] = []
    for n, (dx, dy) in enumerate([
        (0.0, 0.0),
        (+half, 0.0), (+half, +half), (0.0, +half), (-half, +half),
        (-half, 0.0), (-half, -half), (0.0, -half), (+half, -half),
    ]):
        dlat, dlng = deg_offsets(dx, dy, lat0)
        cells.append({
            "id": f"c{n + 1:02d}", "lat": lat0 + dlat, "lng": lng0 + dlng,
            "radius_m": cell_radius, "split": 0,
        })
    return cells


def nearby_body(cell: dict) -> dict:
    return {
        "includedTypes": NEARBY_TYPES,
        "maxResultCount": PAGE_SIZE,
        "rankPreference": "POPULARITY",
        "locationRestriction": {
            "circle": {
                "center": {"latitude": cell["lat"], "longitude": cell["lng"]},
                "radius": float(cell["radius_m"]),
            }
        },
    }


def text_body(city: dict, term: str) -> dict:
    return {
        "textQuery": f"{term} in {city['name']}",
        "pageSize": PAGE_SIZE,
        "locationBias": {
            "circle": {
                "center": {"latitude": city["lat"], "longitude": city["lng"]},
                "radius": float(city["radius_m"]),
            }
        },
    }


def call_plan(city: dict, cells: list[dict]) -> list[dict]:
    plan = []
    for cell in cells:
        plan.append({
            "kind": "nearby", "label": f"nearby/{cell['id']}",
            "endpoint": ENDPOINT_NEARBY, "mask": MASK_NEARBY,
            "body": nearby_body(cell), "cell": cell, "page": 0,
        })
    for term in TEXT_TERMS:
        plan.append({
            "kind": "text", "label": f"text/{term}", "endpoint": ENDPOINT_TEXT,
            "mask": MASK_TEXT, "body": text_body(city, term),
            "cell": None, "page": 0,
        })
    return plan


def describe(call: dict) -> str:
    body = {k: v for k, v in call["body"].items() if k != "textQuery"}
    if call["kind"] == "nearby":
        cell = call["cell"]
        return f"{call['label']} r={cell['radius_m']}m center=({cell['lat']:.6f},{cell['lng']:.6f})"
    return f"{call['label']} page={call['page'] + 1} bias=({call['body']['locationBias']['circle']['center']['latitude']:.6f},{call['body']['locationBias']['circle']['center']['longitude']:.6f}) r={call['body']['locationBias']['circle']['radius']:.0f}m"


def google_post(endpoint: str, body_bytes: bytes, mask: str, api_key: str) -> tuple[bytes, int, int]:
    """POST with retries: exponential backoff on 429/5xx; other 4xx fatal."""
    req = urllib.request.Request(
        endpoint,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Goog-Api-Key": api_key,  # never logged anywhere
            "X-Goog-FieldMask": mask,
        },
        method="POST",
    )
    last_exc: Exception | None = None
    last_status = 0
    for attempt, delay in enumerate([0.0] + BACKOFF_S):
        if attempt:
            sleep_for = delay * (1.0 + random.random() * 0.5)
            print(f"... backoff {sleep_for:.1f}s (attempt {attempt + 1}) — {last_exc}")
            time.sleep(sleep_for)
        try:
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read(), resp.status, int((time.monotonic() - start) * 1000)
        except urllib.error.HTTPError as exc:
            last_exc, last_status = exc, exc.code
            if exc.code not in (429, 500, 502, 503, 504):
                detail = exc.read().decode("utf-8", "replace")[:500]
                raise SystemExit(f"Google API error {exc.code} on {endpoint}: {detail}")
        except Exception as exc:  # noqa: BLE001 — retry any transport error
            last_exc, last_status = exc, 0
    raise SystemExit(f"Google API kept failing after retries ({last_status}): {last_exc}")


def execute_call(call: dict, args, api_key: str, run_dir, seq: int) -> bytes:
    body_bytes = json.dumps(call["body"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    extra = call["mask"]
    params = params_hash(call["endpoint"], body_bytes, extra)
    cache_file = cache_file_for(call["endpoint"], body_bytes, extra)

    if not args.refetch:
        cached = load_cached(cache_file)
        if cached:
            print(f"cache hit for {call['label']} (hash {params}) — reused, no call")
            log_call(run_dir, {
                "script": "fetch_places", "endpoint": call["endpoint"],
                "label": call["label"], "reused": True, "bytes": len(cached),
                "params_hash": params,
            })
            return cached

    data, status, ms = google_post(call["endpoint"], body_bytes, call["mask"], api_key)
    write_cache(cache_file, data)
    saved = save_response(run_dir, seq, f"{call['label']}_{call['page'] + 1:02d}", data)
    log_call(run_dir, {
        "script": "fetch_places", "endpoint": call["endpoint"], "label": call["label"],
        "page": call["page"] + 1, "status": status, "ms": ms, "bytes": len(data),
        "reused": False, "params_hash": params, "saved": saved.name,
    })
    print(f"fetched {call['label']} -> {saved.name} ({len(data)} bytes, {ms} ms)")
    return data


def run_live(city: dict, cells: list[dict], args, api_key: str) -> None:
    run_dir = new_run_dir("google_")
    cap = int(os.getenv("PLACES_MAX_CALLS", "40"))
    budget = CallBudget(cap)
    print(f"Run dir: {run_dir}\nLive-call cap: PLACES_MAX_CALLS={cap}")

    queue = call_plan(city, cells)
    seen_ids: set[str] = set()
    seq = 0
    while queue:
        call = queue.pop(0)
        budget.tick(describe(call))  # aborts via SystemExit if over cap
        seq += 1
        data = execute_call(call, args, api_key, run_dir, seq)
        try:
            payload = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            print(f"WARNING: non-JSON response for {call['label']}")
            continue

        places = payload.get("places", []) or []
        new_ids = 0
        for p in places:
            pid = p.get("id")
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                new_ids += 1
        print(f"[discovery] {call['label']}: {len(places)} results, {new_ids} new "
              f"(total unique {len(seen_ids)})")

        if call["kind"] == "nearby" and len(places) >= PAGE_SIZE:
            cell = call["cell"]
            if cell["split"] < NEARBY_GRID_SPLITS and budget.room() >= 4 and new_ids >= NEW_IDS_MIN:
                print(f"saturation: {call['label']} returned {len(places)} results "
                      f"({new_ids} new) — splitting to depth {cell['split'] + 1}")
                sub_r = max(500.0, cell["radius_m"] / 2)
                quarter_d, _ = deg_offsets(cell["radius_m"] / 4, cell["radius_m"] / 4, city["lat"])
                for i, (dl, dg) in enumerate([
                    (-quarter_d, -quarter_d), (-quarter_d, +quarter_d),
                    (+quarter_d, -quarter_d), (+quarter_d, +quarter_d),
                ]):
                    sub = {
                        "id": f"{cell['id']}s{i + 1}", "lat": cell["lat"] + dl,
                        "lng": cell["lng"] + dg, "radius_m": int(sub_r), "split": cell["split"] + 1,
                    }
                    queue.append({
                        "kind": "nearby", "label": f"nearby/{sub['id']}",
                        "endpoint": ENDPOINT_NEARBY, "mask": MASK_NEARBY,
                        "body": nearby_body(sub), "cell": sub, "page": 0,
                    })
            elif cell["split"] < NEARBY_GRID_SPLITS:
                print(f"NOTE: {call['label']} saturated but branch stopped "
                      f"(new={new_ids} < {NEW_IDS_MIN} or call-room={budget.room()})")
            else:
                print(f"WARNING: {call['label']} still saturated at max split depth "
                      "— tail relies on sibling branches and text pages")

        if call["kind"] == "text" and payload.get("nextPageToken"):
            if call["page"] + 1 < MAX_TEXT_PAGES:
                nxt = {**call["body"], "pageToken": payload["nextPageToken"]}
                queue.append({
                    "kind": "text", "label": call["label"], "endpoint": ENDPOINT_TEXT,
                    "mask": MASK_TEXT, "body": nxt, "cell": None, "page": call["page"] + 1,
                })
            else:
                print(f"text {call['label']}: page token exists but page cap {MAX_TEXT_PAGES} reached")

    print(f"\nDone. Run dir: {run_dir} | live calls used: {budget.used}/{cap}")
    print("No database writes made. Load step comes later.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True, help="City name as in the cities table")
    parser.add_argument("--live", action="store_true",
                        help="Make real billed calls (default: dry-run only)")
    parser.add_argument("--refetch", action="store_true",
                        help="Call Google even if a cached response exists")
    args = parser.parse_args()

    city = load_city(args.city)
    cells = build_grid(city["radius_m"], city["lat"], city["lng"])
    plan = call_plan(city, cells)

    print(f"\nCity: {city['name']} (id {city['id']}) | center lng/lat = "
          f"{city['lng']:.6f}, {city['lat']:.6f} | radius_m = {city['radius_m']}")
    print(f"Grid: 3x3, cell radius {cells[0]['radius_m']} m, spacing {city['radius_m'] / 2:.0f} m")
    print(f"\nRequest plan ({len(plan)} initial calls):")
    for call in plan:
        print(f"  - {describe(call)}")

    print(
        f"\nRuntime rules: saturated nearby cells split into 4 sub-cells, max depth "
        f"{NEARBY_GRID_SPLITS}, and only while a branch adds >= {NEW_IDS_MIN} new places "
        f"(growth stop); text results paginate up to {MAX_TEXT_PAGES} pages; hard cap "
        f"PLACES_MAX_CALLS (default 40); cache first — re-runs don't re-bill (use --refetch to force)"
    )
    sku = "Nearby/Text Search ENTERPRISE (India): rating/userRatingCount/regularOpeningHours/websiteUri/priceLevel/internationalPhoneNumber are Enterprise fields; no Atmosphere fields requested"
    print(f"Field mask: {MASK_TEXT}\nBilling: {sku}")

    if not args.live:
        print("\nDRY-RUN: 0 HTTP calls made, 0 bytes fetched, no files written. OK.")
        return 0

    api_key = os.getenv("GOOGLE_PLACES_API_KEY")
    if not api_key:
        raise SystemExit("GOOGLE_PLACES_API_KEY is empty in .env — cannot run --live")
    if (os.getenv("GOOGLE_BILLING_CONFIRMED", "").lower() != "true"):
        raise SystemExit(
            "GOOGLE_BILLING_CONFIRMED=true not set in .env — confirm the Google "
            "Cloud billing cap first (AGENTS.md), then set it and re-run."
        )
    run_live(city, cells, args, api_key)
    return 0


if __name__ == "__main__":
    sys.exit(main())
