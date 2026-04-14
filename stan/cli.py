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
) -> tuple[int, int, int]:
    """Core backfill logic shared by the CLI command and the baseline
    startup sweep.

    Finds runs in the local DB that are missing TIC traces OR have
    zero peptide/protein counts despite having a report.parquet in
    baseline_output. Repairs both in one pass:

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

    # Runs that need TIC or have zero peptides (or both)
    missing_tic = [r for r in all_runs if r["id"] not in have_tic]
    missing_pep = [r for r in all_runs
                   if r["id"] in have_tic  # already has TIC
                   and (not r.get("n_peptides") or r["n_peptides"] == 0)
                   and r.get("n_precursors", 0) > 0]  # has search results

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
    n_need_pep = len([r for r in missing if (not r.get("n_peptides") or r["n_peptides"] == 0) and r.get("n_precursors", 0) > 0])
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

    for run in missing:
        run_id = run["id"]
        run_name = run.get("run_name", "")
        raw_path_str = run.get("raw_path", "") or ""
        raw_path = Path(raw_path_str) if raw_path_str else None

        trace = None

        # 1. Try Bruker .d raw TIC
        if raw_path and raw_path.exists() and raw_path.suffix.lower() == ".d":
            try:
                trace = extract_tic_bruker(raw_path)
            except Exception:
                logger.debug("extract_tic_bruker failed for %s", raw_path, exc_info=True)

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
                except Exception:
                    logger.debug("extract_tic_from_report failed for %s", report_path, exc_info=True)

        # 3. Try Thermo .raw via fisher_py
        if trace is None and raw_path and raw_path.exists() and raw_path.suffix.lower() == ".raw":
            try:
                trace = extract_tic_thermo(raw_path)
            except Exception:
                logger.debug("extract_tic_thermo failed for %s", raw_path, exc_info=True)

        if trace is None:
            failed += 1
            if verbose:
                console.print(f"  [red]no source[/red] {run_name}")
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
        if (not run.get("n_peptides") or run["n_peptides"] == 0) and run.get("n_precursors", 0) > 0:
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

    if verbose:
        console.print(
            f"\n[bold]Extracted:[/bold] {extracted}  "
            f"[bold]Failed:[/bold] {failed}  "
            f"[bold]Skipped:[/bold] {skipped}"
        )

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
            except Exception:
                logger.exception("Relay TIC push failed for %s", sub_id[:8])
        console.print(f"  [green]{ok}[/green] pushed, [red]{len(pushed_rows) - ok}[/red] failed")

    return (extracted, skipped, failed)


@app.command("backfill-tic")
def backfill_tic(
    push: bool = typer.Option(
        False, "--push",
        help="Also push extracted TIC traces to the community relay for "
             "runs that were already submitted.",
    ),
) -> None:
    """Re-extract TIC traces for runs that are missing one.

    Walks the local runs table and, for each run without a TIC, tries
    (in order) the raw Bruker analysis.tdf, the DIA-NN report.parquet,
    and fisher_py for Thermo .raw. All traces are downsampled to 128
    bins before storage. Fast — seconds per file.

    With ``--push``, also uploads corrections to the community benchmark
    relay via /api/update/{submission_id} for runs that already have a
    submission_id.
    """
    _backfill_tic_impl(push=push, verbose=True)


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
