#!/bin/bash
#SBATCH --job-name=stan_count_acq
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --account=publicgrp
#SBATCH --time=01:00:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --output=/quobyte/proteomics-grp/STAN/logs/count_acq_%j.out
#SBATCH --requeue
#
# Count instrument acquisitions for the dashboard's Samples tab.
#
# Runs INSIDE SLURM, never on a login node: the full archive walk touches
# ~24k paths and takes ~11 minutes, which is exactly the kind of thing that
# gets flagged on login1/login2 (CLAUDE.md rule 1).
#
#   sbatch count_acquisitions.sbatch            # full rebuild (~11 min)
#   sbatch count_acquisitions.sbatch --merge --days 3   # incremental (~seconds)
#
# The incremental form only descends into month directories touched recently
# and merges into the existing JSON, so it is cheap enough to run often and
# keeps the dashboard near-real-time.
set -uo pipefail

# cron does not set LOGNAME, and /etc/profile.d/modules.sh dereferences it;
# under `set -u` that would kill this script silently. Same trap that kept
# the Flinders dispatch cron from ever firing between June and August 2026.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

PY=/quobyte/proteomics-grp/brett/stan_venv/bin/python
SCRIPT=/quobyte/proteomics-grp/brett/count_acquisitions.py
OUT=/quobyte/proteomics-grp/STAN/utilization.json

echo "===== count_acquisitions $(date '+%F %T') on $(hostname) args=$* ====="
"$PY" "$SCRIPT" --out "$OUT" "$@"
rc=$?
echo "===== exit rc=$rc $(date '+%F %T') ====="
exit $rc
