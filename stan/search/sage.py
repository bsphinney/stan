"""Sage SLURM job builder and submission (Track A — DDA).

Sage reads Bruker .d natively (confirmed working for ddaPASEF).
Thermo .raw requires ThermoRawFileParser → mzML conversion before Sage.

Uses frozen community FASTA — cannot be overridden for benchmark submissions.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from stan.search.community_params import (
    COMMUNITY_SAGE_SLURM,
    build_asset_download_script,
    get_community_sage_params,
)
from stan.search.convert import (
    build_cleanup_script,
    build_thermo_conversion_script,
    get_mzml_path,
)
from stan.search.slurm import SlurmClient

logger = logging.getLogger(__name__)


def build_sage_config_json(
    raw_path: Path,
    output_dir: Path,
    vendor: str,
    params: dict | None = None,
) -> str:
    """Build the Sage JSON config file content.

    For Bruker .d: passes .d path directly (Sage reads natively).
    For Thermo .raw: passes expected .mzML path (conversion done before Sage runs).

    The community FASTA is frozen and cannot be overridden.
    """
    p = get_community_sage_params()
    if params:
        # Safety: never allow overriding FASTA for community search
        import copy
        safe = copy.deepcopy(params)
        if "database" in safe:
            safe["database"].pop("fasta", None)
        p.update(safe)

    # Determine input file path for Sage
    if vendor == "thermo":
        input_path = str(get_mzml_path(raw_path, output_dir))
    else:
        input_path = str(raw_path)

    config = {
        **p,
        "mzml_paths": [input_path],
        "output_directory": str(output_dir),
    }

    return json.dumps(config, indent=2)


def build_sage_slurm_script(
    raw_path: Path,
    output_dir: Path,
    instrument_config: dict,
) -> str:
    """Build a complete SLURM batch script for Sage search.

    Includes:
    - Community asset download (FASTA from HF Dataset)
    - ThermoRawFileParser conversion step for Thermo DDA only
    - Sage search
    - mzML cleanup (Thermo only)
    """
    run_name = raw_path.stem
    vendor = instrument_config.get("vendor", "")
    partition = instrument_config.get("hive_partition", "high")
    account = instrument_config.get("hive_account", "")
    mem = instrument_config.get("hive_mem", COMMUNITY_SAGE_SLURM["mem"])

    slurm_params = dict(COMMUNITY_SAGE_SLURM)
    slurm_params["partition"] = partition
    slurm_params["account"] = account
    slurm_params["mem"] = mem
    slurm_params["job-name"] = f"stan-sage-{run_name}"

    sbatch_lines = "\n".join(
        f"#SBATCH --{k}={v}" for k, v in slurm_params.items()
    )

    # Asset download (FASTA only — Sage doesn't use a speclib)
    download_block = build_asset_download_script(vendor)

    sage_config = build_sage_config_json(raw_path, output_dir, vendor)

    # Conversion step for Thermo DDA only
    conversion_block = ""
    cleanup_block = ""
    if vendor == "thermo":
        trfp_path = instrument_config.get(
            "trfp_path",
            "/hive/software/ThermoRawFileParser/ThermoRawFileParser.dll",
        )
        conversion_block = build_thermo_conversion_script(
            raw_path, output_dir, Path(trfp_path)
        )
        keep_mzml = instrument_config.get("keep_mzml", False)
        cleanup_block = build_cleanup_script(raw_path, output_dir, keep_mzml)

    return f"""#!/bin/bash
{sbatch_lines}

set -euo pipefail

echo "STAN Sage DDA search: {run_name}"
echo "Raw file: {raw_path}"
echo "Output: {output_dir}"

# Load .NET SDK for ThermoRawFileParser if needed
module load dotnet/8.0 2>/dev/null || true

mkdir -p {output_dir}

{download_block}

{conversion_block}# Write Sage config
cat > {output_dir}/sage_config.json << 'SAGE_CONFIG_EOF'
{sage_config}
SAGE_CONFIG_EOF

# Run Sage
sage {output_dir}/sage_config.json

echo "Sage search complete: {run_name}"

{cleanup_block}"""


def submit_sage_job(
    raw_path: Path,
    output_dir: Path,
    instrument_config: dict,
    hive_config: dict,
) -> str:
    """Submit a Sage search job to Hive via SLURM.

    Returns:
        SLURM job ID.
    """
    script = build_sage_slurm_script(raw_path, output_dir, instrument_config)

    with SlurmClient(
        host=hive_config.get("host", ""),
        user=hive_config.get("user", ""),
        key_path=hive_config.get("key_path"),
    ) as slurm:
        job_id = slurm.submit_job(script, str(output_dir))
        logger.info("Sage job submitted: %s (job %s)", raw_path.name, job_id)
        return job_id


def get_sage_output(output_dir: Path) -> Path:
    """Return the expected path to Sage results.sage.parquet output."""
    return output_dir / "results.sage.parquet"
