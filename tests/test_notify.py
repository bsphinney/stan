"""Slack transport: what it sends, what it refuses, and what it survives.

Two properties matter more than the feature itself, and both are here:

  * a notifier must never take down its caller — these run from cron jobs
    whose real work already succeeded, so a dead webhook costs a log line;
  * a webhook URL is a bearer credential and must never reach a log.

Nothing here touches the network: `urlopen` is replaced in every test that
posts.
"""

from __future__ import annotations

import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from stan import notify
from stan.notify import (
    Alert,
    AlertStore,
    collapse,
    render_alerts,
    should_send,
)

#: The same URL the shared `posted` fixture (tests/conftest.py) installs.
HOOK = "https://hooks.slack.com/services/T000/B000/xxxxxxxxxxxxxxxxxxxxxxxx"
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


# ── fakes ────────────────────────────────────────────────────────


def _raising(*a, **k):
    raise RuntimeError("SSL SYSCALL error: EOF detected")


@pytest.fixture(autouse=True)
def _no_real_pg(monkeypatch):
    """No test here may reach PG Farm: its alert_state is the live one."""
    import stan.db_pg

    monkeypatch.setattr(stan.db_pg, "_connect", _raising)


# `posted` (capture what would have gone to Slack) lives in tests/conftest.py,
# shared with test_hystar_events.py and test_qc_summary.py.


# ── webhook resolution ───────────────────────────────────────────


def test_env_var_wins(monkeypatch):
    monkeypatch.setenv(notify.WEBHOOK_ENV, HOOK)
    assert notify.slack_webhook() == HOOK
    assert notify.slack_configured()


@pytest.fixture
def unconfigured(monkeypatch, tmp_path):
    """A host with no webhook anywhere: no env, no community.yml, no file."""
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    monkeypatch.setattr("stan.config.load_community", lambda: {})
    monkeypatch.setattr(notify, "_webhook_file", lambda: tmp_path / "slack_webhook")
    return tmp_path / "slack_webhook"


def test_no_config_means_alerts_are_off_not_broken(unconfigured):
    assert notify.slack_webhook() is None
    assert not notify.slack_configured()
    # And a send on such a host is a no-op, not an exception.
    assert notify.send_message("x") is False


def test_a_non_slack_url_is_refused(monkeypatch, unconfigured):
    """A typo'd or attacker-supplied 'webhook' would exfiltrate the alert body."""
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://evil.example.com/collect")
    assert notify.slack_webhook() is None
    assert not notify.slack_configured()


def test_community_yml_is_read_when_the_env_is_unset(monkeypatch, unconfigured):
    monkeypatch.setattr("stan.config.load_community", lambda: {"slack_webhook_url": HOOK})
    assert notify.slack_webhook() == HOOK


def test_file_fallback_is_read(unconfigured):
    unconfigured.write_text(HOOK + "\n")
    assert notify.slack_webhook() == HOOK


# ── payload shape ────────────────────────────────────────────────


def test_payload_shape(posted):
    assert notify.send_message("headline", [{"type": "section",
                                             "text": {"type": "mrkdwn", "text": "x"}}])
    assert len(posted) == 1
    call = posted[0]
    assert call["url"] == HOOK
    assert call["headers"]["Content-type"] == "application/json"
    assert call["body"]["text"] == "headline"
    assert call["body"]["blocks"][0]["type"] == "section"


def test_notification_text_names_instrument_fault_and_number():
    """The lock-screen line has to answer 'do I get up' without expanding."""
    text, blocks = render_alerts([
        Alert(key="clog:x", kind="clog", instrument="Evosep One (TIMS-10878)",
              severity="critical",
              headline="column clog — 432 bar, 34% over baseline"),
    ])
    assert "Evosep One (TIMS-10878)" in text
    assert "clog" in text
    assert "432 bar" in text
    assert blocks[-1]["type"] == "context"
    assert notify.DASHBOARD_URL in json.dumps(blocks)


def test_worst_alert_headlines_a_batch():
    text, _ = render_alerts([
        Alert(key="a", kind="evotip", instrument="i", severity="warning",
              headline="Evotip not seated"),
        Alert(key="b", kind="clog", instrument="i", severity="critical",
              headline="column clog"),
    ])
    assert text.startswith(notify._SEVERITY_ICON["critical"])
    assert "column clog" in text
    assert "+1 more" in text


