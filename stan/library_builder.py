"""Build instrument-specific empirical library from baseline results.

Takes all report.parquet files from baseline and combines them into a
single refined spectral library containing only precursors observed on
this specific instrument, with instrument-calibrated RTs.

This library is much smaller than the community library (typically
30-50K precursors vs 170K) and produces faster searches.

Usage:
    stan build-library
    stan build-library --reports-dir C:\\Users\\Exploris480\\STAN\\baseline_output
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm

from stan.config import get_user_config_dir

logger = logging.getLogger(__name__)
console = Console()


def find_diann_exe() -> str | None:
    """Find DIA-NN executable."""
    # Check PATH
    diann = shutil.which("diann") or shutil.which("diann.exe") or shutil.which("DiaNN.exe")
    if diann:
        return diann

    # Check common Windows locations
    import platform
    if platform.system() == "Windows":
        for p in [
            Path("C:/DIA-NN/2.0/diann.exe"),
            Path("C:/DIA-NN/1.8.1/DiaNN.exe"),
            Path("C:/Program Files/DIA-NN/DiaNN.exe"),
        ]:
            if p.exists():
                return str(p)

    return None


def find_report_parquets(reports_dir: Path | None = None) -> list[Path]:
    """Find all report.parquet files from baseline output."""
    if reports_dir is None:
        reports_dir = get_user_config_dir() / "baseline_output"

    if not reports_dir.exists():
        return []

    reports = sorted(reports_dir.rglob("report.parquet"))
    return reports


def _get_raw_paths_from_reports(reports: list[Path]) -> list[Path]:
    """Extract the original raw file paths from report.parquet files."""
    import polars as pl
    raw_paths: list[Path] = []
    seen: set[str] = set()
    for rp in reports:
        try:
            # The "Run" column in DIA-NN 2.x has the raw file basename
            # But we need the full path — use the directory structure
            # report is at: baseline_output/{run_stem}/report.parquet
            run_stem = rp.parent.name
            # Look for the raw file in the report's own metadata
            df = pl.read_parquet(rp, columns=["Run"], n_rows=1)
            if df.height > 0:
                run_val = df["Run"][0]
                if run_val and run_val not in seen:
                    seen.add(run_val)
                    # Run is the stem — we need to find the actual file
                    # We'll use the existing community library approach instead:
                    # find the raw files directly from the baseline directory
                    raw_paths.append(Path(run_val))
        except Exception:
            logger.debug("Could not extract run from %s", rp, exc_info=True)
    return raw_paths


def build_instrument_library(
    reports_dir: Path | None = None,
    output_path: Path | None = None,
    fasta_path: str | None = None,
    diann_exe: str | None = None,
) -> Path | None:
    """Build an instrument-specific empirical library from baseline reports.

    Uses DIA-NN's --out-lib flag with --use-quant to reuse existing
    .quant files. This requires that the original raw files still exist
    at the paths recorded in the database.

    Args:
        reports_dir: Directory containing baseline_output subdirectories.
        output_path: Where to write the instrument library.
        fasta_path: Path to FASTA file.
        diann_exe: Path to DIA-NN executable.

    Returns:
        Path to the generated library, or None on failure.
    """
    if reports_dir is None:
        reports_dir = get_user_config_dir() / "baseline_output"

    if output_path is None:
        output_path = get_user_config_dir() / "instrument_library.parquet"

    # Find DIA-NN
    if diann_exe is None:
        diann_exe = find_diann_exe()
    if not diann_exe:
        logger.error("DIA-NN not found. Cannot build instrument library.")
        return None

    # Find FASTA
    if fasta_path is None:
        fasta_candidate = get_user_config_dir() / "community_assets" / "human_hela_202604.fasta"
        if fasta_candidate.exists():
            fasta_path = str(fasta_candidate)

    if not fasta_path or not Path(fasta_path).exists():
        logger.error("FASTA not found. Run stan baseline first to download it.")
        return None

    # Get raw file paths from the runs table (has full paths)
    from stan.db import get_runs, get_db_path
    all_runs = get_runs(limit=10000, db_path=get_db_path())
    raw_paths: list[str] = []
    for run in all_runs:
        raw_path = run.get("raw_path")
        mode = (run.get("mode") or "").lower()
        # Only DIA files — library building doesn't make sense for DDA
        if raw_path and Path(raw_path).exists() and "dia" in mode:
            raw_paths.append(raw_path)

    if len(raw_paths) < 3:
        logger.error(
            "Need at least 3 DIA raw files with valid paths to build library. Found %d",
            len(raw_paths),
        )
        return None

    logger.info("Building library from %d raw files", len(raw_paths))

    # Find the community library used for the original search
    community_lib = None
    for vendor_lib in ["hela_timstof_202604.parquet", "hela_orbitrap_202604.parquet"]:
        lib_candidate = get_user_config_dir() / "community_assets" / vendor_lib
        if lib_candidate.exists():
            community_lib = str(lib_candidate)
            break

    if not community_lib:
        logger.error("No community library found in community_assets/")
        return None

    try:
        import os
        threads = max(2, (os.cpu_count() or 4) // 2)

        # Build DIA-NN command with --out-lib to generate empirical library
        # --use-quant reuses existing .quant files from the original search
        cmd = [diann_exe]
        for rp in raw_paths:
            cmd.extend(["--f", rp])
        cmd.extend([
            "--lib", community_lib,
            "--fasta", fasta_path,
            "--out-lib", str(output_path),
            "--use-quant",
            "--threads", str(threads),
            "--qvalue", "0.01",
            "--min-pep-len", "7",
            "--max-pep-len", "30",
            "--missed-cleavages", "1",
            "--min-pr-charge", "2",
            "--max-pr-charge", "4",
            "--cut", "K*,R*",
        ])

        logger.info("Building instrument library with %d files", len(raw_paths))

        log_file = output_path.parent / "build_library.log"
        with open(log_file, "w") as lf:
            result = subprocess.run(
                cmd,
                check=True,
                stdout=lf,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=3600,  # 1 hour timeout
            )

        if output_path.exists():
            size_mb = output_path.stat().st_size / (1024 * 1024)
            logger.info(
                "Instrument library built: %s (%.1f MB)",
                output_path, size_mb,
            )
            return output_path
        else:
            logger.error("DIA-NN did not produce output library")
            return None

    except subprocess.CalledProcessError as e:
        logger.error("DIA-NN library build failed: %s", e)
        return None
    except Exception:
        logger.exception("Failed to build instrument library")
        return None


def run_build_library() -> None:
    """Interactive CLI for building instrument-specific library."""
    console.print()
    console.print("[bold]STAN Instrument Library Builder[/bold]")
    console.print()

    reports_dir = get_user_config_dir() / "baseline_output"
    reports = find_report_parquets(reports_dir)

    if not reports:
        console.print("[red]No baseline reports found.[/red]")
        console.print("Run [cyan]stan baseline[/cyan] first to generate search results.")
        return

    console.print(f"  Found [bold]{len(reports)}[/bold] baseline reports in {reports_dir}")

    output_path = get_user_config_dir() / "instrument_library.parquet"
    console.print(f"  Output: {output_path}")

    if output_path.exists():
        if not Confirm.ask("  Overwrite existing instrument library?", default=True, console=console):
            return

    console.print()
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        task = progress.add_task("Building instrument library...", total=None)

        result = build_instrument_library(reports_dir=reports_dir, output_path=output_path)

        if result:
            progress.update(task, description="[green]Library built!")
        else:
            progress.update(task, description="[red]Library build failed")

    if result:
        # Count precursors in the new library
        try:
            import polars as pl
            lib_df = pl.read_parquet(result)
            n_precursors = lib_df.height
            console.print(
                f"\n  [green]Instrument library:[/green] {n_precursors:,} precursors"
            )
            console.print(f"  [dim]Saved to {result}[/dim]")
            console.print(
                "\n  Future QC runs will use this library automatically."
            )
        except Exception:
            console.print(f"\n  [green]Library saved to {result}[/green]")
