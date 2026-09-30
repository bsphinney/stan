"""`stan init`, `stan add-watch` and `stan list-watch` against a temp HOME.

`stan init` runs as a real subprocess with stdin at end of file, which is
exactly the case that used to fail: it printed "missing source" three times
(its templates were deleted in April 2026) and then the fleet wizard,
defaulting to the UC Davis SMB share, died with "Aborted." on the first
prompt. Nothing here touches the real ~/.stan.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parent.parent


def _stan(home: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "PYTHONPATH": str(REPO),
        "COLUMNS": "200",
        "NO_COLOR": "1",
    }
    return subprocess.run(
        [sys.executable, "-c", "from stan.cli import app; app()", *args],
        input=stdin,
        stdin=subprocess.DEVNULL if stdin is None else None,
        capture_output=True, text=True, env=env, timeout=120, cwd=str(home),
    )


def _cfg_dir(home: Path) -> Path:
    return home / ("STAN" if sys.platform == "win32" else ".stan")


def _yml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ── stan init ──────────────────────────────────────────────────────────


def test_init_without_a_tty_creates_configs_and_takes_fleet_none(tmp_path) -> None:
    proc = _stan(tmp_path, "init")
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out
    assert "missing" not in out
    assert "Aborted" not in out

    cfg = _cfg_dir(tmp_path)
    assert _yml(cfg / "instruments.yml") == {"instruments": []}
    assert _yml(cfg / "thresholds.yml") == {"thresholds": {}}
    comm = _yml(cfg / "community.yml")
    assert comm["community_submit"] is False and comm["error_telemetry"] is False
    assert _yml(cfg / "fleet.yml")["fleet"]["mode"] == "none"


def test_init_never_overwrites_existing_files(tmp_path) -> None:
    cfg = _cfg_dir(tmp_path)
    cfg.mkdir(parents=True)
    mine = {
        "instruments.yml": "instruments:\n- name: Lumos\n  watch_dir: /x\n",
        "community.yml": "display_name: Nimble Edman\nauth_token: tok\n",
    }
    for name, text in mine.items():
        (cfg / name).write_text(text)
    fleet = "fleet:\n  mode: smb\n  root_path: /Volumes/share\n"
    (cfg / "fleet.yml").write_text(fleet)

    proc = _stan(tmp_path, "init")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for name, text in mine.items():
        assert (cfg / name).read_text() == text
    assert (cfg / "fleet.yml").read_text() == fleet  # "keep existing" is the default
    assert (cfg / "thresholds.yml").exists()  # the missing one is still created


def test_init_piped_answer_3_still_means_none(tmp_path) -> None:
    """Docs and scripts pipe `3`; the menu keeps its numbering for them."""
    proc = _stan(tmp_path, "init", stdin="3\n")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _yml(_cfg_dir(tmp_path) / "fleet.yml")["fleet"]["mode"] == "none"


# ── fleet wizard ───────────────────────────────────────────────────────


@pytest.fixture()
def cfg_dir(tmp_path, monkeypatch) -> Path:
    import stan.config as cfg

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    user = home / ".stan"
    monkeypatch.setattr(cfg, "_USER_CONFIG_DIR", user)
    return user


def test_fleet_wizard_defaults_to_none_on_enter(cfg_dir, monkeypatch) -> None:
    from stan import fleet_setup

    assert fleet_setup.DEFAULT_CHOICE == "3"
    monkeypatch.setattr("builtins.input", lambda *a: "")
    assert fleet_setup.run_fleet_wizard()["fleet"]["mode"] == "none"


def test_fleet_wizard_takes_defaults_at_end_of_input(cfg_dir, monkeypatch) -> None:
    from stan import fleet_setup

    def eof(*a):
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert fleet_setup.run_fleet_wizard(force=True)["fleet"]["mode"] == "none"
    assert _yml(cfg_dir / "fleet.yml")["fleet"]["mode"] == "none"


def test_fleet_wizard_smb_still_selectable(cfg_dir, tmp_path, monkeypatch) -> None:
    from stan import fleet_setup

    share = tmp_path / "share"
    share.mkdir()
    answers = iter(["1", str(share)])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    cfg = fleet_setup.run_fleet_wizard(force=True)
    assert cfg["fleet"]["mode"] == "smb"
    assert cfg["fleet"]["root_path"] == str(share)


# ── stan add-watch / list-watch ────────────────────────────────────────


def _cli(*args: str):
    from stan.cli import app

    return CliRunner().invoke(app, list(args), input="")


def test_add_watch_writes_enabled_and_output_dir(cfg_dir, tmp_path) -> None:
    """add-watch never wrote enabled: true, and the watcher starts only
    blocks that have it, so every folder it added was never watched."""
    watch = tmp_path / "tims"
    (watch / "HeLa_QC_001.d").mkdir(parents=True)
    res = _cli("add-watch", str(watch), "--name", "timsTOF HT", "-y")
    assert res.exit_code == 0, res.output

    (b,) = _yml(cfg_dir / "instruments.yml")["instruments"]
    assert b == {
        "name": "timsTOF HT",
        "vendor": "bruker",
        "watch_dir": str(watch.resolve()),
        "extensions": [".d"],
        "stable_secs": 60,
        "enabled": True,
        "qc_only": True,
        "output_dir": str(cfg_dir / "qc_output" / "timsTOF_HT"),
    }

    listing = _cli("list-watch")
    assert listing.exit_code == 0
    assert "not watched" not in listing.output


def _started_by_daemon(monkeypatch, config: dict) -> list[str]:
    """Names WatcherDaemon._apply_config would start (watchers stubbed out)."""
    import stan.watcher.daemon as d

    started: list[str] = []

    class _Stub:
        def __init__(self, cfg: dict) -> None:
            self.cfg = cfg

        def start(self) -> None:
            started.append(self.cfg["name"])

        def stop(self) -> None:
            pass

    monkeypatch.setattr(d, "InstrumentWatcher", _Stub)
    monkeypatch.setattr(d.WatcherDaemon, "_auto_merge_aliases", lambda self, cfg: None)
    d.WatcherDaemon()._apply_config(config)
    return started


def test_the_daemon_starts_what_add_watch_writes(cfg_dir, tmp_path, monkeypatch) -> None:
    res = _cli("add-watch", str(tmp_path), "--vendor", "thermo", "--name", "Exploris 480", "-y")
    assert res.exit_code == 0, res.output
    config = _yml(cfg_dir / "instruments.yml")
    assert _started_by_daemon(monkeypatch, config) == ["Exploris 480"]

    # The block the previous add-watch wrote (no enabled key) started nothing.
    old = {k: v for k, v in config["instruments"][0].items() if k not in ("enabled", "output_dir")}
    assert _started_by_daemon(monkeypatch, {"instruments": [old]}) == []


def test_add_watch_vendor_is_case_insensitive(cfg_dir, tmp_path) -> None:
    """--vendor Bruker used to fall through to the Thermo extensions."""
    res = _cli("add-watch", str(tmp_path), "--vendor", "Bruker", "-y")
    assert res.exit_code == 0, res.output
    (b,) = _yml(cfg_dir / "instruments.yml")["instruments"]
    assert (b["vendor"], b["extensions"], b["stable_secs"]) == ("bruker", [".d"], 60)


def test_add_watch_rejects_an_unknown_vendor(cfg_dir, tmp_path) -> None:
    res = _cli("add-watch", str(tmp_path), "--vendor", "sciex", "-y")
    assert res.exit_code == 2
    assert not (cfg_dir / "instruments.yml").exists()


def test_add_watch_completes_a_block_an_old_setup_wrote(cfg_dir, tmp_path) -> None:
    watch = tmp_path / "exploris"
    watch.mkdir()
    cfg_dir.mkdir(parents=True)
    old = {"name": "auto", "watch_dir": str(watch), "enabled": True,
           "hela_amount_ng": 50.0, "output_dir": "/data/qc"}
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [old]}))

    listing = _cli("list-watch")
    assert "not watched" in listing.output  # the broken block is visible now

    res = _cli("add-watch", str(watch), "--vendor", "thermo", "-y")
    assert res.exit_code == 0, res.output
    assert "Completed the existing block" in res.output
    (b,) = _yml(cfg_dir / "instruments.yml")["instruments"]
    assert b == {**old, "vendor": "thermo", "extensions": [".raw"], "stable_secs": 30}

    again = _cli("add-watch", str(watch), "--vendor", "thermo", "-y")
    assert "Already watching" in again.output
    assert _yml(cfg_dir / "instruments.yml")["instruments"] == [b]


def test_add_watch_completes_an_old_add_watch_block_with_enabled(cfg_dir, tmp_path) -> None:
    watch = tmp_path / "tims"
    watch.mkdir()
    cfg_dir.mkdir(parents=True)
    old = {"name": "tims_bruker", "vendor": "bruker", "watch_dir": str(watch),
           "extensions": [".d"], "stable_secs": 60, "qc_only": True}
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [old]}))

    res = _cli("add-watch", str(watch), "-y")
    assert res.exit_code == 0, res.output
    (b,) = _yml(cfg_dir / "instruments.yml")["instruments"]
    assert b["enabled"] is True
    assert b["output_dir"] == str(cfg_dir / "qc_output" / "tims_bruker")
    assert {k: b[k] for k in old} == old


def test_add_watch_leaves_an_explicitly_disabled_block_disabled(cfg_dir, tmp_path) -> None:
    cfg_dir.mkdir(parents=True)
    blk = {"name": "Lumos", "vendor": "thermo", "watch_dir": str(tmp_path),
           "extensions": [".raw"], "stable_secs": 30, "enabled": False, "output_dir": "/o"}
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [blk]}))
    res = _cli("add-watch", str(tmp_path), "-y")
    assert "Already watching" in res.output and "disabled" in res.output
    assert _yml(cfg_dir / "instruments.yml")["instruments"] == [blk]


def test_add_watch_refuses_a_name_another_folder_uses(cfg_dir, tmp_path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    assert _cli("add-watch", str(tmp_path / "a"), "--vendor", "thermo", "--name", "Lumos", "-y").exit_code == 0
    res = _cli("add-watch", str(tmp_path / "b"), "--vendor", "thermo", "--name", "Lumos", "-y")
    assert res.exit_code == 1
    assert len(_yml(cfg_dir / "instruments.yml")["instruments"]) == 1


# ── add-watch on a folder that already has a block: flags ──────────────


def _norm(text: str) -> str:
    """Output with rich's line wrapping undone."""
    return " ".join(text.split())


