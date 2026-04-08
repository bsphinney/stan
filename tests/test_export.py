"""Tests for export/import functionality."""

from __future__ import annotations

import json
import sqlite3
import tarfile
import zipfile
from pathlib import Path

import pytest

from stan.export import (
    CLAUDE_ANALYSIS_PROMPT,
    METRIC_SCHEMA,
    export_archive,
    export_claude,
    export_json,
    export_parquet,
    import_archive,
)


def _make_test_db(db_path: Path) -> None:
    """Create a minimal stan.db with a few test runs."""
    from stan.db import init_db, insert_run

    init_db(db_path=db_path)
    insert_run(
        instrument="Astral",
        run_name="test1.raw",
        raw_path="/fake/test1.raw",
        mode="dia",
        metrics={"n_precursors": 20000, "n_peptides": 15000, "n_proteins": 8000},
        gate_result="pass",
        amount_ng=50.0,
        spd=60,
        db_path=db_path,
    )
    insert_run(
        instrument="Astral",
        run_name="test2.raw",
        raw_path="/fake/test2.raw",
        mode="dia",
        metrics={"n_precursors": 22000, "n_peptides": 16000, "n_proteins": 8500},
        gate_result="pass",
        amount_ng=50.0,
        spd=60,
        db_path=db_path,
    )


def test_export_archive_creates_tarball(tmp_path, monkeypatch):
    """Export should create a valid .tar.gz with expected contents."""
    # Redirect STAN's config/db paths to tmp_path
    monkeypatch.setattr("stan.db._USER_CONFIG_DIR_OVERRIDE", tmp_path, raising=False)
    monkeypatch.setattr("stan.export.get_user_config_dir", lambda: tmp_path)
    monkeypatch.setattr("stan.export.get_db_path", lambda: tmp_path / "stan.db")

    _make_test_db(tmp_path / "stan.db")

    out = export_archive(output_path=tmp_path / "export.tar.gz")
    assert out.exists()

    with tarfile.open(out) as tar:
        names = tar.getnames()
        assert any("stan.db" in n for n in names)
        assert any("manifest.json" in n for n in names)


def test_export_json_includes_schema(tmp_path, monkeypatch):
    """JSON export should include the metric schema for LLM interpretation."""
    monkeypatch.setattr("stan.export.get_db_path", lambda: tmp_path / "stan.db")
    _make_test_db(tmp_path / "stan.db")

    out = export_json(output_path=tmp_path / "runs.json")
    assert out.exists()

    data = json.loads(out.read_text())
    assert "schema" in data
    assert "n_precursors" in data["schema"]
    assert "ips_score" in data["schema"]
    assert data["n_runs"] == 2
    assert len(data["runs"]) == 2


def test_export_parquet(tmp_path, monkeypatch):
    """Parquet export should produce a readable file."""
    import polars as pl
    monkeypatch.setattr("stan.export.get_db_path", lambda: tmp_path / "stan.db")
    _make_test_db(tmp_path / "stan.db")

    out = export_parquet(output_path=tmp_path / "runs.parquet")
    assert out.exists()

    df = pl.read_parquet(out)
    assert df.shape[0] == 2
    assert "n_precursors" in df.columns


def test_export_claude_includes_prompt(tmp_path, monkeypatch):
    """Claude export should be a zip with the analysis prompt + data."""
    monkeypatch.setattr("stan.export.get_db_path", lambda: tmp_path / "stan.db")
    monkeypatch.setattr("stan.export.get_user_config_dir", lambda: tmp_path)
    _make_test_db(tmp_path / "stan.db")

    out = export_claude(output_path=tmp_path / "for_claude.zip")
    assert out.exists()

    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
        assert "README_START_HERE.md" in names
        assert "stan_runs.json" in names
        assert "manifest.json" in names

        # The prompt should mention Claude and the schema
        prompt = zf.read("README_START_HERE.md").decode()
        assert "STAN" in prompt
        assert "schema" in prompt.lower()
        assert "figures" in prompt.lower()


def test_import_archive_roundtrip(tmp_path, monkeypatch):
    """Export then import should preserve runs and dedup on re-import."""
    # Setup source DB
    db_path = tmp_path / "stan.db"
    monkeypatch.setattr("stan.export.get_db_path", lambda: db_path)
    monkeypatch.setattr("stan.export.get_user_config_dir", lambda: tmp_path)
    monkeypatch.setattr("stan.db.get_db_path", lambda: db_path)

    _make_test_db(db_path)

    # Export
    archive = export_archive(output_path=tmp_path / "export.tar.gz")

    # Wipe the DB
    db_path.unlink()

    # Import
    result = import_archive(archive)
    assert result["imported"] == 2
    assert result["skipped"] == 0

    # Re-import should skip all as duplicates
    result2 = import_archive(archive)
    assert result2["imported"] == 0
    assert result2["skipped"] == 2


def test_metric_schema_covers_critical_fields():
    """The exported schema must document all primary/secondary metrics."""
    required = [
        "n_precursors", "n_peptides", "n_proteins", "n_psms",
        "ips_score", "median_points_across_peak",
        "instrument", "mode", "amount_ng", "spd",
    ]
    for field in required:
        assert field in METRIC_SCHEMA, f"Missing schema entry for {field}"


def test_claude_prompt_is_specific():
    """The prompt should contain specific instructions, not just generic text."""
    prompt = CLAUDE_ANALYSIS_PROMPT
    # Must mention figures
    assert "figures" in prompt.lower() or "matplotlib" in prompt.lower() or "plotly" in prompt.lower()
    # Must mention the metric hierarchy
    assert "precursor" in prompt.lower()
    # Must tell Claude what to produce
    assert "report" in prompt.lower()
    # Must warn about protein count being confounded
    assert "protein" in prompt.lower()
