"""The Evosep column-health endpoint's delivery contract.

The document reaches the dashboard by two routes: PG Farm on the hosted site
(where the Hive publisher upserts it) and a JSON cache in the config dir
everywhere else. What matters is that a hosted read failure degrades to the
file rather than to an error page, and that a fresh install with no extract
yet answers 404 so the panel can hide itself instead of rendering an empty
shell.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stan.dashboard.server import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _doc() -> dict:
    return {"summary": {"n_runs": 3}, "runs": [{"run": "a"}], "column_pump": "Pump-HP"}


def test_missing_cache_is_404_not_500(client, monkeypatch):
    """No extract anywhere -> 404, which the panel treats as 'not configured'."""
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: False)

    def _missing(_name):
        raise FileNotFoundError

    monkeypatch.setattr("stan.dashboard.server.resolve_config_path", _missing)
    r = client.get("/api/maintenance/evosep")
    assert r.status_code == 404


def test_reads_the_file_cache_when_pg_is_off(client, monkeypatch, tmp_path: Path):
    """A local install has no PG; it must serve the bundled JSON."""
    p = tmp_path / "evosep_column_health.json"
    p.write_text(json.dumps(_doc()))
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: False)
    monkeypatch.setattr("stan.dashboard.server.resolve_config_path", lambda _n: p)

    r = client.get("/api/maintenance/evosep")
    assert r.status_code == 200
    assert r.json()["summary"]["n_runs"] == 3
    assert r.json()["column_pump"] == "Pump-HP"


def test_pg_document_wins_over_the_file(client, monkeypatch, tmp_path: Path):
    """On the hosted site the freshly published doc must beat the bundled one."""
    p = tmp_path / "evosep_column_health.json"
    p.write_text(json.dumps(_doc()))
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: True)
    monkeypatch.setattr("stan.db_pg.get_evosep_column_health_pg",
                        lambda: {"summary": {"n_runs": 99}, "runs": []})
    monkeypatch.setattr("stan.dashboard.server.resolve_config_path", lambda _n: p)

    r = client.get("/api/maintenance/evosep")
    assert r.status_code == 200
    assert r.json()["summary"]["n_runs"] == 99


def test_pg_failure_falls_back_to_the_file(client, monkeypatch, tmp_path: Path):
    """A PG outage must not take the panel down — the cache still answers."""
    p = tmp_path / "evosep_column_health.json"
    p.write_text(json.dumps(_doc()))
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: True)

    def _boom():
        raise RuntimeError("pg down")

    monkeypatch.setattr("stan.db_pg.get_evosep_column_health_pg", _boom)
    monkeypatch.setattr("stan.dashboard.server.resolve_config_path", lambda _n: p)

    r = client.get("/api/maintenance/evosep")
    assert r.status_code == 200
    assert r.json()["summary"]["n_runs"] == 3


def test_pg_empty_falls_back_to_the_file(client, monkeypatch, tmp_path: Path):
    """Table exists but the publisher has not run yet -> use the bundled doc."""
    p = tmp_path / "evosep_column_health.json"
    p.write_text(json.dumps(_doc()))
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: True)
    monkeypatch.setattr("stan.db_pg.get_evosep_column_health_pg", lambda: None)
    monkeypatch.setattr("stan.dashboard.server.resolve_config_path", lambda _n: p)

    r = client.get("/api/maintenance/evosep")
    assert r.status_code == 200
    assert r.json()["summary"]["n_runs"] == 3


def test_unreadable_cache_is_503(client, monkeypatch, tmp_path: Path):
    """Corrupt JSON is a server-side problem, not a missing feature."""
    p = tmp_path / "evosep_column_health.json"
    p.write_text("{not json")
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: False)
    monkeypatch.setattr("stan.dashboard.server.resolve_config_path", lambda _n: p)

    r = client.get("/api/maintenance/evosep")
    assert r.status_code == 503


def test_bundled_config_document_is_valid_and_real():
    """The JSON shipped in config/ must actually parse and carry real signals.

    Guards the bundle step: a truncated copy would render an empty panel on
    every install that has no PG.
    """
    p = Path(__file__).resolve().parents[1] / "config" / "evosep_column_health.json"
    if not p.exists():
        pytest.skip("no bundled Evosep document in this checkout")
    doc = json.loads(p.read_text())
    assert doc["column_pump"] == "Pump-HP"
    assert doc["summary"]["n_runs"] > 0
    assert doc["methods"], "no per-method baselines"
    # Column health is meaningless without at least one analytical gradient.
    assert any(m.get("analytical") for m in doc["methods"].values())
