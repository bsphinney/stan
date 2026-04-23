"""PEG (polyethylene glycol) contamination detection in MS1 spectra.

PEG is a ubiquitous contaminant from plastics, detergents (NP-40, Tween,
Triton X-100, Pluronics), and sample-prep reagents. Its signature in
MS1 is a regularly-spaced ladder of peaks 44.026 Da apart — the
ethylene-oxide repeat unit. Detection is straightforward because the
expected ion m/z values are completely determined by chemistry:

    M(neutral) = n × 44.026215 + 18.010565
    [M+adduct]+ = M + adduct_mass

This module:
  1. Generates the reference m/z list (compute once at import)
  2. Scans an MS1 peak list for matches at user-controlled tolerance
  3. Returns a per-run summary: peg_score 0–100, n_ions, intensity_pct

Pure Python, no IO. The caller (stan/metrics/peg_io.py — TBD) is
responsible for reading MS1 peak lists from raw files via alphatims
(Bruker) or fisher_py (Thermo).

References:
    Rardin 2018 (J Am Soc Mass Spectrom 29, 1327-1330,
        doi:10.1007/s13361-018-1940-z) - the Skyline-based contamination
        screening method. STAN's default PEG reference is aligned to this
        paper: PEG1-20 x {H+, Na+, NH4+} x charge 1 (60 ions total).
        Using generate_peg_reference(extended=True) restores the larger
        n=4-30 x 4 adducts x 2 charges superset for research use.
    Schlosser & Volkmer-Engert, J. Mass Spectrom. 2003 - PEG mass spectra
    Zhou et al., J. Am. Soc. Mass Spectrom. 2018 - common contaminant table
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# ── Reference masses ───────────────────────────────────────────────

PEG_REPEAT_MASS = 44.0262147   # CH2-CH2-O monoisotopic
END_GROUP_MASS  = 18.0105646   # H2O (H- and -OH end groups combined)
PROTON_MASS     = 1.0072765
SODIUM_MASS     = 22.9892214
AMMONIUM_MASS   = 18.0338256
POTASSIUM_MASS  = 38.9637585

# Default adducts match the Rardin 2018 Skyline panel: H+, Na+, NH4+.
# K+ is rare in proteomics buffers and mostly contributes false-positive
# risk from coincidental m/z matches - include via extended=True only.
ADDUCTS_DEFAULT: list[tuple[str, float, str]] = [
    ("[M+H]+",   PROTON_MASS,    "+H"),
    ("[M+Na]+",  SODIUM_MASS,    "+Na"),
    ("[M+NH4]+", AMMONIUM_MASS,  "+NH4"),
]
ADDUCTS_EXTENDED: list[tuple[str, float, str]] = ADDUCTS_DEFAULT + [
    ("[M+K]+",   POTASSIUM_MASS, "+K"),
]
# Back-compat alias - old code that imported ADDUCTS expects all four.
ADDUCTS = ADDUCTS_EXTENDED

# Default range matches Rardin 2018 Skyline panel: PEG1 through PEG20.
# On Bruker timsTOF the scan range typically starts >=200 m/z so PEG1-3
# (m/z 63-173) aren't acquired and the panel effectively starts at PEG4.
# Orbitrap scans usually go lower and capture the full n=1-20 range.
N_MIN_DEFAULT = 1
N_MAX_DEFAULT = 20
MZ_MIN_DEFAULT = 50.0
MZ_MAX_DEFAULT = 1500.0

# Extended range for research use via generate_peg_reference(extended=True).
N_MIN_EXTENDED = 4
N_MAX_EXTENDED = 30
MZ_MIN_EXTENDED = 200.0
MZ_MAX_EXTENDED = 1500.0


@dataclass(frozen=True)
class PegIon:
    """One reference PEG m/z value with provenance."""
    mz: float
    n: int            # PEG degree of polymerization
    adduct: str       # "+H", "+Na", "+NH4", "+K"
    charge: int = 1


def generate_peg_reference(
    n_min: int | None = None,
    n_max: int | None = None,
    mz_min: float | None = None,
    mz_max: float | None = None,
    include_doubly_charged: bool | None = None,
    extended: bool = False,
) -> list[PegIon]:
    """Build the reference PEG ion list.

    Args:
        extended: When True (research mode), use the superset -
            n=4-30, four adducts (+K included), both singly and doubly
            charged species. 199 ions. When False (default, matches
            Rardin 2018): n=1-20, three adducts, singly charged only.
            60 ions.
        n_min, n_max, mz_min, mz_max, include_doubly_charged:
            Explicit overrides. When None, the extended flag decides.
    """
    if extended:
        adducts = ADDUCTS_EXTENDED
        _n_min = N_MIN_EXTENDED if n_min is None else n_min
        _n_max = N_MAX_EXTENDED if n_max is None else n_max
        _mz_min = MZ_MIN_EXTENDED if mz_min is None else mz_min
        _mz_max = MZ_MAX_EXTENDED if mz_max is None else mz_max
        _doubly = True if include_doubly_charged is None else include_doubly_charged
    else:
        adducts = ADDUCTS_DEFAULT
        _n_min = N_MIN_DEFAULT if n_min is None else n_min
        _n_max = N_MAX_DEFAULT if n_max is None else n_max
        _mz_min = MZ_MIN_DEFAULT if mz_min is None else mz_min
        _mz_max = MZ_MAX_DEFAULT if mz_max is None else mz_max
        _doubly = False if include_doubly_charged is None else include_doubly_charged

    out: list[PegIon] = []
    for n in range(_n_min, _n_max + 1):
        m_neutral = n * PEG_REPEAT_MASS + END_GROUP_MASS
        for _label, adduct_mass, short in adducts:
            mz1 = m_neutral + adduct_mass
            if _mz_min <= mz1 <= _mz_max:
                out.append(PegIon(mz=mz1, n=n, adduct=short, charge=1))
            if _doubly:
                # [M + 2*adduct]^2+ = (M + 2*adduct) / 2
                mz2 = (m_neutral + 2 * adduct_mass) / 2
                if _mz_min <= mz2 <= _mz_max:
                    out.append(PegIon(mz=mz2, n=n, adduct=short, charge=2))
    return out


# Computed once at import - the default Rardin-aligned panel.
# Callers wanting the extended superset: generate_peg_reference(extended=True).
PEG_REFERENCE: list[PegIon] = generate_peg_reference()


# ── Detection ──────────────────────────────────────────────────────

@dataclass
class PegMatch:
    """One PEG ion matched in an MS1 spectrum."""
    ion: PegIon
    observed_mz: float
    intensity: float
    ppm_error: float


@dataclass
class PegResult:
    """Per-run PEG detection summary."""
    n_ions_detected: int = 0
    n_ions_reference: int = 0
    intensity_pct: float = 0.0           # matched intensity / total MS1 TIC x 100
    peg_score: float = 0.0               # 0..100, see compute_peg_score
    peg_class: str = "clean"             # clean | trace | moderate | heavy
    matches: list[PegMatch] = field(default_factory=list)
    total_intensity: float = 0.0         # sum of all peak intensities scanned
    # v0.2.168: ladder-coherence check inspired by HowDirty 2024
    # (doi:10.1002/pmic.202300134). PEG oligomers on reverse-phase LC
    # elute in monotonically increasing RT with size n, so if detected
    # ions don't follow that order it's chemical noise coincidentally
    # matching PEG m/z, not real contamination. Score 0..1, fraction
    # of adjacent (n, n+k) oligomer pairs within each adduct where
    # the higher-n ion peaks at a later scan index. 1.0 = perfect
    # ladder; 0.5 = random; <0.5 = likely false positives.
    ladder_coherence: float = 1.0
    n_coherence_pairs: int = 0           # number of adjacent pairs checked


def _match_peak_to_ion(
    obs_mz: float,
    obs_intensity: float,
    reference: list[PegIon],
    tolerance_ppm: float,
) -> PegMatch | None:
    """Return the closest reference ion within tolerance, else None.

    Linear scan — fine for ~150 reference values per peak, but if you're
    matching against tens of thousands of peaks you should pre-sort and
    binary-search. The MS1 spectrum scan loop in detect_peg_in_spectra
    pre-filters by m/z range before calling this so the ref list is
    naturally limited.
    """
    best: PegMatch | None = None
    best_abs_ppm = tolerance_ppm
    for ion in reference:
        ppm = (obs_mz - ion.mz) / ion.mz * 1e6
        if abs(ppm) <= best_abs_ppm:
            best_abs_ppm = abs(ppm)
            best = PegMatch(
                ion=ion, observed_mz=obs_mz,
                intensity=obs_intensity, ppm_error=ppm,
            )
    return best


def detect_peg_in_spectra(
    spectra: Iterable[Iterable[tuple[float, float]]],
    reference: list[PegIon] | None = None,
    tolerance_ppm: float = 5.0,
    intensity_threshold: float = 1e4,
) -> PegResult:
    """Scan a sequence of MS1 spectra for PEG ions.

    Args:
        spectra: iterable of MS1 scans, each being an iterable of
            (m/z, intensity) tuples. Caller is responsible for
            extracting these from raw files (vendor-specific).
        reference: PEG reference list (default = the module-level one).
        tolerance_ppm: mass tolerance for peak matching, default 5 ppm.
        intensity_threshold: peaks below this intensity are ignored,
            avoids matching electronic noise. Default 1e4 — works for
            both Bruker and Thermo on typical proteomics samples.

    Returns:
        PegResult. The matches list collapses to "best match per (ion, scan)"
        BUT a single ion seen in N scans counts as 1 detected ion (not N).
        intensity_pct is the SUM of all matched peak intensities across
        every scan, divided by the total intensity of every scanned peak.
    """
    ref = reference or PEG_REFERENCE
    seen_ions: set[tuple[int, str, int]] = set()  # (n, adduct, charge)
    matches: list[PegMatch] = []
    matched_intensity = 0.0
    total_intensity = 0.0
    # v0.2.168: track peak scan index per ion for the ladder-coherence
    # check. Caller must yield scans in RT (acquisition) order for this
    # to be meaningful — peg_io v0.2.168+ does RT-stratified sampling.
    # peak_scan[(n, adduct, charge)] = (scan_index, peak_intensity)
    peak_scan: dict[tuple[int, str, int], tuple[int, float]] = {}

    for scan_idx, scan in enumerate(spectra):
        for mz, intensity in scan:
            if intensity < intensity_threshold:
                continue
            total_intensity += intensity
            m = _match_peak_to_ion(mz, intensity, ref, tolerance_ppm)
            if m is not None:
                matches.append(m)
                matched_intensity += intensity
                key = (m.ion.n, m.ion.adduct, m.ion.charge)
                seen_ions.add(key)
                # Track the scan where this ion had its maximum
                # intensity — that's our "peak RT" proxy.
                prev = peak_scan.get(key)
                if prev is None or intensity > prev[1]:
                    peak_scan[key] = (scan_idx, float(intensity))

    n_detected = len(seen_ions)
    intensity_pct = (
        100.0 * matched_intensity / total_intensity if total_intensity > 0 else 0.0
    )

    # Ladder coherence: within each adduct family, sort detected ions
    # by oligomer size n, then check adjacent pairs peak in RT order.
    # Requires >= 2 ions per family to contribute a pair.
    n_correct_pairs = 0
    n_total_pairs = 0
    by_adduct: dict[tuple[str, int], list[tuple[int, int]]] = {}
    for (n, adduct, charge), (scan_idx, _) in peak_scan.items():
        by_adduct.setdefault((adduct, charge), []).append((n, scan_idx))
    for pairs in by_adduct.values():
        pairs.sort(key=lambda x: x[0])  # sort by n
        for i in range(len(pairs) - 1):
            n_total_pairs += 1
            # Tie = not wrong (same scan could peak for both). Strict
            # monotonic increase would be ideal but ties are noise-
            # tolerant.
            if pairs[i + 1][1] >= pairs[i][1]:
                n_correct_pairs += 1
    coherence = (n_correct_pairs / n_total_pairs) if n_total_pairs > 0 else 1.0

    score = compute_peg_score(
        n_detected, intensity_pct, n_reference=len(ref),
        ladder_coherence=coherence, n_coherence_pairs=n_total_pairs,
    )
    return PegResult(
        n_ions_detected=n_detected,
        n_ions_reference=len(ref),
        intensity_pct=intensity_pct,
        peg_score=score,
        peg_class=classify_peg_score(score, n_ions_detected=n_detected),
        matches=matches,
        total_intensity=total_intensity,
        ladder_coherence=coherence,
        n_coherence_pairs=n_total_pairs,
    )


def compute_peg_score(
    n_detected: int, intensity_pct: float, n_reference: int = 143,
    ladder_coherence: float = 1.0, n_coherence_pairs: int = 0,
) -> float:
    """Composite PEG score 0..100.

    Combines three signals:
      - Breadth: how many reference ions were matched (saturates at 15
        - once you see 15 different PEG oligomers it's clearly a ladder,
        not noise)
      - Magnitude: what fraction of total MS1 intensity is PEG
        (saturates at 10%)
      - Ladder coherence: v0.2.168+. PEG oligomers on reverse-phase LC
        elute in monotonically increasing RT order. When detected ions
        peak in the correct order, coherence=1.0 and the score is kept
        at the breadth+magnitude value. When ions peak out of order
        (chemical noise coincidentally matching PEG m/z), coherence
        drops and the score is penalized. Only applied when we have
        >= 4 coherence pairs - below that the signal isn't statistically
        meaningful (could be 2 ions peaking randomly).

    A run with 8 coherent ions covering 6% of TIC scores ~50; 20
    coherent ions covering 20% of TIC scores ~100. A run with 8 ions
    covering 20% of TIC but coherence=0.2 (signals at random RTs)
    drops from ~100 to ~30.
    """
    breadth = min(n_detected / 15.0, 1.0)
    magnitude = min(intensity_pct / 10.0, 1.0)
    base = 40.0 * breadth + 60.0 * magnitude
    # Coherence penalty: only applied when we have enough pairs to be
    # statistically meaningful (4+). Scale from 1.0 (no penalty) at
    # coherence=1.0 down to 0.3 (70% discount) at coherence=0.
    if n_coherence_pairs >= 4:
        # Linear scale: coherence=1.0 -> 1.0, coherence=0.0 -> 0.3
        multiplier = 0.3 + 0.7 * ladder_coherence
        return base * multiplier
    return base


def classify_peg_score(score: float, n_ions_detected: int = 999) -> str:
    """Map a peg_score to a 4-class label.

    Thresholds tuned around the score formula above:
      clean    < 20  - typical baseline for clean labs (a few stray PEG hits)
      trace   20-50  - measurable PEG, common with shared plasticware
      moderate 50-70 - clearly contaminated, fix sample prep before next QC
      heavy    > 70  - sample is dominated by PEG, hold from community

    v0.2.169: require n_ions_detected >= 4 before allowing moderate+
    classification. Calibration across 15 Hive timsTOF files 2023-2026
    caught a 2023-12 run that scored 65.3 "moderate" from only 2 ions
    at 16% intensity - one high-intensity peak coincidentally at a PEG
    m/z. Real PEG contamination shows as a LADDER (many oligomers);
    without the breadth we don't trust the classification above "trace".
    """
    if score < 20:
        return "clean"
    if score < 50:
        return "trace"
    # Guard against the low-n_ions high-intensity false positive.
    if n_ions_detected < 4:
        return "trace"
    if score < 70:
        return "moderate"
    return "heavy"
