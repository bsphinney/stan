"""Materialise central PG Farm data into the local SQLite the dashboard reads.

`stan dashboard` is a SQLite reader, but the fleet's canonical store is PG
Farm. Without this the local DB holds whatever it was last seeded with, so
the UI serves stale or empty data even though PG is current.

What moves:

* ``runs`` -- a straight column copy (every local column exists in PG), and
  with it ``tic_traces``: PG keeps the TIC inline on the run row as the JSONB
  columns ``tic_rt_bins`` / ``tic_intensity``, whereas SQLite keeps it in a
  side table. Without this projection the dashboard's TIC modal reports "No
  TIC data for this run" for runs whose TIC is sitting right there in PG.
* the detail tables in ``_MIRRORED`` -- straight copies, skipped silently if
  the PG side hasn't been migrated yet.
* ``feature_clouds`` -- the same, but ~150 KB a row, so a cold mirror drains
  it newest-first, ``STAN_PG_CLOUD_MAX_PULL`` clouds (default 50) per tick.

EGRESS IS BILLED, SO ONLY CHANGES MAY CROSS THE WIRE
====================================================
PG Farm runs on Google Cloud: ingress is free, every byte read out of it is
billed. Until v1.1.8 every refresh tick copied every table above in full --
73 MB, every ~5.5 minutes, from an always-on Azure instance: ~19 GB/day for
data that almost never changes (``drift_peak_clouds`` alone was 40 MB a tick
for 81 rows). The Library flagged it on 2026-09-22 as a large part of a
~$150/month egress bill.

The sync is therefore fingerprinted on ``xmin``, the id of the transaction that
wrote each row version. Every INSERT or UPDATE stamps the new row version with
a fresh xid and a DELETE removes the version, so the sorted list of xmins over
a set of rows changes whenever any row in it does. Per table, per tick:

1. One aggregate: ``count(*)`` plus an md5 over every xmin. If that matches
   the last tick, the table is done. This is the steady state, and it costs
   about fifty bytes.
2. Otherwise, the same fingerprint per *sync key* (a run, a sample, ...): a
   list of keys and hashes, a few hundred KB for ``runs`` at worst.
3. Fetch only the keys whose fingerprint moved and replace them locally.
   Delete keys that vanished from PG -- but only keys this mirror brought in
   (``pg_mirror_keys``). A row a local watcher wrote is never the mirror's to
   delete.

Fingerprints are recorded from the read that PRECEDED the fetch, so a row that
changes mid-pull shows up as changed on the next tick instead of being missed:
the mirror is eventually consistent, never silently stale. A pull that dies
half way resumes from the keys it had already landed.

xmin cannot see one kind of change: a change in *what* is copied. When the
local schema gains a column PG already holds data in -- the normal rollout
order, PG migrated and backfilled days before a surface upgrades -- no PG row
changes. Each table therefore also records its *shape* (the copied columns
and the sync key); a different shape marks every key it owns stale, so
the table is re-fetched once, with ownership carried across.

What a tick costs (measured against live PG, 2026-09-22): ~3 KB when nothing
changed; one table's key list when something did (~360 KB for ``runs``), so
~1 MB for a tick in which a new QC run touched several tables. Still two
orders of magnitude under the old 73 MB, every tick.

Known limits, both inherited rather than introduced:

* Rows mirrored by the pre-v1.1.8 full copy carry no ``pg_mirror_keys``
  entry, so if PG has since deleted them they stay. The old code never
  deleted anything at all.
* Local edits to mirrored rows now persist until PG next touches the row
  (they used to be overwritten within one tick). The mirror is a read cache;
  nothing should be editing it.

Do not add a table here as a plain ``SELECT *`` copy "because it is small".
That is how 73 MB a tick happened: each table was small when it was added.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


# table -> (sync key columns, keys per fetch). The sync key is the unit that is
# compared and replaced as a whole: one run's PEG hits or drift centroids move
# together. The fetch size keeps a single round trip to a few MB --
# drift_peak_clouds rows are ~500 KB of JSON each.
_MIRRORED: dict[str, tuple[tuple[str, ...], int]] = {
    "runs": (("id",), 500),
    "peg_ion_hits": (("run_id", "source"), 200),
    "drift_window_centroids": (("run_id", "source"), 200),
    "drift_peak_clouds": (("run_id", "source"), 5),
    # Sample Health moved to PG in v1.0.14. The dashboard still reads
    # SQLite, so mirror it down like everything else. host_origin exists
    # only on the PG side; the column intersection drops it.
    "sample_health": (("id",), 500),
    "health_tic_traces": (("health_id",), 100),
    # The maintenance log: what was physically done to an instrument. Mirrored
    # so a SQLite-reading dashboard still shows it.
    "maintenance_events": (("id",), 500),
    # cIRT anchor RTs behind the Trends "cIRT anchor RT drift" panel. Written
    # centrally by `stan backfill-cirt` on Hive; mirrored so SQLite-reading
    # dashboards get the chart too.
    "irt_anchor_rts": (("run_id",), 200),
    # 4DFF ion clouds, ~150 KB of JSON a row: drained newest-first under a
    # per-tick cap (see ``_pull``), not in one gulp.
    "feature_clouds": (("run_id", "source"), 25),
}
_DETAIL_TABLES = tuple(t for t in _MIRRORED if t not in ("runs", "feature_clouds"))

# md5 over the sorted xmins of a set of rows -- see the module docstring. Not
# a sum: two row versions from one transaction can straddle an older one, and
# a sum of xids can then come out unchanged when the rows did not.
_XMIN_FP = (
    "md5(coalesce(string_agg(xmin::text, ',' ORDER BY xmin::text::bigint), ''))"
)


def pull_from_pg(db_path: Path | None = None, since: str = "") -> dict:
    """Bring the local SQLite mirror in step with PG, moving only what changed.

    ``since`` narrows the ``runs`` comparison to ``run_date >= since``. A
    windowed pull never deletes anything outside its window and never marks
    the table as fully in sync.

    Returns a dict of table -> rows fetched from PG this call (0 when nothing
    changed). Raises on connection or ``runs`` query failure so callers can
    decide whether that is fatal.
    """
    from stan.db import connect, get_db_path, init_db
    from stan.db_pg import _connect

    if db_path is None:
        db_path = get_db_path()
    init_db(db_path)

    pg = _connect()
    local = connect(db_path)
    try:
        # ``with pg`` is load-bearing, not decoration. ``_connect()`` returns a
        # module-level cached connection and psycopg2's context-manager exit is
        # the ONLY thing that ends a transaction on it — it commits/rolls back
        # but does not close. Until 2026-09-02 this function ran its statements
        # bare, so the read transaction opened by the first SELECT stayed
        # open for the life of the dashboard process, growing with every
        # five-minute refresh tick. It held AccessShareLock on every table
        # touched here, ``maintenance_events`` among them; an ``ALTER TABLE``
        # then queued an AccessExclusiveLock behind it, and a *queued* exclusive
        # lock blocks new readers too — so the migration hung for 25 minutes and
        # took the maintenance calendar down with it. The open snapshot also
        # pinned VACUUM's cleanup horizon the whole time.
        with pg, pg.cursor() as cur:
            return _pull(local, _PgSource(cur), since=since)
    finally:
        local.close()


def _pull(local, src, since: str = "") -> dict:
    """The whole sync, against any source with ``_PgSource``'s interface."""
    _ensure_state(local)
    written: dict[str, int] = {}

    tic = _TicProjection()
    written["runs"] = _sync_table(src, local, "runs", since=since, companion=tic) or 0
    written["tic_traces"] = tic.written

    for t in _DETAIL_TABLES:
        try:
            n = _sync_table(src, local, t)
        except Exception as e:  # noqa: BLE001 - one bad table must not stall the rest
            # Warning, not debug: a detail table that stops syncing looks
            # exactly like "no data for this run" in the UI.
            logger.warning("mirror: %s skipped this tick: %s", t, e)
            _rollback(src)
            continue
        if n is not None:
            written[t] = n

    written["feature_clouds"] = _pull_feature_clouds(src, local)
    return written


