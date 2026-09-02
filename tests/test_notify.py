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

HOOK = "https://hooks.slack.com/services/T000/B000/xxxxxxxxxxxxxxxxxxxxxxxx"
NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)


# ── fakes ────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def posted(monkeypatch):
    """Capture what would have gone to Slack. No socket is ever opened."""
    calls = []

    def _fake(req, timeout=None):
        calls.append({"url": req.full_url,
                      "body": json.loads(req.data.decode()),
                      "headers": dict(req.headers)})
        return _Resp(200)

    monkeypatch.setattr(notify.urllib.request, "urlopen", _fake)
    monkeypatch.setenv(notify.WEBHOOK_ENV, HOOK)
    return calls


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


def test_pg_failure_falls_back_to_the_file(monkeypatch, tmp_path, posted):
    """Alerting must work before migrations/2026-09-02_alert_state.sql is applied."""
    store = AlertStore(use_pg=True, path=tmp_path / "s.json")
    monkeypatch.setattr(store, "_pg_get",
                        lambda key: (_ for _ in ()).throw(RuntimeError("no such table")))
    assert store.last("anything") is None
    assert store._use_pg is False, "stop trying PG once it has failed"
    store.record(_standing(), now=NOW)
    assert (tmp_path / "s.json").exists()