# ── failure never propagates ─────────────────────────────────────


def test_http_error_does_not_raise(monkeypatch):
    def _boom(req, timeout=None):
        raise urllib.error.HTTPError(HOOK, 403, "Forbidden", {}, None)

    monkeypatch.setattr(notify.urllib.request, "urlopen", _boom)
    monkeypatch.setenv(notify.WEBHOOK_ENV, HOOK)
    assert notify.post_slack({"text": "x"}) is False


def test_network_error_does_not_raise(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(notify.urllib.request, "urlopen", _boom)
    monkeypatch.setenv(notify.WEBHOOK_ENV, HOOK)
    assert notify.post_slack({"text": "x"}) is False


def test_a_dead_webhook_does_not_break_the_watcher(monkeypatch, tmp_path):
    """The whole point: a failed notification must not fail the cron job."""
    monkeypatch.setattr(notify.urllib.request, "urlopen",
                        lambda req, timeout=None: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setenv(notify.WEBHOOK_ENV, HOOK)
    store = AlertStore(use_pg=False, path=tmp_path / "s.json")
    result = notify.notify([Alert(key="k", kind="clog", instrument="i",
                                  headline="h")], store=store, now=NOW)
    assert result["sent"] is False
    # And it must NOT be recorded, so the next tick retries rather than
    # silently swallowing the alert. Same rule as the HT email watcher.
    assert store.last("k") is None


def test_the_webhook_never_reaches_a_log(monkeypatch, caplog):
    """urllib will happily put the URL it was given into an error string."""
    secret_path = "T0SECRET/B0SECRET/zzzzzzzzzzzzzzzz"
    hook = f"https://hooks.slack.com/services/{secret_path}"

    def _boom(req, timeout=None):
        raise OSError(f"failed to reach {hook}")

    monkeypatch.setattr(notify.urllib.request, "urlopen", _boom)
    monkeypatch.setenv(notify.WEBHOOK_ENV, hook)
    with caplog.at_level("DEBUG"):
        assert notify.post_slack({"text": "x"}) is False
    assert secret_path not in caplog.text
    assert "<webhook>" in caplog.text


def test_scrub_catches_a_url_we_did_not_send():
    other = "https://hooks.slack.com/services/T9/B9/leaked"
    assert "leaked" not in notify._scrub(f"boom {other}", None)


# ── dedup policy ─────────────────────────────────────────────────


def _standing(sig="critical:30"):
    return Alert(key="clog:tims:100spd", kind="clog", instrument="i",
                 headline="column clog", signature=sig, cool_off_hours=12.0)


def _point():
    return Alert(key="evotip:tims:20260831T1430", kind="evotip",
                 instrument="i", headline="Evotip not seated")


def test_never_sent_is_news():
    assert should_send(_standing(), None, now=NOW) == (True, "new")


def test_same_condition_inside_the_cool_off_is_silent():
    prev = {"last_sent": (NOW - timedelta(hours=2)).isoformat(),
            "signature": "critical:30"}
    send, why = should_send(_standing(), prev, now=NOW)
    assert send is False and why == "suppressed"


def test_a_worsening_clog_re_alerts_immediately():
    """20% over baseline and 35% over are different messages, cool-off or not."""
    prev = {"last_sent": (NOW - timedelta(minutes=5)).isoformat(),
            "signature": "critical:20"}
    send, why = should_send(_standing("critical:30"), prev, now=NOW)
    assert send is True and why == "escalated"


def test_a_standing_condition_repeats_after_the_cool_off():
    prev = {"last_sent": (NOW - timedelta(hours=13)).isoformat(),
            "signature": "critical:30"}
    send, why = should_send(_standing(), prev, now=NOW)
    assert send is True and why == "cooled_off"


def test_a_point_event_is_said_once_ever():
    """One aborted injection is not news again tomorrow."""
    prev = {"last_sent": (NOW - timedelta(days=30)).isoformat(), "signature": ""}
    assert should_send(_point(), prev, now=NOW) == (False, "suppressed")


def test_repeat_tick_sends_nothing(posted, tmp_path):
    store = AlertStore(use_pg=False, path=tmp_path / "s.json")
    a = _standing()
    assert notify.notify([a], store=store, now=NOW)["sent"] is True
    again = notify.notify([_standing()], store=store,
                          now=NOW + timedelta(minutes=30))
    assert again["sent"] is False
    assert again["n_suppressed"] == 1
    assert len(posted) == 1, "one condition, one message"


def test_dry_run_sends_nothing_and_remembers_nothing(posted, tmp_path):
    store = AlertStore(use_pg=False, path=tmp_path / "s.json")
    r = notify.notify([_standing()], store=store, dry_run=True, now=NOW)
    assert r["n_fresh"] == 1 and r["sent"] is False
    assert not posted
    assert store.last("clog:tims:100spd") is None


def test_one_batch_is_one_message(posted, tmp_path):
    """Three faults at once are one ping, not three."""
    store = AlertStore(use_pg=False, path=tmp_path / "s.json")
    notify.notify([_standing(), _point(),
                   Alert(key="c", kind="aborted", instrument="i",
                         headline="run aborted")], store=store, now=NOW)
    assert len(posted) == 1


def test_duplicate_keys_collapse_within_a_batch():
    """The Evosep log and Compass see one aborted injection; say it once."""
    evosep = Alert(key="evotip:t:20260831T1430", kind="evotip",
                   instrument="Evosep One", headline="Evotip not seated",
                   severity="warning")
    compass = Alert(key="evotip:t:20260831T1430", kind="evotip",
                    instrument="timsTOF HPZ6", headline="Evotip missing",
                    severity="warning")
    out = collapse([evosep, compass])
    assert len(out) == 1
    assert out[0].instrument == "Evosep One", "keep the first, which has the numbers"


def test_collapse_keeps_the_more_severe_of_a_duplicate_pair():
    warn = Alert(key="k", kind="overpressure", instrument="a",
                 headline="w", severity="warning")
    crit = Alert(key="k", kind="overpressure", instrument="b",
                 headline="c", severity="critical")
    assert collapse([warn, crit])[0].severity == "critical"


# ── state store ──────────────────────────────────────────────────


def test_file_store_round_trips(tmp_path):
    path = tmp_path / "s.json"
    store = AlertStore(use_pg=False, path=path)
    assert store.last("k") is None
    store.record(_standing(), now=NOW)
    assert AlertStore(use_pg=False, path=path).last("clog:tims:100spd")["signature"] \
        == "critical:30"


def test_a_corrupt_state_file_does_not_stop_alerting(tmp_path, posted):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    store = AlertStore(use_pg=False, path=path)
    assert notify.notify([_standing()], store=store, now=NOW)["sent"] is True


def test_pg_is_given_up_only_after_consecutive_failures(monkeypatch, tmp_path):
    """Alerting must work before migrations/2026-09-02_alert_state.sql is applied.

    It used to stop trying PG after ONE failure, for the rest of the tick --
    which is how a single blip on 2026-10-25 21:30 made Friday's alerts new.
    """
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    calls = {"get": 0, "put": 0}

    def get(key):
        calls["get"] += 1
        raise RuntimeError("no such table")

    def put(alert, now):
        calls["put"] += 1
        raise RuntimeError("no such table")

    monkeypatch.setattr(store, "_pg_get", get)
    monkeypatch.setattr(store, "_pg_put", put)
    assert notify._PG_MAX_CONSECUTIVE_FAILURES == 2
    assert store.last("a") is None and store.last("b") is None
    assert calls["get"] == 2
    assert store.last("c") is None
    assert calls["get"] == 2, "two failures in a row: stop asking PG this run"
    assert store.degraded is True and store.last_source == "file"
    store.record(_standing(), now=NOW)
    assert calls["put"] == 0
    row = json.loads((tmp_path / "s.json").read_text())[_standing().key]
    assert row["pg_pending"] is True, "still owed to PG"


def test_a_success_resets_the_consecutive_count(monkeypatch, tmp_path):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    answers = [RuntimeError("blip"), None, RuntimeError("blip"), None]
    calls = {"n": 0}

    def flaky(key):
        a = answers[calls["n"]]
        calls["n"] += 1
        if isinstance(a, Exception):
            raise a
        return a

    monkeypatch.setattr(store, "_pg_get", flaky)
    for key in "abcd":
        store.last(key)
    assert calls["n"] == 4, "fail, ok, fail: never two in a row, never given up"
    assert store.last_source == "pg" and store.degraded is True


def test_a_pg_blip_does_not_resend_a_two_day_old_event(monkeypatch, tmp_path, posted):
    """Replays 2026-10-25 21:30: PG failed once, Friday's failures went out as new."""
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    old = Alert(key="evotip:tims10878:20261023T1230", kind="evotip",
                instrument="timsTOF HPZ6", headline="Evotip missing / not picked up",
                at="2026-10-23 12:32")
    now = datetime(2026, 10, 26, 4, 30, tzinfo=timezone.utc)   # Sun 21:30 PDT
    res = notify.notify([old], store=store, now=now)
    assert old.key in res["held"]
    texts = [c["body"]["text"] for c in posted]
    assert not any("Evotip missing" in t for t in texts)
    assert any("alert memory (PG) is unavailable" in t for t in texts)


def test_a_fresh_event_still_goes_out_while_pg_is_down(monkeypatch, tmp_path, posted):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    fresh = Alert(key="evotip:tims10878:20261024T0200", kind="evotip",
                  instrument="TIMS-10878", headline="queue STOPPED — No Evotip",
                  at="2026-10-24 02:04")
    now = datetime(2026, 10, 24, 9, 5, tzinfo=timezone.utc)    # 02:05 PDT
    res = notify.notify([fresh], store=store, now=now)
    assert res["sent"] and fresh.key not in res["held"]


def test_pg_is_retried_after_one_failure(monkeypatch, tmp_path):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    calls = {"n": 0}
    def flaky(key):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("blip")
        return {"last_sent": "2026-10-23T21:00:29+00:00", "signature": "",
                "kind": "evotip", "thread": None, "ack": None}
    monkeypatch.setattr(store, "_pg_get", flaky)
    assert store.last("a") is None and store.last_source == "file"
    assert store.last("b") is not None and store.last_source == "pg"


def test_sends_during_a_failure_reach_pg_later(monkeypatch, tmp_path):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_put", _raising)
    store.record(_standing(), now=NOW)
    row = json.loads((tmp_path / "s.json").read_text())[_standing().key]
    assert row["pg_pending"] is True
    pushed = []
    monkeypatch.setattr(store, "_pg_put", lambda a, now: pushed.append(a.key))
    assert store.sync_pending_to_pg() == 1 and pushed == [_standing().key]


def test_sync_clears_the_flag_and_pushes_what_was_sent(monkeypatch, tmp_path):
    down = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(down, "_pg_put", _raising)
    down.record(_standing(), now=NOW)
    pushed = []
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")   # next tick
    monkeypatch.setattr(store, "_pg_put",
                        lambda a, now: pushed.append((a.key, a.signature, now)))
    assert store.sync_pending_to_pg() == 1
    assert pushed == [(_standing().key, "critical:30", NOW)], \
        "the send time, not the sync time: PG keeps greatest(last_sent)"
    row = json.loads((tmp_path / "s.json").read_text())[_standing().key]
    assert "pg_pending" not in row
    assert store.sync_pending_to_pg() == 0


def test_a_pending_send_is_not_resent_when_pg_answers_first(monkeypatch, tmp_path,
                                                             posted):
    """Sync failed (blip), then PG answered the lookup: PG does not know yet."""
    path = tmp_path / "s.json"
    down = AlertStore(use_pg=True, path=path)
    monkeypatch.setattr(down, "_pg_put", _raising)
    down.record(_standing(), now=NOW)                 # sent while PG was down

    store = AlertStore(use_pg=True, path=path)
    monkeypatch.setattr(store, "sync_pending_to_pg", lambda: 0)
    monkeypatch.setattr(store, "_pg_get", lambda key: None)   # PG: never sent
    monkeypatch.setattr(store, "_pg_put", lambda a, now: None)
    res = notify.notify([_standing()], store=store, now=NOW + timedelta(minutes=30))
    assert res["sent"] is False and res["n_suppressed"] == 1
    assert not posted


def test_successful_pg_writes_are_mirrored_to_the_file(monkeypatch, tmp_path):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_put", lambda a, now: None)
    store.record(_standing(), now=NOW)
    assert _standing().key in json.loads((tmp_path / "s.json").read_text())


def test_ambiguous_local_time_on_dst_fallback_is_not_held(monkeypatch, tmp_path, posted):
    """2026-11-01 01:30 happens twice in Los Angeles."""
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    a = Alert(key="evotip:tims10878:20261101T0130", kind="evotip",
              instrument="TIMS-10878", headline="h", at="2026-11-01 01:30")
    now = datetime(2026, 11, 1, 9, 35, tzinfo=timezone.utc)    # second 01:35 PST
    assert a.key not in notify.notify([a], store=store, now=now)["held"]


def test_pg_failure_is_logged_at_warning_without_secrets(monkeypatch, tmp_path, caplog):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", lambda k: (_ for _ in ()).throw(
        RuntimeError(f"bad {HOOK}")))
    store.last("x")
    rec = [r for r in caplog.records if r.levelname == "WARNING"]
    assert rec and "RuntimeError" in rec[0].getMessage() and HOOK not in caplog.text


