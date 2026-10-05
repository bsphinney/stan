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

import json
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


def _token_candidates() -> list[Path]:
    """Where a PG Farm credential file may live on this host.

    The same shared volume is mounted at different paths on Hive and on a
    Mac, so try both rather than making every Mac-side caller export
    STAN_PGFARM_TOKEN_FILE by hand.
    """
    override = os.environ.get("STAN_PGFARM_TOKEN_FILE")
    if override:
        return [Path(override)]
    return [
        Path("/quobyte/proteomics-grp/brett/.pgfarm_token"),
        Path("/Volumes/proteomics-grp/brett/.pgfarm_token"),
    ]


def pg_configured() -> bool:
    """True when this host has a PG Farm credential to try at all.

    Env/file check only — deliberately does NOT call
    ``_resolve_pgpassword``, which mints a JWT over the network when the
    stored credential is the long-lived secret. ``stan doctor`` runs on
    instrument PCs that have never heard of PG Farm, and answering "no PG
    here" must not cost a network round-trip.
    """
    if os.environ.get("PGPASSWORD", "").strip():
        return True
    first_error: OSError | None = None
    for p in _token_candidates():
        try:
            if p.exists():
                return True
        except OSError as exc:
            # An unreadable candidate (EACCES, a dead mount) must not hide a
            # readable one later in the list...
            first_error = first_error or exc
            continue
    if first_error is not None:
        # ...but when nothing was readable, say so: the dashboard's mirror
        # gate turns this into one warning naming the path.
        raise first_error
    return False


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
    candidates = _token_candidates()
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


def _warn_if_left_in_transaction(conn) -> bool:
    """Log when the cached connection comes back still inside a transaction.

    That state means the previous caller ran statements on ``_connect()``
    *outside* a ``with`` block: psycopg2's context-manager exit is the only
    thing that ends a transaction here, and nothing else does.

    It is not a cosmetic problem. On 2026-09-02 ``pull_from_pg`` did exactly
    this in the dashboard process and left one read transaction open for 19
    minutes. It held AccessShareLock on every table it had touched, including
    ``maintenance_events``; the ``ALTER TABLE`` behind it queued an
    AccessExclusiveLock, and a *queued* exclusive lock blocks every NEW
    reader as well — so the migration stalled and the maintenance calendar
    went dark with it. The same open snapshot also pins VACUUM's cleanup
    horizon, which bloats tables quietly for as long as it lasts.

    Deliberately warn-only. Rolling back here would be wrong the moment a
    caller nests: an inner ``_connect()`` hands back this same cached
    connection, and the rollback would silently discard the outer caller's
    work. No such nesting exists today (every other call site is
    ``with _connect() as pg, pg.cursor() as cur``), and turning this into a
    silent data-loss trap for whoever writes the first one is not worth the
    auto-heal. The real backstops are the fixed call sites and the
    role-level ``idle_in_transaction_session_timeout``
    (migrations/2026-09-02_idle_in_transaction_timeout.sql).

    Returns True when a leaked transaction was detected, for tests.
    """
    try:
        from psycopg2.extensions import TRANSACTION_STATUS_IDLE
        status = conn.info.transaction_status
    except Exception:  # noqa: BLE001 - diagnostics must never break a connect
        return False
    if status == TRANSACTION_STATUS_IDLE:
        return False
    logger.warning(
        "PG Farm connection handed back inside a transaction "
        "(transaction_status=%s). A caller ran statements on _connect() "
        "outside a `with` block; it is holding read locks and pinning "
        "VACUUM until something commits. This is what blocks migrations.",
        status,
    )
    return True


def _connect():
    """Return a cached PG Farm connection, opening one if needed.

    The connection is cached at module level so repeated calls in the
    same process (e.g. the orphan re-ingest loop, or a long-running
    SLURM job that writes multiple rows) skip the ~300-500ms SSL
    handshake each time. psycopg2's context-manager exit (``with
    _connect() as pg: ...``) commits/rollbacks the transaction but
    does NOT close the connection, so callers can keep using the
    context-manager pattern.

    That last sentence is also the trap: a caller that runs statements
    *without* a ``with`` block leaves the transaction open on the shared
    cached connection forever. Always use
    ``with _connect() as pg, pg.cursor() as cur:``.

    Reconnects if the cached connection has been closed by the server
    (idle timeout, network blip).
    """
    global _CACHED_CONN
    import psycopg2

    if _CACHED_CONN is not None:
        try:
            _warn_if_left_in_transaction(_CACHED_CONN)
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


# How long a session may sit "idle in transaction" before STAN calls it a
# leak rather than a slow moment. Everything STAN does legitimately is one
# statement plus a commit, or a read whose slow half (writing the SQLite
# mirror) now happens outside the transaction — seconds, not minutes. Two
# minutes is comfortably above that and well below the 5-minute role-level
# termination threshold in
# migrations/2026-09-02_idle_in_transaction_timeout.sql, so `stan doctor`
# names the offender before PG kills it and the evidence disappears.
IDLE_TX_WARN_SECONDS = 120


def idle_in_transaction_sessions(
    min_seconds: int = IDLE_TX_WARN_SECONDS,
) -> list[dict]:
    """PG Farm sessions parked ``idle in transaction`` for too long.

    A session in this state is not running anything, but it still holds
    every lock its transaction took. That is invisible until a migration
    blocks on it — which is how the 2026-09-02 incident was found, 25
    minutes in, by hand. One query turns that into a five-second check.

    Reads ``pg_stat_activity``, which only shows another role's ``query``
    text to a superuser: run as the service account (STAN and FRAN both
    connect as it) this sees the sessions that actually matter. Returns
    ``[]`` — never raises — when the view is unreadable, so a diagnostic
    can call it unconditionally.
    """
    rows: list[tuple] = []
    try:
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "SELECT pid, state, coalesce(application_name, ''), "
                "       extract(epoch FROM (now() - xact_start))::int, "
                "       extract(epoch FROM (now() - state_change))::int, "
                "       left(coalesce(query, ''), 120) "
                "  FROM pg_stat_activity "
                " WHERE datname = current_database() "
                "   AND pid <> pg_backend_pid() "
                "   AND state IN ('idle in transaction', "
                "                 'idle in transaction (aborted)') "
                "   AND xact_start < now() - make_interval(secs => %s) "
                " ORDER BY xact_start",
                (int(min_seconds),),
            )
            rows = cur.fetchall()
    except Exception as e:  # noqa: BLE001 - a diagnostic must not raise
        logger.debug("idle-in-transaction probe failed: %s", e)
        return []
    return [
        {
            "pid": r[0],
            "state": r[1],
            "application_name": r[2],
            "xact_age_s": int(r[3] or 0),
            "idle_s": int(r[4] or 0),
            "last_query": (r[5] or "").strip(),
        }
        for r in rows
    ]


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
    "median_ms1_acc_ms", "host_origin", "spd",
)


