"""Community scoring and cohort bucketing.

DIA_Score and DDA_Score are percentile-based composite scores computed
within cohorts (instrument_family × throughput_bucket × amount_bucket).

Throughput is expressed in SPD (samples per day) — the universal unit
across Evosep, Vanquish Neo, and traditional LC setups.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

COHORT_MINIMUM = 5  # minimum submissions before leaderboard appears

# ── Throughput bucketing (SPD-first) ─────────────────────────────────

# Confirmed Evosep gradient times (evosep.com, April 2026):
#   500 SPD  ~2.2 min gradient    300 SPD  ~2.3 min gradient
#   200 SPD  ~4.8 min gradient    100 SPD  ~11 min gradient
#    60 SPD  ~21 min gradient      40 SPD  ~31 min (Whisper)
#    30 SPD  ~44 min gradient     Ext      ~88 min gradient


def spd_bucket(spd: int) -> str:
    """Classify throughput (samples per day) into a cohort bucket.

    This is the primary bucketing method.  Labs select their Evosep,
    Vanquish Neo, or equivalent method by SPD.

    Buckets:
        ≥200  "200+spd"   Evosep 500/300/200, Vanquish Neo 180
        80–199 "100spd"   Evosep 100 SPD
        40–79  "60spd"    Evosep 60 SPD (most popular)
        25–39  "30spd"    Evosep 30 SPD, Whisper 40
        10–24  "15spd"    Evosep Extended, traditional 1h
        <10    "deep"     Traditional 2h+ gradients
    """
    if spd >= 200:
        return "200+spd"
    if spd >= 80:
        return "100spd"
    if spd >= 40:
        return "60spd"
    if spd >= 25:
        return "30spd"
    if spd >= 10:
        return "15spd"
    return "deep"


def gradient_min_to_spd(minutes: int) -> int:
    """Estimate SPD from gradient length.

    For common Evosep methods (30/60/100 SPD), snaps to the exact value
    so cross-lab comparison works. Otherwise estimates from cycle time.

    Evosep ranges (gradient length → SPD):
        10-12 min → 100 SPD
        20-22 min → 60 SPD
        43-45 min → 30 SPD
    """
    if minutes <= 0:
        return 30

    # Snap to known Evosep methods
    if 10 <= minutes <= 13:
        return 100
    if 19 <= minutes <= 23:
        return 60
    if 40 <= minutes <= 46:
        return 30

    # Otherwise estimate from cycle time with 25% overhead
    cycle = minutes * 1.25
    return max(1, int(1440 / cycle))


def validate_spd_from_metadata(raw_path) -> int | None:
    """Infer the true SPD of a run from raw-file metadata.

    This is the authoritative SPD check used to catch mis-bucketed runs
    (e.g. a 30 SPD Whisper40 run accidentally tagged as 100 SPD because
    the cohort default was wrong).

    Resolution order:
      1. Bruker .d → read analysis.tdf GlobalMetadata MethodName and
         pattern-match known Evosep gradients ("30 SPD", "100 SPD",
         "Whisper40", etc).
      2. Bruker .d → derive gradient length from Frames.Time span and
         snap via gradient_min_to_spd().
      3. Thermo .raw → try fisher_py for InstrumentMethod; else use
         stan.tools.trfp.extract_metadata() to get gradient_length_min
         and snap via gradient_min_to_spd().

    Args:
        raw_path: Path or str to a .d directory or .raw file.

    Returns:
        Inferred SPD as int, or None if metadata doesn't support it.
    """
    from pathlib import Path as _Path
    import sqlite3

    path = _Path(raw_path)
    if not path.exists():
        return None

    # ── Bruker .d ──────────────────────────────────────────────
    if path.suffix.lower() == ".d" and path.is_dir():
        # 0. Richest source: XML files under .d/<N>.m/ that carry the
        #    plain-English Evosep method label ("100 samples per day").
        #    This is authoritative when present and works even when the
        #    operator used a cryptic PAC method name like
        #    "DIA_Bps_11x3-k07t13Ra85.proteoscape.m".
        spd_from_xml = _bruker_spd_from_xml(path)
        if spd_from_xml:
            return spd_from_xml

        tdf = path / "analysis.tdf"
        if not tdf.exists():
            return None
        try:
            with sqlite3.connect(str(tdf)) as con:
                # 1. Pattern-match the PAC method name next — unreliable
                #    for UC Davis files but useful for labs that include
                #    an explicit "SPD" token in the method name.
                row = con.execute(
                    "SELECT Value FROM GlobalMetadata WHERE Key = 'MethodName'"
                ).fetchone()
                method_name = row[0] if row and row[0] else ""

                if method_name:
                    spd = _spd_from_method_string(method_name)
                    if spd:
                        return spd

                # 2. Try a runtime/gradient field if Bruker stored one.
                for key in ("Method_RunTime", "RunTime", "MethodRuntime"):
                    row = con.execute(
                        "SELECT Value FROM GlobalMetadata WHERE Key = ?", (key,)
                    ).fetchone()
                    if row and row[0]:
                        try:
                            minutes = float(row[0])
                            if minutes > 1:
                                return gradient_min_to_spd(int(round(minutes)))
                        except (ValueError, TypeError):
                            pass

                # 3. Fall back to the Frames.Time span.
                row = con.execute(
                    "SELECT MIN(Time), MAX(Time) FROM Frames"
                ).fetchone()
                if row and row[0] is not None and row[1] is not None:
                    grad_sec = row[1] - row[0]
                    if grad_sec > 0:
                        grad_min = int(round(grad_sec / 60))
                        if grad_min > 0:
                            return gradient_min_to_spd(grad_min)
        except sqlite3.Error:
            return None
        return None

    # ── Thermo .raw ────────────────────────────────────────────
    if path.suffix.lower() == ".raw" and path.is_file():
        # Try the instrument method via fisher_py (accurate, no TRFP needed).
        try:
            from fisher_py import RawFile  # type: ignore

            rf = RawFile(str(path))
            method = None
            for attr in ("instrument_method", "InstrumentMethod",
                         "method_text", "method"):
                val = getattr(rf, attr, None)
                if val:
                    method = str(val)
                    break
            if method:
                spd = _spd_from_method_string(method)
                if spd:
                    return spd
        except Exception:
            pass

        # Fall back to TRFP-parsed gradient length.
        try:
            from stan.tools.trfp import extract_metadata
            meta = extract_metadata(path)
            # Method-name match first (Evosep names often appear here too).
            acq_method = meta.get("acquisition_method", "")
            if acq_method:
                spd = _spd_from_method_string(acq_method)
                if spd:
                    return spd
            grad_min = meta.get("gradient_length_min")
            if grad_min and int(grad_min) > 0:
                return gradient_min_to_spd(int(grad_min))
        except Exception:
            return None
        return None

    return None


# Known Evosep method-name patterns. The first match wins.
# Patterns are ordered so that longer/more-specific tokens come first.
# We use (?<!\d) instead of \b before the number because method names
# often separate tokens with underscores (e.g. "Evosep_60SPD"), and
# underscores are word characters so \b fails to match there.
_EVOSEP_METHOD_PATTERNS: list[tuple[str, int]] = [
    # Explicit "<N> SPD" or "SPD<N>" tokens win over method-name shortcuts.
    # Names like "Whisper40_SPD30_44min" must resolve to 30 (the SPD)
    # not 40 (the Whisper variant).
    (r"(?<!\d)500\s*spd(?!\d)", 500),
    (r"(?<!\d)300\s*spd(?!\d)", 300),
    (r"(?<!\d)200\s*spd(?!\d)", 200),
    (r"(?<!\d)100\s*spd(?!\d)", 100),
    (r"(?<!\d)60\s*spd(?!\d)", 60),
    (r"(?<!\d)40\s*spd(?!\d)", 40),
    (r"(?<!\d)30\s*spd(?!\d)", 30),
    (r"(?<!\d)15\s*spd(?!\d)", 15),
    # "SPD<N>" ordering (Bruker PAC sometimes writes it this way).
    (r"spd\s*500(?!\d)", 500),
    (r"spd\s*300(?!\d)", 300),
    (r"spd\s*200(?!\d)", 200),
    (r"spd\s*100(?!\d)", 100),
    (r"spd\s*60(?!\d)", 60),
    (r"spd\s*40(?!\d)", 40),
    (r"spd\s*30(?!\d)", 30),
    (r"spd\s*15(?!\d)", 15),
    # Bruker PAC shortcut names (no explicit SPD token).
    (r"whisper100_20min", 60),
    (r"whisper100_40min", 30),
    # Whisper fallbacks — only if nothing more specific matched.
    (r"whisper\s*40", 40),
    (r"whisper\s*20", 40),   # Whisper20 = 40 SPD equivalent throughput
    (r"extended\b", 15),
]


def _spd_from_method_string(method: str) -> int | None:
    """Parse a method/filename string for a known throughput label.

    Returns an SPD value if the string contains a recognised Evosep or
    Whisper token, else None. Case-insensitive; tolerates whitespace.
    """
    import re
    if not method:
        return None
    text = method.lower()
    for pattern, spd in _EVOSEP_METHOD_PATTERNS:
        if re.search(pattern, text):
            return spd
    return None


def _bruker_spd_from_xml(d_path) -> int | None:
    """Read the Evosep method label from Bruker `.d` XML method files.

    Bruker HyStar writes an XML method tree under `<N>.m/` inside every
    `.d` directory. The HyStar_LC submethod carries the plain-English
    method name (e.g. "100 samples per day"), which is the authoritative
    source for Evosep throughput. Also checks the UTF-16 SampleInfo.xml
    at the top of the .d as a secondary source.

    Args:
        d_path: Path to a `.d` directory.

    Returns:
        SPD as int if the method label parses, else None.
    """
    from pathlib import Path as _Path
    import re
    import xml.etree.ElementTree as ET

    d = _Path(d_path)
    if not d.is_dir():
        return None

    # 1. submethods.xml (UTF-8, clean schema). There's one <N>.m/ per
    #    .d, where N is some integer — glob for it rather than guessing.
    for m_dir in d.glob("*.m"):
        submeth = m_dir / "submethods.xml"
        if not submeth.exists():
            continue
        try:
            tree = ET.parse(str(submeth))
            root = tree.getroot()
            for sm in root.iter("submethod"):
                if sm.attrib.get("program") == "HyStar_LC":
                    name_el = sm.find("name")
                    if name_el is not None and name_el.text:
                        spd = _spd_from_evosep_label(name_el.text)
                        if spd:
                            return spd
        except (ET.ParseError, OSError):
            logger.debug("Failed to parse %s", submeth, exc_info=True)

    # 2. SampleInfo.xml (UTF-16, flat attribute list) as a fallback.
    sample_info = d / "SampleInfo.xml"
    if sample_info.exists():
        try:
            text = sample_info.read_text(encoding="utf-16")
            # The HyStar property is stored as:
            #   <Property ... Name="HyStar_LC_Method_Name" Value="100 samples per day" />
            m = re.search(
                r'Name="HyStar_LC_Method_Name"\s+Value="([^"]+)"',
                text,
            )
            if m:
                spd = _spd_from_evosep_label(m.group(1))
                if spd:
                    return spd
            # Also look at the Sample@Method attribute which often
            # points at the Evosep LC method file (e.g. "100spd.m").
            m = re.search(r'Method="[^"]*?(\d+)\s*spd[^"]*"', text, re.IGNORECASE)
            if m:
                return int(m.group(1))
        except (UnicodeError, OSError):
            logger.debug("Failed to read %s", sample_info, exc_info=True)

    return None


# "<N> samples per day" → N. Evosep writes this label both in
# submethods.xml and in the Agilent ICF method file.
_EVOSEP_SPD_LABEL_RE = __import__("re").compile(
    r"(\d+)\s*samples?\s*per\s*day", __import__("re").IGNORECASE
)


def _spd_from_evosep_label(label: str) -> int | None:
    """Parse an Evosep HyStar LC label like '100 samples per day'."""
    if not label:
        return None
    m = _EVOSEP_SPD_LABEL_RE.search(label)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    # Fallback: try the generic Evosep method string matcher (catches
    # "Whisper40", "Extended", explicit "100 SPD" tokens, etc.).
    return _spd_from_method_string(label)


def detect_lc_system(raw_path) -> str | None:
    """Identify the LC system from raw-file metadata.

    Returns one of "evosep", "custom", or None if unknown. Used to
    split the community TIC overlay so Evosep standardized traces
    aren't mixed with custom nanoLC gradients in the same bucket.
    """
    from pathlib import Path as _Path
    import re

    path = _Path(raw_path)
    if not path.exists():
        return None

    # Bruker .d: authoritative signals inside the XML method tree.
    if path.suffix.lower() == ".d" and path.is_dir():
        # submethods.xml with an HyStar_LC submethod → Evosep One (it's
        # the only LC HyStar ships with this tag for our instruments).
        for m_dir in path.glob("*.m"):
            hystar = m_dir / "hystar.method"
            if hystar.exists():
                try:
                    text = hystar.read_text(errors="replace")
                    if "Evosep One" in text or "EVOSEP_ONE" in text:
                        return "evosep"
                except OSError:
                    pass
            submeth = m_dir / "submethods.xml"
            if submeth.exists():
                try:
                    t = submeth.read_text()
                    if 'program="HyStar_LC"' in t and "samples per day" in t:
                        return "evosep"
                except OSError:
                    pass
        # SampleInfo.xml TrayType="96Evotip" is a strong Evosep indicator
        sample_info = path / "SampleInfo.xml"
        if sample_info.exists():
            try:
                text = sample_info.read_text(encoding="utf-16")
                if re.search(r'TrayType"\s+Value="96Evotip"', text):
                    return "evosep"
                if re.search(r'Method="[^"]*evosep', text, re.IGNORECASE):
                    return "evosep"
            except (UnicodeError, OSError):
                pass
        return "custom"

    # Thermo .raw: we don't have a reliable signal without opening the
    # .raw header (fisher_py). Return None and let the caller fall
    # back to its own inference (e.g., column_vendor substring match).
    return None


def throughput_bucket(spd: int | None = None, gradient_min: int | None = None) -> str:
    """Resolve throughput bucket from SPD (preferred) or gradient length (fallback).

    Args:
        spd: Samples per day (primary — use this when known).
        gradient_min: Gradient length in minutes (fallback for custom LC methods).

    Returns:
        Throughput bucket string for cohort ID.
    """
    if spd is not None and spd > 0:
        return spd_bucket(spd)
    if gradient_min is not None and gradient_min > 0:
        return spd_bucket(gradient_min_to_spd(gradient_min))
    return spd_bucket(30)  # default: 30 SPD


def amount_bucket(ng: float) -> str:
    """Classify injection amount (ng) into a cohort bucket.

    Buckets reflect modern proteomics workflows where many labs inject
    10–200 ng on Astral/timsTOF platforms.  Submissions are compared
    only within the same bucket so that a 50 ng run isn't penalised
    against a 500 ng run.
    """
    if ng <= 25:
        return "ultra-low"  # single-cell / very low input
    if ng <= 75:
        return "low"  # 50 ng standard QC (default)
    if ng <= 150:
        return "mid"
    if ng <= 300:
        return "standard"
    if ng <= 600:
        return "high"
    return "very-high"


def _normalize_column_name(column_model: str) -> str:
    """Normalize a column model string for cohort grouping.

    Strips whitespace, lowercases, removes vendor prefixes that might
    vary between submissions of the same column.
    """
    if not column_model:
        return ""
    # Keep the essential info: dimensions + chemistry
    return column_model.strip().lower().replace("  ", " ")


def compute_cohort_id(
    instrument_family: str,
    amount_ng: float,
    spd: int | None = None,
    gradient_min: int | None = None,
    column_model: str = "",
) -> str:
    """Build a column-specific cohort ID for benchmark grouping.

    Returns the most specific cohort that includes the LC column.
    The broader cohort (without column) can be derived by splitting on
    the last underscore group.

    Args:
        instrument_family: e.g. "timsTOF", "Astral", "Exploris".
        amount_ng: HeLa injection amount in nanograms.
        spd: Samples per day (primary throughput measure).
        gradient_min: Gradient length in minutes (fallback if spd not set).
        column_model: LC column model string (e.g. "Aurora Ultimate 25cm x 75um").
    """
    tb = throughput_bucket(spd=spd, gradient_min=gradient_min)
    ab = amount_bucket(amount_ng)
    base = f"{instrument_family}_{tb}_{ab}"

    if column_model:
        col_norm = _normalize_column_name(column_model)
        return f"{base}_{col_norm}"
    return base


def compute_broad_cohort_id(cohort_id: str) -> str:
    """Strip the column suffix from a cohort ID to get the broad cohort.

    A column-specific cohort like 'Astral_60spd_low_aurora ultimate 25cm...'
    maps to broad cohort 'Astral_60spd_low'.

    Used for fallback when column-specific cohort has < COHORT_MINIMUM submissions.
    """
    # Broad cohort is always the first 3 segments: family_spd_amount
    parts = cohort_id.split("_", 3)
    return "_".join(parts[:3])


def compute_dia_score(
    metrics: dict,
    cohort_percentiles: dict,
) -> float:
    """Compute DIA community composite score (0–100).

    DIA_Score =
      40 × percentile_rank(n_precursors)
    + 25 × percentile_rank(n_peptides)
    + 20 × (100 - percentile_rank(median_cv_precursor))  # lower CV = better
    + 15 × percentile_rank(ips_score)
    """
    pr = _percentile_rank

    score = (
        0.40 * pr(metrics.get("n_precursors", 0), cohort_percentiles.get("n_precursors", []))
        + 0.25 * pr(metrics.get("n_peptides", 0), cohort_percentiles.get("n_peptides", []))
        + 0.20 * (100 - pr(
            metrics.get("median_cv_precursor", 0),
            cohort_percentiles.get("median_cv_precursor", []),
        ))
        + 0.15 * pr(metrics.get("ips_score", 0), cohort_percentiles.get("ips_score", []))
    )
    return round(score, 1)


def compute_dda_score(
    metrics: dict,
    cohort_percentiles: dict,
) -> float:
    """Compute DDA community composite score (0–100).

    DDA_Score =
      35 × percentile_rank(n_psms)
    + 25 × percentile_rank(n_peptides_dda)
    + 20 × percentile_rank(pct_delta_mass_lt5ppm)
    + 20 × percentile_rank(ms2_scan_rate)
    """
    pr = _percentile_rank

    score = (
        0.35 * pr(metrics.get("n_psms", 0), cohort_percentiles.get("n_psms", []))
        + 0.25 * pr(
            metrics.get("n_peptides_dda", 0), cohort_percentiles.get("n_peptides_dda", [])
        )
        + 0.20 * pr(
            metrics.get("pct_delta_mass_lt5ppm", 0),
            cohort_percentiles.get("pct_delta_mass_lt5ppm", []),
        )
        + 0.20 * pr(
            metrics.get("ms2_scan_rate", 0), cohort_percentiles.get("ms2_scan_rate", [])
        )
    )
    return round(score, 1)


def _percentile_rank(value: float, sorted_values: list[float]) -> float:
    """Compute percentile rank (0–100) of value within sorted_values."""
    if not sorted_values:
        return 50.0  # no cohort data → assume middle

    n = len(sorted_values)
    count_below = sum(1 for v in sorted_values if v < value)
    count_equal = sum(1 for v in sorted_values if v == value)

    # Average rank method
    percentile = (count_below + 0.5 * count_equal) / n * 100
    return min(100.0, max(0.0, percentile))
