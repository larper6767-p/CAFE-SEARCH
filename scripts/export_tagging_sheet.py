"""Phase 2, step 3a: export the tagging WORKING sheet (owner fills it in).

The sheet goes to data/processed/ (gitignored, on the 2026-12-06 purge list) —
it is a workspace, not durable data. On --commit the tagging importer records
the durable, stripped copy data/decisions/imphal_tags.csv (id, tag, tag_source,
tracked in git).

Sheet format (UTF-8 BOM, all fields quoted):
    id, name, review_count, tag_source, <one column per tag, tags-table order>
Cell semantics: 'y' = has the tag; BLANK = unknown — the importer NEVER treats
blank as "no" (only positive links are stored, v1 join-table model).
tag_source: one value per cafe row — own_visit / own_knowledge / review_derived
/ user; links MIXED across sources export ';'-joined and the importer REJECTS
that row (unify or split it before importing).

Existing links are PREFILLED so edits round-trip. Refuses to overwrite an
existing sheet unless --overwrite (protects owner edits on re-export).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import connect, masked_host  # noqa: E402

COLUMNS_WITHOUT_TAGS = ["id", "name", "review_count", "tag_source"]


def tag_source_column_present(cur) -> bool:
    cur.execute(
        """
        select count(*) from information_schema.columns
        where table_schema = 'public' and table_name = 'cafe_tags'
          and column_name = 'tag_source'
        """
    )
    return cur.fetchone()[0] == 1


def fetch_links(cur, city_id: int, with_source: bool) -> dict[int, dict[str, set]]:
    sql = (
        "select ct.cafe_id, t.name"
        + (", ct.tag_source" if with_source else "")
        + """
          from public.cafe_tags ct
          join public.tags t on t.id = ct.tag_id
          join public.cafes c on c.id = ct.cafe_id
          where c.city_id = %s
          order by ct.cafe_id, t.name
          """
    )
    cur.execute(sql, (city_id,))
    links: dict[int, dict[str, set]] = {}
    for row in cur.fetchall():
        cafe_id, tag = row[0], row[1]
        source = row[2] if with_source else None
        bucket = links.setdefault(cafe_id, {"tags": set(), "sources": set()})
        bucket["tags"].add(tag)
        bucket["sources"].add(source if source is None else str(source))
    return links


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True, help="City name as in the cities table")
    ap.add_argument("--csv", default="data/processed/imphal_tagging.csv",
                    help="output path (default: data/processed/imphal_tagging.csv)")
    ap.add_argument("--overwrite", action="store_true",
                    help="replace an existing sheet (default: refuse, the "
                         "working sheet holds owner edits between exports)")
    args = ap.parse_args()

    out_path = Path(args.csv)
    if out_path.exists() and not args.overwrite:
        raise SystemExit(
            f"refused: {out_path} exists — the working sheet is owner-edited. "
            "Pass --overwrite to replace it deliberately.")

    print(f"DB host: {masked_host()}")
    conn = connect()  # read-only session
    cur = conn.cursor()
    cur.execute("select id from public.cities where name = %s", (args.city,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"city {args.city!r} not found in the cities table")
    city_id = row[0]

    cur.execute("select id, name from public.tags order by id")
    tags = [name for _tid, name in cur.fetchall()]

    cur.execute(
        """
        select id, name, google_review_count
        from public.cafes
        where city_id = %s
        order by google_review_count desc nulls last, name asc nulls last, id
        """,
        (city_id,),
    )
    cafes = cur.fetchall()

    with_source = tag_source_column_present(cur)
    links = fetch_links(cur, city_id, with_source)

    header = COLUMNS_WITHOUT_TAGS + tags
    rows = []
    prefilled = 0
    mixed_rows = 0
    for cafe_id, name, reviews in cafes:
        rec = {c: "" for c in header}
        rec["id"] = cafe_id
        rec["name"] = name if name is not None else ""
        rec["review_count"] = "" if reviews is None else reviews
        bucket = links.get(cafe_id)
        if bucket:
            for tag in bucket["tags"]:
                rec[tag] = "y"
                prefilled += 1
            sources = bucket["sources"]
            if with_source:
                if len(sources) == 1 and None not in sources:
                    rec["tag_source"] = next(iter(sources))
                elif len(sources) > 1:
                    rec["tag_source"] = ";".join(
                        sorted(str(s) for s in sources if s is not None))
                    mixed_rows += 1
        rows.append(rec)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, quoting=csv.QUOTE_ALL)
        writer.writeheader()
        writer.writerows(rows)

    print(f"sheet written: {out_path}")
    print(f"rows: {len(cafes)} cafes x {len(tags)} tag columns")
    if with_source:
        print(f"existing links prefilled: {prefilled} (tag_source column live)")
    else:
        print(f"existing links prefilled: {prefilled} "
              "(cafe_tags.tag_source NOT on this database — cells prefilled "
              "'y' only, tag_source left blank)")
    if mixed_rows:
        print(f"mixed-source rows exported ';'-joined: {mixed_rows} — the "
              "importer REJECTS these until unified")
    if not with_source:
        print("NOTE: apply staged migration 20261110000000 (on approval) "
              "before running the tagging import.")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
