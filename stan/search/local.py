"""Local search execution — runs DIA-NN and Sage directly on this machine.

This is the default execution mode. DIA-NN and Sage run as local subprocesses
on the instrument workstation. No SLURM cluster required.

Two search modes:
  - "local" (default): User provides their own FASTA via fasta_path in
    instruments.yml. DIA-NN runs library-free (predicted from FASTA) unless
    the user also provides a lib_path.
  - "community": Uses frozen community search assets from the HF Dataset.
    Requires assets to be cached locally.

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

from stan.search.convert import get_mzml_path

logger = logging.getLogger(__name__)


def _build_local_diann_params(
    fasta_path: str,
    lib_path: str | None = None,
) -> dict:
    """Build DIA-NN parameters for local mode with user-provided FASTA.

    If no lib_path is provided, DIA-NN runs in library-free mode:
    it generates a predicted spectral library from the FASTA.
    """
    params: dict = {
        "fasta": fasta_path,
        "qvalue": 0.01,
        "min-pep-len": 7,
        "max-pep-len": 30,
        "missed-cleavages": 1,
        "min-pr-charge": 2,
        "max-pr-charge": 4,
        "cut": "K*,R*",
    }

    if lib_path:
        params["lib"] = lib_path
    else:
        # Library-free mode: predict from FASTA
        params["fasta-search"] = ""
        params["predictor"] = ""

    return params


def _build_local_sage_params(
    fasta_path: str,
) -> dict:
    """Build Sage JSON config for local mode with user-provided FASTA."""
    return {
        "database": {
            "fasta": fasta_path,
            "enzyme": {"cleave_at": "KR", "restrict": "P", "missed_cleavages": 1},
            "min_len": 7,
            "max_len": 30,
            "static_mods": {"C": 57.0215},
            "variable_mods": {"M": [15.9949]},
        },
        "precursor_tol": {"ppm": [-10, 10]},
        "fragment_tol": {"ppm": [-20, 20]},
        "precursor_charge": [2, 4],
        "min_peaks": 8,
        "max_peaks": 150,
        "report_psms": 1,
        "wide_window": False,
    }


def run_diann_local(
    raw_path: Path,
    output_dir: Path,
    vendor: str,
    diann_exe: str = "diann",
    threads: int = 0,
    fasta_path: str | None = None,
    lib_path: str | None = None,
    search_mode: str = "local",
) -> Path | None:
    """Run DIA-NN locally as a subprocess.

    Args:
        raw_path: Path to .d directory or .raw file.
        output_dir: Output directory for results.
        vendor: "bruker" or "thermo".
        diann_exe: Path to diann executable (or just "diann" if on PATH).
        threads: Number of threads (0 = let DIA-NN decide).
        fasta_path: Path to FASTA file (required for local mode).
        lib_path: Path to spectral library (optional — DIA-NN runs library-free if omitted).
        search_mode: "local" (user FASTA) or "community" (frozen HF assets).

    Returns:
        Path to report.parquet, or None on failure.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    if search_mode == "community":
        from stan.search.community_params import get_community_diann_params
        params = get_community_diann_params(vendor, cache_dir=str(output_dir.parent / "_community_assets"))
    else:
        if not fasta_path:
            logger.error(
                "No fasta_path configured for instrument. "
                "Set fasta_path in instruments.yml or run `stan setup`."
            )
            return None
        if not Path(fasta_path).exists():
            logger.error("FASTA file not found: %s", fasta_path)
            return None
        params = _build_local_diann_params(fasta_path, lib_path)

    report_path = output_dir / "report.parquet"
    cmd = [diann_exe, "--f", str(raw_path), "--out", str(report_path)]

    for key, val in params.items():
        if val == "":
            cmd.append(f"--{key}")  # flag-only params like --fasta-search
        else:
            cmd.extend([f"--{key}", str(val)])

    # Default to half available cores — instrument PCs need headroom for acquisition
    if threads <= 0:
        import os
        threads = max(2, (os.cpu_count() or 4) // 2)
    cmd.extend(["--threads", str(threads)])

    logger.info("Running DIA-NN locally: %s", raw_path.name)
    logger.debug("Command: %s", " ".join(cmd))

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=14400,  # 4 hour timeout for local execution
        )
        logger.info("DIA-NN complete: %s", raw_path.name)
    except FileNotFoundError as e:
        logger.error(
            "DIA-NN executable not found: %s. "
            "Install DIA-NN or add it to PATH.",
            diann_exe,
        )
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "diann", "vendor": vendor})
        return None
    except subprocess.TimeoutExpired as e:
        logger.error("DIA-NN timed out after 4 hours: %s", raw_path.name)
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "diann", "vendor": vendor})
        return None
    except subprocess.CalledProcessError as e:
        logger.error("DIA-NN failed: %s\nstderr:\n%s", raw_path.name, e.stderr)
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "diann", "vendor": vendor})
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
    fasta_path: str | None = None,
    search_mode: str = "local",
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
        fasta_path: Path to FASTA file (required for local mode).
        search_mode: "local" (user FASTA) or "community" (frozen HF assets).

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
    if search_mode == "community":
        from stan.search.community_params import get_community_sage_params
        params = get_community_sage_params(cache_dir=str(output_dir.parent / "_community_assets"))
    else:
        if not fasta_path:
            logger.error(
                "No fasta_path configured for instrument. "
                "Set fasta_path in instruments.yml or run `stan setup`."
            )
            return None
        if not Path(fasta_path).exists():
            logger.error("FASTA file not found: %s", fasta_path)
            return None
        params = _build_local_sage_params(fasta_path)

    params["mzml_paths"] = [input_path]
    params["output_directory"] = str(output_dir)

    config_path = output_dir / "sage_config.json"
    config_path.write_text(json.dumps(params, indent=2))

    cmd = [sage_exe, str(config_path)]

    logger.info("Running Sage locally: %s", raw_path.name)
    logger.debug("Command: %s", " ".join(cmd))

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=14400,
        )
        logger.info("Sage complete: %s", raw_path.name)
    except FileNotFoundError as e:
        logger.error(
            "Sage executable not found: %s. Install Sage or add it to PATH.",
            sage_exe,
        )
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "sage", "vendor": vendor})
        return None
    except subprocess.TimeoutExpired as e:
        logger.error("Sage timed out after 4 hours: %s", raw_path.name)
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "sage", "vendor": vendor})
        return None
    except subprocess.CalledProcessError as e:
        logger.error("Sage failed: %s\nstderr:\n%s", raw_path.name, e.stderr)
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "sage", "vendor": vendor})
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

    # Sage may write output with a different prefix or at a parent level
    # Search for any .sage.parquet file in the output directory
    sage_files = list(output_dir.glob("*.sage.parquet"))
    if sage_files:
        logger.info("Found Sage output at: %s", sage_files[0])
        return sage_files[0]

    # Also check if Sage wrote to current working directory instead
    cwd_results = Path("results.sage.parquet")
    if cwd_results.exists():
        dest = output_dir / "results.sage.parquet"
        cwd_results.rename(dest)
        logger.info("Moved Sage output from cwd to: %s", dest)
        return dest

    logger.error("Sage output not found in: %s", output_dir)
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
