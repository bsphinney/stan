"""Build baseline QC data from existing HeLa standard directories.

Usage:
    stan baseline

Walks the user through selecting a directory of existing HeLa QC runs,
asks for the run specifics (instrument, amount, SPD, column), then
processes all raw files and stores metrics in the local database.
"""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich.prompt import Confirm, FloatPrompt, Prompt
from rich.table import Table

from stan.columns import COLUMN_CATALOG
from stan.config import get_user_config_dir
from stan.watcher.detector import AcquisitionMode, detect_mode, is_dia

logger = logging.getLogger(__name__)
console = Console()


def run_baseline() -> None:
    """Interactive baseline builder — process existing HeLa QC directories."""
    from rich.panel import Panel

    console.print()
    console.print(Panel(
        "[bold]STAN Baseline Builder[/bold]\n\n"
        "Process existing HeLa QC runs to build historical baseline data.\n"
        "Point STAN at a directory containing .d or .raw files.",
        title="STAN",
        border_style="blue",
    ))
    console.print()

    # ── 1. Directory ─────────────────────────────────────────────
    console.print("[bold]Step 1: Raw data directory[/bold]")
    raw_dir = Prompt.ask("Directory containing HeLa QC runs", console=console)
    raw_path = Path(raw_dir)

    if not raw_path.exists():
        console.print(f"[red]Directory not found: {raw_dir}[/red]")
        return

    # Find raw files
    d_files = sorted(raw_path.glob("*.d"))
    raw_files = sorted(raw_path.glob("*.raw"))
    all_files = d_files + raw_files

    if not all_files:
        console.print("[red]No .d directories or .raw files found in that directory.[/red]")
        return

    vendor = "bruker" if d_files else "thermo"
    console.print(f"\n  Found [bold]{len(all_files)}[/bold] raw files ({vendor})")
    for f in all_files[:5]:
        console.print(f"    {f.name}")
    if len(all_files) > 5:
        console.print(f"    ... and {len(all_files) - 5} more")

    # ── 2. Standard specifics ────────────────────────────────────
    console.print()
    console.print("[bold]Step 2: Standard specifics[/bold]")
    console.print("These apply to ALL files in this directory.")

    instrument_name = Prompt.ask("Instrument name", default="timsTOF Ultra" if vendor == "bruker" else "Astral", console=console)
    instrument_model = Prompt.ask("Instrument model", default=instrument_name, console=console)
    amount = FloatPrompt.ask("HeLa injection amount (ng)", default=50.0, console=console)

    # SPD
    from stan.setup import LC_METHODS
    console.print()
    console.print("[bold]LC method[/bold]")
    for i, lc in enumerate(LC_METHODS, 1):
        if lc["spd"] > 0:
            console.print(f"  [{i}] {lc['name']} (~{lc['gradient_min']} min)")
        else:
            console.print(f"  [{i}] {lc['name']}")
    lc_choice = Prompt.ask("Select method", choices=[str(i) for i in range(1, len(LC_METHODS) + 1)], console=console)
    lc = LC_METHODS[int(lc_choice) - 1]
    spd = lc["spd"] if lc["spd"] > 0 else None

    if lc["spd"] == 0:
        from rich.prompt import IntPrompt
        gradient = IntPrompt.ask("Active gradient length (minutes)", console=console)
        from stan.metrics.scoring import gradient_min_to_spd
        spd = gradient_min_to_spd(gradient)

    # Column
    console.print()
    console.print("[bold]LC column[/bold]")
    use_column = Confirm.ask("Track LC column for these runs?", default=True, console=console)
    column_info: dict = {}
    if use_column:
        from stan.setup import _pick_column
        column_info = _pick_column()

    # ── 3. FASTA ─────────────────────────────────────────────────
    console.print()
    fasta_path = Prompt.ask("Path to FASTA file", console=console)
    if fasta_path and not Path(fasta_path).exists():
        console.print(f"  [yellow]Warning: File not found: {fasta_path}[/yellow]")

    # ── 4. Search engine paths ───────────────────────────────────
    import shutil
    diann_exe = shutil.which("diann") or shutil.which("diann.exe") or "diann"
    sage_exe = shutil.which("sage") or shutil.which("sage.exe") or "sage"

    # ── 5. Confirm ───────────────────────────────────────────────
    console.print()
    table = Table(title="Baseline Configuration", show_header=False, border_style="blue")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Directory", raw_dir)
    table.add_row("Files", f"{len(all_files)} {vendor} files")
    table.add_row("Instrument", f"{instrument_name} ({instrument_model})")
    table.add_row("Amount", f"{amount} ng")
    table.add_row("SPD", str(spd) if spd else "N/A")
    if column_info:
        table.add_row("Column", f"{column_info.get('vendor', '')} {column_info.get('model', '')}".strip())
    table.add_row("FASTA", fasta_path or "(not set)")
    console.print(table)

    console.print()
    if not Confirm.ask(f"Process all {len(all_files)} files?", default=True, console=console):
        console.print("[yellow]Cancelled.[/yellow]")
        return

    # ── 6. Process ───────────────────────────────────────────────
    console.print()

    from stan.db import init_db, insert_run
    from stan.gating.evaluator import evaluate_gates
    from stan.metrics.extractor import extract_dda_metrics, extract_dia_metrics
    from stan.search.local import run_diann_local, run_sage_local

    init_db()

    output_base = Path(get_user_config_dir()) / "baseline_output"
    output_base.mkdir(parents=True, exist_ok=True)

    processed = 0
    failed = 0
    invalid = 0

    from stan.watcher.validate_raw import RawFileValidationError, validate_raw_file

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Processing runs...", total=len(all_files))

        for raw_file in all_files:
            progress.update(task, description=f"Processing {raw_file.name}...")
            output_dir = output_base / raw_file.stem

            # Validate file before search
            try:
                validate_raw_file(raw_file, vendor=vendor)
            except RawFileValidationError as e:
                logger.warning("Skipping invalid file: %s", e)
                console.print(f"  [yellow]skip[/yellow] {raw_file.name}: {e}")
                invalid += 1
                progress.advance(task)
                continue

            try:
                # Detect mode
                mode = detect_mode(raw_file, vendor=vendor)
                if mode == AcquisitionMode.UNKNOWN:
                    # Default to DIA for baseline — most common QC mode
                    mode = AcquisitionMode.DIA_PASEF if vendor == "bruker" else AcquisitionMode.DIA

                # Run search
                if is_dia(mode):
                    result_path = run_diann_local(
                        raw_path=raw_file, output_dir=output_dir,
                        vendor=vendor, diann_exe=diann_exe,
                        fasta_path=fasta_path,
                    )
                else:
                    result_path = run_sage_local(
                        raw_path=raw_file, output_dir=output_dir,
                        vendor=vendor, sage_exe=sage_exe,
                        fasta_path=fasta_path,
                    )

                if result_path is None:
                    failed += 1
                    progress.advance(task)
                    continue

                # Extract metrics
                if is_dia(mode):
                    metrics = extract_dia_metrics(str(result_path))
                else:
                    metrics = extract_dda_metrics(str(result_path))

                # Evaluate gates
                acq_mode = "dia" if is_dia(mode) else "dda"
                decision = evaluate_gates(
                    metrics=metrics,
                    instrument_model=instrument_model,
                    acquisition_mode=acq_mode,
                )

                # Store
                insert_run(
                    instrument=instrument_name,
                    run_name=raw_file.name,
                    raw_path=str(raw_file),
                    mode=mode.value,
                    metrics=metrics,
                    gate_result=decision.result.value,
                    failed_gates=decision.failed_gates,
                    diagnosis=decision.diagnosis,
                    amount_ng=amount,
                    spd=spd,
                )

                processed += 1

            except Exception:
                logger.exception("Failed to process %s", raw_file.name)
                failed += 1

            progress.advance(task)

    # ── 7. Summary ───────────────────────────────────────────────
    console.print()
    console.print(f"[bold]Baseline complete:[/bold]")
    console.print(f"  [green]Processed:[/green] {processed}")
    if invalid:
        console.print(f"  [yellow]Invalid (skipped):[/yellow] {invalid}")
    if failed:
        console.print(f"  [red]Failed:[/red] {failed}")
    console.print(f"  Database: {get_user_config_dir() / 'stan.db'}")
    console.print()
    console.print("View results:")
    console.print("  [cyan]stan status[/cyan]")
    console.print("  [cyan]stan dashboard[/cyan]")
