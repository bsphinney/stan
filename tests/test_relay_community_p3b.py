"""Community redesign P3b, in the vendored relay (Space 1.8.0): the amount and
FAIMS check at read time.

Spec: docs/superpowers/specs/2026-09-29-community-redesign-and-precursor-lookup-design.md
(§A.3 B4 and D8, §A.6 P3b); Brett's decision 3 of 2026-10-05: hold back every
run whose file name states a different amount than the stored one, and mark
FAIMS from the file name. Nothing stored changes.

Server side: /api/leaderboard (and the TIC summaries built from the same
rows) carries three fields worked out from the private file name:
amount_check ('mismatch' or ''), faims (stored, else true from the
file-name hint, else null) and faims_source ('stored', 'filename' or ''). The amount
parser is a copy of stan/community/amount.py, and one fixture list runs
through both copies. No response carries a file name or a piece of one.

Page side, run in node against the page's own script (the harness of
tests/test_relay_community_tic.py): isHeldBack() holds back an unconfirmed
amount, and the Python port (_page_held_back) agrees, so TIC counts never
exceed the page's; the stats row counts "amount unconfirmed" apart from the
>5,000 ng hold-back; FAIMS runs form their own cohorts, titled "· FAIMS",
with no new filter; the lookup compares with runs acquired without FAIMS
and says so; the submissions table marks assumed amounts and lists the
held-back runs; an unconfirmed amount never decides which duplicate copy is
kept (the five real cases of the snapshot); a missing amount is "amount not
recorded" with an open-circle point, never a stand-in 50 ng.
On the 2026-09-29 snapshot: 71 rows unconfirmed (69 runs once copies are
removed), 15 marked FAIMS.
"""

from __future__ import annotations

import io
import json
import re

import polars as pl
import pytest

from stan.community import amount as client_amount
from tests.test_relay_community_p1 import _benchmark_parquet, _row
from tests.test_relay_community_tic import (
    SNAP,
    _assert_parity,
    _run,
    _serve,
    _summary,
    _tic_row,
    needs_snapshot,
)
from tests.test_relay_peg import _page, client, hub, needs_node, relay  # noqa: F401  (fixtures)

# ── the amount parser: one fixture list, both copies ─────────────────

# (file name, stored amount in ng). Real UC Davis names from the 2026-09-29
# snapshot first, then the parser's edge cases.
AMOUNT_FIXTURES = [
    ("FL271022_FaimHe1ug_CV4680microDia-w6_120m_3.raw", 50.0),
    ("FL060526_qCHeL1ug.raw", 50.0),
    ("FL151223_He1000ng-W22_35m.raw", 50.0),
    ("April10_2024_HeL50ug_100spdNOWETT_S1-A6_1_4817.d", 50.0),
    ("Ex260624_HeL50ug_90m_4.raw", 50.0),
    ("Hela100ng_in20ul-OT2_S3-B1_1_17688.d", 50.0),
    ("Hela100ng_in20ul-OT2_S3-B1_1_17688.d", 100.0),
    ("evosep_40ng_hela_S2-B2_1_16718.d", 50.0),
    ("evosep_40ng_hela_S2-B2_1_16718.d", 40.0),
    ("6mar25_doraHel0.45ug-Dia_60spd_protDigStor1wk_S4-A4_1_11746.d", 50.0),
    ("6mar25_doraHel0.45ug-Dia_60spd_protDigStor1wk_S4-A4_1_11746.d", 450.0),
    ("FL030921_HeLa100ug_DIASpcNwin46_PeSe90m_2.raw", 100000.0),
    ("k562100ng_py5_Slot1-50_1_4551.d", 562100.0),
    ("FL-1MaiMuncitoresc_HeL50_90m.raw", 50.0),
    ("FL20170223_Hela4-cntrl.raw", 50.0),
    ("Ex041123_HeLa50ng-DiaW45_4ian90m_2ugli.raw", 50.0),
    ("Ex150421_HeLa50ng_FaimsCV-60UnivPep_peS9apr_90m1.raw", 50.0),
    ("FL120621_HeLa100ngDIASpcNwin46_90mGood.raw", 50.0),
    ("HeL50ngDia.raw", 50.0),
    ("HeLa_1µg_x.raw", 1000.0),           # µ, MICRO SIGN
    ("HeLa_1μg_x.raw", 50.0),             # μ, GREEK SMALL LETTER MU
    ("HeLa_0.5-ug_x.raw", 500.0),
    ("HeLa_250 ng.raw", 250.0),
    ("HeLa_2mcg.raw", 2000.0),
    ("HeLa50NG.raw", 50.0),
    ("HeLa50ng_then_100ng.raw", 50.0),         # two amounts
    ("HeLa50ng_rep_50ng.raw", 50.0),           # one amount, twice
    ("HeLa5ug.raw", 5000.0),
    ("HeLa5ug.raw", 50.0),
    ("HeLa50ng.raw", 50.04),                   # within the 0.1% tolerance
    ("HeLa50ng.raw", 50.1),
    ("HeLa50ng.raw", None),
    ("HeLa50ng.raw", 0.0),
    ("1.5ug.raw", 1500.0),
    ("x1.2.3ng.raw", 50.0),
    ("", 50.0),
    (None, 50.0),
]