#: Cached set of columns ``sample_health`` actually has in PG, or None
#: before the first lookup. The code ships ahead of the owner migration
#: -- instrument PCs pull main automatically, while ALTER TABLE on PG
#: Farm needs the table owner's CAS login -- so between the two there is
#: a window where the INSERT below would name a column that does not
#: exist yet and every sample-health write on Hive would fail. Narrowing
#: the column list to what is really there keeps ingest running through
#: that window; the new column simply stays NULL until the migration
#: lands. Cached for the life of the process, which is right for a
#: short-lived Hive job and picked up on the next dashboard restart.
_SH_PG_COLUMNS: set | None = None


def _sample_health_pg_columns() -> set:
    """Columns present on the PG ``sample_health`` table.

    Returns an empty set if introspection fails, which callers read as
    "don't narrow anything" -- a lookup problem must not silently drop
    data from a write.
    """
    global _SH_PG_COLUMNS
    if _SH_PG_COLUMNS is None:
        try:
            with _connect() as pg, pg.cursor() as cur:
                cur.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'sample_health'"
                )
                _SH_PG_COLUMNS = {r[0] for r in cur.fetchall()}
        except Exception:  # noqa: BLE001
            logger.debug("sample_health column introspection failed",
                         exc_info=True)
            _SH_PG_COLUMNS = set()
    return _SH_PG_COLUMNS


def insert_sample_health_pg(row: dict) -> str:
    """Upsert one Sample Health row into PG. Returns the row id.

    ``row`` is already flattened by ``stan.db.insert_sample_health`` so the
    rawmeat-summary key mapping lives in exactly one place.
    """
    present = _sample_health_pg_columns()
    cols = [c for c in _SH_COLUMNS
            if c in row and (not present or c in present)]
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


def sample_health_spd_candidates_pg(force: bool = False) -> list[dict]:
    """Rows the SPD backfill should consider, newest first.

    Only rows still missing an SPD unless ``force``, so the routine
    Hive tick stays cheap once the archive is caught up.
    """
    where = "" if force else "WHERE spd IS NULL"
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            f"SELECT id, run_name, raw_path, spd FROM sample_health {where} "
            "ORDER BY run_date DESC"
        )
        names = [d[0] for d in cur.description]
        return [dict(zip(names, r)) for r in cur.fetchall()]


def update_sample_health_spd_pg(updates: list[tuple[str, int]]) -> int:
    """Write resolved SPDs back to PG. ``updates`` is [(row_id, spd)].

    Batched in one statement per chunk rather than one round trip per
    row -- the first sweep of this table is a few thousand rows against
    a database on the other side of campus.
    """
    if not updates:
        return 0
    n = 0
    with _connect() as pg, pg.cursor() as cur:
        for i in range(0, len(updates), 500):
            chunk = updates[i:i + 500]
            cur.executemany(
                "UPDATE sample_health SET spd = %s WHERE id = %s",
                [(spd, rid) for rid, spd in chunk],
            )
            n += len(chunk)
        pg.commit()
    return n


def spd_usage_by_instrument_pg(cutoff: str) -> dict[str, dict[int, int]]:
    """PG counterpart of ``stan.db.spd_usage_by_instrument``."""
    # run_date is NOT the same type in the two tables: `runs.run_date` is
    # timestamptz while `sample_health.run_date` is text. Unioning them raw
    # fails, and so does substr() on a timestamptz -- "function
    # substr(timestamp with time zone, integer, integer) does not exist".
    # Cast both to text so the ISO-prefix comparison works on either.
    # `runs.hidden` is an integer, not a boolean, so compare it to 0.
    sql = (
        "SELECT instrument, spd, COUNT(*) FROM ("
        "  SELECT instrument, spd, run_date::text AS run_date FROM runs"
        "   WHERE hidden IS NULL OR hidden = 0"
        "  UNION ALL"
        "  SELECT instrument, spd, run_date::text AS run_date FROM sample_health"
        ") u WHERE spd IS NOT NULL AND substr(run_date, 1, 10) >= %s "
        "GROUP BY instrument, spd"
    )
    out: dict[str, dict[int, int]] = {}
    try:
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(sql, (cutoff,))
            rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - sample_health.spd may predate v1.0.85
        logger.debug("spd usage union failed; falling back to runs",
                     exc_info=True)
        try:
            with _connect() as pg, pg.cursor() as cur:
                cur.execute(
                    "SELECT instrument, spd, COUNT(*) FROM runs "
                    "WHERE spd IS NOT NULL AND substr(run_date::text, 1, 10) >= %s "
                    "AND (hidden IS NULL OR hidden = 0) "
                    "GROUP BY instrument, spd", (cutoff,),
                )
                rows = cur.fetchall()
        except Exception:  # noqa: BLE001
            logger.debug("spd usage unavailable", exc_info=True)
            return {}
    for inst, spd, n in rows:
        if inst and spd is not None:
            out.setdefault(inst, {})[int(spd)] = int(n)
    return out


def _text_date_floor(since) -> str:
    """``'YYYY-MM-DD'`` one day before ``since``, for a TEXT ``run_date``.

    ``sample_health.run_date`` is TEXT holding the acquisition's LOCAL time
    with its offset -- ``'2026-09-21T20:13:45.944-07:00'`` -- and 51 legacy
    rows carry no offset at all (measured 2026-09-22). A string compare
    against a UTC instant is therefore off by up to a day at the boundary,
    and a ``::timestamptz`` cast would fail the whole query on the first
    malformed row. Comparing the calendar-date prefix one day early is
    immune to both, and can only over-include: for a lower bound, a day too
    many is harmless and a day too few is not.
    """
    import datetime as _dt

    if isinstance(since, _dt.datetime):
        day = (since.astimezone(_dt.timezone.utc) if since.tzinfo else since).date()
    elif isinstance(since, _dt.date):
        day = since
    else:
        day = _dt.date.fromisoformat(str(since)[:10])
    return (day - _dt.timedelta(days=1)).isoformat()


