#!/usr/bin/env python
"""Backfill PEG for Thermo (Orbitrap) QC runs on Hive, straight into PG Farm.

Why: PEG needs raw MS1, and the Hive venv has no fisher_py, so every Lumos
and Exploris QC run went through the pipeline with PEG left NULL -- 0 of
2,924 rows scored on 2026-09-28. ``stan.metrics.peg_io.read_ms1_thermo`` now
falls back to the ThermoRawFileParser container (``stan.metrics.peg_trfp``),
so new runs score inline; this driver scores the ones that were missed.

Same computation as the pipeline, not a second one: ``read_ms1_any`` ->
``detect_peg_in_spectra`` with their defaults (80 strided MS1 scans, 5 ppm,
1e4 floor), then ``stan.db.update_peg_result`` + ``insert_peg_ion_hits`` --
exactly what ``stan.pipeline.hive_process._run_peg_and_drift`` calls for a
``.raw``. Two deliberate differences, both about failure and ordering:

  * Any failure leaves PEG NULL. The pipeline stamps ``peg_class='unknown'``
    (score 0.0) on a non-reader exception; here that would take the run out
    of the NULL queue, so a fixable failure could never be retried, and the
    sentinel is not a measurement anyway.
  * The ion hits are written BEFORE the scalars. The queue is "peg_score IS
    NULL", and this runs on the preemptible ``low`` partition: scalars-first
    plus a preemption in between would mark the run done with no ladder
    rows, forever. Hits-first just means a requeued shard redoes the run and
    ``insert_peg_ion_hits`` replaces its rows.

Runs under SLURM only (``peg_backfill_thermo.sbatch``): each run converts a
~1 GB .raw inside the container, which must never happen on a login node.

Must NOT live under /quobyte/proteomics-grp/brett/ -- Python puts the
script's own directory first on sys.path and the ``stan/`` checkout there
shadows the installed package. Canonical copy: ``scripts/`` in the repo;
running copy: /quobyte/proteomics-grp/STAN/.

PG Farm bills every byte it serves and its connection slots are shared with
FRAN, so each shard asks for exactly its own runs (sharding happens in SQL)
and the job holds one cached connection.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO

from stan.db import insert_peg_ion_hits, update_peg_result
from stan.db_pg import _connect, use_pg
from stan.metrics.peg import detect_peg_in_spectra
from stan.metrics.peg_io import N_SCANS_DEFAULT, PegReaderUnavailable, read_ms1_any
from stan.metrics.peg_trfp import TrfpUnavailable, find_trfp_container

logger = logging.getLogger("peg_backfill_thermo")

LOG_DIR = Path("/quobyte/proteomics-grp/STAN/logs")

#: A run's shard, computed IN PG: the first byte of md5(id), mod nshards.
#: Same expression as feature_cloud_backfill.py and for the same reason: the
#: queue is "PEG still NULL", so it shrinks as array tasks finish, and
#: numbering result rows would move runs between shards whenever one task
#: starts later than another on `low`. Hashing makes the shard a property of
#: the run; ORDER BY id makes the order within a shard stable across requeues.
SHARD_EXPR = "get_byte(decode(md5(id::text), 'hex'), 0)"


@dataclass(frozen=True)
class Candidate:
    """One Thermo QC run still missing PEG."""

    run_id: str
    raw_path: str
    run_name: str
    instrument: str


def build_candidates_query(
    *, shard: int, nshards: int, limit: int,
    instrument: str = "", run_ids: tuple[str, ...] = (),
) -> tuple[str, tuple]:
    """SQL + params for the Thermo QC runs this shard should score.

    Thermo = an Orbitrap instrument name or a ``.raw`` raw_path (either is
    enough: 2,924 rows match both today). ``hidden = 0`` is an integer
    compare because PG ``runs.hidden`` is integer, not boolean. Targeted
    columns only -- no SELECT *, no array columns.

    Args:
        shard: this task's shard, in ``[0, nshards)``.
        nshards: total shards; 1 disables sharding.
        limit: cap on rows for this shard; 0 = no cap.
        instrument: optional extra ``instrument ILIKE %x%`` filter.
        run_ids: optional explicit run ids (still subject to PEG IS NULL).

    Returns:
        (sql, params) for ``cursor.execute``.
    """
    sql = ("SELECT id::text, raw_path, run_name, instrument FROM runs"
           " WHERE peg_score IS NULL AND hidden = 0"
           " AND (instrument ILIKE %s OR raw_path ILIKE %s)")
    params: list = ["%orbitrap%", "%.raw"]
    if instrument:
        sql += " AND instrument ILIKE %s"
        params.append(f"%{instrument}%")
    if run_ids:
        sql += " AND id::text = ANY(%s)"
        params.append(list(run_ids))
    if nshards > 1:
        # `%%` is psycopg2's escaped `%`; params are always passed here.
        sql += f" AND {SHARD_EXPR} %% %s = %s"
        params += [nshards, shard]
    sql += " ORDER BY id"
    if limit > 0:
        sql += " LIMIT %s"
        params.append(limit)
    return sql, tuple(params)


def fetch_candidates(sql: str, params: tuple) -> list[Candidate]:
    """Run the candidate query on PG Farm and return the rows.

    The ``with`` block commits on exit, which ends the read transaction
    before the first (minutes-long) conversion -- otherwise it would hold
    read locks on ``runs`` and pin VACUUM for the whole shard.
    """
    with _connect() as pg, pg.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [Candidate(str(r[0]), r[1] or "", r[2] or "", r[3] or "") for r in rows]


def reader_status() -> dict:
    """Which Thermo MS1 readers this host has. Logged at start."""
    status: dict = {"fisher_py": importlib.util.find_spec("fisher_py") is not None}
    try:
        c = find_trfp_container()
        status.update(trfp_sif=str(c.sif), apptainer=c.apptainer, trfp_error=None)
    except TrfpUnavailable as e:
        status.update(trfp_sif=None, apptainer=None, trfp_error=str(e))
    return status


def process_run(cand: Candidate, *, n_scans: int, dry_run: bool) -> dict:
    """Score one run and (unless dry_run) persist it. Returns a log record.

    The record's ``event`` is one of ``done``, ``dry_run``, ``skip``,
    ``unavailable`` or ``error``; failures never raise.
    """
    rec: dict = {"run_id": cand.run_id, "run_name": cand.run_name,
                 "instrument": cand.instrument, "raw_path": cand.raw_path}
    raw = Path(cand.raw_path) if cand.raw_path else None
    if raw is None or not raw.exists():
        return {**rec, "event": "skip", "reason": "raw file not found"}

    t0 = time.monotonic()
    try:
        spectra = list(read_ms1_any(raw, vendor="thermo", n_scans=n_scans))
        peg = detect_peg_in_spectra(spectra)
    except PegReaderUnavailable as e:
        return {**rec, "event": "unavailable", "reason": str(e),
                "sec": round(time.monotonic() - t0, 1)}
    except Exception as e:  # noqa: BLE001 - one bad file must not end the shard
        logger.warning("PEG failed for %s: %s: %s", cand.run_name, type(e).__name__, e)
        return {**rec, "event": "error", "stage": "detect",
                "error_type": type(e).__name__, "error": str(e),
                "sec": round(time.monotonic() - t0, 1)}

    rec.update(
        peg_score=float(peg.peg_score),
        peg_intensity_pct=float(peg.intensity_pct),
        peg_n_ions_detected=int(peg.n_ions_detected),
        peg_class=peg.peg_class,
        ladder_coherence=round(float(peg.ladder_coherence), 3),
        n_coherence_pairs=int(peg.n_coherence_pairs),
        n_spectra=len(spectra),
        n_matches=len(peg.matches),
        sec_measure=round(time.monotonic() - t0, 1),
    )
    if not spectra or peg.total_intensity <= 0:
        # Nothing read (or nothing above the 1e4 floor, which no real
        # Orbitrap QC run produces) would otherwise be stored as a clean
        # 0.0 -- an unmeasured run passed off as a clean one.
        return {**rec, "event": "error", "stage": "detect",
                "error": "reader returned no MS1 signal"}
    if dry_run:
        return {**rec, "event": "dry_run"}

    try:
        rec["n_hits"] = insert_peg_ion_hits(
            run_id=cand.run_id, matches=peg.matches, table="runs",
        )
        updated = update_peg_result(
            run_id=cand.run_id,
            peg_score=peg.peg_score,
            peg_n_ions_detected=peg.n_ions_detected,
            peg_intensity_pct=peg.intensity_pct,
            peg_class=peg.peg_class,
            table="runs",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("PEG write failed for %s: %s: %s", cand.run_name, type(e).__name__, e)
        return {**rec, "event": "error", "stage": "write",
                "error_type": type(e).__name__, "error": str(e)}
    if not updated:
        logger.warning("PEG NOT persisted: no runs row id=%s (%s)", cand.run_id, cand.run_name)
        return {**rec, "event": "error", "stage": "write", "error": "no runs row updated"}
    return {**rec, "event": "done", "sec": round(time.monotonic() - t0, 1)}


def _open_log(log_dir: Path, shard: int, nshards: int) -> tuple[Path, IO[str]]:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    # One file per shard: array tasks start in the same second, and several
    # processes appending to one file on Quobyte is not something to trust.
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"peg_backfill_thermo_{ts}_s{shard}of{nshards}.jsonl"
    return path, open(path, "a", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code (0 ok, 1 failures, 2 refused)."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--shard", type=int, default=0)
    ap.add_argument("--nshards", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0,
                    help="Score at most N runs (per shard); 0 = all.")
    ap.add_argument("--instrument", default="",
                    help="Extra filter: instrument ILIKE %%X%%.")
    ap.add_argument("--run-id", action="append", default=[],
                    help="Only this run id (repeatable).")
    ap.add_argument("--n-scans", type=int, default=N_SCANS_DEFAULT,
                    help="MS1 scans sampled per file. Leave at the default: "
                         "scores are only comparable at the same value.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Compute PEG but write nothing to PG.")
    ap.add_argument("--list-only", action="store_true",
                    help="Log this shard's queue and exit; no conversion.")
    ap.add_argument("--log-dir", type=Path, default=LOG_DIR)
    args = ap.parse_args(argv)
    if args.nshards < 1 or not 0 <= args.shard < args.nshards:
        # An out-of-range shard matches no run, silently.
        ap.error(f"--shard must be in [0, {args.nshards}); got {args.shard}")

    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    log_path, log_fh = _open_log(args.log_dir, args.shard, args.nshards)

    def log(rec: dict) -> None:
        rec["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        log_fh.write(json.dumps(rec, default=str) + "\n")
        log_fh.flush()

    try:
        if not use_pg():
            # update_peg_result would quietly write a SQLite file nobody reads.
            log({"event": "refused", "reason": "STAN_DB_BACKEND is not pg"})
            logger.error("STAN_DB_BACKEND=pg is required; refusing to run")
            return 2
        readers = reader_status()
        if not readers["fisher_py"] and readers["trfp_sif"] is None and not args.list_only:
            log({"event": "refused", "reason": "no Thermo MS1 reader", **readers})
            logger.error("no Thermo MS1 reader: fisher_py missing and %s",
                         readers["trfp_error"])
            return 2

        sql, params = build_candidates_query(
            shard=args.shard, nshards=args.nshards, limit=args.limit,
            instrument=args.instrument, run_ids=tuple(args.run_id),
        )
        cands = fetch_candidates(sql, params)
        log({"event": "start", "n_queued": len(cands), "shard": args.shard,
             "nshards": args.nshards, "limit": args.limit, "n_scans": args.n_scans,
             "dry_run": args.dry_run, "list_only": args.list_only,
             "instrument": args.instrument, "run_ids": args.run_id,
             "host": os.uname().nodename, "slurm_job": os.environ.get("SLURM_JOB_ID"),
             "log": str(log_path), **readers})
        logger.info("shard %d/%d: %d Thermo runs with NULL PEG (log %s)",
                    args.shard, args.nshards, len(cands), log_path)

        if args.list_only:
            for c in cands:
                log({"event": "queued", "run_id": c.run_id, "run_name": c.run_name,
                     "instrument": c.instrument, "raw_path": c.raw_path})
            log({"event": "end", "queued": len(cands)})
            return 0

        counts = {"done": 0, "dry_run": 0, "skip": 0, "unavailable": 0, "error": 0}
        t_start = time.monotonic()
        for i, cand in enumerate(cands, 1):
            rec = process_run(cand, n_scans=args.n_scans, dry_run=args.dry_run)
            counts[rec["event"]] = counts.get(rec["event"], 0) + 1
            log(rec)
            if rec["event"] in ("done", "dry_run"):
                logger.info("[%d/%d] %s %-7s score=%.1f pct=%.3f ions=%d %.0fs",
                            i, len(cands), cand.run_name[:56], rec["peg_class"],
                            rec["peg_score"], rec["peg_intensity_pct"],
                            rec["peg_n_ions_detected"], rec.get("sec", rec["sec_measure"]))
            else:
                logger.warning("[%d/%d] %s %s: %s", i, len(cands), cand.run_name[:56],
                               rec["event"], rec.get("reason") or rec.get("error"))
        log({"event": "end", **counts,
             "elapsed_s": round(time.monotonic() - t_start, 1)})
        logger.info("end: %s", counts)
        return 0 if counts["error"] == 0 and counts["unavailable"] == 0 else 1
    finally:
        log_fh.close()


if __name__ == "__main__":
    # The SELECT goes to PG either way; this makes the writes follow it.
    os.environ.setdefault("STAN_DB_BACKEND", "pg")
    sys.exit(main())