def test_pg_failure_warns_once_per_store(monkeypatch, tmp_path, caplog):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    store.last("x")
    store.last("y")
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_a_file_only_store_never_holds_anything(tmp_path, posted):
    """A single-lab SQLite install has no PG to wait for: holding there would
    drop every point alert older than an hour, permanently."""
    store = AlertStore(use_pg=False, path=tmp_path / "s.json")
    old = Alert(key="evotip:tims10878:20261023T1230", kind="evotip",
                instrument="timsTOF HPZ6", headline="Evotip missing",
                at="2026-10-23 12:32")
    now = datetime(2026, 10, 26, 4, 30, tzinfo=timezone.utc)
    res = notify.notify([old], store=store, now=now)
    assert res["held"] == [] and res["sent"] is True
    assert any("Evotip missing" in c["body"]["text"] for c in posted)


@pytest.mark.parametrize("at", [None, "", "not a time"])
def test_an_event_with_no_usable_time_is_never_held(monkeypatch, tmp_path, posted, at):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    a = Alert(key="evotip:x", kind="evotip", instrument="i", headline="h", at=at)
    res = notify.notify([a], store=store, now=NOW)
    assert res["held"] == [] and res["sent"] is True


def test_a_standing_condition_is_never_held(monkeypatch, tmp_path, posted):
    """Feed stale, watcher down: those are about now, PG or no PG."""
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    a = _standing()
    a.at = "2026-08-20 12:00"
    res = notify.notify([a], store=store, now=NOW)
    assert res["held"] == [] and res["sent"] is True


