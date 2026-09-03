"""Threaded incidents and acknowledgement-by-reaction.

Two properties carry most of the weight here, and both are about degradation
rather than the happy path:

  * a lab with only a webhook must keep working exactly as it did — threading
    is an upgrade, never a dependency; and
  * an acknowledgement means "seen", not "fixed", so it may silence repetition
    and must never silence an escalation.

No test opens a socket: `stan.slack_api` is stubbed throughout.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from stan import notify, slack_api
from stan.notify import (
    ACK_EMOJI,
    Alert,
    AlertStore,
    close_finished_threads,
    poll_acks,
    should_send,
)

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=timezone.utc)
HOOK = "https://hooks.slack.com/services/T000/B000/xxxxxxxxxxxxxxxxxxxxxxxx"
TOKEN = "xoxb-fake-token-for-tests-not-a-real-credential"
CHAN = "C0123456789"


@pytest.fixture
def store(tmp_path):
    return AlertStore(use_pg=False, path=tmp_path / "state.json")


@pytest.fixture
def slack(monkeypatch):
    """A fake Slack with a bot token, recording posts and serving reactions."""
    posted: list[dict] = []
    reactions: dict[str, list[dict]] = {}
    counter = {"n": 0}

    def _post(text, blocks=None, thread_ts=None):
        counter["n"] += 1
        ts = f"17{counter['n']:08d}.000100"
        posted.append({"text": text, "blocks": blocks,
                       "thread_ts": thread_ts, "ts": ts})
        return {"channel": CHAN, "ts": ts}

    monkeypatch.setattr(slack_api, "threading_available", lambda: True)
    monkeypatch.setattr(slack_api, "post_message", _post)
    monkeypatch.setattr(slack_api, "get_reactions",
                        lambda ch, ts: reactions.get(ts))
    return {"posted": posted, "reactions": reactions}


@pytest.fixture
def webhook_only(monkeypatch):
    """A lab with no bot token — the permanent state for other STAN installs."""
    sent: list[dict] = []
    monkeypatch.setattr(slack_api, "threading_available", lambda: False)
    monkeypatch.setattr(notify, "post_slack",
                        lambda payload, webhook=None: sent.append(payload) or True)
    monkeypatch.setattr(notify, "slack_configured", lambda: True)
    return sent


def _clog(sig="critical:30", key="clog:tims:100spd"):
    return Alert(key=key, kind="clog", instrument="Evosep One",
                 headline="column clog — 432 bar", signature=sig,
                 cool_off_hours=12.0, thread_key="clog:tims:100spd")


def _overpressure(bucket="20260903T0800"):
    return Alert(key=f"overpressure:tims:{bucket}", kind="overpressure",
                 instrument="Evosep One", headline="over-pressure, 520 bar",
                 severity="critical", thread_key="clog:tims:100spd")


# ── fallback: the webhook path must be untouched ─────────────────


def test_no_bot_token_degrades_to_one_webhook_message(webhook_only, store):
    r = notify.notify([_clog(), _overpressure()], store=store, now=NOW)
    assert r["sent"] is True
    assert r["threaded"] is False
    assert len(webhook_only) == 1, "webhook path still batches into one message"


def test_webhook_path_records_state_and_dedups(webhook_only, store):
    notify.notify([_clog()], store=store, now=NOW)
    again = notify.notify([_clog()], store=store, now=NOW + timedelta(minutes=30))
    assert again["sent"] is False and again["n_suppressed"] == 1
    assert len(webhook_only) == 1


# ── threading ────────────────────────────────────────────────────


def test_the_first_alert_opens_a_thread_and_is_remembered(slack, store):
    notify.notify([_clog()], store=store, now=NOW)
    assert slack["posted"][0]["thread_ts"] is None, "parent is not a reply"
    th = (store.last("clog:tims:100spd") or {}).get("thread")
    assert th["ts"] == slack["posted"][0]["ts"] and th["channel"] == CHAN


def test_a_later_development_replies_into_the_thread(slack, store):
    notify.notify([_clog()], store=store, now=NOW)
    parent = slack["posted"][0]["ts"]
    # Worse: a signature change escalates and must speak again.
    notify.notify([_clog(sig="critical:40")], store=store,
                  now=NOW + timedelta(minutes=30))
    assert len(slack["posted"]) == 2
    assert slack["posted"][1]["thread_ts"] == parent


def test_one_episode_is_one_thread_even_across_conditions(slack, store):
    """A clog and its over-pressure are one incident, not two channel lines."""
    notify.notify([_clog(), _overpressure()], store=store, now=NOW)
    assert len(slack["posted"]) == 1, "same incident -> one message"
    later = _overpressure(bucket="20260903T0900")
    notify.notify([later], store=store, now=NOW + timedelta(hours=1))
    assert slack["posted"][1]["thread_ts"] == slack["posted"][0]["ts"]


def test_unthreaded_alerts_are_still_batched_together(slack, store):
    """A Compass software error belongs to no episode; those batch as before."""
    a = Alert(key="ms_error:tims:1", kind="ms_error", instrument="timsTOF",
              headline="software error")
    b = Alert(key="connection_lost:tims:1", kind="connection_lost",
              instrument="timsTOF", headline="connection lost")
    notify.notify([a, b], store=store, now=NOW)
    assert len(slack["posted"]) == 1


def test_a_failed_post_is_not_recorded_and_retries(slack, store, monkeypatch):
    monkeypatch.setattr(slack_api, "post_message",
                        lambda text, blocks=None, thread_ts=None: None)
    r = notify.notify([_clog()], store=store, now=NOW)
    assert r["sent"] is False
    assert store.last("clog:tims:100spd") is None


# ── acknowledgement ──────────────────────────────────────────────


def _open_thread(store, slack):
    notify.notify([_clog()], store=store, now=NOW)
    return slack["posted"][0]["ts"]


def test_a_reaction_is_recorded_against_the_incident(slack, store):
    ts = _open_thread(store, slack)
    slack["reactions"][ts] = [{"name": "white_check_mark", "users": ["U123"]}]
    found = poll_acks(store, now=NOW + timedelta(minutes=5))
    assert found and found[0]["by"] == "U123"
    assert (store.last("clog:tims:100spd") or {})["ack"]["emoji"] == "white_check_mark"


def test_an_ack_suppresses_repetition(slack, store):
    ts = _open_thread(store, slack)
    slack["reactions"][ts] = [{"name": "eyes", "users": ["U9"]}]
    poll_acks(store, now=NOW + timedelta(minutes=5))
    # Same condition, cool-off long past: without the ack this would re-ping.
    later = notify.notify([_clog()], store=store, now=NOW + timedelta(hours=13))
    assert later["sent"] is False
    assert later["n_suppressed"] == 1
    assert len(slack["posted"]) == 1


def test_an_ack_does_NOT_suppress_an_escalation(slack, store):
    """Acknowledged means seen, not fixed. If it gets worse, it still speaks."""
    ts = _open_thread(store, slack)
    slack["reactions"][ts] = [{"name": "white_check_mark", "users": ["U9"]}]
    poll_acks(store, now=NOW + timedelta(minutes=5))
    worse = notify.notify([_clog(sig="critical:50")], store=store,
                          now=NOW + timedelta(minutes=10))
    assert worse["sent"] is True, "escalation must survive an acknowledgement"
    assert slack["posted"][1]["thread_ts"] == ts


def test_an_unrelated_reaction_is_not_an_ack(slack, store):
    ts = _open_thread(store, slack)
    slack["reactions"][ts] = [{"name": "tada", "users": ["U9"]}]
    assert poll_acks(store, now=NOW + timedelta(minutes=5)) == []


def test_a_failed_reactions_read_is_not_an_ack(slack, store, monkeypatch):
    """None means "could not tell". Reading it as an ack would silence a live
    alert on a network blip."""
    _open_thread(store, slack)
    monkeypatch.setattr(slack_api, "get_reactions", lambda ch, ts: None)
    assert poll_acks(store, now=NOW) == []
    assert (store.last("clog:tims:100spd") or {}).get("ack") is None


def test_every_ack_emoji_is_honoured(slack, store):
    for emoji in ACK_EMOJI:
        s = AlertStore(use_pg=False, path=store._path.with_name(f"{emoji}.json"))
        notify.notify([_clog()], store=s, now=NOW)
        slack["reactions"][slack["posted"][-1]["ts"]] = [
            {"name": emoji, "users": ["U1"]}]
        assert poll_acks(s, now=NOW), emoji


def test_acks_are_skipped_entirely_without_a_bot_token(webhook_only, store):
    notify.notify([_clog()], store=store, now=NOW)
    assert poll_acks(store, now=NOW) == []


# ── closing the thread ───────────────────────────────────────────


def test_an_episode_that_stops_gets_a_closing_reply(slack, store):
    ts = _open_thread(store, slack)
    closed = close_finished_threads(store, live_keys=set(), now=NOW + timedelta(hours=2))
    assert closed == ["clog:tims:100spd"]
    assert slack["posted"][-1]["thread_ts"] == ts
    assert "Resolved" in slack["posted"][-1]["text"]


def test_a_still_live_episode_is_not_closed(slack, store):
    _open_thread(store, slack)
    assert close_finished_threads(store, live_keys={"clog:tims:100spd"},
                                  now=NOW) == []
    assert len(slack["posted"]) == 1


def test_a_thread_is_only_closed_once(slack, store):
    _open_thread(store, slack)
    close_finished_threads(store, live_keys=set(), now=NOW)
    again = close_finished_threads(store, live_keys=set(), now=NOW + timedelta(hours=1))
    assert again == []


def test_a_point_event_thread_is_never_closed(slack, store):
    """A single aborted run was never an ongoing situation."""
    a = Alert(key="aborted:tims:20260903T0800", kind="aborted",
              instrument="Evosep One", headline="run aborted",
              thread_key="aborted:tims:20260903T0800")
    notify.notify([a], store=store, now=NOW)
    assert close_finished_threads(store, live_keys=set(), now=NOW) == []


# ── state plumbing ───────────────────────────────────────────────


def test_recording_an_alert_does_not_lose_the_thread(store):
    """`record` writes last_sent/signature; it must not clobber the thread id,
    or every reply would silently become a new channel message."""
    store.set_thread("k", CHAN, "1700.1", "clog", now=NOW)
    store.record(Alert(key="k", kind="clog", instrument="i", headline="h",
                       signature="s"), now=NOW)
    assert (store.last("k") or {})["thread"]["ts"] == "1700.1"


def test_an_ack_survives_a_later_record(store):
    store.set_thread("k", CHAN, "1700.1", "clog", now=NOW)
    store.set_ack("k", "U1", "eyes", now=NOW)
    store.record(Alert(key="k", kind="clog", instrument="i", headline="h"),
                 now=NOW)
    assert (store.last("k") or {})["ack"]["by"] == "U1"


def test_ack_only_blocks_the_cool_off_branch():
    prev = {"last_sent": (NOW - timedelta(hours=99)).isoformat(),
            "signature": "critical:30", "ack": {"at": NOW.isoformat()}}
    assert should_send(_clog(), prev, now=NOW) == (False, "acknowledged")
    assert should_send(_clog(sig="critical:40"), prev, now=NOW)[0] is True


# ── credentials never leak ───────────────────────────────────────


def test_a_bot_token_is_never_logged(monkeypatch, caplog):
    monkeypatch.setenv(slack_api.BOT_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(slack_api.CHANNEL_ENV, CHAN)

    def _boom(req, timeout=None):
        raise OSError(f"failed with Authorization: Bearer {TOKEN}")

    monkeypatch.setattr(slack_api.urllib.request, "urlopen", _boom)
    with caplog.at_level("DEBUG"):
        assert slack_api.api_call("chat.postMessage", {"channel": CHAN}) is None
    assert TOKEN not in caplog.text
    assert "<token>" in caplog.text


def test_a_webhook_url_pasted_as_a_bot_token_is_refused(monkeypatch):
    monkeypatch.setenv(slack_api.BOT_TOKEN_ENV, HOOK)
    assert slack_api.bot_token() is None
    assert slack_api.threading_available() is False


def test_threading_needs_both_token_and_channel(monkeypatch):
    monkeypatch.setenv(slack_api.BOT_TOKEN_ENV, TOKEN)
    monkeypatch.delenv(slack_api.CHANNEL_ENV, raising=False)
    monkeypatch.setattr("stan.config.load_community", lambda: {})
    monkeypatch.setattr(slack_api.Path, "home",
                        staticmethod(lambda: __import__("pathlib").Path("/nonexistent")))
    assert slack_api.bot_token() == TOKEN
    assert slack_api.threading_available() is False, "no channel -> no threading"


def test_api_failures_never_raise(monkeypatch):
    monkeypatch.setenv(slack_api.BOT_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(slack_api.CHANNEL_ENV, CHAN)
    for boom in (OSError("down"), ValueError("bad json")):
        monkeypatch.setattr(slack_api.urllib.request,
                            "urlopen",
                            lambda req, timeout=None, e=boom: (_ for _ in ()).throw(e))
        assert slack_api.post_message("x") is None
        assert slack_api.get_reactions(CHAN, "1.0") is None


def test_a_missing_scope_is_reported_not_swallowed(monkeypatch, caplog):
    monkeypatch.setenv(slack_api.BOT_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(slack_api.CHANNEL_ENV, CHAN)

    class _R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(
            {"ok": False, "error": "missing_scope",
             "needed": "reactions:read", "provided": "chat:write"}).encode()

    monkeypatch.setattr(slack_api.urllib.request,
                        "urlopen", lambda req, timeout=None: _R())
    with caplog.at_level("WARNING"):
        assert slack_api.get_reactions(CHAN, "1.0") is None
    assert "reactions:read" in caplog.text


# ── doctor's view ────────────────────────────────────────────────


def test_auth_check_reports_an_unconfigured_host(monkeypatch):
    monkeypatch.delenv(slack_api.BOT_TOKEN_ENV, raising=False)
    monkeypatch.setattr("stan.config.load_community", lambda: {})
    monkeypatch.setattr(slack_api, "_from_config",
                        lambda key, env, filename: None)
    a = slack_api.auth_check()
    assert a["configured"] is False and a["valid"] is False
    assert a["missing_scopes"] == []


def test_auth_check_names_the_scopes_a_webhook_only_token_lacks(monkeypatch):
    """The real 2026-09-03 case: a valid xoxb- token carrying only
    `incoming-webhook`, so neither threading nor acks can work."""
    monkeypatch.setenv(slack_api.BOT_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(slack_api.CHANNEL_ENV, CHAN)

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        headers = {"x-oauth-scopes": "incoming-webhook"}
        def read(self): return json.dumps(
            {"ok": True, "team": "ucdavis", "user": "stan"}).encode()

    monkeypatch.setattr(slack_api.urllib.request,
                        "urlopen", lambda req, timeout=None: _R())
    a = slack_api.auth_check()
    assert a["valid"] is True and a["team"] == "ucdavis"
    assert a["scopes"] == ["incoming-webhook"]
    assert a["missing_scopes"] == ["chat:write", "reactions:read"]


def test_auth_check_never_raises_on_a_dead_network(monkeypatch):
    monkeypatch.setenv(slack_api.BOT_TOKEN_ENV, TOKEN)
    monkeypatch.setattr(slack_api.urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(OSError("down")))
    a = slack_api.auth_check()
    assert a["valid"] is False and a["error"]
    assert TOKEN not in a["error"]
