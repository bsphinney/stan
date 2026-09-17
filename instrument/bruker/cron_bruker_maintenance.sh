#!/bin/bash
# STAN Hive cron: refresh Bruker maintenance signals from the newest Compass
# backup on Quobyte. Reads the backup Bruker's own tooling produced (already
# authenticated -> no DB password), extracts a compact JSON of maintenance
# analytics, and publishes it to a stable path for the dashboard.
#
# Installed in brettsp's crontab, nightly after the instrument has copied the
# day's 18:00 backup to Quobyte:
#   0 20 * * * flock -n /tmp/stan_bruker_maint.lock /quobyte/proteomics-grp/STAN/cron_bruker_maintenance.sh
#
# Login-node-safe: the heavy restore runs inside the pinned apptainer against a
# throwaway postgres in $TMPDIR, torn down on exit. Read-only on all Bruker
# data. flock means a slow run is skipped, never stacked.
set -uo pipefail
export LOGNAME="${LOGNAME:-$(id -un)}"; export USER="${USER:-$LOGNAME}"
set +u; source /etc/profile.d/modules.sh 2>/dev/null || true; source /etc/profile.d/hpccf.sh 2>/dev/null || true; set -u

BR=/quobyte/proteomics-grp/STAN/bruker
OUT=/quobyte/proteomics-grp/STAN/bruker_maintenance.json
mkdir -p /quobyte/proteomics-grp/STAN/logs
LOG=/quobyte/proteomics-grp/STAN/logs/cron_bruker_maint_$(date +%Y%m%d).log

{
  echo "===== tick $(date '+%Y-%m-%d %H:%M:%S') ====="
  newest=$(find /quobyte/proteomics-grp/STAN/BrukerDBBackup -type f -iname '*.backup' -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
  if [ -z "$newest" ]; then echo 'no backup found on Quobyte; skipping'; exit 0; fi
  echo "newest backup: $newest"
  tmp=$(mktemp "${OUT}.XXXX")
  if "$BR/extract_bruker.sh" --backup "$newest" --out "$tmp" 2>&1; then
    mv -f "$tmp" "$OUT"
    echo "published -> $OUT ($(stat -c%s "$OUT") bytes)"
    # Push the document into PG Farm so the HOSTED dashboard serves it. The
    # endpoint reads PG first and falls back to the bundled file, so this is
    # what turns the panel from deploy-frozen into nightly-fresh. DDL-free:
    # the table is owned by brettsp via migration, this account has DML only.
    export STAN_DB_BACKEND=pg
    export PATH=/quobyte/proteomics-grp/brett/stan_venv/bin:$PATH
    ( cd /quobyte/proteomics-grp/brett/stan && \
      python3 "$BR/publish_bruker_pg.py" "$OUT" bruker_maintenance ) 2>&1 || \
      echo 'PG Farm publish failed (non-fatal; file cache still updated)'

  # Email any NEW instrumentation failures (missing Evotip, LC clog, MS error,
  # connection lost). Dedups by acquisition filename against a state file, so a
  # failure is reported once. Nightly, because the backup it reads is nightly.
  if [ -s "$OUT" ]; then
    python3 "$BR/bruker_alert.py" --json "$OUT" 2>&1 || echo "alerter failed (non-fatal)"
  fi

  else
    echo 'extract FAILED; keeping previous JSON'; rm -f "$tmp"; exit 1
  fi
} >> "$LOG" 2>&1