def test_the_held_notice_cools_off(monkeypatch, tmp_path, posted):
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get", _raising)
    old = Alert(key="evotip:old", kind="evotip", instrument="i", headline="h",
                at="2026-10-23 12:32")
    now = datetime(2026, 10, 26, 4, 30, tzinfo=timezone.utc)
    notify.notify([old], store=store, now=now)
    later = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(later, "_pg_get", _raising)
    res = notify.notify([old], store=later, now=now + timedelta(hours=1))
    assert old.key in res["held"]
    assert notify.HELD_NOTICE_KEY in res["suppressed"]
    assert len(posted) == 1


def test_file_lock_is_a_no_op_without_fcntl(monkeypatch, tmp_path):
    """Windows single-lab installs have no fcntl; recording must still work."""
    monkeypatch.setattr(notify, "fcntl", None)
    store = AlertStore(use_pg=False, path=tmp_path / "s.json")
    store.record(_standing(), now=NOW)
    assert store.last(_standing().key)["signature"] == "critical:30"


def test_the_file_lock_lives_next_to_the_state(tmp_path):
    store = AlertStore(use_pg=False, path=tmp_path / "s.json")
    pytest.importorskip("fcntl")
    store.record(_standing(), now=NOW)
    assert (tmp_path / "s.json.lock").exists()


