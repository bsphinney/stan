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


def _find_bundled_fasta() -> Path | None:
    """Return the path to the bundled community FASTA, or None if missing.

    v0.2.156: every pip install of stan-proteomics ships
    ``community_fasta/human_hela_202604.fasta`` via the
    [tool.setuptools.data-files] entry. The FASTA is identical across
    every STAN install (same reference for every community submission)
    so defaulting fasta_path to the bundled copy is the correct
    behavior when the operator hasn't set one.
    """
    # baseline.py:718 uses this same relative path successfully.
    bundled = Path(__file__).resolve().parent.parent.parent / "community_fasta" / "human_hela_202604.fasta"
    if bundled.exists():
        return bundled
    # Fallback: sys.prefix location (some pip configurations)
    import sys
    alt = Path(sys.prefix) / "community_fasta" / "human_hela_202604.fasta"
    if alt.exists():
        return alt
    return None


def _mirror_log_to_hive(log_file: Path, run_stem: str, engine: str) -> None:
    """Copy a failed search log to the Hive mirror directory.

    Silently does nothing if Y:\\STAN (or configured mirror) isn't mounted.
    Writes to a per-instrument subdirectory to avoid collisions.
    """
    try:
        from stan.config import get_hive_mirror_dir
        hive_dir = get_hive_mirror_dir()
        if not hive_dir or not log_file.exists():
            return
        failures_dir = hive_dir / "failures"
        failures_dir.mkdir(parents=True, exist_ok=True)
        from datetime import datetime
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = failures_dir / f"{timestamp}_{engine}_{run_stem}.log"
        shutil.copy2(str(log_file), str(dest))
        logger.info("Mirrored log to Hive: %s", dest)
    except Exception:
        logger.debug("Could not mirror log to Hive", exc_info=True)

logger = logging.getLogger(__name__)


def _build_local_diann_params(
    fasta_path: str,
    lib_path: str | None = None,
    vendor: str = "bruker",
) -> dict:
    """Build DIA-NN parameters for local mode with user-provided FASTA.

    Requires a spectral library — library-free mode is too slow for QC
    and produces non-comparable results for the community benchmark.
    """
    if not lib_path:
        raise ValueError(
            "DIA-NN requires a spectral library for QC searches. "
            "Library-free mode is too slow and produces non-comparable results. "
            "Run `stan baseline` again to download the community library."
        )

    # Fixed mass accuracy skips auto-optimization (saves 2-5 min per file).
    # Vendor-specific values from DE-LIMP confirmed settings.
    if vendor == "thermo":
        ms2_acc = 20   # Orbitrap MS2
        ms1_acc = 10   # Orbitrap MS1
    else:
        ms2_acc = 15   # timsTOF MS2
        ms1_acc = 15   # timsTOF MS1

    params: dict = {
        "fasta": fasta_path,
        "lib": lib_path,
        "qvalue": 0.01,
        "min-pep-len": 7,
        "max-pep-len": 30,
        "missed-cleavages": 1,
        "min-pr-charge": 2,
        "max-pr-charge": 4,
        "cut": "K*,R*",
        "mass-acc": ms2_acc,
        "mass-acc-ms1": ms1_acc,
    }

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


