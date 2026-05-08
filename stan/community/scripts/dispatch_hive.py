"""Hive-side dispatcher: scan watch dirs, submit SLURM jobs.

Walks the configured watch directories on Quobyte, finds raw files
(``.d`` directories or ``.raw`` files) not yet processed in the global
stan.db, and submits one SLURM job per raw running ``stan hive-process``.

Designed to be invoked from Hive cron — single-pass, exits when done:

    */15 * * * * /quobyte/.../stan_venv/bin/stan hive-dispatch \\
        --config /quobyte/proteomics-grp/STAN/dispatch.yml \\
        >> /quobyte/proteomics-grp/STAN/logs/dispatch.log 2>&1

The config YAML enumerates instruments + their watch dirs + SLURM
parameters. ``stan hive-dispatch --print-default-config`` writes a
template to stdout for bootstrapping. Idempotent: ``dispatch_attempts``
rows skip raws already submitted, and re-running against the same raw
is a no-op (``stan hive-process`` short-circuits on existing rows).

Login-node-safe: this dispatcher only walks the filesystem and calls
``sbatch`` — no compute. Per CLAUDE.md, real work always lands inside
SLURM, never on login1.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_CONFIG_PATH = Path("/quobyte/proteomics-grp/STAN/dispatch.yml")
DEFAULT_QC_PATTERN = r"(?i)(he(l[a5\d]|\d)|qc|std[_\-\s]?he)"

# Embedded default-config template. Brett bootstraps via:
#   stan hive-dispatch --print-default-config > /quobyte/.../dispatch.yml
DEFAULT_CONFIG_TEMPLATE = """\
# STAN Hive-side dispatcher config.
#
# Lives at /quobyte/proteomics-grp/STAN/dispatch.yml by default.
# Edit this file rather than redeploying STAN — the dispatcher reads
# it on every invocation. Brett owns this file; lab members edit
# their own instrument entries.

# Global stan.db that all SLURM jobs write to. The dashboard on
# Brett's Mac reads this DB over the existing Quobyte mount.
db_path: /quobyte/proteomics-grp/STAN/stan.db

# Where DIA-NN/Sage outputs land. Per-raw subdirs are created
# automatically — keep this on Quobyte, never on /tmp (node-local,
# invisible after the job ends; CLAUDE.md rule).
out_root: /quobyte/proteomics-grp/STAN/processing

# Where SLURM stdout/stderr land. Same Quobyte rule — never /tmp.
sbatch_log_dir: /quobyte/proteomics-grp/STAN/logs/sbatch

# Where the dispatcher's own JSONL log lands.
dispatch_log_dir: /quobyte/proteomics-grp/STAN/logs/dispatch

# Path to the STAN venv on Hive. The SLURM script sources this so
# `stan hive-process` is on PATH inside the job. Install once via:
#   python -m venv /quobyte/proteomics-grp/brett/stan_venv
#   /quobyte/proteomics-grp/brett/stan_venv/bin/pip install \\
#       'stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip'
stan_venv: /quobyte/proteomics-grp/brett/stan_venv

# SLURM resource defaults. Per CLAUDE.md: high partition with the
# genome-center QOS for community searches; fall back to `low` /
# publicgrp-low-qos if (QOSGrpCpuLimit) is hit.
slurm:
  partition: high
  qos: genome-center-grp-high-qos
  account: genome-center-grp
  time: "06:00:00"
  cpus: 8
  mem: "32G"

# Per-invocation cap on new SLURM jobs to avoid blasting the queue
# during a backlog catch-up. Existing in-flight jobs are NOT counted
# — `squeue` is checked separately. 50/run is plenty for routine cron.
max_submissions_per_run: 50

# Optional QC filename regex. Only raws matching this regex are
# dispatched; non-matching files are silently skipped. Set to ""
# (empty string) to dispatch everything in the watch dirs.
qc_pattern: "{qc_pattern}"

# After this many failed dispatch_attempts on a raw, stop retrying.
# Avoids burning compute on a chronically broken raw (corrupt .d, etc.).
max_attempts: 3