def test_amount_parser_is_the_clients(relay):
    """The relay cannot import stan: its parser is a copy, kept identical."""
    assert relay._AMOUNT_RE.pattern == client_amount._AMOUNT_RE.pattern
    assert relay._AMOUNT_RE.flags == client_amount._AMOUNT_RE.flags
    assert relay._AMOUNT_UNIT_TO_NG == client_amount._UNIT_TO_NG
    assert relay._AMOUNT_MAX_PLAUSIBLE_NG == client_amount.MAX_PLAUSIBLE_NG
    for name, stored in AMOUNT_FIXTURES:
        assert relay._amounts_in(name or "") == client_amount._amounts_in(name or ""), name
        assert relay._amount_parse_ng(name) == client_amount.parse_amount_ng(name), name
        assert relay._amount_problem(name, stored) == client_amount.amount_problem(name, stored), (name, stored)


@pytest.mark.parametrize("name,stored,check", [
    ("FL271022_FaimHe1ug_CV4680microDia-w6_120m_3.raw", 50.0, "mismatch"),     # 1 µg FAIMS stored as 50 ng
    ("April10_2024_HeL50ug_100spdNOWETT_S1-A6_1_4817.d", 50.0, "mismatch"),   # "50 µg", a unit typo
    ("Hela100ng_in20ul-OT2_S3-B1_1_17688.d", 50.0, "mismatch"),
    ("Hela100ng_in20ul-OT2_S3-B1_1_17688.d", 100.0, ""),
    ("HeLa50ng_then_100ng.raw", 50.0, "mismatch"),                            # two amounts
    ("FL-1MaiMuncitoresc_HeL50_90m.raw", 50.0, ""),                           # no unit, no amount
    ("Ex041123_HeLa50ng-DiaW45_4ian90m_2ugli.raw", 50.0, ""),
    # Stored above 5,000 ng: isHeldBack() holds it back as a unit error on
    # its own, and the stats row counts it there, not as unconfirmed.
    ("FL030921_HeLa100ug_DIASpcNwin46_PeSe90m_2.raw", 100000.0, ""),
    ("k562100ng_py5_Slot1-50_1_4551.d", 562100.0, ""),
    ("HeLa5ug.raw", 5000.0, ""),
    ("HeLa50ng.raw", 5000.0, "mismatch"),
    # Nothing stored: the page already says "amount not recorded".
    ("HeLa50ng.raw", None, ""),
    ("HeLa50ng.raw", 0.0, ""),
    ("HeLa50ng.raw", float("nan"), ""),
    ("HeLa50ng.raw", "50", ""),                # read as the page reads it (+v)
    ("", 50.0, ""),
    (None, 50.0, ""),
])
def test_amount_check(relay, name, stored, check):
    assert relay._amount_check(name, stored) == check


FAIMS_YES = [
    "FL271022_FaimHe1ug_CV4680microDia-w6_120m_3.raw",        # UC Davis Lumos (13 on the snapshot)
    "FL181022_FaimHe1ug_CV-50ourDiaW46_120m_1.raw",
    "Ex150421_HeLa50ng_FaimsCV-60UnivPep_peS9apr_90m1.raw",   # UC Davis Exploris 480 (2)
    "FAIMS_CV45_HeLa.raw", "hela_faims-cv50.raw", "Faim.raw", "x-FAIMS.raw", "FAIMSCV",
    "HeLa_no_FAIMS_vs_FAIMS.raw",                             # one token is not negated
    # joined CamelCase after a letter, with a capital F, and FAIMSpro
    "HeLaFAIMS_CV45.raw", "200ngFAIMS.raw", "DIAFAIMS.raw", "withFAIMS.raw", "HeLaFaims.raw",
    "FAIMSpro.raw", "HeLa_FAIMSpro_CV45.raw", "HeLa_FAIMSPro.raw",
    "HeLa50faims.raw",                                         # after a digit: its own token
    "nanoFAIMS.raw", "HeLanoFAIMS.raw",                        # lower-case "no" inside a word
]
FAIMS_NO = [
    "Ex240625_HeL50_masCalButFailSystCal_30m_2good.raw",      # "Fail" (6 on the snapshot)
    "Faimous_run.raw", "Faimsx.raw", "FAIMSproduct.raw",      # a longer word
    "helafaims.raw", "HeLafaims.raw",                         # lower-case after a letter: part of a word
    "HeLa_no_FAIMS.raw", "HeLa_noFAIMS.raw", "noFAIMS.raw", "HeLa_non-FAIMS.raw", "nonFAIMS.raw",
    "HeLa_wo_faims.raw", "woFAIMS.raw", "HeLa_without FAIMS.raw", "withoutFAIMS.raw",
    "HeLa_FAIMS_off.raw", "HeLa_FAIMS-OFF.raw", "HeLa_FAIMSoff.raw", "HeLa_FaimSoff.raw",
    # CamelCase negation after a letter, capital N or W
    "HeLaNoFAIMS.raw", "HeLaNo_FAIMS.raw", "HeLaNonFAIMS.raw", "HeLaWoFAIMS.raw",
    "HeLaWithoutFAIMS.raw", "DIANoFAIMS.raw",
    "FL271022_HeLa50ng_CV4680microDia-w6_120m_3.raw",         # a CV alone is not FAIMS
    "", None,
]


