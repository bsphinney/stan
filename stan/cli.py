"""STAN CLI entry point — ``stan init``, ``stan watch``, ``stan dashboard``."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.logging import RichHandler

from stan import __version__
from stan.config import get_default_config_dir, get_user_config_dir

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="stan",
    help="STAN — Standardized proteomic Throughput ANalyzer. Know your instrument.",
    no_args_is_help=True,
)
console = Console()


def _setup_logging(verbose: bool = False) -> None:
    """Configure logging with rich handler."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Enable debug logging"),
) -> None:
    """STAN — Standardized proteomic Throughput ANalyzer."""
    _setup_logging(verbose)


@app.command()
def version() -> None:
    """Show STAN version."""
    console.print(f"STAN v{__version__}")


@app.command()
def doctor() -> None:
    """Environment + dependency diagnostic, synced to Hive mirror.

    Prints STAN version, Python version, venv path, installed versions
    of critical dependencies (numpy, polars, pandas, alphatims,
    fisher_py, huggingface_hub, watchdog), instrument config summary,
    DB stats, and a smoke-import of alphatims.

    Writes the full output to ~/STAN/logs/doctor_<ts>.log so it syncs
    to the Hive mirror. Brett can share a link to the mirror instead
    of typing anything.

    Run when anything mysterious happens - faster than relaying specific
    diagnostic commands one at a time.
    """
    import importlib
    import platform
    import sqlite3
    import sys
    import traceback
    from datetime import datetime

    from stan.config import get_user_config_dir

    lines: list[str] = []

    def emit(msg: str = "") -> None:
        console.print(msg)
        lines.append(msg)

    def pkg_version(name: str) -> str:
        try:
            from importlib.metadata import version as _v
            return _v(name)
        except Exception:
            return "(not installed)"

    emit(f"[bold]STAN doctor[/bold] - {datetime.now().isoformat(timespec='seconds')}")
    emit("=" * 70)
    emit(f"STAN version:     {__version__}")
    emit(f"Python:           {sys.version.split()[0]} ({platform.python_implementation()})")
    emit(f"Platform:         {platform.system()} {platform.release()}")
    emit(f"sys.prefix:       {sys.prefix}")
    emit(f"sys.executable:   {sys.executable}")
    emit(f"Working dir:      {Path.cwd()}")
    emit("")

    emit("[bold]Dependency versions[/bold]")
    emit("-" * 70)
    for pkg in [
        "numpy", "polars", "pyarrow", "pandas", "watchdog",
        "alphatims", "fisher_py", "huggingface_hub",
        "fastapi", "uvicorn", "httpx", "typer", "rich", "pyyaml",
    ]:
        emit(f"  {pkg:<20} {pkg_version(pkg)}")
    emit("")

    emit("[bold]Critical compat checks[/bold]")
    emit("-" * 70)
    numpy_ver = pkg_version("numpy")
    alphatims_ver = pkg_version("alphatims")
    polars_ver = pkg_version("polars")
    if alphatims_ver.startswith("1.0.9"):
        emit("  [red]alphatims 1.0.9 is BROKEN (polars 1.35+ incompat).[/red]")
        emit("  Fix: stan install-peg-deps")
    elif alphatims_ver == "(not installed)":
        emit("  alphatims not installed (PEG/drift disabled)")
    else:
        # alphatims 1.0.8 + numpy 2.0+ also reported broken
        if numpy_ver and numpy_ver[0].isdigit() and int(numpy_ver.split(".")[0]) >= 2:
            emit(f"  [yellow]numpy {numpy_ver} is 2.0+ - strict searchsorted side= check. "
                 f"alphatims {alphatims_ver} may still fail on searchsorted calls.[/yellow]")
            emit("  Fix if PEG/drift errors: pip install 'numpy<2' in the STAN venv")
        else:
            emit(f"  [green]alphatims {alphatims_ver} + numpy {numpy_ver} pair looks OK[/green]")

    # Smoke-import alphatims.bruker (fails with the actual ValueError
    # if that's what's going on).
    try:
        importlib.import_module("alphatims.bruker")
        emit("  [green]alphatims.bruker imports cleanly[/green]")
    except Exception as e:
        emit(f"  [red]alphatims.bruker import FAILED: {type(e).__name__}: {e}[/red]")
    emit("")

    emit("[bold]STAN config[/bold]")
    emit("-" * 70)
    cfg_dir = get_user_config_dir()
    emit(f"  config dir: {cfg_dir}")
    for name in ("instruments.yml", "community.yml", "thresholds.yml",
                 "stan.db", "instrument_library.parquet"):
        p = cfg_dir / name
        status = f"exists ({p.stat().st_size} bytes)" if p.exists() else "MISSING"
        emit(f"  {name:<32} {status}")
    emit("")

    # DB row counts
    emit("[bold]Database summary[/bold]")
    emit("-" * 70)
    db = cfg_dir / "stan.db"
    if db.exists():
        try:
            with sqlite3.connect(str(db)) as con:
                for table in ("runs", "sample_health", "tic_traces",
                              "health_tic_traces", "peg_ion_hits",
                              "drift_window_centroids", "irt_anchor_rts",
                              "maintenance_events"):
                    try:
                        n = con.execute(
                            f"SELECT COUNT(*) FROM {table}"
                        ).fetchone()[0]
                        emit(f"  {table:<28} {n} rows")
                    except sqlite3.OperationalError:
                        emit(f"  {table:<28} (table missing)")
                # Latest run
                try:
                    row = con.execute(
                        "SELECT substr(run_date,1,16), run_name, "
                        "instrument FROM runs ORDER BY run_date DESC LIMIT 1"
                    ).fetchone()
                    if row:
                        emit(f"  latest runs row:  {row[0]} {row[1]} ({row[2]})")
                except Exception:
                    pass
        except Exception as e:
            emit(f"  [red]DB read failed: {e}[/red]")
    else:
        emit("  (stan.db not found)")
    emit("")

    # Active watcher? Check process list.
    emit("[bold]Watcher + dashboard processes[/bold]")
    emit("-" * 70)
    try:
        import subprocess as _sp
        if platform.system() == "Windows":
            r = _sp.run(
                ["wmic", "process", "where", "name='stan.exe'", "get", "CommandLine,ProcessId"],
                capture_output=True, text=True, timeout=10,
            )
            out = (r.stdout or "").strip()
            emit(out if out else "  (no stan.exe processes)")
        else:
            r = _sp.run(["pgrep", "-af", "stan"], capture_output=True, text=True, timeout=10)
            out = (r.stdout or "").strip()
            emit(out if out else "  (no stan processes)")
    except Exception as e:
        emit(f"  process probe failed: {e}")
    emit("")

    emit("[bold]Recent alerts (last 5)[/bold]")
    emit("-" * 70)
    alerts_dir = cfg_dir / "alerts"
    if alerts_dir.exists():
        alerts = sorted(alerts_dir.glob("*.json"))[-5:]
        if alerts:
            for a in alerts:
                emit(f"  {a.name}")
        else:
            emit("  (no alerts)")
    else:
        emit("  (no alerts dir)")
    emit("")

    # Write to logs/ so it syncs to Hive.
    try:
        log_dir = cfg_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"doctor_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        log_path.write_text("\n".join(lines), encoding="utf-8")
        emit(f"[dim]Log: {log_path}[/dim]")
        try:
            from stan.config import sync_to_hive_mirror
            sync_to_hive_mirror(include_reports=False)
            emit("[dim]Synced to Hive mirror.[/dim]")
        except Exception:
            pass
    except Exception as e:
        emit(f"[yellow]Could not write log: {e}[/yellow]")


