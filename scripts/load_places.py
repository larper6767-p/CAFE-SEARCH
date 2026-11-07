"""Step-6 loader: writes owner-approved include rows from the review CSV into
public.cafes inside ONE transaction. Dry-run is the DEFAULT; --commit is
required for a real load. Curated data is never overwritten.

Writes (approved matrix): name, address, location (lng FIRST), phone,
website, opening_hours, status, google_rating, google_review_count,
price_band (fill only when existing is NULL or price_band_source='google'),
price_band_source, google_place_id, source='google', and
google_data_fetched_at = BATCH FETCH timestamp (max manifest ts across
data/raw/google_*; never now()). last_verified is NOT written by the loader.

The marked review CSV stays authoritative for decisions; phone/website are
not review-CSV columns, so the loader takes them from the raw fetch cache
(same place ids, same batch). Decisions survive purges separately in
data/decisions/ (tracked).

Validation before any write (hard errors):
- csv_gate.validate_structure against the fetched place-id universe
- only decision == 'include' rows load (filter_include_rows)
- coords parse, in range, <= 15 km from the city center (haversine)
- status in approved set; hours_json shape (7 weekday keys, HH:MM intervals)
- price_band in 1..3 or empty; rating in 0..5; review_count >= 0
Pre-flight: cities row present; cafes.google_data_fetched_at column present.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clean_places as cp  # noqa: E402
import csv_gate  # noqa: E402

from db import connect, get_connection_string, masked_host  # noqa: E402

STATUS_VALUES = frozenset({"operational", "closed_temporarily", "closed_permanently"})
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
PRICE_BANDS = {1, 2, 3}
MAX_LOAD_RADIUS_M = 15_000.0
TIME_RE = re.compile(r"([01]\d|2[0-4]):[0-5]\d")

UPSERT_SQL = """
insert into public.cafes
    (city_id, name, address, location, phone, website, opening_hours, status,
     source, google_place_id, google_rating, google_review_count,
     price_band, price_band_source, google_data_fetched_at)
values
    (%(city_id)s, %(name)s, %(address)s,
     st_setsrid(st_makepoint(%(lng)s, %(lat)s), 4326)::extensions.geography,
     %(phone)s, %(website)s, %(opening_hours)s::jsonb, %(status)s,
     'google', %(google_place_id)s, %(rating)s, %(review_count)s,
     %(price_band)s, %(price_band_source)s, %(google_data_fetched_at)s)
on conflict (google_place_id) do update set
    name = excluded.name,
    address = excluded.address,
    location = excluded.location,
    phone = excluded.phone,
    website = excluded.website,
    opening_hours = excluded.opening_hours,
    status = excluded.status,
    google_rating = excluded.google_rating,
    google_review_count = excluded.google_review_count,
    price_band = case
        when public.cafes.price_band_source = 'manual'
            then public.cafes.price_band
        else excluded.price_band
    end,
    price_band_source = case
        when public.cafes.price_band_source = 'manual'
            then public.cafes.price_band_source
        when excluded.price_band is not null then 'google'
        else null
    end,
    google_data_fetched_at = excluded.google_data_fetched_at
