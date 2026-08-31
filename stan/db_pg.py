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
    # v1.0.2: migrated from the personal `brettsp` CAS token to the
    # `genome-proteomics-service-account` service account. The 7-day
    # token in the token file is now minted from the long-lived secret
    # (service-account.json) via scripts/pgfarm_refresh_token.py.
    "user": "genome-proteomics-service-account",
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
    if "lumos" in s:
        return "lumos"
    if "exploris" in s:
        return "exploris"
    if "timstof" in s or "tims-tof" in s:
        return "timstof"
    if "astral" in s:
        return "astral"
    return s.split()[0] if s else "hive"


PGFARM_LOGIN_URL = "https://pgfarm.library.ucdavis.edu/auth/service-account/login"
PGFARM_SERVICE_ACCOUNT = "genome-proteomics-service-account"


def _is_jwt(value: str) -> bool:
    """True if ``value`` looks like a JWT (header.payload.signature)."""
    return value.startswith("eyJ") and value.count(".") == 2


def _mint_jwt(secret: str) -> str:
    """Exchange the long-lived service-account secret for a fresh JWT.

    The secret never leaves this process and is never logged.
    """
    import json
    import urllib.request

    body = json.dumps({
        "username": os.environ.get("STAN_PGFARM_USER", PGFARM_SERVICE_ACCOUNT),
        "secret": secret,
    }).encode()
    req = urllib.request.Request(
        PGFARM_LOGIN_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        token = json.loads(resp.read().decode()).get("access_token")
    if not token:
        raise RuntimeError("PG Farm login returned no access_token")
    return token


def _resolve_pgpassword() -> str:
    """Find the PG Farm password, $PGPASSWORD first then the token file.

    The credential file may hold **either** a short-lived JWT or the
    long-lived 512-char service-account secret. A JWT is used as-is; a
    secret is exchanged for a fresh JWT on the spot.

    Minting on demand (the pattern FRAN's ``_token()`` has used
    reliably) is what makes this self-healing. The previous
    cron-refresh-only design coupled STAN's ability to reach PG to a
    cron tick succeeding every <7 days: when the dispatch cron died on
    2026-06-10 the JWT expired a week later and every PG write failed.
    It also broke on rotation — rotating the shared service-account
    secret for FRAN on ~2026-06-29 silently invalidated the copy in
    STAN's ``.pgfarm_secret.json``, so the refresh script itself could
    no longer mint. Accepting either form fixes both failure modes.
    """
    pwd = os.environ.get("PGPASSWORD", "").strip()
    if pwd:
        return pwd if _is_jwt(pwd) else _mint_jwt(pwd)
    # The same shared volume is mounted at different paths on Hive and on a
    # Mac, so try both rather than making every Mac-side caller export
    # STAN_PGFARM_TOKEN_FILE by hand.
    override = os.environ.get("STAN_PGFARM_TOKEN_FILE")
    candidates = [Path(override)] if override else [
        Path("/quobyte/proteomics-grp/brett/.pgfarm_token"),
        Path("/Volumes/proteomics-grp/brett/.pgfarm_token"),
    ]
    for token_file in candidates:
        if not token_file.exists():
            continue
        try:
            raw = token_file.read_text().strip()
        except OSError as e:
            logger.warning("could not read %s: %s", token_file, e)
            continue
        if raw:
            return raw if _is_jwt(raw) else _mint_jwt(raw)
    raise RuntimeError(
        "no PG Farm password — set PGPASSWORD or place a token at one of: "
        + ", ".join(str(c) for c in candidates)
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

    _CACHED_CONN = _connect_with_retry()
    return _CACHED_CONN


# PG Farm caps concurrent connections, and a Hive backlog drain runs ~100
# SLURM jobs at once that each want one. Without a retry the losers die with
# "remaining connection slots are reserved for roles with the SUPERUSER
# attribute" *after* their DIA-NN search already finished — throwing away
# hours of compute over a transient slot shortage. Backoff is jittered by PID
# so a fleet that all failed at the same instant doesn't retry in lockstep.
_CONNECT_MAX_ATTEMPTS = 6
_CONNECT_BASE_DELAY_S = 4.0
_TRANSIENT_CONNECT_MARKERS = (
    "remaining connection slots",
    "too many clients",
    "could not connect",
    "connection timed out",
    "server closed the connection",
    "temporarily unavailable",
)


def _is_transient_connect_error(exc: Exception) -> bool:
    """True when a failed connect is worth retrying rather than surfacing."""
    msg = str(exc).lower()
    return any(m in msg for m in _TRANSIENT_CONNECT_MARKERS)


def _connect_with_retry():
    """Open a PG Farm connection, retrying transient slot exhaustion.

    Raises the last error once the attempt budget is spent, so a genuine
    auth or config problem still fails loudly instead of hanging.
    """
    import random
    import time

    import psycopg2

    last: Exception | None = None
    for attempt in range(1, _CONNECT_MAX_ATTEMPTS + 1):
        try:
            return psycopg2.connect(
                password=_resolve_pgpassword(), **PG_DEFAULTS
            )
        except psycopg2.OperationalError as e:
            last = e
            if not _is_transient_connect_error(e):
                raise
            if attempt == _CONNECT_MAX_ATTEMPTS:
                break
            delay = _CONNECT_BASE_DELAY_S * (2 ** (attempt - 1))
            delay *= 0.5 + random.random()  # noqa: S311 - jitter, not crypto
            delay = min(delay, 90.0)
            logger.warning(
                "PG Farm connect attempt %d/%d failed (%s) — retrying in %.1fs",
                attempt, _CONNECT_MAX_ATTEMPTS,
                str(e).strip().splitlines()[0][:120], delay,
            )
            time.sleep(delay)
    assert last is not None
    raise last


def update_peg_result_pg(
    run_id: str,
    peg_score: float,
    peg_n_ions_detected: int,
    peg_intensity_pct: float,
    peg_class: str,
) -> bool:
    """Write a PEG detection result onto an existing PG ``runs`` row.

    PG counterpart of ``stan.db.update_peg_result``. Needed because the
    Hive pipeline inserts the row into PG but computed PEG/drift with
    SQLite-only helpers, so every UPDATE matched zero rows against an
    empty local stan.db and the (expensive) result was discarded. That is
    why timsTOF PEG/drift coverage fell to 0% from 2026-06 -- exactly when
    PG became the store of record -- while TIC, written inline at insert,
    kept working.

    Returns True if a row was updated, False if no such id exists.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "UPDATE runs SET peg_score = %s, peg_n_ions_detected = %s, "
            "peg_intensity_pct = %s, peg_class = %s WHERE id = %s",
            (peg_score, peg_n_ions_detected, peg_intensity_pct,
             peg_class, run_id),
        )
        n = cur.rowcount
        pg.commit()
    return n > 0


def update_drift_result_pg(
    run_id: str,
    drift_coverage: float,
    drift_median_im: float,
    drift_p90_abs_im: float,
    drift_class: str,
) -> bool:
    """Write a DIA window-drift result onto an existing PG ``runs`` row.

    PG counterpart of ``stan.db.update_drift_result``. See
    ``update_peg_result_pg`` for why this exists.

    Returns True if a row was updated, False if no such id exists.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "UPDATE runs SET drift_coverage = %s, drift_median_im = %s, "
            "drift_p90_abs_im = %s, drift_class = %s WHERE id = %s",
            (drift_coverage, drift_median_im, drift_p90_abs_im,
             drift_class, run_id),
        )
        n = cur.rowcount
        pg.commit()
    return n > 0


# ---------------------------------------------------------------------------
# PEG/drift detail writers.
#
# These take ALREADY-FLATTENED row tuples rather than the metric objects, so
# the dedup and field extraction stay in stan.db and both backends are
# guaranteed to write identical data. Duplicating the attribute walk here
# would be a second place to get `m.ion.n` vs `m.repeat_n` wrong.
# ---------------------------------------------------------------------------

def insert_peg_ion_hits_pg(run_id: str, rows: list, source: str = "runs") -> int:
    """Replace the PEG ion ladder for one run in PG. Returns rows written.

    ``rows`` are ``(run_id, source, mz, observed_intensity, adduct,
    repeat_n, charge, ppm_error)`` tuples, already deduped to the
    highest-intensity observation per ion by ``stan.db.insert_peg_ion_hits``.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute("DELETE FROM peg_ion_hits WHERE run_id = %s AND source = %s",
                    (run_id, source))
        if rows:
            cur.executemany(
                "INSERT INTO peg_ion_hits (run_id, source, mz, observed_intensity,"
                " adduct, repeat_n, charge, ppm_error) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (run_id, source, repeat_n, adduct, charge) "
                "DO UPDATE SET mz = EXCLUDED.mz, "
                "observed_intensity = EXCLUDED.observed_intensity, "
                "ppm_error = EXCLUDED.ppm_error",
                rows,
            )
        pg.commit()
    return len(rows)


def insert_drift_window_centroids_pg(run_id: str, rows: list, source: str = "runs") -> int:
    """Replace the per-window drift centroids for one run in PG.

    ``rows`` are ``(run_id, source, window_idx, mz_low, mz_high, im_low,
    im_high, im_center, im_mode, drift_im, coverage, in_peptide_zone)``.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute("DELETE FROM drift_window_centroids WHERE run_id = %s AND source = %s",
                    (run_id, source))
        if rows:
            cur.executemany(
                "INSERT INTO drift_window_centroids (run_id, source, window_idx,"
                " mz_low, mz_high, im_low, im_high, im_center, im_mode, drift_im,"
                " coverage, in_peptide_zone) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (run_id, source, window_idx) DO NOTHING",
                rows,
            )
        pg.commit()
    return len(rows)


def insert_drift_peak_cloud_pg(
    run_id: str, mz_json: str, im_json: str, log_intensity_json: str,
    n_points: int, source: str = "runs",
) -> int:
    """Store the ion-cloud scatter for one run in PG (JSON-array strings)."""
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "INSERT INTO drift_peak_clouds (run_id, source, mz, im, log_intensity, n_points) "
            "VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (run_id, source) DO UPDATE SET mz = EXCLUDED.mz, "
            "im = EXCLUDED.im, log_intensity = EXCLUDED.log_intensity, "
            "n_points = EXCLUDED.n_points",
            (run_id, source, mz_json, im_json, log_intensity_json, n_points),
        )
        pg.commit()
    return n_points


