"""Community redesign P3c, in the vendored relay (Space 1.9.0) and the STAN
client (1.2.18): labs counted as facilities, and the token on /api/submit.

Spec: docs/superpowers/specs/2026-09-29-community-redesign-and-precursor-lookup-design.md
(§A.5 the facility id, §A.3 D2, §A.6 P3c); Brett's decision 1 of 2026-10-05:
UC Davis is one facility with an opaque public id, covering Clogged PeakTail
(and its other spellings) and the 127 'Anonymous Lab' rows submitted
2026-04-30T23:29Z..2026-05-01T18:26Z by STAN 0.2.282-0.2.290. Later
'Anonymous Lab' rows stay unattributed and never count as a second lab.

Token: /api/submit checks X-STAN-Auth with _peg_identity, as PEG and
/api/update do. A claimed name with its token is stored name_verified, with
another token refused (403); an unclaimed name, or no token at all (the grace
period), is accepted unverified. Before 1.9.0 any token made the relay call an
undefined helper (500). The client sends its token by default from 1.2.18 and,
against a relay that still answers 500, sends the run again without it (checked
against the real 1.8.0 relay from git history).

Facilities: identity/facilities.json, written by the admin
(scripts/set_facilities.py, scripts/community_facilities.json), read by the
relay with a cache and never written by it. /api/leaderboard rows carry a
public `facility` (the id or ''), never a name. The page's labCount() and the
relay's _page_lab_count() count facilities, a row without one by its lab name,
and never 'Anonymous Lab' as a second lab; the two are checked against each
other, alone and through the TIC summaries. The lab trend never draws a lab's
own facility, under another name, as "other labs".
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tests.test_relay_community_p1 import _benchmark_parquet, _row
from tests.test_relay_community_p2b import _trend_rows
from tests.test_relay_community_tic import (
    SNAP,
    _assert_parity,
    _entry,
    _run,
    _serve,
    _summary,
    _tic_row,
    needs_snapshot,
)
from tests.test_relay_peg import (  # noqa: F401  (fixtures)
    CLAIMS,
    REPO,
    SUBMIT_PAYLOAD,
    _load_module,
    _page,
    _prepare_relay,
    claim_entry,
    client,
    drain,
    hub,
    needs_node,
    queued,
    relay,
    set_claims,
)

FACILITIES = "identity/facilities.json"
UCD = json.loads((REPO / "scripts" / "community_facilities.json").read_text())
TOKEN = "tok-clogged-peaktail-0123456789"

# The live names (2026-10-06): the two names in benchmark_latest.parquet, and
# every claimed name the relay lists at /api/names.
LIVE_ROW_NAMES = ["Clogged PeakTail", "Anonymous Lab"]
LIVE_CLAIMED = ["Clogged PeakTail", "Clogged Peaktail", "CloggedPeakTail", "Oxidized Trypsin", "n"]
# The first and last 'Anonymous Lab' submission of each STAN version in the
# live table (127 rows, all of them in these spans).
LIVE_ANON_SPANS = {
    "0.2.282": ("2026-04-30T23:29:54.621936Z", "2026-04-30T23:58:47.635769Z"),
    "0.2.283": ("2026-05-01T00:29:58.684685Z", "2026-05-01T01:10:22.796594Z"),
    "0.2.288": ("2026-05-01T17:36:42.846129Z", "2026-05-01T17:41:17.878612Z"),
    "0.2.289": ("2026-05-01T17:55:31.210827Z", "2026-05-01T17:56:16.883800Z"),
    "0.2.290": ("2026-05-01T18:25:30.402909Z", "2026-05-01T18:26:10.096906Z"),
}


def _facilities(hub, records) -> None:
    hub.files[FACILITIES] = json.dumps(records).encode()


def _reread(relay) -> None:
    """Make the relay read facilities.json again on its next use."""
    with relay._FACILITIES_LOCK:
        relay._FACILITIES["next"] = 0.0


# ── /api/submit: the token (relay 1.9.0) ─────────────────────────────

def _post(client, relay, token: str | None = None, **over):
    drain(relay)
    headers = {"X-STAN-Auth": token} if token is not None else {}
    r = client.post("/api/submit", json={**SUBMIT_PAYLOAD, **over}, headers=headers)
    stored = None
    if r.status_code == 200:
        (it,) = queued(relay)
        stored = pq.read_table(io.BytesIO(it.data)).to_pylist()[0]
    drain(relay)
    return r, stored


@pytest.mark.parametrize("claimed,token,status,verified", [
    (True, TOKEN, 200, True),           # claimed name, its token: verified
    (True, "someone-elses-token", 403, None),
    (True, None, 200, False),           # no token: accepted in the grace period
    (True, "", 200, False),             # an empty header is no token
    (False, TOKEN, 200, False),         # unclaimed name: accepted, unverified
    (False, None, 200, False),
])
def test_submit_token(client, relay, hub, claimed, token, status, verified):
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, TOKEN)} if claimed else
               {"Someone Else": claim_entry(relay, "other")})
    r, stored = _post(client, relay, token)
    assert r.status_code == status, r.text
    if status == 200:
        assert stored["name_verified"] is verified
    else:
        assert stored is None and "community-claim" in r.json()["detail"]


def test_the_token_bug_is_gone(client, relay, hub):
    """1.8.0 called an undefined _load_claimed_names (NameError, a 500) and
    compared a plain `token` that claims never store (they keep token_hash)."""
    src = (REPO / "hf_space" / "app.py").read_text()
    assert "_load_claimed_names" not in src
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, TOKEN)})
    assert _post(client, relay, TOKEN)[0].status_code == 200


def test_submit_matches_the_claim_by_canonical_name(client, relay, hub):
    set_claims(hub, {"Clogged  PeakTail": claim_entry(relay, TOKEN)})     # stored before canonical keys
    r, stored = _post(client, relay, TOKEN, display_name="Clogged PeakTail​ ")
    assert r.status_code == 200 and stored["name_verified"] is True
    assert stored["display_name"] == "Clogged PeakTail​ "            # stored as sent, as before
    assert _post(client, relay, "wrong", display_name="Clogged PeakTail")[0].status_code == 403


def test_registry_outage(client, relay, hub):
    """With a token the registry must be read, so an outage is 503 ("try
    again"), never a claimed name taken as free; without one nothing is read."""
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, TOKEN)})
    hub.unreachable.add(CLAIMS)
    r, stored = _post(client, relay, TOKEN)
    assert r.status_code == 503 and stored is None
    r, stored = _post(client, relay, None)
    assert r.status_code == 200 and stored["name_verified"] is False


