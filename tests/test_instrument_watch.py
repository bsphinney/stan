"""Instrument fault alerting: what fires, and — mostly — what stays quiet.

The failure this exists for is real and dated: on the night of 2026-08-31 the
column clogged over seven consecutive runs and nobody was told until morning.
So the first test below replays that episode and insists it fires.

Every other test is about silence. 568 runs over that fortnight carried 27
flags; a watcher that alerted on all of them would produce 27 pings in 18 days
and be muted inside a week, at which point the next clog goes unnoticed for
exactly the reason this one did. The thresholds are calibrated against that
window, and the tests pin them there.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from stan.notify import Alert
from stan.reports.instrument_watch import (
    CLOG_CRITICAL_RUNS,
    COLUMN_DRIFT_PCT,
    LOOKBACK_HOURS,
    RISING_RUNS,
    STANDING_COOLOFF_HOURS,
    check_bruker,
    check_column_wear,
    check_evosep,
    log_clog_events,
)

NOW = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
CEILING = 520.0


def _flag(start, severity="critical", kinds=("high_pressure",),
          plateau=430.0, peak=505.0, baseline=322.0, pct=33.0,
          method="100-samples-per-day", **kw):
    f = {"run": f"{method}_{start}", "method": method, "start": start,
         "severity": severity, "kinds": list(kinds),
         "reasons": [f"column backpressure {plateau:.0f} bar is {pct:.0f}% "
                     f"above the {baseline:.0f} bar running baseline"],
         "plateau_bar": plateau, "peak_bar": peak, "baseline_bar": baseline,
         "pct_over_baseline": pct, "duration_min": 14.06,
         "tip_pressure_max_bar": 9.5}
    f.update(kw)
    return f


def _doc(flags, column=None):
    return {"instrument_host": "TIMS-10878", "ceiling_bar": CEILING,
            "column_pump": "Pump-HP", "flags": list(flags),
            "column": column or {"known": True, "baseline_change_pct": 10.0}}


def _series(n, first="2026-09-01T00:00:00", step_min=14, **kw):
    """n consecutive runs, 14 min apart — the 100 SPD cadence."""
    t0 = datetime.fromisoformat(first)
    return [_flag((t0 + timedelta(minutes=step_min * i)).isoformat(), **kw)
            for i in range(n)]


# ── the incident this was built for ──────────────────────────────


def test_the_2026_08_31_clog_fires():
    """Six criticals from 23:18 to 00:29, one at the 520 bar ceiling."""
    flags = [
        _flag("2026-08-31T23:18:45", plateau=399.6, peak=494.0, pct=24.4),
        _flag("2026-08-31T23:32:54", plateau=417.8, peak=499.2, pct=29.8),
        _flag("2026-08-31T23:47:02", plateau=417.8, peak=499.2, pct=29.8),
        _flag("2026-09-01T00:01:10", plateau=442.0, peak=519.1, pct=37.4,
              kinds=("ceiling", "high_pressure")),
        _flag("2026-09-01T00:15:20", plateau=417.8, peak=501.8, pct=29.8),
        _flag("2026-09-01T00:29:29", plateau=432.5, peak=505.2, pct=34.2),
        _flag("2026-09-01T00:43:39", severity="elevated", plateau=408.3,
              peak=499.2, pct=13.1),
    ]
    alerts = check_evosep(_doc(flags), now=NOW)
    kinds = {a.kind for a in alerts}
    assert "clog" in kinds
    assert "overpressure" in kinds

    clog = next(a for a in alerts if a.kind == "clog")
    assert clog.severity == "critical"
    # The headline has to carry the number and be readable on a lock screen.
    assert "432" in clog.headline and "34%" in clog.headline
    assert "Evosep One (TIMS-10878)" == clog.instrument
    # And the body must state the threshold that was crossed.
    assert str(CLOG_CRITICAL_RUNS) in " ".join(clog.detail)
    assert "520" in " ".join(clog.detail)


def test_a_clog_spanning_forty_runs_is_one_alert():
    """The whole point of keying on the condition, not the observation."""
    alerts = check_evosep(_doc(_series(40, first="2026-09-01T00:00:00")),
                          now=datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc))
    assert len([a for a in alerts if a.kind == "clog"]) == 1


# ── over-pressure ────────────────────────────────────────────────


def test_one_run_at_the_ceiling_is_enough():
    """Not a trend — the pump saying it cannot push through."""
    alerts = check_evosep(
        _doc([_flag("2026-09-01T02:00:00", kinds=("ceiling", "high_pressure"),
                    peak=519.2)]), now=NOW)
    over = [a for a in alerts if a.kind == "overpressure"]
    assert len(over) == 1
    assert over[0].severity == "critical"
    assert "519" in over[0].headline and "520" in over[0].headline


def test_a_peak_near_the_ceiling_counts_even_if_untagged():
    """Belt and braces around the extractor's `kinds`."""
    alerts = check_evosep(
        _doc([_flag("2026-09-01T02:00:00", kinds=("high_pressure",), peak=517.0)]),
        now=NOW)
    assert any(a.kind == "overpressure" for a in alerts)


