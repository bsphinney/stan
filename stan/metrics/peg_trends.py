"""PEG trend maths behind the dashboard's PEG tab. Pure functions, no IO.

The readers in ``stan.db`` / ``stan.db_pg`` hand over per-run PEG scalars;
everything here turns those into the ``/api/peg/overview`` document:
a trailing daily median, contamination *episodes*, the best 90-day
baseline, a 30-day summary, the monthly oligomer-ladder fingerprint, PEG
per LC-column period, and what PEG costs in identifications.

Two rules run through all of it.

**Only a real measurement counts.** A run has one iff ``peg_score`` and
``peg_intensity_pct`` are both non-NULL and ``peg_class`` is one of the
four real classes. ``'unknown'`` is the pipeline's failure sentinel and is
stored with ``peg_score = 0.0`` -- exactly what a spotless run scores -- so
anything that forgets the class check counts a failed read as a clean one.
An unmeasured run is dropped, never treated as 0.

**One acquisition is one run.** PG holds the same raw file up to five
times (re-ingested under a second path, re-processed by a newer STAN), and
the copies disagree: live PG on 2026-09-28 had 1,674 timsTOF rows for 1,404
acquisitions, 179 of the 270 extras inside the Dec-Apr episode, with PEG
differing in 168 of the 241 groups. Counting every copy weighted those
acquisitions twice or more; ``pick_canonical`` chooses one, the same one on
both backends and in the share client.

**The unit is ``peg_intensity_pct``** ("PEG share of MS1"), not the 0-100
score. The score saturates at both ends (29 % of UC Davis timsTOF runs are
exactly 0, 95 are exactly 100), so a median of it cannot tell a bad month
from a terrible one. The share keeps rising with contamination.

Everything is bucketed on the **UTC** date. SQLite holds ``run_date`` as
TEXT with whatever offset the acquisition PC wrote (``-08:00`` next to
``+00:00`` for the same instant), so the readers normalise to UTC before
anything here sees a timestamp; ``parse_utc`` is the one place that does it.
"""

from __future__ import annotations

import math
import re
import statistics
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Sequence

# ── Classes and filters ─────────────────────────────────────────────

#: The four real PEG classes, in severity order. Their index is the
#: integer class code the overview payload uses (0 = clean .. 3 = heavy).
PEG_CLASSES: tuple[str, ...] = ("clean", "trace", "moderate", "heavy")
CLASS_INDEX: dict[str, int] = {c: i for i, c in enumerate(PEG_CLASSES)}

#: Blank / wash / deleted-file names, exactly as ``stan submit-all`` skips
#: them (``stan/cli.py``: ``re.search(r"(?i)(wash|blank|blnk|blk|DELETE)",
#: name)``). A blank's PEG is carry-over, not the QC standard's, so letting
#: one into a QC trend reads as a contamination spike that never happened.
BLANK_WASH_PATTERN = r"(wash|blank|blnk|blk|DELETE)"
_BLANK_WASH_RE = re.compile(BLANK_WASH_PATTERN, re.IGNORECASE)

#: The same alternation for PG's case-insensitive ``!~*`` operator, used
#: where a query aggregates in SQL and never sees a run name in Python.
#: A plain alternation of ASCII literals means the same thing in Python
#: ``re`` and a POSIX ARE, so the two filters cannot drift apart --
#: tests/test_peg_trends.py pins that.
PG_BLANK_WASH_REGEX = "wash|blank|blnk|blk|delete"

#: Runs dated on or before this are rejected as bogus: PG holds a Lumos
#: run stamped 1980-01-02, which would stretch every daily series by 45
#: years of nulls.
MIN_RUN_DATE = datetime(2015, 1, 1, tzinfo=timezone.utc)

#: Oligomers the ladder fingerprint shows. The default reference panel is
#: PEG1-20 (``stan.metrics.peg``), and PEG1 at m/z 63-86 sits below every
#: MS1 scan range STAN has seen, so the heatmap starts at 2.
LADDER_N: tuple[int, ...] = tuple(range(2, 21))

RUNS_COLS: list[str] = ["t", "spd", "pct", "score", "ions", "cls", "prec"]


def is_blank_or_wash(run_name: str | None) -> bool:
    """True when a run name marks a blank, wash or deleted file.

    Same regex and semantics as the blank/wash skip in ``stan submit-all``,
    so the PEG tab, the PEG share client and the benchmark submitter agree
    on which runs are QC. A missing name is not a blank.
    """
    if not run_name:
        return False
    return bool(_BLANK_WASH_RE.search(str(run_name)))