def _sanitize_path_for_diann(raw_path: Path, staging_dir: Path) -> Path:
    """Return a DIA-NN-safe path, creating a junction/symlink if needed.

    DIA-NN mis-parses filenames containing double-dashes on Windows —
    it splits the name at each ``--`` and treats every fragment as a
    separate CLI flag. A file named
    ``hela__100spd--toGgBps--C43-tf9d0c24.d`` produces:

        WARNING: unrecognised option [--toGgBps]
        WARNING: unrecognised option [--C43-tf9d0c24.d]
        WARNING: skipping ...hela__100spd - invalid raw MS data format
        0 files will be processed

    Passing the name as a single quoted argv entry does not help because
    DIA-NN does its own argv rescan after Windows tokenization.

    Workaround: for any filename containing ``--`` or other problematic
    characters, create a directory junction (Bruker ``.d``) or hardlink
    (Thermo ``.raw``) with a hash-derived safe name in the per-run
    staging directory, and return that junction path. The original raw
    file is never modified. Returns ``raw_path`` unchanged when
    sanitization isn't needed.
    """
    name = raw_path.name
    if "--" not in name:
        return raw_path

    import hashlib
    import sys

    # Hash of the full absolute path so different files with the same
    # basename can't collide in the staging dir.
    digest = hashlib.md5(str(raw_path.resolve()).encode("utf-8")).hexdigest()[:12]
    safe_name = f"stan_{digest}{raw_path.suffix}"
    junction = staging_dir / safe_name

    if junction.exists():
        return junction

    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            if raw_path.is_dir():
                # Directory junction — no admin privs needed on NTFS.
                # Junctions work on the same volume only; if staging and
                # raw are on different drives we fall back to copytree.
                result = subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(junction), str(raw_path)],
                    capture_output=True, text=True, check=False,
                )
                if result.returncode != 0:
                    logger.warning(
                        "mklink /J failed for %s: %s — falling back to copytree",
                        raw_path.name, result.stderr.strip() or result.stdout.strip(),
                    )
                    import shutil
                    shutil.copytree(str(raw_path), str(junction))
            else:
                import os
                try:
                    os.link(str(raw_path), str(junction))
                except OSError:
                    # Hardlink failed (cross-volume?) — fall back to copy.
                    import shutil
                    shutil.copy2(str(raw_path), str(junction))
        else:
            import os
            os.symlink(str(raw_path), str(junction))
    except Exception:
        logger.warning(
            "Could not create sanitized alias for %s; DIA-NN may fail "
            "to parse the filename.", raw_path.name, exc_info=True,
        )
        return raw_path

    logger.info(
        "DIA-NN filename sanitized: %s -> %s "
        "(filename contained '--' which DIA-NN misparses)",
        raw_path.name, safe_name,
    )
    return junction


