#!/bin/bash
#SBATCH --job-name=stan-4dffcloud
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --account=publicgrp
#SBATCH --array=0-2%3
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=12:00:00
#SBATCH --requeue
#SBATCH --output=/quobyte/proteomics-grp/STAN/logs/ioncloud4dff_%A_%a.out
#SBATCH --error=/quobyte/proteomics-grp/STAN/logs/ioncloud4dff_%A_%a.err
#
# Generate the missing 4DFF .features sidecars for recent Bruker runs and
# publish their ion clouds. Bulk work -> `low`; 3 shards keeps both the
# CPU footprint and the PG connection count small (PG Farm is shared
# with FRAN).
set -uo pipefail

export STAN_DB_BACKEND=pg
export PGPASSWORD="$(cat /quobyte/proteomics-grp/brett/.pgfarm_token)"
export STAN_BRUKER_FF_DIR=/quobyte/proteomics-grp/brett/bruker_ff

PY=/quobyte/proteomics-grp/brett/stan_venv/bin/python
cd /quobyte/proteomics-grp/STAN || exit 1

"$PY" feature_cloud_4dff.py \
    --shard "${SLURM_ARRAY_TASK_ID}" \
    --nshards 3 \
    --days 90
