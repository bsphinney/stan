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


@app.command()
def baseline() -> None:
    """Build baseline QC data from existing HeLa standard directories.

    Point STAN at a directory of existing .d or .raw files to process them
    retroactively and build historical performance data.
    """
    from stan.baseline import run_baseline

    run_baseline()


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
