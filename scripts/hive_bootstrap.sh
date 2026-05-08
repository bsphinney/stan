#!/bin/bash
# Idempotent Hive-side bootstrap for STAN. Called from
# test-hive-flow.bat via SSH so Brett never has to think about it.
#
# Lives at /quobyte/proteomics-grp/STAN/hive_bootstrap.sh.
# Re-run safe — every step checks "does this exist?" first.
set -euo pipefail

VENV=/quobyte/proteomics-grp/brett/stan_venv
DISPATCH_YML=/quobyte/proteomics-grp/STAN/dispatch.yml
GITHUB_ZIP="https://github.com/bsphinney/stan/archive/refs/heads/main.zip"

echo "=== Hive bootstrap starting ==="
echo "user:    $(whoami)"
echo "host:    $(hostname)"
echo "venv:    $VENV"
echo "yml:     $DISPATCH_YML"

# Hive's /usr/bin/python3 ships without ensurepip, so plain
# `python3 -m venv` fails. python/3.11.9 module has it built in.
source /etc/profile.d/modules.sh 2>/dev/null || true
module load python/3.11.9

if [ ! -d "$VENV" ]; then
    echo "--- Creating venv ---"
    mkdir -p "$(dirname "$VENV")"
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
else
    echo "--- venv already exists ---"
fi

echo "--- Installing/upgrading STAN ---"
# Plain --upgrade only. NOT --force-reinstall: that triggers a
# pip metadata-tmpfile rename pattern that fails on Quobyte with
# "OSError [Errno 2] No such file or directory: ...INSTALLER<rand>.tmp"
# (race on Quobyte's distributed-rename semantics). Plain --upgrade
# skips packages already at the right version, so re-runs are fast
# and don't trigger the bug. For a true forced reinstall, run pip
# manually after deleting the venv.
"$VENV/bin/pip" install --quiet --upgrade \
    "stan-proteomics @ ${GITHUB_ZIP}"

echo "Hive STAN version: $($VENV/bin/stan version)"

# Ensure expected directory layout for sbatch logs / dispatch logs /
# search outputs / test logs. Idempotent mkdir -p.
mkdir -p /quobyte/proteomics-grp/STAN/test_logs
mkdir -p /quobyte/proteomics-grp/STAN/processing
mkdir -p /quobyte/proteomics-grp/STAN/logs/sbatch
mkdir -p /quobyte/proteomics-grp/STAN/logs/dispatch
for inst in "timsTOF HT" "Orbitrap Fusion Lumos" "Orbitrap Exploris 480"; do
    mkdir -p "/quobyte/proteomics-grp/STAN/incoming/${inst}"
done

if [ ! -f "$DISPATCH_YML" ]; then
    echo "--- Writing dispatch.yml ---"
    "$VENV/bin/stan" hive-dispatch --print-default-config > "$DISPATCH_YML"
else
    echo "--- dispatch.yml already exists ---"
fi

echo "=== Hive bootstrap OK ==="
