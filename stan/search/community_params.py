"""Frozen community-standardized search parameters for benchmark submissions.

The community search uses small HeLa-specific predicted spectral libraries —
one for timsTOF (TIMS-CID fragmentation) and one for Orbitrap (HCD fragmentation).
These are hosted in the HF Dataset repo and downloaded to a local cache on Hive.

CRITICAL: These libraries and FASTA are NOT user-configurable for community
benchmark submissions. Changing them would invalidate cross-lab comparisons.
The whole point of STAN's benchmark is that every lab searches the same library
with the same parameters — precursor counts are only comparable when everything
upstream is identical.

Do not change any value below without:
  1. Incrementing SEARCH_PARAMS_VERSION
  2. Uploading the new library/FASTA to the HF Dataset repo
  3. Migrating or versioning old submissions
"""

SEARCH_PARAMS_VERSION = "v1.0.0"

# Pinned search engine versions for community benchmark reproducibility.
# Submissions that did not use these exact versions are rejected by the
# community relay. This is the only way to ensure cross-lab comparability —
# different DIA-NN versions produce meaningfully different results.
#
# For commercial users who cannot use DIA-NN 2.x (which requires a paid
# license for commercial use), STAN still works for local QC with any
# DIA-NN version — only community benchmark submission requires the
# pinned version.
#
# Do not upgrade these without also incrementing SEARCH_PARAMS_VERSION
# and rebuilding/re-uploading the community libraries.
PINNED_TOOL_VERSIONS = {
    "diann": "2.3.0",   # used to build hela_orbitrap_202604.parquet and hela_timstof_202604.parquet
    "sage": "0.14.7",
    "thermorawfileparser": "1.4.5",
}


def check_diann_version_compatible(version: str | None) -> tuple[bool, str]:
    """Check if a DIA-NN version matches the pinned community version.

    Returns (is_compatible, message).
    """
    if not version:
        return False, "DIA-NN version could not be detected"

    required = PINNED_TOOL_VERSIONS["diann"]

    # Match major.minor at minimum — allow patch differences within same minor
    req_parts = required.split(".")
    ver_parts = version.split(".")

    if len(req_parts) < 2 or len(ver_parts) < 2:
        return False, f"Invalid version format: {version}"

    if req_parts[0] != ver_parts[0] or req_parts[1] != ver_parts[1]:
        return False, (
            f"DIA-NN version mismatch: submission used {version}, "
            f"community benchmark requires {required}. "
            f"Different DIA-NN versions produce different results and cannot be "
            f"compared. Use DIA-NN {required} for community submissions."
        )

    return True, f"DIA-NN {version} matches pinned version {required}"

# HF Dataset repo where frozen community assets live
HF_DATASET_REPO = "brettsp/stan-benchmark"

# ── Frozen community FASTA (shared by both tracks) ────────────────────

COMMUNITY_FASTA_HF_PATH = "community_fasta/human_hela_202604.fasta"

# ── Frozen HeLa-specific empirical spectral libraries (Track B, DIA) ──
# Built from real HeLa DIA runs — empirical RTs, fragment intensities, and
# (for timsTOF) ion mobility values. HeLa-only (~45k precursors), not full
# human proteome. This keeps search fast (minutes, not hours).
#
# Format: .parquet (DIA-NN 2.0+ empirical library format, also accepted
# by --lib for searching). NOT .predicted.speclib (binary predicted format).

COMMUNITY_SPECLIB = {
    "bruker": {
        "hf_path": "community_library/hela_timstof_202604.parquet",
        "description": "HeLa empirical library for timsTOF (TIMS-CID, with IM)",
    },
    "thermo": {
        "hf_path": "community_library/hela_orbitrap_202604.parquet",
        "description": "HeLa empirical library for Orbitrap/Astral (HCD)",
    },
}

# Total precursor count in each frozen community library. Used to
# compute library_coverage_pct on each submission and warn when a lab
# is saturating the library (n_precursors / library_size > ~0.9 means
# the library is the bottleneck, not the instrument). Update these
# alongside SEARCH_PARAMS_VERSION whenever the library is rebuilt.
COMMUNITY_LIBRARY_PRECURSOR_COUNT = {
    "bruker": 54_000,    # hela_timstof_202604.parquet
    "thermo": 170_000,   # hela_orbitrap_202604.parquet
}
SATURATION_THRESHOLD_PCT = 0.90

