#!/bin/bash
# STAN Hive cron: publish new QC runs to the community benchmark.
#
#   25 */6 * * * flock -n /tmp/stan_community_sync.lock \
#       /quobyte/proteomics-grp/STAN/cron_community_sync.sh
# (as installed in brettsp's crontab, checked 2026-09-22)
#
# Why this can run on Hive at all: the compute nodes' egress to
# *.hf.space is unreliable, which is why submissions used to be pushed
# by hand from the Mac. The LOGIN node reaches both the Space and the
# custom domain fine (verified 2026-08-26), and `stan submit-all` is
# HTTP POSTs against PG-resident rows -- no raw-file reading, no search,
# no meaningful CPU. That keeps it inside the "never compute on the
# login node" rule while removing the manual step.
#
# Idempotent: rows are flagged submitted_to_benchmark=1 on success, so a
# pushed row is never pushed twice. What a tick READS: see below.
set -uo pipefail

# cron sets neither LOGNAME nor USER, and /etc/profile.d/modules.sh
# dereferences LOGNAME unconditionally. Under `set -u` that kills the
# script before it can log -- the failure that made the Flinders
# dispatch cron silent from 2026-06-10 to 2026-08-26.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

VENV=/quobyte/proteomics-grp/brett/stan_venv
LOG=/quobyte/proteomics-grp/STAN/logs/cron_community_sync_$(date +%Y%m%d).log

export STAN_DB_BACKEND=pg
if [ -r /quobyte/proteomics-grp/brett/.pgfarm_token ]; then
    export PGPASSWORD=$(cat /quobyte/proteomics-grp/brett/.pgfarm_token)
fi

# Deliberately NOT date-scoped. `submit-all --backend pg` selects
#   SELECT * FROM runs WHERE submitted_to_benchmark = 0 OR ... IS NULL
# and sets submitted_to_benchmark = 1 only on a SUCCESSFUL push, so rows it
# skips (non-QC names, blanks, zero IDs) are re-read every tick: 1,097 rows,
# 2.65 MB, four times a day -- ~11 MB/day of PG Farm egress (measured
# 2026-09-22), which is billed. A `--since` 30-day window cut that to 93 KB
# and was tried in v1.1.8 review, then dropped: run_date is the ACQUISITION
# date, so a run that becomes submittable late -- a searched backlog of old
# raws, metrics backfilled onto an old run, a recovery like the timsTOF
# dash-dash blackout -- would silently never be pushed, with nothing to say
# so. A few cents a month is the cheaper side of that trade. The right fix is
# a narrower candidate query in submit-all (names and ids first, full rows only
# for the eligible), not a window here.
{
  echo "===== community sync $(date '+%F %T') ====="
  "$VENV/bin/stan" submit-all --backend pg 2>&1 | tail -6
  echo "----- exit=${PIPESTATUS[0]} done $(date '+%F %T')"
  echo
} >> "$LOG" 2>&1