@app.command()
def verify() -> None:
    """Check community benchmark auth status and refresh if needed.

    Shows your current lab name, auth token status, and whether the
    relay accepts your credentials. If your token is missing or
    invalid, offers to re-verify via email.
    """
    from stan.config import load_community

    try:
        comm = load_community()
    except Exception:
        comm = {}

    display_name = comm.get("display_name", "")
    auth_token = comm.get("auth_token", "")
    community_submit = comm.get("community_submit", False)

    console.print()
    console.print("[bold]Community Benchmark Status[/bold]")
    console.print()
    console.print(f"  Lab name:     [cyan]{display_name or 'Not set'}[/cyan]")
    console.print(f"  Auth token:   {'[green]present[/green]' if auth_token else '[red]missing[/red]'}")
    console.print(f"  Submissions:  {'[green]enabled[/green]' if community_submit else '[yellow]disabled[/yellow]'}")

    if not display_name:
        console.print()
        console.print("[yellow]No lab name configured. Run [cyan]stan setup[/cyan] to register.[/yellow]")
        return

    # Verify the token against the relay
    if auth_token:
        import json
        import urllib.error
        import urllib.request
        from stan.community.submit import RELAY_URL

        console.print()
        console.print("  Verifying with relay...", end=" ")
        try:
            # Use the /api/names endpoint to check if our name is claimed
            req = urllib.request.Request(
                f"{RELAY_URL}/api/names",
                headers={"User-Agent": f"STAN/{__version__}"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                names_data = json.loads(resp.read())
                claimed = names_data.get("claimed_names", {})
                if display_name in claimed:
                    console.print("[green]verified[/green]")
                    console.print(f"  [dim]Your name '{display_name}' is claimed and protected.[/dim]")
                else:
                    console.print("[yellow]name not found on relay[/yellow]")
                    console.print(
                        f"  [dim]'{display_name}' may not have completed email "
                        "verification. Run [cyan]stan setup[/cyan] to re-verify.[/dim]"
                    )
        except urllib.error.URLError:
            console.print("[yellow]relay unreachable[/yellow]")
            console.print("  [dim]Could not connect to the community relay. Check your internet.[/dim]")
        except Exception as e:
            console.print(f"[red]error: {e}[/red]")
    else:
        console.print()
        console.print(
            "[yellow]No auth token. Your submissions will be accepted during "
            "the grace period, but run [cyan]stan setup[/cyan] to get "
            "a verified token for permanent access.[/yellow]"
        )

    # Show recent submission count from local DB
    try:
        from stan.db import get_runs, init_db
        init_db()
        runs = get_runs(limit=100000)
        submitted = [r for r in runs if r.get("submission_id")]
        console.print()
        console.print(f"  Local runs:      {len(runs)}")
        console.print(f"  Submitted:       {len(submitted)}")
        if submitted:
            last = submitted[0]
            console.print(f"  Last submission: {last.get('run_name', '?')} ({last.get('submission_id', '?')[:8]})")
    except Exception:
        pass
    console.print()


@app.command()
def init() -> None:
    """Initialize STAN config directory (~/.stan/).

    Copies default config files from the package. Does not overwrite existing files.
    """
    user_dir = get_user_config_dir()
    user_dir.mkdir(parents=True, exist_ok=True)

    config_dir = get_default_config_dir()
    config_files = ["instruments.yml", "thresholds.yml", "community.yml"]

    for filename in config_files:
        src = config_dir / filename
        dst = user_dir / filename

        if dst.exists():
            console.print(f"  [yellow]exists[/yellow]  {dst}")
        elif src.exists():
            shutil.copy2(src, dst)
            console.print(f"  [green]created[/green] {dst}")
        else:
            console.print(f"  [red]missing[/red] source: {src}")

    console.print()
    console.print(f"Config directory: [bold]{user_dir}[/bold]")
    console.print("Edit instruments.yml to configure your instruments, then run: stan watch")


@app.command()
def setup() -> None:
    """Interactive setup wizard — configure your instrument without editing YAML.

    Walks you through instrument selection, directory configuration,
    LC method, and FASTA path. Writes instruments.yml to ~/.stan/.
    """
    from stan.setup import run_setup

    run_setup()


@app.command("export")
def export_cmd(
    format: str = typer.Option(
        "archive",
        "--format", "-f",
        help="archive | json | parquet | claude",
    ),
    output: Path = typer.Option(None, "--output", "-o", help="Output path"),
    limit: int = typer.Option(None, "--limit", help="Max runs to export (newest first)"),
) -> None:
    """Export QC data for backup, migration, or AI analysis.

    Formats:

      archive  — .tar.gz with DB + config, for moving between STAN installations

      json     — flat JSON with schema docs, for LLMs and external tools

      parquet  — columnar parquet, for Python/R/DuckDB analysis

      claude   — .zip bundle with a ready-made prompt that makes Claude
                 produce a full QC report with figures. Drop the zip into
                 Claude/ChatGPT and get instant analysis.
    """
    from stan.export import export_archive, export_claude, export_json, export_parquet

    if format == "archive":
        path = export_archive(output_path=output)
    elif format == "json":
        path = export_json(output_path=output, limit=limit)
    elif format == "parquet":
        path = export_parquet(output_path=output, limit=limit)
    elif format == "claude":
        path = export_claude(output_path=output, limit=limit)
    else:
        console.print(f"[red]Unknown format: {format}[/red]")
        console.print("Valid: archive, json, parquet, claude")
        raise typer.Exit(1)

    console.print(f"[green]Exported to {path}[/green]")
    if format == "claude":
        console.print()
        console.print("[bold]Next steps:[/bold]")
        console.print(f"  1. Open Claude (or ChatGPT / Gemini) in your browser")
        console.print(f"  2. Drag [cyan]{path}[/cyan] into the chat")
        console.print(f"  3. Say: [italic]\"Please analyze my STAN QC data\"[/italic]")
        console.print(f"  4. Claude will read the prompt and produce a full report with figures")


@app.command("import")
def import_cmd(
    archive: Path = typer.Argument(..., help="Path to stan_export_*.tar.gz"),
    overwrite: bool = typer.Option(False, "--overwrite", help="Overwrite duplicates instead of skipping"),
) -> None:
    """Import QC data from a previously exported archive.

    Merges runs with your existing database. Duplicate runs (matching
    instrument + run_name + run_date) are skipped by default.
    """
    from stan.export import import_archive

    result = import_archive(archive, skip_duplicates=not overwrite)
    console.print(f"[bold]Import complete:[/bold]")
    console.print(f"  [green]Imported:[/green] {result['imported']} runs")
    console.print(f"  [yellow]Skipped (duplicates):[/yellow] {result['skipped']}")
    console.print(f"  Total in archive: {result['total']}")


@app.command()
def baseline(
    redo_stale_diann: bool = typer.Option(
        False,
        "--redo-stale-diann",
        help=(
            "Re-search DIA runs whose recorded diann_version differs from "
            "the currently-installed DIA-NN binary. Use after upgrading "
            "DIA-NN to bring historical runs onto the community "
            "benchmark's pinned version. DDA runs are left alone."
        ),
    ),
) -> None:
    """Build baseline QC data from existing HeLa standard directories.

    Point STAN at a directory of existing .d or .raw files to process them
    retroactively and build historical performance data.
    """
    from stan.baseline import run_baseline

    run_baseline(redo_stale_diann=redo_stale_diann)


@app.command("build-library")
def build_library() -> None:
    """Build instrument-specific spectral library from baseline results.

    Combines all report.parquet files from baseline into a refined library
    with only precursors observed on your instrument. Produces faster
    searches than the community library (30-50K vs 170K precursors).
    """
    from stan.library_builder import run_build_library

    run_build_library()


@app.command("add-watch")
def add_watch(
    path: str = typer.Argument(..., help="Watch directory path"),
    name: str = typer.Option(None, "--name", "-n", help="Instrument name (auto-detected if omitted)"),
    vendor: str = typer.Option(None, "--vendor", "-v", help="bruker or thermo (auto-detected)"),
    no_prompt: bool = typer.Option(
        False, "--no-prompt", "-y",
        help="Skip the QC filter prompt. Defaults to the standard HeLa/QC pattern.",
    ),
    qc_pattern: str = typer.Option(
        None, "--qc-pattern",
        help="Custom regex for QC filename detection. Implies --no-prompt.",
    ),
    qc_off: bool = typer.Option(
        False, "--all-files",
        help="Process every raw file in the directory, not just QC files. "
             "Use for dedicated QC watch dirs where every file is a HeLa run.",
    ),
) -> None:
    """Add a new watch directory to instruments.yml.

    Interactive: when run without --qc-pattern or --all-files, this will
    scan the directory, show how many files match the default QC pattern
    vs. the total, and ask you to confirm the filter settings. Each
    watch directory can have its own pattern, so mixed sample dirs can
    be filtered while dedicated HeLa dirs process everything.

    Example:
        stan add-watch F:\\data\\new_hela_runs
        stan add-watch D:\\Data\\HeLa --name "timsTOF HT" --vendor bruker
        stan add-watch E:\\data\\shared --qc-pattern "(?i)(hela|qctest)"
        stan add-watch G:\\qc_only --all-files
    """
    from pathlib import Path as _Path
    import yaml as _yaml
    from rich.prompt import Confirm, Prompt
    from stan.config import resolve_config_path, get_user_config_dir
    from stan.watcher.qc_filter import (
        DEFAULT_QC_PATTERN,
        compile_qc_pattern,
        is_qc_file,
    )

    watch_path = _Path(path)
    if not watch_path.exists():
        console.print(f"[red]Directory does not exist: {path}[/red]")
        return

    # Auto-detect vendor from contents. The watch dir may have raw files
    # at any depth (per-project subdirs, date folders, etc.), so we scan
    # recursively with a hard cap to avoid hanging on huge trees.
    if vendor is None:
        n_d = 0
        n_raw = 0
        SCAN_LIMIT = 5000  # stop after this many entries
        for i, p in enumerate(watch_path.rglob("*")):
            if i >= SCAN_LIMIT:
                break
            try:
                if p.suffix == ".d" and p.is_dir():
                    n_d += 1
                elif p.suffix == ".raw" and p.is_file():
                    n_raw += 1
            except OSError:
                continue
            # Short-circuit once we're confident
            if (n_d >= 3 and n_raw == 0) or (n_raw >= 3 and n_d == 0):
                break

        if n_d > 0 and n_raw == 0:
            vendor = "bruker"
        elif n_raw > 0 and n_d == 0:
            vendor = "thermo"
        elif n_d > 0 and n_raw > 0:
            # Mixed-vendor directory — pick the majority, warn.
            vendor = "bruker" if n_d >= n_raw else "thermo"
            console.print(
                f"[yellow]Mixed-vendor directory ({n_d} .d, {n_raw} .raw) — "
                f"picking '{vendor}'. Specify --vendor to override.[/yellow]"
            )
        else:
            console.print(
                "[yellow]No .d or .raw files found (scanned recursively up "
                f"to {SCAN_LIMIT} entries). Specify --vendor bruker or "
                "--vendor thermo, or check that the directory path is "
                "correct.[/yellow]"
            )
            return

    # Auto-generate name if not given
    if name is None:
        name = f"{watch_path.name}_{vendor}"

    # ── QC filter prompt ───────────────────────────────────────
    # Each watch dir can have its own pattern — some are shared with
    # non-QC samples, others are dedicated HeLa/QC folders.
    qc_only_cfg = True
    qc_pattern_cfg: str | None = None

    if qc_off:
        qc_only_cfg = False
    elif qc_pattern:
        # Explicit pattern supplied via flag — skip the prompt.
        try:
            compile_qc_pattern(qc_pattern)
        except Exception:
            console.print(f"[red]Invalid regex: {qc_pattern}[/red]")
            return
        qc_only_cfg = True
        qc_pattern_cfg = qc_pattern
    elif not no_prompt:
        # Scan the directory and show a preview so the user can see
        # what the default pattern actually catches before committing.
        ext = ".d" if vendor == "bruker" else ".raw"
        found_files: list[_Path] = []
        if ext == ".d":
            for p in watch_path.rglob("*.d"):
                if p.is_dir():
                    found_files.append(p)
        else:
            for p in watch_path.rglob("*.raw"):
                if p.is_file():
                    found_files.append(p)

        default_pat = compile_qc_pattern()
        matched = [f for f in found_files if is_qc_file(f, default_pat)]
        total = len(found_files)

        console.print()
        console.print(
            f"[bold]Scanning {path}[/bold] — found [cyan]{total}[/cyan] "
            f"{ext} files total."
        )
        if total == 0:
            console.print(
                "[yellow]No raw files yet — that's fine, filtering will "
                "apply to future files too.[/yellow]"
            )
        else:
            console.print(
                f"The default QC pattern [dim]{DEFAULT_QC_PATTERN}[/dim] "
                f"matches [cyan]{len(matched)}[/cyan] / {total} files."
            )
            # Show a few examples of matched vs. unmatched so the user
            # knows what they're picking.
            if matched:
                console.print("[green]Matched (will be processed):[/green]")
                for f in matched[:3]:
                    console.print(f"  ✓ {f.name}")
                if len(matched) > 3:
                    console.print(f"  [dim]... and {len(matched) - 3} more[/dim]")
            unmatched = [f for f in found_files if f not in matched]
            if unmatched:
                console.print("[dim]Skipped (non-QC):[/dim]")
                for f in unmatched[:3]:
                    console.print(f"  [dim]✗ {f.name}[/dim]")
                if len(unmatched) > 3:
                    console.print(f"  [dim]... and {len(unmatched) - 3} more[/dim]")

        console.print()
        console.print("QC filtering options:")
        console.print("  [cyan]1[/cyan]  Use the default HeLa/QC pattern (recommended)")
        console.print("  [cyan]2[/cyan]  Custom regex pattern for this directory")
        console.print("  [cyan]3[/cyan]  Process every file (no filter — for dedicated QC dirs)")
        choice = Prompt.ask(
            "Choice", choices=["1", "2", "3"], default="1", console=console
        )

        if choice == "1":
            qc_only_cfg = True
            qc_pattern_cfg = None  # implicit default
        elif choice == "2":
            while True:
                pat = Prompt.ask(
                    "Enter regex (e.g. (?i)(hela|myqc|std.*he))",
                    default=DEFAULT_QC_PATTERN,
                    console=console,
                )
                try:
                    compiled = compile_qc_pattern(pat)
                    # Preview the match count against found files
                    if found_files:
                        n_match = sum(1 for f in found_files if is_qc_file(f, compiled))
                        console.print(
                            f"[dim]Matches {n_match} / {total} files.[/dim]"
                        )
                    if Confirm.ask(
                        "Accept this pattern?", default=True, console=console
                    ):
                        qc_only_cfg = True
                        qc_pattern_cfg = pat
                        break
                except Exception as e:
                    console.print(f"[red]Invalid regex: {e}[/red]")
        else:  # choice == "3"
            qc_only_cfg = False

    # Load current instruments.yml
    try:
        config_path = resolve_config_path("instruments.yml")
    except FileNotFoundError:
        config_path = get_user_config_dir() / "instruments.yml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text("instruments: []\n")

    with open(config_path) as f:
        data = _yaml.safe_load(f) or {}

    if "instruments" not in data:
        data["instruments"] = []

    # Check if already present
    abs_path = str(watch_path.resolve())
    for inst in data["instruments"]:
        existing = str(_Path(inst.get("watch_dir", "")).resolve()) if inst.get("watch_dir") else ""
        if existing == abs_path:
            console.print(f"[yellow]Already watching: {abs_path}[/yellow]")
            console.print(f"  (as instrument '{inst.get('name', 'unnamed')}')")
            return

    # Add new entry
    extensions = [".d"] if vendor == "bruker" else [".raw"]
    stable_secs = 60 if vendor == "bruker" else 30
    new_inst: dict = {
        "name": name,
        "vendor": vendor,
        "watch_dir": abs_path,
        "extensions": extensions,
        "stable_secs": stable_secs,
        "qc_only": qc_only_cfg,
    }
    if qc_pattern_cfg:
        new_inst["qc_pattern"] = qc_pattern_cfg
    data["instruments"].append(new_inst)

    with open(config_path, "w") as f:
        _yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    console.print()
    console.print(f"[green]Added watch directory:[/green]")
    console.print(f"  Name:   {name}")
    console.print(f"  Vendor: {vendor}")
    console.print(f"  Path:   {abs_path}")
    if qc_only_cfg:
        pat_label = qc_pattern_cfg if qc_pattern_cfg else "default HeLa/QC pattern"
        console.print(f"  Filter: [cyan]{pat_label}[/cyan]")
    else:
        console.print(f"  Filter: [cyan]none (processing all files)[/cyan]")
    console.print()
    console.print(f"[dim]Config written to {config_path}[/dim]")
    console.print(f"[dim]The watcher daemon picks up changes automatically (hot-reload).[/dim]")


@app.command("list-watch")
def list_watch() -> None:
    """List all configured watch directories."""
    from pathlib import Path as _Path
    import yaml as _yaml
    from stan.config import resolve_config_path

    try:
        config_path = resolve_config_path("instruments.yml")
    except FileNotFoundError:
        console.print("[yellow]No instruments configured yet.[/yellow]")
        console.print("  Run [cyan]stan add-watch <path>[/cyan] to add one.")
        return

    with open(config_path) as f:
        data = _yaml.safe_load(f) or {}

    instruments = data.get("instruments", [])
    if not instruments:
        console.print("[yellow]No instruments configured.[/yellow]")
        return

    from rich.table import Table
    table = Table(title="Watch Directories", show_header=True, border_style="blue")
    table.add_column("#", style="dim")
    table.add_column("Name")
    table.add_column("Vendor")
    table.add_column("Path")
    table.add_column("Exists")
    for i, inst in enumerate(instruments, 1):
        path = inst.get("watch_dir", "")
        exists = "✓" if path and _Path(path).exists() else "[red]✗[/red]"
        table.add_row(
            str(i),
            inst.get("name", ""),
            inst.get("vendor", ""),
            path,
            exists,
        )
    console.print(table)


@app.command("remove-watch")
def remove_watch(
    name_or_number: str = typer.Argument(..., help="Instrument name or number from list-watch"),
) -> None:
    """Remove a watch directory from instruments.yml."""
    from pathlib import Path as _Path
    import yaml as _yaml
    from stan.config import resolve_config_path

    try:
        config_path = resolve_config_path("instruments.yml")
    except FileNotFoundError:
        console.print("[yellow]No instruments configured.[/yellow]")
        return

    with open(config_path) as f:
        data = _yaml.safe_load(f) or {}

    instruments = data.get("instruments", [])
    if not instruments:
        console.print("[yellow]No instruments configured.[/yellow]")
        return

    # Resolve by number or name
    target_idx = None
    if name_or_number.isdigit():
        idx = int(name_or_number) - 1
        if 0 <= idx < len(instruments):
            target_idx = idx
    else:
        for i, inst in enumerate(instruments):
            if inst.get("name", "").lower() == name_or_number.lower():
                target_idx = i
                break

    if target_idx is None:
        console.print(f"[red]No instrument matching '{name_or_number}'[/red]")
        return

    removed = instruments.pop(target_idx)
    data["instruments"] = instruments

    with open(config_path, "w") as f:
        _yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)

    console.print(f"[green]Removed:[/green] {removed.get('name', '')} ({removed.get('watch_dir', '')})")


@app.command("test-alert")
def test_alert() -> None:
    """Send a test Slack message to verify alerts are configured.

    Requires slack_webhook_url in ~/STAN/community.yml.
    """
    from stan.alerts import test_slack_alert

    if test_slack_alert("STAN alert test"):
        console.print("[green]Test alert sent.[/green] Check your Slack channel.")
    else:
        console.print("[yellow]No Slack webhook configured.[/yellow]")
        console.print("  Add slack_webhook_url to ~/STAN/community.yml:")
        console.print('  [cyan]slack_webhook_url: "https://hooks.slack.com/services/..."[/cyan]')


@app.command()
def sync() -> None:
    """Sync stan.db and config to Hive mirror (if Y:\\STAN is mapped).

    Copies the local QC database and configuration to the Hive mirror
    directory so remote analysis tools (including Claude) can query
    instrument performance history.
    """
    from stan.config import sync_to_hive_mirror, get_hive_mirror_dir

    hive_dir = get_hive_mirror_dir()
    if not hive_dir:
        console.print("[yellow]No Hive mirror directory available.[/yellow]")
        console.print("  Map Hive to Y:\\STAN or set HIVE_MIRROR_DIR env var.")
        return

    console.print(f"Syncing to: [cyan]{hive_dir}[/cyan]")
    if sync_to_hive_mirror():
        console.print("[green]Sync complete.[/green]")
    else:
        console.print("[red]Sync failed.[/red]")


def _backfill_tic_impl(
    push: bool = False,
    verbose: bool = True,
    force: bool = False,
) -> tuple[int, int, int]:
    """Core backfill logic shared by the CLI command and the baseline
    startup sweep.

    With ``force=True`` re-extracts the TIC for every run regardless of
    whether one is already stored — needed after the v0.2.147
    downsample_trace fix (mean-per-bin instead of sum-per-bin) so
    previously-stored Bruker sawtooth-pattern TICs get corrected.

    Without force, finds runs in the local DB that are missing TIC
    traces OR have zero peptide/protein counts despite having a
    report.parquet in baseline_output. Repairs both in one pass:

      TIC sources (in order):
        1. ``analysis.tdf`` inside the .d directory at ``raw_path``
        2. ``report.parquet`` in ``baseline_output/<run_name>/``
        3. ``extract_tic_thermo`` for Thermo ``.raw`` if fisher_py works

      Peptide/protein repair:
        If ``n_peptides`` is 0 or NULL but a ``report.parquet`` exists,
        recompute from ``Stripped.Sequence`` / ``Protein.Group`` at 1% FDR.
        This fixes the Lumos zero-peptide bug where older STAN versions
        populated precursors but not peptides.

    All traces are downsampled to 128 bins before storage so they match
    the identified-TIC format.

    Returns (extracted, skipped, failed).
    """
    import json
    import sqlite3
    import urllib.error
    import urllib.request

    from stan.config import get_user_config_dir
    from stan.db import get_db_path, get_runs, init_db, insert_tic_trace
    from stan.metrics.tic import (
        compute_tic_metrics,
        downsample_trace,
        extract_tic_bruker,
        extract_tic_from_report,
        extract_tic_thermo,
    )

    init_db()
    db_path = get_db_path()
    output_dir = get_user_config_dir() / "baseline_output"

    # Pull every run and work out which ones are missing a TIC trace.
    all_runs = get_runs(limit=100000, db_path=db_path)
    if not all_runs:
        if verbose:
            console.print("[dim]No runs in local DB — nothing to backfill.[/dim]")
        return (0, 0, 0)

    with sqlite3.connect(str(db_path)) as con:
        have_tic = {
            row[0] for row in con.execute(
                "SELECT DISTINCT run_id FROM tic_traces"
            ).fetchall()
        }

    # Runs that need TIC or have zero peptides (or both).
    # With force=True, re-extract every run's TIC regardless.
    if force:
        missing_tic = list(all_runs)
    else:
        missing_tic = [r for r in all_runs if r["id"] not in have_tic]
    missing_pep = [r for r in all_runs
                   if r["id"] in have_tic  # already has TIC
                   and (not r.get("n_peptides") or r["n_peptides"] == 0)
                   and (r.get("n_precursors") or 0) > 0]  # has search results

    missing = missing_tic + missing_pep
    # Deduplicate by run_id (a run could be in both lists)
    seen_ids = set()
    deduped = []
    for r in missing:
        if r["id"] not in seen_ids:
            seen_ids.add(r["id"])
            deduped.append(r)
    missing = deduped

    if not missing:
        if verbose:
            console.print("[green]Every run already has TIC + peptide counts.[/green]")
        return (0, 0, 0)

    n_need_tic = len([r for r in missing if r["id"] not in have_tic])
    n_need_pep = len([r for r in missing if (not r.get("n_peptides") or r["n_peptides"] == 0) and (r.get("n_precursors") or 0) > 0])
    if verbose:
        parts = []
        if n_need_tic:
            parts.append(f"{n_need_tic} missing TIC")
        if n_need_pep:
            parts.append(f"{n_need_pep} missing peptides")
        console.print(
            f"Repairing [bold]{' + '.join(parts)}[/bold] "
            f"(of {len(all_runs)} total runs)..."
        )

    extracted = 0
    skipped = 0
    failed = 0
    pushed_rows: list[tuple[str, list, list]] = []

    # v0.2.151: track skip reasons so operators can see why a --force
    # sweep left rows un-rewritten. Brett's timsTOF 2026-04-22 showed
    # only 27/277 updated with --force, 250 silently skipped — no way
    # to tell why from the console. This histogram fixes that.
    skip_reasons: dict[str, int] = {
        "raw_missing": 0,
        "bruker_extract_failed": 0,
        "no_report_parquet": 0,
        "report_extract_failed": 0,
        "thermo_extract_failed": 0,
        "no_raw_path_recorded": 0,
    }

    for run in missing:
        run_id = run["id"]
        run_name = run.get("run_name", "")
        raw_path_str = run.get("raw_path", "") or ""
        raw_path = Path(raw_path_str) if raw_path_str else None

        trace = None
        last_fail = None  # most recent reason for this run

        # 1. Try Bruker .d raw TIC
        if raw_path and raw_path.suffix.lower() == ".d":
            if not raw_path.exists():
                last_fail = "raw_missing"
            else:
                try:
                    trace = extract_tic_bruker(raw_path)
                    if trace is None:
                        last_fail = "bruker_extract_failed"
                except Exception:
                    logger.debug("extract_tic_bruker failed for %s", raw_path, exc_info=True)
                    last_fail = "bruker_extract_failed"

        # 2. Try the identified TIC from the DIA-NN report.parquet
        if trace is None and output_dir.exists():
            # The baseline output dir for a file is named after the stem
            report_path = None
            for stem_variant in (Path(run_name).stem, run_name, Path(raw_path_str).stem if raw_path_str else ""):
                if not stem_variant:
                    continue
                candidate = output_dir / stem_variant / "report.parquet"
                if candidate.exists():
                    report_path = candidate
                    break
            if report_path is not None:
                try:
                    trace = extract_tic_from_report(report_path)
                    if trace is None:
                        last_fail = "report_extract_failed"
                except Exception:
                    logger.debug("extract_tic_from_report failed for %s", report_path, exc_info=True)
                    last_fail = "report_extract_failed"
            elif last_fail is None:
                last_fail = "no_report_parquet"

        # 3. Try Thermo .raw via fisher_py
        if trace is None and raw_path and raw_path.suffix.lower() == ".raw":
            if not raw_path.exists():
                last_fail = "raw_missing"
            else:
                try:
                    trace = extract_tic_thermo(raw_path)
                    if trace is None:
                        last_fail = "thermo_extract_failed"
                except Exception:
                    logger.debug("extract_tic_thermo failed for %s", raw_path, exc_info=True)
                    last_fail = "thermo_extract_failed"

        if trace is None and not raw_path_str:
            last_fail = "no_raw_path_recorded"

        if trace is None:
            failed += 1
            skip_reasons[last_fail or "unknown"] = skip_reasons.get(last_fail or "unknown", 0) + 1
            if verbose:
                console.print(f"  [red]skip:{last_fail}[/red] {run_name}")
            continue

        # Bin to 128 points so local storage + community submission match
        trace = downsample_trace(trace, n_bins=128)

        try:
            insert_tic_trace(run_id, trace.rt_min, trace.intensity, db_path=db_path)
            tic_metrics = compute_tic_metrics(trace)
            if tic_metrics.total_auc > 0:
                with sqlite3.connect(str(db_path)) as con:
                    con.execute(
                        "UPDATE runs SET tic_auc = ?, peak_rt_min = ? WHERE id = ?",
                        (tic_metrics.total_auc, tic_metrics.peak_rt_min, run_id),
                    )
            extracted += 1
            if verbose:
                console.print(f"  [green]TIC[/green] {run_name}")
        except Exception:
            logger.exception("Failed to store TIC for %s", run_name)
            failed += 1
            continue

        # ── Peptide/protein count repair ──────────────────────────
        # If this run has precursors but zero peptides, recompute from
        # the report.parquet. This fixes the Lumos bug where older STAN
        # versions populated precursors but not peptides/proteins.
        pep_patch: dict = {}
        if (not run.get("n_peptides") or run["n_peptides"] == 0) and (run.get("n_precursors") or 0) > 0:
            report_path = None
            for stem_variant in (Path(run_name).stem, run_name, Path(raw_path_str).stem if raw_path_str else ""):
                if not stem_variant:
                    continue
                candidate = output_dir / stem_variant / "report.parquet"
                if candidate.exists():
                    report_path = candidate
                    break
            if report_path:
                try:
                    import polars as _pl
                    schema = _pl.read_parquet_schema(report_path)
                    avail = set(schema.keys()) if hasattr(schema, "keys") else set(schema)
                    cols_needed = []
                    if "Q.Value" in avail:
                        cols_needed.append("Q.Value")
                    if "Stripped.Sequence" in avail:
                        cols_needed.append("Stripped.Sequence")
                    if "Protein.Group" in avail:
                        cols_needed.append("Protein.Group")
                    if cols_needed and "Q.Value" in cols_needed:
                        rdf = _pl.read_parquet(report_path, columns=cols_needed)
                        rdf = rdf.filter(_pl.col("Q.Value") <= 0.01)
                        if "Stripped.Sequence" in rdf.columns:
                            pep_patch["n_peptides"] = rdf["Stripped.Sequence"].n_unique()
                        if "Protein.Group" in rdf.columns:
                            pep_patch["n_proteins"] = rdf["Protein.Group"].n_unique()
                        if pep_patch:
                            with sqlite3.connect(str(db_path)) as con:
                                for k, v in pep_patch.items():
                                    con.execute(f"UPDATE runs SET {k} = ? WHERE id = ?", (v, run_id))
                            if verbose:
                                console.print(
                                    f"  [cyan]peptides[/cyan] {run_name} "
                                    f"pep={pep_patch.get('n_peptides', '?')} "
                                    f"prot={pep_patch.get('n_proteins', '?')}"
                                )
                except Exception:
                    logger.debug("Peptide repair failed for %s", run_name, exc_info=True)

        # Queue for community push if this run was already submitted
        if push and run.get("submission_id"):
            push_data: dict = {}
            if trace:
                push_data["tic_rt_bins"] = [round(float(r), 3) for r in trace.rt_min]
                push_data["tic_intensity"] = [round(float(v), 0) for v in trace.intensity]
            if pep_patch:
                push_data.update(pep_patch)
            if push_data:
                pushed_rows.append((run["submission_id"], push_data))

    # ── sample_health TIC re-extraction (v0.2.150) ─────────────
    # backfill-tic historically only covered runs / tic_traces.
    # Sample-health rows (non-QC files: blanks, column equilibrations,
    # chowE standards, etc.) got their TICs from the watcher's live
    # ingest path, using whatever downsample_trace version happened to
    # be in the watcher's memory at acquisition time. The v0.2.147
    # sawtooth fix wasn't retroactive for these rows. With force=True
    # we re-extract every sample_health row with the current code so
    # the dashboard Sample panel stops showing the sawtooth on runs
    # that were live-ingested before the update.
    if force:
        from stan.db import insert_health_tic_trace
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            try:
                sh_rows = con.execute(
                    "SELECT id, run_name, raw_path FROM sample_health"
                ).fetchall()
            except sqlite3.OperationalError:
                sh_rows = []
        if sh_rows and verbose:
            console.print(
                f"\n[bold]Re-extracting TIC for {len(sh_rows)} sample_health rows...[/bold]"
            )
        sh_extracted = 0
        sh_skipped = 0
        for sh in sh_rows:
            raw = sh["raw_path"] or ""
            if not raw or not Path(raw).exists():
                sh_skipped += 1
                continue
            raw_path = Path(raw)
            trace = None
            try:
                if raw_path.is_dir() and raw_path.suffix == ".d":
                    trace = extract_tic_bruker(raw_path)
                elif raw_path.suffix.lower() == ".raw":
                    try:
                        trace = extract_tic_thermo(raw_path)
                    except Exception:
                        trace = None
            except Exception:
                trace = None
            if trace is None:
                sh_skipped += 1
                continue
            trace = downsample_trace(trace, n_bins=128)
            try:
                insert_health_tic_trace(
                    sh["id"], trace.rt_min, trace.intensity, db_path=db_path
                )
                sh_extracted += 1
                if verbose:
                    console.print(f"  [green]health TIC[/green] {sh['run_name']}")
            except Exception:
                sh_skipped += 1
        if verbose and sh_rows:
            console.print(
                f"[bold]Sample-health:[/bold] extracted={sh_extracted} skipped={sh_skipped}"
            )

    if verbose:
        console.print(
            f"\n[bold]Extracted:[/bold] {extracted}  "
            f"[bold]Failed:[/bold] {failed}  "
            f"[bold]Skipped:[/bold] {skipped}"
        )
        # v0.2.151: break down the `failed` count by reason so the operator
        # can tell whether the missing coverage is fixable (raw_missing =
        # the disk moved) or code-level (extract_failed = parser bug).
        if failed:
            console.print("[bold]Skip reasons:[/bold]")
            for reason, n in sorted(skip_reasons.items(), key=lambda x: -x[1]):
                if n > 0:
                    console.print(f"  {reason:<24} {n}")

    # v0.2.152: also write a summary log file so the histogram syncs to
    # the Hive mirror via sync_to_hive_mirror's logs/ rule. Before this,
    # backfill-tic output only lived in the cmd console and was lost
    # when the window closed — Brett could see the histogram locally
    # but it never reached Hive for remote debugging.
    try:
        from stan.config import get_user_config_dir, sync_to_hive_mirror
        from datetime import datetime as _dt
        _log_dir = get_user_config_dir() / "logs"
        _log_dir.mkdir(parents=True, exist_ok=True)
        _log_path = _log_dir / f"backfill_tic_{_dt.now().strftime('%Y%m%d_%H%M%S')}.log"
        with open(_log_path, "w", encoding="utf-8") as _fh:
            _fh.write(f"backfill-tic summary  push={push}  force={force}\n")
            _fh.write(f"db: {db_path}\n\n")
            _fh.write(f"Extracted: {extracted}\n")
            _fh.write(f"Failed:    {failed}\n")
            _fh.write(f"Skipped:   {skipped}\n\n")
            _fh.write("Skip reasons:\n")
            for reason, n in sorted(skip_reasons.items(), key=lambda x: -x[1]):
                if n > 0:
                    _fh.write(f"  {reason:<24} {n}\n")
        try:
            sync_to_hive_mirror(include_reports=False)
        except Exception:
            pass
        if verbose:
            console.print(f"[dim]Log: {_log_path}[/dim]")
    except Exception:
        logger.debug("Failed to write backfill-tic summary log", exc_info=True)

    # Push corrections to the community relay
    if push and pushed_rows:
        from stan.community.submit import RELAY_URL
        from stan.config import load_community
        try:
            _comm = load_community()
        except Exception:
            _comm = {}
        _push_token = _comm.get("auth_token", "")

        console.print(
            f"Pushing [bold]{len(pushed_rows)}[/bold] corrections to the relay..."
        )
        ok = 0
        # v0.2.155: capture per-row push errors so the summary log
        # records WHY pushes failed (rate limit? auth? relay down?).
        # Previously only logger.exception fired, invisible to the Hive
        # mirror — Brett 2026-04-22 saw "HF errors" we couldn't diagnose.
        push_errors: list[dict] = []
        for sub_id, push_data in pushed_rows:
            try:
                data = json.dumps(push_data).encode("utf-8")
                _hdrs = {"Content-Type": "application/json"}
                if _push_token:
                    _hdrs["X-STAN-Auth"] = _push_token
                req = urllib.request.Request(
                    f"{RELAY_URL}/api/update/{sub_id}",
                    data=data, method="POST",
                    headers=_hdrs,
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    if resp.status == 200:
                        ok += 1
                    else:
                        push_errors.append({
                            "sub_id": sub_id[:8],
                            "status": resp.status,
                            "error": f"HTTP {resp.status}",
                        })
            except urllib.error.HTTPError as e:
                push_errors.append({
                    "sub_id": sub_id[:8], "status": e.code,
                    "error": f"HTTPError: {e.reason}",
                })
            except urllib.error.URLError as e:
                push_errors.append({
                    "sub_id": sub_id[:8], "status": None,
                    "error": f"URLError: {e.reason}",
                })
            except Exception as e:
                push_errors.append({
                    "sub_id": sub_id[:8], "status": None,
                    "error": f"{type(e).__name__}: {e}",
                })
                logger.exception("Relay TIC push failed for %s", sub_id[:8])
        console.print(f"  [green]{ok}[/green] pushed, [red]{len(pushed_rows) - ok}[/red] failed")

        # Append push-error summary to the syncable log (appended AFTER
        # the main summary block written below).
        try:
            _log_dir = get_user_config_dir() / "logs"
            _log_dir.mkdir(parents=True, exist_ok=True)
            from datetime import datetime as _dt
            _push_log = _log_dir / f"backfill_tic_push_{_dt.now().strftime('%Y%m%d_%H%M%S')}.log"
            with open(_push_log, "w", encoding="utf-8") as _fh:
                _fh.write(f"backfill-tic --push summary\n")
                _fh.write(f"attempted: {len(pushed_rows)}\n")
                _fh.write(f"succeeded: {ok}\n")
                _fh.write(f"failed:    {len(push_errors)}\n\n")
                if push_errors:
                    # Histogram by status / error-type
                    from collections import Counter
                    by_status = Counter(
                        (e.get("status"), e.get("error", "").split(":")[0])
                        for e in push_errors
                    )
                    _fh.write("Failure histogram:\n")
                    for (status, kind), n in by_status.most_common():
                        _fh.write(f"  status={status}  {kind}  x{n}\n")
                    _fh.write("\nFirst 20 failures:\n")
                    for err in push_errors[:20]:
                        _fh.write(f"  {err}\n")
        except Exception:
            logger.debug("push-error log write failed", exc_info=True)

    return (extracted, skipped, failed)


@app.command("backfill-tic")
def backfill_tic(
    push: bool = typer.Option(
        False, "--push",
        help="Also push extracted TIC traces to the community relay for "
             "runs that were already submitted.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-extract TIC for every run, not just ones missing one. "
             "Auto-skipped if this STAN version's force migration is "
             "already marked complete (marker at ~/STAN/.backfill_tic_force_v<ver>.done). "
             "Pass --really-force to bypass the marker.",
    ),
    really_force: bool = typer.Option(
        False, "--really-force",
        help="Force re-extraction even if the version marker says we're done. "
             "Use when an extractor bug needs a second sweep for the same version.",
    ),
) -> None:
    """Re-extract TIC traces for runs that are missing one (or all, with --force).

    With ``--force`` the first time on a given STAN version, every run's TIC
    is re-extracted. On success a marker file is written; subsequent updates
    pass --force but the command auto-degrades to gap-only because the
    migration is already complete for that version. This prevents the
    overnight backfill chain (updater PS1) from redundantly re-sweeping
    277 runs on every click.
    """
    from datetime import datetime
    from stan.config import get_user_config_dir
    from stan import __version__ as _stan_ver

    marker_dir = get_user_config_dir()
    marker = marker_dir / f".backfill_tic_force_v{_stan_ver}.done"

    # Version-sentinel dance: if caller asked for --force but this
    # version's force sweep already ran, silently degrade to gap-only.
    if force and not really_force and marker.exists():
        console.print(
            f"[dim]--force skipped: marker present ({marker.name}). "
            f"Running gap-only sweep. Use --really-force to override.[/dim]"
        )
        force = False

    _backfill_tic_impl(push=push, verbose=True, force=force)

    # On success, persist the marker so the next update is a no-op.
    if (force or really_force):
        try:
            marker_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                f"backfill-tic --force completed at "
                f"{datetime.now().isoformat(timespec='seconds')}\n"
                f"stan version: {_stan_ver}\n"
            )
        except Exception:
            logger.debug("Failed to write backfill-tic force marker", exc_info=True)


@app.command("verify-community-tics")
def verify_community_tics(
    submitter: str = typer.Option(
        "", "--submitter",
        help="Filter to a specific submitter pseudonym. Default = all submissions "
             "(use when you're the only lab, or want a fleet-wide check).",
    ),
    sign_flip_threshold: int = typer.Option(
        45, "--threshold",
        help="Minimum number of bin-to-bin sign flips (out of ~127 possible) "
             "to flag a trace as sawtoothed. Smooth TICs score <20; the old "
             "sum-per-bin Bruker artifact produces 55–75.",
    ),
) -> None:
    """Scan community TIC submissions for the v0.2.147 sawtooth artifact.

    Fetches ``brettsp/stan-benchmark/benchmark_latest.parquet``, counts
    bin-to-bin sign flips in each submission's TIC, and prints
    submission_ids whose TIC still looks like the pre-v0.2.147 sum-per-bin
    output. After running ``stan backfill-tic --force --push`` overnight,
    this should return zero flagged submissions — any that remain either
    failed to push (offline during update, missing submission_id, relay
    rejected) or come from a lab that hasn't updated yet.

    Use this the morning after the overnight backfill to confirm the
    community dataset is fully corrected.
    """
    import polars as pl

    from stan.community.fetch import fetch_benchmark_latest

    console.print("[dim]Downloading benchmark_latest.parquet from HF Dataset...[/dim]")
    path = fetch_benchmark_latest()
    if path is None:
        console.print("[red]Could not download the community parquet.[/red]")
        raise typer.Exit(code=1)

    df = pl.read_parquet(path)
    total = len(df)
    console.print(f"Loaded [bold]{total}[/bold] submissions from community dataset.")

    if submitter:
        for col in ("submitter_pseudonym", "lab", "submitter"):
            if col in df.columns:
                df = df.filter(pl.col(col) == submitter)
                console.print(f"Filtered to submitter={submitter!r} via {col}: {len(df)} rows")
                break

    if "tic_intensity" not in df.columns:
        console.print("[red]No tic_intensity column in the parquet. "
                      "Community schema may have changed.[/red]")
        raise typer.Exit(code=1)

    def sign_flips(seq) -> int:
        """Count bin-to-bin sign-flip count in first-diff of a sequence.

        Smooth TIC: 5–20 flips per 128 bins.
        Sum-per-bin sawtooth artifact: 55–75 flips (alternating up-down
        at the bin-count quantization frequency).
        """
        if seq is None or len(seq) < 4:
            return 0
        diffs = [float(seq[i+1]) - float(seq[i]) for i in range(len(seq) - 1)]
        flips = 0
        for i in range(len(diffs) - 1):
            if diffs[i] * diffs[i+1] < 0:
                flips += 1
        return flips

    flagged: list[tuple] = []
    clean = 0
    no_tic = 0
    for row in df.iter_rows(named=True):
        tic = row.get("tic_intensity")
        if tic is None or len(tic) < 4:
            no_tic += 1
            continue
        sf = sign_flips(tic)
        if sf >= sign_flip_threshold:
            flagged.append((
                row.get("submission_id") or "—",
                row.get("run_name") or "—",
                row.get("instrument_model") or row.get("instrument_family") or "—",
                row.get("spd"),
                sf,
            ))
        else:
            clean += 1

    console.print()
    console.print(f"[green]Clean:[/green]   {clean:>4} submissions  (sign-flips < {sign_flip_threshold})")
    console.print(f"[yellow]No TIC:[/yellow]  {no_tic:>4} submissions")
    console.print(f"[red]Flagged:[/red] {len(flagged):>4} submissions  (still sawtoothed)")

    if not flagged:
        console.print()
        console.print("[bold green]All community TICs are clean.[/bold green]")
        return

    console.print()
    console.print("[bold]Flagged submissions (sorted by worst):[/bold]")
    flagged.sort(key=lambda x: -x[4])
    for sid, run_name, instrument, spd, sf in flagged[:50]:
        console.print(
            f"  [red]{sf:>3} flips[/red]  spd={spd or '?':<4}  "
            f"{instrument[:18]:<18}  {run_name[:50]:<50}  id={sid}"
        )
    if len(flagged) > 50:
        console.print(f"  [dim]... and {len(flagged) - 50} more[/dim]")

    console.print()
    console.print("[dim]To re-push fixes for these runs, run:[/dim]")
    console.print("[dim]  stan backfill-tic --force --push[/dim]")


@app.command("fix-spds")
def fix_spds(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show proposed changes without updating the DB."
    ),
) -> None:
    """Re-validate SPD for every run in the local DB.

    Walks the runs table, re-reads the raw file for each row, and updates
    the ``spd`` column if ``validate_spd_from_metadata()`` disagrees with
    the stored value. This fixes baselines where every run was stamped
    with the cohort default instead of its per-file gradient.

    Prints a diff summary at the end (old SPD → new SPD counts).
    """
    import sqlite3

    from stan.db import get_db_path, init_db
    from stan.metrics.scoring import (
        gradient_min_to_spd,
        validate_spd_from_metadata,
    )

    init_db()
    db_path = get_db_path()

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, run_name, raw_path, spd, gradient_length_min FROM runs"
        ).fetchall()

    console.print(f"Checking [bold]{len(rows)}[/bold] runs for SPD mismatches...")

    updates: list[tuple[str, str, int | None, int, int | None]] = []
    missing = 0
    unchanged = 0

    for row in rows:
        raw_path_str = row["raw_path"]
        if not raw_path_str:
            missing += 1
            continue
        raw_path = Path(raw_path_str)
        if not raw_path.exists():
            missing += 1
            continue

        new_spd = validate_spd_from_metadata(raw_path)
        if new_spd is None and row["gradient_length_min"]:
            new_spd = gradient_min_to_spd(int(row["gradient_length_min"]))
        if new_spd is None:
            missing += 1
            continue

        old_spd = row["spd"]
        if old_spd == new_spd:
            unchanged += 1
            continue

        updates.append(
            (row["id"], row["run_name"], old_spd, new_spd, row["gradient_length_min"])
        )

    # Print proposed changes
    if updates:
        console.print()
        console.print(f"[bold]{len(updates)} runs need SPD correction:[/bold]")
        # Group by (old, new) for a compact summary
        from collections import Counter
        transitions: Counter = Counter()
        for _rid, _name, old_spd, new_spd, _grad in updates:
            transitions[(old_spd, new_spd)] += 1
        for (old_spd, new_spd), n in sorted(transitions.items(), key=lambda x: -x[1]):
            console.print(f"  {old_spd} SPD -> {new_spd} SPD : [cyan]{n}[/cyan] runs")

        # Show first 10 examples
        console.print()
        console.print("[dim]Examples (first 10):[/dim]")
        for rid, name, old_spd, new_spd, grad in updates[:10]:
            console.print(
                f"  {name}  grad={grad}m  {old_spd} -> {new_spd} SPD"
            )
    else:
        console.print("[green]All runs already have correct SPDs.[/green]")

    console.print()
    console.print(
        f"[dim]Unchanged: {unchanged}  Missing raw files: {missing}  "
        f"Needs update: {len(updates)}[/dim]"
    )

    if dry_run:
        console.print()
        console.print("[yellow]--dry-run: no changes written.[/yellow]")
        return

    if not updates:
        return

    # Apply updates
    with sqlite3.connect(str(db_path)) as con:
        for rid, _name, _old, new_spd, _grad in updates:
            con.execute(
                "UPDATE runs SET spd = ? WHERE id = ?",
                (new_spd, rid),
            )
        con.commit()

    console.print(f"[green]Updated {len(updates)} runs.[/green]")
    console.print(
        "[dim]Run [cyan]stan sync[/cyan] to push corrected SPDs "
        "to the community benchmark.[/dim]"
    )