def test_faims_from_the_file_name(relay):
    for name in FAIMS_YES:
        assert relay._faims_from_name(name, "Orbitrap Fusion Lumos", "Lumos") is True, name
    for name in FAIMS_NO:
        assert relay._faims_from_name(name, "Orbitrap Fusion Lumos", "Lumos") is False, name
    # A timsTOF has no FAIMS interface.
    assert relay._faims_from_name("HeLa_FAIMS.d", "timsTOF HT", "timsTOF") is False
    assert relay._faims_from_name("HeLa_FAIMS.d", None, "timsTOF") is False


def test_effective_faims_prefers_the_stored_value(relay):
    name = "FL271022_FaimHe1ug_CV4680microDia-w6_120m_3.raw"
    assert relay._effective_faims(True, "x.raw") == (True, "stored")
    assert relay._effective_faims(False, name) == (False, "stored")     # recorded off wins over the name
    assert relay._effective_faims(None, name) == (True, "filename")
    assert relay._effective_faims(None, "x.raw") == (None, "")             # nothing known: null
    assert relay._effective_faims("maybe", name) == (True, "filename")  # not clearly either: read the name


# ── server: /api/leaderboard ─────────────────────────────────────────

def _p3b_rows() -> list[dict]:
    """Five Lumos 9 SPD runs, one of them 1 µg FAIMS stored as 50 ng (the
    four that topped the DIA table looked like this); an Exploris FAIMS run
    whose amount agrees; a recorded FAIMS off; and a stored 100,000 ng."""
    lumos = dict(instrument_family="Lumos", instrument_model="Orbitrap Fusion Lumos", spd=9, lc_system="custom",
                 gradient_length_min=128, cohort_id="Lumos_9spd_low")
    rows = [_tic_row(i, run_name=f"FL0{i}0526_HeLa50ng_120m_{i}.raw", n_precursors=40000 + 100 * i, **lumos)
            for i in range(1, 5)]
    rows.append(_tic_row(5, run_name="FL271022_FaimHe1ug_CV4680microDia-w6_120m_3.raw", n_precursors=84857, **lumos))
    rows.append(_tic_row(6, run_name="Ex150421_HeLa50ng_FaimsCV-60UnivPep_peS9apr_90m1.raw", spd=12, lc_system="custom",
                         instrument_family="Exploris", instrument_model="Orbitrap Exploris 480", n_precursors=11329,
                         gradient_length_min=88))
    rows.append(_tic_row(7, run_name="Ex150421_HeLa50ng_FaimsCV-60_90m2.raw", spd=12, lc_system="custom", faims=False,
                         instrument_family="Exploris", instrument_model="Orbitrap Exploris 480", n_precursors=11400,
                         gradient_length_min=88))
    rows.append(_tic_row(8, run_name="FL030921_HeLa100ug_DIASpcNwin46_PeSe90m_2.raw", amount_ng=100000.0,
                         n_precursors=30000, **lumos))
    return rows


def _serve_frame(hub, rows: list[dict]) -> None:
    """Serve rows whose columns differ (faims only on some), as the
    consolidated table mixes rows from before and after 1.7.0."""
    frame = pl.concat([pl.from_dicts([r], infer_schema_length=None) for r in rows], how="diagonal_relaxed")
    buf = io.BytesIO()
    frame.write_parquet(buf)
    hub.files["benchmark_latest.parquet"] = buf.getvalue()


def test_leaderboard_serves_the_derived_fields(client, hub):
    _serve_frame(hub, _p3b_rows())
    rows = {r["submission_id"]: r for r in client.get("/api/leaderboard").json()["submissions"]}
    assert rows["s5"]["amount_check"] == "mismatch" and rows["s5"]["faims"] is True and rows["s5"]["faims_source"] == "filename"
    assert rows["s6"]["amount_check"] == "" and rows["s6"]["faims"] is True and rows["s6"]["faims_source"] == "filename"
    assert rows["s7"]["faims"] is False and rows["s7"]["faims_source"] == "stored"
    assert rows["s8"]["amount_check"] == "" and rows["s8"]["amount_ng"] == 100000.0   # held back as a unit error instead
    assert all(rows[f"s{i}"]["amount_check"] == "" and rows[f"s{i}"]["faims"] is None
               and rows[f"s{i}"]["faims_source"] == "" for i in range(1, 5))         # nothing known: null
    assert all("run_name" not in r and "fingerprint" not in r for r in rows.values())


