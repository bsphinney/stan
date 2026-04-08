"""Interactive setup wizard — 5 questions, everything else auto-detected.

Usage:
    stan setup

STAN auto-detects instrument model, serial number, vendor, LC system,
gradient length, DIA window size, and DIA/DDA mode directly from raw
files. The setup wizard only asks for things the raw file can't tell us:
  1. Watch directory (where do your raw files land?)
  2. LC column (not embedded in raw file metadata)
  3. HeLa amount (default 50 ng)
  4. Community benchmark (yes/no + pseudonym)
  5. Daily QC email (morning report + optional weekly summary)
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

# Common LC method presets — used by both setup wizard and baseline builder.
# Each entry: name (display label), spd (samples per day), gradient_min (active gradient).
# spd=0 means "custom" — the user will be prompted for gradient length.
LC_METHODS = [
    {"name": "Evosep 60 SPD (Whisper 21 min)", "spd": 60, "gradient_min": 21},
    {"name": "Evosep 100 SPD (11 min)", "spd": 100, "gradient_min": 11},
    {"name": "Evosep 200 SPD (5 min)", "spd": 200, "gradient_min": 5},
    {"name": "Evosep 300 SPD (2.3 min)", "spd": 300, "gradient_min": 2},
    {"name": "Evosep 30 SPD (44 min)", "spd": 30, "gradient_min": 44},
    {"name": "Vanquish Neo / nanoLC 30 min", "spd": 30, "gradient_min": 30},
    {"name": "Vanquish Neo / nanoLC 60 min", "spd": 15, "gradient_min": 60},
    {"name": "Vanquish Neo / nanoLC 90 min", "spd": 10, "gradient_min": 90},
    {"name": "Custom (enter gradient length)", "spd": 0, "gradient_min": None},
]
console = Console()


def run_setup() -> None:
    """Run the interactive setup wizard — 5 questions."""
    console.print()
    console.print(Panel(
        "[bold]STAN Setup[/bold]\n\n"
        "STAN auto-detects your instrument, LC system, gradient, and\n"
        "acquisition mode from raw files. You only need to answer 5 questions.\n\n"
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
        console.print("  [yellow]Directory does not exist yet.[/yellow] STAN will watch it once created.")

    # Try to auto-detect instrument from existing files
    _probe_existing_files(watch_dir)

    # ── 2. LC column ─────────────────────────────────────────────
    console.print()
    console.print("[bold]2. What LC column is installed?[/bold]")
    console.print("  [dim]This is the one thing STAN can't read from raw files.[/dim]")
    column_vendor, column_model = _pick_column()

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
    auth_token = None
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
            # Re-verify ownership via email
            auth_token = _verify_name_ownership(display_name, reclaim=True)
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

            # Claim the name with email verification
            auth_token = _verify_name_ownership(display_name, reclaim=False)

    # ── 5. Email reports ────────────────────────────────────────
    console.print()
    console.print("[bold]5. Daily QC summary email?[/bold]")
    console.print("  [dim]Get a morning report of all instruments at 7 AM.[/dim]")

    email_enabled = Confirm.ask("  Enable daily email report?", default=True, console=console)
    email_address = ""
    email_weekly = False
    if email_enabled:
        email_address = Prompt.ask("  Email address", console=console)
        if email_address and "@" in email_address:
            email_weekly = Confirm.ask(
                "  Also send weekly summary?", default=True, console=console
            )
        else:
            console.print("  [yellow]Invalid email — skipping email reports.[/yellow]")
            email_enabled = False

    if email_enabled and email_address:
        from stan.reports.daily_email import save_email_config
        save_email_config(
            enabled=True,
            to=email_address,
            daily="07:00",
            weekly="monday" if email_weekly else "",
        )
        console.print(f"  [green]Email reports enabled for {email_address}[/green]")

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

        # Normalize watch_dir for comparison (case-insensitive on Windows)
        def _norm_wd(wd: str) -> str:
            return str(Path(wd).resolve()).casefold() if wd else ""

        # Auto-remove duplicate watch_dir entries (from prior bug)
        if existing_instruments:
            seen_dirs: dict[str, int] = {}
            deduped: list[dict] = []
            for ei in existing_instruments:
                wd = _norm_wd(ei.get("watch_dir", ""))
                if wd and wd in seen_dirs:
                    continue  # skip duplicate
                if wd:
                    seen_dirs[wd] = len(deduped)
                deduped.append(ei)
            if len(deduped) < len(existing_instruments):
                removed = len(existing_instruments) - len(deduped)
                console.print(
                    f"  [yellow]Removed {removed} duplicate instrument(s) from config.[/yellow]"
                )
                existing_instruments = deduped
                existing["instruments"] = existing_instruments

        if existing_instruments:
            console.print()
            console.print(
                f"[yellow]instruments.yml already exists with "
                f"{len(existing_instruments)} instrument(s).[/yellow]"
            )
            # Check if this watch_dir is already configured
            dup_idx = None
            new_wd = _norm_wd(watch_dir)
            for idx, ei in enumerate(existing_instruments):
                if _norm_wd(ei.get("watch_dir", "")) == new_wd:
                    dup_idx = idx
                    break

            if dup_idx is not None:
                dup_name = existing_instruments[dup_idx].get("name", "unnamed")
                console.print(
                    f"  [yellow]This directory is already configured as '{dup_name}'.[/yellow]"
                )
                update = Confirm.ask("  Update this instrument's config?", default=True, console=console)
                if update:
                    existing_instruments[dup_idx] = inst_config
                full_config = existing
                full_config["instruments"] = existing_instruments
            else:
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

    # Write display_name + auth token into community.yml
    community_path = user_dir / "community.yml"
    if community and display_name != "Anonymous Lab":
        try:
            comm_config = yaml.safe_load(community_path.read_text()) or {}
        except Exception:
            comm_config = {}
        comm_config["display_name"] = display_name
        if auth_token:
            comm_config["auth_token"] = auth_token
        community_path.write_text(yaml.dump(comm_config, default_flow_style=False, sort_keys=False))
        # Set permissions so only the user can read the token
        community_path.chmod(0o600)
        console.print(f"  [green]Wrote[/green] display_name + auth token to {community_path}")

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
    if email_enabled and email_address:
        table.add_row("Daily email", email_address)
        table.add_row("Weekly summary", "Yes" if email_weekly else "No")
    else:
        table.add_row("Daily email", "[dim]disabled[/dim]")
    table.add_row("Instrument", "[dim]auto-detected from first raw file[/dim]")
    table.add_row("LC system", "[dim]auto-detected from first raw file[/dim]")
    table.add_row("Gradient", "[dim]auto-detected from first raw file[/dim]")
    table.add_row("DIA/DDA mode", "[dim]auto-detected per run[/dim]")
    console.print(table)

    console.print()

    # Offer to build baseline from existing raw files
    has_existing = any(Path(watch_dir).iterdir()) if Path(watch_dir).exists() else False
    if has_existing:
        console.print(
            "[bold]Existing raw files detected.[/bold] "
            "Build a QC baseline from your historical data?"
        )
        console.print(
            "  [dim]This processes past HeLa runs to establish your instrument's baseline.[/dim]"
        )
        build_baseline = Confirm.ask("  Run baseline builder?", default=True, console=console)
        if build_baseline:
            console.print()
            from stan.baseline import run_baseline
            run_baseline()
            console.print()

    # Offer to start the watcher + dashboard right now
    start_now = Confirm.ask(
        "[bold]Start STAN now?[/bold] (watcher + dashboard)",
        default=True,
        console=console,
    )
    if start_now:
        console.print()
        console.print("  Starting dashboard at [cyan]http://localhost:8421[/cyan]")
        console.print("  Starting watcher on [cyan]{watch_dir}[/cyan]")
        console.print("  [dim]Press Ctrl+C to stop both.[/dim]")
        console.print()

        import threading

        # Start dashboard in a background thread
        def _run_dashboard():
            try:
                import uvicorn
                uvicorn.run("stan.dashboard.server:app", host="127.0.0.1", port=8421, log_level="warning")
            except Exception:
                pass

        dash_thread = threading.Thread(target=_run_dashboard, daemon=True)
        dash_thread.start()

        # Open the dashboard in the default browser after a short delay
        def _open_browser():
            import time
            import webbrowser
            time.sleep(2)
            webbrowser.open("http://localhost:8421")

        browser_thread = threading.Thread(target=_open_browser, daemon=True)
        browser_thread.start()

        # Run the watcher in the foreground (blocks until Ctrl+C)
        from stan.watcher.daemon import WatcherDaemon
        daemon = WatcherDaemon()
        try:
            daemon.run()
        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/yellow]")
            daemon.stop()
    else:
        console.print()
        console.print("[bold]To start later:[/bold]")
        console.print("  [cyan]stan watch[/cyan]       — start monitoring")
        console.print("  [cyan]stan dashboard[/cyan]   — open the QC dashboard")
        console.print()
        console.print(
            "  [dim]When the first raw file arrives, STAN auto-detects"
            " instrument, LC, gradient, and windows.[/dim]"
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
                import re
                import subprocess
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


RELAY_URL = "https://brettsp-stan.hf.space"


def _verify_name_ownership(pseudonym: str, reclaim: bool = False) -> str | None:
    """Claim or reclaim a pseudonym via email verification.

    Returns the auth token on success, or None if skipped/failed.

    Privacy statement shown to user: the email is NEVER stored. Only a
    one-way SHA256 hash is kept. STAN cannot de-anonymize participants.
    """
    console.print()
    console.print("  [bold]Email verification[/bold]")
    console.print("  [dim]Your email is used ONLY to verify ownership of this name.[/dim]")
    console.print("  [dim]STAN stores a one-way hash — your email is NEVER saved,[/dim]")
    console.print("  [dim]cannot be recovered, and cannot be used to identify you.[/dim]")
    console.print()

    email = Prompt.ask("  Your email", console=console)
    if not email or "@" not in email:
        console.print("  [yellow]Skipped — you can verify later with: stan verify[/yellow]")
        return None

    # Call the relay to send verification code
    import json
    import urllib.request

    try:
        payload = json.dumps({"pseudonym": pseudonym, "email": email}).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/api/claim-name",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "STAN"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            console.print(f"  [green]{result.get('message', 'Code sent!')}[/green]")
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode()) if e.headers.get("content-type", "").startswith("application/json") else {}
        detail = body.get("detail", str(e))
        console.print(f"  [red]{detail}[/red]")
        return None
    except Exception as e:
        console.print(f"  [red]Could not reach community site: {e}[/red]")
        console.print("  [dim]You can verify later when online.[/dim]")
        return None

    # Prompt for the code
    console.print()
    console.print("  [yellow]Check your inbox (and SPAM/JUNK folder!) for the 6-digit code.[/yellow]")
    console.print("  [dim]The email comes from noreply@stan-proteomics.org[/dim]")
    code = Prompt.ask("  Enter the 6-digit code", console=console)

    if not code or len(code) != 6:
        console.print("  [yellow]Invalid code. You can verify later with: stan verify[/yellow]")
        return None

    # Verify the code
    try:
        payload = json.dumps({"pseudonym": pseudonym, "code": code}).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/api/verify-claim",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "STAN"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            token = result.get("token")
            console.print(f"  [green]Verified! '{pseudonym}' is now yours.[/green]")
            console.print("  [dim]Nobody else can submit under this name without your email.[/dim]")
            console.print("  [dim]To change your verification email, contact bsphinney@ucdavis.edu[/dim]")
            return token
    except urllib.error.HTTPError as e:
        body = json.loads(e.read().decode()) if e.headers.get("content-type", "").startswith("application/json") else {}
        detail = body.get("detail", str(e))
        console.print(f"  [red]{detail}[/red]")
        return None
    except Exception as e:
        console.print(f"  [red]Verification failed: {e}[/red]")
        return None


def _pick_column() -> tuple[str, str]:
    """Show a numbered list of popular LC columns. Returns (vendor, model)."""
    from stan.columns import COLUMN_CATALOG

    # Build flat numbered list grouped by vendor
    all_choices: list[tuple[str, str]] = []
    for vendor, columns in COLUMN_CATALOG.items():
        for col in columns:
            all_choices.append((vendor, col["model"]))

    # Show grouped by vendor with numbers
    i = 1
    for vendor, columns in COLUMN_CATALOG.items():
        console.print(f"  [bold]{vendor}[/bold]")
        for col in columns:
            console.print(f"    [{i:2d}] {col['model']}")
            i += 1
    console.print(f"    [{i:2d}] [dim]Other / custom column[/dim]")
    console.print()

    choices = [str(n) for n in range(1, len(all_choices) + 2)]
    pick = Prompt.ask("  Select column", choices=choices, console=console)
    idx = int(pick) - 1

    if idx >= len(all_choices):
        # Custom
        custom = Prompt.ask("  Describe your column", default="", console=console)
        # Try to parse vendor
        for vendor in COLUMN_CATALOG:
            if vendor.lower() in custom.lower():
                return vendor, custom
        return "", custom

    return all_choices[idx]


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
