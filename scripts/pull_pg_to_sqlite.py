"""Materialise PG Farm ``runs`` into a local SQLite so the dashboard can read it.

The fleet's canonical store is PG Farm, but ``stan dashboard`` reads SQLite
only. After the move to PG the local ``~/.stan/stan.db`` went empty, so the
dashboard served an empty Runs/Trends view even though PG held thousands of
rows. This script closes that gap: it copies every column the local ``runs``
schema actually has (all 62 are present in PG) and upserts by ``id``.

Only ``runs`` is copied — it is the only table in PG. Per-run detail tables
(tic_traces, drift_*, peg_ion_hits) live wherever the SLURM job wrote them and
are not part of the central store, so those dashboard tabs stay empty here.

Usage::

    export PGPASSWORD="$(cat /Volumes/proteomics-grp/brett/.pgfarm_token)"
    python scripts/pull_pg_to_sqlite.py                 # into ~/.stan/stan.db
    python scripts/pull_pg_to_sqlite.py --since 2026-01-01
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("pull_pg")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db-path", type=Path, default=None,
                    help="Target SQLite file (default: STAN's configured db).")
    ap.add_argument("--since", default="",
                    help="Only copy runs with run_date >= this ISO date.")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    os.environ.setdefault("STAN_DB_BACKEND", "pg")

    from stan.db import get_db_path
    from stan.sync.pg_to_sqlite import pull_from_pg

    db_path = args.db_path or get_db_path()
    if args.dry_run:
        logger.info("dry-run — would pull PG runs + TIC into %s", db_path)
        return 0

    written = pull_from_pg(db_path=db_path, since=args.since)
    for table, n in written.items():
        logger.info("%-14s %6d rows -> %s", table, n, db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