# One entry per instrument. The dispatcher walks `watch_dir` for .d
# directories and .raw files. `name` becomes runs.instrument; `family`
# is the IPS cohort key.
instruments:
  - name: timsTOF HT
    family: timsTOF
    vendor: bruker
    watch_dir: /quobyte/proteomics-grp/hela_qcs/timstofHT
    column_vendor: ""
    column_model: ""

  - name: Orbitrap Fusion Lumos
    family: Lumos
    vendor: thermo
    watch_dir: /quobyte/proteomics-grp/hela_qcs/lumosRox
    column_vendor: ""
    column_model: ""

  - name: Orbitrap Exploris 480
    family: Exploris
    vendor: thermo
    watch_dir: /quobyte/proteomics-grp/hela_qcs/exploris480
    column_vendor: ""
    column_model: ""
""".replace("{qc_pattern}", DEFAULT_QC_PATTERN)


def _load_config(config_path: Path) -> dict:
    """Load + validate the dispatcher YAML."""
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config not found at {config_path}. Bootstrap with:\n"
            f"  stan hive-dispatch --print-default-config > {config_path}"
        )
    import yaml
    cfg = yaml.safe_load(config_path.read_text()) or {}

    required = ["db_path", "out_root", "sbatch_log_dir", "stan_venv", "instruments"]
    missing = [k for k in required if k not in cfg]
    if missing:
        raise ValueError(f"Config {config_path} missing required keys: {missing}")

    if not cfg["instruments"]:
        raise ValueError(f"Config {config_path} has no instruments entries.")

    cfg.setdefault("dispatch_log_dir", cfg["sbatch_log_dir"])
    cfg.setdefault("max_submissions_per_run", 50)
    cfg.setdefault("max_attempts", 3)
    cfg.setdefault("qc_pattern", DEFAULT_QC_PATTERN)
    cfg.setdefault("slurm", {})
    cfg["slurm"].setdefault("partition", "high")
    cfg["slurm"].setdefault("qos", "genome-center-grp-high-qos")
    cfg["slurm"].setdefault("account", "genome-center-grp")
    cfg["slurm"].setdefault("time", "06:00:00")
    cfg["slurm"].setdefault("cpus", 8)
    cfg["slurm"].setdefault("mem", "32G")

    return cfg


def _walk_raws(watch_dir: Path) -> list[Path]:
    """List candidate raws in a watch dir.

    Returns ``.d`` directories (Bruker) and ``.raw`` files (Thermo).
    Skips ``.partial`` / ``.tmp`` markers from in-flight uploads.
    Non-recursive by design — flat layout per instrument.
    """
    if not watch_dir.exists() or not watch_dir.is_dir():
        logger.warning("watch_dir missing or not a directory: %s", watch_dir)
        return []

    out: list[Path] = []
    for entry in sorted(watch_dir.iterdir()):
        name = entry.name
        if name.endswith(".partial") or name.endswith(".tmp"):
            continue
        if entry.is_dir() and entry.suffix.lower() == ".d":
            out.append(entry)
        elif entry.is_file() and entry.suffix.lower() == ".raw":
            out.append(entry)
    return out


def _matches_qc_pattern(name: str, pattern: str) -> bool:
    """Return True when `name` matches the QC filename regex (or pattern is empty)."""
    if not pattern:
        return True
    return re.search(pattern, name) is not None


def _already_processed(db_path: Path, raw_path: Path) -> bool:
    """True if the raw already has a row in `runs` (any instrument).

    Mirrors the `_row_exists` predicate in stan.pipeline.hive_process —
    we use raw_path as the cohort-independent key here so a misconfigured
    instrument name in a prior dispatch run doesn't trigger a duplicate
    submission. The instrument check happens later inside hive-process.
    """
    try:
        with sqlite3.connect(str(db_path)) as con:
            row = con.execute(
                "SELECT 1 FROM runs WHERE raw_path = ? LIMIT 1",
                (str(raw_path),),
            ).fetchone()
            return row is not None
    except sqlite3.OperationalError:
        return False  # fresh DB, no runs table yet — nothing processed


def _failed_too_many(db_path: Path, raw_path: Path, max_attempts: int) -> bool:
    """True when dispatch_attempts.attempt_count exceeds max_attempts AND
    the last status is ``failed``. Stops the cron from burning compute on
    a chronically broken raw.
    """
    try:
        with sqlite3.connect(str(db_path)) as con:
            row = con.execute(
                "SELECT status, attempt_count FROM dispatch_attempts "
                "WHERE raw_path = ? LIMIT 1",
                (str(raw_path),),
            ).fetchone()
            if not row:
                return False
            status, count = row[0], int(row[1] or 0)
            return status == "failed" and count >= max_attempts
    except sqlite3.OperationalError:
        return False


def _job_already_queued(raw_stem: str) -> bool:
    """Best-effort check: is a SLURM job for this raw already queued/running?

    Uses `squeue --me --name=stan-<stem> --noheader` — returns True if
    any line comes back. Defensive against double-submission if the cron
    re-fires while a previous batch is still draining. Skips silently
    when squeue isn't on PATH (e.g. dry-run from a dev box).
    """
    if not shutil.which("squeue"):
        return False
    try:
        result = subprocess.run(
            ["squeue", "--me", "--noheader", "--name", f"stan-{raw_stem}"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


def _shell_quote(value: str) -> str:
    """Wrap a value safely for a Bash heredoc-rendered argv list.

    sbatch scripts are written via heredoc with no parameter expansion,
    but the rendered argv ends up in a Bash command line that does
    re-tokenize. Use the stdlib quoting helper to be safe.
    """
    import shlex
    return shlex.quote(value)


def _render_sbatch(
    raw_path: Path,
    instrument: dict,
    cfg: dict,
) -> str:
    """Render the SLURM sbatch script for a single raw.

    Body invokes ``stan hive-process`` with all the cohort-keying args
    plumbed through. Standard env: load ``apptainer`` + dotnet modules
    so the search engines work, source the STAN venv so the CLI is on
    PATH. Apptainer/dotnet are required by run_one_v1.run_diann/run_sage.
    """
    slurm = cfg["slurm"]
    out_root = Path(cfg["out_root"])
    sbatch_log_dir = Path(cfg["sbatch_log_dir"])
    db_path = Path(cfg["db_path"])
    venv = Path(cfg["stan_venv"])

    raw_stem = raw_path.stem
    job_name = f"stan-{raw_stem}"
    out_dir = out_root / raw_stem

    # Argv for `stan hive-process`. Quoted so spaces / special chars
    # in instrument / family / column / paths can't fragment the line.
    argv = [
        f"{_shell_quote(str(venv))}/bin/stan", "hive-process",
        _shell_quote(str(raw_path)),
        "--instrument", _shell_quote(instrument["name"]),
        "--family", _shell_quote(instrument["family"]),
        "--vendor", _shell_quote(instrument["vendor"]),
        "--db", _shell_quote(str(db_path)),
        "--out-dir", _shell_quote(str(out_dir)),
    ]
    if instrument.get("column_vendor"):
        argv += ["--column-vendor", _shell_quote(instrument["column_vendor"])]
    if instrument.get("column_model"):
        argv += ["--column-model", _shell_quote(instrument["column_model"])]
    if instrument.get("amount_ng"):
        argv += ["--amount-ng", str(float(instrument["amount_ng"]))]
    if instrument.get("spd"):
        argv += ["--spd", str(int(instrument["spd"]))]

    cmd = " ".join(argv)

    # Ensure the SLURM stdout dir exists before the script runs (login
    # side, before sbatch). Cleaner to mkdir here than from inside the
    # job, since SLURM rejects scripts whose --output dir is missing.
    sbatch_log_dir.mkdir(parents=True, exist_ok=True)

    bruker_ff_dir = "/quobyte/proteomics-grp/brett/bruker_ff"
    return f"""#!/bin/bash