def _handler_events(block: dict, created: Path) -> list[str]:
    """Feed one created path through the real watcher handler."""
    import threading

    from watchdog.events import DirCreatedEvent, FileCreatedEvent

    from stan.watcher.daemon import _AcquisitionHandler

    events: list[str] = []
    handler = _AcquisitionHandler(
        dict(block), {}, {}, threading.Lock(),
        on_event=lambda cat, path, detail: events.append(cat),
    )
    ev = DirCreatedEvent(str(created)) if created.suffix == ".d" else FileCreatedEvent(str(created))
    handler.on_created(ev)
    return events


def _old_setup_block(watch: Path) -> dict:
    """What `stan setup` wrote before: enabled, named auto, no vendor/extensions."""
    return {"name": "auto", "watch_dir": str(watch), "enabled": True, "hela_amount_ng": 50.0}


def test_add_watch_applies_name_and_all_files_to_an_existing_block(cfg_dir, tmp_path) -> None:
    """--name and --all-files were dropped silently (exit 0): the block kept
    'auto', and the default QC filter kept skipping Sample_07.d."""
    watch = tmp_path / "tims"
    (watch / "Sample_07.d").mkdir(parents=True)
    (watch / "HeLa_QC_01.d").mkdir()
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [_old_setup_block(watch)]}))

    res = _cli("add-watch", str(watch), "--vendor", "bruker", "--name", "timsTOF HT", "--all-files", "-y")
    assert res.exit_code == 0, res.output
    (b,) = _yml(cfg_dir / "instruments.yml")["instruments"]
    assert b["name"] == "timsTOF HT"
    assert b["qc_only"] is False
    assert (b["vendor"], b["extensions"], b["hela_amount_ng"]) == ("bruker", [".d"], 50.0)
    assert b["output_dir"] == str(cfg_dir / "qc_output" / "timsTOF_HT")
    out = _norm(res.output)
    assert "changed name: auto -> timsTOF HT" in out
    # A rename reaches a running watcher by itself: the reload stops 'auto'
    # and starts 'timsTOF HT' with the new block.
    assert "restart it" not in out
    # The watcher now takes a non-QC-named run, as --all-files asked.
    assert _handler_events(b, watch / "Sample_08.d") == ["tracked_qc"]


