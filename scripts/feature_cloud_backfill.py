#!/usr/bin/env python
"""Publish charge-labeled 4DFF ion clouds from Hive into PG Farm.

Why a standalone driver instead of `stan backfill-feature-cloud`: it adds
the sharding, SLURM plumbing and JSON-cache fallback the bare command does
not have. (It was first written because the Hive checkout was a patched,
never-pulled fork; that checkout now tracks main.)

Must NOT live under /quobyte/proteomics-grp/brett/ -- Python puts the
script's own directory first on sys.path and the `stan/` checkout there
shadows the installed package.

Run under SLURM (partition low). One PG connection for the whole job:
PG Farm has limited connection slots and is shared with FRAN.

PG Farm also bills every byte it serves, and this runs 4 shards an hour, so
each shard asks PG for exactly the runs it will work on -- see
build_runs_query.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/quobyte/proteomics-grp/brett/stan")

from stan.db_pg import _connect  # noqa: E402
from stan.metrics.feature_cloud import extract_feature_cloud  # noqa: E402

LOG_DIR = Path("/quobyte/proteomics-grp/STAN/logs")

DDL = """
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

UPSERT = """
INSERT INTO feature_clouds (run_id, source, mz, mobility, rt, charge,
    intensity, n_points, n_total, features_path, created_at)
VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
ON CONFLICT (run_id, source) DO UPDATE SET
    mz = EXCLUDED.mz, mobility = EXCLUDED.mobility, rt = EXCLUDED.rt,
    charge = EXCLUDED.charge, intensity = EXCLUDED.intensity,
    n_points = EXCLUDED.n_points, n_total = EXCLUDED.n_total,
    features_path = EXCLUDED.features_path, created_at = EXCLUDED.created_at
"""


#: A run's shard, computed IN PG: the first byte of md5(id), mod nshards.
#: Hashing the id rather than numbering result rows makes the shard a property
#: of the run. Array tasks on `low` start whenever a slot frees, and with
#: row-position sharding over a NOT EXISTS result, shard 0 finishing first
#: shrinks the list shard 1 numbers, so runs shift between shards and some are
#: built twice while others wait a tick. md5/decode/get_byte are all
#: documented builtins. Checked live 2026-09-22: 1,126 pending runs split
#: 296/296/260/274, and the four shards' union equals the unsharded set.
SHARD_EXPR = "get_byte(decode(md5(id::text), 'hex'), 0)"


def build_runs_query(*, since: str, limit: int, skip_stored: bool,
                     shard: int, nshards: int) -> tuple[str, tuple]:
    """SQL + params for the `.d` runs this shard should look at.

    Until 2026-09-22 every shard downloaded every `.d` run (1,723 rows, 319 KB)
    and every stored cloud key (597 rows, 23 KB), then discarded three quarters
    of the runs to find its shard and most of the rest because they already
    had a cloud: ~1.36 MB an hour from a bill-per-byte database to learn that
    nothing had changed. Both filters now run in PG, so a shard receives only
    runs that are its own and still lack a cloud -- ~51 KB each, measured the
    same day. What remains is the ~1,100 `.d` runs with no sidecar at all,
    re-offered every tick on purpose (see below).

    skip_stored adds the NOT EXISTS against feature_clouds, an index-only
    anti-join on its primary key. `runs.id` and `feature_clouds.run_id` are
    both TEXT today; the cast keeps the join valid if `runs.id` ever is not.
    Callers pass False under --force (rebuild what exists) and when PG's
    feature_clouds is unusable (the JSON cache decides instead).

    There is deliberately no default `since`: a sidecar that 4DFF writes
    months after its run must still be found, and "lacks a cloud" is already
    the narrowest question that allows that.

    `%%` is psycopg2's escaped `%`; params are always passed, so it is always
    interpolated. With nshards > 1, `limit` caps this shard, not the job.
    """
    sql = "SELECT id, run_name, raw_path FROM runs WHERE raw_path LIKE '%%.d'"
    params: list = []
    if since:
        sql += " AND run_date >= %s"
        params.append(since)
    if skip_stored:
        sql += (" AND NOT EXISTS (SELECT 1 FROM feature_clouds f"
                " WHERE f.run_id = runs.id::text AND f.source = 'runs')")
    if nshards > 1:
        sql += f" AND {SHARD_EXPR} %% %s = %s"
        params += [nshards, shard]
    sql += " ORDER BY run_date DESC"
    if limit > 0:
        sql += " LIMIT %s"
        params.append(limit)
    return sql, tuple(params)