def test_a_verified_token_is_remembered_but_a_new_one_is_checked(client, relay, hub, monkeypatch):
    fetches = []
    real = relay._fetch_claims
    monkeypatch.setattr(relay, "_fetch_claims", lambda: fetches.append(1) or real())
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, TOKEN)})
    for _ in range(3):
        assert _post(client, relay, TOKEN)[1]["name_verified"] is True
    assert len(fetches) == 1                        # one registry read for a run of submissions
    # A re-claim issues a new token: it is checked at once and verified, and
    # a wrong token is still refused (failures are never remembered).
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, "new-token")})
    assert _post(client, relay, "new-token")[1]["name_verified"] is True
    assert _post(client, relay, "wrong")[0].status_code == 403
    assert _post(client, relay, "wrong")[0].status_code == 403
    assert len(fetches) == 4
    # The replaced token stays good only until the memory runs out.
    monkeypatch.setattr(relay, "SUBMIT_VERIFIED_TTL_SEC", 0)
    relay._SUBMIT_VERIFIED.clear()
    assert _post(client, relay, TOKEN)[0].status_code == 403


def test_name_verified_cannot_be_sent_or_patched(client, relay, hub):
    set_claims(hub, {})
    r, stored = _post(client, relay, None, name_verified=True)
    assert r.status_code == 200 and stored["name_verified"] is False
    assert "name_verified" not in relay._UPDATABLE_FIELDS


def test_name_verified_is_a_bool_column_after_the_p3a_columns(client, relay, hub):
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, TOKEN)})
    drain(relay)
    client.post("/api/submit", json=SUBMIT_PAYLOAD, headers={"X-STAN-Auth": TOKEN})
    (it,) = queued(relay)
    schema = pq.read_table(io.BytesIO(it.data)).schema
    assert schema.field("name_verified").type == pa.bool_()
    assert schema.names[-5:] == ["lc_model", "lc_flow", "amount_source", "faims", "name_verified"]


def test_name_verified_is_published_and_old_rows_read_null(client, hub):
    rows = [_row(1, name_verified=True), _row(2, name_verified=False), _row(3, name_verified=None)]
    _serve(hub, rows)
    got = {r["submission_id"]: r["name_verified"] for r in client.get("/api/leaderboard").json()["submissions"]}
    assert got == {"s1": True, "s2": False, "s3": None}


# ── the facility records ─────────────────────────────────────────────

def test_the_uc_davis_records_parse_cleanly(relay):
    fmap, problems = relay._parse_facilities(UCD)
    assert problems == []
    assert set(fmap) == {"f1"}
    f1 = fmap["f1"]
    assert f1.names == frozenset({"clogged peaktail", "cloggedpeaktail"})
    assert f1.anonymous == (datetime(2026, 4, 30, 23, 29, tzinfo=timezone.utc),
                            datetime(2026, 5, 1, 18, 27, tzinfo=timezone.utc))


def test_live_names(relay):
    """Every live name: the claimed spellings of Clogged PeakTail are f1;
    the other claimed names are not; 'Anonymous Lab' is f1 only in its window."""
    fmap, _ = relay._parse_facilities(UCD)
    got = {n: relay._facility_of(n, None, fmap) for n in LIVE_CLAIMED + LIVE_ROW_NAMES}
    assert got == {"Clogged PeakTail": "f1", "Clogged Peaktail": "f1", "CloggedPeakTail": "f1",
                   "Oxidized Trypsin": "", "n": "", "Anonymous Lab": ""}
    for first, last in LIVE_ANON_SPANS.values():
        assert relay._facility_of("Anonymous Lab", first, fmap) == "f1"
        assert relay._facility_of("Anonymous Lab", last, fmap) == "f1"


@pytest.mark.parametrize("name,expected", [
    ("Clogged PeakTail", "f1"), ("clogged peaktail", "f1"), ("CLOGGED PEAKTAIL", "f1"),
    ("  Clogged   PeakTail ", "f1"), ("Clogged PeakTail", "f1"), ("Clogged​PeakTail", "f1"),
    ("Ｃlogged PeakTail", "f1"),            # fullwidth C: NFKC, as claims are matched
    ("cloggedpeaktail", "f1"),
    ("Clogged Peak Tail", ""), ("Clogged PeakTails", ""), ("Clogged PeakTai1", ""),
    ("", ""), (None, ""), (42, ""),
])
def test_name_matching(relay, name, expected):
    fmap, _ = relay._parse_facilities(UCD)
    assert relay._facility_of(name, None, fmap) == expected