# Local cache directory on Hive for downloaded community assets.
# Must be writable by the SLURM job — /hive/data/ is read-only for
# brettsp's account, so the default points to a writable location
# under the proteomics-grp project tree. Override per-call when
# running with a different account.
COMMUNITY_CACHE_DIR = "/quobyte/proteomics-grp/brett/stan_community_assets"

# ── DIA-NN parameters (Track B) ──────────────────────────────────────
# The --lib flag is set dynamically based on instrument vendor.
# The --fasta flag points to the shared community FASTA.
# Neither can be overridden for community benchmark submissions.

COMMUNITY_DIANN_PARAMS_FROZEN: dict = {
    # lib and fasta are set dynamically — see get_community_diann_params()
    "qvalue": 0.01,
    "protein-q": 0.01,
    "min-pep-len": 7,
    "max-pep-len": 30,
    "missed-cleavages": 1,
    "min-pr-charge": 2,
    "max-pr-charge": 4,
    "cut": "K*,R*",
    "threads": 8,
}

COMMUNITY_DIANN_SLURM: dict = {
    "partition": "{hive_partition}",
    "account": "{hive_account}",
    "mem": "32G",
    "cpus-per-task": 8,
    "time": "02:00:00",
    "job-name": "stan-diann-{run_name}",
}


def get_community_diann_params(vendor: str, cache_dir: str | None = None) -> dict:
    """Get the full frozen DIA-NN parameters for a given instrument vendor.

    Args:
        vendor: "bruker" or "thermo" — determines which speclib to use.
        cache_dir: Override for the local cache directory on Hive.

    Returns:
        Complete DIA-NN parameter dict with lib and fasta paths resolved.
    """
    cache = cache_dir or COMMUNITY_CACHE_DIR

    speclib_info = COMMUNITY_SPECLIB.get(vendor)
    if speclib_info is None:
        raise ValueError(
            f"No community speclib for vendor '{vendor}'. "
            f"Supported: {list(COMMUNITY_SPECLIB.keys())}"
        )

    # Paths point to the local cache on Hive (downloaded by SLURM job)
    speclib_filename = speclib_info["hf_path"].split("/")[-1]
    fasta_filename = COMMUNITY_FASTA_HF_PATH.split("/")[-1]

    params = dict(COMMUNITY_DIANN_PARAMS_FROZEN)
    params["lib"] = f"{cache}/{speclib_filename}"
    params["fasta"] = f"{cache}/{fasta_filename}"

    return params


# ── Sage parameters (Track A) ────────────────────────────────────────
# Sage uses the community FASTA directly (no speclib needed for DDA).

COMMUNITY_SAGE_PARAMS: dict = {
    "database": {
        # fasta path set dynamically — see get_community_sage_params()
        "enzyme": {
            "missed_cleavages": 1,
            "min_len": 7,
            "max_len": 30,
            "cleave_at": "KR",
            "restrict": "P",
        },
        "static_mods": {"C": 57.0215},
        "variable_mods": {"M": [15.9949]},
        "max_variable_mods": 2,
    },
    "precursor_tol": {"ppm": [-10, 10]},
    "fragment_tol": {"ppm": [-20, 20]},
    "min_peaks": 8,
    "max_peaks": 150,
    "min_matched_peaks": 4,
    "target_fdr": 0.01,
    "deisotope": True,
}

# Tribrid ion-trap MS2 variant. Lumos / Eclipse / Ascend / Tribrid
# Fusion can acquire DDA MS2 in the ion trap (ITMS) which has
# fundamentally low-resolution fragment masses (~0.4-0.6 Da). With
# the OT-only ±20 ppm fragment_tol above, IT-acquired runs return
# 0 PSMs at 1% FDR. ±0.5 Da is the cross-vendor standard for ITMS
# (MaxQuant Andromeda, MS-GF+, Comet, Mascot defaults) — tolerates
# normal calibration drift while still filtering noise. Brett vetted
# this 2026-05-01.
#
# Note: OT-IT cohort is a SEPARATE leaderboard track from OT-OT —
# different fragment accuracy means PSM counts aren't directly
# comparable. Submission schema's `ms2_analyzer` field carries the
# split through to the dashboard.
COMMUNITY_SAGE_PARAMS_IT: dict = {
    **COMMUNITY_SAGE_PARAMS,
    "fragment_tol": {"da": [-0.5, 0.5]},
}

