#!/usr/bin/env python
"""Run 4DFF on recent .d runs that have no ion cloud yet, then publish.

Companion to feature_cloud_backfill.py, which only reads sidecars that
already exist. This one generates the missing sidecars first. Kept
separate because it is expensive (minutes of CPU + ~100 MB on disk per
run) while the extraction pass is seconds.

Same placement rule: NOT under /quobyte/proteomics-grp/brett/, or the
`stan/` checkout there shadows the installed package on sys.path.
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

# Shared-group install, so `stan install-4dff` never fills the home quota.
os.environ.setdefault("STAN_BRUKER_FF_DIR", "/quobyte/proteomics-grp/brett/bruker_ff")

from stan.db_pg import _connect  # noqa: E402
from stan.metrics.feature_cloud import extract_feature_cloud  # noqa: E402
from stan.metrics.features import (  # noqa: E402
    find_features_file, is_4dff_installed, run_4dff,
)

LOG_DIR = Path("/quobyte/proteomics-grp/STAN/logs")
CACHE_DIR = Path("/quobyte/proteomics-grp/STAN/feature_clouds")

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-points", type=int, default=5000)
    ap.add_argument("--timeout-min", type=int, default=45)
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    args = ap.parse_args()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log_fh = open(LOG_DIR / f"ioncloud_4dff_{ts}_s{args.shard}.jsonl", "a",
                  encoding="utf-8")

    def log(rec: dict) -> None:
        rec["ts"] = datetime.now(timezone.utc).isoformat()
        log_fh.write(json.dumps(rec) + "\n")
        log_fh.flush()

    if not is_4dff_installed("linux"):
        print("[fatal] 4DFF not installed at "
              f"{os.environ['STAN_BRUKER_FF_DIR']}", flush=True)
        log({"event": "fatal", "reason": "4DFF not installed"})
        return 2

    pg = _connect()
    cur = pg.cursor()
    sql = ("SELECT r.id, r.run_name, r.raw_path FROM runs r "
           "WHERE r.raw_path LIKE '%%.d' "
           f"AND r.run_date >= now() - interval '{int(args.days)} days' "
           "AND NOT EXISTS (SELECT 1 FROM feature_clouds f "
           "                WHERE f.run_id = r.id::text) "
           "ORDER BY r.run_date DESC")
    if args.limit > 0:
        sql += f" LIMIT {int(args.limit)}"
    cur.execute(sql)
    rows = [(str(r[0]), r[1], r[2]) for r in cur.fetchall()]
    # End the read transaction before the loop. Without this the AccessShareLock
    # this SELECT took on runs/feature_clouds is held until the first UPSERT
    # commits below -- and the first iteration runs 4DFF, up to --timeout-min
    # (45) minutes. A read transaction that long blocks DDL on those tables and
    # pins VACUUM's cleanup horizon for the whole backfill.
    pg.commit()
    if args.nshards > 1:
        rows = [r for i, r in enumerate(rows) if i % args.nshards == args.shard]

    print(f"[start] {len(rows)} runs need 4DFF (shard {args.shard}/"
          f"{args.nshards}, last {args.days}d)", flush=True)
    log({"event": "start", "n": len(rows), "shard": args.shard,
         "days": args.days})

    done = skipped = errors = 0
    for run_id, run_name, raw_path in rows:
        d = Path(raw_path)
        if not (d.is_dir() and d.suffix.lower() == ".d"):
            skipped += 1
            log({"event": "skip", "run_id": run_id, "run_name": run_name,
                 "reason": "raw .d not readable", "raw_path": raw_path})
            print(f"[skip] {run_name}: .d not readable", flush=True)
            continue
        t0 = time.monotonic()
        feat = find_features_file(d)
        if feat is None:
            try:
                res = run_4dff(d, timeout_min=args.timeout_min, platform="linux")
                feat = res.features_path
                log({"event": "4dff", "run_id": run_id, "run_name": run_name,
                     "sec": res.wall_clock_sec, "rc": res.returncode})
            except Exception as e:
                errors += 1
                log({"event": "error", "stage": "4dff", "run_id": run_id,
                     "run_name": run_name, "error": str(e),
                     "error_type": type(e).__name__})
                print(f"[err] 4dff {run_name}: {e}", flush=True)
                continue
        try:
            cloud = extract_feature_cloud(feat, max_points=args.max_points)
            if cloud.n_points == 0:
                skipped += 1
                log({"event": "skip", "run_id": run_id, "run_name": run_name,
                     "reason": "sidecar has no usable rows"})
                continue
            created = datetime.now(timezone.utc).isoformat(timespec="seconds")
            payload = {
                "run_id": run_id, "run_name": run_name, "source": "runs",
                "mz": cloud.mz, "mobility": cloud.mobility, "rt": cloud.rt,
                "charge": [int(z) for z in cloud.charge],
                "intensity": cloud.intensity, "n_points": cloud.n_points,
                "n_total": cloud.n_total, "features_path": str(feat),
                "created_at": created,
            }
            tmp = CACHE_DIR / f".{run_id}.json.part"
            tmp.write_text(json.dumps(payload))
            tmp.replace(CACHE_DIR / f"{run_id}.json")
            cur.execute(UPSERT, (
                run_id, "runs", json.dumps(cloud.mz),
                json.dumps(cloud.mobility), json.dumps(cloud.rt),
                json.dumps([int(z) for z in cloud.charge]),
                json.dumps(cloud.intensity), cloud.n_points, cloud.n_total,
                str(feat), created,
            ))
            pg.commit()
        except Exception as e:
            errors += 1
            pg.rollback()
            log({"event": "error", "stage": "publish", "run_id": run_id,
                 "run_name": run_name, "error": str(e),
                 "error_type": type(e).__name__})
            print(f"[err] publish {run_name}: {e}", flush=True)
            continue
        done += 1
        log({"event": "done", "run_id": run_id, "run_name": run_name,
             "n_points": cloud.n_points, "n_total": cloud.n_total,
             "sec": round(time.monotonic() - t0, 1)})
        print(f"[ok] {run_name[:56]:<56} {cloud.n_points}/{cloud.n_total} "
              f"{time.monotonic() - t0:.0f}s", flush=True)

    log({"event": "end", "done": done, "skipped": skipped, "errors": errors})
    print(f"[end] done={done} skipped={skipped} errors={errors}", flush=True)
    log_fh.close()
    cur.close()
    pg.close()
    return 0


if __name__ == "__main__":
    os.environ.setdefault("STAN_DB_BACKEND", "pg")
    sys.exit(main())
