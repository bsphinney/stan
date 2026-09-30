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


# B2/B4 amendment (Brett, 2026-09-29): nanoLC cohorts are keyed by a fixed band on the
# ACTIVE GRADIENT plus LC model and flow regime, so two labs on the same LC, flow and band
# land in one cohort and can be ranked against each other. gradient_length_min records the
# acquisition length (Exploris 38 SPD runs store 44 min; their TIC ends at 30 min), so the
# gradient is recovered from the stored SPD, which gradient_min_to_spd derived from it:
# gradient = 1440 / (1.25 x SPD), one decimal. Evosep cohorts keep the method (SPD);
# unverified-Evosep and LC-not-recorded groups keep their SPD, as before.
# Bands: <=20 -> 15, 21-37 -> 30, 38-52 -> 45, 53-75 -> 60, 76-105 -> 90, 106-150 -> 120, >150 -> 180.
# Gradients are fractional, so the integer gaps close at the half minute (20.5, 37.5, ...).
GRAD_BANDS = [(20.5, 15), (37.5, 30), (52.5, 45), (75.5, 60), (105.5, 90), (150.5, 120)]   # (exclusive upper bound, band); else 180
BAND_RANGE = {15: "≤20 min", 30: "21–37 min", 45: "38–52 min", 60: "53–75 min", 90: "76–105 min", 120: "106–150 min", 180: ">150 min"}


def grad_band(mins) -> int:
    for hi, b in GRAD_BANDS:
        if mins < hi:
            return b
    return 180


def gradient_min(spd) -> float:
    """Active gradient behind a stored nanoLC SPD (inverse of gradient_min_to_spd)."""
    return round(1440 / (1.25 * spd), 1)


def lc_model(r):
    """The snapshot records no LC model; never invent one."""
    return r.get("lc_model") or None


def flow_regime(r):
    """The snapshot records no flow regime (nanoflow / capillary / microflow)."""
    return r.get("flow_regime") or None


def spd_txt(spds: C.Counter) -> str:
    """Derived SPD for a band: the common value if it holds 90% of runs, else the range."""
    top, n = spds.most_common(1)[0]
    tot = sum(spds.values())
    return f"~{top}" if n >= 0.9 * tot else f"~{min(spds)}–{max(spds)}"


def grad_label(lc: str, g: int, lens: list[int], spds: C.Counter) -> str:
    if lc == "evosep":
        return EVOSEP_METHODS[g]
    if lc == "evosep_unv":
        return f"Evosep, {runlen_label(lens)} (SPD {g} unverified)"
    if lc == "nanolc":
        return f"nanoLC · {g} min gradient band ({spd_txt(spds)} SPD)"
    return f"{g} SPD, LC not recorded ({runlen_label(lens)})"


valid = [r for r in kept if r["submission_id"] not in flag_reason]
flagged = [r for r in kept if r["submission_id"] in flag_reason]


def ckey(r):
    lc = lc_class(r)
    if lc == "nanolc":
        return (r["sample_type"], r["instrument_model"], track(r), lc, grad_band(gradient_min(int(r["spd"]))), abucket(r["amount_ng"]), lc_model(r), flow_regime(r))
    return (r["sample_type"], r["instrument_model"], track(r), lc, int(r["spd"]), abucket(r["amount_ng"]), None, None)


# the v2 key (exact stored SPD for nanoLC), kept only to count what the amendment changes
ckey_v2 = lambda r: (r["sample_type"], r["instrument_model"], track(r), lc_class(r), int(r["spd"]), abucket(r["amount_ng"]))
coh_rows: dict[tuple, list] = C.defaultdict(list)
for r in valid:
    coh_rows[ckey(r)].append(r)

