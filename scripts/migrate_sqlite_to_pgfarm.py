"""Migrate per-instrument SQLite stan.db files to the central PG Farm Postgres.

PG Farm tenant: ``uc-davis-genome-center-proteomics-core/stan``
Schema lives in the ``stan`` namespace and mirrors the SQLite ``runs`` table
with two extra columns:

  host_origin       — which instrument PC the row came from (lumos, exploris,
                       timstof). Required so the central DB can show fleet
                       state without per-host DBs.
  migrated_at       — timestamp the row first landed in Postgres. Lets us
                       audit migration runs and re-import without losing the
                       original ingestion order.

The composite primary key is (host_origin, id) so SQLite UUIDs cannot collide
between instruments. Submissions stay attributed to the right host.

USAGE
=====

Read-only smoke test (no writes):

    python3 scripts/migrate_sqlite_to_pgfarm.py \\
        --sqlite /Volumes/proteomics-grp/STAN/lumosRox/stan.db \\
        --host-origin lumos \\
        --dry-run

Real migration (requires PGPASSWORD env with a write-enabled token):

    PGPASSWORD=$(pgfarm auth token) python3 scripts/migrate_sqlite_to_pgfarm.py \\
        --sqlite /Volumes/proteomics-grp/STAN/lumosRox/stan.db \\
        --host-origin lumos

The script is idempotent: re-running upserts each row by (host_origin, id).
Existing rows are UPDATEd; rows newer in PG than in SQLite are NOT
overwritten (so a backfill won't clobber live writes).
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("migrate")

PG_DEFAULTS = {
    "host": "pgfarm.library.ucdavis.edu",
    "port": 5432,
    "database": "uc-davis-genome-center-proteomics-core/stan",
    "sslmode": "require",   # 'verify-full' once we resolve the cert path
}

# Column-name → Postgres-type. Everything else in SQLite stays as the
# matching pg_type. Order matters here only for readability.
PG_COLUMN_TYPES = {
    "id":                            "TEXT",
    "instrument":                    "TEXT NOT NULL",
    "run_name":                      "TEXT NOT NULL",
    "run_date":                      "TIMESTAMPTZ",
    "raw_path":                      "TEXT",
    "mode":                          "TEXT",
    "n_precursors":                  "INTEGER",
    "n_peptides":                    "INTEGER",
    "n_proteins":                    "INTEGER",
    "median_cv_precursor":           "REAL",
    "median_fragments_per_precursor":"REAL",
    "pct_fragments_quantified":      "REAL",
    "n_psms":                        "INTEGER",
    "n_peptides_dda":                "INTEGER",
    "median_hyperscore":             "REAL",
    "ms2_scan_rate":                 "REAL",
    "median_delta_mass_ppm":         "REAL",
    "missed_cleavage_rate":          "REAL",
    "pct_charge_1":                  "REAL",
    "pct_charge_2":                  "REAL",
    "pct_charge_3":                  "REAL",
    "median_peak_width_sec":         "REAL",
    "median_points_across_peak":     "REAL",
    "ips_score":                     "INTEGER",
    "tic_auc":                       "DOUBLE PRECISION",
    "peak_rt_min":                   "REAL",
    "irt_max_deviation_min":         "REAL",
    "ms2_fill_time_median_ms":       "REAL",
    "gate_result":                   "TEXT",
    "failed_gates":                  "TEXT",
    "diagnosis":                     "TEXT",
    "amount_ng":                     "REAL DEFAULT 50.0",
    "spd":                           "INTEGER",
    "gradient_length_min":           "INTEGER",
    "column_vendor":                 "TEXT",
    "column_model":                  "TEXT",
    "submitted_to_benchmark":        "INTEGER DEFAULT 0",
    "submission_id":                 "TEXT",
    "ms1_signal":                    "DOUBLE PRECISION",
    "ms2_signal":                    "DOUBLE PRECISION",
    "fwhm_rt_min":                   "REAL",
    "fwhm_scans":                    "REAL",
    "median_mass_acc_ms1_ppm":       "REAL",
    "median_mass_acc_ms2_ppm":       "REAL",
    "peak_capacity":                 "REAL",
    "dynamic_range_log10":           "REAL",
    "lc_system":                     "TEXT DEFAULT ''",
    "diann_version":                 "TEXT",
    "search_engine":                 "TEXT",
    "hidden":                        "INTEGER DEFAULT 0",
    "hidden_reason":                 "TEXT",
    "hidden_at":                     "TIMESTAMPTZ",
    "peg_score":                     "REAL",
    "peg_n_ions_detected":           "INTEGER",
    "peg_intensity_pct":             "REAL",
    "peg_class":                     "TEXT",
    "drift_coverage":                "REAL",
    "drift_median_im":               "REAL",
    "drift_p90_abs_im":              "REAL",
    "drift_class":                   "TEXT",
    "stan_version":                  "TEXT",
    "ms2_analyzer":                  "TEXT",
}

EXTRA_COLUMNS = {
    "host_origin":  "TEXT NOT NULL",
    "migrated_at":  "TIMESTAMPTZ DEFAULT NOW()",
}


def build_create_table() -> str:
    """Return the CREATE TABLE DDL for the central runs table."""
    cols = []
    for c, t in PG_COLUMN_TYPES.items():
        cols.append(f"    {c:<33} {t}")
    for c, t in EXTRA_COLUMNS.items():
        cols.append(f"    {c:<33} {t}")
    cols_sql = ",\n".join(cols)
    return (
        "CREATE TABLE IF NOT EXISTS runs (\n"
        f"{cols_sql},\n"
        "    PRIMARY KEY (host_origin, id)\n"
        ");\n"
        "CREATE INDEX IF NOT EXISTS idx_runs_instrument ON runs(instrument);\n"
        "CREATE INDEX IF NOT EXISTS idx_runs_run_date   ON runs(run_date);\n"
        "CREATE INDEX IF NOT EXISTS idx_runs_host       ON runs(host_origin);\n"
    )


def sqlite_columns(con: sqlite3.Connection) -> list[str]:
    cur = con.execute("PRAGMA table_info(runs)")
    return [row[1] for row in cur.fetchall()]


def upsert_sql(pg_cols: list[str]) -> str:
    """Build an upsert that keeps the newer of (sqlite, pg) for migrated_at-stamped rows."""
    placeholders = ", ".join(["%s"] * len(pg_cols))
    quoted = ", ".join(f'"{c}"' for c in pg_cols)
    updates = ", ".join(
        f'"{c}" = EXCLUDED."{c}"'
        for c in pg_cols
        if c not in ("host_origin", "id", "migrated_at")
    )
    return (
        f"INSERT INTO runs ({quoted}) VALUES ({placeholders}) "
        f"ON CONFLICT (host_origin, id) DO UPDATE SET {updates} "
        f"WHERE runs.run_date IS NULL OR EXCLUDED.run_date >= runs.run_date"
    )


def migrate(sqlite_path: Path, host_origin: str, *, dry_run: bool) -> None:
    sqlite_con = sqlite3.connect(str(sqlite_path))
    sqlite_con.row_factory = sqlite3.Row

    src_cols = set(sqlite_columns(sqlite_con))
    if not src_cols:
        logger.error("sqlite has no runs table — bail")
        return
    common = [c for c in PG_COLUMN_TYPES if c in src_cols]
    pg_cols = common + ["host_origin"]  # migrated_at uses DB default

    rows = list(sqlite_con.execute(f"SELECT {', '.join(common)} FROM runs"))
    logger.info("sqlite: %d rows / %d columns mapped", len(rows), len(common))

    if dry_run:
        logger.info("DDL preview:\n%s", build_create_table())
        logger.info("UPSERT preview:\n%s", upsert_sql(pg_cols))
        if rows:
            sample = dict(zip(pg_cols, list(rows[0]) + [host_origin]))
            logger.info("sample row: %r", {k: sample[k] for k in list(sample)[:8]})
        logger.info("DRY RUN — no Postgres writes.")
        return

    pwd = os.environ.get("PGPASSWORD")
    if not pwd:
        logger.error("PGPASSWORD not set — run `export PGPASSWORD=$(pgfarm auth token)` first")
        sys.exit(2)
    with psycopg2.connect(user="brettsp", password=pwd, **PG_DEFAULTS) as pg, pg.cursor() as cur:
        logger.info("connected to %s", PG_DEFAULTS["database"])
        cur.execute(build_create_table())
        pg.commit()

        psycopg2.extras.execute_batch(
            cur,
            upsert_sql(pg_cols),
            [tuple(list(r) + [host_origin]) for r in rows],
            page_size=200,
        )
        pg.commit()
        cur.execute("SELECT host_origin, COUNT(*) FROM runs GROUP BY host_origin ORDER BY 1")
        for h, n in cur.fetchall():
            logger.info("  %s: %d rows in Postgres after migration", h, n)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sqlite", required=True, type=Path, help="path to stan.db (per-instrument mirror)")
    p.add_argument("--host-origin", required=True,
                   choices=["lumos", "exploris", "timstof"],
                   help="instrument PC label to stamp on every row")
    p.add_argument("--dry-run", action="store_true",
                   help="print DDL + sample row, do NOT write to PG Farm")
    args = p.parse_args()
    migrate(args.sqlite, args.host_origin, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
