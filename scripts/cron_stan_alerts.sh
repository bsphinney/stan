#!/bin/bash
# cron_stan_alerts.sh -- the feed watchdog, deliberately OUTSIDE the feeds.
#
# Install (every 20 minutes):
#
#   */20 * * * * flock -n /tmp/stan_alerts.lock \
#       bash /quobyte/proteomics-grp/STAN/cron_stan_alerts.sh
#
# NOTE THE `bash` IN THAT LINE. It is not decoration. On 2026-09-09
# cron_evosep.sh lost its execute bit and cron answered "Permission denied"
# into nothing -- no log, no error, no alert -- for eight days. Invoking
# through bash makes the execute bit irrelevant, so the watchdog cannot die
# the same way the thing it watches died.
#
# WHY THIS EXISTS AT ALL. The alerting used to live inside cron_evosep.sh,
# which meant the alarm was wired to the thing it was supposed to alarm
# about. When that script stopped, the alerting stopped with it and the
# silence looked exactly like "nothing is wrong". A watchdog has to be able
# to outlive its subject.
#
# AND WHY IT PASSES NO --evosep-json. cron_evosep.sh hands instrument-watch
# a freshly generated extract, which is right for clog alerting -- it wants
# the newest pressure trace -- and wrong for staleness, because a fresh
# document always looks fresh. This runs with no arguments so it reads the
# PUBLISHED documents, which is the only place a stalled publish is visible.
# The two are complementary; cron_evosep.sh keeps its own call.
set -uo pipefail

# cron sets neither LOGNAME nor USER, and /etc/profile.d/modules.sh
# dereferences LOGNAME unconditionally; under `set -u` that kills the script
# before the log block below, silently. That is what hid the Flinders cron
# for eleven weeks in 2026.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

VENV=/quobyte/proteomics-grp/brett/stan_venv
LOGDIR=/quobyte/proteomics-grp/STAN/logs
LOG="$LOGDIR/cron_stan_alerts_$(date +%Y%m%d).log"
mkdir -p "$LOGDIR" 2>/dev/null

export STAN_DB_BACKEND=pg
export STAN_PGFARM_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token

{
  echo "===== alerts tick $(date '+%F %T') on $(hostname) ====="
  if [ ! -x "$VENV/bin/stan" ]; then
    echo "ABORT: $VENV/bin/stan is not executable"
    exit 1
  fi
  "$VENV/bin/stan" instrument-watch 2>&1 | tail -30
  echo "----- exit=${PIPESTATUS[0]} $(date '+%F %T')"
  echo
} >> "$LOG" 2>&1
