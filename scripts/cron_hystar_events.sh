#!/bin/bash
# cron_hystar_events.sh -- Hive's half of the HyStar watcher on TIMS-10878.
#
# Install (every minute):
#
#   * * * * * flock -n /tmp/stan_hystar_events.lock bash /quobyte/proteomics-grp/STAN/cron_hystar_events.sh
#
# Invoked through `bash`, like cron_stan_alerts.sh, so a lost execute bit
# cannot silence it (cron_evosep.sh, 2026-09-09: eight days of nothing).
#
# WHAT IT DOES. The watcher on the instrument PC posts a stopped queue to
# Slack itself and always drops an event JSON plus heartbeat.json in
# /quobyte/proteomics-grp/STAN/hystar_events/TIMS-10878/. Each tick
# `stan hystar-events` (stan/reports/hystar_events.py):
#   * posts any event the PC could not post (posted_by_pc: false);
#   * records each stop's keys in alert_state, so the hourly Evosep and
#     Bruker alerts stay quiet about the same injection;
#   * pages once if heartbeat.json goes 10 min without an update, unless a
#     pause_until.json says the downtime is planned.
#
# LOGIN-NODE COST. This runs 1,440 times a day, so an idle tick starts no
# Python: one `find -maxdepth 1` on a small folder and one `stat`, then a
# single "idle" line. Python starts only when there is something to do: an
# event file waiting, a heartbeat older than 10 min, or a watcher-down page
# whose recovery has not been announced (.down_alerted). A missing folder or
# a missing heartbeat means the watcher is not installed yet, also idle.
# Even the idle line matters: CRON_LOGS watches this log's mtime (30 min).
#
# The PC writes the event files; Hive only renames them into processed/ or
# rejected/, so each file on Quobyte keeps a single writer.
set -uo pipefail

# cron sets neither LOGNAME nor USER, and /etc/profile.d/modules.sh
# dereferences them (and MANPATH); under `set -u` that kills the script
# before it can log. Seed them first and drop -u across the sourcing.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

# Overridable only so tests/test_hystar_events.py can drive the fast path;
# cron sets none of these.
ROOT="${STAN_HYSTAR_EVENT_ROOT:-/quobyte/proteomics-grp/STAN/hystar_events}"
LOGDIR="${STAN_CRON_LOG_DIR:-/quobyte/proteomics-grp/STAN/logs}"
VENV="${STAN_VENV:-/quobyte/proteomics-grp/brett/stan_venv}"
HOST_DIR="$ROOT/TIMS-10878"
DOWN_AFTER_S=600
LOG="$LOGDIR/cron_hystar_events_$(date +%Y%m%d).log"
mkdir -p "$LOGDIR" 2>/dev/null

export STAN_DB_BACKEND=pg
export STAN_PGFARM_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
# One alert-memory fallback file for every alert cron (spec §4.5).
export STAN_ALERT_STATE=/quobyte/proteomics-grp/STAN/alert_state.json
if [ -r /quobyte/proteomics-grp/brett/.stan_slack_webhook ]; then
    export STAN_SLACK_WEBHOOK="$(cat /quobyte/proteomics-grp/brett/.stan_slack_webhook)"
fi
if [ -r /quobyte/proteomics-grp/brett/.stan_slack_mention ]; then
    export STAN_SLACK_MENTION="$(cat /quobyte/proteomics-grp/brett/.stan_slack_mention)"
fi

# GNU stat on Hive; the BSD form only so the test runs on a Mac.
mtime() { stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null; }

{
  if [ ! -d "$HOST_DIR" ]; then
    echo "$(date +%H:%M:%S) idle (no $HOST_DIR yet: watcher not installed)"
    exit 0
  fi
  waiting="$(find "$HOST_DIR" -maxdepth 1 -name '*.json' ! -name heartbeat.json \
               ! -name pause_until.json -print -quit 2>/dev/null)"
  hb_fresh=1   # no heartbeat at all: not installed yet, nothing to page about
  if [ -e "$HOST_DIR/heartbeat.json" ]; then
    hb="$(mtime "$HOST_DIR/heartbeat.json")"
    if [ -z "$hb" ] || [ $(( $(date +%s) - hb )) -ge "$DOWN_AFTER_S" ]; then
      hb_fresh=0
    fi
  fi
  if [ -z "$waiting" ] && [ "$hb_fresh" = 1 ] && [ ! -e "$HOST_DIR/.down_alerted" ]; then
    echo "$(date +%H:%M:%S) idle"
    exit 0
  fi

  echo "===== hystar tick $(date '+%F %T') on $(hostname) ====="
  if [ ! -x "$VENV/bin/stan" ]; then
    echo "ABORT: $VENV/bin/stan is not executable"
    exit 1
  fi
  "$VENV/bin/stan" hystar-events --root "$ROOT" 2>&1 | tail -40
  echo "----- exit=${PIPESTATUS[0]} $(date '+%F %T')"
} >> "$LOG" 2>&1
