#!/bin/bash
# ============================================================================
# extract_bruker.sh  --  Bruker Compass -> STAN maintenance signal extractor
#
# Reads a Bruker Compass Server PostgreSQL backup (pg_dump custom format),
# restores ONLY the tables needed for maintenance analytics into a throwaway
# in-container postgres, runs a read-only analysis, and emits one compact
# JSON document of maintenance signals for the STAN dashboard.
#
# READ-ONLY on all Bruker data. Nothing is written back to the compass DB or
# the backups. The throwaway postgres cluster lives under $TMPDIR and is
# deleted on exit.
#
# Usage:
#   ./extract_bruker.sh [--backup <compass.backup>] [--out <file.json>]
#   (default backup = newest daily/<date>/compass.backup; default out = stdout)
#
# Runs on Hive. If postgres client tools are not on PATH it re-execs itself
# inside the pinned apptainer image automatically.
# ============================================================================
set -uo pipefail

SIF="${SIF:-/quobyte/proteomics-grp/apptainers/postgres16.sif}"
BACKUP_ROOT="${BACKUP_ROOT:-/quobyte/proteomics-grp/brett/BrukerDBBackup}"
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
BACKUP=""
OUT=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) BACKUP="$2"; shift 2;;
    --out)    OUT="$2";    shift 2;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

# Default to the newest daily backup.
if [[ -z "$BACKUP" ]]; then
  BACKUP="$(ls -1t "$BACKUP_ROOT"/daily/*/compass.backup 2>/dev/null | head -1)"
fi
if [[ -z "$BACKUP" || ! -f "$BACKUP" ]]; then
  echo "ERROR: backup not found: '$BACKUP'" >&2; exit 1
fi

# Re-exec inside the container if pg tools are missing.
if ! command -v pg_restore >/dev/null 2>&1; then
  exec apptainer exec --bind /quobyte:/quobyte,/tmp:/tmp "$SIF" bash "$SELF" \
       --backup "$BACKUP" ${OUT:+--out "$OUT"}
fi

SQL_DIR="$(dirname "$SELF")"
BACKUP_DATE="$(basename "$(dirname "$BACKUP")" | cut -c1-10)"

D="$(mktemp -d /tmp/bruker_extract_XXXXXX)"
export PGDATA="$D/data"
SOCK="$D/sock"; mkdir -p "$SOCK"
PORT=55460
cleanup(){ pg_ctl -D "$PGDATA" stop -m immediate >/dev/null 2>&1; rm -rf "$D"; }
trap cleanup EXIT

log(){ echo "[$(date +%H:%M:%S)] $*" >&2; }

log "initdb"
initdb -U p >/dev/null 2>&1
log "start postgres"
pg_ctl -D "$PGDATA" -o "-p $PORT -c fsync=off -c listen_addresses='' -c unix_socket_directories=$SOCK -c max_wal_size=4GB" -w start >/dev/null 2>&1
PSQL(){ psql -h "$SOCK" -p "$PORT" -U p -d compass "$@"; }
createdb -h "$SOCK" -p "$PORT" -U p compass

# ---- filtered TOC: keep all DDL, restore data ONLY for whitelisted tables ----
TOC="$D/toc.txt"; FTOC="$D/toc.filtered"
pg_restore -l "$BACKUP" > "$TOC" 2>/dev/null
KEEP="$D/keep.txt"
cat > "$KEEP" <<'KEOF'
cst task
cst station
cst sample_reference
cst samples2tasks
cst batch
cst task_type
cdr data_set
mm method
mm method_version
mm method_type
KEOF
awk 'NR==FNR{keep[$1" "$2]=1; next}
     /TABLE DATA/{ if (($6" "$7) in keep) print; next } {print}' \
     "$KEEP" "$TOC" > "$FTOC"

log "restore (filtered) from $BACKUP"
pg_restore -h "$SOCK" -p "$PORT" -U p -d compass --no-owner -j 4 -L "$FTOC" "$BACKUP" >/dev/null 2>&1
PSQL -c "ANALYZE;" >/dev/null 2>&1

log "analyze -> JSON"
JSON="$(PSQL -tA -v backup_path="$BACKUP" -v backup_date="$BACKUP_DATE" -f "$SQL_DIR/extract.sql" 2>/dev/null)"

if [[ -z "$JSON" ]]; then echo "ERROR: extraction produced no output" >&2; exit 1; fi

if [[ -n "$OUT" ]]; then
  printf '%s\n' "$JSON" > "$OUT"
  log "wrote $OUT ($(printf '%s' "$JSON" | wc -c) bytes)"
else
  printf '%s\n' "$JSON"
fi