def insert_irt_anchor_rts_pg(run_id: str, rows: list) -> int:
    """Replace the cIRT anchor RTs for one run in PG. Returns rows written.

    ``rows`` are ``(run_id, peptide, observed_rt_min, reference_rt_min)``
    tuples, already assembled by ``stan.db.insert_irt_anchor_rts`` so both
    backends store identical data.

    Deletes first so a re-derived panel (different peptide set) doesn't
    leave orphaned anchors behind that the chart would draw as flat lines.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute("DELETE FROM irt_anchor_rts WHERE run_id = %s", (run_id,))
        if rows:
            cur.executemany(
                "INSERT INTO irt_anchor_rts "
                "(run_id, peptide, observed_rt_min, reference_rt_min) "
                "VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (run_id, peptide) DO UPDATE SET "
                "observed_rt_min = EXCLUDED.observed_rt_min, "
                "reference_rt_min = EXCLUDED.reference_rt_min",
                rows,
            )
        pg.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Sample Health (monitor pipeline).
#
# These were the last tables the Hive pipeline wrote to SQLite. The global
# stan.db lives on Quobyte and ~100 concurrent SLURM writers corrupted it
# three times; moving these writes to PG removes the last concurrent writer.
# SQLite stays fully supported for single-lab installs -- stan.db routes here
# only when use_pg().
# ---------------------------------------------------------------------------

_SH_COLUMNS = (
    "id", "instrument", "run_name", "run_date", "raw_path", "verdict",
    "reasons", "n_ms1_frames", "n_ms2_frames", "rt_duration_min",
    "ms1_max_intensity", "ms1_total_tic", "dynamic_range_log10",
    "dropout_rate_per_100_ms1", "pressure_mean_mbar", "pressure_range_mbar",
    "median_ms1_acc_ms", "host_origin",
)


def insert_sample_health_pg(row: dict) -> str:
    """Upsert one Sample Health row into PG. Returns the row id.

    ``row`` is already flattened by ``stan.db.insert_sample_health`` so the
    rawmeat-summary key mapping lives in exactly one place.
    """
    cols = [c for c in _SH_COLUMNS if c in row]
    placeholders = ",".join(["%s"] * len(cols))
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "id")
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            f"INSERT INTO sample_health ({', '.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT (id) DO UPDATE SET {updates}",
            tuple(row[c] for c in cols),
        )
        pg.commit()
    return str(row.get("id"))


def get_sample_health_pg(
    instrument: str | None = None,
    verdict: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Fetch recent Sample Health rows from PG, newest first."""
    clauses, args = [], []
    if instrument:
        clauses.append("instrument = %s")
        args.append(instrument)
    if verdict:
        clauses.append("verdict = %s")
        args.append(verdict)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(limit)
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            f"SELECT * FROM sample_health {where} ORDER BY run_date DESC LIMIT %s",
            tuple(args),
        )
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def rolling_median_ms1_max_intensity_pg(
    instrument: str, days: int = 30,
) -> float | None:
    """Median ms1_max_intensity over an instrument's recent health rows.

    run_date is TEXT (ISO 8601) to match SQLite, so compare against an ISO
    string rather than a PG interval on a timestamp column.
    """
    import statistics
    from datetime import datetime, timedelta, timezone

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "SELECT ms1_max_intensity FROM sample_health "
            "WHERE instrument = %s AND ms1_max_intensity IS NOT NULL "
            "  AND run_date >= %s",
            (instrument, cutoff),
        )
        vals = [r[0] for r in cur.fetchall() if r[0] and r[0] > 0]
    return statistics.median(vals) if vals else None