def test_leaderboard_without_file_names_or_faims_column(client, hub):
    """STAN_STRIP_RUN_NAME rows (no name) and tables written before 1.7.0 (no
    faims column) read as not recorded: nothing held back, FAIMS null."""
    rows = [_tic_row(i) for i in range(3)]
    for r in rows:
        r.pop("run_name")
    hub.files["benchmark_latest.parquet"] = _benchmark_parquet(rows)
    got = client.get("/api/leaderboard").json()["submissions"]
    assert [(r["amount_check"], r["faims"], r["faims_source"]) for r in got] == [("", None, "")] * 3


# ── privacy: no response carries a file name or a piece of one ───────

PRIVATE_NAMES = [
    "Zq7Wx_FaimHe1ug_Kv4680wQ_120m_3.raw",
    "Wy2jB_HeL50ug_QqTvRspd_S1-A6_9.d",
    "Pp4rG_HeLa50ng_FaimsCV-60Uu_90mK.raw",
    "Jh8tN_Hela100ng_in20ulOTx_S3-B1.d",
    "Mv3cR_HeLa50ng_stilcrapKerry_MK.raw",
]
MIN_PIECE = 6   # every substring this long or longer


def _pieces(name: str) -> set[str]:
    """Every 6-character window: any longer piece contains one."""
    return {name[i:i + MIN_PIECE] for i in range(len(name) - MIN_PIECE + 1)}


def _private_rows() -> list[dict]:
    rows = []
    for i, name in enumerate(PRIVATE_NAMES * 2):
        rows.append(_tic_row(i, run_name=name, fingerprint=f"feedfacecafe{i:04d}", n_precursors=40000 + i,
                             instrument_family="Lumos", instrument_model="Orbitrap Fusion Lumos", lc_system="custom",
                             spd=9 if i % 2 else 12, gradient_length_min=128 if i % 2 else 88))
    return rows


def test_no_response_carries_a_file_name_or_a_piece_of_one(client, hub):
    """Every public GET over the benchmark rows, including the TIC summaries
    and each cohort's traces, after the P3b fields are worked out from the
    names: no substring of 6 or more characters of any name appears."""
    _serve(hub, _private_rows())
    hub.files["cohort_stats/cohort_percentiles_latest.json"] = json.dumps(
        {"timsTOF_100spd_low": {"run_name": PRIVATE_NAMES}}).encode()
    bodies = {}
    for path in ("/", "/api/leaderboard", "/api/tic-summary", "/api/tic-overlay", "/api/cohorts",
                 "/api/cohorts/timsTOF_100spd_low/tic", "/api/version"):
        r = client.get(path)
        assert r.status_code == 200, path
        bodies[path] = r.text
    for c in json.loads(bodies["/api/tic-summary"])["cohorts"]:
        p = dict(sample=c["s"], mode=c["t"], spd=c["spd"], lc=c["lc"])
        r = client.get("/api/tic-traces", params=p)
        assert r.status_code == 200, p
        bodies[f"/api/tic-traces {p}"] = r.text
    lb = json.loads(bodies["/api/leaderboard"])["submissions"]
    assert sum(r["amount_check"] == "mismatch" for r in lb) == 6 and sum(r["faims"] is True for r in lb) == 4   # the check ran
    pieces = set().union(*(_pieces(n) for n in PRIVATE_NAMES))
    for path, body in bodies.items():
        hits = sorted(p for p in pieces if p in body)
        assert not hits, (path, hits[:5])


# ── the Python port holds back what the page holds back ──────────────

# The five acquisitions of the 2026-09-29 snapshot whose copies disagree:
# an older 0.2.282/0.2.283 "Anonymous Lab" seed copy that stores the amount
# the file name states, with an identified-ion TIC, another run length and SPD
# and no LC, and a 0.2.376 Clogged PeakTail copy stored as 50 ng (two for
# FL120621, under two names). Values as stored: (file name, model, family,
# precursors, peptides, proteins, run date, (amount, SPD, run length) of the
# seed copy, (SPD, LC, run length) of the 0.376 copy).
SEED_CHAINS = [
    ("Hela100ng_in20ul-OT2_S3-A1_1_17687.d", "timsTOF HT", "timsTOF", 41555, 37533, 4625,
     "2025-09-23T22:21:11+00:00", (100.0, 100, 13), (100, "evosep", 11)),
    ("evosep_40ng_hela_S2-A2_1_16723.d", "timsTOF HT", "timsTOF", 35329, 32440, 4235,
     "2025-09-04T20:45:31+00:00", (40.0, 100, 13), (100, "evosep", 11)),
    ("FL120621_HeLa100ng_DIASpcNwin46_90m.raw", "Orbitrap Fusion Lumos", "Lumos", 32320, 29325, 4563,
     "2021-06-13T03:58:24+00:00", (100.0, 18, 72), (12, "custom", 88)),
    ("FL010719_Hela200ng160m.raw", "Orbitrap Fusion Lumos", "Lumos", 29162, 26917, 3993,
     "2019-07-02T07:32:18.582017+00:00", (200.0, 10, 130), (7, "custom", 164)),
    ("260121_HeLa100ng_EasyCol_60m_1.raw", "Orbitrap Exploris 480", "Exploris", 11441, 11219, 2609,
     "2021-03-19T20:49:16.241820+00:00", (100.0, 30, 43), (19, "custom", 88)),
]