@pytest.mark.parametrize("when,expected", [
    ("2026-04-30T23:29:00Z", "f1"),                     # the window's first instant
    ("2026-04-30T23:28:59.999999Z", ""),
    ("2026-05-01T18:26:10.096906Z", "f1"),              # the last live row
    ("2026-05-01T18:27:00Z", "f1"),                     # the window's last instant
    ("2026-05-01T18:27:00.000001Z", ""),
    ("2026-05-01T11:26:10-07:00", "f1"),                # an offset
    ("2026-05-02T09:00:00Z", ""),                       # a later 'Anonymous Lab' row: unknown
    ("2026-04-12T09:00:00Z", ""),
    (datetime(2026, 5, 1, 12, tzinfo=timezone.utc), "f1"),
    (datetime(2026, 5, 1, 12), "f1"),                   # stored without a zone: UTC
    (datetime(2026, 5, 3, 12, tzinfo=timezone.utc), ""),
    (None, ""), ("not a date", ""), ("", ""),
])
def test_anonymous_window(relay, when, expected):
    fmap, _ = relay._parse_facilities(UCD)
    assert relay._facility_of("Anonymous Lab", when, fmap) == expected
    assert relay._facility_of(" anonymous  LAB", when, fmap) == expected


@pytest.mark.parametrize("records,problem,kept", [
    ({"UCDavis": {"names": ["X"]}}, "never a name", {}),
    ({"f0": {"names": ["X"]}}, "never a name", {}),
    ({"f01": {"names": ["X"]}}, "never a name", {}),
    ({"Clogged PeakTail": {"names": ["X"]}}, "never a name", {}),
    ({"f1": ["X"]}, "not a JSON object", {}),
    ({"f1": {"names": "X"}}, "not a list", {"f1": (set(), None)}),
    ({"f1": {"names": ["X", ""]}}, "empty or not text", {"f1": ({"x"}, None)}),
    ({"f1": {"names": ["X", "Anonymous Lab"]}}, "only by anonymous_from", {"f1": ({"x"}, None)}),
    ({"f1": {"names": ["X"], "label": "UC Davis"}}, "unknown field", {"f1": ({"x"}, None)}),
    ({"f1": {"names": ["X"], "anonymous_from": "2026-05-01T00:00:00Z"}}, "without anonymous_until",
     {"f1": ({"x"}, None)}),
    ({"f1": {"names": ["X"], "anonymous_until": "2026-05-01T00:00:00"}}, "time zone", {"f1": ({"x"}, None)}),
    ({"f1": {"names": ["X"], "anonymous_until": "May 1"}}, "time zone", {"f1": ({"x"}, None)}),
    ({"f1": {"names": ["X"], "anonymous_from": "2026-05-02T00:00:00Z", "anonymous_until": "2026-05-01T00:00:00Z"}},
     "after anonymous_until", {"f1": ({"x"}, None)}),
    ({"f1": {}}, "matches no run", {"f1": (set(), None)}),
    ({"f1": {"names": ["X"]}, "f2": {"names": ["x "]}}, "listed under f1, f2", {"f1": ({"x"}, None), "f2": ({"x"}, None)}),
    ([], "not a JSON object", {}),
])
def test_problems_are_reported_and_nothing_is_guessed(relay, records, problem, kept):
    fmap, problems = relay._parse_facilities(records)
    assert any(problem in p for p in problems), problems
    assert {fid: (set(f.names), f.anonymous) for fid, f in fmap.items()} == kept


def test_conflicts_leave_rows_unattributed(relay):
    fmap, problems = relay._parse_facilities({
        "f1": {"names": ["Lab A", "Shared"], "anonymous_from": "2026-05-01T00:00:00Z",
               "anonymous_until": "2026-05-03T00:00:00Z"},
        "f2": {"names": ["Lab B", "shared"], "anonymous_until": "2026-05-02T00:00:00Z"},
    })
    assert any("overlap" in p for p in problems) and any("'shared'" in p for p in problems)
    assert relay._facility_of("Lab A", None, fmap) == "f1"
    assert relay._facility_of("Shared", None, fmap) == ""
    assert relay._facility_of("Anonymous Lab", "2026-04-01T00:00:00Z", fmap) == "f2"   # f2's open start
    assert relay._facility_of("Anonymous Lab", "2026-05-01T12:00:00Z", fmap) == ""     # both windows
    assert relay._facility_of("Anonymous Lab", "2026-05-02T12:00:00Z", fmap) == "f1"


# ── /api/leaderboard carries `facility` ──────────────────────────────

def _lab_rows() -> list[dict]:
    at = lambda s: {"submitted_at": s}  # noqa: E731
    return [
        _row(1, display_name="Clogged PeakTail", **at("2026-09-01T00:00:00Z")),
        _row(2, display_name="Clogged Peaktail", **at("2026-09-01T00:00:01Z")),
        _row(3, display_name="CloggedPeakTail", **at("2026-09-01T00:00:02Z")),
        _row(4, display_name="Anonymous Lab", **at("2026-04-30T23:29:54.621936Z")),
        _row(5, display_name="Anonymous Lab", **at("2026-05-01T18:26:10.096906Z")),
        _row(6, display_name="Anonymous Lab", **at("2026-05-02T09:00:00Z")),       # later: unknown
        _row(7, display_name="n", **at("2026-04-12T00:00:00Z")),
        _row(8, display_name=None, **at("2026-09-01T00:00:00Z")),
    ]