def insert_health_tic_trace_pg(
    health_id: str, rt_min: str, intensity: str,
    n_frames: int, bp_intensity: str | None = None,
) -> bool:
    """Store a Sample Health TIC trace in PG. Arrays arrive JSON-encoded."""
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "INSERT INTO health_tic_traces (health_id, rt_min, intensity, "
            "n_frames, bp_intensity) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (health_id) DO UPDATE SET rt_min = EXCLUDED.rt_min, "
            "intensity = EXCLUDED.intensity, n_frames = EXCLUDED.n_frames, "
            "bp_intensity = EXCLUDED.bp_intensity",
            (health_id, rt_min, intensity, n_frames, bp_intensity),
        )
        pg.commit()
    return True


_FEATURE_CLOUDS_DDL = """
CREATE TABLE IF NOT EXISTS feature_clouds (
    run_id        TEXT NOT NULL,
    source        TEXT NOT NULL,
    mz            TEXT NOT NULL,
    mobility      TEXT NOT NULL,
    rt            TEXT NOT NULL,
    charge        TEXT NOT NULL,
    intensity     TEXT NOT NULL,
    n_points      INTEGER NOT NULL,
    n_total       INTEGER NOT NULL,
    features_path TEXT,
    created_at    TEXT,
    PRIMARY KEY (run_id, source)
)
"""

