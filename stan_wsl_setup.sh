#!/bin/bash
# =============================================================================
# stan_wsl_setup.sh — STAN Mode B: WSL2 setup and launcher
# =============================================================================
#
# Usage (inside WSL2 Ubuntu, or invoked by Launch_STAN_WSL.bat):
#   bash stan_wsl_setup.sh            # auto: install if needed, then launch
#   bash stan_wsl_setup.sh install    # one-time install only
#   bash stan_wsl_setup.sh update     # pip install --upgrade + re-check tools
#   bash stan_wsl_setup.sh watch      # start stan watch only
#   bash stan_wsl_setup.sh dashboard  # start stan dashboard only
#   bash stan_wsl_setup.sh diann      # install/reinstall DIA-NN only
#   bash stan_wsl_setup.sh config     # re-run SMB share + instruments.yml wizard
#
# Mode B: a beefy Windows lab box that ingests raw files from instrument PCs
# over SMB and runs the full STAN pipeline (DIA-NN + Sage) locally in WSL2.
# No HPC required. Dashboard on http://localhost:8421.
#
# Adapted from DE-LIMP delimp_wsl_setup.sh (same author, same OS quirks).
# The .NET 8 4-tier install (install_dotnet8_sdk / install_dotnet_system_deps)
# is ported verbatim — 27 hotfix iterations landed on that implementation.
#
# =============================================================================

set -euo pipefail

STAN_BASE="${HOME}/.stan"
VENV_DIR="${STAN_BASE}/venv"
DIANN_DIR="${STAN_BASE}/diann"
SAGE_DIR="${STAN_BASE}/sage"
DIANN_LICENSE_FLAG="${STAN_BASE}/.diann_license_accepted"
STAN_BIN="${VENV_DIR}/bin/stan"
CONFIG_DONE_FLAG="${STAN_BASE}/.mode_b_configured"

# DIA-NN version pin (matches Hive; set DIANN_VERSION=latest to auto-resolve)
DIANN_VERSION="${DIANN_VERSION:-2.3.2}"
DIANN_RELEASE_TAG="2.0"

PORT="${STAN_PORT:-8421}"

# --- Colors ---
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${BLUE}[stan]${NC} $*"; }
warn() { echo -e "${YELLOW}[stan]${NC} $*"; }
err()  { echo -e "${RED}[stan]${NC} $*" >&2; }
ok()   { echo -e "${GREEN}[stan]${NC} $*"; }

# =============================================================================
# 0. Disk space check
# =============================================================================
# Rough install footprint:
#   apt packages + build-essential:   ~500 MB
#   Python venv + stan-proteomics:    ~400 MB
#   DIA-NN binary + libs:             ~500 MB
#   Sage binary:                        ~5 MB
#   .NET 8 SDK:                       ~800 MB
#   Peak build tmp:                  ~500 MB (freed after)
# Hard floor: 3 GB free. Recommend 5 GB.
check_disk_space() {
    local required_gb=3
    local recommended_gb=5

    mkdir -p "${HOME}"
    local avail_gb
    avail_gb=$(df -BG "${HOME}" 2>/dev/null | awk 'NR==2 {gsub("G",""); print $4}')

    if [ -z "${avail_gb}" ]; then
        warn "Could not determine free space — skipping check."
        return 0
    fi

    log "Disk space on ${HOME}: ${avail_gb} GB free"

    if [ "${avail_gb}" -lt "${required_gb}" ]; then
        err "Not enough disk space. Have ${avail_gb} GB, need at least ${required_gb} GB."
        echo ""
        echo "  WSL2 stores your Linux filesystem in a VHDX at:"
        echo "  %USERPROFILE%\\AppData\\Local\\Packages\\CanonicalGroupLimited.*\\LocalState\\ext4.vhdx"
        echo ""
        echo "  To free space: sudo apt clean  (clears apt cache)"
        echo "  To grow the VHDX limit, edit %USERPROFILE%\\.wslconfig and add:"
        echo "    [wsl2]"
        echo "    memory=8GB"
        echo "    processors=4"
        exit 1
    fi

    if [ "${avail_gb}" -lt "${recommended_gb}" ]; then
        warn "Only ${avail_gb} GB free — will probably fit but leaves little headroom."
        warn "Recommended: ${recommended_gb} GB+ for comfortable operation."
    fi
}

