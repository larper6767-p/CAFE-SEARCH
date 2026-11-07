"""Step-7 importer: owner-curated manual rows from a tracked CSV (dry-run DEFAULT).

Owner-approved step-7 decisions:
- Always writes source = 'manual'; price_band_source = 'manual' when a band is
  given (else both NULL). status = 'operational' (fixed). google_place_id stays
  NULL — the loader's ON CONFLICT (google_place_id) can never match a NULL, so
  loader upserts can never touch a manual row.
- Manual rows may be up to 15 km from the city center; the row distance is
  printed in kilometres for every accepted row.
- Coordinates MUST come from phone GPS or OpenStreetMap — never copied from
  Google Maps (PROJECT.md section 9).
- Hours: light syntax, e.g. "mon-fri 09:00-18:00; sat 09:00-15:00, 19:00-22:00;
  sun closed" — day tokens/ranges mon..sun, intervals HH:MM-HH:MM (single-digit
  hours padded), 'closed' keyword, multi-intervals comma-separated, missing
  days = closed, empty cell = NULL. Overnight = two clauses
  ("sat 18:00-24:00; sun 00:00-02:00"); open must be < close per interval.
- Hard errors (no partial import): template file refused (data/manual/
  *_template*.csv is documentation only); exact header match required (UTF-8
  BOM mandatory); name non-empty after normalization; lat/lng numeric, in
  range, <= 15 km from center; hours grammar; price_band in 1..3; last_verified
  a YYYY-MM-DD date; tags must exist in the tags table; two rows in one file
  with the same normalized name <= 50 m apart are refused.
- Dedupe identity = normalized(name) + coords <= 50 m:
    re-import of an existing manual row → SKIPPED ('already imported')
    near-match vs a GOOGLE row (same name, <= 50 m) → inserted anyway, reported
    ('[near match] ... never merged')
- instagram_url / notes need the staged migration 20261109100000; rows filling
  those cells while the columns are missing are refused.
- Dry-run lists every row verdict; --commit performs ONE transaction.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import date

import psycopg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clean_places as cp  # noqa: E402

from db import get_connection_string, masked_host  # noqa: E402

FIELDS = ["name", "address", "lat", "lng", "hours", "price_band", "tags",
          "phone", "website", "instagram_url", "notes", "last_verified"]
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DAY_INDEX = {d: i for i, d in enumerate(DAYS)}
MAX_RADIUS_M = 15_000.0      # manual rows may sit up to 15 km from the center
SAME_CAFE_M = 50.0           # dedupe proximity
INSTAGRAM_RE = re.compile(r"^https?://(www\.)?instagram\.com/[\w./-]+/?$", re.IGNORECASE)
INTERVAL_RE = re.compile(r"^(\d{1,2}):([0-5]\d)\s*-\s*(\d{1,2}):([0-5]\d)$")


class ImporterError(Exception):
    """Hard import error: nothing is written."""


def pad_time(hours: str, minutes: str) -> str:
    return f"{int(hours):02d}:{minutes}"


def add_days(token: str) -> list[str]:
    token = token.strip().lower()
    rng = re.fullmatch(r"([a-z]{3})-([a-z]{3})", token)
    if rng:
        a, b = rng.group(1), rng.group(2)
        if a not in DAY_INDEX or b not in DAY_INDEX:
            raise ValueError(f"unknown day range {token!r}")
        ia, ib = DAY_INDEX[a], DAY_INDEX[b]
        if ib < ia:
            raise ValueError(f"day range {token!r} wraps — list the days explicitly")
        return list(DAYS[ia:ib + 1])
    if token in DAY_INDEX:
        return [token]
    raise ValueError(f"unknown day token {token!r}")


def parse_hours_light(text: str | None) -> dict | None:
    """Light-syntax hours → per-weekday interval dict; None when the cell is empty."""
    raw = (text or "").strip()
    if not raw:
        return None
    hours: dict[str, list] = {d: [] for d in DAYS}
    for chunk in raw.split(";"):
        part = chunk.strip()
        if not part:
            continue
        m = re.match(r"^([A-Za-z,\- ]+?)\s+(.*)$", part)
        if not m:
            raise ValueError(f"hours clause {chunk!r} needs 'days intervals'")
        days_part, rest = m.group(1).strip(), m.group(2).strip()
        targets: list[str] = []
        for token in [s.strip() for s in days_part.split(",") if s.strip()]:
            targets.extend(add_days(token))
        if not targets:
            raise ValueError(f"hours clause {chunk!r} has no days")
        intervals: list[list[str]] = []
        if is_closed_keyword(rest):
            intervals = []
        else:
            for piece in [s.strip() for s in rest.split(",") if s.strip()]:
                im = INTERVAL_RE.fullmatch(piece)
                if not im:
                    raise ValueError(f"interval {piece!r} is not HH:MM-HH:MM")
                op = pad_time(im.group(1), im.group(2))
                cl = pad_time(im.group(3), im.group(4))
                if op >= cl:
                    raise ValueError(f"interval {piece!r} has open >= close "
                                     "(overnight: split across two day clauses)")
                intervals.append([op, cl])
        for d in targets:
            hours[d].extend(intervals)
    return hours


def is_closed_keyword(rest: str) -> bool:
    return rest.strip().lower() == "closed"


def hours_json(hours_text: str | None) -> tuple[str | None, list[str]]:
    try:
        hours = parse_hours_light(hours_text)
    except ValueError as exc:
        return None, [str(exc)]
    if hours is None:
        return None, []
    return json.dumps(hours, ensure_ascii=False, separators=(",", ":")), []


def validate_manual_row(row: dict, city_lat: float, city_lng: float,
                        known_tags: frozenset[str],
                        has_notes_cols: bool) -> tuple[dict, list[str], float]:
    """Validate one CSV row; returns (params, errors, distance_m)."""
    errors: list[str] = []
    params: dict = {
        "name": None, "address": None, "lat": None, "lng": None,
        "opening_hours": None, "price_band": None, "price_band_source": None,
        "phone": None, "website": None, "instagram_url": None,
        "notes": None, "last_verified": None, "tags": None,
    }
    name = cp.normalize_text(row.get("name"))
    if not name:
        errors.append("empty name after normalization")
    params["name"] = name
    params["address"] = cp.normalize_text(row.get("address")) or None

    distance_m = 0.0
    try:
        lat, lng = float(row.get("lat")), float(row.get("lng"))
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lng <= 180.0):
            errors.append("lat/lng out of range")
            lat = lng = None
    except (TypeError, ValueError):
        errors.append("lat/lng missing or not numeric")
        lat = lng = None
    if lat is not None:
        distance_m = cp.haversine_m(city_lat, city_lng, lat, lng)
        if distance_m > MAX_RADIUS_M:
            errors.append(f"{distance_m / 1000:.2f} km from center exceeds the 15 km manual window")
        else:
            params["lat"], params["lng"] = lat, lng

    params["opening_hours"], hours_errors = hours_json(row.get("hours"))
    errors.extend(hours_errors)

    band_raw = (row.get("price_band") or "").strip()
    if band_raw:
        try:
            band = int(band_raw)
        except ValueError:
            band = -1
        if band not in (1, 2, 3):
            errors.append(f"invalid price_band {band_raw!r}")
        else:
            params["price_band"] = band
            params["price_band_source"] = "manual"   # owner decision

    tags_raw = (row.get("tags") or "").strip()
    if tags_raw:
        requested = [cp.normalize_text(t).lower() for t in tags_raw.split(";") if t.strip()]
        unknown = [t for t in requested if t not in known_tags]
        if unknown:
            errors.append(f"unknown tag(s) {unknown} — allowed: {sorted(known_tags)}")
        else:
            params["tags"] = requested

    params["phone"] = cp.normalize_text(row.get("phone")) or None
    params["website"] = cp.normalize_text(row.get("website")) or None

    insta_raw = (row.get("instagram_url") or "").strip()
    if insta_raw:
        if not has_notes_cols:
            errors.append("instagram_url filled but cafes.instagram_url is missing — "
                          "apply staged migration 20261109100000 first")
        elif not INSTAGRAM_RE.fullmatch(insta_raw):
            errors.append(f"instagram_url {insta_raw!r} is not an instagram.com profile URL")
        else:
            params["instagram_url"] = insta_raw

    notes_raw = (row.get("notes") or "").strip()
    if notes_raw:
        if not has_notes_cols:
            errors.append("notes filled but cafes.notes is missing — "
                          "apply staged migration 20261109100000 first")
        else:
            params["notes"] = cp.normalize_text(notes_raw)

    lv_raw = (row.get("last_verified") or "").strip()
    if lv_raw:
        try:
            params["last_verified"] = date.fromisoformat(lv_raw)
        except ValueError:
            errors.append(f"last_verified {lv_raw!r} is not YYYY-MM-DD")
    return params, errors, distance_m


def read_manual_csv(path: str) -> list[dict]:
    with open(path, "rb") as fh:
        if fh.read(3) != b"\xef\xbb\xbf":
            raise ImporterError(
                f"{os.path.basename(path)} is missing the UTF-8 BOM — "
                "re-save as 'CSV UTF-8' (usually Excel's default)")
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        if fields != FIELDS:
            raise ImporterError(f"header mismatch.\nexpected: {FIELDS}\nfound:    {fields}")
        rows = list(reader)
    if not rows:
        raise ImporterError("no data rows")
    for i, row in enumerate(rows, start=2):
        row["_line"] = i
    return rows


def ensure_not_template(path: str) -> None:
    """The template file is documentation only — importing it is always refused."""
    if "template" in os.path.basename(path).lower():
        raise SystemExit(
            f"refused: {os.path.basename(path)} is the template file. "
            "Copy it to a new name (e.g. imphal_manual.csv) first and edit the copy.")


def find_intra_file_duplicates(rows: list[dict]) -> None:
    """Refuse two rows in ONE file with the same normalized name <= 50 m apart."""
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            if (cp.normalize_text(rows[i].get("name"))
                    == cp.normalize_text(rows[j].get("name"))
                    and _dist_meter(rows[i], rows[j]) <= SAME_CAFE_M):
                raise ImporterError(
                    f"duplicate rows within the file: line {rows[i]['_line']} and "
                    f"line {rows[j]['_line']} (same name, <= 50 m) — no write performed")


def _dist_meter(a: dict, b: dict) -> float:
    try:
        return cp.haversine_m(float(a["lat"]), float(a["lng"]),
                              float(b["lat"]), float(b["lng"]))
    except (TypeError, ValueError, KeyError):
        return float("inf")


def find_match(params: dict, existing: list[tuple]) -> tuple[str | None, list[tuple]]:
    """Dedupe lookup. Returns ('manual', ...) when a manual row should be skipped,
    or (None, google_near_matches) with reports."""
    name = cp.normalize_text(params.get("name") or "")
    reports: list[tuple] = []
    for src, name_db, lat_db, lng_db, gid in existing:
        if lat_db is None or params.get("lat") is None:
            continue
        if cp.normalize_text(name_db or "") != name:
            continue
        dist = cp.haversine_m(params["lat"], params["lng"], lat_db, lng_db)
        if dist > SAME_CAFE_M:
            continue
        if src == "manual":
            return "manual", reports
        reports.append((gid, name_db, dist))
    return None, reports


def _load_city(cur, city_name: str) -> dict:
    cur.execute(
        """
        select id, radius_m,
               extensions.st_y(center::extensions.geometry),
               extensions.st_x(center::extensions.geometry)
        from public.cities where name = %s
        """,
        (city_name,),
    )
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"City {city_name!r} not found in the cities table")
    return {"id": row[0], "lat": float(row[2]), "lng": float(row[3])}


def _columns_present(cur, names) -> bool:
    cur.execute(
        """
        select count(*) from information_schema.columns
        where table_schema = 'public' and table_name = 'cafes'
          and column_name = any(%s)
        """,
        (list(names),),
    )
    return cur.fetchone()[0] == len(names)


def _existing_rows(cur) -> list[tuple]:
    cur.execute(
        """
        select source, name,
               extensions.st_y(location::extensions.geometry),
               extensions.st_x(location::extensions.geometry),
               google_place_id
        from public.cafes
        """
    )
    return cur.fetchall()


def insert_manual_row(cur, params: dict, city_id: int, has_notes_cols: bool) -> int:
    if has_notes_cols:
        cur.execute(
            """
            insert into public.cafes
                (city_id, name, address, location, phone, website, opening_hours,
                 status, source, price_band, price_band_source,
                 instagram_url, notes, last_verified)
            values (%(city_id)s, %(name)s, %(address)s,
                    st_setsrid(st_makepoint(%(lng)s, %(lat)s), 4326)::extensions.geography,
                    %(phone)s, %(website)s, %(opening_hours)s::jsonb,
                    'operational', 'manual', %(price_band)s, %(price_band_source)s,
                    %(instagram_url)s, %(notes)s, %(last_verified)s)
            returning id
            """,
            {**params, "city_id": city_id},
        )
    else:
        cur.execute(
            """
            insert into public.cafes
                (city_id, name, address, location, phone, website, opening_hours,
                 status, source, price_band, price_band_source, last_verified)
            values (%(city_id)s, %(name)s, %(address)s,
                    st_setsrid(st_makepoint(%(lng)s, %(lat)s), 4326)::extensions.geography,
                    %(phone)s, %(website)s, %(opening_hours)s::jsonb,
                    'operational', 'manual', %(price_band)s, %(price_band_source)s,
                    %(last_verified)s)
            returning id
            """,
            {**params, "city_id": city_id},
        )
    cafe_id = cur.fetchone()[0]
    for tag in params.get("tags") or []:
        cur.execute(
            """
            insert into public.cafe_tags (cafe_id, tag_id)
            values (%s, (select id from public.tags where lower(name) = %s))
            on conflict (cafe_id, tag_id) do nothing
            """,
            (cafe_id, tag),
        )
    return cafe_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--city", required=True, help="City name as in the cities table")
    parser.add_argument("--csv", required=True, help="manual CSV path (data/manual/...)")
    parser.add_argument("--commit", action="store_true",
                        help="insert for real (default: dry-run + rollback)")
    args = parser.parse_args()

    ensure_not_template(args.csv)

    url = get_connection_string()
    print(f"DB host: {masked_host(url)}")
    conn = psycopg.connect(url)  # non-autocommit: single explicit commit on --commit
    cur = conn.cursor()
    city = _load_city(cur, args.city)
    cur.execute("select lower(name) from public.tags")
    known_tags = frozenset(r[0] for r in cur.fetchall())
    has_notes_cols = _columns_present(cur, ("notes", "instagram_url"))

    try:
        rows = read_manual_csv(args.csv)
        find_intra_file_duplicates(rows)
    except ImporterError as exc:
        print(f"\nIMPORT REFUSED: {exc}")
        conn.rollback()
        cur.close()
        conn.close()
        return 1
    print(f"CSV: {args.csv} | rows {len(rows)} | header OK "
          f"(notes/insta columns {'present' if has_notes_cols else 'MISSING (staged)'})")

    print("\nrow verdicts:")
    inserts: list[dict] = []
    skipped = reported = 0
    lines: list[tuple[int, list[str]]] = []
    for row in rows:
        params, errors, dist = validate_manual_row(
            row, city["lat"], city["lng"], known_tags, has_notes_cols)
        if errors:
            lines.append((row["_line"], errors))
            continue
        match, nearby = find_match(params, _existing_rows(cur))
        if match == "manual":
            skipped += 1
            print(f"  [skip]   line {row['_line']} {params['name']!r} — "
                  "already imported (same name, <= 50 m of an existing manual row)")
            continue
        for gid, name_db, dist_m in nearby:
            reported += 1
            print(f"  [near match] line {row['_line']} {params['name']!r} ~ google "
                  f"{name_db!r} ({gid}) at {dist_m:.0f} m — inserting anyway (never merged)")
        inserts.append(params)
        print(f"  [insert] line {row['_line']} {params['name']!r} — "
              f"{dist / 1000:.2f} km from center")

    if lines:
        print("\nVALIDATION ERRORS (no write performed):")
        for line_no, row_errors in lines:
            print(f"  line {line_no}: " + "; ".join(row_errors))
        conn.rollback()
        cur.close()
        conn.close()
        return 1

    print(f"\nverdict: to insert {len(inserts)}, already imported {skipped}, "
          f"google near-matches reported {reported}")

    if not args.commit:
        conn.rollback()
        print("DRY-RUN: nothing written. Pass --commit to insert.")
        cur.close()
        conn.close()
        return 0
    if not inserts:
        print("Nothing new to insert (all rows already imported).")
        cur.close()
        conn.close()
        return 0
    for params in inserts:
        insert_manual_row(cur, params, city["id"], has_notes_cols)
    conn.commit()
    print(f"COMMITTED: {len(inserts)} manual row(s) inserted.")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