# Create-once-per-process guard. The table is owner-created; every other
# writer just needs it to exist before the first INSERT.
_feature_clouds_ready = False


def ensure_feature_clouds_table_pg() -> bool:
    """Create ``feature_clouds`` in PG if it isn't there yet.

    Returns True when the table is usable. Swallows a permission error
    (a non-owner role can't CREATE) and returns False so the caller can
    report "ask the owner to run the migration" instead of crashing a
    backfill mid-walk.
    """
    global _feature_clouds_ready
    if _feature_clouds_ready:
        return True
    try:
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(_FEATURE_CLOUDS_DDL)
            pg.commit()
        _feature_clouds_ready = True
        return True
    except Exception as e:  # noqa: BLE001 - diagnostics, not control flow
        logger.warning("feature_clouds table not available in PG: %s", e)
        return False


def insert_feature_cloud_pg(
    run_id: str, mz_json: str, mobility_json: str, rt_json: str,
    charge_json: str, intensity_json: str, n_points: int,
    n_total: int = 0, features_path: str = "", source: str = "runs",
) -> int:
    """Store one charge-labeled 4DFF ion cloud in PG (JSON-array strings)."""
    from datetime import datetime, timezone

    ensure_feature_clouds_table_pg()
    created = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "INSERT INTO feature_clouds (run_id, source, mz, mobility, rt, "
            "charge, intensity, n_points, n_total, features_path, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (run_id, source) DO UPDATE SET "
            "mz = EXCLUDED.mz, mobility = EXCLUDED.mobility, "
            "rt = EXCLUDED.rt, charge = EXCLUDED.charge, "
            "intensity = EXCLUDED.intensity, n_points = EXCLUDED.n_points, "
            "n_total = EXCLUDED.n_total, "
            "features_path = EXCLUDED.features_path, "
            "created_at = EXCLUDED.created_at",
            (run_id, source, mz_json, mobility_json, rt_json, charge_json,
             intensity_json, n_points, n_total, features_path, created),
        )
        pg.commit()
    return n_points


def get_feature_cloud_pg(run_id: str, source: str = "runs") -> dict | None:
    """Read one charge-labeled ion cloud straight from PG.

    Needed for the PG-direct dashboard: without it the view silently
    depends on the SQLite mirror still running, and an install with
    ``STAN_PG_REFRESH_SECONDS=0`` would show empty ion clouds with no
    error anywhere to explain why.
    """
    import json as _json

    try:
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "SELECT mz, mobility, rt, charge, intensity, n_points, "
                "n_total, features_path FROM feature_clouds "
                "WHERE run_id = %s AND source = %s",
                (str(run_id), source),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 - table absent / unreachable
        logger.debug("feature cloud read from PG failed: %s", e)
        return None
    if row is None:
        return None

    def _arr(v):
        return _json.loads(v) if isinstance(v, str) else (v or [])

    return {
        "mz": _arr(row[0]),
        "mobility": _arr(row[1]),
        "rt": _arr(row[2]),
        "charge": _arr(row[3]),
        "intensity": _arr(row[4]),
        "n_points": row[5],
        "n_total": row[6],
        "features_path": row[7] or "",
    }


# ---------------------------------------------------------------------------
# Maintenance events.
#
# The operator's record of what was physically done to an instrument -- column
# changes, source cleans, PMs, LC service. It is what turns "IPS dropped on the
# 24th" into "because we swapped the column on the 24th", so it has to be
# fleet-wide rather than stranded on whichever PC happened to log it.
#
# No host_origin here, unlike runs/sample_health: an event is already keyed to
# a named instrument, and the same instrument can be logged from more than one
# host, so an origin column would fragment the history we're unifying.
# ---------------------------------------------------------------------------

_EVENT_COLUMNS = (
    "id", "instrument", "event_type", "event_date", "notes", "operator",
    "column_vendor", "column_model", "column_serial",
)


def insert_event_pg(row: dict) -> str:
    """Insert one maintenance event into PG. Returns the event id."""
    cols = [c for c in _EVENT_COLUMNS if c in row]
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            f"INSERT INTO maintenance_events ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (id) DO NOTHING",
            tuple(row[c] for c in cols),
        )
        pg.commit()
    return str(row.get("id"))


def get_events_pg(instrument: str | None = None, limit: int = 100) -> list[dict]:
    """Maintenance events from PG, newest first."""
    with _connect() as pg, pg.cursor() as cur:
        if instrument:
            cur.execute(
                "SELECT * FROM maintenance_events WHERE instrument = %s "
                "ORDER BY event_date DESC LIMIT %s", (instrument, limit))
        else:
            cur.execute(
                "SELECT * FROM maintenance_events ORDER BY event_date DESC "
                "LIMIT %s", (limit,))
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def get_last_event_pg(instrument: str, event_type: str) -> dict | None:
    """Most recent event of one type for an instrument, or None."""
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "SELECT * FROM maintenance_events WHERE instrument = %s "
            "AND event_type = %s ORDER BY event_date DESC LIMIT 1",
            (instrument, event_type))
        row = cur.fetchone()
        if not row:
            return None
        names = [d[0] for d in cur.description]
        return dict(zip(names, row))


