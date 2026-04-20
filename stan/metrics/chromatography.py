"""Chromatography and instrument performance metrics.

IPS (Instrument Performance Score) is a 0-100 cohort-calibrated score
derived from 359 real UC Davis HeLa QC runs (April 2026 reference set).

The metric uses only inputs we actually measure reliably:
    n_precursors, n_peptides, n_proteins
scored by position within the (instrument_family, spd_bucket) reference
distribution observed in the UCD seed cohort:

    value <= p10   → 0-30   (linear from 0)
    p10 < value <= p50 → 30-60  (linear)
    p50 < value <= p90 → 60-90  (linear)
    value >  p90   → 90-100 (asymptotic, 1.5× p90 = 100)

Weights: 50% precursors + 30% peptides + 20% proteins.
By construction a run that exactly matches its cohort median scores 60.
Runs at or above cohort p90 score 90+. Runs below cohort p10 score <30.

Previous composite (fragments/precursor, points-across-peak, mcr, CV) was
abandoned because those fields are not populated by current STAN extractors
and the formula was dominated by hardcoded constants, giving a near-flat
score with almost no discrimination between good and bad runs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Reference:
    """p10/p50/p90 reference values for a cohort."""
    n: int
    precursors: tuple[int, int, int]
    peptides: tuple[int, int, int]
    proteins: tuple[int, int, int]


# ── Cohort references baked from 359 UCD HeLa runs (April 2026) ─────
# Regenerate via scripts/rebuild_ips_references.py when the seed cohort
# grows. Keyed by (instrument_family, spd_bucket). "*" is a family-wide
# fallback used when the specific SPD bucket has no reference.

IPS_REFERENCES: dict[tuple[str, str], Reference] = {
    ("Exploris 480", "deep"): Reference(
        n=8,
        precursors=(26974, 31698, 35425),
        peptides=(23845, 28545, 32111),
        proteins=(3373, 3993, 4195),
    ),
    ("Exploris 480", "medium"): Reference(
        n=46,
        precursors=(19159, 25259, 29874),
        peptides=(17550, 23020, 27081),
        proteins=(2539, 3104, 3478),
    ),
    ("Lumos", "deep"): Reference(
        n=26,
        precursors=(31423, 39149, 56834),
        peptides=(29066, 35762, 50551),
        proteins=(3982, 4510, 5509),
    ),
    ("Lumos", "medium"): Reference(
        n=64,
        precursors=(16705, 27251, 37839),
        peptides=(15452, 24847, 33727),
        proteins=(2657, 3614, 4347),
    ),
    ("timsTOF HT", "fast"): Reference(
        n=74,
        precursors=(32305, 42778, 48757),
        peptides=(28864, 38195, 43578),
        proteins=(4300, 4768, 5104),
    ),
    ("timsTOF HT", "medium"): Reference(
        n=30,
        precursors=(36153, 45262, 50142),
        peptides=(32779, 40531, 44574),
        proteins=(4730, 4972, 5160),
    ),
    ("timsTOF HT", "ultra"): Reference(
        n=104,
        precursors=(25203, 37051, 45731),
        peptides=(23722, 33106, 40851),
        proteins=(3940, 4509, 4945),
    ),
    # family-level fallbacks (any SPD)
    ("timsTOF HT", "*"): Reference(
        n=208,
        precursors=(30003, 40364, 47857),
        peptides=(26423, 36155, 42531),
        proteins=(4141, 4703, 5068),
    ),
    ("Exploris 480", "*"): Reference(
        n=54,
        precursors=(19159, 25908, 31036),
        peptides=(17550, 23474, 28321),
        proteins=(2539, 3166, 3775),
    ),
    ("Lumos", "*"): Reference(
        n=90,
        precursors=(18519, 30522, 47340),
        peptides=(16941, 27907, 43154),
        proteins=(2965, 3917, 4906),
    ),
}

# Global fallback if family is unknown — pooled across all 352 DIA runs.
_GLOBAL_REFERENCE = Reference(
    n=352,
    precursors=(19000, 35000, 48000),
    peptides=(17000, 31000, 42000),
    proteins=(2900, 4200, 5100),
)


# ── DDA cohort references ──────────────────────────────────────────
# DDA and DIA have fundamentally different PSM/peptide/protein distributions
# on the same instrument — DIA samples everything every cycle, DDA picks top-N.
# Using DIA references for DDA IPS scoring gave catastrophic underestimates
# (a 23k-PSM Exploris 480 DDA run in the top 4% of its cohort scored IPS 49
# because the DIA reference p50 for Exploris 480 was 25k precursors).
# These references are calibrated from real UCD DDA seed runs (n_psms ≥ 1000).
#
# For DDA the `precursors` field is used for n_psms anchoring.
IPS_REFERENCES_DDA: dict[tuple[str, str], Reference] = {
    ("Exploris 480", "*"): Reference(
        n=26,
        precursors=(13705, 18565, 22165),  # n_psms anchors
        peptides=(11335, 16630, 19589),
        proteins=(2730, 3420, 3815),
    ),
    ("timsTOF HT", "*"): Reference(
        n=5,  # small cohort — widen manually later
        precursors=(27005, 27310, 42141),
        peptides=(24596, 24950, 37620),
        proteins=(3917, 4074, 4853),
    ),
    # Lumos DDA: no seed yet (Sage failed, DIA-NN --dda produces 0 IDs on
    # Lumos HCD-IT files). Falls through to absolute anchors.
}

# Absolute-anchor fallback used when a family has no DDA cohort reference.
_DDA_FALLBACK = Reference(
    n=0,
    precursors=(15000, 40000, 80000),  # generic Orbitrap/timsTOF DDA range
    peptides=(12000, 25000, 45000),
    proteins=(2500, 3800, 5000),
)


def spd_bucket(spd: int | float | None) -> str:
    """Bucket samples-per-day into coarse throughput classes.

    deep   ≤15 spd  (long gradient / deep proteome)
    medium 16-40    (standard)
    fast   41-80
    ultra  >80      (short-gradient / high-throughput)
    """
    if spd is None or spd <= 0:
        return "medium"
    if spd <= 15:
        return "deep"
    if spd <= 40:
        return "medium"
    if spd <= 80:
        return "fast"
    return "ultra"


def _get_reference(instrument_family: str | None, spd: int | float | None) -> Reference:
    """Look up cohort reference with graceful fallback."""
    if instrument_family:
        key = (instrument_family, spd_bucket(spd))
        if key in IPS_REFERENCES:
            return IPS_REFERENCES[key]
        # fall back to family-wide
        key2 = (instrument_family, "*")
        if key2 in IPS_REFERENCES:
            return IPS_REFERENCES[key2]
    return _GLOBAL_REFERENCE


def _component_score(value: float, p10: float, p50: float, p90: float) -> float:
    """Piecewise-linear score 0-100 from cohort percentiles.

    Anchors: 0→0, p10→30, p50→60, p90→90, 1.5×p90→100.
    """
    if value is None or value <= 0 or p10 <= 0 or p50 <= 0 or p90 <= 0:
        return 0.0
    if value <= p10:
        return 30.0 * (value / p10)
    if value <= p50:
        return 30.0 + 30.0 * (value - p10) / (p50 - p10)
    if value <= p90:
        return 60.0 + 30.0 * (value - p50) / (p90 - p50)
    excess = min((value - p90) / (0.5 * p90), 1.0)
    return 90.0 + 10.0 * excess


def compute_ips_dia(metrics: dict) -> int:
    """Compute DIA Instrument Performance Score (0-100).

    Cohort-calibrated from 359 real UCD HeLa QC runs. Needs only:
        n_precursors, n_peptides, n_proteins, instrument_family, spd

    Weights: 50% precursors + 30% peptides + 20% proteins.
    A run at its cohort median scores 60.

    Args:
        metrics: Dict from extract_dia_metrics() plus optional
                 instrument_family and spd keys for cohort selection.

    Returns:
        IPS score as integer 0-100.
    """
    ref = _get_reference(metrics.get("instrument_family"), metrics.get("spd"))
    s_prec = _component_score(metrics.get("n_precursors", 0) or 0, *ref.precursors)
    s_pep = _component_score(metrics.get("n_peptides", 0) or 0, *ref.peptides)
    s_pro = _component_score(metrics.get("n_proteins", 0) or 0, *ref.proteins)
    ips = 0.5 * s_prec + 0.3 * s_pep + 0.2 * s_pro
    return int(round(_clamp(ips, 0, 100)))


def _get_dda_reference(instrument_family: str | None) -> Reference:
    """Look up DDA cohort reference with fallback to absolute anchors."""
    if instrument_family:
        ref = IPS_REFERENCES_DDA.get((instrument_family, "*"))
        if ref is not None:
            return ref
    return _DDA_FALLBACK


def compute_ips_dda(metrics: dict) -> int:
    """Compute DDA Instrument Performance Score (0-100).

    DDA uses PSM counts as the depth proxy. Unlike DIA, PSMs/peptides/
    proteins on DDA have fundamentally different distributions on the same
    instrument (DDA picks top-N precursors per cycle rather than sampling
    all), so DDA uses its own cohort reference table keyed only by
    instrument_family — there's not enough DDA seed data yet to bucket
    by SPD as well.

    All three components (PSMs, peptides, proteins) are cohort-calibrated.

    Args:
        metrics: Dict from extract_dda_metrics() plus instrument_family.

    Returns:
        IPS score as integer 0-100.
    """
    ref = _get_dda_reference(metrics.get("instrument_family"))
    s_psms = _component_score(metrics.get("n_psms", 0) or 0, *ref.precursors)
    s_pep = _component_score(metrics.get("n_peptides", 0) or 0, *ref.peptides)
    s_pro = _component_score(metrics.get("n_proteins", 0) or 0, *ref.proteins)
    ips = 0.5 * s_psms + 0.3 * s_pep + 0.2 * s_pro
    return int(round(_clamp(ips, 0, 100)))


# ── iRT deviation ──────────────────────────────────────────────────
#
# v1 of this module used the Biognosys iRT kit peptides below with
# absolute RT values as the reference panel. That made the metric
# always return 0 on UC Davis data because nobody spikes Biognosys.
# v0.2.125 replaces the default reference with the empirical
# `EMPIRICAL_CIRT_PANELS` in stan.metrics.cirt, which is keyed by
# (instrument_family, spd) and derived from each lab's own history.
#
# The Biognosys table is kept here for use ONLY when a lab spikes
# iRT standards — pass it explicitly as `reference_rts` to
# compute_irt_deviation. It is no longer used as a default.

DEFAULT_IRT_LIBRARY: dict[str, float] = {
    # Biognosys iRT kit (Escher et al. 2012, J. Proteome Res.,
    # doi:10.1021/pr300542g). Values are iRT units (0–100), not minutes —
    # a caller that wants to use this has to linear-regress the
    # observed absolute RTs of the found peptides against these
    # reference iRT values to build a conversion, then compute
    # deviations in iRT units. Deprecated as a default because most
    # UC Davis labs don't spike this kit.
    "LGGNEQVTR": 0.0,
    "GAGSSEPVTGLDAK": 26.1,
    "VEATFGVDESNAK": 33.4,
    "YILAGVENSK": 42.3,
    "TPVISGGPYEYR": 54.6,
    "TPVITGAPYEYR": 57.3,
    "DGLDAASYYAPVR": 64.2,
    "ADVTPADFSEWSK": 67.7,
    "GTFIIDPGGVIR": 70.7,
    "GTFIIDPAAVIR": 87.2,
    "LFLQFGAQGSPFLK": 100.0,
}


def compute_irt_deviation(
    report_path: Path,
    instrument_family: str | None = None,
    spd: int | None = None,
    reference_rts: dict[str, float] | None = None,
    min_peptides: int = 3,
) -> dict:
    """RT deviation of the cIRT anchor panel for this run.

    Default behavior (recommended): pass `instrument_family` + `spd`,
    look up the empirical panel seeded per (family, spd) in
    stan.metrics.cirt.EMPIRICAL_CIRT_PANELS, compute each anchor's
    observed-minus-reference deviation in minutes, return the max
    and median.

    Override: pass `reference_rts` explicitly (e.g. Biognosys) when
    the lab spikes a known iRT standard.

    Returns:
        {"max_deviation_min", "median_deviation_min", "n_irt_found"}

    When fewer than `min_peptides` anchors are detected at 1% FDR,
    max/median return None (distinct from "zero deviation") so GRS
    scoring can skip the metric cleanly. The n_irt_found field still
    reports the actual count so operators can see why the metric
    was skipped.
    """
    null_result = {
        "max_deviation_min": None,
        "median_deviation_min": None,
        "n_irt_found": 0,
    }

    # Build the reference panel. Explicit override wins; otherwise
    # look up the empirical panel for (family, spd).
    panel: list[tuple[str, float]] = []
    if reference_rts is not None:
        panel = list(reference_rts.items())
    else:
        from stan.metrics.cirt import get_panel
        panel = get_panel(instrument_family or "", spd)

    if not panel:
        return null_result

    # Delegate the report-read + per-peptide median RT to cirt.py so
    # there's one code path for "read anchor RTs from a report".
    from stan.metrics.cirt import extract_anchor_rts
    observed = extract_anchor_rts(report_path, panel)
    if len(observed) < min_peptides:
        null_result["n_irt_found"] = len(observed)
        return null_result

    ref_map = {seq: ref for seq, ref in panel}
    deviations = [
        abs(rt - ref_map[seq])
        for seq, rt in observed.items()
        if seq in ref_map
    ]
    if not deviations:
        null_result["n_irt_found"] = len(observed)
        return null_result

    deviations.sort()
    return {
        "max_deviation_min": max(deviations),
        "median_deviation_min": deviations[len(deviations) // 2],
        "n_irt_found": len(deviations),
    }


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
