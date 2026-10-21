"""The watchdog's fourth question: is a job FAILING, rather than silent?

check_cron_heartbeat measures silence. From 2026-09-23 to 2026-10-21 shard 1
of the hourly stan-ioncloud array failed every hour on one unreadable sidecar
while cron_ioncloud.log was written every hour, so the heartbeat counted it
healthy for four weeks. It was found by reading the job list, not by any
alert. check_job_failures asks the scheduler how each run actually ended.

The fixtures are sacct's own parsable output, the exact columns
_SACCT_FIELDS asks for.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import stan.reports.instrument_watch as iw
from stan.notify import should_send
from stan.reports.instrument_watch import check_job_failures

#: 11:30 PDT on the day the ion-cloud failure was found.
NOW = datetime(2026, 10, 21, 18, 30, tzinfo=timezone.utc)
LOCAL = iw.INSTRUMENT_TZ

_raw_ids = iter(range(30_000_000, 40_000_000))


def _row(jobid: str, name: str, state: str, start: datetime | None,
         out: str = "", err: str = "") -> str:
    start_s = start.astimezone(LOCAL).strftime("%Y-%m-%dT%H:%M:%S") if start else "Unknown"
    end_s = ((start + timedelta(seconds=3)).astimezone(LOCAL)
             .strftime("%Y-%m-%dT%H:%M:%S")) if start else "Unknown"
    return "|".join([jobid, str(next(_raw_ids)), name, state, start_s, end_s,
                     out, err])


def _ioncloud(hours_ago: float, run_id: int, bad_task: int | None,
              logs: str = "/logs", state: str = "FAILED") -> list[str]:
    """One hourly run: a 4-task array, with ``bad_task`` failing (or none)."""
    start = NOW - timedelta(hours=hours_ago)
    return [
        _row(f"{run_id}_{t}", "stan-ioncloud",
             state if t == bad_task else "COMPLETED", start,
             out=f"{logs}/ioncloud_%A_%a.out", err=f"{logs}/ioncloud_%A_%a.err")
        for t in range(4)
    ]


def _hourly(fail_hours: int, ok_before: int = 3, logs: str = "/logs") -> str:
    """``ok_before`` good runs, then ``fail_hours`` runs with task 1 failing."""
    rows, rid = [], 24_900_000
    for h in range(ok_before + fail_hours, 0, -1):
        rid += 1
        bad = 1 if h <= fail_hours else None
        rows += _ioncloud(h - 0.8, rid, bad, logs)
    return "\n".join(rows) + "\n"


def _job(alerts, name):
    return [a for a in alerts if a.extra.get("job") == name]


def _fmt(dt: datetime) -> str:
    return dt.astimezone(LOCAL).strftime("%Y-%m-%dT%H:%M:%S")


def _outage(name: str, period: timedelta, tasks: int, days: int,
            fails=lambda k: True) -> list[datetime]:
    """When the watchdog posts, ticking every 20 min through a ``days`` outage.

    Runs start on ``period`` with up to 90 s of queue delay and run 1-8 min;
    in the outage, task 1 of the k-th run fails when ``fails(k)``. sacct's
    window rule is applied: a run that overlaps the window's start is
    reported, one that ended before it is not, and one still running has no
    End.
    """
    import random

    rnd = random.Random(7)
    t0 = NOW - timedelta(days=days)
    runs, t, rid, k = [], t0 - timedelta(hours=3), 1000, 0
    while t < NOW:
        rid += 1
        start = t + timedelta(seconds=rnd.uniform(0, 90))
        end = start + timedelta(minutes=rnd.uniform(1, 8))
        failed = start >= t0 and fails(k)
        k += start >= t0
        runs.append((rid, start, end, "FAILED" if failed else "COMPLETED"))
        t += period
    sent, store, tick = [], None, t0
    while tick <= NOW:
        w0 = tick - timedelta(hours=iw.JOB_LOOKBACK_HOURS)
        lines = []
        for rid, start, end, state in runs:
            if start > tick or end < w0:
                continue
            for task in range(tasks):
                jobid = f"{rid}_{task}" if tasks > 1 else str(rid)
                st = ("RUNNING" if end > tick
                      else state if task == min(1, tasks - 1) else "COMPLETED")
                lines.append("|".join([jobid, f"{rid}{task}", name, st, _fmt(start),
                                       "Unknown" if end > tick else _fmt(end), "", ""]))
        for a in _job(check_job_failures(now=tick, sacct_text="\n".join(lines)), name):
            if should_send(a, store, now=tick)[0]:
                store = {"signature": a.signature, "last_sent": tick.isoformat()}
                sent.append(tick)
        tick += timedelta(minutes=20)
    return sent


class TestTheIonCloudIncident:

    def test_a_day_of_hourly_failures_is_critical_and_names_the_cause(self, tmp_path):
        text = _hourly(fail_hours=30, logs=str(tmp_path))
        newest = max(int(line.split("|")[0].split("_")[0])
                     for line in text.splitlines())
        (tmp_path / f"ioncloud_{newest}_1.out").write_text(
            "[start] 295 runs queued, 649 already stored, shard 1/4\n"
            "[err] 09212026_HE50_60-spd-dia_S1-A2_1_24515.d: attempt to write "
            "a readonly database\n"
            "[end] done=0 skipped=294 errors=1\n"
            "############### Job 1 summary ###############\n"
            "State               : FAILED\n"
        )
        (tmp_path / f"ioncloud_{newest}_1.err").write_text(
            'WARNING:  database "stan" has a collation version mismatch\n')

        [a] = _job(check_job_failures(now=NOW, sacct_text=text), "stan-ioncloud")

        assert a.severity == "critical"
        assert "30 runs in a row" in a.headline
        assert "task 1" in a.headline
        body = "\n".join(a.detail)
        assert "attempt to write a readonly database" in body
        assert "collation" not in body
        assert f"ioncloud_{newest}_1.out" in body     # %A_%a expanded

    def test_two_failed_runs_are_not_yet_news(self):
        assert _job(check_job_failures(now=NOW, sacct_text=_hourly(2)),
                    "stan-ioncloud") == []

    def test_the_third_is_a_warning(self):
        [a] = _job(check_job_failures(now=NOW, sacct_text=_hourly(3)),
                   "stan-ioncloud")
        assert a.severity == "warning"

    def test_one_good_run_resets_the_streak(self):
        rows = (_ioncloud(4.2, 1, 1) + _ioncloud(3.2, 2, 1)
                + _ioncloud(2.2, 3, None) + _ioncloud(1.2, 4, 1))
        assert _job(check_job_failures(now=NOW, sacct_text="\n".join(rows)),
                    "stan-ioncloud") == []

    def test_a_run_still_going_is_waited_for(self):
        """It may be the one that ends the streak. The backup after the
        2026-10-18 failure took 14 minutes; a repeat meanwhile was wrong."""
        streak = (_ioncloud(4.2, 1, None) + _ioncloud(3.2, 2, 1)
                  + _ioncloud(2.2, 3, 1) + _ioncloud(1.2, 4, 1))
        going = [_row("5_0", "stan-ioncloud", "RUNNING", NOW),
                 _row("5_[1-3%4]", "stan-ioncloud", "PENDING", None)]
        assert _job(check_job_failures(now=NOW, sacct_text="\n".join(streak + going)),
                    "stan-ioncloud") == []
        assert len(_job(check_job_failures(now=NOW, sacct_text="\n".join(streak)),
                        "stan-ioncloud")) == 1

    def test_a_queued_run_older_than_the_streak_does_not_block_it(self):
        rows = ([_row("0_[0-3%4]", "stan-ioncloud", "PENDING", None)]
                + _ioncloud(4.2, 1, None) + _ioncloud(3.2, 2, 1)
                + _ioncloud(2.2, 3, 1) + _ioncloud(1.2, 4, 1))
        assert len(_job(check_job_failures(now=NOW, sacct_text="\n".join(rows)),
                        "stan-ioncloud")) == 1

    def test_a_cancelled_run_says_nothing_either_way(self):
        cancelled = [_row(f"3_{t}", "stan-ioncloud", "CANCELLED by 349971",
                          NOW - timedelta(hours=3.2)) for t in range(4)]
        rows = (_ioncloud(5.2, 1, None) + _ioncloud(4.2, 2, 1) + cancelled
                + _ioncloud(2.2, 4, 1) + _ioncloud(1.2, 5, 1))
        [a] = _job(check_job_failures(now=NOW, sacct_text="\n".join(rows)),
                   "stan-ioncloud")
        assert "3 runs in a row" in a.headline

    def test_timeout_and_oom_count_as_failures(self):
        rows = (_ioncloud(4.2, 1, None) + _ioncloud(3.2, 2, 1, state="TIMEOUT")
                + _ioncloud(2.2, 3, 2, state="OUT_OF_MEMORY")
                + _ioncloud(1.2, 4, 1))
        assert len(_job(check_job_failures(now=NOW, sacct_text="\n".join(rows)),
                        "stan-ioncloud")) == 1


class TestEachJobHasItsOwnBar:

    def test_one_failed_backup_is_critical_and_says_why(self, tmp_path):
        log = tmp_path / "db_backup_%j.out"
        rows = [_row("100", "stan_db_backup", "COMPLETED", NOW - timedelta(hours=39)),
                _row("200", "stan_db_backup", "FAILED", NOW - timedelta(hours=15),
                     out=str(log))]
        (tmp_path / f"db_backup_{rows[1].split('|')[1]}.out").write_text(
            "===== stan db backup 2026-10-18 03:17:13 on hive-as-11-4-73 =====\n"
            "  destination free: 70338 GB\n"
            "ABORT: could not obtain a PG Farm token\n")
        [a] = _job(check_job_failures(now=NOW, sacct_text="\n".join(rows)),
                   "stan_db_backup")
        assert a.severity == "critical"
        assert a.headline == "stan_db_backup failed its last run"
        assert "could not obtain a PG Farm token" in "\n".join(a.detail)

    def test_the_acquisition_counter_needs_four(self):
        def runs(n_failed):
            rows = [_row("10", "stan_count_acq", "COMPLETED",
                         NOW - timedelta(hours=3))]
            rows += [_row(str(20 + i), "stan_count_acq", "FAILED",
                          NOW - timedelta(minutes=15 * (n_failed - i)))
                     for i in range(n_failed)]
            return "\n".join(rows)

        assert _job(check_job_failures(now=NOW, sacct_text=runs(3)),
                    "stan_count_acq") == []
        assert len(_job(check_job_failures(now=NOW, sacct_text=runs(4)),
                        "stan_count_acq")) == 1

    def test_other_projects_never_post_to_stans_channel(self):
        rows = [_row(str(50 + i), name, "FAILED", NOW - timedelta(hours=i + 1))
                for i, name in enumerate(["diann_search"] * 5 + ["sky_import"] * 5)]
        assert check_job_failures(now=NOW, sacct_text="\n".join(rows)) == []


class TestPerFileQcJobs:
    """One job per raw file, so "in a row" does not fit. Brett's bar
    (2026-10-21): three files failing within a day is worth a message."""

    def _mon(self, jobid, run, state, hours_ago, out=""):
        return _row(jobid, f"stan-mon-{run}", state,
                    NOW - timedelta(hours=hours_ago), out=out)

    def test_three_files_in_a_day_warn(self, tmp_path):
        log = tmp_path / "mon_%j.out"
        rows = [self._mon("1", "a.d", "FAILED", 2, out=str(log)),
                self._mon("2", "b.d", "FAILED", 5, out=str(log)),
                self._mon("3", "c.d", "OUT_OF_MEMORY", 20)]
        # A crash: logger.exception prints the ERROR line and a traceback
        # first, and the JSON status line names the actual cause last.
        (tmp_path / f"mon_{rows[0].split('|')[1]}.out").write_text(
            "[17:00:27] ERROR    step_monitor failed for a.d   hive_steps.py:514\n"
            "Traceback (most recent call last):\n"
            "KeyError: 'Frames'\n"
            '{"status": "error", "run_id": null, "mode": "monitor", '
            '"error": "KeyError: \'Frames\'", "verdict": null}\n'
            "############### Job 1 summary ###############\n")
        rows[2] = self._mon("3", "c.d", "OUT_OF_MEMORY", 20, out=str(log))
        (tmp_path / f"mon_{rows[2].split('|')[1]}.out").write_text(
            "slurmstepd: error: Detected 1 oom_kill event in StepId=3.batch\n")
        # The real shape: the cause is in the closing JSON status line.
        (tmp_path / f"mon_{rows[1].split('|')[1]}.out").write_text(
            "[05:46:22] WARNING  PG Farm connect attempt 1/6 failed\n"
            '{"status": "error", "run_id": null, "mode": "monitor", '
            '"error": "rawmeat extraction returned empty", "verdict": null}\n')
        [a] = check_job_failures(now=NOW, sacct_text="\n".join(rows))
        assert a.severity == "warning"
        assert a.headline.startswith("3 ")
        body = "\n".join(a.detail)
        assert "stan-mon-a.d" in body and "KeyError: 'Frames'" in body
        assert "step_monitor failed" not in body
        assert "rawmeat extraction returned empty" in body
        assert "oom_kill" in body
        assert "PG Farm connect" not in body

    def test_two_are_not_yet_news(self):
        rows = [self._mon("1", "a.d", "FAILED", 2),
                self._mon("2", "b.d", "FAILED", 5)]
        assert check_job_failures(now=NOW, sacct_text="\n".join(rows)) == []

    def test_a_retry_that_worked_is_not_counted(self):
        rows = [self._mon("1", "a.d", "FAILED", 6),
                self._mon("9", "a.d", "COMPLETED", 1),
                self._mon("2", "b.d", "FAILED", 5),
                self._mon("3", "c.d", "FAILED", 4)]
        assert check_job_failures(now=NOW, sacct_text="\n".join(rows)) == []

    def test_failures_older_than_a_day_are_not_counted(self):
        rows = [self._mon("1", "a.d", "FAILED", 30),
                self._mon("2", "b.d", "FAILED", 5),
                self._mon("3", "c.d", "FAILED", 4)]
        assert check_job_failures(now=NOW, sacct_text="\n".join(rows)) == []

    def test_search_jobs_count_too(self):
        rows = [_row("1", "stan-Ex150926_HeLa_30m_2", "FAILED", NOW - timedelta(hours=1)),
                _row("2", "stan-FL161026_HeL50_35m_1", "FAILED", NOW - timedelta(hours=2)),
                self._mon("3", "c.d", "FAILED", 3)]
        assert len(check_job_failures(now=NOW, sacct_text="\n".join(rows))) == 1

    def test_a_bulk_array_is_not_a_file(self):
        """stan-peg-thermo and stan-4dffcloud are arrays somebody started by
        hand. Counting each task's name once would count one of them, chosen
        by whichever task sacct listed first."""
        rows = [_row(f"77_{t}", "stan-peg-thermo", "FAILED", NOW - timedelta(hours=1))
                for t in range(8)]
        rows += [_row(f"78_{t}", "stan-4dffcloud", "FAILED", NOW - timedelta(hours=2))
                 for t in range(3)]
        rows += [self._mon("3", "c.d", "FAILED", 3)]
        assert check_job_failures(now=NOW, sacct_text="\n".join(rows)) == []

    def test_blanks_and_washes_are_not_counted(self):
        # Real names from sacct: a blank has nothing for the monitor to
        # measure, and "rawmeat extraction returned empty" is how that ends.
        # They were 22 of the 24 failed files in 30 days (Brett, 2026-10-21).
        rows = [self._mon("1", "blank_S1-H1_1_25184", "FAILED", 1),
                self._mon("2", "8oct-newZDF_blank_S1-H2_1_25155", "FAILED", 2),
                _row("3", "stan-blankDia-b4-HELA_S1-H4_1_24705", "FAILED",
                     NOW - timedelta(hours=3)),
                self._mon("4", "Blank-100spd-method_S1-A1_1_25001", "FAILED", 4),
                self._mon("5", "wash_S1-A3_1_25002", "FAILED", 5),
                self._mon("6", "FL230926_Yanxin_90m_3", "FAILED", 6),
                self._mon("7", "HeLa_S1-A4_1_25003", "FAILED", 7)]
        assert check_job_failures(now=NOW, sacct_text="\n".join(rows)) == []
        rows.append(self._mon("8", "HeLa_S1-A5_1_25004", "FAILED", 8))
        [a] = check_job_failures(now=NOW, sacct_text="\n".join(rows))
        assert a.headline.startswith("3 ")
        assert "blank" not in "\n".join(a.detail[:-1]).lower()


class TestSaidOnceADay:

    def _sent(self, alert, at):
        return {"signature": alert.signature, "last_sent": at.isoformat()}

    def test_the_next_tick_is_quiet(self):
        [a] = _job(check_job_failures(now=NOW, sacct_text=_hourly(5)), "stan-ioncloud")
        later = NOW + timedelta(minutes=20)
        [b] = _job(check_job_failures(now=later, sacct_text=_hourly(5)), "stan-ioncloud")
        assert should_send(b, self._sent(a, NOW), now=later) == (False, "suppressed")

    def test_a_streak_too_long_to_see_the_start_of_stays_quiet(self):
        """Its oldest visible run moves every tick. sacct reports a run that
        overlaps the window's start, so the oldest one is 48.2 h old on one
        tick and 47.2 h the next: a day count in the signature flipped there,
        and every flip posted."""
        a = _job(check_job_failures(now=NOW, sacct_text=_hourly(49, ok_before=0)),
                 "stan-ioncloud")[0]
        shifted = _hourly(49, ok_before=0).splitlines()[4:]   # oldest run gone
        b = _job(check_job_failures(now=NOW + timedelta(minutes=20),
                                    sacct_text="\n".join(shifted)),
                 "stan-ioncloud")[0]
        assert b.signature == a.signature

    @pytest.mark.parametrize("name, period_min, tasks", [
        ("stan-ioncloud", 60, 4),
        ("stan_count_acq", 15, 1),
    ])
    def test_a_five_day_outage_posts_about_once_a_day(self, name, period_min, tasks):
        """Every 20-min tick of a 5-day outage, with jittered starts and
        runtimes and sacct's window rule. The day-count signature posted 25
        times here for stan_count_acq, whose runs land on the tick."""
        sent = _outage(name, timedelta(minutes=period_min), tasks, days=5)
        hours = [(t - sent[0]).total_seconds() / 3600 for t in sent]
        assert 5 <= len(sent) <= 6, hours
        gaps = [b - a for a, b in zip(hours, hours[1:])]
        assert min(gaps) >= 20, hours

    @pytest.mark.parametrize("name, period_min, tasks, need", [
        ("stan-ioncloud", 60, 4, 3),
        ("stan_count_acq", 15, 1, 4),
    ])
    def test_a_job_that_works_now_and_then_is_said_once_a_day(
            self, name, period_min, tasks, need):
        """Fail ``need`` runs, work once, repeat. Each streak's opening run
        was in the signature and nothing clears the store, so every streak
        was news: every 4 h for ion-cloud, every 75 min for the counter."""
        sent = _outage(name, timedelta(minutes=period_min), tasks, days=2,
                       fails=lambda k: k % (need + 1) != need)
        hours = [(t - sent[0]).total_seconds() / 3600 for t in sent]
        assert 2 <= len(sent) <= 3, hours
        assert min(b - a for a, b in zip(hours, hours[1:])) >= 20, hours

    def test_per_file_failures_at_most_once_a_day(self):
        rows = [_row(str(i), f"stan-mon-{i}.d", "FAILED", NOW - timedelta(hours=i))
                for i in (1, 2, 3)]
        [a] = check_job_failures(now=NOW, sacct_text="\n".join(rows))
        rows.append(_row("4", "stan-mon-4.d", "FAILED", NOW - timedelta(minutes=5)))
        [b] = check_job_failures(now=NOW + timedelta(hours=1),
                                 sacct_text="\n".join(rows))
        assert should_send(b, self._sent(a, NOW), now=NOW + timedelta(hours=1)) \
            == (False, "suppressed")

    def test_midnight_passing_over_the_same_failures_is_not_news(self):
        """The replay of Sep-Oct posted at 17:20 and again at 00:00 about the
        same three files, because the key was the day it was checked."""
        evening = datetime(2026, 9, 25, 0, 20, tzinfo=timezone.utc)   # 17:20 PDT
        rows = [_row(str(i), f"stan-mon-{i}.d", "FAILED",
                     evening - timedelta(hours=i)) for i in (1, 2, 3)]
        [a] = check_job_failures(now=evening, sacct_text="\n".join(rows))
        after_midnight = evening + timedelta(hours=7)                  # 00:20 PDT
        [b] = check_job_failures(now=after_midnight, sacct_text="\n".join(rows))
        assert b.signature == a.signature

    def test_a_retry_that_works_after_midnight_is_not_news(self):
        """The newest failure's retry succeeding made the newest failure
        yesterday's, and a date signature stepped back and posted again."""
        evening = datetime(2026, 9, 25, 6, 50, tzinfo=timezone.utc)   # 23:50 PDT
        rows = [_row(str(i), f"stan-mon-{i}.d", "FAILED",
                     evening - timedelta(hours=i)) for i in (1, 2, 3)]
        rows.append(_row("4", "stan-mon-4.d", "FAILED", evening + timedelta(minutes=15)))
        t1 = evening + timedelta(minutes=30)                           # 00:20 PDT
        [a] = check_job_failures(now=t1, sacct_text="\n".join(rows))
        rows.append(_row("9", "stan-mon-4.d", "COMPLETED", evening + timedelta(minutes=40)))
        t2 = evening + timedelta(minutes=50)
        [b] = check_job_failures(now=t2, sacct_text="\n".join(rows))
        assert should_send(b, self._sent(a, t1), now=t2) == (False, "suppressed")


