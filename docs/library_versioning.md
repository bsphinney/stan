# Community Library Versioning Strategy

STAN's community benchmark depends on every lab using the exact same frozen
library. Different DIA-NN versions produce meaningfully different results,
so when DIA-NN upgrades, the community libraries need to be rebuilt and
re-frozen. This document is the migration runbook.

## Naming Convention

All community assets are **date-stamped** with the year and month they were built:

```
community_library/hela_orbitrap_YYYYMM.parquet
community_library/hela_timstof_YYYYMM.parquet
community_fasta/human_hela_YYYYMM.fasta
```

Current active set: **202604** (April 2026, DIA-NN 2.3.0)

## When to Rebuild

Rebuild the community libraries when any of these change:

1. **DIA-NN major or minor version** (e.g., 2.3 → 2.4 or 2.x → 3.x)
   — different neural network, different RT/IM/intensity predictions
2. **FASTA source** (e.g., UniProt release update, contaminant list change)
3. **Search parameters** (charge range, peptide length, modifications)
   — rare, but would break comparability

Patch updates within the same minor (2.3.0 → 2.3.2) usually do NOT require
a rebuild. Verify by re-searching one test file and confirming IDs are within 1%.

## Migration Runbook

When DIA-NN 2.4 (hypothetical) is released and needs to become the new community standard:

### 1. Build new libraries on Hive

```bash
# Update build_astral_hela_lib.sbatch to use new container
IMG=/quobyte/proteomics-grp/dia-nn/diann_2.4.0.sif

# Output to new date-stamped path
OUTDIR=${BASE}/astral_hela_lib_202510   # Oct 2025 for example

# Run the build
sbatch build_astral_hela_lib.sbatch

# Same for timsTOF
sbatch build_timstof_lib.sbatch
```

### 2. Upload new libraries to HF Dataset

```python
from huggingface_hub import HfApi
import hashlib

api = HfApi(token=HF_TOKEN)

# Upload new libraries
api.upload_file(
    path_or_fileobj="report-lib.parquet",
    path_in_repo="community_library/hela_orbitrap_202510.parquet",
    repo_id="brettsp/stan-benchmark",
    repo_type="dataset",
)
# Same for timsTOF

# Compute and record MD5 hashes
# Paste into EXPECTED_ASSET_HASHES in stan/community/validate.py
```

### 3. Update STAN code (single constant change)

Edit `stan/search/community_params.py`:

```python
# Bump version
SEARCH_PARAMS_VERSION = "v2.0.0"   # increment major for incompatible libraries

# Update pinned version
PINNED_TOOL_VERSIONS = {
    "diann": "2.4.0",   # was 2.3.0
    ...
}

# Update library paths
COMMUNITY_SPECLIB = {
    "bruker": {
        "hf_path": "community_library/hela_timstof_202510.parquet",   # was 202604
        ...
    },
    "thermo": {
        "hf_path": "community_library/hela_orbitrap_202510.parquet",  # was 202604
        ...
    },
}
```

Edit `stan/community/validate.py`:

```python
EXPECTED_ASSET_HASHES = {
    "hela_timstof_202510.parquet": "new_md5_here",
    "hela_orbitrap_202510.parquet": "new_md5_here",
    # Keep old hashes too — old submissions remain valid historical data
    "hela_timstof_202604.parquet": "old_md5...",
    "hela_orbitrap_202604.parquet": "old_md5...",
}
```

### 4. Update HF Space relay

Edit `app.py` on `brettsp/stan` Space:

```python
PINNED_DIANN_VERSION = "2.4"   # was "2.3"
```

Push to Space. Space rebuilds automatically.

### 5. Handle existing submissions

**Do not delete old submissions.** They remain historical data under the old
`SEARCH_PARAMS_VERSION`. The dashboard can filter by version:

- "Current" view: only submissions matching current `SEARCH_PARAMS_VERSION`
- "Historical" view: all submissions grouped by version

Add a deprecation notice in the dashboard for old versions: "These submissions
used DIA-NN 2.3. Current benchmark uses DIA-NN 2.4. Re-submit with the new
version for inclusion in current cohort stats."

### 6. Announce the change

Post to the STAN GitHub releases page and update the HF Space homepage:
- New DIA-NN version
- New library MD5 hashes
- Migration instructions for existing users
- Expected impact on ID counts (should be minor — within 10% usually)

## Things That Stay The Same Across Versions

- HeLa standard (Pierce 88328)
- FASTA source (UniProt reviewed human + contaminants)
- Cohort structure (instrument × SPD × amount × column)
- Fingerprint format for dedup
- HF Dataset repo URL

## Things That MUST Be Updated Together

These four files must always agree on the active version:

| File | What to update |
|------|---------------|
| `stan/search/community_params.py` | `SEARCH_PARAMS_VERSION`, `PINNED_TOOL_VERSIONS`, `COMMUNITY_SPECLIB` paths |
| `stan/community/validate.py` | `EXPECTED_ASSET_HASHES` (add new, keep old) |
| HF Space `app.py` | `PINNED_DIANN_VERSION` |
| HF Dataset | Upload new library files |

If any of these drift, submissions will silently fail or produce wrong results.

## Why This Design Is Future-Proof

1. **Date-stamped filenames** mean old libraries never get overwritten
2. **Single source of truth** (`community_params.py`) for the active version
3. **Old submissions remain valid historical data** — nothing is lost
4. **Version tagging on every submission** means the dashboard can filter,
   compare, or deprecate by version
5. **Relay version check** enforces consistency at the server level
6. **Migration is a single PR** — four files, clear pattern, reproducible
