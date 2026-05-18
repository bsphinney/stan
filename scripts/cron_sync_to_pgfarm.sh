#!/bin/bash
# Push each instrument's mirror SQLite stan.db into PG Farm every 30 min.
# Idempotent — upserts by (host_origin, id) so rows landing on the mirror
# between fires reach PG within the window.
#
# Canonical home: Brett's Mac via launchd
# (~/Library/LaunchAgents/com.brettphinney.stan-pgfarm-sync.plist).
# The same script also works on Hive — autodetects the base path. We
# moved off Hive's login2 cron on 2026-05-18 because:
#   1. Login-node cron jobs violate the "no compute on login nodes" rule
#   2. login2 was load-avg ~35 (crowded)
#   3. The script doesn't need cluster resources — it's a few SELECT +
#      INSERT round trips against PG Farm
#
# Token: ~/Library/Application Support is too far; we keep it on the
# shared volume so Hive jobs that need PG-direct can read the same file.
# Refresh via `pgfarm auth login` weekly (7-day CAS) until Justin's
# service account is live.

set -u

# Path auto-detect: /Volumes/ on Mac (SMB-mounted Quobyte), /quobyte/ on Hive.
if [ -d /Volumes/proteomics-grp ]; then
    BASE=/Volumes/proteomics-grp
    PYTHON=/opt/anaconda3/bin/python3
elif [ -d /quobyte/proteomics-grp ]; then
    BASE=/quobyte/proteomics-grp
    # Hive: use the stan venv's Python so psycopg2 is importable. System
    # Python under cron's PATH historically lacked psycopg2 and failed
    # silently from 2026-05-15 to 2026-05-18.
    PYTHON=/quobyte/proteomics-grp/brett/stan_venv/bin/python
else
    echo "FATAL: neither /Volumes/proteomics-grp nor /quobyte/proteomics-grp visible" >&2
    exit 1
fi

SCRIPT=$(cd "$(dirname "$0")" && pwd)/migrate_sqlite_to_pgfarm.py
TOKEN_FILE=$BASE/brett/.pgfarm_token
LOG_DIR=$BASE/STAN/logs
LOG=$LOG_DIR/pg_sync_$(date +%Y%m%d).log

if [ ! -r "$TOKEN_FILE" ]; then
    mkdir -p "$LOG_DIR"
    echo "$(date -u +%FT%TZ) FATAL token not readable at $TOKEN_FILE" >> "$LOG"
    exit 1
fi
TOKEN=$(cat "$TOKEN_FILE")

mkdir -p "$LOG_DIR"
echo "$(date -u +%FT%TZ) sync start (base=$BASE python=$PYTHON)" >> "$LOG"

for host in lumos:lumosRox exploris:DESKTOP-FOT3DAA timstof:TIMS-10878; do
    label=${host%:*}
    dir=${host#*:}
    db=$BASE/STAN/$dir/stan.db
    if [ ! -r "$db" ]; then
        echo "  $label: db missing at $db" >> "$LOG"
        continue
    fi
    PGPASSWORD="$TOKEN" "$PYTHON" "$SCRIPT" --sqlite "$db" --host-origin "$label" >> "$LOG" 2>&1 \
        && echo "  $label: synced" >> "$LOG" \
        || echo "  $label: FAILED" >> "$LOG"
done

echo "$(date -u +%FT%TZ) sync done" >> "$LOG"