def _seed_chain_rows() -> tuple[list[dict], list[str], list[str]]:
    """The five chains as stored rows: (rows, seed copy ids, 0.376 copy ids)."""
    rows, seed, kept = [], [], []
    for n, (name, model, family, prec, pep, prot, when, (amt, spd0, len0), (spd1, lc1, len1)) in enumerate(SEED_CHAINS):
        common = dict(instrument_model=model, instrument_family=family, n_precursors=prec, n_peptides=pep,
                      n_proteins=prot, n_psms=0, run_date=when)
        sid = 100 + 10 * n
        rows.append(_tic_row(sid, start=2.0, run_name=name, display_name="Anonymous Lab", stan_version="0.2.283",
                             acquisition_mode="dia", amount_ng=amt, spd=spd0, lc_system="", gradient_length_min=len0,
                             submitted_at="2026-05-01T01:09:47.684153Z", **common))
        rows.append(_tic_row(sid + 1, run_name=name, stan_version="0.2.376", amount_ng=50.0, spd=spd1,
                             lc_system=lc1, gradient_length_min=len1, submitted_at="2026-05-27T00:41:01.624786Z", **common))
        seed.append(f"s{sid}")
        kept.append(f"s{sid + 1}")
        if name.startswith("FL120621"):          # the same acquisition under a second name
            rows.append(_tic_row(sid + 2, run_name="FL120621_HeLa100ng_DIASpcNwin35_90mGood.raw", stan_version="0.2.376",
                                 amount_ng=50.0, spd=spd1, lc_system=lc1, gradient_length_min=len1,
                                 submitted_at="2026-05-27T00:41:04.301119Z", **common))
    return rows, seed, kept


@needs_node
def test_python_port_matches_the_page_with_unconfirmed_amounts(client, hub, relay, tmp_path):
    """The copy kept is the one kept before P3b: an unconfirmed amount never
    decides it, so a degraded seed copy is never swapped in, and the
    acquisition is held back as a whole. Held-back copies are in no TIC
    cohort; the page and the port agree on all of it."""
    chains, seed, kept = _seed_chain_rows()
    rows = _p3b_rows() + chains
    base = dict(n_precursors=42000.0, n_peptides=36000, n_proteins=5000, n_psms=0, spd=100, lc_system="evosep")
    rows += [   # two copies, both unconfirmed: held back either way
        _tic_row(22, run_name="evosep_40ng_hela_S2-B2_1_16718.d", run_date="2026-05-02T10:00:00Z", **base),
        _tic_row(23, run_name="evosep_40ng_hela_S2-B2_1_16718.d", run_date="2026-05-02T10:00:01Z", **base),
    ]
    _serve_frame(hub, rows)
    lb = {r["submission_id"]: r for r in client.get("/api/leaderboard").json()["submissions"]}
    assert all(lb[k]["amount_check"] == "mismatch" for k in kept) and all(lb[k]["amount_check"] == "" for k in seed)
    js, py = _assert_parity(client, relay, tmp_path)
    assert set(kept) <= set(py["kept"]) and not set(seed) & set(py["kept"])   # the 0.376 copy, as before P3b
    assert "s121" in py["kept"] and "s122" not in py["kept"]                  # earliest submitted of two 0.376 copies
    assert not (set(kept) | set(seed)) & set(py["usable"])                    # held back as a whole
    assert not {"s5", "s8", "s22", "s23"} & set(py["usable"])
    assert ("s22" in py["kept"]) != ("s23" in py["kept"])
    drawn = {sid for v in py["groups"].values() for sid, _ in v}
    assert not {"s5", "s8", "s22", "s23"} & drawn and not (set(kept) | set(seed)) & drawn
    assert {"s6", "s7"} <= drawn
    assert not any(k.endswith("|unrec") for k in py["groups"])                # no seed copy's "LC not recorded" entry


# ── page ─────────────────────────────────────────────────────────────

def _page_rows() -> list[dict]:
    """Rows as /api/leaderboard serves them since 1.8.0 (the page never sees a file name)."""
    def r(i, **over):
        row = _row(i, **over)
        row.pop("run_name")
        row.setdefault("amount_check", "")
        row.setdefault("faims", None)
        row.setdefault("faims_source", "")
        return row
    lumos = dict(instrument_family="Lumos", instrument_model="Orbitrap Fusion Lumos", spd=9, lc_system="custom",
                 gradient_length_min=128)
    rows = [r(i, n_precursors=40000 + 100 * i, **lumos) for i in range(6)]
    rows += [r(10 + i, n_precursors=84000 - 100 * i, amount_check="mismatch", faims=True, faims_source="filename", **lumos)
             for i in range(4)]                                                   # 1 µg FAIMS stored as 50 ng
    rows += [r(20 + i, n_precursors=60000 + 100 * i, faims=True, faims_source="filename", **lumos) for i in range(5)]
    rows += [r(30, n_precursors=30000, amount_ng=100000.0, **lumos)]               # a unit error
    rows += [r(40 + i, n_precursors=35000 + 10 * i, amount_source="assumed") for i in range(5)]   # timsTOF HT Evosep 100
    rows += [r(50, n_precursors=36000, is_flagged=True)]                            # flagged: left out, not listed
    return rows


