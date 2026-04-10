"""STAN CLI entry point — ``stan init``, ``stan watch``, ``stan dashboard``."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler

from stan import __version__
from stan.config import get_default_config_dir, get_user_config_dir

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
def baseline() -> None:
    """Build baseline QC data from existing HeLa standard directories.

    Point STAN at a directory of existing .d or .raw files to process them
    retroactively and build historical performance data.
    """
    from stan.baseline import run_baseline

    run_baseline()


@app.command("build-library")
def build_library() -> None:
    """Build instrument-specific spectral library from baseline results.

    Combines all report.parquet files from baseline into a refined library
    with only precursors observed on your instrument. Produces faster
    searches than the community library (30-50K vs 170K precursors).
    """
    from stan.library_builder import run_build_library

    run_build_library()


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


@app.command("backfill-tic")
def backfill_tic() -> None:
    """Extract identified TIC traces from existing baseline reports.

    Reads all report.parquet files in baseline_output/ and extracts
    TIC chromatograms without re-running any searches. Fast — takes
    seconds per file.
    """
    from stan.config import get_user_config_dir
    from stan.db import get_db_path, get_runs, init_db, insert_tic_trace
    from stan.metrics.tic import extract_tic_from_report, compute_tic_metrics

    import sqlite3

    init_db()
    db_path = get_db_path()
    output_dir = get_user_config_dir() / "baseline_output"

    if not output_dir.exists():
        console.print("[red]No baseline_output directory found.[/red]")
        return

    # Find all report.parquet files
    reports = sorted(output_dir.rglob("report.parquet"))
    console.print(f"Found [bold]{len(reports)}[/bold] report.parquet files")

    # Get all runs from DB to match report dirs to run IDs
    all_runs = get_runs(limit=10000, db_path=db_path)
    run_map = {}
    for run in all_runs:
        run_map[run["run_name"]] = run["id"]

    extracted = 0
    skipped = 0
    for rp in reports:
        dir_name = rp.parent.name
        # Match directory name to run_name (with extension)
        run_id = None
        for ext in [".d", ".raw", ""]:
            candidate = dir_name + ext
            if candidate in run_map:
                run_id = run_map[candidate]
                break

        if not run_id:
            skipped += 1
            continue

        # Check if TIC already exists
        with sqlite3.connect(str(db_path)) as con:
            existing = con.execute(
                "SELECT 1 FROM tic_traces WHERE run_id = ?", (run_id,)
            ).fetchone()
        if existing:
            skipped += 1
            continue

        trace = extract_tic_from_report(rp)
        if trace:
            tic_metrics = compute_tic_metrics(trace)
            insert_tic_trace(run_id, trace.rt_min, trace.intensity, db_path=db_path)
            if tic_metrics.total_auc > 0:
                with sqlite3.connect(str(db_path)) as con:
                    con.execute(
                        "UPDATE runs SET tic_auc = ?, peak_rt_min = ? WHERE id = ?",
                        (tic_metrics.total_auc, tic_metrics.peak_rt_min, run_id),
                    )
            extracted += 1
            console.print(f"  [green]TIC[/green] {dir_name}")

    console.print(f"\nExtracted: {extracted}, Skipped: {skipped}")


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
    """
    from stan.watcher.daemon import WatcherDaemon

    console.print(f"[bold]STAN v{__version__}[/bold] — watcher starting")
    console.print()

    daemon = WatcherDaemon()
    try:
        daemon.run()
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down...[/yellow]")
        daemon.stop()


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
