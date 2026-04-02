"""Shared pytest fixtures for STAN tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml


@pytest.fixture()
def tmp_stan_dir(tmp_path: Path) -> Path:
    """Create a temporary ~/.stan/ equivalent for isolated testing."""
    stan_dir = tmp_path / ".stan"
    stan_dir.mkdir()
    return stan_dir


@pytest.fixture()
def sample_instruments_config() -> dict:
    """Return a sample instruments.yml structure."""
    return {
        "hive": {
            "host": "hive.ucdavis.edu",
            "user": "testuser",
            "key_path": "~/.ssh/id_rsa",
        },
        "instruments": [
            {
                "name": "timsTOF Ultra",
                "vendor": "bruker",
                "model": "timsTOF Ultra",
                "watch_dir": "/tmp/test_watch",
                "output_dir": "/tmp/test_output",
                "extensions": [".d"],
                "stable_secs": 60,
                "enabled": True,
                "qc_modes": ["dia", "dda"],
                "hive_partition": "high",
                "hive_account": "test-grp",
                "hive_mem": "32G",
                "community_submit": False,
            },
        ],
    }


@pytest.fixture()
def sample_thresholds_config() -> dict:
    """Return a sample thresholds.yml structure."""
    return {
        "thresholds": {
            "default": {
                "dia": {"n_precursors_min": 5000, "median_cv_precursor_max": 20.0},
                "dda": {"n_psms_min": 10000},
            },
        },
    }


@pytest.fixture()
def mock_d_dir(tmp_path: Path) -> Path:
    """Create a minimal Bruker .d directory with a fake analysis.tdf SQLite.

    The Frames table contains MsmsType values for testing mode detection.
    """
    d_dir = tmp_path / "test_run.d"
    d_dir.mkdir()

    tdf_path = d_dir / "analysis.tdf"
    con = sqlite3.connect(str(tdf_path))
    con.execute("CREATE TABLE Frames (Id INTEGER, MsmsType INTEGER)")
    con.execute("INSERT INTO Frames VALUES (1, 0)")   # MS1
    con.execute("INSERT INTO Frames VALUES (2, 9)")   # diaPASEF
    con.execute("INSERT INTO Frames VALUES (3, 9)")   # diaPASEF
    con.commit()
    con.close()

    # Add a small binary file to give the .d directory some size
    (d_dir / "analysis.tdf_bin").write_bytes(b"\x00" * 1024)

    return d_dir


@pytest.fixture()
def mock_d_dir_dda(tmp_path: Path) -> Path:
    """Create a minimal Bruker .d directory with ddaPASEF MsmsType."""
    d_dir = tmp_path / "test_dda_run.d"
    d_dir.mkdir()

    tdf_path = d_dir / "analysis.tdf"
    con = sqlite3.connect(str(tdf_path))
    con.execute("CREATE TABLE Frames (Id INTEGER, MsmsType INTEGER)")
    con.execute("INSERT INTO Frames VALUES (1, 0)")   # MS1
    con.execute("INSERT INTO Frames VALUES (2, 8)")   # ddaPASEF
    con.commit()
    con.close()

    (d_dir / "analysis.tdf_bin").write_bytes(b"\x00" * 512)

    return d_dir


@pytest.fixture()
def mock_raw_file(tmp_path: Path) -> Path:
    """Create a minimal placeholder .raw file for path-based tests."""
    raw = tmp_path / "test_run.raw"
    raw.write_bytes(b"\x00" * 2048)
    return raw


@pytest.fixture()
def config_dir_with_files(tmp_path: Path, sample_instruments_config, sample_thresholds_config):
    """Create a temporary config directory with instruments.yml and thresholds.yml."""
    config_dir = tmp_path / ".stan"
    config_dir.mkdir()

    instruments_path = config_dir / "instruments.yml"
    instruments_path.write_text(yaml.dump(sample_instruments_config))

    thresholds_path = config_dir / "thresholds.yml"
    thresholds_path.write_text(yaml.dump(sample_thresholds_config))

    community_path = config_dir / "community.yml"
    community_path.write_text(yaml.dump({"hf_token": "", "display_name": "Test Lab"}))

    return config_dir