@app.command("repair-metadata")
def repair_metadata(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show proposed changes without updating the DB."
    ),
    push: bool = typer.Option(
        False, "--push",
        help="Also push corrections to the community relay for runs that "
             "were already submitted. Uses /api/update/{id}.",
    ),
) -> None:
    """Re-read raw-file metadata and fix SPD, run_date, and lc_system.

    Walks every row in the local runs table, re-reads the raw file at
    ``raw_path``, and updates:

      * ``spd`` — from validate_spd_from_metadata() (Bruker XML is
        authoritative; Thermo falls back to fisher_py + gradient snap)
      * ``run_date`` — from get_acquisition_date() (analysis.tdf
        GlobalMetadata.AcquisitionDateTime for Bruker, fisher_py
        CreationDate for Thermo)
      * ``lc_system`` — from detect_lc_system() (Bruker .d XML tree
        for Evosep; Thermo currently returns None so we leave the
        column empty)

    This is the fix for historical baselines where the client wrote
    today's date + cohort-default SPD for every run. It does NOT
    re-run DIA-NN or Sage — metadata only.

    With --push, also forwards the corrections to the HF Space relay
    at /api/update/{submission_id} for runs that were previously
    submitted to the community benchmark. The relay rewrites the
    stored parquet in place and invalidates its cache.
    """
    import json
    import sqlite3
    import urllib.error
    import urllib.request

    from stan.db import get_db_path, init_db
    from stan.metrics.scoring import (
        detect_lc_system,
        gradient_min_to_spd,
        validate_spd_from_metadata,
    )
    from stan.watcher.acquisition_date import get_acquisition_date

    init_db()
    db_path = get_db_path()

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, run_name, raw_path, spd, run_date, lc_system, "
            "gradient_length_min, submission_id FROM runs"
        ).fetchall()

    console.print(
        f"Repairing metadata for [bold]{len(rows)}[/bold] runs in "
        f"[dim]{db_path}[/dim]..."
    )

    updates: list[dict] = []
    missing = 0
    unchanged = 0

    for row in rows:
        raw_path_str = row["raw_path"]
        if not raw_path_str:
            missing += 1
            continue
        raw_path = Path(raw_path_str)
        if not raw_path.exists():
            missing += 1
            continue

        # Extract from raw file
        new_spd = validate_spd_from_metadata(raw_path)
        if new_spd is None and row["gradient_length_min"]:
            new_spd = gradient_min_to_spd(int(row["gradient_length_min"]))
        new_date = get_acquisition_date(raw_path)
        new_lc = detect_lc_system(raw_path)

        # Compare against stored values
        patch: dict = {}
        if new_spd is not None and new_spd != row["spd"]:
            patch["spd"] = new_spd
        if new_date and new_date != row["run_date"]:
            patch["run_date"] = new_date
        if new_lc and new_lc != (row["lc_system"] or ""):
            patch["lc_system"] = new_lc

        if not patch:
            unchanged += 1
            continue

        updates.append({
            "run_id": row["id"],
            "run_name": row["run_name"],
            "submission_id": row["submission_id"],
            "patch": patch,
            "old": {
                "spd": row["spd"],
                "run_date": row["run_date"],
                "lc_system": row["lc_system"],
            },
        })

    # Print proposed changes
    if updates:
        console.print()
        console.print(f"[bold]{len(updates)} runs need metadata correction:[/bold]")
        from collections import Counter
        field_counts: Counter = Counter()
        spd_transitions: Counter = Counter()
        for u in updates:
            for k in u["patch"]:
                field_counts[k] += 1
            if "spd" in u["patch"]:
                spd_transitions[(u["old"]["spd"], u["patch"]["spd"])] += 1
        for field, n in field_counts.most_common():
            console.print(f"  {field}: [cyan]{n}[/cyan] runs")
        if spd_transitions:
            console.print("[dim]SPD transitions:[/dim]")
            for (old_s, new_s), n in sorted(
                spd_transitions.items(), key=lambda x: -x[1]
            ):
                console.print(f"  {old_s} -> {new_s} SPD : [cyan]{n}[/cyan] runs")

        console.print()
        console.print("[dim]Examples (first 10):[/dim]")
        for u in updates[:10]:
            diffs = ", ".join(
                f"{k}={u['old'].get(k)}->{u['patch'][k]}"
                for k in u["patch"]
            )
            console.print(f"  {u['run_name']}  [{diffs}]")
    else:
        console.print("[green]All runs already have correct metadata.[/green]")

    console.print()
    console.print(
        f"[dim]Unchanged: {unchanged}  Missing raw files: {missing}  "
        f"Needs update: {len(updates)}[/dim]"
    )

    if dry_run:
        console.print()
        console.print("[yellow]--dry-run: no changes written.[/yellow]")
        return

    if not updates:
        return

    # Apply local DB updates
    with sqlite3.connect(str(db_path)) as con:
        for u in updates:
            cols = ", ".join(f"{k} = ?" for k in u["patch"])
            vals = list(u["patch"].values()) + [u["run_id"]]
            con.execute(f"UPDATE runs SET {cols} WHERE id = ?", vals)
        con.commit()
    console.print(f"[green]Updated {len(updates)} runs in local DB.[/green]")

    # Optional: push corrections to the community relay
    if not push:
        console.print(
            "[dim]Run with [cyan]--push[/cyan] to also update "
            "already-submitted runs on the community benchmark.[/dim]"
        )
        return

    from stan.community.submit import RELAY_URL  # noqa: E402

    submitted = [u for u in updates if u["submission_id"]]
    if not submitted:
        console.print(
            "[dim]No submitted runs needed updating on the community relay.[/dim]"
        )
        return

    # Auth token for /api/update — prevents forks from patching data
    _repair_token = community_config.get("auth_token", "")

    console.print(
        f"Pushing [bold]{len(submitted)}[/bold] corrections to the relay..."
    )
    pushed = 0
    failed = 0
    for u in submitted:
        try:
            data = json.dumps(u["patch"]).encode("utf-8")
            _hdrs = {"Content-Type": "application/json"}
            if _repair_token:
                _hdrs["X-STAN-Auth"] = _repair_token
            req = urllib.request.Request(
                f"{RELAY_URL}/api/update/{u['submission_id']}",
                data=data,
                method="POST",
                headers=_hdrs,
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status == 200:
                    pushed += 1
                else:
                    failed += 1
        except urllib.error.HTTPError as e:
            logger.warning(
                "Relay update failed for %s: HTTP %s", u["submission_id"][:8], e.code
            )
            failed += 1
        except Exception:
            logger.exception("Relay update failed for %s", u["submission_id"][:8])
            failed += 1

    console.print(
        f"[green]Pushed: {pushed}[/green]  [red]Failed: {failed}[/red]"
    )
    if pushed:
        console.print(
            "[dim]The HF Space dashboard cache will refresh within 5 minutes "
            "(or now at https://brettsp-stan.hf.space/api/leaderboard?refresh=1).[/dim]"
        )


@app.command()
def baseline_download(
    instrument_family: str = typer.Option(None, "--instrument", "-i", help="e.g. Astral, timsTOF, Exploris"),
    spd: int = typer.Option(None, "--spd", help="Samples per day"),
    amount_ng: float = typer.Option(None, "--amount", help="HeLa amount in ng"),
    cache: bool = typer.Option(False, "--cache", help="Cache full baseline locally"),
) -> None:
    """Download baseline statistics from the STAN community benchmark.

    Instead of building a baseline from your own QC history, pull community
    reference ranges directly. Useful for new instruments or labs without
    historical data.
    """
    from stan.community.fetch_baseline import cache_baseline_locally, fetch_community_baseline

    if cache:
        path = cache_baseline_locally()
        console.print(f"[green]Cached community baseline to {path}[/green]")
        return

    console.print("[bold]Fetching community baseline...[/bold]")
    stats = fetch_community_baseline(
        instrument_family=instrument_family,
        spd=spd,
        amount_ng=amount_ng,
    )

    if not stats or stats.get("matching_submissions") == 0:
        console.print("[yellow]No matching community data found.[/yellow]")
        console.print("Try removing filters or checking back later as more labs contribute.")
        return

    n = stats.get("n_submissions", 0)
    console.print(f"\n[bold]Community baseline ({n} matching submissions)[/bold]")
    console.print()

    from rich.table import Table
    t = Table(show_header=True, header_style="bold", border_style="blue")
    t.add_column("Metric")
    t.add_column("25th", justify="right")
    t.add_column("Median", justify="right")
    t.add_column("75th", justify="right")

    metrics_display = [
        ("n_precursors", "Precursors (DIA)"),
        ("n_peptides", "Peptides"),
        ("n_proteins", "Proteins"),
        ("n_psms", "PSMs (DDA)"),
        ("ips_score", "IPS"),
        ("median_fragments_per_precursor", "Fragments/precursor"),
        ("median_points_across_peak", "Points/peak"),
    ]
    for key, label in metrics_display:
        q25 = stats.get(f"{key}_q25")
        med = stats.get(f"{key}_median")
        q75 = stats.get(f"{key}_q75")
        if med is not None:
            def fmt(v):
                if v is None:
                    return "--"
                return f"{int(v):,}" if v >= 10 else f"{v:.2f}"
            t.add_row(label, fmt(q25), fmt(med), fmt(q75))

    console.print(t)
    console.print()

    if "instrument_breakdown" in stats:
        console.print("[dim]Instruments in this cohort:[/dim]")
        for model, count in sorted(stats["instrument_breakdown"].items(), key=lambda x: -x[1]):
            console.print(f"  {model}: {count}")


@app.command()
def watch() -> None:
    """Start the instrument watcher daemon.

    Monitors directories configured in instruments.yml for new raw files,
    detects acquisition mode, and dispatches search jobs.

    v0.2.155: watcher logs are now mirrored to
    ``~/STAN/logs/watch_<ts>.log`` and synced to Hive every 5 minutes.
    Cascade bugs, observer deaths, and unhandled exceptions used to be
    invisible to Claude troubleshooting the Hive mirror — now they're
    captured in a syncable log + an alert file on crash.
    """
    import logging as _logging
    import sys as _sys
    import threading as _threading
    import time as _time
    from datetime import datetime as _dt

    from stan.watcher.daemon import WatcherDaemon
    from stan.config import get_user_config_dir
    from stan.backfill_telemetry import write_alert

    console.print(f"[bold]STAN v{__version__}[/bold] — watcher starting")
    console.print()

    # File-log setup: attach a FileHandler to the root logger so every
    # module's warn/error/info shows up in the mirrored log. Stderr
    # handler stays — operator still sees messages in the console.
    log_dir = get_user_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"watch_{_dt.now().strftime('%Y%m%d_%H%M%S')}.log"
    try:
        fh = _logging.FileHandler(str(log_path), encoding="utf-8")
        fh.setLevel(_logging.INFO)
        fh.setFormatter(_logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
        ))
        _logging.getLogger().addHandler(fh)
        _logging.getLogger().setLevel(_logging.INFO)
        console.print(f"[dim]Log: {log_path}[/dim]")
    except Exception:
        console.print("[yellow]Warning: could not open watcher log file[/yellow]")

    # Unhandled-exception alert: intercept sys.excepthook so a
    # watcher crash drops a high-signal alert into ~/STAN/alerts/.
    _orig_excepthook = _sys.excepthook

    def _alert_on_crash(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            return _orig_excepthook(exc_type, exc_value, exc_tb)
        try:
            write_alert(
                kind="watcher_crash",
                summary=f"stan watch crashed: {exc_type.__name__}: {exc_value}",
                payload={
                    "exc_type": exc_type.__name__,
                    "exc_value": str(exc_value),
                    "log_path": str(log_path),
                },
            )
        except Exception:
            pass
        return _orig_excepthook(exc_type, exc_value, exc_tb)

    _sys.excepthook = _alert_on_crash

    # Periodic sync thread: daemon=True so it dies when the main thread
    # exits. 5-minute cadence keeps Hive within ~5 min of the live
    # watcher state without over-syncing during idle periods.
    _stop_sync = _threading.Event()

    def _sync_loop():
        from stan.config import sync_to_hive_mirror
        while not _stop_sync.wait(300):  # 5 min
            try:
                sync_to_hive_mirror(include_reports=False)
            except Exception:
                _logging.getLogger(__name__).debug(
                    "watcher periodic sync failed", exc_info=True,
                )

    sync_thread = _threading.Thread(target=_sync_loop, daemon=True,
                                    name="watch-hive-sync")
    sync_thread.start()

    daemon = WatcherDaemon()
    try:
        daemon.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")
        daemon.stop()
    finally:
        _stop_sync.set()
        # Final sync on exit so the last log lines make it to Hive.
        try:
            from stan.config import sync_to_hive_mirror
            sync_to_hive_mirror(include_reports=False)
        except Exception:
            pass


@app.command()
def dashboard(
    port: int = typer.Option(8421, "--port", "-p", help="Dashboard port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Dashboard host"),
) -> None:
    """Start the local STAN dashboard.

    Serves the QC dashboard at http://localhost:8421.
    """
    import uvicorn

    console.print(f"[bold]STAN v{__version__}[/bold] — dashboard")
    console.print(f"  http://{host}:{port}")
    console.print(f"  API docs: http://{host}:{port}/docs")
    console.print()

    uvicorn.run(
        "stan.dashboard.server:app",
        host=host,
        port=port,
        log_level="info",
    )


@app.command()
def column_health(
    instrument: str = typer.Argument(..., help="Instrument name to assess"),
) -> None:
    """Assess column health from longitudinal TIC trends."""
    from stan.metrics.column_health import assess_column_health

    report = assess_column_health(instrument)
    if report is None:
        console.print("[yellow]Insufficient data for column health assessment.[/yellow]")
        console.print("Need at least 10 runs with TIC AUC data.")
        return

    color = {"healthy": "green", "watch": "yellow", "degraded": "red"}.get(report.status, "white")
    console.print(f"[bold]Column health: [{color}]{report.status.upper()}[/{color}][/bold]")
    console.print(f"  Runs analyzed: {report.n_runs}")
    console.print(f"  TIC AUC slope: {report.tic_auc_trend_slope} (R²={report.tic_auc_r2})")
    console.print(f"  Peak RT slope: {report.peak_rt_trend_slope} (R²={report.peak_rt_r2})")
    console.print(f"  {report.message}")


@app.command("log")
def log_event_cmd(
    instrument: str = typer.Argument(..., help="Instrument name"),
    event: str = typer.Argument(
        ...,
        help="Event type: column-change, source-clean, calibration, pm, lc-service, other",
    ),
    notes: str = typer.Option("", "--notes", "-n", help="Description of what was done"),
    operator: str = typer.Option("", "--operator", "-op", help="Who performed the maintenance"),
    column: str = typer.Option(None, "--column", "-c", help="New column description (for column-change)"),
) -> None:
    """Log a maintenance event (column change, source cleaning, calibration, etc.).

    STAN tracks these events and overlays them on trend charts so you can see
    cause-and-effect. Column changes reset the injection counter for column
    lifetime tracking.

    Examples:

      stan log Lumos column-change --column "PepSep 25cm x 150um" --operator "Brett"

      stan log Lumos source-clean --notes "Cleaned emitter + ion transfer tube"

      stan log Lumos calibration --notes "Positive mode FlexMix"
    """
    from stan.db import log_event, get_column_lifetime, EVENT_TYPES

    # Normalize event type
    event_type = event.lower().replace("-", "_")
    if event_type not in EVENT_TYPES:
        console.print(f"[red]Unknown event type: {event}[/red]")
        console.print(f"Valid types: {', '.join(EVENT_TYPES)}")
        raise typer.Exit(1)

    # Parse column info for column_change events
    column_vendor = column_model = None
    if column and event_type == "column_change":
        # Simple parse: if column contains a known vendor, split it out
        col_lower = column.lower()
        for vendor in ["pepsep", "ionopticks", "thermo", "waters", "phenomenex", "agilent"]:
            if vendor in col_lower:
                column_vendor = vendor.title()
                column_model = column
                break
        if not column_vendor:
            column_model = column

    event_id = log_event(
        instrument=instrument,
        event_type=event_type,
        notes=notes,
        operator=operator,
        column_vendor=column_vendor,
        column_model=column_model,
    )

    console.print(f"[green]Logged[/green] {event_type} on {instrument} (event {event_id})")

    # Show column lifetime summary after a column change
    if event_type == "column_change":
        life = get_column_lifetime(instrument)
        if life.get("injections_since_change", 0) > 0:
            console.print(f"  Previous column: {life['injections_since_change']} injections over {life['days_on_column']} days")
        console.print(f"  New column: {column or '(not specified)'}")
        console.print(f"  Injection counter reset to 0")


@app.command("email-report")
def email_report(
    send: bool = typer.Option(False, "--send", help="Send daily report now"),
    send_weekly: bool = typer.Option(False, "--send-weekly", help="Send weekly summary now"),
    test: bool = typer.Option(False, "--test", help="Send a test email to verify setup"),
    enable: bool = typer.Option(False, "--enable", help="Enable scheduled email reports"),
    disable: bool = typer.Option(False, "--disable", help="Disable scheduled email reports"),
    to: str = typer.Option(None, "--to", help="Recipient email address"),
    daily: str = typer.Option("07:00", "--daily", help="Daily report time (HH:MM)"),
    weekly: str = typer.Option("monday", "--weekly", help="Weekly report day"),
) -> None:
    """Send or configure daily/weekly QC email reports.

    Examples:

      stan email-report --send             Send daily report now

      stan email-report --send-weekly      Send weekly summary now

      stan email-report --test             Send a test email to verify setup

      stan email-report --enable --to EMAIL --daily 07:00 --weekly monday

      stan email-report --disable
    """
    from stan.reports.daily_email import (
        get_email_config,
        install_scheduled_task,
        save_email_config,
        send_daily_report,
        send_test_email,
        send_weekly_report,
    )

    if disable:
        save_email_config(enabled=False, to="")
        console.print("[yellow]Email reports disabled.[/yellow]")
        return

    if enable:
        if not to:
            cfg = get_email_config()
            to = cfg.get("to", "")
        if not to:
            console.print("[red]--to EMAIL is required when enabling reports.[/red]")
            raise typer.Exit(1)
        save_email_config(enabled=True, to=to, daily=daily, weekly=weekly)
        console.print("[green]Email reports enabled.[/green]")
        console.print(f"  To: {to}")
        console.print(f"  Daily at: {daily}")
        console.print(f"  Weekly on: {weekly}")
        console.print()
        # Show cron/schtasks instructions
        try:
            instructions = install_scheduled_task(daily_time=daily)
            console.print("[bold]To automate delivery:[/bold]")
            console.print(instructions)
        except RuntimeError as exc:
            console.print(f"[yellow]Could not create scheduled task: {exc}[/yellow]")
            console.print("You can run manually: stan email-report --send")
        return

    if test:
        console.print("Sending test email...")
        try:
            result = send_test_email(to=to)
            console.print(f"[green]Test email sent![/green] ID: {result.get('id', 'unknown')}")
        except Exception as exc:
            console.print(f"[red]Failed: {exc}[/red]")
            raise typer.Exit(1)
        return

    if send_weekly:
        console.print("Composing weekly summary...")
        try:
            result = send_weekly_report(to=to)
            console.print(f"[green]Weekly report sent![/green] ID: {result.get('id', 'unknown')}")
        except Exception as exc:
            console.print(f"[red]Failed: {exc}[/red]")
            raise typer.Exit(1)
        return

    if send:
        console.print("Composing daily report...")
        try:
            result = send_daily_report(to=to)
            console.print(f"[green]Daily report sent![/green] ID: {result.get('id', 'unknown')}")
        except Exception as exc:
            console.print(f"[red]Failed: {exc}[/red]")
            raise typer.Exit(1)
        return

    # No action specified -- show current config
    cfg = get_email_config()
    if cfg.get("enabled"):
        console.print("[bold]Email reports: [green]enabled[/green][/bold]")
        console.print(f"  To: {cfg.get('to', '(not set)')}")
        console.print(f"  Daily at: {cfg.get('daily', '07:00')}")
        console.print(f"  Weekly on: {cfg.get('weekly', 'monday')}")
    else:
        console.print("[bold]Email reports: [yellow]disabled[/yellow][/bold]")
        console.print()
        console.print("To enable:")
        console.print("  [cyan]stan email-report --enable --to your@email.com[/cyan]")
        console.print()
        console.print("To send a one-off report:")
        console.print("  [cyan]stan email-report --send[/cyan]")


@app.command("backfill-metrics")
def backfill_metrics(
    push: bool = typer.Option(
        False, "--push",
        help="Push updated metrics to the community relay via /api/update.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would be updated without writing to DB or relay.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help=(
            "Overwrite existing metric values, not just NULL/zero gaps. "
            "Use this after the extractor code is fixed (e.g. the v0.2.105 "
            "Bruker pts/peak correction) to replace stale values written "
            "by the previous version."
        ),
    ),
    only: str = typer.Option(
        "", "--only",
        help=(
            "Comma-separated list of specific metric fields to backfill. "
            "Default (empty) re-extracts every supported field. Useful "
            "when you only want to refresh one column, e.g. "
            "--only median_points_across_peak,ips_score."
        ),
    ),
) -> None:
    """Re-extract metrics from existing report.parquet files and fill gaps.

    Walks baseline_output/*/report.parquet for each configured instrument,
    re-runs the metric extractor (v0.2.105+ with correct pts/peak), and
    updates local DB rows where fields are NULL/zero. Recalculates IPS.

    With ``--push``, also POSTs updated fields to the community relay for
    runs that have a submission_id.

    With ``--force``, also overwrites fields that already have a non-null,
    non-zero value. Needed when an old extractor wrote stale numbers that
    the gap-filling logic would otherwise leave alone.

    With ``--only field1,field2``, only re-extract those fields (and
    ips_score, which recomputes from the others). Example:
    ``--only median_points_across_peak`` to fix the pts/peak regression
    without touching anything else.

    This is the one command that fills every data gap: dynamic_range,
    ms1_signal, ms2_signal, mass_accuracy, pts/peak, peak_width, IPS.
    """
    import json
    import sqlite3
    import urllib.error
    import urllib.request
    from pathlib import Path

    from stan.config import get_user_config_dir, load_instruments
    from stan.db import get_db_path, init_db
    from stan.metrics.chromatography import compute_ips_dia, compute_ips_dda
    from stan.metrics.extractor import extract_dia_metrics, extract_dda_metrics

    init_db()
    db_path = get_db_path()
    output_base = get_user_config_dir() / "baseline_output"

    if not output_base.exists():
        console.print("[yellow]No baseline_output directory found.[/yellow]")
        return

    # Persist a backfill diagnostic log so it syncs to the Hive mirror
    # via sync_to_hive_mirror's logs/ rule. Lets the operator ship the
    # complete reason-for-skip list back to whoever's debugging without
    # having to copy/paste from a terminal that already scrolled away.
    from datetime import datetime as _dt
    log_dir = get_user_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    diag_log_path = log_dir / f"backfill_metrics_{_dt.now().strftime('%Y%m%d_%H%M%S')}.log"
    diag_lines: list[str] = [
        f"backfill-metrics  push={push}  dry_run={dry_run}  force={force}  only={only or '(all)'}",
        f"db: {db_path}",
        "",
    ]

    # Load instrument config for vendor info
    try:
        _, inst_list = load_instruments()
    except Exception:
        inst_list = []

    # Build vendor lookup from config
    vendor_map: dict[str, str] = {}
    for inst in inst_list:
        vendor_map[inst.get("name", "")] = inst.get("vendor", "")

    # Fields we want to fill
    ALL_METRIC_FIELDS = [
        "dynamic_range_log10", "ms1_signal", "ms2_signal",
        "median_mass_acc_ms1_ppm", "median_mass_acc_ms2_ppm",
        "median_peak_width_sec", "median_points_across_peak",
        "fwhm_rt_min", "peak_capacity", "ips_score",
    ]
    if only:
        requested = {f.strip() for f in only.split(",") if f.strip()}
        unknown = requested - set(ALL_METRIC_FIELDS)
        if unknown:
            console.print(
                f"[red]Unknown field(s) in --only: {sorted(unknown)}. "
                f"Valid: {ALL_METRIC_FIELDS}[/red]"
            )
            raise typer.Exit(2)
        # Always keep ips_score so the recompute stays in sync when a
        # scoring input changed.
        METRIC_FIELDS = [f for f in ALL_METRIC_FIELDS if f in requested or f == "ips_score"]
        console.print(f"[dim]--only: updating {METRIC_FIELDS}[/dim]")
    else:
        METRIC_FIELDS = list(ALL_METRIC_FIELDS)
    if force:
        console.print("[yellow]--force: overwriting existing non-null values[/yellow]")

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        all_runs = con.execute(
            "SELECT * FROM runs ORDER BY run_date DESC"
        ).fetchall()

    console.print(f"[bold]{len(all_runs)} runs in DB[/bold]")

    updated = 0
    pushed = 0
    skipped = 0
    errors = 0

    for i, row in enumerate(all_runs):
        run_name = row["run_name"]
        run_id = row["id"]
        mode = row["mode"] or ""
        instrument = row["instrument"] or ""
        submission_id = row["submission_id"]

        # Find report.parquet
        stem = run_name
        for ext in (".d", ".raw"):
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        report_path = output_base / stem / "report.parquet"
        if not report_path.exists():
            report_path = output_base / run_name / "report.parquet"
        if not report_path.exists():
            skipped += 1
            continue

        # Which fields to (re-)populate. With --force we consider every
        # METRIC_FIELD fair game; without it we only touch NULL/zero
        # cells so correct values the operator has already accepted
        # don't get clobbered by a new extractor version.
        if force:
            missing = list(METRIC_FIELDS)
        else:
            missing = [f for f in METRIC_FIELDS
                       if row[f] is None or row[f] == 0]
        if not missing:
            skipped += 1
            continue

        # Find raw path so the Bruker accurate pts/peak path can fire
        # in extract_dia_metrics. PRE-v0.2.136 BUG: this used to gate
        # on `vendor_map.get(instrument)` returning "bruker". But the
        # DB stores the instrument *model* ("timsTOF HT") while
        # instruments.yml is keyed by the watcher *name* ("data_bruker"),
        # so the lookup always missed. Result: raw_path stayed None,
        # extract_dia_metrics got is_bruker=False, the broken fallback
        # ran, pts/peak landed at ~108 instead of the correct ~9.
        #
        # Fix: always honor the stored raw_path. extract_dia_metrics
        # itself derives is_bruker from the .d suffix when vendor isn't
        # passed, so we don't actually need vendor_map at all here.
        # Vendor inference is left to extract_dia_metrics.
        raw_path = None
        raw_path_diag = ""
        stored = row["raw_path"]
        if stored:
            candidate = Path(stored)
            if candidate.exists():
                raw_path = candidate
            else:
                raw_path_diag = f"raw_path not on disk: {stored}"
        else:
            raw_path_diag = "no raw_path stored on this row"
        # vendor still passed when we have it (helps for the Thermo
        # branch that doesn't have a .d directory to disambiguate).
        # vendor_map is keyed by instruments.yml `name` ("data_bruker")
        # while the DB stores the instrument *model* ("timsTOF HT") —
        # so the lookup almost always misses, which is exactly the
        # silent failure mode the v0.2.136 fix targets. Filename suffix
        # is the most reliable fallback.
        vendor = vendor_map.get(instrument, "")
        if not vendor:
            lname = (run_name or "").lower()
            if lname.endswith(".d"):
                vendor = "bruker"
            elif lname.endswith(".raw"):
                vendor = "thermo"

        # Re-extract metrics
        try:
            is_dia = "dia" in mode.lower() if mode else True
            if is_dia:
                metrics = extract_dia_metrics(
                    str(report_path),
                    raw_path=raw_path,
                    vendor=vendor or None,
                )
                metrics["instrument_family"] = instrument
                metrics["spd"] = row["spd"]
                new_ips = compute_ips_dia(metrics)
            else:
                metrics = extract_dda_metrics(str(report_path))
                metrics["instrument_family"] = instrument
                new_ips = compute_ips_dda(metrics)
            metrics["ips_score"] = new_ips
        except Exception as e:
            if errors < 3:
                console.print(f"  [red]Extract error: {run_name}: {e}[/red]")
            errors += 1
            continue

        # Build UPDATE set.
        # - Gap-fill mode (default): only write when old is NULL/zero
        #   AND new has a real value. Preserves operator-reviewed data.
        # - Force mode: write whenever new has a real value, even if
        #   old was already populated. Lets a new extractor replace
        #   stale values from a prior version.
        updates: dict = {}
        skipped_fields: list[tuple[str, str]] = []  # (field, reason) for diag
        for field in METRIC_FIELDS:
            old_val = row[field]
            new_val = metrics.get(field)
            if new_val is None or new_val == 0:
                # Diagnostic: when --force was requested but the new
                # extractor returned no value, the operator was probably
                # expecting an update and got nothing. Surface the
                # reason instead of silently moving on.
                if force and old_val is not None and old_val != 0:
                    skipped_fields.append((field, "extractor returned null"))
                continue
            gap = (old_val is None or old_val == 0)
            if force or gap:
                updates[field] = new_val

        if force and skipped_fields:
            reason_summary = ", ".join(f"{f}={r}" for f, r in skipped_fields)
            extra = f" [{raw_path_diag}]" if raw_path_diag else ""
            line = f"{run_name} -> no-op: {reason_summary}{extra}"
            # Always write to the log so we have the complete picture.
            diag_lines.append(line)
            # Echo the first ~8 to console so the operator sees the
            # pattern without scrolling 200 rows.
            if (errors + updated) < 8:
                console.print(f"  [dim]{run_name[:55]} → no-op for: {reason_summary}{extra}[/dim]")

        if not updates:
            skipped += 1
            continue

        if dry_run:
            if updated < 10:
                console.print(
                    f"  [dim]{run_name[:50]}[/dim] → "
                    f"{', '.join(f'{k}={v}' for k, v in list(updates.items())[:4])}"
                )
            updated += 1
            continue

        # Write to local DB
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [run_id]
        with sqlite3.connect(str(db_path)) as con:
            con.execute(f"UPDATE runs SET {set_clause} WHERE id = ?", vals)

        updated += 1

        # Push to relay
        if push and submission_id:
            try:
                from stan.community.submit import RELAY_URL
                body = json.dumps(updates).encode()
                req = urllib.request.Request(
                    f"{RELAY_URL}/api/update/{submission_id}",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": f"STAN/{__version__}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=15) as resp:
                    pushed += 1
            except Exception:
                pass  # non-fatal

        if (i + 1) % 25 == 0:
            console.print(
                f"  [dim]{i + 1}/{len(all_runs)} — "
                f"updated={updated} pushed={pushed} skipped={skipped}[/dim]"
            )

    action = "Would update" if dry_run else "Updated"
    console.print()
    console.print(f"[bold]{action} {updated} runs[/bold] "
                  f"(skipped {skipped}, errors {errors})")
    if push and not dry_run:
        console.print(f"  Pushed {pushed} to community relay")

    # Persist the diag log + sync to Hive mirror so a remote debugger
    # can see every "no-op for:" reason without copy/paste from the
    # operator's terminal.
    diag_lines.append("")
    diag_lines.append(
        f"summary: {action} {updated} runs (skipped {skipped}, errors {errors})"
    )
    try:
        diag_log_path.write_text("\n".join(diag_lines), encoding="utf-8")
        from stan.config import sync_to_hive_mirror
        try:
            sync_to_hive_mirror(include_reports=False)
        except Exception:
            pass
        console.print(f"[dim]Diag log: {diag_log_path}[/dim]")
    except Exception:
        logger.debug("could not write backfill diag log", exc_info=True)