returning (xmax = 0) as inserted, public.cafes.name as db_name
"""


def load_city(city_name: str) -> dict:
    url = get_connection_string()
    print(f"DB host: {masked_host(url)} (pre-check, read-only)")
    with connect(url) as conn:
        row = conn.execute(
            """
            select id, name, radius_m,
                   extensions.st_y(center::extensions.geometry) as lat,
                   extensions.st_x(center::extensions.geometry) as lng
            from public.cities where name = %s
            """,
            (city_name,),
        ).fetchone()
        col = conn.execute(
            """
            select exists (select 1 from information_schema.columns
                           where table_schema='public' and table_name='cafes'
                             and column_name='google_data_fetched_at')
            """
        ).fetchone()
    if not row:
        raise SystemExit(f"City {city_name!r} not found in cities table.")
    if not col[0]:
        raise SystemExit("cafes.google_data_fetched_at missing — apply the "
                         "20261107000000 migration via scripts/apply_migrations.py first.")
    return {"id": row[0], "name": row[1], "radius_m": float(row[2]),
            "lat": float(row[3]), "lng": float(row[4])}


def batch_fetch_timestamp(raw_root: str) -> str | None:
    """Max manifest ts_utc across google_* runs (all +00:00, lexicographic OK)."""
    best = None
    for manifest in glob.glob(os.path.join(raw_root, "google_*", "manifest.jsonl")):
        with open(manifest, encoding="utf-8") as fh:
            for line in fh:
                try:
                    ts = json.loads(line).get("ts_utc")
                except (ValueError, json.JSONDecodeError):
                    continue
                if ts and (best is None or ts > best):
                    best = ts
    return best


def newest_review_csv(processed_root: str) -> str:
    files = glob.glob(os.path.join(processed_root, "step5_review_*.csv"))
    if not files:
        raise SystemExit(f"No step5_review_*.csv under {processed_root}")
    return max(files, key=os.path.getmtime)


def _check_hours_shape(hours_json: str) -> dict | None:
    """Validate hours_json shape; return the dict or None; raise ValueError."""
    if hours_json == "":
        return None
    try:
        hours = json.loads(hours_json)
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"hours_json is not valid JSON ({exc})") from None
    if not isinstance(hours, dict) or sorted(hours.keys()) != sorted(WEEKDAYS):
        raise ValueError(f"hours_json keys must be exactly {list(WEEKDAYS)}")
    for day in WEEKDAYS:
        intervals = hours[day]
        if not isinstance(intervals, list):
            raise ValueError(f"hours_json[{day!r}] must be a list")
        for interval in intervals:
            if not (isinstance(interval, list) and len(interval) == 2
                    and all(isinstance(x, str) for x in interval)):
                raise ValueError(f"hours_json[{day!r}] interval is not a [str, str] pair")
            op, cl = interval
            if not (TIME_RE.fullmatch(op) and TIME_RE.fullmatch(cl)):
                raise ValueError(f"hours_json[{day!r}] interval {interval} not HH:MM (<= 24:00)")
            if op >= cl:
                raise ValueError(f"hours_json[{day!r}] interval {interval} has open >= close")
    return hours


def validate_row(row: dict, city_lat: float, city_lng: float) -> tuple[dict, list[str]]:
    """Return (sql_params, errors). Pure; used by tests."""
    errors: list[str] = []
    params: dict = {
        "google_place_id": (row.get("google_place_id") or "").strip(),
        "city_id": None,
        "lat": None, "lng": None,
        "opening_hours": None,
        "price_band": None, "price_band_source": None,
        "rating": None, "review_count": None,
        "phone": None, "website": None,
    }
    if not params["google_place_id"]:
        errors.append("empty google_place_id")

    name = cp.normalize_text(row.get("name"))
    if not name:
        errors.append("empty name after normalization")
    params["name"] = name
    params["address"] = cp.normalize_text(row.get("address")) or None

    try:
        lat, lng = float(row.get("lat")), float(row.get("lng"))
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            errors.append("lat/lng out of range")
            lat, lng = None, None
    except (TypeError, ValueError):
        errors.append("lat/lng missing or not numeric")
        lat, lng = None, None
    if lat is not None:
        dist = cp.haversine_m(city_lat, city_lng, lat, lng)
        if dist > MAX_LOAD_RADIUS_M:
            errors.append(f"coords {dist / 1000:.2f} km from center exceed the 15 km load window")
        else:
            params["lat"], params["lng"] = lat, lng

    status = (row.get("business_status") or "").strip().lower()
    if status not in STATUS_VALUES:
        errors.append(f"invalid status {status!r}")
    params["status"] = status

    try:
        hours = _check_hours_shape(row.get("hours_json") or "")
    except ValueError as exc:
        errors.append(str(exc))
        hours = None
    params["opening_hours"] = (json.dumps(hours, ensure_ascii=False, separators=(",", ":"))
                               if hours else None)

    band_raw = (row.get("suggested_price_band") or "").strip()
    if band_raw != "":
        try:
            band = int(band_raw)
        except ValueError:
            band = -1
        if band not in PRICE_BANDS:
            errors.append(f"invalid price_band {band_raw!r}")
        else:
            params["price_band"] = band
    params["price_band_source"] = "google" if params["price_band"] is not None else None

    rating_raw = (row.get("rating") or "").strip()
    if rating_raw != "":
        try:
            rating = float(rating_raw)
        except (TypeError, ValueError):
            rating = -1.0
        if not (0 <= rating <= 5):
            errors.append(f"rating {rating_raw!r} outside 0..5")
        else:
            params["rating"] = rating

    rc_raw = (row.get("review_count") or "").strip()
    if rc_raw != "":
        try:
            rc = int(rc_raw)
        except (TypeError, ValueError):
            rc = -1
        if rc < 0:
            errors.append(f"review_count {rc_raw!r} negative")
        else:
            params["review_count"] = rc

    params["phone"] = cp.normalize_text(row.get("phone")) or None
    params["website"] = cp.normalize_text(row.get("website")) or None
    return params, errors


def load_rows(cur, params_list: list[dict], city_id: int, batch_ts: datetime) -> dict:
    """Upsert via cur; caller owns the transaction. Returns counts + names."""
    counts = {"inserted": 0, "updated": 0}
    names: list[str] = []
    for params in params_list:
        payload = dict(params)
        payload["city_id"] = city_id
        payload["google_data_fetched_at"] = batch_ts
        cur.execute(UPSERT_SQL, payload)
        inserted, db_name = cur.fetchone()
        if inserted:
            counts["inserted"] += 1
        else:
            counts["updated"] += 1
        names.append(db_name)
    return {"counts": counts, "names": names}


def export_decisions(rows: list[dict], path: str) -> None:
    """Durable decisions copy (id, decision, notes) — tracked, no Google fields."""
    import csv as csvmod
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csvmod.DictWriter(fh, fieldnames=["id", "decision", "notes"],
                                   quoting=csvmod.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "id": row.get("google_place_id", ""),
                "decision": row.get("decision", ""),
                "notes": row.get("notes", "") or "",
            })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True)
    parser.add_argument("--commit", action="store_true",
                        help="COMMIT the load (default: dry-run + rollback)")
    parser.add_argument("--csv", help="explicit review CSV path (default: newest in data/processed)")
    parser.add_argument("--decisions-out", help="also write decisions-only CSV to this path")
    parser.add_argument("--batch-ts", help="override batch fetch timestamp (ISO-8601)")
    args = parser.parse_args()

    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw_root = os.path.join(base, "data", "raw")
    processed_root = os.path.join(base, "data", "processed")

    city = load_city(args.city)

    csv_path = args.csv or newest_review_csv(processed_root)
    rows = csv_gate.read_approved_csv(csv_path)
    errors = csv_gate.validate_structure(rows, list(cp.union_places(raw_root)))
    if errors:
        raise SystemExit("CSV gate failed:\n  " + "\n  ".join(errors[:10]))
    print(f"CSV: {csv_path} | rows {len(rows)} | gate integrity OK")

    kept = csv_gate.filter_include_rows(rows)
    print(f"loader-eligible rows: {len(kept)} (refused non-include {len(rows) - len(kept)})")

    # phone/website come from the raw cache (not review-CSV columns).
    raw = cp.union_places(raw_root)
    for row in kept:
        place = raw.get(row.get("google_place_id")) or {}
        row["phone"] = place.get("internationalPhoneNumber") or ""
        row["website"] = place.get("websiteUri") or ""

    batch_ts_str = args.batch_ts or batch_fetch_timestamp(raw_root)
    if not batch_ts_str:
        raise SystemExit("no manifest ts under data/raw (purged?) — pass --batch-ts")
    batch_ts = datetime.fromisoformat(batch_ts_str)
    print(f"batch fetch timestamp: {batch_ts.isoformat()}")

    params_list: list[dict] = []
    validation_errors: dict[str, list[str]] = {}
    for row in kept:
        params, row_errors = validate_row(row, city["lat"], city["lng"])
        if row_errors:
            validation_errors[params["google_place_id"]] = row_errors
        else:
            params_list.append(params)
    if validation_errors:
        raise SystemExit(f"validation errors on {len(validation_errors)}/{len(kept)} rows; "
                         f"first: {list(validation_errors.items())[0]}")
    print(f"validated rows: {len(params_list)} — all checks passed")

    if args.decisions_out:
        export_decisions(rows, args.decisions_out)
        print(f"decisions-only CSV written: {args.decisions_out} (for git tracking)")

    conn = psycopg.connect(get_connection_string())  # txn managed explicitly below
    try:
        with conn.cursor() as cur:
            pre = cur.execute(
                """
                select coalesce(array_agg(google_place_id), '{}')
                from public.cafes
                where source = 'google' and google_place_id = any(%s)
                """,
                ([p["google_place_id"] for p in params_list],),
            ).fetchone()[0]
            expected_inserts = sum(1 for p in params_list
                                   if p["google_place_id"] not in set(pre))
            expected_updates = len(params_list) - expected_inserts
            result = load_rows(cur, params_list, city["id"], batch_ts)
        got = result["counts"]
        match = (got["inserted"] == expected_inserts
                 and got["updated"] == expected_updates)
        print(f"\ncounts: inserted {got['inserted']} / updated {got['updated']} "
              f"(pre-snapshot expected {expected_inserts}/{expected_updates} — match: {match})")
        print("first rows:", ", ".join(result["names"][:10]))
        if not match:
            raise RuntimeError("RETURNING counts disagree with pre-snapshot")
        if args.commit:
            conn.commit()
            print("\nCOMMITTED.")
        else:
            conn.rollback()
            print("\nDRY-RUN: transaction rolled back — NO database writes.")
    except Exception:
        conn.rollback()
        print("\nERROR — transaction rolled back; nothing written.")
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
