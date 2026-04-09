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


def build_instrument_library(
    reports_dir: Path | None = None,
    output_path: Path | None = None,
    fasta_path: str | None = None,
    diann_exe: str | None = None,
) -> Path | None:
    """Build an instrument-specific empirical library from baseline reports.

    DIA-NN can combine multiple report.parquet files into a refined
    empirical library using --lib on the combined reports.

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

    # Find all report.parquet files
    reports = find_report_parquets(reports_dir)
    if not reports:
        logger.error("No report.parquet files found in %s", reports_dir)
        return None

    # Merge all reports into one combined report
    logger.info("Found %d report.parquet files", len(reports))

    try:
        import polars as pl

        dfs = []
        for rp in reports:
            try:
                df = pl.read_parquet(rp)
                dfs.append(df)
            except Exception:
                logger.debug("Skipping unreadable report: %s", rp)

        if not dfs:
            logger.error("No valid report.parquet files found")
            return None

        combined = pl.concat(dfs, how="diagonal_relaxed")

        # Write combined report
        combined_path = get_user_config_dir() / "baseline_output" / "_combined_report.parquet"
        combined_path.parent.mkdir(parents=True, exist_ok=True)
        combined.write_parquet(combined_path)

        logger.info(
            "Combined %d reports: %d total precursors",
            len(dfs), combined.height,
        )

        # Run DIA-NN to generate empirical library from combined report
        # --lib on an existing report extracts an empirical library
        import os
        threads = max(2, (os.cpu_count() or 4) // 2)

        cmd = [
            diann_exe,
            "--lib", str(combined_path),
            "--fasta", fasta_path,
            "--out-lib", str(output_path),
            "--threads", str(threads),
            "--qvalue", "0.01",
        ]

        logger.info("Building instrument library: %s", " ".join(cmd))

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
