"""Postgres writer for the central PG Farm ``runs`` table.

Activated when ``STAN_DB_BACKEND=pg`` is set in the environment. The
SQLite path in ``stan.db`` stays as the default for instrument-PC
watchers and local development — only Hive bulk-search jobs and the
``stan ingest-orphans`` recovery flow route through here.

Background: Hive's SQLite ``stan.db`` on Quobyte suffered repeated index
corruption under high concurrent-writer load (May 11 + May 16, 2026),
silently dropping the bookkeeping half of ~2,700 weekend search jobs.
The fix is to skip Quobyte SQLite entirely on Hive and write straight
to the central Postgres at PG Farm — which has the same schema (laid
out by ``scripts/migrate_sqlite_to_pgfarm.py``) plus two extra columns:

  - ``host_origin`` — instrument family (lumos / exploris / timstof)
  - ``migrated_at`` — server-side default NOW()

Composite PK is (host_origin, id), so PG-direct inserts coexist
cleanly with the existing 678 rows the migration script seeded.

Credentials: ``$PGPASSWORD`` or
``/quobyte/proteomics-grp/brett/.pgfarm_token`` (override path via
``$STAN_PGFARM_TOKEN_FILE``). The 7-day CAS token must be refreshed
weekly until Justin's service account is live.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

PG_DEFAULTS = {
    "host": "pgfarm.library.ucdavis.edu",
    "port": 5432,
    "database": "uc-davis-genome-center-proteomics-core/stan",
    "sslmode": "require",
    "user": "brettsp",
}

# Map ``--family`` (canonical instrument family from instruments.yml /
# hive-dispatch) to the short host_origin label used by the migration
# and the dashboard. New families must be added here AND in the
# dashboard's host filter.
FAMILY_TO_HOST_ORIGIN = {
    "Lumos": "lumos",
    "Exploris": "exploris",
    "timsTOF": "timstof",
}


def host_origin_from_family(family: str) -> str:
    """Map ``--family`` to a host_origin label."""
    return FAMILY_TO_HOST_ORIGIN.get(family, (family or "hive").lower())


def host_origin_from_instrument(instrument: str) -> str:
    """Map an instrument's canonical model name to a host_origin label.

    Used when only the instrument name is in scope (e.g. inside
    ``stan.db.insert_run`` which doesn't receive ``family``). Mirrors
    the ``family`` mapping by substring match — keeps the host_origin
    space aligned with the per-instrument SQLite cron sync.
    """
    s = (instrument or "").lower()
    if "lumos" in s: return "lumos"
    if "exploris" in s: return "exploris"
    if "timstof" in s or "tims-tof" in s: return "timstof"
    if "astral" in s: return "astral"
    return s.split()[0] if s else "hive"


def _resolve_pgpassword() -> str:
    """Find the PG Farm token, $PGPASSWORD first then the token file."""
    pwd = os.environ.get("PGPASSWORD", "").strip()
    if pwd:
        return pwd
    token_file = Path(os.environ.get(
        "STAN_PGFARM_TOKEN_FILE",
        "/quobyte/proteomics-grp/brett/.pgfarm_token",
    ))
    if token_file.exists():
        try:
            return token_file.read_text().strip()
        except OSError as e:
            logger.warning("could not read %s: %s", token_file, e)
    raise RuntimeError(
        "no PG Farm password — set PGPASSWORD or place a token at "
        f"{token_file} (chmod 600)"
    )


_CACHED_CONN = None


def _connect():
    """Return a cached PG Farm connection, opening one if needed.

    The connection is cached at module level so repeated calls in the
    same process (e.g. the orphan re-ingest loop, or a long-running
    SLURM job that writes multiple rows) skip the ~300-500ms SSL
    handshake each time. psycopg2's context-manager exit (``with
    _connect() as pg: ...``) commits/rollbacks the transaction but
    does NOT close the connection, so callers can keep using the
    context-manager pattern.

    Reconnects if the cached connection has been closed by the server
    (idle timeout, network blip).
    """
    global _CACHED_CONN
    import psycopg2

    if _CACHED_CONN is not None:
        try:
            with _CACHED_CONN.cursor() as c:
                c.execute("SELECT 1")
            return _CACHED_CONN
        except (psycopg2.InterfaceError, psycopg2.OperationalError):
            try:
                _CACHED_CONN.close()
            except Exception:
                pass
            _CACHED_CONN = None

    _CACHED_CONN = psycopg2.connect(password=_resolve_pgpassword(), **PG_DEFAULTS)
    return _CACHED_CONN


def insert_run_pg(
    instrument: str,
    run_name: str,
    raw_path: str,
    mode: str,
    metrics: dict,
    *,
    host_origin: str,
    gate_result: str = "",
    failed_gates: list[str] | None = None,
    diagnosis: str = "",
    amount_ng: float = 50.0,
    spd: int | None = None,
    gradient_length_min: int | None = None,
    run_date: str | None = None,
) -> str:
    """Upsert one row into PG ``runs``. Returns the row id (UUID).

    Mirrors ``stan.db.insert_run`` — same kwargs, same row dict
    construction (via ``_build_runs_row``). On (host_origin, id)
    conflict (re-running an idempotent recovery), every non-key
    column is updated with the new value.
    """
    from stan.db import _build_runs_row

    row = _build_runs_row(
        instrument=instrument, run_name=run_name, raw_path=raw_path,
        mode=mode, metrics=metrics, gate_result=gate_result,
        failed_gates=failed_gates, diagnosis=diagnosis,
        amount_ng=amount_ng, spd=spd,
        gradient_length_min=gradient_length_min, run_date=run_date,
    )

    cols = list(row.keys()) + ["host_origin"]
    col_list = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join(["%s"] * len(cols))
    # Conflict resolution uses the natural-key unique index
    # idx_runs_natural (host_origin, instrument, run_name, raw_path).
    # When a re-ingest produces a new UUID for an already-known raw,
    # we update the existing row in place instead of inserting a dup.
    # Don't overwrite host_origin or id; preserve the original
    # migrated_at so we can tell when a row first landed.
    update_cols = [
        c for c in cols
        if c not in ("id", "host_origin", "instrument", "run_name", "raw_path")
    ]
    updates = ", ".join(f'"{c}" = EXCLUDED."{c}"' for c in update_cols)
    sql = (
        f'INSERT INTO runs ({col_list}) VALUES ({placeholders}) '
        f'ON CONFLICT (host_origin, instrument, run_name, raw_path) '
        f'DO UPDATE SET {updates}'
    )
    values = list(row.values()) + [host_origin]

    with _connect() as pg, pg.cursor() as cur:
        cur.execute(sql, values)
        pg.commit()
    logger.info("PG insert %s: %s (%s)", row["id"][:8], run_name, host_origin)
    return row["id"]


def row_exists_pg(
    instrument: str, raw_path: str | Path, *, host_origin: str,
) -> str | None:
    """Return the existing row id for (instrument, raw_path), or None.

    Mirrors ``stan.pipeline.hive_process._row_exists`` for the PG
    backend. The unique key in PG is the composite PK + a
    natural-key tuple (instrument, raw_path) — we treat the raw
    path as the de-facto natural identifier because the PG
    schema doesn't currently enforce uniqueness on it.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            'SELECT id FROM runs '
            'WHERE host_origin = %s AND instrument = %s AND raw_path = %s '
            'LIMIT 1',
            (host_origin, instrument, str(raw_path)),
        )
        r = cur.fetchone()
    return r[0] if r else None


def use_pg() -> bool:
    """Return True when the PG backend should be used for writes."""
    return os.environ.get("STAN_DB_BACKEND", "").lower() == "pg"
