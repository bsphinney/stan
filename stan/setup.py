"""Interactive setup wizard — 6 questions, everything else auto-detected.

Usage:
    stan setup

STAN auto-detects instrument model, serial number, LC system, gradient
length, DIA window size, and DIA/DDA mode directly from raw files. The
setup wizard only asks for things the raw file can't tell us:
  1. Watch directory (where do your raw files land?) — plus the vendor
     when the folder's contents can't tell, the instrument name and the
     QC filename filter
  2. LC column (not embedded in raw file metadata)
  3. HeLa amount (default 50 ng)
  4. Community benchmark (yes/no + pseudonym)
  5. Daily QC email (morning report + optional weekly summary)
  6. Error reports

This module also holds the instrument-block helpers that ``stan setup``
and ``stan add-watch`` share, and the minimal config files ``stan init``
creates. There is one definition of "a block the watcher can use": the
watcher ignores a block without ``enabled: true`` and every file whose
suffix is not in ``extensions``, so both commands must write the same keys.
"""

from __future__ import annotations

import logging
import os
import platform
import re
import shutil
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, Prompt
from rich.table import Table

from stan.config import get_user_config_dir, read_config_text

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
    """Run the interactive setup wizard — 6 questions."""
    console.print()
    console.print(Panel(
        "[bold]STAN Setup[/bold]\n\n"
        "STAN auto-detects your instrument, LC system, gradient, and\n"
        "acquisition mode from raw files. You only need to answer 6 questions.\n\n"
        "[dim]DIA-NN license: free academic / commercial license required[/dim]\n"
        "[dim]Sage license: MIT (open source)[/dim]",
        title="STAN — Know Your Instrument",
        border_style="blue",
    ))
    console.print()

    # ── 1. Watch directory ───────────────────────────────────────
    console.print("[bold]1. Where do your raw files land?[/bold]")
    console.print("  [dim]This is the directory your instrument writes .raw or .d files to.[/dim]")
    watch_dir = ""
    while not watch_dir:
        watch_dir = Prompt.ask("  Watch directory", console=console).strip()
    watch_path = Path(watch_dir).expanduser()
    if not watch_path.exists():
        console.print("  [yellow]Directory does not exist yet.[/yellow] STAN will watch it once created.")

    # Try to auto-detect instrument from existing files
    detected_model = _probe_existing_files(str(watch_path))

    # The watcher needs these three to process anything; see watcher_block().
    existing_block = find_instrument_block(watch_path)
    vendor = _ask_vendor(watch_path, fallback=(existing_block or {}).get("vendor"))
    name = _ask_instrument_name(watch_path, vendor, detected_model, existing_block)
    qc_only, qc_pattern = prompt_qc_filter(watch_path, vendor, console)

    # ── 2. LC column ─────────────────────────────────────────────
    console.print()
    console.print("[bold]2. What LC column is installed?[/bold]")
    console.print("  [dim]This is the one thing STAN can't read from raw files.[/dim]")
    column_vendor, column_model = _pick_column()
    lc_flow = _pick_lc_flow((existing_block or {}).get("lc_flow"))

    # ── 3. HeLa amount ───────────────────────────────────────────
    console.print()
    console.print("[bold]3. HeLa injection amount[/bold]")
    console.print("  [dim]Your usual amount. A file name that states one (50ng, 1ug) wins.[/dim]")
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
            while True:
                display_name = Prompt.ask(
                    "  Enter your existing name",
                    console=console,
                )
                if len(display_name.strip()) >= 3:
                    break
                console.print(
                    "  [yellow]Name must be at least 3 characters. "
                    "Try again.[/yellow]"
                )
            console.print(f"  Using: [bold cyan]{display_name}[/bold cyan]")
            # Re-verify ownership via email
            auth_token = _verify_name_ownership(display_name, reclaim=True)
        else:
            console.print("  [dim]Generating your anonymous lab name...[/dim]")
            display_name = generate_unique_pseudonym()
            console.print(
                f"\n  Your lab name: [bold cyan]{display_name}[/bold cyan]\n"
            )
            console.print(
                "  [dim]This is your permanent anonymous identity on the "
                "community benchmark. Use the same name on all your "
                "instruments so your data stays together.[/dim]"
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
        try:
            _promote_legacy_config("community.yml")  # or this would shadow it
        except OSError as e:
            console.print(f"  [yellow]Could not copy the legacy community.yml: {e}[/yellow]")
        try:
            save_email_settings(
                enabled=True,
                to=email_address,
                daily="07:00",
                weekly="monday" if email_weekly else "",
            )
            console.print(f"  [green]Email reports enabled for {email_address}[/green]")
        except (OSError, ValueError, yaml.YAMLError) as e:
            # write_community_keys refuses a community.yml it cannot parse
            # rather than replace it (and its auth_token) with email_reports.
            console.print(
                f"  [yellow]Email reports not saved: {e}. Fix community.yml, "
                "then run `stan email-report --enable --to ADDRESS`.[/yellow]"
            )

    # ── 6. Error telemetry ──────────────────────────────────────
    console.print()
    console.print("[bold]6. Help improve STAN?[/bold]")
    console.print("  [dim]Send error reports to the STAN developers so we can fix common issues.[/dim]")
    console.print("  [dim]A report holds the error message (which can include file paths), the raw[/dim]")
    console.print("  [dim]file's name, and your STAN, Python and OS versions. No patient data.[/dim]")
    error_telemetry = Confirm.ask("  Enable error reporting?", default=False, console=console)

    # ── Check search engines ─────────────────────────────────────
    console.print()
    _check_search_engines()

    # ── Build config ─────────────────────────────────────────────
    # A block the watcher can run: vendor, extensions, stable_secs and
    # enabled are what it filters on. Model, gradient, SPD etc. are still
    # read from each raw file as it is processed.
    inst_config = watcher_block(
        name, vendor, watch_path, qc_only=qc_only, qc_pattern=qc_pattern,
    )
    inst_config["hela_amount_ng"] = amount
    if column_vendor:
        inst_config["column_vendor"] = column_vendor
    if column_model:
        inst_config["column_model"] = column_model
    if lc_flow:
        inst_config["lc_flow"] = lc_flow
    # None removes the key from a block being updated: the default QC
    # pattern replaces a custom one, and community_submit lives in
    # community.yml now (an older setup wrote an ignored copy here).
    inst_config["qc_pattern"] = qc_pattern
    inst_config["community_submit"] = None

    # ── Write config ─────────────────────────────────────────────
    ensure_default_configs()  # thresholds.yml etc. when absent; never overwrites
    instruments_path = instruments_config_path()
    write_block = True
    try:
        n_existing = len(_read_instruments_doc(instruments_path).get("instruments") or [])
    except (OSError, ValueError, yaml.YAMLError) as e:
        console.print(f"  [red]Cannot read {instruments_path}: {e}[/red]")
        _print_block_by_hand(inst_config)
        n_existing, write_block, existing_block = 0, False, None

    if not write_block:
        pass
    elif existing_block is not None:
        console.print()
        console.print(
            f"  [yellow]This directory is already configured as "
            f"'{existing_block.get('name', 'unnamed')}'.[/yellow]"
        )
        write_block = Confirm.ask("  Update this instrument's config?", default=True, console=console)
    elif n_existing:
        console.print()
        console.print(
            f"[yellow]instruments.yml already exists with {n_existing} instrument(s).[/yellow]"
        )
        write_block = Confirm.ask(
            "Add this watch directory to existing config?", default=True, console=console,
        )

    if write_block:
        read_path = instruments_path
        try:
            action, inst_config, instruments_path = upsert_instrument_block(
                inst_config, config_path=instruments_path, drop_duplicates=True,
            )
        except InstrumentNameClash as e:  # another setup/add-watch ran in between
            console.print(f"  [red]{e}[/red] instruments.yml was not changed.")
        except (OSError, ValueError, yaml.YAMLError) as e:
            console.print(f"  [red]Could not update {instruments_path}: {e}[/red]")
            _print_block_by_hand(inst_config)
        else:
            if action == "unchanged":
                console.print(f"\n  {instruments_path} already has this block ('{inst_config['name']}').")
            else:
                console.print(f"\n  [green]Wrote[/green] {instruments_path} ({action} '{inst_config['name']}')")
            for note in config_write_notes(read_path, instruments_path, existing_block, inst_config):
                console.print(f"  [yellow]{note}[/yellow]", soft_wrap=True)
    else:
        console.print(f"  [dim]{instruments_path} left unchanged.[/dim]")

    # community_submit belongs in community.yml: `stan submit-all`, `stan
    # verify` and the arcade read it there, never from instruments.yml.
    comm_updates: dict = {
        "community_submit": community,
        "error_telemetry": error_telemetry,
    }
    if community and display_name != "Anonymous Lab":
        comm_updates["display_name"] = display_name
        if auth_token:
            comm_updates["auth_token"] = auth_token
    _write_community_answers(comm_updates)

    # ── Summary ──────────────────────────────────────────────────
    console.print()
    table = Table(title="Setup Complete", show_header=False, border_style="blue")
    table.add_column("", style="bold")
    table.add_column("")
    table.add_row("Watch directory", str(inst_config.get("watch_dir", watch_path)))
    shown_name = str(inst_config.get("name", name))
    if shown_name.lower() in _PLACEHOLDER_NAMES:
        shown_name += " [dim](read from the first raw file)[/dim]"
    table.add_row("Instrument name", shown_name)
    table.add_row("Vendor", f"{vendor} ({', '.join(inst_config.get('extensions', []))})")
    if not inst_config.get("qc_only", True):
        table.add_row("QC filter", "none — every file is searched")
    else:
        table.add_row("QC filter", inst_config.get("qc_pattern") or "default HeLa/QC pattern")
    table.add_row("Results folder", str(inst_config.get("output_dir", "")))
    table.add_row("LC column", f"{column_vendor} {column_model}".strip() or "(not set)")
    table.add_row("LC flow", inst_config.get("lc_flow") or "(not set)")
    table.add_row("HeLa amount", f"{amount} ng")
    table.add_row("Community", "Yes" if community else "No")
    if community and display_name != "Anonymous Lab":
        table.add_row("Your lab name", f"[bold cyan]{display_name}[/bold cyan]")
    if email_enabled and email_address:
        table.add_row("Daily email", email_address)
        table.add_row("Weekly summary", "Yes" if email_weekly else "No")
    else:
        table.add_row("Daily email", "[dim]disabled[/dim]")
    table.add_row("Error telemetry", "Yes" if error_telemetry else "No")
    table.add_row("LC system", "[dim]auto-detected from first raw file[/dim]")
    table.add_row("Gradient", "[dim]auto-detected from first raw file[/dim]")
    table.add_row("DIA/DDA mode", "[dim]auto-detected per run[/dim]")
    console.print(table)

    console.print()

    # Offer to build baseline from existing raw files
    has_existing = any(watch_path.iterdir()) if watch_path.is_dir() else False
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

            # v0.2.230: after a successful baseline, auto-run
            # `stan test --n 5` so the operator gets immediate
            # confirmation that every metadata field expected by the
            # v1.0 community schema is actually populated. If the
            # audit comes back red, the operator finds out NOW
            # rather than during their first community submission.
            console.print()
            console.print("[bold]Verifying setup with stan test on the baselined runs...[/bold]")
            try:
                import sqlite3
                from stan.db import get_db_path
                with sqlite3.connect(str(get_db_path())) as _con:
                    n_runs = _con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                if n_runs == 0:
                    console.print(
                        "[yellow]No runs in the DB yet — skipping verification. "
                        "Re-run [cyan]stan test --n 5[/cyan] after the first "
                        "live ingest.[/yellow]"
                    )
                else:
                    from stan.cli import test_latest_runs as _test_cmd
                    _test_cmd(n=5, instrument=None, extract=False)
            except Exception as _e:
                console.print(
                    f"[yellow]Verification skipped ({type(_e).__name__}). "
                    f"Run [cyan]stan test --n 5[/cyan] manually.[/yellow]"
                )
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
        console.print(f"  Starting watcher on [cyan]{watch_path}[/cyan]")
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


def _probe_existing_files(watch_dir: str) -> str | None:
    """If the watch directory already has raw files, peek at one to show
    what STAN can auto-detect. This gives the user immediate confidence
    that the path is correct and STAN can read their files.

    Returns:
        The instrument model read from the file (e.g. "timsTOF HT"), or
        None when there is no raw file yet or its metadata can't be read.
    """
    p = Path(watch_dir)
    if not p.exists():
        return None

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
        return None

    console.print(f"  [dim]Found {raw_file.name} — probing metadata...[/dim]")

    found_model: str | None = None
    try:
        if raw_file.suffix.lower() == ".d" and raw_file.is_dir():
            # Bruker — quick TDF read
            from stan.tdf import connect_tdf
            tdf = raw_file / "analysis.tdf"
            if tdf.exists():
                with connect_tdf(tdf) as con:
                    model = con.execute(
                        "SELECT Value FROM GlobalMetadata WHERE Key='InstrumentName'"
                    ).fetchone()
                    acq = con.execute(
                        "SELECT Value FROM GlobalMetadata WHERE Key='AcquisitionDateTime'"
                    ).fetchone()
                if model:
                    found_model = str(model[0]).strip() or None
                    console.print(f"  [green]Instrument:[/green] {model[0]}")
                if acq:
                    console.print(f"  [green]Last acquisition:[/green] {acq[0][:19]}")
        elif raw_file.suffix.lower() == ".raw" and raw_file.is_file():
            # Thermo — try TRFP if available, otherwise binary strings
            try:
                from stan.tools.trfp import extract_metadata
                meta = extract_metadata(raw_file)
                if meta.get("instrument_model"):
                    found_model = str(meta["instrument_model"]).strip() or None
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
                # 'strings' is a Unix utility; skip on Windows
                if shutil.which("strings"):
                    import subprocess
                    proc = subprocess.run(
                        ["strings", str(raw_file)],
                        capture_output=True, text=True, timeout=15,
                    )
                    if proc.returncode == 0:
                        models = re.findall(r'Thermo Scientific instrument model.*?value="([^"]+)"', proc.stdout)
                        if models:
                            found_model = models[0].strip() or None
                            console.print(f"  [green]Instrument:[/green] {models[0]}")
    except Exception:
        pass  # Don't block setup on a probe failure
    if found_model and found_model.lower() in _PLACEHOLDER_NAMES:
        found_model = None
    return found_model


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
        console.print("  [yellow]Skipped — you can verify later with: stan community-claim[/yellow]")
        return None

    # Call the relay to send verification code
    import json
    import urllib.request

    claim_id = ""
    try:
        payload = json.dumps({"pseudonym": pseudonym, "email": email}).encode()
        req = urllib.request.Request(
            f"{RELAY_URL}/api/claim-name",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "STAN"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            # Echoed to verify-claim: the relay only lets the caller holding
            # it spend the code's wrong-guess budget, so a stranger's guesses
            # cannot throw away the code this user was just emailed.
            claim_id = str(result.get("claim_id") or "")
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
        console.print("  [yellow]Invalid code. You can verify later with: stan community-claim[/yellow]")
        return None

    # Verify the code
    try:
        body = {"pseudonym": pseudonym, "code": code}
        if claim_id:  # a relay that predates claim_id gets the request it expects
            body["claim_id"] = claim_id
        payload = json.dumps(body).encode()
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


#: What `stan setup` offers for the LC flow regime (spec decision 12).
_LC_FLOW_CHOICES = {
    "nano": "nanoflow, under 1 µL/min",
    "capillary": "capillary flow, 1-10 µL/min",
    "micro": "microflow, over 10 µL/min",
}


def _pick_lc_flow(current: str | None = None) -> str:
    """Ask for the LC flow regime. Returns nano | capillary | micro, or ''.

    Not reliably in raw files, so it is asked once per instrument, like the
    column. The community benchmark groups non-Evosep runs by it; an Evosep
    lab, or anyone unsure, skips it. Skipping keeps a value already set.
    """
    from stan.metrics.scoring import normalize_lc_flow

    console.print("  [dim]LC flow regime, used to group non-Evosep LC runs on the[/dim]")
    console.print("  [dim]community benchmark. Skip for an Evosep, or if unsure.[/dim]")
    for key, label in _LC_FLOW_CHOICES.items():
        console.print(f"    [bold]{key}[/bold]  {label}")
    current = normalize_lc_flow(current)
    pick = Prompt.ask(
        "  LC flow regime",
        choices=[*_LC_FLOW_CHOICES, "skip"],
        default=current or "skip",
        console=console,
    )
    return "" if pick == "skip" else normalize_lc_flow(pick)


def _check_search_engines() -> None:
    """Check if DIA-NN and Sage are on PATH."""
    console.print("[dim]Checking for search engines...[/dim]")

    diann = shutil.which("diann") or shutil.which("diann.exe") or shutil.which("diann-linux")
    sage = shutil.which("sage") or shutil.which("sage.exe")

    if diann:
        console.print(f"  [green]DIA-NN:[/green] {diann}")
        if not (shutil.which("diann") or shutil.which("diann.exe")):
            # Found only as diann-linux; the dispatcher runs plain `diann`.
            console.print(
                "  [yellow]The watcher runs `diann` from PATH, which is not there.[/yellow] "
                "[dim]Set diann_path: to the DIA-NN executable in this instrument's "
                "block in instruments.yml.[/dim]"
            )
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


def _print_block_by_hand(block: dict) -> None:
    """Print a block for the user to paste when instruments.yml can't be written.

    With the ``output_dir`` a written block would get: without one the
    watcher writes results relative to its working directory, which under a
    service manager is not writable.
    """
    clean = {k: v for k, v in block.items() if v is not None}
    if not clean.get("output_dir") and clean.get("watch_dir"):
        clean["output_dir"] = str(default_output_dir(
            str(clean.get("name") or ""), Path(str(clean["watch_dir"]))))
    console.print("  [yellow]Add this block under 'instruments:' by hand:[/yellow]")
    console.print(yaml.safe_dump([clean], default_flow_style=False, sort_keys=False),
                  markup=False, highlight=False, soft_wrap=True)


def save_email_settings(
    enabled: bool, to: str, daily: str = "07:00", weekly: str = "monday"
) -> None:
    """Store the daily/weekly email settings under ``email_reports`` in community.yml.

    Goes through ``write_community_keys``, which keeps every other key
    (``auth_token``, ``display_name``, ``community_submit``), reads a file
    PowerShell wrote with a byte-order mark, and refuses to overwrite a file
    it cannot parse. ``daily_email.save_email_config`` read with the platform
    encoding, turned any parse error into ``{}`` and wrote back only
    ``email_reports``, which erased a lab's verified-name token.

    Raises:
        OSError, ValueError, yaml.YAMLError: community.yml could not be
        read or written; nothing was changed.
    """
    from stan.community.peg_submit import write_community_keys

    write_community_keys({
        "email_reports": {"enabled": enabled, "to": to, "daily": daily, "weekly": weekly},
    })


def _write_community_answers(updates: dict) -> Path | None:
    """Merge the wizard's community answers into community.yml.

    Every other key (email_reports, peg_share, hive_mirror_dir, ...) is
    kept, and the file is written owner-only, through the same writer
    ``stan community-claim`` uses. An unreadable community.yml is reported,
    never replaced: the old code parsed a broken file as ``{}`` and wrote
    that back, which threw away the lab's auth_token.
    """
    from stan.community.peg_submit import write_community_keys

    try:
        _promote_legacy_config("community.yml")
        path = write_community_keys(updates)
    except PermissionError as e:
        console.print(f"  [red]Permission denied writing community.yml: {e}[/red]")
        console.print("  [yellow]Try right-clicking the file → Properties → uncheck Read-only[/yellow]")
    except (OSError, ValueError, yaml.YAMLError) as e:
        console.print(f"  [red]Could not update community.yml: {e}[/red]")
    else:
        console.print(f"  [green]Wrote[/green] community settings to {path}")
        return path
    console.print("  [yellow]Add these lines to community.yml by hand:[/yellow]")
    for key, value in updates.items():
        console.print(f"  {key}: {yaml.safe_dump(value).splitlines()[0]}",
                      markup=False, highlight=False, soft_wrap=True)
    return None


# ── Instrument blocks (shared by `stan setup` and `stan add-watch`) ─────

#: Keys that follow from the vendor. The watcher registers only files whose
#: suffix is in ``extensions`` (a Bruker .d is a directory, a Thermo .raw a
#: file) and waits ``stable_secs`` without growth before searching one.
VENDOR_DEFAULTS: dict[str, dict] = {
    "bruker": {"extensions": [".d"], "stable_secs": 60},
    "thermo": {"extensions": [".raw"], "stable_secs": 30},
}

#: Directory entries detect_vendor() looks at before it gives up.
VENDOR_SCAN_LIMIT = 5000

#: Names the watcher treats as "read the model from the first raw file".
_PLACEHOLDER_NAMES = ("auto", "unknown", "")


class InstrumentNameClash(ValueError):
    """Another watch folder's block already uses this instrument name.

    The watcher keys its instruments by name, so two blocks sharing one
    start a single watcher and the other folder is never watched.
    """

    def __init__(self, name: str, other_watch_dir: str) -> None:
        super().__init__(
            f"The instrument name '{name}' is already used for {other_watch_dir}; "
            "each watch folder needs its own name."
        )
        self.name = name
        self.other_watch_dir = other_watch_dir


def normalize_vendor(vendor: str | None) -> str | None:
    """Return ``bruker`` / ``thermo`` for any spelling, None for blank.

    Raises:
        ValueError: for anything else. add-watch used to accept any string
            and then treat everything that was not exactly ``bruker`` as
            Thermo, so ``--vendor Bruker`` watched for ``.raw`` files.
    """
    if vendor is None or not str(vendor).strip():
        return None
    v = str(vendor).strip().lower()
    if v not in VENDOR_DEFAULTS:
        raise ValueError(f"vendor must be 'bruker' or 'thermo', not {vendor!r}")
    return v


def _scan_raw_files(
    watch_dir: Path, ext: str, limit: int = VENDOR_SCAN_LIMIT
) -> tuple[list[Path], bool]:
    """Raw files (``.d`` folders or ``.raw`` files) under ``watch_dir``, bounded.

    Walks at most ``limit`` directory entries and never descends into a
    ``.d`` folder (each holds thousands of files), so pointing ``stan setup``
    at a large archive cannot stall it.

    Returns:
        ``(files, truncated)``; ``truncated`` is True when the limit was hit.
    """
    found: list[Path] = []
    seen = 0
    if not watch_dir.is_dir():
        return found, False
    try:
        for dirpath, dirnames, filenames in os.walk(watch_dir):
            base = Path(dirpath)
            for name in list(dirnames):
                seen += 1
                if name.endswith(".d"):
                    dirnames.remove(name)  # a .d is a run, not a folder to search
                    if ext == ".d":
                        found.append(base / name)
            if ext == ".raw":
                for name in filenames:
                    seen += 1
                    if name.endswith(".raw"):
                        found.append(base / name)
            else:
                seen += len(filenames)
            if seen >= limit:
                return found, True
    except OSError:
        pass  # an unreadable subfolder ends the scan; what was found stands
    return found, False


def count_raw_files(watch_dir: Path, limit: int = VENDOR_SCAN_LIMIT) -> tuple[int, int]:
    """Count Bruker ``.d`` folders and Thermo ``.raw`` files under ``watch_dir``.

    Recursive, because raws often sit in per-project or per-date subfolders,
    but it stops after ``limit`` entries, or once it has seen three of one
    kind and none of the other, so a large archive cannot stall it.

    Returns:
        ``(n_d, n_raw)``.
    """
    n_d = n_raw = 0
    root = Path(watch_dir)
    if not root.is_dir():
        return 0, 0
    try:
        for i, entry in enumerate(root.rglob("*")):
            if i >= limit:
                break
            try:
                if entry.suffix == ".d" and entry.is_dir():
                    n_d += 1
                elif entry.suffix == ".raw" and entry.is_file():
                    n_raw += 1
            except OSError:
                continue
            if (n_d >= 3 and n_raw == 0) or (n_raw >= 3 and n_d == 0):
                break
    except OSError:
        pass  # an unreadable subfolder ends the scan; what was counted stands
    return n_d, n_raw


def detect_vendor(watch_dir: Path, limit: int = VENDOR_SCAN_LIMIT) -> tuple[str | None, int, int]:
    """Guess the vendor from the raw files already in ``watch_dir``.

    Returns:
        ``(vendor, n_d, n_raw)``. ``vendor`` is None when neither kind was
        found. A folder holding both gets the majority; callers should say
        so, because one block watches for one vendor's files only.
    """
    n_d, n_raw = count_raw_files(watch_dir, limit)
    if n_d and not n_raw:
        return "bruker", n_d, n_raw
    if n_raw and not n_d:
        return "thermo", n_d, n_raw
    if n_d and n_raw:
        return ("bruker" if n_d >= n_raw else "thermo"), n_d, n_raw
    return None, n_d, n_raw


def default_output_dir(name: str, watch_dir: Path) -> Path:
    """Where a block's search results go when it names no ``output_dir``.

    Without one the watcher writes each run's results relative to its own
    working directory, which under a service manager is ``/`` and not
    writable. One subfolder per instrument, so two instruments' ``HeLa_001``
    never share a results folder.
    """
    label = name if (name or "").strip().lower() not in _PLACEHOLDER_NAMES else Path(watch_dir).name
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("._") or "instrument"
    return get_user_config_dir() / "qc_output" / slug


def watcher_block(
    name: str,
    vendor: str,
    watch_dir: Path | str,
    *,
    qc_only: bool = True,
    qc_pattern: str | None = None,
) -> dict:
    """The keys the watcher needs before it will process a folder.

    Leave one out and the install looks healthy while doing nothing: no
    ``enabled: true`` and the block is never started, empty ``extensions``
    and every new file is ignored, no ``vendor`` and mode detection fails.
    ``output_dir`` is added by :func:`upsert_instrument_block` when the
    final block has none, so an existing one is never replaced.
    """
    v = normalize_vendor(vendor)
    if v is None:
        raise ValueError("vendor is required (bruker or thermo)")
    block: dict = {
        "name": name,
        "vendor": v,
        "watch_dir": str(_absolute(watch_dir)),
        "extensions": list(VENDOR_DEFAULTS[v]["extensions"]),
        "stable_secs": VENDOR_DEFAULTS[v]["stable_secs"],
        "enabled": True,
        "qc_only": bool(qc_only),
    }
    if qc_pattern:
        block["qc_pattern"] = qc_pattern
    return block


def _absolute(path: Path | str) -> Path:
    p = Path(str(path)).expanduser()
    try:
        return p.resolve()
    except OSError:
        return p.absolute()


def _norm_watch_dir(watch_dir: object) -> str:
    """Comparison key for a watch folder: absolute, case-folded on Windows."""
    if not watch_dir:
        return ""
    return os.path.normcase(str(_absolute(str(watch_dir))))


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _legacy_config_path(filename: str) -> Path | None:
    """``~/.stan/<filename>`` on Windows, where older installs kept config.

    ``stan.config.resolve_config_path`` still reads it when
    ``%USERPROFILE%\\STAN\\<filename>`` is absent, and the Windows installer
    still writes ``.stan\\instruments.yml``.
    """
    if not _is_windows():
        return None
    return Path.home() / ".stan" / filename


def existing_user_config(filename: str) -> Path | None:
    """The user-level config file STAN actually reads for ``filename``, if any."""
    primary = get_user_config_dir() / filename
    if primary.exists():
        return primary
    legacy = _legacy_config_path(filename)
    if legacy is not None and legacy.exists():
        return legacy
    return None


def _promote_legacy_config(filename: str) -> None:
    """Copy a legacy ``~/.stan`` file into the config dir before a write there.

    Writing a fresh ``%USERPROFILE%\\STAN\\<file>`` next to a legacy one would
    shadow it, and every key only the legacy copy holds (an auth_token, say)
    would silently stop being read.
    """
    primary = get_user_config_dir() / filename
    legacy = _legacy_config_path(filename)
    if primary.exists() or legacy is None or not legacy.exists():
        return
    primary.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(legacy, primary)


def instruments_config_path() -> Path:
    """The instruments.yml the watcher reads, or where a new one goes.

    On Windows that can be the legacy ``%USERPROFILE%\\.stan`` copy, which
    install_stan.ps1 creates; reads use it as it is. A write goes to the
    config dir instead (see :func:`_instruments_write_path`).
    """
    return existing_user_config("instruments.yml") or (get_user_config_dir() / "instruments.yml")


def _same_file(a: Path, b: Path) -> bool:
    return os.path.normcase(str(_absolute(a))) == os.path.normcase(str(_absolute(b)))


def _instruments_write_path(path: Path) -> Path:
    """Where a change to ``path`` is written.

    ``path`` itself, except for the legacy ``%USERPROFILE%\\.stan`` copy
    while the config dir has none. install_stan.ps1 creates that file (an
    empty ``instruments: []`` skeleton) on a fresh Windows install, and the
    per-instrument wrappers write there too, but update_stan.ps1
    looks for Bruker blocks, and the installer tells people to edit, only
    ``%USERPROFILE%\\STAN\\instruments.yml``. The first write therefore
    copies the legacy file's content there, the way
    :func:`_promote_legacy_config` does for community.yml, and STAN reads
    that file from then on. A ``stan watch`` already running keeps the path
    it resolved at start-up, so callers say to restart it
    (:func:`config_write_notes`).
    """
    legacy = _legacy_config_path("instruments.yml")
    primary = get_user_config_dir() / "instruments.yml"
    if legacy is not None and not primary.exists() and _same_file(path, legacy):
        return primary
    return path


def config_write_notes(read_path: Path, written_path: Path, before: dict | None, after: dict) -> list[str]:
    """What to tell the user after a block was written, as rich-markup lines.

    The watcher daemon hot-reloads instruments.yml, but it starts a watcher
    only for a name it is not running yet and never re-applies a changed
    block to a running one. So repairing a block that was already enabled
    (an old ``stan setup`` block: enabled, no extensions, every file
    ignored) reached nothing until a restart, and nothing said so.
    """
    from rich.markup import escape

    notes: list[str] = []
    if not _same_file(read_path, written_path):
        notes.append(
            f"Copied the settings in {escape(str(read_path))} (written by the Windows "
            f"installer) into {escape(str(written_path))}. STAN reads that file from now "
            "on; edit it, not the old one."
        )
        notes.append(
            "If [cyan]stan watch[/cyan] is running, restart it: it keeps reading the "
            "file it found when it started."
        )
    elif before and before.get("enabled", False) and before != after:
        old = before.get("name")
        # A rename is applied by the reload itself (the old name is stopped,
        # the new one started), unless another enabled block keeps the old
        # name running, with whichever block's settings it started from.
        if after.get("name") == old or _enabled_elsewhere(written_path, old, after.get("watch_dir")):
            notes.append(
                f"If [cyan]stan watch[/cyan] is running, restart it: a running watcher "
                f"keeps the settings '{escape(str(old or ''))}' had when it started."
            )
    return notes


def _enabled_elsewhere(path: Path, name: object, watch_dir: object) -> bool:
    """Another enabled block in ``path`` is named ``name`` (True if unreadable)."""
    try:
        blocks = _read_instruments_doc(path).get("instruments") or []
    except (OSError, ValueError, yaml.YAMLError):
        return True
    key = _norm_watch_dir(watch_dir)
    return any(
        isinstance(b, dict) and b.get("name") == name and b.get("enabled", False)
        and _norm_watch_dir(b.get("watch_dir")) != key
        for b in blocks
    )


def _read_instruments_doc(path: Path) -> dict:
    """instruments.yml as a dict, ``{}`` when absent; raises when it is not one.

    Decoded like the watcher reads it (:func:`stan.config.read_config_text`),
    so the installer's BOM-prefixed file parses on Windows too.
    """
    if not path.exists():
        return {}
    data = yaml.safe_load(read_config_text(path)) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a YAML mapping; refusing to overwrite it")
    blocks = data.get("instruments")
    if blocks is not None and not isinstance(blocks, list):
        raise ValueError(f"{path}: 'instruments' is not a list; refusing to overwrite it")
    return data


def _write_yaml_atomic(path: Path, doc: dict) -> None:
    """Replace ``path`` in one step so a hot-reload never reads half a file.

    A truncated instruments.yml parses as empty, and the watcher would stop
    every instrument until the next poll.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(doc, default_flow_style=False, sort_keys=False)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except PermissionError:
        # Windows refuses to replace a file another process holds open (the
        # watcher's own reload read); overwriting in place still works.
        try:
            tmp.unlink()
        except OSError:
            pass
        path.write_text(text, encoding="utf-8")


def find_instrument_block(watch_dir: Path | str, config_path: Path | None = None) -> dict | None:
    """The block that watches ``watch_dir``, or None (also when unreadable)."""
    path = Path(config_path) if config_path else instruments_config_path()
    try:
        doc = _read_instruments_doc(path)
    except (OSError, ValueError, yaml.YAMLError):
        return None
    key = _norm_watch_dir(watch_dir)
    for blk in doc.get("instruments") or []:
        if isinstance(blk, dict) and _norm_watch_dir(blk.get("watch_dir")) == key:
            return dict(blk)
    return None


def instrument_name_owner(name: str, watch_dir: Path | str, config_path: Path | None = None) -> str | None:
    """The watch folder of another block already named ``name``, or None."""
    path = Path(config_path) if config_path else instruments_config_path()
    try:
        doc = _read_instruments_doc(path)
    except (OSError, ValueError, yaml.YAMLError):
        return None
    key = _norm_watch_dir(watch_dir)
    for blk in doc.get("instruments") or []:
        if (isinstance(blk, dict) and blk.get("name") == name
                and _norm_watch_dir(blk.get("watch_dir")) != key):
            return str(blk.get("watch_dir") or "?")
    return None


def _is_unset(value: object) -> bool:
    return value is None or value == "" or value == []


def upsert_instrument_block(
    block: dict,
    *,
    config_path: Path | None = None,
    fill_only: bool = False,
    override: tuple[str, ...] | list[str] = (),
    drop_duplicates: bool = False,
) -> tuple[str, dict, Path]:
    """Add ``block`` to instruments.yml, or merge it into the folder's block.

    Blocks are matched by watch folder, so running ``stan setup`` again on a
    folder updates its block instead of adding a second watcher for it.
    Keys the caller does not pass (output_dir, diann_path, lib_path, ...)
    are kept; a value of None removes that key. ``output_dir`` is set to
    :func:`default_output_dir` when the result has none.

    Args:
        block: Must hold ``watch_dir``; usually built by :func:`watcher_block`.
        config_path: instruments.yml to read; default
            :func:`instruments_config_path`. The change is written there,
            except that the Windows installer's legacy ``.stan`` copy is
            first moved into the config dir (:func:`_instruments_write_path`).
        fill_only: Only add keys the existing block lacks or has empty, never
            change one already set. ``stan add-watch`` uses it to complete a
            block an older version left unusable.
        override: Keys of ``block`` applied as given even with ``fill_only``:
            what a flag asked for explicitly (None removes the key).
        drop_duplicates: Also delete later blocks for the same folder, which
            an old ``stan setup`` bug wrote.

    Returns:
        ``(action, block, path)``: action is ``added``, ``updated``,
        ``completed`` (fill_only added keys and no override changed one) or
        ``unchanged``; block is the block as written; path is the file
        written, or the one read when nothing was.

    Raises:
        InstrumentNameClash: another folder's block already has the name.
        ValueError, yaml.YAMLError: instruments.yml exists but is not a
            mapping, or not valid YAML. It is never overwritten then.
    """
    path = Path(config_path) if config_path else instruments_config_path()
    doc = _read_instruments_doc(path)
    blocks = list(doc.get("instruments") or [])
    key = _norm_watch_dir(block.get("watch_dir"))
    if not key:
        raise ValueError("an instrument block needs a watch_dir")

    matches = [
        i for i, b in enumerate(blocks)
        if isinstance(b, dict) and _norm_watch_dir(b.get("watch_dir")) == key
    ]
    idx = matches[0] if matches else None
    current = dict(blocks[idx]) if idx is not None else {}
    merged = dict(current)
    for k, v in block.items():
        if (fill_only and k not in override
                and (v is None or not _is_unset(current.get(k)))):
            continue
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    if not merged.get("output_dir"):
        merged["output_dir"] = str(default_output_dir(
            str(merged.get("name") or ""), Path(str(merged.get("watch_dir")))))

    ignore = set(matches) if drop_duplicates else ({idx} if idx is not None else set())
    name = merged.get("name")
    if idx is None or name != current.get("name"):
        for i, b in enumerate(blocks):
            if i not in ignore and isinstance(b, dict) and b.get("name") == name:
                raise InstrumentNameClash(str(name), str(b.get("watch_dir") or "?"))

    dupes = matches[1:] if drop_duplicates else []
    if merged == current and not dupes:
        return "unchanged", merged, path
    if idx is None:
        blocks.append(merged)
        action = "added"
    else:
        blocks[idx] = merged
        overridden = any(current.get(k) != merged.get(k) for k in override)
        if merged == current:
            action = "unchanged"
        else:
            action = "completed" if fill_only and not overridden else "updated"
    for i in reversed(dupes):
        del blocks[i]
    target = _instruments_write_path(path)
    if dupes:
        logger.warning("Removed %d duplicate block(s) for %s from %s",
                       len(dupes), merged.get("watch_dir"), target)
    doc["instruments"] = blocks
    _write_yaml_atomic(target, doc)
    return action, merged, target


def prompt_qc_filter(
    watch_path: Path,
    vendor: str,
    out: Console | None = None,
) -> tuple[bool, str | None]:
    """Ask which files in this folder get searched.

    Shows how many of the raws already there the default HeLa/QC filename
    pattern matches, so a lab whose QC files are named differently finds out
    now, not after its runs have been skipped for a week.

    Returns:
        ``(qc_only, qc_pattern)``; ``qc_pattern`` None means the default.
    """
    from stan.watcher.qc_filter import (
        DEFAULT_QC_PATTERN,
        compile_qc_pattern,
        is_qc_file,
    )

    out = out or console
    ext = ".d" if normalize_vendor(vendor) == "bruker" else ".raw"
    found_files, truncated = _scan_raw_files(Path(watch_path), ext)

    default_pat = compile_qc_pattern()
    matched = [f for f in found_files if is_qc_file(f, default_pat)]
    total = len(found_files)

    out.print()
    more = f" (stopped after {VENDOR_SCAN_LIMIT:,} entries)" if truncated else ""
    out.print(f"[bold]Scanning {watch_path}[/bold] — found [cyan]{total}[/cyan] {ext} files{more}.")
    if total == 0:
        out.print("[yellow]No raw files yet — that's fine, filtering will apply to future files too.[/yellow]")
    else:
        out.print(
            f"The default QC pattern [dim]{DEFAULT_QC_PATTERN}[/dim] "
            f"matches [cyan]{len(matched)}[/cyan] / {total} files."
        )
        # A few examples of matched vs. unmatched so the user knows what
        # they are picking.
        if matched:
            out.print("[green]Matched (will be processed):[/green]")
            for f in matched[:3]:
                out.print(f"  ✓ {f.name}")
            if len(matched) > 3:
                out.print(f"  [dim]... and {len(matched) - 3} more[/dim]")
        unmatched = [f for f in found_files if f not in matched]
        if unmatched:
            out.print("[dim]Skipped (non-QC):[/dim]")
            for f in unmatched[:3]:
                out.print(f"  [dim]✗ {f.name}[/dim]")
            if len(unmatched) > 3:
                out.print(f"  [dim]... and {len(unmatched) - 3} more[/dim]")

    out.print()
    out.print("QC filtering options:")
    out.print("  [cyan]1[/cyan]  Use the default HeLa/QC pattern (recommended)")
    out.print("  [cyan]2[/cyan]  Custom regex pattern for this directory")
    out.print("  [cyan]3[/cyan]  Process every file (no filter — for dedicated QC dirs)")
    choice = Prompt.ask("Choice", choices=["1", "2", "3"], default="1", console=out)

    if choice == "1":
        return True, None
    if choice == "3":
        return False, None
    while True:
        pat = Prompt.ask(
            "Enter regex (e.g. (?i)(hela|myqc|std.*he))",
            default=DEFAULT_QC_PATTERN,
            console=out,
        )
        # re.compile: compile_qc_pattern never raises, it falls back to the
        # default, so a typo used to be accepted and written to the config.
        try:
            re.compile(pat)
        except re.error as e:
            from rich.markup import escape

            out.print(f"[red]Invalid regex: {escape(str(e))}[/red]")
            continue
        compiled = compile_qc_pattern(pat)
        if found_files:
            n_match = sum(1 for f in found_files if is_qc_file(f, compiled))
            out.print(f"[dim]Matches {n_match} / {total} files.[/dim]")
        if Confirm.ask("Accept this pattern?", default=True, console=out):
            return True, pat


def _ask_vendor(watch_path: Path, fallback: str | None = None) -> str:
    """The folder's vendor: from the raws already in it, otherwise asked.

    ``fallback`` (the vendor an existing block for this folder names) is the
    default answer when the folder cannot tell.
    """
    detected, n_d, n_raw = detect_vendor(watch_path)
    if detected and not (n_d and n_raw):
        what = f"{n_d} .d folder(s)" if detected == "bruker" else f"{n_raw} .raw file(s)"
        console.print(f"  [green]Vendor:[/green] {detected} (found {what})")
        return detected
    if n_d and n_raw:
        console.print(
            f"  [yellow]Found both .d ({n_d}) and .raw ({n_raw}) files.[/yellow] "
            "A watch folder is set up for one vendor's files."
        )
    else:
        console.print("  [dim]No .d or .raw files yet, so the vendor has to be given.[/dim]")
    try:
        default = detected or normalize_vendor(fallback)
    except ValueError:
        default = None
    kwargs: dict = {"choices": ["bruker", "thermo"], "console": console}
    if default:
        kwargs["default"] = default
    return Prompt.ask("  Vendor (bruker = timsTOF .d, thermo = Orbitrap .raw)", **kwargs)


def _ask_instrument_name(
    watch_path: Path,
    vendor: str,
    detected_model: str | None,
    existing_block: dict | None,
) -> str:
    """Ask for the block's name; it must not be another folder's name."""
    current = str((existing_block or {}).get("name") or "").strip()
    if current and current.lower() not in _PLACEHOLDER_NAMES:
        default = current
    elif detected_model:
        default = detected_model
    else:
        default = "auto"
    console.print(
        "  [dim]The name is stored on every run and shown on the dashboard; include the "
        "model (e.g. 'timsTOF HT'). 'auto' reads it from the first raw file.[/dim]"
    )
    while True:
        name = Prompt.ask("  Instrument name", default=default, console=console).strip()
        if not name:
            continue
        owner = instrument_name_owner(name, watch_path)
        if owner is None:
            return name
        console.print(
            f"  [yellow]'{name}' already names the instrument watching {owner}. "
            "Each watch folder needs its own name.[/yellow]"
        )
        fallback = f"{watch_path.name}_{vendor}"
        default = fallback if name != fallback else f"{fallback}_2"


# ── Minimal config files (`stan init`, `stan setup`) ───────────────────
# ASCII only: Python on Windows reads these with the system code page.

DEFAULT_INSTRUMENTS_YML = """\
# STAN instruments -- one block per watch folder. Created by `stan init`;
# STAN never replaces this file once it exists.
#
# Add a folder with either of:
#   stan add-watch <folder> --vendor bruker|thermo --name "<model>" -y
#   stan setup
# Both write a block the watcher can use, like this one:
#
# - name: timsTOF HT          # unique per folder; include the model
#   vendor: bruker            # bruker | thermo
#   watch_dir: /path/to/raw   # watched recursively
#   extensions: ['.d']        # ['.raw'] for Thermo
#   stable_secs: 60           # 30 for Thermo
#   enabled: true             # the watcher skips a block without this
#   qc_only: true             # search only HeLa/QC-named files
#   output_dir: /path/to/qc_output/timsTOF_HT
instruments: []
"""

DEFAULT_THRESHOLDS_YML = """\
# STAN QC thresholds, keyed by instrument model (or "default") and mode.
# Created empty by `stan init`: with no thresholds every run passes the
# gate. Example -- uncomment and adjust:
#
# thresholds:
#   default:
#     dia:
#       n_precursors_min: 5000
#       median_cv_precursor_max: 20.0
#     dda:
#       n_psms_min: 10000
thresholds: {}
"""

DEFAULT_COMMUNITY_YML = """\
# STAN community settings. Created by `stan init` with every sharing
# option off: nothing is shared until you opt in here, in `stan setup`,
# or with the dashboard's Sync button.
#
# Your lab's name on the public benchmark. After setting it, run
# `stan community-claim` to verify it by email; that adds auth_token.
display_name: ""
# true: `stan submit-all` and the dashboard Sync button upload aggregate
# QC metrics (never raw files) to the community benchmark.
community_submit: false
# true: `stan peg-sync` shares per-run PEG measurements.
peg_share: false
# true: send error reports to the STAN relay: the error type and message
# (a failed search's message is its command line, paths included), a
# traceback stripped to file names, the raw file's name, and the STAN,
# Python and OS versions. Off unless set to true.
error_telemetry: false
"""

_DEFAULT_CONFIGS: tuple[tuple[str, str], ...] = (
    ("instruments.yml", DEFAULT_INSTRUMENTS_YML),
    ("thresholds.yml", DEFAULT_THRESHOLDS_YML),
    ("community.yml", DEFAULT_COMMUNITY_YML),
)


def ensure_default_configs() -> list[tuple[Path, bool]]:
    """Create the minimal config files that do not exist yet.

    Never overwrites, including a legacy ``~/.stan`` copy on Windows (a new
    file in the config dir would shadow it). The package used to copy these
    from ``config/``, where they were deleted in April 2026, so ``stan init``
    printed "missing source" three times and created nothing.

    Returns:
        ``(path, created)`` per file; ``path`` is the file STAN reads.
    """
    user_dir = get_user_config_dir()
    user_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, bool]] = []
    for filename, text in _DEFAULT_CONFIGS:
        found = existing_user_config(filename)
        if found is not None:
            results.append((found, False))
            continue
        dst = user_dir / filename
        # O_EXCL: a file that appeared since the check is left alone.
        mode = 0o600 if filename == "community.yml" else 0o644  # it will hold auth_token
        try:
            fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        except FileExistsError:
            results.append((dst, False))
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        results.append((dst, True))
    return results