def get_sample_health_pg(
    instrument: str | None = None,
    verdict: str | None = None,
    limit: int = 200,
    since=None,
) -> list[dict]:
    """Fetch recent Sample Health rows from PG, newest first.

    Args:
        instrument: Only this instrument, when given.
        verdict: Only this verdict, when given.
        limit: Maximum rows.
        since: Lower bound on ``run_date`` (datetime, date or ISO string),
            applied in SQL so the rows before it never cross the wire. None
            (the default) is unbounded, as it always was. Compared on the
            date prefix with a day of slack -- see ``_text_date_floor`` --
            and rows with no ``run_date`` are kept, because an undated row
            cannot be shown to fall before the bound.
    """
    clauses, args = [], []
    if instrument:
        clauses.append("instrument = %s")
        args.append(instrument)
    if verdict:
        clauses.append("verdict = %s")
        args.append(verdict)
    if since is not None:
        # TEXT column: compare text to text. Never pass a datetime here --
        # PG would coerce the column side, and "PG and SQLite do not share
        # column types" (CLAUDE.md) is exactly how that goes wrong.
        clauses.append("(run_date >= %s OR run_date IS NULL OR run_date = '')")
        args.append(_text_date_floor(since))
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

# Every column the table can have. Names are interpolated into the INSERT, so
# this list is also what keeps a caller's dict keys out of the SQL.
#
# It MUST grow with every migration. Until 2026-09-23 it stopped at
# column_serial, a month after migrations/2026-08-28_maintenance_downtime.sql
# added the rest: log_event checked each optional column existed and put it
# in the row, and this list then quietly dropped it. Every event logged in PG
# mode -- the hosted dashboard form included -- lost its first_run,
# part_spec, end_date, created_by, created_at and share_community. The one
# row that ever had a first_run got it from a hand-written UPDATE.
_EVENT_COLUMNS = (
    "id", "instrument", "event_type", "event_date", "notes", "operator",
    "column_vendor", "column_model", "column_serial",
    "first_run", "part_spec", "end_date",
    "created_by", "created_at", "share_community",
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


#: Columns of the live PG ``runs`` table, read once per process (see
#: :func:`_pg_runs_write_columns`). None until the first insert.
_RUNS_WRITE_COLS: set | None = None
#: Row keys already reported as missing from PG, so the warning is logged
#: once per process rather than once per run.
_RUNS_DROPPED_LOGGED: set = set()


def _pg_runs_write_columns(cur) -> set:
    """Column names of PG ``public.runs``, cached for the process.

    ``_build_runs_row`` gains a key whenever STAN records something new,
    and the PG migration that adds the column needs the table owner
    (``brettsp``, CAS login), so it lands later and on Brett's schedule.
    Until it does, writing the new key would fail every Hive insert
    ("column ... of relation runs does not exist"). Reading the live
    column list lets the code ship first: the new keys are simply not
    written until the column exists. A short-lived Hive job reads it once;
    the dashboard picks up a migration on its next restart.

    Raises whatever the query raises: an unreadable schema means the
    connection is broken, and the insert would fail anyway.
    """
    global _RUNS_WRITE_COLS
    if _RUNS_WRITE_COLS is None:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'runs'"
        )
        cols = {r[0] for r in cur.fetchall()}
        if not cols:
            # Not visible to this role: do not narrow on an empty answer,
            # which would drop every column. Let the INSERT speak for itself.
            return set()
        _RUNS_WRITE_COLS = cols
    return _RUNS_WRITE_COLS


def _filter_runs_row_for_pg(row: dict, present: set) -> dict:
    """``row`` without the keys PG ``runs`` does not have (yet).

    Logs the dropped keys once per process at WARNING, naming the
    migration that adds them, so a Hive log shows why they are NULL.
    """
    if not present:
        return row
    dropped = sorted(k for k in row if k not in present)
    new = [k for k in dropped if k not in _RUNS_DROPPED_LOGGED]
    if new:
        _RUNS_DROPPED_LOGGED.update(new)
        logger.warning(
            "PG runs has no column(s) %s; not writing them until the owner "
            "migration is applied (migrations/2026-10-05_runs_lc_faims.sql, "
            "see docs/PG_FARM.md)", ", ".join(new),
        )
    return {k: v for k, v in row.items() if k in present}


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

    with _connect() as pg, pg.cursor() as cur:
        # Only the columns PG has: a key added to _build_runs_row ahead of
        # its owner migration must not break every Hive insert.
        row = _filter_runs_row_for_pg(row, _pg_runs_write_columns(cur))
        run_id = _upsert_runs_row(cur, row, host_origin)
        pg.commit()
    logger.info("PG insert %s: %s (%s)", run_id[:8], run_name, host_origin)
    return run_id


def _upsert_runs_row(cur, row: dict, host_origin: str) -> str:
    """Execute the natural-key upsert of one built ``runs`` row."""
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
    cur.execute(sql, values)
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
    since=None,
) -> list[dict]:
    """Recent ``runs`` rows from PG, newest first.

    Mirrors ``stan.db.get_runs``'s SQL half: same WHERE clauses, same
    ``ORDER BY run_date DESC``, same 3x over-fetch when the caller will
    post-filter to QC rows. The QC filtering itself stays in ``stan.db``
    so both backends share one copy of it.

    ``since`` (datetime or ISO string) bounds ``run_date`` in SQL; None, the
    default, is unbounded as before. Unlike ``sample_health``,
    ``runs.run_date`` is ``timestamp with time zone``, so this is an exact
    instant comparison rather than a date-prefix one. Rows with no
    ``run_date`` are kept, for the same reason as there.
    """
    fetch = limit * 3 if qc_only else limit
    where, args = [], []
    if instrument:
        where.append("instrument = %s")
        args.append(instrument)
    if not include_hidden:
        where.append("(hidden IS NULL OR hidden = 0)")
    if since is not None:
        where.append("(run_date >= %s OR run_date IS NULL)")
        args.append(since)
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