def _pull_feature_clouds(src, local) -> int:
    """Ion clouds: the same fingerprinted sync, drained newest-first under a cap.

    A first sync against a fully backfilled fleet is ~90 MB; pulling it in one
    tick would stall the refresh loop for minutes and hand the user a
    dashboard that looks hung. So each tick takes the ``STAN_PG_CLOUD_MAX_PULL``
    newest changed clouds (0 or less = no cap), and the table is only marked
    in sync once a tick drains it completely.
    """
    if (os.environ.get("STAN_PG_CLOUD_FULL_REFRESH") or "").strip():
        # Until v1.1.8 this re-downloaded the newest 50 clouds on EVERY tick
        # while set -- ~2 GB/day as an Azure app setting. Fingerprints now
        # catch a re-backfilled cloud on their own.
        logger.warning(
            "STAN_PG_CLOUD_FULL_REFRESH is ignored since v1.1.8: re-backfilled "
            "clouds are detected automatically. Unset it."
        )
    try:
        cap = int(os.environ.get("STAN_PG_CLOUD_MAX_PULL", "50"))
    except ValueError:
        cap = 50
    try:
        return _sync_table(src, local, "feature_clouds", cap=cap, newest_first=True) or 0
    except Exception as e:  # noqa: BLE001 - a broken sync must not stall the rest
        # Warning, not debug: the "no ion cloud for this run" symptom is
        # indistinguishable from "not backfilled yet", so a silent failure
        # here is a bug that hides itself.
        logger.warning("feature_clouds sync failed: %s", e)
        # Swallowing the error must not also swallow the transaction: leave
        # the shared connection clean for the next caller rather than aborted.
        _rollback(src)
        return 0


