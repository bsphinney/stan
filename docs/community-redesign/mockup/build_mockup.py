#!/usr/bin/env python3
"""Build the STAN community-site redesign mockup from the 2026-09-29 snapshot.

Reads the read-only capture in ../sitereview/ (api_leaderboard.json and the PEG
API snapshots), computes every number the mockup shows, and writes
community_mockup.html by filling the /*__DATA__*/ slot in community_mockup.template.html.

Rules applied here, matching the review (REVIEW.md) and the critic pass:
  D8  duplicates removed: same instrument, track and all four ID counts, with
      acquisition instants within 2 s of each other (copies differ only in
      sub-second precision). The filename is NOT part of the key (D4).
  D8  runs held back: stored amount > 5,000 ng, or a unit-anchored amount in the
      filename that disagrees with the stored amount by more than 1.5x (B4).
      A parsed amount above 5 ug is treated as unparseable ("HeL50ug" on an
      Evotip is a typo, not 50,000 ng). Filenames are read here, server-side,
      and never written to the page.
  B2  one cohort key: QC standard x model x track x LC/gradient x amount bucket.
      - Evosep runs are named only by real Evosep methods. Any other Evosep SPD
        is labelled "SPD unverified" and not ranked.
      - nanoLC cohorts are labelled from the recorded run length plus the
        stored (derived) SPD, e.g. "44 min run (~38 SPD)".
      - A run with no LC recorded joins the nanoLC cohort for its SPD when that
        SPD is not an Evosep method; at an Evosep-method SPD it is listed but
        not ranked. Missing metadata never defines a ranked cohort.
  D2  labs are counted as facilities. Both pseudonyms are UC Davis.
  D1  the primary value is precursors for DIA and PSMs for DDA, per cohort.
  D3  IPS: the stored scores are re-derived to show that Exploris and timsTOF
      fall through to the pooled global reference (family-key mismatch).

Run:  python3 build_mockup.py
"""
from __future__ import annotations

import collections as C
import datetime as dt
import json
import math
import pathlib
import re
import statistics as S

HERE = pathlib.Path(__file__).resolve().parent
import os

# The 2026-09-29 snapshot (live API JSON, ~14 MB) is not in git. It is kept at
# ~/stan-handoff-2026-09-29/sitereview/; point STAN_MOCK_SNAPSHOT at any
# directory holding the same files (see ../README.md for how to re-fetch).
REV = pathlib.Path(os.environ.get("STAN_MOCK_SNAPSHOT", str(HERE.parent / "sitereview")))
TEMPLATE = HERE / "community_mockup.template.html"
OUT = HERE / "community_mockup.html"

rows = json.load(open(REV / "api_leaderboard.json"))["submissions"]
peg = json.load(open(REV / "api_peg_leaderboard.json"))
pegt = json.load(open(REV / "api_peg_trend.json"))

EVOSEP_METHODS = {100: "Evosep 100 SPD", 60: "Evosep 60 SPD", 30: "Evosep 30 SPD", 200: "Evosep 200 SPD",
                  300: "Evosep 300 SPD", 500: "Evosep 500 SPD", 20: "Evosep Whisper 20 SPD",
                  40: "Evosep Whisper 40 SPD", 80: "Evosep Whisper 80 SPD", 120: "Evosep Whisper 120 SPD"}
# Both pseudonyms in the snapshot are the UC Davis Proteomics Core (REVIEW.md D2).
FACILITY = {"Clogged PeakTail": "UC Davis Proteomics Core", "Anonymous Lab": "UC Davis Proteomics Core"}


def track(r) -> str:
    return "DDA" if (r.get("acquisition_mode") or "").lower() == "dda" else "DIA"


def instant(r) -> dt.datetime:
    return dt.datetime.fromisoformat(r["run_date"].replace("Z", "+00:00")).astimezone(dt.timezone.utc)


def primary(r) -> int:
    return int(r["n_psms"] or 0) if track(r) == "DDA" else int(r["n_precursors"] or 0)


def q(sv: list[float], p: float) -> float | None:
    """Linear-interpolated quantile of an already sorted list."""
    if not sv:
        return None
    if len(sv) == 1:
        return sv[0]
    x = (len(sv) - 1) * p
    lo = math.floor(x)
    hi = min(lo + 1, len(sv) - 1)
    return sv[lo] + (sv[hi] - sv[lo]) * (x - lo)


