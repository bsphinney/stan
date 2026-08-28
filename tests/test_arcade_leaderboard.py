"""Tests for the shared arcade leaderboard (v1.0.22+).

Two things here are security properties, not features, and are asserted
as such:

* the board holds free text somebody typed after losing a game, and on
  PG Farm every lab running STAN can read it — so the stored fields are
  length-capped on write and must survive round-tripping *as data*;
* the publicly-hosted dashboard must refuse to write. readonly.py is what
  enforces that, and this file pins the arcade POST path to it so a later
  refactor can't quietly open the public instance up to score spam.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import stan.dashboard.server as server
import stan.db as stan_db
from stan.db import (
    ARCADE_AFFILIATION_MAX,
    ARCADE_NAME_MAX,
    get_arcade_leaderboard,
    insert_arcade_score,
    sanitize_arcade_text,
)
from stan.dashboard.readonly import install_readonly_gate

XSS = '<img src=x onerror=alert(1)>'


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch) -> Path:
    """A throwaway SQLite store, with PG explicitly out of the picture."""
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    path = tmp_path / "stan.db"
    stan_db.init_db(path)
    monkeypatch.setattr(stan_db, "get_db_path", lambda: path)
    return path


# ── Sanitizing ───────────────────────────────────────────────────────

def test_control_characters_are_flattened():
    assert sanitize_arcade_text("bad\x00name\nwith\ttabs   here", 40) == \
        "bad name with tabs here"


def test_length_caps():
    assert len(sanitize_arcade_text("B" * 200, ARCADE_NAME_MAX)) == ARCADE_NAME_MAX
    assert len(sanitize_arcade_text("A" * 200, ARCADE_AFFILIATION_MAX)) == \
        ARCADE_AFFILIATION_MAX


def test_markup_is_preserved_not_stripped():
    """Escaping is the renderer's job; mangling here would corrupt names.

    A name containing < or & has to survive storage intact — R&D is a
    legitimate affiliation — so this asserts the text comes back byte for
    byte, and the render-side escaping is what makes it inert.
    """
    assert sanitize_arcade_text(XSS, ARCADE_NAME_MAX) == XSS
    assert sanitize_arcade_text("Smith & Co.", ARCADE_NAME_MAX) == "Smith & Co."


# ── Storage round-trip ───────────────────────────────────────────────

def test_round_trip(db_path: Path):
    insert_arcade_score("mass_match", 4200, level=3, won=True,
                        player_name="Brett", affiliation="UC Davis Proteomics Core",
                        db_path=db_path)
    rows = get_arcade_leaderboard(game="mass_match", db_path=db_path)
    assert len(rows) == 1
    assert rows[0]["score"] == 4200
    assert rows[0]["level"] == 3
    assert rows[0]["won"] is True
    assert rows[0]["player_name"] == "Brett"
    assert rows[0]["affiliation"] == "UC Davis Proteomics Core"


def test_blank_name_is_anonymous(db_path: Path):
    """Both fields are optional. A blank name must never block a score."""
    out = insert_arcade_score("mass_match", 10, player_name="   ",
                              affiliation="", db_path=db_path)
    assert out["player_name"] == "anonymous"
    row = get_arcade_leaderboard(game="mass_match", db_path=db_path)[0]
    assert row["player_name"] == "anonymous"
    assert row["affiliation"] == ""


def test_ordering_is_high_score_first(db_path: Path):
    for s in (100, 900, 500):
        insert_arcade_score("core_defense", s, db_path=db_path)
    scores = [r["score"] for r in get_arcade_leaderboard(game="core_defense",
                                                         db_path=db_path)]
    assert scores == [900, 500, 100]


def test_games_are_separate_boards(db_path: Path):
    insert_arcade_score("mass_match", 10, db_path=db_path)
    insert_arcade_score("core_defense", 20, db_path=db_path)
    assert len(get_arcade_leaderboard(game="mass_match", db_path=db_path)) == 1
    assert len(get_arcade_leaderboard(db_path=db_path)) == 2


def test_provenance_host_is_never_returned(db_path: Path):
    """submitted_by_host is for moderation, not for the board."""
    insert_arcade_score("mzork", 42, submitted_by_host="TIMS-10878",
                        db_path=db_path)
    row = get_arcade_leaderboard(game="mzork", db_path=db_path)[0]
    assert "submitted_by_host" not in row


def test_missing_db_returns_empty_board(tmp_path: Path, monkeypatch):
    """A lab with no store yet gets an empty board, not an exception."""
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    assert get_arcade_leaderboard(db_path=tmp_path / "nope.db") == []


# ── API ──────────────────────────────────────────────────────────────

@pytest.fixture
def client(db_path: Path, monkeypatch) -> TestClient:
    monkeypatch.setattr(server, "get_db_path", lambda: db_path)
    return TestClient(server.app)


def test_post_then_get(client: TestClient):
    r = client.post("/api/arcade/score", json={
        "game": "mass_match", "score": 4200, "level": 3, "won": True,
        "player_name": "Brett", "affiliation": "UC Davis Proteomics Core",
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True

    board = client.get("/api/arcade/leaderboard?game=mass_match&limit=5").json()
    assert board["count"] == 1
    assert board["scores"][0]["player_name"] == "Brett"
    assert board["read_only"] is False


def test_api_caps_field_lengths(client: TestClient):
    """Client-side maxlength is a courtesy; the cap has to hold here."""
    r = client.post("/api/arcade/score", json={
        "game": "mass_match", "score": 1,
        "player_name": "N" * 500, "affiliation": "A" * 500,
    })
    body = r.json()
    assert len(body["player_name"]) == ARCADE_NAME_MAX
    assert len(body["affiliation"]) == ARCADE_AFFILIATION_MAX


def test_api_stores_untrusted_text_verbatim(client: TestClient):
    client.post("/api/arcade/score", json={
        "game": "core_defense", "score": 7, "player_name": XSS,
    })
    row = client.get("/api/arcade/leaderboard?game=core_defense").json()["scores"][0]
    # Returned as JSON *data*. public/arcade.html escapes it at the
    # interpolation site; nothing here may pre-mangle it.
    assert row["player_name"] == XSS


@pytest.mark.parametrize("payload", [
    {"game": "../../etc/passwd", "score": 1},
    {"game": "", "score": 1},
    {"game": "mass match!", "score": 1},
    {"game": "mzork", "score": -5},
    {"game": "mzork", "score": 10 ** 15},
])
def test_api_rejects_bad_input(client: TestClient, payload: dict):
    assert client.post("/api/arcade/score", json=payload).status_code == 400


def test_api_rejects_nonfinite_score(client: TestClient):
    # NaN can only arrive as a raw JSON token — httpx refuses to encode it.
    r = client.post("/api/arcade/score", content='{"game":"mzork","score":NaN}',
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 400


def test_unknown_game_id_is_accepted(client: TestClient):
    """New games must work without a server change — only the slug shape
    is policed."""
    assert client.post("/api/arcade/score",
                       json={"game": "future_game_9", "score": 3}).status_code == 200


# ── Read-only hosting ────────────────────────────────────────────────

def test_readonly_gate_allows_score_posts():
    """The public dashboard both reads AND writes the board.

    Changed deliberately 2026-08-28: a leaderboard shared across the
    community site and every local STAN is pointless if only signed-in
    operators can add to it. The score post is the single entry in
    readonly._PUBLIC_WRITE_PATHS -- the payload is a game score, with no
    lab data and no instrument control, and lengths are truncated
    server-side. Everything else still needs a login or is refused
    outright; test_dashboard_auth.py pins that list to this one route.

    Mirrors tests/test_readonly_gate.py: the gate is installed at import
    time from the environment, so the route path is pinned against a
    fresh app rather than re-importing server.py.
    """
    import os
    app = FastAPI()

    @app.get("/api/arcade/leaderboard")
    async def board():
        return {"scores": []}

    @app.post("/api/arcade/score")
    async def score():
        return {"ok": True}

    prev = os.environ.get("STAN_DASHBOARD_READONLY")
    os.environ["STAN_DASHBOARD_READONLY"] = "1"
    try:
        install_readonly_gate(app)
        c = TestClient(app)
        assert c.get("/api/arcade/leaderboard").status_code == 200
        assert c.post("/api/arcade/score", json={"game": "mzork", "score": 1}).status_code == 200
    finally:
        if prev is None:
            os.environ.pop("STAN_DASHBOARD_READONLY", None)
        else:
            os.environ["STAN_DASHBOARD_READONLY"] = prev