def run_ids_with_tic_pg(run_ids: list[str]) -> set[str]:
    """Ids among ``run_ids`` whose row carries a TIC trace (ids only, no arrays)."""
    if not run_ids:
        return set()
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "SELECT id::text FROM runs WHERE id::text = ANY(%s) "
            "AND tic_rt_bins IS NOT NULL AND tic_rt_bins::text NOT IN ('', '[]', 'null')",
            (list(run_ids),),
        )
        return {r[0] for r in cur.fetchall()}


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


# ── PEG Watch readers (v1.2.0) ──────────────────────────────────────────
# Back the dashboard's PEG tab and the `stan peg-sync` share client. PG Farm
# bills every byte read out of it (see stan/sync/pg_to_sqlite.py), so each of
# these names its columns -- never SELECT *, never the inline TIC arrays --
# and anything the caller only needs a summary of is aggregated in SQL.
#
# Type notes, because SQLite reproduces none of them:
#   * runs.run_date is timestamptz. It is formatted with to_char(... AT TIME
#     ZONE 'UTC'), never substr(), which does not exist for timestamptz.
#   * runs.hidden is an integer, not a boolean: compared to 0.
#   * peg_score / peg_intensity_pct are float4. Read through ::numeric so
#     16.1 arrives as 16.1, not 16.100000381469727; the cast keeps FLT_DIG
#     significant digits, which is all a float4 ever held.
#
# These return rows *with* run_name where a filter needs it; stan.db applies
# the blank/wash exclusion in Python (one regex, shared with submit-all) and
# strips the name before anything leaves the process. Queries that aggregate
# here use PG_BLANK_WASH_REGEX, the same alternation.
#
# Every reader goes through _peg_canonical_sql: one row per acquisition. PG
# holds the same raw file up to five times (1,674 timsTOF rows for 1,404
# acquisitions on 2026-09-28), with PEG readings that disagree; counting every
# copy moved the episode, column-period and impact numbers, and an unordered
# tie decided which copy the share client sent.

def _peg_real_qc_where(alias: str = "") -> str:
    """The real-PEG + QC filter (spec 4.1) as a SQL fragment.

    ``'unknown'`` is excluded by the class list: it is the pipeline's failure
    sentinel, stored with peg_score 0.0, and must never count as clean. The
    2015 floor drops the bogus 1980-01-02 Lumos row. NaN is excluded because
    float4 can hold it, PG sorts it above every number (so it would drag a
    percentile_cont upward), and it is not a measurement. The last clause
    drops failed acquisitions -- 0 precursors, 0 PEG ions, exactly 0 % -- which
    detect_peg_in_spectra classifies 'clean' when it summed no MS1 at all
    (``stan.metrics.peg_trends.is_failed_acquisition``).
    """
    p = f"{alias}." if alias else ""
    return (
        f"COALESCE({p}hidden, 0) = 0 "
        f"AND {p}run_date > TIMESTAMPTZ '2015-01-01 00:00:00+00' "
        f"AND {p}peg_score IS NOT NULL AND {p}peg_intensity_pct IS NOT NULL "
        f"AND {p}peg_score <> 'NaN' AND {p}peg_intensity_pct <> 'NaN' "
        f"AND COALESCE({p}peg_class, '') IN ('clean', 'trace', 'moderate', 'heavy') "
        f"AND NOT (COALESCE({p}n_precursors, -1) = 0 "
        f"AND COALESCE({p}peg_n_ions_detected, -1) = 0 AND {p}peg_intensity_pct = 0)"
    )


# Whether a run's PEG ion ladder was stored. peg_ion_hits is indexed on run_id.
_PEG_HAS_HITS = (
    "EXISTS (SELECT 1 FROM peg_ion_hits x WHERE x.run_id = r.id AND x.source = 'runs')"
)
# stan_version as a numeric array: the leading dotted number, so 1.0.10 beats
# 1.0.9 and 1.0.44 beats 0.2.376 (text order gets both wrong). NULL when there
# is none. Same regex as peg_trends.version_key and peg_submit's ranking.
_PEG_VERSION_KEY = (
    r"string_to_array(substring(r.stan_version FROM '^\s*v?([0-9]+(?:\.[0-9]+)*)'),"
    " '.')::numeric[]"
)


# Python's str.strip(), as an ARE. The patterns in these expressions use only
# what PG's AREs and Python's re read alike -- anchors, \s, and a bracket of
# "/" and "\" written [/\\] (in an ARE a backslash stays special inside [],
# so \\ is one literal backslash, as in Python) -- which is what lets
# tests/test_peg_overview_endpoint.py run them against run_basename. Needs
# standard_conforming_strings (PG's default since 9.1) so the literals reach
# the regex engine unescaped.
_PEG_STRIP_SQL = r"regexp_replace(COALESCE({col}, ''), '^\s+|\s+$', '', 'g')"
# peg_trends.acquisition_key in SQL: the trimmed instrument, run_name's
# basename (trim, drop trailing separators, drop everything up to the last
# / or \), and the UTC second -- the fields run_key hashes, normalised the
# way the share client normalises them. Keyed on the raw run_name, a file
# ingested under a PC path and a Hive path was two runs on the tab and one
# record on the board.
_PEG_ACQUISITION_KEY = (
    _PEG_STRIP_SQL.format(col="r.instrument") + ", "
    + r"regexp_replace(regexp_replace("
    + _PEG_STRIP_SQL.format(col="r.run_name")
    + r", '[/\\]+$', ''), '^.*[/\\]', ''), "
    + "date_trunc('second', r.run_date)"
)


def _peg_canonical_sql(cols: str, extra_where: str = "") -> str:
    """Real-PEG QC runs, one row per acquisition, as a subquery (alias ``r``).

    An acquisition is ``_PEG_ACQUISITION_KEY`` -- (instrument, basename of
    run_name, run_date to the second), the fields the share client's run_key
    hashes. Of its copies, the one kept has its ion ladder stored, then the
    newest ``stan_version``, then the highest ``id`` (bytewise, ``COLLATE
    "C"``, to match Python's order): exactly
    ``stan.metrics.peg_trends.pick_canonical``, which the SQLite path runs in
    Python, so the mirror and PG keep the same copy.

    Args:
        cols: Select list over ``runs r``; may use ``_PEG_HAS_HITS``.
        extra_where: More ``AND`` clauses on ``r`` (instrument, blank regex).
    """
    return (
        f"SELECT DISTINCT ON ({_PEG_ACQUISITION_KEY}) "
        f"{cols} FROM runs r WHERE {_peg_real_qc_where('r')}{extra_where} "
        f"ORDER BY {_PEG_ACQUISITION_KEY}, "
        f"{_PEG_HAS_HITS} DESC, {_PEG_VERSION_KEY} DESC NULLS LAST, "
        'r.id::text COLLATE "C" DESC'
    )