@app.command("column-install")
def column_install(
    instrument: str = typer.Option(..., "--instrument", help="Instrument name (must match instruments.yml)."),
    vendor: str = typer.Option("", "--vendor", help='e.g. "Waters", "IonOpticks", "Aurora".'),
    model: str = typer.Option("", "--model", help='e.g. "HSS T3", "25cm 75um C18".'),
    serial: str = typer.Option("", "--serial", help="Column serial/lot number (optional)."),
    length_mm: Optional[int] = typer.Option(None, "--length-mm", help="Column length in mm."),
    id_um: Optional[int] = typer.Option(None, "--id-um", help="Inner diameter in µm."),
    particle_size_um: Optional[float] = typer.Option(None, "--particle-size-um", help="Particle size in µm."),
    operator: str = typer.Option("", "--operator", help='Who installed it. Default "".'),
    notes: str = typer.Option("", "--notes", help="Free-text notes."),
    date: Optional[str] = typer.Option(None, "--date", help="ISO date/datetime; default = now."),
) -> None:
    """Record a column install as a maintenance_events row.

    Convenience wrapper around `stan log <instrument> column-change`
    that accepts all the column-specific fields as explicit options
    and builds a structured notes string for the ones the table
    doesn't have columns for (length, id, particle size).

    Example:
      stan column-install --instrument timsTOF-Ultra-2 \\
        --vendor IonOpticks --model Aurora \\
        --length-mm 250 --id-um 75 --particle-size-um 1.7
    """
    from stan.db import init_db, log_event

    init_db()

    # Pack the dimension fields into the notes string so they survive
    # even though the table schema doesn't have dedicated columns for
    # them. Keep it parseable: "len=250mm id=75um ps=1.7um <user notes>"
    parts: list[str] = []
    if length_mm is not None:
        parts.append(f"len={length_mm}mm")
    if id_um is not None:
        parts.append(f"id={id_um}um")
    if particle_size_um is not None:
        parts.append(f"ps={particle_size_um}um")
    combined_notes = " ".join(parts)
    if notes:
        combined_notes = f"{combined_notes} {notes}".strip() if combined_notes else notes

    event_id = log_event(
        instrument=instrument,
        event_type="column_change",
        notes=combined_notes,
        operator=operator,
        event_date=date,
        column_vendor=vendor or None,
        column_model=model or None,
        column_serial=serial or None,
    )
    console.print(f"[green]Logged column install[/green] for [bold]{instrument}[/bold]")
    console.print(f"  event_id: {event_id}")
    if vendor or model:
        console.print(f"  column: {vendor} {model}".strip())
    if combined_notes:
        console.print(f"  notes: {combined_notes}")


