#!/bin/bash
# Hive cron: push each instrument's mirror DB rows into the central PG Farm
# Postgres every 30 min. Idempotent — re-runs upsert by (host_origin, id) so
# rows landing on the mirror between cron firings reach PG within the window.
#
# Token: lives in /quobyte/proteomics-grp/brett/.pgfarm_token (chmod 600).
# Refresh it via `pgfarm auth login` whenever the 7-day CAS token expires.
# Long-term replace with a service-account password from Justin Merz.

set -u
SCRIPT=/quobyte/proteomics-grp/STAN/scripts/migrate_sqlite_to_pgfarm.py
TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
LOG=/quobyte/proteomics-grp/STAN/logs/pg_sync_$(date +%Y%m%d).log

if [ ! -r "$TOKEN_FILE" ]; then
    echo "$(date -u +%FT%TZ) FATAL token not readable at $TOKEN_FILE" >> "$LOG"
    exit 1
fi
TOKEN=$(cat "$TOKEN_FILE")

mkdir -p "$(dirname "$LOG")"
echo "$(date -u +%FT%TZ) sync start" >> "$LOG"

for host in lumos:lumosRox exploris:DESKTOP-FOT3DAA timstof:TIMS-10878; do
    label=${host%:*}
    dir=${host#*:}
    db=/quobyte/proteomics-grp/STAN/$dir/stan.db
    if [ ! -r "$db" ]; then
        echo "  $label: db missing at $db" >> "$LOG"
        continue
    fi
    PGPASSWORD="$TOKEN" python3 "$SCRIPT" --sqlite "$db" --host-origin "$label" >> "$LOG" 2>&1 \
        && echo "  $label: synced" >> "$LOG" \
        || echo "  $label: FAILED" >> "$LOG"
done

echo "$(date -u +%FT%TZ) sync done" >> "$LOG"
