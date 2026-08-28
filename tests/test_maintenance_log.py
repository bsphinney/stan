"""Maintenance log: attribution, downtime spans, and the hosted-write gate.

These entries drive LC-column age and the planned community reliability
leaderboard (README: MTBF / availability / recovery-time per instrument
model), so an entry nobody can trace is worse than no entry. On the hosted
dashboard the write is reachable only by a signed-in, allow-listed operator,
and the authenticated identity is recorded on the row.
"""

from __future__ import annotations

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _principal(upn: str) -> str:
    blob = {"auth_typ": "aad",
            "claims": [{"typ": "preferred_username", "val": upn}],
            "userDetails": upn}
    return base64.b64encode(json.dumps(blob).encode()).decode()


def test_downtime_is_a_first_class_event_type():
    from stan.db import DOWNTIME_EVENT_TYPES, EVENT_TYPES
    assert "downtime" in EVENT_TYPES
    assert DOWNTIME_EVENT_TYPES == {"downtime"}, (
        "the reliability maths needs one definition of what counts as down")


def test_log_event_accepts_span_and_attribution(tmp_path, monkeypatch):
    """created_by/end_date must reach the row when the columns exist."""
    captured = {}

    def _fake_insert(row):
        captured.update(row)

    monkeypatch.setattr("stan.db_pg.use_pg", lambda: True)
    monkeypatch.setattr("stan.db_pg.insert_event_pg", _fake_insert)
    monkeypatch.setattr("stan.db._event_column_exists", lambda n, p=None: True)

    from stan.db import log_event
    log_event(instrument="timsTOF HT", event_type="downtime",
              event_date="2026-08-20", end_date="2026-08-22",
              notes="turbo pump", created_by="bsphinney@ucdavis.edu",
              share_community=True)

    assert captured["event_type"] == "downtime"
    assert captured["end_date"] == "2026-08-22"
    assert captured["created_by"] == "bsphinney@ucdavis.edu"
    assert captured["share_community"] is True
    assert captured["created_at"], "a log entry must record when it was made"


def test_log_event_works_before_the_migration(monkeypatch):
    """The new columns need a migration run by the table owner.

    Until that happens the dashboard must keep logging events rather than
    failing every save with UndefinedColumn.
    """
    captured = {}
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: True)
    monkeypatch.setattr("stan.db_pg.insert_event_pg", lambda r: captured.update(r))
    monkeypatch.setattr("stan.db._event_column_exists", lambda n, p=None: False)

    from stan.db import log_event
    log_event(instrument="timsTOF HT", event_type="source_clean",
              event_date="2026-08-20", created_by="bsphinney@ucdavis.edu")

    assert "created_by" not in captured, "must not send a column that does not exist"
    assert captured["event_type"] == "source_clean"


def test_share_community_defaults_off(monkeypatch):
    """Maintenance notes can name people and customers: opt-in only."""
    captured = {}
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: True)
    monkeypatch.setattr("stan.db_pg.insert_event_pg", lambda r: captured.update(r))
    monkeypatch.setattr("stan.db._event_column_exists", lambda n, p=None: True)

    from stan.db import log_event
    log_event(instrument="timsTOF HT", event_type="pm", event_date="2026-08-20")
    assert captured["share_community"] is False


# ── the hosted write gate ──────────────────────────────────────────

@pytest.fixture
def gated_app(monkeypatch):
    monkeypatch.setenv("STAN_DASHBOARD_READONLY", "1")
    import importlib

    import stan.dashboard.readonly as ro
    importlib.reload(ro)
    app = FastAPI()

    @app.post("/api/instruments/{instrument}/events")
    async def _log(instrument: str):
        return {"ok": True}

    assert ro.install_readonly_gate(app) is True
    return TestClient(app)


def test_anonymous_cannot_log_an_event(gated_app):
    r = gated_app.post("/api/instruments/timsTOF%20HT/events", json={})
    assert r.status_code == 403
    assert "login_url" in r.json(), "tell the caller how to sign in"


def test_signed_in_operator_can_log_an_event(gated_app, monkeypatch):
    monkeypatch.setenv("STAN_ALLOWED_USERS", "bsphinney@ucdavis.edu")
    r = gated_app.post(
        "/api/instruments/timsTOF%20HT/events", json={},
        headers={"x-ms-client-principal": _principal("bsphinney@ucdavis.edu"),
                 "x-ms-client-principal-name": "bsphinney@ucdavis.edu"})
    assert r.status_code == 200


def test_pattern_does_not_open_neighbouring_routes(gated_app, monkeypatch):
    """The privileged pattern is anchored to exactly the events route."""
    monkeypatch.setenv("STAN_ALLOWED_USERS", "bsphinney@ucdavis.edu")
    import stan.dashboard.readonly as ro
    for path in ("/api/instruments/x/events/extra",
                 "/api/instruments/x/config",
                 "/api/fleet/command"):
        assert not ro._is_privileged_path(path), path
    assert ro._is_privileged_path("/api/instruments/timsTOF HT/events")


def test_same_day_span_is_not_rejected(monkeypatch):
    """A one-day outage must be accepted however the dates are shaped.

    event_date is TEXT with mixed shapes in the wild: the older Trends form
    writes "2026-08-10T12:00:00Z" while a date input yields a bare
    "2026-08-10". A raw string compare calls that pair end-before-start,
    because "2026-08-10" < "2026-08-10T12:00:00Z".
    """
    captured = {}
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: True)
    monkeypatch.setattr("stan.db_pg.insert_event_pg", lambda r: captured.update(r))
    monkeypatch.setattr("stan.db._event_column_exists", lambda n, p=None: True)

    from stan.db import log_event
    log_event(instrument="timsTOF HT", event_type="downtime",
              event_date="2026-08-10T12:00:00Z", end_date="2026-08-10")
    assert captured["end_date"] == "2026-08-10T12:00:00Z", (
        "a bare end date must be normalised to the start's shape")


def test_span_end_left_alone_when_shapes_already_match(monkeypatch):
    captured = {}
    monkeypatch.setattr("stan.db_pg.use_pg", lambda: True)
    monkeypatch.setattr("stan.db_pg.insert_event_pg", lambda r: captured.update(r))
    monkeypatch.setattr("stan.db._event_column_exists", lambda n, p=None: True)

    from stan.db import log_event
    log_event(instrument="timsTOF HT", event_type="downtime",
              event_date="2026-08-10", end_date="2026-08-12")
    assert captured["end_date"] == "2026-08-12"