LC_ORDER = ["evosep", "nanolc", "evosep_unv", "unrec"]
rep_spd = {k: C.Counter(int(r["spd"]) for r in rs).most_common(1)[0][0] for k, rs in coh_rows.items()}
order = sorted(coh_rows, key=lambda k: (SAMPLES.index(k[0]), MODELS.index(k[1]), k[2], k[5], LC_ORDER.index(k[3]), -rep_spd[k], -k[4]))
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
    spds = C.Counter(int(r["spd"]) for r in rs)
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
        "s": k[0], "m": MODELS.index(k[1]), "t": k[2], "lc": lc, "spd": rep_spd[k], "a": k[5],
        "band": k[4] if lc == "nanolc" else None, "lcm": k[6], "flow": k[7],
        "g": grad_label(lc, k[4], lens, spds),
        "n": len(rs), "why": why,
        "labs": sorted({LABS.index(r["display_name"]) for r in rs}),
        "fac": len({FACILITY.get(r["display_name"], r["display_name"]) for r in rs}),
        "nolc": sum(1 for r in rs if not (r.get("lc_system") or "")),
        "lens": [[x, c] for x, c in C.Counter(lens).most_common()],
        "spds": [[x, c] for x, c in spds.most_common()],
        "v": v, "pep": pep,
        "prot": [round(q(prot, .25)), round(q(prot, .5)), round(q(prot, .75))],
        "pts": round(med([r["median_points_across_peak"] for r in rs]) or 0, 1),
        # DDA rows carry 0.0 here (not measured); treat 0 as missing so the page prints a dash
        "ppm": (lambda x: round(x, 2) if x else None)(med([r["median_mass_acc_ms1_ppm"] or None for r in rs])),
        "lib": round(S.median(lib), 1) if lib and k[1].startswith("timsTOF") and k[2] == "DIA" else None,
        "d0": dates[0].strftime("%Y-%m"), "d1": dates[-1].strftime("%Y-%m"),
        "cols": [c for c, _ in cols.most_common()],
    })

# ---- what the amendment changed, nanoLC runs only (the other LC classes keep their v2 key)
_nano = [r for r in valid if lc_class(r) == "nanolc"]
_old = C.defaultdict(set)
_new = C.defaultdict(set)
for r in _nano:
    _old[ckey_v2(r)].add(r["submission_id"])
    _new[ckey(r)].add(r["submission_id"])
_old_of = {sid: k for k, ids in _old.items() for sid in ids}
_new_of = {sid: k for k, ids in _new.items() for sid in ids}
band_runs_changed = sum(1 for sid in _new_of if _new[_new_of[sid]] != _old[_old_of[sid]])
_merged = [k for k, ids in _new.items() if len({_old_of[s] for s in ids}) >= 2]
_split = [k for k, ids in _old.items() if len({_new_of[s] for s in ids}) >= 2]
_ranked = lambda groups: sum(1 for ids in groups.values() if len(ids) >= 5)
# a run "moved" if its new cohort is led by a different v2 cohort than its own
_moved = 0
for k, ids in _new.items():
    lead = C.Counter(_old_of[s] for s in ids).most_common(1)[0][0]
    _moved += sum(1 for s in ids if _old_of[s] != lead)
_all_old = C.Counter(ckey_v2(r) for r in valid)
_all_new = C.Counter(ckey(r) for r in valid)
band_stats = {
    "nano_runs": len(_nano), "old": len(_old), "new": len(_new),
    "runs_changed": band_runs_changed, "moved": _moved,
    "cohorts_old": len(_all_old), "cohorts_new": len(_all_new),
    "merged": len(_merged), "merged_from": sum(len({_old_of[s] for s in _new[k]}) for k in _merged),
    "split": len(_split), "ranked_old": _ranked(_old), "ranked_new": _ranked(_new),
    "mixed_spd": sum(1 for c in cohorts if c["lc"] == "nanolc" and len(c["spds"]) > 1),
}
band_mixed = []
for k in order:
    if k[3] != "nanolc":
        continue
    rs = coh_rows[k]
    by = C.defaultdict(list)
    for r in rs:
        by[int(r["spd"])].append(primary(r))
    if len(by) < 2:
        continue
    band_mixed.append({"s": k[0], "m": k[1], "t": k[2], "a": k[5], "band": k[4], "n": len(rs),
                       "med": round(S.median(primary(r) for r in rs)),
                       "parts": [[spd, len(v), round(S.median(v)), gradient_min(spd)] for spd, v in sorted(by.items(), key=lambda kv: -len(kv[1]))]})
band_examples = [f'{m["m"]} {m["t"]} {m["s"]} {m["a"]} · {m["band"]} min band, n {m["n"]}, median {m["med"]}: ' + "; ".join(f"{spd} SPD ({g} min) ×{n} median {md}" for spd, n, md, g in m["parts"]) for m in band_mixed]

