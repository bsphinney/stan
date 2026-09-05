#!/bin/bash
#SBATCH --job-name=stan_db_backup
#SBATCH --account=publicgrp
#SBATCH --partition=low
#SBATCH --qos=publicgrp-low-qos
#SBATCH --time=00:30:00
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --requeue
#SBATCH --output=/quobyte/proteomics-grp/STAN/logs/db_backup_%j.out
#
# Full pg_dump of the STAN database to Flinders.
#
# WHY THIS EXISTS. PG Farm is not known to keep its own backups, and as of
# 2026-09-04 STAN had NONE -- `ls -d .../Data/*stan*` returned nothing while
# de-limp-db-backups sat right beside it. PG Farm is the store of record for
# every run, every sample_health row and the whole community submission
# history, so that was the only copy.
#
# Modelled on fran_db_backup.sbatch, which paid for each guard below once
# already. Two things are deliberately NOT copied across: FRAN's 24 h
# walltime and its queue-stacking guard. Those exist because a 228 GB dump
# runs for hours; STAN is 107 MB and finishes in seconds, so a tick can
# never land on top of its predecessor. Everything else is correctness
# rather than scale and is kept verbatim in spirit.
#
# Partition is `low`, not FRAN's `high`: CLAUDE.md's partition policy puts
# bulk batch work there so it cannot crowd out live QC dispatch. `low` is
# preemptible, which would be a poor fit for a multi-hour job and is a
# non-issue for one measured in seconds -- and --requeue covers it anyway.
set -uo pipefail
source /etc/profile.d/modules.sh 2>/dev/null || true
source /etc/profile.d/hpccf.sh   2>/dev/null || true

SIF=/quobyte/proteomics-grp/apptainers/postgres16.sif
VENV=/quobyte/proteomics-grp/brett/stan_venv
DEST=/nfs/lssc0/flinders/proteomics/Data/stan-db-backups
DB="uc-davis-genome-center-proteomics-core/stan"

# Daily generations. FRAN keeps 12 weekly because each dump costs 5.5 h and
# ~50 GB; STAN's costs seconds and ~100 MB, so there is no reason to accept
# a week of data loss. 30 dailies is ~3 GB against 70 TB free.
KEEP="${STAN_BACKUP_KEEP:-30}"
STAMP=$(date '+%Y%m%d_%H%M')
OUT="$DEST/stan_${STAMP}.dump"

echo "===== stan db backup $(date '+%F %T') on $(hostname) ====="
[ -f "$SIF" ]  || { echo "ABORT: no $SIF"; exit 1; }
mkdir -p "$DEST" 2>/dev/null
[ -d "$DEST" ] || { echo "ABORT: cannot create $DEST"; exit 1; }
mkdir -p /quobyte/proteomics-grp/STAN/logs 2>/dev/null

avail=$(df -BG --output=avail "$DEST" 2>/dev/null | tail -1 | tr -dc '0-9')
echo "  destination free: ${avail:-?} GB"
if [ -n "${avail:-}" ] && [ "$avail" -lt 5 ]; then
  echo "ABORT: under 5 GB free at $DEST"; exit 1
fi