def med(vals):
    v = [x for x in vals if x is not None]
    return S.median(v) if v else None


# ---------------------------------------------------------------- D8 dedupe (2 s tolerance)
lab_total = C.Counter(r["display_name"] for r in rows)
by_counts: dict[tuple, list] = C.defaultdict(list)
for r in rows:
    by_counts[(r["instrument_model"], track(r), r["n_precursors"], r["n_peptides"], r["n_proteins"], r["n_psms"])].append(r)
groups: list[list] = []
for g in by_counts.values():
    # same instrument, track and counts: copies of one acquisition sit within 2 s of each other
    g.sort(key=instant)
    cur: list = []
    for r in g:
        if cur and (instant(r) - instant(cur[-1])).total_seconds() <= 2.0:
            cur.append(r)
        else:
            if cur:
                groups.append(cur)
            cur = [r]
    groups.append(cur)
kept, dup_groups, dup_extra, dup_cross = [], 0, 0, 0
for g in groups:
    if len(g) > 1:
        dup_groups += 1
        dup_extra += len(g) - 1
        if len({x["display_name"] for x in g}) > 1:
            dup_cross += 1
    # keep the copy from the larger contributor, then the first submitted
    g.sort(key=lambda x: (-lab_total[x["display_name"]], x["submitted_at"]))
    kept.append(g[0])

# the exact-instant key the first mockup used, for the annotation only
exact = C.Counter((r["instrument_model"], track(r), instant(r).isoformat(), r["n_precursors"], r["n_peptides"], r["n_proteins"], r["n_psms"]) for r in rows)
dup_extra_exact = sum(v - 1 for v in exact.values() if v > 1)
by_name = C.defaultdict(list)
for r in rows:
    by_name[(r["run_name"], r["n_precursors"], r["instrument_model"])].append(r)
dup_by_name = sum(1 for g in by_name.values() if len(g) > 1)

# ---------------------------------------------------------------- D8 / B4 flags
UNIT = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(ng|ug|µg)", re.I)


def parsed_amount(name: str) -> float | None:
    s = re.sub(r"k562", "", name or "", flags=re.I)
    m = UNIT.search(s)
    if not m:
        return None
    v = float(m.group(1))
    return v if m.group(2).lower() == "ng" else v * 1000.0


flag_reason: dict[str, str] = {}
amb_amount = 0
for r in kept:
    a = r["amount_ng"] or 0
    p = parsed_amount(r["run_name"])
    if p is not None and p > 5000:
        amb_amount += 1          # "HeL50ug": unparseable, neither trusted nor flagged
        p = None
    if a > 5000:
        flag_reason[r["submission_id"]] = "amount"          # implausible stored amount
    elif p is not None and a > 0 and (p / a > 1.5 or a / p > 1.5):
        flag_reason[r["submission_id"]] = "mismatch"        # filename says otherwise
n_faims = sum(1 for r in kept if "faim" in (r["run_name"] or "").lower())
flag_counts = C.Counter(flag_reason.values())
flag_mismatch_top = sorted((primary(r) for r in kept if flag_reason.get(r["submission_id"]) == "mismatch"), reverse=True)[:4]

# ---------------------------------------------------------------- B2 cohort key
MODELS = ["timsTOF HT", "timsTOF Pro", "timsTOF Pro 2", "Orbitrap Exploris 480", "Orbitrap Fusion Lumos"]
LABS = [n for n, _ in lab_total.most_common()]
FACS = sorted({FACILITY.get(n, n) for n in LABS})
SAMPLES = ["hela", "k562", "yeast"]
VENDOR = {m: ("bruker" if m.startswith("timsTOF") else "thermo") for m in MODELS}


def lc_class(r) -> str:
    """evosep | evosep_unv | nanolc | unrec (no LC recorded at an Evosep-method SPD)."""
    lc = (r.get("lc_system") or "").lower()
    spd = int(r["spd"])
    if lc == "evosep":
        return "evosep" if spd in EVOSEP_METHODS else "evosep_unv"
    if lc:
        return "nanolc"
    return "unrec" if spd in EVOSEP_METHODS else "nanolc"


def abucket(a) -> str:
    if not a:
        return "unk"
    if a <= 25:
        return "le25"
    if a <= 75:
        return "50"
    if a <= 250:
        return "100_250"
    return "gt250"


