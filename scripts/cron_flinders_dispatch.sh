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

# cron's environment lacks LOGNAME/USER, and /etc/profile.d/modules.sh
# dereferences LOGNAME unconditionally. Under `set -u` that aborts the whole
# script at this line -- before the log block below is ever reached, so the
# failure is completely silent. This is why the cron produced zero output
# between its install (2026-06-10) and 2026-08-26. Seed both vars, and drop
# -u across the sourcing so a stray unbound var in a system profile can never
# kill the tick again.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"

set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

# Canonical store is PG Farm (service account). use_pg() keys off this, so
# dedup + writes both go to PG; the local SQLite holds only the dispatch
# audit + sample_health tables.
export STAN_DB_BACKEND=pg

VENV=/quobyte/proteomics-grp/brett/stan_venv
PY="$VENV/bin/python"
STAN="$VENV/bin/stan"
CONFIG=/quobyte/proteomics-grp/STAN/dispatch.yml
LINKER=/quobyte/proteomics-grp/brett/link_flinders_qc.py
LOG="/quobyte/proteomics-grp/STAN/logs/cron_flinders_$(date +%Y%m%d).log"

{
  echo "===== tick $(date '+%Y-%m-%d %H:%M:%S') ====="
  # NO token-refresh step. $TOKEN holds the long-lived 512-char
  # service-account secret, and stan.db_pg._resolve_pgpassword() now mints a
  # fresh JWT from it on every connect (same pattern FRAN's _token() uses).
  # Re-adding a refresh here would overwrite that secret with a 7-day JWT and
  # re-couple PG access to this cron succeeding -- the exact failure that took
  # STAN's PG writes down from 2026-06-10 to 2026-08-26.
  echo "--- link Flinders QC (rolling 30d) ---"
  "$PY" "$LINKER" --config "$CONFIG" --since-days 30 2>&1 | grep -E "DONE|ERROR" || true
  echo "--- dispatch (cap 50) ---"
  "$STAN" hive-dispatch --config "$CONFIG" 2>&1 | tail -2 || true
  echo
} >> "$LOG" 2>&1
