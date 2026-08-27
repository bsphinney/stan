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
    _polars_ver = pkg_version("polars")
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

    emit("[bold]ThermoRawFileParser (TRFP)[/bold]")
    emit("-" * 70)
    # TRFP mode-detect failures cascade catastrophically — when the
    # watcher can't read a Thermo .raw, it routes the file to monitor
    # mode (no search) instead of qc mode (search + DB write). Result
    # is the runs table silently stops growing while sync_to_hive_mirror
    # keeps reporting "alive". Lumos lost ~3 weeks of new ingests this
    # way (Apr 11 → May 5). Diagnose at every doctor run so the failure
    # surfaces fast.
    try:
        from stan.tools.trfp import _tools_dir, _variant
        trfp_dir = _tools_dir()
        emit(f"  install dir:      {trfp_dir}")
        if not trfp_dir.exists():
            emit("  [yellow]install dir missing — TRFP not installed yet[/yellow]")
        else:
            try:
                v = _variant()
                exe_name = v.get("exe", "ThermoRawFileParser.exe")
                exe = trfp_dir / exe_name
                emit(f"  binary:           {exe}")
                if exe.exists():
                    emit(f"  binary size:      {exe.stat().st_size} bytes")
                    # Probe --help so we catch broken installs before
                    # the watcher ships them an actual .raw and silently
                    # downgrades to monitor mode.
                    import subprocess as _sp
                    try:
                        if exe_name.lower().endswith(".dll"):
                            cmd = ["dotnet", str(exe), "--help"]
                        else:
                            cmd = [str(exe), "--help"]
                        r = _sp.run(cmd, capture_output=True, text=True, timeout=15)
                        emit(f"  --help exit code: {r.returncode}")
                        head = (r.stdout or r.stderr or "").splitlines()[:3]
                        for ln in head:
                            emit(f"    {ln}")
                        if r.returncode != 0:
                            emit("  [red]TRFP --help failed — binary broken or AV-quarantined.[/red]")
                            emit("  [red]Fix: delete the install dir above and restart stan watch[/red]")
                            emit("  [red](it auto-redownloads on next run).[/red]")
                    except FileNotFoundError as _e:
                        emit(f"  [red]launcher not found: {_e}[/red]")
                    except Exception as _e:
                        emit(f"  [red]--help probe failed: {type(_e).__name__}: {_e}[/red]")
                else:
                    emit("  [yellow]binary missing — re-run stan watch to auto-install[/yellow]")
            except Exception as _e:
                emit(f"  [yellow]variant probe failed: {_e}[/yellow]")
    except Exception as _e:
        emit(f"  [yellow]TRFP module import failed: {_e}[/yellow]")
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
def init(
    reconfigure_fleet: bool = typer.Option(
        False, "--reconfigure-fleet",
        help="Re-run only the fleet-sync wizard. Skip the config-file copy.",
    ),
) -> None:
    """Initialize STAN config directory (~/.stan/).

    Copies default config files from the package. Does not overwrite existing
    files. Also walks the operator through the fleet-sync wizard so godmode
    knows where to read this instrument's mirrored QC data.

    Use ``--reconfigure-fleet`` to update only the fleet config later
    (e.g. when the network drive path changes or the lab joins the
    HF-Space relay).
    """
    from stan.fleet_setup import run_fleet_wizard

    if reconfigure_fleet:
        run_fleet_wizard(force=True)
        return

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

    # v0.2.216: prompt once for the fleet-sync root so godmode knows
    # where to read this lab's mirrored QC. Skips silently if a
    # fleet.yml already exists with a non-"none" mode.
    run_fleet_wizard(force=False)

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
        console.print("  1. Open Claude (or ChatGPT / Gemini) in your browser")
        console.print(f"  2. Drag [cyan]{path}[/cyan] into the chat")
        console.print("  3. Say: [italic]\"Please analyze my STAN QC data\"[/italic]")
        console.print("  4. Claude will read the prompt and produce a full report with figures")


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
    console.print("[bold]Import complete:[/bold]")
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
    console.print("[green]Added watch directory:[/green]")
    console.print(f"  Name:   {name}")
    console.print(f"  Vendor: {vendor}")
    console.print(f"  Path:   {abs_path}")
    if qc_only_cfg:
        pat_label = qc_pattern_cfg if qc_pattern_cfg else "default HeLa/QC pattern"
        console.print(f"  Filter: [cyan]{pat_label}[/cyan]")
    else:
        console.print("  Filter: [cyan]none (processing all files)[/cyan]")
    console.print()
    console.print(f"[dim]Config written to {config_path}[/dim]")
    console.print("[dim]The watcher daemon picks up changes automatically (hot-reload).[/dim]")


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


@app.command("sync-raw-now")
def sync_raw_now(path: str) -> None:
    """Push one .d or .raw to the Hive SMB mirror.

    Use for ad-hoc copies. For batch backlog use `sync-raw-backlog`.
    """
    from pathlib import Path as _P

    from stan.sync.raw import sync_raw_file_to_hive

    result = sync_raw_file_to_hive(_P(path))
    status = result.get("status", "?")
    if status == "synced":
        mb = (result.get("size_bytes") or 0) / 1e6
        elapsed = result.get("elapsed_s") or 0.0
        console.print(
            f"[green]Synced[/green] {path} ({mb:.1f} MB in {elapsed:.1f}s) -> "
            f"{result.get('dest')}"
        )
    elif status == "skipped":
        console.print(f"[yellow]Already on Hive[/yellow]: {path}")
    elif status == "no_mirror":
        console.print("[red]No Hive mirror configured for this host.[/red]")
    else:
        console.print(f"[red]Failed[/red]: {result.get('error', 'unknown')}")


