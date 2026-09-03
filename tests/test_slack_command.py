"""`/stan` slash command — the signature check is the feature.

This route is public, unauthenticated in the platform sense, and mounted on a
production app. So most of what is below is about refusal: a tampered body, a
stale timestamp, another workspace, an unconfigured install.

The construction itself is pinned against **Slack's own published example**
rather than against a round trip of this module, because a round trip only
proves the code agrees with itself — it would happily pass with the base
string in the wrong order.

Nothing here touches the network or PG; the document is injected.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stan.dashboard import slack_command as sc

# ── Slack's documented example (api.slack.com, "Verifying requests") ──
DOC_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
DOC_TS = "1531420618"
DOC_BODY = (
    b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow"
    b"&channel_id=G8PSS9T3V&channel_name=foobar&user_id=U2CERLKJA"
    b"&user_name=roadrunner&command=%2Fwebhook-collect&text="
    b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F"
    b"397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
    b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
)
DOC_SIG = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"
DOC_TEAM = "T1DC2JH3J"

SECRET = "test-signing-secret"


def _sign(secret: str, ts: str, body: bytes) -> str:
    return "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body,
                            hashlib.sha256).hexdigest()


def _body(team: str = DOC_TEAM, command: str = "%2Fstan") -> bytes:
    return f"token=abc&team_id={team}&command={command}&text=&user_id=U1".encode()


@pytest.fixture
def client(monkeypatch):
    """The route mounted behind the real read-only gate, as in production."""
    monkeypatch.setenv("STAN_DASHBOARD_READONLY", "1")
    monkeypatch.setenv(sc.SIGNING_SECRET_ENV, SECRET)
    monkeypatch.setenv(sc.TEAM_ID_ENV, DOC_TEAM)
    monkeypatch.setattr(sc, "_load_document", lambda: _DOC)
    sc._rate.clear()

    from stan.dashboard.readonly import install_readonly_gate

    app = FastAPI()
    install_readonly_gate(app)
    app.include_router(sc.router)
    return TestClient(app)


def _post(client, body: bytes, ts: str = "1700000000", sig: str | None = None,
          secret: str = SECRET):
    import time as _t

    ts = str(int(_t.time())) if ts == "now" else ts
    return client.post(
        sc.ROUTE, content=body,
        headers={"X-Slack-Request-Timestamp": ts,
                 "X-Slack-Signature": sig or _sign(secret, ts, body),
                 "Content-Type": "application/x-www-form-urlencoded"})


_DOC = {
    "instrument_host": "TIMS-10878", "ceiling_bar": 520.0,
    "generated_at": "2026-09-03T18:39:04Z",
    "summary": {"n_runs": 27222},
    "column": {"known": True, "confidence": "logged", "days_since": 0.34,
               "injections_since": 14, "log_covers_install": True},
    "runs": [{"plateau_bar": 452.6, "expected_plateau_bar": 320.9,
              "pct_over_expected": 41.0}],
    "flags": [],
    # Present in the real document and deliberately never rendered.
    "sample_impact": {"flags": [
        {"at": "2026-09-03T10:00:00", "run_name": "20260828_100spd_COH-21_S5-E3_1_24214.d",
         "well": "S5-E3", "submission": "PROT_0793"}]},
}


# ── the construction, pinned against Slack ───────────────────────


def test_slacks_own_example_verifies():
    """Pins the base string `v0:{ts}:{body}` and the hex digest, so a wrong
    field order or separator fails here rather than in production."""
    assert sc.verify_signature(DOC_SECRET, DOC_TS, DOC_BODY, DOC_SIG,
                               now=int(DOC_TS)) is True


def test_slacks_example_fails_under_the_wrong_secret():
    assert sc.verify_signature("wrong", DOC_TS, DOC_BODY, DOC_SIG,
                               now=int(DOC_TS)) is False


def test_a_single_flipped_byte_fails():
    assert sc.verify_signature(DOC_SECRET, DOC_TS, DOC_BODY + b"x", DOC_SIG,
                               now=int(DOC_TS)) is False


def test_the_replay_window_is_enforced_both_ways():
    ok = int(DOC_TS)
    assert sc.verify_signature(DOC_SECRET, DOC_TS, DOC_BODY, DOC_SIG,
                               now=ok + sc.MAX_AGE_SECONDS - 1) is True
    assert sc.verify_signature(DOC_SECRET, DOC_TS, DOC_BODY, DOC_SIG,
                               now=ok + sc.MAX_AGE_SECONDS + 1) is False
    # A timestamp from the future is just as much a replay risk.
    assert sc.verify_signature(DOC_SECRET, DOC_TS, DOC_BODY, DOC_SIG,
                               now=ok - sc.MAX_AGE_SECONDS - 1) is False


def test_a_garbage_timestamp_is_refused_not_crashed():
    for ts in ("", "not-a-number", "1e9", None):
        assert sc.verify_signature(DOC_SECRET, ts, DOC_BODY, DOC_SIG,
                                   now=int(DOC_TS)) is False


def test_missing_pieces_are_refused():
    assert sc.verify_signature("", DOC_TS, DOC_BODY, DOC_SIG) is False
    assert sc.verify_signature(DOC_SECRET, DOC_TS, DOC_BODY, "") is False


# ── the route ────────────────────────────────────────────────────


def test_a_correctly_signed_request_gets_a_status(client):
    r = _post(client, _body(), ts="now")
    assert r.status_code == 200
    assert r.json()["response_type"] == "ephemeral"
    assert "TIMS-10878" in r.json()["text"]


def test_a_tampered_body_is_rejected(client):
    body = _body()
    sig = _sign(SECRET, "1700000000", body)
    r = _post(client, body + b"&injected=1", ts="1700000000", sig=sig)
    assert r.status_code == 401


def test_an_old_timestamp_is_rejected(client):
    r = _post(client, _body(), ts="1531420618")   # 2018
    assert r.status_code == 401


def test_a_wrong_signature_is_rejected(client):
    r = _post(client, _body(), ts="now", sig="v0=" + "0" * 64)
    assert r.status_code == 401


def test_a_signature_from_another_workspace_is_rejected(client):
    """A valid signature only proves possession of the secret. Pinning the
    team means a leaked secret installed elsewhere still reads nothing."""
    r = _post(client, _body(team="T_SOMEONE_ELSE"), ts="now")
    assert r.status_code == 403


def test_an_unconfigured_install_returns_404(monkeypatch):
    """Fail closed, and look like there is no such endpoint -- an error saying
    "signing secret not configured" tells a prober exactly what to hunt for."""
    monkeypatch.delenv(sc.SIGNING_SECRET_ENV, raising=False)
    monkeypatch.setattr(sc, "_from_config", lambda key, env: None)
    app = FastAPI()
    app.include_router(sc.router)
    r = TestClient(app).post(sc.ROUTE, content=b"x")
    assert r.status_code == 404
    assert "secret" not in r.text.lower()


def test_the_read_only_gate_does_not_block_the_route(client):
    """The gate refuses every unauthenticated POST. If the allow-list entry is
    ever dropped this returns 403 and the command silently stops working."""
    r = _post(client, _body(), ts="now")
    assert r.status_code != 403, "route must be exempt in _PUBLIC_WRITE_PATHS"


def test_the_gate_still_blocks_everything_else(client):
    """Proves the exemption is path-scoped, not a hole in the gate."""
    assert client.post("/api/fleet/command", json={"cmd": "x"}).status_code in (403, 404)


def test_rate_limited_after_the_cap(client):
    for _ in range(sc.RATE_LIMIT_PER_MINUTE):
        assert _post(client, _body(), ts="now").status_code == 200
    r = _post(client, _body(), ts="now")
    assert r.status_code == 200 and "Slow down" in r.json()["text"]


# ── what the reply may contain ───────────────────────────────────


def test_the_reply_never_names_a_sample_or_submission():
    """A Slack channel is searchable by the whole workspace, forever. The
    document carries sample_impact; the reply must not."""
    text = sc.build_status(_DOC)
    for secret_ish in ("COH-21", "PROT_0793", "S5-E3", ".d", "24214"):
        assert secret_ish not in text, f"leaked {secret_ish!r}"


def test_the_reply_answers_the_question_on_the_first_line():
    doc = dict(_DOC, flags=[
        {"start": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
         "severity": "critical", "kinds": ["ceiling"], "method": "100spd",
         "pct_over_expected": 61.9}])
    text = sc.build_status(doc)
    assert text.splitlines()[0].startswith(":red_circle:")
    assert "critical" in text.splitlines()[0]


def test_a_quiet_instrument_says_so():
    text = sc.build_status(dict(_DOC, flags=[]))
    assert text.splitlines()[0].startswith(":white_check_mark:")
    assert "Open episode:* none" in text


def test_an_unverifiable_column_age_is_not_asserted():
    """Same gate as the wear alert: a confidently wrong age is worse than
    none, and the /stan reply is where someone would act on it."""
    doc = dict(_DOC, column={"known": True, "confidence": "unverifiable",
                             "days_since": 32.99})
    column_line = next(ln for ln in sc.build_status(doc).splitlines()
                       if ln.startswith("*Column:*"))
    assert "not verifiable" in column_line
    assert "32" not in column_line, "must not state an age it cannot stand behind"


def test_a_lower_bound_count_is_labelled_as_one():
    doc = dict(_DOC, column={"known": True, "confidence": "logged",
                             "days_since": 33.0, "injections_since": 900,
                             "log_covers_install": False,
                             "counts_are_lower_bounds": True})
    assert "at least" in sc.build_status(doc)


def test_a_missing_document_does_not_crash():
    assert "No column-health document" in sc.build_status(None)


def test_a_malformed_document_does_not_crash():
    for doc in ({}, {"flags": [{"start": "nonsense"}]}, {"runs": [{}]},
                {"column": None, "flags": None}):
        assert isinstance(sc.build_status(doc), str)


# ── nothing sensitive reaches a log ──────────────────────────────


def test_neither_body_nor_signature_is_logged(client, caplog):
    body = _body() + b"&text=something-private"
    with caplog.at_level("DEBUG"):
        _post(client, body, ts="now", sig="v0=" + "0" * 64)
    assert "something-private" not in caplog.text
    assert "0" * 64 not in caplog.text
    assert SECRET not in caplog.text
