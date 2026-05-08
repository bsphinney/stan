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

# ---------------------------------------------------------------------------
# Tool installation paths
# ---------------------------------------------------------------------------
STAN_BASE="/quobyte/proteomics-grp/STAN"

# DIA-NN: pulled as a pre-built Apptainer .sif from the STAN Docker image.
# The image bundles DIA-NN 2.3.x + .NET 8 SDK (required for Thermo .raw).
# NOTE: This placeholder URL (docker://registry.hf.space/brettsp-stan-proteomics:latest)
# does not yet exist — Brett needs to push the image before this step succeeds.
# The script continues past a failed pull so other bootstrap steps still run.
# TODO(deferred): push brettsp-stan-proteomics Docker image to HF Space registry.
DIANN_SIF="${STAN_BASE}/containers/diann.sif"
DIANN_HF_IMAGE="docker://registry.hf.space/brettsp-stan-proteomics:latest"
APPTAINER_CACHE="${STAN_BASE}/.apptainer_cache"

# Sage: static Linux binary, no container needed. Pinned fallback version
# matches Mode B (stan_wsl_setup.sh) — overridden if GitHub API is reachable.
SAGE_DIR="${STAN_BASE}/sage"
SAGE_FALLBACK_TAG="v0.14.7"

# ---------------------------------------------------------------------------
# pull_diann_sif — apptainer-pull of the pre-built STAN image to .sif
#
# Mirrors the DE-LIMP hpc_setup.sh pattern exactly:
#   - Set APPTAINER_CACHEDIR on shared storage (never /tmp — node-local)
#   - Load apptainer/singularity module
#   - Run on a compute node via srun (SIF conversion is CPU-intensive)
#
# The pull is idempotent: skipped if .sif already exists.
# A failed pull (e.g. image not yet published) is non-fatal — the script
# prints a warning and continues so Python venv + Sage still install.
# ---------------------------------------------------------------------------
pull_diann_sif() {
    if [ -f "${DIANN_SIF}" ]; then
        echo "--- DIA-NN .sif already present: ${DIANN_SIF} ---"
        return 0
    fi

    echo "--- Pulling STAN/DIA-NN container from Hugging Face ---"
    echo "    Image : ${DIANN_HF_IMAGE}"
    echo "    Target: ${DIANN_SIF}"
    echo "    NOTE  : Image not yet published — pull will fail until Brett pushes"
    echo "            brettsp-stan-proteomics to the HF Space Docker registry."
    echo "            Continuing bootstrap; other steps are unaffected."

    mkdir -p "$(dirname "${DIANN_SIF}")"
    mkdir -p "${APPTAINER_CACHE}"

    export APPTAINER_CACHEDIR="${APPTAINER_CACHE}"
    export SINGULARITY_CACHEDIR="${APPTAINER_CACHE}"

    # SIF conversion is CPU-intensive — run on a compute node, not the login node.
    # Using publicgrp/low/publicgrp-low-qos (always available, no quota cap).
    # --export=ALL passes APPTAINER_CACHEDIR into the srun environment.
    local srun_ok=0
    srun \
        --account=publicgrp \
        --partition=low \
        --qos=publicgrp-low-qos \
        --time=1:00:00 \
        --mem=16G \
        --cpus-per-task=4 \
        --export=ALL \
        bash -c "
            module load apptainer 2>/dev/null || module load singularity 2>/dev/null || {
                echo 'ERROR: neither apptainer nor singularity module found on compute node'
                exit 1
            }
            apptainer pull --force '${DIANN_SIF}' '${DIANN_HF_IMAGE}'
        " && srun_ok=1 || true

    if [ "${srun_ok}" = "1" ] && [ -f "${DIANN_SIF}" ]; then
        echo "--- DIA-NN .sif installed: ${DIANN_SIF} ($(du -h "${DIANN_SIF}" | cut -f1)) ---"
    else
        echo "WARNING: DIA-NN .sif pull failed (image likely not published yet)."
        echo "         Re-run after Brett pushes the image: bash hive_bootstrap.sh pull-diann"
        echo "         DIA searches will use the existing Hive container at:"
        echo "         /quobyte/proteomics-grp/dia-nn/diann_2.3.0.sif"
    fi
}