def test_a_normal_peak_is_not_over_pressure():
    alerts = check_evosep(
        _doc([_flag("2026-09-01T02:00:00", kinds=("high_pressure",), peak=466.0)]),
        now=NOW)
    assert not any(a.kind == "overpressure" for a in alerts)


# ── silence ──────────────────────────────────────────────────────


def test_a_clean_document_says_nothing():
    assert check_evosep(_doc([]), now=NOW) == []


def test_one_critical_run_is_not_a_clog():
    """A single critical can be a bubble or a badly seated tip."""
    alerts = check_evosep(_doc([_flag("2026-09-01T02:00:00", peak=470.0)]), now=NOW)
    assert not any(a.kind == "clog" for a in alerts)


def test_the_clog_threshold_is_exactly_where_it_says_it_is():
    at = check_evosep(_doc(_series(CLOG_CRITICAL_RUNS,
                                   first="2026-09-01T02:00:00", peak=470.0)), now=NOW)
    below = check_evosep(_doc(_series(CLOG_CRITICAL_RUNS - 1,
                                      first="2026-09-01T02:00:00", peak=470.0)), now=NOW)
    assert any(a.kind == "clog" for a in at)
    assert not any(a.kind == "clog" for a in below)


def test_scattered_criticals_hours_apart_are_not_one_clog():
    """Two events six hours apart are two episodes, and neither is a clog."""
    flags = [_flag("2026-09-01T00:00:00", peak=470.0),
             _flag("2026-09-01T06:00:00", peak=470.0)]
    alerts = check_evosep(_doc(flags), now=datetime(2026, 9, 1, 7, 0,
                                                    tzinfo=timezone.utc))
    assert not any(a.kind == "clog" for a in alerts)


def test_a_wash_between_analytical_runs_does_not_join_the_episode():
    """Different method, different pressure regime — not a continuation."""
    flags = [_flag("2026-09-01T02:00:00", peak=470.0),
             _flag("2026-09-01T02:14:00", peak=470.0,
                   method="System-and-column-wash")]
    alerts = check_evosep(_doc(flags), now=NOW)
    assert not any(a.kind == "clog" for a in alerts)


def test_old_faults_are_not_dredged_up():
    """A fresh state file must not replay a fortnight into the channel."""
    old = NOW - timedelta(hours=LOOKBACK_HOURS + 2)
    alerts = check_evosep(_doc(_series(6, first=old.replace(tzinfo=None).isoformat())),
                          now=NOW)
    assert alerts == []


