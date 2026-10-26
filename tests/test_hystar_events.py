"""Hive's half of the HyStar watcher: relay, remember, and watch the watcher.

Saturday 2026-10-24 is the case these replay: HyStar stopped the timsTOF
queue at 02:04:24 over a missing Evotip (vial S1-A5). The watcher on the PC
posts that itself and drops an event file; Hive must then

  * relay it only if the PC could not post it, and only once;
  * mark the keys the hourly Evosep path would use, so its 03:00 alert about
    the same injection never goes out;
  * page if the watcher's heartbeat stops, once, and say when it is back;
  * let the QC summary find the stop for the empty run it caused.

No test reaches Slack or PG: `posted` (tests/conftest.py) captures posts, and
the autouse fixture below makes PG unreachable and threading unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from stan.notify import AlertStore, notify
from stan.reports.hystar_events import (
    DEFAULT_EVENT_ROOT,
    HOST,
    SUPPRESS_KINDS,
    check_watcher_heartbeat,
    find_stop_for_run,
    load_new_events,
    mark_processed,
    relay_event,
    run_hystar_tick,
    suppression_keys,
)

REPO = Path(__file__).resolve().parent.parent
FIX = Path(__file__).parent / "fixtures" / "hystar_events"
PT = ZoneInfo("America/Los_Angeles")


def _pdt(y, mo, d, h, mi, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=PT)


SAT_0206 = _pdt(2026, 10, 24, 2, 6)
SAT_0207 = _pdt(2026, 10, 24, 2, 7)
SAT_0300 = _pdt(2026, 10, 24, 3, 0)


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _copy_fixture(root, name, processed=False):
    host = root / HOST
    dest = host / "processed" / "2026-10" if processed else host
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / name, dest / name)
    return host


def _write_heartbeat(host, at):
    doc = _load("heartbeat.json")
    doc["at"] = at
    (host / "heartbeat.json").write_text(json.dumps(doc))


def _host_dir(root, heartbeat_at=None):
    host = root / HOST
    host.mkdir(parents=True, exist_ok=True)
    if heartbeat_at:
        _write_heartbeat(host, heartbeat_at)
    return host


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """No PG (its alert_state is the live one), no bot-token threading, and no
    `runs` lookup for the identity line."""
    import stan.db_pg
    from stan import slack_api

    def _no_pg(*a, **k):
        raise RuntimeError("no PG in tests")

    monkeypatch.setattr(stan.db_pg, "_connect", _no_pg)
    monkeypatch.setattr(slack_api, "threading_available", lambda: False)
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)


@pytest.fixture
def store(tmp_path):
    return AlertStore(use_pg=False, path=tmp_path / "s.json")


# ── the brief's cases ────────────────────────────────────────────


def test_suppression_keys_cover_the_hourly_paths_saturday_key():
    ev = _load("stop_saturday.json")          # at 2026-10-24T02:04:24, category evotip
    keys = suppression_keys(ev)
    assert "evotip:tims10878:20261024T0200" in keys      # the 03:00 alert's real key
    assert "aborted:tims10878:20261024T0200" in keys
    assert "evotip:tims10878:20261024T0100" in keys      # mid-run MS errors start earlier


def test_pc_posted_event_is_recorded_not_reposted(tmp_path, posted, store):
    host = _copy_fixture(tmp_path, "stop_saturday.json")  # posted_by_pc: true
    res = run_hystar_tick(tmp_path, store=store, now=SAT_0206)
    assert posted == [] and res["relayed"] == []
    assert store.last("evotip:tims10878:20261024T0200") is not None
    assert not list(host.glob("*.json")) or {p.name for p in host.glob("*.json")} <= {"heartbeat.json"}


def test_unposted_event_is_relayed_once_with_the_relay_note(tmp_path, posted, store):
    _copy_fixture(tmp_path, "stop_saturday_unposted.json")
    run_hystar_tick(tmp_path, store=store, now=SAT_0206)
    run_hystar_tick(tmp_path, store=store, now=SAT_0207)
    assert len(posted) == 1
    assert "Relayed by Hive" in json.dumps(posted[0]["body"])
    assert posted[0]["body"]["text"].startswith("<@")


def test_the_hourly_evosep_tick_is_then_silent(tmp_path, posted, store):
    _copy_fixture(tmp_path, "stop_saturday.json")
    run_hystar_tick(tmp_path, store=store, now=SAT_0206)
    doc = {"flags": [{"run": "100-samples-per-day_2026-10-24_02-03-36", "start": "2026-10-24T02:03:36",
                      "kinds": ["tip"], "severity": "warning", "method": "m"}]}
    from stan.reports.instrument_watch import check_evosep_episodes
    res = notify(check_evosep_episodes(doc, now=SAT_0300), store=store, now=SAT_0300)
    assert res["n_fresh"] == 0


def test_watcher_down_pages_once_then_recovers(tmp_path, posted, store):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 2, 11))
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 2, 40))
    _write_heartbeat(host, "2026-10-24T03:00:00")
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 3, 0))
    bodies = [p["body"]["text"] for p in posted]
    assert len(bodies) == 2 and "<@" in bodies[0] and "reporting again" in bodies[1]


def test_no_heartbeat_ever_means_not_installed_not_down(tmp_path, posted, store):
    (tmp_path / "TIMS-10878").mkdir()
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 3, 0))
    assert posted == []


def test_only_stop_events_suppress_hourly_keys():
    assert suppression_keys({**_load("stop_saturday.json"), "type": "reminder"}) == []


def test_pause_silences_the_down_alert(tmp_path, posted, store):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    (host / "pause_until.json").write_text('{"until": "2026-10-24T10:00:00"}')
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 3, 0))
    assert posted == []


def test_qc_run_finds_its_stop(tmp_path):
    _copy_fixture(tmp_path, "stop_saturday.json", processed=True)
    hit = find_stop_for_run("10212026_HE50_100-spd-dia_S1-A5_1_25352.d",
                            "2026-10-24T09:04:25+00:00", root=tmp_path)
    assert hit and hit["category"] == "evotip"
    assert find_stop_for_run("other_S1-A6_1_25353.d", "2026-10-24T09:04:25+00:00", root=tmp_path) is None
    assert find_stop_for_run("10212026_HE50_100-spd-dia_S1-A5_1_25352.d",
                             "2026-10-24T09:30:00+00:00", root=tmp_path) is None


# ── keys ─────────────────────────────────────────────────────────


def test_suppression_keys_span_exactly_the_hour_before_the_stop():
    keys = suppression_keys(_load("stop_saturday.json"))
    buckets = sorted({k.rsplit(":", 1)[1] for k in keys})
    assert buckets == ["20261024T0100", "20261024T0110", "20261024T0120",
                       "20261024T0130", "20261024T0140", "20261024T0150",
                       "20261024T0200"]
    assert {k.split(":", 1)[0] for k in keys} == {"evotip", "aborted"}


def test_each_category_suppresses_its_own_kinds():
    ev = _load("stop_saturday.json")
    for category, kinds in SUPPRESS_KINDS.items():
        got = {k.split(":", 1)[0] for k in suppression_keys({**ev, "category": category})}
        assert got == set(kinds), category
    assert suppression_keys({**ev, "category": "hystar_crash"}) == []


def test_an_event_with_no_time_suppresses_nothing():
    assert suppression_keys({**_load("stop_saturday.json"), "at": None}) == []


# ── event I/O ────────────────────────────────────────────────────


def test_load_skips_heartbeat_and_pause_and_sorts_by_time(tmp_path):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:05:00")
    shutil.copy(FIX / "pause_until.json", host / "pause_until.json")
    late = {**_load("stop_saturday.json"), "id": "late", "at": "2026-10-24T05:00:00"}
    (host / "a_late.json").write_text(json.dumps(late))
    shutil.copy(FIX / "stop_saturday.json", host / "z_early.json")
    assert [e["id"] for e in load_new_events(host)] == ["TIMS-10878_20261024T020424_1", "late"]


def test_a_bom_from_powershell_is_read(tmp_path):
    """PS 5.1 `-Encoding UTF8` writes a BOM; json.loads refuses it."""
    host = _host_dir(tmp_path)
    (host / "ev.json").write_bytes(b"\xef\xbb\xbf" + (FIX / "stop_saturday.json").read_bytes())
    assert [e["id"] for e in load_new_events(host)] == ["TIMS-10878_20261024T020424_1"]


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"type": "stop"}', "\xff\xfe"])
def test_a_bad_event_is_rejected_not_fatal(tmp_path, posted, store, caplog, content):
    host = _host_dir(tmp_path)
    (host / "bad.json").write_text(content, encoding="latin-1")
    _copy_fixture(tmp_path, "stop_saturday_unposted.json")
    with caplog.at_level("WARNING"):
        res = run_hystar_tick(tmp_path, store=store, now=SAT_0206)
    assert (host / "rejected" / "bad.json").exists()
    assert res["rejected"] == ["bad.json"]
    assert "bad.json" in caplog.text
    assert len(posted) == 1, "the good event still goes out"


def test_mark_processed_files_by_the_event_month(tmp_path):
    host = _copy_fixture(tmp_path, "stop_saturday.json")
    (ev,) = load_new_events(host)
    mark_processed(host, ev)
    assert (host / "processed" / "2026-10" / "stop_saturday.json").exists()
    assert not (host / "stop_saturday.json").exists()


# ── relay ────────────────────────────────────────────────────────


def test_relay_posts_the_pc_payload_verbatim_plus_one_note(posted, store):
    ev = _load("stop_saturday_unposted.json")
    assert relay_event(ev, store, SAT_0206) is True
    body = posted[0]["body"]
    assert body["text"] == ev["payload"]["text"]
    assert body["blocks"][:-1] == ev["payload"]["blocks"]
    assert body["blocks"][-1] == {"type": "context", "elements": [
        {"type": "mrkdwn", "text": "Relayed by Hive: the watcher could not reach Slack"}]}
    assert ev["payload"]["blocks"][-1]["type"] == "context" and \
        "Relayed" not in json.dumps(ev["payload"]), "the event itself is not modified"
    assert store.last("hystar:tims10878:TIMS-10878_20261024T020424_1") is not None


def test_a_relay_with_no_blocks_still_shows_the_message(posted, store):
    """Blocks replace the text in the channel; a lone context block would
    show only 'Relayed by Hive' and lose the stop itself."""
    ev = _load("stop_saturday_unposted.json")
    ev["payload"] = {"text": ev["payload"]["text"], "blocks": []}
    relay_event(ev, store, SAT_0206)
    blocks = posted[0]["body"]["blocks"]
    assert blocks[0]["type"] == "section" and "queue STOPPED" in blocks[0]["text"]["text"]
    assert [b["type"] for b in blocks].count("context") == 1


def test_only_text_and_blocks_are_forwarded(posted, store):
    ev = _load("stop_saturday_unposted.json")
    ev["payload"] = {**ev["payload"], "channel": "#general", "username": "x"}
    relay_event(ev, store, SAT_0206)
    assert set(posted[0]["body"]) == {"text", "blocks"}


def test_every_unposted_type_is_relayed_but_only_stops_suppress(posted, store):
    ev = {**_load("stop_saturday_unposted.json"), "type": "test", "id": "t1"}
    assert relay_event(ev, store, SAT_0206) is True
    assert len(posted) == 1
    assert store.last("hystar:tims10878:t1") is not None
    assert store.last("evotip:tims10878:20261024T0200") is None


def test_an_unsent_relay_is_retried_and_not_recorded(tmp_path, monkeypatch, store):
    from stan import notify as notify_mod

    monkeypatch.setenv(notify_mod.WEBHOOK_ENV, "https://hooks.slack.com/services/T/B/x")

    def _down(req, timeout=None):
        raise OSError("Network is unreachable")

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", _down)
    host = _copy_fixture(tmp_path, "stop_saturday_unposted.json")
    res = run_hystar_tick(tmp_path, store=store, now=SAT_0206)
    assert res["pending"] == ["TIMS-10878_20261024T020424_1"]
    assert (host / "stop_saturday_unposted.json").exists(), "left for the next tick"
    assert store.last("evotip:tims10878:20261024T0200") is None


def test_an_event_already_recorded_is_not_posted_again(tmp_path, posted, store):
    """A relay whose move to processed/ failed must not page twice."""
    ev = _load("stop_saturday_unposted.json")
    relay_event(ev, store, SAT_0206)
    _copy_fixture(tmp_path, "stop_saturday_unposted.json")
    run_hystar_tick(tmp_path, store=store, now=SAT_0207)
    assert len(posted) == 1


def test_dry_run_posts_records_and_moves_nothing(tmp_path, posted, store):
    host = _copy_fixture(tmp_path, "stop_saturday_unposted.json")
    (host / "bad.json").write_text("{nope")
    res = run_hystar_tick(tmp_path, store=store, now=SAT_0206, dry_run=True)
    assert posted == []
    assert (host / "stop_saturday_unposted.json").exists() and (host / "bad.json").exists()
    assert store.last("hystar:tims10878:TIMS-10878_20261024T020424_1") is None
    assert res["would_relay"] == ["TIMS-10878_20261024T020424_1"]


def test_a_missing_root_is_a_quiet_tick(tmp_path, posted, store):
    res = run_hystar_tick(tmp_path / "nowhere", store=store, now=SAT_0206)
    assert posted == [] and res["events"] == 0


# ── heartbeat ────────────────────────────────────────────────────


def test_a_fresh_heartbeat_is_quiet(tmp_path):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:55:00")
    assert check_watcher_heartbeat(host, _pdt(2026, 10, 24, 3, 0)) == []


def test_the_down_alert_is_shaped_to_page(tmp_path):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    (a,) = check_watcher_heartbeat(host, _pdt(2026, 10, 24, 2, 11))
    assert a.key == "hystar_watcher_down:tims10878" and a.kind == "watcher_down"
    assert a.severity == "critical" and a.mention is True and a.cool_off_hours == 24.0
    assert a.headline == "No heartbeat from the HyStar watcher"
    assert "The queue is not being watched." in a.detail[0]


def test_ten_minutes_is_not_yet_down(tmp_path):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    assert check_watcher_heartbeat(host, _pdt(2026, 10, 24, 2, 10)) == []


def test_the_heartbeat_is_read_in_instrument_time(tmp_path):
    """A naive local `at` read as UTC would look 7 h old and page at once."""
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    assert check_watcher_heartbeat(host, datetime(2026, 10, 24, 9, 5, tzinfo=timezone.utc)) == []


def test_an_expired_pause_no_longer_silences(tmp_path):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    shutil.copy(FIX / "pause_until.json", host / "pause_until.json")   # until 10:00
    assert check_watcher_heartbeat(host, _pdt(2026, 10, 24, 9, 0)) == []
    assert check_watcher_heartbeat(host, _pdt(2026, 10, 24, 10, 1)) != []


def test_a_second_outage_the_same_day_pages_again(tmp_path, posted, store):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 2, 11))
    _write_heartbeat(host, "2026-10-24T03:00:00")
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 3, 0))
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 3, 30))
    texts = [p["body"]["text"] for p in posted]
    assert len(texts) == 3, texts
    assert "No heartbeat" in texts[0] and "reporting again" in texts[1] and "No heartbeat" in texts[2]


def test_the_down_marker_tracks_the_episode(tmp_path, posted, store):
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 2, 11))
    assert (host / ".down_alerted").exists()
    _write_heartbeat(host, "2026-10-24T03:00:00")
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 3, 0))
    assert not (host / ".down_alerted").exists()


def test_no_marker_is_written_when_the_page_did_not_go(tmp_path, monkeypatch, store):
    from stan import notify as notify_mod

    monkeypatch.delenv(notify_mod.WEBHOOK_ENV, raising=False)
    monkeypatch.setattr("stan.config.load_community", lambda: {})
    monkeypatch.setattr(notify_mod, "_webhook_file", lambda: tmp_path / "none")
    host = _host_dir(tmp_path, heartbeat_at="2026-10-24T02:00:00")
    run_hystar_tick(tmp_path, store=store, now=_pdt(2026, 10, 24, 2, 11))
    assert not (host / ".down_alerted").exists()
    assert store.last("hystar_watcher_down:tims10878") is None, "retried next tick"


def test_an_unreadable_heartbeat_falls_back_to_its_mtime(tmp_path):
    host = _host_dir(tmp_path)
    hb = host / "heartbeat.json"
    hb.write_text("{half")
    t = _pdt(2026, 10, 24, 2, 0).timestamp()
    os.utime(hb, (t, t))
    assert check_watcher_heartbeat(host, _pdt(2026, 10, 24, 2, 5)) == []
    assert check_watcher_heartbeat(host, _pdt(2026, 10, 24, 2, 30)) != []


# ── QC lookup ────────────────────────────────────────────────────


def test_a_stop_without_a_vial_matches_only_within_90_s(tmp_path):
    ev = {**_load("stop_saturday.json"), "vial": None}
    (tmp_path / HOST).mkdir()
    (tmp_path / HOST / "ev.json").write_text(json.dumps(ev))
    name = "10212026_HE50_100-spd-dia_S1-A5_1_25352.d"
    assert find_stop_for_run(name, "2026-10-24T09:05:30+00:00", root=tmp_path) is not None
    assert find_stop_for_run(name, "2026-10-24T09:06:30+00:00", root=tmp_path) is None


def test_the_lookup_reaches_last_months_folder(tmp_path):
    """A stop at 23:59 on the 31st and its .d at 00:00 on the 1st."""
    ev = {**_load("stop_saturday.json"), "at": "2026-10-31T23:59:30"}
    d = tmp_path / HOST / "processed" / "2026-10"
    d.mkdir(parents=True)
    (d / "ev.json").write_text(json.dumps(ev))
    hit = find_stop_for_run("x_S1-A5_1_1.d", "2026-11-01T00:00:40-07:00", root=tmp_path)
    assert hit is not None


def test_only_stops_match_a_qc_run(tmp_path):
    _copy_fixture(tmp_path, "stop_saturday.json")
    p = tmp_path / HOST / "stop_saturday.json"
    p.write_text(json.dumps({**_load("stop_saturday.json"), "type": "reminder"}))
    assert find_stop_for_run("x_S1-A5_1_1.d", "2026-10-24T09:04:25+00:00", root=tmp_path) is None


def test_the_lookup_never_raises(tmp_path):
    assert find_stop_for_run("x_S1-A5_1_1.d", "not a date", root=tmp_path) is None
    assert find_stop_for_run("x_S1-A5_1_1.d", None, root=tmp_path) is None
    assert find_stop_for_run(None, "2026-10-24T09:04:25+00:00", root=tmp_path / "none") is None


def test_the_default_root_is_the_share():
    assert DEFAULT_EVENT_ROOT == Path("/quobyte/proteomics-grp/STAN/hystar_events")


# ── CLI ──────────────────────────────────────────────────────────


def test_cli_prints_the_summary(tmp_path, posted):
    from typer.testing import CliRunner

    from stan.cli import app

    _copy_fixture(tmp_path, "stop_saturday.json")
    r = CliRunner().invoke(app, ["hystar-events", "--root", str(tmp_path), "--dry-run"],
                           env={"STAN_ALERT_STATE": str(tmp_path / "s.json")})
    assert r.exit_code == 0, r.output
    summary = json.loads(r.output.strip().splitlines()[-1])
    assert summary["dry_run"] is True and summary["events"] == 1
    assert posted == []


# ── the cron's bash fast path (no Python when idle) ──────────────


def _run_cron(tmp_path, root):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    stan = venv / "bin" / "stan"
    stan.write_text("#!/bin/bash\necho \"FAKE-STAN $*\"\n")
    stan.chmod(0o755)
    logs = tmp_path / "logs"
    env = {**os.environ, "STAN_HYSTAR_EVENT_ROOT": str(root),
           "STAN_CRON_LOG_DIR": str(logs), "STAN_VENV": str(venv)}
    r = subprocess.run(["bash", str(REPO / "scripts" / "cron_hystar_events.sh")],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    (log,) = logs.glob("cron_hystar_events_20*.log")
    return log.read_text()


def _age(path, minutes):
    t = time.time() - minutes * 60
    os.utime(path, (t, t))


@pytest.mark.parametrize("setup, python_runs", [
    ("no_host_dir", False),
    ("not_installed", False),
    ("fresh_heartbeat", False),
    ("stale_heartbeat", True),
    ("event_waiting", True),
    ("down_marker", True),
])
def test_cron_fast_path(tmp_path, setup, python_runs):
    root = tmp_path / "events"
    host = root / HOST
    if setup != "no_host_dir":
        host.mkdir(parents=True)
    if setup in ("fresh_heartbeat", "stale_heartbeat", "event_waiting", "down_marker"):
        shutil.copy(FIX / "heartbeat.json", host / "heartbeat.json")
        shutil.copy(FIX / "pause_until.json", host / "pause_until.json")
    if setup == "stale_heartbeat":
        _age(host / "heartbeat.json", 11)
    if setup == "event_waiting":
        shutil.copy(FIX / "stop_saturday.json", host / "stop_saturday.json")
    if setup == "down_marker":
        (host / ".down_alerted").write_text("{}")
    log = _run_cron(tmp_path, root)
    assert ("FAKE-STAN hystar-events --root" in log) is python_runs, log
    if not python_runs:
        assert log.rstrip().endswith("idle") or "idle (" in log, log


def test_cron_script_carries_its_crontab_line_and_env():
    text = (REPO / "scripts" / "cron_hystar_events.sh").read_text()
    assert ("* * * * * flock -n /tmp/stan_hystar_events.lock bash "
            "/quobyte/proteomics-grp/STAN/cron_hystar_events.sh") in text
    for line in ("export STAN_DB_BACKEND=pg",
                 "export STAN_PGFARM_TOKEN_FILE=/quobyte/proteomics-grp/brett/.pgfarm_token",
                 "export STAN_ALERT_STATE=/quobyte/proteomics-grp/STAN/alert_state.json"):
        assert line in text
    assert ".stan_slack_mention" in text and ".stan_slack_webhook" in text


def test_every_alert_cron_shares_one_fallback_file():
    """Spec §4.5: two fallback files meant neither mirror covered the other."""
    want = "export STAN_ALERT_STATE=/quobyte/proteomics-grp/STAN/alert_state.json"
    for rel in ("scripts/cron_stan_alerts.sh", "instrument/evosep/cron_evosep.sh",
                "scripts/cron_hystar_events.sh"):
        assert want in (REPO / rel).read_text(), rel


def test_the_heartbeat_watches_this_cron():
    from stan.reports.instrument_watch import CRON_LOGS
    assert CRON_LOGS["hystar_events"] == ("cron_hystar_events_20*.log", 0.5)
