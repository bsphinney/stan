"""Search dispatch — routes acquisitions to the correct search engine.

DIA (diaPASEF, DIA Orbitrap) → DIA-NN (Track B)
DDA (ddaPASEF, DDA Orbitrap) → Sage (Track A)

Supports two execution modes:
  - "local": run DIA-NN and Sage directly on this machine (default)
  - "slurm": submit jobs to Hive HPC via SSH/SLURM
"""

from __future__ import annotations

import logging
from pathlib import Path

from stan.config import load_instruments
from stan.watcher.detector import AcquisitionMode, is_dda, is_dia

logger = logging.getLogger(__name__)


def dispatch_search(
    raw_path: Path,
    mode: AcquisitionMode,
    instrument_config: dict,
) -> Path | None:
    """Route a completed acquisition to the appropriate search engine.

    Execution mode is determined by the 'execution_mode' field in
    instruments.yml (default: "local"). Set to "slurm" for HPC submission.

    Args:
        raw_path: Path to .d directory or .raw file.
        mode: Detected acquisition mode.
        instrument_config: Instrument config dict from instruments.yml.

    Returns:
        Path to search output (report.parquet or results.sage.parquet),
        or None if the search failed.
    """
    output_dir = Path(instrument_config.get("output_dir", "")) / raw_path.stem
    exec_mode = instrument_config.get("execution_mode", "local")

    if exec_mode == "slurm":
        return _dispatch_slurm(raw_path, output_dir, mode, instrument_config)
    return _dispatch_local(raw_path, output_dir, mode, instrument_config)


def _dispatch_local(
    raw_path: Path,
    output_dir: Path,
    mode: AcquisitionMode,
    instrument_config: dict,
) -> Path | None:
    """Run search locally using subprocess."""
    from stan.search.local import run_diann_local, run_sage_local

    vendor = instrument_config.get("vendor", "")
    search_mode = instrument_config.get("search_mode", "local")
    fasta_path = instrument_config.get("fasta_path")
    lib_path = instrument_config.get("lib_path")

    if is_dia(mode):
        logger.info("Local DIA-NN search for %s", raw_path.name)
        return run_diann_local(
            raw_path=raw_path,
            output_dir=output_dir,
            vendor=vendor,
            diann_exe=instrument_config.get("diann_path", "diann"),
            fasta_path=fasta_path,
            lib_path=lib_path,
            search_mode=search_mode,
        )

    if is_dda(mode):
        logger.info("Local Sage search for %s", raw_path.name)
        return run_sage_local(
            raw_path=raw_path,
            output_dir=output_dir,
            vendor=vendor,
            sage_exe=instrument_config.get("sage_path", "sage"),
            trfp_exe=instrument_config.get("trfp_path"),
            keep_mzml=instrument_config.get("keep_mzml", False),
            fasta_path=fasta_path,
            search_mode=search_mode,
        )

    logger.error("Cannot dispatch search for mode: %s", mode)
    return None


def _dispatch_slurm(
    raw_path: Path,
    output_dir: Path,
    mode: AcquisitionMode,
    instrument_config: dict,
) -> Path | None:
    """Submit search to Hive HPC via SLURM."""
    from stan.search.diann import get_diann_output, submit_diann_job
    from stan.search.sage import get_sage_output, submit_sage_job
    from stan.search.slurm import SlurmClient, SlurmError

    hive_config, _ = load_instruments()

    if is_dia(mode):
        logger.info("SLURM DIA-NN job for %s", raw_path.name)
        try:
            job_id = submit_diann_job(raw_path, output_dir, instrument_config, hive_config)
            with SlurmClient(
                host=hive_config.get("host", ""),
                user=hive_config.get("user", ""),
                key_path=hive_config.get("key_path"),
            ) as slurm:
                slurm.poll_completion(job_id)
        except (SlurmError, Exception):
            logger.exception("DIA-NN SLURM job failed for %s", raw_path.name)
            return None
        return get_diann_output(output_dir)

    if is_dda(mode):
        logger.info("SLURM Sage job for %s", raw_path.name)
        try:
            job_id = submit_sage_job(raw_path, output_dir, instrument_config, hive_config)
            with SlurmClient(
                host=hive_config.get("host", ""),
                user=hive_config.get("user", ""),
                key_path=hive_config.get("key_path"),
            ) as slurm:
                slurm.poll_completion(job_id)
        except (SlurmError, Exception):
            logger.exception("Sage SLURM job failed for %s", raw_path.name)
            return None
        return get_sage_output(output_dir)

    logger.error("Cannot dispatch search for mode: %s", mode)
    return None
