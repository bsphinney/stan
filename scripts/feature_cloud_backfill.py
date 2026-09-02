#!/usr/bin/env python
"""Publish charge-labeled 4DFF ion clouds from Hive into PG Farm.

Why a standalone driver instead of `stan backfill-feature-cloud`:
the Hive checkout at /quobyte/proteomics-grp/brett/stan is a patched,
deliberately-not-pulled fork (v0.2.376) and must not be updated wholesale.
The only file added to it for this work is stan/metrics/feature_cloud.py,
whose canonical home is the STAN repo on Brett's Mac. Everything else the
backfill needs lives here.

Must NOT live under /quobyte/proteomics-grp/brett/ -- Python puts the
script's own directory first on sys.path and the `stan/` checkout there
shadows the installed package.

Run under SLURM (partition low). One PG connection for the whole job:
PG Farm has limited connection slots and is shared with FRAN.
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
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
    args = ap.parse_args()

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

    sql = "SELECT id, run_name, raw_path FROM runs WHERE raw_path LIKE '%%.d'"
    params: list = []
    if args.since:
        sql += " AND run_date >= %s"
        params.append(args.since)
    sql += " ORDER BY run_date DESC"
    if args.limit > 0:
        sql += " LIMIT %s"
        params.append(args.limit)
    cur.execute(sql, tuple(params))
    rows = [(str(r[0]), r[1], r[2]) for r in cur.fetchall()]

    have: set[str] = set()
    if not args.force:
        if pg_ok:
            try:
                cur.execute(
                    "SELECT run_id FROM feature_clouds WHERE source = 'runs'"
                )
                have = {str(r[0]) for r in cur.fetchall()}
            except Exception:
                pg.rollback()
        if cache_dir and not pg_ok:
            have = {f.stem for f in cache_dir.glob("*.json")}

    if args.nshards > 1:
        rows = [r for i, r in enumerate(rows) if i % args.nshards == args.shard]

    # End the read transaction the SELECTs above opened. It is otherwise held
    # until the first per-run UPSERT commits, with the sidecar extraction of
    # run #1 sitting inside it -- read locks on runs/feature_clouds and a
    # pinned VACUUM horizon for no reason.
    pg.commit()

    log({"event": "start", "n_queued": len(rows), "already_stored": len(have),
         "shard": args.shard, "nshards": args.nshards,
         "max_points": args.max_points, "force": args.force,
         "since": args.since, "log": str(log_path),
         "pg_ok": pg_ok, "cache_dir": str(cache_dir) if cache_dir else ""})
    print(f"[start] {len(rows)} runs queued, {len(have)} already stored, "
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
