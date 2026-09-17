"""A feed that stops arriving must alert, even though nothing failed.

The Evosep mirror stopped on 2026-09-03 and the Bruker backups on
2026-09-01. Neither copy ever errored -- they simply never ran again. Every
Hive-side consumer kept succeeding against the frozen inputs, and the
nightly Bruker extract re-published the same 2026-08-31 backup for eight
days with a fresh ``updated_at`` each time. So every freshness signal in the
system reported healthy while a replaced column was being described by
week-old numbers.

These tests pin the one property that makes that detectable: the check reads
how old the DATA is, never when the job last ran.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from stan.reports.instrument_watch import (
    BRUKER_STALE_DAYS,
    EVOSEP_STALE_DAYS,
    check_feed_freshness,
)

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def _evosep(last_run: str | None) -> dict:
    return {"summary": {"last_run": last_run}, "instrument_host": "TIMS-10878"}


def _bruker(backup_date: str | None) -> dict:
    return {"backup_date": backup_date, "instrument": {"name": "TIMS-10878"}}


class TestSilentWhenCurrent:
    def test_fresh_feeds_say_nothing(self):
        fresh = (NOW - timedelta(hours=6)).isoformat()
        alerts = check_feed_freshness(_evosep(fresh), _bruker(fresh[:10]), now=NOW)
        assert alerts == []

    def test_just_inside_the_threshold_is_silent(self):
        e = (NOW - timedelta(days=EVOSEP_STALE_DAYS - 0.1)).isoformat()
        b = (NOW - timedelta(days=BRUKER_STALE_DAYS - 0.1)).isoformat()
        assert check_feed_freshness(_evosep(e), _bruker(b), now=NOW) == []

    def test_a_long_weekend_does_not_cry_wolf_on_evosep(self):
        """Evosep logs only appear when the Evosep runs. Friday evening to
        Tuesday morning is normal, and an alert there would train everyone
        to ignore the channel."""
        friday_evening = (NOW - timedelta(days=2, hours=18)).isoformat()
        alerts = check_feed_freshness(_evosep(friday_evening), None, now=NOW)
        assert alerts == []


class TestAlertsWhenAbsent:
    def test_the_real_incident_alerts(self):
        """The actual 2026-09-08 state: Evosep last run 09-02, Bruker
        backup 08-31."""
        alerts = check_feed_freshness(
            _evosep("2026-09-02T13:15:10"), _bruker("2026-08-31"), now=NOW)
        kinds = sorted(a.kind for a in alerts)
        assert kinds == ["bruker_stale", "evosep_stale"]

    def test_headline_names_the_gap_in_days(self):
        alerts = check_feed_freshness(None, _bruker("2026-08-31"), now=NOW)
        assert "8 days ago" in alerts[0].headline

    def test_escalates_to_critical_when_badly_overdue(self):
        alerts = check_feed_freshness(None, _bruker("2026-08-31"), now=NOW)
        # 8 days against a 2-day threshold is 4x over.
        assert alerts[0].severity == "critical"

    def test_still_only_a_warning_just_over_the_line(self):
        b = (NOW - timedelta(days=BRUKER_STALE_DAYS + 0.5)).isoformat()
        alerts = check_feed_freshness(None, _bruker(b), now=NOW)
        assert alerts[0].severity == "warning"

    def test_detail_says_where_to_go(self):
        alerts = check_feed_freshness(None, _bruker("2026-08-31"), now=NOW)
        joined = " ".join(alerts[0].detail)
        assert "copy_bruker_backup.bat" in joined


class TestDedupBehaviour:
    def test_signature_is_the_day_count_so_it_escalates_not_repeats(self):
        """cool_off_hours alone would go quiet after the first message. The
        signature changing daily makes it speak once a day while broken."""
        a1 = check_feed_freshness(None, _bruker("2026-08-31"), now=NOW)[0]
        a2 = check_feed_freshness(None, _bruker("2026-08-31"),
                                  now=NOW + timedelta(days=1))[0]
        assert a1.key == a2.key            # same condition
        assert a1.signature != a2.signature  # but re-alerts as it worsens
        assert a1.cool_off_hours == 24.0

    def test_same_day_recheck_keeps_one_signature(self):
        a1 = check_feed_freshness(None, _bruker("2026-08-31"), now=NOW)[0]
        a2 = check_feed_freshness(None, _bruker("2026-08-31"),
                                  now=NOW + timedelta(hours=3))[0]
        assert a1.signature == a2.signature


class TestNeverReadsTheJobTimestamp:
    def test_a_freshly_republished_stale_document_still_alerts(self):
        """The trap that hid this for eight days: the extract job re-ran
        nightly and stamped generated_at = now while backup_date stayed at
        2026-08-31. Reading generated_at would report everything healthy."""
        doc = _bruker("2026-08-31")
        doc["generated_at"] = NOW.isoformat()   # job ran minutes ago
        alerts = check_feed_freshness(None, doc, now=NOW)
        assert len(alerts) == 1
        assert alerts[0].kind == "bruker_stale"


class TestDegradesQuietly:
    def test_missing_documents_do_not_raise(self):
        assert check_feed_freshness(None, None, now=NOW) == []

    def test_unparseable_dates_do_not_raise(self):
        assert check_feed_freshness(_evosep("not-a-date"),
                                    _bruker("garbage"), now=NOW) == []

    def test_empty_documents_do_not_raise(self):
        assert check_feed_freshness({}, {}, now=NOW) == []


class TestPublishFreshnessIsADifferentQuestion:
    """Data arriving and the panel updating are two separate failures.

    On 2026-09-17 only the second was true: Evosep logs landed through the
    current day while the panel served a document last written eight days
    earlier, because cron_evosep.sh had silently lost its execute bit. The
    data-age check could not see it -- cron_evosep.sh hands instrument-watch
    a freshly generated extract via --evosep-json, so last_run read as today.
    """

    def _fake_pg(self, monkeypatch, updated):
        """Stand in for PG so the check can be exercised off-cluster."""
        import stan.db_pg as db_pg
        from stan.reports import instrument_watch as iw

        class _Cur:
            def execute(self, sql):
                self._t = "evosep" if "evosep" in sql else "bruker"
            def fetchone(self):
                return (updated.get(self._t),)
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class _Con:
            def cursor(self): return _Cur()
            def rollback(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(db_pg, "use_pg", lambda: True)
        monkeypatch.setattr(db_pg, "_connect", lambda: _Con())
        return iw

    def test_stalled_publish_alerts_even_though_data_is_current(self, monkeypatch):
        iw = self._fake_pg(monkeypatch, {
            "evosep": NOW - timedelta(days=8),   # the real incident
            "bruker": NOW - timedelta(hours=6),
        })
        alerts = iw.check_publish_freshness(now=NOW)
        kinds = [a.kind for a in alerts]
        assert kinds == ["evosep_publish_stale"]
        assert "8 days" in alerts[0].headline

    def test_silent_when_both_are_being_republished(self, monkeypatch):
        iw = self._fake_pg(monkeypatch, {
            "evosep": NOW - timedelta(hours=3),
            "bruker": NOW - timedelta(hours=10),
        })
        assert iw.check_publish_freshness(now=NOW) == []

    def test_escalates_when_badly_overdue(self, monkeypatch):
        iw = self._fake_pg(monkeypatch, {
            "evosep": NOW - timedelta(days=8), "bruker": NOW - timedelta(days=9)})
        alerts = iw.check_publish_freshness(now=NOW)
        assert all(a.severity == "critical" for a in alerts)

    def test_no_op_without_pg(self, monkeypatch):
        """A single-lab SQLite install publishes nothing and must stay quiet."""
        import stan.db_pg as db_pg
        from stan.reports import instrument_watch as iw
        monkeypatch.setattr(db_pg, "use_pg", lambda: False)
        assert iw.check_publish_freshness(now=NOW) == []

    def test_never_raises_when_pg_is_unreachable(self, monkeypatch):
        import stan.db_pg as db_pg
        from stan.reports import instrument_watch as iw
        monkeypatch.setattr(db_pg, "use_pg", lambda: True)
        def _boom(): raise RuntimeError("pg down")
        monkeypatch.setattr(db_pg, "_connect", _boom)
        assert iw.check_publish_freshness(now=NOW) == []


class TestCronHeartbeat:
    """The generic form of the 2026-09-09 failure.

    cron_evosep.sh lost its execute bit, cron answered "Permission denied"
    into nothing, and nobody found out for eight days. No per-feed check
    catches that: the symptom is a job producing NO output at all. A job
    that has stopped writing its log has stopped, whatever the cause.
    """

    def _logs(self, tmp_path, ages_hours):
        import os, time
        from stan.reports.instrument_watch import CRON_LOGS
        for name, age in ages_hours.items():
            # Build the filename from the real glob, so the test cannot pass
            # against a pattern that would miss the actual log on Hive.
            f = tmp_path / CRON_LOGS[name][0].replace("*", "260917")
            f.write_text("tick\n")
            t = time.time() - age * 3600
            os.utime(f, (t, t))
        return str(tmp_path)

    def test_silent_job_alerts(self, tmp_path):
        from stan.reports.instrument_watch import check_cron_heartbeat
        # evosep is a */30 job; 8 days of nothing is the real incident.
        d = self._logs(tmp_path, {"evosep": 24 * 8, "ht_watch": 0.2})
        # Filter to jobs that went QUIET; the fixture deliberately omits the
        # other logs, which now report separately as never-seen.
        alerts = [a for a in check_cron_heartbeat(log_dir=d)
                  if not a.extra.get("missing_log")]
        assert [a.extra["cron"] for a in alerts] == ["evosep"]
        assert "192 h" in alerts[0].headline or "h" in alerts[0].headline

    def test_healthy_jobs_are_silent(self, tmp_path):
        from stan.reports.instrument_watch import check_cron_heartbeat
        d = self._logs(tmp_path, {"evosep": 0.4, "ht_watch": 0.3,
                                  "bruker_maint": 12, "community_sync": 4})
        assert [a for a in check_cron_heartbeat(log_dir=d)
                if not a.extra.get("missing_log")] == []

    def test_nightly_job_gets_a_nightly_allowance(self, tmp_path):
        """bruker_maint runs once a day; 12 h of quiet is normal and 40 is not."""
        from stan.reports.instrument_watch import check_cron_heartbeat
        def quiet(h):
            return [a for a in check_cron_heartbeat(log_dir=self._logs(tmp_path, {"bruker_maint": h}))
                    if not a.extra.get("missing_log")]
        assert quiet(12) == []
        assert quiet(40) != []

    def test_a_job_never_installed_here_is_not_an_alert(self, tmp_path):
        """No log at all means not deployed on this host, not dead."""
        from stan.reports.instrument_watch import check_cron_heartbeat
        assert check_cron_heartbeat(log_dir=str(tmp_path)) == []

    def test_escalates_when_very_overdue(self, tmp_path):
        from stan.reports.instrument_watch import check_cron_heartbeat
        d = self._logs(tmp_path, {"evosep": 24 * 8})
        quiet = [a for a in check_cron_heartbeat(log_dir=d)
                 if not a.extra.get("missing_log")]
        assert quiet[0].severity == "critical"

    def test_the_watchdog_watches_itself(self, tmp_path):
        """stan_alerts is in the table, so if the watchdog stops and anything
        else still runs a watch, its own silence is reported."""
        from stan.reports.instrument_watch import CRON_LOGS
        assert "stan_alerts" in CRON_LOGS

    def test_missing_log_dir_does_not_raise(self):
        from stan.reports.instrument_watch import check_cron_heartbeat
        assert check_cron_heartbeat(log_dir="/no/such/place") == []


class TestHeartbeatCoversEveryScheduledJob:
    def test_a_dead_job_is_not_masked_by_a_sibling_log(self, tmp_path):
        """cron_evosep_*.log also matched cron_evosep_watch_*.log, so a dead
        cron_evosep could hide behind its sibling. The glob is anchored on
        the date to keep them apart."""
        import os, time
        from stan.reports.instrument_watch import check_cron_heartbeat
        dead = tmp_path / "cron_evosep_20260909.log"; dead.write_text("x")
        t = time.time() - 24 * 8 * 3600; os.utime(dead, (t, t))
        sibling = tmp_path / "cron_evosep_watch_20260917.log"; sibling.write_text("x")
        alerts = [a for a in check_cron_heartbeat(log_dir=str(tmp_path))
                  if not a.extra.get("missing_log")]
        assert [a.extra["cron"] for a in alerts] == ["evosep"], \
            "the sibling log masked a dead job"

    def test_the_oddly_named_logs_are_covered(self):
        """count_acq_submit_* and cron_flinders_* do not follow the naming of
        the others, and a pattern derived from the job name missed both."""
        from stan.reports.instrument_watch import CRON_LOGS
        assert CRON_LOGS["count_acquisitions"][0].startswith("count_acq_submit")
        assert CRON_LOGS["flinders_dispatch"][0].startswith("cron_flinders_")


class TestMissingLogIsReportedNotSkipped:
    """A log the check cannot find used to be skipped in silence. That is
    how it monitored five of eight jobs and reported everything healthy."""

    def test_missing_log_alerts_once_others_are_present(self, tmp_path):
        import os, time
        from stan.reports.instrument_watch import check_cron_heartbeat, CRON_LOGS
        # One healthy job present, so this is clearly the Hive log dir...
        f = tmp_path / CRON_LOGS["evosep"][0].replace("*", "260917")
        f.write_text("tick"); t = time.time(); os.utime(f, (t, t))
        alerts = check_cron_heartbeat(log_dir=str(tmp_path))
        kinds = {a.key for a in alerts}
        assert "cron_nolog:ioncloud" in kinds
        assert "cron_nolog:bruker_maint" in kinds

    def test_empty_dir_is_not_hive_and_stays_quiet(self, tmp_path):
        """A single-lab install runs none of these; it must not be spammed."""
        from stan.reports.instrument_watch import check_cron_heartbeat
        assert check_cron_heartbeat(log_dir=str(tmp_path)) == []

    def test_the_two_undated_logs_are_named_exactly(self):
        from stan.reports.instrument_watch import CRON_LOGS
        assert CRON_LOGS["count_acquisitions"][0] == "count_acq_submit.log"
        assert CRON_LOGS["ioncloud"][0] == "cron_ioncloud.log"
