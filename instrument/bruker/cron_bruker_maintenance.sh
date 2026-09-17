#!/bin/bash
# =============================================================================
# cron_bruker_maintenance.sh   (SUGGESTED -- do not install without review)
#
# Refresh the Bruker maintenance cache the STAN dashboard serves.
# Login-node-safe: runs the pinned postgres apptainer directly (~40s, read-only),
# no SLURM needed. Mirrors the style of the other /quobyte/proteomics-grp/STAN
# cron_*.sh scripts.
#
# Suggested crontab entry (every 30 min, single-flighted with flock):
#   */30 * * * * flock -n /tmp/stan_bruker_maint.lock /quobyte/proteomics-grp/STAN/cron_bruker_maintenance.sh
# =============================================================================
set -uo pipefail

EXTRACTOR=/quobyte/proteomics-grp/brett/HT_bruker_scratch/extract_bruker.sh   # or wherever you deploy HT_work/bruker/
OUT_CANON=/quobyte/proteomics-grp/STAN/bruker_maintenance.json                # served cache (next to acq_date_cache.json)
LOG=/quobyte/proteomics-grp/STAN/logs/bruker_maint.log
TMP="$(mktemp /tmp/bruker_maint_XXXXXX.json)"
trap 'rm -f "$TMP"' EXIT

mkdir -p "$(dirname "$LOG")"

# Extract to a temp file first so a half-written cache never reaches the dashboard.
if "$EXTRACTOR" --out "$TMP" >>"$LOG" 2>&1 && [ -s "$TMP" ]; then
  # sanity: must be JSON with a summary block before we publish it
  if grep -q '"summary"' "$TMP"; then
    mv -f "$TMP" "$OUT_CANON"
    echo "$(date '+%F %T') OK -> $OUT_CANON ($(wc -c <"$OUT_CANON") bytes)" >>"$LOG"
  else
    echo "$(date '+%F %T') ABORT: output missing summary block" >>"$LOG"; exit 1
  fi
else
  echo "$(date '+%F %T') ABORT: extractor failed or produced no output" >>"$LOG"; exit 1
fi

# --- Delivery to the dashboard host ---------------------------------------
# The line above publishes the cache into the STAN dir on Hive. If the
# dashboard that serves the Maintenance tab runs ON Hive, resolve_config_path
# finds it there and you are done. If the dashboard runs elsewhere (the hosted
# PG-Farm dashboard), sync the file to that host's ~/STAN/ dir, e.g.:
#   rsync -az "$OUT_CANON" dashboardhost:STAN/bruker_maintenance.json
# (add such a line here once the target host is known).
