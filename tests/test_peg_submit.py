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

import hashlib
import importlib
import json
import re
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