def test_a_finished_episode_is_not_re_announced_as_live():
    """An episode that ended yesterday sits inside the lookback all day.

    Without the liveness gate, the 12 h cool-off would re-ping it as though
    the column were still blocked.
    """
    n = 6
    # The episode must END outside the cool-off, not merely start there.
    started = NOW - timedelta(hours=STANDING_COOLOFF_HOURS + 1,
                              minutes=14 * (n - 1))
    alerts = check_evosep(_doc(_series(n, first=started.replace(tzinfo=None).isoformat())),
                          now=NOW)
    assert not any(a.kind == "clog" for a in alerts)

    # Still inside it, the same episode is live and does alert.
    live = NOW - timedelta(hours=STANDING_COOLOFF_HOURS - 1,
                           minutes=14 * (n - 1))
    assert any(a.kind == "clog" for a in
               check_evosep(_doc(_series(n, first=live.replace(tzinfo=None).isoformat())),
                            now=NOW))


# ── early warning ────────────────────────────────────────────────


def test_a_rising_trend_that_never_goes_critical_still_warns():
    """2026-08-29 ran six consecutive `elevated` flags. That is a column
    heading for trouble, and the Compass error log cannot see it at all."""
    alerts = check_evosep(
        _doc(_series(RISING_RUNS, severity="elevated", plateau=421.0,
                     peak=500.0, pct=17.0, first="2026-09-01T04:00:00")),
        now=NOW)
    rising = [a for a in alerts if a.kind == "pressure_rising"]
    assert len(rising) == 1
    assert rising[0].severity == "warning"
    assert str(RISING_RUNS) in " ".join(rising[0].detail)


def test_a_short_run_of_elevated_flags_stays_quiet():
    alerts = check_evosep(
        _doc(_series(RISING_RUNS - 1, severity="elevated", plateau=421.0,
                     peak=500.0, pct=17.0, first="2026-09-01T04:00:00")),
        now=NOW)
    assert not any(a.kind == "pressure_rising" for a in alerts)


def test_a_clog_does_not_also_report_a_rising_trend():
    """One condition, one line — the clog supersedes its own early warning."""
    alerts = check_evosep(_doc(_series(6, first="2026-09-01T04:00:00")), now=NOW)
    assert not any(a.kind == "pressure_rising" for a in alerts)


# ── tips, aborts, column wear ────────────────────────────────────


def test_an_unseated_evotip_is_reported_once_per_event():
    flags = [_flag("2026-09-01T02:00:00", severity="elevated",
                   kinds=("tip", "aborted"), pct=-9.7)]
    alerts = check_evosep(_doc(flags), now=NOW)
    tips = [a for a in alerts if a.kind == "evotip"]
    assert len(tips) == 1
    assert tips[0].cool_off_hours is None, "a point event is said once, ever"


def test_a_bare_abort_is_not_double_reported_as_a_tip():
    flags = [_flag("2026-09-01T02:00:00", severity="elevated", kinds=("aborted",))]
    kinds = {a.kind for a in check_evosep(_doc(flags), now=NOW)}
    assert "aborted" in kinds and "evotip" not in kinds


def test_column_wear_fires_only_past_the_drift_threshold():
    worn = _doc([], column={"known": True, "baseline_change_pct": COLUMN_DRIFT_PCT,
                            "confidence": "logged", "log_covers_install": True,
                            "baseline_at_install_bar": 312.4,
                            "baseline_now_bar": 400.0, "installed": "2026-07-31",
                            "injections_since": 455, "days_since": 13.2})
    fresh = _doc([], column={"known": True, "baseline_change_pct": 10.0,
                             "confidence": "logged", "log_covers_install": True})
    assert any(a.kind == "column_worn" for a in check_evosep(worn, now=NOW))
    assert not any(a.kind == "column_worn" for a in check_evosep(fresh, now=NOW))


def test_an_unknown_column_does_not_alert():
    doc = _doc([], column={"known": False, "baseline_change_pct": 90.0})
    assert not any(a.kind == "column_worn" for a in check_evosep(doc, now=NOW))


def test_an_unverifiable_column_age_stays_silent():
    """2026-09-02: the inferred install (08-19) was a capillary swap, not a
    column. The real change was 07-31, before the log window opens. A wear
    alert built on that age would be confidently wrong."""
    doc = _doc([], column={"known": True, "baseline_change_pct": 90.0,
                           "confidence": "unverifiable"})
    assert check_column_wear(doc) == []


