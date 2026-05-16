"""Re-ingest orphan DIA-NN reports from /quobyte/proteomics-grp/STAN/processing/.

Each subdir in processing/ that has report.parquet + the raw file metadata
but no row in the global stan.db `runs` table represents a successful
DIA-NN search whose DB-write step failed (the weekend-of-2026-05-16
corruption episode left ~1,000 of these).

This script:
  1. Walks /processing/ for subdirs with report.parquet
  2. Skips ones whose `run_name` is already in runs
  3. For each orphan: invokes the STAN metric extractor on the parquet
     + the raw file, then inserts the row into stan.db
  4. Counts inserted / failed / skipped at end

USAGE
=====
    # Dry-run (no DB writes, prints what would be ingested)
    python3 scripts/ingest_orphan_parquets.py --dry-run

    # Real ingest (after DB has been repaired)
    python3 scripts/ingest_orphan_parquets.py
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ingest-orphans")

PROCESSING = Path("/quobyte/proteomics-grp/STAN/processing")
DB = Path("/quobyte/proteomics-grp/STAN/stan.db")


def already_in_runs(con: sqlite3.Connection, run_name: str) -> bool:
    cur = con.execute("SELECT 1 FROM runs WHERE run_name = ? LIMIT 1", (run_name,))
    return cur.fetchone() is not None


def ingest_one(con: sqlite3.Connection, parquet: Path, dry_run: bool) -> str:
    """Return 'inserted' / 'skipped' / 'failed' for the given parquet path."""
    run_name = parquet.parent.name + (".raw" if not parquet.parent.name.endswith(".d") else "")
    if already_in_runs(con, run_name):
        return "skipped"

    # Try the canonical STAN extract path. The hive-process pipeline writes
    # processing/<run>/{report.parquet, run.json, metrics.json}. If metrics.json
    # exists it's already-extracted — use that. Otherwise re-extract via the
    # STAN extractor module.
    metrics_json = parquet.parent / "metrics.json"
    if metrics_json.exists():
        import json
        metrics = json.loads(metrics_json.read_text())
    else:
        # Lazy import so the script runs even when STAN isn't on PATH.
        try:
            from stan.metrics.extractor import extract_dia_metrics
        except ImportError:
            logger.error("STAN not installed; cannot re-extract from raw parquet")
            return "failed"
        try:
            metrics = extract_dia_metrics(parquet)
        except Exception as e:
            logger.warning("extract failed for %s: %s", parquet.parent.name, e)
            return "failed"

    if dry_run:
        return "inserted"

    try:
        from stan.db import insert_run
        insert_run(metrics, db_path=DB)
        return "inserted"
    except Exception as e:
        logger.warning("insert failed for %s: %s", parquet.parent.name, e)
        return "failed"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="don't write to DB")
    p.add_argument("--processing-dir", type=Path, default=PROCESSING)
    p.add_argument("--db", type=Path, default=DB)
    args = p.parse_args()

    con = sqlite3.connect(str(args.db))
    counts = {"inserted": 0, "skipped": 0, "failed": 0}
    total = 0
    for parquet in args.processing_dir.glob("*/report.parquet"):
        total += 1
        result = ingest_one(con, parquet, args.dry_run)
        counts[result] += 1
        if total % 100 == 0:
            logger.info("  %d processed  inserted=%d skipped=%d failed=%d",
                        total, counts["inserted"], counts["skipped"], counts["failed"])
    con.commit()
    con.close()

    logger.info("DONE  total=%d  inserted=%d  skipped=%d  failed=%d",
                total, counts["inserted"], counts["skipped"], counts["failed"])


if __name__ == "__main__":
    main()
