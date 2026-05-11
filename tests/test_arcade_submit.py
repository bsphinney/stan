"""Tests for arcade score submission to the community leaderboard relay."""

from __future__ import annotations

import json
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

from stan.community.arcade_submit import (
    _instrument_family,
    _is_arcade_submit_enabled,
    fetch_leaderboard,
    submit_arcade_score,
)


# ---------------------------------------------------------------------------
# Unit tests: helpers
# ---------------------------------------------------------------------------


def test_instrument_family_timstof():
    assert _instrument_family("timsTOF HT") == "timsTOF"
    assert _instrument_family("timsTOF Pro 2") == "timsTOF"


def test_instrument_family_exploris():
    assert _instrument_family("Orbitrap Exploris 480") == "Exploris"


def test_instrument_family_lumos():
    assert _instrument_family("Orbitrap Fusion Lumos") == "Lumos"


def test_instrument_family_unknown():
    # Unknown model falls back to the original string
    assert _instrument_family("MysterioSpec 9000") == "MysterioSpec 9000"


def test_is_arcade_submit_enabled_both_true():
    assert _is_arcade_submit_enabled({"community_submit": True, "arcade_submit": True})


def test_is_arcade_submit_enabled_inherits_community():
    # arcade_submit not set → inherits community_submit
    assert _is_arcade_submit_enabled({"community_submit": True}) is True
    assert _is_arcade_submit_enabled({"community_submit": False}) is False


def test_is_arcade_submit_enabled_override_false():
    # community_submit=True but arcade_submit=False → disabled
    assert _is_arcade_submit_enabled({"community_submit": True, "arcade_submit": False}) is False


def test_is_arcade_submit_enabled_empty_config():
    assert _is_arcade_submit_enabled({}) is False


# ---------------------------------------------------------------------------
# Integration-style tests: submit_arcade_score (HTTP mocked)
# ---------------------------------------------------------------------------


def _make_response(payload: dict, status: int = 200) -> MagicMock:
    """Build a fake urllib response context manager."""
    body = json.dumps(payload).encode()
    resp = MagicMock()
    resp.read.return_value = body
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


@patch("stan.community.arcade_submit.load_community")
@patch("stan.community.arcade_submit.urllib.request.urlopen")
def test_submit_success(mock_urlopen, mock_load_community):
    """Happy path: relay returns accepted, function returns status ok."""
    mock_load_community.return_value = {
        "community_submit": True,
        "display_name": "UC Davis Core",
        "auth_token": "tok123",
    }
    mock_urlopen.return_value = _make_response({"status": "accepted", "rank": 3})

    result = submit_arcade_score("keratin_invaders", 4200, instrument="timsTOF HT")

    assert result["status"] == "ok"
    # Verify the outgoing payload shape
    call_args = mock_urlopen.call_args[0][0]  # urllib.request.Request object
    payload = json.loads(call_args.data)
    assert payload["game"] == "keratin_invaders"
    assert payload["score"] == 4200
    assert payload["display_name"] == "UC Davis Core"
    assert payload["instrument_family"] == "timsTOF"
    assert "stan_version" in payload
    assert "achieved_at" in payload
    # Privacy: no raw file content, no sample IDs
    assert "run_name" not in payload
    assert "instrument_model" not in payload


@patch("stan.community.arcade_submit.load_community")
def test_submit_skipped_when_disabled(mock_load_community):
    """Score not sent when arcade_submit is disabled."""
    mock_load_community.return_value = {"community_submit": False}

    result = submit_arcade_score("mzork", 100)

    assert result["status"] == "skipped"


@patch("stan.community.arcade_submit.load_community")
def test_submit_skipped_when_no_config(mock_load_community):
    """Missing community.yml → graceful skip, no exception."""
    mock_load_community.side_effect = FileNotFoundError("no community.yml")

    result = submit_arcade_score("angry_specs", 500)

    assert result["status"] == "skipped"


@patch("stan.community.arcade_submit.load_community")
@patch("stan.community.arcade_submit.urllib.request.urlopen")
def test_submit_retries_on_5xx(mock_urlopen, mock_load_community):
    """5xx response triggers one retry; second success → status ok."""
    mock_load_community.return_value = {"community_submit": True, "display_name": "Lab X"}

    http_err = urllib.error.HTTPError(
        url="http://x", code=503, msg="Service Unavailable",
        hdrs=None, fp=BytesIO(b'{"detail":"overload"}'),  # type: ignore[arg-type]
    )
    success_resp = _make_response({"status": "accepted"})

    mock_urlopen.side_effect = [http_err, success_resp]

    with patch("stan.community.arcade_submit.time.sleep"):
        result = submit_arcade_score("keratin_invaders", 1000)

    assert result["status"] == "ok"
    assert mock_urlopen.call_count == 2


@patch("stan.community.arcade_submit.load_community")
@patch("stan.community.arcade_submit.urllib.request.urlopen")
def test_submit_no_retry_on_4xx(mock_urlopen, mock_load_community):
    """4xx (client error) is not retried and returns failed status."""
    mock_load_community.return_value = {"community_submit": True, "display_name": "Lab Y"}

    http_err = urllib.error.HTTPError(
        url="http://x", code=400, msg="Bad Request",
        hdrs=None, fp=BytesIO(b'{"detail":"unknown game"}'),  # type: ignore[arg-type]
    )
    mock_urlopen.side_effect = http_err

    result = submit_arcade_score("invalid_game", 99)

    assert result["status"] == "failed"
    assert "400" in result["error"]
    assert mock_urlopen.call_count == 1


@patch("stan.community.arcade_submit.load_community")
@patch("stan.community.arcade_submit.urllib.request.urlopen")
def test_submit_network_error_returns_failed(mock_urlopen, mock_load_community):
    """Network failure after retries returns failed status, never raises."""
    mock_load_community.return_value = {"community_submit": True, "display_name": "Lab Z"}
    mock_urlopen.side_effect = urllib.error.URLError("Name or service not known")

    with patch("stan.community.arcade_submit.time.sleep"):
        result = submit_arcade_score("mzork", 42)

    assert result["status"] == "failed"
    assert "error" in result


# ---------------------------------------------------------------------------
# fetch_leaderboard
# ---------------------------------------------------------------------------


@patch("stan.community.arcade_submit.urllib.request.urlopen")
def test_fetch_leaderboard_success(mock_urlopen):
    rows = [
        {"display_name": "Lab A", "score": 9000, "won": False,
         "instrument_family": "timsTOF", "achieved_at": "2026-05-08T12:00:00Z",
         "stan_version": "0.2.353"},
    ]
    mock_urlopen.return_value = _make_response({"game": "keratin_invaders", "scores": rows})

    result = fetch_leaderboard("keratin_invaders", limit=5)

    assert isinstance(result, list)
    assert result[0]["display_name"] == "Lab A"
    assert result[0]["score"] == 9000


@patch("stan.community.arcade_submit.urllib.request.urlopen")
def test_fetch_leaderboard_offline(mock_urlopen):
    """Network error during leaderboard fetch returns empty list, never raises."""
    mock_urlopen.side_effect = urllib.error.URLError("offline")

    result = fetch_leaderboard("angry_specs")

    assert result == []