@app.command("sync-raw-backlog")
def sync_raw_backlog(
    limit: int = 0,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    """Walk every watched dir in instruments.yml and sync raw QC files.

    --limit N    cap at N files (smoke test = 10)
    --dry-run    enumerate only
    --force      re-sync even if manifest says already synced
    """
    from pathlib import Path as _P

    from stan.config import load_instruments
    from stan.sync.raw import sync_raw_backlog as _sync_backlog

    _, instruments = load_instruments()
    watched: list[_P] = []
    for inst in instruments:
        wd = inst.get("watch_dir") or inst.get("path")
        if wd:
            watched.append(_P(wd))
    if not watched:
        console.print("[red]No watched dirs in instruments.yml.[/red]")
        return

    cap = limit if limit > 0 else None
    console.print(f"Scanning {len(watched)} watched dir(s)... (limit={cap})")
    results = _sync_backlog(
        watched_dirs=watched,
        limit=cap,
        dry_run=dry_run,
    )

    n = len(results)
    n_synced = sum(1 for r in results if r.get("status") == "synced")
    n_skipped = sum(1 for r in results if r.get("status") == "skipped")
    n_failed = sum(1 for r in results if r.get("status") == "failed")
    n_dry = sum(1 for r in results if r.get("status") == "dry_run")
    total_mb = sum(int(r.get("size_bytes") or 0) for r in results) / 1e6

    console.print(
        f"Done: {n} candidates ({total_mb:.0f} MB), "
        f"synced={n_synced}, skipped={n_skipped}, failed={n_failed}, dry={n_dry}"
    )


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
        # v0.2.309: rows that have TIC but no BPC. The bp_intensity
        # column landed in v0.2.300 — pre-migration rows have a TIC
        # blob but bp_intensity NULL forever unless we re-extract.
        # Without this set, the dashboard's TIC | BPC toggle stays
        # hidden on every existing instrument until force=True is
        # invoked manually. Bruker rows pick up bp_intensity for
        # free from MaxIntensity in extract_tic_bruker; Thermo rows
        # don't have BPC plumbing yet so re-extracting them is
        # benign (the column stays NULL, no harm done).
        try:
            have_bpc = {
                row[0] for row in con.execute(
                    "SELECT DISTINCT run_id FROM tic_traces "
                    "WHERE bp_intensity IS NOT NULL"
                ).fetchall()
            }
        except sqlite3.OperationalError:
            # Pre-v0.2.300 schema without bp_intensity column —
            # treat every TIC row as missing BPC.
            have_bpc = set()

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
    # v0.2.309: rows with TIC but no BPC, on Bruker only (Thermo
    # extract_tic_thermo doesn't populate bp_intensity).
    missing_bpc = [r for r in all_runs
                   if r["id"] in have_tic
                   and r["id"] not in have_bpc
                   and (r.get("raw_path") or "").lower().endswith(".d")]

    missing = missing_tic + missing_pep + missing_bpc
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
            # v0.2.304: forward bp_intensity so backfill-tic populates
            # the BPC column on Bruker rows. Pre-fix this call dropped
            # the bp_intensity Bruker reads for free from MaxIntensity,
            # so backfill-tic --force never produced BPC data and the
            # dashboard's TIC | BPC toggle stayed hidden.
            insert_tic_trace(run_id, trace.rt_min, trace.intensity,
                             db_path=db_path,
                             bp_intensity=trace.bp_intensity)
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

    # ── sample_health TIC extraction (v0.2.150 / v0.2.251) ─────
    # backfill-tic historically only covered runs / tic_traces.
    # Sample-health rows (non-QC files: blanks, column equilibrations,
    # chowE standards, etc.) got their TICs from the watcher's live
    # ingest path. v0.2.251: even without --force, sweep sample_health
    # rows that have no TIC stored yet so every acquisition is covered,
    # not just QC runs. With force=True, re-extract ALL rows regardless
    # (retroactive fix for the v0.2.147 sawtooth correction).
    from stan.db import insert_health_tic_trace
    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        try:
            if force:
                sh_rows = con.execute(
                    "SELECT id, run_name, raw_path FROM sample_health"
                ).fetchall()
            else:
                # Only rows that don't already have a health TIC trace.
                sh_rows = con.execute(
                    "SELECT s.id, s.run_name, s.raw_path "
                    "FROM sample_health s "
                    "LEFT JOIN health_tic_traces h ON h.health_id = s.id "
                    "WHERE h.health_id IS NULL"
                ).fetchall()
        except sqlite3.OperationalError:
            sh_rows = []
    if sh_rows and verbose:
        console.print(
            f"\n[bold]{'Re-extracting' if force else 'Extracting'} TIC "
            f"for {len(sh_rows)} sample_health rows...[/bold]"
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
        # v0.2.198: wake the HF Space BEFORE the burst. Spaces sleep
        # after 48h idle and the first request lands on a "Space is
        # starting..." HTML loading page that still returns HTTP 200.
        # Before this fix the client counted those loading-page 200s
        # as successful patches. Result: 590 sawtooth rows on HF all
        # summer because every push reported succeeded but nothing
        # committed. Poll /api/health until we get the real JSON
        # `{"status":"ok"}` payload (up to 90s). Skip the burst and
        # log every row as failed if the Space never wakes.
        import time as _time
        _space_awake = False
        _wake_started = _time.time()
        while _time.time() - _wake_started < 90:
            try:
                with urllib.request.urlopen(
                    f"{RELAY_URL}/api/health", timeout=15
                ) as _r:
                    _body = _r.read(512).decode("utf-8", errors="replace")
                    if _r.status == 200 and '"status":"ok"' in _body:
                        _space_awake = True
                        break
            except Exception:
                pass
            _time.sleep(3)
        if not _space_awake:
            console.print(
                "[yellow]Relay health check never returned "
                "{\"status\":\"ok\"} within 90s — the Space may be "
                "asleep or down. Skipping push.[/yellow]"
            )
        ok = 0
        # v0.2.155: capture per-row push errors so the summary log
        # records WHY pushes failed (rate limit? auth? relay down?).
        # v0.2.198: now also validates the JSON response body, not just
        # the HTTP status. Sleeping-Space loading pages return HTTP
        # 200 with HTML — previously counted as success despite no
        # commit landing.
        push_errors: list[dict] = []
        for sub_id, push_data in pushed_rows:
            if not _space_awake:
                push_errors.append({
                    "sub_id": sub_id[:8], "status": None,
                    "error": "Relay asleep — skipped",
                })
                continue
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
                    _body_text = resp.read().decode("utf-8", errors="replace")
                    if resp.status == 200:
                        # Validate that the response is a real patch
                        # result, not an HF loading page.
                        try:
                            _body_json = json.loads(_body_text)
                            if _body_json.get("status") == "updated":
                                ok += 1
                            else:
                                push_errors.append({
                                    "sub_id": sub_id[:8],
                                    "status": 200,
                                    "error": (
                                        "Relay returned 200 but not "
                                        f"'updated': {_body_text[:160]}"
                                    ),
                                })
                        except ValueError:
                            push_errors.append({
                                "sub_id": sub_id[:8],
                                "status": 200,
                                "error": (
                                    "Relay returned non-JSON 200 "
                                    "(likely sleeping-Space HTML): "
                                    f"{_body_text[:160]}"
                                ),
                            })
                    else:
                        push_errors.append({
                            "sub_id": sub_id[:8],
                            "status": resp.status,
                            "error": f"HTTP {resp.status}: {_body_text[:160]}",
                        })
            except urllib.error.HTTPError as e:
                _err_body = ""
                try:
                    _err_body = e.read(512).decode("utf-8", errors="replace")
                except Exception:
                    pass
                push_errors.append({
                    "sub_id": sub_id[:8], "status": e.code,
                    "error": f"HTTPError: {e.reason} — {_err_body[:160]}",
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
                _fh.write("backfill-tic --push summary\n")
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
    from stan.config import load_community  # noqa: E402

    submitted = [u for u in updates if u["submission_id"]]
    if not submitted:
        console.print(
            "[dim]No submitted runs needed updating on the community relay.[/dim]"
        )
        return

    # Auth token for /api/update — prevents forks from patching data
    try:
        _community_config = load_community()
    except Exception:
        _community_config = {}
    _repair_token = _community_config.get("auth_token", "")

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
def watch(
    no_keep_awake: bool = typer.Option(
        False,
        "--no-keep-awake",
        help="Disable Windows keep-awake (allow screen saver / sleep while watching).",
    ),
) -> None:
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

    if not no_keep_awake:
        from stan.keep_awake import keep_awake
        keep_awake()  # Windows-only no-op elsewhere

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


def _detect_tailscale() -> dict | None:
    """Best-effort probe for a running Tailscale daemon on this host.

    Returns ``{'hostname': str, 'suffix': str, 'ip': str}`` or None if
    Tailscale isn't installed or isn't currently logged in. Used by
    `stan dashboard` to auto-configure godmode access without
    requiring the operator to set env vars or edit stan.bat.

    v0.2.315: added so installing Tailscale on an instrument PC is the
    only manual step — `stan dashboard` reads the local Tailscale
    state on startup, expands the bind host + Origin allowlist
    automatically. Pre-fix the operator had to know their tailnet
    suffix, set STAN_DASHBOARD_EXTRA_ORIGINS by hand, and add
    ``--host 0.0.0.0`` to the launch line.
    """
    import json
    import shutil
    import subprocess

    if not shutil.which("tailscale"):
        return None
    try:
        r = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if r.returncode != 0 or not r.stdout:
            return None
        data = json.loads(r.stdout)
    except Exception:
        return None

    self_node = data.get("Self") or {}
    hostname = (self_node.get("HostName") or "").lower()
    suffix = data.get("MagicDNSSuffix") or ""
    ips = self_node.get("TailscaleIPs") or []
    ip = ips[0] if ips else ""

    if not hostname and not ip:
        return None
    return {"hostname": hostname, "suffix": suffix, "ip": ip}


@app.command()
def _resolve_dashboard_backend(backend: str) -> None:
    """Set STAN_DB_BACKEND for the dashboard process.

    An explicit STAN_DB_BACKEND in the environment always wins -- the
    flag is a convenience, not an override of what the operator already
    said. Otherwise "auto" probes PG Farm once (bounded, ~1s when the
    credential file is absent) and uses it if it answers.
    """
    import os

    choice = (backend or "auto").strip().lower()
    if choice not in ("auto", "pg", "sqlite"):
        console.print(f"[red]--backend must be auto, pg or sqlite (got {backend!r})[/red]")
        raise typer.Exit(1)

    if os.environ.get("STAN_DB_BACKEND"):
        return
    if choice == "sqlite":
        return
    if choice == "pg":
        os.environ["STAN_DB_BACKEND"] = "pg"
        return

    from stan.db import get_db_path
    from stan.db_pg import probe_pg
    if probe_pg():
        os.environ["STAN_DB_BACKEND"] = "pg"
        console.print("  Store:       [green]PG Farm (direct)[/green]")
    else:
        console.print(f"  Store:       SQLite ({get_db_path()})")


@app.command()
def dashboard(
    port: int = typer.Option(8421, "--port", "-p", help="Dashboard port"),
    host: str = typer.Option("127.0.0.1", "--host", help="Dashboard host"),
    backend: str = typer.Option(
        "auto", "--backend",
        help="Which store to read: auto | pg | sqlite",
    ),
) -> None:
    """Start the local STAN dashboard.

    Serves the QC dashboard at http://localhost:8421.

    v1.0.15: reads the central PG Farm store directly when it can reach
    it, instead of waiting on the 5-minute SQLite mirror. --backend auto
    (the default) probes for PG credentials once at startup and falls
    back to SQLite when there are none -- so a single-lab install with no
    PG Farm access is unchanged and needs no flag. Force either side with
    --backend pg / --backend sqlite, or by exporting STAN_DB_BACKEND.

    v0.2.315: if Tailscale is installed and logged in on this host,
    the dashboard auto-configures godmode access:
      - bind expands from 127.0.0.1 to 0.0.0.0 so Tailscale traffic
        can reach the listener (Windows firewall still gates inbound)
      - Tailscale URLs are added to the Origin allowlist so godmode
        action POSTs from a remote browser don't 403
    Set --host explicitly to override; existing
    STAN_DASHBOARD_EXTRA_ORIGINS env var entries are preserved.
    """
    import os
    import uvicorn

    _resolve_dashboard_backend(backend)

    ts = _detect_tailscale()
    if ts:
        extra: list[str] = []
        if ts["hostname"]:
            extra.append(f"http://{ts['hostname']}:{port}")
            if ts["suffix"]:
                extra.append(f"http://{ts['hostname']}.{ts['suffix']}:{port}")
        if ts["ip"]:
            extra.append(f"http://{ts['ip']}:{port}")

        existing = os.environ.get("STAN_DASHBOARD_EXTRA_ORIGINS", "")
        parts = [p.strip() for p in existing.split(",") if p.strip()]
        for e in extra:
            if e not in parts:
                parts.append(e)
        os.environ["STAN_DASHBOARD_EXTRA_ORIGINS"] = ",".join(parts)

        if host == "127.0.0.1":
            host = "0.0.0.0"

        console.print(f"[bold]STAN v{__version__}[/bold] — dashboard "
                      f"[cyan](Tailscale detected)[/cyan]")
        console.print(f"  Bound to:    {host}:{port}")
        console.print(f"  Local:       http://localhost:{port}")
        for u in extra:
            console.print(f"  Tailscale:   {u}")
        console.print()
    else:
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
        console.print("  Injection counter reset to 0")


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

    # Fields we want to fill. Extended in v0.2.213 with the metadata
    # the v1.0 community schema requires (engine version, gradient,
    # column metadata) so backfill-metrics retroactively populates
    # everything stan-test audits as missing.
    ALL_METRIC_FIELDS = [
        "dynamic_range_log10", "ms1_signal", "ms2_signal",
        "median_mass_acc_ms1_ppm", "median_mass_acc_ms2_ppm",
        "median_peak_width_sec", "median_points_across_peak",
        "fwhm_rt_min", "peak_capacity", "ips_score",
        "missed_cleavage_rate",
        "median_fragments_per_precursor", "median_cv_precursor",
        "diann_version", "search_engine",
        "gradient_length_min",
        # v0.2.219: re-stamp the row's producing version so the v1.0
        # readiness audit can see "this row was last touched by stan
        # X" and decide whether to keep or re-extract.
        "stan_version",
        # v0.2.225: column metadata pulled from instruments.yml so a
        # single backfill-metrics pass populates historical rows.
        "column_vendor", "column_model",
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

        # v0.2.213: pass gradient_min so peak_capacity computes during
        # backfill. Use stored gradient_length_min when present, else
        # snap from spd via the same Evosep table the live watcher uses.
        existing_grad = row["gradient_length_min"]
        existing_spd = row["spd"]
        derived_grad = existing_grad
        if not derived_grad and existing_spd:
            _SPD_TO_GRAD = {200: 6, 100: 11, 60: 21, 40: 30, 30: 44, 15: 88}
            for s, g in _SPD_TO_GRAD.items():
                if int(existing_spd) >= s:
                    derived_grad = g
                    break

        # Re-extract metrics
        try:
            is_dia = "dia" in mode.lower() if mode else True
            if is_dia:
                metrics = extract_dia_metrics(
                    str(report_path),
                    raw_path=raw_path,
                    vendor=vendor or None,
                    gradient_min=derived_grad,
                )
                metrics["instrument_family"] = instrument
                metrics["spd"] = existing_spd
                metrics["gradient_length_min"] = derived_grad
                new_ips = compute_ips_dia(metrics)
            else:
                metrics = extract_dda_metrics(
                    str(report_path),
                    gradient_min=derived_grad or 60,
                )
                metrics["instrument_family"] = instrument
                metrics["gradient_length_min"] = derived_grad
                new_ips = compute_ips_dda(metrics)
            metrics["ips_score"] = new_ips
        except Exception as e:
            if errors < 3:
                console.print(f"  [red]Extract error: {run_name}: {e}[/red]")
            errors += 1
            continue

        # v0.2.219: stamp the row with the current stan version so
        # downstream queries can find "rows extracted by stan X+" for
        # the v1.0 wipe-and-repopulate readiness check.
        try:
            from stan import __version__ as _stan_ver
        except Exception:
            _stan_ver = "unknown"
        metrics["stan_version"] = _stan_ver

        # v0.2.225: pull column metadata from instruments.yml so the
        # backfill stamps it on existing NULL rows. Brett's setup
        # already captured column_vendor + column_model per instrument
        # at install time — pre-fix only the live watcher path read
        # those values into the runs row, so historical rows came in
        # with NULL columns.
        # v0.2.228: keyed-lookup-on-name was wrong — instruments.yml
        # uses config NAME ("auto"), DB stores MODEL ("Orbitrap
        # Exploris 480"). Same pattern as the test --extract fix:
        # prefer name/alias match, else fall back to first block with
        # column data (each PC is typically one instrument).
        if not hasattr(_backfill_tic_impl, "_yml_cache"):
            try:
                import yaml as _y
                from stan.config import resolve_config_path
                _yml_path = resolve_config_path("instruments.yml")
                _yml = _y.safe_load(_yml_path.read_text(encoding="utf-8")) or {}
                _backfill_tic_impl._yml_blocks = list(_yml.get("instruments") or [])
                # Pre-build name+alias index
                _backfill_tic_impl._yml_by_name = {}
                for blk in _backfill_tic_impl._yml_blocks:
                    n = blk.get("name")
                    if n:
                        _backfill_tic_impl._yml_by_name[n] = blk
                    for a in (blk.get("aliases") or []):
                        _backfill_tic_impl._yml_by_name[a] = blk
                # Fallback block with any column data
                _backfill_tic_impl._yml_first_with_col = next(
                    (b for b in _backfill_tic_impl._yml_blocks
                     if b.get("column_vendor") or b.get("column_model")),
                    None,
                )
                _backfill_tic_impl._yml_cache = True
            except Exception:
                _backfill_tic_impl._yml_blocks = []
                _backfill_tic_impl._yml_by_name = {}
                _backfill_tic_impl._yml_first_with_col = None
                _backfill_tic_impl._yml_cache = True
        chosen = (
            _backfill_tic_impl._yml_by_name.get(instrument)
            or _backfill_tic_impl._yml_first_with_col
        )
        if chosen is not None:
            cv = chosen.get("column_vendor")
            cm = chosen.get("column_model")
            if cv:
                metrics["column_vendor"] = cv
            if cm:
                metrics["column_model"] = cm

        # v0.2.213: detect LC system from raw file when DB has empty
        # string (legacy default) or NULL. Live watcher started doing
        # this in v0.2.212 but existing rows from before the fix need
        # this backfill pass to populate.
        existing_lc = (row["lc_system"] or "").strip() if "lc_system" in row.keys() else ""
        if not existing_lc and raw_path:
            try:
                from stan.metrics.scoring import detect_lc_system
                lc_sys = detect_lc_system(Path(raw_path))
                if lc_sys:
                    metrics["lc_system"] = lc_sys
            except Exception:
                pass

        # Build UPDATE set.
        # - Gap-fill mode (default): only write when old is NULL/zero
        #   AND new has a real value. Preserves operator-reviewed data.
        # - Force mode: write whenever new has a real value, even if
        #   old was already populated. Lets a new extractor replace
        #   stale values from a prior version.
        updates: dict = {}
        skipped_fields: list[tuple[str, str]] = []  # (field, reason) for diag
        # Special-case lc_system: empty string is a "gap" too, not just NULL.
        if metrics.get("lc_system") and not existing_lc:
            updates["lc_system"] = metrics["lc_system"]
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
            # Empty-string is a gap for text columns (column_vendor,
            # column_model, search_engine, diann_version, stan_version).
            gap = (old_val is None or old_val == 0 or old_val == "")
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
                with urllib.request.urlopen(req, timeout=15) as _resp:
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
    # v0.2.202: route through detect_drift_best so every backfilled run
    # gets the feature-based detector when a .features sidecar exists
    # and falls back to the MS1-mode histogram otherwise. This is how
    # Brett's 21144 / 12816 / 21149 get correctly classified after the
    # 4DFF install step runs earlier in the PS1 chain.
    from stan.metrics.window_drift import detect_drift_best as detect_window_drift

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
        # v0.2.300: drift is an ion-mobility metric — orbitraps don't
        # have it. Skip Thermo .raw silently before invoking the
        # detector so Exploris / Lumos / Astral backfills don't emit
        # one warning per file ("No .features file for X.raw — run
        # stan run-4dff" / "alphatims not installed — window drift
        # detection disabled"). On Thermo this code path is never
        # going to produce useful drift, and the warning spam confuses
        # operators who think they need to install something.
        if raw_path.suffix.lower() == ".raw":
            n_skip_unknown += 1
            _log({"event": "skip", "run_id": run["id"],
                  "reason": "Thermo .raw — drift is ion-mobility-only",
                  "raw_path": raw})
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


# ─────────────────────────────────────────────────────────────
#  4DFF — Bruker universal feature finder integration (v0.2.196+)
# ─────────────────────────────────────────────────────────────

@app.command("install-4dff")
def install_4dff_cmd(
    platform: str = typer.Option(
        "", "--platform",
        help="'linux' or 'windows'. Empty = auto-detect from current OS.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-download even if cached binaries pass SHA verification.",
    ),
) -> None:
    """Download Bruker 4D feature finder (uff-cmdline2) into ~/.stan/bruker_ff/.

    4DFF is vendored by AlphaPept (MIT). We pull from a pinned
    alphapept commit SHA and SHA256-verify every file. License text
    is installed alongside per AlphaPept's redistribution terms.

    Use `--platform linux` from a mac when bootstrapping a Hive
    install (mac won't pick linux64 via auto-detect).
    """
    from stan.metrics.features import _ALPHAPEPT_PINNED_SHA, install_4dff

    plat_arg = platform or None
    try:
        binary = install_4dff(platform=plat_arg, force=force)
    except Exception as e:
        console.print(f"[red]4DFF install failed: {e}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[green]4DFF installed[/green] at [bold]{binary}[/bold]\n"
        f"  alphapept pin: {_ALPHAPEPT_PINNED_SHA}"
    )


@app.command("run-4dff")
def run_4dff_cmd(
    d_path: Path = typer.Argument(..., help="Path to a Bruker .d directory."),
    timeout: int = typer.Option(
        30, "--timeout",
        help="Max minutes to wait for uff-cmdline2.",
    ),
    platform: str = typer.Option(
        "", "--platform",
        help="'linux' or 'windows'. Empty = auto-detect.",
    ),
) -> None:
    """Run Bruker 4D feature finder on a .d directory.

    Produces <d>/<stem>.features (SQLite) which the feature-based
    drift detector reads. Fails if 4DFF is not installed — run
    `stan install-4dff` first.
    """
    from stan.metrics.features import run_4dff

    plat_arg = platform or None
    try:
        result = run_4dff(d_path, timeout_min=timeout, platform=plat_arg)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    except Exception as e:
        console.print(f"[red]4DFF run failed: {e}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[green]4DFF complete[/green] in {result.wall_clock_sec:.1f}s "
        f"(rc={result.returncode})\n"
        f"  features: [bold]{result.features_path}[/bold]"
    )


@app.command("backfill-feature-cloud")
def backfill_feature_cloud(
    limit: int = typer.Option(
        0, "--limit", help="Stop after this many runs (0 = all queued).",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-extract even for runs that already have a stored cloud.",
    ),
    instrument: str = typer.Option(
        "", "--instrument", help="Only runs from this instrument.",
    ),
    max_points: int = typer.Option(
        0, "--max-points",
        help="Points kept per run (0 = module default, 5000).",
    ),
    since: str = typer.Option(
        "", "--since",
        help="Only runs with run_date >= this ISO date (e.g. 2026-06-01).",
    ),
    cache_dir_opt: str = typer.Option(
        "", "--cache-dir",
        help="Also write each cloud as <run_id>.json here. Use when the "
             "extraction host can't reach the DB — the other host picks "
             "them up with --from-cache.",
    ),
    from_cache: str = typer.Option(
        "", "--from-cache",
        help="Load pre-extracted <run_id>.json clouds from this directory "
             "instead of reading .features sidecars. Use on a host that "
             "can't see the raw data (e.g. "
             "/Volumes/proteomics-grp/STAN/feature_clouds).",
    ),
) -> None:
    """Publish charge-labeled ion clouds from 4DFF .features into the DB.

    ``backfill-features`` generates the ``.features`` sidecars; this
    reads them and stores a downsampled, charge-labeled point cloud in
    the ``feature_clouds`` table (PG when ``STAN_DB_BACKEND=pg``).

    Run it on the host that can see the raw data — the dashboard almost
    never can. Before this existed, the Plotly ion-cloud view only
    rendered on a machine with the ``.d`` mounted locally, so the fleet
    dashboard showed "no .features file found" for every single run even
    though 4DFF had written the sidecars hours earlier.

    Newest runs first, because that is what anyone opens first.
    """
    import json as _json
    import sqlite3
    from datetime import datetime, timezone

    from stan.config import get_user_config_dir
    from stan.db import get_db_path, init_db, insert_feature_cloud
    from stan.db_pg import use_pg
    from stan.metrics.feature_cloud import (
        DEFAULT_MAX_POINTS, cloud_to_json, extract_feature_cloud,
        load_feature_cloud_json,
    )
    from stan.metrics.features import find_features_file

    cap = max_points if max_points > 0 else DEFAULT_MAX_POINTS
    cache_dir = Path(from_cache) if from_cache else None
    if cache_dir is not None and not cache_dir.is_dir():
        console.print(f"[red]--from-cache dir not found: {cache_dir}[/red]")
        raise typer.Exit(1)
    write_cache = Path(cache_dir_opt) if cache_dir_opt else None
    if write_cache is not None:
        write_cache.mkdir(parents=True, exist_ok=True)

    # Row source follows the store of record: on Hive that is PG, and
    # reading the local SQLite there would queue a handful of stale
    # rows instead of the fleet's 200+.
    rows: list[dict] = []
    have: set[str] = set()
    if use_pg():
        from stan.db_pg import _connect
        with _connect() as pg, pg.cursor() as cur:
            sql = ("SELECT id, run_name, instrument, raw_path FROM runs "
                   "WHERE raw_path LIKE '%%.d'")
            params: list = []
            if instrument:
                sql += " AND instrument = %s"
                params.append(instrument)
            if since:
                sql += " AND run_date >= %s"
                params.append(since)
            sql += " ORDER BY run_date DESC"
            if limit > 0:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql, tuple(params))
            rows = [
                {"id": str(r[0]), "run_name": r[1], "instrument": r[2],
                 "raw_path": r[3]}
                for r in cur.fetchall()
            ]
            if not force:
                try:
                    cur.execute(
                        "SELECT run_id FROM feature_clouds WHERE source = 'runs'"
                    )
                    have = {str(r[0]) for r in cur.fetchall()}
                except Exception:
                    have = set()
    else:
        init_db()
        with sqlite3.connect(str(get_db_path())) as con:
            con.row_factory = sqlite3.Row
            sql = "SELECT id, run_name, instrument, raw_path FROM runs WHERE raw_path LIKE '%.d'"
            params = []
            if instrument:
                sql += " AND instrument = ?"
                params.append(instrument)
            if since:
                sql += " AND run_date >= ?"
                params.append(since)
            sql += " ORDER BY run_date DESC"
            if limit > 0:
                sql += f" LIMIT {int(limit)}"
            rows = [dict(r) for r in con.execute(sql, params).fetchall()]
            if not force:
                try:
                    have = {
                        r[0] for r in con.execute(
                            "SELECT run_id FROM feature_clouds WHERE source = 'runs'"
                        )
                    }
                except sqlite3.OperationalError:
                    have = set()

    console.print(
        f"[bold]{len(rows)} runs queued for ion-cloud extraction[/bold] "
        f"(cap {cap:,} points/run, {len(have)} already stored)"
    )

    log_dir = get_user_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (
        log_dir
        / f"backfill_feature_cloud_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    log_fh = open(log_path, "a", encoding="utf-8")

    def _log(record: dict) -> None:
        record["ts"] = datetime.now(timezone.utc).isoformat()
        log_fh.write(_json.dumps(record) + "\n")
        log_fh.flush()

    _log({"event": "start", "n_queued": len(rows), "force": force,
          "max_points": cap, "backend": "pg" if use_pg() else "sqlite"})

    n_done = n_skipped = n_errors = 0
    for run in rows:
        rid = run["id"]
        raw_path = run.get("raw_path") or ""
        if not force and rid in have:
            n_skipped += 1
            continue
        if not raw_path:
            n_skipped += 1
            _log({"event": "skip", "run_id": rid, "reason": "no raw_path"})
            continue
        if cache_dir is not None:
            # Look the file up by run id rather than listing the
            # directory: the quobyte share is mounted over SMB on the Mac
            # and readdir results go stale for minutes at a time, so a
            # glob reports an empty directory whose files stat fine.
            feat = cache_dir / f"{rid}.json"
            if not feat.exists():
                n_skipped += 1
                continue
        else:
            feat = find_features_file(raw_path)
            if feat is None:
                n_skipped += 1
                _log({"event": "skip", "run_id": rid, "run_name": run["run_name"],
                      "reason": "no .features sidecar", "raw_path": raw_path})
                continue
        try:
            if cache_dir is not None:
                cloud = load_feature_cloud_json(feat)
            else:
                cloud = extract_feature_cloud(feat, max_points=cap)
            if cloud.n_points == 0:
                n_skipped += 1
                _log({"event": "skip", "run_id": rid, "run_name": run["run_name"],
                      "reason": "sidecar has no usable rows"})
                continue
            if write_cache is not None:
                # Write via a temp name so a reader on the other side of
                # the share never picks up a half-written cloud.
                tmp = write_cache / f".{rid}.json.part"
                tmp.write_text(_json.dumps(
                    cloud_to_json(cloud, rid, str(run.get("run_name") or ""))
                ))
                tmp.replace(write_cache / f"{rid}.json")
            insert_feature_cloud(
                run_id=rid, mz=cloud.mz, mobility=cloud.mobility,
                rt=cloud.rt, charge=cloud.charge, intensity=cloud.intensity,
                n_total=cloud.n_total,
                features_path=cloud.features_path or str(feat), table="runs",
            )
        except Exception as e:  # noqa: BLE001 - one bad sidecar must not stop the walk
            n_errors += 1
            _log({"event": "error", "run_id": rid, "run_name": run["run_name"],
                  "error": str(e), "error_type": type(e).__name__})
            console.print(f"  [red]{str(run['run_name'])[:60]}: {e}[/red]")
            continue

        n_done += 1
        charges = sorted({int(z) for z in cloud.charge})
        _log({"event": "done", "run_id": rid, "run_name": run["run_name"],
              "n_points": cloud.n_points, "n_total": cloud.n_total,
              "charges": charges, "features_path": str(feat)})
        console.print(
            f"  [green]{str(run['run_name'])[:56]:<56}[/green] "
            f"{cloud.n_points:>6,}/{cloud.n_total:<7,} pts  "
            f"z={','.join(str(z) for z in charges)}"
        )

    _log({"event": "end", "done": n_done, "skipped": n_skipped,
          "errors": n_errors})
    log_fh.close()
    console.print(
        f"[bold]Ion-cloud backfill complete[/bold] — "
        f"done={n_done} skipped={n_skipped} errors={n_errors}"
    )
    console.print(f"[dim]Log: {log_path}[/dim]")


@app.command("backfill-features")
def backfill_features(
    limit: int = typer.Option(
        0, "--limit",
        help="Stop after this many runs (0 = all queued).",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-run 4DFF even when a .features file already exists.",
    ),
    instrument: str = typer.Option(
        "", "--instrument",
        help="Only runs from this instrument (DB instrument string).",
    ),
    timeout: int = typer.Option(
        30, "--timeout",
        help="Max minutes per run.",
    ),
    platform: str = typer.Option(
        "", "--platform",
        help="'linux' or 'windows'. Empty = auto-detect.",
    ),
) -> None:
    """Generate 4DFF .features for every indexed Bruker .d run.

    Iterates the local DB, calls run_4dff on each run's raw_path
    that doesn't yet have a .features file alongside. Logs a JSONL
    record per run (and the summary) to ~/.stan/logs/ so the Hive
    mirror can surface progress.
    """
    import json as _json
    import sqlite3
    from datetime import datetime, timezone

    from stan.config import get_user_config_dir, sync_to_hive_mirror
    from stan.db import get_db_path, init_db
    from stan.metrics.features import find_features_file, run_4dff

    init_db()
    db_path = get_db_path()

    with sqlite3.connect(str(db_path)) as con:
        con.row_factory = sqlite3.Row
        where = ["mode LIKE '%dia%' OR mode = 'qc'"]
        params: list = []
        if instrument:
            where.append("instrument = ?")
            params.append(instrument)
        sql = "SELECT id, run_name, instrument, raw_path FROM runs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY run_date DESC"
        if limit > 0:
            sql += f" LIMIT {limit}"
        rows = con.execute(sql, params).fetchall()

    console.print(f"[bold]{len(rows)} runs queued for 4DFF backfill[/bold]")
    log_dir = get_user_config_dir() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = (
        log_dir / f"backfill_features_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    )
    log_fh = open(log_path, "a", encoding="utf-8")

    def _log(record: dict) -> None:
        record["ts"] = datetime.now(timezone.utc).isoformat()
        log_fh.write(_json.dumps(record) + "\n")
        log_fh.flush()

    _log({"event": "start", "n_queued": len(rows), "force": force,
          "timeout_min": timeout})

    n_done = 0
    n_skipped = 0
    n_errors = 0
    plat_arg = platform or None

    for row in rows:
        run = dict(row)
        raw_path = run.get("raw_path") or ""
        if not raw_path:
            n_skipped += 1
            _log({"event": "skip", "run_id": run["id"], "reason": "no raw_path"})
            continue
        d = Path(raw_path)
        if not d.exists() or not d.is_dir() or d.suffix.lower() != ".d":
            n_skipped += 1
            _log({"event": "skip", "run_id": run["id"],
                  "reason": "not a .d on disk", "raw_path": raw_path})
            continue
        if not force and find_features_file(d) is not None:
            n_skipped += 1
            _log({"event": "skip", "run_id": run["id"],
                  "reason": "already has .features"})
            continue

        try:
            result = run_4dff(d, timeout_min=timeout, platform=plat_arg)
        except Exception as e:
            n_errors += 1
            _log({"event": "error", "run_id": run["id"], "run_name": run["run_name"],
                  "error": str(e), "error_type": type(e).__name__})
            console.print(f"  [red]{run['run_name'][:60]}: {e}[/red]")
            continue

        n_done += 1
        _log({
            "event": "done", "run_id": run["id"], "run_name": run["run_name"],
            "features_path": str(result.features_path),
            "wall_clock_sec": result.wall_clock_sec,
            "returncode": result.returncode,
        })
        console.print(
            f"  [green]{run['run_name'][:60]:<60}[/green] "
            f"{result.wall_clock_sec:6.1f}s"
        )

    _log({"event": "end", "done": n_done, "skipped": n_skipped, "errors": n_errors})
    log_fh.close()
    console.print(
        f"[bold]4DFF backfill complete[/bold] — "
        f"done={n_done} skipped={n_skipped} errors={n_errors}"
    )
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

    console.print("[bold]Rewrite plan:[/bold]")
    console.print(f"  from: {from_name!r}")
    console.print(f"  to:   {to_name!r}")
    console.print(f"  runs with {from_name!r}:          {n_runs}")
    console.print(f"  sample_health with {from_name!r}: {n_sh}")
    console.print(f"  runs already on {to_name!r} (merge target): {conflict_runs}")
    console.print()

    if n_runs == 0 and n_sh == 0:
        console.print("[yellow]No rows to rewrite - nothing to do.[/yellow]")
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
    _skipped_already_there = 0
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
    elif installed_ver.startswith("1.0.9"):
        console.print(
            f"[yellow]alphatims {installed_ver} is BROKEN against polars 1.35+ - "
            f"forcing downgrade to <1.0.9...[/yellow]"
        )
    elif numpy_bad:
        console.print(
            f"[yellow]numpy {numpy_ver} is 2.0+ - strict searchsorted side= "
            f"breaks alphatims {installed_ver}. Pinning numpy<2...[/yellow]"
        )
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
    output_base: Optional[str] = typer.Option(
        None, "--output-base",
        help="Directory holding <run_stem>/report.parquet. Defaults to "
             "$STAN_OUTPUT_BASE, else ~/.stan/baseline_output. On Hive the "
             "search outputs live in /quobyte/proteomics-grp/STAN/processing.",
    ),
    instrument: Optional[str] = typer.Option(
        None, "--instrument", help="Limit to one instrument name.",
    ),
    limit: int = typer.Option(
        0, "--limit", help="Process at most N runs (0 = all). Newest first.",
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-extract runs that already have anchors stored.",
    ),
) -> None:
    """Extract cIRT anchor retention times for every run with a report.parquet.

    Reads the panel seeded in stan/metrics/cirt.py keyed on (instrument_family,
    spd), finds each run's report.parquet under <output-base>/<run_stem>/,
    extracts the observed RT for each anchor peptide, and writes rows to the
    `irt_anchor_rts` table. Runs are skipped when: there's no panel for their
    (family, spd), their report.parquet is missing, or they're non-DIA (only
    DIA reports have DIA-NN RT columns).

    Backend follows the store of record: with STAN_DB_BACKEND=pg the work
    queue comes from PG and the anchors are written back to PG. Reading the
    local SQLite on Hive queued zero rows, which is why this chart stayed
    empty there. SQLite installs are unaffected.

    Safe to re-run; the write is an upsert on (run_id, peptide) in both
    backends.
    """
    import os
    import sqlite3

    from stan.config import get_user_config_dir
    from stan.db import get_db_path, init_db, insert_irt_anchor_rts
    from stan.db_pg import use_pg
    from stan.metrics.cirt import extract_anchor_rts, get_panel
    from stan.community.submit import _instrument_family

    if output_base:
        base = Path(output_base)
    elif os.environ.get("STAN_OUTPUT_BASE"):
        base = Path(os.environ["STAN_OUTPUT_BASE"])
    else:
        base = get_user_config_dir() / "baseline_output"
    if not base.is_dir():
        console.print(f"[red]--output-base not found: {base}[/red]")
        raise typer.Exit(1)

    # Row source follows the store of record — same split as
    # backfill-feature-clouds. `have` lets --force be the only way to
    # redo work that is already stored.
    rows: list[dict] = []
    have: set[str] = set()
    db_path = get_db_path()
    if use_pg():
        from stan.db_pg import _connect
        with _connect() as pg, pg.cursor() as cur:
            sql = "SELECT id, run_name, instrument, mode, spd FROM runs"
            params: list = []
            if instrument:
                sql += " WHERE instrument = %s"
                params.append(instrument)
            sql += " ORDER BY run_date DESC"
            if limit > 0:
                sql += f" LIMIT {int(limit)}"
            cur.execute(sql, tuple(params))
            rows = [
                {"id": str(r[0]), "run_name": r[1], "instrument": r[2],
                 "mode": r[3], "spd": r[4]}
                for r in cur.fetchall()
            ]
            if not force:
                try:
                    cur.execute("SELECT DISTINCT run_id FROM irt_anchor_rts")
                    have = {str(r[0]) for r in cur.fetchall()}
                except Exception:
                    have = set()
    else:
        init_db()
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            sql = "SELECT id, run_name, instrument, mode, spd FROM runs"
            sq_params: list = []
            if instrument:
                sql += " WHERE instrument = ?"
                sq_params.append(instrument)
            sql += " ORDER BY run_date DESC"
            if limit > 0:
                sql += f" LIMIT {int(limit)}"
            rows = [dict(r) for r in con.execute(sql, sq_params).fetchall()]
            if not force:
                try:
                    have = {
                        r[0] for r in con.execute(
                            "SELECT DISTINCT run_id FROM irt_anchor_rts"
                        ).fetchall()
                    }
                except sqlite3.OperationalError:
                    have = set()

    console.print(f"[bold]{len(rows)} runs in "
                  f"{'PG' if use_pg() else 'SQLite'}[/bold] (reports under {base})")

    processed = 0
    no_panel = 0
    no_report = 0
    non_dia = 0
    no_anchors = 0
    already = 0
    total_anchors = 0

    for run in rows:
        # Match any DIA flavor: "DIA" (Thermo), "diaPASEF" (Bruker),
        # "dia_foo" (hypothetical). The original exact-equality check
        # skipped every Bruker run because "diaPASEF" != "dia".
        if not (run.get("mode") or "").lower().startswith("dia"):
            non_dia += 1
            continue
        if run["id"] in have:
            already += 1
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
        report = base / stem / "report.parquet"
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
                  f"{no_report} no-report, {no_anchors} no-anchors-detected, "
                  f"{already} already-done")


@app.command("derive-cirt-panel")
def derive_cirt_panel(
    instrument_family: Optional[str] = typer.Option(
        None, "--family",
        help="timsTOF | Astral | Exploris | Lumos | Eclipse | Orbitrap. "
             "Required unless --auto.",
    ),
    spd: Optional[int] = typer.Option(
        None, "--spd",
        help="Samples per day. Required unless --auto.",
    ),
    min_precursors: int = typer.Option(
        10000, "--min-precursors",
        help="Minimum n_precursors to consider a run 'good'.",
    ),
    min_runs: int = typer.Option(
        5, "--min-runs",
        help="Minimum cohort size before deriving a panel.",
    ),
    n_anchors: int = typer.Option(
        10, "--n-anchors", help="Target panel size.",
    ),
    max_cv: float = typer.Option(
        5.0, "--max-cv",
        help="Maximum RT CV percent for an anchor candidate. Default 5 is "
             "tight; for long-timespan cohorts (months of runs across "
             "column changes) try 10-15 to find any stable anchors at all.",
    ),
    min_presence: float = typer.Option(
        0.9, "--min-presence",
        help="Fraction of cohort runs the peptide must appear in.",
    ),
    max_days: int = typer.Option(
        0, "--max-days",
        help="Limit cohort to runs in the last N days. 0 = all-time. "
             "Useful when an instrument's history spans column changes; "
             "RT scale shifts across changes make stable anchors hard "
             "to find from the full history.",
    ),
    auto: bool = typer.Option(
        False, "--auto",
        help="Derive panels for every (family, spd) cohort with enough "
             "good DIA runs and write the result to "
             "~/.stan/cirt_panels_auto.yml. Loaded at runtime by "
             "get_panel(), so backfill-cirt picks them up immediately. "
             "When a cohort can't derive its own panel, falls back to "
             "borrowing peptides from a same-vendor neighbour cohort "
             "(Lumos peptides reused on Exploris with locally-derived "
             "RTs) so every cohort gets at least an approximate panel. "
             "Skips silently when the YAML already exists; pass "
             "--force-auto to re-derive (e.g. after the CV-as-diagnostic "
             "logic change in v0.2.224 made old panels stale).",
    ),
    force_auto: bool = typer.Option(
        False, "--force-auto",
        help="Re-derive auto panels even when ~/.stan/cirt_panels_auto.yml "
             "already exists. Use after upgrading to a STAN version that "
             "changes panel selection logic.",
    ),
) -> None:
    """Print or save an empirical cIRT panel.

    Without --auto: prints a single (family, spd) panel pasteable into
    stan/metrics/cirt.py::EMPIRICAL_CIRT_PANELS.

    With --auto: scans the local DB for every (family, spd) cohort
    that has at least min_runs good DIA runs (≥ min_precursors), loads
    each run's report.parquet from ~/.stan/baseline_output/, derives a
    panel via the empirical algorithm (peptides at 1% FDR in ≥90% of
    runs, low RT CV, evenly spread), and writes everything to
    ~/.stan/cirt_panels_auto.yml. Run this once after backfill-metrics
    completes so backfill-cirt has panels for non-TIMS cohorts.
    """
    import sqlite3
    import yaml as _yaml

    from stan.config import get_user_config_dir
    from stan.db import get_db_path, init_db
    from stan.metrics.cirt import derive_panel_from_cohort
    from stan.community.submit import _instrument_family

    init_db()
    db_path = get_db_path()
    user_dir = get_user_config_dir()
    output_base = user_dir / "baseline_output"

    if auto:
        out_path = user_dir / "cirt_panels_auto.yml"
        # If the panels already exist, skip — overnight chain calls
        # this after every metrics backfill and we don't want to
        # re-derive every night when the cohorts haven't changed.
        # Pass --force-auto to override (e.g. after upgrading to a
        # STAN version that changes panel-selection logic).
        if out_path.exists() and not force_auto:
            try:
                existing = _yaml.safe_load(out_path.read_text(encoding="utf-8")) or []
                if existing:
                    console.print(
                        f"[dim]cirt_panels_auto.yml exists with "
                        f"{len(existing)} panels — skipping. Pass "
                        f"[bold]--force-auto[/bold] to re-derive.[/dim]"
                    )
                    return
            except Exception:
                pass

        # Scan every (family, spd) cohort and derive panels for any
        # that meet the minimum-cohort threshold.
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            sql = (
                "SELECT run_name, instrument, spd, run_date FROM runs "
                "WHERE n_precursors > ? AND mode = 'DIA' AND spd IS NOT NULL"
            )
            params: list = [min_precursors]
            if max_days and max_days > 0:
                # Use ISO date math — run_date is stored as ISO8601
                from datetime import datetime as _dt, timedelta as _td, timezone as _tz
                cutoff = (_dt.now(_tz.utc) - _td(days=max_days)).isoformat()
                sql += " AND run_date >= ?"
                params.append(cutoff)
            sql += " ORDER BY instrument, spd"
            rows = con.execute(sql, params).fetchall()

        from collections import defaultdict
        cohorts: dict[tuple[str, int], list[Path]] = defaultdict(list)
        for row in rows:
            fam = _instrument_family(row["instrument"] or "")
            if not fam or row["spd"] is None:
                continue
            stem = Path(row["run_name"]).stem
            report = output_base / stem / "report.parquet"
            if report.exists():
                cohorts[(fam, int(row["spd"]))].append(report)

        # v0.2.224: Brett's correction — CV is a diagnostic, not a
        # filter. Pick peptides that are highly present + spread across
        # the gradient; their RT CV across the cohort goes into the
        # output as info but doesn't reject candidates. If a peptide
        # is moving around between runs, that's exactly the signal
        # cIRT is meant to surface, not throw away.
        from stan.metrics.cirt import derive_rts_for_peptides

        out_panels: list[dict] = []
        derived = 0
        skipped = []
        # First pass — derive each cohort's own panel.
        own_panels: dict[tuple[str, int], list[tuple[str, float]]] = {}
        for (fam, sp), reports in sorted(cohorts.items()):
            if len(reports) < min_runs:
                skipped.append((fam, sp, len(reports)))
                continue
            panel = derive_panel_from_cohort(
                reports, n_anchors=n_anchors,
                min_presence=min_presence,
            )
            if panel:
                own_panels[(fam, sp)] = panel
                out_panels.append({
                    "family": fam, "spd": sp,
                    "n_runs": len(reports),
                    "source": "self",
                    "peptides": [
                        {"seq": seq, "rt": round(rt, 2)} for seq, rt in panel
                    ],
                })
                derived += 1
                console.print(
                    f"[green]✓[/green] ({fam}, SPD={sp}): "
                    f"{len(panel)} anchors from {len(reports)} runs"
                )
                continue

            # v0.2.223: borrow fallback. If this cohort can't derive
            # its own panel (e.g. Exploris with 26 months of drift),
            # borrow peptide sequences from a same-vendor neighbour
            # cohort and re-anchor RTs locally. Vendor families that
            # share Orbitrap chemistry: Lumos, Exploris, Astral,
            # Eclipse, Orbitrap. timsTOF is its own family.
            ORBITRAP_FAMILY = {"Lumos", "Exploris", "Astral", "Eclipse", "Orbitrap"}
            BRUKER_FAMILY = {"timsTOF"}
            if fam in ORBITRAP_FAMILY:
                neighbour_set = ORBITRAP_FAMILY
            elif fam in BRUKER_FAMILY:
                neighbour_set = BRUKER_FAMILY
            else:
                neighbour_set = {fam}

            borrowed: list[tuple[str, float]] = []
            borrowed_from: tuple[str, int] | None = None
            # Sort neighbour panels by size descending (try richest first)
            for (n_fam, n_sp), n_panel in sorted(
                own_panels.items(),
                key=lambda kv: -len(kv[1]),
            ):
                if n_fam not in neighbour_set:
                    continue
                if (n_fam, n_sp) == (fam, sp):
                    continue
                seqs = [s for s, _ in n_panel]
                local = derive_rts_for_peptides(
                    reports, seqs,
                    min_presence=min(min_presence, 0.5),
                )
                if local:
                    borrowed = local[:n_anchors]
                    borrowed_from = (n_fam, n_sp)
                    break

            if borrowed and borrowed_from is not None:
                out_panels.append({
                    "family": fam, "spd": sp,
                    "n_runs": len(reports),
                    "source": f"borrowed:{borrowed_from[0]}/SPD{borrowed_from[1]}",
                    "peptides": [
                        {"seq": seq, "rt": round(rt, 2)} for seq, rt in borrowed
                    ],
                })
                derived += 1
                console.print(
                    f"[cyan]↪[/cyan] ({fam}, SPD={sp}): {len(borrowed)} "
                    f"anchors borrowed from {borrowed_from[0]}/SPD={borrowed_from[1]}"
                )
                continue

            skipped.append((fam, sp, f"{len(reports)} runs but no stable anchors (max CV {max_cv:.0f}%)"))

        out_path = user_dir / "cirt_panels_auto.yml"
        out_path.write_text(_yaml.safe_dump(out_panels, sort_keys=False), encoding="utf-8")
        console.print(f"\n[bold]Wrote {derived} panels[/bold] → {out_path}")
        if skipped:
            console.print("[dim]Skipped cohorts (too few runs or unstable):[/dim]")
            for fam, sp, why in skipped:
                console.print(f"  [dim]({fam}, SPD={sp}): {why}[/dim]")
        return

    # Single-cohort mode (legacy behavior)
    if not instrument_family or spd is None:
        console.print("[red]--family and --spd are required (or use --auto)[/red]")
        raise typer.Exit(2)

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
    if len(cohort_reports) < min_runs:
        console.print(f"[yellow]Not enough runs — need at least {min_runs}.[/yellow]")
        return

    panel = derive_panel_from_cohort(
        cohort_reports, n_anchors=n_anchors,
        max_cv_pct=max_cv, min_presence=min_presence,
    )
    if not panel:
        console.print(
            "[yellow]No stable anchors found at current thresholds. "
            "Try [bold]--max-cv 12[/bold] or [bold]--max-cv 20[/bold] "
            "if this cohort spans many months of runs.[/yellow]"
        )
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
    force: bool = typer.Option(
        False, "--force",
        help="Re-submit runs even if submitted_to_benchmark=1. Use after "
             "the community dataset is wiped (v1.0 cutover) so every "
             "row gets a fresh push to the relay.",
    ),
    backend: str = typer.Option(
        "sqlite", "--backend",
        help="Read source: 'sqlite' (local stan.db, default) or 'pg' "
             "(central PG Farm, requires PGPASSWORD or token file).",
    ),
    since: str = typer.Option(
        "", "--since",
        help="Only submit runs with run_date >= this ISO date "
             "(e.g. 2026-05-13). Scopes a push to recently-processed runs.",
    ),
) -> None:
    """Submit all un-submitted QC runs to the community benchmark.

    Walks the local runs table, finds rows where submitted_to_benchmark=0
    and the run looks like a valid QC file, and calls submit_to_benchmark()
    for each. Skips blanks, test files, and runs that fail validation.

    With ``--force``, ALL rows that pass validation are submitted —
    use this after the community dataset is wiped (v1.0 cutover)
    so every run gets a fresh push.

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

    backend_l = backend.lower().strip()
    if backend_l not in ("sqlite", "pg"):
        console.print(f"[red]--backend must be sqlite or pg, got {backend!r}[/red]")
        raise typer.Exit(2)

    if backend_l == "pg":
        # Pull candidates from PG Farm. `submitted_to_benchmark` lives on the
        # PG row directly, mirroring the SQLite schema — set by the post-
        # submit UPDATE below so re-runs short-circuit. Datetime columns
        # (run_date, migrated_at, hidden_at) come back as native
        # ``datetime.datetime`` objects; we ISO-stringify them so the
        # downstream submit_to_benchmark → json.dumps path doesn't trip
        # over "Object of type datetime is not JSON serializable".
        from datetime import datetime as _dt
        from stan.db_pg import _connect as _pg_connect
        conds, params = [], []
        if not force:
            conds.append(
                "(submitted_to_benchmark = 0 OR submitted_to_benchmark IS NULL)"
            )
        if since:
            conds.append("run_date >= %s")
            params.append(since)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        with _pg_connect() as pg, pg.cursor() as cur:
            cur.execute(
                f"SELECT * FROM runs {where} ORDER BY run_date ASC NULLS LAST",
                params,
            )
            col_names = [d.name for d in cur.description]
            candidates = []
            for row in cur.fetchall():
                d = {}
                for c, v in zip(col_names, row):
                    d[c] = v.isoformat() if isinstance(v, _dt) else v
                candidates.append(d)
    else:
        with sqlite3.connect(str(db_path)) as con:
            con.row_factory = sqlite3.Row
            conds, params = [], []
            if not force:
                conds.append(
                    "(submitted_to_benchmark = 0 OR submitted_to_benchmark IS NULL)"
                )
            if since:
                conds.append("run_date >= ?")
                params.append(since)
            where = ("WHERE " + " AND ".join(conds)) if conds else ""
            candidates = con.execute(
                f"SELECT * FROM runs {where} ORDER BY run_date ASC", params
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
            # Mark as submitted in whichever DB we read from.
            if backend_l == "pg":
                from stan.db_pg import _connect as _pg_connect
                with _pg_connect() as pg, pg.cursor() as cur:
                    cur.execute(
                        "UPDATE runs SET submitted_to_benchmark = 1, "
                        "submission_id = %s WHERE host_origin = %s AND id = %s",
                        (sid, run.get("host_origin"), run["id"]),
                    )
                    pg.commit()
            else:
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
            msg = "  [red]watch_dir does not exist[/red]"
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


def _test_extract_pipeline(
    run_id: str,
    run_name: str,
    raw_path: Optional[Path],
    report_path: Optional[Path],
    spd: Optional[int],
    gradient_min: Optional[int],
    instrument: str,
    mode: str,
    db_path: Path,
) -> dict:
    """Run the full extraction pipeline on one existing run and return
    a per-step pass/fail dict. Used by `stan test --extract` to verify
    that v0.2.214 fixes work on the 5 latest runs of each instrument
    BEFORE the operator commits to a full DB-wide backfill.

    Each step writes its result to the runs row (or its child tables)
    so a follow-up audit reflects what extraction would produce. Steps
    that depend on each other (drift needs 4DFF features) are ordered
    accordingly. Failures don't propagate — every step is best-effort
    and logged independently.
    """
    import sqlite3
    steps: dict = {}
    is_bruker = raw_path is not None and raw_path.is_dir() and raw_path.suffix == '.d'
    is_thermo = raw_path is not None and raw_path.is_file() and raw_path.suffix == '.raw'
    is_dia = 'dia' in (mode or '').lower()

    # ── 0. Auto-search when report.parquet is missing ─────────
    # v0.2.221: TIMS audit revealed all 5 latest QC files had no
    # search output — the watcher's dispatch had silently broken on
    # 4/20. Rather than leave the test failing every step downstream,
    # try to actually run DIA-NN (DIA) or Sage (DDA) here. Slow but
    # gets us to a real verdict on the metrics+cIRT steps. Skipped
    # automatically if the search binary isn't on PATH.
    search_step: dict = {'ok': True, 'skipped': 'report exists'}
    if (report_path is not None
            and not report_path.exists()
            and raw_path is not None
            and raw_path.exists()):
        try:
            output_dir = report_path.parent
            if is_dia:
                from stan.search.local import run_diann_local
                vendor = 'bruker' if is_bruker else 'thermo'
                produced = run_diann_local(
                    raw_path=raw_path, output_dir=output_dir,
                    vendor=vendor, timeout_sec=1500,
                )
                if produced and produced.exists():
                    search_step = {'ok': True, 'engine': 'diann', 'produced': str(produced.name)}
                else:
                    search_step = {'ok': False, 'engine': 'diann',
                                   'why': 'run_diann_local returned no parquet'}
            else:
                from stan.search.local import run_sage_local
                produced = run_sage_local(
                    raw_path=raw_path, output_dir=output_dir,
                    timeout_sec=1500,
                )
                if produced and produced.exists():
                    search_step = {'ok': True, 'engine': 'sage', 'produced': str(produced.name)}
                else:
                    search_step = {'ok': False, 'engine': 'sage',
                                   'why': 'run_sage_local returned no parquet'}
        except FileNotFoundError as e:
            search_step = {'ok': False, 'why': f'search binary not on PATH: {e}'}
        except Exception as e:
            search_step = {'ok': False, 'why': f'{type(e).__name__}: {e}'}
    steps['search'] = search_step

    # ── 1. Metrics + LC system ───────────────────────────────
    try:
        if not report_path or not report_path.exists():
            steps['metrics'] = {'ok': False, 'why': f'report.parquet missing at {report_path}'}
        else:
            from stan.metrics.extractor import extract_dia_metrics, extract_dda_metrics
            from stan.metrics.scoring import detect_lc_system
            from stan.metrics.chromatography import compute_ips_dia, compute_ips_dda

            # Snap gradient from spd if not pinned
            grad = gradient_min
            if not grad and spd:
                _SPD_TO_GRAD = {200: 6, 100: 11, 60: 21, 40: 30, 30: 44, 15: 88}
                for s, g in _SPD_TO_GRAD.items():
                    if int(spd) >= s:
                        grad = g
                        break

            vendor = 'bruker' if is_bruker else ('thermo' if is_thermo else None)
            if is_dia:
                m = extract_dia_metrics(
                    str(report_path), raw_path=raw_path, vendor=vendor,
                    gradient_min=grad,
                )
                m['ips_score'] = compute_ips_dia(m)
            else:
                m = extract_dda_metrics(str(report_path), gradient_min=grad or 60)
                m['ips_score'] = compute_ips_dda(m)
            try:
                if raw_path:
                    lc = detect_lc_system(raw_path)
                    if lc:
                        m['lc_system'] = lc
            except Exception:
                pass
            m['gradient_length_min'] = grad
            try:
                from stan import __version__ as _sv
            except Exception:
                _sv = 'unknown'
            m['stan_version'] = _sv

            # v0.2.228: pull column_vendor + column_model from
            # instruments.yml. v0.2.227 keyed lookup on instrument
            # NAME (e.g. "auto") but the audit's `instrument` value
            # is the MODEL (e.g. "Orbitrap Exploris 480") so the
            # match never fired. Strategy: prefer name match, then
            # alias match, else fall back to first entry that has
            # column data (each PC typically has one instrument).
            try:
                import yaml as _y
                from stan.config import resolve_config_path
                _yml_path = resolve_config_path('instruments.yml')
                _yml = _y.safe_load(_yml_path.read_text(encoding='utf-8')) or {}
                blocks = _yml.get('instruments') or []
                chosen = None
                for inst_block in blocks:
                    aliases = inst_block.get('aliases') or []
                    if (
                        inst_block.get('name') == instrument
                        or instrument in aliases
                    ):
                        chosen = inst_block
                        break
                if chosen is None:
                    for inst_block in blocks:
                        if inst_block.get('column_vendor') or inst_block.get('column_model'):
                            chosen = inst_block
                            break
                if chosen is not None:
                    cv = chosen.get('column_vendor')
                    cm = chosen.get('column_model')
                    if cv:
                        m['column_vendor'] = cv
                    if cm:
                        m['column_model'] = cm
            except Exception:
                pass

            steps['metrics'] = {
                'ok': True,
                'fields': {k: m.get(k) for k in (
                    'diann_version', 'search_engine', 'lc_system',
                    'peak_capacity', 'dynamic_range_log10',
                    'median_fragments_per_precursor',
                    'median_peak_width_sec', 'median_points_across_peak',
                )},
            }
            # UPDATE runs row
            _cols = [k for k in m.keys() if k not in ('instrument_family',)]
            with sqlite3.connect(str(db_path)) as con:
                # Discover which columns exist
                runs_cols = {r[1] for r in con.execute('PRAGMA table_info(runs)').fetchall()}
                writable = {k: v for k, v in m.items() if k in runs_cols}
                if writable:
                    set_clause = ', '.join(f'{k} = ?' for k in writable)
                    con.execute(
                        f'UPDATE runs SET {set_clause} WHERE id = ?',
                        list(writable.values()) + [run_id],
                    )
    except Exception as e:
        steps['metrics'] = {'ok': False, 'why': f'{type(e).__name__}: {e}'}

    # ── 2. TIC ───────────────────────────────────────────────
    try:
        from stan.metrics.tic import (
            extract_tic_bruker, extract_tic_thermo,
            extract_tic_from_report, downsample_trace,
        )
        from stan.db import insert_tic_trace
        trace = None
        src = None
        if report_path and report_path.exists():
            trace = extract_tic_from_report(report_path, n_bins=128)
            if trace:
                src = 'report'
        if trace is None and is_bruker and raw_path:
            trace = extract_tic_bruker(raw_path)
            if trace:
                src = 'bruker'
        if trace is None and is_thermo and raw_path and raw_path.exists():
            trace = extract_tic_thermo(raw_path)
            if trace:
                src = 'thermo'
        if trace is None:
            steps['tic'] = {'ok': False, 'why': 'no extractor produced a trace'}
        else:
            trace = downsample_trace(trace, n_bins=128)
            insert_tic_trace(run_id, trace.rt_min, trace.intensity,
                             db_path=db_path,
                             bp_intensity=trace.bp_intensity)
            # sawtooth check on the resulting trace
            it = trace.intensity
            diffs = [it[i + 1] - it[i] for i in range(len(it) - 1)]
            sc = sum(1 for i in range(len(diffs) - 1) if diffs[i] * diffs[i + 1] < 0)
            pct = 100 * sc / max(1, len(diffs) - 1)
            steps['tic'] = {
                'ok': True, 'src': src,
                'n_points': len(it), 'sawtooth_pct': round(pct, 1),
            }
    except Exception as e:
        steps['tic'] = {'ok': False, 'why': f'{type(e).__name__}: {e}'}

    # ── 3. PEG (both vendors) ────────────────────────────────
    try:
        from stan.metrics.peg import detect_peg_in_spectra
        from stan.metrics.peg_io import read_ms1_any
        from stan.db import update_peg_result
        if not raw_path or not raw_path.exists():
            steps['peg'] = {'ok': False, 'why': 'raw path missing'}
        else:
            spectra = list(read_ms1_any(raw_path))
            peg = detect_peg_in_spectra(spectra)
            update_peg_result(
                run_id=run_id, peg_score=peg.peg_score,
                peg_n_ions_detected=peg.n_ions_detected,
                peg_intensity_pct=peg.intensity_pct,
                peg_class=peg.peg_class, db_path=db_path,
            )
            steps['peg'] = {
                'ok': True, 'score': round(peg.peg_score, 1),
                'n_ions': peg.n_ions_detected, 'class': peg.peg_class,
            }
    except Exception as e:
        steps['peg'] = {'ok': False, 'why': f'{type(e).__name__}: {e}'}

    # ── 4. Drift (Bruker only — orbitraps have no ion mobility) ──
    if not is_bruker:
        steps['drift'] = {'ok': True, 'skipped': 'no ion mobility'}
    else:
        try:
            from stan.metrics.window_drift import detect_drift_best
            from stan.db import update_drift_result
            r = detect_drift_best(raw_path)
            update_drift_result(
                run_id=run_id,
                drift_class=r.drift_class,
                drift_coverage=r.global_coverage,
                drift_median_im=r.median_drift_im,
                drift_p90_abs_im=r.p90_abs_drift_im,
                db_path=db_path,
            )
            steps['drift'] = {
                'ok': True, 'class': r.drift_class,
                'coverage': r.global_coverage,
                'p90': r.p90_abs_drift_im,
            }
        except Exception as e:
            steps['drift'] = {'ok': False, 'why': f'{type(e).__name__}: {e}'}

    # ── 5. cIRT ──────────────────────────────────────────────
    try:
        from stan.metrics.cirt import extract_anchor_rts, get_panel
        from stan.community.submit import _instrument_family
        from stan.db import insert_irt_anchor_rts
        family = _instrument_family(instrument or '')
        panel = get_panel(family, spd)
        if not panel:
            steps['cirt'] = {'ok': False, 'why': f'no panel for ({family}, spd={spd})'}
        elif not report_path or not report_path.exists():
            steps['cirt'] = {'ok': False, 'why': 'report.parquet missing'}
        else:
            observed = extract_anchor_rts(report_path, panel)
            if not observed:
                steps['cirt'] = {
                    'ok': False, 'panel_size': len(panel),
                    'why': 'no panel anchors found in report',
                }
            else:
                n = insert_irt_anchor_rts(run_id, observed, panel, db_path=db_path)
                steps['cirt'] = {
                    'ok': True, 'panel_size': len(panel), 'n_anchors': n,
                }
    except Exception as e:
        steps['cirt'] = {'ok': False, 'why': f'{type(e).__name__}: {e}'}

    return steps


@app.command("set-column")
def set_column(
    instrument: Optional[str] = typer.Option(
        None, "--instrument",
        help="Instrument name (matches instruments.yml). If omitted "
             "and only one instrument is configured, that one is used.",
    ),
    vendor: str = typer.Option(..., "--vendor", help="Column vendor (e.g. IonOpticks, PepMap)."),
    model: str = typer.Option(..., "--model", help="Column model (e.g. Aurora 25cm)."),
    backfill: bool = typer.Option(
        True, "--backfill / --no-backfill",
        help="Also UPDATE existing runs rows on this instrument that "
             "have NULL column_vendor/column_model so the dashboard + "
             "community submissions match the new column.",
    ),
) -> None:
    """Update the LC column metadata for an instrument.

    Writes ``column_vendor`` + ``column_model`` to the instrument's
    block in ``~/.stan/instruments.yml`` and (by default) backfills
    every existing runs row that's missing the column metadata. Use
    after replacing or first installing a column. The watcher reads
    the values from instruments.yml on next ingest, so future QC
    rows will have the correct column tagged.

    Maintenance events: this command also writes a column_change
    event to maintenance_events so the dashboard's event feed shows
    the column swap.
    """
    import sqlite3
    import yaml as _yaml
    from datetime import datetime, timezone
    from stan.config import resolve_config_path
    from stan.db import get_db_path, init_db, insert_maintenance_event

    cfg_path = resolve_config_path("instruments.yml")
    if not cfg_path or not cfg_path.exists():
        console.print(f"[red]instruments.yml not found at {cfg_path}[/red]")
        raise typer.Exit(2)

    cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    instruments = cfg.get("instruments", []) or []
    if not instruments:
        console.print("[red]No instruments configured in instruments.yml[/red]")
        raise typer.Exit(2)

    if instrument is None:
        if len(instruments) == 1:
            instrument = instruments[0].get("name")
        else:
            names = [i.get("name") for i in instruments]
            console.print(
                f"[yellow]--instrument required (have: {names})[/yellow]"
            )
            raise typer.Exit(2)

    target = next(
        (i for i in instruments if i.get("name") == instrument), None,
    )
    if target is None:
        console.print(f"[red]Instrument {instrument!r} not in instruments.yml[/red]")
        raise typer.Exit(2)

    # Update YAML
    target["column_vendor"] = vendor
    target["column_model"] = model
    cfg_path.write_text(_yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    console.print(
        f"[green]✓[/green] {instrument}: column_vendor={vendor}  column_model={model}"
    )
    console.print(f"[dim]wrote {cfg_path}[/dim]")

    # Maintenance event so the column-change shows on the dashboard
    init_db()
    db = get_db_path()
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        insert_maintenance_event(
            instrument=instrument,
            event_type="column_change",
            event_date=now_iso,
            column_vendor=vendor,
            column_model=model,
            note="set via stan set-column",
        )
        console.print("[dim]✓ maintenance_events: column_change recorded[/dim]")
    except Exception as e:
        console.print(f"[yellow]Could not log maintenance event: {e}[/yellow]")

    if backfill:
        with sqlite3.connect(str(db)) as con:
            cur = con.execute(
                "UPDATE runs SET column_vendor = ?, column_model = ? "
                "WHERE instrument = ? "
                "AND (column_vendor IS NULL OR column_vendor = '') "
                "AND (column_model IS NULL OR column_model = '')",
                (vendor, model, instrument),
            )
            console.print(
                f"[green]✓[/green] Backfilled {cur.rowcount} runs row(s) "
                f"on {instrument} with NULL column metadata."
            )


@app.command("test")
def test_latest_runs(
    n: int = typer.Option(5, "--n", help="Number of recent runs to test."),
    instrument: Optional[str] = typer.Option(
        None, "--instrument",
        help="Filter to a specific instrument name (default: all in DB).",
    ),
    extract: bool = typer.Option(
        False, "--extract",
        help="Re-run the full extraction pipeline (metrics, lc, TIC, "
             "PEG, drift, cIRT) on the N latest runs before auditing. "
             "Use this to verify v1.0 readiness before committing to a "
             "full DB-wide backfill.",
    ),
) -> None:
    """Audit the N most recent QC runs against the v1.0 community schema.

    Reports per-run which metadata fields are populated and which are
    NULL/0.0. Counts child-table rows (tic_traces, drift_window_centroids,
    peg_ion_hits, irt_anchor_rts). Flags TIC sawtooth via first-difference
    sign-change ratio. Writes a JSONL log to ~/STAN/logs/ and syncs to
    the Hive mirror so failures can be diagnosed remotely.

    Run on every instrument before flipping STAN to v1.0 + wiping the
    community dataset — green across the board means re-population will
    succeed; reds tell you what's still broken.
    """
    import sqlite3
    import json as _json
    from datetime import datetime as _dt
    from stan.db import get_db_path

    db = get_db_path()

    # Fields by category. NULL/0.0 in any of these = broken for v1.0.
    # The set mirrors the community submission parquet schema so that
    # every column required to render dashboard graphs (precursor /
    # peptide / protein leaderboard, IPS, PEG, drift, mass accuracy,
    # peak capacity, dynamic range, TIC overlay, cIRT) is verified.
    REQ_CORE = [
        'instrument', 'mode', 'spd', 'gradient_length_min', 'amount_ng',
        # v0.2.219: row's producing stan version. NULL = legacy row,
        # likely needs re-extraction before v1.0 community submission.
        'stan_version',
    ]
    REQ_ENGINE = ['diann_version', 'search_engine']
    REQ_LC = ['lc_system', 'column_vendor', 'column_model']
    REQ_COUNTS = ['n_precursors', 'n_peptides', 'n_proteins']
    REQ_QUANT = [
        'median_cv_precursor',
        'median_fragments_per_precursor',
        'missed_cleavage_rate',
    ]
    REQ_MASS = ['median_mass_acc_ms1_ppm', 'median_mass_acc_ms2_ppm']
    REQ_CHROM = [
        'fwhm_rt_min',
        'peak_capacity',
        'dynamic_range_log10',
        'median_peak_width_sec',
        'median_points_across_peak',
    ]
    REQ_SIGNAL = ['ms1_signal', 'ms2_signal']
    REQ_IPS = ['ips_score']
    REQ_PEG = ['peg_score', 'peg_class', 'peg_n_ions_detected', 'peg_intensity_pct']
    REQ_DRIFT = ['drift_class', 'drift_coverage', 'drift_median_im', 'drift_p90_abs_im']

    ALL_FIELDS = (
        REQ_CORE + REQ_ENGINE + REQ_LC + REQ_COUNTS + REQ_QUANT
        + REQ_MASS + REQ_CHROM + REQ_SIGNAL + REQ_IPS + REQ_PEG + REQ_DRIFT
    )

    # Fields where NULL is semantically valid (no replicates, column
    # absent, etc.). The pre-v0.2.212 silent 0.0 was the actual bug —
    # for these fields, NULL is the correct "not applicable" signal,
    # 0.0 is broken (legacy default).
    NULL_OK = {
        'median_cv_precursor',           # no replicates available
        'median_fragments_per_precursor',  # n_frag_extracted absent
        'missed_cleavage_rate',          # depends on Missed.Cleavages col
    }

    def is_populated(value, field: str) -> bool:
        """Field-aware presence check.

        - NULL → broken, except when the field is in NULL_OK
          (semantically nullable, e.g. CV without replicates).
        - 0.0/0 → broken for fields where 0 is meaningless or legacy
          default.
        - Empty string → broken for lc_system + display-style fields.
        """
        if value is None:
            return field in NULL_OK
        if field in ('lc_system', 'column_vendor', 'column_model') and value == '':
            return False
        # 0.0 is broken for these fields (used to be the legacy default)
        if field in (
            'median_cv_precursor', 'median_fragments_per_precursor',
            'amount_ng', 'spd', 'gradient_length_min',
            'peak_capacity', 'dynamic_range_log10',
            'ms1_signal', 'ms2_signal',
        ) and (value == 0 or value == 0.0):
            return False
        return True

    def sawtooth_pct(intensity: list[float]) -> float | None:
        if not intensity or len(intensity) < 6:
            return None
        diffs = [intensity[i + 1] - intensity[i] for i in range(len(intensity) - 1)]
        sc = sum(1 for i in range(len(diffs) - 1) if diffs[i] * diffs[i + 1] < 0)
        return 100.0 * sc / max(1, len(diffs) - 1)

    log_dir = Path.home() / 'STAN' / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"test_{_dt.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
    fh = log_path.open('w', encoding='utf-8')

    def jsonl(rec: dict) -> None:
        fh.write(_json.dumps(rec, default=str) + '\n')

    jsonl({
        'event': 'start',
        'ts': _dt.utcnow().isoformat() + 'Z',
        'n': n,
        'instrument_filter': instrument,
    })

    with sqlite3.connect(str(db)) as con:
        con.row_factory = sqlite3.Row

        if instrument:
            insts = [(instrument,)]
        else:
            insts = con.execute(
                'SELECT DISTINCT instrument FROM runs ORDER BY instrument'
            ).fetchall()
            insts = [(r[0],) for r in insts]

        # Aggregate counters per field across all instruments
        broken_per_field: dict[str, int] = {f: 0 for f in ALL_FIELDS}
        total_runs = 0

        # When --extract: run the full pipeline on the latest N runs
        # of each instrument before auditing. This is the "fast pass":
        # 5 runs × 6 steps × ~30 s each ≈ 15 min per instrument vs.
        # hours of full DB backfill, and produces the same audit signal.
        # v0.2.217: filter to HeLa QC runs only — the runs table can
        # contain non-QC samples (e.g. YWyle on the Lumos) that were
        # ingested before qc_filter was tightened, and the test should
        # not run the pipeline on real-sample acquisitions.
        if extract:
            from stan.config import get_user_config_dir
            from stan.watcher.qc_filter import is_qc_file, compile_qc_pattern
            qc_pattern = compile_qc_pattern()
            output_base = get_user_config_dir() / 'baseline_output'
            for (inst_name,) in insts:
                console.print(f'\n[bold magenta]── extracting on {inst_name} ──[/bold magenta]')
                # Pull more rows than n and keep only QC ones, since
                # the latest N rows by date may include non-QC samples.
                candidate_rows = con.execute(
                    'SELECT id, run_name, raw_path, mode, spd, gradient_length_min '
                    'FROM runs WHERE instrument=? ORDER BY run_date DESC LIMIT ?',
                    (inst_name, n * 5),  # over-fetch and filter
                ).fetchall()
                rows = []
                for r in candidate_rows:
                    rn = r['run_name'] or ''
                    if is_qc_file(Path(rn), qc_pattern):
                        rows.append(r)
                        if len(rows) >= n:
                            break
                if not rows:
                    console.print(
                        f'  [yellow]no QC runs found in latest {n*5} rows[/yellow]'
                    )
                for r in rows:
                    raw_path = Path(r['raw_path']) if r['raw_path'] else None
                    stem = Path(r['run_name']).stem if r['run_name'] else ''
                    report_path = (output_base / stem / 'report.parquet') if stem else None
                    console.print(f'  [bold]{r["run_name"][:50]}[/bold]')
                    steps = _test_extract_pipeline(
                        run_id=r['id'], run_name=r['run_name'],
                        raw_path=raw_path, report_path=report_path,
                        spd=r['spd'], gradient_min=r['gradient_length_min'],
                        instrument=inst_name, mode=r['mode'] or '',
                        db_path=db,
                    )
                    for step_name, result in steps.items():
                        ok = result.get('ok', False)
                        marker = '✓' if ok else ('skip' if result.get('skipped') else '✗')
                        color = 'green' if ok else ('dim' if result.get('skipped') else 'red')
                        detail_pairs = [
                            f'{k}={v}' for k, v in result.items()
                            if k not in ('ok', 'why', 'skipped', 'fields')
                        ]
                        detail = '  '.join(detail_pairs) if detail_pairs else ''
                        why = result.get('why') or result.get('skipped', '')
                        line = f'    [{color}]{marker}[/{color}] {step_name:<8s} {detail}'
                        if why and not ok:
                            line += f'  [dim]({why})[/dim]'
                        console.print(line)
                    jsonl({
                        'event': 'extract',
                        'instrument': inst_name,
                        'run_id': r['id'],
                        'run_name': r['run_name'],
                        'steps': steps,
                    })

        # Reuse the same QC filter for the audit loop so non-QC samples
        # (real research runs that landed in the DB) aren't graded
        # against the QC schema.
        from stan.watcher.qc_filter import is_qc_file as _is_qc, compile_qc_pattern as _qc_compile
        _qc_pat = _qc_compile()

        for (inst_name,) in insts:
            console.print(f'\n[bold cyan]══ {inst_name} ══[/bold cyan]')
            candidates = con.execute(
                'SELECT * FROM runs WHERE instrument=? ORDER BY run_date DESC LIMIT ?',
                (inst_name, n * 5),
            ).fetchall()
            rows = []
            for r in candidates:
                rn = r['run_name'] or ''
                if _is_qc(Path(rn), _qc_pat):
                    rows.append(r)
                    if len(rows) >= n:
                        break
            if not rows:
                console.print(
                    f'  [yellow]no QC runs found in latest {n*5} rows[/yellow]'
                )
                continue

            inst_summary = {f: 0 for f in ALL_FIELDS}
            inst_total = 0
            inst_thermo = '.raw' in (rows[0]['raw_path'] or '').lower()

            for row in rows:
                d = dict(row)
                run_id = d['id']
                run_name = d['run_name']
                run_date = (d.get('run_date') or '')[:19]
                inst_total += 1
                total_runs += 1

                # Field-presence audit
                missing: list[str] = []
                for f in ALL_FIELDS:
                    # skip drift on Thermo (Bruker-only metric)
                    if inst_thermo and f in REQ_DRIFT:
                        continue
                    if not is_populated(d.get(f), f):
                        missing.append(f)
                        broken_per_field[f] += 1
                        inst_summary[f] += 1

                # Child tables
                tic_n = con.execute(
                    'SELECT COUNT(*) FROM tic_traces WHERE run_id=?', (run_id,),
                ).fetchone()[0]
                drift_n = con.execute(
                    'SELECT COUNT(*) FROM drift_window_centroids WHERE run_id=?',
                    (run_id,),
                ).fetchone()[0]
                peg_n = con.execute(
                    'SELECT COUNT(*) FROM peg_ion_hits WHERE run_id=?', (run_id,),
                ).fetchone()[0]
                irt_n = con.execute(
                    'SELECT COUNT(*) FROM irt_anchor_rts WHERE run_id=?', (run_id,),
                ).fetchone()[0]

                # TIC shape check
                tic_pct = None
                if tic_n:
                    tic_row = con.execute(
                        'SELECT intensity FROM tic_traces WHERE run_id=? LIMIT 1',
                        (run_id,),
                    ).fetchone()
                    if tic_row:
                        try:
                            inten = _json.loads(tic_row[0])
                            tic_pct = sawtooth_pct(inten)
                        except Exception:
                            pass

                # Console block per run
                ok_count = sum(
                    1 for f in ALL_FIELDS
                    if not (inst_thermo and f in REQ_DRIFT)
                    and is_populated(d.get(f), f)
                )
                applicable = (
                    len(ALL_FIELDS) - (len(REQ_DRIFT) if inst_thermo else 0)
                )
                console.print(
                    f'  [bold]{run_name[:55]}[/bold]  ({run_date})'
                )
                console.print(
                    f'    fields populated: {ok_count}/{applicable}  ·  '
                    f'tic={tic_n}{"" if tic_pct is None else f" ({tic_pct:.0f}% noise)"}  '
                    f'drift_pts={drift_n}  peg_hits={peg_n}  irt={irt_n}'
                )
                if missing:
                    console.print(
                        f'    [red]missing:[/red] {", ".join(missing)}'
                    )

                jsonl({
                    'event': 'run',
                    'instrument': inst_name,
                    'run_id': run_id,
                    'run_name': run_name,
                    'run_date': d.get('run_date'),
                    'mode': d.get('mode'),
                    'thermo': inst_thermo,
                    'fields_ok': ok_count,
                    'fields_applicable': applicable,
                    'missing': missing,
                    'children': {
                        'tic_traces': tic_n,
                        'drift_centroids': drift_n,
                        'peg_hits': peg_n,
                        'irt_anchors': irt_n,
                    },
                    'tic_sawtooth_pct': tic_pct,
                })

            # Per-instrument summary
            console.print(f'  [dim]── {inst_name} summary ──[/dim]')
            problem_fields = [
                (f, inst_summary[f]) for f in ALL_FIELDS
                if inst_summary[f] > 0
            ]
            if problem_fields:
                console.print(
                    '    [red]broken in {}/{}:[/red] {}'.format(
                        max(c for _, c in problem_fields), inst_total,
                        ', '.join(f'{f}({c})' for f, c in problem_fields),
                    )
                )
            else:
                console.print('    [green]all required fields populated ✓[/green]')

            jsonl({
                'event': 'instrument_summary',
                'instrument': inst_name,
                'n_runs_tested': inst_total,
                'thermo': inst_thermo,
                'broken_per_field': {
                    f: c for f, c in inst_summary.items() if c > 0
                },
            })

    # Global summary
    console.print('\n[bold yellow]══ GLOBAL ══[/bold yellow]')
    sorted_broken = sorted(
        ((f, c) for f, c in broken_per_field.items() if c > 0),
        key=lambda x: -x[1],
    )
    if sorted_broken:
        console.print(f'  total runs tested: {total_runs}')
        console.print('  fields with at least one failure (worst first):')
        for f, c in sorted_broken:
            console.print(f'    [red]✗[/red] {f:<35s} {c}/{total_runs}')
    else:
        console.print('  [green]every required field populated on every run ✓[/green]')

    jsonl({
        'event': 'end',
        'ts': _dt.utcnow().isoformat() + 'Z',
        'total_runs': total_runs,
        'broken_per_field': {f: c for f, c in broken_per_field.items() if c > 0},
    })
    fh.close()

    console.print(f'\n[dim]log: {log_path}[/dim]')

    # Sync log to Hive so the result is remotely visible.
    try:
        from stan.config import sync_to_hive_mirror
        sync_to_hive_mirror(include_reports=False)
        console.print('[dim]synced to Hive mirror.[/dim]')
    except Exception:
        logger.debug('sync to Hive mirror failed', exc_info=True)


def _parse_version_tuple(v: str | None) -> tuple[int, ...] | None:
    """Parse "0.2.219" -> (0, 2, 219). NULL or unparseable -> None.

    Used for sorting/comparison in stan list-stale. Tolerant of stray
    leading "v" prefixes and short forms like "0.2".
    """
    if not v:
        return None
    s = v.strip().lstrip("v")
    parts = s.split(".")
    out: list[int] = []
    for p in parts:
        # Strip any non-digit suffix (e.g. "0.2.5rc1" -> 0.2.5)
        digits = ""
        for ch in p:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            return None
        out.append(int(digits))
    return tuple(out)


@app.command("list-stale")
def list_stale(
    before: str = typer.Option(
        ...,
        "--before",
        help="Cutoff version. Rows with stan_version NULL or < cutoff "
             "are reported as stale. e.g. --before 1.0.0",
    ),
    instrument: Optional[str] = typer.Option(
        None, "--instrument",
        help="Filter to a specific instrument name.",
    ),
    detail: bool = typer.Option(
        False, "--detail",
        help="Show every stale run_name (default: summary only).",
    ),
) -> None:
    """List rows produced by an old STAN version.

    Useful before the v1.0 community wipe-and-repopulate to see which
    rows still need ``stan backfill-metrics --force`` to refresh their
    extraction. NULL stan_version is always treated as stale.

    Example workflow:

        stan list-stale --before 1.0.0           # quick count
        stan list-stale --before 1.0.0 --detail  # every run_name
        stan backfill-metrics --force            # refresh stale rows
        stan list-stale --before 1.0.0           # confirm zero stale
    """
    import sqlite3
    from collections import Counter
    from stan.db import get_db_path

    cutoff = _parse_version_tuple(before)
    if cutoff is None:
        console.print(f"[red]Could not parse --before {before!r}[/red]")
        raise typer.Exit(2)

    db = get_db_path()
    where = ""
    params: list = []
    if instrument:
        where = " WHERE instrument = ?"
        params.append(instrument)

    with sqlite3.connect(str(db)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT id, instrument, run_name, run_date, stan_version "
            f"FROM runs{where} ORDER BY instrument, run_date DESC",
            params,
        ).fetchall()

    if not rows:
        console.print("No runs in DB.")
        return

    by_instrument: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        v = _parse_version_tuple(r["stan_version"])
        is_stale = v is None or v < cutoff
        if is_stale:
            by_instrument.setdefault(r["instrument"], []).append(r)

    total_stale = sum(len(v) for v in by_instrument.values())
    total_rows = len(rows)
    console.print(
        f"[bold]Stale rows (stan_version < {before} or NULL):[/bold] "
        f"{total_stale} / {total_rows}"
    )

    for inst_name in sorted(by_instrument.keys()):
        stale_rows = by_instrument[inst_name]
        # Tally versions present
        versions = Counter(
            (r["stan_version"] or "NULL") for r in stale_rows
        )
        console.print()
        console.print(f"[cyan]{inst_name}[/cyan]: {len(stale_rows)} stale")
        for v, n in versions.most_common():
            console.print(f"  [dim]{v:<12s}[/dim] {n}")
        if detail:
            for r in stale_rows:
                rd = (r["run_date"] or "")[:19]
                v = r["stan_version"] or "NULL"
                console.print(
                    f"    [dim]{v:<10s}[/dim] {rd}  {r['run_name'][:60]}"
                )

    if total_stale == 0:
        console.print("[green]✓ no stale rows — DB is at or above cutoff[/green]")
    else:
        console.print(
            "\n[dim]Run [bold]stan backfill-metrics --force[/bold] "
            "to refresh stale rows.[/dim]"
        )


@app.command("screencap-now")
def screencap_now(
    out: Optional[Path] = typer.Option(
        None, "--out", help="Override output directory (default: local_dir from screencap.yml)."
    ),
    run_name: Optional[str] = typer.Option(
        None, "--run-name", help="Tag frame as a run-end capture for this run name."
    ),
) -> None:
    """Capture one screenshot now and print the saved path.

    Reads ~/.stan/screencap.yml (or ~/STAN/screencap.yml on Windows).
    Requires enabled: true — edit the config and run
    ``stan screencap-preview`` first to validate mask regions.

    Note: requires an interactive desktop session.
    """
    from stan.screencap import capture_now, load_screencap_config

    cfg = load_screencap_config()

    if not cfg.enabled:
        console.print(
            "[red]screencap is disabled.[/red]\n"
            "Edit [bold]~/.stan/screencap.yml[/bold] and set [bold]enabled: true[/bold].\n"
            "Run [bold]stan screencap-preview[/bold] to validate mask regions first."
        )
        raise typer.Exit(1)

    # Override local_dir if --out provided.
    if out is not None:
        import dataclasses
        cfg = dataclasses.replace(cfg, local_dir=out)

    path = capture_now(cfg, run_name=run_name)
    if path is None:
        console.print("[yellow]Capture skipped (screen locked or no source available).[/yellow]")
        raise typer.Exit(1)
    console.print(f"[green]Saved:[/green] {path}")


@app.command("screencap-daemon")
def screencap_daemon() -> None:
    """Run the screen capture heartbeat daemon (foreground).

    Captures a screenshot every heartbeat_min minutes as configured in
    ~/.stan/screencap.yml (or ~/STAN/screencap.yml on Windows).

    Runs in the foreground. Use start_stan_loop.bat or a supervisor
    script to auto-restart on Windows. On Unix, run under systemd or
    a process manager.

    Note: requires an interactive desktop session — a locked or headless
    screen will produce black frames (skipped automatically).
    """
    from stan.screencap import load_screencap_config, run_daemon

    cfg = load_screencap_config()

    if not cfg.enabled:
        console.print(
            "[red]screencap is disabled.[/red]\n"
            "Edit [bold]~/.stan/screencap.yml[/bold] and set [bold]enabled: true[/bold].\n"
            "Run [bold]stan screencap-preview[/bold] to validate mask regions first."
        )
        raise typer.Exit(1)

    console.print(
        f"[bold]screencap-daemon[/bold] starting — "
        f"heartbeat every [cyan]{cfg.heartbeat_min}[/cyan] min, "
        f"saving to [cyan]{cfg.local_dir}[/cyan]"
    )
    console.print("[dim]Press Ctrl+C to stop.[/dim]")
    try:
        run_daemon(cfg)
    except KeyboardInterrupt:
        console.print("\n[dim]screencap-daemon stopped.[/dim]")


@app.command("screencap-preview")
def screencap_preview(
    mask_only: bool = typer.Option(
        False, "--mask-only",
        help="Draw mask outlines only (no live capture — uses a blank canvas)."
    ),
) -> None:
    """Capture one frame and open it for visual inspection.

    Always runs even if enabled: false — this is a config-validation tool.
    Red outlines are drawn over regions that WOULD be masked. Validate
    that the mask covers personal info before enabling the daemon.

    Opens the preview image with the system default viewer (startfile /
    open / xdg-open).
    """
    import os
    import platform
    import subprocess
    import tempfile
    from stan.screencap import (
        _downsize,
        _grab_fullscreen,
        _grab_window,
        _mss_to_pil,
        load_screencap_config,
    )

    cfg = load_screencap_config()
    # Preview always runs regardless of enabled flag.

    image = None
    if not mask_only:
        # Try window-by-title first, then full screen.
        for title in cfg.window_titles:
            shot = _grab_window(title)
            if shot is not None:
                image = _mss_to_pil(shot)
                console.print(f"Captured window: [cyan]{title}[/cyan]")
                break

        if image is None and cfg.fallback_full_screen:
            shot = _grab_fullscreen()
            if shot is not None:
                image = _mss_to_pil(shot)
                console.print("Captured [cyan]full screen[/cyan]")

    if image is None:
        # --mask-only or capture failed: create a grey canvas.
        try:
            from PIL import Image  # type: ignore[import-untyped]
            image = Image.new("RGB", (1280, 720), color=(128, 128, 128))
            if mask_only:
                console.print("[dim]Using grey canvas (--mask-only).[/dim]")
            else:
                console.print("[yellow]Live capture failed — using grey canvas.[/yellow]")
        except ImportError:
            console.print("[red]Pillow not installed. Run: pip install Pillow[/red]")
            raise typer.Exit(1)

    image = _downsize(image, cfg.max_dimension)

    # Draw red outlines (not solid black) for preview.
    if cfg.mask_regions:
        try:
            from PIL import ImageDraw  # type: ignore[import-untyped]
            draw = ImageDraw.Draw(image)
            for region in cfg.mask_regions:
                try:
                    x = int(region["x"])
                    y = int(region["y"])
                    w = int(region["w"])
                    h = int(region["h"])
                    draw.rectangle((x, y, x + w, y + h), outline="red", width=2)
                except (KeyError, ValueError, TypeError) as exc:
                    console.print(f"[yellow]Skipping invalid mask_region {region!r}: {exc}[/yellow]")
        except ImportError:
            console.print("[yellow]Pillow ImageDraw not available — skipping mask outlines.[/yellow]")
        console.print(
            f"[bold]{len(cfg.mask_regions)}[/bold] mask region(s) shown as [red]red outlines[/red]."
        )
    else:
        console.print("[dim]No mask_regions configured.[/dim]")

    # Save to a temp file and open it.
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".jpg", prefix="stan_preview_", delete=False
        ) as tmp:
            tmp_path = Path(tmp.name)

        image.save(str(tmp_path), format="JPEG", quality=cfg.quality)
        console.print(f"Preview saved to: [cyan]{tmp_path}[/cyan]")

        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(str(tmp_path))  # type: ignore[attr-defined]
            elif system == "Darwin":
                subprocess.run(["open", str(tmp_path)], check=False)
            else:
                subprocess.run(["xdg-open", str(tmp_path)], check=False)
        except Exception as exc:
            console.print(
                f"[yellow]Could not auto-open preview: {exc}[/yellow]\n"
                f"Open manually: {tmp_path}"
            )
    except Exception as exc:
        console.print(f"[red]Failed to save preview: {exc}[/red]")
        raise typer.Exit(1)


@app.command("backup-now")
def backup_now() -> None:
    """Take an immediate atomic snapshot of stan.db to the Hive mirror.

    Useful for troubleshooting when the live mirror DB is corrupt.
    Runs the same sqlite3 online-backup that the automatic sync uses,
    and also writes a timestamped snapshot under <mirror>/backups/.
    """
    from stan.config import sync_to_hive_mirror

    ok = sync_to_hive_mirror(include_reports=False)
    if ok:
        console.print("[green]Snapshot synced.[/green]")
    else:
        console.print("[red]Sync failed (no mirror configured?)[/red]")
        raise typer.Exit(1)


@app.command("hive-process")
def hive_process_cmd(
    raw: Path = typer.Argument(..., help="Path to .d directory or .raw file on Hive."),
    instrument: str = typer.Option(..., "--instrument",
        help="Canonical instrument model name (e.g. 'timsTOF HT', "
             "'Orbitrap Fusion Lumos', 'Orbitrap Exploris 480')."),
    family: str = typer.Option(..., "--family",
        help="Instrument family for IPS cohort key: timsTOF | Lumos | Exploris."),
    db: Path = typer.Option(
        Path("/quobyte/proteomics-grp/STAN/stan.db"), "--db",
        help="Global Hive-resident stan.db. Defaults to the unified DB."),
    out_dir: Path = typer.Option(..., "--out-dir",
        help="Directory for DIA-NN/Sage outputs. Created if absent."),
    vendor: str = typer.Option("", "--vendor",
        help="bruker | thermo. Auto-inferred from raw shape when empty."),
    mode: str = typer.Option("", "--mode",
        help="Force dia/dda. Empty = auto-detect from raw metadata."),
    column_vendor: str = typer.Option("", "--column-vendor"),
    column_model: str = typer.Option("", "--column-model"),
    amount_ng: float = typer.Option(50.0, "--amount-ng",
        help="HeLa injection amount stamp."),
    spd: int = typer.Option(0, "--spd",
        help="Cohort default if metadata-resolve fails. 0 = no override."),
    gradient_min: int = typer.Option(0, "--gradient-min",
        help="Forced gradient length in minutes. 0 = snap from SPD."),
    force: bool = typer.Option(False, "--force",
        help="Re-run even when a completed row already exists."),
    step: str = typer.Option("full", "--step",
        help="Pipeline step to run: full | search | features | "
             "pegdrift | extract. Default 'full' is the all-in-one "
             "sequential job. Per-step modes are used by parallel "
             "SLURM DAG dispatch (each step in its own job)."),
    classification: str = typer.Option("auto", "--classification",
        help="auto | qc | monitor. 'auto' classifies by filename "
             "via the QC pattern; 'monitor' forces the lightweight "
             "sample-health pipeline (used by the monitor sbatch)."),
) -> None:
    """Run the full STAN QC pipeline against ONE raw file on Hive.

    Body of each SLURM job in the Hive-side architecture. With
    ``--step full`` (default), runs detect→search→extract→IPS→
    gates→DB write→TIC→4DFF→PEG/drift sequentially. With other
    --step values, runs only that step (used by parallel DAG mode).

    Per-step semantics:
      search   — DIA-NN/Sage; writes <out_dir>/report.parquet
      features — 4DFF; writes <raw>.features sidecar (Bruker only)
      pegdrift — alphatims PEG + drift; writes peg_result.json +
                 drift_result.json under <out_dir>
      extract  — reads search + pegdrift artifacts, writes runs row
                 + tic_traces + child tables (insertion step)

    Designed for SLURM dispatch by stan hive-dispatch.
    Idempotent: skips raws whose row already exists unless --force.
    """
    import json as _json

    if step == "full":
        from stan.pipeline.hive_process import process_raw
        result = process_raw(
            raw_path=raw,
            instrument=instrument,
            family=family,
            db_path=db,
            out_dir=out_dir,
            vendor=vendor,
            forced_mode=mode,
            column_vendor=column_vendor,
            column_model=column_model,
            hela_amount_ng=amount_ng,
            spd=spd or None,
            gradient_length_min=gradient_min or None,
            force=force,
            classification=classification,
        )
    elif step in ("search", "features", "pegdrift", "extract"):
        from stan.pipeline.hive_steps import (
            step_search, step_features, step_pegdrift, step_extract,
        )
        # Per-step modes don't need the full instrument/family kwargs
        # for search/features/pegdrift — but we accept them for a
        # consistent CLI surface.
        if step == "search":
            actual_vendor = vendor or _vendor_from_raw(raw)
            result = step_search(
                raw_path=raw,
                family=family,
                vendor=actual_vendor,
                out_dir=out_dir,
                forced_mode=mode,
            )
        elif step == "features":
            result = step_features(raw_path=raw)
        elif step == "pegdrift":
            result = step_pegdrift(raw_path=raw, out_dir=out_dir)
        else:  # extract
            actual_vendor = vendor or _vendor_from_raw(raw)
            result = step_extract(
                raw_path=raw,
                instrument=instrument,
                family=family,
                vendor=actual_vendor,
                db_path=db,
                out_dir=out_dir,
                forced_mode=mode,
                column_vendor=column_vendor,
                column_model=column_model,
                hela_amount_ng=amount_ng,
                spd=spd or None,
                gradient_length_min=gradient_min or None,
            )
    else:
        console.print(f"[red]Unknown --step value: {step!r}[/red]")
        raise typer.Exit(2)

    print(_json.dumps(result, default=str), flush=True)
    if result.get("status") not in ("ok", "skipped", "search_empty"):
        raise typer.Exit(1)


def _vendor_from_raw(raw: Path) -> str:
    """Best-effort vendor inference for per-step CLI invocations."""
    if raw.is_dir() and raw.suffix.lower() == ".d":
        return "bruker"
    if raw.is_file() and raw.suffix.lower() == ".raw":
        return "thermo"
    return ""


@app.command("hive-dispatch")
def hive_dispatch_cmd(
    config: Path = typer.Option(
        Path("/quobyte/proteomics-grp/STAN/dispatch.yml"), "--config",
        help="Path to the dispatcher YAML."),
    dry_run: bool = typer.Option(False, "--dry-run",
        help="Report what would be submitted, don't sbatch."),
    instrument: str = typer.Option("", "--instrument",
        help="Substring filter on instrument name (case-insensitive)."),
    print_default_config: bool = typer.Option(False, "--print-default-config",
        help="Write a default dispatch.yml template to stdout and exit."),
    raw: str = typer.Option("", "--raw",
        help="Submit a SLURM job for ONE specific raw on Hive, skipping "
             "the watch-dir walk. Pair with --instrument-name + --family "
             "+ --vendor. Used by the watcher's per-file self-submit and "
             "the partition timing test."),
    instrument_name: str = typer.Option("", "--instrument-name",
        help="Required with --raw: canonical model name (e.g. 'timsTOF HT')."),
    family: str = typer.Option("", "--family",
        help="Required with --raw: timsTOF | Lumos | Exploris."),
    vendor: str = typer.Option("", "--vendor",
        help="Required with --raw: bruker | thermo."),
    column_vendor: str = typer.Option("", "--column-vendor"),
    column_model: str = typer.Option("", "--column-model"),
    partition: str = typer.Option("", "--partition",
        help="Override slurm.partition from dispatch.yml. With 'low' "
             "or 'high', the matching qos+account triple is auto-selected. "
             "Used by partition-comparison timing tests."),
    force: bool = typer.Option(False, "--force",
        help="Bypass the already-processed and already-queued short-"
             "circuits. For timing tests + manual reprocess."),
    parallel: bool = typer.Option(False, "--parallel",
        help="Submit a 4-job DAG (Bruker) or 2-job (Thermo) instead "
             "of one all-in-one job: search/features/pegdrift run in "
             "parallel; extract waits via afterany. Cuts wall time "
             "from sum(steps) to max(steps) — ~30% on Bruker .d. "
             "Only meaningful with --raw."),
    classification: str = typer.Option("auto", "--classification",
        help="Override auto-classification for --raw mode. 'auto' infers "
             "from filename: HeLa/QC pattern → qc (search+extract pipeline), "
             "everything else → monitor (lightweight sample-health pipeline). "
             "'qc' or 'monitor' force the respective sbatch. Default: auto."),
) -> None:
    """Hive-side dispatcher: scan watch dirs OR submit ONE raw.

    Two modes:
      walk  (default): walk each instrument's ``watch_dir``, finds raws
            not yet in the global stan.db, submits one SLURM job per
            raw. Idempotent; safe to re-run. Used for backlog catch-up.
      raw   (--raw): submit a SLURM job for one specific raw already
            on Hive. Used by the watcher's per-file self-submit
            (instrument PC SSHes here after upload) and by the
            time-hive-partitions timing test.

    Bootstrap a config:
      stan hive-dispatch --print-default-config > /quobyte/.../dispatch.yml
    """
    import json as _json
    import sys as _sys
    from stan.community.scripts.dispatch_hive import (
        DEFAULT_CONFIG_TEMPLATE, dispatch_all, dispatch_one_raw,
    )

    if print_default_config:
        _sys.stdout.write(DEFAULT_CONFIG_TEMPLATE)
        return

    if raw:
        if not (instrument_name and family and vendor):
            console.print(
                "[red]ERROR: --raw requires --instrument-name + --family "
                "+ --vendor[/red]"
            )
            raise typer.Exit(2)
        result = dispatch_one_raw(
            raw_path=Path(raw),
            instrument={
                "name": instrument_name,
                "family": family,
                "vendor": vendor,
                "column_vendor": column_vendor,
                "column_model": column_model,
            },
            config_path=config,
            dry_run=dry_run,
            partition=partition,
            force=force,
            parallel=parallel,
            classification=classification,
        )
        # JSONL on stdout for the SSH-invoking caller (watcher,
        # time-hive-partitions) to parse. Plain print, not console.print.
        print(_json.dumps(result, default=str), flush=True)
        if result.get("status") not in ("submitted", "skipped"):
            raise typer.Exit(1)
        return

    summary = dispatch_all(
        config_path=config,
        dry_run=dry_run,
        instrument_filter=instrument,
    )
    totals = summary["totals"]
    console.print(
        f"[{'cyan' if dry_run else 'green'}]"
        f"{'Dry-run' if dry_run else 'Dispatch'}:[/] "
        f"scanned={totals['scanned']} "
        f"submitted={totals['submitted']} "
        f"skipped(processed={totals['skipped_processed']}, "
        f"pattern={totals['skipped_pattern']}, "
        f"in_flight={totals['skipped_in_flight']}, "
        f"max_attempts={totals['skipped_max_attempts']}) "
        f"failed={totals['submit_failed']} "
        f"capped={totals['capped']}"
    )
    if totals["submit_failed"] > 0:
        raise typer.Exit(1)


@app.command("ingest-orphans")
def ingest_orphans_cmd(
    processing_dir: Path = typer.Option(
        Path("/quobyte/proteomics-grp/STAN/processing"), "--processing-dir",
        help="Hive-side processing root with one subdir per raw."),
    sbatch_log_dir: Path = typer.Option(
        Path("/quobyte/proteomics-grp/STAN/logs/sbatch"), "--sbatch-log-dir",
        help="Where the dispatcher staged per-raw sbatch scripts. The "
             "scripts/ subdir is parsed to recover cohort args (instrument, "
             "family, vendor, columns, amount_ng, spd) for each orphan."),
    db: Path = typer.Option(
        Path("/quobyte/proteomics-grp/STAN/stan.db"), "--db",
        help="SQLite stan.db (only used when --backend sqlite)."),
    backend: str = typer.Option("pg", "--backend",
        help="Target DB: 'pg' (PG Farm, default — survives Quobyte "
             "corruption) or 'sqlite' (legacy, requires --db to point "
             "at a healthy file)."),
    dry_run: bool = typer.Option(False, "--dry-run",
        help="Walk + parse but don't call step_extract or write to DB."),
    instrument_filter: str = typer.Option("", "--instrument",
        help="Substring filter on the recovered instrument name."),
    force: bool = typer.Option(False, "--force",
        help="Re-ingest every parquet even when the row already exists "
             "in the target DB. Use after a schema change so the upsert "
             "(ON CONFLICT) refreshes the row with new columns "
             "(e.g. tic_rt_bins added in v0.2.370)."),
    shard: str = typer.Option("", "--shard",
        help="SLURM-array sharding: 'N/M' means this worker processes "
             "stems whose md5(stem) % M == N. Default empty = single "
             "worker handles everything. Use for parallel re-ingest:"
             " 10 workers across the array each set N to their "
             "SLURM_ARRAY_TASK_ID."),
) -> None:
    """Recover orphan parquets: re-extract metrics, insert runs row.

    An "orphan" is a processing/<raw_stem>/ dir that has a valid
    ``report.parquet`` but no row in the central ``runs`` table.
    This is the failure mode the weekend 2026-05-16 SQLite-corruption
    episode left behind: DIA-NN succeeded, the parquet landed, but the
    DB-write step crashed on a corrupted index. With ``--backend pg``
    (the default) inserts go straight to PG Farm so a corrupted Hive
    SQLite doesn't have to be rebuilt first.

    Cohort args (instrument, family, vendor, column_vendor,
    column_model, amount_ng, spd) are recovered by parsing the
    sbatch script under ``<sbatch_log_dir>/scripts/<raw_stem>.sbatch``
    — which the dispatcher already writes for every job.

    Idempotent: short-circuits if a row already exists for this raw.
    Safe to re-run.
    """
    import json as _json
    import os as _os
    import re
    import shlex
    from stan.pipeline.hive_steps import step_extract
    from stan.pipeline.hive_process import _row_exists
    from stan.db_pg import host_origin_from_family

    # Hive→Mac path translations. The sbatch sidecars store paths as the
    # Hive sees them (/nfs/lssc0/flinders/..., /quobyte/proteomics-grp/...),
    # but step_extract calls raw_path.exists() which fails on the Mac
    # where the same data is mounted under /Volumes/. Translate at parse
    # time, falling back to the original when neither exists (so logs
    # still print a sensible "raw not on Hive" error).
    PATH_TRANSLATIONS = [
        ("/nfs/lssc0/flinders/proteomics/", "/Volumes/proteomics/"),
        ("/quobyte/proteomics-grp/", "/Volumes/proteomics-grp/"),
    ]

    def _translate_path(p: Path) -> Path:
        if p.exists():
            return p
        s = str(p)
        for prefix, replacement in PATH_TRANSLATIONS:
            if s.startswith(prefix):
                alt = Path(s.replace(prefix, replacement, 1))
                if alt.exists():
                    return alt
        return p

    backend_l = backend.lower().strip()
    if backend_l not in ("pg", "sqlite"):
        console.print(f"[red]--backend must be pg or sqlite, got {backend!r}[/red]")
        raise typer.Exit(2)
    if backend_l == "pg":
        _os.environ["STAN_DB_BACKEND"] = "pg"
    if force:
        # step_extract checks this env var to skip its internal
        # row_exists short-circuit. Required when re-ingesting after
        # a schema change (e.g. adding tic_rt_bins in v0.2.370).
        _os.environ["STAN_FORCE_REINGEST"] = "1"

    # Bulk-load existing (host_origin, instrument, raw_path) keys once so
    # the per-orphan existence check is in-memory rather than a separate
    # PG round-trip per row. With 2,700+ orphans, per-row connects took
    # >10 min — this drops it to one query + a set lookup. Skipped when
    # --force is set (we're going to UPSERT every row anyway) so each
    # array worker doesn't pull the same 3k rows on startup.
    existing_keys: set[tuple[str, str, str]] = set()
    if backend_l == "pg" and not force:
        from stan.db_pg import _connect
        with _connect() as pg, pg.cursor() as cur:
            cur.execute("SELECT host_origin, instrument, raw_path FROM runs")
            existing_keys = {(h, i, r) for h, i, r in cur.fetchall()}
        console.print(f"[cyan]Loaded {len(existing_keys)} existing rows from PG[/cyan]")

    scripts_dir = sbatch_log_dir / "scripts"
    if not scripts_dir.exists():
        console.print(f"[red]sbatch scripts dir missing: {scripts_dir}[/red]")
        raise typer.Exit(2)
    if not processing_dir.exists():
        console.print(f"[red]processing dir missing: {processing_dir}[/red]")
        raise typer.Exit(2)

    arg_re = re.compile(r"stan\s+hive-process\s+(.*?)$", re.MULTILINE)

    def _parse_sbatch_args(sbatch_path: Path) -> dict | None:
        try:
            text = sbatch_path.read_text()
        except OSError:
            return None
        m = arg_re.search(text)
        if not m:
            return None
        try:
            tokens = shlex.split(m.group(1))
        except ValueError:
            return None
        if not tokens or tokens[0].startswith("--"):
            return None
        # Two paths: the canonical Hive-side path (used for DB storage —
        # must stay stable across Mac vs Hive runs to keep the natural
        # key (host_origin, instrument, run_name, raw_path) consistent),
        # and the translated path for local IO when running on the Mac
        # against /Volumes/ SMB mounts.
        canonical = Path(tokens[0])
        out = {"raw_path": canonical, "raw_path_local": _translate_path(canonical)}
        i = 1
        while i < len(tokens):
            tok = tokens[i]
            if tok.startswith("--") and i + 1 < len(tokens):
                key = tok[2:].replace("-", "_")
                out[key] = tokens[i + 1]
                i += 2
            else:
                i += 1
        return out

    # Shard parsing for parallel re-ingest. Empty = no sharding.
    shard_n: int | None = None
    shard_m: int | None = None
    if shard:
        try:
            shard_n_s, shard_m_s = shard.split("/", 1)
            shard_n = int(shard_n_s)
            shard_m = int(shard_m_s)
            if shard_m <= 0 or not (0 <= shard_n < shard_m):
                raise ValueError(f"shard out of range: {shard}")
            console.print(f"[cyan]Sharding: worker {shard_n} of {shard_m}[/cyan]")
        except Exception as e:
            console.print(f"[red]--shard parse failed ({e}); expected N/M[/red]")
            raise typer.Exit(2)

    import hashlib as _hashlib

    counts = {"inserted": 0, "skipped": 0, "no_sbatch": 0,
              "no_parquet": 0, "filter": 0, "failed": 0}
    total = 0

    for sub in sorted(processing_dir.iterdir()):
        if shard_m is not None:
            # md5 % M == N → this worker owns the stem
            bucket = int(_hashlib.md5(sub.name.encode()).hexdigest()[:8], 16) % shard_m
            if bucket != shard_n:
                continue
        if not sub.is_dir():
            continue
        report = sub / "report.parquet"
        if not report.exists():
            counts["no_parquet"] += 1
            continue
        total += 1
        raw_stem = sub.name
        sbatch_path = scripts_dir / f"{raw_stem}.sbatch"
        if not sbatch_path.exists():
            counts["no_sbatch"] += 1
            logger.warning("no sbatch script for %s — cannot recover args",
                           raw_stem)
            continue
        args = _parse_sbatch_args(sbatch_path)
        if args is None or "raw_path" not in args:
            counts["no_sbatch"] += 1
            logger.warning("sbatch script for %s did not parse", raw_stem)
            continue
        instrument_name = args.get("instrument", "")
        family_name = args.get("family", "")
        if instrument_filter and instrument_filter.lower() not in instrument_name.lower():
            counts["filter"] += 1
            continue
        if backend_l == "pg":
            ho = host_origin_from_family(family_name)
            key = (ho, instrument_name, str(args["raw_path"]))
            exists = key in existing_keys
        else:
            exists = _row_exists(db, instrument_name, args["raw_path"])
        if exists and not force:
            counts["skipped"] += 1
            continue
        if dry_run:
            counts["inserted"] += 1
            continue
        # IO against the local (possibly translated) form, storage with
        # canonical Hive-side path. Prevents the same raw file landing
        # twice in PG just because one run saw it via /Volumes/ and
        # another via /quobyte/.
        _os.environ["STAN_RAW_PATH_CANONICAL"] = str(args["raw_path"])
        try:
            result = step_extract(
                raw_path=args.get("raw_path_local", args["raw_path"]),
                instrument=instrument_name,
                family=args.get("family", ""),
                vendor=args.get("vendor", ""),
                db_path=db,
                out_dir=sub,
                column_vendor=args.get("column_vendor", ""),
                column_model=args.get("column_model", ""),
                hela_amount_ng=float(args.get("amount_ng", 50.0)),
                spd=int(args["spd"]) if args.get("spd") else None,
            )
        except Exception as e:
            counts["failed"] += 1
            logger.warning("step_extract failed for %s: %s", raw_stem, e)
            continue
        if result.get("status") in ("ok", "skipped"):
            counts["inserted"] += 1
        else:
            counts["failed"] += 1
            logger.warning("step_extract returned %s for %s: %s",
                           result.get("status"), raw_stem, result.get("error"))

    summary = {"total_with_parquet": total, **counts}
    print(_json.dumps(summary, indent=2), flush=True)
    if counts["failed"]:
        raise typer.Exit(1)


@app.command("time-hive-partitions")
def time_hive_partitions_cmd(
    instrument: str = typer.Option("", "--instrument",
        help="Substring of instrument name from instruments.yml. "
             "If empty, uses the first instrument entry."),
    raw: Optional[Path] = typer.Option(None, "--raw",
        help="Specific QC raw to time. If omitted, picks the smallest "
             "QC-pattern-matching .d/.raw in the watch_dir so the "
             "test isn't wasteful."),
    partitions: str = typer.Option("low,high", "--partitions",
        help="Comma-separated SLURM partitions to compare."),
    timeout_min: int = typer.Option(120, "--timeout-min",
        help="Stop polling after this many minutes."),
    poll_sec: int = typer.Option(30, "--poll-sec",
        help="Seconds between sacct polls."),
    ssh_key: Optional[Path] = typer.Option(None, "--ssh-key",
        help="SSH private key. Default: %USERPROFILE%/.ssh/id_ed25519."),
) -> None:
    """Time SLURM round trip for one QC raw on each requested partition.

    Picks one QC file from the configured watch_dir (smallest by size
    to keep the test cheap), uploads it to the Hive incoming dir, and
    submits a SLURM job per partition with --force so the dispatcher's
    dedup doesn't short-circuit. Polls sacct over SSH until all jobs
    end, then prints a comparison table.

    Use this once per lab to decide which partition to set as
    slurm.partition in dispatch.yml — `low` is preemptible but huge
    capacity; `high` has stricter caps but priority scheduling.
    """
    import json as _json
    import os
    import re
    import subprocess
    import time as _time
    from stan.config import load_instruments
    from stan.sync.upload_to_hive import (
        upload_raw_to_incoming,
    )

    _hive, insts = load_instruments()
    if not insts:
        console.print("[red]No instruments in instruments.yml[/red]")
        raise typer.Exit(1)

    if instrument:
        match = next(
            (i for i in insts if instrument.lower() in (i.get("name") or "").lower()),
            None,
        )
        if not match:
            console.print(f"[red]No instrument matching '{instrument}'[/red]")
            raise typer.Exit(1)
        inst = match
    else:
        inst = insts[0]
        console.print(
            f"[cyan]No --instrument given; using first entry: {inst.get('name')!r}[/cyan]"
        )

    watch_dir = Path(inst.get("watch_dir") or "")
    if not watch_dir.exists():
        console.print(f"[red]watch_dir missing: {watch_dir}[/red]")
        raise typer.Exit(1)

    if raw is None:
        # Pick smallest QC-pattern-matching candidate
        qc_pat = re.compile(r"(?i)(he(l[a5\d]|\d)|qc|std[_\-\s]?he)")
        candidates: list[tuple[int, Path]] = []
        for child in watch_dir.iterdir():
            if not qc_pat.search(child.name):
                continue
            if child.is_dir() and child.suffix.lower() == ".d":
                size = sum(
                    p.stat().st_size for p in child.rglob("*") if p.is_file()
                )
            elif child.is_file() and child.suffix.lower() == ".raw":
                size = child.stat().st_size
            else:
                continue
            candidates.append((size, child))
        if not candidates:
            console.print(f"[red]No QC files in {watch_dir}[/red]")
            raise typer.Exit(1)
        candidates.sort()
        raw = candidates[0][1]
        console.print(
            f"[cyan]Selected smallest QC file: {raw.name} "
            f"({candidates[0][0]/1e6:.1f} MB)[/cyan]"
        )

    # Upload (or detect already-uploaded). Auto-derive vendor + family
    # from the instrument's `name` if the fields aren't set explicitly
    # — instruments.yml predates these fields and Brett shouldn't have
    # to edit existing configs to opt into the Hive-mode CLI surface.
    from stan.config import resolve_vendor_family
    inst_name = inst.get("name") or "unknown"
    vendor, family = resolve_vendor_family(inst)
    if not (family and vendor):
        console.print(
            f"[red]Could not derive vendor/family from name={inst_name!r}.[/red]\n"
            "Add 'vendor: thermo' (or 'bruker') and 'family: Exploris' "
            "(or Lumos / timsTOF) to instruments.yml."
        )
        raise typer.Exit(1)

    dest_dir = inst.get("hive_upload_dir") or (
        f"Y:/STAN/incoming/{inst_name}"
    )
    console.print(f"[cyan]Uploading {raw.name} -> {dest_dir}[/cyan]")
    up = upload_raw_to_incoming(raw, Path(dest_dir))
    if up["status"] not in ("done", "skipped"):
        console.print(f"[red]Upload failed: {up.get('error')}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]Upload {up['status']}[/green]")

    if ssh_key is None:
        ssh_key = Path(os.path.expanduser("~/.ssh/id_ed25519"))
    if not ssh_key.exists():
        console.print(f"[red]SSH key not found at {ssh_key}[/red]")
        raise typer.Exit(1)

    parts = [p.strip() for p in partitions.split(",") if p.strip()]
    submitted: list[dict] = []
    for part in parts:
        # Pass --force so the dispatcher dedup doesn't skip the second
        # submission. We're explicitly testing both partitions on the
        # same file. Same processing happens both times — what we're
        # measuring is queue-wait + run time per partition.
        console.print(f"\n[cyan]Submitting to partition={part}...[/cyan]")
        cmd = [
            "ssh", "-i", str(ssh_key), "-o", "BatchMode=yes",
            f"{inst.get('hive_user', 'brettsp')}@"
            f"{inst.get('hive_host', 'hive.hpc.ucdavis.edu')}",
            f"{inst.get('hive_venv', '/quobyte/proteomics-grp/brett/stan_venv')}/bin/stan",
            "hive-dispatch",
            "--config",
            inst.get("hive_dispatch_yml", "/quobyte/proteomics-grp/STAN/dispatch.yml"),
            "--raw", _smb_to_quobyte(Path(up["dest"])),
            "--instrument-name", inst_name,
            "--family", family,
            "--vendor", vendor,
            "--partition", part,
            "--force",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            console.print(f"[red]SSH submit failed: {result.stderr.strip()}[/red]")
            continue
        out: dict | None = None
        for line in reversed(result.stdout.strip().splitlines()):
            try:
                out = _json.loads(line)
                break
            except _json.JSONDecodeError:
                continue
        if not out or out.get("status") != "submitted":
            console.print(f"[yellow]No job_id returned for {part}: {out}[/yellow]")
            continue
        console.print(f"[green]{part}: job_id={out.get('job_id')}[/green]")
        submitted.append({
            "partition": part,
            "job_id": out["job_id"],
            "submit_ts": _time.time(),
        })

    if not submitted:
        console.print("[red]No jobs submitted; nothing to time.[/red]")
        raise typer.Exit(1)

    # Poll sacct via SSH until all jobs end.
    job_ids = ",".join(s["job_id"] for s in submitted)
    deadline = _time.time() + (timeout_min * 60)
    finished: dict[str, dict] = {}
    console.print(
        f"\n[cyan]Polling sacct for {len(submitted)} jobs every "
        f"{poll_sec}s (timeout {timeout_min}min)...[/cyan]"
    )
    while _time.time() < deadline and len(finished) < len(submitted):
        sacct_cmd = [
            "ssh", "-i", str(ssh_key), "-o", "BatchMode=yes",
            f"{inst.get('hive_user', 'brettsp')}@"
            f"{inst.get('hive_host', 'hive.hpc.ucdavis.edu')}",
            f"sacct -j {job_ids} -P --format=JobID,Elapsed,State,Start,End "
            "--noheader 2>/dev/null | head -20",
        ]
        r = subprocess.run(sacct_cmd, capture_output=True, text=True, timeout=60)
        for line in r.stdout.strip().splitlines():
            cols = line.split("|")
            if len(cols) < 3:
                continue
            jid_full = cols[0]
            jid = jid_full.split(".")[0]  # strip .batch / .extern suffixes
            if jid in [s["job_id"] for s in submitted] and jid not in finished:
                state = cols[2]
                if state in ("COMPLETED", "FAILED", "CANCELLED",
                             "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL"):
                    finished[jid] = {
                        "elapsed": cols[1],
                        "state": state,
                        "start": cols[3] if len(cols) > 3 else "",
                        "end": cols[4] if len(cols) > 4 else "",
                    }
                    console.print(
                        f"[green]Job {jid} -> {state} after {cols[1]}[/green]"
                    )
        if len(finished) < len(submitted):
            _time.sleep(poll_sec)

    # Print comparison table.
    console.print("\n[bold cyan]Partition latency comparison[/bold cyan]")
    console.print(
        f"{'partition':<10} {'job_id':<10} {'state':<12} {'elapsed':<12}"
    )
    console.print("-" * 50)
    for s in submitted:
        row = finished.get(s["job_id"], {})
        console.print(
            f"{s['partition']:<10} {s['job_id']:<10} "
            f"{row.get('state','PENDING'):<12} {row.get('elapsed','—'):<12}"
        )

    if len(finished) < len(submitted):
        console.print(
            f"[yellow]Timeout reached; {len(submitted) - len(finished)} "
            f"job(s) still running. Check `squeue --me` on Hive.[/yellow]"
        )


def _smb_to_quobyte(p: Path) -> str:
    """Convenience wrapper so the CLI doesn't need to import a private."""
    from stan.sync.upload_to_hive import _smb_to_quobyte_path
    return _smb_to_quobyte_path(p)


@app.command("backfill-from-dir")
def backfill_from_dir_cmd(
    src: Path = typer.Argument(..., help="Local directory containing raws to backfill (e.g. F:\\data\\may26)"),
    instrument: str = typer.Option("", "--instrument",
        help="Instrument substring from instruments.yml. Empty = first entry."),
    qc_only: bool = typer.Option(False, "--qc-only/--no-qc-only",
        help="When True, only upload + submit raws matching the QC "
             "regex. Default False — backfill all files; hive-process "
             "auto-classifies into QC (search → runs table) vs monitor "
             "(rawmeat → sample_health table) at job time."),
    partition: str = typer.Option("low", "--partition",
        help="SLURM partition for the search jobs. low | high."),
    limit: int = typer.Option(0, "--limit",
        help="Stop after N files. 0 = unlimited."),
    dry_run: bool = typer.Option(False, "--dry-run",
        help="Walk + report what would be uploaded/submitted, do nothing."),
) -> None:
    """Upload + submit every raw in a directory to the Hive pipeline.

    Use this to backfill a folder of historical acquisitions. For each
    .raw/.d that matches the QC regex (or all, with --qc-only off):
      1. SMB-uploads to Y:\\STAN\\incoming\\<instrument>\\
      2. SSHes to Hive and submits a SLURM job (stan hive-dispatch --raw)
      3. Records the SLURM job id in the local uploads table

    Designed for the operator to fire from the instrument PC against
    a non-watched data directory. Resumable: re-running skips uploads
    that already landed at matching size on the Hive side.
    """
    import re
    from stan.config import load_instruments, resolve_vendor_family
    from stan.sync.upload_to_hive import (
        upload_raw_to_incoming, submit_one_via_ssh,
    )

    if not src.exists() or not src.is_dir():
        print(f"[red]Source dir not found: {src}[/red]")
        raise typer.Exit(1)

    _hive, insts = load_instruments()
    if not insts:
        print("[red]No instruments in instruments.yml[/red]")
        raise typer.Exit(1)

    if instrument:
        inst = next(
            (i for i in insts if instrument.lower() in (i.get("name") or "").lower()),
            None,
        )
        if inst is None:
            print(f"[red]No instrument matching '{instrument}'[/red]")
            raise typer.Exit(1)
    else:
        inst = insts[0]

    inst_name = inst.get("name") or "unknown"
    vendor, family = resolve_vendor_family(inst)
    if not (vendor and family):
        print(f"ERROR: Could not derive vendor/family for {inst_name!r}.")
        raise typer.Exit(1)

    qc_pattern = re.compile(r"(?i)(he(l[a5\d]|\d)|qc|std[_\-\s]?he)")
    candidates: list[Path] = []
    for child in sorted(src.iterdir()):
        if child.is_dir() and child.suffix.lower() == ".d":
            pass  # Bruker .d directory
        elif child.is_file() and child.suffix.lower() == ".raw":
            pass  # Thermo .raw file
        else:
            continue
        if qc_only and not qc_pattern.search(child.name):
            continue
        candidates.append(child)
        if limit and len(candidates) >= limit:
            break

    print(f"[cyan]Found {len(candidates)} raws in {src}[/cyan]")
    if dry_run:
        for c in candidates:
            print(f"  would process: {c.name}")
        return

    dest_dir = inst.get("hive_upload_dir") or (
        f"Y:/STAN/incoming/{inst_name.strip()}"
    )

    import os as _os
    ssh_key = Path(_os.path.expanduser("~/.ssh/id_ed25519"))
    if not ssh_key.exists():
        print(f"[red]SSH key not found at {ssh_key}[/red]")
        raise typer.Exit(1)

    n_ok = n_skipped = n_failed = 0
    for raw in candidates:
        print(f"\n[cyan]==> {raw.name}[/cyan]")
        up = upload_raw_to_incoming(raw, Path(dest_dir))
        if up.get("status") not in ("done", "skipped"):
            print(f"  [red]upload failed: {up.get('error')}[/red]")
            n_failed += 1
            continue
        print(f"  upload {up['status']}")
        sub = submit_one_via_ssh(
            raw_source=raw,
            raw_dest_smb=Path(up["dest"]),
            instrument=inst_name,
            family=family,
            vendor=vendor,
            ssh_key=ssh_key,
            hive_user=inst.get("hive_user", "brettsp"),
            hive_host=inst.get("hive_host", "hive.hpc.ucdavis.edu"),
            hive_venv=inst.get(
                "hive_venv", "/quobyte/proteomics-grp/brett/stan_venv",
            ),
            column_vendor=inst.get("column_vendor", ""),
            column_model=inst.get("column_model", ""),
        )
        if sub.get("status") == "submitted":
            print(f"  [green]submitted: job_id={sub.get('job_id')}[/green]")
            n_ok += 1
        elif sub.get("status") == "skipped":
            print(f"  [yellow]skipped: {sub.get('error')}[/yellow]")
            n_skipped += 1
        else:
            print(f"  [red]submit failed: {sub.get('error')}[/red]")
            n_failed += 1

    print(f"\nDone. submitted={n_ok} skipped={n_skipped} failed={n_failed}")


@app.command("hive-upload")
def hive_upload_cmd(
    raw: Path = typer.Argument(..., help="Path to .d directory or .raw file to upload."),
    dest_dir: Optional[Path] = typer.Option(None, "--dest-dir",
        help="Override Y:/STAN/incoming/<inst>/. "
             "If omitted, derives from instruments.yml first instrument."),
    instrument: str = typer.Option("", "--instrument",
        help="Instrument substring for dest-dir derivation. "
             "Ignored when --dest-dir is set."),
) -> None:
    """SMB-upload one raw to the Hive incoming dir.

    Standalone test path — does NOT submit a SLURM job. Use to verify
    the SMB write side of the pipeline before the full hive-mode
    watcher rollout, or when the Hive venv isn't bootstrapped yet.

    Default dest matches the watcher's hive-mode behavior:
    Y:/STAN/incoming/<instrument-name>/
    """
    import json as _json
    from stan.config import load_instruments
    from stan.sync.upload_to_hive import upload_raw_to_incoming

    if dest_dir is None:
        _hive, insts = load_instruments()
        if not insts:
            console.print("[red]No instruments in instruments.yml[/red]")
            raise typer.Exit(1)
        if instrument:
            inst = next(
                (i for i in insts if instrument.lower() in (i.get("name") or "").lower()),
                None,
            )
            if not inst:
                console.print(f"[red]No instrument matching '{instrument}'[/red]")
                raise typer.Exit(1)
        else:
            inst = insts[0]
        inst_name = (inst.get("name") or "unknown").strip()
        dest_dir = Path(
            inst.get("hive_upload_dir")
            or f"Y:/STAN/incoming/{inst_name}"
        )

    console.print(f"[cyan]Uploading {raw.name} -> {dest_dir}[/cyan]")
    result = upload_raw_to_incoming(raw, dest_dir)
    console.print(_json.dumps(result, default=str, indent=2))
    if result.get("status") not in ("done", "skipped"):
        raise typer.Exit(1)


# Allow `python -m stan.cli ...` to actually invoke the typer app.
# Without this, the subprocess form (used by remote actions like
# _action_backfill_from_dir) imports the module but never calls
# app() — exits silently with 0 bytes of output. v0.2.332 backfill
# crashed exactly this way 2026-05-08.
if __name__ == "__main__":
    app()