@app.command("backfill-window-drift")
def backfill_window_drift(
    force: bool = typer.Option(
        False, "--force",
        help="Recompute drift even for rows that already have drift_class set.",
    ),
    instrument: str = typer.Option(
        "", "--instrument",
        help="Only backfill runs from this instrument (DB instrument string).",
    ),
    limit: int = typer.Option(
        0, "--limit",
        help="Stop after processing this many runs (0 = all).",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Log per-run details.",
    ),
) -> None:
    """Scan each run's .d for DIA window drift and populate drift_* columns.

    Bruker-only (Thermo .raw doesn't have the same "isolation windows
    with defined 1/K0 ranges" concept). Requires alphatims —
    `stan install-peg-deps` if not installed.

    Writes drift_coverage / drift_median_im / drift_p90_abs_im /
    drift_class to the runs table. Verdict semantics in
    stan.metrics.window_drift: ok / warn / drifted / unknown.
    """
    import json as _json
    import sqlite3
    from datetime import datetime, timezone

    from stan.config import get_user_config_dir, sync_to_hive_mirror
    from stan.db import (
        get_db_path, init_db, update_drift_result,
        insert_drift_window_centroids, insert_drift_peak_cloud,
    )
    from stan.metrics.window_drift import detect_window_drift

    init_db()
    db_path = get_db_path()

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        where = ["mode LIKE '%dia%'"]
        params: list = []
        if instrument:
            where.append("instrument = ?")
            params.append(instrument)
        if not force:
            # Queue runs missing the summary OR missing the breakdown
            # (v0.2.147+ added drift_window_centroids — rows from before
            # the upgrade have drift_class populated but no breakdown,
            # so we re-scan them on the first post-upgrade backfill so
            # the dashboard chart has data).
            where.append(
                "("
                " drift_class IS NULL OR drift_class = '' "
                " OR id NOT IN (SELECT run_id FROM drift_window_centroids "
                "               WHERE source = 'runs')"
                ")"
            )
        sql = (
            "SELECT id, run_name, instrument, raw_path FROM runs "
            "WHERE " + " AND ".join(where) + " ORDER BY run_date DESC"
        )
        if limit > 0:
            sql += f" LIMIT {limit}"
        rows = con.execute(sql, params).fetchall()

    console.print(f"[bold]{len(rows)} runs queued for drift backfill[/bold]")

    log_dir = get_user_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"backfill_drift_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    log_fh = open(log_path, "a", encoding="utf-8")

    def _log(record: dict) -> None:
        record["ts"] = datetime.now(timezone.utc).isoformat()
        log_fh.write(_json.dumps(record) + "\n")
        log_fh.flush()

    _log({"event": "start", "n_queued": len(rows), "force": force})

    # v0.2.153: streaming telemetry — abort after 10 consecutive same-
    # type errors and flush the log to Hive every 25 rows so remote
    # debugging doesn't have to wait for the full 2.5-hour loop to end.
    # This specifically targets the 2026-04-22 drift cascade where 220
    # of 220 runs failed with the same alphatims/polars ValueError.
    from stan.backfill_telemetry import (
        AbortIfRepeating, AbortedForRepeatingErrors, PeriodicSync,
    )
    guard = AbortIfRepeating(run_label="backfill-window-drift")
    sync = PeriodicSync()

    n_updated = 0
    n_skip_no_path = 0
    n_skip_unknown = 0
    n_errors = 0
    aborted = False

    for row in rows:
        run = dict(row)
        raw = run.get("raw_path") or ""
        if not raw:
            n_skip_no_path += 1
            _log({"event": "skip", "run_id": run["id"], "reason": "no raw_path"})
            continue
        raw_path = Path(raw)
        if not raw_path.exists():
            n_skip_no_path += 1
            _log({"event": "skip", "run_id": run["id"],
                  "reason": "raw_path missing on disk", "raw_path": raw})
            continue

        try:
            drift = detect_window_drift(raw_path)
        except Exception as e:
            n_errors += 1
            _log({"event": "error", "run_id": run["id"], "run_name": run["run_name"],
                  "error": str(e), "error_type": type(e).__name__})
            if n_errors <= 3:
                console.print(f"  [red]{run['run_name'][:50]}: {e}[/red]")
            # Record the error for the guard — if we've hit 10 of the
            # same error_type in a row, this raises AbortedForRepeatingErrors.
            try:
                guard.record_error(
                    e, context={"run_id": run["id"], "run_name": run["run_name"]}
                )
            except AbortedForRepeatingErrors as abort_exc:
                console.print(f"\n[red bold]{abort_exc}[/red bold]")
                console.print(
                    "[yellow]Aborted early — see ~/STAN/alerts/ for details. "
                    "Fix the root cause and re-run.[/yellow]"
                )
                _log({"event": "aborted_for_repeating", "error_type": type(e).__name__,
                      "consecutive": guard._consecutive})
                aborted = True
                break
            sync.maybe_sync()
            continue

        guard.record_success()

        if drift.drift_class == "unknown":
            n_skip_unknown += 1
            _log({"event": "skip_unknown", "run_id": run["id"],
                  "run_name": run["run_name"]})
            sync.maybe_sync()
            continue

        update_drift_result(
            run_id=run["id"],
            drift_coverage=drift.global_coverage,
            drift_median_im=drift.median_drift_im,
            drift_p90_abs_im=drift.p90_abs_drift_im,
            drift_class=drift.drift_class,
        )
        # v0.2.147: also persist the per-window breakdown so the
        # dashboard drift-scatter chart has data for historical runs.
        try:
            insert_drift_window_centroids(
                run_id=run["id"], per_window=drift.per_window, table="runs",
            )
        except Exception as _e:
            _log({"event": "breakdown_error", "run_id": run["id"],
                  "error": str(_e), "error_type": type(_e).__name__})
        # v0.2.173: persist the m/z x 1/K0 cloud for the Bruker-
        # DataAnalysis-style visualization.
        try:
            if drift.cloud_mz:
                insert_drift_peak_cloud(
                    run_id=run["id"],
                    mz=drift.cloud_mz, im=drift.cloud_im,
                    log_intensity=drift.cloud_log_intensity,
                    table="runs",
                )
        except Exception as _e:
            _log({"event": "cloud_error", "run_id": run["id"],
                  "error": str(_e), "error_type": type(_e).__name__})
        n_updated += 1
        _log({
            "event": "updated",
            "run_id": run["id"], "run_name": run["run_name"],
            "coverage": drift.global_coverage,
            "median_drift_im": drift.median_drift_im,
            "p90_abs_drift_im": drift.p90_abs_drift_im,
            "drift_class": drift.drift_class,
            "n_windows": drift.n_windows,
        })
        tag = {"ok": "dim", "warn": "yellow bold",
               "drifted": "red bold"}.get(drift.drift_class, "")
        if verbose or drift.drift_class in ("warn", "drifted"):
            console.print(
                f"  [{tag}]{run['run_name'][:50]:<50} "
                f"cov={drift.global_coverage:.1%} "
                f"drift={drift.median_drift_im:+.3f} "
                f"p90={drift.p90_abs_drift_im:.3f} "
                f"class={drift.drift_class}[/{tag}]"
            )
        sync.maybe_sync()

    console.print()
    status = "ABORTED" if aborted else "complete"
    console.print(
        f"[bold]Drift backfill {status}[/bold] — "
        f"updated={n_updated} no_path={n_skip_no_path} "
        f"unknown={n_skip_unknown} errors={n_errors}"
    )
    _log({"event": "end", "updated": n_updated,
          "skipped_no_path": n_skip_no_path, "skipped_unknown": n_skip_unknown,
          "errors": n_errors})
    log_fh.close()
    try:
        sync_to_hive_mirror(include_reports=False)
    except Exception:
        pass
    console.print(f"[dim]Log: {log_path}[/dim]")


