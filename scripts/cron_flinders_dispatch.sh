#!/bin/bash
# STAN Hive cron: pull new Flinders QC raws into the dispatcher watch dirs,
# then submit up to max_submissions_per_run (50) SLURM search jobs.
#
# Installed in brettsp's Hive crontab, every 15 min, wrapped in flock so
# overlapping ticks are skipped:
#
#   */15 * * * * flock -n /tmp/stan_flinders_cron.lock \
#       /quobyte/proteomics-grp/STAN/cron_flinders_dispatch.sh
#
# Login-node-safe: only walks the filesystem, creates symlinks, and calls
# sbatch. All real compute lands inside SLURM (partition `low` per
# dispatch.yml). Canonical source for this file is scripts/ in the stan
# repo; the running copy lives at /quobyte/proteomics-grp/STAN/.
set -uo pipefail

source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true

# Canonical store is PG Farm (service account). use_pg() keys off this, so
# dedup + writes both go to PG; the local SQLite holds only the dispatch
# audit + sample_health tables.
export STAN_DB_BACKEND=pg

VENV=/quobyte/proteomics-grp/brett/stan_venv
PY="$VENV/bin/python"
STAN="$VENV/bin/stan"
CONFIG=/quobyte/proteomics-grp/STAN/dispatch.yml
LINKER=/quobyte/proteomics-grp/brett/link_flinders_qc.py
REFRESH=/quobyte/proteomics-grp/brett/pgfarm_refresh_token.py
SECRET=/quobyte/proteomics-grp/brett/.pgfarm_secret.json
TOKEN=/quobyte/proteomics-grp/brett/.pgfarm_token
LOG="/quobyte/proteomics-grp/STAN/logs/cron_flinders_$(date +%Y%m%d).log"

{
  echo "===== tick $(date '+%Y-%m-%d %H:%M:%S') ====="
  echo "--- refresh PG Farm token (mint if > 5 days old) ---"
  "$PY" "$REFRESH" --secret-file "$SECRET" --token-file "$TOKEN" --max-age-days 5 2>&1 | grep -iE "mint|no refresh|error" || true
  echo "--- link Flinders QC (rolling 30d) ---"
  "$PY" "$LINKER" --config "$CONFIG" --since-days 30 2>&1 | grep -E "DONE|ERROR" || true
  echo "--- dispatch (cap 50) ---"
  "$STAN" hive-dispatch --config "$CONFIG" 2>&1 | tail -2 || true
  echo
} >> "$LOG" 2>&1
