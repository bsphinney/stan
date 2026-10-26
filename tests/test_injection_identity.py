from datetime import datetime

from stan.reports.injection_identity import (
    find_run_near, identity_line, sample_from_run_name)


def test_sample_and_vial_come_out_of_a_hystar_name():
    assert sample_from_run_name("10212026_HE50_100-spd-dia_S1-A5_1_25352.d") == \
        ("10212026_HE50_100-spd-dia", "S1-A5")

def test_compass_style_name_too():
    assert sample_from_run_name(
        "23oct26_2jourHeL50-Ker2Anly-newEvoNoodl-tf9d0_100spd_S4-E1_1_25301.d") == \
        ("23oct26_2jourHeL50-Ker2Anly-newEvoNoodl-tf9d0_100spd", "S4-E1")

def test_unrecognised_name_keeps_the_name_and_no_vial():
    assert sample_from_run_name("weird.d") == ("weird", None)
    assert sample_from_run_name(None) == (None, None)

def test_identity_is_vial_sample_time():
    at = datetime(2026, 10, 24, 2, 4, 24)
    assert identity_line("S1-A5", "10212026_HE50_100-spd-dia", at) == \
        "S1-A5 · 10212026_HE50_100-spd-dia · 02:04"
    assert identity_line("S1-A5", None, at) == "S1-A5 · 02:04"
    assert identity_line(None, None, at) == "02:04"

def test_find_run_near_is_none_without_pg_backend(monkeypatch):
    monkeypatch.delenv("STAN_DB_BACKEND", raising=False)
    def boom():
        raise AssertionError("must not touch PG")
    monkeypatch.setattr("stan.db_pg._connect", boom)
    assert find_run_near("S1-A5", datetime(2026, 10, 24, 2, 4)) is None

def test_find_run_near_is_none_on_pg_error(monkeypatch):
    monkeypatch.setenv("STAN_DB_BACKEND", "pg")
    def boom():
        raise RuntimeError("pg down")
    monkeypatch.setattr("stan.db_pg._connect", boom)
    assert find_run_near("S1-A5", datetime(2026, 10, 24, 2, 4)) is None

def test_parse_local_ambiguous_time_takes_later_offset():
    from datetime import timedelta
    from stan.instrument_time import INSTRUMENT_TZ, parse_local
    dt = parse_local("2026-11-01T01:30:00")
    assert dt.utcoffset() == timedelta(hours=-8)       # PST, the second 01:30
    assert parse_local("2026-10-24T02:04:24").utcoffset() == timedelta(hours=-7)
    aware = datetime(2026, 1, 1, tzinfo=INSTRUMENT_TZ)
    assert parse_local(aware) is aware

def test_instrument_watch_still_exports_the_old_names():
    from stan.reports import instrument_watch as iw
    from stan.instrument_time import INSTRUMENT_TZ
    assert iw.INSTRUMENT_TZ is INSTRUMENT_TZ
    assert iw._parse_local("2026-10-24T02:04:24").tzinfo is INSTRUMENT_TZ
