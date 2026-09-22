#!/bin/bash
# STAN Hive cron: publish charge-labeled 4DFF ion clouds for any new run.
#
# Installed in brettsp's crontab (verified 2026-09-22):
#   17 * * * * flock -n /tmp/stan_ioncloud.lock /quobyte/proteomics-grp/STAN/cron_ioncloud.sh
#
# Why a cron rather than the ingest pipeline: STAN v1.0.16 publishes the
# cloud inline right after 4DFF (stan/pipeline/hive_process.py
# _run_4dff_inline, stan/watcher/daemon.py), but this tick predates the Hive
# checkout tracking main, and it also catches a sidecar that appears after
# its run was ingested.
#
# Cheap and idempotent. Each shard asks PG only for runs in its own shard
# that still lack a cloud (feature_cloud_backfill.build_runs_query), so a
# tick with nothing new moves ~200 KB across all 4 shards, nearly all of it
# the ~1,100 `.d` runs that have no sidecar yet. Before 2026-09-22 every
# shard pulled every `.d` run and every stored cloud key: ~1.36 MB a tick
# from a database that bills per byte. Login-node-safe — it only calls sbatch.
set -uo pipefail

# cron sets neither LOGNAME nor USER, and /etc/profile.d/modules.sh
# dereferences LOGNAME unconditionally. Under `set -u` that aborts the
# script before any logging happens — the exact failure that made the
# Flinders dispatch cron silent from 2026-06-10 to 2026-08-26.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"

set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

LOG=/quobyte/proteomics-grp/STAN/logs/cron_ioncloud.log
cd /quobyte/proteomics-grp/STAN || exit 1

# Anti-stacking. flock only stops two TICKS overlapping; sbatch returns
# immediately, so without this a backed-up `low` partition would queue one
# job per hour and they would all start together and rescan the same runs.
# `id -un` rather than $USER: cron does not set it. Fail CLOSED — if squeue
# cannot be read, assume something is queued rather than piling on.
#
# (Added on Hive by patch_ioncloud_cron.py on 2026-08-26 and only brought
# into the repo on 2026-09-22; until then a deploy from here would have
# silently removed it.) Every branch writes a line to $LOG: the heartbeat
# in stan.reports.instrument_watch.CRON_LOGS judges this job by that file's
# mtime, so a quiet skip must still be a written one.
JOB=$(grep -m1 -oE 'job-name=[A-Za-z0-9_.-]+' feature_cloud.sbatch | cut -d= -f2)
JOB=${JOB:-stan_feature_cloud}
ME=$(id -un)
if ! command -v squeue >/dev/null 2>&1; then
  echo "$(date -Is) ABORT: squeue not on PATH" >> "$LOG"; exit 1
fi
if ! q=$(squeue -h -u "$ME" -n "$JOB" -t PENDING,RUNNING 2>/dev/null); then
  echo "$(date -Is) ABORT: squeue failed; refusing to submit blind" >> "$LOG"; exit 1
fi
if [ "$(printf '%s' "$q" | grep -c .)" -gt 0 ]; then
  echo "$(date -Is) skip: $JOB already queued/running" >> "$LOG"; exit 0
fi

echo "$(date -Is) submitting feature_cloud.sbatch (job=$JOB)" >> "$LOG"
sbatch feature_cloud.sbatch >> "$LOG" 2>&1
