"""The CSRF Origin gate on state-changing dashboard requests.

The gate 403s any POST/PUT/DELETE/PATCH whose Origin isn't ours. It runs
ahead of auth, so before 2026-09-02 it knew only about localhost and
whatever STAN_DASHBOARD_EXTRA_ORIGINS listed — and the hosted dashboard
at https://ucd.stan-proteomics.org, whose origin nobody had added, had
every maintenance-log save rejected at a signed-in browser.

What these lock down is the shape of the fix: a request whose Origin
matches the origin the app is *being served on* is same-origin and
passes, and nothing else got looser. The probe path deliberately does
not exist — the middleware wraps routing, so a request that clears the
gate falls through to 404 and the assertions never touch the database.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from stan.dashboard.server import app

PROBE = "/api/__origin_gate_probe__"
HOSTED = "ucd.stan-proteomics.org"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def test_same_origin_behind_a_tls_terminating_proxy_is_allowed(client):
    """The production case: Azure terminates TLS, so the container sees
    an http scheme while the browser sends an https Origin. Without
    honouring X-Forwarded-Proto the hosted app never matches itself."""
    r = client.post(
        PROBE,
        headers={
            "Host": HOSTED,
            "X-Forwarded-Proto": "https",
            "Origin": f"https://{HOSTED}",
        },
    )
    assert r.status_code != 403
    assert r.status_code == 404


def test_same_origin_without_a_proxy_is_allowed(client):
    """Plain uvicorn on a LAN box: no X-Forwarded-Proto, scheme comes
    from the request itself."""
    r = client.post(
        PROBE,
        headers={"Host": "lumosrox.local:8421", "Origin": "http://lumosrox.local:8421"},
    )
    assert r.status_code == 404


def test_a_foreign_origin_is_still_rejected(client):
    """The whole point of the gate. An attacker page's fetch carries our
    Host (the browser writes it from the URL) but its own Origin."""
    r = client.post(
        PROBE,
        headers={
            "Host": HOSTED,
            "X-Forwarded-Proto": "https",
            "Origin": "https://evil.example",
        },
    )
    assert r.status_code == 403
    assert "evil.example" in r.json()["detail"]


def test_x_forwarded_host_cannot_be_used_to_declare_an_origin(client):
    """X-Forwarded-Host is settable by any non-browser client. Honouring
    it would let a caller name its own origin as ours and walk through."""
    r = client.post(
        PROBE,
        headers={
            "Host": HOSTED,
            "X-Forwarded-Host": "evil.example",
            "X-Forwarded-Proto": "https",
            "Origin": "https://evil.example",
        },
    )
    assert r.status_code == 403


def test_the_scheme_must_match_too(client):
    """No scheme wildcard: https-served app, http Origin, still 403."""
    r = client.post(
        PROBE,
        headers={
            "Host": HOSTED,
            "X-Forwarded-Proto": "https",
            "Origin": f"http://{HOSTED}",
        },
    )
    assert r.status_code == 403


def test_a_missing_origin_is_still_allowed(client):
    """CLI clients (curl, requests) send no Origin and must keep working."""
    r = client.post(PROBE, headers={"Host": HOSTED})
    assert r.status_code == 404


def test_the_builtin_localhost_origins_still_pass(client):
    r = client.post(
        PROBE, headers={"Host": "127.0.0.1:8421", "Origin": "http://localhost:8421"}
    )
    assert r.status_code == 404


def test_extra_origins_env_var_still_admits_its_hosts(monkeypatch):
    """STAN_DASHBOARD_EXTRA_ORIGINS is read at import time, so this loads
    a private copy of the module rather than reloading the shared one
    other test modules have already bound `app` from."""
    monkeypatch.setenv(
        "STAN_DASHBOARD_EXTRA_ORIGINS",
        "http://lumosrox.tail-foo-bar.ts.net:8421,https://godmode.stan-proteomics.org",
    )
    path = Path(__file__).resolve().parents[1] / "stan" / "dashboard" / "server.py"
    spec = importlib.util.spec_from_file_location("_stan_server_envprobe", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
        assert "https://godmode.stan-proteomics.org" in module._DASHBOARD_ORIGINS
        c = TestClient(module.app)
        # A tunnelled origin reaching the app under a different Host is
        # exactly what the env var exists for — same-origin wouldn't save it.
        r = c.post(
            PROBE,
            headers={
                "Host": "stan-ucd-proteomics.azurewebsites.net",
                "Origin": "https://godmode.stan-proteomics.org",
            },
        )
        assert r.status_code == 404
        r = c.post(
            PROBE,
            headers={
                "Host": "stan-ucd-proteomics.azurewebsites.net",
                "Origin": "https://evil.example",
            },
        )
        assert r.status_code == 403
    finally:
        sys.modules.pop(spec.name, None)
