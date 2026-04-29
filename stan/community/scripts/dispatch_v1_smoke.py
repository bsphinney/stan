"""Dispatch SLURM jobs for the v1.0 community smoke test.

Reads /tmp/v1_sample.json (output of sample_backlog.py), generates a
per-file SLURM script, sbatchs each one. Runs ONCE on a Hive head
node (or anywhere with sbatch on PATH).

USAGE
    python -m stan.community.scripts.dispatch_v1_smoke \
        --manifest /tmp/v1_sample.json \
        --max-jobs 30   # smoke first; bump to 300 for full

Output
- One SLURM script per file at /tmp/v1_smoke_jobs/<idx>.sh
- One sbatch submission per script
- /tmp/v1_smoke_dispatch.jsonl with submitted job IDs
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

JOB_DIR = Path("/tmp/v1_smoke_jobs")
RESULT_BASE = Path("/quobyte/proteomics-grp/brett/v1_smoke")
DISPATCH_LOG = Path("/tmp/v1_smoke_dispatch.jsonl")

# Where the latest STAN code lives on Hive (must be pip-installed
# into a venv that sbatch can `source` from inside the job).
STAN_VENV = "/quobyte/proteomics-grp/brett/stan/.venv"


SLURM_TEMPLATE = """#!/bin/bash
#SBATCH --partition={partition}
#SBATCH --qos={qos}
#SBATCH --account={account}
#SBATCH --requeue
#SBATCH --time=02:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --job-name=stan-v1-{run_id}
#SBATCH --output=/tmp/v1_smoke_jobs/%j.out

set -euo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true

# Activate STAN venv (must exist; one-time setup on Hive)
source {stan_venv}/bin/activate

python -m stan.community.scripts.run_one_v1 \\
    --raw '{raw}' \\
    --mode {mode} \\
    --vendor {vendor} \\
    --family {family} \\
    --out-dir '{out_dir}'
"""


def _vendor_for(family: str) -> str:
    return "bruker" if family == "timsTOF" else "thermo"


def _normalise_mode(mode: str) -> str:
    """Treat 'unknown' as 'dia' (dominant mode at Brett's lab)."""
    return "dia" if mode == "unknown" else mode


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True, type=Path,
                   help="Sampler output JSON (see sample_backlog.py).")
    p.add_argument("--max-jobs", type=int, default=10,
                   help="Cap dispatch (default 10 for smoke; bump to 300 for full).")
    p.add_argument("--family", action="append",
                   choices=["timsTOF", "Lumos", "Exploris"],
                   help="Restrict to one or more families.")
    p.add_argument("--mode", action="append",
                   choices=["dia", "dda"],
                   help="Restrict to one or more modes.")
    p.add_argument("--dry-run", action="store_true",
                   help="Generate scripts but don't sbatch.")
    p.add_argument("--partition", default="high",
                   help="SLURM partition (default high; use low for big batches).")
    p.add_argument("--qos", default="genome-center-grp-high-qos",
                   help="SLURM QOS for the chosen partition.")
    p.add_argument("--account", default="genome-center-grp",
                   help="SLURM account (must pair with the chosen QOS).")
    args = p.parse_args()

    manifest = json.loads(args.manifest.read_text())
    files = manifest["files"]

    families_filter = set(args.family) if args.family else None
    modes_filter = set(args.mode) if args.mode else None

    JOB_DIR.mkdir(parents=True, exist_ok=True)

    submitted: list[dict] = []
    skipped = 0

    for i, f in enumerate(files):
        if args.max_jobs and len(submitted) >= args.max_jobs:
            break

        family = f["family"]
        if families_filter and family not in families_filter:
            skipped += 1
            continue

        mode = _normalise_mode(f["mode"])
        if modes_filter and mode not in modes_filter:
            skipped += 1
            continue

        raw = Path(f["path"])
        run_id = raw.stem.replace(".", "_")[:40]
        out_dir = RESULT_BASE / run_id
        vendor = _vendor_for(family)

        script = SLURM_TEMPLATE.format(
            run_id=run_id,
            stan_venv=STAN_VENV,
            raw=raw,
            mode=mode,
            vendor=vendor,
            family=family,
            out_dir=out_dir,
            partition=args.partition,
            qos=args.qos,
            account=args.account,
        )
        script_path = JOB_DIR / f"{i:04d}_{run_id}.sh"
        script_path.write_text(script)

        if args.dry_run:
            submitted.append({
                "idx": i, "raw": str(raw), "script": str(script_path),
                "job_id": "DRY_RUN", "family": family, "mode": mode,
            })
            continue

        result = subprocess.run(
            ["sbatch", str(script_path)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            logger.error("sbatch failed for %s: %s", raw.name, result.stderr.strip())
            submitted.append({
                "idx": i, "raw": str(raw), "script": str(script_path),
                "job_id": None, "family": family, "mode": mode,
                "error": result.stderr.strip(),
            })
            continue

        # sbatch prints "Submitted batch job 12345"
        job_id = result.stdout.strip().rsplit(maxsplit=1)[-1]
        submitted.append({
            "idx": i, "raw": str(raw), "script": str(script_path),
            "job_id": job_id, "family": family, "mode": mode,
        })
        logger.info("Submitted %s -> job %s", raw.name, job_id)

    with DISPATCH_LOG.open("w", encoding="utf-8") as fh:
        for r in submitted:
            fh.write(json.dumps(r) + "\n")

    by_family: dict[str, int] = {}
    by_mode: dict[str, int] = {}
    for r in submitted:
        by_family[r["family"]] = by_family.get(r["family"], 0) + 1
        by_mode[r["mode"]] = by_mode.get(r["mode"], 0) + 1

    logger.info(
        "Dispatch complete: %d jobs submitted, %d skipped. By family: %s. By mode: %s. Log: %s",
        len(submitted), skipped, by_family, by_mode, DISPATCH_LOG,
    )


if __name__ == "__main__":
    main()
