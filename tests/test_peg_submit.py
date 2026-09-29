"""PEG share client (stan.community.peg_submit, spec §4.4).

What this pins down, in order of how badly it would hurt to get wrong:

* No run name leaves the lab. The run name is the one field that can carry a
  customer or project identifier, and it is used only to derive ``run_key``.
* ``'unknown'`` is never shared. It is the reader-failure sentinel, stored
  with ``peg_score = 0.0``, so a client that trusted the score would publish
  every broken reader as a perfectly clean run -- and top the leaderboard.
* Sharing off means no DB read and no network call, so a lab that never
  opted in pays nothing for the cron line.
* ``run_key`` is stable across the ways one run is stored: ``\\`` vs ``/``
  paths, ``-08:00`` vs ``+00:00`` offsets, PG ``datetime`` vs SQLite TEXT.
  An unstable key would count one run twice on the relay, forever.

The DB reader (``stan.db.get_peg_share_rows``) and the blank filter
(``stan.metrics.peg_trends.is_blank_or_wash``) belong to the backend and are
mocked here; one test at the bottom exercises the real blank filter once it
exists.
"""

from __future__ import annotations

import builtins
import contextlib
import errno
import hashlib
import importlib
import io
import json
import os
import re
import stat
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import yaml

import stan.config as stan_config
import stan.db as stan_db
from stan.community import peg_submit
from stan.community.peg_submit import (
    BATCH_SIZE,
    build_peg_records,
    compute_run_key,
    run_basename,
    sync_peg,
    utc_iso,
    write_community_keys,
)

SPEC_RECORD_KEYS = {
    "run_key", "run_date", "instrument_family", "instrument_model", "lc_system",
    "lc_model", "spd", "acquisition_mode", "sample_type", "amount_ng",
    "peg_intensity_pct", "peg_score", "peg_n_ions_detected", "peg_class", "peg_method",
}

_BLANK_RE = re.compile(r"(?i)(wash|blank|blnk|blk|DELETE)")  # submit-all's filter


# ── fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path, request):
    """Fresh config dir, clean env, stubbed blank filter, no real sleeping."""
    user_dir = tmp_path / "dotstan"
    user_dir.mkdir()
    monkeypatch.setattr(stan_config, "_USER_CONFIG_DIR", user_dir)
    for var in ("STAN_PEG_SHARE", "STAN_DISPLAY_NAME", "STAN_DB_BACKEND"):
        monkeypatch.delenv(var, raising=False)
    sleeps: list[float] = []
    monkeypatch.setattr(peg_submit, "_sleep", sleeps.append)

    # Nothing here may reach the shared drive, even on a Mac that has it mounted.
    monkeypatch.setattr(stan_config, "sync_to_hive_mirror", lambda *a, **k: False)

    if "real_blank_filter" not in request.node.name:
        try:
            trends = importlib.import_module("stan.metrics.peg_trends")
        except Exception:  # backend module not written yet, or mid-edit
            trends = types.ModuleType("stan.metrics.peg_trends")
            monkeypatch.setitem(sys.modules, "stan.metrics.peg_trends", trends)
        monkeypatch.setattr(trends, "is_blank_or_wash",
                            lambda name: bool(_BLANK_RE.search(name or "")), raising=False)
    return {"user_dir": user_dir, "sleeps": sleeps}


def _write_cfg(user_dir: Path, **cfg) -> Path:
    path = user_dir / "community.yml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return path


def _row(i: int = 0, **over) -> dict:
    """A realistic timsTOF + Evosep QC row, shaped like get_peg_share_rows()."""
    base = {
        "run_name": f"/quobyte/proteomics-grp/STAN/raw/03Feb25_Hela50-Dia_60spd_S1-A7_1_{11184 + i}.d",
        "instrument": "timsTOF HT",
        "run_date": (datetime(2025, 2, 3, 19, 47, 26, tzinfo=timezone.utc)
                     + timedelta(minutes=14 * i)),
        "spd": 60,
        "mode": "diaPASEF",
        "amount_ng": 50.0,
        "lc_system": "evosep",
        "peg_score": 7.5,
        "peg_intensity_pct": 0.353,
        "peg_n_ions_detected": 2,
        "peg_class": "clean",
    }
    base.update(over)
    return base


class _Relay:
    """Stands in for httpx.post; replies from a script, records every call."""

    def __init__(self, replies=None):
        self.calls: list[dict] = []
        self.replies = list(replies or [])

    def __call__(self, url, content=None, headers=None, timeout=None):
        payload = json.loads(content)
        self.calls.append({"url": url, "headers": dict(headers or {}), "payload": payload,
                           "raw": content})
        req = httpx.Request("POST", url)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply if isinstance(reply, httpx.Response) else httpx.Response(
                reply[0], json=reply[1], request=req)
        n = len(payload["records"])
        return httpx.Response(200, request=req, json={
            "status": "ok", "display_name": payload["display_name"], "verified": True,
            "accepted": n, "unchanged": 0, "rejected": []})


@pytest.fixture
def relay(monkeypatch):
    fake = _Relay()
    monkeypatch.setattr(peg_submit.httpx, "post", fake)
    return fake