# The UTC run timestamp in the shared-record format; identical to
# stan.metrics.peg_trends.utc_iso, so run keys do not depend on the backend.
_PEG_RUN_DATE_UTC = (
    "to_char(run_date AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')"
)


def get_peg_runs_pg(instrument: str | None = None) -> list[dict]:
    """Real-PEG QC ``runs`` rows for the PEG tab, oldest first.

    Seven scalars a run plus the identifiers the filter needs, one row per
    acquisition, and ``has_hits`` for the ladder's denominator. ``run_name``
    is returned only so ``stan.db.get_peg_runs`` can drop blanks with the
    submit-all regex; it strips the name before returning.
    """
    inner = _peg_canonical_sql(
        "r.run_date, r.run_name, r.instrument, r.spd, r.peg_score, "
        "r.peg_intensity_pct, r.peg_n_ions_detected, r.peg_class, r.n_precursors, "
        f"r.mode, r.lc_system, {_PEG_HAS_HITS} AS has_hits",
        " AND r.instrument = %s" if instrument else "",
    )
    sql = (
        f"SELECT {_PEG_RUN_DATE_UTC} AS run_date_utc, run_name, instrument, spd, "
        "peg_score::numeric AS peg_score, "
        "peg_intensity_pct::numeric AS peg_intensity_pct, "
        "peg_n_ions_detected, peg_class, n_precursors, mode, lc_system, has_hits "
        f"FROM ({inner}) c ORDER BY run_date ASC, instrument, run_name"
    )
    args: list = [instrument] if instrument else []
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(sql, tuple(args))
        return _rows(cur)


def get_peg_instrument_counts_pg(blank_regex: str) -> list[tuple[str, str, int]]:
    """``(instrument, lc_system, n)`` over real-PEG QC runs, blanks excluded.

    A handful of rows: which instruments have PEG at all, and on which LC.
    """
    inner = _peg_canonical_sql("r.instrument, r.lc_system",
                               " AND COALESCE(r.run_name, '') !~* %s")
    sql = (
        "SELECT instrument, COALESCE(lc_system, '') AS lc_system, count(*) "
        f"FROM ({inner}) c GROUP BY 1, 2"
    )
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(sql, (blank_regex,))
        return [(r[0], r[1], int(r[2])) for r in cur.fetchall()]


def get_peg_ladder_month_counts_pg(
    instrument: str, blank_regex: str,
) -> list[tuple[str, int, str, int]]:
    """Runs per (UTC month, oligomer, adduct) in which that PEG ion was seen.

    Aggregated here so ~8k hit rows cross the wire as a few hundred counts.
    ``count(DISTINCT run_id)`` because the numerator is runs, not hits. The
    runs are the ones ``get_peg_runs_pg`` returns -- same filter, same copy
    of each acquisition -- so the numerator never counts a run the
    denominator does not.
    """
    inner = _peg_canonical_sql(
        "r.id, r.run_date",
        " AND r.instrument = %s AND COALESCE(r.run_name, '') !~* %s",
    )
    sql = (
        "SELECT to_char(c.run_date AT TIME ZONE 'UTC', 'YYYY-MM') AS month, "
        "h.repeat_n, h.adduct, count(DISTINCT h.run_id) AS n_runs "
        f"FROM peg_ion_hits h JOIN ({inner}) c ON c.id = h.run_id "
        "WHERE h.source = 'runs' "
        "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"
    )
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(sql, (instrument, blank_regex))
        return [(r[0], int(r[1]), r[2], int(r[3])) for r in cur.fetchall()]


