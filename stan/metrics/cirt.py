"""cIRT (common internal retention time) peptides — endogenous HeLa anchors.

Purpose: track retention-time drift across runs WITHOUT requiring any
spike-in standard. We pick ~10 abundant HeLa tryptic peptides that are
detected in nearly every sample with tight RT reproducibility, then
chart their observed RT over time to catch LC column drift, gradient
shape changes, and solvent problems that don't show up in ID counts.

Why not Biognosys iRT: UC Davis HeLa QC doesn't include spike-in. At
1% FDR, 0/11 Biognosys peptides are detected across our reports.

Why not the Parker et al. 2015 CiRT set (MCP 14(10):2800,
doi:10.1074/mcp.O114.042267): tested against 25 good timsTOF runs at
SPD=100, only 3/14 peptides are usable (≥80% presence, CV<5%).
Insufficient as a panel, though the 3 that work (SYELPDGQVITIGNER,
YFPTQALNFAFK, DSTLIMQLLR) are reasonable late-gradient anchors.

Approach: empirically select per (instrument_family, spd) from a
cohort of good runs. See `derive_panel_from_cohort()` below. The
`EMPIRICAL_CIRT_PANELS` dict at the bottom of this file is seeded
with panels derived from real UC Davis timsTOF data; other
(instrument_family, spd) combos fall back to empirical derivation
when the user runs `stan derive-cirt-panel`.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from pathlib import Path

import polars as pl

logger = logging.getLogger(__name__)


# ── Panel selection ────────────────────────────────────────────────

def derive_rts_for_peptides(
    report_paths: list[Path],
    peptide_seqs: list[str],
    min_presence: float = 0.5,
    q_value_max: float = 0.01,
) -> list[tuple[str, float]]:
    """Compute median RT for a fixed peptide set across a cohort.

    Used by the cross-instrument fallback: when a cohort can't derive
    its own panel (e.g. Exploris with 89 runs spanning months of RT
    drift), borrow peptide sequences from a neighbour cohort that
    DID derive successfully (e.g. Lumos), and re-anchor those
    peptides to this cohort's local RT scale. The peptide identity
    is portable across Thermo Orbitrap instruments — only the RT
    scale isn't.

    Returns peptides RT-sorted, dropping any not present in
    ``min_presence`` fraction of the cohort.
    """
    if not report_paths or not peptide_seqs:
        return []
    seq_set = set(peptide_seqs)
    seq_rts: dict[str, list[float]] = defaultdict(list)
    for path in report_paths:
        try:
            df = pl.read_parquet(
                str(path), columns=["Stripped.Sequence", "RT", "Q.Value"],
            )
        except Exception:
            continue
        df = df.filter(pl.col("Q.Value") < q_value_max)
        df = df.filter(pl.col("Stripped.Sequence").is_in(list(seq_set)))
        g = df.group_by("Stripped.Sequence").agg(pl.col("RT").median().alias("rt"))
        for row in g.iter_rows(named=True):
            seq_rts[row["Stripped.Sequence"]].append(row["rt"])

    if not seq_rts:
        return []
    min_n = max(1, int(min_presence * len(report_paths)))
    out: list[tuple[str, float]] = []
    for seq in peptide_seqs:
        rts = seq_rts.get(seq, [])
        if len(rts) < min_n:
            continue
        out.append((seq, round(sum(rts) / len(rts), 3)))
    out.sort(key=lambda x: x[1])
    return out


def derive_panel_from_cohort(
    report_paths: list[Path],
    n_anchors: int = 10,
    min_presence: float = 0.9,
    max_cv_pct: float = 5.0,
    min_len: int = 9,
    max_len: int = 18,
    q_value_max: float = 0.01,
) -> list[tuple[str, float]]:
    """Pick ~n_anchors peptides to use as cIRT anchors for a cohort.

    A cohort is a set of runs expected to have the same absolute RT
    scale — typically all runs at one (instrument_family, spd).

    Selection criteria:
    - Peptide present at 1% FDR in ≥ min_presence fraction of runs.
    - Median RT CV across runs < max_cv_pct%.
    - Tryptic C-terminus (K or R).
    - Length in [min_len, max_len].
    - Spread across the gradient (RT-binned).

    Args:
        report_paths: DIA-NN report.parquet paths for the cohort.
        n_anchors: Target panel size.
        min_presence: Fraction of runs the peptide must appear in.
        max_cv_pct: Reject peptides with higher RT CV.
        min_len/max_len: Length bounds for the peptide.
        q_value_max: FDR cutoff (per DIA-NN Q.Value column).

    Returns:
        List of (peptide_sequence, reference_rt_min) tuples, RT-sorted.
        Empty list if no cohort or no stable anchors.
    """
    if not report_paths:
        return []

    seq_rts: dict[str, list[float]] = defaultdict(list)
    for path in report_paths:
        try:
            df = pl.read_parquet(
                str(path),
                columns=["Stripped.Sequence", "RT", "Q.Value"],
            )
        except Exception:
            logger.debug("Could not read %s for cIRT derivation", path)
            continue
        df = df.filter(pl.col("Q.Value") < q_value_max)
        # median RT per peptide within this run (collapses charge states)
        g = df.group_by("Stripped.Sequence").agg(pl.col("RT").median().alias("rt"))
        for row in g.iter_rows(named=True):
            seq_rts[row["Stripped.Sequence"]].append(row["rt"])

    if not seq_rts:
        return []

    min_present_n = max(1, int(min_presence * len(report_paths)))

    # Filter to stable candidates
    candidates: list[tuple[str, float, float]] = []  # (seq, mean_rt, cv)
    for seq, rts in seq_rts.items():
        if len(rts) < min_present_n:
            continue
        if not (min_len <= len(seq) <= max_len):
            continue
        if not seq.endswith(("K", "R")):
            continue
        m = sum(rts) / len(rts)
        if m <= 0:
            continue
        sd = (sum((r - m) ** 2 for r in rts) / len(rts)) ** 0.5
        cv = 100 * sd / m
        if cv < max_cv_pct:
            candidates.append((seq, m, cv))

    if not candidates:
        return []

    candidates.sort(key=lambda x: x[1])
    rt_min, rt_max = candidates[0][1], candidates[-1][1]
    if rt_max <= rt_min:
        # Degenerate: all anchors at the same RT
        return [(s, r) for s, r, _ in candidates[:n_anchors]]

    # Pick anchors spread evenly across the gradient
    picks: list[tuple[str, float]] = []
    for i in range(n_anchors):
        target = rt_min + (i + 0.5) * (rt_max - rt_min) / n_anchors
        remaining = [c for c in candidates if c[0] not in {p[0] for p in picks}]
        if not remaining:
            break
        best = min(remaining, key=lambda c: abs(c[1] - target))
        picks.append((best[0], round(best[1], 3)))

    return sorted(picks, key=lambda x: x[1])


# ── Per-run extraction ─────────────────────────────────────────────

def extract_anchor_rts(
    report_path: Path,
    panel: list[tuple[str, float]],
    q_value_max: float = 0.01,
) -> dict[str, float]:
    """Read a single run's observed RT for each anchor peptide.

    Args:
        report_path: DIA-NN report.parquet for one run.
        panel: List of (peptide, reference_rt) — reference_rt is
            ignored here; we just need the peptide set.
        q_value_max: FDR cutoff.

    Returns:
        dict[peptide -> observed_rt_min]. Peptides not detected at
        the FDR cutoff are omitted from the returned dict (not set
        to None) so downstream code can distinguish "wasn't
        extracted yet" from "wasn't detected".
    """
    if not panel:
        return {}
    try:
        df = pl.read_parquet(
            str(report_path),
            columns=["Stripped.Sequence", "RT", "Q.Value"],
        )
    except Exception:
        logger.debug("Could not read %s for anchor RT extraction", report_path)
        return {}

    seqs = {s for s, _ in panel}
    df = df.filter(
        (pl.col("Q.Value") < q_value_max)
        & (pl.col("Stripped.Sequence").is_in(seqs))
    )
    if df.height == 0:
        return {}

    g = df.group_by("Stripped.Sequence").agg(pl.col("RT").median().alias("rt"))
    out: dict[str, float] = {}
    for row in g.iter_rows(named=True):
        out[row["Stripped.Sequence"]] = float(row["rt"])
    return out


def _load_auto_panels() -> dict[tuple[str, int], list[tuple[str, float]]]:
    """Read user-derived panels from ~/.stan/cirt_panels_auto.yml.

    Format (matches the in-code dict):

        - family: Lumos
          spd: 12
          peptides:
            - {seq: DGVLQQPVR, rt: 12.40}
            ...

    These take precedence over the hard-coded EMPIRICAL_CIRT_PANELS
    so labs can derive panels for their own cohorts without editing
    the package. v0.2.218: added so Lumos + Exploris cohorts work
    out of the box once `stan derive-cirt-panel --auto` has run.
    """
    try:
        import yaml
        from stan.config import get_user_config_dir
        path = get_user_config_dir() / "cirt_panels_auto.yml"
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        result: dict[tuple[str, int], list[tuple[str, float]]] = {}
        for entry in data:
            fam = entry.get("family")
            spd = entry.get("spd")
            peptides = entry.get("peptides") or []
            if not fam or spd is None or not peptides:
                continue
            result[(fam, int(spd))] = [
                (p["seq"], float(p["rt"])) for p in peptides
                if "seq" in p and "rt" in p
            ]
        return result
    except Exception:
        return {}


def get_panel(instrument_family: str, spd: int | None) -> list[tuple[str, float]]:
    """Look up a seeded cIRT panel for a given instrument family + SPD.

    Search order:
      1. User-derived panels in ~/.stan/cirt_panels_auto.yml — these
         override the in-package defaults so labs can manage their
         own cohort-specific panels without forking the codebase.
      2. EMPIRICAL_CIRT_PANELS (hard-coded UC Davis panels).

    Returns an empty list if no panel is seeded. Callers should
    trigger empirical derivation via ``stan derive-cirt-panel``
    or ``stan derive-cirt-panel --auto`` when an empty panel is
    returned and there's a cohort available.
    """
    if instrument_family is None or spd is None:
        return []
    key = (instrument_family, int(spd))
    auto = _load_auto_panels()
    if key in auto:
        return list(auto[key])
    return list(EMPIRICAL_CIRT_PANELS.get(key, []))


# ── Seeded panels (derived empirically from UC Davis data) ─────────
#
# Format: (peptide, reference_rt_min).
# Derivation recipe (reproduce with `stan derive-cirt-panel`):
# - Cohort = runs at this (instrument_family, spd) with n_precursors > 10k
# - Peptide present at 1% FDR in ≥ 90% of runs
# - Tryptic C-terminus, length 9–18
# - RT CV < 5% across the cohort
# - Pick 10 peptides spread evenly across the gradient
#
# When the cohort's LC column/gradient changes materially, these
# reference RTs will drift — rebuild the panel and delete stale
# rows in irt_anchor_rts.

EMPIRICAL_CIRT_PANELS: dict[tuple[str, int], list[tuple[str, float]]] = {
    # timsTOF SPD=30 (9 good runs, UC Davis TIMS-10878, April 2026)
    ("timsTOF", 30): [
        ("DGVLQQPVR",        11.82),   # CV=4.47%
        ("LQISTNLQR",        14.90),   # CV=4.31%
        ("SCQFSVDEEFQK",     18.03),   # CV=3.32%
        ("MPEEEDEAPVLDVR",   21.02),   # CV=2.89%
        ("DLNPDVNVFQR",      24.09),   # CV=2.16%
        ("ALNIVDQEGSLLGK",   27.10),   # CV=2.79%
        ("DCEECIQLEPTFIK",   30.10),   # CV=4.35%
        ("LAMLEEDLLALK",     33.13),   # CV=3.20%
        ("EYEIPSNLTPADVFFR", 36.17),   # CV=1.21%
        ("MALELLTQEFGIPIER", 39.20),   # CV=3.37%
    ],
    # timsTOF SPD=60 (24 good runs, UC Davis TIMS-10878, April 2026)
    ("timsTOF", 60): [
        ("DDEYDYLFK",         14.24),  # CV=4.20%
        ("ADSSSVLPSPLSISTK",  14.60),  # CV=4.68%
        ("DQDLITIIGK",        15.27),  # CV=4.36%
        ("PLEDQLPLGEYGLK",    15.79),  # CV=4.17%
        ("DAVLLVFANK",        16.40),  # CV=4.32%
        ("DLLIAYYDVDYEK",     16.88),  # CV=4.98%
        ("DEVLYVFPSDFCR",     17.45),  # CV=4.96%
        ("YYGGAEVVDEIELLCQR", 18.00),  # CV=3.98%
        ("IESEGLLSLTTQLVK",   18.56),  # CV=4.48%
        ("VNALLPTETFIPVIR",   19.11),  # CV=4.05%
    ],
    # timsTOF SPD=100 (30 good runs, UC Davis TIMS-10878, April 2026)
    ("timsTOF", 100): [
        ("TEVSLTLTNK",          5.40),  # CV=2.49%
        ("SFAGAVSPQEEEEFR",     6.00),  # CV=2.49%
        ("TEVNYTQLVDLHAR",      6.76),  # CV=4.79%
        ("LIVDHNIADYMTAK",      7.10),  # CV=3.38%
        ("EEASDYLELDTIK",       7.70),  # CV=4.94%
        ("NIGDLLSSSIDR",        8.29),  # CV=4.43%
        ("YMIGVTYGGDDIPLSPYR",  8.88),  # CV=4.01%
        ("EQLQDMGLEDLFSPEK",    9.48),  # CV=4.19%
        ("NILEESLCELVAK",      10.07),  # CV=3.02%
        ("SGLLVLTTPLASLAPR",   10.70),  # CV=1.00%
    ],
    # Exploris / Lumos / Astral: not yet seeded. Run
    # `stan derive-cirt-panel --instrument <...> --spd <...>` to
    # build an empirical panel once you have a cohort with enough
    # good runs (≥ 10 at >10k precursors).
}
