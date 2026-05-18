#!/bin/bash
# Recovery for the weekend 2026-05-16 orphan parquet trove. Run on Hive.
#
# Strategy: skip the corrupted SQLite stan.db entirely. STAN now supports a
# PG-direct write path (STAN_DB_BACKEND=pg), so we re-ingest the orphan
# parquets straight into PG Farm. The SQLite file stays as-is for forensic
# inspection — a separate cool-down task can rebuild it later if anything
# legitimately needs it locally on Hive.
#
# Sequence:
#   1. Preflight: stan >= v0.2.359 (has PG-direct + ingest-orphans CLI)
#   2. Verify PG Farm token is readable
#   3. Stop the weekend dispatcher if it's still alive
#   4. Cancel pending stan-* low jobs by explicit ID
#   5. Snapshot the corrupted stan.db for forensics
#   6. stan ingest-orphans --backend pg → PG Farm

set -u
HIVE_DB=/quobyte/proteomics-grp/STAN/stan.db
PROCESSING=/quobyte/proteomics-grp/STAN/processing
STAN_BIN=/quobyte/proteomics-grp/brett/stan_venv/bin/stan
TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token
MIN_STAN=0.2.359
LOG=/quobyte/proteomics-grp/STAN/logs/recovery_$(date +%Y%m%d_%H%M).log
exec >> "$LOG" 2>&1

echo "$(date -u +%FT%TZ) recovery start"

# ── Preflight: stan version ────────────────────────────────────────────────
cur=$("$STAN_BIN" version 2>&1 | awk '{print $NF}' | sed 's/^v//')
if ! printf '%s\n%s\n' "$MIN_STAN" "$cur" | sort -V -C; then
    echo "  HALT: stan_venv has v$cur but recovery needs >= v$MIN_STAN"
    echo "  Run: cd /quobyte/proteomics-grp/brett/stan && git pull && "
    echo "       /quobyte/proteomics-grp/brett/stan_venv/bin/pip install -e . && "
    echo "       /quobyte/proteomics-grp/brett/stan_venv/bin/pip install psycopg2-binary"
    exit 1
fi
echo "  stan version $cur OK (>= $MIN_STAN)"

# ── Preflight: PG Farm token ───────────────────────────────────────────────
if [ ! -r "$TOKEN_FILE" ]; then
    echo "  HALT: PG Farm token not readable at $TOKEN_FILE"
    echo "  Refresh via 'pgfarm auth login' (7-day CAS token)."
    exit 1
fi
# Also confirm psycopg2 is importable in the venv (cron has historically
# hit this and silently failed every 30 min).
"$STAN_BIN" python -c "import psycopg2" 2>/dev/null || \
    "$STAN_BIN"/..//python -c "import psycopg2" 2>/dev/null || {
    if ! /quobyte/proteomics-grp/brett/stan_venv/bin/python -c "import psycopg2" 2>/dev/null; then
        echo "  HALT: psycopg2 not in stan_venv. Install with:"
        echo "  /quobyte/proteomics-grp/brett/stan_venv/bin/pip install psycopg2-binary"
        exit 1
    fi
}
echo "  PG Farm token + psycopg2 OK"

# ── Step 1: stop dispatcher if alive ───────────────────────────────────────
if ps -p 1425177 > /dev/null 2>&1; then
    echo "  killing weekend dispatcher PID 1425177"
    kill 1425177
    sleep 2
    ps -p 1425177 > /dev/null 2>&1 && echo "  WARN: still running, SIGKILL" && kill -9 1425177
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

# ── Step 3: snapshot the corrupted DB for forensics ────────────────────────
# The PG-direct path doesn't touch SQLite, but keep the snapshot in case
# we later want to audit which rows landed locally before corruption hit.
backup="${HIVE_DB}.corrupted.$(date +%s)"
cp "$HIVE_DB" "$backup" 2>/dev/null && echo "  snapshot: $backup" || \
    echo "  WARN: snapshot failed (DB unreadable) — proceeding to PG"

# ── Step 4: re-ingest orphan parquets into PG Farm ─────────────────────────
# Walks $PROCESSING, parses each per-raw sbatch script for cohort args,
# invokes step_extract with STAN_DB_BACKEND=pg → inserts straight into PG
# bypassing the corrupted SQLite. Idempotent on (host_origin, id).
echo "  starting ingest-orphans into PG Farm..."
export PGPASSWORD=$(cat "$TOKEN_FILE")
export STAN_DB_BACKEND=pg
"$STAN_BIN" ingest-orphans \
    --processing-dir "$PROCESSING" \
    --backend pg \
    || echo "  WARN: ingest-orphans exited non-zero (some rows failed)"

echo "$(date -u +%FT%TZ) recovery done"
