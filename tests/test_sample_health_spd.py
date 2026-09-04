"""SPD on sample_health rows.

Regression cover for the bug that made the dashboard's TIC overlay
report "Sample (0) · Blank (0)" on a week with several hundred sample
acquisitions: ``sample_health`` had no ``spd`` column, the API stubbed
the field to None for every row, and the frontend's SPD filter compares
``String(r.spd) === String(spdFilter)`` — so selecting any gradient
dropped 100 % of Sample and Blank traces regardless of the data.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from stan.db import init_db, insert_sample_health
from stan.metrics.scoring import spd_from_filename


class TestSpdFromFilename:
    def test_resolves_common_evosep_tokens(self):
        assert spd_from_filename("20260902_793_100spd_COH-39_S5-C2_1.d") == 100
        assert spd_from_filename("20260821_60spd_HeLa_S1-A1.d") == 60
        assert spd_from_filename("run_30-spd_test.raw") == 30

    def test_separator_variants_all_match(self):
        # The bug that left 10.7 % of runs gradient-less was a `\s*`
        # separator that matched "100 spd" but not "60-spd".
        for name in ("x_60spd_y.d", "x_60 spd_y.d", "x_60-spd-dia_y.d",
                     "x_60_spd_y.d", "x_60SPD_y.d"):
            assert spd_from_filename(name) == 60, name

    def test_refuses_a_number_glued_to_a_date(self):
        # "0604202560Spd" is a date followed by a throughput token.
        # Refusing to guess is correct: NULL renders as "SPD unknown",
        # a wrong value silently bucket-mixes a cohort.
        assert spd_from_filename("0604202560Spd_sample.d") is None

    def test_no_token_returns_none(self):
        assert spd_from_filename("FLsep_wa_20260904145507.raw") is None
        assert spd_from_filename("") is None
        assert spd_from_filename(None) is None


class TestInsertSampleHealthStoresSpd:
    def test_spd_resolved_from_run_name_when_raw_unreachable(self, tmp_path: Path):
        """A sample row gets an SPD even when the raw file is not local.

        The dashboard runs on a Mac while the .d lives on Hive, so
        raw_path almost never resolves on the host doing the insert.
        The filename token has to carry it.
        """
        db_path = tmp_path / "test.db"
        init_db(db_path)

        health_id = insert_sample_health(
            instrument="timsTOF HT",
            run_name="20260902_793_100spd_COH-39_S5-C2_1.d",
            run_date="2026-09-02T10:00:00",
            raw_path="/nowhere/that/exists.d",
            verdict="pass",
            reasons=[],
            rawmeat_summary={},
            db_path=db_path,
        )

        with sqlite3.connect(str(db_path)) as con:
            spd = con.execute(
                "SELECT spd FROM sample_health WHERE id = ?", (health_id,)
            ).fetchone()[0]
        assert spd == 100

    def test_explicit_spd_wins_over_resolution(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        init_db(db_path)
        health_id = insert_sample_health(
            instrument="timsTOF HT",
            run_name="20260902_100spd_thing.d",
            run_date="2026-09-02T10:00:00",
            raw_path="/nowhere.d",
            verdict="pass",
            reasons=[],
            rawmeat_summary={},
            db_path=db_path,
            spd=60,
        )
        with sqlite3.connect(str(db_path)) as con:
            spd = con.execute(
                "SELECT spd FROM sample_health WHERE id = ?", (health_id,)
            ).fetchone()[0]
        assert spd == 60

    def test_unresolvable_name_stores_null_not_a_guess(self, tmp_path: Path):
        db_path = tmp_path / "test.db"
        init_db(db_path)
        health_id = insert_sample_health(
            instrument="Orbitrap Fusion Lumos",
            run_name="FLsep_wa_20260904145507.raw",
            run_date="2026-09-04T14:55:07",
            raw_path="/nowhere.raw",
            verdict="pass",
            reasons=[],
            rawmeat_summary={},
            db_path=db_path,
        )
        with sqlite3.connect(str(db_path)) as con:
            spd = con.execute(
                "SELECT spd FROM sample_health WHERE id = ?", (health_id,)
            ).fetchone()[0]
        assert spd is None


class TestTicOverviewSurfacesSpd:
    def test_sample_rows_carry_spd_so_the_filter_can_match(
        self, tmp_path: Path, monkeypatch
    ):
        """The API must return the stored SPD, not a None stub.

        This is the assertion that fails against the old code: every
        sample facet row came back with spd=None, so the UI's
        `String(r.spd) === String(spdFilter)` never matched and the
        Sample/Blank panels reported zero.
        """
        import asyncio

        db_path = tmp_path / "test.db"
        init_db(db_path)
        monkeypatch.setattr("stan.db.get_db_path", lambda: db_path)

        from stan.db import insert_health_tic_trace

        health_id = insert_sample_health(
            instrument="timsTOF HT",
            run_name="20260902_793_100spd_COH-39_S5-C2_1.d",
            run_date="2026-09-02T10:00:00",
            raw_path="/nowhere.d",
            verdict="pass",
            reasons=[],
            rawmeat_summary={},
            db_path=db_path,
        )
        insert_health_tic_trace(
            health_id,
            [float(i) * 0.1 for i in range(64)],
            [float(i) * 10.0 for i in range(64)],
            db_path=db_path,
        )

        from stan.dashboard import server as srv
        monkeypatch.setattr(srv, "get_db_path", lambda: db_path, raising=False)

        payload = asyncio.run(
            srv.api_today_tic_overview(date="2026-09-02", days=7,
                                       instrument="timsTOF HT")
        )
        samples = payload["facets"]["sample"]
        assert len(samples) == 1
        assert samples[0]["spd"] == 100
        assert samples[0]["has_tic"] is True

        # And the filter the UI applies now keeps the row.
        kept = [r for r in samples if str(r.get("spd")) == "100"]
        assert len(kept) == 1


class TestInstrumentCapacities:
    """Utilisation is scored against the gradients an instrument runs.

    The snapshot's global (100, 60) is Evosep's timsTOF ladder. Applied
    to an Orbitrap it reports "42.9 % of 100 SPD" about a lab that has
    never run a 100 SPD method — a percentage of nothing.
    """

    def test_picks_the_two_most_used_gradients_high_first(self):
        from stan.dashboard.server import _instrument_capacities
        # Real shape: Exploris 480 over 90 days.
        assert _instrument_capacities({38: 50, 19: 27, 12: 13}, [100, 60]) == [38, 19]
        # Real shape: timsTOF HT — unchanged, because it does run these.
        assert _instrument_capacities({100: 642, 60: 459, 30: 14}, [100, 60]) == [100, 60]

    def test_single_gradient_yields_a_single_tile(self):
        from stan.dashboard.server import _instrument_capacities
        assert _instrument_capacities({30: 12}, [100, 60]) == [30]

    def test_falls_back_to_global_when_nothing_resolved(self):
        from stan.dashboard.server import _instrument_capacities
        assert _instrument_capacities(None, [100, 60]) == [100, 60]
        assert _instrument_capacities({}, [100, 60]) == [100, 60]

    def test_ties_break_toward_the_higher_spd(self):
        from stan.dashboard.server import _instrument_capacities
        # Stable ordering matters: a tie that flips on one acquisition
        # would rename the tiles between refreshes.
        assert _instrument_capacities({60: 10, 100: 10, 30: 10}, [100, 60]) == [100, 60]


class TestSpdUsageByInstrument:
    def test_counts_qc_and_sample_rows_together(self, tmp_path: Path):
        """Sample load, not the QC injections bracketing it, decides
        what gradient an instrument 'runs'."""
        from stan.db import insert_run, spd_usage_by_instrument

        db_path = tmp_path / "test.db"
        init_db(db_path)

        insert_run(
            instrument="timsTOF HT",
            run_name="HeLa_QC_60spd.d",
            raw_path="/nowhere/HeLa_QC_60spd.d",
            mode="DIA",
            metrics={"n_precursors": 20000},
            spd=60,
            db_path=db_path,
        )
        for i in range(3):
            insert_sample_health(
                instrument="timsTOF HT",
                run_name=f"20260902_100spd_S{i}.d",
                run_date="2026-09-02T10:00:00",
                raw_path="/nowhere.d",
                verdict="pass",
                reasons=[],
                rawmeat_summary={},
                db_path=db_path,
            )

        usage = spd_usage_by_instrument(days=3650, db_path=db_path)
        assert usage["timsTOF HT"][100] == 3
        assert usage["timsTOF HT"].get(60) == 1

    def test_null_spd_rows_are_skipped_not_bucketed(self, tmp_path: Path):
        from stan.db import spd_usage_by_instrument

        db_path = tmp_path / "test.db"
        init_db(db_path)
        insert_sample_health(
            instrument="Orbitrap Fusion Lumos",
            run_name="FLsep_wa_20260904145507.raw",
            run_date="2026-09-04T14:55:07",
            raw_path="/nowhere.raw",
            verdict="pass",
            reasons=[],
            rawmeat_summary={},
            db_path=db_path,
        )
        usage = spd_usage_by_instrument(days=3650, db_path=db_path)
        assert usage.get("Orbitrap Fusion Lumos") in (None, {})


class TestPgInsertSurvivesThePreMigrationWindow:
    """Code reaches instrument PCs before the PG owner migration runs.

    ALTER TABLE on PG Farm needs the table owner's CAS login, while
    `update-stan.bat` pulls main on its own schedule. If the INSERT
    named `spd` before the column existed, every sample-health write on
    Hive would fail for the length of that window.
    """

    def test_column_list_narrows_to_what_pg_actually_has(self, monkeypatch):
        from stan import db_pg

        monkeypatch.setattr(db_pg, "_SH_PG_COLUMNS", None, raising=False)
        # A PG that has not run the v1.0.85 migration yet.
        monkeypatch.setattr(
            db_pg, "_sample_health_pg_columns",
            lambda: {"id", "instrument", "run_name", "run_date", "verdict"},
        )

        captured = {}

        class _Cur:
            def execute(self, sql, params):
                captured["sql"] = sql
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class _Conn:
            def cursor(self): return _Cur()
            def commit(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(db_pg, "_connect", lambda: _Conn())
        db_pg.insert_sample_health_pg({
            "id": "abc", "instrument": "timsTOF HT", "run_name": "x.d",
            "run_date": "2026-09-02T10:00:00", "verdict": "pass", "spd": 100,
        })
        assert "spd" not in captured["sql"]
        assert "instrument" in captured["sql"]

    def test_introspection_failure_does_not_drop_columns(self, monkeypatch):
        """An unreadable catalog must not silently discard real data."""
        from stan import db_pg

        monkeypatch.setattr(db_pg, "_SH_PG_COLUMNS", None, raising=False)
        monkeypatch.setattr(db_pg, "_sample_health_pg_columns", lambda: set())

        captured = {}

        class _Cur:
            def execute(self, sql, params):
                captured["sql"] = sql
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class _Conn:
            def cursor(self): return _Cur()
            def commit(self): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(db_pg, "_connect", lambda: _Conn())
        db_pg.insert_sample_health_pg({
            "id": "abc", "instrument": "timsTOF HT", "run_name": "x.d",
            "run_date": "2026-09-02T10:00:00", "verdict": "pass", "spd": 100,
        })
        assert "spd" in captured["sql"]


class TestPgQueriesMatchTheRealColumnTypes:
    """`runs.run_date` and `sample_health.run_date` are NOT the same type.

    In PG Farm `runs.run_date` is `timestamp with time zone` while
    `sample_health.run_date` is `text`, and `runs.hidden` is `integer`,
    not boolean. A UNION across the two without casts raises

        function substr(timestamp with time zone, integer, integer)
        does not exist

    which `spd_usage_by_instrument_pg` catches and answers `{}` for —
    so the per-instrument utilisation capacities silently fell back to
    the global Evosep 100/60 pair and the feature looked unimplemented
    rather than broken. SQLite stores both as TEXT, so nothing local
    reproduces it.
    """

    def test_sql_casts_run_date_and_compares_hidden_as_integer(self):
        import inspect
        from stan import db_pg

        src = inspect.getsource(db_pg.spd_usage_by_instrument_pg)
        # Both arms of the UNION must cast, so the mixed types never meet.
        # (The outer substr() reads the subquery alias, which is already
        # text by then — that one is fine uncast.)
        assert "run_date::text AS run_date FROM runs" in src
        assert "run_date::text AS run_date FROM sample_health" in src
        # The runs-only fallback casts inline instead.
        assert "substr(run_date::text, 1, 10)" in src
        # hidden is integer in PG; `= false` is a type error.
        assert "hidden = false" not in src, "hidden is integer, compare to 0"
        assert "hidden = 0" in src

    @pytest.mark.integration
    def test_returns_real_counts_against_pg(self):
        """Runs only where PG is reachable (Hive). This is the check that
        actually proves it — the source assertions above are a guard, not
        a substitute."""
        from stan.db_pg import spd_usage_by_instrument_pg, use_pg
        if not use_pg():
            pytest.skip("needs STAN_DB_BACKEND=pg")
        usage = spd_usage_by_instrument_pg("2026-06-06")
        assert usage, "query returned nothing — the casts regressed"
        assert any(v for v in usage.values())