# ---------------------------------------------------------------- runs for table, lookup, lab trend and every restored chart
# One row per valid run. Columns 0-7 are unchanged from v2; 8-13 feed the restored
# Plotly charts (ID-free series, Depth by Amount, Matthews & Hayes, Column Comparison).
#   0 date  1 lab  2 cohort  3 prec  4 pep  5 prot  6 psms  7 pts/peak
#   8 |MS1 ppm| (null for DDA: not measured)  9 log10 MS1 signal  10 log10 dynamic range
#  11 amount_ng  12 known-column index (-1 = column not recorded)  13 peak width (s)  14 stored SPD
COLS: list[str] = []


def col_index(r) -> int:
    v, m = (r.get("column_vendor") or "").strip(), (r.get("column_model") or "").strip()
    if not m or m.lower() == "unknown" or v.lower() == "unknown":
        return -1                      # colKey() fix: "Unknown" is not a column
    label = m if m.lower().startswith(v.lower()) else f"{v} {m}".strip()
    if label not in COLS:
        COLS.append(label)
    return COLS.index(label)


def rnd(x, k):
    return None if x is None else round(x, k)


runs = []
for r in sorted(valid, key=lambda r: instant(r)):
    dda = track(r) == "DDA"
    ppm = r.get("median_mass_acc_ms1_ppm")
    sig = r.get("ms1_signal")
    runs.append([
        instant(r).strftime("%Y-%m-%d"), LABS.index(r["display_name"]), cidx[ckey(r)],
        int(r["n_precursors"] or 0), int(r["n_peptides"] or 0), int(r["n_proteins"] or 0), int(r["n_psms"] or 0),
        round(r["median_points_across_peak"] or 0, 1),
        None if (dda or ppm is None) else round(abs(ppm), 2),
        None if (dda or not sig) else round(math.log10(sig), 3),
        None if dda else rnd(r.get("dynamic_range_log10"), 3),
        r["amount_ng"], col_index(r), rnd(r.get("median_peak_width_sec"), 1), int(r["spd"]),
    ])
flag_rows = []
for r in sorted(flagged, key=lambda r: -primary(r)):
    flag_rows.append([
        instant(r).strftime("%Y-%m-%d"), LABS.index(r["display_name"]), MODELS.index(r["instrument_model"]),
        track(r), int(r["spd"]), r["amount_ng"], r["sample_type"],
        int(r["n_precursors"] or 0), int(r["n_peptides"] or 0), int(r["n_proteins"] or 0), int(r["n_psms"] or 0),
        flag_reason[r["submission_id"]],
    ])

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
expl38 = {"v": sorted(primary(r) for r in valid if r["sample_type"] == "hela" and r["instrument_model"] == "Orbitrap Exploris 480" and track(r) == "DIA" and lc_class(r) == "nanolc" and int(r["spd"]) == 38 and abucket(r["amount_ng"]) == "50")}
expl38["n"] = len(expl38["v"])
tims_lib = sorted(r["library_coverage_pct"] for r in kept if r["instrument_family"] == "timsTOF" and r.get("library_coverage_pct") is not None)
two_col = sum(1 for c in cohorts if len(c["cols"]) >= 2)
latest = max(instant(r) for r in kept)
unranked = [c for c in cohorts if c["why"]]
# ---------------------------------------------------------------- TIC overlay (Brett, 2026-09-29: keep it broken out by SPD and LC)
# Spec §A.4. Same grouping as the live chart (SPD x LC x mode, each trace scaled to
# its own peak), with these fixes:
#   * built from `valid` (duplicates removed, held-back runs excluded), like every panel;
#   * LC from lc_class(), the page's one rule: Evosep only at a real Evosep method,
#     "Evosep ... (SPD unverified)" otherwise, and "LC not recorded" never put into Evosep;
#   * the trace is the raw MS1 TIC. Traces whose first bin starts more than 0.1 min after
#     acquisition start are identified-ion traces from STAN 0.2.282/0.2.283 (they begin at
#     the first identification). They never feed the median and are drawn as their own series;
#   * percentiles at the same minute (each trace interpolated onto the cohort's median axis);
#   * bands only from 5+ runs, else every run on its own full axis;
#   * menu labels from the stored gradient_length_min.
import bisect
import gzip

IDION_START = 0.1          # min. Raw MS1 traces start within 0.07 min; identified-ion ones at >= 1.5 min.
tic_raw = {x["submission_id"]: x for x in json.load(open(REV / "api_tic_overlay.json"))["traces"]}
kept_ids = {r["submission_id"] for r in kept}
valid_ids = {r["submission_id"] for r in valid}
tic_dups = sum(1 for r in rows if r["submission_id"] in tic_raw and r["submission_id"] not in kept_ids)
tic_held = sum(1 for r in kept if r["submission_id"] in tic_raw and r["submission_id"] not in valid_ids)