def test_a_document_with_no_confidence_field_stays_silent():
    """Fails closed. Three windows on 2026-09-02 gave three install dates
    (09-01, 08-19, truth 07-31); a document too old to say which it is does
    not get to drive an alert."""
    doc = _doc([], column={"known": True, "baseline_change_pct": 90.0})
    assert check_column_wear(doc) == []


def test_a_lower_bound_install_date_stays_silent():
    doc = _doc([], column={"known": True, "baseline_change_pct": 90.0,
                           "confidence": "inferred",
                           "installed_is_lower_bound": True})
    assert check_column_wear(doc) == []


def test_a_trustworthy_column_age_still_alerts():
    """The gate must not silence the check permanently -- once the anchor is
    logged AND the document spans the install, wear alerting has to resume."""
    doc = _doc([], column={"known": True, "baseline_change_pct": 30.0,
                           "confidence": "logged",
                           "installed_is_lower_bound": False,
                           "log_covers_install": True,
                           "baseline_at_install_bar": 312.4,
                           "baseline_now_bar": 406.0, "installed": "2026-07-31",
                           "injections_since": 900, "days_since": 33.0})
    assert [a.kind for a in check_column_wear(doc)] == ["column_worn"]


def test_the_short_window_document_never_judges_wear():
    """The quiet one, and the reason the date gate alone is not enough.

    Once Brett's column change was logged as an anchored event, `confidence`
    became "logged" on BOTH documents. But the 30-min tick carries only a
    3-day window, so its `baseline_at_install_bar` is the baseline three days
    ago, not at the install. Real values from 2026-09-02: days_since 32.99
    against observed_days 1.94. Drift measured from that origin describes
    three days, reads far too low, and the threshold would simply never
    fire -- a silent failure, which is the exact shape of the problem this
    whole change exists to end.
    """
    doc = _doc([], column={"known": True, "confidence": "logged",
                           "installed": "2026-07-31T11:04:35",
                           "days_since": 32.99, "observed_days": 1.94,
                           "log_covers_install": False,
                           "counts_are_lower_bounds": True,
                           "runs_since": 103, "injections_since": 104,
                           # Looks harmless precisely because the origin is
                           # wrong; the true figure over 33 days is larger.
                           "baseline_change_pct": 3.4})
    assert check_column_wear(doc) == []


def test_lower_bound_counts_alone_are_enough_to_stay_silent():
    doc = _doc([], column={"known": True, "confidence": "logged",
                           "log_covers_install": True,
                           "counts_are_lower_bounds": True,
                           "baseline_change_pct": 90.0})
    assert check_column_wear(doc) == []


# ── maintenance calendar ─────────────────────────────────────────


def test_a_fresh_clog_is_logged_to_the_maintenance_calendar(monkeypatch):
    calls = []
    monkeypatch.setattr("stan.db.log_event",
                        lambda **kw: calls.append(kw) or "evt123")
    a = Alert(key="clog:t:100spd", kind="clog", instrument="Evosep One",
              headline="column clog — 432 bar, 34% over baseline",
              extra={"why": "new", "n_critical": 6, "method": "100spd",
                     "worst_pct_over_baseline": 37.4})
    assert log_clog_events([a]) == ["evt123"]
    kw = calls[0]
    assert kw["event_type"] == "column_clog"
    assert kw["instrument"] == "timsTOF HT", "must match how `runs` names it"
    # Unmistakably machine-written, carrying the numbers that triggered it.
    assert kw["operator"] == "STAN auto" and kw["created_by"] == "STAN auto"
    assert "432 bar" in kw["notes"] and "threshold 2" in kw["notes"]
    assert "Not a human observation" in kw["notes"]


def test_a_repeated_clog_does_not_log_a_second_event(monkeypatch):
    """One event per episode. A 12 h cool-off re-ping is the same incident."""
    calls = []
    monkeypatch.setattr("stan.db.log_event", lambda **kw: calls.append(kw) or "x")
    for why in ("cooled_off", "escalated", "state_unavailable"):
        log_clog_events([Alert(key="clog:t:100spd", kind="clog",
                               instrument="i", headline="h",
                               extra={"why": why})])
    assert calls == []