def test_add_watch_sets_the_qc_pattern_of_an_existing_block(cfg_dir, tmp_path) -> None:
    cfg_dir.mkdir(parents=True)
    blk = {"name": "Exploris 480", "vendor": "thermo", "watch_dir": str(tmp_path),
           "extensions": [".raw"], "stable_secs": 30, "enabled": True, "qc_only": True,
           "output_dir": "/o"}
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [blk]}))

    res = _cli("add-watch", str(tmp_path), "--qc-pattern", "(?i)(k562|hela)", "-y")
    assert res.exit_code == 0, res.output
    (b,) = _yml(cfg_dir / "instruments.yml")["instruments"]
    assert b == {**blk, "qc_pattern": "(?i)(k562|hela)"}
    assert _handler_events(b, tmp_path / "K562_200ng_01.raw") == ["tracked_qc"]


def test_add_watch_rejects_an_invalid_qc_pattern(cfg_dir, tmp_path) -> None:
    """compile_qc_pattern never raises, so the old check let a typo through:
    it was written, and the watcher quietly used the default pattern."""
    res = _cli("add-watch", str(tmp_path), "--vendor", "thermo", "--qc-pattern", "(?i)(hela", "-y")
    assert res.exit_code == 2
    assert "Invalid regex" in res.output
    assert not (cfg_dir / "instruments.yml").exists()


