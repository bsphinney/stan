#!/bin/bash
# STAN Hive cron: Evosep One column health — extract, publish, and alert.
#
#   */30 * * * * flock -n /tmp/stan_evosep.lock /quobyte/proteomics-grp/STAN/cron_evosep.sh
#
# ONE script, one crontab line, one writer. Two jobs re-running the same
# extract would race the `evosep_column_health` row; this replaces the pair
# (the old `cron_evosep_watch.sh` has been removed).
#
# WHY A MODE BRANCH RATHER THAN ONE CADENCE
#   The two halves want opposite things and a single cadence serves neither:
#
#   * Alerting wants ~30 min. A clog is called on two consecutive critical
#     runs and a 100 SPD run is ~14 min, so ~28 min is the physical floor —
#     ticking faster cannot find a clog sooner. Nightly would drop clog
#     latency to 24 h, which is not meaningfully better than the silence that
#     let the 2026-08-31 overnight clog go unnoticed until morning.
#   * The full-history extract wants to be rare. Measured 94 s wall / 27 s CPU
#     for 568 runs; the mirror is backfilling to 2023 (~23,000 runs), so the
#     full parse is minutes. That must not run every half hour on a login
#     node.
#
#   So: every tick does a cheap 3-day extract and evaluates alerts, on the
#   login node, unconditionally. The full extract AND the PG publish are
#   SUBMITTED TO SLURM, and only when the published document is older than
#   PUBLISH_MAX_AGE_H. This tick never waits for that job, so a slow full
#   extract can no longer hold the lock and silence alerting.
#
# THE PUBLISH RULE THAT MATTERS
#   A bounded 3-day document must NEVER reach `evosep_column_health`. That row
#   is the full-history document the Maintenance panel reads, and overwriting
#   it with a 3-day window would silently destroy the column history. The
#   publish call therefore sits inside the full branch only — never on the
#   common path.
#
# Login-node safe: a few seconds of CPU parsing text logs, one PG read, one
# sbatch submission at most, and at most one HTTPS POST. Nothing heavy runs
# here. flock means a slow tick is skipped, never stacked.
set -uo pipefail

# cron sets neither LOGNAME nor USER, and /etc/profile.d/modules.sh
# dereferences LOGNAME unconditionally; under `set -u` that kills the script
# before its log redirect exists. That failure is what kept the Flinders cron
# silent from 2026-06-10 to 2026-08-26 — and a dead alerter is exactly as
# useless as the no-alerter it replaces.
export LOGNAME="${LOGNAME:-$(id -un)}"
export USER="${USER:-$LOGNAME}"
set +u
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true
set -u

EV=/quobyte/proteomics-grp/STAN/evosep
LOGS=/quobyte/proteomics-grp/brett/evosep_logs
VENV=/quobyte/proteomics-grp/brett/stan_venv
FULL=/quobyte/proteomics-grp/STAN/evosep_column_health.json
RECENT="$EV/evosep_recent.json"
INSTRUMENT='timsTOF HT'
RECENT_DAYS=3
PUBLISH_MAX_AGE_H=20

mkdir -p /quobyte/proteomics-grp/STAN/logs "$EV"
LOG=/quobyte/proteomics-grp/STAN/logs/cron_evosep_$(date +%Y%m%d).log

export STAN_DB_BACKEND=pg
export PATH="$VENV/bin:$PATH"

