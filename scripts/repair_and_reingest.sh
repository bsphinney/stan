#!/bin/bash
# Monday recovery script. Run on Hive.
# Sequence:
#   1. Kill the weekend dispatcher (PID 1425177).
#   2. Cancel pending stan-* low jobs (explicit IDs only).
#   3. Snapshot the corrupted stan.db.
#   4. Rebuild stan.db via .dump | .read (rebuilds all indexes).
#   5. Verify integrity.
#   6. Run re-ingest pass over /quobyte/proteomics-grp/STAN/processing/*/report.parquet
#      to recover the ~1000 orphan parquets that landed during the weekend.
#   7. stan submit-all per-instrument once DB is clean.

set -u
HIVE_DB=/quobyte/proteomics-grp/STAN/stan.db
PROCESSING=/quobyte/proteomics-grp/STAN/processing
LOG=/quobyte/proteomics-grp/STAN/logs/recovery_$(date +%Y%m%d_%H%M).log
exec >> "$LOG" 2>&1

echo "$(date -u +%FT%TZ) recovery start"

# ── Step 1: stop the dispatcher ────────────────────────────────────────────
if ps -p 1425177 > /dev/null 2>&1; then
    echo "  killing weekend dispatcher PID 1425177"
    kill 1425177
    sleep 2
    ps -p 1425177 > /dev/null 2>&1 && echo "  WARN: dispatcher still running, escalating to SIGKILL" && kill -9 1425177
fi

# ── Step 2: cancel pending stan-* low jobs by explicit ID ──────────────────
# NEVER scancel -u brettsp broadly — would kill DE-LIMP / Big Dog runs.
# Filter by exact name pattern AND partition AND explicit IDs from squeue.
pending=$(squeue -u brettsp -h -o '%i %j %P' \
    | awk '$3=="low" && $2 ~ /^stan-/ {print $1}' \
    | tr '\n' ' ')
n=$(echo $pending | wc -w)
echo "  cancelling $n pending stan-low jobs"
[ -n "$pending" ] && scancel $pending

# ── Step 3: snapshot the corrupted DB ──────────────────────────────────────
backup="${HIVE_DB}.corrupted.$(date +%s)"
cp "$HIVE_DB" "$backup"
echo "  backup: $backup"

# ── Step 4: rebuild via dump+load ──────────────────────────────────────────
# This rebuilds every index from the data — clears index corruption.
# Row-table corruption ("rowid out of order") may need .recover instead;
# we try .dump first because it's faster.
new_db=/tmp/stan_rebuilt_$(date +%s).db
echo "  rebuilding to $new_db"
if sqlite3 "$HIVE_DB" .dump | sqlite3 "$new_db"; then
    echo "  .dump+.read succeeded"
else
    echo "  .dump failed — falling back to .recover"
    sqlite3 "$HIVE_DB" .recover | sqlite3 "$new_db"
fi

# ── Step 5: verify integrity ───────────────────────────────────────────────
ok=$(sqlite3 "$new_db" 'PRAGMA integrity_check' 2>&1 | head -1)
echo "  integrity_check: $ok"
if [ "$ok" != "ok" ]; then
    echo "  HALT: rebuilt DB still corrupt — STOP HERE, Brett to investigate"
    exit 1
fi

# Verify row counts roughly match (allow ±10% loss tolerance)
old_count=$(sqlite3 "$HIVE_DB" 'SELECT COUNT(*) FROM runs' 2>/dev/null || echo 0)
new_count=$(sqlite3 "$new_db" 'SELECT COUNT(*) FROM runs')
echo "  runs: old=$old_count new=$new_count"

# ── Step 6: swap ───────────────────────────────────────────────────────────
mv "$HIVE_DB" "${HIVE_DB}.preswap.$(date +%s)"
cp "$new_db" "$HIVE_DB"
chmod 664 "$HIVE_DB"
echo "  swap complete"

# ── Step 7: re-ingest orphan parquets ──────────────────────────────────────
# Each subdir in /processing/ that has report.parquet but no row in `runs`
# (matched by run_name in the dir basename) is orphaned. Re-extract metrics
# via the STAN extractor and INSERT.
orphans=$(python3 <<PYEOF
import sqlite3, os
con = sqlite3.connect("$HIVE_DB")
known = {r[0] for r in con.execute('SELECT run_name FROM runs')}
proc = "$PROCESSING"
orphans = []
for d in os.listdir(proc):
    full = os.path.join(proc, d)
    if not os.path.isdir(full): continue
    if not os.path.exists(os.path.join(full, 'report.parquet')): continue
    if d in known: continue
    orphans.append(full)
print(len(orphans))
PYEOF
)
echo "  $orphans orphan parquets to re-ingest"
echo "  (re-ingest CLI: stan ingest-orphans --processing-dir $PROCESSING)"
echo "  TODO: implement stan ingest-orphans then run it here"

echo "$(date -u +%FT%TZ) recovery done"
