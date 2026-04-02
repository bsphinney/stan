"""DIA-NN SLURM job builder and submission (Track B — DIA).

Uses frozen community speclibs: one for timsTOF (TIMS-CID), one for Orbitrap (HCD).
The speclib is selected based on instrument vendor and CANNOT be overridden
for community benchmark submissions.
"""

from __future__ import annotations

import logging
from pathlib import Path

from stan.search.community_params import (
    COMMUNITY_DIANN_SLURM,
    build_asset_download_script,
    get_community_diann_params,
)
from stan.search.slurm import SlurmClient

logger = logging.getLogger(__name__)


def build_diann_command(
    raw_path: Path,
    output_dir: Path,
    vendor: str,
    params: dict | None = None,
) -> str:
    """Build the DIA-NN CLI command string.

    DIA-NN reads both Bruker .d and Thermo .raw natively — no conversion needed.

    Args:
        raw_path: Path to .d or .raw file.
        output_dir: Output directory for results.
        vendor: "bruker" or "thermo" — selects the correct community speclib.
        params: Optional overrides (NOT used for community benchmark submissions).
    """
    # Start from frozen community params (vendor-specific speclib)
    p = get_community_diann_params(vendor)
    if params:
        # Safety: never allow overriding lib or fasta for community search
        safe_overrides = {k: v for k, v in params.items() if k not in ("lib", "fasta")}
        p.update(safe_overrides)

    parts = ["diann"]
    parts.append(f"--f {raw_path}")
    parts.append(f"--out {output_dir}")
    for key, val in p.items():
        parts.append(f"--{key} {val}")

    return " \\\n  ".join(parts)


def build_diann_slurm_script(
    raw_path: Path,
    output_dir: Path,
    instrument_config: dict,
) -> str:
    """Build a complete SLURM batch script for DIA-NN search.

    Includes asset download step to cache the frozen community speclib + FASTA
    from the HF Dataset before running DIA-NN.
    """
    run_name = raw_path.stem
    vendor = instrument_config.get("vendor", "bruker")
    partition = instrument_config.get("hive_partition", "high")
    account = instrument_config.get("hive_account", "")
    mem = instrument_config.get("hive_mem", COMMUNITY_DIANN_SLURM["mem"])

    slurm_params = dict(COMMUNITY_DIANN_SLURM)
    slurm_params["partition"] = partition
    slurm_params["account"] = account
    slurm_params["mem"] = mem
    slurm_params["job-name"] = f"stan-diann-{run_name}"

    # Asset download script (fetches speclib + FASTA from HF if not cached)
    download_block = build_asset_download_script(vendor)

    diann_cmd = build_diann_command(raw_path, output_dir, vendor)

    sbatch_lines = "\n".join(
        f"#SBATCH --{k}={v}" for k, v in slurm_params.items()
    )

    return f"""#!/bin/bash
{sbatch_lines}

set -euo pipefail

echo "STAN DIA-NN search: {run_name}"
echo "Raw file: {raw_path}"
echo "Output: {output_dir}"
echo "Vendor: {vendor}"

# Load .NET SDK for DIA-NN on Linux
module load dotnet/8.0 2>/dev/null || true

mkdir -p {output_dir}

{download_block}

{diann_cmd}

echo "DIA-NN search complete: {run_name}"
"""


def submit_diann_job(
    raw_path: Path,
    output_dir: Path,
    instrument_config: dict,
    hive_config: dict,
) -> str:
    """Submit a DIA-NN search job to Hive via SLURM.

    Returns:
        SLURM job ID.
    """
    script = build_diann_slurm_script(raw_path, output_dir, instrument_config)

    with SlurmClient(
        host=hive_config.get("host", ""),
        user=hive_config.get("user", ""),
        key_path=hive_config.get("key_path"),
    ) as slurm:
        job_id = slurm.submit_job(script, str(output_dir))
        logger.info("DIA-NN job submitted: %s (job %s)", raw_path.name, job_id)
        return job_id


def get_diann_output(output_dir: Path) -> Path:
    """Return the expected path to DIA-NN report.parquet output."""
    return output_dir / "report.parquet"
