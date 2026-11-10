"""Phase 2, step 5: compute cafe ranking (final_score, PROJECT.md section 7).

Dry-run DEFAULT — nothing is written unless --commit. Prints the top/bottom-20
preview; use --explain "name" for a single-cafe breakdown (read-only).

Formula (owner-approved 2026-11-10):
    WR = (v*R + m*C) / (v + m)         v = google_review_count, R = google_rating
    final_score = w*WR + (1-w)*curated_score   (renormalized when a term missing)

Branches per row:
    blended      - WR and curated_score both present
    google only  - WR present, curated missing    -> final = WR
    curated only - WR absent (no rating), curated present -> final = curated
    null         - neither present -> final_score = NULL (row stays, unranked)
Purged stubs (location IS NULL OR status = 'unknown') are skipped entirely:
final_score NULL + the standing stub-filter keeps them out of serving queries.
Manual rows participate with the google term missing by design (renormalized).

Config: config/scoring.toml (tracked; owner set m/w/C policy). C defaults to
the mean google_rating of the city's rated rows at run time; 'closed_'
temporarily rows are excluded from C per config (owner flag ON, 2026-11-10).
"""
from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import connect, masked_host  # noqa: E402

FIELDS = ("id", "name", "status", "source", "google_rating",
          "google_review_count", "curated_score", "has_location")

FETCH_SQL = """
    select id, name, status, source,
           google_rating::float8, google_review_count,
           curated_score::float8,
           location is not null
    from public.cafes
    where city_id = %s
    order by id
"""


def load_config(path: str) -> dict:
    """Parse + validate config/scoring.toml (stdlib tomllib)."""
    with open(path, "rb") as fh:
        cfg = tomllib.load(fh)
    rank = cfg.get("ranking", {})
    out = cfg.get("output", {})
    m = float(rank["m"])
    w = float(rank["w"])
    if not 0.0 <= w <= 1.0:
        raise ValueError(f"w must be in [0,1], got {w}")
    if m <= 0:
        raise ValueError(f"m must be > 0, got {m}")
    c_mode = rank.get("c_mode", "computed")
    if c_mode not in ("computed", "override"):
        raise ValueError(f"c_mode must be computed|override, got {c_mode!r}")
    c_override = rank.get("c_override")
    if c_mode == "override":
        if c_override is None:
            raise ValueError('c_mode = "override" needs ranking.c_override')
        c_override = float(c_override)
        if not 1.0 <= c_override <= 5.0:
            raise ValueError(f"c_override must be in [1,5], got {c_override}")
    return {
        "m": m,
        "w": w,
        "c_mode": c_mode,
        "c_override": c_override,
        "exclude_closed_from_c":
            bool(rank.get("exclude_closed_temporarily_from_c", False)),
        "top_n": int(out.get("top_n", 20)),
        "bottom_n": int(out.get("bottom_n", 20)),
    }


def wr(row: dict, c: float | None, m: float) -> float | None:
    """Bayesian-weighted rating; None when the row has no Google credit data."""
    rating = row.get("google_rating")
    reviews = row.get("google_review_count") or 0
    if rating is None or reviews < 1 or c is None:
        return None
    return (reviews * rating + m * c) / (reviews + m)


def is_stub(row: dict) -> bool:
    """Standing stub-filter: purged Google rows are out of ranking entirely."""
    return (not row.get("has_location", False)) or row.get("status") == "unknown"


def score_row(row: dict, c: float, m: float, w: float,
              stub: bool | None = None) -> tuple[float | None, str]:
    """Returns (final_score, branch). stub=None -> derive from the row."""
    if stub is None:
        stub = is_stub(row)
    if stub:
        return None, "stub"
    weighted = wr(row, c, m)
    curated = row.get("curated_score")
    if weighted is not None and curated is not None:
        return round(w * weighted + (1.0 - w) * curated, 2), "blended"
    if weighted is not None:
        return round(weighted, 2), "google only"
    if curated is not None:
        return round(curated, 2), "curated only"
    return None, "null"


def compute_c(rows: list[dict], exclude_closed: bool) -> tuple[float | None, int]:
    """Mean google_rating over the city's rated rows (pure, over fetched rows)."""
    pool = [r for r in rows if r.get("google_rating") is not None]
    if exclude_closed:
        pool = [r for r in pool if r.get("status") != "closed_temporarily"]
    if not pool:
        return None, 0
    return round(sum(r["google_rating"] for r in pool) / len(pool), 3), len(pool)


def explain_lines(row: dict, c: float, m: float, w: float) -> list[str]:
    """Per-component breakdown for --explain "<name>"."""
    final, branch = score_row(row, c, m, w)
    weighted = wr(row, c, m)
    v, r = row.get("google_review_count"), row.get("google_rating")
    lines = [
        f"cafe:   {row['name']!r} (id {row['id']}, source {row['source']}, "
        f"status {row['status']})",
        f"google_rating R = {r}",
        f"google_review_count v = {v}",
        f"prior C = {c}   threshold m = {m}   weight w = {w} "
        f"(curated weight {1.0 - w})",
    ]
    if weighted is None:
        lines.append("WR = n/a (no google credit data)")
    else:
        lines.append(f"WR = (v*R + m*C) / (v + m) = ({v}*{r} + {m}*{c}) / "
                     f"({v} + {m}) = {weighted:.4f}")
    lines.append(f"curated_score = {row.get('curated_score')}")
    lines.append(f"branch = {branch}")
    if final is None:
        lines.append("final_score = NULL (not used in ranking)")
    elif branch == "blended":
        lines.append(f"final_score = {w}*WR + {1.0 - w}*curated = {final:.2f}")
    elif branch == "google only":
        lines.append(f"final_score = WR = {final:.2f}")
    else:
        lines.append(f"final_score = curated_score = {final:.2f}")
    return lines


