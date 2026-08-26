#!/bin/bash
# Submit the acquisition counter, at most one at a time.
#
# Login-node-safe: this only calls sbatch. All walking happens inside SLURM.
# Pass --full for the complete rebuild; default is the cheap incremental.
set -uo pipefail
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

JOB=stan_count_acq
LOG=/quobyte/proteomics-grp/STAN/logs/count_acq_submit.log
SBATCH=/quobyte/proteomics-grp/STAN/count_acquisitions.sbatch
ME=$(id -un)

if ! command -v sbatch >/dev/null 2>&1; then
  echo "$(date '+%F %T') ABORT: sbatch not on PATH" >> "$LOG"; exit 1
fi
# Fail CLOSED: if the queue can't be read, assume one is queued rather than stack.
if ! q=$(squeue -h -u "$ME" -n "$JOB" -t PENDING,RUNNING 2>/dev/null); then
  echo "$(date '+%F %T') ABORT: squeue failed" >> "$LOG"; exit 1
fi
if [ "$(printf '%s' "$q" | grep -c .)" -gt 0 ]; then
  echo "$(date '+%F %T') skip: counter already queued/running" >> "$LOG"; exit 0
fi

if [ "${1:-}" = "--full" ]; then
  out=$(sbatch "$SBATCH" --days 400 2>&1)
else
  out=$(sbatch "$SBATCH" --merge --days 3 2>&1)
fi
echo "$(date '+%F %T') ${1:-incremental} $out" >> "$LOG"
