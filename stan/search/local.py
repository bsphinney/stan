"""Local search execution — runs DIA-NN and Sage directly on this machine.

For labs without HPC/SLURM access. DIA-NN and Sage run as local subprocesses
instead of being submitted as SLURM batch jobs.

Conversion pipeline for Thermo DDA:
  .raw → ThermoRawFileParser → .mzML → Sage

No conversion needed for:
  - DIA (any vendor): DIA-NN reads .raw and .d natively
  - Bruker DDA: Sage reads .d natively
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from stan.search.community_params import (
    get_community_diann_params,
    get_community_sage_params,
)
from stan.search.convert import get_mzml_path

logger = logging.getLogger(__name__)


def run_diann_local(
    raw_path: Path,
    output_dir: Path,
    vendor: str,
    diann_exe: str = "diann",
    threads: int = 0,
) -> Path | None:
    """Run DIA-NN locally as a subprocess.

    Args:
        raw_path: Path to .d directory or .raw file.
        output_dir: Output directory for results.
        vendor: "bruker" or "thermo".
        diann_exe: Path to diann executable (or just "diann" if on PATH).
        threads: Number of threads (0 = let DIA-NN decide).

    Returns:
        Path to report.parquet, or None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    params = get_community_diann_params(vendor, cache_dir=str(output_dir.parent / "_community_assets"))

    cmd = [diann_exe, "--f", str(raw_path), "--out", str(output_dir)]

    for key, val in params.items():
        cmd.extend([f"--{key}", str(val)])

    if threads > 0:
        # Override thread count for local execution
        cmd.extend(["--threads", str(threads)])

    logger.info("Running DIA-NN locally: %s", raw_path.name)
    logger.debug("Command: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=14400,  # 4 hour timeout for local execution
        )
        logger.info("DIA-NN complete: %s", raw_path.name)
    except FileNotFoundError:
        logger.error(
            "DIA-NN executable not found: %s. "
            "Install DIA-NN or add it to PATH.",
            diann_exe,
        )
        return None
    except subprocess.TimeoutExpired:
        logger.error("DIA-NN timed out after 4 hours: %s", raw_path.name)
        return None
    except subprocess.CalledProcessError as e:
        logger.error("DIA-NN failed: %s\nstderr: %s", raw_path.name, e.stderr[-500:])
        return None

    report = output_dir / "report.parquet"
    if report.exists():
        return report

    logger.error("DIA-NN output not found: %s", report)
    return None


def run_sage_local(
    raw_path: Path,
    output_dir: Path,
    vendor: str,
    sage_exe: str = "sage",
    trfp_exe: str | None = None,
    keep_mzml: bool = False,
    threads: int = 0,
) -> Path | None:
    """Run Sage locally as a subprocess.

    For Thermo DDA: converts .raw → mzML via ThermoRawFileParser first.
    For Bruker DDA: passes .d directly to Sage (reads natively).

    Args:
        raw_path: Path to .d directory or .raw file.
        output_dir: Output directory for results.
        vendor: "bruker" or "thermo".
        sage_exe: Path to sage executable (or just "sage" if on PATH).
        trfp_exe: Path to ThermoRawFileParser.exe (needed for Thermo DDA only).
        keep_mzml: Keep converted mzML files after search.
        threads: Number of threads (0 = let Sage decide).

    Returns:
        Path to results.sage.parquet, or None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    # Determine input path — convert Thermo .raw to mzML if needed
    if vendor == "thermo":
        mzml_path = _convert_raw_to_mzml(raw_path, output_dir, trfp_exe)
        if mzml_path is None:
            return None
        input_path = str(mzml_path)
    else:
        input_path = str(raw_path)

    # Build Sage JSON config
    params = get_community_sage_params(cache_dir=str(output_dir.parent / "_community_assets"))
    params["mzml_paths"] = [input_path]
    params["output_directory"] = str(output_dir)

    config_path = output_dir / "sage_config.json"
    config_path.write_text(json.dumps(params, indent=2))

    cmd = [sage_exe, str(config_path)]

    logger.info("Running Sage locally: %s", raw_path.name)
    logger.debug("Command: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=14400,
        )
        logger.info("Sage complete: %s", raw_path.name)
    except FileNotFoundError:
        logger.error(
            "Sage executable not found: %s. Install Sage or add it to PATH.",
            sage_exe,
        )
        return None
    except subprocess.TimeoutExpired:
        logger.error("Sage timed out after 4 hours: %s", raw_path.name)
        return None
    except subprocess.CalledProcessError as e:
        logger.error("Sage failed: %s\nstderr: %s", raw_path.name, e.stderr[-500:])
        return None
    finally:
        # Clean up mzML if requested
        if vendor == "thermo" and not keep_mzml:
            mzml = get_mzml_path(raw_path, output_dir)
            if mzml.exists():
                mzml.unlink()
                logger.debug("Cleaned up: %s", mzml)

    results = output_dir / "results.sage.parquet"
    if results.exists():
        return results

    logger.error("Sage output not found: %s", results)
    return None


def _convert_raw_to_mzml(
    raw_path: Path,
    output_dir: Path,
    trfp_exe: str | None,
) -> Path | None:
    """Convert a Thermo .raw file to indexed mzML using ThermoRawFileParser.

    Args:
        raw_path: Path to .raw file.
        output_dir: Output directory for .mzML file.
        trfp_exe: Path to ThermoRawFileParser.exe.

    Returns:
        Path to the generated .mzML file, or None on failure.
    """
    if trfp_exe is None:
        # Try to find it on PATH
        trfp_exe = shutil.which("ThermoRawFileParser") or shutil.which("ThermoRawFileParser.exe")

    if trfp_exe is None:
        logger.error(
            "ThermoRawFileParser not found. Required for Thermo DDA (.raw → mzML). "
            "Install it or set trfp_path in instruments.yml."
        )
        return None

    mzml_path = get_mzml_path(raw_path, output_dir)

    logger.info("Converting %s → mzML...", raw_path.name)

    cmd = [
        str(trfp_exe),
        f"-i={raw_path}",
        f"-o={output_dir}/",
        "-f=2",   # indexed mzML
        "-m=0",   # JSON metadata
    ]

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min timeout for conversion
        )
    except FileNotFoundError:
        logger.error("ThermoRawFileParser not found at: %s", trfp_exe)
        return None
    except subprocess.CalledProcessError as e:
        logger.error("Conversion failed: %s\nstderr: %s", raw_path.name, e.stderr[-500:])
        return None

    if mzml_path.exists():
        logger.info("Converted: %s → %s", raw_path.name, mzml_path.name)
        return mzml_path

    logger.error("mzML not found after conversion: %s", mzml_path)
    return None