def get_column_change_events_pg(instrument: str) -> list[tuple[str, str | None]]:
    """``(event_date, column_model)`` for an instrument's column changes.

    Notes and operator are left behind on purpose: they are free text that
    can name people and customers, and the PEG tab is a public read.
    ``event_date::text`` keeps this correct whichever type the column is.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "SELECT event_date::text, column_model FROM maintenance_events "
            "WHERE instrument = %s AND event_type = 'column_change' "
            "ORDER BY event_date ASC",
            (instrument,),
        )
        return [(r[0], r[1]) for r in cur.fetchall()]


def get_peg_share_rows_pg() -> list[dict]:
    """Every real-PEG QC run, any LC, for the PEG share client.

    ``run_name`` is included because the client hashes it into ``run_key``;
    it is never sent. ``sample_type`` is selected only when PG has the
    column (it does not today); the client falls back to the filename.

    One row per acquisition, in a total order: with ``ORDER BY run_date``
    alone, copies of one raw file tied and came back in whatever order the
    sort left them, so which PEG reading the client shared could change
    after any UPDATE -- and the relay would commit the flip.

    ``id``, ``stan_version`` and ``has_hits`` -- what the copy was chosen
    by -- come along so the client's own tie-break ranks by the same keys
    (``peg_trends.canonical_rank``). The client never sends them.
    """
    with _connect() as pg, pg.cursor() as cur:
        has_st = "sample_type" in _runs_columns(cur)
        inner = _peg_canonical_sql(
            "r.run_date, r.run_name, r.instrument, r.spd, r.mode, r.amount_ng, "
            "r.lc_system, r.peg_score, r.peg_intensity_pct, r.peg_n_ions_detected, "
            f"r.peg_class, r.id, r.stan_version, {_PEG_HAS_HITS} AS has_hits"
            + (", r.sample_type" if has_st else "")
        )
        cur.execute(
            f"SELECT run_name, instrument, {_PEG_RUN_DATE_UTC} AS run_date_utc, "
            "spd, mode, amount_ng::numeric AS amount_ng, lc_system, "
            "peg_score::numeric AS peg_score, "
            "peg_intensity_pct::numeric AS peg_intensity_pct, "
            "peg_n_ions_detected, peg_class, id, stan_version, has_hits"
            + (", sample_type" if has_st else "")
            + f" FROM ({inner}) c ORDER BY run_date ASC, instrument, run_name"
        )
        return _rows(cur)


def get_peg_lab_lc_summary_pg(as_of, blank_regex: str, weeks: int = 26) -> list[dict]:
    """Per-instrument PEG share over 90/365 days plus weekly medians.

    Everything is aggregated here with ``percentile_cont`` -- the same
    median ``statistics.median`` computes on the SQLite path -- so the
    payload is a few numbers per instrument however many runs there are.
    The UTC timestamp ``t`` is a plain ``timestamp`` (``AT TIME ZONE 'UTC'``
    of a timestamptz), compared against naive UTC datetimes. Weeks are the
    trailing 7-day buckets of ``peg_trends.week_window``, ``floor((t -
    wstart) / 7 days)`` -- the relay's lc-compare buckets, not calendar weeks.
    """
    import datetime as _dt

    from stan.metrics.peg_trends import finalize_lab_lc_row, sort_lab_lc, week_window

    def _midnight(d):
        return _dt.datetime(d.year, d.month, d.day)

    wstart, _wend = week_window(as_of, weeks)
    params = {
        "blank": blank_regex,
        "end": _midnight(as_of + _dt.timedelta(days=1)),
        "s90": _midnight(as_of - _dt.timedelta(days=89)),
        "s365": _midnight(as_of - _dt.timedelta(days=364)),
        "wstart": wstart.replace(tzinfo=None),
    }
    inner = _peg_canonical_sql(
        "r.instrument, r.lc_system, r.peg_intensity_pct, r.peg_class, r.run_date",
        " AND COALESCE(r.run_name, '') !~* %(blank)s",
    )
    cte = (
        "WITH q AS ("
        " SELECT instrument, COALESCE(lc_system, '') AS lc_system,"
        "  peg_intensity_pct::float8 AS pct, peg_class,"
        "  (run_date AT TIME ZONE 'UTC') AS t"
        f" FROM ({inner}) c"
        "  WHERE (run_date AT TIME ZONE 'UTC') < %(end)s"
        ") "
    )
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            cte
            + "SELECT instrument,"
            " count(*) FILTER (WHERE t >= %(s90)s),"
            " percentile_cont(0.5) WITHIN GROUP (ORDER BY pct)"
            "   FILTER (WHERE t >= %(s90)s),"
            " count(*) FILTER (WHERE t >= %(s90)s AND peg_class = 'clean'),"
            " count(*) FILTER (WHERE t >= %(s365)s),"
            " percentile_cont(0.5) WITHIN GROUP (ORDER BY pct)"
            "   FILTER (WHERE t >= %(s365)s)"
            " FROM q GROUP BY instrument",
            params,
        )
        stats = cur.fetchall()
        cur.execute(cte + "SELECT instrument, lc_system, count(*) FROM q GROUP BY 1, 2",
                    params)
        lc_rows = cur.fetchall()
        cur.execute(
            cte
            + "SELECT instrument,"
            " floor(extract(epoch FROM (t - %(wstart)s)) / 604800)::int AS wk,"
            " percentile_cont(0.5) WITHIN GROUP (ORDER BY pct)"
            " FROM q WHERE t >= %(wstart)s GROUP BY 1, 2",
            params,
        )
        week_rows = cur.fetchall()

    lcs: dict[str, dict[str, int]] = {}
    for inst, lc, n in lc_rows:
        lcs.setdefault(inst, {})[lc or ""] = int(n)
    weekly: dict[str, dict] = {}
    for inst, wk, med in week_rows:
        weekly.setdefault(inst, {})[int(wk)] = None if med is None else float(med)
    out = [
        finalize_lab_lc_row(
            inst, lcs.get(inst, {}), n90, None if m90 is None else float(m90), c90,
            n365, None if m365 is None else float(m365), weekly.get(inst, {}),
            weeks,
        )
        for inst, n90, m90, c90, n365, m365 in stats
    ]
    return sort_lab_lc(out)


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


def mark_submitted_pg(run_id: str, submission_id: str | None) -> bool:
    """Flag a run as submitted to the community benchmark in PG.

    ``stan submit-all --backend pg`` pushes rows read from PG, so the
    "already submitted" bookkeeping has to land there too -- against
    SQLite it marks a row nothing will ever read again, and the next
    submit-all re-sends every run.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "UPDATE runs SET submitted_to_benchmark = 1, "
            "submission_id = COALESCE(%s, submission_id) WHERE id = %s",
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


# ── Bruker maintenance cache ────────────────────────────────────────────
# The Bruker timsTOF's Compass Server keeps its own PostgreSQL database of the
# instrument's acquisition history. A Hive-side extractor reads the newest
# Compass BACKUP (no live-DB access, no password) and produces one compact JSON
# of maintenance signals. On the hosted dashboard that JSON reaches us through
# PG Farm rather than a synced file: the extractor upserts it here, the
# maintenance endpoint reads it back. One row, replaced each run.

def _ensure_bruker_maintenance_table(cur) -> None:
    cur.execute(
        "CREATE TABLE IF NOT EXISTS bruker_maintenance ("
        " id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),"
        " updated_at timestamptz NOT NULL DEFAULT now(),"
        " doc jsonb NOT NULL)"
    )


def upsert_bruker_maintenance_pg(doc: dict) -> None:
    """Replace the single Bruker maintenance document. Called by the extractor."""
    with _connect() as pg, pg.cursor() as cur:
        _ensure_bruker_maintenance_table(cur)
        cur.execute(
            "INSERT INTO bruker_maintenance (id, updated_at, doc)"
            " VALUES (1, now(), %s)"
            " ON CONFLICT (id) DO UPDATE SET"
            " updated_at = excluded.updated_at, doc = excluded.doc",
            (json.dumps(doc),),
        )
        pg.commit()


