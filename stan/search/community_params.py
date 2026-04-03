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
# Do not upgrade these without also incrementing SEARCH_PARAMS_VERSION.
PINNED_TOOL_VERSIONS = {
    "diann": "2.3.2",
    "sage": "0.14.7",
    "thermorawfileparser": "1.4.5",
}

# HF Dataset repo where frozen community assets live
HF_DATASET_REPO = "brettsp/stan-benchmark"

# ── Frozen community FASTA (shared by both tracks) ────────────────────

COMMUNITY_FASTA_HF_PATH = "community_fasta/human_hela_202604.fasta"

# ── Frozen HeLa-specific predicted spectral libraries (Track B, DIA) ─
# These are SMALL — HeLa-only, not full human proteome. This keeps search
# fast (minutes, not hours) and results standardized across labs.

COMMUNITY_SPECLIB = {
    "bruker": {
        "hf_path": "community_library/hela_timstof_202604.predicted.speclib",
        "description": "HeLa predicted speclib for timsTOF (TIMS-CID fragmentation)",
    },
    "thermo": {
        "hf_path": "community_library/hela_orbitrap_202604.predicted.speclib",
        "description": "HeLa predicted speclib for Orbitrap (HCD fragmentation)",
    },
}

# Local cache directory on Hive for downloaded community assets
# This gets created by the SLURM job if it doesn't exist
COMMUNITY_CACHE_DIR = "/hive/data/stan_community_assets"

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

COMMUNITY_SAGE_SLURM: dict = {
    "partition": "{hive_partition}",
    "account": "{hive_account}",
    "mem": "32G",
    "cpus-per-task": 8,
    "time": "02:00:00",
    "job-name": "stan-sage-{run_name}",
}


def get_community_sage_params(cache_dir: str | None = None) -> dict:
    """Get the full frozen Sage parameters with FASTA path resolved.

    Args:
        cache_dir: Override for the local cache directory on Hive.

    Returns:
        Complete Sage parameter dict.
    """
    import copy

    cache = cache_dir or COMMUNITY_CACHE_DIR
    fasta_filename = COMMUNITY_FASTA_HF_PATH.split("/")[-1]

    params = copy.deepcopy(COMMUNITY_SAGE_PARAMS)
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
        f"  huggingface-cli download {HF_DATASET_REPO} "
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
            f"  huggingface-cli download {HF_DATASET_REPO} "
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