@pytest.fixture
def share_rows(monkeypatch):
    """Serve rows through stan.db.get_peg_share_rows (the backend's reader)."""
    state: dict = {"rows": [], "calls": 0, "backend_seen": []}

    def fake():
        import os
        state["calls"] += 1
        state["backend_seen"].append(os.environ.get("STAN_DB_BACKEND"))
        return state["rows"]

    monkeypatch.setattr(stan_db, "get_peg_share_rows", fake, raising=False)
    return state


# ── record building ─────────────────────────────────────────────────────

def test_record_has_exactly_the_spec_fields_and_types():
    records, skipped = build_peg_records([_row()], "1.2.0")
    assert skipped == {}
    (rec,) = records
    assert set(rec) == SPEC_RECORD_KEYS
    assert rec["run_date"] == "2025-02-03T19:47:26Z"
    assert rec["instrument_family"] == "timsTOF"
    assert rec["instrument_model"] == "timsTOF HT"
    assert rec["lc_system"] == "evosep"
    assert rec["lc_model"] is None
    assert rec["spd"] == 60 and isinstance(rec["spd"], int)
    assert rec["acquisition_mode"] == "diapasef"
    assert rec["sample_type"] == "hela"
    assert rec["amount_ng"] == 50.0
    assert rec["peg_intensity_pct"] == 0.353
    assert rec["peg_score"] == 7.5
    assert rec["peg_n_ions_detected"] == 2 and isinstance(rec["peg_n_ions_detected"], int)
    assert rec["peg_class"] == "clean"
    assert rec["peg_method"] == "stan-peg-1"
    assert re.fullmatch(r"[0-9a-f]{24}", rec["run_key"])


def test_run_name_never_appears_in_any_record():
    rows = [_row(i, run_name=name) for i, name in enumerate([
        "D:\\Data\\ProjectZebra\\03Feb25_Hela50-Dia_60spd_S1-A7_1_11184.d",
        "/nfs/lssc0/flinders/proteomics/Data/raw_data/tTOF_HT/CustomerX_QC_hela_001.d/",
        "Plain_Hela_Run_42.raw",
    ])]
    records, _ = build_peg_records(rows, "1.2.0")
    assert len(records) == 3
    blob = json.dumps(records)
    for fragment in ("11184", "ProjectZebra", "CustomerX", "Plain_Hela_Run_42",
                     "flinders", "run_name", "Hela50"):
        assert fragment not in blob, fragment


def test_run_key_is_the_spec_formula():
    rec = build_peg_records([_row()], "1.2.0")[0][0]
    expected = hashlib.sha256(
        "timsTOF HT|03Feb25_Hela50-Dia_60spd_S1-A7_1_11184.d|2025-02-03T19:47:26Z".encode()
    ).hexdigest()[:24]
    assert rec["run_key"] == expected


@pytest.mark.parametrize("name", [
    "/quobyte/x/03Feb25_Hela.d",
    "D:\\Data\\QC\\03Feb25_Hela.d",
    "03Feb25_Hela.d/",
    "D:\\Data\\QC\\03Feb25_Hela.d\\",
    "  03Feb25_Hela.d  ",
])
def test_basename_handles_both_separators(name):
    assert run_basename(name) == "03Feb25_Hela.d"


@pytest.mark.parametrize("when", [
    "2025-02-03T11:47:26-08:00",          # SQLite, instrument-PC local time
    "2025-02-03T19:47:26+00:00",          # SQLite, UTC
    "2025-02-03T19:47:26Z",
    "2025-02-03 19:47:26.123456",          # naive: taken as UTC
    "2025-02-03T19:47:26.5+0000",
    datetime(2025, 2, 3, 19, 47, 26, 999, tzinfo=timezone.utc),       # PG timestamptz
    datetime(2025, 2, 3, 11, 47, 26, tzinfo=timezone(timedelta(hours=-8))),
])
def test_run_key_stable_across_date_forms(when):
    assert utc_iso(when) == "2025-02-03T19:47:26Z"
    key = compute_run_key("timsTOF HT", "D:\\QC\\run.d", utc_iso(when))
    assert key == compute_run_key("timsTOF HT", "/quobyte/QC/run.d", "2025-02-03T19:47:26Z")


def test_run_key_distinguishes_extension_instrument_and_time():
    t = "2025-02-03T19:47:26Z"
    base = compute_run_key("timsTOF HT", "run.d", t)
    assert base != compute_run_key("timsTOF HT", "run.raw", t)
    assert base != compute_run_key("timsTOF Pro 2", "run.d", t)
    assert base != compute_run_key("timsTOF HT", "run.d", "2025-02-03T19:47:27Z")


@pytest.mark.parametrize("stored,sent", [
    ("evosep", "evosep"), ("EVOSEP ", "evosep"), ("custom", "other"), ("Custom", "other"),
])
def test_lc_mapping(stored, sent):
    records, _ = build_peg_records([_row(lc_system=stored)], "1.2.0")
    assert records[0]["lc_system"] == sent


