"""Share links let a collaborator watch their own submission, and only theirs.

An external collaborator has no UC Davis account, so a login cannot serve
them — but HT data carries a customer's submission number and sample names,
so it cannot simply be public either.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stan.dashboard.ht_share import make_token, verify_token


def test_token_is_the_same_for_padded_and_bare_numbers():
    """Operators type 0793; filenames carry 793. One link must serve both."""
    assert make_token("0793") == make_token("793")
    assert verify_token("0793", make_token("793"))


def test_token_does_not_open_another_submission():
    """Editing the number in the URL is how secret links usually leak."""
    t = make_token("0793")
    assert verify_token("0793", t)
    assert not verify_token("0794", t)
    assert not verify_token("0079", t)


def test_garbage_and_empty_tokens_are_refused():
    assert not verify_token("0793", "deadbeef")
    assert not verify_token("0793", "")
    assert not verify_token("", make_token("0793") or "x")


def test_sharing_disabled_when_no_secret_available(monkeypatch):
    """No secret must mean no sharing, never a predictable token."""
    import stan.dashboard.ht_share as m
    monkeypatch.setattr(m, "_secret", lambda: None)
    assert m.make_token("0793") is None
    assert m.verify_token("0793", "anything") is False


# ── the gate ───────────────────────────────────────────────────────

@pytest.fixture
def hosted(monkeypatch):
    monkeypatch.setenv("STAN_DASHBOARD_READONLY", "1")
    monkeypatch.delenv("STAN_ALLOWED_USERS", raising=False)
    import importlib

    import stan.dashboard.readonly as ro
    importlib.reload(ro)
    app = FastAPI()

    @app.get("/api/ht/submission")
    async def _ht(q: str, token: str | None = None):
        # Mirrors the real endpoint's own check.
        if not (token and verify_token(q, token)):
            return {"blocked": True}
        return {"q": q, "shared_view": True}

    ro.install_readonly_gate(app)
    return TestClient(app)


def test_no_token_and_no_login_is_refused_by_the_gate(hosted):
    r = hosted.get("/api/ht/submission?q=0793")
    assert r.status_code == 403
    assert "login_url" in r.json()


def test_share_link_reaches_the_endpoint(hosted):
    """The gate cannot check the token itself — it sees the path, not whose
    plate is being asked for — so it must let a tokened request through to
    the endpoint that can."""
    t = make_token("0793")
    r = hosted.get(f"/api/ht/submission?q=0793&token={t}")
    assert r.status_code == 200
    assert r.json().get("shared_view") is True


def test_wrong_submission_token_is_refused_at_the_endpoint(hosted):
    """Passing the gate is not access: the endpoint still checks the pair."""
    t = make_token("0793")
    r = hosted.get(f"/api/ht/submission?q=0794&token={t}")
    assert r.status_code == 200
    assert r.json().get("blocked") is True, "token must not open another submission"
