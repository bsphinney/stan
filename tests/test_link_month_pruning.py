"""The Flinders linker must only descend into recently-created month dirs.

The export holds ~9,310 `.d` directories across 33 month dirs. Walking all
of them cost an NFS stat() each — a bare `find` over the tree took 132 s,
which is the ~90-122 s the linker spent per cron tick to discover that
nothing had changed in 2025. Pruning to the recently-created dirs took the
same walk to ~1 s.

Selection is by creation time read via `stat -c %W`, not os.stat():
CPython does not expose st_birthtime on Linux before 3.12 and Hive runs
3.11, so reading it in Python there silently disables the pruning. These
tests mock the subprocess so they also pass on macOS, whose BSD stat has
no -c flag at all.
"""

from __future__ import annotations

import subprocess

import pytest

from stan.community.scripts import link_flinders_qc as mod

NOW = 1_787_000_000.0
DAY = 86400.0


@pytest.fixture
def tree(tmp_path):
    for name in ("Aug26", "JUL26", "June26", "Libraries", "Service"):
        (tmp_path / name).mkdir()
    (tmp_path / "desktop.ini").write_text("x")
    return tmp_path


def _fake_stat(births: dict[str, float]):
    """Stand in for `stat -c %W|%n`, echoing the births we specify."""
    def run(cmd, **kw):
        paths = cmd[3:]
        out = "\n".join(
            f"{births.get(p.rsplit('/', 1)[-1], 0.0):.0f}|{p}" for p in paths
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return run


def test_keeps_only_recently_created_dirs(tree, monkeypatch):
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    monkeypatch.setattr(mod.subprocess, "run", _fake_stat({
        "Aug26": NOW - 5 * DAY,
        "JUL26": NOW - 40 * DAY,
        "June26": NOW - 200 * DAY,
    }))
    keep = mod._recent_month_dirs(tree, window_days=31, keep_newest=1)
    assert "Aug26" in keep
    assert "June26" not in keep, "a 200-day-old month must not be walked"


def test_non_data_dirs_are_never_walked(tree, monkeypatch):
    """Service/ held tune files that failed dispatch on every tick."""
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    monkeypatch.setattr(mod.subprocess, "run", _fake_stat({
        "Aug26": NOW - 1 * DAY,
        "JUL26": NOW - 40 * DAY,
        "June26": NOW - 200 * DAY,
        "Service": NOW - 1 * DAY,   # recent, but still not acquisitions
        "Libraries": NOW - 1 * DAY,
    }))
    keep = mod._recent_month_dirs(tree, window_days=31)
    assert "Aug26" in keep
    assert "Service" not in keep
    assert "Libraries" not in keep


def test_month_boundary_always_keeps_newest(tree, monkeypatch):
    """Nothing created recently must still walk the newest dirs, not zero."""
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    monkeypatch.setattr(mod.subprocess, "run", _fake_stat({
        "Aug26": NOW - 90 * DAY,
        "JUL26": NOW - 120 * DAY,
        "June26": NOW - 150 * DAY,
    }))
    keep = mod._recent_month_dirs(tree, window_days=31, keep_newest=2)
    assert keep == {"Aug26", "JUL26"}, "never select nothing"


def test_a_single_undated_dir_is_kept_not_a_full_walk(tree, monkeypatch):
    """One unreadable dir costs one extra dir, not the whole archive."""
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    monkeypatch.setattr(mod.subprocess, "run", _fake_stat({
        "Aug26": NOW - 1 * DAY,
        "JUL26": NOW - 400 * DAY,
        # June26 omitted -> birth 0 -> unreadable
    }))
    keep = mod._recent_month_dirs(tree, window_days=31, keep_newest=1)
    assert "Aug26" in keep
    assert "June26" in keep, "unknown creation time must not skip data"
    assert "JUL26" not in keep, "known-old dirs are still pruned"


def test_missing_creation_time_walks_everything(tree, monkeypatch):
    """A filesystem with no birth time at all must not skip new data."""
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    monkeypatch.setattr(mod.subprocess, "run", _fake_stat({}))  # all zeros
    assert mod._recent_month_dirs(tree, window_days=31) is None


def test_stat_failure_falls_back_to_full_walk(tree, monkeypatch):
    def boom(*a, **k):
        raise OSError("stat missing")
    monkeypatch.setattr(mod.time, "time", lambda: NOW)
    monkeypatch.setattr(mod.subprocess, "run", boom)
    assert mod._recent_month_dirs(tree, window_days=31) is None