def runlen_label(lens: list[int]) -> str:
    s = sorted(lens)
    lo, hi = s[int(0.1 * (len(s) - 1))], s[int(round(0.9 * (len(s) - 1)))]
    return f"{lo} min run" if lo == hi else f"{lo}–{hi} min runs"


def grad_label(lc: str, spd: int, lens: list[int]) -> str:
    if lc == "evosep":
        return EVOSEP_METHODS[spd]
    if lc == "evosep_unv":
        return f"Evosep, {runlen_label(lens)} (SPD {spd} unverified)"
    if lc == "nanolc":
        return f"{runlen_label(lens)} (~{spd} SPD)"
    return f"{spd} SPD, LC not recorded ({runlen_label(lens)})"


valid = [r for r in kept if r["submission_id"] not in flag_reason]
flagged = [r for r in kept if r["submission_id"] in flag_reason]

ckey = lambda r: (r["sample_type"], r["instrument_model"], track(r), lc_class(r), int(r["spd"]), abucket(r["amount_ng"]))
coh_rows: dict[tuple, list] = C.defaultdict(list)
for r in valid:
    coh_rows[ckey(r)].append(r)

LC_ORDER = ["evosep", "nanolc", "evosep_unv", "unrec"]
order = sorted(coh_rows, key=lambda k: (SAMPLES.index(k[0]), MODELS.index(k[1]), k[2], k[5], LC_ORDER.index(k[3]), -k[4]))
cohorts, cidx = [], {}
for i, k in enumerate(order):
    rs = coh_rows[k]
    cidx[k] = i
    v = sorted(primary(r) for r in rs)
    pep = sorted(int(r["n_peptides"] or 0) for r in rs)
    prot = sorted(int(r["n_proteins"] or 0) for r in rs)
    dates = sorted(instant(r) for r in rs)
    lib = [r["library_coverage_pct"] for r in rs if r.get("library_coverage_pct") is not None]
    cols = C.Counter(r["column_model"] for r in rs if (r.get("column_model") or "Unknown").lower() not in ("unknown", ""))
    lens = [int(r["gradient_length_min"]) for r in rs]
    lc = k[3]
    if lc in ("evosep", "nanolc") and len(rs) >= 5:
        why = ""
    elif lc == "evosep_unv":
        why = "unv"
    elif lc == "unrec":
        why = "nolc"
    else:
        why = "sparse"
    cohorts.append({
        "s": k[0], "m": MODELS.index(k[1]), "t": k[2], "lc": lc, "spd": k[4], "a": k[5],
        "g": grad_label(lc, k[4], lens),
        "n": len(rs), "why": why,
        "labs": sorted({LABS.index(r["display_name"]) for r in rs}),
        "fac": len({FACILITY.get(r["display_name"], r["display_name"]) for r in rs}),
        "nolc": sum(1 for r in rs if not (r.get("lc_system") or "")),
        "lens": [[x, c] for x, c in C.Counter(lens).most_common()],
        "v": v, "pep": pep,
        "prot": [round(q(prot, .25)), round(q(prot, .5)), round(q(prot, .75))],
        "pts": round(med([r["median_points_across_peak"] for r in rs]) or 0, 1),
        # DDA rows carry 0.0 here (not measured); treat 0 as missing so the page prints a dash
        "ppm": (lambda x: round(x, 2) if x else None)(med([r["median_mass_acc_ms1_ppm"] or None for r in rs])),
        "lib": round(S.median(lib), 1) if lib and k[1].startswith("timsTOF") and k[2] == "DIA" else None,
        "d0": dates[0].strftime("%Y-%m"), "d1": dates[-1].strftime("%Y-%m"),
        "cols": [c for c, _ in cols.most_common()],
    })

# ---------------------------------------------------------------- runs for table, lookup, lab trend
runs = []
for r in sorted(valid, key=lambda r: instant(r)):
    runs.append([
        instant(r).strftime("%Y-%m-%d"), LABS.index(r["display_name"]), cidx[ckey(r)],
        int(r["n_precursors"] or 0), int(r["n_peptides"] or 0), int(r["n_proteins"] or 0), int(r["n_psms"] or 0),
        round(r["median_points_across_peak"] or 0, 1),
    ])