_SETUP = "setSubmissions(ROWS); renderFilterBar(); updateStats(); renderPanels(null);"


@needs_node
def test_held_back_rule_and_stats_note(client, tmp_path):
    scenario = f"""(() => {{ {_SETUP}
        return {{ note: els['stats-note'].textContent, runs: els['stat-submissions'].textContent,
                  held: allDataRaw.filter(isHeldBack).map(s => s.submission_id).sort(),
                  unconf: allDataRaw.filter(amountUnconfirmed).length, implaus: allDataRaw.filter(amountImplausible).length,
                  usable: usableRows().length }}; }})()"""
    got = _run(client, tmp_path, scenario, data=f"var ROWS = {json.dumps(_page_rows())};")["out"]
    assert got["held"] == ["s10", "s11", "s12", "s13", "s30"]
    assert got["unconf"] == 4 and got["implaus"] == 1
    assert got["usable"] == 16 and got["runs"] == "16"
    note = got["note"]
    assert "1 run held back from every range and ranking because the stored amount is above 5,000 ng" in note
    assert "4 runs held back as amount unconfirmed, because the file name states a different amount than the one stored" in note
    assert "listed below the submissions table" in note and "1 flagged run left out" in note


@needs_node
def test_faims_runs_form_their_own_cohort(client, tmp_path):
    """FAIMS is in the B2 key and the titles, never a filter (decision 9)."""
    scenario = f"""(() => {{ {_SETUP}
        const lumos = allData.filter(s => s.instrument_model === 'Orbitrap Fusion Lumos');
        const cs = cohortsOf(lumos);
        const strip = h => h.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim();
        const last = (id) => plots.filter(p => p.id === id).slice(-1)[0];
        return {{
            cohorts: cs.map(c => [c.key, c.faims, c.rows.length, cohortTitle(c)]),
            plainKey: rowKey(allData.find(s => s.submission_id === 's1')).key,
            gradients: [...els['fbar-gradient'].innerHTML.matchAll(/<option value="([^"]*)"/g)].map(m => m[1]),
            cards: [...els['ref-ranges-container'].innerHTML.matchAll(/<h4>([^<]*)<\\/h4>/g)].map(m => m[1]),
            best: [...els['config-leaderboard'].innerHTML.matchAll(/<tr[^>]*>([\\s\\S]*?)<\\/tr>/g)].map(m => strip(m[1])),
            violin: JSON.stringify(last('chart-violin') || {{}}),
            cohortCells: [...els['table-container'].innerHTML.matchAll(/min-width:12.5rem">([^<]*)/g)].map(m => m[1]),
        }}; }})()"""
    got = _run(client, tmp_path, scenario, data=f"var ROWS = {json.dumps(_page_rows())};")["out"]
    (plain,), (faims,) = [c for c in got["cohorts"] if not c[1]], [c for c in got["cohorts"] if c[1]]
    assert plain[0] == "hela|Orbitrap Fusion Lumos|DIA|nanolc:9|50" == got["plainKey"]   # as before 1.8.0
    assert faims[0] == "hela|Orbitrap Fusion Lumos|DIA|nanolc:9|50|faims"
    assert plain[2] == 6 and faims[2] == 5
    assert plain[3] == "Orbitrap Fusion Lumos · DIA · ~128 min gradient (9 SPD) · 128 min run · 50 ng"
    assert faims[3] == "Orbitrap Fusion Lumos · DIA · ~128 min gradient (9 SPD) · 128 min run · FAIMS · 50 ng"
    assert got["gradients"].count("nanolc:9") == 1                 # one gradient option: no FAIMS filter
    assert "~128 min gradient (9 SPD) · 128 min run · FAIMS" in got["cards"]
    assert "~128 min gradient (9 SPD) · 128 min run" in got["cards"]
    assert any("FAIMS" in r and "60,200" in r for r in got["best"])          # the FAIMS cohort's median, its own row
    assert any("FAIMS" not in r and "40,250" in r for r in got["best"])
    assert "FAIMS" in got["violin"]
    assert sum("· FAIMS ·" in c for c in got["cohortCells"]) == 5


@needs_node
def test_the_lookup_compares_with_runs_without_faims(client, tmp_path):
    """The lookup has no FAIMS field: its rows and its cohort leave FAIMS runs
    out, even when a FAIMS run comes first in the rows (most precursors)."""
    scenario = f"""(() => {{ {_SETUP}
        lkSet('model', 'Orbitrap Fusion Lumos'); lkSet('grad', 'nanolc:9'); lkSet('amt', '50');
        const R = lkResolve();
        return {{ rows: lkRows().filter(s => rowKey(s).f).length, all: lkRows().length,
                  key: R.c && R.c.key, n: R.c && R.c.rows.length }}; }})()"""
    rows = _page_rows()
    rows.sort(key=lambda r: -r["n_precursors"])          # /api/leaderboard order: the FAIMS runs lead
    got = _run(client, tmp_path, scenario, data=f"var ROWS = {json.dumps(rows)};")["out"]
    assert got["rows"] == 0 and got["all"] == 11
    assert got["key"] == "hela|Orbitrap Fusion Lumos|DIA|nanolc:9|50" and got["n"] == 6