def interp(rt, y, x):
    if x < rt[0] or x > rt[-1]:
        return None
    i = bisect.bisect_left(rt, x)
    if i == 0:
        return y[0]
    x0, x1 = rt[i - 1], rt[i]
    return y[i - 1] if x1 <= x0 else y[i - 1] + (y[i] - y[i - 1]) * (x - x0) / (x1 - x0)


tic_groups: dict[tuple, list] = C.defaultdict(list)
for r in valid:
    x = tic_raw.get(r["submission_id"])
    if not x:
        continue
    rt = json.loads(x["tic_rt_bins"])
    it = json.loads(x["tic_intensity"])
    mx = max(it) if it else 0
    if len(rt) < 2 or mx <= 0 or len(rt) != len(it):
        continue
    t_start = rt[0] - (rt[1] - rt[0]) / 2
    tic_groups[(r["sample_type"], track(r), int(r["spd"]), lc_class(r))].append({
        "id": r["submission_id"], "rt": rt, "y": [v / mx for v in it], "m": MODELS.index(r["instrument_model"]),
        "d": instant(r), "idion": t_start > IDION_START, "len": int(r["gradient_length_min"]),
        "fac": FACILITY.get(r["display_name"], r["display_name"]), "ver": r.get("stan_version") or "",
    })

TICN, TRN, TRMAX = 128, 64, 40


def med_axis(tr: list) -> list:
    nb = C.Counter(len(t["rt"]) for t in tr).most_common(1)[0][0]
    same = [t for t in tr if len(t["rt"]) == nb]
    axis = [S.median(t["rt"][j] for t in same) for j in range(nb)]
    if nb != TICN:
        axis = [axis[0] + (axis[-1] - axis[0]) * j / (TICN - 1) for j in range(TICN)]
    return axis


def off_axis(tr: list, axis: list) -> set:
    span = axis[-1] - axis[0]
    return {t["id"] for t in tr if abs(t["rt"][0] - axis[0]) > 0.05 * span or abs(t["rt"][-1] - axis[-1]) > 0.05 * span}


def full(t):
    return [t["m"], [round(v, 2) for v in t["rt"]], [round(1000 * v) for v in t["y"]]]


def summarise(raw: list, idt: list, parts: list) -> dict:
    """One menu entry: bands from the raw MS1 traces (5+), else each raw run on its own axis."""
    n = len(raw)
    base = raw or idt
    axis = med_axis(base)
    out = {"parts": parts, "n": n, "nid": len(idt),
           "fac": len({t["fac"] for t in raw + idt}),
           "inst": C.Counter(MODELS[t["m"]] for t in raw).most_common(),
           "iinst": C.Counter(MODELS[t["m"]] for t in idt).most_common(),
           "iver": sorted({t["ver"] for t in idt}),
           "b": None, "rt": None, "tr": [], "solo": [], "idt": [full(t) for t in idt]}
    if n >= 5:
        need = max(5, math.ceil(n / 2))
        bands = {k: [] for k in ("p10", "p25", "p50", "p75", "p90")}
        for x in axis:
            col = sorted(v for v in (interp(t["rt"], t["y"], x) for t in raw) if v is not None)
            ok = len(col) >= need
            for k, p in (("p10", .1), ("p25", .25), ("p50", .5), ("p75", .75), ("p90", .9)):
                bands[k].append(round(1000 * q(col, p)) if ok else None)
        out["b"] = bands
        out["rt"] = [round(v, 2) for v in axis]
        # "show all traces": up to 40 runs, evenly spaced in acquisition time, each on its own time axis
        by_t = sorted(raw, key=lambda t: t["d"])
        k = min(n, TRMAX)
        pick = [by_t[round(i * (n - 1) / (k - 1))] if k > 1 else by_t[0] for i in range(k)]
        for t in pick:
            a, b = t["rt"][0], t["rt"][-1]
            grid = [min(b, a + (b - a) * j / (TRN - 1)) for j in range(TRN)]
            out["tr"].append([t["m"], round(a, 2), round(b, 2), [round(100 * interp(t["rt"], t["y"], x)) for x in grid]])
    else:
        out["solo"] = [full(t) for t in raw]
    return out


def part(lc: str, tr: list) -> list:
    raw = [t for t in tr if not t["idion"]]
    return [lc, len(raw), len(tr) - len(raw), runlen_label([t["len"] for t in tr])]