def _served_facilities(client) -> dict:
    return {r["submission_id"]: r["facility"] for r in client.get("/api/leaderboard").json()["submissions"]}


def test_leaderboard_rows_carry_the_facility_id_never_a_name(client, hub):
    _serve(hub, _lab_rows())
    _facilities(hub, UCD)
    assert _served_facilities(client) == {"s1": "f1", "s2": "f1", "s3": "f1", "s4": "f1", "s5": "f1",
                                          "s6": "", "s7": "", "s8": ""}
    body = client.get("/api/leaderboard").text
    assert "anonymous_until" not in body and "anonymous_from" not in body


def test_no_file_means_no_facilities(client, hub):
    _serve(hub, _lab_rows())
    assert set(_served_facilities(client).values()) == {""}


@pytest.mark.parametrize("bad", [b"{not json", b'{"UCDavis": {"names": ["Clogged PeakTail"]}}', b"[]"])
def test_a_bad_file_attributes_nothing_and_breaks_nothing(client, hub, bad, caplog):
    _serve(hub, _lab_rows())
    hub.files[FACILITIES] = bad
    assert set(_served_facilities(client).values()) == {""}
    assert client.get("/api/tic-summary").status_code == 200
    assert any(FACILITIES in m for m in caplog.messages)


def test_the_file_is_cached_and_an_outage_keeps_the_last_copy(client, relay, hub):
    _serve(hub, _lab_rows())
    _facilities(hub, UCD)
    assert _served_facilities(client)["s1"] == "f1"
    _facilities(hub, {"f2": {"names": ["Clogged PeakTail"]}})
    assert _served_facilities(client)["s1"] == "f1"            # cached for FACILITIES_TTL_SEC
    hub.unreachable.add(FACILITIES)
    _reread(relay)
    assert _served_facilities(client)["s1"] == "f1"            # unreachable: the last copy
    assert relay._FACILITIES["next"] - relay.time.monotonic() <= relay.FACILITIES_RETRY_SEC
    hub.unreachable.clear()
    _reread(relay)
    assert _served_facilities(client)["s1"] == "f2"
    del hub.files[FACILITIES]
    _reread(relay)
    assert _served_facilities(client)["s1"] == ""              # removed: no facilities


def test_admin_refresh_rereads_the_file(client, relay, hub, monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    _serve(hub, _lab_rows())
    assert _served_facilities(client)["s1"] == ""
    _facilities(hub, UCD)
    assert client.post("/api/admin/refresh-cache").status_code == 200
    assert _served_facilities(client)["s1"] == "f1"


def test_the_relay_never_writes_the_file(client, relay, hub):
    _serve(hub, _lab_rows())
    _facilities(hub, UCD)
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, TOKEN)})
    client.get("/api/leaderboard")
    client.get("/api/tic-summary")
    _post(client, relay, TOKEN)
    assert not any(u["path"] == FACILITIES for u in hub.uploads)
    assert not any(FACILITIES in c["paths"] for c in hub.commits)


# ── labCount and _page_lab_count: facilities, in lockstep ────────────

def _lab(name, facility=""):
    return {"display_name": name, "facility": facility}


LAB_CASES = {
    "empty": [],
    "one facility, three names": [_lab("Clogged PeakTail", "f1"), _lab("Clogged Peaktail", "f1"),
                                  _lab("CloggedPeakTail", "f1")],
    "facility + its anonymous rows": [_lab("Clogged PeakTail", "f1"), _lab("Anonymous Lab", "f1")],
    "facility + unknown anonymous": [_lab("Clogged PeakTail", "f1"), _lab("Anonymous Lab")],
    "only its anonymous rows": [_lab("Anonymous Lab", "f1")] * 3,
    "only unknown anonymous": [_lab("Anonymous Lab")] * 2,
    "facility + another lab": [_lab("Clogged PeakTail", "f1"), _lab("n")],
    "two facilities": [_lab("A", "f1"), _lab("B", "f2"), _lab("Anonymous Lab", "f2")],
    "no facilities: names, as before": [_lab("Clogged PeakTail"), _lab("Anonymous Lab"), _lab("n"), _lab("N")],
    "a name that looks like an id": [_lab("f1"), _lab("Lab", "f1")],
    "no name": [_lab(None), _lab(""), {"display_name": None}, {}],
    "facility, no name": [_lab(None, "f1"), _lab("Anonymous Lab")],
    "facility null": [{"display_name": "A", "facility": None}, {"display_name": "Anonymous Lab", "facility": None}],
}
LAB_EXPECTED = {
    "empty": 0, "one facility, three names": 1, "facility + its anonymous rows": 1,
    "facility + unknown anonymous": 1, "only its anonymous rows": 1, "only unknown anonymous": 1,
    "facility + another lab": 2, "two facilities": 2, "no facilities: names, as before": 3,
    "a name that looks like an id": 2, "no name": 0, "facility, no name": 1, "facility null": 1,
}


def _old_page_lab_count(rows):
    """labCount() as relay 1.8.0 had it."""
    names = {r.get("display_name") for r in rows if r.get("display_name")}
    anon = "Anonymous Lab" in names
    names.discard("Anonymous Lab")
    return len(names) or (1 if anon else 0)