def is_real_peg(peg_score: Any, peg_intensity_pct: Any, peg_class: Any) -> bool:
    """True when a row carries a genuine PEG measurement.

    Rejects NULLs and the ``'unknown'`` failure sentinel (score 0.0), which
    must never read as clean. Also rejects NaN/infinity: PG's ``real`` can
    hold them, SQLite cannot, so no local test would catch one -- and a
    single NaN reaching ``json.dumps`` turns the public endpoint into a 500.
    """
    return (
        _finite(peg_score)
        and _finite(peg_intensity_pct)
        and (peg_class or "") in CLASS_INDEX
    )


def _finite(v: Any) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def is_failed_acquisition(n_precursors: Any, peg_n_ions_detected: Any,
                          peg_intensity_pct: Any) -> bool:
    """True for a row that looks unmeasured though its PEG reads clean.

    ``detect_peg_in_spectra`` answers 0 % and 0 ions when it summed no MS1
    signal at all, and that scores 0 and classifies ``'clean'`` -- the same
    trap as the ``'unknown'`` sentinel. The readers cannot see the spectra,
    so they drop the combination that marks it: no precursors identified,
    no PEG ion, and exactly 0 % PEG. On live PG 21 of the 22 timsTOF runs
    with 0 precursors read exactly that, against a 29 % base rate, and the
    ones checked on Hive were 13-364 MB acquisitions beside a 1.1 GB median.
    A genuinely clean run identifies precursors, so it is not caught; a
    missing precursor count (DDA, or never searched) is not 0 and keeps the
    row. Both readers apply it in SQL too (``COALESCE(..., -1) = 0``).
    """
    return (_int_or_none(n_precursors) == 0
            and _int_or_none(peg_n_ions_detected) == 0
            and _float_or_none(peg_intensity_pct) == 0.0)


_VERSION_RE = re.compile(r"\s*v?([0-9]+(?:\.[0-9]+)*)")


def version_key(value: Any) -> tuple[int, ...]:
    """``"1.0.10"`` -> ``(1, 0, 10)``; NULL or unparseable -> ``()``, oldest.

    The leading dotted number, compared numerically (text order would put
    ``0.2.376`` after ``1.0.44``). Same regex as ``stan.community.peg_submit``
    and as the PG readers' ``substring(stan_version FROM ...)``, so all three
    rank a run's copies alike.
    """
    m = _VERSION_RE.match(str(value or ""))
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def acquisition_key(row: dict) -> tuple:
    """``(instrument, run_name, UTC second)``: which rows are one raw file.

    PG's ``DISTINCT ON`` uses the same three (``date_trunc('second', ...)``);
    the share client's ``run_key`` hashes them too, with the name's basename.
    """
    return (row.get("instrument"), row.get("run_name"), row.get("run_date_utc"))


def pick_canonical(rows: Iterable[dict]) -> list[dict]:
    """One row per acquisition, the rest dropped (both backends, spec 4.1).

    Among copies of one acquisition (``acquisition_key``) the kept row is
    the one whose ion ladder was stored (``has_hits``), then the newest
    ``stan_version`` (``version_key``), then the highest ``id``. Hits come
    first because the ladder needs them -- a copy without them is a run
    whose oligomers are unknown -- and on live PG the copies with hits are
    also the Hive re-processing (0.2.376) of the instrument PC's 0.2.222
    rows. The PG readers apply the identical order in SQL (``DISTINCT ON``
    in ``stan.db_pg``), so the SQLite mirror and PG keep the same copy.

    The choice never depends on input order. Rows must carry
    ``run_date_utc`` (already normalised); ``id``, ``stan_version`` and
    ``has_hits`` may be absent and then rank lowest. Returns the kept rows
    in input order.
    """
    best: dict[tuple, tuple[tuple, int]] = {}
    ordered = list(rows)
    for i, r in enumerate(ordered):
        rank = (bool(r.get("has_hits")), version_key(r.get("stan_version")),
                str(r.get("id") or ""))
        k = acquisition_key(r)
        held = best.get(k)
        if held is None or rank > held[0]:
            best[k] = (rank, i)
    keep = {i for _, i in best.values()}
    return [r for i, r in enumerate(ordered) if i in keep]


# ── Time handling ───────────────────────────────────────────────────

_FRACTION_RE = re.compile(r"(\d{2}:\d{2}:\d{2})\.(\d+)")