@pytest.mark.parametrize("stored", [None, "", "nanoElute", "unknown"])
def test_undetected_lc_is_not_shared(stored):
    records, skipped = build_peg_records([_row(lc_system=stored)], "1.2.0")
    assert records == [] and skipped == {"lc_unknown": 1}


def test_unknown_sentinel_is_never_shared_as_clean():
    # The reader-failure sentinel: score 0.0, pct 0.0, ions 0 -- by the numbers
    # alone, the cleanest run the lab ever acquired.
    rows = [_row(peg_class="unknown", peg_score=0.0, peg_intensity_pct=0.0,
                 peg_n_ions_detected=0)]
    records, skipped = build_peg_records(rows, "1.2.0")
    assert records == [] and skipped == {"peg_unknown": 1}


def test_true_zero_clean_run_is_shared():
    rows = [_row(peg_class="clean", peg_score=0.0, peg_intensity_pct=0.0, peg_n_ions_detected=0)]
    records, _ = build_peg_records(rows, "1.2.0")
    assert records[0]["peg_score"] == 0.0 and records[0]["peg_class"] == "clean"


def test_every_exclusion_reason_is_counted():
    future = datetime.now(timezone.utc) + timedelta(days=3)
    rows = [
        _row(0),                                                      # shared
        _row(1, hidden=1),
        _row(2, hidden="1"),
        _row(3, run_name=""),
        _row(4, run_name="D:\\QC\\Blank_60spd_01.d"),
        _row(5, run_name="Wash_after_hela.d"),
        _row(6, run_name="03Feb25_blk_2.d"),
        _row(7, peg_class="unknown", peg_score=0.0),
        _row(8, peg_class=None),
        _row(9, peg_score=None),
        _row(10, peg_intensity_pct=None),
        _row(11, peg_intensity_pct=float("nan")),
        _row(12, peg_n_ions_detected=None),
        _row(13, peg_intensity_pct=140.0),
        _row(14, lc_system=None),
        _row(15, instrument=""),
        _row(16, run_date="1980-01-01T00:00:00"),
        _row(17, run_date="not a date"),
        _row(18, run_date=future),
        _row(19, spd=None),
        _row(20, spd=0),
        _row(0),                                                      # duplicate key
    ]
    records, skipped = build_peg_records(rows, "1.2.0")
    assert len(records) == 1
    assert skipped == {
        "hidden": 2, "no_run_name": 1, "blank_or_wash": 3, "peg_unknown": 1,
        "no_peg": 5, "peg_out_of_range": 1, "lc_unknown": 1, "no_instrument": 1,
        "bad_date": 3, "no_spd": 2, "duplicate_run_key": 1,
    }
    assert sum(skipped.values()) + len(records) == len(rows)


def test_hidden_zero_as_string_is_not_hidden():
    records, _ = build_peg_records([_row(hidden="0"), _row(1, hidden=0)], "1.2.0")
    assert len(records) == 2


def test_sample_type_from_row_then_name():
    rows = [_row(0, sample_type="K562"),
            _row(1, run_name="03Feb25_K562_50ng_60spd.d"),
            _row(2)]
    types_ = [r["sample_type"] for r in build_peg_records(rows, "1.2.0")[0]]
    assert types_ == ["k562", "k562", "hela"]


def test_float4_noise_is_rounded_so_resends_stay_identical():
    rec = build_peg_records([_row(peg_score=34.59999847, peg_intensity_pct=0.35300001)],
                            "1.2.0")[0][0]
    assert rec["peg_score"] == 34.6 and rec["peg_intensity_pct"] == 0.353


def test_null_amount_gets_submit_all_default():
    assert build_peg_records([_row(amount_ng=None)], "1.2.0")[0][0]["amount_ng"] == 50.0


def test_records_sorted_by_date():
    rows = [_row(i) for i in (5, 1, 3)]
    dates = [r["run_date"] for r in build_peg_records(rows, "1.2.0")[0]]
    assert dates == sorted(dates)


# ── re-processing duplicates ────────────────────────────────────────────
#
# Live PG holds 241 acquisitions two to five times over (same file, same
# instant, different id), and in 168 of them the PEG values differ -- 78 in
# peg_class. Keeping "the first row the reader returned" published whichever
# the heap happened to hold first, so the same run could flip clean <-> heavy
# on the public board after any UPDATE touched those rows.

def _clean(**over) -> dict:
    return _row(0, peg_class="clean", peg_score=2.0, peg_intensity_pct=0.2,
                peg_n_ions_detected=1, **over)


def _heavy(**over) -> dict:
    return _row(0, peg_class="heavy", peg_score=60.0, peg_intensity_pct=9.5,
                peg_n_ions_detected=40, **over)


def test_duplicate_winner_does_not_depend_on_row_order():
    # What the reader returns today: nothing that says which row is newer.
    forward = build_peg_records([_clean(), _heavy()], "1.2.0")
    backward = build_peg_records([_heavy(), _clean()], "1.2.0")
    assert forward == backward
    assert len(forward[0]) == 1 and forward[1] == {"duplicate_run_key": 1}


