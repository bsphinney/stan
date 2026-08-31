#!/bin/bash
# STAN Hive cron: watch active high-throughput plates, email on trouble.
#
#   */20 * * * * flock -n /tmp/stan_ht_watch.lock \
#       /quobyte/proteomics-grp/STAN/cron_ht_watch.sh
#
# Every 20 min. Faster than that buys nothing: the stall threshold is 3 h,
# and each condition emails once per submission, so a tighter loop would
# only re-check the same quiet plates.
#
# Login-node safe: PG reads plus at most one HTTPS POST to Resend. No
# raw-file IO, no search, no meaningful CPU.
set -uo pipefail

# cron sets neither LOGNAME nor USER, and /etc/profile.d/modules.sh
# dereferences LOGNAME unconditionally; under `set -u` that kills the script
# before its log redirect exists. The failure that kept the Flinders cron
# silent from 2026-06-10 to 2026-08-26.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

VENV=/quobyte/proteomics-grp/brett/stan_venv
LOG=/quobyte/proteomics-grp/STAN/logs/cron_ht_watch_$(date +%Y%m%d).log

export STAN_DB_BACKEND=pg
if [ -r /quobyte/proteomics-grp/brett/.pgfarm_token ]; then
    export PGPASSWORD="$(cat /quobyte/proteomics-grp/brett/.pgfarm_token)"
fi

{
  echo "===== ht-watch $(date '+%F %T') ====="
  "$VENV/bin/stan" ht-watch 2>&1 | tail -30
  echo "----- exit=${PIPESTATUS[0]} $(date '+%F %T')"
  echo
} >> "$LOG" 2>&1