class TestNoRunFinishedInTooLong:
    """The streak check waits for an unfinished run's verdict, and a run
    held in the queue never gives one; its cron logs "already queued" every
    tick, so the heartbeat sees a fresh log as well."""

    def _crons(self, tmp_path, *crons, at=NOW, **last):
        """The named crons' logs as on Hive, last written ``at``; ``last``
        gives a log's final line. A job whose cron has no log here is not
        scheduled here."""
        import os

        for cron in crons + tuple(last):
            log = tmp_path / iw.CRON_LOGS[cron][0]
            log.write_text("2026-10-07 11:30:02 incremental Submitted batch job 1\n"
                           + last.get(cron, "") + "\n")
            os.utime(log, (at.timestamp(), at.timestamp()))
        return str(tmp_path)

    def _counter_on_7_oct(self, held=True):
        """Counter run 24684245: submitted 11:30, held until 13:57."""
        t = lambda h, m: datetime(2026, 10, 7, h, m, tzinfo=LOCAL)  # noqa: E731
        rows = [_row("24684009", "stan_count_acq", "COMPLETED", t(11, 0)),
                _row("24684095", "stan_count_acq", "COMPLETED", t(11, 15))]
        if held:
            rows.append("|".join(["24684245", "24684245", "stan_count_acq",
                                  "PENDING", "Unknown", "Unknown", "", ""]))
        return "\n".join(rows)

    def test_a_run_held_in_the_queue_is_reported(self, tmp_path):
        now = datetime(2026, 10, 7, 17, 30, tzinfo=LOCAL)
        root = self._crons(tmp_path, at=now, count_acquisitions=(
            "2026-10-07 17:30:02 skip: counter already queued/running"))
        [a] = check_job_failures(now=now, sacct_text=self._counter_on_7_oct(),
                                 log_dir=root)
        assert a.key == "job_stalled:stan_count_acq"
        assert a.severity == "warning"
        assert a.headline == "stan_count_acq has not finished a run for 6 h"
        body = "\n".join(a.detail)
        assert "Wed 07 Oct 11:15 (completed)" in body
        assert "run 24684245 waiting in the queue" in body
        assert "skip: counter already queued/running" in body

    def test_the_real_2_5_h_wait_was_not_news(self, tmp_path):
        """It cleared itself at 13:57; the limit is the heartbeat's 6 h."""
        now = datetime(2026, 10, 7, 13, 50, tzinfo=LOCAL)
        assert check_job_failures(
            now=now, sacct_text=self._counter_on_7_oct(),
            log_dir=self._crons(tmp_path, "count_acquisitions", at=now)) == []

    def test_one_hung_task_holds_up_the_run(self, tmp_path):
        """2026-10-19: three tasks of 24911582 finished at 16:17 and one
        ran until its 8 h limit; the cron skipped seven times."""
        rows = (_ioncloud(8.1, 1, None)
                + [_row(f"2_{t}", "stan-ioncloud", "COMPLETED", NOW - timedelta(hours=7.1))
                   for t in (0, 1, 3)]
                + [_row("2_2", "stan-ioncloud", "RUNNING", NOW - timedelta(hours=7.1))])
        [a] = check_job_failures(now=NOW, sacct_text="\n".join(rows),
                                 log_dir=self._crons(tmp_path, "ioncloud"))
        assert a.key == "job_stalled:stan-ioncloud"
        assert a.headline == "stan-ioncloud has not finished a run for 8 h"
        assert "run 2 running since" in "\n".join(a.detail)

    def test_failed_runs_and_then_nothing_is_a_stop_not_a_streak(self, tmp_path):
        """"Has failed 3 runs in a row" would hide that nothing has run
        since, until the failures left sacct's window two days later."""
        rows = (_ioncloud(12.2, 1, None) + _ioncloud(11.2, 2, 1)
                + _ioncloud(10.2, 3, 1) + _ioncloud(9.2, 4, 1))
        root = self._crons(tmp_path, ioncloud=(
            "2026-10-21T11:17:02-07:00 ABORT: squeue failed; refusing to "
            "submit blind"))
        [a] = check_job_failures(now=NOW, sacct_text="\n".join(rows), log_dir=root)
        assert a.key == "job_stalled:stan-ioncloud"
        body = "\n".join(a.detail)
        assert "(failed)" in body and "*Last good run:*" in body
        assert "no run queued or running" in body
        assert "ABORT: squeue failed" in body

    def test_a_job_still_finishing_runs_that_fail_is_a_streak(self, tmp_path):
        [a] = _job(check_job_failures(now=NOW, sacct_text=_hourly(10),
                                      log_dir=self._crons(tmp_path, "ioncloud")),
                   "stan-ioncloud")
        assert a.key == "job_failing:stan-ioncloud"

    def test_a_cron_that_submits_nothing_is_reported(self, tmp_path):
        """ABORT every tick: no run at all for 48 h, and a log as fresh as
        ever. Other jobs' rows are there, as they always are on Hive."""
        root = self._crons(tmp_path, ioncloud=(
            "2026-10-21T11:17:02-07:00 ABORT: squeue failed; refusing to "
            "submit blind"))
        others = "\n".join(_row(str(i), "stan_count_acq", "COMPLETED",
                                NOW - timedelta(minutes=15 * i)) for i in range(1, 5))
        [ion] = check_job_failures(now=NOW, sacct_text=others, log_dir=root)
        assert ion.key == "job_stalled:stan-ioncloud"
        assert ion.headline == "stan-ioncloud has not finished a run in 48 h"
        assert ion.severity == "critical"
        body = "\n".join(ion.detail)
        assert "no run queued or running" in body
        assert "ABORT: squeue failed" in body

    def test_an_empty_history_is_sacct_not_every_job_stopping(self, tmp_path):
        """Three critical stalls about healthy jobs would be the wrong
        message when sacct's output is what changed."""
        root = self._crons(tmp_path, "ioncloud", "count_acquisitions", "db_backup")
        for text in ("", "a|format|sacct|changed\n"):
            [a] = check_job_failures(now=NOW, sacct_text=text, log_dir=root)
            assert a.key == "job_failing:unreadable"

    def test_a_cron_gone_quiet_is_left_to_the_heartbeat(self, tmp_path):
        """2026-09-28 18:15 to 09-29 10:00 nothing on login2's crontab ran.
        Its stale logs are the heartbeat's to report; a second alert a day
        about the same cause is one too many."""
        last = datetime(2026, 9, 28, 18, 15, tzinfo=LOCAL)
        now = datetime(2026, 9, 29, 10, 0, tzinfo=LOCAL)
        rows = [_row("24183459", "stan_count_acq", "COMPLETED", last)]
        assert check_job_failures(
            now=now, sacct_text="\n".join(rows),
            log_dir=self._crons(tmp_path, "count_acquisitions", at=last)) == []

    def test_a_missed_backup_is_critical_at_once(self, tmp_path):
        rows = [_row("100", "stan_db_backup", "COMPLETED", NOW - timedelta(hours=31))]
        [a] = check_job_failures(now=NOW, sacct_text="\n".join(rows),
                                 log_dir=self._crons(tmp_path, "db_backup"))
        assert a.key == "job_stalled:stan_db_backup"
        assert a.severity == "critical"

    def test_a_run_that_never_finishes_is_reported(self, tmp_path):
        rows = (_ioncloud(8.2, 1, None)
                + [_row(f"2_{t}", "stan-ioncloud", "RUNNING", NOW - timedelta(hours=7.2))
                   for t in range(4)])
        [a] = _job(check_job_failures(now=NOW, sacct_text="\n".join(rows),
                                      log_dir=self._crons(tmp_path, "ioncloud")),
                   "stan-ioncloud")
        assert a.key == "job_stalled:stan-ioncloud"

    def test_the_run_after_a_failure_is_waited_for(self, tmp_path):
        """2026-10-19 03:20: the 18 Oct backup had failed (and said so), and
        the next one was running. "No good backup in 48 h" then was a second
        message about the same failure, minutes before the run fixed it."""
        rows = [_row("100", "stan_db_backup", "COMPLETED", NOW - timedelta(hours=48.1)),
                _row("200", "stan_db_backup", "FAILED", NOW - timedelta(hours=24.1)),
                _row("300", "stan_db_backup", "RUNNING", NOW - timedelta(minutes=3))]
        assert check_job_failures(now=NOW, sacct_text="\n".join(rows),
                                  log_dir=self._crons(tmp_path, "db_backup")) == []

    def test_a_job_preempted_every_time_is_reported(self, tmp_path):
        rows = [_row("1", "stan_count_acq", "COMPLETED", NOW - timedelta(hours=8))]
        rows += [_row(str(10 + i), "stan_count_acq", "PREEMPTED",
                      NOW - timedelta(minutes=15 * i)) for i in range(1, 25)]
        [a] = check_job_failures(now=NOW, sacct_text="\n".join(rows),
                                 log_dir=self._crons(tmp_path, "count_acquisitions"))
        assert a.key == "job_stalled:stan_count_acq"

    def test_a_job_not_scheduled_here_is_not_watched(self, tmp_path):
        """Another lab's SLURM install runs none of these jobs."""
        rows = [_row("1", "someone_else", "COMPLETED", NOW - timedelta(hours=1))]
        assert check_job_failures(now=NOW, sacct_text="\n".join(rows),
                                  log_dir=str(tmp_path)) == []

    def test_once_a_day_and_once_more_when_it_turns_critical(self, tmp_path):
        import os

        base = datetime(2026, 10, 7, 11, 15, tzinfo=LOCAL)
        root = self._crons(tmp_path, "count_acquisitions", at=base)
        log = os.path.join(root, iw.CRON_LOGS["count_acquisitions"][0])
        rows = [_row("1", "stan_count_acq", "COMPLETED", base)]
        sent, store, t = [], None, base
        while t <= base + timedelta(hours=60):
            os.utime(log, (t.timestamp(), t.timestamp()))   # "already queued"
            for a in check_job_failures(now=t, sacct_text="\n".join(rows),
                                        log_dir=root):
                ok, why = should_send(a, store, now=t)
                if ok:
                    store = {"signature": a.signature, "last_sent": t.isoformat()}
                    sent.append(((t - base).total_seconds() / 3600, why))
            t += timedelta(minutes=20)
        # 6 h: warning; 24 h: critical; then daily. (In life the run falls
        # out of sacct's 48 h window, which changes nothing here.)
        assert [round(h) for h, _ in sent] == [6, 24, 48]
        assert [w for _, w in sent] == ["new", "escalated", "cooled_off"]

    def test_every_job_has_a_cron_and_a_limit_sacct_can_see(self):
        assert set(iw.SLURM_JOB_CRON) == set(iw.SLURM_JOBS)
        for cron in iw.SLURM_JOB_CRON.values():
            assert iw.CRON_LOGS[cron][1] < iw.JOB_LOOKBACK_HOURS