# ── published-document cache ────────────────────────────────────────────
# `evosep_column_health` (864 KB as text) and `bruker_maintenance` (77 KB) are
# single-row documents that change about once a day. They were read in full
# every 20 min by the watchdog (cron_stan_alerts.sh), every 30 min by
# cron_evosep.sh, and on every page view of the hosted dashboard -- ~72 MB/day
# from the crons alone, measured 2026-09-22 -- and PG Farm runs on Google Cloud,
# which bills every byte a client reads out of it.
#
# So each read first asks for the row's `xmin`: the id of the transaction that
# wrote this row version. Measured on the wire 2026-09-22, a read of an
# unchanged document now costs 255 B (probe plus the connection's liveness
# check) instead of 865,909 B (Evosep) or 77,605 B (Bruker). Any UPDATE or INSERT, by any writer,
# makes a new row version with a new xmin, so an unchanged xmin means the SAME
# document PG holds -- not "probably the same". That is why it is keyed on
# xmin rather than `updated_at`, which only the writers that remember to set it
# will move. It matters because one of these readers is the watchdog: it has to
# see exactly what PG serves, never a stale or partial copy (CLAUDE.md, "A
# watchdog must not live inside what it watches").
#
# Two layers. In-process memory, which is what helps the long-lived dashboard.
# And an opt-in file per table under $STAN_PG_DOC_CACHE_DIR, which is what
# helps the crons -- each tick is a new process and starts with empty memory.
# A file that is missing, truncated, from another database or fails its
# checksum is a miss, never an error and never a wrong answer. Freezing or
# restoring can only CHANGE a row's visible xmin, which is a harmless miss.

#: Env var naming the directory for the optional per-table file cache.
DOC_CACHE_DIR_ENV = "STAN_PG_DOC_CACHE_DIR"

_DOC_TABLES = frozenset({"bruker_maintenance", "evosep_column_health"})
_DOC_CACHE_MAGIC = "stan-pg-doc-cache/1"

#: table -> (xmin, document as the JSON text PG returned). Text rather than
#: the parsed dict, so every caller gets its own fresh object: the dashboard
#: and the alerter share this process-wide, and a caller that mutates what it
#: was handed must not change what the next caller sees.
_DOC_CACHE: dict[str, tuple[str, str]] = {}


def _doc_cache_source() -> str:
    """Which database a cached document came from. xmin is per-cluster."""
    return f"{PG_DEFAULTS['host']}/{PG_DEFAULTS['database']}"


def _doc_cache_path(table: str) -> Path | None:
    d = os.environ.get(DOC_CACHE_DIR_ENV, "").strip()
    return Path(d) / f"{table}.pgdoc" if d else None


def _read_doc_cache_file(table: str) -> tuple[str, str] | None:
    """(xmin, doc_text) from the file cache, or None for any kind of miss.

    Format: one JSON header line (magic, source, table, xmin, sha256 of the
    body), then the document text exactly as PG returned it. The checksum is
    what makes "corrupt means miss" true rather than hopeful -- a file cut
    short on a network filesystem can still parse.
    """
    import hashlib

    path = _doc_cache_path(table)
    if path is None:
        return None
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    try:
        head, sep, body = raw.partition(b"\n")
        if not sep:
            return None
        meta = json.loads(head.decode("utf-8"))
        if (meta.get("magic") != _DOC_CACHE_MAGIC
                or meta.get("source") != _doc_cache_source()
                or meta.get("table") != table
                or not isinstance(meta.get("xmin"), str)
                or hashlib.sha256(body).hexdigest() != meta.get("sha256")):
            return None
        return meta["xmin"], body.decode("utf-8")
    except (ValueError, AttributeError, TypeError):
        logger.debug("unusable PG document cache %s; treating as a miss", path)
        return None