def test_add_watch_refuses_what_it_cannot_apply_and_changes_nothing(cfg_dir, tmp_path) -> None:
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "instruments.yml"
    blk = {"name": "Lumos", "vendor": "thermo", "watch_dir": str(tmp_path),
           "extensions": [".raw"], "stable_secs": 30, "enabled": True, "output_dir": "/o"}
    path.write_text(yaml.safe_dump({"instruments": [blk]}))
    before = path.read_text()

    # A real name: its runs are stored under it, so a rename is not a flag's job.
    res = _cli("add-watch", str(tmp_path), "--name", "Lumos 2", "--all-files", "-y")
    assert res.exit_code == 1
    assert "fix-instrument-names --from 'Lumos' --to 'Lumos 2'" in _norm(res.output)
    res = _cli("add-watch", str(tmp_path), "--vendor", "bruker", "-y")
    assert res.exit_code == 1
    assert "--vendor bruker was not applied" in _norm(res.output)
    assert path.read_text() == before  # --all-files was not applied either


def test_add_watch_refuses_to_complete_blocks_that_share_a_name(cfg_dir, tmp_path, monkeypatch) -> None:
    """Two old `stan setup` blocks were both named auto. The daemon keys its
    watchers by name, so only one folder was ever watched, and completing
    either block (exit 0) could not rename it."""
    tims, exploris = tmp_path / "tims", tmp_path / "exploris"
    (tims / "HeLa_1.d").mkdir(parents=True)
    exploris.mkdir()
    cfg_dir.mkdir(parents=True)
    path = cfg_dir / "instruments.yml"
    path.write_text(yaml.safe_dump({"instruments": [_old_setup_block(tims), _old_setup_block(exploris)]}))
    before = path.read_text()
    assert _started_by_daemon(monkeypatch, _yml(path)) == ["auto"]  # one of two

    listing = _norm(_cli("list-watch").output)
    assert "2 enabled blocks are named 'auto'" in listing
    assert '--name "<model>"' in listing

    res = _cli("add-watch", str(tims), "--vendor", "bruker", "-y")
    assert res.exit_code == 1
    assert f"also used by the block for {exploris}" in _norm(res.output)
    assert path.read_text() == before

    res = _cli("add-watch", str(tims), "--vendor", "bruker", "--name", "timsTOF HT", "-y")
    assert res.exit_code == 0, res.output
    # 'auto' keeps running (for exploris now) with the settings it started with.
    assert "restart it" in _norm(res.output)
    res = _cli("add-watch", str(exploris), "--vendor", "thermo", "--name", "Exploris 480", "-y")
    assert res.exit_code == 0, res.output
    assert "restart it" not in _norm(res.output)  # the reload stops 'auto' now
    assert sorted(_started_by_daemon(monkeypatch, _yml(path))) == ["Exploris 480", "timsTOF HT"]
    assert "enabled blocks are named" not in _norm(_cli("list-watch").output)