flag_rows = []
for r in sorted(flagged, key=lambda r: -primary(r)):
    flag_rows.append([
        instant(r).strftime("%Y-%m-%d"), LABS.index(r["display_name"]), MODELS.index(r["instrument_model"]),
        track(r), int(r["spd"]), r["amount_ng"], r["sample_type"],
        int(r["n_precursors"] or 0), int(r["n_peptides"] or 0), int(r["n_proteins"] or 0), int(r["n_psms"] or 0),
        flag_reason[r["submission_id"]],
    ])

# ---------------------------------------------------------------- B5 UC Davis instrument history (ID-free, HeLa, per instrument)
hist = {}
for m in ["timsTOF HT", "Orbitrap Exploris 480", "Orbitrap Fusion Lumos"]:
    by_mo = C.defaultdict(list)
    for r in kept:
        if r["instrument_model"] == m and r["sample_type"] == "hela":
            by_mo[instant(r).strftime("%Y-%m")].append(r)
    months = sorted(by_mo)
    ser = {"mo": months, "n": [len(by_mo[x]) for x in months]}
    for key, f in (("ppm", lambda r: r["median_mass_acc_ms1_ppm"]),
                   ("sig", lambda r: math.log10(r["ms1_signal"]) if r.get("ms1_signal") else None),
                   ("dr", lambda r: r["dynamic_range_log10"]),
                   ("pts", lambda r: r["median_points_across_peak"])):
        ser[key] = [None if med([f(r) for r in by_mo[x]]) is None else round(med([f(r) for r in by_mo[x]]), 2) for x in months]
    hist[m] = ser

# ---------------------------------------------------------------- D3 IPS: re-derive stored scores (relay app.py:780-906)
IPS_REF = {
    ("Exploris 480", "deep"): ((26974, 31698, 35425), (23845, 28545, 32111), (3373, 3993, 4195)),
    ("Exploris 480", "medium"): ((19159, 25259, 29874), (17550, 23020, 27081), (2539, 3104, 3478)),
    ("Lumos", "deep"): ((31423, 39149, 56834), (29066, 35762, 50551), (3982, 4510, 5509)),
    ("Lumos", "medium"): ((16705, 27251, 37839), (15452, 24847, 33727), (2657, 3614, 4347)),
    ("timsTOF HT", "fast"): ((32305, 42778, 48757), (28864, 38195, 43578), (4300, 4768, 5104)),
    ("timsTOF HT", "medium"): ((36153, 45262, 50142), (32779, 40531, 44574), (4730, 4972, 5160)),
    ("timsTOF HT", "ultra"): ((25203, 37051, 45731), (23722, 33106, 40851), (3940, 4509, 4945)),
    ("timsTOF HT", "*"): ((30003, 40364, 47857), (26423, 36155, 42531), (4141, 4703, 5068)),
    ("Exploris 480", "*"): ((19159, 25908, 31036), (17550, 23474, 28321), (2539, 3166, 3775)),
    ("Lumos", "*"): ((18519, 30522, 47340), (16941, 27907, 43154), (2965, 3917, 4906)),
}
IPS_GLOBAL = ((19000, 35000, 48000), (17000, 31000, 42000), (2900, 4200, 5100))


def _bucket(spd):
    if not spd or spd <= 0:
        return "medium"
    return "deep" if spd <= 15 else "medium" if spd <= 40 else "fast" if spd <= 80 else "ultra"


def _ref(fam, spd):
    if fam:
        if (fam, _bucket(spd)) in IPS_REF:
            return IPS_REF[(fam, _bucket(spd))]
        if (fam, "*") in IPS_REF:
            return IPS_REF[(fam, "*")]
    return IPS_GLOBAL


def _cs(v, p10, p50, p90):
    if v is None or v <= 0:
        return 0.0
    if v <= p10:
        return 30 * v / p10
    if v <= p50:
        return 30 + 30 * (v - p10) / (p50 - p10)
    if v <= p90:
        return 60 + 30 * (v - p50) / (p90 - p50)
    return 90 + 10 * min((v - p90) / (0.5 * p90), 1)


def _ips(r, fam):
    R = _ref(fam, int(r["spd"]))
    x = .5 * _cs(r["n_precursors"], *R[0]) + .3 * _cs(r["n_peptides"], *R[1]) + .2 * _cs(r["n_proteins"], *R[2])
    return int(round(max(0, min(100, x))))