def put_utilization_snapshot(generated_at: str, payload: str) -> bool:
    """Store the acquisition-counter snapshot centrally (single row)."""
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "INSERT INTO utilization_snapshot (id, generated_at, payload) "
            "VALUES ('current', %s, %s) ON CONFLICT (id) DO UPDATE SET "
            "generated_at = EXCLUDED.generated_at, payload = EXCLUDED.payload",
            (generated_at, payload),
        )
        pg.commit()
    return True


def get_utilization_snapshot() -> str | None:
    """Return the stored counter JSON, or None if nothing published yet."""
    with _connect() as pg, pg.cursor() as cur:
        cur.execute("SELECT payload FROM utilization_snapshot WHERE id = 'current'")
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Arcade leaderboard (migrations/2026-08-26_arcade_scores.sql)
#
# Rows arrive already flattened + sanitized by ``stan.db.insert_arcade_score``
# so the name/affiliation truncation lives in exactly one place and both
# backends store identical data.
# ---------------------------------------------------------------------------

_ARCADE_COLUMNS = (
    "id", "game", "score", "level", "won", "player_name", "affiliation",
    "submitted_by_host", "created_at",
)

#: What a reader is allowed to see. ``submitted_by_host`` is provenance for
#: moderation/de-dup, not board content — selecting the public subset here
#: rather than filtering at the API means no future endpoint can leak it by
#: forgetting to pop the key.
_ARCADE_PUBLIC_COLUMNS = (
    "id", "game", "score", "level", "won", "player_name", "affiliation",
    "created_at",
)


def insert_arcade_score_pg(row: dict) -> str:
    """Insert one arcade high score into PG. Returns the row id.

    ``id`` is a client-generated uuid hex, so a retry of the same
    submission is a no-op rather than a duplicate board entry.
    """
    cols = [c for c in _ARCADE_COLUMNS if c in row]
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            f"INSERT INTO arcade_scores ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (id) DO NOTHING",
            tuple(row[c] for c in cols),
        )
        pg.commit()
    return str(row.get("id"))


def get_arcade_leaderboard_pg(game: str | None = None, limit: int = 10) -> list[dict]:
    """Top arcade scores from PG, highest first.

    Ties break on ``created_at`` ascending so whoever got there first
    keeps the higher rank. ``game=None`` returns the top scores across
    every game, which is only useful for admin/debug — the arcade page
    asks per game.
    """
    cols = ", ".join(_ARCADE_PUBLIC_COLUMNS)
    with _connect() as pg, pg.cursor() as cur:
        if game:
            cur.execute(
                f"SELECT {cols} FROM arcade_scores WHERE game = %s "
                f"ORDER BY score DESC, created_at ASC LIMIT %s",
                (game, limit),
            )
        else:
            cur.execute(
                f"SELECT {cols} FROM arcade_scores "
                f"ORDER BY score DESC, created_at ASC LIMIT %s",
                (limit,),
            )
        return _rows(cur)


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

    # JSONB columns need an explicit Json wrapper — psycopg2's default
    # adapter sends Python lists as PG arrays (`{1, 2, ...}`), which the
    # JSONB column rejects. Listed here so adding a new JSONB column
    # to PG only requires touching this set.
    from psycopg2.extras import Json
    JSONB_COLS = {"tic_rt_bins", "tic_intensity"}
    for c in JSONB_COLS:
        if c in row and row[c] is not None:
            row[c] = Json(row[c])

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
    # COALESCE preserves existing non-NULL values when the new extraction
    # didn't produce a column. Without this, re-ingesting a row whose
    # current extractor path can't compute (e.g.) median_peak_width_sec
    # silently nulls the value populated by the SQLite-path before it.
    # Trade-off: a true NULL value can't be set to NULL via re-ingest
    # — needs an explicit UPDATE. Worth it for forward-only enrichment.
    updates = ", ".join(
        f'"{c}" = COALESCE(EXCLUDED."{c}", runs."{c}")'
        for c in update_cols
    )
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