@app.command("fix-instrument-names")
def fix_instrument_names(
    from_name: str = typer.Option(
        ..., "--from",
        help="Current instrument value to replace (e.g. 'data_bruker').",
    ),
    to_name: str = typer.Option(
        ..., "--to",
        help="Canonical instrument value to rewrite it to (e.g. 'timsTOF HT').",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Preview the update without writing to the DB.",
    ),
) -> None:
    """Rewrite instrument column values in runs + sample_health.

    Brett 2026-04-23: dashboard shows two cards for the same physical
    timsTOF HT because historical rows were inserted with the model
    name from metadata ('timsTOF HT') while v0.2.159+ catchup used
    the instruments.yml config key ('data_bruker'). This CLI merges
    them into one canonical value.

    Always pass the MODEL name as --to (e.g. 'timsTOF HT',
    'Orbitrap Fusion Lumos', 'Orbitrap Exploris 480'). The model is
    what community benchmarks key off, so it's the right canonical
    value.
    """
    import sqlite3
    from stan.db import get_db_path, init_db

    init_db()
    db = get_db_path()
    if not db.exists():
        console.print(f"[red]DB not found: {db}[/red]")
        raise typer.Exit(1)

    with sqlite3.connect(str(db)) as con:
        n_runs = con.execute(
            "SELECT COUNT(*) FROM runs WHERE instrument = ?", (from_name,)
        ).fetchone()[0]
        n_sh = 0
        try:
            n_sh = con.execute(
                "SELECT COUNT(*) FROM sample_health WHERE instrument = ?",
                (from_name,),
            ).fetchone()[0]
        except sqlite3.OperationalError:
            pass
        conflict_runs = con.execute(
            "SELECT COUNT(*) FROM runs WHERE instrument = ?", (to_name,)
        ).fetchone()[0]

    console.print(f"[bold]Rewrite plan:[/bold]")
    console.print(f"  from: {from_name!r}")
    console.print(f"  to:   {to_name!r}")
    console.print(f"  runs with {from_name!r}:          {n_runs}")
    console.print(f"  sample_health with {from_name!r}: {n_sh}")
    console.print(f"  runs already on {to_name!r} (merge target): {conflict_runs}")
    console.print()

    if n_runs == 0 and n_sh == 0:
        console.print(f"[yellow]No rows to rewrite - nothing to do.[/yellow]")
        return

    if dry_run:
        console.print("[yellow]--dry-run: no DB writes.[/yellow]")
        return

    with sqlite3.connect(str(db)) as con:
        r1 = con.execute(
            "UPDATE runs SET instrument = ? WHERE instrument = ?",
            (to_name, from_name),
        )
        r2 = (0,)
        try:
            r2 = con.execute(
                "UPDATE sample_health SET instrument = ? WHERE instrument = ?",
                (to_name, from_name),
            )
        except sqlite3.OperationalError:
            pass
        con.commit()
        console.print(
            f"[green]Rewrote {r1.rowcount} runs + "
            f"{r2.rowcount if hasattr(r2,'rowcount') else 0} sample_health rows.[/green]"
        )
    console.print("Refresh the dashboard - the two cards should merge into one.")


@app.command("recover-search-outputs")
def recover_search_outputs(
    src: str = typer.Option(
        "", "--src",
        help="Source directory to sweep. Default: ~/Downloads",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would be moved/deleted without touching the filesystem.",
    ),
    delete_duplicates: bool = typer.Option(
        False, "--delete-duplicates",
        help="When a source folder matches a target that already exists, "
             "assume the target is canonical and DELETE the source copy. "
             "Default: skip duplicates (no cleanup).",
    ),
) -> None:
    """Move orphan DIA-NN / Sage search-output dirs into baseline_output.

    v0.2.170: Brett 2026-04-23 found ~40 run-stem directories in
    Downloads because update-stan.bat was clicked from there, the
    spawned backfill process inherited Downloads as CWD, and some
    STAN code path wrote relative output paths. This command sweeps
    a source directory for folders that contain ``report.parquet``
    or ``results.sage.parquet`` (confirming they're real search
    outputs) and moves them into ~/STAN/baseline_output/<stem>/.

    Run this once after upgrading to v0.2.170. The PS1 fix prevents
    future occurrences by spawning all processes with a stable
    CWD = ~/STAN.
    """
    import shutil
    from stan.config import get_user_config_dir

    src_dir = Path(src) if src else Path.home() / "Downloads"
    dest_dir = get_user_config_dir() / "baseline_output"

    if not src_dir.exists():
        console.print(f"[red]Source dir not found: {src_dir}[/red]")
        raise typer.Exit(code=1)

    dest_dir.mkdir(parents=True, exist_ok=True)

    moved = 0
    skipped_not_search = 0
    skipped_already_there = 0
    collisions = 0

    for entry in sorted(src_dir.iterdir()):
        if not entry.is_dir():
            continue
        # Must contain at least one search-output marker to be
        # considered a DIA-NN / Sage result dir.
        markers = (
            list(entry.glob("report.parquet"))
            + list(entry.glob("results.sage.parquet"))
            + list(entry.glob("*.stats.tsv"))
            + list(entry.glob("diann.log"))
            + list(entry.glob("sage.log"))
        )
        if not markers:
            skipped_not_search += 1
            continue

        target = dest_dir / entry.name
        if target.exists():
            # Already in baseline_output.
            if delete_duplicates:
                if dry_run:
                    console.print(
                        f"  [dim]would delete[/dim] Downloads/{entry.name} "
                        f"(duplicate of baseline_output copy)"
                    )
                    collisions += 1
                else:
                    try:
                        shutil.rmtree(str(entry))
                        console.print(
                            f"  [cyan]deleted[/cyan] duplicate: {entry.name}"
                        )
                        collisions += 1
                    except Exception as e:
                        console.print(
                            f"  [red]fail[/red] delete {entry.name}: {e}"
                        )
            else:
                collisions += 1
                console.print(
                    f"  [yellow]skip[/yellow] (already exists): {entry.name}"
                )
            continue

        if dry_run:
            console.print(f"  [dim]would move[/dim] {entry.name} -> baseline_output/")
            moved += 1
        else:
            try:
                shutil.move(str(entry), str(target))
                console.print(f"  [green]moved[/green] {entry.name}")
                moved += 1
            except Exception as e:
                console.print(f"  [red]fail[/red] {entry.name}: {e}")

    console.print()
    console.print(
        f"[bold]Summary:[/bold] moved {moved}, "
        f"already-in-dest {collisions}, "
        f"skipped (not search output) {skipped_not_search}"
    )
    if dry_run:
        console.print("[yellow]--dry-run: nothing was actually moved.[/yellow]")
    elif moved > 0:
        console.print()
        console.print("Now rerun these to ingest the moved runs:")
        console.print("  [cyan]stan backfill-metrics[/cyan]")
        console.print("  [cyan]stan backfill-cirt[/cyan]")
        console.print("  [cyan]stan backfill-tic --push[/cyan]")


