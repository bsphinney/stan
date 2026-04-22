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
    intensity_pct: float = 0.0           # matched intensity / total MS1 TIC × 100
    peg_score: float = 0.0               # 0..100, see compute_peg_score
    peg_class: str = "clean"             # clean | trace | moderate | heavy
    matches: list[PegMatch] = field(default_factory=list)
    total_intensity: float = 0.0         # sum of all peak intensities scanned


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

    for scan in spectra:
        for mz, intensity in scan:
            if intensity < intensity_threshold:
                continue
            total_intensity += intensity
            m = _match_peak_to_ion(mz, intensity, ref, tolerance_ppm)
            if m is not None:
                matches.append(m)
                matched_intensity += intensity
                seen_ions.add((m.ion.n, m.ion.adduct, m.ion.charge))

    n_detected = len(seen_ions)
    intensity_pct = (
        100.0 * matched_intensity / total_intensity if total_intensity > 0 else 0.0
    )
    score = compute_peg_score(n_detected, intensity_pct, n_reference=len(ref))
    return PegResult(
        n_ions_detected=n_detected,
        n_ions_reference=len(ref),
        intensity_pct=intensity_pct,
        peg_score=score,
        peg_class=classify_peg_score(score),
        matches=matches,
        total_intensity=total_intensity,
    )


def compute_peg_score(
    n_detected: int, intensity_pct: float, n_reference: int = 143
) -> float:
    """Composite PEG score 0..100.

    Combines two signals:
      - Breadth: how many reference ions were matched (saturates at 15 — once
        you see 15 different PEG oligomers it's clearly a ladder, not noise)
      - Magnitude: what fraction of total MS1 intensity is PEG (saturates at 10%)

    A run with 8 ions covering 6% of TIC scores ~50; a run with 20 ions
    covering 20% of TIC scores ~100.
    """
    breadth = min(n_detected / 15.0, 1.0)
    magnitude = min(intensity_pct / 10.0, 1.0)
    return 40.0 * breadth + 60.0 * magnitude


def classify_peg_score(score: float) -> str:
    """Map a peg_score to a 4-class label.

    Thresholds tuned around the score formula above:
      clean    < 20  — typical baseline for clean labs (a few stray PEG hits)
      trace   20-50  — measurable PEG, common with shared plasticware
      moderate 50-70 — clearly contaminated, fix sample prep before next QC
      heavy    > 70  — sample is dominated by PEG, hold from community
    """
    if score < 20:
        return "clean"
    if score < 50:
        return "trace"
    if score < 70:
        return "moderate"
    return "heavy"