def raw_run_id_pg(raw_path: str | Path) -> str | None:
    """Return the runs.id for ``raw_path`` if present in PG, else None.

    Cohort-independent (matches ``dispatch_hive._already_processed`` and
    ``hive_process._row_exists``): keyed on ``raw_path`` alone so a
    mislabeled instrument in a prior run can't trigger a duplicate
    submission. This is the PG-mode replacement for the SQLite dedup —
    in PG mode writes go only to PG, so the SQLite ``runs`` table never
    sees completions and must not be consulted for "already processed".
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "SELECT id FROM runs WHERE raw_path = %s LIMIT 1", (str(raw_path),)
        )
        r = cur.fetchone()
    return str(r[0]) if r else None


# ---------------------------------------------------------------------------
# Readers (dashboard).
#
# Until v1.0.15 the dashboard was a SQLite-only reader and PG reached it by
# way of a 5-minute mirror (``stan.sync.pg_to_sqlite``). That made every
# panel up to five minutes stale and put a full table copy on the wire each
# tick. These functions let ``stan.db`` read PG straight through when
# ``use_pg()``; the mirror stays for hosts that genuinely want a local cache.
#
# Every reader returns the SAME SHAPE as its SQLite counterpart -- same keys,
# same Python types. Two conversions carry that:
#
#   * PG ``runs.run_date`` / ``hidden_at`` / ``migrated_at`` are
#     ``timestamptz``; SQLite holds ISO-8601 TEXT. ``_normalize_row``
#     re-serialises datetimes with ``.isoformat()``, which is exactly what
#     ``_build_runs_row`` writes on the SQLite side.
#   * PG keeps the TIC inline on the run row as JSONB; SQLite keeps a
#     ``tic_traces`` side table of JSON strings. ``get_tic_trace_pg``
#     projects the former into the latter's shape.
# ---------------------------------------------------------------------------

# The inline TIC arrays are ~2 x 300 floats per run. A 150-row dashboard page
# would drag several MB across the wire that no caller of get_runs() looks at,
# so they are excluded from row reads and fetched on demand by
# get_tic_trace_pg / get_tic_traces_for_instrument_pg.
_RUNS_FAT_COLS = ("tic_rt_bins", "tic_intensity")

_RUNS_COLS_CACHE: list[str] | None = None


def _runs_columns(cur) -> list[str]:
    """Column list for ``SELECT`` on ``runs``, minus the fat TIC columns.

    Read from information_schema once per process so a column added to PG
    shows up without a code change, the same way ``SELECT *`` behaves on
    the SQLite side.
    """
    global _RUNS_COLS_CACHE
    if _RUNS_COLS_CACHE is None:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'runs' "
            "ORDER BY ordinal_position"
        )
        _RUNS_COLS_CACHE = [
            r[0] for r in cur.fetchall() if r[0] not in _RUNS_FAT_COLS
        ]
    return _RUNS_COLS_CACHE


def _normalize_row(d: dict) -> dict:
    """Coerce PG-native scalars to the types the SQLite reader yields."""
    import datetime as _dt
    import decimal as _dec

    for k, v in d.items():
        if isinstance(v, _dt.datetime):
            d[k] = v.isoformat()
        elif isinstance(v, (_dt.date, _dt.time)):
            d[k] = v.isoformat()
        elif isinstance(v, _dec.Decimal):
            d[k] = float(v)
        elif isinstance(v, memoryview):
            d[k] = bytes(v)
    return d


def _rows(cur) -> list[dict]:
    """Fetch the open cursor as normalized dicts."""
    names = [c[0] for c in cur.description]
    return [_normalize_row(dict(zip(names, r))) for r in cur.fetchall()]


def _as_list(v) -> list:
    """JSONB comes back decoded; tolerate a TEXT column holding JSON too."""
    if v is None:
        return []
    if isinstance(v, str):
        import json as _json
        try:
            v = _json.loads(v)
        except ValueError:
            return []
    return list(v) if isinstance(v, (list, tuple)) else []


def get_runs_pg(
    instrument: str | None = None,
    limit: int = 50,
    offset: int = 0,
    qc_only: bool = False,
    include_hidden: bool = False,
) -> list[dict]:
    """Recent ``runs`` rows from PG, newest first.

    Mirrors ``stan.db.get_runs``'s SQL half: same WHERE clauses, same
    ``ORDER BY run_date DESC``, same 3x over-fetch when the caller will
    post-filter to QC rows. The QC filtering itself stays in ``stan.db``
    so both backends share one copy of it.
    """
    fetch = limit * 3 if qc_only else limit
    where, args = [], []
    if instrument:
        where.append("instrument = %s")
        args.append(instrument)
    if not include_hidden:
        where.append("(hidden IS NULL OR hidden = 0)")
    clause = f" WHERE {' AND '.join(where)}" if where else ""
    args.extend([fetch, offset])
    with _connect() as pg, pg.cursor() as cur:
        cols = ", ".join(f'"{c}"' for c in _runs_columns(cur))
        cur.execute(
            f"SELECT {cols} FROM runs{clause} "
            f"ORDER BY run_date DESC LIMIT %s OFFSET %s",
            tuple(args),
        )
        return _rows(cur)


def get_run_pg(run_id: str) -> dict | None:
    """One ``runs`` row by id, or None."""
    with _connect() as pg, pg.cursor() as cur:
        cols = ", ".join(f'"{c}"' for c in _runs_columns(cur))
        cur.execute(f"SELECT {cols} FROM runs WHERE id = %s", (run_id,))
        rows = _rows(cur)
    return rows[0] if rows else None


def get_trends_pg(
    instrument: str,
    limit: int = 100,
    qc_only: bool = False,
    include_hidden: bool = False,
) -> list[dict]:
    """Trend rows for one instrument, oldest-first for charting.

    Takes the NEWEST ``limit`` (x3 when the caller will drop non-QC rows)
    and only then flips to ascending -- selecting ``ORDER BY run_date ASC
    LIMIT n`` would pin every trend chart to the oldest rows in a table
    that now holds the whole fleet's multi-year history. Same inner/outer
    shape as the SQLite query it mirrors.
    """
    fetch = limit * 3 if qc_only else limit
    inner_where = ["instrument = %s"]
    args: list = [instrument]
    if not include_hidden:
        inner_where.append("(hidden IS NULL OR hidden = 0)")
    args.append(fetch)
    with _connect() as pg, pg.cursor() as cur:
        cols = ", ".join(f'"{c}"' for c in _runs_columns(cur))
        cur.execute(
            f"SELECT * FROM (SELECT {cols} FROM runs "
            f"WHERE {' AND '.join(inner_where)} "
            f"ORDER BY run_date DESC LIMIT %s) t ORDER BY run_date ASC",
            tuple(args),
        )
        return _rows(cur)


def get_tic_trace_pg(run_id: str) -> dict | None:
    """Project PG's inline TIC columns into SQLite's ``tic_traces`` shape.

    Returns ``{run_id, rt_min, intensity, n_frames}`` -- lists, not JSON
    strings, exactly as ``stan.db.get_tic_trace`` returns after its
    ``json.loads``. ``n_frames`` is ``len(rt_min)``, matching what the
    mirror wrote into the SQLite column.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "SELECT id, tic_rt_bins, tic_intensity FROM runs WHERE id = %s",
            (run_id,),
        )
        row = cur.fetchone()
    if not row:
        return None
    rt, inten = _as_list(row[1]), _as_list(row[2])
    if not rt or not inten:
        return None
    return {
        "run_id": str(row[0]),
        "rt_min": rt,
        "intensity": inten,
        "n_frames": len(rt),
    }


