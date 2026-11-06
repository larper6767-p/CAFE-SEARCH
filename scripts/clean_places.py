"""Step-5 cleaning: classify + normalize fetched Google places into an
owner-review CSV. Local files only — NO database writes.

Owner-approved rules (2026-11-06):
- Auto-include primaryType cafe/coffee_shop/tea_house inside the city radius.
- bakery/cake_shop: default EXCLUDE (bakery_default_exclude); moved to review
  when their `types` list contains a cafe-family type or the name matches the
  keyword list.
- Every other type: review if the name matches keywords, else exclude.
- CLOSED_PERMANENTLY -> exclude (none observed; synthetic tests only).
- >15 km -> exclude (any type). Cafe-family inside 12-15 km -> review with
  reason outside_city_radius_km(<d>).
- Hours normalized into per-weekday JSON (format in the module docstring of
  normalize_hours) BEFORE any insert; missing days = closed (empty list).
- priceLevel -> 1/2/3 band, NULL when missing.
- Same lowercased name within 50 m keeps both rows; flagged in the CSV.
- Names/addresses: NFC + whitespace collapse.

CSV: UTF-8 with BOM, all fields quoted, decision pre-filled on every row,
sorted by review_count descending, written to data/processed/ (gitignored).
The loader (step 6) must validate it through scripts/csv_gate.py before use.
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import math
import os
import sys
import unicodedata
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import connect, get_connection_string, masked_host  # noqa: E402

REVIEW_MAX_KM = 15.0        # approved: beyond this everything is excluded
SAME_NAME_MAX_M = 50.0      # approved dedupe flag threshold
AUTO_TYPES = frozenset({"cafe", "coffee_shop", "tea_house"})
BAKERY_TYPES = frozenset({"bakery", "cake_shop"})
KEYWORDS = (
    "cafe", "café", "coffee", "tea", "chai", "espresso", "brew", "roast",
    "roastery", "bistro", "patisserie", "dessert", "bakehouse", "lounge",
)
PRICE_MAP = {
    "price_level_unspecified": None,
    "price_level_free": 1,
    "price_level_inexpensive": 1,
    "price_level_moderate": 2,
    "price_level_expensive": 3,
    "price_level_very_expensive": 3,
}

CSV_COLUMNS = [
    "google_place_id", "decision", "reason",
    "name", "address", "primary_type", "types",
    "business_status", "price_level", "suggested_price_band",
    "rating", "review_count", "hours_present", "hours_json",
    "lat", "lng", "city_distance_km",
]


def normalize_text(value: str | None) -> str:
    """NFC-normalize, replace NBSP, trim, collapse all whitespace runs."""
    if not value:
        return ""
    value = unicodedata.normalize("NFC", value)
    value = value.replace("\u00a0", " ")
    return " ".join(value.split())


def name_has_keyword(name: str) -> bool:
    folded = normalize_text(name).casefold()
    return any(k in folded for k in KEYWORDS)


def price_band(price_level: str | None) -> int | None:
    if not price_level:
        return None
    return PRICE_MAP.get(price_level.strip().lower())


def normalize_hours(hours: dict | None) -> dict | None:
    """Google regularOpeningHours -> {"mon": [[open, close], ...], ..., "sun": [...]}.

    - day codes 0=Sunday..6=Saturday; output keys mon..sun, all 7 present.
    - times "HHMM" -> "HH:MM"; "2400" -> "24:00".
    - A period without `close` = place is 24/7 -> all days [["00:00","24:00"]].
    - Overnight (close on next day, or close.time <= open.time): the interval
      is split at midnight — [open, "24:00"] on the open day and
      ["00:00", close] on the following day (week wraps sat -> sun).
    - All "0000" -> "0000" (or equal times) means a 24-hour day.
    - Days with no period are closed: empty list. Missing hours data -> None.
    """
    if not hours or not hours.get("periods"):
        return None
    day_key = {0: "sun", 1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat"}
    order = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

    def fmt(t: str | None) -> str:
        t = (t or "").strip() or "0000"
        if not t.isdigit() or len(t) != 4:
            return "00:00"
        return "24:00" if t == "2400" else f"{t[:2]}:{t[2:]}"

    if any(not p.get("close") for p in hours["periods"]):
        return {d: [["00:00", "24:00"]] for d in order}

    out: dict[str, list] = {d: [] for d in order}
    for period in hours["periods"]:
        o, c = period.get("open") or {}, period.get("close") or {}
        od, cd = day_key.get(o.get("day")), day_key.get(c.get("day"))
        if od is None or cd is None:
            continue  # malformed period; skip conservatively
        ot, ct = fmt(o.get("time")), fmt(c.get("time"))
        if od == cd:
            if ot == ct:  # 24-hour day
                out[od].append(["00:00", "24:00"])
            elif ot < ct:
                out[od].append([ot, ct])
            else:  # overnight reported against the same day: split at midnight
                out[od].append([ot, "24:00"])
                nxt = day_key[(o["day"] + 1) % 7]
                if ct != "00:00":
                    out[nxt].append(["00:00", ct])
        else:
            out[od].append([ot, "24:00"])
            if ct != "00:00":
                out[cd].append(["00:00", ct])

    for d in order:
        seen, merged = set(), []
        for interval in sorted(out[d]):
            if interval[0] == interval[1]:  # zero-length, drop
                continue
            key = (interval[0], interval[1])
            if key not in seen:
                seen.add(key)
                merged.append(list(interval))
        out[d] = merged
    return out


def classify_place(place: dict, dist_km: float, city_radius_km: float) -> tuple[str, str]:
    """Return (decision, reason) per the approved rules."""
    status = (place.get("businessStatus") or "").strip().lower()
    if status == "closed_permanently":
        return "exclude", "closed_permanently"
    if dist_km > REVIEW_MAX_KM:
        return "exclude", f"outside_city_radius_km({dist_km:.2f})"

    primary = (place.get("primaryType") or "").strip().lower() or None
    types = [t.strip().lower() for t in (place.get("types") or [])]
    name = place.get("displayName", {}).get("text") or ""
    keyword_hit = name_has_keyword(name)

    if primary in AUTO_TYPES or primary is None and any(t in AUTO_TYPES for t in types):
        base_decision, reason = "include", "type_include"
    elif primary in BAKERY_TYPES or primary is None and any(t in BAKERY_TYPES for t in types):
        if any(t in AUTO_TYPES for t in types) or keyword_hit:
            return "review", "bakery_cafe_signal"
        return "exclude", "bakery_default_exclude"
    elif primary is None:
        base_decision = keyword_hit and "review" or "exclude"
        reason = "cafe_like_name" if keyword_hit else "type_exclude"
    elif keyword_hit:
        return "review", "cafe_like_name"
    else:
        return "exclude", "type_exclude"

    if base_decision == "include" and dist_km > city_radius_km:
        return "review", f"outside_city_radius_km({dist_km:.2f})"
    return base_decision, reason


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def scan_same_name_flags(places: list[dict]) -> set[str]:
    """IDs of places sharing a lowercased normalized name within 50 m."""
    by_name: dict[str, list[dict]] = {}
    for p in places:
        loc = p.get("location") or {}
        if loc.get("latitude") is None:
            continue
        by_name.setdefault(normalize_text(
            (p.get("displayName") or {}).get("text", "")).casefold(), []).append(p)
    flagged: set[str] = set()
    for _, group in by_name.items():
        if len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                d = haversine_m(a["location"]["latitude"], a["location"]["longitude"],
                                b["location"]["latitude"], b["location"]["longitude"])
                if d <= SAME_NAME_MAX_M:
                    flagged.add(a["id"])
                    flagged.add(b["id"])
    return flagged


def load_city(city_name: str) -> dict:
    url = get_connection_string()
    print(f"DB host: {masked_host(url)} (read-only)")
    with connect(url) as conn:
        row = conn.execute(
            """
            select id, name,
                   extensions.st_x(center::extensions.geometry) as lng,
                   extensions.st_y(center::extensions.geometry) as lat,
                   radius_m
            from public.cities where name = %s
            """,
            (city_name,),
        ).fetchone()
    if not row:
        raise SystemExit(f"City {city_name!r} not found in cities table.")
    return {"id": row[0], "name": row[1], "lng": float(row[2]),
            "lat": float(row[3]), "radius_m": float(row[4])}


def union_places(raw_root: str) -> dict[str, dict]:
    unique: dict[str, dict] = {}
    for path in sorted(glob.glob(os.path.join(raw_root, "google_*", "*.json"))):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for place in data.get("places") or []:
            pid = place.get("id")
            if pid:
                unique.setdefault(pid, place)
    return unique


def write_csv(rows: list[dict], path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def sort_key(row: dict):
    try:
        rc = float(row["review_count"]) if row["review_count"] != "" else -1.0
    except ValueError:
        rc = -1.0
    return (-rc, normalize_text(row["name"]).casefold(), row["google_place_id"])


def build_rows(city: dict, places: dict[str, dict], raw_root: str) -> list[dict]:
    flagged_same_name = scan_same_name_flags(list(places.values()))
    center_lat, center_lng, radius_km = city["lat"], city["lng"], city["radius_m"] / 1000.0

    rows: list[dict] = []
    for pid, place in places.items():
        name = normalize_text((place.get("displayName") or {}).get("text"))
        address = normalize_text(place.get("formattedAddress"))
        raw_types = [t.strip().lower() for t in (place.get("types") or [])]
        status = (place.get("businessStatus") or "").strip().lower()
        band = price_band(place.get("priceLevel"))
        hours = normalize_hours(place.get("regularOpeningHours"))
        loc = place.get("location") or {}

        if loc.get("latitude") is None:
            decision, reason, dist_km = "exclude", "missing_location", 0.0
        else:
            dist_km = haversine_m(center_lat, center_lng, loc["latitude"], loc["longitude"]) / 1000.0
            decision, reason = classify_place(place, dist_km, radius_km)

        flags: list[str] = []
        if pid in flagged_same_name:
            flags.append("same_name_within_50m")

        rows.append({
            "google_place_id": pid,
            "decision": decision,
            "reason": "|".join([reason, *flags]),
            "name": name,
            "address": address,
            "primary_type": (place.get("primaryType") or "").strip().lower(),
            "types": ";".join(raw_types),
            "business_status": status,
            "price_level": (place.get("priceLevel") or "").strip().lower(),
            "suggested_price_band": "" if band is None else str(band),
            "rating": "" if place.get("rating") is None else str(place["rating"]),
            "review_count": "" if place.get("userRatingCount") is None else str(place["userRatingCount"]),
            "hours_present": "true" if hours else "false",
            "hours_json": json.dumps(hours, ensure_ascii=False, separators=(",", ":")) if hours else "",
            "lat": "" if loc.get("latitude") is None else str(loc["latitude"]),
            "lng": "" if loc.get("longitude") is None else str(loc["longitude"]),
            "city_distance_km": f"{dist_km:.2f}",
        })
    rows.sort(key=sort_key)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True, help="City name as in the cities table")
    args = parser.parse_args()

    raw_root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "raw")
    city = load_city(args.city)
    places = union_places(raw_root)
    if not places:
        raise SystemExit(f"No cached Google responses found under {raw_root}")

    rows = build_rows(city, places, raw_root)
    print(f"\nUnique places: {len(rows)} (city radius {city['radius_m']:.0f} m, center "
          f"{city['lat']:.6f},{city['lng']:.6f})")

    decisions: dict[str, int] = {}
    reasons: dict[str, int] = {}
    for row in rows:
        decisions[row["decision"]] = decisions.get(row["decision"], 0) + 1
        reasons[row["reason"].split("|")[0]] = reasons.get(row["reason"].split("|")[0], 0) + 1
    print("Decision counts:", dict(sorted(decisions.items())))
    print("Primary-reason counts:", dict(sorted(reasons.items())))

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "processed", f"step5_review_{run_id}.csv")
    write_csv(rows, out_path)
    print(f"\nReview CSV written: {out_path}")
    print(f"{sum(1 for r in rows if r['review_count'])} rows with review_count; "
          f"{sum(1 for r in rows if r['hours_present'] == 'true')} with normalized hours; "
          f"{sum(1 for r in rows if r['suggested_price_band'])} with price band")
    print("NO database writes were performed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
