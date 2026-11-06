"""OSM Overpass cross-check for one city. Local JSON only — NO database writes.

Reads the city row (center, radius) from the cities table by --city name;
nothing city-specific is hardcoded. Dry-run is the default; --live makes the
one Overpass POST for this city.

OSM data used here is (c) OpenStreetMap contributors, ODbL 1.0 — attribution
required wherever OSM-derived fields are shown (see PROJECT.md section 15).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from db import connect, get_connection_string, masked_host
from raw_store import cache_file_for, load_cached, log_call, new_run_dir, save_response, write_cache

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "cafe-tracker-phase1-fetch/0.1"
BACKOFF_S = [2.0, 4.0, 8.0]


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


def build_query(city: dict, extra_radius_m: int) -> str:
    radius = city["radius_m"] + extra_radius_m
    return (
        f"[out:json][timeout:60];\n"
        f"(\n"
        f'  node["amenity"="cafe"](around:{radius},{city["lat"]},{city["lng"]});\n'
        f'  way["amenity"="cafe"](around:{radius},{city["lat"]},{city["lng"]});\n'
        f'  node["shop"="bakery"](around:{radius},{city["lat"]},{city["lng"]});\n'
        f'  way["shop"="bakery"](around:{radius},{city["lat"]},{city["lng"]});\n'
        f");\n"
        f"out center tags;\n"
    )


def overpass(query: str, run_dir: Path, seq: int, manifest_extra: dict) -> bytes:
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        OVERPASS_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
        method="POST",
    )
    last_exc: Exception | None = None
    for attempt, delay in enumerate([0.0] + BACKOFF_S):
        if attempt:
            print(f"... retry {attempt} after {delay:.0f}s: {last_exc}")
            time.sleep(delay)
        try:
            start = time.monotonic()
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read()
            log_call(run_dir, {
                "script": "fetch_osm", "label": "overpass", "endpoint": OVERPASS_URL,
                "status": 200, "ms": int((time.monotonic() - start) * 1000),
                "bytes": len(data), "reused": False, "attempts": attempt + 1,
                **manifest_extra,
            })
            return data
        except Exception as exc:  # noqa: BLE001 — retry any transport/API error
            last_exc = exc
    raise SystemExit(f"Overpass request failed after retries: {last_exc}")


def summarize(data: bytes) -> dict:
    payload = json.loads(data)
    elements = payload.get("elements", [])
    counts = {"cafe": 0, "bakery": 0, "nodes": 0, "ways": 0}
    names: list[str] = []
    for el in elements:
        tags = el.get("tags") or {}
        counts["nodes" if el.get("type") == "node" else "ways"] += 1
        if tags.get("amenity") == "cafe":
            counts["cafe"] += 1
        elif tags.get("shop") == "bakery":
            counts["bakery"] += 1
        if tags.get("name") and len(names) < 8:
            names.append(tags["name"])
    return {"total": len(elements), "counts": counts, "sample_names": names}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True, help="City name as in the cities table")
    parser.add_argument("--live", action="store_true", help="Actually call Overpass")
    parser.add_argument("--refetch", action="store_true",
                        help="Call Overpass even if a cached response exists")
    parser.add_argument("--pad-m", type=int, default=0,
                        help="Extra radius on top of the city radius (meters)")
    args = parser.parse_args()

    city = load_city(args.city)
    query = build_query(city, args.pad_m)
    digest_source = cache_file_for(OVERPASS_URL, query.encode("utf-8"))

    print(
        f"\nCity: {city['name']} (id {city['id']}) | center lng/lat = "
        f"{city['lng']:.6f}, {city['lat']:.6f} | radius_m = {city['radius_m']} "
        f"(+pad {args.pad_m}, total {city['radius_m'] + args.pad_m} m)\n"
        f"Endpoint: {OVERPASS_URL}\n"
        f"Query:\n{query}"
    )
    print(
        f"Mode: {'LIVE (one Overpass POST, free public API)' if args.live else 'dry-run — zero HTTP calls'}"
    )

    cache_file = cache_file_for(OVERPASS_URL, query.encode("utf-8"), USER_AGENT)
    if not args.live:
        cached = load_cached(cache_file)
        status = "cached response available, would skip HTTP" if cached else "no cached response yet"
        print(f"(dry-run: {status})\nOK — nothing fetched, nothing saved")
        return 0

    run_dir = new_run_dir("osm_")
    seq = 1
    if args.refetch:
        data = overpass(query, run_dir, seq, {"params_hash": digest_source.name})
    else:
        cached = load_cached(cache_file)
        if cached:
            data = cached
            print("cache hit — reused, no call made")
            log_call(run_dir, {"script": "fetch_osm", "label": "overpass",
                               "endpoint": OVERPASS_URL, "reused": True,
                               "bytes": len(data), "params_hash": digest_source.name})
        else:
            data = overpass(query, run_dir, seq, {"params_hash": digest_source.name})

    cache_file.parent.mkdir(parents=True, exist_ok=True)
    write_cache(cache_file, data)
    saved = save_response(run_dir, seq, "overpass", data)
    summary = summarize(data)
    print(f"\nRaw response saved to {saved}")
    print(f"Results: {summary['total']} elements ({summary['counts']})")
    print(f"Sample names: {summary['sample_names']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
