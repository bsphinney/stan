"""Community sync endpoints must survive a missing community.yml.

`load_community()` raises FileNotFoundError when no config exists. That is
the normal state of (a) a brand-new install and (b) the public read-only
host, so both endpoints have to treat it as an empty config. Letting it
escape made sync-status a 500 on the public dashboard and made the very
first sync from a fresh install impossible — which is exactly the case the
pseudonym-minting path was written for.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    from stan.dashboard import server

    return TestClient(server.app)


def _no_community_yml(monkeypatch):
    """Simulate a fresh install.

    Patching `load_community` to raise is deliberate: setting HOME does not
    isolate this, because `stan.config` resolves the user config dir at
    import time, so once any other test has imported it the lookup still
    finds the developer's real ~/.stan/community.yml. Patching the call
    tests the actual condition — the raise — rather than the environment.
    """
    def _boom():
        raise FileNotFoundError("community.yml not found")

    monkeypatch.setattr("stan.config.load_community", _boom)


def test_sync_status_without_community_yml(client, monkeypatch):
    """No community.yml must not 500 — it's a fresh install, not an error."""
    from stan.dashboard import server

    monkeypatch.setattr(server, "is_readonly", lambda: False)
    _no_community_yml(monkeypatch)
    r = client.get("/api/community/sync-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["display_name"] is None
    # A name is offered so the operator never publishes as "anonymous"
    # merely for having skipped setup.
    assert body["suggested_name"]
    assert body["readonly"] is False


def test_sync_status_readonly_host_short_circuits(client, monkeypatch):
    """The public host reports readonly without counting or minting."""
    from stan.dashboard import server

    monkeypatch.setattr(server, "is_readonly", lambda: True)
    r = client.get("/api/community/sync-status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["readonly"] is True
    assert body["pending"] == 0
    assert body["suggested_name"] is None


def test_load_community_cfg_swallows_missing_file(monkeypatch, tmp_path):
    """The helper returns {} rather than raising FileNotFoundError."""
    from stan.dashboard import server

    def _boom():
        raise FileNotFoundError("community.yml not found")

    monkeypatch.setattr("stan.config.load_community", _boom)
    assert server._load_community_cfg() == {}


def test_pending_excludes_washes_and_zero_id_runs():
    """The button's count must match what `stan submit-all` would push."""
    from stan.dashboard.server import _pending_community_runs

    rows = [
        {"id": "a", "run_name": "HeLa_QC_01.d", "n_precursors": 40000,
         "submitted_to_benchmark": 0},
        {"id": "b", "run_name": "wash_blank_02.d", "n_precursors": 30000,
         "submitted_to_benchmark": 0},
        {"id": "c", "run_name": "HeLa_QC_03.d", "n_precursors": 0,
         "submitted_to_benchmark": 0},
        {"id": "d", "run_name": "HeLa_QC_04.d", "n_precursors": 50000,
         "submitted_to_benchmark": 1},
    ]
    kept = {r["id"] for r in _pending_community_runs(rows)}
    assert "a" in kept
    assert "b" not in kept, "washes/blanks are not QC"
    assert "c" not in kept, "a zero-ID run is a failed search, not a result"
    assert "d" not in kept, "already submitted"
