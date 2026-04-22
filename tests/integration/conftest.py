"""Shared fixtures for integration tests against real Hive .d / .raw files.

The Hive quobyte NFS mount on macOS has restrictive read permissions
that block SQLite from opening files inside `.d/` directories (affects
alphatims, polars, and direct sqlite3). Workaround: every test copies
its target into a local temp dir before accessing it. Copies are
session-scoped so we only pay the cost once per pytest run.

Every integration test is marked `@pytest.mark.integration` and skips
gracefully when the Hive mount or a specific fixture file isn't
reachable - so `pytest -k 'not integration'` stays fast for the
day-to-day dev loop.

Test paths live in memory/reference_hive_test_files.md.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


# Hive quobyte mount candidates, checked in order.
HIVE_MOUNT_CANDIDATES = [
    Path("/Volumes/proteomics-grp"),          # Brett's dev mac
    Path("/quobyte/proteomics-grp"),          # direct on Hive
]

# Known-good test files with properties assertable by the tests.
HIVE_FIXTURES = {
    # Bruker timsTOF HT - clean HeLa DIA.
    "bruker_hela_dia_100spd": "hela_qcs/timstofHT/dia/03jun2024_HeLa50ng_DIA_100spd_S1-B2_1_6205.d",
    "bruker_hela_dia_60spd":  "hela_qcs/timstofHT/dia/040823_HeLa50ng_60spd_DIA_S1-A2_1_413.d",
    # Bruker timsTOF HT - DDA.
    "bruker_hela_dda":        "hela_qcs/timstofHT/dda/10mai_HeL50-Dda_100spd_S1-A2_1_5604.d",
    # Thermo Lumos - DIA.
    "thermo_lumos_dia":       "hela_qcs/lumos/FL030326_HeL50_35m_1OK-goo.raw",
    # Thermo Exploris 480 - DDA.
    "thermo_exploris_dda":    "hela_qcs/480/Ex040326_HeL50-aftLCPM_30m_1good.raw",
}


def _find_hive_root() -> Path | None:
    """Return the first reachable Hive quobyte root, or None."""
    for p in HIVE_MOUNT_CANDIDATES:
        if p.exists() and p.is_dir():
            if (p / "hela_qcs").exists() or (p / "brett").exists():
                return p
    return None


@pytest.fixture(scope="session")
def hive_root() -> Path:
    root = _find_hive_root()
    if root is None:
        pytest.skip(
            "Hive quobyte mount not available - run on a machine with "
            "/Volumes/proteomics-grp/ mounted or accessible via SSH."
        )
    return root


@pytest.fixture(scope="session")
def hive_fixture_path(hive_root: Path, tmp_path_factory):
    """Callable that resolves a HIVE_FIXTURES key to a LOCAL copy.

    Copies .d directories (recursive) or .raw files to a session-scoped
    temp dir to work around Mac NFS permission issues that block SQLite
    from opening files directly on the mount. Cached per-key so each
    fixture is copied at most once per pytest run.
    """
    cache: dict[str, Path] = {}
    staging = tmp_path_factory.mktemp("hive_fixtures")

    def _resolve(key: str) -> Path:
        if key in cache:
            return cache[key]
        if key not in HIVE_FIXTURES:
            raise KeyError(f"Unknown Hive fixture: {key}. "
                           f"Add to conftest.HIVE_FIXTURES.")
        remote = hive_root / HIVE_FIXTURES[key]
        if not remote.exists():
            pytest.skip(
                f"Hive fixture {key} not present at {remote}. "
                "File may have moved - update conftest.HIVE_FIXTURES."
            )
        local = staging / remote.name
        try:
            if remote.is_dir():
                shutil.copytree(str(remote), str(local))
            else:
                shutil.copy2(str(remote), str(local))
        except Exception as e:
            pytest.skip(
                f"Failed to stage Hive fixture {key} to {local}: {e}"
            )
        cache[key] = local
        return local
    return _resolve