def _release(cur) -> None:
    """End the PG read transaction before the slow local write that follows.

    Every SELECT here opens a transaction that holds AccessShareLock on the
    table until something commits, and the slow half of each phase is the
    SQLite side — tens of megabytes of feature-cloud JSON on a first sync.
    There is no reason to hold PG locks (or pin VACUUM's horizon) through it.
    The mirror is eventually consistent by design, so a per-phase snapshot is
    exactly as correct as one snapshot for the whole pull.
    """
    cur.connection.commit()


def _rollback(src) -> None:
    """Leave the shared connection clean after a swallowed error."""
    try:
        rb = getattr(src, "rollback", None)
        if rb is not None:
            rb()
    except Exception:  # noqa: BLE001 - connection already gone
        pass


class _PgSource:
    """Every query the mirror sends to PG Farm, and nothing else.

    Kept in one place because each one is billed by the byte: anything new
    here should return keys and hashes, or rows already known to be wanted.
    Every method ends its read transaction before returning (``_release``),
    so no PG lock is held through the SQLite write that follows it.
    """

    def __init__(self, cur):
        self.cur = cur

    def _all(self, sql: str, params: tuple | None = None) -> list:
        self.cur.execute(sql, params)
        rows = self.cur.fetchall()
        _release(self.cur)
        return rows

    def rollback(self) -> None:
        self.cur.connection.rollback()

    def columns(self, table: str) -> list[str]:
        return [r[0] for r in self._all(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s", (table,),
        )]

    def table_fp(self, table: str) -> str:
        n, h = self._all(f"SELECT count(*), {_XMIN_FP} FROM {table}")[0]
        return f"{n}:{h}"

    def key_fps(
        self, table: str, key_cols: tuple[str, ...], since: str = "",
        newest_first: bool = False,
    ) -> list:
        keys = _quote(key_cols)
        sql = f"SELECT {keys}, {_XMIN_FP} AS fp FROM {table}"
        params = None
        if since:
            sql += " WHERE run_date >= %s"
            params = (since,)
        sql += f" GROUP BY {keys}"
        n = len(key_cols)
        if newest_first:
            # Newest first by the RUN's acquisition date: a dashboard catching
            # up on a fresh backfill should light up the runs someone is
            # actually looking at, not 2024's. (A cloud's own created_at is
            # useless here -- a bulk backfill stamps every row in one minute.)
            outer = ", ".join(f'k."{c}"' for c in key_cols)
            try:
                rows = self._all(
                    f"SELECT {outer}, k.fp FROM ({sql}) k "
                    "LEFT JOIN (SELECT id, max(run_date) AS d FROM runs GROUP BY id) r "
                    "ON r.id = k.run_id ORDER BY r.d DESC NULLS LAST",
                    params,
                )
                return [(tuple(r[:n]), r[n]) for r in rows]
            except Exception:  # noqa: BLE001 - ordering is a nicety; fall back unordered
                self.rollback()
        return [(tuple(r[:n]), r[n]) for r in self._all(sql, params)]

    def fetch(self, table: str, cols: list[str], key_cols: tuple[str, ...], keys: list) -> list:
        if len(key_cols) == 1:
            where, vals = _quote(key_cols), tuple(k[0] for k in keys)
        else:
            where, vals = f"({_quote(key_cols)})", tuple(tuple(k) for k in keys)
        return self._all(
            f"SELECT {_quote(cols)} FROM {table} WHERE {where} IN %s", (vals,),
        )

    def fetch_tic(self, ids: list) -> list:
        return self._all(
            "SELECT id, tic_rt_bins, tic_intensity FROM runs "
            "WHERE id IN %s AND tic_rt_bins IS NOT NULL AND tic_intensity IS NOT NULL",
            (tuple(ids),),
        )



def _quote(cols) -> str:
    return ", ".join('"' + c + '"' for c in cols)


def _enc(key) -> str:
    """Stable text form of a key tuple, for the local bookkeeping tables."""
    return json.dumps(list(key), default=str, separators=(",", ":"))