def run_diann_local(
    raw_path: Path,
    output_dir: Path,
    vendor: str,
    diann_exe: str = "diann",
    threads: int = 0,
    fasta_path: str | None = None,
    lib_path: str | None = None,
    search_mode: str = "local",
    timeout_sec: int = 1200,
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

    # v0.2.160: auto-discover assets when operator hasn't fully
    # configured local mode. Two auto-uses:
    #   1. No fasta_path -> use bundled human_hela_202604.fasta
    #   2. No lib_path -> use ~/STAN/instrument_library.parquet if
    #      stan has already built one (stan library_builder).
    # Falls back to community download mode only when both auto-
    # discoveries fail (e.g. fresh install before first baseline).
    # Brett's timsTOF HT 2026-04-22: had an instrument_library.parquet
    # already built but the watcher wasn't passing lib_path to
    # run_diann_local, so every QC dispatch errored "DIA-NN requires
    # a spectral library" despite the library being right there.
    if search_mode == "local":
        if not fasta_path:
            bundled_fa = _find_bundled_fasta()
            if bundled_fa:
                logger.info(
                    "Auto-using bundled FASTA: %s", bundled_fa,
                )
                fasta_path = str(bundled_fa)
        if not lib_path:
            from stan.config import get_user_config_dir
            candidate_lib = get_user_config_dir() / "instrument_library.parquet"
            if candidate_lib.exists():
                logger.info(
                    "Auto-using built instrument library: %s", candidate_lib,
                )
                lib_path = str(candidate_lib)
        if not fasta_path or not lib_path:
            logger.info(
                "Local assets incomplete - falling back to community "
                "search mode (pulls FASTA + library from HF Dataset)."
            )
            search_mode = "community"

    if search_mode == "community":
        from stan.search.community_params import get_community_diann_params
        params = get_community_diann_params(vendor, cache_dir=str(output_dir.parent / "_community_assets"))
    else:
        if not Path(fasta_path).exists():
            logger.error("FASTA file not found: %s", fasta_path)
            return None
        params = _build_local_diann_params(fasta_path, lib_path, vendor=vendor)

    report_path = output_dir / "report.parquet"

    # Work around DIA-NN's double-dash filename parsing bug by creating
    # a junction/symlink with a safe name when necessary.
    staging_dir = output_dir.parent / "_stan_diann_staging"
    raw_for_diann = _sanitize_path_for_diann(raw_path, staging_dir)

    # Same bug applies to the --out path: a run named e.g.
    # "18ian24_HeL50ng3hrTip--DIA-BPS_..." produces an output_dir with
    # "--" in its name, which DIA-NN's parser fragments. DIA-NN then
    # writes to a truncated location (e.g. baseline_output/18ian24_HeL50ng3hrTip.parquet)
    # instead of output_dir/report.parquet, and STAN can't find the
    # result so it reports the run as failed even though the search
    # succeeded. Fix: if the output dir name contains "--", route
    # DIA-NN at a hash-named staging output dir, then move its
    # contents back to the real output dir once it completes.
    output_dir_for_diann = output_dir
    out_rename_back = False
    if "--" in output_dir.name:
        import hashlib
        digest = hashlib.md5(str(output_dir.resolve()).encode("utf-8")).hexdigest()[:12]
        output_dir_for_diann = output_dir.parent / f"_stan_out_{digest}"
        # Clean up any stale staging dir from a prior failed run
        if output_dir_for_diann.exists():
            import shutil
            shutil.rmtree(str(output_dir_for_diann), ignore_errors=True)
        output_dir_for_diann.mkdir(parents=True, exist_ok=True)
        out_rename_back = True
        logger.info(
            "DIA-NN output path contains '--'; routing to staging dir "
            "%s and renaming back after completion",
            output_dir_for_diann.name,
        )
    report_path_for_diann = output_dir_for_diann / "report.parquet"

    cmd = [diann_exe, "--f", str(raw_for_diann), "--out", str(report_path_for_diann)]

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
    logger.info("Command: %s", " ".join(cmd))

    # Write DIA-NN output to log file so we can diagnose issues.
    # Keep the log in the real output_dir so operators find it in the
    # expected place even when we routed DIA-NN itself at a staging dir.
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / "diann.log"
    try:
        with open(log_file, "w") as lf:
            result = subprocess.run(
                cmd,
                check=True,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
            )
        logger.info("DIA-NN complete: %s", raw_path.name)
        # If we routed DIA-NN's output to a staging dir (because the
        # real output_dir had "--" in its name), move the artifacts back
        # now that DIA-NN is done reading/writing them.
        if out_rename_back:
            import shutil
            try:
                for item in output_dir_for_diann.iterdir():
                    dest = output_dir / item.name
                    if dest.exists():
                        if dest.is_dir():
                            shutil.rmtree(str(dest), ignore_errors=True)
                        else:
                            dest.unlink()
                    shutil.move(str(item), str(dest))
                output_dir_for_diann.rmdir()
            except Exception:
                logger.exception(
                    "Could not rename DIA-NN staging output %s -> %s",
                    output_dir_for_diann, output_dir,
                )
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
        logger.error(
            "DIA-NN timed out after %d min: %s",
            timeout_sec // 60, raw_path.name,
        )
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "diann", "vendor": vendor})
        _mirror_log_to_hive(log_file, raw_path.stem, "diann")
        return None
    except subprocess.CalledProcessError as e:
        logger.error("DIA-NN failed: %s\nstderr:\n%s", raw_path.name, e.stderr)
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "diann", "vendor": vendor})
        _mirror_log_to_hive(log_file, raw_path.stem, "diann")
        return None

    report = output_dir / "report.parquet"
    if report.exists():
        return report

    # DIA-NN exits with rc=0 even when it processed zero files (e.g. when
    # the filename confused its argv parser). Scan the log and surface a
    # clear error message + mirror the log to Hive for remote debugging.
    diagnosis = "output file missing"
    try:
        log_text = log_file.read_text(errors="replace")
        if "0 files will be processed" in log_text:
            diagnosis = "DIA-NN processed 0 files — filename parsing error?"
        elif "invalid raw MS data format" in log_text:
            diagnosis = "DIA-NN rejected the raw file (invalid format or unreadable)"
        elif "unrecognised option" in log_text:
            diagnosis = "DIA-NN rejected one or more CLI options"
        elif "Library does not contain" in log_text or "Spectral library" in log_text and "loaded" not in log_text:
            diagnosis = "Spectral library problem"
    except Exception:
        pass

    logger.error("DIA-NN failed: %s — %s", raw_path.name, diagnosis)
    _mirror_log_to_hive(log_file, raw_path.stem, "diann")
    return None


