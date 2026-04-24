"""Tests for stan.metrics.features — 4DFF binary management + .features reader.

These tests do NOT download anything or run uff-cmdline2. They exercise
the pure-Python surfaces (hash verification, file discovery, SQLite
read). The Hive end-to-end test is performed out-of-band.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from stan.metrics import features as feat_mod
from stan.metrics.features import (
    _ALPHAPEPT_PINNED_SHA,
    find_features_file,
    is_4dff_installed,
    read_features,
)


# ─────────────────────────────────────────────────────────────
#  Sanity
# ─────────────────────────────────────────────────────────────

def test_pinned_sha_looks_right():
    """Pinned SHA must be a 40-char lowercase hex string."""
    assert len(_ALPHAPEPT_PINNED_SHA) == 40
    assert all(c in "0123456789abcdef" for c in _ALPHAPEPT_PINNED_SHA)


# ─────────────────────────────────────────────────────────────
#  find_features_file
# ─────────────────────────────────────────────────────────────

def test_find_features_file_missing(tmp_path):
    d = tmp_path / "run01.d"
    d.mkdir()
    assert find_features_file(d) is None


def test_find_features_file_inside_d(tmp_path):
    """4DFF's default: <d>/<stem>.features"""
    d = tmp_path / "run01.d"
    d.mkdir()
    feat = d / "run01.features"
    feat.write_bytes(b"")  # touch
    assert find_features_file(d) == feat


def test_find_features_file_sibling(tmp_path):
    """Older 4DFF: <parent>/<stem>.features"""
    d = tmp_path / "run01.d"
    d.mkdir()
    sibling = tmp_path / "run01.features"
    sibling.write_bytes(b"")
    assert find_features_file(d) == sibling


def test_find_features_file_prefers_inside(tmp_path):
    """If both exist, the one INSIDE the .d wins (matches 4DFF default)."""
    d = tmp_path / "run01.d"
    d.mkdir()
    inside = d / "run01.features"
    inside.write_bytes(b"")
    sibling = tmp_path / "run01.features"
    sibling.write_bytes(b"")
    assert find_features_file(d) == inside


# ─────────────────────────────────────────────────────────────
#  read_features against a synthetic SQLite LcTimsMsFeature table
# ─────────────────────────────────────────────────────────────