#SBATCH --job-name={job_name}
#SBATCH --partition={slurm['partition']}
#SBATCH --qos={slurm['qos']}
#SBATCH --account={slurm['account']}
#SBATCH --time={slurm['time']}
#SBATCH --cpus-per-task={slurm['cpus']}
#SBATCH --mem={slurm['mem']}
#SBATCH --output={sbatch_log_dir}/{raw_stem}_%j.out

set -euo pipefail

# Module env so apptainer + dotnet (TRFP runtime) are on PATH for the
# search engines invoked by stan.community.scripts.run_one_v1.
source /etc/profile.d/modules.sh 2>/dev/null || true
module load apptainer 2>/dev/null || true
module load dotnet-core-sdk/8.0.4 2>/dev/null || true

# 4DFF (Bruker uff-cmdline2) lives on shared Quobyte storage and
# needs LD_LIBRARY_PATH for libtbb.so.2 plus STAN_BRUKER_FF_DIR for
# stan.metrics.features._install_dir() to find it. Without these,
# is_4dff_installed() returns False and _run_4dff_inline silently
# skips the .features sidecar — exactly what bit the 2026-05-07
# timsTOF test (no ion cloud generated despite the .d being Bruker).
export STAN_BRUKER_FF_DIR={bruker_ff_dir}
export LD_LIBRARY_PATH={bruker_ff_dir}/linux:${{LD_LIBRARY_PATH:-}}