@pytest.mark.parametrize("newest_first", [False, True])
def test_newest_processing_of_a_duplicate_is_shared(newest_first):
    # 1.0.10 is newer than 1.0.9, which a string comparison gets backwards; the
    # ids are ordered the other way so an id-first rule would pick the old row.
    old = _clean(id="ff01", stan_version="1.0.9")
    new = _heavy(id="0a02", stan_version="1.0.10")
    rows = [new, old] if newest_first else [old, new]
    (rec,), skipped = build_peg_records(rows, "1.2.0")
    assert (rec["peg_class"], rec["peg_intensity_pct"]) == ("heavy", 9.5)
    assert skipped == {"duplicate_run_key": 1}
    assert set(rec) == SPEC_RECORD_KEYS, "id / stan_version must not ride along"


def test_duplicate_rank_is_the_readers():
    """Stored hits, then the newest version, then the highest id -- pick_canonical's order.

    The reader already hands over one row per acquisition, so this rank is a
    second line of defence. Ranked any other way (it used ``migrated_at``
    and ignored hits), the moment two copies got through it would share a
    different one from the copy the PEG tab counts.
    """
    from stan.metrics.peg_trends import pick_canonical

    t0 = datetime(2026, 6, 1, tzinfo=timezone.utc)
    pairs = [
        # a stored ladder beats a newer version
        (_heavy(id="aaa", stan_version="0.2.376", has_hits=True),
         _clean(id="zzz", stan_version="1.1.12", has_hits=False)),
        # no ladder on either: the newer version, by number not text
        (_heavy(id="aaa", stan_version="1.0.10"), _clean(id="zzz", stan_version="1.0.9")),
        # same version: the highest id, whatever migrated_at says
        (_heavy(id="zzz", stan_version="1.0.85", migrated_at=t0),
         _clean(id="aaa", stan_version="1.0.85", migrated_at=t0 + timedelta(days=1))),
        # a version beats none (rows processed before the column existed)
        (_heavy(stan_version="0.2.301"), _clean(stan_version=None)),
    ]
    for winner, loser in pairs:
        for rows in ([winner, loser], [loser, winner]):
            (rec,), skipped = build_peg_records(rows, "1.2.0")
            assert rec["peg_class"] == "heavy", (winner, loser)
            assert skipped == {"duplicate_run_key": 1}
            as_read = [dict(r, run_date_utc=utc_iso(r["run_date"])) for r in rows]
            assert [r["peg_class"] for r in pick_canonical(as_read)] == ["heavy"]


_KEY_PAIRS = {
    # same acquisition
    "windows-vs-posix-dir": (_row(run_name=r"D:\Data\QC\HeLa_k.d"),
                             _row(run_name="/quobyte/proteomics-grp/STAN/raw/HeLa_k.d"), True),
    "trailing-separator": (_row(run_name="HeLa_k.d/"), _row(run_name="HeLa_k.d"), True),
    "instrument-whitespace": (_row(instrument="timsTOF HT "), _row(), True),
    "offset-vs-utc": (_row(run_date="2025-02-03T11:47:26-08:00"),
                      _row(run_date="2025-02-03 19:47:26+00:00"), True),
    "fractional-second": (_row(run_date="2025-02-03T19:47:26.900Z"), _row(), True),
    # different acquisitions
    "next-second": (_row(run_date="2025-02-03T19:47:27Z"), _row(), False),
    "d-vs-raw": (_row(run_name="HeLa_k.d"), _row(run_name="HeLa_k.raw"), False),
    "other-instrument": (_row(instrument="timsTOF Ultra"), _row(), False),
}


@pytest.mark.parametrize("a,b,same", list(_KEY_PAIRS.values()), ids=list(_KEY_PAIRS))
def test_run_key_and_the_readers_acquisition_key_agree(a, b, same):
    """Two rows are one acquisition to the reader iff they share a run_key.

    If these disagree, the tab counts one file twice while the board holds
    it once (or the reverse), and neither side can see the other's rule.
    """
    from stan.metrics.peg_trends import acquisition_key, parse_utc
    from stan.metrics.peg_trends import utc_iso as reader_utc

    def akey(r):
        return acquisition_key(dict(r, run_date_utc=reader_utc(parse_utc(r["run_date"]))))

    def rkey(r):
        (rec,), _ = build_peg_records([r], "1.2.0")
        return rec["run_key"]

    assert (akey(a) == akey(b)) is same
    assert (rkey(a) == rkey(b)) is same


# ── sync: opt-in, identity, network ─────────────────────────────────────

