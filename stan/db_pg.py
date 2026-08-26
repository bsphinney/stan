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
    if "lumos" in s: return "lumos"
    if "exploris" in s: return "exploris"
    if "timstof" in s or "tims-tof" in s: return "timstof"
    if "astral" in s: return "astral"
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


def use_pg() -> bool:
    """Return True when the PG backend should be used for writes."""
    return os.environ.get("STAN_DB_BACKEND", "").lower() == "pg"
