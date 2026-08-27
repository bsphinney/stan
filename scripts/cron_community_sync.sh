#!/bin/bash
# STAN Hive cron: publish new QC runs to the community benchmark.
#
#   40 */6 * * * flock -n /tmp/stan_community_sync.lock \
#       /quobyte/proteomics-grp/STAN/cron_community_sync.sh
#
# Why this can run on Hive at all: the compute nodes' egress to
# *.hf.space is unreliable, which is why submissions used to be pushed
# by hand from the Mac. The LOGIN node reaches both the Space and the
# custom domain fine (verified 2026-08-26), and `stan submit-all` is
# HTTP POSTs against PG-resident rows -- no raw-file reading, no search,
# no meaningful CPU. That keeps it inside the "never compute on the
# login node" rule while removing the manual step.
#
# Idempotent: rows are flagged submitted_to_benchmark=1 on success, so a
# tick with nothing new costs one PG query.
set -uo pipefail

# cron sets neither LOGNAME nor USER, and /etc/profile.d/modules.sh
# dereferences LOGNAME unconditionally. Under `set -u` that kills the
# script before it can log -- the failure that made the Flinders
# dispatch cron silent from 2026-06-10 to 2026-08-26.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

VENV=/quobyte/proteomics-grp/brett/stan_venv
LOG=/quobyte/proteomics-grp/STAN/logs/cron_community_sync_$(date +%Y%m%d).log

export STAN_DB_BACKEND=pg
if [ -r /quobyte/proteomics-grp/brett/.pgfarm_token ]; then
    export PGPASSWORD=$(cat /quobyte/proteomics-grp/brett/.pgfarm_token)
fi

# No date scoping: `submit-all --backend pg` already selects only
#   WHERE submitted_to_benchmark = 0 OR submitted_to_benchmark IS NULL
# and, on success, does its own UPDATE ... SET submitted_to_benchmark = 1
# straight against PG. So each tick pushes exactly what is new and a tick
# with nothing new costs one query. (The Hive checkout predates the
# --since flag; it would be a no-op scoping hint here anyway.)
{
  echo "===== community sync $(date '+%F %T') ====="
  "$VENV/bin/stan" submit-all --backend pg 2>&1 | tail -6
  echo "----- exit=${PIPESTATUS[0]} done $(date '+%F %T')"
  echo
} >> "$LOG" 2>&1
