"""Phase 2, step 1 (READ-ONLY): ranking input statistics.

Runs SELECT queries only — nothing written, no schema changes. Prints per-city
review_count and rating distributions for google-sourced cafes, plus the
closed_temporarily subset, to inform the ranking rubric (PROJECT.md section 7).

Usage: python scripts/ranking_stats.py --city Imphal
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from db import connect, masked_host  # noqa: E402

REVIEW_STATS_SQL = """
    select count(*)                                                            as rows_all,
           count(*) filter (where google_review_count is not null)             as with_reviews,
           min(google_review_count)                                            as min_v,
           percentile_cont(0.10) within group (order by google_review_count)   as p10,
           percentile_cont(0.25) within group (order by google_review_count)   as p25,
           percentile_cont(0.50) within group (order by google_review_count)   as median,
           percentile_cont(0.75) within group (order by google_review_count)   as p75,
           percentile_cont(0.90) within group (order by google_review_count)   as p90,
           round(avg(google_review_count)::numeric, 1)                          as mean_v,
           max(google_review_count)                                            as max_v,
           count(*) filter (where google_review_count < 5)                     as v_lt_5,
           count(*) filter (where google_review_count < 10)                    as v_lt_10,
           count(*) filter (where google_review_count < 25)                    as v_lt_25,
           count(*) filter (where google_review_count > 100)                   as v_gt_100
    from public.cafes
    where city_id = %s and source = 'google'{status}
"""

RATING_STATS_SQL = """
    select count(*)                                                            as rows_all,
           count(*) filter (where google_rating is not null)                   as with_rating,
           min(google_rating)                                                  as min_r,
           percentile_cont(0.25) within group (order by google_rating)         as p25,
           percentile_cont(0.50) within group (order by google_rating)         as median,
           percentile_cont(0.75) within group (order by google_rating)         as p75,
           round(avg(google_rating)::numeric, 3)                                as mean_r,
           max(google_rating)                                                  as max_r,
           count(*) filter (where google_rating >= 4.5)                        as r_ge_45,
           count(*) filter (where google_rating >= 4.0 and google_rating < 4.5) as r_40_to_44,
           count(*) filter (where google_rating < 4.0)                         as r_lt_40,
           round(stddev_samp(google_rating)::numeric, 3)                        as stddev_r
    from public.cafes
    where city_id = %s and source = 'google'{status}
"""


def fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return str(v)


def show(cur, sql: str, params: tuple, title: str, headers: tuple[str, ...]) -> None:
    print(f"\n--- {title}")
    cur.execute(sql, params)
    row = cur.fetchone()
    for head, val in zip(headers, row):
        print(f"  {head:12} {fmt(val)}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True, help="City name as in the cities table")
    args = ap.parse_args()

    print(f"DB host: {masked_host()}")  # read-only session
    conn = connect()
    cur = conn.cursor()
    cur.execute("select id from public.cities where name = %s", (args.city,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"city {args.city!r} not found in the cities table")
    city_id = row[0]

    review_headers = ("rows", "with_reviews", "min", "p10", "p25", "median",
                      "p75", "p90", "mean", "max", "<5", "<10", "<25", ">100")
    rating_headers = ("rows", "with_rating", "min", "p25", "median", "p75",
                      "mean", "max", ">=4.5", "4.0-4.4", "<4.0", "stddev")

    show(cur, REVIEW_STATS_SQL.format(status=""), (city_id,),
         f"A. review_count — all google rows ({args.city})", review_headers)
    show(cur, REVIEW_STATS_SQL.format(status=" and status = 'closed_temporarily'"),
         (city_id,),
         "C. review_count — status = 'closed_temporarily' only", review_headers)
    show(cur, RATING_STATS_SQL.format(status=""), (city_id,),
         f"B. google_rating — all google rows ({args.city})", rating_headers)
    show(cur, RATING_STATS_SQL.format(status=" and status = 'closed_temporarily'"),
         (city_id,),
         "D. google_rating — status = 'closed_temporarily' only", rating_headers)

    cur.execute(
        """
        select count(*) total,
               count(*) filter (where google_rating is not null) rated
        from public.cafes
        where city_id = %s and source = 'google'
        """,
        (city_id,),
    )
    total, rated = cur.fetchone()
    print(f"\nE. no-rating rows: {total - rated} of {total} google rows "
          f"({rated} rated)")

    cur.execute("select current_user")
    grants = cur.fetchone()[0]
    print(f"\n(read-only step: session user {grants}; no writes issued)")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
