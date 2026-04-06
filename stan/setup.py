"""Interactive setup wizard — configures STAN without editing YAML by hand.

Usage:
    stan setup

Walks the user through instrument selection, directory configuration,
LC method, and community benchmark preferences. Writes instruments.yml
to ~/.stan/ when complete.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
from rich.table import Table

from stan.config import get_default_config_dir, get_user_config_dir

logger = logging.getLogger(__name__)
console = Console()

# ── Instrument catalog ──────────────────────────────────────────────

INSTRUMENT_CATALOG: dict[str, list[dict]] = {
    "bruker": [
        {"model": "timsTOF Ultra 2", "extensions": [".d"], "stable_secs": 60},
        {"model": "timsTOF Ultra", "extensions": [".d"], "stable_secs": 60},
        {"model": "timsTOF Pro 2", "extensions": [".d"], "stable_secs": 60},
        {"model": "timsTOF SCP", "extensions": [".d"], "stable_secs": 60},
        {"model": "timsTOF HT", "extensions": [".d"], "stable_secs": 60},
    ],
    "thermo": [
        {"model": "Astral", "extensions": [".raw"], "stable_secs": 30},
        {"model": "Exploris 480", "extensions": [".raw"], "stable_secs": 30},
        {"model": "Exploris 240", "extensions": [".raw"], "stable_secs": 30},
        {"model": "Eclipse", "extensions": [".raw"], "stable_secs": 30},
        {"model": "Fusion Lumos", "extensions": [".raw"], "stable_secs": 30},
    ],
}

# ── LC method catalog ─��─────────────────────────────────────────────

LC_METHODS: list[dict] = [
    {"name": "Evosep 200 SPD", "spd": 200, "gradient_min": 5},
    {"name": "Evosep 100 SPD", "spd": 100, "gradient_min": 11},
    {"name": "Evosep 60 SPD", "spd": 60, "gradient_min": 21},
    {"name": "Evosep Whisper 40 SPD", "spd": 40, "gradient_min": 31},
    {"name": "Evosep 30 SPD", "spd": 30, "gradient_min": 44},
    {"name": "Evosep Extended", "spd": 15, "gradient_min": 88},
    {"name": "Vanquish Neo 40 SPD", "spd": 40, "gradient_min": 25},
    {"name": "Custom gradient", "spd": 0, "gradient_min": 0},
]


def run_setup() -> None:
    """Run the interactive setup wizard."""
    console.print()
    console.print(Panel(
        "[bold]STAN Setup Wizard[/bold]\n\n"
        "This will configure your instrument for QC monitoring.\n"
        "No YAML editing required.\n\n"
        "[yellow]Note:[/yellow] STAN calls DIA-NN and Sage as external tools.\n"
        "You must install them separately under their own licenses:\n"
        "  [dim]DIA-NN: free academic / commercial license required[/dim]\n"
        "  [dim]Sage: MIT (open source)[/dim]",
        title="STAN",
        border_style="blue",
    ))
    console.print()

    # ── 1. Vendor ────────────────────────────────────────────────
    vendor = _pick_vendor()

    # ── 2. Instrument model ──────────────────────────────────────
    instrument = _pick_instrument(vendor)

    # ── 3. Directories ─���─────────────────────────────────────────
    watch_dir = _ask_directory("Raw data directory (where instrument writes files)")
    output_dir = _ask_directory("Output directory (where STAN writes results)")

    # ── 4. LC method ─────────────────────────────────────────────
    lc = _pick_lc_method()

    # ── 5. HeLa amount ───────────────────────────────────────────
    amount = FloatPrompt.ask(
        "HeLa injection amount (ng)", default=50.0, console=console
    )

    # ── 6. FASTA path ────────────────────────────────────────────
    console.print()
    console.print(
        "STAN needs a FASTA database for searching.\n"
        "For HeLa QC, use a reviewed human proteome from UniProt."
    )
    fasta_path = Prompt.ask("Path to FASTA file", console=console)
    if fasta_path and not Path(fasta_path).exists():
        console.print(f"  [yellow]Warning:[/yellow] File not found: {fasta_path}")
        console.print("  You can update this later in ~/.stan/instruments.yml")

    # ── 7. LC column ─────────────────────────────────────────────
    column = _pick_column()

    # ── 8. Search engine detection ───────────────────────────────
    console.print()
    _check_search_engines()

    # ── 9. Community benchmark ───────────────────────────────────
    console.print()
    community = Confirm.ask(
        "Participate in the community HeLa benchmark? (anonymous, no account needed)",
        default=False,
        console=console,
    )

    display_name = "Anonymous Lab"
    if community:
        from stan.community.pseudonym import generate_pseudonym

        suggested = generate_pseudonym()
        console.print()
        console.print(
            f"  Your anonymous lab name: [bold cyan]{suggested}[/bold cyan]"
        )
        console.print(
            "  This is how your submissions appear on the community leaderboard."
        )
        console.print(
            "  Only [bold]you[/bold] know which name is yours. You can change it anytime in ~/.stan/community.yml."
        )
        keep = Confirm.ask(
            f"  Use '{suggested}'? (or enter your own)",
            default=True,
            console=console,
        )
        if keep:
            display_name = suggested
        else:
            display_name = Prompt.ask(
                "  Your display name (real or made up)",
                default=suggested,
                console=console,
            )

    # ── 10. Instrument name ──────────────────────────────────────
    name = Prompt.ask(
        "Give this instrument a name",
        default=instrument["model"],
        console=console,
    )

    # ── Build config ─────────────────────────────────────────────
    inst_config: dict = {
        "name": name,
        "vendor": vendor,
        "model": instrument["model"],
        "watch_dir": watch_dir,
        "output_dir": output_dir,
        "extensions": instrument["extensions"],
        "stable_secs": instrument["stable_secs"],
        "enabled": True,
        "hela_amount_ng": amount,
        "spd": lc["spd"] if lc["spd"] > 0 else None,
        "fasta_path": fasta_path or None,
        "column_vendor": column.get("vendor") if column.get("vendor") != "other" else None,
        "column_model": column.get("model") if column.get("model") != "custom" else None,
        "community_submit": community,
    }

    if lc["gradient_min"] > 0:
        inst_config["gradient_length_min"] = lc["gradient_min"]

    # Clean None values
    inst_config = {k: v for k, v in inst_config.items() if v is not None}

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
            add = Confirm.ask("Add this instrument to existing config?", default=True, console=console)
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
    table = Table(title="Configuration Summary", show_header=False, border_style="blue")
    table.add_column("Field", style="bold")
    table.add_column("Value")
    table.add_row("Instrument", f"{name} ({vendor})")
    table.add_row("Watch directory", watch_dir)
    table.add_row("Output directory", output_dir)
    table.add_row("LC method", lc["name"])
    table.add_row("HeLa amount", f"{amount} ng")
    table.add_row("LC Column", f"{column.get('vendor', '')} {column.get('model', '')}".strip() or "(not set)")
    table.add_row("FASTA", fasta_path or "(not set)")
    table.add_row("Community benchmark", "Yes" if community else "No")
    if community and display_name != "Anonymous Lab":
        table.add_row("Your lab name", f"[bold cyan]{display_name}[/bold cyan]")
    console.print(table)

    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print("  1. Start the watcher:  [cyan]stan watch[/cyan]")
    console.print("  2. Run a HeLa QC acquisition on your instrument")
    console.print("  3. STAN will auto-detect, search, and evaluate it")
    console.print("  4. Check results:      [cyan]stan status[/cyan]")
    console.print("  5. View dashboard:     [cyan]stan dashboard[/cyan]")
    console.print()


def _pick_vendor() -> str:
    """Prompt user to pick instrument vendor."""
    console.print("[bold]Step 1: Instrument vendor[/bold]")
    console.print("  [1] Bruker (timsTOF)")
    console.print("  [2] Thermo (Orbitrap)")
    choice = Prompt.ask("Select vendor", choices=["1", "2"], console=console)
    return "bruker" if choice == "1" else "thermo"


def _pick_instrument(vendor: str) -> dict:
    """Prompt user to pick instrument model."""
    models = INSTRUMENT_CATALOG[vendor]
    console.print()
    console.print(f"[bold]Step 2: {vendor.title()} instrument model[/bold]")
    for i, m in enumerate(models, 1):
        console.print(f"  [{i}] {m['model']}")
    choices = [str(i) for i in range(1, len(models) + 1)]
    choice = Prompt.ask("Select model", choices=choices, console=console)
    return models[int(choice) - 1]


def _ask_directory(label: str) -> str:
    """Prompt for a directory path."""
    console.print()
    console.print(f"[bold]{label}[/bold]")
    path = Prompt.ask("Path", console=console)
    p = Path(path)
    if not p.exists():
        console.print(f"  [yellow]Directory does not exist yet.[/yellow] STAN will watch it once created.")
    return path


def _pick_lc_method() -> dict:
    """Prompt user to pick LC method."""
    console.print()
    console.print("[bold]Step 4: LC method[/bold]")
    for i, lc in enumerate(LC_METHODS, 1):
        if lc["spd"] > 0:
            console.print(f"  [{i}] {lc['name']} (~{lc['gradient_min']} min gradient)")
        else:
            console.print(f"  [{i}] {lc['name']}")
    choices = [str(i) for i in range(1, len(LC_METHODS) + 1)]
    choice = Prompt.ask("Select method", choices=choices, console=console)
    method = LC_METHODS[int(choice) - 1]

    if method["spd"] == 0:
        # Custom gradient
        gradient = IntPrompt.ask("Active gradient length (minutes)", console=console)
        from stan.metrics.scoring import gradient_min_to_spd
        estimated_spd = gradient_min_to_spd(gradient)
        method = {"name": f"Custom ({gradient} min)", "spd": estimated_spd, "gradient_min": gradient}
        console.print(f"  Estimated throughput: ~{estimated_spd} SPD")

    return method


def _pick_column() -> dict:
    """Prompt user to pick their LC column."""
    from stan.columns import COLUMN_CATALOG, parse_column_choice

    console.print()
    console.print("[bold]Step 7: LC column[/bold]")

    # Show vendors first
    vendors = list(COLUMN_CATALOG.keys()) + ["Other"]
    for i, v in enumerate(vendors, 1):
        console.print(f"  [{i}] {v}")
    v_choice = Prompt.ask("Select column vendor", choices=[str(i) for i in range(1, len(vendors) + 1)], console=console)
    vendor = vendors[int(v_choice) - 1]

    if vendor == "Other":
        custom = Prompt.ask("Column description (e.g. 'In-house packed 25cm C18')", default="", console=console)
        return {"vendor": "other", "model": custom or "custom"}

    # Show columns for selected vendor
    columns = COLUMN_CATALOG[vendor]
    console.print()
    for i, col in enumerate(columns, 1):
        console.print(f"  [{i}] {col['model']}")
    console.print(f"  [{len(columns) + 1}] Other {vendor} column")

    choices = [str(i) for i in range(1, len(columns) + 2)]
    c_choice = Prompt.ask("Select column", choices=choices, console=console)
    idx = int(c_choice) - 1

    if idx >= len(columns):
        custom = Prompt.ask("Column description", default="", console=console)
        return {"vendor": vendor, "model": custom or "custom"}

    return {"vendor": vendor, "model": columns[idx]["model"]}


def _check_search_engines() -> None:
    """Check if DIA-NN and Sage are on PATH."""
    console.print("[bold]Search engine detection[/bold]")

    diann = shutil.which("diann") or shutil.which("diann.exe") or shutil.which("diann-linux")
    sage = shutil.which("sage") or shutil.which("sage.exe")

    if diann:
        console.print(f"  [green]DIA-NN found:[/green] {diann}")
    else:
        console.print(
            "  [yellow]DIA-NN not found on PATH.[/yellow] "
            "Install from https://github.com/vdemichev/DiaNN/releases"
        )

    if sage:
        console.print(f"  [green]Sage found:[/green] {sage}")
    else:
        console.print(
            "  [yellow]Sage not found on PATH.[/yellow] "
            "Install from https://github.com/lazear/sage/releases"
        )

    if not diann or not sage:
        console.print(
            "\n  You can still complete setup. Install the search engines before running [cyan]stan watch[/cyan]."
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