@app.command("install-peg-deps")
def install_peg_deps() -> None:
    """Install or repair Bruker-only PEG + drift dependencies.

    Only useful on timsTOF instruments. Thermo instruments use
    fisher_py (installed separately by update_stan.ps1) for MS1
    spectrum access; alphatims is irrelevant on Orbitrap.

    Handles two compat breaks: alphatims 1.0.9 vs polars 1.35+,
    and alphatims 1.0.8 vs numpy 2.0+. Probes installed versions,
    force-downgrades whichever is broken. Safe to run multiple
    times - no-op when versions already satisfy the pin.
    """
    import subprocess
    import sys

    # Probe the installed version.
    installed_ver: str | None = None
    try:
        import alphatims  # noqa: F401
        try:
            from importlib.metadata import version as _pkg_version
            installed_ver = _pkg_version("alphatims")
        except Exception:
            installed_ver = getattr(alphatims, "__version__", None)
    except ImportError:
        installed_ver = None

    # v0.2.166: also probe numpy version - alphatims 1.0.8 breaks
    # against numpy 2.0+ strict searchsorted. Both need to be pinned.
    numpy_ver: str | None = None
    try:
        from importlib.metadata import version as _pkg_version
        numpy_ver = _pkg_version("numpy")
    except Exception:
        pass

    pin = "alphatims>=1.0,<1.0.9"
    numpy_bad = bool(numpy_ver and numpy_ver[0].isdigit()
                     and int(numpy_ver.split(".")[0]) >= 2)

    if installed_ver is None:
        console.print("alphatims not installed - installing alphatims<1.0.9 + numpy<2...")
        needs_install = True
    elif installed_ver.startswith("1.0.9"):
        console.print(
            f"[yellow]alphatims {installed_ver} is BROKEN against polars 1.35+ - "
            f"forcing downgrade to <1.0.9...[/yellow]"
        )
        needs_install = True
    elif numpy_bad:
        console.print(
            f"[yellow]numpy {numpy_ver} is 2.0+ - strict searchsorted side= "
            f"breaks alphatims {installed_ver}. Pinning numpy<2...[/yellow]"
        )
        needs_install = True
    elif any(installed_ver.startswith(v) for v in ("1.0.5", "1.0.6", "1.0.7", "1.0.8")):
        console.print(
            f"[green]alphatims {installed_ver} + numpy {numpy_ver} "
            f"already OK (pins satisfied).[/green]"
        )
        return
    else:
        console.print(
            f"alphatims {installed_ver} - unknown version, reinstalling pinned..."
        )
        needs_install = True

    # v0.2.166: also pin numpy<2. alphatims 1.0.8 uses
    # np.searchsorted with side values that numpy 2.0+ rejects as
    # strict "left"/"right" only. Brett timsTOF 2026-04-22: after
    # alphatims downgrade to 1.0.8, PEG backfill STILL failed because
    # numpy was 2.4.4. Pinning both solves the whole compat chain.
    console.print("Installing alphatims + numpy<2 with --force-reinstall...")
    cmd = [sys.executable, "-m", "pip", "install",
           "--force-reinstall", "--quiet", pin, "numpy<2"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        console.print("[red]pip install timed out after 10 min.[/red]")
        raise typer.Exit(1)

    if result.returncode != 0:
        console.print(f"[red]pip install failed (exit {result.returncode}):[/red]")
        console.print(result.stderr[-2000:] if result.stderr else "(no stderr)")
        raise typer.Exit(1)

    # Verify the downgrade landed.
    try:
        import importlib
        importlib.invalidate_caches()
        # Force a re-import so we see the new version.
        if "alphatims" in sys.modules:
            del sys.modules["alphatims"]
        importlib.import_module("alphatims")
        from importlib.metadata import version as _pkg_version
        new_ver = _pkg_version("alphatims")
        if new_ver.startswith("1.0.9"):
            console.print(
                f"[red]Still 1.0.9 after reinstall: {new_ver} - "
                f"pip may be using a cached wheel. Try manually: "
                f"pip install --no-cache-dir --force-reinstall '{pin}'[/red]"
            )
            raise typer.Exit(1)
        console.print(f"[green]alphatims {new_ver} installed and importable.[/green]")
        console.print("Now rerun: [bold]stan backfill-peg[/bold] and "
                      "[bold]stan backfill-window-drift[/bold]")
    except ImportError as e:
        console.print(f"[red]alphatims installed but import fails: {e}[/red]")
        raise typer.Exit(1)


@app.command("backfill-peg")
def backfill_peg(
    force: bool = typer.Option(
        False, "--force",
        help="Recompute PEG score even for rows that already have one.",
    ),
    instrument: str = typer.Option(
        "", "--instrument",
        help="Only backfill runs from this instrument (DB instrument string).",
    ),
    limit: int = typer.Option(
        0, "--limit",
        help="Stop after processing this many runs (0 = all).",
    ),
    n_scans: int = typer.Option(
        80, "--n-scans",
        help="MS1 scans to sample per file. Fewer = faster, less sensitive.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Log per-run details.",
    ),
) -> None:
    """Scan each run's raw file for PEG ions and populate peg_* columns.

    Iterates runs in the local DB, reads MS1 spectra from the raw file
    at `raw_path`, scans for the full PEG ion reference list at 5 ppm
    tolerance, and writes peg_score / peg_n_ions_detected /
    peg_intensity_pct / peg_class back to the row. Skips rows whose
    raw_path is missing from disk.

    Bruker (.d) files are supported in v0.2.139 via alphatims. Thermo
    (.raw) support lands separately. Runs with missing extras (e.g.
    alphatims not installed) are gracefully skipped with a clear
    message; the command never crashes the DB.

    A JSONL log is written to ~/.stan/logs/backfill_peg_<ts>.jsonl and
    synced to the Hive mirror so remote debuggers can see the full
    per-run breakdown.
    """
    import json as _json
    import sqlite3
    from datetime import datetime, timezone

    from stan.config import get_user_config_dir, sync_to_hive_mirror
    from stan.db import (
        get_db_path, init_db, update_peg_result,
        insert_peg_ion_hits,
    )

    init_db()
    db_path = get_db_path()

    # Import the algorithm first (pure Python, always available) so we
    # fail fast on import errors before even trying the IO layer.
    from stan.metrics.peg import (
        classify_peg_score,
        detect_peg_in_spectra,
    )
    # IO layer — may fail for vendors where the optional extra isn't
    # installed. We import here so the CLI still loads for --help even
    # without alphatims.
    from stan.metrics.peg_io import (
        PegReaderUnavailable,
        read_ms1_any,
        N_SCANS_DEFAULT,
    )

    _ = N_SCANS_DEFAULT  # imported for the help text reference

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        where = []
        params: list = []
        if instrument:
            where.append("instrument = ?")
            params.append(instrument)
        if not force:
            # Queue rows missing the summary score OR missing the per-ion
            # breakdown (v0.2.147+ added peg_ion_hits — runs that were
            # PEG-scanned before the upgrade have peg_score populated but
            # no breakdown, so we re-scan them on the first post-upgrade
            # sweep so the lollipop chart has data).
            where.append(
                "("
                " peg_score IS NULL "
                " OR id NOT IN (SELECT run_id FROM peg_ion_hits "
                "               WHERE source = 'runs')"
                ")"
            )
        sql = "SELECT id, run_name, instrument, raw_path, mode, peg_score FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY run_date DESC"
        if limit > 0:
            sql += f" LIMIT {limit}"
        rows = con.execute(sql, params).fetchall()

    console.print(f"[bold]{len(rows)} runs queued for PEG backfill[/bold]")
    if force:
        console.print("[yellow]--force: recomputing even rows that already have a score[/yellow]")

    # Diag log mirrors stan backfill-metrics for consistency.
    log_dir = get_user_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"backfill_peg_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    log_fh = open(log_path, "a", encoding="utf-8")

    def _log(record: dict) -> None:
        record["ts"] = datetime.now(timezone.utc).isoformat()
        log_fh.write(_json.dumps(record) + "\n")
        log_fh.flush()

    _log({"event": "start", "n_queued": len(rows), "n_scans": n_scans, "force": force})

    # v0.2.153: streaming telemetry for PEG backfill.
    from stan.backfill_telemetry import (
        AbortIfRepeating, AbortedForRepeatingErrors, PeriodicSync,
    )
    guard = AbortIfRepeating(run_label="backfill-peg")
    sync = PeriodicSync()

    n_updated = 0
    n_skipped_no_path = 0
    n_skipped_reader = 0
    n_errors = 0
    aborted = False

    for i, row in enumerate(rows, 1):
        run = dict(row)
        raw_path_str = run.get("raw_path") or ""
        if not raw_path_str:
            n_skipped_no_path += 1
            _log({"event": "skip", "run_id": run["id"], "run_name": run["run_name"],
                  "reason": "no raw_path"})
            continue
        raw_path = Path(raw_path_str)
        if not raw_path.exists():
            n_skipped_no_path += 1
            _log({"event": "skip", "run_id": run["id"], "run_name": run["run_name"],
                  "reason": "raw_path not on disk", "raw_path": raw_path_str})
            continue

        t0 = datetime.now()
        try:
            spectra = list(read_ms1_any(raw_path, n_scans=n_scans))
            result = detect_peg_in_spectra(spectra)
        except PegReaderUnavailable as e:
            n_skipped_reader += 1
            _log({"event": "skip", "run_id": run["id"], "run_name": run["run_name"],
                  "reason": "reader unavailable", "detail": str(e)})
            if n_skipped_reader <= 3:
                console.print(f"  [yellow]{run['run_name'][:50]}: {e}[/yellow]")
            continue
        except Exception as e:
            n_errors += 1
            _log({"event": "error", "run_id": run["id"], "run_name": run["run_name"],
                  "error": str(e), "error_type": type(e).__name__})
            if n_errors <= 3:
                console.print(f"  [red]{run['run_name'][:50]}: {e}[/red]")
            try:
                guard.record_error(
                    e, context={"run_id": run["id"], "run_name": run["run_name"]}
                )
            except AbortedForRepeatingErrors as abort_exc:
                console.print(f"\n[red bold]{abort_exc}[/red bold]")
                _log({"event": "aborted_for_repeating",
                      "error_type": type(e).__name__})
                aborted = True
                break
            sync.maybe_sync()
            continue

        guard.record_success()

        elapsed = (datetime.now() - t0).total_seconds()
        update_peg_result(
            run_id=run["id"],
            peg_score=result.peg_score,
            peg_n_ions_detected=result.n_ions_detected,
            peg_intensity_pct=result.intensity_pct,
            peg_class=result.peg_class,
        )
        # v0.2.147: persist per-ion breakdown for the dashboard
        # lollipop chart (dedup'd by insert_peg_ion_hits).
        try:
            insert_peg_ion_hits(
                run_id=run["id"], matches=result.matches, table="runs",
            )
        except Exception as _e:
            _log({"event": "breakdown_error", "run_id": run["id"],
                  "error": str(_e), "error_type": type(_e).__name__})
        n_updated += 1
        _log({
            "event": "updated",
            "run_id": run["id"], "run_name": run["run_name"],
            "peg_score": round(result.peg_score, 2),
            "peg_class": result.peg_class,
            "n_ions": result.n_ions_detected,
            "intensity_pct": round(result.intensity_pct, 3),
            "elapsed_sec": round(elapsed, 1),
        })

        tag = {"clean": "dim", "trace": "yellow", "moderate": "yellow bold",
               "heavy": "red bold"}.get(result.peg_class, "")
        msg = (f"  [{tag}]{run['run_name'][:45]:<45} "
               f"score={result.peg_score:>5.1f} {result.peg_class:<8} "
               f"n_ions={result.n_ions_detected:>3} "
               f"int_pct={result.intensity_pct:>5.2f} ({elapsed:.0f}s)[/{tag}]")
        if verbose or result.peg_class in ("moderate", "heavy"):
            console.print(msg)
        if i % 10 == 0 and not verbose:
            console.print(f"  [dim]{i}/{len(rows)} - updated={n_updated} "
                          f"skipped={n_skipped_no_path + n_skipped_reader} "
                          f"errors={n_errors}[/dim]")
        sync.maybe_sync()

    console.print()
    status = "ABORTED" if aborted else "complete"
    console.print(
        f"[bold]PEG backfill {status}[/bold] - "
        f"updated={n_updated} skipped_no_path={n_skipped_no_path} "
        f"skipped_reader={n_skipped_reader} errors={n_errors}"
    )

    _log({"event": "end", "n_updated": n_updated,
          "n_skipped_no_path": n_skipped_no_path,
          "n_skipped_reader": n_skipped_reader, "n_errors": n_errors})
    log_fh.close()

    try:
        sync_to_hive_mirror(include_reports=False)
    except Exception:
        pass
    console.print(f"[dim]Log: {log_path}[/dim]")


@app.command("backfill-cirt")
def backfill_cirt(
    verbose: bool = typer.Option(
        False, "--verbose", help="Log per-run extraction details.",
    ),
) -> None:
    """Extract cIRT anchor retention times for every run with a report.parquet.

    Reads the panel seeded in stan/metrics/cirt.py keyed on (instrument_family,
    spd), finds each run's report.parquet under ~/.stan/baseline_output/<run_name>/,
    extracts the observed RT for each anchor peptide, and writes rows to the
    `irt_anchor_rts` table. Runs are skipped when: there's no panel for their
    (family, spd), their report.parquet is missing, or they're non-DIA (only
    DIA reports have DIA-NN RT columns).

    Safe to re-run; INSERT OR REPLACE on the composite PK overwrites existing
    rows for the same (run_id, peptide).
    """
    import sqlite3

    from stan.config import get_user_config_dir
    from stan.db import get_db_path, init_db, insert_irt_anchor_rts
    from stan.metrics.cirt import extract_anchor_rts, get_panel
    from stan.community.submit import _instrument_family

    init_db()
    db_path = get_db_path()
    output_base = get_user_config_dir() / "baseline_output"

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, run_name, instrument, mode, spd FROM runs"
        ).fetchall()

    console.print(f"[bold]{len(rows)} runs in DB[/bold]")

    processed = 0
    no_panel = 0
    no_report = 0
    non_dia = 0
    no_anchors = 0
    total_anchors = 0

    for row in rows:
        run = dict(row)
        # Match any DIA flavor: "DIA" (Thermo), "diaPASEF" (Bruker),
        # "dia_foo" (hypothetical). The original exact-equality check
        # skipped every Bruker run because "diaPASEF" != "dia".
        if not (run.get("mode") or "").lower().startswith("dia"):
            non_dia += 1
            continue
        family = _instrument_family(run.get("instrument") or "")
        spd = run.get("spd")
        panel = get_panel(family, spd)
        if not panel:
            no_panel += 1
            if verbose:
                console.print(f"  [dim]no panel for {family}/{spd}: {run['run_name']}[/dim]")
            continue
        # Report dir name drops the .d / .raw extension
        stem = Path(run["run_name"]).stem
        report = output_base / stem / "report.parquet"
        if not report.exists():
            no_report += 1
            if verbose:
                console.print(f"  [dim]no report: {run['run_name']}[/dim]")
            continue
        observed = extract_anchor_rts(report, panel)
        if not observed:
            no_anchors += 1
            if verbose:
                console.print(f"  [yellow]no anchors detected: {run['run_name']}[/yellow]")
            continue
        n = insert_irt_anchor_rts(run["id"], observed, panel, db_path=db_path)
        total_anchors += n
        processed += 1
        if verbose:
            console.print(f"  [green]{run['run_name']}: {n}/{len(panel)} anchors[/green]")

    console.print()
    console.print(f"[bold]Extracted cIRT anchors from {processed} runs[/bold] "
                  f"({total_anchors} anchor-RT rows written)")
    console.print(f"  Skipped: {non_dia} non-DIA, {no_panel} no-panel, "
                  f"{no_report} no-report, {no_anchors} no-anchors-detected")


@app.command("derive-cirt-panel")
def derive_cirt_panel(
    instrument_family: str = typer.Option(
        ..., "--family", help="timsTOF | Astral | Exploris | Lumos | Eclipse | Orbitrap",
    ),
    spd: int = typer.Option(..., "--spd", help="Samples per day."),
    min_precursors: int = typer.Option(
        10000, "--min-precursors",
        help="Minimum n_precursors to consider a run 'good'.",
    ),
    n_anchors: int = typer.Option(
        10, "--n-anchors", help="Target panel size.",
    ),
) -> None:
    """Print an empirical cIRT panel for an (instrument_family, spd) cohort.

    Scans every run in the local DB matching the filters, loads each
    report.parquet from ~/.stan/baseline_output, and runs the empirical
    selection algorithm. Output is pasteable into
    stan/metrics/cirt.py::EMPIRICAL_CIRT_PANELS.
    """
    import sqlite3

    from stan.config import get_user_config_dir
    from stan.db import get_db_path, init_db
    from stan.metrics.cirt import derive_panel_from_cohort
    from stan.community.submit import _instrument_family

    init_db()
    db_path = get_db_path()
    output_base = get_user_config_dir() / "baseline_output"

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT run_name, instrument, spd, n_precursors FROM runs "
            "WHERE spd = ? AND n_precursors > ? AND mode = 'DIA'",
            (spd, min_precursors),
        ).fetchall()

    cohort_reports: list[Path] = []
    for row in rows:
        if _instrument_family(row["instrument"] or "") != instrument_family:
            continue
        stem = Path(row["run_name"]).stem
        report = output_base / stem / "report.parquet"
        if report.exists():
            cohort_reports.append(report)

    console.print(f"[bold]Cohort: {len(cohort_reports)} reports[/bold] "
                  f"({instrument_family}, SPD={spd}, >{min_precursors} precursors)")
    if len(cohort_reports) < 5:
        console.print("[yellow]Not enough runs — need at least 5.[/yellow]")
        return

    panel = derive_panel_from_cohort(cohort_reports, n_anchors=n_anchors)
    if not panel:
        console.print("[yellow]No stable anchors found with current thresholds.[/yellow]")
        return

    console.print()
    console.print(f'    ("{instrument_family}", {spd}): [')
    for seq, rt in panel:
        console.print(f'        ({seq!r:<22}, {rt:>6.2f}),')
    console.print('    ],')