# STAN venv on shared Quobyte storage.
source {venv}/bin/activate

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] starting hive-process for {raw_path.name}"
{cmd}
echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] hive-process exit=$?"
"""


def _submit_sbatch(script_text: str, sbatch_log_dir: Path, raw_stem: str) -> str | None:
    """Write the script to disk and submit. Returns SLURM job id or None.

    Script lands under ``<sbatch_log_dir>/scripts/`` so it's auditable
    after the fact (--output captures stdout, but the script itself is
    sometimes useful when debugging an unexpected --bind or module issue).
    """
    scripts_dir = sbatch_log_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    script_path = scripts_dir / f"{raw_stem}.sbatch"
    script_path.write_text(script_text)
    script_path.chmod(0o755)

    try:
        result = subprocess.run(
            ["sbatch", str(script_path)],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except FileNotFoundError:
        logger.error("sbatch not on PATH — are we on Hive with module env loaded?")
        return None
    except Exception:
        logger.exception("sbatch invocation failed for %s", raw_stem)
        return None

    if result.returncode != 0:
        logger.error(
            "sbatch failed for %s (exit %d): stderr=%s",
            raw_stem, result.returncode, result.stderr.strip(),
        )
        return None

    # sbatch prints "Submitted batch job <id>" on stdout.
    m = re.search(r"\b(\d+)\b", result.stdout)
    if not m:
        logger.error("sbatch returned 0 but no job id parseable: %r", result.stdout)
        return None
    return m.group(1)


def dispatch_all(
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    dry_run: bool = False,
    instrument_filter: str = "",
    log: logging.Logger | None = None,
) -> dict:
    """Single-pass walk + submit for every instrument in the config.

    Args:
        config_path: Path to dispatch.yml.
        dry_run: When True, log what would be submitted but don't sbatch.
        instrument_filter: When set, only process instruments whose
            ``name`` matches this substring (case-insensitive). Useful
            for backfilling one instrument at a time.

    Returns a summary dict for the JSONL run log:
        {ts, dry_run, by_instrument: {...}, totals: {...}}
    """
    log = log or logger
    cfg = _load_config(config_path)
    db_path = Path(cfg["db_path"])
    max_subs = int(cfg["max_submissions_per_run"])
    max_attempts = int(cfg["max_attempts"])
    qc_pattern = cfg["qc_pattern"]
    sbatch_log_dir = Path(cfg["sbatch_log_dir"])

    summary: dict = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "dry_run": dry_run,
        "by_instrument": {},
        "totals": {
            "scanned": 0, "skipped_pattern": 0, "skipped_processed": 0,
            "skipped_in_flight": 0, "skipped_max_attempts": 0,
            "submitted": 0, "submit_failed": 0, "capped": 0,
        },
    }
    submitted_total = 0

    for inst in cfg["instruments"]:
        if instrument_filter and instrument_filter.lower() not in inst["name"].lower():
            continue

        watch_dir = Path(inst["watch_dir"])
        per_inst = {
            "name": inst["name"],
            "family": inst["family"],
            "watch_dir": str(watch_dir),
            "scanned": 0, "skipped_pattern": 0, "skipped_processed": 0,
            "skipped_in_flight": 0, "skipped_max_attempts": 0,
            "submitted": 0, "submit_failed": 0,
            "submissions": [],
        }

        raws = _walk_raws(watch_dir)
        per_inst["scanned"] = len(raws)
        summary["totals"]["scanned"] += len(raws)

        for raw in raws:
            if submitted_total >= max_subs:
                summary["totals"]["capped"] += 1
                break

            if not _matches_qc_pattern(raw.name, qc_pattern):
                per_inst["skipped_pattern"] += 1
                summary["totals"]["skipped_pattern"] += 1
                continue

            if _already_processed(db_path, raw):
                per_inst["skipped_processed"] += 1
                summary["totals"]["skipped_processed"] += 1
                continue

            if _failed_too_many(db_path, raw, max_attempts):
                per_inst["skipped_max_attempts"] += 1
                summary["totals"]["skipped_max_attempts"] += 1
                continue

            if _job_already_queued(raw.stem):
                per_inst["skipped_in_flight"] += 1
                summary["totals"]["skipped_in_flight"] += 1
                continue

            script = _render_sbatch(raw, inst, cfg)
            if dry_run:
                log.info("[dry-run] would submit %s (%s)", raw.name, inst["name"])
                per_inst["submitted"] += 1
                summary["totals"]["submitted"] += 1
                per_inst["submissions"].append(
                    {"raw": str(raw), "job_id": None, "dry_run": True}
                )
                submitted_total += 1
                continue

            job_id = _submit_sbatch(script, sbatch_log_dir, raw.stem)
            if job_id:
                log.info("submitted %s as SLURM job %s", raw.name, job_id)
                per_inst["submitted"] += 1
                summary["totals"]["submitted"] += 1
                per_inst["submissions"].append(
                    {"raw": str(raw), "job_id": job_id}
                )
                submitted_total += 1
            else:
                per_inst["submit_failed"] += 1
                summary["totals"]["submit_failed"] += 1
                per_inst["submissions"].append(
                    {"raw": str(raw), "job_id": None, "error": "sbatch failed"}
                )

        summary["by_instrument"][inst["name"]] = per_inst
        if submitted_total >= max_subs:
            log.warning(
                "max_submissions_per_run=%d reached — remaining raws "
                "will be picked up next cron tick",
                max_subs,
            )
            break

    # Append the summary line to the dispatch JSONL log.
    if not dry_run:
        log_dir = Path(cfg["dispatch_log_dir"])
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"dispatch_{datetime.now().strftime('%Y%m%d')}.jsonl"
        try:
            with log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(summary, default=str) + "\n")
        except OSError:
            log.exception("Failed to write dispatch summary to %s", log_path)

    return summary


def dispatch_one_raw(
    raw_path: Path,
    instrument: dict,
    config_path: Path = DEFAULT_CONFIG_PATH,
    *,
    dry_run: bool = False,
    partition: str = "",
    force: bool = False,
) -> dict:
    """Render + submit a single SLURM job for a specific raw file.

    Used by per-instrument self-submission: after the watcher SMB-
    uploads a file to Quobyte, it SSHes to Hive and calls this with
    the instrument metadata it already has, skipping the walk + filter
    work. Returns a dict the caller can JSON-parse:

        {status: 'submitted'|'skipped'|'failed', job_id: str|None,
         raw: str, error: str|None}

    Idempotency: ``_already_processed`` short-circuits if the global DB
    already has a row for this raw_path; ``_job_already_queued`` skips
    if a SLURM job for this stem is already running. Safe to call
    repeatedly (e.g. watcher restart re-attempts after a crash).
    """
    cfg = _load_config(config_path)
    db_path = Path(cfg["db_path"])
    sbatch_log_dir = Path(cfg["sbatch_log_dir"])

    # Optional partition override — used for partition-comparison
    # timing tests. Mutate a copy so the dispatch.yml on disk stays
    # untouched. ``low`` partition needs a different qos+account triple
    # per CLAUDE.md; map known partitions to their valid combos so the
    # operator doesn't have to hand-edit dispatch.yml just to time
    # ``low``.
    if partition:
        cfg = dict(cfg)
        cfg["slurm"] = dict(cfg.get("slurm") or {})
        cfg["slurm"]["partition"] = partition
        if partition == "low":
            cfg["slurm"]["qos"] = "publicgrp-low-qos"
            cfg["slurm"]["account"] = "publicgrp"
        elif partition == "high":
            cfg["slurm"]["qos"] = "genome-center-grp-high-qos"
            cfg["slurm"]["account"] = "genome-center-grp"

    out: dict = {
        "status": "failed",
        "job_id": None,
        "raw": str(raw_path),
        "partition": cfg["slurm"]["partition"],
        "error": None,
    }

    if not raw_path.exists():
        out["error"] = f"raw not on Hive yet: {raw_path}"
        return out

    if not force:
        if _already_processed(db_path, raw_path):
            out["status"] = "skipped"
            out["error"] = "row already in runs table (--force to override)"
            return out
        if _job_already_queued(raw_path.stem):
            out["status"] = "skipped"
            out["error"] = "SLURM job already queued/running (--force to override)"
            return out

    script = _render_sbatch(raw_path, instrument, cfg)
    if dry_run:
        out["status"] = "submitted"  # would-be
        return out

    job_id = _submit_sbatch(script, sbatch_log_dir, raw_path.stem)
    if job_id:
        out["status"] = "submitted"
        out["job_id"] = job_id
    else:
        out["error"] = "sbatch failed — see stderr"
    return out


def main() -> None:
    """CLI entry point for `stan hive-dispatch`."""
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    p.add_argument(
        "--dry-run", action="store_true",
        help="Walk + report what would be submitted, but don't sbatch.",
    )
    p.add_argument(
        "--instrument", default="",
        help="Substring filter on instrument name (case-insensitive). "
             "Useful for backfilling one instrument at a time.",
    )
    p.add_argument(
        "--print-default-config", action="store_true",
        help="Write a default dispatch.yml template to stdout and exit.",
    )
    p.add_argument(
        "--raw", default="",
        help="Submit a SLURM job for ONE specific raw on Hive, skipping "
             "the watch-dir walk. Pair with --instrument-name + --family "
             "+ --vendor. Used by the watcher's per-file self-submit.",
    )
    p.add_argument(
        "--instrument-name", default="",
        help="Required with --raw: canonical model name (e.g. 'timsTOF HT').",
    )
    p.add_argument("--family", default="", help="Required with --raw.")
    p.add_argument("--vendor", default="", help="Required with --raw.")
    p.add_argument("--column-vendor", default="")
    p.add_argument("--column-model", default="")
    p.add_argument(
        "--partition", default="",
        help="Override slurm.partition from dispatch.yml. With 'low' "
             "or 'high', the matching qos+account triple is auto-selected. "
             "Used by partition-comparison timing tests.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Bypass the already-processed and already-queued short-"
             "circuits. For timing tests + manual reprocess.",
    )
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    if args.print_default_config:
        sys.stdout.write(DEFAULT_CONFIG_TEMPLATE)
        return

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.raw:
        if not (args.instrument_name and args.family and args.vendor):
            print("ERROR: --raw requires --instrument-name + --family + --vendor",
                  file=sys.stderr)
            sys.exit(2)
        inst = {
            "name": args.instrument_name,
            "family": args.family,
            "vendor": args.vendor,
            "column_vendor": args.column_vendor,
            "column_model": args.column_model,
        }
        result = dispatch_one_raw(
            raw_path=Path(args.raw),
            instrument=inst,
            config_path=args.config,
            dry_run=args.dry_run,
            partition=args.partition,
            force=args.force,
        )
        print(json.dumps(result, default=str), flush=True)
        if result["status"] not in ("submitted", "skipped"):
            sys.exit(1)
        return

    summary = dispatch_all(
        config_path=args.config,
        dry_run=args.dry_run,
        instrument_filter=args.instrument,
    )

    totals = summary["totals"]
    logger.info(
        "Dispatch %s: scanned=%d submitted=%d skipped(processed=%d, "
        "pattern=%d, in_flight=%d, max_attempts=%d) failed=%d capped=%d",
        "[dry-run]" if args.dry_run else "",
        totals["scanned"], totals["submitted"],
        totals["skipped_processed"], totals["skipped_pattern"],
        totals["skipped_in_flight"], totals["skipped_max_attempts"],
        totals["submit_failed"], totals["capped"],
    )

    if totals["submit_failed"] > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