def find_features_file(d_path: str) -> Path | None:
    """Locate the .features sidecar 4DFF wrote for a .d run.

    Mirrors stan.metrics.features.find_features_file. 4DFF preserves the
    `.d` before `.features` (foo.d/foo.d.features); older builds and Ziggy
    used the stem and/or the parent dir, so all four forms are tried.
    """
    d = Path(d_path)
    full, stem = d.name, d.stem
    for c in (d / f"{full}.features", d / f"{stem}.features",
              d.parent / f"{full}.features", d.parent / f"{stem}.features"):
        try:
            if c.exists():
                return c
        except OSError:
            continue
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N runs, newest first (per shard).")
    ap.add_argument("--since", default="")
    ap.add_argument("--max-points", type=int, default=5000)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--cache-dir",
                    default="/quobyte/proteomics-grp/STAN/feature_clouds",
                    help="Write one <run_id>.json per cloud here. This is the "
                         "delivery path when PG lacks the feature_clouds table: "
                         "the dir is visible on Brett's Mac as "
                         "/Volumes/proteomics-grp/STAN/feature_clouds and loads "
                         "with `stan backfill-feature-cloud --from-cache`.")
    ap.add_argument("--no-pg", action="store_true",
                    help="Skip the PG upsert; write the JSON cache only.")
    args = ap.parse_args(argv)
    if args.nshards < 1 or not 0 <= args.shard < args.nshards:
        # An out-of-range shard matches no run, silently, every tick.
        ap.error(f"--shard must be in [0, {args.nshards}); got {args.shard}")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"feature_cloud_backfill_{ts}_s{args.shard}.jsonl"
    log_fh = open(log_path, "a", encoding="utf-8")

    def log(rec: dict) -> None:
        rec["ts"] = datetime.now(timezone.utc).isoformat()
        log_fh.write(json.dumps(rec) + "\n")
        log_fh.flush()

    cache_dir = Path(args.cache_dir) if args.cache_dir else None
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)

    pg = _connect()
    cur = pg.cursor()

    # The service account has DML on the existing tables but no CREATE on
    # schema public -- every table is owned by `brettsp` (CAS login only).
    # So try, and on refusal fall back to the JSON cache rather than dying:
    # a cloud on disk that the Mac can load is worth more than a clean abort.
    pg_ok = not args.no_pg
    if pg_ok:
        try:
            cur.execute(DDL)
            pg.commit()
        except Exception as e:
            pg.rollback()
            try:
                cur.execute("SELECT 1 FROM feature_clouds LIMIT 1")
                cur.fetchall()
                pg.commit()
            except Exception:
                pg.rollback()
                pg_ok = False
                print(f"[warn] feature_clouds unavailable in PG ({e}); "
                      f"writing JSON cache only", flush=True)

    # PG leaves out stored clouds itself when it can (skip_stored); it used to
    # send every key so this loop could drop them. With PG's table unusable,
    # the JSON cache is the record of what is already done.
    sql, params = build_runs_query(
        since=args.since, limit=args.limit,
        skip_stored=pg_ok and not args.force,
        shard=args.shard, nshards=args.nshards,
    )
    cur.execute(sql, params)
    rows = [(str(r[0]), r[1], r[2]) for r in cur.fetchall()]

    have: set[str] = set()
    n_stored: int | None = None
    if not args.force and not pg_ok and cache_dir:
        have = {f.stem for f in cache_dir.glob("*.json")}
    if pg_ok:
        # For the start log only: one row, where the key list was 597.
        try:
            cur.execute("SELECT count(*) FROM feature_clouds WHERE source = 'runs'")
            n_stored = int(cur.fetchone()[0])
        except Exception:
            pg.rollback()

    # End the read transaction the SELECTs above opened. It is otherwise held
    # until the first per-run UPSERT commits, with the sidecar extraction of
    # run #1 sitting inside it -- read locks on runs/feature_clouds and a
    # pinned VACUUM horizon for no reason.
    pg.commit()

    already = n_stored if n_stored is not None else len(have)
    log({"event": "start", "n_queued": len(rows), "already_stored": already,
         "shard": args.shard, "nshards": args.nshards,
         "max_points": args.max_points, "force": args.force,
         "since": args.since, "log": str(log_path),
         "pg_ok": pg_ok, "cache_dir": str(cache_dir) if cache_dir else ""})
    print(f"[start] {len(rows)} runs queued, {already} already stored, "
          f"shard {args.shard}/{args.nshards}", flush=True)

    done = skipped = errors = 0
    for run_id, run_name, raw_path in rows:
        if not args.force and run_id in have:
            skipped += 1
            continue
        feat = find_features_file(raw_path)
        if feat is None:
            skipped += 1
            log({"event": "skip", "run_id": run_id, "run_name": run_name,
                 "reason": "no .features sidecar", "raw_path": raw_path})
            continue
        t0 = time.monotonic()
        try:
            cloud = extract_feature_cloud(feat, max_points=args.max_points)
        except Exception as e:
            errors += 1
            log({"event": "error", "stage": "extract", "run_id": run_id,
                 "run_name": run_name, "error": str(e),
                 "error_type": type(e).__name__})
            print(f"[err] {run_name}: {e}", flush=True)
            continue
        if cloud.n_points == 0:
            skipped += 1
            log({"event": "skip", "run_id": run_id, "run_name": run_name,
                 "reason": "sidecar has no usable rows"})
            continue
        if args.dry_run:
            done += 1
            print(f"[dry] {run_name} {cloud.n_points}/{cloud.n_total}", flush=True)
            continue
        created = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = {
            "run_id": run_id, "run_name": run_name, "source": "runs",
            "mz": cloud.mz, "mobility": cloud.mobility, "rt": cloud.rt,
            "charge": [int(z) for z in cloud.charge],
            "intensity": cloud.intensity,
            "n_points": cloud.n_points, "n_total": cloud.n_total,
            "features_path": str(feat), "created_at": created,
        }
        if cache_dir:
            tmp = cache_dir / f".{run_id}.json.part"
            try:
                tmp.write_text(json.dumps(payload))
                tmp.replace(cache_dir / f"{run_id}.json")
            except OSError as e:
                errors += 1
                log({"event": "error", "stage": "cache", "run_id": run_id,
                     "run_name": run_name, "error": str(e)})
                print(f"[err] cache {run_name}: {e}", flush=True)
                continue
        if pg_ok:
            try:
                cur.execute(UPSERT, (
                    run_id, "runs",
                    json.dumps(cloud.mz), json.dumps(cloud.mobility),
                    json.dumps(cloud.rt),
                    json.dumps([int(z) for z in cloud.charge]),
                    json.dumps(cloud.intensity),
                    cloud.n_points, cloud.n_total, str(feat), created,
                ))
                pg.commit()
            except Exception as e:
                errors += 1
                pg.rollback()
                log({"event": "error", "stage": "upsert", "run_id": run_id,
                     "run_name": run_name, "error": str(e),
                     "error_type": type(e).__name__})
                print(f"[err] upsert {run_name}: {e}", flush=True)
                continue
        done += 1
        charges = sorted({int(z) for z in cloud.charge})
        log({"event": "done", "run_id": run_id, "run_name": run_name,
             "n_points": cloud.n_points, "n_total": cloud.n_total,
             "charges": charges, "sec": round(time.monotonic() - t0, 1),
             "features_path": str(feat)})
        print(f"[ok] {run_name[:56]:<56} {cloud.n_points:>6}/{cloud.n_total:<7} "
              f"z={charges} {time.monotonic() - t0:.1f}s", flush=True)

    log({"event": "end", "done": done, "skipped": skipped, "errors": errors})
    print(f"[end] done={done} skipped={skipped} errors={errors}", flush=True)
    log_fh.close()
    cur.close()
    pg.close()
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    os.environ.setdefault("STAN_DB_BACKEND", "pg")
    sys.exit(main())