class TestWhenSacctIsMissingOrBroken:

    def test_no_slurm_means_no_check(self, monkeypatch, tmp_path):
        """A single-lab install has no scheduler; that is not an outage."""
        monkeypatch.setattr(iw.shutil, "which", lambda _n: None)
        monkeypatch.setattr(iw, "SACCT_FALLBACK", "/no/such/sacct")
        monkeypatch.setattr(iw, "CRON_LOG_DIR", str(tmp_path))
        assert check_job_failures(now=NOW) == []

    def test_no_sacct_on_hive_is_reported(self, monkeypatch, tmp_path):
        """STAN's cron logs are here, so this is Hive and sacct has moved:
        going quiet would switch the check off without a word."""
        monkeypatch.setattr(iw.shutil, "which", lambda _n: None)
        monkeypatch.setattr(iw, "SACCT_FALLBACK", "/no/such/sacct")
        monkeypatch.setattr(iw, "CRON_LOG_DIR", str(tmp_path))
        pattern = next(iter(iw.CRON_LOGS.values()))[0]
        (tmp_path / pattern.replace("*", "20261021")).write_text("tick\n")
        [a] = check_job_failures(now=NOW)
        assert a.key == "job_failing:unreadable"
        assert "/no/such/sacct" in "\n".join(a.detail)

    def test_a_failing_sacct_is_reported_not_swallowed(self, monkeypatch):
        import subprocess

        def boom(*_a, **_k):
            raise subprocess.CalledProcessError(1, "sacct", stderr="slurmdbd down")

        monkeypatch.setattr(iw.shutil, "which", lambda _n: "/usr/bin/sacct")
        monkeypatch.setattr(iw.subprocess, "run", boom)
        [a] = check_job_failures(now=NOW)
        assert a.key == "job_failing:unreadable"
        assert a.severity == "warning"
        assert "slurmdbd down" in "\n".join(a.detail)

    def test_garbage_lines_are_skipped(self):
        assert check_job_failures(now=NOW, sacct_text="nonsense\n|||\n") == []