def test_python_lab_count(relay):
    assert {k: relay._page_lab_count(v) for k, v in LAB_CASES.items()} == LAB_EXPECTED
    # Without facilities it is exactly the 1.8.0 rule.
    for rows in LAB_CASES.values():
        plain = [{**r, "facility": ""} for r in rows]
        assert relay._page_lab_count(plain) == _old_page_lab_count(plain)


@needs_node
def test_page_lab_count_matches_the_relay(client, relay, tmp_path):
    scenario = f"""(() => {{
        const cases = {json.dumps(LAB_CASES)};
        const out = {{}};
        for (const [k, rows] of Object.entries(cases)) out[k] = [labCount(rows), labsSubText(rows)];
        return out; }})()"""
    got = _run(client, tmp_path, scenario)["out"]
    assert {k: v[0] for k, v in got.items()} == {k: relay._page_lab_count(v) for k, v in LAB_CASES.items()}
    sub = {k: v[1] for k, v in got.items()}
    assert sub["one facility, three names"] == "under 3 lab names"
    assert sub["facility + its anonymous rows"] == "under 2 lab names"
    assert sub["facility + unknown anonymous"] == "1 run sent as Anonymous Lab, not counted"
    assert sub["only unknown anonymous"] == ""
    assert sub["facility + another lab"] == ""
    assert sub["no facilities: names, as before"] == "1 run sent as Anonymous Lab, not counted"


def _tic_lab_rows() -> list[dict]:
    """One TIC cohort (HeLa DIA 100 SPD Evosep) holding f1 under three names
    and in its window, a later 'Anonymous Lab' and another lab; a 60 SPD one
    with only the later 'Anonymous Lab'; a nanoLC one with f1 and its
    anonymous rows; a 30 SPD one with two spellings of a lab with no record."""
    rows = [
        _tic_row(1, display_name="Clogged PeakTail"), _tic_row(2, display_name="Clogged Peaktail"),
        _tic_row(3, display_name="CloggedPeakTail"),
        _tic_row(4, display_name="Anonymous Lab", submitted_at="2026-05-01T00:30:00.000000Z"),
        _tic_row(5, display_name="Anonymous Lab", submitted_at="2026-06-01T00:00:00.000000Z"),
        _tic_row(6, display_name="Lab X"), _tic_row(7, display_name="Lab X"),
    ]
    rows += [_tic_row(10 + i, display_name="Anonymous Lab", spd=60, gradient_length_min=21,
                      submitted_at="2026-06-02T00:00:00.000000Z", n_precursors=45000 + i) for i in range(3)]
    rows += [_tic_row(20 + i, display_name=["Clogged PeakTail", "Anonymous Lab"][i % 2], spd=38,
                      lc_system="custom", instrument_model="Orbitrap Exploris 480", instrument_family="Exploris",
                      gradient_length_min=44, submitted_at="2026-05-01T12:00:00.000000Z",
                      n_precursors=22000 + i) for i in range(4)]
    rows += [_tic_row(30 + i, display_name=["Lab Y", "lab y"][i % 2], spd=30, gradient_length_min=44,
                      n_precursors=30000 + i) for i in range(2)]
    return rows


@needs_node
@pytest.mark.parametrize("with_file,labs", [
    (True, {100: 2, 60: 1, 38: 1, 30: 2}),
    (False, {100: 4, 60: 1, 38: 1, 30: 2}),      # names, as in 1.8.0
])
def test_tic_lab_counts_follow_the_facilities_as_the_page_does(client, relay, hub, tmp_path, with_file, labs):
    _serve(hub, _tic_lab_rows())
    if with_file:
        _facilities(hub, UCD)
    _assert_parity(client, relay, tmp_path)      # the page's labCount == the summary's labs, cohort by cohort
    s = _summary(client)
    got = {spd: _entry(s, "hela", "DIA", spd, lc)["labs"]
           for spd, lc in ((100, "evosep"), (60, "evosep"), (38, "nanolc"), (30, "evosep"))}
    assert got == labs


def test_tic_summaries_are_rebuilt_when_the_facilities_change(client, relay, hub, monkeypatch):
    monkeypatch.delenv("ADMIN_SECRET", raising=False)
    _serve(hub, _tic_lab_rows())
    assert _entry(_summary(client), "hela", "DIA", 100, "evosep")["labs"] == 4
    builds = relay._TIC_CACHE["builds"]
    _facilities(hub, UCD)
    client.post("/api/admin/refresh-cache")
    assert _entry(_summary(client), "hela", "DIA", 100, "evosep")["labs"] == 2
    assert relay._TIC_CACHE["builds"] == builds + 1
    client.post("/api/admin/refresh-cache")         # same rows, same records: no rebuild
    _summary(client)
    assert relay._TIC_CACHE["builds"] == builds + 1


