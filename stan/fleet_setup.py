"""Fleet sync setup wizard for stan init.

Prompts the operator for the fleet-sync root path so godmode can
monitor every instrument's STAN install from one place. Saves to
``~/.stan/fleet.yml``. Three modes:

- ``smb``: a mapped network drive / SMB mount path
  (e.g. ``\\\\fileserver\\STAN\\`` on Windows,
  ``/Volumes/proteomics-grp/STAN/`` on macOS). This is how UC Davis ran
  it: STAN's daemon writes a mirror of every important file to that
  path, and godmode reads it.

- ``hf_space``: an HTTP relay via the public HF Space — for sites
  that can't or won't share an SMB mount (e.g. external collaborators).

- ``none`` (the default): this instrument doesn't participate in the
  fleet view. Every other lab wants this; the other two need a share or
  relay that is set up first.

With no terminal to answer on (stdin at end of file: ``</dev/null``, a
service, a pipe that has run dry) every question takes its default, so
``stan init`` finishes with ``mode: none`` instead of aborting.

Writes:

    fleet:
      mode: smb | hf_space | none
      root_path: /Volumes/proteomics-grp/STAN/  # smb only
      space_url: https://...                     # hf_space only
      configured_at: 2026-04-27T...

Nothing reads this file yet (checked 2026-09-29: no reader in STAN or
in godmode). The watcher's ``sync_to_hive_mirror`` finds its mirror
through ``HIVE_MIRROR_DIR`` or ``hive_mirror_dir`` in community.yml, not
through ``root_path`` here, so choosing ``smb`` does not by itself make
anything sync.

This is the v1.0 onboarding — first-time install asks once, stores
the answer, and any future reconfiguration goes through
``stan init --reconfigure-fleet``.
"""
from __future__ import annotations

import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml
from rich.console import Console
from rich.prompt import Prompt

from stan.config import get_user_config_dir

console = Console()

#: The menu entry taken on Enter or when there is no input at all.
DEFAULT_CHOICE = "3"


def _ask(prompt: str, **kwargs) -> str:
    """``Prompt.ask``, but end of input answers with the default.

    Rich reads with ``input()``, which raises EOFError when stdin is not a
    terminal and has nothing left; Click then prints "Aborted." and exits 1.
    That is what ``stan init </dev/null`` used to do.
    """
    try:
        return Prompt.ask(prompt, **kwargs)
    except EOFError:
        default = kwargs.get("default", "")
        console.print(f"\n[dim](no input; using the default: {default or 'none'})[/dim]")
        return default


def _suggested_default() -> str:
    """OS-aware default for the SMB mount path."""
    if sys.platform == "darwin":
        return "/Volumes/proteomics-grp/STAN/"
    if sys.platform == "win32":
        return r"\\fileserver\proteomics-grp\STAN\\"
    return "/mnt/proteomics-grp/STAN/"


def _validate_smb_path(path_str: str) -> tuple[bool, str]:
    """Check that the path exists and is writable.

    Returns (ok, message). ``message`` contains a one-line summary
    suitable for display when ok is False.
    """
    if not path_str.strip():
        return False, "empty path"
    p = Path(path_str)
    if not p.exists():
        return False, f"path does not exist or is not mounted: {p}"
    if not p.is_dir():
        return False, f"path is not a directory: {p}"
    # Try a write probe — godmode + sync both need write access
    probe = p / ".stan_fleet_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as e:
        return False, f"path is not writable: {e}"
    return True, "ok"


def _fleet_yaml_path() -> Path:
    return get_user_config_dir() / "fleet.yml"


def load_fleet_config() -> dict:
    """Read the saved fleet config; empty dict if absent."""
    path = _fleet_yaml_path()
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def save_fleet_config(cfg: dict) -> Path:
    """Persist the fleet config to ``~/.stan/fleet.yml``."""
    path = _fleet_yaml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return path


def run_fleet_wizard(force: bool = False) -> dict:
    """Interactive prompt; returns the saved config dict.

    If ``force`` is False and a fleet.yml already exists (any mode), the
    wizard offers to keep it instead of re-prompting; Enter, or no input
    at all, keeps it. ``force=True`` (the ``--reconfigure-fleet`` flag
    path) always re-prompts. A fresh prompt defaults to ``none``.
    """
    existing = load_fleet_config().get("fleet", {}) or {}
    if existing and not force:
        mode = existing.get("mode", "?")
        root = existing.get("root_path") or existing.get("space_url") or ""
        console.print(
            f"\n[bold cyan]Fleet sync already configured:[/bold cyan] "
            f"mode={mode} path={root}"
        )
        keep = _ask(
            "Keep existing config? [Y/n]", default="y", show_default=False,
        ).strip().lower()
        if keep in ("y", "yes", ""):
            return load_fleet_config()

    console.print()
    console.print("[bold]STAN fleet sync setup[/bold]")
    console.print(
        "Pick how this instrument shares its QC data with the fleet view "
        "(godmode). You can change this later via "
        "[cyan]stan init --reconfigure-fleet[/cyan]."
    )
    console.print()
    # The numbers are kept as they were: docs and scripts answer "3" for
    # None, and renumbering would turn that answer into the HF relay.
    console.print("  [bold]1[/bold]  Mapped network drive / SMB mount  "
                  "[dim](UC Davis style; the share must already be mounted)[/dim]")
    console.print("  [bold]2[/bold]  Hugging Face Space (HTTP relay)  "
                  "[dim](for sites with no SMB share)[/dim]")
    console.print("  [bold]3[/bold]  None — this instrument is solo  "
                  "[dim](default)[/dim]")
    console.print()

    choice = _ask("Choice", choices=["1", "2", "3"], default=DEFAULT_CHOICE).strip()

    cfg: dict = {
        "fleet": {
            "configured_at": datetime.now(timezone.utc).isoformat(),
            "platform": platform.system().lower(),
        }
    }

    if choice == "1":
        suggested = _suggested_default()
        path_str = _ask(
            "Path to fleet root (must already exist + be writable)",
            default=suggested,
        ).strip()
        ok, msg = _validate_smb_path(path_str)
        if not ok:
            console.print(f"[yellow]warning:[/yellow] {msg}")
            confirm = _ask(
                "Save anyway? godmode + sync will fail until the path is "
                "mounted/writable. [y/N]",
                default="n",
            ).strip().lower()
            if confirm not in ("y", "yes"):
                console.print("Aborted; no changes written.")
                return existing or {}
        cfg["fleet"]["mode"] = "smb"
        cfg["fleet"]["root_path"] = str(Path(path_str))

    elif choice == "2":
        url = _ask(
            "HF Space URL",
            default="https://brettsp-stan.hf.space",
        ).strip()
        if not url.startswith(("http://", "https://")):
            console.print(f"[red]invalid URL:[/red] {url}")
            return existing or {}
        cfg["fleet"]["mode"] = "hf_space"
        cfg["fleet"]["space_url"] = url

    else:
        cfg["fleet"]["mode"] = "none"
        console.print(
            "Set mode=none. This instrument's QC stays local; godmode "
            "won't see it. Re-run [cyan]stan init --reconfigure-fleet[/cyan] "
            "to opt back in."
        )

    saved_to = save_fleet_config(cfg)
    console.print(f"\n[green]saved[/green] {saved_to}")
    return cfg