@app.command("submit-all")
def submit_all(
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Show what would be submitted without actually POSTing.",
    ),
) -> None:
    """Submit all un-submitted QC runs to the community benchmark.

    Walks the local runs table, finds rows where submitted_to_benchmark=0
    and the run looks like a valid QC file, and calls submit_to_benchmark()
    for each. Skips blanks, test files, and runs that fail validation.

    Use after stan backfill-metrics to ensure metrics are populated
    before submission.
    """
    import json as _json
    import sqlite3
    from datetime import datetime, timezone

    from stan.config import get_user_config_dir, load_community, sync_to_hive_mirror
    from stan.db import get_db_path, init_db

    init_db()
    db_path = get_db_path()

    try:
        comm = load_community()
    except Exception:
        comm = {}

    if not comm.get("community_submit"):
        console.print("[yellow]community_submit is not enabled in community.yml[/yellow]")
        console.print("[dim]Run stan setup to enable community submissions.[/dim]")
        return

    # Set up a JSONL log at ~/.stan/logs/submit_all_{YYYYMMDD}.jsonl
    # One record per run (submitted / skipped / failed). Synced to the
    # Hive mirror at the end so Brett can read it from Quobyte without
    # SSHing into the instrument PC.
    log_dir = get_user_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"submit_all_{datetime.now().strftime('%Y%m%d')}.jsonl"
    log_fh = open(log_path, "a", encoding="utf-8")

    def _log(record: dict) -> None:
        record["ts"] = datetime.now(timezone.utc).isoformat()
        log_fh.write(_json.dumps(record) + "\n")
        log_fh.flush()

    _log({
        "event": "start",
        "stan_version": __version__,
        "dry_run": dry_run,
    })

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        candidates = con.execute(
            "SELECT * FROM runs WHERE submitted_to_benchmark = 0 "
            "OR submitted_to_benchmark IS NULL "
            "ORDER BY run_date ASC"
        ).fetchall()

    console.print(f"[bold]{len(candidates)} un-submitted runs found[/bold]")
    _log({"event": "candidates", "count": len(candidates)})

    from stan.community.submit import submit_to_benchmark
    from stan.watcher.qc_filter import compile_qc_pattern, is_qc_file
    from pathlib import Path

    qc_pat = compile_qc_pattern()
    submitted = 0
    skipped = 0
    failed = 0

    for row in candidates:
        run = dict(row)
        name = run.get("run_name", "")
        run_id = run.get("id")

        # Skip non-QC files
        if not is_qc_file(Path(name), qc_pat):
            skipped += 1
            _log({"event": "skip", "run_id": run_id, "run_name": name, "reason": "non_qc"})
            continue

        # Skip blanks/washes
        import re
        if re.search(r"(?i)(wash|blank|blnk|blk|DELETE)", name):
            skipped += 1
            _log({"event": "skip", "run_id": run_id, "run_name": name, "reason": "blank_or_wash"})
            continue

        # Skip runs with zero IDs
        n_prec = run.get("n_precursors") or 0
        n_psms = run.get("n_psms") or 0
        if n_prec == 0 and n_psms == 0:
            skipped += 1
            _log({"event": "skip", "run_id": run_id, "run_name": name, "reason": "zero_ids"})
            continue

        if dry_run:
            if submitted < 10:
                console.print(f"  [dim]Would submit: {name[:60]}[/dim]")
            submitted += 1
            _log({"event": "would_submit", "run_id": run_id, "run_name": name})
            continue

        try:
            result = submit_to_benchmark(
                run,
                spd=run.get("spd"),
                gradient_length_min=run.get("gradient_length_min"),
                amount_ng=run.get("amount_ng") or 50.0,
                diann_version=run.get("diann_version"),
            )
            sid = result.get("submission_id", "")
            # Mark as submitted in local DB
            with sqlite3.connect(str(db_path)) as con:
                con.execute(
                    "UPDATE runs SET submitted_to_benchmark = 1, "
                    "submission_id = ? WHERE id = ?",
                    (sid, run["id"]),
                )
            submitted += 1
            _log({
                "event": "submitted",
                "run_id": run_id,
                "run_name": name,
                "submission_id": sid,
                "cohort_id": result.get("cohort_id", ""),
                "is_flagged": result.get("is_flagged", False),
                "flags": result.get("flags", []),
            })
            if submitted % 10 == 0:
                console.print(
                    f"  [dim]{submitted} submitted, {skipped} skipped, "
                    f"{failed} failed[/dim]"
                )
        except ValueError as e:
            # Validation rejection (version mismatch, hard gates, etc.)
            if failed < 5:
                console.print(f"  [yellow]{name[:45]}: {e}[/yellow]")
            failed += 1
            _log({
                "event": "rejected",
                "run_id": run_id,
                "run_name": name,
                "error": str(e),
                "error_type": "ValueError",
            })
        except Exception as e:
            if failed < 5:
                console.print(f"  [red]{name[:45]}: {e}[/red]")
            failed += 1
            _log({
                "event": "failed",
                "run_id": run_id,
                "run_name": name,
                "error": str(e),
                "error_type": type(e).__name__,
            })

    action = "Would submit" if dry_run else "Submitted"
    console.print()
    console.print(
        f"[bold]{action} {submitted} runs[/bold] "
        f"(skipped {skipped} non-QC/blank/empty, "
        f"failed {failed} validation)"
    )

    _log({
        "event": "end",
        "submitted": submitted,
        "skipped": skipped,
        "failed": failed,
    })
    log_fh.close()

    # Mirror the log to Hive so it's readable from /Volumes/proteomics-grp/STAN
    try:
        sync_to_hive_mirror(include_reports=False)
    except Exception:
        pass
    console.print(f"[dim]Log: {log_path}[/dim]")


@app.command("watch-status")
def watch_status(
    days: int = typer.Option(
        14, "--days", help="Look at files acquired in the last N days.",
    ),
    to_log: bool = typer.Option(
        True, "--to-log/--no-log",
        help="Also write the report to ~/.stan/logs/ so it syncs to the "
             "Hive mirror. Useful when diagnosing remotely.",
    ),
) -> None:
    """Diagnose why recent acquisitions aren't showing up in STAN.

    For each instrument in instruments.yml, list raw files in its
    watch_dir acquired in the last N days and show for each file:
    (a) whether the QC filter matched it,
    (b) whether it's in the `runs` table,
    (c) whether it's in `sample_health`,
    (d) the file's mtime.

    When a file is physically on disk but not in either table, the
    watcher missed it. When it matched the QC pattern but isn't in
    `runs`, the search failed or never triggered. Everything you
    need to tell the difference between "operator saved the QC to
    the wrong folder" and "the daemon is broken".
    """
    import sqlite3
    import re
    from datetime import datetime, timedelta, timezone

    from stan.config import get_user_config_dir, load_instruments, sync_to_hive_mirror
    from stan.db import get_db_path, init_db
    from stan.watcher.qc_filter import compile_qc_pattern

    init_db()
    db_path = get_db_path()

    try:
        _hive, instruments = load_instruments()
    except FileNotFoundError:
        console.print("[red]instruments.yml not found — run 'stan init'.[/red]")
        raise typer.Exit(1)

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_ts = cutoff_dt.timestamp()

    # Read DB tables once — avoid per-file SQL.
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        runs_by_name = {
            r["run_name"]: r["gate_result"]
            for r in con.execute("SELECT run_name, gate_result FROM runs").fetchall()
        }
        try:
            sh_by_name = {
                r["run_name"]: r["verdict"]
                for r in con.execute(
                    "SELECT run_name, verdict FROM sample_health"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            sh_by_name = {}

    # Build a plain-text report alongside the console output so we can
    # persist it to ~/.stan/logs/.
    lines: list[str] = []

    def log_line(s: str = "") -> None:
        lines.append(s)

    log_line(f"stan watch-status  ·  last {days} days  ·  "
             f"cutoff {cutoff_dt.isoformat(timespec='seconds')}")
    log_line(f"DB: {db_path}")
    log_line(f"runs rows: {len(runs_by_name)}   sample_health rows: {len(sh_by_name)}")
    log_line("")

    console.print(f"[bold]stan watch-status[/bold]  ·  last {days} days")
    console.print(f"[dim]DB {db_path}[/dim]")
    console.print()

    for inst in instruments:
        if not inst.get("enabled", True):
            continue
        name = inst.get("name", "<unnamed>")
        watch_dir = Path(inst.get("watch_dir", ""))
        exts = {e.lower() for e in inst.get("extensions", [".d", ".raw"])}
        pattern = compile_qc_pattern(inst.get("qc_pattern"))
        exclude_raw = inst.get("exclude_pattern")
        exclude = re.compile(exclude_raw) if exclude_raw else None

        header = f"[bold cyan]{name}[/bold cyan]  {watch_dir}  (exts={','.join(sorted(exts))})"
        console.print(header)
        log_line(f"== {name}  watch_dir={watch_dir}  exts={sorted(exts)} ==")

        if not watch_dir.exists():
            msg = f"  [red]watch_dir does not exist[/red]"
            console.print(msg)
            log_line(f"  WATCH_DIR MISSING: {watch_dir}")
            continue

        # Gather recent raw files. The daemon watches recursively
        # (daemon.py:266 uses recursive=True), so we must recurse here
        # too or we'll miss every file in a subdirectory and falsely
        # report "empty". Skip descending INTO Bruker .d directories —
        # they're raw files themselves, not folders of raw files.
        recent: list[tuple[Path, float]] = []
        try:
            for p in watch_dir.rglob("*"):
                # Skip anything inside a .d (its own contents: analysis.tdf, etc.)
                if any(parent.suffix == ".d" for parent in p.parents):
                    continue
                is_d_dir = p.is_dir() and p.suffix == ".d"
                is_file = p.is_file() and p.suffix.lower() in exts and p.suffix.lower() != ".d"
                if not (is_d_dir or is_file):
                    continue
                if p.suffix.lower() not in exts:
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff_ts:
                    recent.append((p, mtime))
        except PermissionError:
            console.print(f"  [red]permission denied reading {watch_dir}[/red]")
            log_line(f"  PERMISSION DENIED: {watch_dir}")
            continue

        if not recent:
            console.print(f"  [dim]no {sorted(exts)} files in last {days} days[/dim]")
            log_line(f"  empty ({days}d window)")
            log_line("")
            continue

        recent.sort(key=lambda t: t[1], reverse=True)

        # Tally
        n_qc_match = 0
        n_excluded = 0
        n_in_runs = 0
        n_in_sh = 0
        n_orphan = 0  # on disk, not in either table

        console.print(f"  [dim]{len(recent)} files in last {days} days[/dim]")
        log_line(f"  {len(recent)} files in window:")

        # Table header
        hdr = f"    {'mtime':<19}  {'QC':<3}  {'runs':<4}  {'SH':<3}  file"
        console.print(f"[dim]{hdr}[/dim]")
        log_line(hdr)

        for path, mtime in recent:
            name_only = path.name
            stem = path.stem
            # Relative path so the operator can see which subdir the
            # file lives in. If it's top-level, rel_path == name_only.
            try:
                rel_path = str(path.relative_to(watch_dir))
            except ValueError:
                rel_path = str(path)
            qc_hit = bool(pattern.search(stem))
            if qc_hit:
                n_qc_match += 1
            if exclude and exclude.search(stem):
                n_excluded += 1
            in_runs = name_only in runs_by_name
            in_sh = name_only in sh_by_name
            if in_runs:
                n_in_runs += 1
            if in_sh:
                n_in_sh += 1
            if not in_runs and not in_sh:
                n_orphan += 1
            mt_str = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            qc_mark = "y" if qc_hit else "-"
            runs_mark = runs_by_name.get(name_only, "-")[:4] if in_runs else "-"
            sh_mark = sh_by_name.get(name_only, "-")[:3] if in_sh else "-"
            row = f"    {mt_str}  {qc_mark:<3}  {runs_mark:<4}  {sh_mark:<3}  {rel_path}"
            # Color orphans red for immediate visibility
            if not in_runs and not in_sh:
                console.print(f"[red]{row}[/red]")
            else:
                console.print(row)
            log_line(row)

        summary = (
            f"  summary: qc_match={n_qc_match}  excluded_by_pattern={n_excluded}  "
            f"in_runs={n_in_runs}  in_sample_health={n_in_sh}  orphans={n_orphan}"
        )
        console.print()
        console.print(f"[bold]{summary}[/bold]")
        log_line(summary)
        if n_orphan:
            diag = (
                f"  [yellow]{n_orphan} file(s) on disk but in neither table — "
                f"watcher missed them[/yellow]"
            )
            console.print(diag)
            log_line(f"  DIAG: {n_orphan} orphans (watcher missed)")
        log_line("")
        console.print()

    if to_log:
        try:
            log_dir = get_user_config_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = log_dir / f"watch_status_{ts}.log"
            log_path.write_text("\n".join(lines), encoding="utf-8")
            console.print(f"[dim]Report: {log_path}[/dim]")
            try:
                sync_to_hive_mirror(include_reports=False)
            except Exception:
                pass
        except Exception:
            logger.exception("Could not write watch-status log")


@app.command()
def status() -> None:
    """Show current STAN configuration and database status."""
    from stan.config import resolve_config_path
    from stan.db import get_db_path, get_runs

    console.print(f"[bold]STAN v{__version__}[/bold]")
    console.print()

    # Config
    try:
        config_path = resolve_config_path("instruments.yml")
        console.print(f"  Config: {config_path}")
    except FileNotFoundError:
        console.print("  Config: [red]not found[/red] — run: stan init")
        return

    # Database
    db_path = get_db_path()
    if db_path.exists():
        runs = get_runs(limit=1)
        total_query = "SELECT COUNT(*) FROM runs"
        import sqlite3
        with sqlite3.connect(str(db_path)) as con:
            total = con.execute(total_query).fetchone()[0]
        console.print(f"  Database: {db_path} ({total} runs)")
        if runs:
            last = runs[0]
            console.print(f"  Last run: {last['run_name']} ({last['instrument']}, {last['gate_result']})")
    else:
        console.print(f"  Database: {db_path} [yellow](not created yet)[/yellow]")


# ── Remote-control helpers (stan.control) ───────────────────────────────

def _mirror_root() -> Path | None:
    """Resolve the shared `Y:\\STAN\\` (or equivalent) root, not the
    per-host subdir. Returns None if no mirror is reachable."""
    from stan.config import get_hive_mirror_root
    return get_hive_mirror_root()


@app.command("send-command")
def send_command(
    action: str = typer.Argument(..., help="Whitelisted action name: ping, status, tail_log, export_db_snapshot"),
    host: str = typer.Option("", "--host", "-h", help="Target hostname (subdir of the mirror root). Omit to target this machine."),
    arg: list[str] = typer.Option([], "--arg", "-a", help="Action arguments as key=value (repeatable)"),
    wait: bool = typer.Option(False, "--wait", help="Block until the result file appears."),
    timeout: int = typer.Option(120, "--timeout", help="Seconds to wait for a result when --wait is set."),
) -> None:
    """Drop a command file into an instrument's control queue on the shared mirror.

    Examples:
      stan send-command status --host lumosRox --wait
      stan send-command tail_log --host lumosRox --arg name=baseline --arg n=50 --wait
      stan send-command export_db_snapshot --host TIMS-10878
    """
    import time
    from stan.control import enqueue_command

    # Parse --arg key=value repeats
    args_dict: dict = {}
    for a in arg:
        if "=" not in a:
            console.print(f"[red]--arg must be key=value, got {a!r}[/red]")
            raise typer.Exit(2)
        k, v = a.split("=", 1)
        # Best-effort int coercion
        if v.lstrip("-").isdigit():
            args_dict[k] = int(v)
        else:
            args_dict[k] = v

    if host:
        root = _mirror_root()
        if root is None:
            console.print("[red]No hive mirror mounted on this machine.[/red]")
            raise typer.Exit(1)
        target = root / host
        if not target.exists():
            console.print(f"[red]No such host directory under the mirror: {target}[/red]")
            raise typer.Exit(1)
        cmd_file = enqueue_command(action, args_dict, mirror_dir=target)
    else:
        cmd_file = enqueue_command(action, args_dict)

    console.print(f"Queued {action!r} → {cmd_file}")
    if not wait:
        return

    cmd_id = cmd_file.stem
    results_dir = cmd_file.parent.parent / "results"
    result_path = results_dir / f"{cmd_id}.result.json"

    console.print(f"Waiting up to {timeout}s for result...")
    start = time.time()
    while time.time() - start < timeout:
        if result_path.exists():
            import json
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            console.print_json(data=payload)
            return
        time.sleep(2)
    console.print(f"[yellow]Timeout — no result after {timeout}s.[/yellow]")
    raise typer.Exit(2)


@app.command("fleet-status")
def fleet_status(
    stale_min: int = typer.Option(30, "--stale-min", help="Flag hosts whose heartbeat is older than this many minutes."),
) -> None:
    """Aggregate status.json across every host directory on the shared mirror.

    Reads `<mirror>/<host>/status.json` — written periodically by each
    running `stan watch` daemon — and prints a one-line summary per host.
    Useful from a central Mac/laptop that mounts the same share as all
    the instrument PCs.
    """
    import json
    from datetime import datetime, timezone
    from rich.table import Table

    root = _mirror_root()
    if root is None:
        console.print("[red]No hive mirror mounted on this machine.[/red]")
        raise typer.Exit(1)

    hosts = sorted(p for p in root.iterdir() if p.is_dir())
    if not hosts:
        console.print(f"[yellow]No host directories under {root}[/yellow]")
        return

    now = datetime.now(timezone.utc)
    table = Table(title=f"STAN fleet status — {root}")
    table.add_column("Host")
    table.add_column("Heartbeat")
    table.add_column("Version")
    table.add_column("Runs", justify="right")
    table.add_column("Last run")
    table.add_column("Gate")

    for h in hosts:
        status_file = h / "status.json"
        if not status_file.exists():
            table.add_row(h.name, "[dim]no status.json[/dim]", "-", "-", "-", "-")
            continue
        try:
            payload = json.loads(status_file.read_text(encoding="utf-8"))
        except Exception as e:
            table.add_row(h.name, f"[red]parse error: {e}[/red]", "-", "-", "-", "-")
            continue

        # Heartbeat age
        try:
            ts = datetime.fromisoformat(payload.get("timestamp", "").replace("Z", "+00:00"))
            age_min = (now - ts).total_seconds() / 60
            if age_min < 1:
                hb = f"{int(age_min * 60)}s ago"
            elif age_min < 60:
                hb = f"{age_min:.0f}m ago"
            else:
                hb = f"{age_min / 60:.1f}h ago"
            if age_min > stale_min:
                hb = f"[yellow]{hb}[/yellow]"
        except Exception:
            hb = "[red]bad timestamp[/red]"

        last = payload.get("last_run") or {}
        table.add_row(
            h.name,
            hb,
            str(payload.get("stan_version", "?")),
            str(payload.get("n_runs", "?")),
            last.get("run_name", "-"),
            last.get("gate_result", "-"),
        )

    console.print(table)


@app.command("poll-commands")
def poll_commands_cmd() -> None:
    """Run one pass of the control-queue poller and exit. (Normally
    `stan watch` polls every 30s automatically — this is for testing.)"""
    from stan.control import poll_once

    n = poll_once()
    console.print(f"Processed {n} command(s).")