def _ensure_state(local) -> None:
    """The mirror's own bookkeeping: what it brought in, at which fingerprint.

    It is a cache of what PG looked like, nothing more. If it is ever in an
    unexpected form, throwing it away costs one full re-sync and loses nothing.
    """
    cols = {r[1] for r in local.execute("PRAGMA table_info(pg_mirror_tables)")}
    if cols and "shape" not in cols:
        local.executescript(
            "DROP TABLE IF EXISTS pg_mirror_tables; DROP TABLE IF EXISTS pg_mirror_keys;"
        )
    local.executescript(
        """
        CREATE TABLE IF NOT EXISTS pg_mirror_keys (
            tbl TEXT NOT NULL,
            k   TEXT NOT NULL,
            fp  TEXT NOT NULL,
            PRIMARY KEY (tbl, k)
        );
        CREATE TABLE IF NOT EXISTS pg_mirror_tables (
            tbl       TEXT PRIMARY KEY,
            shape     TEXT NOT NULL,
            fp        TEXT,
            synced_at TEXT
        );
        """
    )


def _delete_keys(local, table: str, key_cols: tuple[str, ...], keys: list) -> None:
    where = " AND ".join(f'"{c}" = ?' for c in key_cols)
    local.executemany(f"DELETE FROM {table} WHERE {where}", [tuple(k) for k in keys])


def _table_state(local, table: str, shape: str, key_cols: tuple[str, ...]) -> str | None:
    """Return the stored table fingerprint, restarting the table on a new shape.

    A different set of copied columns (or a different sync key) means every
    fingerprint on record was taken of a different copy, so none can be
    trusted: every owned key is marked stale and this tick re-fetches the
    table. Ownership itself is carried over, re-expressed in the new key's
    terms -- dropping it would strand rows PG has since deleted, and keeping
    old-arity keys would break the "gone" path on every tick after.
    """
    row = local.execute(
        "SELECT shape, fp FROM pg_mirror_tables WHERE tbl = ?", (table,)
    ).fetchone()
    if row is not None and row[0] == shape:
        return row[1]

    owned: set[str] = set()
    if row is not None:
        old = [
            tuple(json.loads(k)) for (k,) in
            local.execute("SELECT k FROM pg_mirror_keys WHERE tbl = ?", (table,))
        ]
        try:
            old_cols = tuple(json.loads(row[0])["key"])
        except (ValueError, KeyError, TypeError):
            old_cols = ()
        if old_cols == tuple(key_cols):
            owned = {_enc(k) for k in old}
        elif old_cols:
            where = " AND ".join(f'"{c}" = ?' for c in old_cols)
            try:
                for k in old:
                    if len(k) == len(old_cols):
                        owned.update(_enc(r) for r in local.execute(
                            f"SELECT DISTINCT {_quote(key_cols)} FROM {table} WHERE {where}", k,
                        ))
            except sqlite3.OperationalError:  # an old key column no longer exists locally
                owned = set()
    with local:
        local.execute("DELETE FROM pg_mirror_keys WHERE tbl = ?", (table,))
        # '' never equals an md5, so every owned key is re-fetched (or deleted
        # if PG no longer has it) on this tick.
        local.executemany(
            "INSERT INTO pg_mirror_keys (tbl, k, fp) VALUES (?, ?, '')",
            [(table, k) for k in sorted(owned)],
        )
        local.execute(
            "INSERT OR REPLACE INTO pg_mirror_tables (tbl, shape, fp, synced_at) "
            "VALUES (?, ?, NULL, NULL)",
            (table, shape),
        )
    return None