def test_a_write_keeps_rows_another_process_added(tmp_path):
    """Two crons share one fallback file; a write must not clobber the other's."""
    path = tmp_path / "s.json"
    mine = AlertStore(use_pg=False, path=path)
    assert mine.last("theirs") is None                # cache loaded, empty
    AlertStore(use_pg=False, path=path).record(
        Alert(key="theirs", kind="k", instrument="i", headline="h"), now=NOW)
    mine.record(_standing(), now=NOW)
    data = json.loads(path.read_text())
    assert "theirs" in data and _standing().key in data


def test_mention_prefixes_text_and_first_block(monkeypatch, unconfigured):
    monkeypatch.setenv("STAN_SLACK_MENTION", "U0ABC1234")
    a = Alert(key="k", kind="evotip", instrument="TIMS-10878",
              headline="queue STOPPED — No Evotip", severity="critical", mention=True)
    text, blocks = render_alerts([a])
    assert text.startswith("<@U0ABC1234> ")
    assert blocks[0]["text"]["text"].startswith("<@U0ABC1234> ")

def test_no_mention_without_an_id_or_flag(monkeypatch, unconfigured):
    monkeypatch.delenv("STAN_SLACK_MENTION", raising=False)
    monkeypatch.setattr(notify.Path, "home", lambda: unconfigured.parent)
    a = Alert(key="k", kind="x", instrument="i", headline="h", mention=True)
    assert "<@" not in render_alerts([a])[0]
    monkeypatch.setenv("STAN_SLACK_MENTION", "U0ABC1234")
    b = Alert(key="k", kind="x", instrument="i", headline="h")   # mention False
    assert "<@" not in render_alerts([b])[0]

