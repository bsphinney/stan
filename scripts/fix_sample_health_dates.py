"""Repair sample_health.run_date rows that recorded PROCESSING time.

``hive_steps`` fell back to ``datetime.now()`` when rawmeat carried no
``metadata.acquisition_date`` (fixed in v1.0.15), so every monitor row written
by a backfill got stamped with the moment the backfill ran. On the dashboard's
week-at-a-glance grid that piles every Sample/Blank run onto one column.

Recovers the real date from the filename, which the lab stamps reliably:
  1. an embedded ``_YYYYMMDDHHMMSS`` run stamp, when present  (most precise)
  2. the ``XX DDMMYY_`` prefix convention (``FL060526_`` -> 2026-05-06)
Rows we cannot resolve are left alone rather than guessed at.

    python scripts/fix_sample_health_dates.py --dry-run
    python scripts/fix_sample_health_dates.py --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys

logger = logging.getLogger("fix_sh_dates")

# A now() stamp has sub-second precision; instrument timestamps don't.
NOW_STAMP = re.compile(r"\.\d{3,}")
EMBEDDED = re.compile(r"_(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(?:\D|$)")
PREFIX_DDMMYY = re.compile(r"^[A-Za-z]{1,3}[-_]?(\d{2})(\d{2})(\d{2})[_-]")


def date_from_name(name: str) -> str | None:
    """Best available acquisition timestamp from a run filename, or None."""
    m = EMBEDDED.search(name)
    if m:
        y, mo, d, hh, mm, ss = m.groups()
        if 2000 <= int(y) <= 2100 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}T{hh}:{mm}:{ss}"
    m = PREFIX_DDMMYY.match(name)
    if m:
        dd, mo, yy = (int(x) for x in m.groups())
        if 1 <= dd <= 31 and 1 <= mo <= 12:
            return f"20{yy:02d}-{mo:02d}-{dd:02d}T12:00:00"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ.setdefault("STAN_DB_BACKEND", "pg")

    from stan.db_pg import _connect

    pg = _connect()
    cur = pg.cursor()
    cur.execute("SELECT id, run_name, run_date FROM sample_health")
    rows = cur.fetchall()

    fixes, unresolved = [], []
    for rid, name, run_date in rows:
        if not NOW_STAMP.search(str(run_date or "")):
            continue                      # already a real timestamp
        new = date_from_name(name or "")
        (fixes if new else unresolved).append((rid, name, run_date, new))

    logger.info("rows=%d  mis-stamped=%d  recoverable=%d  unresolved=%d",
                len(rows), len(fixes) + len(unresolved), len(fixes), len(unresolved))
    for _, name, old, new in fixes[:8]:
        logger.info("  %-46s %s -> %s", name[:46], str(old)[:19], new)
    for _, name, _, _ in unresolved[:5]:
        logger.info("  UNRESOLVED (left alone): %s", name[:60])

    if not args.apply:
        logger.info("dry-run — nothing written. Re-run with --apply.")
        return 0

    for rid, _, _, new in fixes:
        cur.execute("UPDATE sample_health SET run_date = %s WHERE id = %s", (new, rid))
    pg.commit()
    logger.info("updated %d rows", len(fixes))
    return 0


if __name__ == "__main__":
    sys.exit(main())