def test_sharing_off_makes_no_db_read_no_post_and_no_log(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", auth_token="t")
    share_rows["rows"] = [_row()]
    result = sync_peg(backend="pg")
    assert result["status"] == "sharing_off" and result["exit_code"] == 0
    assert "peg_share" in result["reason"]
    assert relay.calls == []
    assert share_rows["calls"] == 0
    assert not (_isolated["user_dir"] / "logs").exists()


def test_env_opt_in(_isolated, relay, share_rows, monkeypatch):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail")
    monkeypatch.setenv("STAN_PEG_SHARE", "1")
    share_rows["rows"] = [_row()]
    assert sync_peg()["status"] == "ok"
    assert len(relay.calls) == 1


def test_sharing_without_a_token_warns_the_name_is_unclaimed(_isolated, relay, share_rows,
                                                             caplog):
    """Unclaimed names are allowed, so it still sends -- but says who can take it."""
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    with caplog.at_level("WARNING", logger="stan.community.peg_submit"):
        result = sync_peg()
    assert result["status"] == "ok" and len(relay.calls) == 1
    assert "X-STAN-Auth" not in relay.calls[0]["headers"]
    assert result["unclaimed"] is True
    warned = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("unclaimed" in m and "stan community-claim" in m for m in warned), warned


def test_a_token_means_no_unclaimed_warning(_isolated, relay, share_rows, caplog):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True,
               auth_token="tok")
    share_rows["rows"] = [_row()]
    with caplog.at_level("WARNING", logger="stan.community.peg_submit"):
        result = sync_peg()
    assert result["status"] == "ok" and result["unclaimed"] is False
    assert relay.calls[0]["headers"]["X-STAN-Auth"] == "tok"
    assert not [r for r in caplog.records if "unclaimed" in r.getMessage()]