@needs_node
@needs_snapshot
def test_snapshot_is_one_facility(client, relay, hub, tmp_path):
    """The 2026-09-29 snapshot: every row is f1 (Clogged PeakTail, and the
    'Anonymous Lab' rows, all inside the window), so every count is 1 lab, as
    the 1.8.0 rule also said; the page and the summaries agree."""
    rows = json.loads((SNAP / "api_leaderboard.json").read_text())["submissions"]
    tic = {t["submission_id"]: t for t in json.loads((SNAP / "api_tic_overlay.json").read_text())["traces"]}
    for r in rows:
        t = tic.get(r["submission_id"])
        r["tic_rt_bins"] = t["tic_rt_bins"] if t else None
        r["tic_intensity"] = t["tic_intensity"] if t else None
    hub.files["benchmark_latest.parquet"] = _benchmark_parquet(rows)
    _facilities(hub, UCD)
    served = client.get("/api/leaderboard").json()["submissions"]
    names = {r["display_name"] for r in served}
    assert names == {"Clogged PeakTail", "Anonymous Lab"}
    assert {r["facility"] for r in served} == {"f1"}
    _assert_parity(client, relay, tmp_path)
    assert {c["labs"] for c in _summary(client)["cohorts"]} == {1}
    got = _run(client, tmp_path, f"""(() => {{ setSubmissions({json.dumps(served)}); renderFilterBar(); updateStats();
        return [els['stat-labs'].textContent, els['stat-labs-label'].textContent, els['stat-labs-sub'].textContent]; }})()""")
    assert got["out"] == ["1", "Contributing facility", "under 2 lab names"]


# ── the page: stats tile and lab trend ───────────────────────────────

@needs_node
@pytest.mark.parametrize("with_file,expected", [
    (True, ["2", "Contributing facilities", "under 5 lab names"]),
    (False, ["4", "Contributing facilities", "1 run sent as Anonymous Lab, not counted"]),
])
def test_stats_tile(client, hub, tmp_path, with_file, expected):
    rows = [_row(i, display_name=n, submitted_at=t) for i, (n, t) in enumerate([
        ("Clogged PeakTail", "2026-09-01T00:00:00Z"), ("Clogged Peaktail", "2026-09-01T00:00:00Z"),
        ("CloggedPeakTail", "2026-09-01T00:00:00Z"), ("Anonymous Lab", "2026-05-01T00:00:00Z"),
        ("Lab X", "2026-09-01T00:00:00Z")])]
    _serve(hub, rows)
    if with_file:
        _facilities(hub, UCD)
    served = client.get("/api/leaderboard").json()["submissions"]
    got = _run(client, tmp_path, f"""(() => {{ setSubmissions({json.dumps(served)}); renderFilterBar(); updateStats();
        return [els['stat-labs'].textContent, els['stat-labs-label'].textContent, els['stat-labs-sub'].textContent]; }})()""")
    assert got["out"] == expected


def test_stats_tile_markup_and_glossary(client):
    html = _page(client)
    assert '<div class="label" id="stat-labs-label">Contributing facilities</div>' in html
    assert "<div><b>Lab</b>a contributing facility." in html
    assert "A facility code (such as <code>f1</code>, never a name)" in html


def _trend_with(facilities: dict) -> list[dict]:
    rows = _trend_rows()
    for r in rows:
        r["facility"] = facilities.get(r["display_name"], "")
    return rows


_TREND = """(() => {{
    setSubmissions({rows}); renderFilterBar();
    const state = () => {{ const p = plots.filter(p => p.id === 'chart-lab-trend').slice(-1)[0];
        return {{ names: p.traces.map(t => t.name), note: els['lab-trend-note'].innerHTML, sum: els['lab-trend-sum'].innerHTML }}; }};
    renderLabTrend(); const a = state();
    pickTrend('lab', 'Lab B'); const b = state();
    pickTrend('lab', 'Anonymous Lab'); const anon = state();
    return {{ a, b, anon }}; }})()"""


@needs_node
def test_lab_trend_never_draws_the_labs_own_facility_as_other_labs(client, tmp_path):
    """_trend_rows(): Lab A 32 runs in the cohort, Lab B 7, Anonymous Lab 6."""
    # Without facilities, as in 1.8.0: Anonymous Lab's reference is both named labs.
    plain = _run(client, tmp_path, _TREND.format(rows=json.dumps(_trend_with({}))))["out"]
    assert "Other labs, 10–90th pct (39 runs · 2 labs)" in plain["anon"]["names"]
    # Lab A's anonymous rows are its own (f1): picking Anonymous Lab leaves only Lab B.
    got = _run(client, tmp_path, _TREND.format(rows=json.dumps(_trend_with(
        {"Lab A": "f1", "Anonymous Lab": "f1"}))))["out"]
    assert "Other labs, 10–90th pct (7 runs · 1 lab)" in got["anon"]["names"]
    assert "Other labs, 10–90th pct (7 runs · 1 lab)" in got["a"]["names"]         # Lab A: unchanged
    assert "Other labs, 10–90th pct (32 runs · 1 lab)" in got["b"]["names"]        # Lab B: unchanged
    assert "the cohort holds 45 runs · 2 labs" in got["a"]["sum"]
    # Lab A and Lab B one facility: neither has another lab to compare with.
    one = _run(client, tmp_path, _TREND.format(rows=json.dumps(_trend_with(
        {"Lab A": "f1", "Lab B": "f1", "Anonymous Lab": "f1"}))))["out"]
    for k in ("a", "b", "anon"):
        assert not any(n.startswith("Other labs") for n in one[k]["names"]), k
        assert "No other lab in this cohort yet." in one[k]["note"], k
    assert "the cohort holds 45 runs · 1 lab" in one["a"]["sum"]


# ── STAN client: sends its token by default, safe against relay 1.8.0 ──