{
echo "===== tick $(date '+%Y-%m-%d %H:%M:%S') ====="

if [ ! -d "$LOGS" ]; then echo "no Evosep log mirror at $LOGS; skipping"; exit 0; fi
if [ ! -r "$EV/extract_evosep.py" ]; then
    echo "extract_evosep.py not deployed to $EV; skipping"; exit 0
fi

# ── Which mode? Age of the PUBLISHED document decides. PG is authoritative
#    because another host may have published; the file mtime is the fallback
#    when PG is unreachable, so a PG outage cannot wedge us into never
#    publishing.
age_h=$("$VENV/bin/python" - <<'PY' 2>/dev/null || echo ""
import sys
try:
    from datetime import datetime, timezone
    from stan.db_pg import _connect
    with _connect() as pg, pg.cursor() as cur:
        cur.execute("SELECT updated_at FROM evosep_column_health WHERE id=1")
        row = cur.fetchone()
    if row and row[0]:
        print(f"{(datetime.now(timezone.utc) - row[0]).total_seconds() / 3600:.2f}")
    else:
        print("999999")
except Exception:
    sys.exit(1)
PY
)
if [ -z "$age_h" ]; then
    if [ -f "$FULL" ]; then
        age_h=$(( ( $(date +%s) - $(stat -c %Y "$FULL") ) / 3600 ))
        echo "PG unreachable; using $FULL mtime, age ${age_h}h"
    else
        age_h=999999
        echo "PG unreachable and no $FULL; treating as never published"
    fi
fi

if awk "BEGIN{exit !($age_h >= $PUBLISH_MAX_AGE_H)}"; then MODE=full; else MODE=recent; fi
echo "published document age ${age_h}h (threshold ${PUBLISH_MAX_AGE_H}h) -> mode=$MODE"

# ── Full history: SUBMITTED to SLURM, never run here ─────────────────────
#   Measured on Hive: 2,024 runs took 5m42s wall / 1m41s CPU reading Quobyte,
#   ~50 ms/run, so the finished ~23,000-run mirror is about an hour. That must
#   not run on a login node, and because this script holds the flock it would
#   also silence the 30-minute alert ticks for that whole hour.
#
#   So the full extract + publish is an sbatch job and this tick does not wait
#   for it. The publish lives inside that job, which keeps the rule that only
#   a full-history document ever reaches `evosep_column_health`.
if [ "$MODE" = full ]; then
    queued=$(squeue -u "$USER" -h -n ev-full-publish 2>/dev/null | wc -l | tr -d ' ')
    if [ "${queued:-0}" -gt 0 ]; then
        echo "full extract already queued/running ($queued); not submitting another"
    elif ! command -v sbatch >/dev/null 2>&1; then
        echo 'sbatch unavailable; skipping the full extract this tick'
    else
        jid=$(sbatch --parsable --partition=high --account=genome-center-grp \
              --qos=genome-center-grp-high-qos --cpus-per-task=4 --mem=16G \
              --time=02:00:00 --job-name=ev-full-publish \
              --output=/quobyte/proteomics-grp/STAN/logs/ev_full_%j.out \
              --wrap="export STAN_DB_BACKEND=pg; \
tmp=\$(mktemp ${FULL}.XXXX); \
$VENV/bin/python $EV/extract_evosep.py --root $LOGS --instrument '$INSTRUMENT' --out \$tmp || exit 1; \
$VENV/bin/python -c \"import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if d.get('summary',{}).get('n_runs',0)>0 and d.get('daily') else 1)\" \$tmp || { echo 'no runs/daily; keeping previous'; rm -f \$tmp; exit 1; }; \
mv -f \$tmp $FULL; \
$VENV/bin/python $EV/publish_evosep_pg.py $FULL evosep_column_health" 2>&1)
        echo "submitted full extract + publish as job $jid"
    fi
fi

# ── Every tick: the cheap window, then alerting ──────────────────────────
#   Unconditional, even when a full extract is off in SLURM. If alerting only
#   ran on the recent branch's own terms, a submitted-but-unfinished job would
#   skip alerting for that tick -- reintroducing the blindness this exists to
#   remove.
since=$(date -d "${RECENT_DAYS} days ago" +%Y-%m-%d 2>/dev/null \
        || date -v-${RECENT_DAYS}d +%Y-%m-%d)
tmp=$(mktemp "${RECENT}.XXXX")
# Carry the full document's per-column pressure reference forward. A 3-day
# window is far too thin to BUILD a reference, but it is exactly what needs
# measuring against one -- that is what puts an absolute, drift-proof
# `pct_over_expected` on every run of every tick.
# A plain string, not an array: `"${arr[@]}"` on an EMPTY array is an
# unbound-variable error under `set -u` on bash < 4.4, and this has to run on
# whatever the login node ships. No path here contains a space.
REF=""
[ -r "$FULL" ] && REF="--reference-from $FULL"
# shellcheck disable=SC2086
if "$VENV/bin/python" "$EV/extract_evosep.py" --root "$LOGS" --since "$since" \
        --instrument "$INSTRUMENT" $REF --out "$tmp"; then
    mv -f "$tmp" "$RECENT"
    OUT="$RECENT"
    echo "extracted (since $since) -> $RECENT ($(stat -c%s "$RECENT") bytes)"
else
    echo 'recent extract FAILED; no alerting this tick'; rm -f "$tmp"; exit 1
fi

# --- STAN instrument alerting (Slack). Owned by the slack-alerts change. ---
# Reads the document you just wrote; writes nothing to PG except alert_state.
export STAN_ALERT_STATE=/quobyte/proteomics-grp/STAN/evosep/alert_state.json
if [ -r /quobyte/proteomics-grp/brett/.stan_slack_webhook ]; then
    export STAN_SLACK_WEBHOOK="$(cat /quobyte/proteomics-grp/brett/.stan_slack_webhook)"
fi
if "$VENV/bin/python" -c 'import stan.reports.instrument_watch' 2>/dev/null; then
    "$VENV/bin/stan" instrument-watch --evosep-json "$OUT" 2>&1 | tail -40
else
    echo 'stan.reports.instrument_watch not deployed yet; no alerting this tick'
fi

echo "tick done $(date '+%H:%M:%S')"
} >> "$LOG" 2>&1