def test_sharing_off_says_nothing_about_claiming(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail")
    assert sync_peg()["unclaimed"] is False


def test_no_community_yml_at_all_is_sharing_off(relay, share_rows):
    assert sync_peg()["status"] == "sharing_off"
    assert relay.calls == [] and share_rows["calls"] == 0


@pytest.mark.parametrize("name", ["", "Anonymous Lab", "  anonymous lab ", "x" * 61])
def test_refuses_unrankable_names_before_reading(_isolated, relay, share_rows, name):
    _write_cfg(_isolated["user_dir"], display_name=name, peg_share=True)
    share_rows["rows"] = [_row()]
    result = sync_peg()
    assert result["status"] == "no_display_name" and result["exit_code"] == 1
    assert relay.calls == [] and share_rows["calls"] == 0


def test_display_name_env_fallback(_isolated, relay, share_rows, monkeypatch):
    _write_cfg(_isolated["user_dir"], peg_share=True)
    monkeypatch.setenv("STAN_DISPLAY_NAME", "Clogged PeakTail")
    share_rows["rows"] = [_row()]
    assert sync_peg()["status"] == "ok"
    assert relay.calls[0]["payload"]["display_name"] == "Clogged PeakTail"


def test_community_yml_name_wins_over_env(_isolated, relay, share_rows, monkeypatch):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    monkeypatch.setenv("STAN_DISPLAY_NAME", "Someone Else")
    share_rows["rows"] = [_row()]
    sync_peg()
    assert relay.calls[0]["payload"]["display_name"] == "Clogged PeakTail"


def test_batches_of_2000(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row(i) for i in range(4500)]
    result = sync_peg(backend="pg")
    assert [len(c["payload"]["records"]) for c in relay.calls] == [BATCH_SIZE, BATCH_SIZE, 500]
    assert all(c["url"] == "https://brettsp-stan.hf.space/api/peg/submit" for c in relay.calls)
    assert result["status"] == "ok" and result["exit_code"] == 0
    assert result["accepted"] == 4500 and result["batches_ok"] == 3
    keys = [r["run_key"] for c in relay.calls for r in c["payload"]["records"]]
    assert len(set(keys)) == 4500


def test_payload_envelope_and_no_run_name_on_the_wire(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    sync_peg(relay_url="https://relay.example/")
    (call,) = relay.calls
    assert call["url"] == "https://relay.example/api/peg/submit"
    assert set(call["payload"]) == {"display_name", "stan_version", "records"}
    assert call["payload"]["stan_version"] == peg_submit.__version__
    assert b"11184" not in call["raw"] and b"Hela50" not in call["raw"]


def test_auth_header_sent_when_token_present(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail",
               auth_token="tok-123", peg_share=True)
    share_rows["rows"] = [_row()]
    sync_peg()
    assert relay.calls[0]["headers"]["X-STAN-Auth"] == "tok-123"
    assert relay.calls[0]["headers"]["Content-Type"] == "application/json"


def test_no_auth_header_without_token(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    sync_peg()
    assert "X-STAN-Auth" not in relay.calls[0]["headers"]


def test_403_says_how_to_fix_it_and_stops(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row(i) for i in range(4500)]
    relay.replies = [(403, {"detail": "This lab name is claimed. Run `stan community-claim` "
                                      "to get a token."})]
    result = sync_peg()
    assert len(relay.calls) == 1, "a claimed-name refusal must not be repeated per batch"
    assert result["status"] == "failed" and result["exit_code"] == 1
    assert "stan community-claim" in result["errors"][0]
    assert "claimed" in result["errors"][0]
    assert result["batches_failed"] == 3


def test_retries_once_on_503_then_succeeds(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    relay.replies = [(503, {"detail": "busy"})]
    result = sync_peg()
    assert len(relay.calls) == 2 and result["status"] == "ok"
    assert _isolated["sleeps"] == [peg_submit.RETRY_BACKOFF_S]


def test_429_honours_short_retry_after(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    relay.replies = [httpx.Response(429, headers={"Retry-After": "3600"},
                                    json={"detail": "rate limit"},
                                    request=httpx.Request("POST", "x"))]
    sync_peg()
    assert _isolated["sleeps"] == [peg_submit.MAX_RETRY_WAIT_S]


def test_network_error_retried_once(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    relay.replies = [httpx.ConnectError("[Errno -5] No address associated with hostname")]
    assert sync_peg()["status"] == "ok" and len(relay.calls) == 2


def test_persistent_500_on_every_batch_is_failure(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row(i) for i in range(2500)]
    relay.replies = [(500, {"detail": "boom"})] * 4
    result = sync_peg()
    assert len(relay.calls) == 4  # two batches x (try + one retry)
    assert result["status"] == "failed" and result["exit_code"] == 1


def test_one_failed_batch_of_two_is_partial_and_exits_zero(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row(i) for i in range(2500)]
    relay.replies = [(500, {"detail": "boom"}), (500, {"detail": "boom"})]
    result = sync_peg()
    assert result["status"] == "partial" and result["exit_code"] == 0
    assert result["batches_ok"] == 1 and result["batches_failed"] == 1


def test_sleeping_space_html_200_is_not_success(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    page = httpx.Response(200, text="<html>Space is starting</html>",
                          request=httpx.Request("POST", "x"))
    relay.replies = [page, page]
    result = sync_peg()
    assert result["status"] == "failed" and result["accepted"] == 0


def test_counts_aggregate_and_rejections_map_to_run_keys(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row(i) for i in range(3)]
    relay.replies = [(200, {"status": "ok", "display_name": "Clogged PeakTail",
                            "verified": False, "accepted": 1, "unchanged": 1,
                            "rejected": [{"index": 2, "reason": "run_date in the future"}]})]
    result = sync_peg()
    assert (result["accepted"], result["unchanged"], result["rejected"]) == (1, 1, 1)
    assert result["verified"] is False
    assert result["rejected_reasons"] == {"run_date in the future": 1}
    lines = [json.loads(ln) for ln in Path(result["log_path"]).read_text().splitlines()]
    batch = next(ln for ln in lines if ln["event"] == "batch")
    sent_key = relay.calls[0]["payload"]["records"][2]["run_key"]
    assert batch["rejected_sample"][0]["run_key"] == sent_key


def test_log_file_has_one_line_per_batch_and_a_summary(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row(i) for i in range(2500)] + [_row(9999, peg_class="unknown")]
    result = sync_peg(backend="pg")
    log = Path(result["log_path"])
    assert log.parent == _isolated["user_dir"] / "logs"
    assert re.fullmatch(r"peg_sync_\d{8}T\d{6}Z\.jsonl", log.name)
    lines = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert [ln["event"] for ln in lines] == ["batch", "batch", "summary"]
    summary = lines[-1]
    assert summary["status"] == "ok" and summary["n_records"] == 2500
    assert summary["skipped"] == {"peg_unknown": 1}
    assert "11184" not in log.read_text(), "the log must not carry run names either"


def test_db_error_is_logged_and_exits_nonzero(_isolated, relay, monkeypatch):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)

    def boom():
        raise RuntimeError("JWT expired")

    monkeypatch.setattr(stan_db, "get_peg_share_rows", boom, raising=False)
    result = sync_peg(backend="pg")
    assert result["status"] == "db_error" and result["exit_code"] == 1
    assert relay.calls == []
    assert "JWT expired" in Path(result["log_path"]).read_text()


def test_nothing_to_share_posts_nothing(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row(peg_class="unknown")]
    result = sync_peg()
    assert result["status"] == "nothing_to_share" and result["exit_code"] == 0
    assert relay.calls == []


def test_dry_run_builds_but_sends_nothing_even_with_sharing_off(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail")
    share_rows["rows"] = [_row(i) for i in range(2100)]
    result = sync_peg(dry_run=True)
    assert relay.calls == []
    assert result["status"] == "dry_run" and result["exit_code"] == 0
    assert result["n_records"] == 2100 and result["batches"] == 2
    assert "peg_share" in result["reason"]
    assert Path(result["log_path"]).exists()


def test_backend_flag_sets_env_for_the_read_and_restores_it(share_rows, relay, _isolated,
                                                           monkeypatch):
    import os
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    sync_peg(backend="pg")
    sync_peg(backend="sqlite")
    assert share_rows["backend_seen"] == ["pg", "sqlite"]
    assert "STAN_DB_BACKEND" not in os.environ
    monkeypatch.setenv("STAN_DB_BACKEND", "pg")
    sync_peg(backend="sqlite")
    assert os.environ["STAN_DB_BACKEND"] == "pg"
    sync_peg()  # no flag: follow the environment
    assert share_rows["backend_seen"][-1] == "pg"


# ── community.yml writing ───────────────────────────────────────────────

def test_write_community_keys_keeps_other_keys(_isolated):
    path = _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail",
                      auth_token="old", community_submit=True,
                      email_reports={"enabled": True, "to": "x@y"})
    out = write_community_keys({"auth_token": "new"}, set_if_missing={"display_name": "Other"})
    assert out == path
    data = yaml.safe_load(path.read_text())
    assert data == {"display_name": "Clogged PeakTail", "auth_token": "new",
                    "community_submit": True, "email_reports": {"enabled": True, "to": "x@y"}}
    if sys.platform != "win32":
        assert (path.stat().st_mode & 0o077) == 0, "the token file must not be group/world readable"


def test_write_community_keys_creates_file(_isolated):
    path = write_community_keys({"auth_token": "t"}, set_if_missing={"display_name": "Lab"})
    assert yaml.safe_load(path.read_text()) == {"display_name": "Lab", "auth_token": "t"}


@contextlib.contextmanager
def _umask(mask: int):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


class _QuotaHit:
    """A text file that dies half-way through its write, as on a full home quota."""

    def __init__(self, fh, modes: list[int]):
        self._fh, self._modes = fh, modes

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._fh.close()
        return False

    def __getattr__(self, name):
        return getattr(self._fh, name)

    def write(self, text):
        self._modes.append(stat.S_IMODE(os.fstat(self._fh.fileno()).st_mode))
        self._fh.write(text[: len(text) // 2])
        self._fh.flush()
        raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_community_yml_tmp_is_private_from_creation_and_removed_on_failure(
        _isolated, monkeypatch):
    # Hive: umask 002, and ~ and ~/.stan are drwxrwsr-x. write_text() made the
    # tmp 0664 with the Slack bot token already in it, and a write that died
    # left it behind at 0664 for anyone to read.
    path = _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail",
                      auth_token="old", slack_bot_token="xoxb-secret")
    path.chmod(0o600)
    before = path.read_text()
    modes: list[int] = []
    real_open = io.open

    def spy_open(file, mode="r", *args, **kwargs):
        fh = real_open(file, mode, *args, **kwargs)
        is_tmp = isinstance(file, int) or str(file).endswith("community.yml.tmp")
        return _QuotaHit(fh, modes) if "w" in mode and is_tmp else fh

    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(builtins, "open", spy_open)
    with _umask(0o002), pytest.raises(OSError):
        write_community_keys({"auth_token": "new"})
    assert modes == [0o600], "secrets were written into a group/world-readable file"
    assert not (_isolated["user_dir"] / "community.yml.tmp").exists()
    assert path.read_text() == before


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlinks and modes")
def test_planted_or_stale_tmp_is_replaced_not_followed(_isolated, tmp_path):
    # ~/.stan is group-writable on Hive, so the tmp name can already exist --
    # left over from a crash, or a symlink someone else put there.
    path = _write_cfg(_isolated["user_dir"], display_name="Lab", slack_bot_token="xoxb-secret")
    bait = tmp_path / "elsewhere.txt"
    bait.write_text("")
    (_isolated["user_dir"] / "community.yml.tmp").symlink_to(bait)
    with _umask(0o002):
        write_community_keys({"auth_token": "new"})
    assert bait.read_text() == "", "the secrets followed a planted symlink"
    assert not path.is_symlink()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert yaml.safe_load(path.read_text()) == {
        "display_name": "Lab", "slack_bot_token": "xoxb-secret", "auth_token": "new"}
    assert not (_isolated["user_dir"] / "community.yml.tmp").exists()


# ── CLI ─────────────────────────────────────────────────────────────────

def _cli(args, input_=None):
    from typer.testing import CliRunner

    from stan.cli import app
    return CliRunner().invoke(app, args, input=input_)


def test_cli_peg_sync_sharing_off_exits_zero(_isolated, relay, share_rows):
    res = _cli(["peg-sync", "--backend", "pg"])
    assert res.exit_code == 0, res.output
    assert "PEG sharing is off" in res.output
    assert relay.calls == []


def test_cli_peg_sync_all_batches_failed_exits_one(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    relay.replies = [(403, {"detail": "This lab name is claimed."})]
    res = _cli(["peg-sync", "--backend", "pg"])
    assert res.exit_code == 1, res.output
    assert "community-claim" in res.output


def test_cli_peg_sync_success_summary(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row(), _row(1, peg_class="unknown")]
    res = _cli(["peg-sync", "--backend", "pg"])
    assert res.exit_code == 0, res.output
    assert "accepted 1" in res.output and "peg_unknown 1" in res.output


def test_cli_peg_sync_unclaimed_name_prints_a_yellow_warning(_isolated, relay, share_rows,
                                                             monkeypatch):
    import stan.cli as stan_cli
    from rich.console import Console

    buf = io.StringIO()
    monkeypatch.setattr(stan_cli, "console",
                        Console(file=buf, force_terminal=True, color_system="standard",
                                width=200))
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True)
    share_rows["rows"] = [_row()]
    res = _cli(["peg-sync", "--backend", "pg"])
    assert res.exit_code == 0, res.output
    assert len(relay.calls) == 1                     # still sent
    out = buf.getvalue()
    line = next((ln for ln in out.splitlines() if "unclaimed" in ln), "")
    assert "\x1b[33m" in line, out                   # yellow
    plain = re.sub(r"\x1b\[[0-9;]*m", "", line)
    assert "'Clogged PeakTail' is unclaimed" in plain
    assert "anyone can claim it" in plain and "stan community-claim" in plain


def test_cli_peg_sync_claimed_name_prints_no_unclaimed_line(_isolated, relay, share_rows):
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", peg_share=True,
               auth_token="tok")
    share_rows["rows"] = [_row()]
    res = _cli(["peg-sync", "--backend", "pg"])
    assert res.exit_code == 0 and "unclaimed" not in res.output


def test_cli_peg_sync_rejects_bad_backend(_isolated, relay, share_rows):
    assert _cli(["peg-sync", "--backend", "mysql"]).exit_code == 2


def test_cli_community_claim_stores_new_token(_isolated, monkeypatch):
    import stan.setup as stan_setup
    path = _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail",
                      auth_token="old", community_submit=True)
    asked: list[str] = []
    monkeypatch.setattr(stan_setup, "_verify_name_ownership",
                        lambda name, reclaim=False: asked.append(name) or "tok-new")
    res = _cli(["community-claim"])
    assert res.exit_code == 0, res.output
    assert asked == ["Clogged PeakTail"]
    assert yaml.safe_load(path.read_text()) == {
        "display_name": "Clogged PeakTail", "auth_token": "tok-new", "community_submit": True}


def test_cli_community_claim_failure_leaves_file_alone(_isolated, monkeypatch):
    import stan.setup as stan_setup
    path = _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", auth_token="old")
    before = path.read_text()
    monkeypatch.setattr(stan_setup, "_verify_name_ownership", lambda name, reclaim=False: None)
    res = _cli(["community-claim"])
    assert res.exit_code == 1
    assert path.read_text() == before


@pytest.mark.parametrize("problem", ["not_a_mapping", "not_yaml", "read_only_dir"])
def test_cli_community_claim_checks_the_file_before_the_relay_rotates(
        _isolated, monkeypatch, problem):
    # The relay retires the old token the moment it issues a new one, so a
    # community.yml that cannot then be written must stop the claim first.
    import stan.setup as stan_setup
    user_dir = _isolated["user_dir"]
    monkeypatch.setenv("STAN_DISPLAY_NAME", "Clogged PeakTail")
    if problem == "not_a_mapping":
        (user_dir / "community.yml").write_text("- display_name\n- auth_token\n")
    elif problem == "not_yaml":
        (user_dir / "community.yml").write_text("display_name: [unclosed\n")
    else:
        if sys.platform == "win32" or os.geteuid() == 0:
            pytest.skip("POSIX directory permissions, not as root")
        _write_cfg(user_dir, display_name="Clogged PeakTail", auth_token="old")
        user_dir.chmod(0o500)
    asked: list[str] = []
    monkeypatch.setattr(stan_setup, "_verify_name_ownership",
                        lambda name, reclaim=False: asked.append(name) or "tok-new")
    try:
        res = _cli(["community-claim"])
    finally:
        user_dir.chmod(0o700)
    assert res.exit_code == 1, res.output
    assert asked == [], "the relay was asked for a token that could not be stored"
    assert "community.yml" in res.output or str(user_dir) in res.output


def test_cli_community_claim_prints_the_token_if_the_write_fails(_isolated, monkeypatch):
    import stan.setup as stan_setup
    _write_cfg(_isolated["user_dir"], display_name="Clogged PeakTail", auth_token="old")
    monkeypatch.setattr(stan_setup, "_verify_name_ownership",
                        lambda name, reclaim=False: "tok-new-Zq9_x")

    tmp = str(_isolated["user_dir"] / "community.yml.tmp")

    def full_disk(*a, **k):
        raise OSError(errno.EDQUOT, "Disk quota exceeded", tmp)

    monkeypatch.setattr(peg_submit, "write_community_keys", full_disk)
    res = _cli(["community-claim"])
    assert res.exit_code == 1
    assert "auth_token: tok-new-Zq9_x" in res.output, "the only working token was lost"
    # Whole, not hard-wrapped at 80 columns: the path is what the user acts on.
    assert "Disk quota exceeded" in res.output and tmp in res.output


def test_cli_community_claim_needs_a_name(_isolated, monkeypatch):
    import stan.setup as stan_setup
    called: list[str] = []
    monkeypatch.setattr(stan_setup, "_verify_name_ownership",
                        lambda name, reclaim=False: called.append(name))
    assert _cli(["community-claim"]).exit_code == 1
    assert called == []


# ── integration with the backend's real blank filter ────────────────────

def test_real_blank_filter_drops_blanks_and_washes():
    """Runs against stan.metrics.peg_trends once the backend has written it."""
    trends = pytest.importorskip("stan.metrics.peg_trends")
    if not hasattr(trends, "is_blank_or_wash"):
        pytest.skip("peg_trends.is_blank_or_wash not written yet")
    rows = [_row(0), _row(1, run_name="Blank_60spd.d"), _row(2, run_name="wash_01.d"),
            _row(3, run_name="QC_blk_3.d")]
    records, skipped = build_peg_records(rows, "1.2.0")
    assert len(records) == 1 and skipped == {"blank_or_wash": 3}