# =============================================================================
# 1. System packages (apt)
# =============================================================================
install_system_deps() {
    log "Installing system packages via apt (may prompt for sudo password)..."

    sudo apt-get update -qq

    sudo apt-get install -y \
        python3 python3-venv python3-pip \
        build-essential \
        git curl wget unzip \
        libgomp1 libstdc++6 \
        lsb-release

    local pyver
    pyver=$(python3 --version 2>&1)
    ok "System packages installed. Python: ${pyver}"
}

# =============================================================================
# 2. Python venv + STAN pip install
# =============================================================================
install_stan_python() {
    if [ ! -d "${VENV_DIR}" ]; then
        log "Creating Python venv at ${VENV_DIR}..."
        python3 -m venv "${VENV_DIR}"
    fi

    log "Installing/upgrading stan-proteomics from GitHub main..."
    "${VENV_DIR}/bin/pip" install --quiet --upgrade pip
    "${VENV_DIR}/bin/pip" install --quiet --upgrade \
        "stan-proteomics @ https://github.com/bsphinney/stan/archive/refs/heads/main.zip"

    local stanver
    stanver=$("${STAN_BIN}" --version 2>/dev/null || echo "unknown")
    ok "STAN installed: ${stanver}"
}

# =============================================================================
# 3. DIA-NN + .NET 8 SDK
# =============================================================================
# The .NET 8 install went through 27 hotfix iterations in DE-LIMP before
# settling on this 4-tier approach. Port verbatim — do not simplify.
# Key facts:
#   - DIA-NN 2.x needs the .NET 8 SDK, not just the runtime. The error it
#     throws without the SDK: "cannot read .raw files, please download and
#     install .NET Runtime .NET SDK 8.0.407 or later".
#   - dotnet-install.sh does NOT install system deps. Without them, dotnet
#     command works (just reads file headers) but .NET apps fail silently.
#   - On newly-released Ubuntu versions, Microsoft's apt channel doesn't have
#     .NET 8 packages yet — Tier 4 (dotnet-install.sh) is the safe fallback.
#   - Use --channel 8.0, never --version 8.0 (the latter 404s).

install_dotnet8_sdk() {
    # Tier 1: already have 8.x SDK
    if command -v dotnet >/dev/null 2>&1; then
        local sdk
        sdk="$(dotnet --list-sdks 2>/dev/null | grep -E '^8\.' | head -1)"
        if [ -n "${sdk}" ]; then
            log ".NET 8 SDK already installed: ${sdk}"
            return 0
        fi
        local rt
        rt="$(dotnet --list-runtimes 2>/dev/null | grep -E 'Microsoft\.NETCore\.App 8\.' | head -1)"
        if [ -n "${rt}" ]; then
            warn "Runtime present (${rt}) but NO 8.x SDK — DIA-NN needs the SDK. Installing..."
        else
            warn "dotnet exists but no 8.x SDK or runtime — installing SDK..."
        fi
    fi

    # Tier 2: apt with multiple package-name candidates
    for pkg in dotnet-sdk-8.0 dotnet-sdk-8 dotnet8-sdk; do
        if apt-cache show "${pkg}" >/dev/null 2>&1; then
            log "Installing ${pkg} from default apt..."
            if sudo apt-get install -y "${pkg}"; then return 0; fi
        fi
    done

    # Tier 3: Microsoft apt repo + same sweep
    log "Default apt has no dotnet 8 SDK — adding Microsoft repo..."
    local urel
    urel="$(lsb_release -rs)"
    if wget -q "https://packages.microsoft.com/config/ubuntu/${urel}/packages-microsoft-prod.deb" \
            -O /tmp/packages-microsoft-prod.deb; then
        sudo dpkg -i /tmp/packages-microsoft-prod.deb >/dev/null 2>&1 || true
        rm -f /tmp/packages-microsoft-prod.deb
        sudo apt-get update -qq || true
        for pkg in dotnet-sdk-8.0 dotnet-sdk-8 dotnet8-sdk; do
            if apt-cache show "${pkg}" >/dev/null 2>&1; then
                log "Installing ${pkg} from Microsoft apt..."
                if sudo apt-get install -y "${pkg}"; then return 0; fi
            fi
        done
        warn "Microsoft repo for Ubuntu ${urel} has no dotnet-sdk-8 yet (likely too-new Ubuntu)."
    fi

    # Tier 4: Microsoft's official dotnet-install.sh
    # CRITICAL: no --runtime flag (that installs runtime only; we need SDK).
    # CRITICAL: --channel 8.0, not --version 8.0 (version flag 404s).
    log "Falling back to Microsoft's official dotnet-install.sh..."
    local installer="/tmp/dotnet-install.sh"
    if curl -sSL https://dot.net/v1/dotnet-install.sh -o "${installer}"; then
        chmod +x "${installer}"
        sudo "${installer}" --channel "8.0" --install-dir /usr/share/dotnet
        sudo ln -sf /usr/share/dotnet/dotnet /usr/local/bin/dotnet
        rm -f "${installer}"
        if command -v dotnet >/dev/null 2>&1 && \
           dotnet --list-sdks 2>/dev/null | grep -qE '^8\.'; then
            log ".NET 8 SDK installed via dotnet-install.sh"
            return 0
        fi
    fi

    err ".NET 8 SDK install FAILED at all four tiers."
    err "DIA-NN's Thermo .raw reader requires .NET 8 SDK — Thermo raw searches will fail."
    return 1
}

