#!/bin/bash
# STAN Hive cron: publish charge-labeled 4DFF ion clouds for any new run.
#
# NOT INSTALLED — add this line to brettsp's crontab to enable:
#   17 * * * * flock -n /tmp/stan_ioncloud.lock /quobyte/proteomics-grp/STAN/cron_ioncloud.sh
#
# Why a cron rather than the ingest pipeline: STAN v1.0.16 publishes the
# cloud inline right after 4DFF (stan/pipeline/hive_process.py
# _run_4dff_inline, stan/watcher/daemon.py), but the Hive checkout at
# /quobyte/proteomics-grp/brett/stan is a patched fork that is
# deliberately never pulled, so it will not pick that up. Until the fork
# is reconciled, this tick covers the gap.
#
# Cheap and idempotent: runs already in feature_clouds are skipped, so a
# tick with nothing new costs one PG query per shard (~80 s wall for a
# full 1,600-run scan). Login-node-safe — it only calls sbatch.
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
echo "$(date -Is) submitting feature_cloud.sbatch" >> "$LOG"
sbatch feature_cloud.sbatch >> "$LOG" 2>&1