def fmt_rating(v) -> str:
    return "n/a" if v is None else f"{v:.2f}".rstrip("0").rstrip(".")


def _print_ranking(rows: list[dict], results: list[tuple[float | None, str]],
                   cfg: dict) -> None:
    scored = [dict(row, final=f, branch=b)
              for row, (f, b) in zip(rows, results) if f is not None]
    scored.sort(key=lambda r: (-r["final"], -(r.get("google_review_count") or 0),
                               r["name"] or ""))
    print(f"\nTOP {cfg['top_n']} by final_score:")
    for r in scored[:cfg["top_n"]]:
        print(f"  {r['final']:5.2f}  {str(r['name'])[:40]:40} "
              f"[{r['branch']:12}] R={fmt_rating(r.get('google_rating'))} "
              f"v={r.get('google_review_count') or 0}")
    print(f"\nBOTTOM {cfg['bottom_n']} by final_score:")
    for r in list(reversed(scored))[:cfg["bottom_n"]]:
        print(f"  {r['final']:5.2f}  {str(r['name'])[:40]:40} "
              f"[{r['branch']:12}] R={fmt_rating(r.get('google_rating'))} "
              f"v={r.get('google_review_count') or 0}")
    if not scored:
        print("  (no scored rows)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True, help="City name as in the cities table")
    ap.add_argument("--config", default="config/scoring.toml",
                    help="scoring config (tracked toml)")
    ap.add_argument("--commit", action="store_true",
                    help="write final_score for REAL (default: dry-run)")
    ap.add_argument("--explain", metavar="NAME",
                    help="print the per-component breakdown for one cafe "
                         "(forces read-only)")
    args = ap.parse_args()

    if args.explain and args.commit:
        print("--explain is read-only; --commit ignored for this run.")
        args.commit = False

    cfg = load_config(args.config)
    print(f"DB host: {masked_host()}")
    conn = connect()  # the only write path is the single --commit transaction
    cur = conn.cursor()
    cur.execute("select id from public.cities where name = %s", (args.city,))
    row = cur.fetchone()
    if not row:
        raise SystemExit(f"city {args.city!r} not found in the cities table")
    city_id = row[0]

    cur.execute(FETCH_SQL, (city_id,))
    rows = [dict(zip(FIELDS, r)) for r in cur.fetchall()]

    if cfg["c_mode"] == "override":
        c = cfg["c_override"]
        c_source = "override"
    else:
        c, n = compute_c(rows, cfg["exclude_closed_from_c"])
        c_source = f"computed over {n} rated rows" + (
            " (closed_temporarily excluded)"
            if cfg["exclude_closed_from_c"] else "")
    print(f"\nscoring inputs: C = {c} ({c_source}) | m = {cfg['m']} "
          f"| w = {cfg['w']} (google) / {1.0 - cfg['w']:.1f} (curated)")

    results = [score_row(r, c, cfg["m"], cfg["w"]) for r in rows]
    branches: dict[str, int] = {}
    for _, b in results:
        branches[b] = branches.get(b, 0) + 1
    stub_count = branches.get("stub", 0)
    branch_line = ", ".join(
        f"{b} {branches.get(b, 0)}"
        for b in ("blended", "google only", "curated only", "null"))
    print(f"rows per branch: {branch_line} | stub-skipped {stub_count} "
          f"(out of {len(rows)} rows)")

    if args.explain:
        matches = [r for r in rows
                   if args.explain.lower().strip() in (r.get("name") or "").lower()]
        if not matches:
            print(f"no cafe name matching {args.explain!r}")
        elif len(matches) > 1:
            print("ambiguous match - be more specific:")
            for r in matches:
                print(f"  {r['name']!r} (id {r['id']})")
        else:
            print(f"\n--- explain {args.explain!r}")
            for line in explain_lines(matches[0], c, cfg["m"], cfg["w"]):
                print("  " + line)
        conn.rollback()
        print("\nDRY-RUN: nothing written. Pass --commit to write.")
        cur.close()
        conn.close()
        return 0

    _print_ranking(rows, results, cfg)

    if not args.commit:
        conn.rollback()
        print("\nDRY-RUN: nothing written. Pass --commit to write.")
        cur.close()
        conn.close()
        return 0

    updated = 0
    for row, (final, _) in zip(rows, results):
        cur.execute("update public.cafes set final_score = %s where id = %s",
                    (final, row["id"]))
        updated += cur.rowcount
    conn.commit()
    print(f"\nCOMMITTED: final_score written for {updated} row(s) "
          "(NULLs included where neither term exists / purged stubs).")
    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
