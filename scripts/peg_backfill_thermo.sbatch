#!/bin/bash
#SBATCH --job-name=stan-peg-thermo
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --account=publicgrp
#SBATCH --array=0-7%8
#SBATCH --cpus-per-task=2
#SBATCH --mem=8G
#SBATCH --time=08:00:00
#SBATCH --requeue
#SBATCH --output=/quobyte/proteomics-grp/STAN/logs/peg_backfill_thermo_%A_%a.out
#SBATCH --error=/quobyte/proteomics-grp/STAN/logs/peg_backfill_thermo_%A_%a.err
#
# Score PEG for Thermo (Lumos / Exploris) QC runs whose PEG is still NULL in
# PG Farm: each .raw is converted to an MS1-only mzML by the
# ThermoRawFileParser container on node-local $TMPDIR, 80 strided MS1 scans
# are scored, and the result is written to runs.peg_* + peg_ion_hits.
#
# Canonical copy: scripts/ in the stan repo. Running copies of this file and
# of peg_backfill_thermo.py live in /quobyte/proteomics-grp/STAN/ -- never in
# /quobyte/proteomics-grp/brett/, whose stan/ checkout would shadow the
# installed package. The stan_venv checkout must include
# stan/metrics/peg_trfp.py (git pull there first).
#
# Running this is a deploy step that needs Brett's go-ahead. Test first:
#   sbatch --array=0 peg_backfill_thermo.sbatch --limit 5
# (one task => one shard; the .py gets any extra arguments), check the five
# rows in PG, then submit the full array:
#   sbatch peg_backfill_thermo.sbatch
#
# Bulk backfill -> `low` per the partition policy; live QC dispatch owns
# `high`. Preemption is expected: --requeue restarts the shard, and because
# the queue is "PEG IS NULL" (ion hits written before the scalars) a restart
# only redoes the run that was in flight. 8 shards, not more: every shard
# holds a PG Farm connection, and those slots are shared with FRAN.
#
# Sizing, from SLURM job 24179541 (2026-09-28): one 1.1 GB Exploris .raw took
# 25-35 s end to end (conversion 34 s) with a 1.3 GB peak RSS. 2,924 runs is
# ~24 CPU-hours serial, ~3 h per shard at 8 shards; 8 h leaves room for the
# 120-min gradients and a slow NFS day. 8G covers bigger raws than that one.
set -uo pipefail

# cron-style environments lack LOGNAME/USER and /etc/profile.d/modules.sh
# dereferences them; under `set -u` that kills the shell silently (the trap
# that kept the Flinders dispatch cron from ever firing, Jun-Aug 2026). Seed
# both and drop -u across the sourcing, as cron_flinders_dispatch.sh does.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
# apptainer is /usr/bin/apptainer on the compute nodes already (checked
# 2026-09-28); the module is loaded only in case that ever changes.
module load apptainer 2>/dev/null || true
set -u

export STAN_DB_BACKEND=pg
export PGPASSWORD="$(cat /quobyte/proteomics-grp/brett/.pgfarm_token)"
export STAN_TRFP_SIF="${STAN_TRFP_SIF:-/quobyte/proteomics-grp/STAN/historical_bsa/trfp.sif}"

PY=/quobyte/proteomics-grp/brett/stan_venv/bin/python
SHARD=$(( ${SLURM_ARRAY_TASK_ID:-0} - ${SLURM_ARRAY_TASK_MIN:-0} ))
NSHARDS=${SLURM_ARRAY_TASK_COUNT:-1}

echo "===== peg_backfill_thermo $(date '+%F %T') on $(hostname)" \
     "shard=${SHARD}/${NSHARDS} TMPDIR=${TMPDIR:-unset} args=$* ====="
if [ ! -f "$STAN_TRFP_SIF" ]; then
    echo "ERROR: ThermoRawFileParser image missing: $STAN_TRFP_SIF" >&2
    exit 2
fi

cd /quobyte/proteomics-grp/STAN || exit 1
"$PY" peg_backfill_thermo.py --shard "$SHARD" --nshards "$NSHARDS" "$@"
rc=$?
echo "===== exit rc=$rc $(date '+%F %T') ====="
exit $rc
