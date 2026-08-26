"""Materialise central PG Farm data into the local SQLite the dashboard reads.

`stan dashboard` is a SQLite reader, but the fleet's canonical store is PG
Farm. Without this the local DB holds whatever it was last seeded with, so
the UI serves stale or empty data even though PG is current.

Two kinds of data move here:

* ``runs`` -- a straight column copy (every local column exists in PG).
* ``tic_traces`` -- PG keeps the TIC inline on the run row as the JSONB
  columns ``tic_rt_bins`` / ``tic_intensity``, whereas SQLite keeps it in a
  side table. Without this projection the dashboard's TIC modal reports "No
  TIC data for this run" for runs whose TIC is sitting right there in PG.

* the PEG/drift detail tables (``peg_ion_hits``,
  ``drift_window_centroids``, ``drift_peak_clouds``) -- straight copies,
  skipped silently if the PG side hasn't been migrated yet.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def pull_from_pg(db_path: Path | None = None, since: str = "") -> dict:
    """Copy PG ``runs`` (and its inline TIC) into the local SQLite.

    Returns a dict of table -> row count written. Raises on connection or
    query failure so callers can decide whether that is fatal.
    """
    from stan.db import connect, get_db_path, init_db
    from stan.db_pg import _connect

    if db_path is None:
        db_path = get_db_path()
    init_db(db_path)

    pg = _connect()
    cur = pg.cursor()
    local = connect(db_path)
    written: dict[str, int] = {}

    try:
        sq_cols = [r[1] for r in local.execute("PRAGMA table_info(runs)").fetchall()]
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='runs'"
        )
        pg_cols = {r[0] for r in cur.fetchall()}
        cols = [c for c in sq_cols if c in pg_cols]

        quoted = ", ".join('"' + c + '"' for c in cols)
        sql = f"SELECT {quoted} FROM runs"
        params: tuple = ()
        if since:
            sql += " WHERE run_date >= %s"
            params = (since,)
        cur.execute(sql, params)
        rows = cur.fetchall()
        with local:
            local.executemany(
                f"INSERT OR REPLACE INTO runs ({', '.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [tuple(r) for r in rows],
            )
        written["runs"] = len(rows)

        written["tic_traces"] = _pull_tic(cur, local, since)
        written.update(_pull_detail_tables(cur, local))
        written["feature_clouds"] = _pull_feature_clouds(cur, local)
    finally:
        local.close()
    return written


def _pull_tic(cur, local, since: str = "") -> int:
    """Project PG's inline TIC columns into the local ``tic_traces`` table.

    PG stores the trace as JSONB arrays on the run row; SQLite's table wants
    JSON *strings* in ``rt_min`` / ``intensity`` (``get_tic_trace`` calls
    ``json.loads`` on them), so re-serialise rather than passing the parsed
    lists straight through.
    """
    sql = ("SELECT id, tic_rt_bins, tic_intensity FROM runs "
           "WHERE tic_rt_bins IS NOT NULL AND tic_intensity IS NOT NULL")
    params: tuple = ()
    if since:
        sql += " AND run_date >= %s"
        params = (since,)
    cur.execute(sql, params)

    batch = []
    for run_id, rt, inten in cur.fetchall():
        if not rt or not inten:
            continue
        # psycopg2 hands JSONB back already decoded; tolerate a str either way.
        if isinstance(rt, str):
            rt = json.loads(rt)
        if isinstance(inten, str):
            inten = json.loads(inten)
        batch.append((str(run_id), json.dumps(rt), json.dumps(inten), len(rt)))

    if not batch:
        return 0
    with local:
        local.executemany(
            "INSERT OR REPLACE INTO tic_traces (run_id, rt_min, intensity, n_frames) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )
    return len(batch)


# Detail tables are identical in both stores, so a plain column copy works.
_DETAIL_TABLES = (
    "peg_ion_hits", "drift_window_centroids", "drift_peak_clouds",
    # Sample Health moved to PG in v1.0.14. The dashboard still reads
    # SQLite, so mirror it down like everything else. host_origin exists
    # only on the PG side; the column intersection below drops it.
    "sample_health", "health_tic_traces",
    # The maintenance log: what was physically done to an instrument. Mirrored
    # so a SQLite-reading dashboard still shows it.
    "maintenance_events",
)


def _pull_detail_tables(cur, local) -> dict:
    """Copy the PEG/drift drill-down tables from PG into local SQLite.

    Missing tables are skipped rather than raised: PG needs an owner-run
    migration to create them, and the dashboard should keep working (showing
    summary scores without breakdowns) until that lands.
    """
    out: dict = {}
    for t in _DETAIL_TABLES:
        try:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = %s", (t,),
            )
            pg_cols = [r[0] for r in cur.fetchall()]
            if not pg_cols:
                continue
            sq_cols = [r[1] for r in local.execute(f"PRAGMA table_info({t})").fetchall()]
            cols = [c for c in sq_cols if c in pg_cols]
            if not cols:
                continue
            cur.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM {t}')
            rows = cur.fetchall()
        except Exception as e:  # noqa: BLE001 - table absent / not yet migrated
            logger.debug("skipping %s: %s", t, e)
            continue
        if not rows:
            out[t] = 0
            continue
        with local:
            local.executemany(
                f"INSERT OR REPLACE INTO {t} ({', '.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [tuple(r) for r in rows],
            )
        out[t] = len(rows)
    return out


def _pull_feature_clouds(cur, local) -> int:
    """Mirror ``feature_clouds`` from PG, pulling only rows we don't have.

    This table is two orders of magnitude fatter than the other detail
    tables -- ~400 KB of JSON per run against a few KB for
    ``drift_window_centroids`` -- so the blanket "SELECT everything,
    every refresh" treatment used above would drag tens of megabytes
    across the wire every ``STAN_PG_REFRESH_SECONDS``. Rows are
    effectively immutable (a re-backfill of the same run is rare), so an
    anti-join on the keys we already hold makes the steady-state cost
    zero.

    Set ``STAN_PG_CLOUD_FULL_REFRESH=1`` to force a full re-pull after a
    ``stan backfill-feature-cloud --force`` on the cluster side.
    """
    import os

    try:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'feature_clouds'"
        )
        pg_cols = [r[0] for r in cur.fetchall()]
        if not pg_cols:
            return 0
        sq_cols = [
            r[1] for r in
            local.execute("PRAGMA table_info(feature_clouds)").fetchall()
        ]
        cols = [c for c in sq_cols if c in pg_cols]
        if not cols:
            return 0

        full = (os.environ.get("STAN_PG_CLOUD_FULL_REFRESH") or "").strip().lower()
        have: set[tuple] = set()
        if full not in ("1", "true", "yes"):
            have = {
                (r[0], r[1]) for r in
                local.execute("SELECT run_id, source FROM feature_clouds")
            }

        quoted = ", ".join('"' + c + '"' for c in cols)
        # Fetch keys first so the fat JSON columns only cross the wire
        # for rows we are actually going to store. Newest first: a
        # dashboard catching up on a fresh backfill should light up the
        # runs someone is actually looking at, not 2024's.
        # Order by the run's acquisition date, not the cloud's created_at:
        # a bulk backfill stamps every row within the same minute, so
        # created_at carries no useful ordering afterwards and a catching-up
        # dashboard would fill in essentially at random. Fall back to
        # created_at if the join can't run (id type mismatch across
        # deployments).
        try:
            cur.execute(
                "SELECT f.run_id, f.source FROM feature_clouds f "
                "LEFT JOIN runs r ON r.id::text = f.run_id "
                "ORDER BY r.run_date DESC NULLS LAST"
            )
        except Exception:
            cur.connection.rollback()
            cur.execute(
                "SELECT run_id, source FROM feature_clouds "
                "ORDER BY created_at DESC NULLS LAST"
            )
        wanted = [k for k in cur.fetchall() if (k[0], k[1]) not in have]
        if not wanted:
            return 0

        # Drain a bounded slice per refresh. A first sync against a fully
        # backfilled fleet is ~170 MB; pulling it in one tick would stall
        # the refresh loop for minutes and hand the user a dashboard that
        # looks hung. Subsequent ticks pick up where this one stopped.
        try:
            max_pull = int(os.environ.get("STAN_PG_CLOUD_MAX_PULL", "50"))
        except ValueError:
            max_pull = 50
        if max_pull > 0:
            wanted = wanted[:max_pull]

        written = 0
        for i in range(0, len(wanted), 25):
            chunk = wanted[i:i + 25]
            cur.execute(
                f"SELECT {quoted} FROM feature_clouds "
                "WHERE (run_id, source) IN %s",
                (tuple((str(a), str(b)) for a, b in chunk),),
            )
            rows = cur.fetchall()
            if not rows:
                continue
            with local:
                local.executemany(
                    f"INSERT OR REPLACE INTO feature_clouds ({', '.join(cols)}) "
                    f"VALUES ({','.join('?' * len(cols))})",
                    [tuple(r) for r in rows],
                )
            written += len(rows)
        return written
    except Exception as e:  # noqa: BLE001 - a broken sync must not stall the rest
        # Warning, not debug: the "no ion cloud for this run" symptom is
        # indistinguishable from "not backfilled yet", so a silent failure
        # here is a bug that hides itself. The absent-table case already
        # returned above.
        logger.warning("feature_clouds sync failed: %s", e)
        return 0