def _make_synthetic_features(path: Path, rows: list[tuple]) -> None:
    """Build a minimal LcTimsMsFeature SQLite DB for tests.

    Schema mirrors the real 4DFF output columns we care about.
    """
    con = sqlite3.connect(str(path))
    con.execute(
        """CREATE TABLE LcTimsMsFeature (
            Id INTEGER PRIMARY KEY,
            MZ REAL,
            Charge INTEGER,
            RT REAL,
            RT_lower REAL,
            RT_upper REAL,
            Mobility REAL,
            Mobility_lower REAL,
            Mobility_upper REAL,
            Intensity REAL,
            ClusterCount INTEGER
        )"""
    )
    con.executemany(
        "INSERT INTO LcTimsMsFeature VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def test_read_features_returns_polars_df(tmp_path):
    feat = tmp_path / "run01.features"
    _make_synthetic_features(feat, [
        # (Id, MZ, Charge, RT, RTlo, RThi, Mob, Moblo, Mobhi, Int, ClusterCount)
        (1, 400.5, 2, 600.0, 590.0, 610.0, 0.90, 0.88, 0.92, 1e5, 3),
        (2, 500.7, 2, 700.0, 690.0, 710.0, 0.95, 0.93, 0.97, 2e5, 4),
        (3, 800.2, 1, 800.0, 790.0, 810.0, 1.25, 1.22, 1.28, 5e4, 2),
    ])
    df = read_features(feat)
    assert df.height == 3
    assert "MZ" in df.columns
    assert "Charge" in df.columns

    # column subsetting
    df2 = read_features(feat, columns=["MZ", "Charge", "Mobility"])
    assert df2.columns == ["MZ", "Charge", "Mobility"]


def test_read_features_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_features(tmp_path / "nonexistent.features")


# ─────────────────────────────────────────────────────────────
#  is_4dff_installed
# ─────────────────────────────────────────────────────────────

def test_is_4dff_installed_false_on_empty_dir(tmp_path, monkeypatch):
    """Fresh install dir → not installed."""
    # Redirect get_user_config_dir to a fresh tmp so we don't touch the real one.
    import stan.config as cfg
    monkeypatch.setattr(cfg, "get_user_config_dir", lambda: tmp_path)
    assert is_4dff_installed(platform="linux") is False


def test_is_4dff_installed_false_on_wrong_sha(tmp_path, monkeypatch):
    """Files present but with wrong content → SHA mismatch → not installed."""
    import stan.config as cfg
    monkeypatch.setattr(cfg, "get_user_config_dir", lambda: tmp_path)

    # Create all the expected filenames with garbage content so the
    # SHA check must fail.
    install_dir = tmp_path / "bruker_ff" / "linux"
    install_dir.mkdir(parents=True)
    for rel in (
        "proteomics_4d.config",
        "THIRD-PARTY-LICENSE-README.txt",
        "linux64/uff-cmdline2",
        "linux64/libtbb.so.2",
    ):
        flat = install_dir / Path(rel).name
        flat.write_bytes(b"garbage")
    assert is_4dff_installed(platform="linux") is False


# ─────────────────────────────────────────────────────────────
#  detect_feature_drift — no features → unknown
# ─────────────────────────────────────────────────────────────

def test_detect_feature_drift_no_features_returns_unknown(tmp_path):
    """detect_feature_drift on a .d with no .features next to it
    must return DriftResult(drift_class='unknown')."""
    from stan.metrics.feature_drift import detect_feature_drift
    d = tmp_path / "run01.d"
    d.mkdir()
    r = detect_feature_drift(d)
    assert r.drift_class == "unknown"
    assert r.n_windows == 0


# ─────────────────────────────────────────────────────────────
#  install_4dff with mocked httpx — verifies the flow without
#  actually downloading 100 MB.
# ─────────────────────────────────────────────────────────────

def test_install_4dff_mocked(tmp_path, monkeypatch):
    """Monkey-patch httpx.stream + SHA check to simulate install.

    We build fake file contents, overwrite the expected SHA table to
    match those contents, and confirm install_4dff lays them out in
    the right place and returns the binary path.
    """
    import hashlib

    import stan.config as cfg
    monkeypatch.setattr(cfg, "get_user_config_dir", lambda: tmp_path)

    payloads = {
        "proteomics_4d.config": b"config-body",
        "THIRD-PARTY-LICENSE-README.txt": b"license-body",
        "linux64/uff-cmdline2": b"fake-binary-body",
        "linux64/libtbb.so.2": b"fake-tbb-body",
    }
    fake_shared = {
        "proteomics_4d.config": (
            hashlib.sha256(payloads["proteomics_4d.config"]).hexdigest(),
            len(payloads["proteomics_4d.config"]),
        ),
        "THIRD-PARTY-LICENSE-README.txt": (
            hashlib.sha256(payloads["THIRD-PARTY-LICENSE-README.txt"]).hexdigest(),
            len(payloads["THIRD-PARTY-LICENSE-README.txt"]),
        ),
    }
    fake_linux = {
        "linux64/uff-cmdline2": (
            hashlib.sha256(payloads["linux64/uff-cmdline2"]).hexdigest(),
            len(payloads["linux64/uff-cmdline2"]),
        ),
        "linux64/libtbb.so.2": (
            hashlib.sha256(payloads["linux64/libtbb.so.2"]).hexdigest(),
            len(payloads["linux64/libtbb.so.2"]),
        ),
    }
    monkeypatch.setattr(feat_mod, "_SHARED_FILES", fake_shared)
    monkeypatch.setattr(feat_mod, "_LINUX_FILES", fake_linux)

    class FakeCtx:
        def __init__(self, payload):
            self.payload = payload
            self.status_code = 200
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def raise_for_status(self):
            pass
        def iter_bytes(self, chunk_size):
            yield self.payload

    def fake_stream(method, url, **kwargs):
        # Map URL tail back to payload key
        for rel, body in payloads.items():
            if url.endswith("/" + rel):
                return FakeCtx(body)
        raise AssertionError(f"Unexpected URL: {url}")

    import httpx
    monkeypatch.setattr(httpx, "stream", fake_stream)

    binary = feat_mod.install_4dff(platform="linux")
    assert binary.exists()
    assert binary.name == "uff-cmdline2"
    # is_4dff_installed should now report True (with patched SHAs).
    assert feat_mod.is_4dff_installed(platform="linux") is True