def test_run_watch_asks_the_question(monkeypatch):
    seen = []
    monkeypatch.setattr(iw, "load_documents", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(iw, "check_publish_freshness", lambda **_k: [])
    monkeypatch.setattr(iw, "check_cron_heartbeat", lambda **_k: [])
    monkeypatch.setattr(iw, "check_job_failures",
                        lambda **_k: seen.append(True) or [])

    class Store:
        def get(self, _k):
            return None

        def record(self, *_a, **_k):
            pass

        def flush(self):
            pass

    iw.run_watch(dry_run=True, now=NOW, store=Store())
    assert seen == [True]


def test_a_bug_in_it_does_not_cost_the_tick_its_other_alerts(monkeypatch):
    from stan.notify import Alert

    beat = Alert(key="cron_silent:x", kind="cron_silent", instrument="t",
                 severity="warning", headline="x has gone quiet", detail=[],
                 signature="s")
    monkeypatch.setattr(iw, "load_documents", lambda *_a, **_k: (None, None))
    monkeypatch.setattr(iw, "check_publish_freshness", lambda **_k: [])
    monkeypatch.setattr(iw, "check_cron_heartbeat", lambda **_k: [beat])

    def boom(**_k):
        raise KeyError("start")

    monkeypatch.setattr(iw, "check_job_failures", boom)
    recorded = []

    class Store:
        def record(self, alert, **_k):
            recorded.append(alert)

        def flush(self):
            pass

    # seed records every alert the tick produced, which is what is asserted.
    iw.run_watch(now=NOW, store=Store(), seed=True)
    keys = {a.key for a in recorded}
    assert keys == {"cron_silent:x", "job_failing:unreadable"}
    [u] = [a for a in recorded if a.key == "job_failing:unreadable"]
    assert "KeyError" in "\n".join(u.detail)


class TestReadingTheLog:
    """This runs on a login node every tick, for every alerting job."""

    def _row(self, path, err=""):
        return {"jobid": "5", "raw": "5", "name": "stan-mon-a.d",
                "out": str(path), "err": err}

    def _counting(self, monkeypatch):
        """Wrap open() in the module and count the bytes it hands back."""
        import builtins

        read = []

        class Counted:
            def __init__(self, fh):
                self._fh = fh

            def __enter__(self):
                return self

            def __exit__(self, *a):
                self._fh.close()

            def __getattr__(self, n):
                return getattr(self._fh, n)

            def read(self, *a):
                b = self._fh.read(*a)
                read.append(len(b))
                return b

            def readline(self, *a):
                b = self._fh.readline(*a)
                read.append(len(b))
                return b

        monkeypatch.setattr(iw, "open", lambda *a, **k: Counted(builtins.open(*a, **k)),
                            raising=False)
        return read

    def test_a_log_with_no_newlines_is_not_read_whole(self, tmp_path, monkeypatch):
        log = tmp_path / "a.out"
        log.write_bytes(b"x" * (20 * 1024 * 1024))
        read = self._counting(monkeypatch)
        assert iw._first_error(self._row(log)) is None
        assert sum(read) <= iw._LOG_TAIL_BYTES + iw._LOG_HEAD_BYTES

    def test_one_file_named_twice_is_read_once(self, tmp_path, monkeypatch):
        """Without --error, sacct reports StdOut as StdErr too."""
        log = tmp_path / "a.out"
        log.write_bytes(b"y" * (2 * 1024 * 1024))
        read = self._counting(monkeypatch)
        iw._first_error(self._row(log, err=str(log)))
        assert sum(read) <= iw._LOG_TAIL_BYTES + iw._LOG_HEAD_BYTES

    def test_the_status_line_is_found_past_a_huge_line(self, tmp_path):
        log = tmp_path / "a.out"
        log.write_bytes(b"x" * (3 * 1024 * 1024) + b"\n"
                        + b'{"status": "error", "error": "rawmeat extraction returned empty"}\n')
        assert iw._first_error(self._row(log)) == "rawmeat extraction returned empty"

    def test_binary_junk_finds_nothing_and_does_not_raise(self, tmp_path):
        log = tmp_path / "a.out"
        log.write_bytes(bytes(range(256)) * 4096)
        assert iw._first_error(self._row(log)) is None


@pytest.mark.parametrize("path, expect", [
    ("/l/ioncloud_%A_%a.out", "/l/ioncloud_123_1.out"),
    ("/l/x_%j.out", "/l/x_777.out"),
    ("/l/%x.log", "/l/stan-ioncloud.log"),
])
def test_log_names_are_expanded_like_slurm_does(path, expect):
    row = {"jobid": "123_1", "raw": "777", "name": "stan-ioncloud"}
    assert iw._expand_log(path, row) == expect