RELAY_1_8_0 = "4ef0f59"      # main when relay 1.8.0 was deployed (Space app.py sha256 98133136...)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _route(submit_mod, monkeypatch, app, calls: list) -> None:
    """Send the client's urlopen to ``app`` in-process, as the network would."""
    tc = TestClient(app, raise_server_exceptions=False)

    def fake_urlopen(req, timeout=30):
        headers = dict(req.header_items())
        calls.append({k.lower(): v for k, v in headers.items()})
        r = tc.post("/api/submit", content=req.data, headers=headers)
        if r.status_code >= 400:
            raise urllib.error.HTTPError(req.full_url, r.status_code, r.reason_phrase, r.headers,
                                         io.BytesIO(r.content))
        return _Resp(r.content)

    monkeypatch.setattr(submit_mod.urllib.request, "urlopen", fake_urlopen)


def _client_run(i: int = 0) -> dict:
    from tests.test_submission_readiness import READY_DIA
    # The TIC as attach_tic() leaves it on a run: lists, which the relay requires.
    return dict(READY_DIA, id=f"r{i}", instrument="Orbitrap Exploris 480",
                tic_rt_bins=[0.1, 0.2, 0.3], tic_intensity=[1.0, 2.0, 1.5],
                run_name=f"Ex051026_HeL50_30m_{i}.raw", run_date="2026-10-05T00:00:00Z",
                spd=38, amount_ng=50.0, vendor="thermo", column_vendor="IonOpticks", column_model="Aurora")


@pytest.fixture
def stan_submit(monkeypatch):
    """stan.community.submit with no local DB; ``config(**cfg)`` sets community.yml."""
    from stan.community import submit
    monkeypatch.setattr(submit, "_TOKEN_REFUSED_UNTIL", 0.0)
    monkeypatch.setattr(submit, "mark_submitted", lambda *a, **k: None)
    monkeypatch.delenv("STAN_SEND_AUTH", raising=False)

    def config(**cfg):
        monkeypatch.setattr(submit, "load_community", lambda: {"display_name": "Clogged PeakTail", **cfg})
    return SimpleNamespace(mod=submit, config=config)


def _send(s, i: int = 0) -> dict:
    return s.mod.submit_to_benchmark(_client_run(i), spd=38, gradient_length_min=30, amount_ng=50.0,
                                     diann_version="2.3.0")


def test_client_sends_its_token_and_the_row_is_verified(stan_submit, relay, hub, monkeypatch):
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, TOKEN)})
    calls: list = []
    _route(stan_submit.mod, monkeypatch, relay.app, calls)
    stan_submit.config(auth_token=TOKEN)
    drain(relay)
    assert _send(stan_submit)["status"] == "submitted"
    assert [c.get("x-stan-auth") for c in calls] == [TOKEN]
    (it,) = queued(relay)
    assert pq.read_table(io.BytesIO(it.data)).to_pylist()[0]["name_verified"] is True


def test_client_against_relay_1_8_0_resends_without_the_token(stan_submit, hub, monkeypatch, tmp_path):
    """The real 1.8.0 relay, from git history: a token makes it fail (500);
    the client sends the run again without it, once, and leaves the token
    off for the rest of the run instead of failing every submission."""
    try:
        src = subprocess.run(["git", "-C", str(REPO), "show", f"{RELAY_1_8_0}:hf_space/app.py"],
                             capture_output=True, text=True, timeout=60, check=True).stdout
    except (subprocess.SubprocessError, OSError):
        pytest.skip(f"relay 1.8.0 ({RELAY_1_8_0}) not in git history")
    assert 'SPACE_VERSION = "1.8.0"' in src and "_load_claimed_names()" in src
    path = tmp_path / "relay_1_8_0.py"
    path.write_text(src)
    old = _load_module(path, f"relay_1_8_0_{tmp_path.name}")
    try:
        _prepare_relay(old, monkeypatch)
        set_claims(hub, {"Clogged PeakTail": claim_entry(old, TOKEN)})
        calls: list = []
        _route(stan_submit.mod, monkeypatch, old.app, calls)
        stan_submit.config(auth_token=TOKEN)
        assert _send(stan_submit, 1)["status"] == "submitted"
        assert [c.get("x-stan-auth") for c in calls] == [TOKEN, None]
        assert _send(stan_submit, 2)["status"] == "submitted"
        assert [c.get("x-stan-auth") for c in calls] == [TOKEN, None, None]   # one extra request in all
        assert len(old._SUBMIT_QUEUE.queue) == 2
        # an hour on, the token is tried again
        monkeypatch.setattr(stan_submit.mod, "_TOKEN_REFUSED_UNTIL", 0.0)
        assert _send(stan_submit, 3)["status"] == "submitted"
        assert [c.get("x-stan-auth") for c in calls][3:] == [TOKEN, None]
    finally:
        sys.modules.pop(old.__name__, None)


def test_client_wrong_token_is_an_error_and_not_resent(stan_submit, relay, hub, monkeypatch):
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, "the-real-token")})
    calls: list = []
    _route(stan_submit.mod, monkeypatch, relay.app, calls)
    stan_submit.config(auth_token=TOKEN)
    with pytest.raises(RuntimeError, match="stan community-claim"):
        _send(stan_submit)
    assert [c.get("x-stan-auth") for c in calls] == [TOKEN]
    assert queued(relay) == []