# Install .NET 8 system deps (dotnet-install.sh does NOT pull these).
# Without them, `dotnet --list-sdks` works but executing a .NET binary fails
# silently. Run every time — apt is a no-op when already installed (~1s).
install_dotnet_system_deps() {
    log "Ensuring .NET 8 system dependencies are installed..."
    sudo apt-get install -y --no-install-recommends \
        libc6 libgcc-s1 libssl3 libstdc++6 libunwind8 zlib1g \
        libgssapi-krb5-2 liblttng-ust1 >/dev/null 2>&1 || true
    # libicu version varies by Ubuntu release — probe in order
    for ic in libicu76 libicu74 libicu72 libicu71 libicu70; do
        if apt-cache show "${ic}" >/dev/null 2>&1; then
            sudo apt-get install -y --no-install-recommends "${ic}" >/dev/null 2>&1 || true
            log "  Installed ${ic}"
            break
        fi
    done
}

verify_diann_runtime() {
    local all_ok=1

    if ! command -v dotnet >/dev/null 2>&1; then
        err "  dotnet not on PATH"; all_ok=0
    elif ! dotnet --list-sdks 2>/dev/null | grep -qE '^8\.'; then
        err "  No .NET 8 SDK (dotnet --list-sdks shows nothing 8.x)"
        err "  Found: $(dotnet --list-sdks 2>&1 | head -3)"
        all_ok=0
    else
        log "  .NET 8 SDK present"
    fi

    if [ ! -x "${DIANN_DIR}/diann-linux" ]; then
        err "  diann-linux not found at ${DIANN_DIR}/diann-linux"; all_ok=0
    else
        log "  DIA-NN binary present"
    fi

    local n_raw_dll
    n_raw_dll=$(find "${DIANN_DIR}" -maxdepth 2 -name '*RawFileReader*' 2>/dev/null | wc -l)
    if [ "${n_raw_dll}" -lt 1 ]; then
        err "  No RawFileReader DLLs in ${DIANN_DIR} — Thermo .raw searches will fail"
        all_ok=0
    else
        log "  RawFileReader DLLs present (${n_raw_dll} files)"
    fi

    if [ "${all_ok}" = "1" ]; then
        local missing_libs
        missing_libs=$(ldd "${DIANN_DIR}/diann-linux" 2>&1 | grep 'not found' || true)
        if [ -n "${missing_libs}" ]; then
            err "  ldd: missing libraries:"
            while IFS= read -r line; do err "    ${line}"; done <<< "${missing_libs}"
            all_ok=0
        else
            log "  All dynamic libs resolve (ldd clean)"
        fi

        local smoke_exit
        LD_LIBRARY_PATH="${DIANN_DIR}:${LD_LIBRARY_PATH:-}" \
            DOTNET_ROOT="${DOTNET_ROOT:-/usr/share/dotnet}" \
            "${DIANN_DIR}/diann-linux" --help </dev/null >/dev/null 2>/tmp/diann_smoke_err
        smoke_exit=$?
        rm -f /tmp/diann_smoke_err
        if [ "${smoke_exit}" != "0" ]; then
            err "  diann-linux --help exited ${smoke_exit}"
            all_ok=0
        else
            log "  diann-linux exits cleanly"
        fi
    fi

    if [ "${all_ok}" != "1" ]; then
        err "DIA-NN runtime verification FAILED."
        err "Fix the issues above before running DIA searches."
        return 1
    fi
    ok "DIA-NN runtime verified — Thermo .raw reading should work."
    return 0
}

