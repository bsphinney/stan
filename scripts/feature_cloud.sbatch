#!/bin/bash
#SBATCH --job-name=stan-ioncloud
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --account=publicgrp
#SBATCH --array=0-3%4
#SBATCH --cpus-per-task=1
#SBATCH --mem=4G
#SBATCH --time=8:00:00
#SBATCH --requeue
#SBATCH --output=/quobyte/proteomics-grp/STAN/logs/ioncloud_%A_%a.out
#SBATCH --error=/quobyte/proteomics-grp/STAN/logs/ioncloud_%A_%a.err
#
# Extract charge-labeled 4DFF ion clouds from the .features sidecars and
# publish them (JSON cache on quobyte + PG when the table exists) so the
# dashboard's Ion cloud tab works on hosts that cannot see the raw data.
#
# Bulk backfill -> `low` per the partition policy; live QC dispatch owns
# `high`. 4 shards, not 24: PG Farm's connection slots are shared with FRAN.
set -uo pipefail

export STAN_DB_BACKEND=pg
export PGPASSWORD="$(cat /quobyte/proteomics-grp/brett/.pgfarm_token)"

PY=/quobyte/proteomics-grp/brett/stan_venv/bin/python
cd /quobyte/proteomics-grp/STAN || exit 1

"$PY" feature_cloud_backfill.py \
    --shard "${SLURM_ARRAY_TASK_ID}" \
    --nshards 4 \
    --max-points 5000
