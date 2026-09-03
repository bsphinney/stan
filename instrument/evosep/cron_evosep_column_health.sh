#!/bin/bash
# STAN Hive cron: refresh Evosep One column-health signals from the mirrored
# instrument pressure logs on Quobyte, and publish them to PG Farm for the
# hosted dashboard.
#
# SUGGESTED crontab line -- NOT installed by this script:
#   15 * * * * flock -n /tmp/stan_evosep_health.lock /quobyte/proteomics-grp/STAN/cron_evosep_column_health.sh
#
# Hourly rather than nightly: the value of this feature is catching a rising
# column before the next plate is burned, and the extract is ~1 min for a
# fortnight of runs. flock means a slow run is skipped, never stacked.
#
# Read-only on all instrument data. The extractor never writes under the log
# root; this script only writes its own output JSON and log.
set -uo pipefail
export LOGNAME="${LOGNAME:-$(id -un)}"; export USER="${USER:-$LOGNAME}"
set +u; source /etc/profile.d/modules.sh 2>/dev/null || true; source /etc/profile.d/hpccf.sh 2>/dev/null || true; set -u

EV=/quobyte/proteomics-grp/STAN/evosep
LOGS=/quobyte/proteomics-grp/brett/evosep_logs
OUT=/quobyte/proteomics-grp/STAN/evosep_column_health.json
mkdir -p /quobyte/proteomics-grp/STAN/logs
LOG=/quobyte/proteomics-grp/STAN/logs/cron_evosep_health_$(date +%Y%m%d).log

# The full instrument history (2023 -> now) is several GB. Window the extract
# so a routine tick stays fast and bounded; widen or drop --since to rebuild
# the whole record on demand.
SINCE=$(date -d '120 days ago' +%Y-%m-%d 2>/dev/null || date -v-120d +%Y-%m-%d)

{
  echo "===== tick $(date '+%Y-%m-%d %H:%M:%S') ====="
  if [ ! -d "$LOGS" ]; then echo "no Evosep log mirror at $LOGS; skipping"; exit 0; fi
  echo "extracting from $LOGS since $SINCE"

  tmp=$(mktemp "${OUT}.XXXX")
  if python3 "$EV/extract_evosep.py" --root "$LOGS" --since "$SINCE" --out "$tmp" 2>&1; then
    # Sanity-check before publishing: a truncated or empty document must not
    # replace a good one.
    if ! python3 -c "import json,sys; d=json.load(open(sys.argv[1])); \
sys.exit(0 if d.get('summary',{}).get('n_runs',0) > 0 else 1)" "$tmp"; then
      echo 'extract produced no runs; keeping previous JSON'; rm -f "$tmp"; exit 1
    fi
    mv -f "$tmp" "$OUT"
    echo "published -> $OUT ($(stat -c%s "$OUT") bytes)"

    # Push into PG Farm so the HOSTED dashboard serves it. The endpoint reads
    # PG first and falls back to the bundled file, so this is what turns the
    # panel from deploy-frozen into hourly-fresh. DDL-free: the table is owned
    # by brettsp via migration, this account has DML only.
    export STAN_DB_BACKEND=pg
    export PATH=/quobyte/proteomics-grp/brett/stan_venv/bin:$PATH
    ( cd /quobyte/proteomics-grp/brett/stan && \
      python3 "$EV/publish_evosep_pg.py" "$OUT" evosep_column_health ) 2>&1 || \
      echo 'PG Farm publish failed (non-fatal; file cache still updated)'
  else
    echo 'extract FAILED; keeping previous JSON'; rm -f "$tmp"; exit 1
  fi
} >> "$LOG" 2>&1