@needs_node
def test_table_marks_assumed_amounts_and_lists_the_held_back_runs(client, tmp_path):
    scenario = f"""(() => {{ {_SETUP}
        const out = {{ dia: els['table-container'].innerHTML, inview: els['fbar-inview'].innerHTML }};
        setView({{ model: 'timsTOF HT' }}); out.ht = els['table-container'].innerHTML;
        setView({{ model: '', amount: 'all' }}); out.all = els['table-container'].innerHTML;
        setView({{ amount: '50', mode: 'dda' }}); out.dda = els['table-container'].innerHTML;
        return out; }})()"""
    got = _run(client, tmp_path, scenario, data=f"var ROWS = {json.dumps(_page_rows())};")["out"]
    dia = got["dia"]
    main, held = dia.split('<details class="held"')
    # the table holds the runs in view, as before
    assert "<span>16 submissions</span>" in main and "<b>16</b> runs in view" in got["inview"]
    assert main.count(">assumed</span>") == 5
    assert "unconfirmed" not in main and "84,000" not in main
    # the held-back runs of the view, under it: 4 unconfirmed, the flagged one left out
    assert "4 held-back runs in view (4 amount unconfirmed): not in the table above or in any range or ranking" in held
    assert held.count(">unconfirmed</span>") == 4 and "84,000" in held and "36,000" not in held
    assert held.count("The file name states a different amount than the one stored") == 4
    assert held.count("· FAIMS") == 4
    # the unit error is above 5,000 ng: in the >250 ng bucket, so it shows under all amounts
    assert '<details class="held"' not in got["ht"]
    assert "5 held-back runs in view (4 amount unconfirmed, 1 stored above 5,000 ng)" in got["all"]
    assert ">above 5,000 ng</span>" in got["all"] and "100,000 ng" in got["all"]
    assert '<details class="held"' not in got["dda"]


@needs_node
def test_held_back_list_escapes_submitter_strings(client, tmp_path):
    evil = '<img src=x onerror="alert(1)">'
    rows = _page_rows()
    for r in rows:
        if r["amount_check"] == "mismatch":
            r["instrument_model"] = evil
    scenario = f"""(() => {{ {_SETUP} setView({{ model: '' }}); return els['table-container'].innerHTML; }})()"""
    html = _run(client, tmp_path, scenario, data=f"var ROWS = {json.dumps(rows)};")["out"]
    assert evil not in html and "&lt;img src=x" in html


@needs_node
def test_a_missing_amount_is_never_a_stand_in_50_ng(client, tmp_path):
    """Hovers say "amount not recorded" and the point is an open circle, not
    the 50 ng circle, in Depth by Throughput and the platform violins."""
    rows = _page_rows()
    for i in range(5):            # a ranked cohort (5 runs) with no amount recorded
        r = dict(next(x for x in rows if x["submission_id"] == "s40"))
        r.update(submission_id=f"s7{i}", amount_ng=None, amount_source=None, n_precursors=33000 + 10 * i,
                 n_peptides=30000 + i, run_date=f"2026-08-{10 + i}T10:00:00Z")
        rows.append(r)
    scenario = f"""(() => {{ {_SETUP} setView({{ amount: 'all' }});
        const last = (id) => plots.filter(p => p.id === id).slice(-1)[0];
        const pts = (id) => [].concat(...last(id).traces.filter(t => t.mode === 'markers').map(t =>
            (t.text || []).map((x, i) => [x, [].concat((t.marker || {{}}).symbol)[i]])));
        return {{ text: [amountText({{ amount_ng: null }}), amountText({{ amount_ng: 0 }}), amountText({{ amount_ng: 50 }}),
                         amountText({{ amount_ng: 1000 }})],
                  shape: [null, 0, 5, 50, 500].map(a => amountShapeOf({{ amount_ng: a }})),
                  spd: pts('chart-spd-depth'), violin: pts('chart-violin') }}; }})()"""
    got = _run(client, tmp_path, scenario, data=f"var ROWS = {json.dumps(rows)};")["out"]
    assert got["text"] == ["amount not recorded", "amount not recorded", "50 ng", "1,000 ng"]
    assert got["shape"] == ["circle-open", "circle-open", "diamond", "circle", "square"]
    for chart in ("spd", "violin"):
        missing = [sym for txt, sym in got[chart] if "amount not recorded" in txt]
        assert len(missing) == 5 and set(missing) == {"circle-open"}, chart
        assert not any("SPD, 50 ng" in txt and sym == "circle-open" for txt, sym in got[chart]), chart