# ---------------------------------------------------------------------------
# download_sage_linux — fetch latest Sage Linux binary to ${SAGE_DIR}/sage
#
# Sage is a static Rust binary — no container needed. Mirrors Mode B's
# install_sage() in stan_wsl_setup.sh exactly (same URL pattern, same
# tar.gz --strip-components logic, same fallback tag).
# Writes sage_binary path into dispatch.yml after download.
# ---------------------------------------------------------------------------
download_sage_linux() {
    if [ -x "${SAGE_DIR}/sage" ]; then
        echo "--- Sage already installed: ${SAGE_DIR}/sage ---"
        _write_sage_to_dispatch
        return 0
    fi

    echo "--- Downloading Sage Linux binary ---"

    local tag
    tag="$(curl -s --max-time 15 \
        "https://api.github.com/repos/lazear/sage/releases/latest" \
        | grep -oE '"tag_name": "[^"]+"' \
        | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')" || true

    if [ -z "${tag}" ]; then
        tag="${SAGE_FALLBACK_TAG}"
        echo "    GitHub API unreachable — falling back to Sage ${tag}"
    fi
    echo "    Downloading Sage ${tag}..."

    mkdir -p "${SAGE_DIR}"
    local url="https://github.com/lazear/sage/releases/download/${tag}/sage-${tag}-x86_64-unknown-linux-gnu.tar.gz"
    local tarball="${SAGE_DIR}/sage.tar.gz"

    if ! wget --progress=bar -O "${tarball}" "${url}" 2>/dev/null; then
        # Some releases omit the version prefix in the tarball filename
        url="https://github.com/lazear/sage/releases/download/${tag}/sage-x86_64-unknown-linux-gnu.tar.gz"
        if ! wget --progress=bar -O "${tarball}" "${url}" 2>/dev/null; then
            echo "WARNING: Sage download failed. Check https://github.com/lazear/sage/releases"
            rm -f "${tarball}"
            return 1
        fi
    fi

    tar -xzf "${tarball}" -C "${SAGE_DIR}" --strip-components=1 2>/dev/null || \
        tar -xzf "${tarball}" -C "${SAGE_DIR}"
    rm -f "${tarball}"

    local sage_bin
    sage_bin="$(find "${SAGE_DIR}" -name sage -type f | head -1)"
    if [ -z "${sage_bin}" ]; then
        echo "WARNING: sage binary not found in extracted archive."
        return 1
    fi
    chmod +x "${sage_bin}"
    if [ "${sage_bin}" != "${SAGE_DIR}/sage" ]; then
        mv "${sage_bin}" "${SAGE_DIR}/sage"
    fi

    echo "--- Sage installed: ${SAGE_DIR}/sage ---"
    _write_sage_to_dispatch
}

# Write sage_binary into dispatch.yml (idempotent — removes old line first).
_write_sage_to_dispatch() {
    if [ ! -f "${DISPATCH_YML}" ]; then
        return 0
    fi
    # Remove any existing sage_binary line, then append the current path.
    local tmp
    tmp="$(mktemp)"
    grep -v "^sage_binary:" "${DISPATCH_YML}" > "${tmp}" || true
    echo "sage_binary: ${SAGE_DIR}/sage" >> "${tmp}"
    mv "${tmp}" "${DISPATCH_YML}"
    echo "    dispatch.yml sage_binary -> ${SAGE_DIR}/sage"
}

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

# alphatims for PEG + DIA window drift (Bruker MS1 reader). Pinned
# per pyproject.toml [peg] extra: alphatims 1.0.8 last works against
# numpy<2 and polars 1.x. Without these, PEG and drift silently skip
# on Hive — TIC + 4DFF + IPS still work. Install lazily (idempotent).
"$VENV/bin/pip" install --quiet \
    'alphatims>=1.0,<1.0.9' 'numpy<2'

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

# ---------------------------------------------------------------------------
# DIA-NN: apptainer-pull of pre-built STAN image (non-fatal if not yet published)
# ---------------------------------------------------------------------------
pull_diann_sif

# ---------------------------------------------------------------------------
# Sage: download latest Linux binary to ${STAN_BASE}/sage/sage
# ---------------------------------------------------------------------------
download_sage_linux

echo "=== Hive bootstrap OK ==="