def _sync_table(
    src, local, table: str, *, since: str = "", companion=None,
    cap: int = 0, newest_first: bool = False,
) -> int | None:
    """Bring one table in step with PG. Returns rows fetched, or None if absent.

    ``companion`` rides along with the table's changed keys -- ``runs`` uses
    it to carry the TIC arrays into ``tic_traces``. ``cap`` > 0 bounds how many
    changed keys one call fetches; the table is only marked in sync once a
    call has fetched all of them.
    """
    key_cols, chunk = _MIRRORED[table]
    pg_cols = set(src.columns(table))
    if not pg_cols:
        return None  # PG side not migrated yet; the dashboard shows summaries only
    local_cols = [r[1] for r in local.execute(f"PRAGMA table_info({table})")]
    cols = [c for c in local_cols if c in pg_cols]
    if not all(k in cols for k in key_cols):
        logger.warning("mirror: %s lacks key columns %s on one side; skipped", table, key_cols)
        return None

    shape = json.dumps({"key": list(key_cols), "cols": cols}, separators=(",", ":"))
    stored_fp = _table_state(local, table, shape, key_cols)
    state = dict(local.execute("SELECT k, fp FROM pg_mirror_keys WHERE tbl = ?", (table,)))
    local_keys = {
        _enc(r) for r in local.execute(f"SELECT DISTINCT {_quote(key_cols)} FROM {table}")
    }
    # A row the mirror brought in and something since deleted locally: PG has
    # not changed, but the table fingerprint alone would never notice.
    locally_missing = any(k not in local_keys for k in state)

    table_fp = None
    if not since:
        table_fp = src.table_fp(table)
        if stored_fp == table_fp and not locally_missing:
            return 0

    pg_keys = {
        _enc(k): (k, fp)
        for k, fp in src.key_fps(table, key_cols, since, newest_first=newest_first)
    }
    changed = [
        e for e, (_, fp) in pg_keys.items()
        if state.get(e) != fp or e not in local_keys
    ]
    drained = True
    if cap > 0 and len(changed) > cap:
        changed, drained = changed[:cap], False
    # A windowed key list is partial by construction: absence means nothing.
    gone = [] if since else [e for e in state if e not in pg_keys]

    insert = (
        f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
        f"VALUES ({','.join('?' * len(cols))})"
    )
    written = 0
    for i in range(0, len(changed), chunk):
        part = changed[i:i + chunk]
        keys = [pg_keys[e][0] for e in part]
        rows = src.fetch(table, cols, key_cols, keys)
        extra = companion.fetch(src, keys) if companion else None
        # One local transaction per chunk, bookkeeping included, so a pull
        # that dies here resumes after the chunks that already landed.
        with local:
            _delete_keys(local, table, key_cols, keys)
            local.executemany(insert, [tuple(r) for r in rows])
            local.executemany(
                "INSERT OR REPLACE INTO pg_mirror_keys (tbl, k, fp) VALUES (?, ?, ?)",
                [(table, e, pg_keys[e][1]) for e in part],
            )
            if companion:
                companion.apply(local, extra)
        written += len(rows)

    if gone:
        keys = [tuple(json.loads(e)) for e in gone]
        with local:
            _delete_keys(local, table, key_cols, keys)
            if companion:
                companion.drop(local, keys)
            local.executemany(
                "DELETE FROM pg_mirror_keys WHERE tbl = ? AND k = ?",
                [(table, e) for e in gone],
            )

    if table_fp is not None and drained:
        with local:
            local.execute(
                "UPDATE pg_mirror_tables SET fp = ?, synced_at = datetime('now') "
                "WHERE tbl = ?",
                (table_fp, table),
            )
    return written


class _TicProjection:
    """Carry PG's inline TIC columns into the local ``tic_traces`` table.

    PG stores the trace as JSONB arrays on the run row; SQLite's table wants
    JSON *strings* in ``rt_min`` / ``intensity`` (``get_tic_trace`` calls
    ``json.loads`` on them), so re-serialise rather than passing the parsed
    lists straight through. Only runs whose row changed are fetched.

    A run whose PG row has no TIC keeps any local trace: on a single-lab
    install the watcher may have extracted one that PG never received.

    A malformed trace is skipped, not raised: it is written in the same local
    transaction as its run rows, so an exception here would roll back the
    whole chunk of runs and fail the same way on every tick after.
    """

    def __init__(self) -> None:
        self.written = 0

    def fetch(self, src, keys: list) -> list:
        return src.fetch_tic([k[0] for k in keys])

    def apply(self, local, fetched: list) -> None:
        batch, bad = [], 0
        for run_id, rt, inten in fetched:
            # psycopg2 hands JSONB back already decoded; tolerate a str either way.
            try:
                if isinstance(rt, str):
                    rt = json.loads(rt)
                if isinstance(inten, str):
                    inten = json.loads(inten)
            except ValueError:
                bad += 1
                continue
            if not isinstance(rt, list) or not isinstance(inten, list):
                bad += 1
                continue
            if not rt or not inten:
                continue
            batch.append((str(run_id), json.dumps(rt), json.dumps(inten), len(rt)))
        if bad:
            logger.warning("mirror: skipped %d malformed TIC trace(s)", bad)
        if batch:
            local.executemany(
                "INSERT OR REPLACE INTO tic_traces (run_id, rt_min, intensity, n_frames) "
                "VALUES (?, ?, ?, ?)",
                batch,
            )
        self.written += len(batch)

    def drop(self, local, keys: list) -> None:
        local.executemany(
            "DELETE FROM tic_traces WHERE run_id = ?", [(str(k[0]),) for k in keys],
        )