FAMKEY = {"Exploris": "Exploris 480", "timsTOF": "timsTOF HT", "Lumos": "Lumos"}
ips = {}
for fam in ("Exploris", "timsTOF", "Lumos"):
    rs = [r for r in rows if r["instrument_family"] == fam and r["sample_type"] == "hela" and track(r) == "DIA"]
    stored = [r["ips_score"] for r in rs]
    fixed = [_ips(r, FAMKEY[fam]) for r in rs]
    ips[fam] = {
        "n": len(rs), "fallback_match": sum(1 for r in rs if _ips(r, fam) == r["ips_score"]),
        "med_stored": S.median(stored), "med_fixed": S.median(fixed),
        "lt60_stored": round(100 * sum(x < 60 for x in stored) / len(rs)),
        "lt60_fixed": round(100 * sum(x < 60 for x in fixed) / len(rs)),
    }
assert ips["Exploris"]["fallback_match"] == ips["Exploris"]["n"] and ips["timsTOF"]["fallback_match"] == ips["timsTOF"]["n"]

# ---------------------------------------------------------------- facts for copy, decisions and annotations
hela_dia_all = [r for r in rows if r["sample_type"] == "hela" and track(r) == "DIA"]
expl38 = next(c for c in cohorts if c["s"] == "hela" and MODELS[c["m"]] == "Orbitrap Exploris 480" and c["t"] == "DIA" and c["lc"] == "nanolc" and c["spd"] == 38 and c["a"] == "50")
tims_lib = sorted(r["library_coverage_pct"] for r in kept if r["instrument_family"] == "timsTOF" and r.get("library_coverage_pct") is not None)
two_col = sum(1 for c in cohorts if len(c["cols"]) >= 2)
latest = max(instant(r) for r in kept)
unranked = [c for c in cohorts if c["why"]]
# ---------------------------------------------------------------- TIC overlay (Brett, 2026-09-29: keep it broken out by SPD and LC)
# Same grouping as the live chart (app.py renderCommunityTIC): SPD x LC system
# (explicit lc_system, else inferLcSystem) x acquisition mode, each trace scaled
# to its own peak. Two differences, both annotated on the page:
#   * percentiles are taken at the same minute across runs (each trace
#     interpolated onto the cohort's median time axis), not at the same bin index;
#   * bands are drawn only where at least 5 traces (and half the cohort) cover.
# Duplicate copies are removed with the same D8 key as every other panel.
import bisect

tic_raw = {x["submission_id"]: x for x in json.load(open(REV / "api_tic_overlay.json"))["traces"]}
kept_ids = {r["submission_id"] for r in kept}


def infer_lc(r) -> str:
    v = (r.get("column_vendor") or "").lower()
    m = (r.get("column_model") or "").lower()
    if "evo" in v or "evo" in m or re.match(r"^ev\d", m):
        return "evosep"
    return "evosep" if r.get("spd") in (100, 60, 30) else "custom"


def interp(rt, y, x):
    if x < rt[0] or x > rt[-1]:
        return None
    i = bisect.bisect_left(rt, x)
    if i == 0:
        return y[0]
    x0, x1 = rt[i - 1], rt[i]
    return y[i - 1] if x1 <= x0 else y[i - 1] + (y[i] - y[i - 1]) * (x - x0) / (x1 - x0)


tic_groups: dict[tuple, list] = C.defaultdict(list)
tic_dups = 0
for r in rows:
    x = tic_raw.get(r["submission_id"])
    if not x:
        continue
    if r["submission_id"] not in kept_ids:
        tic_dups += 1
        continue
    if r.get("is_flagged"):
        continue
    rt = json.loads(x["tic_rt_bins"]); it = json.loads(x["tic_intensity"])
    mx = max(it) if it else 0
    if not rt or mx <= 0 or len(rt) != len(it):
        continue
    lc = r.get("lc_system") or infer_lc(r)
    lc = "evosep" if lc == "evosep" else "custom"
    tic_groups[(r["sample_type"], track(r), int(r["spd"]), lc)].append(
        {"rt": rt, "y": [v / mx for v in it], "m": r["instrument_model"]})

TICN, TRN, TRMAX = 128, 64, 40
tic_off_axis = 0