@pytest.mark.parametrize("cfg,env,sent", [
    ({}, None, None),                              # no claim: no token
    ({"auth_token": ""}, None, None),
    ({"auth_token": TOKEN}, "0", None),            # STAN_SEND_AUTH=0 opts out
    ({"auth_token": TOKEN}, "off", None),
    ({"auth_token": TOKEN}, "1", TOKEN),
    ({"auth_token": f"  {TOKEN}\n"}, None, TOKEN),
])
def test_client_token_choice(stan_submit, relay, hub, monkeypatch, cfg, env, sent):
    set_claims(hub, {"Clogged PeakTail": claim_entry(relay, TOKEN)})
    if env is not None:
        monkeypatch.setenv("STAN_SEND_AUTH", env)
    calls: list = []
    _route(stan_submit.mod, monkeypatch, relay.app, calls)
    stan_submit.config(**cfg)
    _send(stan_submit)
    assert [c.get("x-stan-auth") for c in calls] == [sent]


@pytest.mark.parametrize("status,body,error", [
    (500, b'{"detail": "HF_TOKEN not configured on Space"}', RuntimeError),   # the relay's own 500
    (503, b'{"detail": "Lab-name registry unavailable. Try again shortly."}', RuntimeError),
    (409, b'{"detail": "Duplicate submission: fingerprint x already exists. '
          b'Existing submission_id: 12345678-1234-5678-1234-567812345678."}', "duplicate"),
])
def test_client_only_an_unhandled_500_drops_the_token(stan_submit, monkeypatch, status, body, error):
    calls: list = []

    def fake_urlopen(req, timeout=30):
        calls.append(req.get_header("X-stan-auth"))
        raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(body))

    monkeypatch.setattr(stan_submit.mod.urllib.request, "urlopen", fake_urlopen)
    stan_submit.config(auth_token=TOKEN)
    expected = stan_submit.mod.DuplicateSubmission if error == "duplicate" else error
    with pytest.raises(expected):
        _send(stan_submit)
    assert calls == [TOKEN]
    assert stan_submit.mod._TOKEN_REFUSED_UNTIL == 0.0


@pytest.mark.parametrize("code,body,expected", [
    (500, "Internal Server Error", True), (500, "", True), (500, "<html>502</html>", True),
    (500, '{"detail": "x"}', False), (500, '["x"]', True), (502, "Bad Gateway", False),
    (403, "Internal Server Error", False),
])
def test_unhandled_server_error(stan_submit, code, body, expected):
    assert stan_submit.mod._unhandled_server_error(code, body) is expected


# ── the admin script ─────────────────────────────────────────────────

def _script():
    spec = importlib.util.spec_from_file_location("set_facilities_under_test", REPO / "scripts" / "set_facilities.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_dry_run_uploads_nothing_and_reports(hub, tmp_path, caplog):
    script = _script()
    rows = [_row(i, display_name="Clogged PeakTail", submitted_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
                 stan_version="1.2.17") for i in range(3)]
    rows += [_row(10, display_name="Anonymous Lab", submitted_at=datetime(2026, 5, 1, 12, tzinfo=timezone.utc),
                  stan_version="0.2.288"),
             _row(11, display_name="Anonymous Lab", submitted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
                  stan_version="1.0.0")]
    hub.files["benchmark_latest.parquet"] = _benchmark_parquet(rows)
    caplog.set_level("INFO")
    assert script.main([]) == 0
    assert hub.uploads == [] and FACILITIES not in hub.files
    text = "\n".join(caplog.messages)
    assert "Dry run: nothing uploaded" in text and "has no identity/facilities.json yet" in text
    assert '"anonymous window": 1' in text and "\"name 'Clogged PeakTail'\": 3" in text
    assert '"anonymous_unattributed": 1' in text and '"0.2.288": 1' in text


def test_script_uploads_with_yes_and_only_a_clean_file(hub, tmp_path, caplog):
    script = _script()
    hub.files["benchmark_latest.parquet"] = _benchmark_parquet([_row(1)])
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"UCDavis": {"names": ["Clogged PeakTail"]}}))
    assert script.main([str(bad), "--yes", "--no-live"]) == 1
    assert hub.uploads == []
    assert script.main(["--yes", "--no-live"]) == 0
    (up,) = hub.uploads
    assert up["path"] == FACILITIES and json.loads(up["data"]) == UCD
    assert up["data"] == (json.dumps(UCD, indent=2) + "\n").encode()
    assert script.main(["--yes", "--no-live"]) == 0          # unchanged: nothing to upload
    assert len(hub.uploads) == 1


def test_script_refuses_when_the_hub_cannot_be_reached(hub):
    script = _script()
    hub.unreachable.add(FACILITIES)
    with pytest.raises(Exception):
        script.main(["--yes", "--no-live"])
    assert hub.uploads == []


def test_the_committed_records_are_brett_s_decision():
    assert UCD == {"f1": {"names": ["Clogged PeakTail", "Clogged Peaktail", "CloggedPeakTail"],
                          "anonymous_from": "2026-04-30T23:29:00Z",
                          "anonymous_until": "2026-05-01T18:27:00Z"}}
    first = min(a for a, _ in LIVE_ANON_SPANS.values())
    last = max(b for _, b in LIVE_ANON_SPANS.values())
    lo = datetime.fromisoformat(UCD["f1"]["anonymous_from"].replace("Z", "+00:00"))
    hi = datetime.fromisoformat(UCD["f1"]["anonymous_until"].replace("Z", "+00:00"))
    assert lo <= datetime.fromisoformat(first.replace("Z", "+00:00")) and lo > datetime.fromisoformat(
        first.replace("Z", "+00:00")) - timedelta(minutes=1)
    assert datetime.fromisoformat(last.replace("Z", "+00:00")) <= hi < datetime.fromisoformat(
        last.replace("Z", "+00:00")) + timedelta(minutes=1)