tic_out = []
off_own: set = set()
off_own_id: set = set()
off_union: set = set()
for s, t, spd in sorted({(k[0], k[1], k[2]) for k in tic_groups}):
    present = [lc for lc in LC_ORDER if (s, t, spd, lc) in tic_groups]
    for lc in present:
        tr = tic_groups[(s, t, spd, lc)]
        raw = [x for x in tr if not x["idion"]]
        idt = [x for x in tr if x["idion"]]
        o = off_axis(tr, med_axis(raw or idt))
        off_own |= o
        off_own_id |= {x["id"] for x in idt} & o
        tic_out.append({"s": s, "t": t, "spd": spd, "lc": lc, **summarise(raw, idt, [part(lc, tr)])})
    if len(present) > 1:          # "All LC systems" mixes these; the label and take line say so
        tr = [x for lc in present for x in tic_groups[(s, t, spd, lc)]]
        raw = [x for x in tr if not x["idion"]]
        idt = [x for x in tr if x["idion"]]
        off_union |= off_axis(tr, med_axis(raw or idt))
        tic_out.append({"s": s, "t": t, "spd": spd, "lc": "all",
                        **summarise(raw, idt, [part(lc, tic_groups[(s, t, spd, lc)]) for lc in present])})
off_union |= off_own
tic_all = sum(len(v) for v in tic_groups.values())
tic_idion = sum(1 for v in tic_groups.values() for x in v if x["idion"])
tic_idion_ver = C.Counter(x["ver"] for v in tic_groups.values() for x in v if x["idion"])
tic_big = max((c for c in tic_out if c["s"] == "hela" and c["t"] == "DIA"), key=lambda c: c["n"])
_tic_json = json.dumps(tic_out, separators=(",", ":")).encode()
_tic_bands = json.dumps([{k: v for k, v in c.items() if k not in ("tr", "solo", "idt")} for c in tic_out], separators=(",", ":")).encode()
tic_size = {"all_raw": len(_tic_json), "all_gz": len(gzip.compress(_tic_json, 9)),
            "bands_raw": len(_tic_bands), "bands_gz": len(gzip.compress(_tic_bands, 9))}
tic_live_raw = (REV / "api_tic_overlay.json").stat().st_size

# ---------------------------------------------------------------- Part B: preliminary example calibration (not from the snapshot)
# DIA-NN 2.7.0 library-free (--fasta-search --predictor), MBR off, 1% run FDR, timsTOF HT,
# paired with STAN's standard search of the same raw. Research table, not the site snapshot,
# so the page badges it "Example (preliminary)". Only numbers are read; raw names never leave here.
SCAL = HERE.parent / "scaling" / "paired_raws_vs_stan_standard.tsv"
example = {"engine": "DIA-NN", "version": "2.7.0", "ratio": 0.913, "n_pairs": 8,
           "n_lo": 27690, "n_hi": 40932, "s_lo": 35149, "s_hi": 44071, "spds": [60, 100], "band": 0.08, "lib": 51487}
if SCAL.exists():
    import csv
    pr = [x for x in csv.DictReader(open(SCAL), delimiter="\t")
          if x["cfg"].startswith("2.7.0|predicted.predicted.speclib") and "mbr=False" in x["cfg"]
          and x["n_std"] and x["pg_inst"] == "timsTOF HT"]
    if pr:
        Nn = [float(x["n_ext"]) for x in pr]
        Ss = [float(x["n_std"]) for x in pr]
        example.update(ratio=round(S.median(n / s for n, s in zip(Nn, Ss)), 3), n_pairs=len(pr),
                       n_lo=int(min(Nn)), n_hi=int(max(Nn)), s_lo=int(min(Ss)), s_hi=int(max(Ss)),
                       spds=sorted({int(float(x["spd"])) for x in pr}))