def get_tic_traces_for_instrument_pg(
    instrument: str, limit: int = 20,
) -> list[dict]:
    """Recent TIC traces for an instrument, newest first.

    The SQLite version joins ``tic_traces`` to ``runs``; in PG the trace
    already lives on the run row, so the ``IS NOT NULL`` predicates stand
    in for the join's inner-join semantics.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "SELECT id, tic_rt_bins, tic_intensity, run_name, run_date, "
            "gate_result FROM runs WHERE instrument = %s "
            "AND tic_rt_bins IS NOT NULL AND tic_intensity IS NOT NULL "
            "ORDER BY run_date DESC LIMIT %s",
            (instrument, limit),
        )
        raw = cur.fetchall()
    out = []
    for run_id, rt, inten, run_name, run_date, gate in raw:
        rt, inten = _as_list(rt), _as_list(inten)
        if not rt or not inten:
            continue
        out.append({
            "run_id": str(run_id),
            "rt_min": rt,
            "intensity": inten,
            "n_frames": len(rt),
            "run_name": run_name,
            "run_date": run_date.isoformat() if hasattr(run_date, "isoformat")
            else run_date,
            "gate_result": gate,
        })
    return out


def get_peg_ion_hits_pg(run_id: str, source: str = "runs") -> list[dict]:
    """PEG ion ladder for one run, sorted by m/z."""
    try:
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "SELECT mz, observed_intensity, adduct, repeat_n, charge, "
                "ppm_error FROM peg_ion_hits WHERE run_id = %s AND source = %s "
                "ORDER BY mz ASC",
                (run_id, source),
            )
            return _rows(cur)
    except Exception as e:  # noqa: BLE001 - table not migrated yet
        logger.warning("get_peg_ion_hits_pg: %s", e)
        return []


def get_cirt_history_pg(instrument: str, limit: int = 500) -> list[dict]:
    """cIRT anchor observations joined to their runs, oldest-first.

    Same shape as the SQLite half in ``stan.db.get_cirt_history``:
    one row per (run, anchor peptide), capped newest-first and then
    re-sorted ascending so a long history loses its oldest rows rather
    than the recent end the chart is about. ``run_date`` comes back as
    an ISO string via ``_rows`` -- psycopg2 hands back ``datetime``
    where SQLite hands back text, and the dashboard slices it as text.
    """
    try:
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "SELECT * FROM ("
                "  SELECT r.id AS run_id, r.run_name, r.run_date, r.spd,"
                "         a.peptide, a.observed_rt_min, a.reference_rt_min"
                "  FROM runs r JOIN irt_anchor_rts a ON a.run_id = r.id"
                "  WHERE r.instrument = %s"
                "  ORDER BY r.run_date DESC LIMIT %s"
                ") t ORDER BY run_date ASC",
                (instrument, limit),
            )
            rows = _rows(cur)
    except Exception as e:  # noqa: BLE001 - table not migrated yet
        logger.warning("get_cirt_history_pg: %s", e)
        return []
    for r in rows:
        r["run_id"] = str(r["run_id"])
    return rows


def get_drift_window_centroids_pg(run_id: str, source: str = "runs") -> list[dict]:
    """Per-window DIA drift centroids for one run, by window index."""
    try:
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "SELECT window_idx, mz_low, mz_high, im_low, im_high, "
                "im_center, im_mode, drift_im, coverage, in_peptide_zone "
                "FROM drift_window_centroids WHERE run_id = %s AND source = %s "
                "ORDER BY window_idx ASC",
                (run_id, source),
            )
            rows = _rows(cur)
    except Exception as e:  # noqa: BLE001 - table not migrated yet
        logger.warning("get_drift_window_centroids_pg: %s", e)
        return []
    # Same normalisation the SQLite reader does: API callers always see an
    # int 0/1, never NULL, on this key.
    for r in rows:
        r["in_peptide_zone"] = int(r.get("in_peptide_zone") or 0)
    return rows


def get_drift_peak_cloud_pg(run_id: str, source: str = "runs") -> dict | None:
    """Stored MS1 ion cloud as ``{mz, im, log_intensity, n_points}``.

    The three arrays are TEXT columns holding JSON in both backends, so
    this decodes them the same way the SQLite reader does.
    """
    import json as _json
    try:
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "SELECT mz, im, log_intensity, n_points FROM drift_peak_clouds "
                "WHERE run_id = %s AND source = %s",
                (run_id, source),
            )
            row = cur.fetchone()
    except Exception as e:  # noqa: BLE001 - table not migrated yet
        logger.warning("get_drift_peak_cloud_pg: %s", e)
        return None
    if row is None:
        return None
    return {
        "mz": _json.loads(row[0]),
        "im": _json.loads(row[1]),
        "log_intensity": _json.loads(row[2]),
        "n_points": row[3],
    }


def get_detail_summary_pg(run_id: str, table: str, cols: "tuple[str, ...]") -> dict:
    """Scalar columns from ``runs``/``sample_health`` for a detail panel.

    Backs the PEG/drift endpoints' summary badge. ``table`` is validated
    by the caller against a two-item allowlist before it reaches here.
    """
    col_list = ", ".join(f'"{c}"' for c in cols)
    try:
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                f"SELECT {col_list} FROM {table} WHERE id = %s", (run_id,)
            )
            rows = _rows(cur)
    except Exception as e:  # noqa: BLE001
        logger.warning("get_detail_summary_pg(%s): %s", table, e)
        return {}
    return rows[0] if rows else {}


def set_run_hidden_pg(run_id: str, hidden: bool, reason: str = "") -> bool:
    """Soft-delete or restore a run in PG. Returns True if a row changed.

    The dashboard's hide button is a write against the same table the
    dashboard reads. Once reads come from PG, leaving this on SQLite
    means the row reappears on the next page load -- the operator hides
    a bad run and nothing happens.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat() if hidden else None
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "UPDATE runs SET hidden = %s, hidden_reason = %s, hidden_at = %s "
            "WHERE id = %s",
            (1 if hidden else 0, reason or None, now, run_id),
        )
        n = cur.rowcount
        pg.commit()
    return n > 0