def test_page_reads_no_stand_in_amount_and_the_lookup_says_faims_is_left_out(client):
    html = _page(client)
    assert not re.search(r"amount_ng\s*\|\|\s*50", html)
    assert ("Runs acquired with FAIMS are left out of these cohorts: there is no FAIMS field here, "
            "so your run is compared with runs acquired without it.") in html


# ── the 2026-09-29 snapshot ──────────────────────────────────────────

def _snapshot_rows() -> list[dict]:
    rows = json.loads((SNAP / "api_leaderboard.json").read_text())["submissions"]
    tic = {t["submission_id"]: t for t in json.loads((SNAP / "api_tic_overlay.json").read_text())["traces"]}
    for r in rows:
        t = tic.get(r["submission_id"])
        r["tic_rt_bins"] = t["tic_rt_bins"] if t else None
        r["tic_intensity"] = t["tic_intensity"] if t else None
    return rows


@needs_node
@needs_snapshot
def test_snapshot_holds_back_71_rows_and_marks_15_faims(client, hub, tmp_path):
    """Decision 3 on the 2026-09-29 snapshot (3,305 rows, every one with its
    file name): 71 rows whose file name states another amount, all STAN
    0.2.376 Clogged PeakTail rows stored as 50 ng, and 15 FAIMS names. Once
    copies are removed they are 69 runs, all held back: the dedupe keeps the
    copy it kept before P3b, never an older seed copy."""
    snap = _snapshot_rows()
    names = {r["submission_id"]: r["run_name"] for r in snap}
    buf = io.BytesIO()
    pl.from_dicts(snap, infer_schema_length=None).write_parquet(buf)
    hub.files["benchmark_latest.parquet"] = buf.getvalue()
    lb = client.get("/api/leaderboard").json()["submissions"]
    mm = [r for r in lb if r["amount_check"] == "mismatch"]
    assert len(mm) == 71
    assert {(r["display_name"], r["stan_version"], r["amount_ng"]) for r in mm} == {("Clogged PeakTail", "0.2.376", 50.0)}
    by = {}
    for r in mm:
        stated = client_amount.parse_amount_ng(names[r["submission_id"]])
        by[(r["instrument_model"], stated)] = by.get((r["instrument_model"], stated), 0) + 1
    assert by[("Orbitrap Fusion Lumos", 1000.0)] == 15          # 13 "FaimHe1ug", "qCHeL1ug", "He1000ng"
    assert by[("timsTOF HT", 100.0)] == 18 and by[("timsTOF HT", 40.0)] == 10
    assert by[("timsTOF HT", 50000.0)] + by[("Orbitrap Exploris 480", 50000.0)] == 10   # "HeL50ug"
    assert sum(1 for r in mm if "FaimHe1ug" in names[r["submission_id"]]) == 13
    fa = [r for r in lb if r["faims"] is True]
    assert len(fa) == 15 and {r["faims_source"] for r in fa} == {"filename"}
    assert all(r["faims"] is None for r in lb if r["faims_source"] == "")
    assert sorted({r["instrument_model"] for r in fa}) == ["Orbitrap Exploris 480", "Orbitrap Fusion Lumos"]
    assert all("faim" in names[r["submission_id"]].lower() for r in fa)

    # The page on those rows: 69 runs unconfirmed once copies are removed,
    # the four 1 µg FAIMS runs leave the table for the held-back list, and the
    # two Exploris FAIMS runs are their own (unranked, 1-run) cohorts.
    scenario = f"""(() => {{ {_SETUP}
        const strip = h => h.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim();
        const html = els['table-container'].innerHTML, i = html.indexOf('<details class="held"');
        return {{ note: els['stats-note'].textContent, runs: els['stat-submissions'].textContent,
                  table: strip(html.slice(0, i)).slice(0, 2000), held: strip(html.slice(i)),
                  sparse: strip(els['ref-ranges-container'].innerHTML) }}; }})()"""
    got = _run(client, tmp_path, scenario, data=f"var ROWS = {json.dumps(lb)};")["out"]
    assert "244 duplicate copies removed" in got["note"]
    assert "2 runs held back from every range and ranking because the stored amount is above 5,000 ng" in got["note"]
    assert "69 runs held back as amount unconfirmed" in got["note"]
    assert got["runs"] == "2,966"                                  # 3,035 before P3b
    for top in ("84,857", "84,461", "82,477", "69,682"):
        assert top not in got["table"] and top in got["held"]
    assert "~96 min gradient (12 SPD) · 88 min run · FAIMS · 50 ng" in got["sparse"]
    assert "~61 min gradient (19 SPD) · 88 min run · FAIMS · 50 ng" in got["sparse"]
    # the TIC summary counts only what the page counts
    s = _summary(client)
    assert s["usable"] == 2990 and s["traces"]["DIA"] == 2954
    assert re.search(r"(?i)faim", json.dumps(s)) is None
    assert not [c for c in s["cohorts"] if c["spd"] == 100 and c["lc"] == "unrec"]   # no seed copy swapped in