def test_add_watch_says_when_a_running_watcher_must_restart(cfg_dir, tmp_path) -> None:
    """The daemon's hot-reload starts watchers for new names only. A block
    that was already enabled was running with no extensions, and completing
    it reached nothing until a restart; add-watch did not say so."""
    running, idle = tmp_path / "running", tmp_path / "idle"
    running.mkdir()
    idle.mkdir()
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "instruments.yml").write_text(yaml.safe_dump({"instruments": [
        {"name": "timsTOF HT", "watch_dir": str(running), "enabled": True},
        # An old add-watch block: never started (no enabled), so the reload starts it.
        {"name": "Exploris 480", "vendor": "thermo", "watch_dir": str(idle),
         "extensions": [".raw"], "stable_secs": 30},
    ]}))

    res = _cli("add-watch", str(running), "--vendor", "bruker", "-y")
    assert res.exit_code == 0, res.output
    assert "restart it" in _norm(res.output)

    res = _cli("add-watch", str(idle), "-y")
    assert res.exit_code == 0, res.output
    assert "restart it" not in _norm(res.output)


def test_add_watch_exits_nonzero_when_it_writes_nothing(cfg_dir, tmp_path) -> None:
    """Both used to print a message and exit 0, which an install script reads as done."""
    assert _cli("add-watch", str(tmp_path / "missing"), "--vendor", "thermo", "-y").exit_code == 1
    empty = tmp_path / "empty"
    empty.mkdir()
    assert _cli("add-watch", str(empty), "-y").exit_code == 1  # vendor cannot be told
    assert not (cfg_dir / "instruments.yml").exists()


# ── the Windows installer's instruments.yml ────────────────────────────


# Byte for byte what install_stan.ps1 step 6 writes (PS 5.1 `Out-File
# -Encoding utf8` puts a UTF-8 byte-order mark first); see test_setup.py.
INSTALLER_YML = (
    b"\xef\xbb\xbf# STAN instrument configuration\n"
    b"# Edit this file to add watch directories and instrument names.\n"
    b"instruments: []\n\n"
    b'diann_binary: "C:/Program Files/DIA-NN/2.3.0/diann.exe"\n'
    b'sage_binary: "C:/Users/qc/STAN/tools/sage/sage.exe"'
)


def test_add_watch_on_a_fresh_windows_install(tmp_path, monkeypatch) -> None:
    """install_stan.ps1 writes %USERPROFILE%\\.stan\\instruments.yml with a
    BOM; add-watch read it with the ANSI code page and exited 1 with
    'Could not update ...: expected <document start>'. The block now goes to
    %USERPROFILE%\\STAN\\instruments.yml, with the installer's keys."""
    import builtins
    import locale
    import platform

    import stan.config as cfg

    home = tmp_path / "home"
    (home / ".stan").mkdir(parents=True)
    legacy = home / ".stan" / "instruments.yml"
    legacy.write_bytes(INSTALLER_YML)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    monkeypatch.setattr(cfg, "_USER_CONFIG_DIR", home / "STAN")
    real_open = builtins.open

    def cp1252_open(file, mode="r", buffering=-1, encoding=None, *args, **kwargs):
        if "b" not in mode and encoding in (None, "locale"):
            encoding = "cp1252"  # Python on Windows outside UTF-8 mode
        return real_open(file, mode, buffering, encoding, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", cp1252_open)
    monkeypatch.setattr(locale, "getpreferredencoding", lambda do_setlocale=True: "cp1252")

    raw = tmp_path / "raw"
    raw.mkdir()
    res = _cli("add-watch", str(raw), "--vendor", "bruker", "--name", "timsTOF HT", "-y")
    assert res.exit_code == 0, res.output
    target = home / "STAN" / "instruments.yml"
    doc = _yml(target)
    assert doc["diann_binary"] == "C:/Program Files/DIA-NN/2.3.0/diann.exe"
    (b,) = doc["instruments"]
    assert (b["name"], b["vendor"], b["enabled"]) == ("timsTOF HT", "bruker", True)
    assert cfg.resolve_config_path("instruments.yml") == target
    assert legacy.read_bytes() == INSTALLER_YML
    out = _norm(res.output)
    assert "Copied the settings in" in out and "restart it" in out

    # list-watch reads it (and would have read the BOM file) too.
    listing = _cli("list-watch")
    assert listing.exit_code == 0 and "timsTOF HT" in listing.output