def mark_submitted_pg(run_id: str, submission_id: str) -> bool:
    """Flag a run as submitted to the community benchmark in PG.

    ``stan submit-all --backend pg`` pushes rows read from PG, so the
    "already submitted" bookkeeping has to land there too -- against
    SQLite it marks a row nothing will ever read again, and the next
    submit-all re-sends every run.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "UPDATE runs SET submitted_to_benchmark = 1, submission_id = %s "
            "WHERE id = %s",
            (submission_id, run_id),
        )
        n = cur.rowcount
        pg.commit()
    return n > 0


def probe_pg(timeout: int = 8) -> bool:
    """One bounded attempt to reach PG Farm. True when it answered.

    ``stan dashboard`` calls this to decide whether to read PG directly.
    It deliberately does NOT go through ``_connect_with_retry`` -- that
    backs off for up to several minutes on slot exhaustion, which is the
    right behaviour for a search job that has already spent an hour of
    compute and the wrong behaviour for a startup probe. A successful
    connection is stashed as the module cache, so the probe costs one
    connection, not two.
    """
    global _CACHED_CONN
    try:
        import psycopg2
        conn = psycopg2.connect(
            password=_resolve_pgpassword(), connect_timeout=timeout,
            **PG_DEFAULTS,
        )
    except Exception as e:  # noqa: BLE001 - absence of PG is a normal state
        logger.info("PG Farm not available (%s)",
                    str(e).strip().splitlines()[0][:120])
        return False
    if _CACHED_CONN is None:
        _CACHED_CONN = conn
    else:
        conn.close()
    return True


def use_pg() -> bool:
    """Return True when the PG backend should be used for reads and writes."""
    return os.environ.get("STAN_DB_BACKEND", "").lower() == "pg"


def insert_ht_search_pg(row: dict) -> str:
    """Insert one recorded HT search into PG."""
    cols = list(row.keys())
    marks = ", ".join(["%s"] * len(cols))
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            f"INSERT INTO ht_searches ({', '.join(cols)}) VALUES ({marks})",
            [row[c] for c in cols],
        )
    return row["id"]


def get_ht_searches_pg(submission: str | None = None, limit: int = 200) -> list[dict]:
    """Recorded HT searches from PG, newest first."""
    sql = "SELECT * FROM ht_searches"
    args: list = []
    if submission:
        sql += " WHERE submission = %s"
        args.append(submission)
    sql += " ORDER BY searched_at DESC LIMIT %s"
    args.append(int(limit))
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(sql, args)
        names = [d[0] for d in cur.description]
        # Normalise timestamps to ISO strings: psycopg2 returns datetime
        # where SQLite returns text, and the dashboard renders whatever it
        # is given. That mismatch has bitten this codebase before.
        out = []
        for r in cur.fetchall():
            d = dict(zip(names, r))
            for k, v in list(d.items()):
                if hasattr(v, "isoformat"):
                    d[k] = v.isoformat()
            out.append(d)
        return out

