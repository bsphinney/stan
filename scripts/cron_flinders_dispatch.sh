#!/bin/bash
# STAN Hive cron: pull new Flinders QC raws into the dispatcher watch dirs,
# then submit up to max_submissions_per_run (50) SLURM search jobs.
#
# Installed in brettsp's Hive crontab, every 5 min, wrapped in flock so
# overlapping ticks are skipped:
#
#   */5 * * * * flock -n /tmp/stan_flinders_cron.lock \
#       /quobyte/proteomics-grp/STAN/cron_flinders_dispatch.sh
#
# 5 min (was 15, changed 2026-08-27) so a raw that robocopy lands is
# searched within minutes instead of up to a quarter hour later. A tick
# costs ~80-105 s, nearly all of it the rolling-30d walk of the Flinders
# export; narrowing the window does not help much (--since-days 2 still
# takes 64 s), because the cost is walking the tree, not the date filter.
# That is a ~30% duty cycle of stat() calls on the login node -- IO, not
# compute -- and flock means a slow tick is skipped rather than stacked.
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
  # --all-runs links NON-QC acquisitions too. Without it the linker only
  # symlinks HeLa QC raws, so customer samples and blanks never reach a watch
  # dir, never get a monitor job, and never appear in sample_health -- which
  # is why the Samples panel and the week-at-a-glance grid showed nothing for
  # timsTOF after 2026-08-26 16:32 (the last time someone ran the linker by
  # hand with this flag). Sample-health ingestion was effectively manual.
  #
  # Safe: dispatch_hive._classify_raw() routes non-QC raws to the lightweight
  # monitor pipeline -- rawmeat metadata only, no search engine, and they are
  # never submitted to the community benchmark. The catch-up is self-throttling
  # because the dispatcher submits at most max_submissions_per_run (50) a tick.
  echo "--- link Flinders raws (rolling 30d, QC + samples) ---"
  "$PY" "$LINKER" --config "$CONFIG" --since-days 30 --all-runs 2>&1 | grep -E "DONE|ERROR" || true
  echo "--- dispatch (cap 50) ---"
  "$STAN" hive-dispatch --config "$CONFIG" 2>&1 | tail -2 || true
  echo
} >> "$LOG" 2>&1