def test_a_malformed_mention_id_is_ignored(monkeypatch, unconfigured):
    monkeypatch.setenv("STAN_SLACK_MENTION", "@channel")
    assert notify.slack_mention_id() is None


def test_a_mention_with_no_member_id_warns_once(monkeypatch, unconfigured, caplog):
    """Deploy safety: an urgent alert that renders without its @mention looks
    sent and buzzes no phone. Say so at WARNING, once per process (one cron
    tick), not once per alert."""
    monkeypatch.setattr(notify, "slack_mention_id", lambda: None)
    monkeypatch.setattr(notify, "_mention_missing_warned", False, raising=False)
    urgent = Alert(key="k", kind="watcher_down", instrument="TIMS-10878",
                   headline="No heartbeat", severity="critical", mention=True)
    with caplog.at_level("WARNING", logger="stan.notify"):
        render_alerts([urgent, urgent])
        render_alerts([urgent])
    warned = [r for r in caplog.records
              if r.levelname == "WARNING" and "mention" in r.getMessage()]
    assert len(warned) == 1, caplog.text
    assert "STAN_SLACK_MENTION" in warned[0].getMessage()


def test_no_warning_when_the_mention_resolves_or_is_not_asked(monkeypatch, unconfigured, caplog):
    monkeypatch.setattr(notify, "_mention_missing_warned", False, raising=False)
    with caplog.at_level("WARNING", logger="stan.notify"):
        monkeypatch.setattr(notify, "slack_mention_id", lambda: "U0ABC1234")
        render_alerts([Alert(key="k", kind="x", instrument="i", headline="h", mention=True)])
        monkeypatch.setattr(notify, "slack_mention_id", lambda: None)
        render_alerts([Alert(key="k", kind="x", instrument="i", headline="h")])
    assert not [r for r in caplog.records if r.levelname == "WARNING"], caplog.text


# ── a stalled lock holder must not freeze every alerting cron ────


def _hold_lock(path):
    """Take the state file's lock on a separate open file, as another cron would."""
    fcntl = pytest.importorskip("fcntl")
    fh = open(path.with_name(path.name + ".lock"), "a")
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
    return fh


def test_a_held_lock_is_waited_for_only_so_long(monkeypatch, tmp_path, caplog):
    """Every alert cron shares one file on /quobyte. A holder that hangs (a
    stuck NFS op, a SIGSTOPped tick) must cost a WARNING, not a silent pager."""
    import time

    path = tmp_path / "s.json"
    monkeypatch.setattr(notify, "_LOCK_WAIT_SECONDS", 0.3)
    holder = _hold_lock(path)
    try:
        store = AlertStore(use_pg=False, path=path)
        t0 = time.monotonic()
        with caplog.at_level("WARNING", logger="stan.notify"):
            store.record(Alert(key="k", kind="k", instrument="i", headline="h"), now=NOW)
        elapsed = time.monotonic() - t0
    finally:
        holder.close()
    assert elapsed < 3.0, f"blocked {elapsed:.1f}s behind a held lock"
    assert "without the lock" in caplog.text
    assert store.last("k") is not None, "the write still happened"


def test_a_lock_released_in_time_is_taken(monkeypatch, tmp_path, caplog):
    import threading

    path = tmp_path / "s.json"
    monkeypatch.setattr(notify, "_LOCK_WAIT_SECONDS", 5.0)
    holder = _hold_lock(path)
    threading.Timer(0.3, holder.close).start()
    store = AlertStore(use_pg=False, path=path)
    with caplog.at_level("WARNING", logger="stan.notify"):
        store.record(Alert(key="k", kind="k", instrument="i", headline="h"), now=NOW)
    assert "without the lock" not in caplog.text
    assert store.last("k") is not None