def test_only_clogs_reach_the_calendar(monkeypatch):
    """Over-pressure is keyed on a run whose identity can shift mid-episode."""
    calls = []
    monkeypatch.setattr("stan.db.log_event", lambda **kw: calls.append(kw) or "x")
    log_clog_events([
        Alert(key="overpressure:t:2026", kind="overpressure", instrument="i",
              headline="h", extra={"why": "new"}),
        Alert(key="evotip:t:2026", kind="evotip", instrument="i",
              headline="h", extra={"why": "new"}),
    ])
    assert calls == []


def test_a_failing_event_write_does_not_break_alerting(monkeypatch):
    """The calendar is a bonus; Slack is the job."""
    monkeypatch.setattr("stan.db.log_event",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("PG down")))
    assert log_clog_events([Alert(key="k", kind="clog", instrument="i",
                                  headline="h", extra={"why": "new"})]) == []


# ── Bruker / Compass ─────────────────────────────────────────────


def _bruker(failures):
    return {"instrument": {"name": "HPZ6"}, "backup_date": "2026-08-31",
            "failures_recent": list(failures)}


def _failure(start, category, fname="20260828_100spd_COH-48_S5-H6_1_24180.d",
             well="S5-H6", message="Critical Error in ICF System"):
    return {"start_date": start, "fname": fname, "well": well,
            "category": category, "message": message}


def test_compass_failures_reach_slack():
    doc = _bruker([_failure("2026-09-01 02:07", "LC pressure / clog"),
                   _failure("2026-09-01 03:00", "Evotip missing / not picked up")])
    alerts = check_bruker(doc, now=NOW)
    assert {a.kind for a in alerts} == {"overpressure", "evotip"}
    assert all(a.instrument == "timsTOF HPZ6" for a in alerts)
    over = next(a for a in alerts if a.kind == "overpressure")
    assert over.severity == "critical"


def test_the_catch_all_category_is_ignored():
    """'Other failure' is noise — the same exclusion bruker_alert.py makes."""
    doc = _bruker([_failure("2026-09-01 02:07", "Other failure")])
    assert check_bruker(doc, now=NOW) == []


def test_stale_compass_failures_are_ignored():
    doc = _bruker([_failure("2026-08-20 02:07", "LC pressure / clog")])
    assert check_bruker(doc, now=NOW) == []


def test_a_failure_older_than_a_backup_cycle_still_alerts():
    """The Compass document lags reality by up to ~29 h and that is structural.

    The backup is written at 18:00 and the extractor publishes at 20:00 the
    next evening, so a 14:38 failure is not visible to anything until well
    past a 24 h window. Dropping it would be the same silent miss this whole
    change exists to end.
    """
    lagged = (NOW - timedelta(hours=LOOKBACK_HOURS + 5)).strftime("%Y-%m-%d %H:%M")
    doc = _bruker([_failure(lagged, "LC pressure / clog")])
    assert len(check_bruker(doc, now=NOW)) == 1


def test_both_sources_seeing_one_failure_share_a_key():
    """The Evosep log and Compass log the same aborted injection a minute
    apart. They must collapse to one message, not two."""
    evosep = check_evosep(
        _doc([_flag("2026-09-01T02:09:45", severity="elevated",
                    kinds=("tip", "aborted"))]), now=NOW)
    compass = check_bruker(
        _bruker([_failure("2026-09-01 02:09", "Evotip missing / not picked up")]),
        now=NOW)
    assert [a.key for a in evosep if a.kind == "evotip"] == \
           [a.key for a in compass]


def test_two_distinct_injections_do_not_share_a_key():
    """Runs are ~14 min apart; a 10-minute bucket must always separate them."""
    a = check_bruker(_bruker([_failure("2026-09-01 02:09", "Connection lost")]),
                     now=NOW)
    b = check_bruker(_bruker([_failure("2026-09-01 02:23", "Connection lost")]),
                     now=NOW)
    assert a[0].key != b[0].key