def _write_doc_cache_file(table: str, xmin: str, text: str) -> None:
    """Atomically replace the file cache for ``table``. Never raises.

    Temp file in the same directory, fsync, then ``os.replace``: a reader sees
    the old file or the new one, never half of either. ``mkstemp`` creates it
    0600, which is deliberate -- the Evosep document carries customer sample
    names (see ``_EVOSEP_IDENTIFYING_FIELDS`` in the dashboard) and the cache
    sits on a shared volume.
    """
    import hashlib
    import tempfile

    path = _doc_cache_path(table)
    if path is None:
        return
    body = text.encode("utf-8")
    header = json.dumps({
        "magic": _DOC_CACHE_MAGIC, "source": _doc_cache_source(),
        "table": table, "xmin": xmin,
        "sha256": hashlib.sha256(body).hexdigest(),
    }).encode("utf-8")
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                                   prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as f:
            f.write(header + b"\n" + body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        tmp = None
    except OSError as e:
        # A cache that cannot be written costs bytes, not correctness: the
        # next tick simply downloads the document again. Loud enough to be
        # found in the cron log, not loud enough to fail the tick.
        logger.warning("could not write PG document cache %s: %s", path, e)
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _get_published_doc_pg(table: str) -> dict | None:
    """The single ``id = 1`` document in ``table``, downloaded only if changed.

    Returns None when the table or the row does not exist -- read-only and
    DDL-free, the same contract the readers always had. The file cache is
    only touched while no transaction is open: a slow network filesystem
    must never hold a PG transaction open (the 2026-09-02
    idle-in-transaction incident).
    """
    if table not in _DOC_TABLES:
        raise ValueError(f"not a published-document table: {table!r}")

    with _connect() as pg, pg.cursor() as cur:
        try:
            cur.execute(f"SELECT xmin::text FROM {table} WHERE id = 1")  # noqa: S608 - whitelisted
        except Exception:  # noqa: BLE001 - undefined_table etc. -> not stored yet
            pg.rollback()
            return None
        row = cur.fetchone()
    if row is None:
        return None
    live_xmin = row[0]

    cached = _DOC_CACHE.get(table)
    if cached is None or cached[0] != live_xmin:
        # Memory missed or is stale; another process may have refreshed the
        # file since (every cron tick is a fresh process).
        cached = _read_doc_cache_file(table)
    if cached is not None and cached[0] == live_xmin:
        try:
            doc = json.loads(cached[1])
        except ValueError:
            # Cannot happen for text that came from PG or passed the
            # checksum; if it somehow does, download rather than trust it.
            pass
        else:
            _DOC_CACHE[table] = cached
            return doc

    # Changed (or never seen). Re-read xmin WITH the document, so the pair
    # that gets cached is one row version even if a writer landed between
    # the probe and this read.
    with _connect() as pg, pg.cursor() as cur:
        try:
            cur.execute(f"SELECT xmin::text, doc::text FROM {table} WHERE id = 1")  # noqa: S608
        except Exception:  # noqa: BLE001 - dropped between the two reads
            pg.rollback()
            return None
        row = cur.fetchone()
    if row is None:
        return None
    xmin, text = row
    doc = json.loads(text)
    _DOC_CACHE[table] = (xmin, text)
    _write_doc_cache_file(table, xmin, text)
    return doc


def get_bruker_maintenance_pg() -> dict | None:
    """The latest Bruker maintenance document, or None when none has been stored.

    Read-only and DDL-free: the hosted service account has no CREATE privilege,
    so a missing table is treated as "nothing stored yet" (the endpoint then
    falls back to the file cache) rather than an error.

    Costs ~255 bytes when the document has not changed since this process (or,
    with ``$STAN_PG_DOC_CACHE_DIR``, any process) last read it -- see
    ``_get_published_doc_pg``.
    """
    return _get_published_doc_pg("bruker_maintenance")


def get_evosep_column_health_pg() -> dict | None:
    """The latest Evosep column-health document, or None when none is stored.

    Same contract as get_bruker_maintenance_pg: read-only and DDL-free. The
    table is created by migration as its owner; the hosted service account has
    DML only, so a missing table means "the publisher has not run yet" and the
    endpoint falls back to the file cache rather than erroring.

    Cached on the row's xmin like get_bruker_maintenance_pg, which matters more
    here: the document is ~864 KB as text.
    """
    return _get_published_doc_pg("evosep_column_health")


# ── dispatch attempts ───────────────────────────────────────────────────
# Moved off the Quobyte SQLite file in v1.0.54. The dispatcher UPSERTs up to
# 50-60 of these every 5 minutes while SLURM jobs write their own outcomes
# concurrently; SQLite's locking assumes a POSIX filesystem that Quobyte does
# not faithfully provide, and the index corruption always landed here. See
# migrations/2026-09-01_dispatch_attempts_pg.sql.

def record_dispatch_attempt_pg(
    raw_path: str,
    status: str,
    error: str | None = None,
    error_type: str | None = None,
    last_run_id: str | None = None,
    host_origin: str | None = None,
) -> None:
    """Record a dispatch outcome. Idempotent on raw_path, bumps attempt_count.

    Mirrors ``stan.db.record_dispatch_attempt``: a re-attempt UPDATEs the row
    rather than appending, and ``last_run_id`` is only overwritten by a newer
    non-NULL value so an 'ok' run id survives a later 'failed'.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(
            "INSERT INTO dispatch_attempts"
            " (raw_path, attempted_at, status, error, error_type,"
            "  attempt_count, last_run_id, host_origin)"
            " VALUES (%s, now(), %s, %s, %s, 1, %s, %s)"
            " ON CONFLICT (raw_path) DO UPDATE SET"
            "   attempted_at  = now(),"
            "   status        = excluded.status,"
            "   error         = excluded.error,"
            "   error_type    = excluded.error_type,"
            "   attempt_count = dispatch_attempts.attempt_count + 1,"
            "   last_run_id   = COALESCE(excluded.last_run_id,"
            "                            dispatch_attempts.last_run_id)",
            (raw_path, status, error, error_type, last_run_id, host_origin),
        )
        pg.commit()


def capped_raws_pg(
    max_attempts: int, raw_paths: list[str] | None = None,
) -> set[str]:
    """Raws that have failed at least ``max_attempts`` times.

    The dispatcher's dedup predicate, identical to the SQLite version it
    replaces. A missing table means nothing recorded yet -> empty set, so a
    fresh deployment does not fail closed and skip every file.

    ``raw_paths`` scopes the answer to those paths (``raw_path = ANY``), so
    the dispatcher is told only about the raws it is deciding on rather than
    every capped raw ever recorded. None keeps the unscoped query; an empty
    list is answered without a round trip.
    """
    sql = ("SELECT raw_path FROM dispatch_attempts"
           " WHERE status = 'failed' AND attempt_count >= %s")
    args: tuple = (max_attempts,)
    if raw_paths is not None:
        paths = list(dict.fromkeys(str(p) for p in raw_paths))
        if not paths:
            return set()
        sql += " AND raw_path = ANY(%s)"
        args = (max_attempts, paths)
    with _connect() as pg, pg.cursor() as cur:
        try:
            cur.execute(sql, args)
        except Exception:  # noqa: BLE001 - undefined_table etc.
            pg.rollback()
            return set()
        return {r[0] for r in cur.fetchall()}


#: Tables ``unknown_raw_paths_pg`` may be asked about. The name is formatted
#: into the SQL, so it is checked against this rather than trusted.
_RAW_PATH_TABLES = frozenset({"runs", "sample_health"})


def unknown_raw_paths_pg(table: str, raw_paths: list[str]) -> set[str]:
    """The subset of ``raw_paths`` that ``table`` has no row for.

    The dispatcher's dedup used to download every ``raw_path`` in ``runs``
    and ``sample_health`` -- 8,496 rows, 914,749 B on the wire, every 5
    minutes -- to learn that none of the ~4,138 walked raws was new (~263
    MB/day of billed PG Farm egress). Sending the candidates up instead costs
    ingress, which is free, and what comes back is only the raws PG has never
    seen: 47 paths on 2026-09-22, the whole preload 9,911 B on the wire.

    Raises on any failure. The caller must treat an exception as "don't
    know", NOT as "none of them are known" -- the latter would resubmit every
    walked raw to SLURM.

    Args:
        table: ``runs`` or ``sample_health``.
        raw_paths: Candidate paths, in exactly the form stored in
            ``raw_path`` (the dispatcher resolves symlinks to the real
            ``/nfs/...`` path first, which is what ingest recorded).
    """
    if table not in _RAW_PATH_TABLES:
        raise ValueError(f"not a raw_path table: {table!r}")
    paths = list(dict.fromkeys(str(p) for p in raw_paths))
    if not paths:
        return set()
    with _connect() as pg, pg.cursor() as cur:
        # A hash anti-join: one pass over the table server-side (0.25 s for
        # 1,658 candidates against 4,642 runs, measured), no index needed.
        cur.execute(
            f"SELECT c FROM unnest(%s::text[]) AS c "  # noqa: S608 - whitelisted
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE t.raw_path = c)",
            (paths,),
        )
        rows = cur.fetchall()
    asked = set(paths)
    return {r[0] for r in rows if r[0] in asked}
