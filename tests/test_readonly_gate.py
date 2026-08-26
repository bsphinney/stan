"""The read-only gate must actually close the routes that matter.

These assert on the specific endpoints that make a public deployment
dangerous — /api/fleet/command dispatches commands to instrument PCs — so a
future refactor that quietly drops the gate fails here instead of in
production.
"""

import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stan.dashboard.readonly import install_readonly_gate, is_readonly


def _app():
    app = FastAPI()

    @app.get("/api/runs")
    async def runs():
        return [{"id": "r1"}]

    @app.post("/api/fleet/command")
    async def fleet_command():          # pragma: no cover - must never run
        return {"ok": True, "danger": "dispatched to an instrument PC"}

    @app.post("/api/thresholds")
    async def thresholds():             # pragma: no cover
        return {"ok": True}

    @app.delete("/api/instruments/0")
    async def del_instrument():         # pragma: no cover
        return {"ok": True}

    @app.get("/api/dashboard-errors")
    async def errors():                 # pragma: no cover
        return {"traces": ["secret"]}

    install_readonly_gate(app)
    return app


@pytest.fixture
def readonly(monkeypatch):
    monkeypatch.setenv("STAN_DASHBOARD_READONLY", "1")
    return TestClient(_app())


@pytest.fixture
def normal(monkeypatch):
    monkeypatch.delenv("STAN_DASHBOARD_READONLY", raising=False)
    return TestClient(_app())


def test_env_flag_parsing(monkeypatch):
    for val, want in [("1", True), ("true", True), ("YES", True), ("on", True),
                      ("0", False), ("", False), ("no", False)]:
        monkeypatch.setenv("STAN_DASHBOARD_READONLY", val)
        assert is_readonly() is want, val
    monkeypatch.delenv("STAN_DASHBOARD_READONLY", raising=False)
    assert is_readonly() is False


def test_reads_still_work(readonly):
    r = readonly.get("/api/runs")
    assert r.status_code == 200 and r.json() == [{"id": "r1"}]


@pytest.mark.parametrize("method,path", [
    ("post", "/api/fleet/command"),     # remote code execution on instruments
    ("post", "/api/thresholds"),        # overwrites thresholds.yml
    ("delete", "/api/instruments/0"),   # rewrites instruments.yml
])
def test_mutating_routes_refused(readonly, method, path):
    # request() rather than the verb helpers: httpx's .delete() takes no
    # json= kwarg, and the body is irrelevant to what we're asserting.
    r = readonly.request(method.upper(), path)
    assert r.status_code == 403, f"{method} {path} was NOT blocked"
    assert "read-only" in r.json()["detail"].lower()


def test_introspection_hidden(readonly):
    for p in ("/docs", "/openapi.json", "/api/dashboard-errors"):
        assert readonly.get(p).status_code == 404, p


def test_gate_off_by_default(normal):
    """A local operator install must be completely unaffected."""
    assert normal.request("POST", "/api/fleet/command").status_code == 200
    assert normal.get("/api/dashboard-errors").status_code == 200
