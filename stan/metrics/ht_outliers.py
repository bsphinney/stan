"""Outlier detection for a high-throughput submission.

The question this answers is "which wells in this batch should be re-run?",
and the useful comparison is against the batch itself, not against a global
threshold. A submission is one customer's samples, prepared together and run
back to back on one method, so its siblings are the right control: 500 M
counts of MS1 signal may be normal for one sample type and a failure for
another, but a well an order of magnitude below its own plate-mates is
suspect whatever the sample is.

Robust statistics throughout — median and MAD rather than mean and standard
deviation. A tray with three dead wells would drag a mean badly enough to
hide the very samples being looked for, and inflate the SD enough to hide
the rest.

Directionality matters and is set per metric: unusually HIGH signal is not a
reason to re-run a sample, whereas unusually low is. Flagging both tails
would send good samples back to the queue.
"""

from __future__ import annotations

import logging
import math
import re

logger = logging.getLogger(__name__)

#: Modified z-score threshold. Iglewicz & Hoaglin's conventional 3.5 —
#: deliberately not tuned to make the output look good on one batch.
Z_THRESHOLD = 3.5

#: Fraction a value must differ from the batch median by, when MAD is zero
#: and a z-score is therefore undefined. Half the batch's level is a large,
#: explainable difference -- not a threshold tuned to make one plate's output
#: look tidy.
REL_THRESHOLD = 0.5

#: Below this many samples the batch cannot define its own normal: MAD over
#: four points is noise, and one bad well would make its neighbours look like
#: outliers. Small submissions fall back to the per-sample verdict alone.
MIN_COHORT = 6

#: (column, direction, human label). direction "low" flags the lower tail
#: only, "high" the upper, "both" either side.
_METRICS: tuple[tuple[str, str, str], ...] = (
    ("ms1_max_intensity", "low", "MS1 max intensity"),
    ("ms1_total_tic", "low", "total ion current"),
    ("dynamic_range_log10", "low", "dynamic range"),
    ("dropout_rate_per_100_ms1", "high", "MS1 dropouts"),
    ("rt_duration_min", "both", "run duration"),
    ("n_ms2_frames", "low", "MS2 frame count"),
)


def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _mad(xs: list[float], med: float) -> float:
    """Median absolute deviation, the robust analogue of a standard deviation."""
    return _median([abs(x - med) for x in xs])


def _numeric(rows: list[dict], col: str) -> list[tuple[int, float]]:
    out = []
    for i, r in enumerate(rows):
        v = r.get(col)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out.append((i, f))
    return out


def find_outliers(
    rows: list[dict],
    z_threshold: float = Z_THRESHOLD,
    min_cohort: int = MIN_COHORT,
    rel_threshold: float = REL_THRESHOLD,
) -> dict:
    """Flag samples in one submission that look unlike their batch-mates.

    Args:
        rows: sample_health rows for a single submission.
        z_threshold: modified z-score above which a sample is flagged.
        min_cohort: below this many samples, statistics are skipped.

    Returns a dict with the annotated rows, the per-metric batch stats used
    (so the UI can show what "normal" was), and the subset needing a re-run.
    Every flag carries the numbers behind it: a bare "outlier" badge is not
    something anyone should re-run a customer's sample on.
    """
    rows = [dict(r) for r in rows]
    for r in rows:
        r["outlier_reasons"] = []
        r["is_outlier"] = False

    stats: dict[str, dict] = {}
    cohort_ok = len(rows) >= min_cohort

    if cohort_ok:
        for col, direction, label in _METRICS:
            pairs = _numeric(rows, col)
            if len(pairs) < min_cohort:
                continue
            vals = [v for _, v in pairs]
            med = _median(vals)
            mad = _mad(vals, med)
            # MAD is zero whenever more than half the batch shares one exact
            # value. Two very different situations produce that, and they
            # need opposite treatment:
            #
            #  * A uniform plate with one dead well (median 1e10, well 1e6).
            #    That is the case this feature exists to catch.
            #  * A metric that simply sits at zero for most runs. On real
            #    submission 0793, dropouts are 0.0 for 69 of 88 samples, so
            #    ANY non-zero value is infinitely many "deviations" out. A
            #    mean-absolute-deviation fallback flagged seven samples over
            #    a 0.32-per-100 dropout rate -- three tenths of one percent,
            #    statistically extreme and practically meaningless.
            #
            # So the zero-MAD fallback is a RELATIVE test, which needs a
            # non-zero median to be defined. Where the median is zero the
            # metric is left alone: without domain knowledge of what counts
            # as a material amount, flagging on it is guesswork, and a
            # re-run list nobody trusts is worse than a shorter one.
            stats[col] = {"median": med, "mad": mad, "n": len(vals),
                          "label": label, "direction": direction,
                          "rule": "mad" if mad > 0 else
                                  ("relative" if med else "skipped")}
            for i, v in pairs:
                if mad > 0:
                    z = 0.6745 * (v - med) / mad
                    if direction == "low" and z > -z_threshold:
                        continue
                    if direction == "high" and z < z_threshold:
                        continue
                    if direction == "both" and abs(z) < z_threshold:
                        continue
                    z_out = round(z, 1)
                elif med:
                    rel = (v - med) / abs(med)
                    if direction == "low" and rel > -rel_threshold:
                        continue
                    if direction == "high" and rel < rel_threshold:
                        continue
                    if direction == "both" and abs(rel) < rel_threshold:
                        continue
                    z_out = None
                else:
                    continue
                rows[i]["outlier_reasons"].append({
                    "metric": col, "label": label, "value": v,
                    "median": med, "z": z_out, "side": "below" if v < med else "above",
                    "pct_of_median": round(v / med * 100, 1) if med else None,
                })
                rows[i]["is_outlier"] = True

    # An explicit fail/warn verdict stands on its own, whatever the batch
    # looks like — if every well in a tray is bad, none of them is a
    # statistical outlier and all of them still need attention.
    for r in rows:
        verdict = str(r.get("verdict") or "").lower()
        if verdict in ("fail", "warn"):
            r["is_outlier"] = True
            reasons = r.get("reasons")
            if isinstance(reasons, str):
                reasons = [reasons]
            r["outlier_reasons"].append({
                "metric": "verdict", "label": f"health check {verdict}",
                "value": None, "median": None, "z": None, "side": verdict,
                "detail": list(reasons or []),
            })

    needs_rerun = [r for r in rows if r["is_outlier"]]
    return {
        "rows": rows,
        "stats": stats,
        "cohort_size": len(rows),
        "cohort_ok": cohort_ok,
        "min_cohort": min_cohort,
        "z_threshold": z_threshold,
        "needs_rerun": needs_rerun,
        "n_needs_rerun": len(needs_rerun),
    }