COMMUNITY_SAGE_SLURM: dict = {
    "partition": "{hive_partition}",
    "account": "{hive_account}",
    "mem": "32G",
    "cpus-per-task": 8,
    "time": "02:00:00",
    "job-name": "stan-sage-{run_name}",
}


def get_community_sage_params(
    cache_dir: str | None = None,
    ms2_analyzer: str = "OT",
) -> dict:
    """Get the full frozen Sage parameters with FASTA path resolved.

    Args:
        cache_dir: Override for the local cache directory on Hive.
        ms2_analyzer: "OT" for orbitrap MS2 (default; ±20 ppm fragment_tol)
            or "IT" for ion-trap MS2 (±0.5 Da). Tribrid Lumos/Eclipse/
            Ascend can switch between the two; detect via
            ``stan.tools.trfp.detect_ms2_analyzer`` before calling.

    Returns:
        Complete Sage parameter dict.
    """
    import copy

    cache = cache_dir or COMMUNITY_CACHE_DIR
    fasta_filename = COMMUNITY_FASTA_HF_PATH.split("/")[-1]

    base = COMMUNITY_SAGE_PARAMS_IT if ms2_analyzer == "IT" else COMMUNITY_SAGE_PARAMS
    params = copy.deepcopy(base)
    params["database"]["fasta"] = f"{cache}/{fasta_filename}"

    return params


def build_asset_download_script(vendor: str, cache_dir: str | None = None) -> str:
    """Build shell commands to download frozen community assets from HF Dataset.

    This block goes at the top of the SLURM job script, before the search.
    Uses huggingface-cli to download the speclib and FASTA if not already cached.

    Args:
        vendor: "bruker" or "thermo".
        cache_dir: Override for cache directory.

    Returns:
        Shell script fragment for embedding in SLURM scripts.
    """
    cache = cache_dir or COMMUNITY_CACHE_DIR
    speclib_info = COMMUNITY_SPECLIB.get(vendor, {})
    speclib_hf_path = speclib_info.get("hf_path", "")
    speclib_filename = speclib_hf_path.split("/")[-1] if speclib_hf_path else ""
    fasta_filename = COMMUNITY_FASTA_HF_PATH.split("/")[-1]

    lines = [
        f"# Download frozen community search assets (if not cached)",
        f"mkdir -p {cache}",
        f"",
    ]

    # FASTA
    lines.append(f"if [ ! -f {cache}/{fasta_filename} ]; then")
    lines.append(f"  echo 'Downloading community FASTA...'")
    lines.append(
        f"  hf download {HF_DATASET_REPO} "
        f"{COMMUNITY_FASTA_HF_PATH} "
        f"--repo-type dataset "
        f"--local-dir {cache}"
    )
    lines.append(f"  # Flatten: move from subdir to cache root")
    lines.append(f"  mv {cache}/{COMMUNITY_FASTA_HF_PATH} {cache}/{fasta_filename} 2>/dev/null || true")
    lines.append(f"fi")
    lines.append(f"")

    # Speclib (DIA only)
    if speclib_hf_path:
        lines.append(f"if [ ! -f {cache}/{speclib_filename} ]; then")
        lines.append(f"  echo 'Downloading community speclib ({vendor})...'")
        lines.append(
            f"  hf download {HF_DATASET_REPO} "
            f"{speclib_hf_path} "
            f"--repo-type dataset "
            f"--local-dir {cache}"
        )
        lines.append(f"  mv {cache}/{speclib_hf_path} {cache}/{speclib_filename} 2>/dev/null || true")
        lines.append(f"fi")
        lines.append(f"")

    lines.append(f"echo 'Community assets ready in {cache}'")
    lines.append(f"ls -lh {cache}/")
    lines.append(f"")

    return "\n".join(lines)
