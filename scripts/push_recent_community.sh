#!/bin/bash
# Push recently-processed QC runs from PG Farm to the community benchmark relay.
#
# Run from the Mac (Hive has no egress to *.hf.space). Mints a fresh PG Farm
# token from the service-account secret, then submits every un-submitted QC
# run with run_date >= SINCE to the relay (which uploads parquet to the HF
# Dataset the community Space reads). Idempotent: rows are marked
# submitted_to_benchmark=1 after a successful push, so re-runs only send new
# ones. Intended to run AFTER the Hive dispatch cron has drained the backlog
# into PG.
#
#   scripts/push_recent_community.sh 2026-05-13        # default cutoff if omitted
#
set -euo pipefail

SINCE="${1:-2026-05-13}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
# The credential file holds the long-lived service-account secret;
# stan.db_pg._resolve_pgpassword() exchanges it for a fresh JWT per connect
# (v1.0.3+), so there is nothing to refresh here. See docs/PG_FARM_ACCESS.md.
CRED=/Volumes/proteomics-grp/brett/.pgfarm_token

export PGPASSWORD="$(cat "$CRED")"
export STAN_DB_BACKEND=pg

echo "Dry-run first (run_date >= $SINCE):"
stan submit-all --backend pg --since "$SINCE" --dry-run 2>&1 | tail -3

echo
read -r -p "Submit these to the community benchmark? [y/N] " ans
if [ "$ans" = "y" ] || [ "$ans" = "Y" ]; then
    stan submit-all --backend pg --since "$SINCE"
else
    echo "Aborted — nothing submitted."
fi

unset PGPASSWORD
