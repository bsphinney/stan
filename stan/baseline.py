"""Build baseline QC data from existing HeLa standard directories.

Usage:
    stan baseline

Walks the user through selecting a directory of existing HeLa QC runs,
asks for the run specifics (instrument, amount, SPD, column), then
processes all raw files and stores metrics in the local database.

Features:
  - Recursive discovery of .d and .raw files
  - Metadata extraction (acquisition date, instrument model, mode detection)
  - Summary table with instrument breakdown before committing
  - Scheduling: run now, tonight (8 PM), or weekend (Saturday 8 AM)
  - Resume support via ~/.stan/baseline_progress.json
  - IPS computation for every run
  - Duplicate detection (skip files already in the database)
  - Community benchmark batch upload (if enabled)
  - Detailed per-file progress output
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
from rich.table import Table

from stan.config import get_user_config_dir, load_community
from stan.watcher.detector import AcquisitionMode, detect_bruker_mode, is_dda, is_dia

logger = logging.getLogger(__name__)
console = Console()

PROGRESS_FILE = "baseline_progress.json"


# ── File discovery ──────────────────────────────────────────────────

def _find_raw_files(directory: Path, qc_pattern=None) -> list[Path]:
    """Recursively find all .d directories and .raw files.

    Args:
        directory: Root directory to scan.
        qc_pattern: Compiled regex to filter QC files only. If None, returns all files.
    """
    from stan.watcher.qc_filter import is_qc_file

    files: list[Path] = []

    # .d directories (Bruker) — don't recurse into them
    for item in sorted(directory.rglob("*.d")):
        if item.is_dir() and (item / "analysis.tdf").exists():
            if qc_pattern is None or is_qc_file(item, qc_pattern):
                files.append(item)

    # .raw files (Thermo)
    for item in sorted(directory.rglob("*.raw")):
        if item.is_file() and item.stat().st_size > 100_000:
            if qc_pattern is None or is_qc_file(item, qc_pattern):
                files.append(item)

    return files


def _classify_vendor(path: Path) -> str:
    """Classify a raw file as bruker or thermo."""
    if path.suffix.lower() == ".d" and path.is_dir():
        return "bruker"
    return "thermo"


# ── Metadata extraction ────────────────────────────────────────────

def _extract_file_metadata(raw_path: Path, vendor: str) -> dict:
    """Extract metadata from a raw file without running a search.

    Returns dict with: acquisition_date, instrument_model, acquisition_mode,
    gradient_length_min, and vendor-specific fields.
    """
    meta: dict = {
        "vendor": vendor,
        "acquisition_date": None,
        "instrument_model": None,
        "acquisition_mode": None,
        "gradient_length_min": None,
    }

    if vendor == "bruker":
        meta.update(_extract_bruker_metadata(raw_path))
    elif vendor == "thermo":
        meta.update(_extract_thermo_metadata(raw_path))

    return meta


def _extract_bruker_metadata(d_path: Path) -> dict:
    """Extract metadata from a Bruker .d directory."""
    result: dict = {}

    # Acquisition date
    from stan.watcher.acquisition_date import get_acquisition_date
    result["acquisition_date"] = get_acquisition_date(d_path)

    # Instrument model + gradient length from analysis.tdf
    tdf = d_path / "analysis.tdf"
    if tdf.exists():
        try:
            with sqlite3.connect(str(tdf)) as con:
                # Instrument model from GlobalMetadata
                for key in ["InstrumentName", "InstrumentType"]:
                    row = con.execute(
                        "SELECT Value FROM GlobalMetadata WHERE Key = ?", (key,)
                    ).fetchone()
                    if row and row[0]:
                        result["instrument_model"] = row[0]
                        break

                # Gradient length from Frames table
                # Time column is in seconds; gradient = (max - min) / 60
                row = con.execute(
                    "SELECT MIN(Time), MAX(Time) FROM Frames"
                ).fetchone()
                if row and row[0] is not None and row[1] is not None:
                    gradient_sec = row[1] - row[0]
                    if gradient_sec > 0:
                        result["gradient_length_min"] = int(gradient_sec / 60)
        except sqlite3.Error:
            pass

    # Acquisition mode
    mode = detect_bruker_mode(d_path)
    if mode != AcquisitionMode.UNKNOWN:
        result["acquisition_mode"] = mode

    return result


def _extract_thermo_metadata(raw_path: Path) -> dict:
    """Extract metadata from a Thermo .raw file using TRFP."""
    result: dict = {}

    try:
        from stan.tools.trfp import extract_metadata
        trfp_meta = extract_metadata(raw_path)

        result["acquisition_date"] = trfp_meta.get("creation_date")
        result["instrument_model"] = trfp_meta.get("instrument_model")
        result["gradient_length_min"] = trfp_meta.get("gradient_length_min")

        # Mode detection from method name or scan filters
        acq_mode = trfp_meta.get("acquisition_mode")
        if acq_mode == "dia":
            result["acquisition_mode"] = AcquisitionMode.DIA_ORBITRAP
        elif acq_mode == "dda":
            result["acquisition_mode"] = AcquisitionMode.DDA_ORBITRAP
    except Exception:
        logger.debug("TRFP metadata extraction failed for %s", raw_path, exc_info=True)

    # Parent folder name overrides scan ratio — but only if the folder is
    # specifically named "dda" or "dia" (exact or with simple prefix/suffix).
    # Avoid matching folder names that just contain the letters (e.g. "DdaDia").
    import re
    parent_name = raw_path.parent.name
    # Match: "dda", "DDA", "dda_files", "my_dda" but NOT "DdaDia" or "Std_He_ExPeS-DdaDia"
    if re.match(r"^dda$", parent_name, re.IGNORECASE):
        result["acquisition_mode"] = AcquisitionMode.DDA_ORBITRAP
        logger.info("Mode override from folder '%s': DDA for %s", parent_name, raw_path.name)
    elif re.match(r"^dia$", parent_name, re.IGNORECASE):
        if result.get("acquisition_mode") is None:
            result["acquisition_mode"] = AcquisitionMode.DIA_ORBITRAP
            logger.info("Mode from folder '%s': DIA for %s", parent_name, raw_path.name)

    # Fallback: try filename-based heuristics
    if result.get("acquisition_mode") is None:
        name_lower = raw_path.stem.lower()
        if "dia" in name_lower:
            result["acquisition_mode"] = AcquisitionMode.DIA_ORBITRAP
        elif "dda" in name_lower:
            result["acquisition_mode"] = AcquisitionMode.DDA_ORBITRAP

    return result


# ── Date parsing helpers ────────────────────────────────────────────

def _parse_date(date_str: str | None) -> datetime | None:
    """Parse an ISO 8601 date string to a datetime object."""
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def _format_date_range(dates: list[datetime]) -> str:
    """Format a date range as 'Mon YYYY - Mon YYYY'."""
    if not dates:
        return "unknown"
    dates_sorted = sorted(dates)
    start = dates_sorted[0].strftime("%b %Y")
    end = dates_sorted[-1].strftime("%b %Y")
    if start == end:
        return start
    return f"{start} - {end}"


# ── Progress file management ───────────────────────────────────────

def _get_progress_path() -> Path:
    return get_user_config_dir() / PROGRESS_FILE


def _load_progress() -> dict | None:
    """Load baseline progress file if it exists."""
    path = _get_progress_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_progress(progress_data: dict) -> None:
    """Save baseline progress to disk."""
    path = _get_progress_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(progress_data, indent=2, default=str))


def _clear_progress() -> None:
    """Remove the progress file."""
    path = _get_progress_path()
    if path.exists():
        path.unlink()


# ── Duplicate detection ────────────────────────────────────────────

def _get_existing_run_names(instrument: str) -> set[str]:
    """Get all run names already in the database for this instrument."""
    from stan.db import get_db_path
    db_path = get_db_path()
    if not db_path.exists():
        return set()
    try:
        with sqlite3.connect(str(db_path)) as con:
            rows = con.execute(
                "SELECT run_name FROM runs WHERE instrument = ?",
                (instrument,),
            ).fetchall()
        return {r[0] for r in rows}
    except sqlite3.Error:
        return set()


# ── Scheduling ──────────────────────────────────────────────────────

def _wait_for_schedule(schedule: str) -> None:
    """Wait until the scheduled time to start processing."""
    now = datetime.now()

    if schedule == "tonight":
        # Wait until 8 PM today (or 8 PM tomorrow if already past 8 PM)
        from datetime import timedelta
        target = now.replace(hour=20, minute=0, second=0, microsecond=0)
        if now >= target:
            target = target + timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        console.print(
            f"\n[yellow]Scheduled for tonight at 8:00 PM "
            f"(starting in {wait_secs / 3600:.1f} hours)[/yellow]"
        )
        console.print("[dim]Press Ctrl+C to cancel[/dim]")
        time.sleep(wait_secs)

    elif schedule == "weekend":
        # Wait until Saturday 8 AM
        days_until_saturday = (5 - now.weekday()) % 7
        if days_until_saturday == 0 and now.hour >= 8:
            days_until_saturday = 7
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if days_until_saturday > 0:
            from datetime import timedelta
            target += timedelta(days=days_until_saturday)
        wait_secs = (target - now).total_seconds()
        if wait_secs > 0:
            console.print(
                f"\n[yellow]Scheduled for Saturday 8:00 AM "
                f"(starting in {wait_secs / 3600:.1f} hours)[/yellow]"
            )
            console.print("[dim]Press Ctrl+C to cancel[/dim]")
            time.sleep(wait_secs)


# ── Community batch upload ──────────────────────────────────────────

def _batch_submit_community(
    pending_runs: list[dict],
    spd: int | None,
    gradient_length_min: int | None,
    amount_ng: float,
) -> tuple[int, int]:
    """Submit runs to the community benchmark in batches.

    Args:
        pending_runs: List of run dicts from the local DB that need submission.
        spd: Samples per day.
        gradient_length_min: Gradient length in minutes.
        amount_ng: HeLa injection amount.

    Returns:
        (submitted_count, failed_count)
    """
    from stan.community.submit import submit_to_benchmark

    submitted = 0
    failed = 0
    batch_size = 20

    for i in range(0, len(pending_runs), batch_size):
        batch = pending_runs[i : i + batch_size]

        for run in batch:
            try:
                submit_to_benchmark(
                    run=run,
                    spd=spd,
                    gradient_length_min=gradient_length_min,
                    amount_ng=amount_ng,
                )
                submitted += 1
            except Exception as e:
                logger.debug("Community submission failed for %s: %s", run.get("run_name"), e)
                failed += 1

        # Small delay between batches to avoid rate limits
        if i + batch_size < len(pending_runs):
            time.sleep(2)

    return submitted, failed


# ── Estimate search time ───────────────────────────────────────────

def _estimate_search_time(n_files: int, vendor: str) -> str:
    """Estimate total search time. Very rough — depends on hardware."""
    # Rough estimates: DIA-NN ~2-5 min/file, Sage ~1-3 min/file on modern HW
    # Thermo files take longer due to mzML conversion
    minutes_per_file = 4 if vendor == "thermo" else 3
    total_min = n_files * minutes_per_file
    if total_min < 60:
        return f"~{total_min} minutes"
    hours = total_min / 60
    if hours < 24:
        return f"~{hours:.1f} hours"
    days = hours / 24
    return f"~{days:.1f} days"


# ── Main entry point ───────────────────────────────────────────────

def run_baseline() -> None:
    """Interactive baseline builder — process existing HeLa QC directories."""
    # Set up file logging so baseline output is captured for debugging
    log_path = get_user_config_dir() / "baseline.log"
    file_handler = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S"
    ))
    # Flush after every log message so the file is never truncated
    file_handler.flush = file_handler.stream.flush  # type: ignore[assignment]
    logging.getLogger().addHandler(file_handler)
    logging.getLogger().setLevel(logging.DEBUG)
    logger.info("Baseline log: %s", log_path)

    # Also log to Hive mirror if available (enables remote debugging)
    from stan.config import get_hive_mirror_dir
    hive_dir = get_hive_mirror_dir()
    if hive_dir:
        try:
            hive_log_path = hive_dir / "baseline.log"
            hive_handler = logging.FileHandler(str(hive_log_path), mode="w", encoding="utf-8")
            hive_handler.setLevel(logging.DEBUG)
            hive_handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
            ))
            hive_handler.flush = hive_handler.stream.flush  # type: ignore[assignment]
            logging.getLogger().addHandler(hive_handler)
            logger.info("Hive mirror log: %s", hive_log_path)
        except Exception:
            logger.debug("Could not write to Hive mirror", exc_info=True)

    console.print()
    console.print(Panel(
        "[bold]STAN Baseline Builder v3[/bold]\n\n"
        "Process existing HeLa QC runs to build historical baseline data.\n"
        "Point STAN at a directory containing .d or .raw files.",
        title="STAN",
        border_style="blue",
    ))
    console.print()

    # ── Check for resume ────────────────────────────────────────
    existing_progress = _load_progress()
    if existing_progress:
        completed = existing_progress.get("completed", 0)
        total = existing_progress.get("total", 0)
        raw_dir = existing_progress.get("directory", "")
        console.print(
            f"[yellow]Found incomplete baseline: {completed}/{total} files "
            f"processed from {raw_dir}[/yellow]"
        )
        if Confirm.ask(f"Resume from file {completed + 1}/{total}?", default=True, console=console):
            _resume_baseline(existing_progress)
            return
        else:
            _clear_progress()
            console.print("[dim]Starting fresh.[/dim]")

    # ── 1. Directory ─────────────────────────────────────────────
    console.print("[bold]Step 1: Raw data directory[/bold]")

    # Try to suggest the watch_dir from instruments.yml
    default_dir = ""
    try:
        from stan.config import load_instruments
        _, instruments = load_instruments()
        if instruments:
            wd = instruments[0].get("watch_dir", "")
            if wd and Path(wd).exists():
                default_dir = wd
    except Exception:
        pass

    raw_dir = Prompt.ask(
        "Directory containing HeLa QC runs",
        default=default_dir or None,
        console=console,
    )
    raw_path = Path(raw_dir)

    if not raw_path.exists():
        console.print(f"[red]Directory not found: {raw_dir}[/red]")
        return

    # Find raw files recursively
    console.print(f"\n[dim]Scanning {raw_path}...[/dim]")
    all_raw = _find_raw_files(raw_path)

    if not all_raw:
        console.print("[red]No .d directories or .raw files found.[/red]")
        return

    # Filter to QC files only
    from stan.watcher.qc_filter import compile_qc_pattern, filter_qc_files
    console.print(f"  Found [bold]{len(all_raw)}[/bold] total raw files")
    qc_filter_on = Confirm.ask(
        "  Filter to QC/HeLa files only?", default=True, console=console
    )
    if qc_filter_on:
        qc_pat = compile_qc_pattern()
        all_files = filter_qc_files(all_raw, qc_pat)
        if not all_files:
            console.print("[yellow]No QC files matched. Showing all files instead.[/yellow]")
            all_files = all_raw
        else:
            console.print(f"  Matched [bold]{len(all_files)}[/bold] QC files (skipped {len(all_raw) - len(all_files)} non-QC)")
    else:
        all_files = all_raw

    # ── 2. Extract metadata from all files ──────────────────────
    console.print(f"\n  Processing [bold]{len(all_files)}[/bold] raw files")
    console.print("  [dim]Extracting metadata (this may take a moment for Thermo files)...[/dim]")

    file_metadata: list[dict] = []
    _first_dia_file: Path | None = None
    _first_dda_file: Path | None = None
    _scan_vendor: str = ""
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Scanning files...", total=len(all_files))
        for f in all_files:
            vendor = _classify_vendor(f)
            _scan_vendor = vendor
            meta = _extract_file_metadata(f, vendor)
            meta["path"] = f
            meta["name"] = f.name
            file_metadata.append(meta)
            # Track first DIA and DDA files for early test search
            mode = meta.get("acquisition_mode")
            if mode and _first_dia_file is None and is_dia(mode):
                _first_dia_file = f
            if mode and _first_dda_file is None and is_dda(mode):
                _first_dda_file = f
            progress.advance(task)

    # ── 3. Build summary table ──────────────────────────────────
    # Group by instrument model and mode
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for meta in file_metadata:
        model = meta.get("instrument_model") or "Unknown"
        mode_obj = meta.get("acquisition_mode")
        if mode_obj is None or mode_obj == AcquisitionMode.UNKNOWN:
            mode_str = "Unknown"
        elif is_dia(mode_obj):
            mode_str = "DIA"
        elif is_dda(mode_obj):
            mode_str = "DDA"
        else:
            mode_str = str(mode_obj.value) if mode_obj else "Unknown"
        groups[(model, mode_str)].append(meta)

    console.print()
    console.print(
        f"[bold]Found {len(all_files)} HeLa QC files in {raw_path}[/bold]"
    )
    console.print()

    summary_table = Table(show_header=True, header_style="bold", border_style="blue")
    summary_table.add_column("Instrument")
    summary_table.add_column("Dates")
    summary_table.add_column("Files", justify="right")
    summary_table.add_column("Mode")

    for (model, mode_str), entries in sorted(groups.items()):
        dates = [
            _parse_date(e.get("acquisition_date"))
            for e in entries
            if e.get("acquisition_date")
        ]
        dates = [d for d in dates if d is not None]
        date_range = _format_date_range(dates)
        summary_table.add_row(model, date_range, str(len(entries)), mode_str)

    console.print(summary_table)

    # Determine dominant vendor
    vendors = [_classify_vendor(f) for f in all_files]
    vendor = "bruker" if vendors.count("bruker") > vendors.count("thermo") else "thermo"

    est_time = _estimate_search_time(len(all_files), vendor)
    console.print(f"\n  Estimated search time: [bold]{est_time}[/bold]")

    # ── 3b. Download community search assets (FASTA + library) ──
    console.print()
    console.print("[bold]Downloading search assets...[/bold]")

    assets_dir = get_user_config_dir() / "community_assets"
    assets_dir.mkdir(parents=True, exist_ok=True)

    RELEASE_URL = "https://github.com/bsphinney/stan/releases/download/v0.1.0-assets"
    ASSETS = {
        "fasta": "human_hela_202604.fasta",
        "lib_thermo": "hela_orbitrap_202604.parquet",
        "lib_bruker": "hela_timstof_202604.parquet",
    }

    def _get_asset(name: str) -> Path | None:
        """Find or download a community asset."""
        local = assets_dir / name
        if local.exists():
            return local
        # Check bundled (shipped with pip install)
        bundled = Path(__file__).resolve().parent.parent / "community_fasta" / name
        if bundled.exists():
            return bundled
        # Download from GitHub release
        url = f"{RELEASE_URL}/{name}"
        console.print(f"  Downloading {name}...")
        try:
            import urllib.request
            urllib.request.urlretrieve(url, str(local))
            console.print(f"  [green]Downloaded:[/green] {name}")
            return local
        except Exception as e:
            console.print(f"  [yellow]Download failed: {e}[/yellow]")
            return None

    fasta_path = None
    lib_path = None

    fasta_file = _get_asset(ASSETS["fasta"])
    if fasta_file:
        fasta_path = str(fasta_file)
        console.print(f"  [green]FASTA:[/green] {fasta_file.name}")

    # Prefer instrument-specific library (faster) over community library
    instrument_lib = get_user_config_dir() / "instrument_library.parquet"
    if instrument_lib.exists():
        lib_path = str(instrument_lib)
        console.print(f"  [green]Library:[/green] {instrument_lib.name} (instrument-specific)")
    else:
        lib_key = "lib_thermo" if vendor == "thermo" else "lib_bruker"
        lib_file = _get_asset(ASSETS[lib_key])
        if lib_file:
            lib_path = str(lib_file)
            console.print(f"  [green]Library:[/green] {lib_file.name} (community)")
        else:
            console.print(
                f"  [red]Library not found:[/red] {ASSETS[lib_key]}\n"
                f"  DIA searches require the community spectral library.\n"
                f"  DIA files will be skipped. DDA files will still be processed."
            )

    if not fasta_path:
        console.print("  [yellow]No FASTA available — searches will fail.[/yellow]")
        fasta_path = Prompt.ask("  Path to local FASTA file", default="", console=console)

    # ── 4. Standard specifics ───────────────────────────────────
    console.print()
    console.print("[bold]Step 2: Standard specifics[/bold]")
    console.print("These apply to ALL files in this directory.")

    # Suggest instrument name from metadata
    detected_models = [
        m.get("instrument_model") for m in file_metadata if m.get("instrument_model")
    ]
    default_instrument = detected_models[0] if detected_models else (
        "timsTOF Ultra" if vendor == "bruker" else "Astral"
    )

    instrument_name = Prompt.ask(
        "Instrument name", default=default_instrument, console=console
    )
    instrument_model = Prompt.ask(
        "Instrument model", default=instrument_name, console=console
    )
    amount = FloatPrompt.ask("HeLa injection amount (ng)", default=50.0, console=console)

    # SPD — auto-detect from raw file metadata when possible
    from stan.metrics.scoring import gradient_min_to_spd

    detected_gradients = [
        m.get("gradient_length_min") for m in file_metadata
        if m.get("gradient_length_min") and m["gradient_length_min"] > 0
    ]
    detected_lc = next(
        (m.get("lc_system") for m in file_metadata if m.get("lc_system")), None
    )

    if detected_gradients:
        # Show all unique gradient lengths found
        from collections import Counter
        grad_counts = Counter(detected_gradients)
        unique_grads = sorted(grad_counts.keys())
        grad_summary = ", ".join(f"{g} min ({grad_counts[g]} files)" for g in unique_grads)
        lc_label = f"{detected_lc}, " if detected_lc else ""
        console.print(f"  [green]Auto-detected gradients:[/green] {lc_label}{grad_summary}")

        # Use median as the default for global settings
        detected_gradients.sort()
        median_grad = detected_gradients[len(detected_gradients) // 2]
        gradient_length_min = median_grad
        spd = gradient_min_to_spd(gradient_length_min)
        console.print(f"  [dim]Per-file gradients will be used during processing[/dim]")
        console.print(
            f"  [dim]Default (median): {gradient_length_min} min ({spd} SPD)[/dim]"
        )
    else:
        # Fallback to manual selection
        from stan.setup import LC_METHODS
        console.print()
        console.print("[bold]LC method[/bold]")
        for i, lc in enumerate(LC_METHODS, 1):
            if lc["spd"] > 0:
                console.print(f"  [{i}] {lc['name']} (~{lc['gradient_min']} min)")
            else:
                console.print(f"  [{i}] {lc['name']}")
        lc_choice = Prompt.ask(
            "Select method",
            choices=[str(i) for i in range(1, len(LC_METHODS) + 1)],
            console=console,
        )
        lc = LC_METHODS[int(lc_choice) - 1]
        spd = lc["spd"] if lc["spd"] > 0 else None
        gradient_length_min = lc.get("gradient_min")

        if not spd:
            gradient_length_min = IntPrompt.ask("Active gradient length (minutes)", console=console)
            spd = gradient_min_to_spd(gradient_length_min)

    # Column
    console.print()
    console.print("[bold]LC column[/bold]")
    use_column = Confirm.ask("Track LC column for these runs?", default=True, console=console)
    column_info: dict = {}
    if use_column:
        from stan.setup import _pick_column
        col_vendor, col_model = _pick_column()
        column_info = {"vendor": col_vendor, "model": col_model}

    # (FASTA + library already downloaded in step 3b above)

    # ── 6. Search engines — find, validate, test ─────────────────
    diann_exe = _find_diann()
    sage_exe = _find_sage()

    # DIA-NN: find/test using the first DIA file captured during scan
    if _first_dia_file:
        if not diann_exe:
            console.print("  [red]DIA-NN not found[/red] — needed for DIA files.")
            custom = Prompt.ask("  Path to DiaNN.exe (or Enter to skip DIA)", default="", console=console)
            if custom and Path(custom).exists():
                diann_exe = custom
            elif custom:
                console.print(f"  [yellow]Not found: {custom}[/yellow]")

        if diann_exe:
            ok, msg = _test_diann(diann_exe, fasta_path, _first_dia_file, _scan_vendor, console)
            if not ok:
                console.print(f"  [red]DIA-NN test failed:[/red] {msg}")
                custom = Prompt.ask("  Path to a different DiaNN.exe (or Enter to skip DIA)", default="", console=console)
                if custom and Path(custom).exists():
                    diann_exe = custom
                    ok2, msg2 = _test_diann(diann_exe, fasta_path, _first_dia_file, _scan_vendor, console)
                    if not ok2:
                        console.print(f"  [red]Still failing:[/red] {msg2}")
                        diann_exe = None
                else:
                    diann_exe = None
    else:
        console.print("  DIA-NN: [dim]not needed (no DIA files)[/dim]")

    # Sage: find/test using the first DDA file
    if _first_dda_file:
        if not sage_exe:
            console.print("  [red]Sage not found[/red] — needed for DDA files.")
            custom = Prompt.ask("  Path to sage.exe (or Enter to skip DDA)", default="", console=console)
            if custom and Path(custom).exists():
                sage_exe = custom
            elif custom:
                console.print(f"  [yellow]Not found: {custom}[/yellow]")

        if sage_exe:
            ok, msg = _test_sage(sage_exe, fasta_path, _first_dda_file, _scan_vendor, console)
            if not ok:
                console.print(f"  [red]Sage test failed:[/red] {msg}")
                custom = Prompt.ask("  Path to a different sage.exe (or Enter to skip DDA)", default="", console=console)
                if custom and Path(custom).exists():
                    sage_exe = custom
                    ok2, msg2 = _test_sage(sage_exe, fasta_path, _first_dda_file, _scan_vendor, console)
                    if not ok2:
                        console.print(f"  [red]Still failing:[/red] {msg2}")
                        sage_exe = None
                else:
                    sage_exe = None
    else:
        console.print("  Sage: [dim]not needed (no DDA files)[/dim]")

    console.print()
    console.print(f"  DIA-NN: {diann_exe or '[dim]skipped[/dim]'}")
    console.print(f"  Sage:   {sage_exe or '[dim]skipped[/dim]'}")

    # ── 7. Community upload (via relay — no token needed) ──────
    community_submit = False
    try:
        community_cfg = load_community()
    except Exception:
        community_cfg = {}

    if community_cfg.get("auto_submit", False):
        community_submit = True
        console.print("  Community: [green]enabled[/green] (auto_submit is on)")
    else:
        community_submit = Confirm.ask(
            "Submit results to community benchmark?", default=True, console=console
        )

    # ── 8. Confirmation ─────────────────────────────────────────
    console.print()
    config_table = Table(title="Baseline Configuration", show_header=False, border_style="blue")
    config_table.add_column("Field", style="bold")
    config_table.add_column("Value")
    config_table.add_row("Directory", str(raw_path))
    config_table.add_row("Files", f"{len(all_files)} ({vendor})")
    config_table.add_row("Instrument", f"{instrument_name} ({instrument_model})")
    config_table.add_row("Amount", f"{amount} ng")
    config_table.add_row("SPD", str(spd) if spd else "per-file")
    if detected_gradients:
        grad_set = sorted(set(detected_gradients))
        config_table.add_row("Gradients", ", ".join(f"{g} min" for g in grad_set))
    else:
        config_table.add_row("Gradient", f"{gradient_length_min} min" if gradient_length_min else "N/A")
    if column_info:
        config_table.add_row(
            "Column",
            f"{column_info.get('vendor', '')} {column_info.get('model', '')}".strip(),
        )
    config_table.add_row("FASTA", fasta_path or "(not set)")
    config_table.add_row("Community", "[green]yes[/green]" if community_submit else "no")
    config_table.add_row("Est. time", est_time)
    console.print(config_table)

    console.print()
    if not Confirm.ask("Build baseline?", default=True, console=console):
        console.print("[yellow]Cancelled.[/yellow]")
        return

    # ── 9. Scheduling ───────────────────────────────────────────
    schedule = Prompt.ask(
        "Run now or schedule?",
        choices=["now", "tonight", "weekend"],
        default="now",
        console=console,
    )

    if schedule != "now":
        _wait_for_schedule(schedule)

    # ── 10. Process ─────────────────────────────────────────────
    # Sort files by acquisition date
    def _sort_key(meta: dict) -> str:
        return meta.get("acquisition_date") or "9999"

    file_metadata.sort(key=_sort_key)
    sorted_files = [m["path"] for m in file_metadata]

    _process_files(
        files=sorted_files,
        file_metadata_map={str(m["path"]): m for m in file_metadata},
        instrument_name=instrument_name,
        instrument_model=instrument_model,
        amount_ng=amount,
        spd=spd,
        gradient_length_min=gradient_length_min,
        column_info=column_info,
        fasta_path=fasta_path,
        diann_exe=diann_exe,
        sage_exe=sage_exe,
        community_submit=community_submit,
        directory=str(raw_path),
        start_index=0,
        lib_path=lib_path,
    )


def _resume_baseline(progress_data: dict) -> None:
    """Resume a previously interrupted baseline build."""
    directory = Path(progress_data["directory"])
    if not directory.exists():
        console.print(f"[red]Directory no longer exists: {directory}[/red]")
        _clear_progress()
        return

    all_files = _find_raw_files(directory)
    if not all_files:
        console.print("[red]No raw files found in directory.[/red]")
        _clear_progress()
        return

    # Rebuild minimal metadata for sorting
    file_metadata: list[dict] = []
    for f in all_files:
        vendor = _classify_vendor(f)
        meta = {"path": f, "name": f.name, "vendor": vendor}
        file_metadata.append(meta)

    # Sort same way as original
    sorted_files = [m["path"] for m in file_metadata]

    start_index = progress_data.get("completed", 0)

    console.print(f"\n[bold]Resuming baseline from file {start_index + 1}/{len(sorted_files)}[/bold]")

    _process_files(
        files=sorted_files,
        file_metadata_map={str(m["path"]): m for m in file_metadata},
        instrument_name=progress_data.get("instrument_name", "Unknown"),
        instrument_model=progress_data.get("instrument_model", "Unknown"),
        amount_ng=progress_data.get("amount_ng", 50.0),
        spd=progress_data.get("spd"),
        gradient_length_min=progress_data.get("gradient_length_min"),
        column_info=progress_data.get("column_info", {}),
        fasta_path=progress_data.get("fasta_path", ""),
        diann_exe=progress_data.get("diann_exe"),
        sage_exe=progress_data.get("sage_exe"),
        community_submit=progress_data.get("community_submit", False),
        directory=str(directory),
        start_index=start_index,
        lib_path=progress_data.get("lib_path"),
    )


def _process_files(
    files: list[Path],
    file_metadata_map: dict[str, dict],
    instrument_name: str,
    instrument_model: str,
    amount_ng: float,
    spd: int | None,
    gradient_length_min: int | None,
    column_info: dict,
    fasta_path: str,
    diann_exe: str | None,
    sage_exe: str | None,
    community_submit: bool,
    directory: str,
    start_index: int,
    lib_path: str | None = None,
) -> None:
    """Process a list of raw files, extracting metrics and storing results."""
    from stan.db import init_db, insert_run, insert_tic_trace
    from stan.gating.evaluator import evaluate_gates
    from stan.metrics.chromatography import compute_ips_dda, compute_ips_dia
    from stan.metrics.extractor import extract_dda_metrics, extract_dia_metrics
    from stan.search.local import run_diann_local, run_sage_local
    from stan.watcher.detector import AcquisitionMode, detect_mode, is_dia
    from stan.watcher.validate_raw import RawFileValidationError, validate_raw_file

    init_db()

    output_base = get_user_config_dir() / "baseline_output"
    output_base.mkdir(parents=True, exist_ok=True)

    # Check which files are already in the DB
    existing_names = _get_existing_run_names(instrument_name)

    total = len(files)
    processed = 0
    skipped = 0
    failed = 0
    invalid = 0
    results_for_community: list[dict] = []

    # Save initial progress
    progress_state = {
        "total": total,
        "completed": start_index,
        "failed": 0,
        "current": "",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "directory": directory,
        "instrument_name": instrument_name,
        "instrument_model": instrument_model,
        "amount_ng": amount_ng,
        "spd": spd,
        "gradient_length_min": gradient_length_min,
        "column_info": column_info,
        "fasta_path": fasta_path,
        "lib_path": lib_path,
        "diann_exe": diann_exe,
        "sage_exe": sage_exe,
        "community_submit": community_submit,
    }
    _save_progress(progress_state)

    console.print()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        TimeElapsedColumn(),
        console=console,
    ) as progress_bar:
        task = progress_bar.add_task("Processing runs...", total=total, completed=start_index)

        for idx in range(start_index, total):
            raw_file = files[idx]
            vendor = _classify_vendor(raw_file)
            file_meta = file_metadata_map.get(str(raw_file), {})

            progress_bar.update(task, description=f"[{idx + 1}/{total}] {raw_file.name}")

            # Update progress file
            progress_state["current"] = raw_file.name
            progress_state["completed"] = idx
            _save_progress(progress_state)

            # Skip if already in database
            if raw_file.name in existing_names:
                console.print(f"  [dim][{idx + 1}/{total}] {raw_file.name} -- already in DB, skipping[/dim]")
                skipped += 1
                progress_bar.advance(task)
                continue

            # Validate file
            try:
                validate_raw_file(raw_file, vendor=vendor)
            except RawFileValidationError as e:
                console.print(f"  [yellow][{idx + 1}/{total}] {raw_file.name} -- invalid: {e}[/yellow]")
                invalid += 1
                progress_bar.advance(task)
                continue

            try:
                # Detect mode — check metadata, then parent folder name, then default
                mode_obj = file_meta.get("acquisition_mode")
                if mode_obj is None or mode_obj == AcquisitionMode.UNKNOWN:
                    mode_obj = detect_mode(raw_file, vendor=vendor)
                if mode_obj is None or mode_obj == AcquisitionMode.UNKNOWN:
                    # Check if immediate parent folder is exactly "dda" or "dia"
                    import re as _re
                    _parent = raw_file.parent.name
                    if _re.match(r"^dda$", _parent, _re.IGNORECASE):
                        mode_obj = (
                            AcquisitionMode.DDA_PASEF if vendor == "bruker"
                            else AcquisitionMode.DDA_ORBITRAP
                        )
                        logger.info("Mode from folder '%s': DDA", _parent)
                    elif _re.match(r"^dia$", _parent, _re.IGNORECASE):
                        mode_obj = (
                            AcquisitionMode.DIA_PASEF if vendor == "bruker"
                            else AcquisitionMode.DIA_ORBITRAP
                        )
                        logger.info("Mode from folder '%s': DIA", _parent)
                if mode_obj is None or mode_obj == AcquisitionMode.UNKNOWN:
                    # Final default to DIA — most common QC mode
                    mode_obj = (
                        AcquisitionMode.DIA_PASEF if vendor == "bruker"
                        else AcquisitionMode.DIA_ORBITRAP
                    )
                    logger.info("Mode defaulted to DIA for %s", raw_file.name)

                acq_label = "DIA" if is_dia(mode_obj) else "DDA"
                logger.info("File %s -> %s", raw_file.name, acq_label)

                output_dir = output_base / raw_file.stem

                # Run search
                if is_dia(mode_obj):
                    if not diann_exe:
                        console.print(
                            f"  [red][{idx + 1}/{total}] {raw_file.name} "
                            f"-- DIA file but DIA-NN not found[/red]"
                        )
                        failed += 1
                        progress_bar.advance(task)
                        continue

                    result_path = run_diann_local(
                        raw_path=raw_file,
                        output_dir=output_dir,
                        vendor=vendor,
                        diann_exe=diann_exe,
                        fasta_path=fasta_path,
                        lib_path=lib_path,
                    )
                else:
                    if not sage_exe:
                        console.print(
                            f"  [red][{idx + 1}/{total}] {raw_file.name} "
                            f"-- DDA file but Sage not found[/red]"
                        )
                        failed += 1
                        progress_bar.advance(task)
                        continue

                    if vendor == "thermo":
                        console.print(
                            f"  [dim]Converting .raw → mzML for Sage (Sage cannot read "
                            f".raw directly, this adds ~2-5 min)[/dim]"
                        )
                    result_path = run_sage_local(
                        raw_path=raw_file,
                        output_dir=output_dir,
                        vendor=vendor,
                        sage_exe=sage_exe,
                        fasta_path=fasta_path,
                    )

                if result_path is None:
                    console.print(
                        f"  [red][{idx + 1}/{total}] {raw_file.name} "
                        f"-- search failed[/red]"
                    )
                    failed += 1
                    progress_bar.advance(task)
                    continue

                # Extract metrics — per-file gradient takes priority over global
                grad_min = file_meta.get("gradient_length_min") or gradient_length_min
                if is_dia(mode_obj):
                    metrics = extract_dia_metrics(
                        result_path, gradient_min=float(grad_min) if grad_min else None
                    )
                else:
                    metrics = extract_dda_metrics(
                        result_path,
                        gradient_min=grad_min or 60,
                    )

                # Compute IPS
                metrics["instrument_family"] = instrument_model
                metrics["spd"] = spd
                if is_dia(mode_obj):
                    ips = compute_ips_dia(metrics)
                else:
                    ips = compute_ips_dda(metrics)
                metrics["ips_score"] = ips

                # Evaluate gates
                acq_mode = "dia" if is_dia(mode_obj) else "dda"
                decision = evaluate_gates(
                    metrics=metrics,
                    instrument_model=instrument_model,
                    acquisition_mode=acq_mode,
                )

                # Use real acquisition date if available, otherwise fallback
                run_date = file_meta.get("acquisition_date")

                # Store in database
                run_id = insert_run(
                    instrument=instrument_name,
                    run_name=raw_file.name,
                    raw_path=str(raw_file),
                    mode=mode_obj.value,
                    metrics=metrics,
                    gate_result=decision.result.value,
                    failed_gates=decision.failed_gates,
                    diagnosis=decision.diagnosis,
                    amount_ng=amount_ng,
                    spd=spd,
                    gradient_length_min=grad_min,
                )

                # Compute per-file SPD from actual gradient
                file_spd = spd
                if grad_min and grad_min != gradient_length_min:
                    from stan.metrics.scoring import gradient_min_to_spd
                    file_spd = gradient_min_to_spd(grad_min)

                # Update run_date to the actual acquisition date if we have it
                if run_date:
                    _update_run_date(run_id, run_date)

                # Extract and store TIC traces
                tic_trace = None  # best available TIC for community/local
                try:
                    from stan.metrics.tic import (
                        extract_tic_bruker, extract_tic_thermo,
                        extract_tic_from_report, compute_tic_metrics,
                    )
                    # Raw TIC from instrument file (preferred for community — search-independent)
                    raw_tic = None
                    if vendor == "bruker" and raw_file.is_dir():
                        raw_tic = extract_tic_bruker(raw_file)
                    # Identified TIC from DIA-NN report (works for all vendors)
                    id_tic = None
                    if result_path and result_path.exists() and is_dia(mode_obj):
                        id_tic = extract_tic_from_report(result_path)

                    # Store the best available TIC locally
                    tic_trace = raw_tic or id_tic
                    if tic_trace:
                        tic_metrics = compute_tic_metrics(tic_trace)
                        insert_tic_trace(run_id, tic_trace.rt_min, tic_trace.intensity)
                        if tic_metrics.total_auc > 0:
                            _update_tic_metrics(run_id, tic_metrics)
                except Exception:
                    logger.debug("TIC extraction failed for %s", raw_file.name, exc_info=True)

                # Track for community upload (per-file gradient and SPD)
                if community_submit:
                    community_run = {
                        "id": run_id,
                        "run_name": raw_file.name,
                        "instrument": instrument_name,
                        "mode": acq_mode.upper(),
                        "gradient_length_min": grad_min,
                        "spd": file_spd,
                        **metrics,
                    }
                    # Include identified TIC for community if available
                    if tic_trace:
                        community_run["tic_rt_bins"] = [round(r, 3) for r in tic_trace.rt_min]
                        community_run["tic_intensity"] = [round(v, 0) for v in tic_trace.intensity]
                    results_for_community.append(community_run)

                # Print result line
                primary_metric = metrics.get("n_precursors", 0) if is_dia(mode_obj) else metrics.get("n_psms", 0)
                metric_label = "precursors" if is_dia(mode_obj) else "PSMs"
                gate_icon = {
                    "pass": "[green]OK[/green]",
                    "warn": "[yellow]WARN[/yellow]",
                    "fail": "[red]FAIL[/red]",
                }.get(decision.result.value, "")

                grad_label = f"{grad_min}m" if grad_min else ""
                console.print(
                    f"  [{idx + 1}/{total}] {raw_file.name} -- "
                    f"{acq_label} {grad_label} "
                    f"{primary_metric:,} {metric_label} "
                    f"(IPS {ips}) {gate_icon}"
                )

                processed += 1

            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted. Progress saved.[/yellow]")
                progress_state["completed"] = idx
                _save_progress(progress_state)
                console.print("  Resume with: [cyan]stan baseline[/cyan]")
                return
            except Exception as e:
                logger.exception("Failed to process %s", raw_file.name)
                console.print(f"  [red][{idx + 1}/{total}] {raw_file.name} -- error (see log)[/red]")
                from stan.telemetry import report_error
                try:
                    search_eng = "diann" if is_dia(mode_obj) else "sage"
                except NameError:
                    search_eng = "unknown"
                report_error(e, {
                    "search_engine": search_eng,
                    "vendor": vendor,
                    "raw_file_name": raw_file.stem,
                })
                failed += 1

            progress_bar.advance(task)

    # ── Community upload ────────────────────────────────────────
    if community_submit and results_for_community:
        console.print()
        console.print(
            f"[bold]Uploading {len(results_for_community)} runs to community benchmark...[/bold]"
        )
        submitted, sub_failed = _batch_submit_community(
            results_for_community,
            spd=spd,
            gradient_length_min=gradient_length_min,
            amount_ng=amount_ng,
        )
        console.print(f"  [green]Submitted:[/green] {submitted}")
        if sub_failed:
            console.print(f"  [yellow]Failed:[/yellow] {sub_failed}")

    # ── Summary ─────────────────────────────────────────────────
    console.print()
    console.print("[bold]Baseline complete[/bold]")
    console.print(f"  [green]Processed:[/green] {processed}")
    if skipped:
        console.print(f"  [dim]Skipped (already in DB):[/dim] {skipped}")
    if invalid:
        console.print(f"  [yellow]Invalid (skipped):[/yellow] {invalid}")
    if failed:
        console.print(f"  [red]Failed:[/red] {failed}")
    console.print(f"  Database: {get_user_config_dir() / 'stan.db'}")

    # Clear progress file on successful completion
    _clear_progress()

    console.print()
    console.print("View results:")
    console.print("  [cyan]stan status[/cyan]")
    console.print("  [cyan]stan dashboard[/cyan]")


# ── Utility functions ───────────────────────────────────────────────

def _update_run_date(run_id: str, run_date: str) -> None:
    """Update the run_date for a run to use the actual acquisition date."""
    from stan.db import get_db_path
    db_path = get_db_path()
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.execute(
                "UPDATE runs SET run_date = ? WHERE id = ?",
                (run_date, run_id),
            )
    except sqlite3.Error:
        logger.debug("Failed to update run_date for %s", run_id, exc_info=True)


def _update_tic_metrics(run_id: str, tic_metrics) -> None:
    """Update a run with TIC shape metrics (AUC, peak RT, etc.)."""
    from stan.db import get_db_path
    db_path = get_db_path()
    try:
        with sqlite3.connect(str(db_path)) as con:
            con.execute(
                "UPDATE runs SET tic_auc = ?, peak_rt_min = ? WHERE id = ?",
                (tic_metrics.total_auc, tic_metrics.peak_rt_min, run_id),
            )
    except sqlite3.Error:
        logger.debug("Failed to update TIC metrics for %s", run_id, exc_info=True)


def _test_diann(
    diann_exe: str,
    fasta_path: str | None,
    test_file: Path,
    vendor: str,
    console,
) -> tuple[bool, str]:
    """Run a quick DIA-NN test to verify it works.

    Uses the provided test file, runs DIA-NN with a short timeout,
    and parses output to verify it started correctly.

    Returns (success, message).
    """
    import subprocess
    from stan.telemetry import report_error

    console.print("  [dim]Testing DIA-NN...[/dim]")

    # First check: does the exe run at all?
    try:
        proc = subprocess.run(
            [diann_exe],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = proc.stdout + proc.stderr
        # DIA-NN prints its version on startup
        if "DIA-NN" not in output and "diann" not in output.lower():
            msg = f"Executable runs but doesn't look like DIA-NN. Output: {output[:200]}"
            report_error(RuntimeError(msg), {"search_engine": "diann", "vendor": vendor})
            return False, msg
    except FileNotFoundError:
        msg = f"Executable not found: {diann_exe}"
        report_error(FileNotFoundError(msg), {"search_engine": "diann", "vendor": vendor})
        return False, msg
    except subprocess.TimeoutExpired:
        # DIA-NN may hang waiting for input — that's OK, it at least started
        pass
    except Exception as e:
        msg = f"Could not run DIA-NN: {e}"
        report_error(e, {"search_engine": "diann", "vendor": vendor})
        return False, msg

    # Quick test: can DIA-NN read this file? (15s timeout — just checks it starts)
    test_path = test_file
    test_output = Path(os.environ.get("TEMP", "/tmp")) / "stan_test_diann"
    test_output.mkdir(parents=True, exist_ok=True)
    report_path = test_output / "report.parquet"

    # Minimal command — just point at the file with no search params
    # DIA-NN will try to read the file and fail fast if it can't
    cmd = [diann_exe, "--f", str(test_path), "--out", str(report_path)]
    if fasta_path:
        cmd.extend(["--fasta", fasta_path])
    cmd.extend(["--threads", "1"])

    console.print(f"  [dim]Quick test: {test_path.name}...[/dim]")

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,  # 15s — just enough to check it starts
        )
        output = proc.stdout + proc.stderr

        if proc.returncode != 0:
            # Parse the output for useful error info
            if "cannot open" in output.lower() or "file not found" in output.lower():
                msg = f"Cannot read input file. Output: {output[:300]}"
            elif ".net" in output.lower() or "runtime" in output.lower():
                msg = f"Missing .NET runtime. Output: {output[:300]}"
            else:
                msg = f"Exit code {proc.returncode}. Output: {output[:500]}"
            report_error(RuntimeError(msg), {"search_engine": "diann", "vendor": vendor})
            return False, msg

        console.print("  [green]DIA-NN test passed[/green]")
        return True, "OK"

    except subprocess.TimeoutExpired:
        # 15s timeout means DIA-NN started successfully and is working
        console.print("  [green]DIA-NN OK[/green] (started successfully)")
        return True, "OK"
    except Exception as e:
        msg = f"Test error: {e}"
        report_error(e, {"search_engine": "diann", "vendor": vendor})
        return False, msg
    finally:
        import shutil as _shutil
        _shutil.rmtree(test_output, ignore_errors=True)


def _test_sage(
    sage_exe: str,
    fasta_path: str | None,
    test_file: Path,
    vendor: str,
    console,
) -> tuple[bool, str]:
    """Run a quick Sage test to verify it works.

    Returns (success, message).
    """
    import subprocess
    from stan.telemetry import report_error

    console.print("  [dim]Testing Sage...[/dim]")

    # First check: does the exe run?
    try:
        proc = subprocess.run(
            [sage_exe, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        output = proc.stdout + proc.stderr
        if "sage" not in output.lower():
            msg = f"Executable runs but doesn't look like Sage. Output: {output[:200]}"
            report_error(RuntimeError(msg), {"search_engine": "sage", "vendor": vendor})
            return False, msg
        console.print(f"  [green]Sage OK:[/green] {output.strip()}")
        return True, "OK"
    except FileNotFoundError:
        msg = f"Executable not found: {sage_exe}"
        report_error(FileNotFoundError(msg), {"search_engine": "sage", "vendor": vendor})
        return False, msg
    except subprocess.TimeoutExpired:
        console.print("  [green]Sage executable responds[/green]")
        return True, "OK"
    except Exception as e:
        msg = f"Could not run Sage: {e}"
        report_error(e, {"search_engine": "sage", "vendor": vendor})
        return False, msg


def _find_diann() -> str | None:
    """Find DIA-NN executable, preferring version 2.0+.

    Searches common install locations first (newest versions tend to be
    in higher-numbered directories like C:\\DIA-NN\\2.0), then falls back
    to PATH. This avoids using an outdated 1.x on PATH when 2.x is installed.
    """
    candidates: list[str] = []

    # Common Windows locations — search for all DiaNN.exe recursively
    search_roots = [
        Path("C:/DIA-NN"),
        Path("C:/Program Files/DIA-NN"),
        Path(os.environ.get("LOCALAPPDATA", "") or "C:/Users") / "DIA-NN",
        Path(os.environ.get("PROGRAMFILES", "C:\\Program Files")) / "DIA-NN",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:\\Program Files (x86)")) / "DIA-NN",
        Path.home() / "DIA-NN",
    ]

    for root in search_roots:
        if root.exists():
            for exe in root.rglob("DiaNN.exe"):
                candidates.append(str(exe))
            for exe in root.rglob("diann.exe"):
                if str(exe) not in candidates:
                    candidates.append(str(exe))

    # Check STAN tools dir
    stan_diann_exe = get_user_config_dir() / "tools" / "diann" / "DiaNN.exe"
    if stan_diann_exe.exists():
        candidates.append(str(stan_diann_exe))
    stan_diann = get_user_config_dir() / "tools" / "diann" / "diann"
    if stan_diann.exists():
        candidates.append(str(stan_diann))

    # Check PATH last (may find old version)
    for name in ["diann", "diann.exe", "DiaNN", "DiaNN.exe"]:
        found = shutil.which(name)
        if found and found not in candidates:
            candidates.append(found)

    # Linux/Mac common
    for p in [Path("/usr/local/bin/diann"), Path.home() / ".local" / "bin" / "diann"]:
        if p.exists() and str(p) not in candidates:
            candidates.append(str(p))

    if not candidates:
        return None

    # Prefer highest version number in path (e.g. 2.0 > 1.8.1)
    import re
    def _version_key(path: str) -> tuple:
        m = re.search(r"(\d+)\.(\d+)\.?(\d*)", path)
        if m:
            return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))
        return (0, 0, 0)

    candidates.sort(key=_version_key, reverse=True)
    return candidates[0]

    return None


def _find_sage() -> str | None:
    """Find Sage executable on PATH or common locations."""
    for name in ["sage", "sage.exe"]:
        found = shutil.which(name)
        if found:
            return found

    # Check STAN tools dir
    stan_sage = get_user_config_dir() / "tools" / "sage" / "sage"
    if stan_sage.exists():
        return str(stan_sage)
    stan_sage_exe = get_user_config_dir() / "tools" / "sage" / "sage.exe"
    if stan_sage_exe.exists():
        return str(stan_sage_exe)

    return None
