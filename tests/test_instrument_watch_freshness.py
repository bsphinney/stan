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
