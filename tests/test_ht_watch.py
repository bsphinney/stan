"""HT plate alerting: what fires, what stays quiet, and who it reaches.

An alert that fires on ordinary batches gets filtered to a folder, and then
the real one is missed too. So the tests here are as much about silence as
about firing.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from stan.reports.ht_watch import (
    CONSECUTIVE_FAILURES,
    STALL_HOURS,
    alert_key,
    check_plate,
    render_email,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)


def _analysis(points=None, plates=None, trend=None, query="0793"):
    return {
        "query": query,
        "queue": {"points": points or [], "standards_trend_precursors": trend},
        "plate": {"plates": plates or []},
    }


def _plate(n_wells=96, complete=True, missing=None):
    return {"plate": "S6", "n_wells": n_wells, "n_expected": 96,
            "n_missing": 96 - n_wells, "is_complete": complete,
            "missing_wells": missing or []}


def test_complete_plate_is_silent():
    a = _analysis(plates=[_plate()],
                  points=[{"run_date": (NOW - timedelta(hours=9)).isoformat()}])
    assert check_plate(a, now=NOW) == []


def test_stalled_partial_plate_alerts_with_what_is_left():
    """Real case: plate S5 stopped at 39 of 96 wells."""
    a = _analysis(
        plates=[_plate(n_wells=39, complete=False, missing=["A1", "C1"])],
        points=[{"run_date": (NOW - timedelta(hours=5)).isoformat()}])
    alerts = check_plate(a, now=NOW)
    assert len(alerts) == 1
    al = alerts[0]
    assert al["kind"] == "stalled_plate"
    assert al["remaining"] == 57
    assert "57" in al["summary"], "say how many wells still need running"


def test_partial_plate_still_running_is_silent():
    """A queue mid-flight is not a fault; only silence is."""
    a = _analysis(
        plates=[_plate(n_wells=39, complete=False)],
        points=[{"run_date": (NOW - timedelta(hours=STALL_HOURS - 1)).isoformat()}])
    assert check_plate(a, now=NOW) == []


def test_barely_started_plate_is_not_called_stalled():
    a = _analysis(
        plates=[_plate(n_wells=2, complete=False)],
        points=[{"run_date": (NOW - timedelta(hours=9)).isoformat()}])
    assert check_plate(a, now=NOW) == []


def test_consecutive_failures_alert():
    pts = [{"kind": "sample", "is_outlier": i in (3, 4, 5), "run_name": f"r{i}"}
           for i in range(9)]
    alerts = check_plate(_analysis(points=pts), now=NOW)
    assert any(a["kind"] == "consecutive_failures" for a in alerts)
    a = next(a for a in alerts if a["kind"] == "consecutive_failures")
    assert a["count"] == CONSECUTIVE_FAILURES


def test_scattered_failures_do_not_alert():
    """Isolated poor samples are normal; a run of them is not."""
    pts = [{"kind": "sample", "is_outlier": i in (1, 5, 8), "run_name": f"r{i}"}
           for i in range(9)]
    assert not any(a["kind"] == "consecutive_failures"
                   for a in check_plate(_analysis(points=pts), now=NOW))


def test_mild_standards_decline_is_silent():
    """Real submission 0793 declined 7% over the queue — ordinary wear."""
    a = _analysis(trend={"pct_change_over_queue": -7.0, "n": 8})
    assert check_plate(a, now=NOW) == []


def test_steep_standards_decline_alerts():
    a = _analysis(trend={"pct_change_over_queue": -31.0, "n": 8})
    alerts = check_plate(a, now=NOW)
    assert any(x["kind"] == "standards_declining" for x in alerts)
    assert "31" in alerts[0]["summary"]


def test_alert_key_ignores_the_changing_numbers():
    """Idle 4 h and idle 5 h is the same news; re-sending trains people to
    ignore the alert."""
    base = {"kind": "stalled_plate", "submission": "0793", "plate": "S6"}
    assert alert_key({**base, "idle_hours": 4}) == alert_key({**base, "idle_hours": 5})


def test_alert_key_separates_submissions_and_conditions():
    k = alert_key({"kind": "stalled_plate", "submission": "0793", "plate": "S6"})
    assert k != alert_key({"kind": "stalled_plate", "submission": "0794", "plate": "S6"})
    assert k != alert_key({"kind": "consecutive_failures", "submission": "0793"})


def test_email_names_the_problem_in_the_subject():
    subject, html = render_email([
        {"kind": "stalled_plate", "submission": "0793", "summary": "Plate stopped"}])
    assert "0793" in subject and "stopped" in subject.lower()
    assert "Plate stopped" in html