# Token via STAN's own resolver, so this uses the same self-refreshing
# service-account secret every cron job uses and inherits its JWT minting.
# Run from / so the script's directory never lands on sys.path: the checkout
# at /quobyte/proteomics-grp/brett/stan is itself named `stan` and shadows
# the package when a script beside it is executed (this cost half a day on
# 2026-09-04). The service account can SELECT all 15 public tables, so a
# dump taken as it is COMPLETE and needs no CAS login.
PGPASSWORD=$(cd / && "$VENV/bin/python" -c "
from stan.db_pg import _resolve_pgpassword
print(_resolve_pgpassword())" 2>/dev/null | tail -1)
[ -z "${PGPASSWORD:-}" ] && { echo "ABORT: could not obtain a PG Farm token"; exit 1; }

# Hand the token in through the environment, NEVER `--env PGPASSWORD=...`.
# An --env flag lands in apptainer's own argv and argv is world-readable:
# /proc on the compute nodes has no hidepid. APPTAINERENV_<VAR> arrives
# inside as <VAR> and stays readable only by this job's uid.
export APPTAINERENV_PGPASSWORD="$PGPASSWORD"
export APPTAINERENV_PGSSLMODE=require
unset PGPASSWORD

# --bind is required, not optional: apptainer mounts $HOME and a few
# defaults but NOT /nfs, so without it pg_dump fails with "No such file or
# directory" even though the job can stat $DEST perfectly well.
#
# --no-owner --no-privileges matters more here than for FRAN: STAN's 15
# tables are owned by brettsp while this dump runs as the service account,
# so a dump carrying ownership could not be restored by the account that
# wrote it.
#
# The container pins pg_dump 16.15 against server 16.14. A client OLDER
# than the server is refused outright, and FRAN learned that dumps written
# by an 18.4 client are archive format 1.16 which pg_restore 16.15 cannot
# even LIST. Pin the client and keep the .toc so each dump records which
# one wrote it.
t0=$(date +%s)
apptainer exec --bind /nfs/lssc0/flinders/proteomics "$SIF" \
  pg_dump -h pgfarm.library.ucdavis.edu -p 5432 \
          -U genome-proteomics-service-account \
          -d "$DB" \
          -Fc -Z 6 --no-owner --no-privileges \
          -f "$OUT.part"
rc=$?
el=$(( $(date +%s) - t0 ))
if [ $rc -ne 0 ]; then
  echo "FAILED: pg_dump rc=$rc after ${el}s"; rm -f "$OUT.part"; exit $rc
fi

# Write to .part and rename only on success: a truncated dump that looks
# finished is worse than no dump, because retention would count it as a
# good generation. And a backup nobody can read is not a backup --
# pg_restore --list parses the archive TOC and fails on a corrupt file
# without restoring anything.
if ! apptainer exec --bind /nfs/lssc0/flinders/proteomics "$SIF" \
        pg_restore --list "$OUT.part" > "$OUT.toc" 2>/dev/null; then
  echo "FAILED: dump not readable by pg_restore — kept as $OUT.part.bad"
  mv "$OUT.part" "$OUT.part.bad"; rm -f "$OUT.toc"; exit 1
fi

# Readable is not the same as useful. A schema-only dump parses perfectly
# and restores an empty database, so require actual TABLE DATA sections --
# a dump of 15 table definitions and no rows would otherwise be counted as
# a good generation and eventually prune a real one.
ndata=$(grep -c "TABLE DATA" "$OUT.toc" 2>/dev/null || echo 0)
if [ "${ndata:-0}" -lt 1 ]; then
  echo "FAILED: dump contains no TABLE DATA — kept as $OUT.part.bad"
  mv "$OUT.part" "$OUT.part.bad"; exit 1
fi

mv "$OUT.part" "$OUT"
# Report bytes from stat, NOT `du -h`. On this NFS export du reports block
# usage that reads as "512" for a 44 MB file, which looks exactly like an
# empty dump in the log and would send the next reader hunting a
# non-existent bug (it sent me hunting one on 2026-09-04).
bytes=$(stat -c %s "$OUT" 2>/dev/null || echo 0)
mb=$(awk -v b="$bytes" 'BEGIN{printf "%.1f", b/1048576}')
echo "  wrote $OUT (${mb} MB, ${bytes} bytes) in ${el}s"
echo "  verified: $(wc -l < "$OUT.toc") archive entries, ${ndata} with table data"

# Retention. Never prunes below one surviving dump, and the glob sees only
# complete files -- .part and .part.bad cannot be mistaken for generations.
mapfile -t old < <(ls -1t "$DEST"/stan_*.dump 2>/dev/null | tail -n +$((KEEP+1)))
if [ ${#old[@]} -gt 0 ]; then
  for f in "${old[@]}"; do
    echo "  pruning $(basename "$f") ($(du -h "$f" | cut -f1))"
    rm -f "$f" "$f.toc"
  done
fi
total=$(ls -1 "$DEST"/stan_*.dump 2>/dev/null | wc -l)
echo "  kept: ${total} dump(s) in $DEST"
echo "===== done $(date '+%F %T') ====="