_log = HERE.parent / "scaling" / "stan_example_report_log.txt"
if _log.exists():
    _m = re.search(r"Spectral library loaded: .*? (\d+) precursors", _log.read_text())
    if _m:
        example["lib"] = int(_m.group(1))   # the timsTOF library STAN's standard search used for these pairs

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
    "tic_raw_total": len(tic_raw), "tic_dups": tic_dups, "tic_held": tic_held,
    "tic_idion": tic_idion, "tic_idion_ver": sorted(tic_idion_ver.items()),
    "tic_off_own": len(off_own), "tic_off_own_id": len(off_own_id), "tic_off_union": len(off_union),
    "tic_size": tic_size, "tic_live_raw": tic_live_raw,
    "tic_live_gz": 3017306,          # network.tsv: /api/tic-overlay bytes transferred (gzip) on the live cold load
    "cols_unknown": sum(1 for r in rows if (r.get("column_model") or "Unknown").lower() == "unknown"),
    "band": band_stats, "band_mixed": band_mixed,
}
# ---------------------------------------------------------------- Evosep PEG Watch, drawn as on live 1.2.1
# The snapshot saved the default payloads only: the timsTOF 100 SPD 30-day leaderboard,
# its 52-week trend, and the timsTOF LC comparison. For Exploris and Lumos the capture
# kept what live printed on each LC card (lc_v121.json): runs, labs, median, clean,
# heavy. Their quartiles and weekly lines were not saved, so the page says so.
pegl = json.load(open(REV / "api_peg_lc_compare.json"))
peg_lc = {pegl["family"]: pegl}
_v121 = json.load(open(REV / "lc_v121.json"))["desktop"]["chips"]
for ch in _v121:
    fam = ch["chip"]
    if fam in peg_lc:
        continue
    t = ch["text"]
    groups = []
    for lc, label in (("evosep", "EVOSEP"), ("other", "OTHER LC")):
        seg = t.split(label + "\n", 1)[1] if (label + "\n") in t else ""
        m = re.match(r"(\d+) labs? · ([\d,]+) runs\n([\d.]+)%median PEG share", seg)
        if not m:
            groups.append({"lc": lc, "n_labs": 0, "n_runs": 0, "weekly": []})
            continue
        cl = re.search(r"clean (\d+)%", seg)
        hv = re.search(r"heavy (\d+)%", seg)
        groups.append({"lc": lc, "n_labs": int(m.group(1)), "n_runs": int(m.group(2).replace(",", "")),
                       "median_pct": float(m.group(3)), "p25_pct": None, "p75_pct": None,
                       "clean_pct": int(cl.group(1)) if cl else None, "heavy_pct": int(hv.group(1)) if hv else None,
                       "weekly": None, "partial": True})
    peg_lc[fam] = {"family": fam, "window_days": pegl["window_days"], "as_of": pegl["as_of"], "groups": groups,
                   "families": pegl["families"], "partial": True}
pegsum = {"board": peg, "trend": pegt["weeks"], "lc": peg_lc, "lc_families": pegl["families"]}
# every field the public API returns today, by name only (no values)
fields = [k for k in rows[0].keys() if k not in ("run_name", "fingerprint")]  # Decision 1 removes these two

DATA = {"models": MODELS, "labs": LABS, "vendor": VENDOR, "cohorts": cohorts, "runs": runs,
        "flagged": flag_rows, "facts": facts, "peg": pegsum, "fields": fields,
        "labFac": [FACS.index(FACILITY.get(n, n)) for n in LABS], "cols": COLS,
        "evosep": {str(k): v for k, v in EVOSEP_METHODS.items()},
        "libsize": {"bruker": 54000, "thermo": 170000}, "tic": tic_out, "example": example,
        "bands": [[hi, b] for hi, b in GRAD_BANDS] + [[None, 180]], "bandRange": {str(k): v for k, v in BAND_RANGE.items()}}

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
print(f"TIC: {len(tic_raw)} traces, {tic_dups} duplicate copies dropped, {tic_held} held back; {tic_all} valid traces ({tic_idion} identified-ion: {dict(tic_idion_ver)}) in {len(tic_out)} menu entries; default {tic_big['spd']} SPD {tic_big['lc']} n={tic_big['n']}")
print(f"TIC off-axis (live bin-index method): {len(off_own)} distinct runs in their own LC cohort ({len(off_own_id)} identified-ion), {len(off_union)} counting the combined All views; sizes {tic_size}; live {tic_live_raw}")
print(f"example calibration: {example}")
print(f"known columns: {COLS}")
print(f"nanoLC gradient bands: {band_stats}")
for e in band_examples:
    print("  mixed-SPD band:", e)
for sp in (38, 32, 30, 19, 12, 9):
    print(f"  check: {sp} SPD -> {gradient_min(sp)} min -> {grad_band(gradient_min(sp))} band")
print(f"payload {len(payload)/1024:.0f} KB -> {OUT.name} {OUT.stat().st_size/1024:.0f} KB")
