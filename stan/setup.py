"""Interactive setup wizard — 4 questions, everything else auto-detected.

Usage:
    stan setup

STAN auto-detects instrument model, serial number, vendor, LC system,
gradient length, DIA window size, and DIA/DDA mode directly from raw
files. The setup wizard only asks for things the raw file can't tell us:
  1. Watch directory (where do your raw files land?)
  2. LC column (not embedded in raw file metadata)
  3. HeLa amount (default 50 ng)
  4. Community benchmark (yes/no + pseudonym)
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, Prompt
from rich.table import Table

from stan.config import get_default_config_dir, get_user_config_dir

logger = logging.getLogger(__name__)
console = Console()


def run_setup() -> None:
    """Run the interactive setup wizard — 4 questions."""
    console.print()
    console.print(Panel(
        "[bold]STAN Setup[/bold]\n\n"
        "STAN auto-detects your instrument, LC system, gradient, and\n"
        "acquisition mode from raw files. You only need to answer 4 questions.\n\n"
        "[dim]DIA-NN license: free academic / commercial license required[/dim]\n"
        "[dim]Sage license: MIT (open source)[/dim]",
        title="STAN — Know Your Instrument",
        border_style="blue",
    ))
    console.print()

    # ── 1. Watch directory ───────────────────────────────────────
    console.print("[bold]1. Where do your raw files land?[/bold]")
    console.print("  [dim]This is the directory your instrument writes .raw or .d files to.[/dim]")
    watch_dir = Prompt.ask("  Watch directory", console=console)
    p = Path(watch_dir)
    if not p.exists():
        console.print(f"  [yellow]Directory does not exist yet.[/yellow] STAN will watch it once created.")

    # Try to auto-detect instrument from existing files
    _probe_existing_files(watch_dir)

    # ── 2. LC column ─────────────────────────────────────────────
    console.print()
    console.print("[bold]2. What LC column is installed?[/bold]")
    console.print("  [dim]This is the one thing STAN can't read from raw files.[/dim]")
    column_desc = Prompt.ask(
        "  Column (e.g. 'PepSep 25cm x 150um', 'IonOpticks Aurora 25cm')",
        default="",
        console=console,
    )

    # Parse vendor from common names
    column_vendor = ""
    column_model = column_desc
    for vendor in ["PepSep", "IonOpticks", "Thermo", "Waters", "Phenomenex", "Agilent"]:
        if vendor.lower() in column_desc.lower():
            column_vendor = vendor
            break

    # ── 3. HeLa amount ───────────────────────────────────────────
    console.print()
    console.print("[bold]3. HeLa injection amount[/bold]")
    amount = FloatPrompt.ask("  Amount (ng)", default=50.0, console=console)

    # ── 4. Community benchmark ───────────────────────────────────
    console.print()
    console.print("[bold]4. Community benchmark[/bold]")
    console.print("  [dim]Compare your instrument against labs worldwide at[/dim]")
    console.print("  [dim]community.stan-proteomics.org — anonymous, no account needed.[/dim]")
    community = Confirm.ask("  Participate?", default=True, console=console)

    display_name = "Anonymous Lab"
    if community:
        from stan.community.pseudonym import generate_unique_pseudonym

        console.print()
        existing_name = Confirm.ask(
            "  Already have a STAN name from another instrument?",
            default=False,
            console=console,
        )

        if existing_name:
            display_name = Prompt.ask(
                "  Enter your existing name",
                console=console,
            )
            console.print(f"  Using: [bold cyan]{display_name}[/bold cyan]")
            console.print(
                "  [dim]Tip: copy ~/.stan/community.yml between machines to skip typing.[/dim]"
            )
        else:
            console.print("  [dim]Checking community site for existing names...[/dim]")
            suggested = generate_unique_pseudonym()
            console.print(
                f"  Your anonymous lab name: [bold cyan]{suggested}[/bold cyan]"
            )
            console.print(
                "  [dim]Only you know which name is yours. Use the same name on all instruments.[/dim]"
            )
            keep = Confirm.ask(
                f"  Use '{suggested}'?",
                default=True,
                console=console,
            )
            if keep:
                display_name = suggested
            else:
                display_name = Prompt.ask(
                    "  Your display name",
                    default=suggested,
                    console=console,
                )

    # ── Check search engines ─────────────────────────────────────
    console.print()
    _check_search_engines()

    # ── Build config ─────────────────────────────────────────────
    # Minimal config — instrument model, vendor, extensions, gradient,
    # SPD, etc. are all filled in automatically when the first raw file
    # is processed by the watcher. The config just needs enough to start
    # watching the directory.
    inst_config: dict = {
        "name": "auto",  # replaced by auto-detected model on first run
        "watch_dir": watch_dir,
        "enabled": True,
        "hela_amount_ng": amount,
        "community_submit": community,
    }

    if column_vendor:
        inst_config["column_vendor"] = column_vendor
    if column_model:
        inst_config["column_model"] = column_model

    full_config = {"instruments": [inst_config]}

    # ── Write config ─────────────────────────────────────────────
    user_dir = get_user_config_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    instruments_path = user_dir / "instruments.yml"

    # If instruments.yml already exists, offer to add or replace
    if instruments_path.exists():
        existing = yaml.safe_load(instruments_path.read_text()) or {}
        existing_instruments = existing.get("instruments", [])
        if existing_instruments:
            console.print()
            console.print(
                f"[yellow]instruments.yml already exists with "
                f"{len(existing_instruments)} instrument(s).[/yellow]"
            )
            add = Confirm.ask("Add this watch directory to existing config?", default=True, console=console)
            if add:
                existing_instruments.append(inst_config)
                full_config = existing
                full_config["instruments"] = existing_instruments

    instruments_path.write_text(yaml.dump(full_config, default_flow_style=False, sort_keys=False))
    console.print(f"\n  [green]Wrote[/green] {instruments_path}")

    # Copy thresholds and community configs if missing
    _copy_if_missing("thresholds.yml", user_dir)
    _copy_if_missing("community.yml", user_dir)

    # Write display_name into community.yml so submissions use it
    community_path = user_dir / "community.yml"
    if community and display_name != "Anonymous Lab":
        try:
            comm_config = yaml.safe_load(community_path.read_text()) or {}
        except Exception:
            comm_config = {}
        comm_config["display_name"] = display_name
        community_path.write_text(yaml.dump(comm_config, default_flow_style=False, sort_keys=False))
        console.print(f"  [green]Wrote[/green] display_name '{display_name}' to {community_path}")

    # ── Summary ──────────────────────────────────────────────────
    console.print()
    table = Table(title="Setup Complete", show_header=False, border_style="blue")
    table.add_column("", style="bold")
    table.add_column("")
    table.add_row("Watch directory", watch_dir)
    table.add_row("LC column", f"{column_vendor} {column_model}".strip() or "(not set)")
    table.add_row("HeLa amount", f"{amount} ng")
    table.add_row("Community", "Yes" if community else "No")
    if community and display_name != "Anonymous Lab":
        table.add_row("Your lab name", f"[bold cyan]{display_name}[/bold cyan]")
    table.add_row("Instrument", "[dim]auto-detected from first raw file[/dim]")
    table.add_row("LC system", "[dim]auto-detected from first raw file[/dim]")
    table.add_row("Gradient", "[dim]auto-detected from first raw file[/dim]")
    table.add_row("DIA/DDA mode", "[dim]auto-detected per run[/dim]")
    console.print(table)

    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  1. Start the watcher:  [cyan]stan watch[/cyan]")
    console.print("  2. Run a HeLa QC on your instrument")
    console.print("  3. STAN auto-detects everything and runs the search")
    console.print("  4. Check results:      [cyan]stan dashboard[/cyan]")
    console.print()
    console.print(
        "  [dim]When the first raw file arrives, STAN will print what it"
        " auto-detected (instrument, LC, gradient, windows).[/dim]"
    )
    console.print()


def _probe_existing_files(watch_dir: str) -> None:
    """If the watch directory already has raw files, peek at one to show
    what STAN can auto-detect. This gives the user immediate confidence
    that the path is correct and STAN can read their files."""
    p = Path(watch_dir)
    if not p.exists():
        return

    # Find the first .raw or .d file
    raw_file = None
    for ext in ["*.raw", "*.d"]:
        matches = list(p.glob(ext))
        if matches:
            raw_file = matches[0]
            break
    # Also check one level deep
    if not raw_file:
        for ext in ["*/*.raw", "*/*.d"]:
            matches = list(p.glob(ext))
            if matches:
                raw_file = matches[0]
                break

    if not raw_file:
        console.print("  [dim]No raw files found yet — will auto-detect when files arrive.[/dim]")
        return

    console.print(f"  [dim]Found {raw_file.name} — probing metadata...[/dim]")

    try:
        if raw_file.suffix.lower() == ".d" and raw_file.is_dir():
            # Bruker — quick TDF read
            from stan.watcher.acquisition_date import _bruker_acquisition_date
            import sqlite3
            tdf = raw_file / "analysis.tdf"
            if tdf.exists():
                with sqlite3.connect(str(tdf)) as con:
                    model = con.execute(
                        "SELECT Value FROM GlobalMetadata WHERE Key='InstrumentName'"
                    ).fetchone()
                    acq = con.execute(
                        "SELECT Value FROM GlobalMetadata WHERE Key='AcquisitionDateTime'"
                    ).fetchone()
                if model:
                    console.print(f"  [green]Instrument:[/green] {model[0]}")
                if acq:
                    console.print(f"  [green]Last acquisition:[/green] {acq[0][:19]}")
        elif raw_file.suffix.lower() == ".raw" and raw_file.is_file():
            # Thermo — try TRFP if available, otherwise binary strings
            try:
                from stan.tools.trfp import extract_metadata
                meta = extract_metadata(raw_file)
                if meta.get("instrument_model"):
                    console.print(f"  [green]Instrument:[/green] {meta['instrument_model']}")
                if meta.get("lc_system"):
                    console.print(f"  [green]LC system:[/green] {meta['lc_system']}")
                if meta.get("gradient_length_min"):
                    console.print(f"  [green]Gradient:[/green] {meta['gradient_length_min']} min")
                if meta.get("dia_isolation_width_th"):
                    console.print(f"  [green]DIA window:[/green] {meta['dia_isolation_width_th']} Th")
                if meta.get("creation_date"):
                    console.print(f"  [green]Acquired:[/green] {meta['creation_date'][:19]}")
            except Exception:
                # TRFP not available yet — try binary string scan
                import subprocess, re
                proc = subprocess.run(
                    ["strings", str(raw_file)],
                    capture_output=True, text=True, timeout=15,
                )
                if proc.returncode == 0:
                    models = re.findall(r'Thermo Scientific instrument model.*?value="([^"]+)"', proc.stdout)
                    if models:
                        console.print(f"  [green]Instrument:[/green] {models[0]}")
    except Exception:
        pass  # Don't block setup on a probe failure


def _check_search_engines() -> None:
    """Check if DIA-NN and Sage are on PATH."""
    console.print("[dim]Checking for search engines...[/dim]")

    diann = shutil.which("diann") or shutil.which("diann.exe") or shutil.which("diann-linux")
    sage = shutil.which("sage") or shutil.which("sage.exe")

    if diann:
        console.print(f"  [green]DIA-NN:[/green] {diann}")
    else:
        console.print(
            "  [yellow]DIA-NN not found.[/yellow] "
            "[dim]Install from github.com/vdemichev/DiaNN/releases[/dim]"
        )

    if sage:
        console.print(f"  [green]Sage:[/green] {sage}")
    else:
        console.print(
            "  [yellow]Sage not found.[/yellow] "
            "[dim]Install from github.com/lazear/sage/releases[/dim]"
        )

    if not diann and not sage:
        console.print(
            "\n  [dim]Install search engines before running stan watch.[/dim]"
        )


def _copy_if_missing(filename: str, user_dir: Path) -> None:
    """Copy a default config file to user dir if it doesn't exist."""
    dst = user_dir / filename
    if dst.exists():
        return
    src = get_default_config_dir() / filename
    if src.exists():
        shutil.copy2(src, dst)
        console.print(f"  [green]Wrote[/green] {dst}")
