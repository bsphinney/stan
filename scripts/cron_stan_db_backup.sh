#!/bin/bash
# cron_stan_db_backup.sh -- submit the STAN pg_dump job.
#
# Runs on the Hive LOGIN node from cron, so it stays trivial: submit an
# sbatch and exit. All real work happens inside SLURM (stan_db_backup.sbatch).
#
# INSTALLED in brettsp's crontab 2026-09-17, daily at 03:17:
#
#   17 3 * * * flock -n /tmp/stan_db_backup.lock \
#       bash /quobyte/proteomics-grp/STAN/cron_stan_db_backup.sh
#
# Invoked via `bash` rather than executed, so a lost execute bit cannot
# silently kill the backup -- the 2026-09-09 cron_evosep.sh failure mode.
# Watched by check_cron_heartbeat via CRON_LOGS["db_backup"], so this job
# going quiet is itself reported.
#
# WHY NO QUEUE-STACKING GUARD. FRAN's equivalent checks squeue before
# submitting, because a 228 GB dump runs for hours and a weekly tick can
# land on a run that has not finished. STAN's database is 107 MB and dumps
# in seconds, so two ticks cannot overlap; flock alone is enough. If STAN's
# database ever grows into hours, port FRAN's guard across -- and port its
# lesson with it: use `id -un`, never $USER, because cron does not reliably
# set $USER and under `set -u` the guard subshell dies, the count comes back
# empty, and it FAILS OPEN. That is how FRAN once queued a second job while
# the first was still pending.
set -uo pipefail

# cron's environment lacks LOGNAME/USER and /etc/profile.d/modules.sh
# dereferences LOGNAME unconditionally; under `set -u` that aborts the whole
# script before the log line below is ever reached, so the failure is
# completely silent. This is what kept the Flinders cron from producing any
# output between 2026-06-10 and 2026-08-26.
#
# Seeding the vars is NOT sufficient on its own, and this script shipped
# staged for 13 days with the insufficient version: the exports sat AFTER the
# sources, and LOGNAME is only the FIRST unbound variable in modules.sh --
# line 19 goes on to dereference MANPATH, which a clean cron environment does
# not set either. Measured under `env -i`: exit 127, "MANPATH: unbound
# variable", and not one byte written to the log. So seed both vars BEFORE
# sourcing and drop -u ACROSS the sourcing, exactly as
# cron_flinders_dispatch.sh does -- then no stray unbound variable in any
# system profile, present or future, can kill the tick. `|| true` does not
# save you here: under `set -u` the shell itself exits and never evaluates it.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"

set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

LOG=/quobyte/proteomics-grp/STAN/logs/db_backup_submit.log
SBATCH=/quobyte/proteomics-grp/STAN/stan_db_backup.sbatch

mkdir -p "$(dirname "$LOG")" 2>/dev/null

if ! command -v sbatch >/dev/null 2>&1; then
  echo "$(date '+%F %T') ABORT: sbatch not on PATH" >> "$LOG"
  exit 1
fi
[ -f "$SBATCH" ] || { echo "$(date '+%F %T') ABORT: no $SBATCH" >> "$LOG"; exit 1; }

out=$(sbatch "$SBATCH" 2>&1)
echo "$(date '+%F %T') $out" >> "$LOG"