# ── 96-well plate mapping ──────────────────────────────────────────
#
# Well position is encoded in the filename by the acquisition software as
# `_S<plate>-<row><col>_`, e.g. `..._60SPD_DIA-SK-10_S3-E7_1_23686.d` is
# plate 3, row E, column 7. Verified against the Flinders archive: rows A-H,
# columns 1-12, i.e. a standard 96-well plate.

_WELL_RE = re.compile(r"_S(\d+)-([A-H])(\d{1,2})_")

PLATE_ROWS = ("A", "B", "C", "D", "E", "F", "G", "H")
PLATE_COLS = tuple(range(1, 13))


def parse_well(run_name: str) -> dict | None:
    """Plate/row/column from a run filename, or None if it carries no well."""
    m = _WELL_RE.search(str(run_name or ""))
    if not m:
        return None
    col = int(m.group(3))
    if not 1 <= col <= 12:
        return None
    return {"plate": f"S{m.group(1)}", "row": m.group(2), "col": col}


def is_edge_well(row: str, col: int) -> bool:
    """True for the outer ring — the wells that evaporate and pipette worst."""
    return row in ("A", "H") or col in (1, 12)


def plate_map(rows: list[dict], metric: str = "ms1_total_tic") -> dict:
    """Lay the batch out by well and test for an edge effect.

    Colouring a plate is only half the question. Eyes find patterns in noise,
    so the edge-vs-interior comparison is computed rather than left to
    judgement: outer-ring median against interior median, expressed as a
    percentage difference. Medians again, because one dead well on an edge
    would otherwise look like an edge effect.
    """
    plates: dict[str, dict] = {}
    for r in rows:
        w = parse_well(r.get("run_name", ""))
        if not w:
            continue
        # A HeLa standard is judged on identifications, not raw current: a
        # dirty source still makes ions, it just stops identifying peptides.
        val = r.get("n_precursors") if r.get("kind") == "qc" else r.get(metric)
        try:
            val = float(val) if val is not None else None
        except (TypeError, ValueError):
            val = None
        p = plates.setdefault(w["plate"], {"plate": w["plate"], "wells": {}})
        p["wells"][f"{w['row']}{w['col']}"] = {
            "row": w["row"], "col": w["col"], "value": val,
            "kind": r.get("kind", "sample"),
            "run_name": r.get("run_name"), "verdict": r.get("verdict"),
            "is_outlier": bool(r.get("is_outlier")),
            "is_edge": is_edge_well(w["row"], w["col"]),
        }

    for p in plates.values():
        # Sample wells only. Standards carry precursor counts, which are
        # orders of magnitude away from a TIC, so including them would make
        # the colour scale useless and the edge comparison meaningless.
        sample_wells = [w for w in p["wells"].values()
                        if w.get("kind") != "qc" and w["value"] is not None]
        vals = [w["value"] for w in sample_wells]
        edge = [w["value"] for w in sample_wells if w["is_edge"]]
        inner = [w["value"] for w in sample_wells if not w["is_edge"]]
        p["min"] = min(vals) if vals else None
        p["max"] = max(vals) if vals else None
        p["median"] = _median(vals) if vals else None
        p["n_wells"] = len(p["wells"])
        # Both sides need enough wells to have a median worth comparing.
        if len(edge) >= 3 and len(inner) >= 3:
            em, im = _median(edge), _median(inner)
            p["edge_effect"] = {
                "edge_median": em, "interior_median": im,
                "n_edge": len(edge), "n_interior": len(inner),
                "pct_diff": round((em - im) / im * 100, 1) if im else None,
            }
        else:
            p["edge_effect"] = None

    return {"metric": metric,
            "plates": [plates[k] for k in sorted(plates)],
            "rows": list(PLATE_ROWS), "cols": list(PLATE_COLS)}


