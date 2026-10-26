"""The instruments' clock on a host with no tz database.

`stan.notify` imports `stan.instrument_time` at module level, so a Windows
single-lab install without the `tzdata` package logged a "no tz database"
WARNING on every import, whether or not it ever read an instrument time.
Windows installs now get `tzdata`, and the fallback only warns (once) when an
instrument-local time is actually read in it.
"""

from __future__ import annotations

import importlib.util
import logging
import re
import zoneinfo
from datetime import timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load_without_tz(monkeypatch):
    """A private copy of stan.instrument_time, as if no tz database existed."""
    def no_tz(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", no_tz)
    spec = importlib.util.spec_from_file_location(
        "_instrument_time_no_tz", REPO / "stan" / "instrument_time.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_import_without_a_tz_database_is_quiet(monkeypatch, caplog):
    with caplog.at_level(logging.DEBUG):
        mod = _load_without_tz(monkeypatch)
    assert mod.INSTRUMENT_TZ is timezone.utc
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING], caplog.text


def test_reading_a_local_time_in_the_fallback_warns_once(monkeypatch, caplog):
    mod = _load_without_tz(monkeypatch)
    caplog.clear()                      # only what reading a time says
    with caplog.at_level(logging.WARNING):
        first = mod.parse_local("2026-10-24T02:04:24")
        mod.parse_local("2026-10-24T03:04:24")
    assert first.tzinfo is timezone.utc
    warned = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warned) == 1, caplog.text
    assert "tz database" in warned[0].getMessage()


def test_an_aware_time_needs_no_tz_database(monkeypatch, caplog):
    mod = _load_without_tz(monkeypatch)
    with caplog.at_level(logging.WARNING):
        mod.parse_local("2026-10-24T09:04:24+00:00")
    assert not caplog.records, caplog.text


def test_with_a_tz_database_nothing_is_said(caplog):
    from stan import instrument_time
    with caplog.at_level(logging.WARNING):
        instrument_time.parse_local("2026-10-24T02:04:24")
    assert not caplog.records, caplog.text


def test_windows_installs_get_tzdata():
    tomllib = pytest.importorskip("tomllib")
    deps = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["dependencies"]
    pattern = re.compile(r"""tzdata\b[^;]*;\s*sys_platform\s*==\s*["']win32["']\s*$""")
    assert any(pattern.match(d) for d in deps), deps