def parse_utc(value: Any) -> datetime | None:
    """Parse a stored ``run_date`` / ``event_date`` to an aware UTC datetime.

    Accepts a ``datetime`` (psycopg2), a ``date``, or ISO-8601 text in any of
    the shapes the stores actually hold: ``2026-09-01 22:47:38+00:00``,
    ``2025-02-03T11:47:26-08:00``, ``...T12:00:00Z``, fractional seconds of
    any length, or a bare date. Text with no offset is taken as UTC -- the
    convention ``stan.db.get_column_lifetime`` already applies to legacy rows.

    Returns None for anything unparseable rather than raising: one malformed
    row must not take the whole tab down.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    else:
        s = str(value).strip()
        if not s:
            return None
        if s[-1:] in ("Z", "z"):
            s = s[:-1] + "+00:00"
        # Python 3.10's fromisoformat only takes 3- or 6-digit fractions;
        # PEG never needs sub-second precision, so drop the fraction.
        s = _FRACTION_RE.sub(r"\1", s)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def utc_iso(dt: datetime) -> str:
    """``YYYY-MM-DDTHH:MM:SSZ``, whole seconds, from an aware datetime.

    The shared-record format. PG produces the identical string with
    ``to_char(run_date AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')``,
    which also truncates the fraction -- so a run's timestamp, and the
    ``run_key`` hashed from it, do not depend on which backend was read.
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def week_window(as_of: date, weeks: int = 26) -> tuple[datetime, datetime]:
    """``(start, end)`` of ``weeks`` trailing 7-day buckets ending with ``as_of``.

    ``end`` is the end of the as-of day (next UTC midnight), ``start`` is
    ``weeks`` x 7 days before it. These are the relay's lc-compare buckets
    (``_peg_week_edges``): the newest point is always a full week, where a
    Monday-start calendar week left the lab's sparkline ending on a partial
    week (or on nothing at all when ``as_of`` was a Monday) beside the
    community one ending on a full one, in the same panel.
    """
    nxt = as_of + timedelta(days=1)
    end = datetime(nxt.year, nxt.month, nxt.day, tzinfo=timezone.utc)
    return end - timedelta(days=7 * weeks), end


def week_index(t: datetime, start: datetime, weeks: int = 26) -> int | None:
    """Bucket of ``t`` in ``week_window`` (0 = oldest), None outside it.

    ``(t - start) // 7 days``, as the relay computes it and as the PG reader
    does with ``floor(extract(epoch FROM t - start) / 604800)``.
    """
    if t < start:
        return None
    i = int((t - start) // timedelta(days=7))
    return i if i < weeks else None


# ── Run records ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class PegRun:
    """One QC run with a real PEG measurement, timestamp in UTC."""

    t: datetime
    pct: float
    cls: str
    score: float | None = None
    ions: int | None = None
    spd: int | None = None
    prec: int | None = None
    mode: str | None = None
    lc_system: str | None = None
    has_hits: bool | None = None

    @property
    def day(self) -> date:
        """UTC calendar date of the acquisition."""
        return self.t.date()

    @property
    def ladder_known(self) -> bool:
        """True when this run's oligomer ladder is on record.

        Either its ion hits were stored, or it detected no PEG ion at all
        (so every oligomer is known to be absent). A run with ions but no
        stored hits is unknown, not "oligomer absent": counting it in the
        ladder's denominator is the same error as counting an unmeasured
        run as clean.
        """
        return bool(self.has_hits) or self.ions == 0


def to_peg_runs(rows: Iterable[dict]) -> list[PegRun]:
    """Build sorted ``PegRun``s from reader rows, dropping anything not real.

    Accepts the ``stan.db.get_peg_runs`` shape (``run_date_utc``,
    ``peg_intensity_pct``, ``has_hits``, ...). Defends the same filter the
    readers apply in SQL, so a caller that hands in raw rows still cannot
    smuggle an ``'unknown'``, a failed acquisition or a 1980 row into the
    maths. It does not de-duplicate: that needs run names, which never
    reach this module, so the readers do it (``pick_canonical``).
    """
    out: list[PegRun] = []
    for r in rows:
        if not is_real_peg(r.get("peg_score"), r.get("peg_intensity_pct"),
                           r.get("peg_class")):
            continue
        if is_failed_acquisition(r.get("n_precursors"), r.get("peg_n_ions_detected"),
                                 r.get("peg_intensity_pct")):
            continue
        t = parse_utc(r.get("run_date_utc") or r.get("run_date"))
        if t is None or t <= MIN_RUN_DATE:
            continue
        hits = r.get("has_hits")
        out.append(PegRun(
            t=t,
            pct=float(r["peg_intensity_pct"]),
            cls=str(r["peg_class"]),
            score=_float_or_none(r.get("peg_score")),
            ions=_int_or_none(r.get("peg_n_ions_detected")),
            spd=_int_or_none(r.get("spd")),
            prec=_int_or_none(r.get("n_precursors")),
            mode=r.get("mode"),
            lc_system=r.get("lc_system"),
            has_hits=None if hits is None else bool(hits),
        ))
    out.sort(key=lambda p: p.t)
    return out


def _float_or_none(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _int_or_none(v: Any) -> int | None:
    try:
        return None if v is None else int(v)
    except (TypeError, ValueError):
        return None


def _rnd(v: float | None, nd: int) -> float | None:
    """Round for the payload; None for missing or non-finite (not JSON)."""
    return round(float(v), nd) if _finite(v) else None


def _median(vals: Sequence[float]) -> float | None:
    return statistics.median(vals) if vals else None


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of sorted values (PG ``percentile_cont``)."""
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _pct_of(k: int, n: int) -> int | None:
    return round(100 * k / n) if n else None


def _in_days(runs: Sequence[PegRun], first: date, last: date) -> list[PegRun]:
    return [r for r in runs if first <= r.day <= last]


# ── Daily series and episodes ───────────────────────────────────────

def rolling_median_daily(
    times: Sequence[datetime | date],
    pcts: Sequence[float | None],
    start: date,
    end: date,
    window_days: int = 14,
    min_runs: int = 5,
) -> list[float | None]:
    """Trailing median PEG share for every UTC day from ``start`` to ``end``.

    Day ``d``'s window holds the runs dated ``d - window_days < day <= d``
    (so 14 calendar days including ``d``). The value is None when fewer than
    ``min_runs`` runs fall in it: a 14-day median over two injections is a
    coin toss, and a gap in the line says so honestly. Values are unrounded;
    round only for display, since episode detection thresholds on them.
    """
    pairs = sorted(
        ((t.date() if isinstance(t, datetime) else t, float(p))
         for t, p in zip(times, pcts) if t is not None and p is not None),
        key=lambda x: x[0],
    )
    days = [d for d, _ in pairs]
    vals = [v for _, v in pairs]
    out: list[float | None] = []
    d = start
    while d <= end:
        lo = bisect_right(days, d - timedelta(days=window_days))
        hi = bisect_right(days, d)
        w = vals[lo:hi]
        out.append(statistics.median(w) if len(w) >= min_runs else None)
        d += timedelta(days=1)
    return out


def detect_episodes(
    daily: Sequence[float | None],
    start: date,
    runs: Sequence[PegRun],
    threshold_pct: float = 3.0,
    gap_days: int = 21,
    min_days: int = 14,
    window_days: int = 14,
    ongoing_days: int = 3,
) -> list[dict]:
    """Find sustained PEG episodes in a trailing-median daily series.

    A day is *hot* when its trailing median is at or above
    ``threshold_pct``. Hot days less than ``gap_days`` apart merge into one
    episode, so a fortnight of clean-ish runs mid-episode does not split it.
    The trailing window lags the truth by up to ``window_days``, so each
    episode's start is pulled back to the first run at or above the
    threshold inside the first hot day's window -- the day PEG actually
    showed up, not the day the median noticed. Episodes shorter than
    ``min_days`` after that are dropped as blips.

    ``daily[i]`` is the value for ``start + i`` days; the last entry is
    taken as the as-of day, and an episode is ``ongoing`` when its last hot
    day is within ``ongoing_days`` of it.

    On UC Davis's timsTOF HT (live PG, 2026-09-28, one row per acquisition)
    this yields 2025-12-15 -> 2026-04-07 (113 d, median 5.30 %) and
    2026-05-21 -> 2026-09-18 (120 d, median 5.31 %). Counting every
    duplicate ingest had stretched the first to 04-26 (132 d).

    Returns:
        ``[{start, end, days, n, median_pct, heavy_pct, ongoing}]`` oldest
        first, dates as ISO strings.
    """
    if not daily:
        return []
    as_of = start + timedelta(days=len(daily) - 1)
    spans: list[list[date]] = []
    cur: list[date] | None = None
    for i, m in enumerate(daily):
        if m is None or m < threshold_pct:
            continue
        day = start + timedelta(days=i)
        if cur is not None and (day - cur[1]).days < gap_days:
            cur[1] = day
        else:
            if cur is not None:
                spans.append(cur)
            cur = [day, day]
    if cur is not None:
        spans.append(cur)

    ordered = sorted(runs, key=lambda r: r.t)
    out: list[dict] = []
    for first_hot, last_hot in spans:
        s = first_hot - timedelta(days=window_days - 1)
        hot_runs = [r for r in _in_days(ordered, s, last_hot) if r.pct >= threshold_pct]
        if hot_runs:
            s = hot_runs[0].day
        inside = _in_days(ordered, s, last_hot)
        if (last_hot - s).days < min_days or not inside:
            continue
        heavy = sum(1 for r in inside if r.cls == "heavy")
        out.append({
            "start": s.isoformat(),
            "end": last_hot.isoformat(),
            "days": (last_hot - s).days,
            "n": len(inside),
            "median_pct": round(statistics.median(r.pct for r in inside), 2),
            "heavy_pct": round(100 * heavy / len(inside)),
            "ongoing": (as_of - last_hot).days <= ongoing_days,
        })
    return out


def best_baseline(
    runs: Sequence[PegRun],
    window_days: int = 90,
    min_runs: int = 20,
    as_of: date | None = None,
) -> dict | None:
    """The instrument's cleanest sustained stretch: what "normal" looked like.

    Slides a trailing ``window_days`` window (same edge convention as
    ``rolling_median_daily``) over every day from the first run to
    ``as_of`` and keeps the lowest median PEG share among windows holding at
    least ``min_runs`` runs. Ties are the norm, not the exception: on UC
    Davis's timsTOF 425 of 969 eligible windows have a median of exactly 0.
    They go to the lowest upper quartile, then the most runs, then the most
    recent. Breaking them on run count alone picked a 218-run window that
    was only 80 % clean (mean 4.5 %); on the mean, a sparse 24-run one. The
    upper quartile asks for a stretch clean on three runs in four, which is
    what "baseline" should mean, without letting one outlier decide.

    Returns:
        ``{median_pct, start, end, n}`` (``start``/``end`` = first and last
        day of the window) or None when no window reaches ``min_runs``.
    """
    ordered = sorted(runs, key=lambda r: r.t)
    if len(ordered) < min_runs:
        return None
    days = [r.day for r in ordered]
    last = as_of or days[-1]
    best: tuple | None = None
    d = days[0]
    while d <= last:
        lo = bisect_right(days, d - timedelta(days=window_days))
        hi = bisect_right(days, d)
        n = hi - lo
        if n >= min_runs:
            window = sorted(r.pct for r in ordered[lo:hi])
            med = statistics.median(window)
            key = (med, _quantile(window, 0.75), -n, -d.toordinal())
            if best is None or key < best[0]:
                best = (key, d, n, med)
        d += timedelta(days=1)
    if best is None:
        return None
    _, end_day, n, med = best
    return {
        "median_pct": round(med, 3),
        "start": (end_day - timedelta(days=window_days - 1)).isoformat(),
        "end": end_day.isoformat(),
        "n": n,
    }


def summary_30d(runs: Sequence[PegRun], as_of: date) -> dict:
    """Headline numbers for the last 30 UTC days against the 30 before.

    "Last 30 days" is ``as_of - 29 .. as_of`` inclusive, the previous
    window the 30 days before that. ``change_pct`` is the relative change
    of the median share, None when either window is empty or the previous
    median is 0 (a change from nothing has no percentage). ``streak_clean``
    counts consecutive clean runs back from the newest.
    """
    last = _in_days(runs, as_of - timedelta(days=29), as_of)
    prev = _in_days(runs, as_of - timedelta(days=59), as_of - timedelta(days=30))
    m_last = _median([r.pct for r in last])
    m_prev = _median([r.pct for r in prev])
    change = None
    if m_last is not None and m_prev:
        change = round((m_last - m_prev) / m_prev * 100)
    clean = sum(1 for r in last if r.cls == "clean")
    heavy = sum(1 for r in last if r.cls == "heavy")
    streak = 0
    for r in sorted(runs, key=lambda x: x.t, reverse=True):
        if r.cls != "clean":
            break
        streak += 1
    return {
        "n_30d": len(last),
        "median_30d": _rnd(m_last, 3),
        "median_prev_30d": _rnd(m_prev, 3),
        "change_pct": change,
        "clean_30d": clean,
        "heavy_30d": heavy,
        "clean_rate_30d": _pct_of(clean, len(last)),
        "streak_clean": streak,
    }


# ── Ladder fingerprint ──────────────────────────────────────────────

def ladder_by_month(
    hits_rows: Iterable[Sequence],
    runs_per_month: dict[str, int],
    as_of: date,
    n_values: Sequence[int] = LADDER_N,
) -> dict:
    """How often each PEG oligomer was seen, month by month.

    ``share[j][i]`` is, for oligomer ``n_values[j]`` in ``months[i]``, the
    largest fraction of that month's runs *with a known ladder*
    (``PegRun.ladder_known``) in which any single adduct of it was
    detected. Max over adducts rather than a union because
    the reader aggregates per (month, n, adduct) in SQL; a run seen as both
    [M+H]+ and [M+NH4]+ is one run, and the max never double counts it.

    Months run from January of the year before ``as_of`` through ``as_of``'s
    month, keeping only months with at least one known-ladder run. ``adducts``
    totals every (run, oligomer) detection per adduct across all the rows
    given, for the adduct-mix bar.

    Args:
        hits_rows: ``(month 'YYYY-MM', repeat_n, adduct, n_runs)`` tuples
            from ``stan.db.get_peg_ladder_month_counts``.
        runs_per_month: Real-PEG QC runs per UTC month whose ladder is
            known (the denominator). Every run the numerator counts has
            hits, so it is always in here too.
        as_of: The overview's as-of date.
        n_values: Oligomers to report (rows of ``share``).
    """
    counts: dict[tuple[str, int, str], int] = defaultdict(int)
    adducts: Counter = Counter()
    for month, n, adduct, k in hits_rows:
        k = int(k or 0)
        counts[(str(month), int(n), str(adduct))] += k
        adducts[str(adduct)] += k
    first = f"{as_of.year - 1}-01"
    last = f"{as_of.year:04d}-{as_of.month:02d}"
    months = sorted(m for m, k in runs_per_month.items()
                    if k and first <= m <= last)
    by_mn: dict[tuple[str, int], int] = defaultdict(int)
    for (m, n, _a), k in counts.items():
        by_mn[(m, n)] = max(by_mn[(m, n)], k)
    share = [
        [round(min(1.0, by_mn.get((m, n), 0) / runs_per_month[m]), 3) for m in months]
        for n in n_values
    ]
    return {
        "months": months,
        "n": list(n_values),
        "share": share,
        "nruns": [int(runs_per_month[m]) for m in months],
        "adducts": dict(sorted(adducts.items())),
    }


def runs_per_month(runs: Iterable[PegRun]) -> dict[str, int]:
    """Real-PEG runs per UTC month (``'YYYY-MM'``)."""
    c: Counter = Counter(r.t.strftime("%Y-%m") for r in runs)
    return dict(c)


# ── Column periods, precursor cost ──────────────────────────────────

def column_periods(
    events: Iterable[Sequence],
    runs: Sequence[PegRun],
    as_of: date | None = None,
) -> list[dict]:
    """PEG on each LC column, from the maintenance log's column changes.

    Each ``column_change`` opens a period that runs to the next one; the
    newest stays open (``retired`` None). A run belongs to the period whose
    install date is on or before its UTC date and whose retirement is after
    it. Two changes logged on the same day are one install. Events dated
    after ``as_of`` are ignored.

    A period with no QC runs yet (a column fitted today) is kept with
    ``n_qc`` 0 and null statistics: dropping it would present the previous
    column as the one in the instrument.

    Args:
        events: ``(event_date, column_model)`` pairs for column_change
            events only (``stan.db.get_column_change_events``).
        runs: Real-PEG runs for the same instrument.
        as_of: Ignore events after this date.

    Returns:
        ``[{installed, retired, column_model, n_qc, median_pct, heavy_pct,
        clean_pct}]``, newest last. ``[]`` when nothing was logged.
    """
    installs: dict[date, str | None] = {}
    for ev_date, model in events:
        dt = parse_utc(ev_date)
        if dt is None:
            continue
        d = dt.date()
        if as_of is not None and d > as_of:
            continue
        if d not in installs or (model and not installs[d]):
            installs[d] = model or None
    starts = sorted(installs)
    ordered = sorted(runs, key=lambda r: r.t)
    days = [r.day for r in ordered]
    out: list[dict] = []
    for i, d in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else None
        lo = bisect_left(days, d)
        hi = bisect_left(days, nxt) if nxt is not None else len(days)
        inside = ordered[lo:hi]
        n = len(inside)
        out.append({
            "installed": d.isoformat(),
            "retired": nxt.isoformat() if nxt else None,
            "column_model": installs[d],
            "n_qc": n,
            "median_pct": _rnd(_median([r.pct for r in inside]), 2),
            "heavy_pct": _pct_of(sum(1 for r in inside if r.cls == "heavy"), n),
            "clean_pct": _pct_of(sum(1 for r in inside if r.cls == "clean"), n),
        })
    return out


def _is_dia(mode: str | None) -> bool:
    return "dia" in (mode or "").lower()


def impact_by_class(runs: Sequence[PegRun], min_per_class: int = 10) -> dict:
    """Median precursors per PEG class, per SPD, for DIA runs.

    Precursor count is STAN's primary DIA depth metric, so this is what PEG
    costs in identifications. Only DIA runs (``mode`` contains "dia") with a
    positive precursor count are used -- DDA reports PSMs, which do not
    compare, and 0 precursors is a failed search, not a depth (on live PG
    all 22 such timsTOF runs were 'clean', which understated the clean-heavy
    gap at 100 SPD by 7.6 %) -- and only SPDs with at least
    ``min_per_class`` clean *and* heavy runs, since a contrast needs both
    ends populated.

    Returns:
        ``{"<spd>": {class: [n, median n_precursors]}}``, SPDs descending.
        Classes with no runs at a qualifying SPD are ``[0, None]``.
    """
    by_spd: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for r in runs:
        if r.spd is None or r.prec is None or r.prec <= 0 or not _is_dia(r.mode):
            continue
        by_spd[r.spd][r.cls].append(r.prec)
    out: dict[str, dict] = {}
    for spd in sorted(by_spd, reverse=True):
        cls_map = by_spd[spd]
        if (len(cls_map.get("clean", [])) < min_per_class
                or len(cls_map.get("heavy", [])) < min_per_class):
            continue
        entry = {}
        for c in PEG_CLASSES:
            vals = cls_map.get(c, [])
            med = _median(vals)
            entry[c] = [len(vals), None if med is None else round(med)]
        out[str(spd)] = entry
    return out


# ── Instruments and the lab's own LC comparison ─────────────────────

def _top_lc(lc_counts: dict[str, int]) -> str | None:
    """Most common non-empty ``lc_system``; ties to the alphabetical first."""
    real = [(-n, lc) for lc, n in lc_counts.items() if lc and n]
    return min(real)[1] if real else None


def instruments_from_counts(counts: Iterable[Sequence]) -> list[dict]:
    """``[{instrument, n_runs, evosep}]`` from ``(instrument, lc_system, n)``.

    ``evosep`` is True when Evosep is the instrument's most common LC across
    its real-PEG QC runs. Sorted by run count, most first, so the first
    entry is the overview's default instrument.
    """
    per: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for inst, lc, n in counts:
        if not inst:
            continue
        per[str(inst)][str(lc or "")] += int(n or 0)
    out = [
        {"instrument": inst, "n_runs": sum(lc.values()),
         "evosep": _top_lc(lc) == "evosep"}
        for inst, lc in per.items()
    ]
    out.sort(key=lambda d: (-d["n_runs"], d["instrument"]))
    return [d for d in out if d["n_runs"] > 0]


def pick_default_instrument(instruments: Sequence[dict]) -> str | None:
    """The instrument with the most real-PEG runs, or None if none have any."""
    best = max(instruments, key=lambda d: (d.get("n_runs") or 0), default=None)
    return best["instrument"] if best and best.get("n_runs") else None


def finalize_lab_lc_row(
    instrument: str,
    lc_counts: dict[str, int],
    n_90d: int,
    median_90d: float | None,
    clean_90d: int,
    n_365d: int,
    median_365d: float | None,
    weekly: dict[int, float | None],
    weeks: int = 26,
) -> dict:
    """One ``lab_lc`` entry, identical in shape whichever backend aggregated.

    Both the PG path (``percentile_cont`` in SQL) and the SQLite path
    (``lab_lc_summary`` here) end in this function, so rounding and keys
    cannot differ between them. ``weekly`` maps a ``week_index`` bucket
    (0 = oldest) to its median; missing buckets are null.
    """
    return {
        "instrument": instrument,
        "lc_system": _top_lc(lc_counts),
        "n_90d": int(n_90d or 0),
        "median_90d": _rnd(median_90d, 3),
        "clean_rate_90d": _pct_of(int(clean_90d or 0), int(n_90d or 0)),
        "n_365d": int(n_365d or 0),
        "median_365d": _rnd(median_365d, 3),
        "weekly": [_rnd(weekly.get(i), 3) for i in range(weeks)],
    }


def sort_lab_lc(rows: list[dict]) -> list[dict]:
    """Busiest instrument first, by year then by 90 days, then by name."""
    return sorted(rows, key=lambda d: (-d["n_365d"], -d["n_90d"], d["instrument"]))


def lab_lc_summary(rows: Iterable[dict], as_of: date, weeks: int = 26) -> list[dict]:
    """Per-instrument 90/365-day PEG share and weekly medians (SQLite path).

    ``rows`` are already-filtered real-PEG QC rows with ``instrument``,
    ``lc_system``, ``run_date_utc`` (or ``t``), ``peg_intensity_pct`` and
    ``peg_class``. Windows are UTC days ``as_of - 89 .. as_of`` and
    ``as_of - 364 .. as_of``; weeks are the trailing 7-day buckets of
    ``week_window``, the last ending with ``as_of``. Runs after ``as_of``
    are ignored. Every instrument with a real measurement gets a row, even
    one idle for a year.
    """
    s90 = as_of - timedelta(days=89)
    s365 = as_of - timedelta(days=364)
    w0, _end = week_window(as_of, weeks)
    per: dict[str, dict] = {}
    for r in rows:
        inst = r.get("instrument")
        t = r.get("t") or parse_utc(r.get("run_date_utc") or r.get("run_date"))
        pct = r.get("peg_intensity_pct")
        if not inst or t is None or pct is None:
            continue
        d = t.date()
        if d > as_of:
            continue
        acc = per.setdefault(inst, {"lc": defaultdict(int), "p90": [], "c90": 0,
                                    "p365": [], "wk": defaultdict(list)})
        acc["lc"][r.get("lc_system") or ""] += 1
        if d >= s90:
            acc["p90"].append(float(pct))
            acc["c90"] += 1 if r.get("peg_class") == "clean" else 0
        if d >= s365:
            acc["p365"].append(float(pct))
        wk = week_index(t, w0, weeks)
        if wk is not None:
            acc["wk"][wk].append(float(pct))
    out = [
        finalize_lab_lc_row(
            inst, dict(a["lc"]), len(a["p90"]), _median(a["p90"]), a["c90"],
            len(a["p365"]), _median(a["p365"]),
            {w: _median(v) for w, v in a["wk"].items()}, weeks,
        )
        for inst, a in per.items()
    ]
    return sort_lab_lc(out)


# ── The overview document ───────────────────────────────────────────

def empty_overview(as_of: date, instrument: str | None = None,
                   instruments: Sequence[dict] = ()) -> dict:
    """A valid overview with no runs: the tab renders its empty state."""
    return {
        "as_of": as_of.isoformat(),
        "instrument": instrument,
        "instruments": list(instruments),
        "lc_system": None,
        "instrument_family": None,
        "runs_cols": list(RUNS_COLS),
        "runs": [],
        "rolling_start": None,
        "rolling": {"all": []},
        "episodes": [],
        "baseline": None,
        "summary": summary_30d([], as_of),
        "ladder": ladder_by_month([], {}, as_of),
        "column_periods": [],
        "impact": {},
        "lab_lc": [],
    }


def build_overview(
    *,
    as_of: date,
    instrument: str | None,
    instruments: Sequence[dict],
    runs: Iterable[dict],
    ladder_rows: Iterable[Sequence] = (),
    column_events: Iterable[Sequence] = (),
    lab_lc: Iterable[dict] = (),
    family_fn: Callable[[str], str] | None = None,
) -> dict:
    """Assemble the ``GET /api/peg/overview`` document (spec section 4.2).

    Everything but ``sharing`` (config, added by the endpoint). Carries no
    run or sample names: the readers never return them and nothing here
    invents one.

    Args:
        as_of: UTC date of the request; runs dated after it are ignored so a
            mis-set acquisition clock cannot stretch the series.
        instrument: The instrument the per-run sections describe.
        instruments: ``stan.db.get_peg_instruments()`` output.
        runs: ``stan.db.get_peg_runs(instrument)`` rows, one per
            acquisition, with ``has_hits`` for the ladder's denominator.
        ladder_rows: ``stan.db.get_peg_ladder_month_counts(instrument)``.
        column_events: ``stan.db.get_column_change_events(instrument)``.
        lab_lc: ``stan.db.get_peg_lab_lc_summary(as_of)`` rows.
        family_fn: Instrument model -> family. Defaults to
            ``stan.community.submit._instrument_family``, the single source
            the community cohorts use.
    """
    if family_fn is None:
        from stan.community.submit import _instrument_family as family_fn

    peg = [r for r in to_peg_runs(runs) if r.day <= as_of]
    lab = []
    for row in lab_lc:
        entry = {"instrument": row["instrument"],
                 "family": family_fn(row["instrument"] or "")}
        entry.update({k: v for k, v in row.items() if k != "instrument"})
        lab.append(entry)

    doc = empty_overview(as_of, instrument, instruments)
    doc["lab_lc"] = lab
    if instrument:
        doc["instrument_family"] = family_fn(instrument)
    if not peg:
        return doc

    doc["lc_system"] = _top_lc(Counter(r.lc_system or "" for r in peg))
    doc["runs"] = [
        [r.t.strftime("%Y-%m-%dT%H:%M"), r.spd, round(r.pct, 3),
         _rnd(r.score, 1), r.ions, CLASS_INDEX[r.cls], r.prec]
        for r in peg
    ]

    start = peg[0].day
    rolling = {"all": rolling_median_daily(
        [r.t for r in peg], [r.pct for r in peg], start, as_of)}
    spd_n = Counter(r.spd for r in peg if r.spd is not None)
    for spd in sorted((s for s, n in spd_n.items() if n >= 20), reverse=True):
        sub = [r for r in peg if r.spd == spd]
        rolling[str(spd)] = rolling_median_daily(
            [r.t for r in sub], [r.pct for r in sub], start, as_of)
    doc["rolling_start"] = start.isoformat()
    doc["episodes"] = detect_episodes(rolling["all"], start, peg)
    doc["rolling"] = {k: [_rnd(v, 3) for v in series] for k, series in rolling.items()}

    doc["baseline"] = best_baseline(peg, as_of=as_of)
    doc["summary"] = summary_30d(peg, as_of)
    # The denominator is runs whose ladder is known, not every real-PEG
    # run. Live PG had 206 timsTOF rows with ions but no stored hits, 147 in
    # Feb-Apr 2026; counted as "oligomer absent" they drew the heart of the
    # worst episode as its cleanest months. Most were duplicate ingests, but
    # 13 remain once each acquisition counts once.
    doc["ladder"] = ladder_by_month(
        ladder_rows, runs_per_month(r for r in peg if r.ladder_known), as_of)
    doc["column_periods"] = column_periods(column_events, peg, as_of)
    doc["impact"] = impact_by_class(peg)
    return doc