def run_sage_local(
    raw_path: Path,
    output_dir: Path,
    vendor: str,
    sage_exe: str = "sage",
    trfp_exe: str | list[str] | None = None,
    keep_mzml: bool = False,
    threads: int = 0,
    fasta_path: str | None = None,
    search_mode: str = "local",
    timeout_sec: int = 1200,
    community_cache_dir: str | None = None,
    ms2_analyzer: str = "OT",
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

    # v0.2.156: same bundled-FASTA fallback as the DIA-NN path above.
    if search_mode == "local" and not fasta_path:
        bundled = _find_bundled_fasta()
        if bundled:
            logger.info(
                "No fasta_path configured for Sage — using bundled "
                "community FASTA: %s", bundled,
            )
            fasta_path = str(bundled)
        else:
            logger.warning(
                "No fasta_path configured and bundled FASTA missing — "
                "falling back to community download mode."
            )
            search_mode = "community"

    # Build Sage JSON config
    if search_mode == "community":
        from stan.search.community_params import get_community_sage_params
        cache = community_cache_dir or str(output_dir.parent / "_community_assets")
        params = get_community_sage_params(cache_dir=cache, ms2_analyzer=ms2_analyzer)
    else:
        if not Path(fasta_path).exists():
            logger.error("FASTA file not found: %s", fasta_path)
            return None
        params = _build_local_sage_params(fasta_path)

    params["mzml_paths"] = [input_path]
    params["output_directory"] = str(output_dir)

    config_path = output_dir / "sage_config.json"
    config_path.write_text(json.dumps(params, indent=2))

    # --parquet: write results.sage.parquet instead of results.sage.tsv
    # (STAN's extractor reads parquet, not TSV)
    cmd = [sage_exe, "--parquet", str(config_path)]

    logger.info("Running Sage locally: %s", raw_path.name)
    logger.info("Command: %s", " ".join(cmd))

    log_file = output_dir / "sage.log"
    try:
        with open(log_file, "w") as lf:
            subprocess.run(
                cmd,
                check=True,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
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
        logger.error(
            "Sage timed out after %d min: %s",
            timeout_sec // 60, raw_path.name,
        )
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "sage", "vendor": vendor})
        _mirror_log_to_hive(log_file, raw_path.stem, "sage")
        return None
    except subprocess.CalledProcessError as e:
        logger.error("Sage failed: %s\nstderr:\n%s", raw_path.name, e.stderr)
        from stan.telemetry import report_error
        report_error(e, {"search_engine": "sage", "vendor": vendor})
        _mirror_log_to_hive(log_file, raw_path.stem, "sage")
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
    # Mirror the sage.log + directory listing so we can diagnose from Hive
    _mirror_log_to_hive(log_file, raw_path.stem, "sage")
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
        # Try the auto-installed TRFP first
        try:
            from stan.tools.trfp import ensure_installed, _build_command
            trfp_path = ensure_installed()
            # _build_command returns ["dotnet", "path/to/dll"] or ["path/to/exe"]
            trfp_cmd_parts = _build_command(trfp_path)
            trfp_exe = trfp_cmd_parts  # store as list for subprocess
        except Exception:
            # Fall back to PATH
            trfp_exe = shutil.which("ThermoRawFileParser") or shutil.which("ThermoRawFileParser.exe")

    if trfp_exe is None:
        logger.error(
            "ThermoRawFileParser not found. Required for Thermo DDA (.raw → mzML). "
            "Install it or set trfp_path in instruments.yml."
        )
        return None

    mzml_path = get_mzml_path(raw_path, output_dir)

    logger.info("Converting %s → mzML...", raw_path.name)

    # trfp_exe can be a string ("path/to/exe") or list (["dotnet", "path/to/dll"])
    if isinstance(trfp_exe, list):
        cmd = list(trfp_exe)
    else:
        cmd = [str(trfp_exe)]
    cmd += [
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
