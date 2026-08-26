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
import sqlite3
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

    from stan.db import get_db_path, init_db
    from stan.db_pg import _connect

    db_path = args.db_path or get_db_path()
    init_db(db_path)

    local = sqlite3.connect(str(db_path))
    sq_cols = [r[1] for r in local.execute("PRAGMA table_info(runs)").fetchall()]

    pg = _connect()
    cur = pg.cursor()
    cur.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name='runs'"
    )
    pg_cols = {r[0] for r in cur.fetchall()}

    cols = [c for c in sq_cols if c in pg_cols]
    missing = [c for c in sq_cols if c not in pg_cols]
    if missing:
        logger.warning("columns absent in PG, left NULL locally: %s", missing)

    q = f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM runs'
    params: tuple = ()
    if args.since:
        q += " WHERE run_date >= %s"
        params = (args.since,)
    cur.execute(q, params)
    rows = cur.fetchall()
    logger.info("read %d rows from PG (%d columns)", len(rows), len(cols))

    if args.dry_run:
        logger.info("dry-run — nothing written to %s", db_path)
        return 0

    placeholders = ",".join("?" * len(cols))
    sql = (f'INSERT OR REPLACE INTO runs ({", ".join(cols)}) '
           f"VALUES ({placeholders})")
    with local:
        local.executemany(sql, [tuple(r) for r in rows])
    total = local.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    newest = local.execute("SELECT MAX(run_date) FROM runs").fetchone()[0]
    logger.info("local %s now holds %d runs (newest %s)", db_path, total, newest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