install_diann() {
    # License gate — write a flag file once user agrees, skip on re-runs
    if [ ! -f "${DIANN_LICENSE_FLAG}" ]; then
        echo ""
        echo -e "${YELLOW}====================== DIA-NN License ======================${NC}"
        echo "  DIA-NN is developed by Vadim Demichev."
        echo "  Free for academic and non-commercial use."
        echo "  Commercial use requires a separate license from the author."
        echo ""
        echo "  Full terms: https://github.com/vdemichev/DiaNN/blob/master/LICENSE.md"
        echo ""
        echo "  Citation: Demichev V et al. Nature Methods. 2020;17(1):41-44."
        echo -e "${YELLOW}=============================================================${NC}"
        echo ""
        read -r -p "Do you accept the DIA-NN license terms? [yes/no]: " accept
        if [ "${accept}" != "yes" ]; then
            warn "License not accepted. Skipping DIA-NN install."
            warn "Re-run with: bash ~/stan_wsl_setup.sh diann"
            return 0
        fi
        mkdir -p "${STAN_BASE}"
        date > "${DIANN_LICENSE_FLAG}"
    fi

    log "Installing .NET 8 SDK (required by DIA-NN for Thermo .raw reading)..."
    install_dotnet8_sdk
    install_dotnet_system_deps

    # Resolve version if "latest" requested
    if [ "${DIANN_VERSION}" = "latest" ]; then
        log "Querying GitHub for latest DIA-NN release..."
        local resolved
        resolved="$(curl -s --max-time 15 \
            "https://api.github.com/repos/vdemichev/DiaNN/releases/tags/${DIANN_RELEASE_TAG}" \
            | grep -oE '"name": "DIA-NN-[0-9.]+-Academia-Linux\.zip"' \
            | grep -v -i 'preview' \
            | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' \
            | sort -V | tail -1)"
        if [ -z "${resolved}" ]; then
            warn "GitHub API unreachable — falling back to pinned version ${DIANN_VERSION}"
        else
            DIANN_VERSION="${resolved}"
            log "Newest DIA-NN stable release: ${DIANN_VERSION}"
        fi
    fi

    # Download binary if not already present
    if [ ! -x "${DIANN_DIR}/diann-linux" ]; then
        log "Downloading DIA-NN ${DIANN_VERSION} (~500 MB)..."
        mkdir -p "${DIANN_DIR}"
        local zipfile="${DIANN_DIR}/diann.zip"
        local url="https://github.com/vdemichev/DiaNN/releases/download/${DIANN_RELEASE_TAG}/DIA-NN-${DIANN_VERSION}-Academia-Linux.zip"
        wget --progress=bar -O "${zipfile}" "${url}"
        if [ ! -s "${zipfile}" ]; then
            err "DIA-NN download failed. Verify version ${DIANN_VERSION} exists at:"
            err "  https://github.com/vdemichev/DiaNN/releases"
            rm -f "${zipfile}"
            return 1
        fi
        log "Extracting..."
        unzip -q "${zipfile}" -d "${DIANN_DIR}/extract"
        local bin_src
        bin_src="$(find "${DIANN_DIR}/extract" -name diann-linux -type f | head -1)"
        if [ -z "${bin_src}" ]; then
            err "diann-linux not found in extracted archive."
            return 1
        fi
        local bin_dir
        bin_dir="$(dirname "${bin_src}")"
        mv "${bin_dir}"/* "${DIANN_DIR}/"
        rm -rf "${DIANN_DIR}/extract" "${zipfile}"
        chmod +x "${DIANN_DIR}/diann-linux"
    fi

    # Symlink onto PATH
    mkdir -p "${HOME}/.local/bin"
    ln -sf "${DIANN_DIR}/diann-linux" "${HOME}/.local/bin/diann"

    verify_diann_runtime
}

# =============================================================================
# 4. Sage (DDA search engine — static Linux binary, no license needed)
# =============================================================================
install_sage() {
    if [ -x "${SAGE_DIR}/sage" ]; then
        log "Sage already installed at ${SAGE_DIR}/sage"
        return 0
    fi

    log "Fetching latest Sage release tag from GitHub..."
    local tag
    tag="$(curl -s --max-time 15 \
        "https://api.github.com/repos/lazear/sage/releases/latest" \
        | grep -oE '"tag_name": "[^"]+"' \
        | grep -oE 'v[0-9]+\.[0-9]+\.[0-9]+')" || true

    if [ -z "${tag}" ]; then
        # Fallback to known good version
        tag="v0.14.7"
        warn "GitHub API unreachable — falling back to Sage ${tag}"
    fi
    log "Downloading Sage ${tag}..."

    mkdir -p "${SAGE_DIR}"
    local url="https://github.com/lazear/sage/releases/download/${tag}/sage-${tag}-x86_64-unknown-linux-gnu.tar.gz"
    local tarball="${SAGE_DIR}/sage.tar.gz"

    if ! wget --progress=bar -O "${tarball}" "${url}"; then
        # Try without the version prefix in the filename
        url="https://github.com/lazear/sage/releases/download/${tag}/sage-x86_64-unknown-linux-gnu.tar.gz"
        if ! wget --progress=bar -O "${tarball}" "${url}"; then
            err "Sage download failed. Check https://github.com/lazear/sage/releases"
            rm -f "${tarball}"
            return 1
        fi
    fi

    tar -xzf "${tarball}" -C "${SAGE_DIR}" --strip-components=1 2>/dev/null || \
        tar -xzf "${tarball}" -C "${SAGE_DIR}"
    rm -f "${tarball}"

    # Find sage binary wherever it landed
    local sage_bin
    sage_bin="$(find "${SAGE_DIR}" -name sage -type f | head -1)"
    if [ -z "${sage_bin}" ]; then
        err "sage binary not found in extracted archive."
        return 1
    fi
    chmod +x "${sage_bin}"
    # Normalize to expected path
    if [ "${sage_bin}" != "${SAGE_DIR}/sage" ]; then
        mv "${sage_bin}" "${SAGE_DIR}/sage"
    fi

    mkdir -p "${HOME}/.local/bin"
    ln -sf "${SAGE_DIR}/sage" "${HOME}/.local/bin/sage"

    ok "Sage installed: ${SAGE_DIR}/sage"
}

# =============================================================================
# 5. instruments.yml + community.yml config wizard (Mode B flavored)
# =============================================================================
# Prompts for the SMB share path and writes ~/.stan/instruments.yml.
# Skipped if ~/.stan/.mode_b_configured exists (idempotent re-runs).
configure_instruments() {
    if [ -f "${CONFIG_DONE_FLAG}" ] && [ -f "${STAN_BASE}/instruments.yml" ]; then
        log "Config already present (${STAN_BASE}/instruments.yml). Skipping wizard."
        log "To re-run wizard: bash ~/stan_wsl_setup.sh config"
        return 0
    fi

    mkdir -p "${STAN_BASE}"

    echo ""
    echo -e "${BLUE}=================== STAN Mode B Configuration ===================${NC}"
    echo "  STAN needs to know where your raw mass-spec files arrive on this box."
    echo ""
    echo "  Instrument PCs push raw files to an SMB share. On Windows that might"
    echo "  be a drive letter like Y:\\incoming or a UNC path like"
    echo "  \\\\labserver\\proteomics\\incoming."
    echo ""
    echo "  Inside WSL2 these appear as /mnt/<letter>/... or you can type the"
    echo "  UNC path and this script will suggest a mount point."
    echo ""
    echo "  Examples:"
    echo "    Windows:  Y:\\incoming"
    echo "    Windows:  \\\\labserver\\proteomics\\incoming"
    echo "    WSL path: /mnt/y/incoming"
    echo -e "${BLUE}=================================================================${NC}"
    echo ""

    local user_path wsl_watch_dir instrument_name
    while true; do
        read -r -p "Path to incoming raw files [leave blank for ~/stan_incoming]: " user_path

        # Strip trailing slashes/backslashes
        while [[ -n "${user_path}" && "${user_path: -1}" =~ [\\/] ]]; do
            user_path="${user_path%?}"
        done

        if [ -z "${user_path}" ]; then
            wsl_watch_dir="${HOME}/stan_incoming"
            mkdir -p "${wsl_watch_dir}"
            ok "Using default watch dir: ${wsl_watch_dir}"
            break
        fi

        # Tilde expansion
        user_path="${user_path/#\~/$HOME}"

        # Convert Windows path to WSL path
        if [[ "${user_path}" =~ ^[A-Za-z]:[\\/].* ]]; then
            wsl_watch_dir="$(wslpath -u "${user_path}" 2>/dev/null || true)"
            if [ -z "${wsl_watch_dir}" ]; then
                warn "Could not convert '${user_path}' to a WSL path."
                warn "Is that drive letter mounted? Check: ls /mnt/"
                continue
            fi
        elif [[ "${user_path}" =~ ^\\\\\\\\.*|^//[^/].* ]]; then
            # UNC path — user needs to mount it first
            echo ""
            warn "UNC paths (\\\\server\\share) must be mounted in WSL first."
            echo "  Run these commands in an Ubuntu terminal:"
            echo "    sudo mkdir -p /mnt/smb_incoming"
            echo "    sudo mount -t drvfs '${user_path}' /mnt/smb_incoming"
            echo "  Then re-run this setup with watch dir: /mnt/smb_incoming"
            echo ""
            read -r -p "Or enter the already-mounted WSL path now: " user_path
            wsl_watch_dir="${user_path}"
        else
            wsl_watch_dir="${user_path}"
        fi

        if ! mkdir -p "${wsl_watch_dir}" 2>/dev/null; then
            warn "Cannot create '${wsl_watch_dir}'."
            warn "Is the drive mounted? Check: ls /mnt/"
            warn "Try a different path or blank to use the default."
            continue
        fi

        ok "Watch directory set to: ${wsl_watch_dir}"
        break
    done

    echo ""
    read -r -p "Instrument name (e.g. 'timsTOF HT', 'Exploris 480') [leave blank for 'Lab Instrument']: " instrument_name
    if [ -z "${instrument_name}" ]; then
        instrument_name="Lab Instrument"
    fi

    # Write instruments.yml — Mode B defaults:
    #   processing_mode: local  (not hive — we are the compute node)
    #   raw_handling: convert_mzml  (Sage needs mzML for Thermo .raw)
    # DIANN_BINARY and SAGE_BINARY point at the paths this script installs.
    cat > "${STAN_BASE}/instruments.yml" <<YAML
# STAN Mode B — WSL2 lab box configuration
# Generated by stan_wsl_setup.sh — edit as needed.
#
# processing_mode: local  means STAN runs DIA-NN + Sage on this machine
# raw_handling: convert_mzml  means Thermo .raw files go through
#               ThermoRawFileParser -> mzML before Sage (Sage needs mzML;
#               DIA-NN 2.x reads .raw natively so this only affects DDA).

instruments:
- name: "${instrument_name}"
  watch_dir: "${wsl_watch_dir}"
  enabled: true
  processing_mode: local
  raw_handling: convert_mzml
  hela_amount_ng: 50.0
  community_submit: false
  stable_secs: 60
  exclude_pattern: '(?i)(wash|blank|blnk|blk|tune)'

# Tool paths (set by stan_wsl_setup.sh install):
diann_binary: "${DIANN_DIR}/diann-linux"
sage_binary: "${SAGE_DIR}/sage"
YAML

    # Write a minimal community.yml (opt-in to benchmark submission later)
    if [ ! -f "${STAN_BASE}/community.yml" ]; then
        cat > "${STAN_BASE}/community.yml" <<YAML
# STAN community benchmark — set submit: true to opt in
submit: false
lab_name: ""
instrument_serial: ""
YAML
    fi

    date > "${CONFIG_DONE_FLAG}"
    ok "Config written to ${STAN_BASE}/instruments.yml"
    echo ""
    echo "  Edit ${STAN_BASE}/instruments.yml to:"
    echo "    - Add more watch directories"
    echo "    - Set community_submit: true to join the community benchmark"
    echo "    - Adjust stable_secs for your instrument's write pattern"
    echo ""
}

# =============================================================================
# 6. Source env (DIA-NN LD_LIBRARY_PATH + ~/.local/bin on PATH)
# =============================================================================
source_env() {
    export PATH="${HOME}/.local/bin:${VENV_DIR}/bin:${PATH}"
    if [ -d "${DIANN_DIR}" ]; then
        export LD_LIBRARY_PATH="${DIANN_DIR}:${LD_LIBRARY_PATH:-}"
        export DOTNET_ROOT="${DOTNET_ROOT:-/usr/share/dotnet}"
    fi
}

# =============================================================================
# 7. Run: dashboard + watcher
# =============================================================================
run_stan() {
    source_env

    if [ ! -x "${STAN_BIN}" ]; then
        err "stan not found at ${STAN_BIN}. Run install first."
        exit 1
    fi

    # Initialize ~/.stan/ with default thresholds.yml if missing
    if [ ! -f "${STAN_BASE}/thresholds.yml" ]; then
        log "Initializing STAN config directory..."
        "${STAN_BIN}" init 2>/dev/null || true
    fi

    log "Starting STAN dashboard on http://localhost:${PORT}..."
    # Bind to 0.0.0.0 so the Windows host can reach it via localhost forwarding
    "${STAN_BIN}" dashboard --host 0.0.0.0 --port "${PORT}" &
    DASHBOARD_PID=$!

    # Give dashboard a moment to bind
    sleep 3

    log "Starting STAN watcher..."
    log "  Config: ${STAN_BASE}/instruments.yml"
    log "  Watch:  $(grep 'watch_dir' "${STAN_BASE}/instruments.yml" 2>/dev/null | head -1 | sed 's/.*: //')"
    log ""
    log "Press Ctrl+C to stop STAN."
    log ""
    echo -e "${GREEN}[stan]${NC} STAN dashboard ready at http://localhost:${PORT}"
    echo ""

    # Run watcher — blocks until Ctrl+C
    "${STAN_BIN}" watch --no-keep-awake || true

    # Clean up dashboard when watcher stops
    kill "${DASHBOARD_PID}" 2>/dev/null || true
    wait "${DASHBOARD_PID}" 2>/dev/null || true
}

# =============================================================================
# Dispatch
# =============================================================================
CMD="${1:-auto}"
case "${CMD}" in
    install)
        check_disk_space
        install_system_deps
        install_stan_python
        install_dotnet8_sdk
        install_dotnet_system_deps
        install_diann
        install_sage
        configure_instruments
        ok "Install complete. Run: bash $0 (or double-click Launch_STAN_WSL.bat)"
        ;;

    update)
        source_env
        install_stan_python
        install_sage
        # Re-run .NET system deps in case Ubuntu updated libicu
        install_dotnet_system_deps
        if [ -x "${DIANN_DIR}/diann-linux" ]; then
            install_dotnet8_sdk || true
            verify_diann_runtime || warn "DIA-NN runtime check failed — re-run: bash $0 diann"
        fi
        ok "Update complete."
        ;;

    watch)
        source_env
        log "Starting STAN watcher..."
        "${STAN_BIN}" watch --no-keep-awake
        ;;

    dashboard)
        source_env
        log "Starting STAN dashboard on http://localhost:${PORT}..."
        "${STAN_BIN}" dashboard --host 0.0.0.0 --port "${PORT}"
        ;;

    diann)
        install_dotnet8_sdk
        install_dotnet_system_deps
        install_diann
        ;;

    config)
        rm -f "${CONFIG_DONE_FLAG}"
        configure_instruments
        ;;

    auto)
        # Idempotent: each step checks whether it needs to run.
        # Disk check only when something big is missing.
        if [ ! -x "${STAN_BIN}" ] || [ ! -x "${DIANN_DIR}/diann-linux" ]; then
            check_disk_space
        fi

        # apt: cheap when already installed (~2s); catches new deps after updates
        install_system_deps

        # venv + STAN: pip skips when already current
        install_stan_python

        # .NET 8 system deps: always run (idempotent, ~1s when installed)
        install_dotnet_system_deps

        # DIA-NN: install if binary missing; verify every time
        if [ ! -x "${DIANN_DIR}/diann-linux" ]; then
            install_diann
        else
            install_dotnet8_sdk || true
            install_dotnet_system_deps
            verify_diann_runtime || warn "DIA-NN runtime check failed — run: bash $0 diann"
        fi

        # Sage: install if binary missing
        if [ ! -x "${SAGE_DIR}/sage" ]; then
            install_sage
        fi

        # Config wizard: runs once, then skipped
        configure_instruments

        run_stan
        ;;

    *)
        echo "Usage: bash $0 [install|update|watch|dashboard|diann|config]"
        echo "  install    — install all dependencies + configure"
        echo "  update     — pip upgrade STAN + refresh tool checks"
        echo "  watch      — start stan watch only"
        echo "  dashboard  — start stan dashboard only"
        echo "  diann      — install/reinstall DIA-NN (re-prompts for license)"
        echo "  config     — re-run SMB share + instruments.yml wizard"
        exit 1
        ;;
esac
