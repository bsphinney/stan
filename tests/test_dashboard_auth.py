"""The hosted dashboard's write gate must be fail-closed.

The public STAN site is open by design — anyone may read the QC data. What
must never be open is anything acting on the lab's behalf: publishing runs
to the community benchmark under the lab's pseudonym. A signed-in,
allow-listed operator gets that back; nobody else does, under any
combination of missing config, spoofed headers, or partial sign-in.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient


def _principal_header(upn: str | None = None, groups: list[str] | None = None) -> str:
    claims = []
    if upn:
        claims.append({"typ": "preferred_username", "val": upn})
    for g in groups or []:
        claims.append({"typ": "groups", "val": g})
    blob = {"auth_typ": "aad", "claims": claims, "userDetails": upn or ""}
    return base64.b64encode(json.dumps(blob).encode()).decode()


class _Req:
    def __init__(self, headers: dict):
        self.headers = {k.lower(): v for k, v in headers.items()}


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("STAN_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("STAN_REQUIRED_GROUP", raising=False)


def test_anonymous_is_not_privileged():
    from stan.dashboard.auth import is_privileged
    assert is_privileged(_Req({})) is False


def test_signed_in_but_no_allowlist_configured_is_refused(monkeypatch):
    """Authenticated must never imply authorized: anyone can get a Microsoft
    account, so an unset allow-list has to fail closed, not open."""
    from stan.dashboard.auth import is_privileged
    req = _Req({"x-ms-client-principal": _principal_header("stranger@example.com"),
                "x-ms-client-principal-name": "stranger@example.com"})
    assert is_privileged(req) is False


def test_signed_in_and_allow_listed_is_privileged(monkeypatch):
    from stan.dashboard.auth import is_privileged
    monkeypatch.setenv("STAN_ALLOWED_USERS", "bsphinney@ucdavis.edu, other@ucdavis.edu")
    req = _Req({"x-ms-client-principal": _principal_header("bsphinney@ucdavis.edu"),
                "x-ms-client-principal-name": "bsphinney@ucdavis.edu"})
    assert is_privileged(req) is True


def test_signed_in_but_not_on_the_list_is_refused(monkeypatch):
    from stan.dashboard.auth import is_privileged
    monkeypatch.setenv("STAN_ALLOWED_USERS", "bsphinney@ucdavis.edu")
    req = _Req({"x-ms-client-principal": _principal_header("intruder@ucdavis.edu"),
                "x-ms-client-principal-name": "intruder@ucdavis.edu"})
    assert is_privileged(req) is False


def test_principal_name_header_alone_is_not_enough(monkeypatch):
    """The name header without the signed principal blob must not authorize.

    Easy Auth strips client-supplied X-MS-CLIENT-PRINCIPAL* headers, but the
    gate should not depend on that being true to stay closed.
    """
    from stan.dashboard.auth import is_privileged
    monkeypatch.setenv("STAN_ALLOWED_USERS", "bsphinney@ucdavis.edu")
    req = _Req({"x-ms-client-principal-name": "bsphinney@ucdavis.edu"})
    assert is_privileged(req) is False


def test_malformed_principal_is_refused(monkeypatch):
    from stan.dashboard.auth import is_privileged
    monkeypatch.setenv("STAN_ALLOWED_USERS", "bsphinney@ucdavis.edu")
    req = _Req({"x-ms-client-principal": "!!!not-base64!!!",
                "x-ms-client-principal-name": "bsphinney@ucdavis.edu"})
    assert is_privileged(req) is False


def test_group_membership_grants_access(monkeypatch):
    from stan.dashboard.auth import is_privileged
    monkeypatch.setenv("STAN_REQUIRED_GROUP", "abc-123-group")
    req = _Req({"x-ms-client-principal":
                _principal_header("someone@ucdavis.edu", groups=["abc-123-group"])})
    assert is_privileged(req) is True


def test_wrong_group_is_refused(monkeypatch):
    from stan.dashboard.auth import is_privileged
    monkeypatch.setenv("STAN_REQUIRED_GROUP", "abc-123-group")
    req = _Req({"x-ms-client-principal":
                _principal_header("someone@ucdavis.edu", groups=["other-group"])})
    assert is_privileged(req) is False


# ── the gate itself ────────────────────────────────────────────────

@pytest.fixture
def readonly_client(monkeypatch):
    monkeypatch.setenv("STAN_DASHBOARD_READONLY", "1")
    import importlib

    import stan.dashboard.readonly as ro
    importlib.reload(ro)
    from fastapi import FastAPI
    app = FastAPI()

    @app.post("/api/community/sync")
    async def _sync():
        return {"ok": True}

    @app.post("/api/fleet/command")
    async def _cmd():
        return {"ok": True}

    @app.post("/api/arcade/score")
    async def _score():
        return {"ok": True}

    assert ro.install_readonly_gate(app) is True
    return TestClient(app)


def test_gate_refuses_anonymous_sync(readonly_client):
    r = readonly_client.post("/api/community/sync", json={})
    assert r.status_code == 403
    assert "login_url" in r.json(), "a 403 should tell the caller how to sign in"


def test_gate_allows_authorized_sync(readonly_client, monkeypatch):
    monkeypatch.setenv("STAN_ALLOWED_USERS", "bsphinney@ucdavis.edu")
    r = readonly_client.post(
        "/api/community/sync", json={},
        headers={"x-ms-client-principal": _principal_header("bsphinney@ucdavis.edu"),
                 "x-ms-client-principal-name": "bsphinney@ucdavis.edu"},
    )
    assert r.status_code == 200


def test_fleet_command_stays_refused_even_when_authorized(readonly_client, monkeypatch):
    """Signing in must not unlock remote code execution on instrument PCs.

    /api/fleet/command enqueues update_stan / restart_watcher against
    instrument hosts. It is deliberately NOT in _PRIVILEGED_PATHS.
    """
    monkeypatch.setenv("STAN_ALLOWED_USERS", "bsphinney@ucdavis.edu")
    r = readonly_client.post(
        "/api/fleet/command", json={},
        headers={"x-ms-client-principal": _principal_header("bsphinney@ucdavis.edu"),
                 "x-ms-client-principal-name": "bsphinney@ucdavis.edu"},
    )
    assert r.status_code == 403


def test_arcade_score_is_accepted_without_login(readonly_client):
    """A shared leaderboard needs public writes, or nobody can post to it."""
    r = readonly_client.post("/api/arcade/score", json={})
    assert r.status_code != 403, "the arcade score post must not need a login"


def test_public_write_list_stays_narrow():
    """Guard against widening the public hole beyond the arcade score.

    Anything touching QC data, instruments or config must stay behind the
    gate no matter how convenient it would be to open.
    """
    import importlib

    import stan.dashboard.readonly as ro
    importlib.reload(ro)
    assert ro._PUBLIC_WRITE_PATHS == {"/api/arcade/score"}
    assert "/api/fleet/command" not in ro._PUBLIC_WRITE_PATHS
    assert "/api/community/sync" not in ro._PUBLIC_WRITE_PATHS
