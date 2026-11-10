"""Phase 2, step 3b: import the owner's tagging sheet into cafe_tags.

Dry-run DEFAULT; --commit gated on the owner. One transaction, idempotent
(running the same sheet twice = no-op).

Cell semantics (owner, 2026-11-10):
  'y'             -> the cafe carries the tag (link created/kept)
  blank           -> unknown — NEVER treated as "no"; never inferred
  anything else   -> hard error, no write at all
tag_source: exactly one token per row (own_visit / own_knowledge /
review_derived / user); it is applied to links this import CREATES. Existing
links keep their stored source (never clobbered by a re-import).

Deletion rule (owner-approved): a link REMOVED from the sheet is deleted ONLY
when its stored tag_source is 'own_visit' or 'own_knowledge'. Links with
tag_source 'review_derived' or 'user' (or NULL) are NEVER deleted — they are
reported as kept. The dry-run lists every deletion.

Durable record: on --commit this importer regenerates data/decisions/
imphal_tags.csv (id, tag, tag_source — tracked in git, one row per link)
directly from the resulting database state. Dry-run does not write it.

Requires migration 20261110000000 (cafe_tags.tag_source) to be applied; the
importer refuses to run while the column is missing.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_connection_string, masked_host  # noqa: E402

TAG_SOURCES = ("own_visit", "own_knowledge", "review_derived", "user")
DELETABLE_SOURCES = ("own_visit", "own_knowledge")
DECISIONS_PATH = Path(__file__).resolve().parent.parent / "data" / "decisions" / "imphal_tags.csv"


class TagImportError(Exception):
    """Hard error: nothing is written."""


def _bom_check(path: str) -> None:
    with open(path, "rb") as fh:
        if fh.read(3) != b"\xef\xbb\xbf":
            raise TagImportError(
                f"{Path(path).name} is missing the UTF-8 BOM — re-save as "
                "'CSV UTF-8' (Excel default)")


def read_sheet(path: str, expected_header: list[str]) -> list[dict]:
    _bom_check(path)
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = reader.fieldnames or []
        if header != expected_header:
            raise TagImportError(
                f"header mismatch.\nexpected: {expected_header}\nfound:    "
                f"{header}")
        rows = list(reader)
    if not rows:
        raise TagImportError("no data rows")
    for i, row in enumerate(rows, start=2):
        row["_line"] = i
    return rows


def parse_tags(row: dict, tags: list[str]) -> set[str]:
    """Validate tag cells for one row; returns the set of 'y' tag names."""
    desired: set[str] = set()
    for tag in tags:
        cell = (row.get(tag) or "").strip()
        if not cell:
            continue
        if cell.lower() != "y":
            raise TagImportError(
                f"line {row['_line']} {row.get('name')!r}: tag column "
                f"{tag!r} has {cell!r} — only 'y' or blank is valid "
                "(blank = unknown, never 'no')")
        desired.add(tag)
    return desired


def parse_tag_source(row: dict) -> str:
    token = (row.get("tag_source") or "").strip().lower()
    if ";" in token or "," in token:
        raise TagImportError(
            f"line {row['_line']} {row.get('name')!r}: mixed tag_source "
            f"{token!r} — unify or split the row first")
    if token not in TAG_SOURCES:
        raise TagImportError(
            f"line {row['_line']} {row.get('name')!r}: tag_source "
            f"{token!r} not one of {TAG_SOURCES}")
    return token


def plan_changes(rows: list[dict], city_cafes: dict[int, str],
                 existing: dict[int, dict[str, str | None]],
                 tags: list[str]) -> tuple[list, list, list, list]:
    """Pure planner. Returns (adds, dels, kept, undeletable_kept).

    adds  = (cafe_id, tag, cafe_name, tag_source, line)
    dels  = (cafe_id, tag, cafe_name, stored_source, line)
    kept  = (cafe_id, tag, cafe_name)
    undeletable_kept = (cafe_id, tag, cafe_name, stored_source, line)
    """
    adds: list = []
    dels: list = []
    kept: list = []
    undeletable: list = []
    seen_ids: set[int] = set()
    for row in rows:
        line = row["_line"]
        raw_id = str(row.get("id") or "").strip()
        if not raw_id.isdigit():
            raise TagImportError(
                f"line {line}: id {raw_id!r} is not an integer")
        cafe_id = int(raw_id)
        if cafe_id not in city_cafes:
            raise TagImportError(
                f"line {line}: id {cafe_id} is not a cafe of this city")
        if cafe_id in seen_ids:
            raise TagImportError(
                f"line {line}: duplicate cafe id {cafe_id} in the file")
        seen_ids.add(cafe_id)
        name = city_cafes[cafe_id]
        desired = parse_tags(row, tags)
        source = parse_tag_source(row)
        links = existing.get(cafe_id, {})
        for tag in sorted(desired - set(links)):
            adds.append((cafe_id, tag, name, source, line))
        for tag, stored in links.items():
            if tag not in desired:
                if stored in DELETABLE_SOURCES:
                    dels.append((cafe_id, tag, name, stored, line))
                else:
                    undeletable.append((cafe_id, tag, name, stored, line))
            else:
                kept.append((cafe_id, tag, name))
    return adds, dels, kept, undeletable


def regen_decisions_file(cur, city_id: int) -> int:
    """Rewrite data/decisions/imphal_tags.csv from the current DB truth."""
    cur.execute(
        """
        select c.id, t.name, ct.tag_source
        from public.cafe_tags ct
        join public.tags t on t.id = ct.tag_id
        join public.cafes c on c.id = ct.cafe_id
        where c.city_id = %s
        order by c.id, t.name
        """,
        (city_id,),
    )
    lines = cur.fetchall()
    with open(DECISIONS_PATH, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_ALL)
        writer.writerow(["id", "tag", "tag_source"])
        for cafe_id, tag, source in lines:
            writer.writerow([cafe_id, tag, source if source is not None else ""])
    return len(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True, help="City name as in the cities table")
    ap.add_argument("--csv", required=True,
                    help="tagging sheet path (data/processed/...)")
    ap.add_argument("--commit", action="store_true",
                    help="apply for real (default: dry-run)")
    args = ap.parse_args()

    print(f"DB host: {masked_host()}")
    conn = psycopg.connect(get_connection_string())  # explicit commit only
    cur = conn.cursor()
    cur.execute("select id from public.cities where name = %s", (args.city,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"city {args.city!r} not found in the cities table")
    city_id = row[0]

    cur.execute(
        """
        select count(*) from information_schema.columns
        where table_schema = 'public' and table_name = 'cafe_tags'
          and column_name = 'tag_source'
        """
    )
    if cur.fetchone()[0] != 1:
        raise SystemExit("refused: cafe_tags.tag_source is missing — apply "
                         "staged migration 20261110000000 first (on "
                         "owner approval)")

    cur.execute("select id, name from public.tags order by id")
    tags = [name for _tid, name in cur.fetchall()]
    expected_header = ["id", "name", "review_count", "tag_source"] + tags

    cur.execute("select id, coalesce(name, '') from public.cafes where city_id = %s",
                (city_id,))
    city_cafes = dict(cur.fetchall())
    cur.execute(
        """
        select ct.cafe_id, t.name, ct.tag_source
        from public.cafe_tags ct
        join public.tags t on t.id = ct.tag_id
        join public.cafes c on c.id = ct.cafe_id
        where c.city_id = %s
        """,
        (city_id,),
    )
    existing: dict[int, dict[str, str | None]] = {}
    for cafe_id, tag, source in cur.fetchall():
        existing.setdefault(cafe_id, {})[tag] = source

    try:
        rows = read_sheet(args.csv, expected_header)
        adds, dels, kept, undeletable = plan_changes(
            rows, city_cafes, existing, tags)
    except TagImportError as exc:
        conn.rollback()
        cur.close()
        conn.close()
        raise SystemExit(f"IMPORT REFUSED: {exc}")

    print(f"\nsheet: {args.csv} | rows {len(rows)}")
    if adds:
        print("\nto add:")
        for cafe_id, tag, name, source, line in adds:
            print(f"  line {line}  {name!r} (id {cafe_id}) + {tag} "
                  f"(tag_source={source})")
    if kept:
        print(f"\nkept unchanged: {len(kept)} link(s)")
    if dels:
        print("\nDELETIONS (stored tag_source own_visit/own_knowledge):")
        for cafe_id, tag, name, stored, line in dels:
            print(f"  line {line}  {name!r} (id {cafe_id}) - {tag} "
                  f"(was {stored})")
    if undeletable:
        print("\nkept because NOT deletable (review_derived/user/NULL):")
        for cafe_id, tag, name, stored, line in undeletable:
            print(f"  line {line}  {name!r} (id {cafe_id}) stays {tag} "
                  f"({stored or 'NULL'})")
    print(f"\nverdict: add {len(adds)}, delete {len(dels)}, "
          f"kept {len(kept)} + {len(undeletable)} protected")

    if not args.commit:
        conn.rollback()
        print("DRY-RUN: nothing written. On --commit, data/decisions/"
              "imphal_tags.csv is regenerated from the resulting state.")
        cur.close()
        conn.close()
        return 0

    cur.execute("select name, id from public.tags")
    tag_ids = dict(cur.fetchall())
    added = 0
    for cafe_id, tag, _name, source, _line in adds:
        cur.execute(
            """
            insert into public.cafe_tags (cafe_id, tag_id, tag_source)
            values (%s, %s, %s)
            on conflict (cafe_id, tag_id) do nothing
            """,
            (cafe_id, tag_ids[tag], source),
        )
        added += cur.rowcount
    deleted = 0
    for cafe_id, tag, _name, _stored, _line in dels:
        cur.execute(
            """
            delete from public.cafe_tags
            where cafe_id = %s and tag_id = %s
              and tag_source in ('own_visit', 'own_knowledge')
            """,
            (cafe_id, tag_ids[tag]),
        )
        deleted += cur.rowcount
    conn.commit()
    decisions_rows = regen_decisions_file(cur, city_id)
    cur.close()
    conn.close()
    print(f"\nCOMMITTED: {added} link(s) added, {deleted} link(s) deleted. "
          f"durable record regenerated: data/decisions/imphal_tags.csv "
          f"({decisions_rows} links).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