def summarise(tr: list) -> dict:
    global tic_off_axis
    n = len(tr)
    nb = C.Counter(len(t["rt"]) for t in tr).most_common(1)[0][0]
    same = [t for t in tr if len(t["rt"]) == nb]
    axis = [S.median(t["rt"][j] for t in same) for j in range(nb)]
    if nb != TICN:
        axis = [axis[0] + (axis[-1] - axis[0]) * j / (TICN - 1) for j in range(TICN)]
    span = axis[-1] - axis[0]
    tic_off_axis += sum(1 for t in tr if abs(t["rt"][0] - axis[0]) > 0.05 * span or abs(t["rt"][-1] - axis[-1]) > 0.05 * span)
    need = max(5, math.ceil(n / 2))
    bands = {k: [] for k in ("p10", "p25", "p50", "p75", "p90")}
    for x in axis:
        col = sorted(v for v in (interp(t["rt"], t["y"], x) for t in tr) if v is not None)
        ok = len(col) >= need
        for k, p in (("p10", .1), ("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9)):
            bands[k].append(round(1000 * q(col, p)) if ok else None)
    step = max(1, n / TRMAX)
    pick = [tr[int(i * step)] for i in range(min(n, TRMAX))]
    tgrid = [axis[0] + span * j / (TRN - 1) for j in range(TRN)]
    traces = [[None if (v := interp(t["rt"], t["y"], x)) is None else round(100 * v) for x in tgrid] for t in pick]
    return {"n": n, "inst": C.Counter(t["m"] for t in tr).most_common(),
            "len": round(axis[-1]), "rt": [round(v, 2) for v in axis], "t0": round(axis[0], 2), "t1": round(axis[-1], 2),
            "b": bands if n >= 5 else None, "tr": traces}


tic_out = []
keys3 = sorted({(k[0], k[1], k[2]) for k in tic_groups})
for s, t, spd in keys3:
    ev, cu = tic_groups.get((s, t, spd, "evosep"), []), tic_groups.get((s, t, spd, "custom"), [])
    for lc, tr in (("evosep", ev), ("custom", cu), ("all", ev + cu)):
        if not tr or (lc == "all" and not (ev and cu)):
            continue          # "all" equals the one LC present; the page reuses it
        tic_out.append({"s": s, "t": t, "spd": spd, "lc": lc, **summarise(tr)})
tic_all = sum(len(v) for v in tic_groups.values())
tic_big = max((c for c in tic_out if c["s"] == "hela" and c["t"] == "DIA"), key=lambda c: c["n"])

facts = {
    "snapshot": "2026-09-29",
    "rows_api": len(rows),
    "rows_with_name": sum(1 for r in rows if r.get("run_name")),
    "dup_groups": dup_groups, "dup_extra": dup_extra, "dup_cross": dup_cross, "dup_by_name": dup_by_name,
    "dup_extra_exact": dup_extra_exact,
    "kept": len(kept), "valid": len(valid),
    "flag_amount": flag_counts.get("amount", 0), "flag_mismatch": flag_counts.get("mismatch", 0),
    "flag_mismatch_top": flag_mismatch_top, "amb_amount": amb_amount,
    "faims": n_faims,
    "labs": len(LABS), "facilities": len(FACS), "models": len({r["instrument_model"] for r in kept}),
    "latest": latest.strftime("%Y-%m-%d"),
    "first": min(instant(r) for r in kept).strftime("%Y-%m-%d"),
    "n_dia": sum(1 for r in valid if track(r) == "DIA"), "n_dda": sum(1 for r in valid if track(r) == "DDA"),
    "nolc_merged": sum(c["nolc"] for c in cohorts if c["lc"] == "nanolc"),
    "unranked_cohorts": len(unranked), "unranked_runs": sum(c["n"] for c in unranked),
    "unv_runs": sum(c["n"] for c in cohorts if c["lc"] == "evosep_unv"),
    "unrec_runs": sum(c["n"] for c in cohorts if c["lc"] == "unrec"),
    "ips": ips, "ips_n_all": len(hela_dia_all),
    "ips_lt60_all": round(100 * sum(1 for r in hela_dia_all if r["ips_score"] < 60) / len(hela_dia_all)),
    "expl38_med": round(q(expl38["v"], .5)), "expl38_n": expl38["n"],
    "expl_ref_med": 25259,           # IPS_REFERENCES[("Exploris 480","medium")] p50, app.py:782
    "global_ref_med": 35000,         # _GLOBAL_REFERENCE p50, app.py:793-798
    "tims_lib_med": round(S.median(tims_lib), 1), "tims_lib_max": round(max(tims_lib), 1),
    "tims_lib_gt90": sum(1 for x in tims_lib if x > 90),
    "two_col_cohorts": two_col,
    "multi_fac_cohorts": sum(1 for c in cohorts if c["fac"] >= 2),
    "tic_default": f"{tic_big['spd']} SPD", "tic_default_n": tic_big["n"], "tic_total": tic_all,
    "tic_raw_total": len(tic_raw), "tic_dups": tic_dups, "tic_off_axis": tic_off_axis, "tic_summary_kb": round(len(json.dumps(tic_out, separators=(",", ":"))) / 1024),
}
pegsum = {
    "family": peg["family"], "spd": peg["spd"], "window": peg["window_days"],
    "n_labs": peg["community"]["n_labs"], "n_runs": peg["community"]["n_runs"],
    "p25": peg["community"]["p25_pct"], "med": peg["community"]["median_pct"], "p75": peg["community"]["p75_pct"],
    "cohorts": peg["cohorts"],
    "weeks": [w["p50"] for w in pegt["weeks"]],
    "w0": pegt["weeks"][0]["week_start"], "w1": pegt["weeks"][-1]["week_start"],
}
# every field the public API returns today, by name only (no values)
fields = [k for k in rows[0].keys() if k not in ("run_name", "fingerprint")]  # Decision 1 removes these two

DATA = {"models": MODELS, "labs": LABS, "vendor": VENDOR, "cohorts": cohorts, "runs": runs,
        "flagged": flag_rows, "hist": hist, "facts": facts, "peg": pegsum, "fields": fields,
        "evosep": {str(k): v for k, v in EVOSEP_METHODS.items()},
        "libsize": {"bruker": 54000, "thermo": 170000}, "tic": tic_out}

payload = "window.STAN_MOCK = " + json.dumps(DATA, separators=(",", ":"), ensure_ascii=False) + ";"
# guard: no raw filename may reach the page (D4)
names = {r["run_name"] for r in rows if r.get("run_name")}
assert not any(n in payload for n in names if len(n) > 8), "a raw filename leaked into the page data"

html = TEMPLATE.read_text().replace("/*__DATA__*/", payload)
OUT.write_text(html)

print(f"rows {len(rows)} -> kept {len(kept)} (dup groups {dup_groups}, extra {dup_extra} [exact-instant key: {dup_extra_exact}], cross-pseudonym {dup_cross}; by-name groups {dup_by_name})")
print(f"held back: {dict(flag_counts)}  top mismatch primaries {flag_mismatch_top}  ambiguous >5ug parses {amb_amount}  faims {n_faims}")
print(f"valid {len(valid)}  DIA {facts['n_dia']}  DDA {facts['n_dda']}  cohorts {len(cohorts)}  unranked {len(unranked)} ({facts['unranked_runs']} runs; unv {facts['unv_runs']}, nolc {facts['unrec_runs']})  no-LC merged into nanoLC {facts['nolc_merged']}  multi-facility cohorts {facts['multi_fac_cohorts']}")
print("IPS:", json.dumps(ips), f"all HeLa DIA <60 {facts['ips_lt60_all']}%  Exploris 38 SPD median {facts['expl38_med']} (n {facts['expl38_n']})")
for c in cohorts:
    if c["s"] == "hela":
        print(f"  {MODELS[c['m']]:24s} {c['t']} {c['lc']:10s} {c['a']:7s} n={c['n']:4d} why={c['why'] or '-':6s} nolc={c['nolc']:3d} labs={c['labs']} {c['g']}")
print(f"TIC: {len(tic_raw)} traces, {tic_dups} duplicate copies dropped, {tic_all} kept in {len(tic_out)} SPD x LC cohorts; default {tic_big['spd']} SPD {tic_big['lc']} n={tic_big['n']}; off-axis traces {tic_off_axis}; tic json {len(json.dumps(tic_out, separators=(',', ':')))/1024:.0f} KB")
print(f"payload {len(payload)/1024:.0f} KB -> {OUT.name} {OUT.stat().st_size/1024:.0f} KB")