# ── injection order ────────────────────────────────────────────────
#
# The trailing token is the acquisition counter: `..._S6-A9_1_24080.d` is
# injection 24080. It is the only reliable ordering available -- run_date on
# sample_health is the time the monitor job ran, not the time of acquisition,
# and well position is meaningless as an order because injection order is
# deliberately randomised against plate layout.

_INJECTION_RE = re.compile(r"_(\d+)\.d$", re.IGNORECASE)


def parse_injection(run_name: str) -> int | None:
    """Acquisition sequence number from the filename, or None."""
    m = _INJECTION_RE.search(str(run_name or "").strip())
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def queue_series(rows: list[dict]) -> dict:
    """Signal against injection order, for spotting a drifting instrument.

    Answers "is the source getting dirty as this queue runs?". Customer
    samples are the noisy series -- they differ from each other by design, so
    a downward slope across them can just mean the plate was loaded with
    different material. The interspersed HeLa standards are the control:
    identical material, so any trend in *those* is the instrument, not the
    samples. Both series are returned and the trend is computed over the
    standards when there are enough of them.
    """
    pts = []
    for r in rows:
        inj = parse_injection(r.get("run_name", ""))
        if inj is None:
            continue
        pts.append({
            "injection": inj,
            "run_name": r.get("run_name"),
            "kind": r.get("kind", "sample"),
            "tic": r.get("ms1_total_tic"),
            "n_precursors": r.get("n_precursors"),
            "verdict": r.get("verdict"),
            "is_outlier": bool(r.get("is_outlier")),
        })
    pts.sort(key=lambda p: p["injection"])

    def _trend(series: list[dict], key: str) -> dict | None:
        """Least-squares slope over the series, as % change end-to-end.

        Reported as a percentage of the starting level so it is readable
        without knowing the units, and only when there are enough points for
        a line to mean anything.
        """
        vals = [(p["injection"], float(p[key])) for p in series
                if p.get(key) is not None]
        if len(vals) < 4:
            return None
        n = len(vals)
        mx = sum(x for x, _ in vals) / n
        my = sum(y for _, y in vals) / n
        denom = sum((x - mx) ** 2 for x, _ in vals)
        if denom <= 0:
            return None
        slope = sum((x - mx) * (y - my) for x, y in vals) / denom
        first_x, last_x = vals[0][0], vals[-1][0]
        start = my + slope * (first_x - mx)
        change = slope * (last_x - first_x)
        return {
            "n": n, "slope_per_injection": slope,
            "pct_change_over_queue": round(change / start * 100, 1) if start else None,
            "first_injection": first_x, "last_injection": last_x,
        }

    standards = [p for p in pts if p["kind"] == "qc"]
    samples = [p for p in pts if p["kind"] != "qc"]
    return {
        "points": pts,
        "n_points": len(pts),
        "standards_trend_tic": _trend(standards, "tic"),
        "standards_trend_precursors": _trend(standards, "n_precursors"),
        "samples_trend_tic": _trend(samples, "tic"),
        "n_standards": len(standards),
        "n_samples": len(samples),
    }


# ── submission matching ────────────────────────────────────────────

_SUBMISSION_TOKEN = re.compile(r"^\d+$")


def matches_submission(run_name: str, query: str) -> bool:
    """Does this filename belong to the given submission?

    A plain substring test is wrong for a numeric submission in two ways,
    both seen in the real archive for submission 0793:

    * The operator's number is zero-padded (`0793`) while the filename
      carries it bare (`..._793_100spd_...`), so a literal search finds
      nothing at all.
    * The trailing acquisition counter contains the same digits by
      coincidence -- `20aug26_GallEV_60spd_med6_S4-F12_1_23793.d` and
      `07102026_HE50_60-spd-dia-_S1-A2_1_22793.d` both contain "793" and
      belong to other submissions entirely. Including them would put another
      customer's samples on this plate map.

    So a numeric query matches the submission *token* -- delimited, with
    leading zeros ignored on both sides. Anything non-numeric (`SK-`, a
    project code) keeps the substring behaviour, which is what makes those
    findable at all.
    """
    name = str(run_name or "")
    q = str(query or "").strip()
    if not q:
        return False
    if _SUBMISSION_TOKEN.match(q):
        bare = q.lstrip("0") or "0"
        return re.search(rf"(?:^|[_-])0*{re.escape(bare)}(?:[_-]|$)", name) is not None
    return q.lower() in name.lower()
