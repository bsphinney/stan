"""Watch the instrument itself — over-pressure, clogs, and hard errors.

`ht_watch` watches a *plate*: has the queue stalled, are wells failing, are
the standards drifting. This watches the *hardware* underneath it, which is
the other half of the same 2026-08-31 incident: the column clogged overnight,
the runs kept firing into a blocked bed, and nobody was told until morning.

TWO SOURCES, ONE INSTRUMENT
---------------------------
* **Evosep column health** — from the Evosep One's own procedure logs, which
  are the only place a pressure trace exists (Bruker's Compass database
  records no pressure at all). The extractor gives per-run flags with a
  severity, so a partial occlusion is visible as a *rising curve* long before
  it aborts anything.
* **Bruker maintenance** — from the timsTOF's nightly Compass backup: the
  hard failures the instrument itself logged ("Evosep One: No Evotip was
  present", "pressure limit exceeded", software errors, connection lost).

They overlap: an over-pressure abort appears in both, minutes apart. Point
events are therefore keyed on a 10-minute time bucket rather than on a run
name, so the same physical failure seen by both sources produces one message.
Runs are ~14 min apart at 100 SPD, so a 10-minute bucket can never merge two
distinct injections.

This module only *reads* the analysis documents. The extractor that produces
them is owned elsewhere; consuming its output means the dashboard and the
alerter can never disagree about what counts as a problem — the same reason
`ht_watch` reads what `/api/ht/submission` returns.

QUIET BY DEFAULT
----------------
Every threshold below was chosen against the real 2026-08-14 → 09-01 window
(568 runs, 27 flagged). Firing on every flag would have produced 27 pings in
18 days, most of them for pressure jitter. The rules here would have produced
roughly one alert every other day — and would have caught the 23:18 clog on
2026-08-31 within half an hour of it starting.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from stan.metrics.peg_trends import is_blank_or_wash
from stan.instrument_time import INSTRUMENT_TZ, parse_local as _parse_local, parse_raw as _parse_raw  # noqa: F401
from stan.notify import Alert
from stan.reports.injection_identity import (
    find_run_near, identity_line, sample_from_run_name)

logger = logging.getLogger(__name__)

# ── thresholds ───────────────────────────────────────────────────

#: Flagged runs closer together than this belong to one episode. A 100 SPD
#: run is 14 min, so two hours is ~8 clean runs between events — comfortably
#: past "the same clog" and short of "unrelated trouble the next shift".
EPISODE_GAP_HOURS = 2.0

#: Critical high-pressure runs in an episode before it is called a clog. One
#: can be a bubble or a badly seated Evotip; two consecutive on the same
#: method is the column. At 100 SPD that is ~28 min, so the alert still lands
#: while the clog is happening.
CLOG_CRITICAL_RUNS = 2

#: Flagged runs at ANY severity before an episode is called a rising trend.
#: This is the early warning the pressure trace exists for: 2026-08-29 ran six
#: consecutive `elevated` flags that never reached critical, which is a column
#: heading for trouble and is invisible to the Compass error log.
RISING_RUNS = 4

#: Peak pressure this close to the pump ceiling counts as having hit it, even
#: if the extractor did not tag the run. Belt and braces around `kinds`.
CEILING_MARGIN_BAR = 5.0

#: Baseline drift since the column was installed that is worth planning a
#: change around. 10% is a fortnight of ordinary use; 25% is a bed that is
#: packing down and will clog.
COLUMN_DRIFT_PCT = 25.0

#: Ignore anything older than this. Guards the first run (and a restored
#: state file) from dredging up weeks of resolved history — the same job
#: `--lookback-days` does in bruker_alert.py.
LOOKBACK_HOURS = 24.0

#: Compass failures need a wider window than the Evosep logs, and the reason
#: is a lag, not a preference. The Bruker document is rebuilt nightly at 22:30
#: from a backup the instrument wrote at 18:00, so a failure at 14:38 is not
#: visible to anything until ~8 h later -- and ~32 h later on a night the
#: backup is missed or lands after the rebuild. Under the 24 h window it would be
#: dropped in silence — the precise failure mode this whole change exists to
#: end. Three days matches bruker_alert.py's own `--lookback-days 3`, and is
#: safe to widen because each failure is keyed per event and said once ever.
BRUKER_LOOKBACK_HOURS = 72.0

#: A standing condition that is STILL true this long later has survived a
#: whole shift unnoticed and is worth saying again. Twelve hours is also
#: longer than any clog episode in the record (the worst, 2026-08-28, ran
#: 14 h and would re-ping once), so one clog gives one message.
STANDING_COOLOFF_HOURS = 12.0

#: Column wear is a "order a column" alert, not a "get up" alert.
COLUMN_COOLOFF_HOURS = 24.0 * 7

#: A column younger than this cannot be "worn". A freshly fitted bed is still
#: settling, and its first-hour baseline is not a fair origin to measure drift
#: from -- a big jump there is an install problem, not wear. Brett changed the
#: column on 2026-07-31 and again on 2026-09-02, so a hours-old column is a
#: real, current case, not a hypothetical. Wear takes weeks; waiting costs
#: nothing and stops a confident false positive on a brand-new column.
COLUMN_MIN_AGE_DAYS = 3.0

#: ...and enough use to have worn. An idle instrument can satisfy the age
#: check with almost no injections. ~1 day of 100 SPD acquisition.
COLUMN_MIN_INJECTIONS = 100

#: Percent over the column's ABSOLUTE expected plateau at which a flagged run
#: counts as critical, whatever the trailing-baseline severity says.
#:
#: The extractor's `severity` is scored against a *running* baseline that
#: chases the fault. Measured on the 2026-08-31 clog: essentially the same
#: plateau scored +29.8% at 23:32 and +13.1% at 00:43 -- critical demoted to
#: elevated -- purely because the baseline climbed 321.8 -> 361.1 underneath
#: it. Extrapolate and a clog that persists stops being flagged at all while
#: the column is still fully blocked. `pct_over_expected` is measured against
#: a fixed per-column, per-flow expectation (p5 of the column's own healthy
#: distribution) and cannot drift: those same two runs read +30.2% and +27.2%.
#:
#: CALIBRATED against the deployed field, 2026-09-02 (39 runs, 19 carrying an
#: expectation). The two populations separate cleanly, with nothing at all
#: between 8.7 and 24.2:
#:
#:     unflagged (healthy)   n=8    min  0.0   median  0.6   max  8.7
#:     flagged               n=11   min 24.2   median 30.2   max 61.9
#:
#: 20% sits inside that empty band. The margin is deliberately asymmetric --
#: far closer to the flagged floor than the healthy ceiling -- because the
#: healthy side cannot produce a false positive here at all: this only ever
#: reclassifies a run the extractor ALREADY flagged, so an unflagged run is
#: never tested against it. The job of the number is to catch the weakest real
#: flag, and 24.2 is the weakest observed.
#:
#: A lower-bound value (`wear_is_lower_bound`, set when the record does not
#: reach the column's install) is deliberately NOT excluded here. It means "at
#: least this far over", so a lower bound crossing 20% is a sound detection --
#: the truth is worse, not better. That is the opposite of the column-wear
#: case, where a lower bound understates and must be gated out.
PCT_OVER_EXPECTED_CRITICAL = 20.0

#: Point events are keyed on this bucket so the Evosep log and the Compass
#: database, which see the same failure a minute apart, collapse to one key.
BUCKET_MINUTES = 10

#: Keys for point events omit the instrument label, because the two sources
#: name the same physical station differently ("TIMS-10878" in the Evosep
#: logs, "HPZ6" in Compass) and would otherwise never collapse. Pass an
#: explicit station if a second Evosep/timsTOF pair is ever installed.
DEFAULT_STATION = "tims10878"

#: Compass failure categories worth an alert. "Other failure" is the
#: catch-all and is noise — the same exclusion bruker_alert.py makes.
BRUKER_CATEGORIES = {
    "Evotip missing / not picked up": ("evotip", "warning"),
    "LC pressure / clog": ("overpressure", "critical"),
    "MS / acquisition software error": ("ms_error", "warning"),
    "Connection lost": ("connection_lost", "warning"),
}


# ── helpers ──────────────────────────────────────────────────────


def _parse(ts) -> datetime | None:
    """Parse the timestamps both documents use, as UTC-aware.

    The Evosep doc writes naive local ISO ("2026-09-01T00:15:20"), Compass
    writes "2026-08-31 14:38". Neither carries a zone; both are instrument
    local time. They are only ever compared against each other and against
    `now`, so treating them as UTC keeps the arithmetic consistent — the
    lookback window is a day wide, which absorbs the offset. An age, which
    the offset does not cancel out of, uses `_parse_local` instead.
    """
    dt = _parse_raw(ts)
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _bucket(dt: datetime | None) -> str:
    """A coarse time key, so two sources reporting one failure agree."""
    if dt is None:
        return "unknown"
    floored = dt.replace(minute=(dt.minute // BUCKET_MINUTES) * BUCKET_MINUTES,
                         second=0, microsecond=0)
    return floored.strftime("%Y%m%dT%H%M")


def _band(value: float | None, width: float = 10.0) -> str:
    """Coarse band for a signature, so jitter does not re-alert."""
    if value is None:
        return "?"
    return str(int(value // width) * int(width))


def evosep_label(doc: dict) -> str:
    host = str(doc.get("instrument_host") or "").strip()
    return f"Evosep One ({host})" if host else "Evosep One"


def bruker_label(doc: dict) -> str:
    name = str((doc.get("instrument") or {}).get("name") or "").strip()
    return f"timsTOF {name}" if name else "timsTOF"


def _episodes(flags: list[dict]) -> list[list[dict]]:
    """Group flagged runs into episodes: same method, close together in time.

    Grouping is what turns "40 flagged runs" into "one clog". Method is part
    of the grouping because a wash procedure interleaved with a 100 SPD queue
    is a different pressure regime, not a continuation of the same event.
    """
    dated = [(f, _parse(f.get("start"))) for f in flags]
    dated = [(f, t) for f, t in dated if t is not None]
    dated.sort(key=lambda ft: ft[1])

    out: list[list[dict]] = []
    for flag, t in dated:
        if out:
            prev = out[-1][-1]
            prev_t = _parse(prev.get("start"))
            same_method = prev.get("method") == flag.get("method")
            if (same_method and prev_t is not None
                    and (t - prev_t) <= timedelta(hours=EPISODE_GAP_HOURS)):
                out[-1].append(flag)
                continue
        out.append([flag])
    return out


def impacted_runs(doc: dict, start: datetime | None,
                  end: datetime | None) -> list[dict]:
    """Injections `sample_impact` ties to a baseline step inside this episode.

    "What was in the column when it clogged" is the first thing anyone asks,
    and the extractor already works it out, so a clog alert may as well carry
    it. This ENRICHES the clog alert rather than becoming an alert of its own:
    every step it has found so far coincides with a clog already detected, so
    a separate notification would just say the same thing twice.

    Attribution is correlation, never cause -- a sample that happens to be
    running while a clog is already forming gets blamed for it. That caveat
    travels with the text, because this names somebody's sample. `low`
    confidence joins are dropped rather than hedged.
    """
    if start is None or end is None:
        return []
    out = []
    for f in ((doc.get("sample_impact") or {}).get("flags") or []):
        at = _parse(f.get("at"))
        if at is None or not (start <= at <= end):
            continue
        if str(f.get("confidence") or "").lower() == "low":
            continue
        out.append(f)
    return out


def _impact_lines(hits: list[dict]) -> list[str]:
    """Render the sample attribution, caveat included."""
    if not hits:
        return []
    lines = []
    for h in hits[:3]:
        what = h.get("run_name") or h.get("well") or "?"
        bits = []
        if h.get("well"):
            bits.append(f"well {h['well']}")
        if h.get("confidence"):
            bits.append(f"{h['confidence']} confidence")
        if h.get("is_control"):
            # A blank or HeLa standard fouling the column is a different
            # conversation from a customer's sample doing it.
            bits.append("a control/QC run, not a submitted sample")
        suffix = f" ({', '.join(bits)})" if bits else ""
        lines.append(f"*In the column when the baseline stepped:* `{what}`{suffix}")
    lines.append("_Correlation, not cause — a sample running while a clog is "
                 "already forming will be blamed for it._")
    return lines


def is_critical(flag: dict) -> bool:
    """Critical for clog purposes: trailing severity OR absolute excess.

    A missing `pct_over_expected` is "no opinion", never zero. There is
    deliberately no cross-column fallback in the extractor -- the column
    fitted on 2026-09-02 runs at 399-453 bar where its predecessor's healthy
    floor was 321, so borrowing the old reference would report +24% and +41%
    for a column hours old and fire alarms purely from swapping columns.
    Treating absent as zero would quietly re-introduce exactly that.
    """
    if flag.get("severity") == "critical":
        return True
    if flag.get("expected_unavailable"):
        return False
    pct = flag.get("pct_over_expected")
    if pct is None:
        return False
    try:
        return float(pct) >= PCT_OVER_EXPECTED_CRITICAL
    except (TypeError, ValueError):
        return False


def _at_ceiling(flag: dict, ceiling_bar: float | None) -> bool:
    if "ceiling" in (flag.get("kinds") or []):
        return True
    peak = flag.get("peak_bar")
    if peak is None or not ceiling_bar:
        return False
    return float(peak) >= float(ceiling_bar) - CEILING_MARGIN_BAR


# ── Evosep ───────────────────────────────────────────────────────


def check_evosep(doc: dict, now: datetime | None = None,
                 station: str = DEFAULT_STATION,
                 lookback_hours: float = LOOKBACK_HOURS) -> list[Alert]:
    """Every alert an Evosep column-health document supports.

    Reads only the extractor's own output: the per-run `flags` it already
    scored, plus the `column` block for wear. Nothing here re-derives a
    pressure threshold — the analysis owns that.
    """
    return (check_evosep_episodes(doc, now=now, station=station,
                                  lookback_hours=lookback_hours)
            + check_column_wear(doc, station=station))


def _flag_identity(vial: str | None, start) -> str:
    """Bold ``vial · sample · HH:MM`` for an Evosep flag.

    The clock time is the wall time as written in the document (instrument
    local); the sample comes from PG when it can be found, never failing the
    alert over it.
    """
    sample = None
    try:
        local = _parse_local(start)
        if local is not None:
            sample, run_vial = sample_from_run_name(find_run_near(vial, local))
            vial = vial or run_vial
    except Exception:
        logger.debug("identity lookup failed", exc_info=True)
    return f"*{identity_line(vial, sample, _parse_raw(start))}*"


def check_evosep_episodes(doc: dict, now: datetime | None = None,
                          station: str = DEFAULT_STATION,
                          lookback_hours: float = LOOKBACK_HOURS) -> list[Alert]:
    """The time-bounded faults: over-pressure, clogs, tips, aborts.

    Separate from column wear because these only need the last few days,
    which is what lets the scheduled watcher run a cheap `--since` extract
    every half hour instead of re-parsing years of logs.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    label = evosep_label(doc)
    ceiling = doc.get("ceiling_bar")
    alerts: list[Alert] = []

    recent = [f for f in (doc.get("flags") or [])
              if (_parse(f.get("start")) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff]

    for episode in _episodes(recent):
        method = episode[0].get("method") or "?"
        # Everything this episode produces -- the clog, the over-pressure
        # events, the aborted run -- is ONE incident, so it all threads under
        # whichever of them speaks first. The clog key is the natural name for
        # it: no timestamp, so one episode maps to one thread by construction.
        incident = f"clog:{station}:{method}"
        last = episode[-1]
        last_at = _parse(last.get("start"))
        at_str = last_at.strftime("%Y-%m-%d %H:%M") if last_at else None

        pressure = [f for f in episode if "high_pressure" in (f.get("kinds") or [])]
        criticals = [f for f in pressure if is_critical(f)]
        ceiling_hits = [f for f in episode if _at_ceiling(f, ceiling)]
        worst = max((f.get("pct_over_baseline") or 0) for f in episode) if episode else 0

        # A standing condition is only re-announced while it is still
        # happening. Without this, an episode that ended at 00:43 stays inside
        # the 24 h lookback all day and the cool-off re-pings it at 12:43 as
        # though the column were still blocked. A clog that IS still going
        # keeps producing flagged runs, so its newest flag stays recent and it
        # re-pings on schedule -- which is the case the cool-off is for.
        live = (last_at is not None
                and (now - last_at) <= timedelta(hours=STANDING_COOLOFF_HOURS))

        # 1. The pump reached its hard limit. One run is enough — that is not
        #    a trend, it is the instrument telling you it cannot push through.
        if ceiling_hits:
            hit = max(ceiling_hits, key=lambda f: f.get("peak_bar") or 0)
            alerts.append(Alert(
                key=f"overpressure:{station}:{_bucket(_parse(hit.get('start')))}",
                kind="overpressure",
                thread_key=incident,
                instrument=label,
                severity="critical",
                headline=(f"over-pressure, {hit.get('peak_bar'):.0f} bar "
                          f"(pump limit {float(ceiling):.0f})"
                          if hit.get("peak_bar") and ceiling
                          else "over-pressure at the pump limit"),
                detail=[
                    f"*Run:* `{hit.get('run')}`   *Method:* {method}",
                    f"*Peak:* {hit.get('peak_bar')} bar against a "
                    f"{ceiling} bar pump ceiling",
                    f"*Plateau:* {hit.get('plateau_bar')} bar "
                    f"({hit.get('pct_over_baseline')}% over the "
                    f"{hit.get('baseline_bar')} bar running baseline)",
                    f"{len(ceiling_hits)} run(s) in this episode hit the ceiling.",
                ],
                at=at_str,
                extra={"run": hit.get("run"), "method": method,
                       "peak_bar": hit.get("peak_bar"), "ceiling_bar": ceiling},
            ))

        # 2. A clog: consecutive critical high-pressure runs on one method.
        #    Standing condition -- keyed without a time, so the whole episode
        #    is one alert and only gets repeated if it is still going.
        if live and len(criticals) >= CLOG_CRITICAL_RUNS:
            newest = criticals[-1]
            alerts.append(Alert(
                key=f"clog:{station}:{method}",
                kind="clog",
                thread_key=incident,
                instrument=label,
                severity="critical",
                signature=f"critical:{_band(worst)}",
                cool_off_hours=STANDING_COOLOFF_HOURS,
                headline=(f"column clog — {newest.get('plateau_bar'):.0f} bar, "
                          f"{newest.get('pct_over_baseline'):.0f}% over baseline"
                          if newest.get("plateau_bar") is not None
                          and newest.get("pct_over_baseline") is not None
                          else "column clog"),
                detail=[
                    f"{len(criticals)} consecutive runs at critical back-pressure "
                    f"on *{method}* (threshold: {CLOG_CRITICAL_RUNS}).",
                    f"*Now:* {newest.get('plateau_bar')} bar vs a "
                    f"{newest.get('baseline_bar')} bar baseline "
                    f"(+{newest.get('pct_over_baseline')}%), peak "
                    f"{newest.get('peak_bar')} bar of {ceiling}.",
                    f"*Started:* {(_parse(episode[0].get('start')) or now).strftime('%Y-%m-%d %H:%M')}",
                    "Reasons: " + "; ".join(newest.get("reasons") or []),
                    *_impact_lines(impacted_runs(
                        doc, _parse(episode[0].get("start")), last_at)),
                ],
                at=at_str,
                extra={"method": method, "n_critical": len(criticals),
                       "worst_pct_over_baseline": worst},
            ))

        # 3. Pressure trending up without any run going critical yet. The
        #    whole reason the pressure trace is worth having.
        elif live and len(pressure) >= RISING_RUNS:
            newest = pressure[-1]
            alerts.append(Alert(
                key=f"pressure_rising:{station}:{method}",
                kind="pressure_rising",
                thread_key=incident,
                instrument=label,
                severity="warning",
                signature=f"elevated:{_band(worst)}",
                cool_off_hours=STANDING_COOLOFF_HOURS,
                headline=(f"back-pressure climbing — {len(pressure)} runs "
                          f"up to {worst:.0f}% over baseline"),
                detail=[
                    f"{len(pressure)} consecutive runs above the running "
                    f"baseline on *{method}* (threshold: {RISING_RUNS}).",
                    f"*Latest:* {newest.get('plateau_bar')} bar vs "
                    f"{newest.get('baseline_bar')} bar baseline "
                    f"(+{newest.get('pct_over_baseline')}%).",
                    "Not critical yet — this is the early warning, while a "
                    "wash can still fix it.",
                ],
                at=at_str,
                extra={"method": method, "n_runs": len(pressure)},
            ))

        # 4. Evotip seating failures and bare aborts. Point events: one
        #    injection lost, said once.
        for f in episode:
            kinds = f.get("kinds") or []
            f_at = _parse(f.get("start"))
            if "tip" in kinds:
                alerts.append(Alert(
                    key=f"evotip:{station}:{_bucket(f_at)}",
                    kind="evotip",
                thread_key=incident,
                    instrument=label,
                    severity="warning",
                    headline="Evotip not seated — injection aborted",
                    detail=[_flag_identity(f.get("well"), f.get("start")),
                            f"Evosep log: `{f.get('run')}`   *Method:* {f.get('method')}",
                            f"*Low-pressure-side peak:* "
                            f"{f.get('tip_pressure_max_bar')} bar"],
                    at=f_at.strftime("%Y-%m-%d %H:%M") if f_at else None,
                    extra={"run": f.get("run")},
                ))
            elif "aborted" in kinds:
                alerts.append(Alert(
                    key=f"aborted:{station}:{_bucket(f_at)}",
                    kind="aborted",
                thread_key=incident,
                    instrument=label,
                    severity="warning",
                    headline="run aborted part-way",
                    detail=[_flag_identity(f.get("well"), f.get("start")),
                            f"Evosep log: `{f.get('run')}`   *Method:* {f.get('method')}",
                            f"*Ran for* {f.get('duration_min')} min. "
                            + "; ".join(f.get("reasons") or [])],
                    at=f_at.strftime("%Y-%m-%d %H:%M") if f_at else None,
                    extra={"run": f.get("run")},
                ))

    return alerts


def check_column_wear(doc: dict, station: str = DEFAULT_STATION) -> list[Alert]:
    """Baseline drift since the column was installed.

    Not an episode — a standing fact about the column currently fitted, worth
    one nudge a week.

    The hard part is not the drift, it is trusting the install date it is
    measured from. When the extractor cannot find a logged column change it
    infers one from a step drop in baseline pressure, and a step drop is not
    specific to a column: on 2026-09-02 the inferred date was 2026-08-19,
    which turned out to be a *glass capillary* swap. The real column change
    was 2026-07-31, nineteen days earlier and outside the mirrored log window
    entirely — so `baseline_change_pct` was measured from a false origin and
    `days_since` understated a ~32-day-old column by more than half.

    Worse, the answer moves with the window: three extracts on 2026-09-02 of
    the same instrument gave 2026-09-01 (3-day), 2026-08-19 (full history)
    and the truth, 2026-07-31. So this trusts nothing the document does not
    positively vouch for — see the `confidence` gate below. The real fix is
    upstream: log column changes as `column_change` maintenance events, which
    puts `column_age` on its "logged" path and makes it correct regardless of
    how far back the logs happen to reach.
    """
    label = evosep_label(doc)
    alerts: list[Alert] = []
    col = doc.get("column") or {}
    drift = col.get("baseline_change_pct")

    # TWO independent things have to be true, and they fail differently.
    #
    # 1. The install DATE must be trustworthy. `known` alone is not enough:
    #    on 2026-09-02 the extractor inferred 2026-08-19 from a step drop in
    #    baseline pressure, but the operator's own run names record the real
    #    change on 2026-07-31 -- what happened on 08-19 was a glass capillary
    #    swap. Three windows that same day gave three answers (2026-09-01,
    #    2026-08-19, and the truth). So `confidence` must positively vouch
    #    for it; a document too old to carry the field is one of those
    #    windows with no way to tell which, and does not get a say.
    #
    # 2. The BASELINE ORIGIN must span the install. `baseline_change_pct` is
    #    only "wear" if it is measured from the install; measured from the
    #    start of a short window it describes that window and reads far too
    #    low, so the threshold would never fire and nothing would ever say
    #    so. A silent miss, which is the shape of the problem this whole
    #    change exists to end. `log_covers_install` is how a document says
    #    which it is -- and it is also how the alert step tells the daily
    #    full document from the 30-minute one.
    #
    # 3. The column must be OLD ENOUGH for "worn" to be a claim about wear.
    #    Brett changed the column twice: 2026-07-31 and again on 2026-09-02.
    #    The current one is hours old, and a fresh column is exactly where a
    #    drift number misleads in the other direction -- a bed still settling
    #    can show a large jump from its first-hour baseline, and calling that
    #    "worn" would be a confident false positive on a column that was just
    #    fitted. Wear takes weeks, so nothing is lost by waiting.
    #
    # (History, because the numbers are easy to misread: on 2026-09-02 the
    # document reported days_since 32.99 against observed_days 1.94. That
    # 32.99 was NOT the current column's age -- the resolver had anchored to
    # the older 07-31 event, so it was the age of the column that had just
    # been REMOVED. Two different faults, one pair of numbers.)
    #
    # Fails CLOSED on all three. A wear alert is a nudge to order a column; a
    # nudge built on a number nobody can stand behind is worse than none.
    trustworthy_date = (str(col.get("confidence") or "") in ("logged", "inferred")
                        and not col.get("installed_is_lower_bound"))
    trustworthy_baseline = (bool(col.get("log_covers_install"))
                            and not col.get("counts_are_lower_bounds"))
    age_days = col.get("days_since")
    injections = col.get("injections_since")
    old_enough = (age_days is not None and float(age_days) >= COLUMN_MIN_AGE_DAYS
                  and injections is not None
                  and int(injections) >= COLUMN_MIN_INJECTIONS)
    if not (trustworthy_date and trustworthy_baseline and old_enough):
        logger.info(
            "column wear not judgeable (confidence=%s, "
            "installed_is_lower_bound=%s, log_covers_install=%s, "
            "counts_are_lower_bounds=%s, days_since=%s, injections_since=%s); "
            "skipping",
            col.get("confidence"), col.get("installed_is_lower_bound"),
            col.get("log_covers_install"), col.get("counts_are_lower_bounds"),
            age_days, injections)
        return alerts

    if col.get("known") and drift is not None and float(drift) >= COLUMN_DRIFT_PCT:
        alerts.append(Alert(
            key=f"column_worn:{station}",
            kind="column_worn",
            instrument=label,
            severity="warning",
            signature=f"drift:{_band(float(drift))}",
            cool_off_hours=COLUMN_COOLOFF_HOURS,
            headline=(f"column baseline up {float(drift):.0f}% since install "
                      f"(threshold {COLUMN_DRIFT_PCT:.0f}%)"),
            detail=[
                f"*Baseline:* {col.get('baseline_at_install_bar')} bar at install "
                f"-> {col.get('baseline_now_bar')} bar now.",
                f"*Installed:* {col.get('installed')} — "
                f"{col.get('injections_since')} injections, "
                f"{col.get('days_since')} days ago.",
                "Worth planning a column change before it clogs mid-plate.",
            ],
            extra={"baseline_change_pct": drift},
        ))

    return alerts


# ── Bruker / Compass ─────────────────────────────────────────────


def check_bruker(doc: dict, now: datetime | None = None,
                 station: str = DEFAULT_STATION,
                 lookback_hours: float = BRUKER_LOOKBACK_HOURS) -> list[Alert]:
    """Alerts from a Bruker maintenance document.

    The same failures `bruker_alert.py` already emails. Slack is added
    alongside that email, not instead of it: Brett reads the mail, and a
    channel he relies on should not silently disappear because a new one
    appeared.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=lookback_hours)
    label = bruker_label(doc)
    alerts: list[Alert] = []

    for f in doc.get("failures_recent") or []:
        cat = f.get("category")
        mapped = BRUKER_CATEGORIES.get(cat)
        if not mapped:
            continue
        kind, severity = mapped
        at = _parse(f.get("start_date"))
        if at is None or at < cutoff:
            continue
        msg = (f.get("message") or "").strip()
        sample, fvial = sample_from_run_name(f.get("fname"))
        ident = f"*{identity_line(f.get('well') or fvial, sample, _parse_raw(f.get('start_date')))}*"
        alerts.append(Alert(
            key=f"{kind}:{station}:{_bucket(at)}",
            kind=kind,
            instrument=label,
            severity=severity,
            headline=cat,
            detail=[
                ident,
                f"> {msg[:220]}" if msg else "",
                f"*File:* `{f.get('fname')}`",
            ],
            at=at.strftime("%Y-%m-%d %H:%M"),
            extra={"fname": f.get("fname"), "category": cat},
        ))
    return alerts


#: How stale a feed's newest DATA may get before it is worth a human.
#:
#: Evosep gets more slack than Bruker because its logs only appear when the
#: Evosep actually runs: a Friday-to-Tuesday holiday weekend is four quiet
#: days and entirely normal. Bruker's backup is written at 18:00 whether or
#: not anyone acquires, so it is held to a tighter line.
#:
#: Not 2.0, though, which is what it was until 1.2.19 -- and which alerted
#: every day on a perfectly healthy feed. The copy lands on Hive at ~20:41,
#: cron_bruker_maintenance.sh ran at 20:00 and so always read the day before,
#: and the age was counted from that day's midnight in UTC. A document was
#: already 2.1 d "old" the moment it was published.
#:
#: The arithmetic now, with the cron at 22:30 and the age taken from the
#: backup's own 18:00 stamp: a healthy document is at most 28.5 h old
#: (yesterday's backup, just before tonight's rebuild). One missed or late
#: backup -- 10, 14 and 17 Oct 2026 have none -- takes that to 52.5 h, 2.19 d.
#: Two in a row cross 2.5 d at 06:00 the morning after the second. So 2.5 is
#: the line that rides out one miss and catches two.
EVOSEP_STALE_DAYS = 3.0
BRUKER_STALE_DAYS = 2.5

#: How far the last Compass acquisition may trail the last Evosep log and
#: still be "the same last run". Each acquisition's Evosep procedure starts
#: minutes before it; an hour absorbs that and any drift between the clocks.
IDLE_SLACK = timedelta(hours=1)

#: How long after an acquisition the published Evosep document must have
#: been written before its silence about that acquisition means anything:
#: the instrument copies the logs every 60 min, and the extract runs on the
#: next tick. The document is only republished once it is 20 h old
#: (cron_evosep.sh PUBLISH_MAX_AGE_H), so when the timsTOF comes back from an
#: idle spell the Compass backup can show the new runs a whole evening before
#: the document does.
EVOSEP_REFRESH_SLACK = timedelta(hours=2)

#: Compass names each backup folder for the moment it was taken:
#: .../daily/2026-10-18_180000/compass.backup.
_BACKUP_STAMP = re.compile(r"(\d{4}-\d{2}-\d{2})_(\d{2})(\d{2})(\d{2})")


def bruker_backup_time(doc: dict | None) -> datetime | None:
    """When the Compass backup behind ``doc`` was taken.

    `backup_date` is only the day, and counted from its midnight an 18:00
    backup reads 18 h older than it is. The folder name in `backup_path`
    carries the time, so it wins; the bare date is the fallback.
    """
    doc = _as_dict(doc)
    stamps = _BACKUP_STAMP.findall(str(doc.get("backup_path") or ""))
    if stamps:
        day, hh, mm, ss = stamps[-1]
        at = _parse_local(f"{day}T{hh}:{mm}:{ss}")
        if at is not None:
            return at
    return _parse_local(doc.get("backup_date"))


def _as_dict(value) -> dict:
    """``value`` if it is a dict, else empty -- for reading odd documents."""
    return value if isinstance(value, dict) else {}


def _explain_evosep_silence(alert: Alert, evosep: dict | None,
                            evosep_at: datetime, bruker: dict | None,
                            bruker_at: datetime | None,
                            now: datetime) -> Alert | None:
    """Say whether silent Evosep logs mean an idle timsTOF or a dead copy.

    Evosep logs only exist when the Evosep runs, so their absence alone cannot
    tell "nobody is acquiring" from "the copy stopped". The Compass backup
    can: it records every acquisition, and it reaches Hive by its own copy.

    The tempting signal -- no new raws on Flinders, so call it idle and stay
    quiet -- would be wrong. Those raws come off the same PC, so a PC that
    stops sending everything would read as idle, and the alert would go
    silent exactly when it mattered. Nothing here is muted: idle is said once,
    acquiring-without-logs is said louder than before.

    Each verdict needs its own evidence, and without it the alert stays as it
    was:

      * Only a backup taken AFTER the last Evosep log can testify about the
        silence at all.
      * "Idle" also needs that backup to be current. A PC that stops sending
        everything leaves a last backup that still postdates the last Evosep
        log, and calling that idle would contradict the bruker_stale alert
        firing beside it.
      * "Kept acquiring" needs an Evosep document written after those
        acquisitions. Until then this returns None -- no alert this tick --
        because the document is only republished every 20 h and has simply
        not caught up yet. check_publish_freshness covers it never doing so.
    """
    summary = _as_dict(_as_dict(bruker).get("summary"))
    last_acq = _parse_local(summary.get("last_run"))
    if last_acq is None or bruker_at is None or bruker_at <= evosep_at:
        return alert
    status = _as_dict(_as_dict(bruker).get("instrument")).get("status_text") or "?"
    last_log = evosep_at.strftime("%Y-%m-%d %H:%M")

    if last_acq <= evosep_at + IDLE_SLACK:
        if (now - bruker_at).total_seconds() / 86400.0 > BRUKER_STALE_DAYS:
            return alert
        return replace(
            alert,
            severity="info",
            headline=(f"timsTOF idle since {last_acq:%a %d %b %H:%M} "
                      "-- no Evosep logs expected"),
            detail=[
                f"*Last acquisition (Compass):* "
                f"{last_acq:%Y-%m-%d %H:%M}, status `{status}`",
                f"*Last Evosep log:* {last_log}",
                f"*Checked against the backup of:* {bruker_at:%Y-%m-%d %H:%M}",
                "Nothing to fix on the instrument PC. Said once; it speaks "
                "again if acquisitions resume without Evosep logs.",
            ],
            # One message per idle spell: the signature names the spell, and
            # no cool-off means it is never repeated while that holds.
            signature=f"idle:{evosep_at:%Y%m%dT%H%M}",
            cool_off_hours=None,
            extra={**alert.extra, "last_acquisition": last_acq.isoformat()},
        )

    written = _parse_local(_as_dict(evosep).get("generated_at"))
    if written is None:
        return alert
    if written < last_acq + EVOSEP_REFRESH_SLACK:
        return None

    whole = alert.extra["whole_days"]
    return replace(
        alert,
        severity="critical",
        headline=(f"Evosep logs stopped arriving {whole} days ago "
                  "while the timsTOF kept acquiring"),
        detail=[
            f"*Last Evosep log:* {last_log}",
            f"*Compass recorded acquisitions through:* "
            f"{last_acq:%Y-%m-%d %H:%M}",
            "*Feed:* Y:\\STAN\\evosep_logs -- copy_evosep_logs.bat "
            "on the instrument",
            "Either that copy has stopped, or those runs did not use the "
            "Evosep.",
        ],
        signature=f"acquiring:{whole}d",
        extra={**alert.extra, "last_acquisition": last_acq.isoformat()},
    )


def check_feed_freshness(evosep: dict | None, bruker: dict | None,
                         now: datetime | None = None,
                         station: str = DEFAULT_STATION) -> list[Alert]:
    """Alert when an instrument feed stops arriving.

    THIS FIRES ON ABSENCE, NOT ON FAILURE, and that is the whole point.

    The obvious design is to have the copy script Slack us when it errors.
    That would have caught none of what actually happened. The Evosep mirror
    stopped on 2026-09-03 and the Bruker backups on 2026-09-01, and neither
    copy ever failed -- they simply never ran again, because nothing was
    scheduled to run them. A component cannot report its own silence. Worse,
    the Hive consumers kept succeeding against the frozen inputs: the nightly
    Bruker extract re-published the same 2026-08-31 backup for eight days,
    stamping a fresh `updated_at` each time, so every freshness signal in the
    system said "fine" while the panel showed a column that had been replaced.

    So the check lives here, on Hive, where something is still alive, and
    watches the one number that cannot be faked: how old the newest DATA is.
    `generated_at` deliberately goes unread -- it says the job ran, not that
    anything arrived.

    A silent Evosep feed is then checked against the Compass backup, which
    records every acquisition: see `_explain_evosep_silence`.

    Args:
        evosep: Evosep column-health document, or None if absent.
        bruker: Bruker maintenance document, or None if absent.
        now: Override for tests.
        station: Instrument label for the alert.

    Returns:
        Zero, one or two alerts. Silent when both feeds are current.
    """
    now = now or datetime.now(timezone.utc)
    alerts: list[Alert] = []

    evosep_at = _parse_local(_as_dict(_as_dict(evosep).get("summary")).get("last_run"))
    bruker_at = bruker_backup_time(bruker)
    feeds = [
        ("evosep_stale", "Evosep procedure logs", EVOSEP_STALE_DAYS, evosep_at,
         evosep_label(evosep) if evosep else station,
         "Y:\\STAN\\evosep_logs -- copy_evosep_logs.bat on the instrument"),
        ("bruker_stale", "Bruker Compass backup", BRUKER_STALE_DAYS, bruker_at,
         bruker_label(bruker) if bruker else station,
         "Y:\\STAN\\BrukerDBBackup -- copy_bruker_backup.bat on the instrument"),
    ]

    for kind, what, max_days, at, label, where in feeds:
        if at is None:
            continue
        age_days = (now - at).total_seconds() / 86400.0
        if age_days <= max_days:
            continue
        whole = int(age_days)
        alert = Alert(
            key=f"{kind}:{station}",
            kind=kind,
            instrument=label,
            severity="critical" if age_days >= max_days * 3 else "warning",
            headline=f"{what} stopped arriving {whole} days ago",
            detail=[
                f"*Newest data:* {at:%Y-%m-%d %H:%M} "
                f"({whole}d old, alerting past {max_days:g}d)",
                f"*Feed:* {where}",
                "The panel keeps rendering, so this is invisible on the "
                "dashboard until someone checks the date.",
            ],
            # The condition persists, so re-alerting every 20 min would be
            # noise. The signature is the whole-day age, so it speaks again
            # once a day while broken and escalates rather than going quiet.
            signature=f"{whole}d",
            cool_off_hours=24.0,
            at=at.strftime("%Y-%m-%d %H:%M"),
            extra={"age_days": round(age_days, 1), "newest": at.isoformat(),
                   "whole_days": whole},
        )
        if kind == "evosep_stale":
            alert = _explain_evosep_silence(alert, evosep, at, bruker,
                                            bruker_at, now)
        if alert is not None:
            alerts.append(alert)
    return alerts

#: How long a published telemetry document may go unrefreshed. Evosep
#: republishes whenever the live document passes 20 h, Bruker nightly, so two
#: days is two missed cycles either way.
PUBLISH_STALE_DAYS = 2.0

#: The PG documents the dashboard actually serves, and the cron that should
#: be refreshing each one.
_PUBLISHED_DOCS = (
    ("evosep_publish_stale", "evosep_column_health",
     "Evosep column health", "cron_evosep.sh (every 30 min)"),
    ("bruker_publish_stale", "bruker_maintenance",
     "Bruker maintenance", "cron_bruker_maintenance.sh (nightly 22:30)"),
)


def check_publish_freshness(now: datetime | None = None,
                            station: str = DEFAULT_STATION) -> list[Alert]:
    """Alert when a document stops being REPUBLISHED, even with fresh data.

    check_feed_freshness answers "did the data stop arriving". This answers
    the other half: "did the job that turns data into a panel stop running".
    They are genuinely different, and on 2026-09-17 only the second was true
    -- Evosep logs were landing through the current day while the panel
    served a document last written eight days earlier, because
    cron_evosep.sh had silently lost its execute bit.

    The data-age check could not see it. cron_evosep.sh hands
    instrument-watch a FRESHLY GENERATED extract via --evosep-json, so
    last_run read as today and the check was satisfied. Only the published
    row was stale, so only the published row can reveal it.

    Reads PG directly rather than any document passed in, for exactly that
    reason: a caller holding a fresh copy is the situation this is meant to
    catch. No-ops on a SQLite install, which has no published documents.
    """
    now = now or datetime.now(timezone.utc)
    try:
        from stan.db_pg import use_pg
        if not use_pg():
            return []
        from stan.db_pg import _connect
    except Exception:  # noqa: BLE001
        return []

    alerts: list[Alert] = []
    try:
        with _connect() as con, con.cursor() as cur:
            for kind, table, label, who in _PUBLISHED_DOCS:
                try:
                    cur.execute(f"SELECT updated_at FROM {table}")  # noqa: S608
                    row = cur.fetchone()
                except Exception:  # noqa: BLE001 - table absent on an older DB
                    con.rollback()
                    continue
                if not row or not row[0]:
                    continue
                ts = row[0]
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_days = (now - ts).total_seconds() / 86400.0
                if age_days <= PUBLISH_STALE_DAYS:
                    continue
                whole = int(age_days)
                alerts.append(Alert(
                    key=f"{kind}:{station}",
                    kind=kind,
                    instrument=station,
                    severity="critical" if age_days >= PUBLISH_STALE_DAYS * 3 else "warning",
                    headline=f"{label} has not been republished for {whole} days",
                    detail=[
                        f"*Last published:* {ts:%Y-%m-%d %H:%M} UTC ({whole}d ago, "
                        f"alerting past {PUBLISH_STALE_DAYS:g}d)",
                        f"*Expected from:* {who}",
                        "New data may still be arriving -- this says the job that "
                        "publishes it has stopped, which the panel cannot show.",
                    ],
                    signature=f"{whole}d",
                    cool_off_hours=24.0,
                    at=ts.strftime("%Y-%m-%d %H:%M"),
                    extra={"table": table, "age_days": round(age_days, 1)},
                ))
    except Exception:  # noqa: BLE001 - never break the watch over telemetry
        logger.debug("publish-freshness check failed", exc_info=True)
    return alerts

#: Every scheduled job on Hive, and how long its silence may run before that
#: silence is itself the news. Each is several missed cycles, not one, so a
#: slow tick or a reboot does not cry wolf.
#:
#: This is the generic form of the 2026-09-09 failure: cron_evosep.sh lost
#: its execute bit, cron answered "Permission denied" into nothing, and
#: nobody found out for eight days. No per-feed check catches that, because
#: the symptom is a job producing NO output at all. A job that has stopped
#: writing its log has stopped, whatever the reason -- wrong permissions, a
#: syntax error, a dead interpreter, an unmounted share, a removed crontab
#: line. All of them look identical from here, and all of them matter.
#: job label -> (log glob, hours of silence allowed).
#:
#: The glob is explicit rather than derived from the label, because the log
#: names do not follow one rule: the acquisition counter writes
#: count_acq_submit_*, the Flinders dispatcher writes cron_flinders_*, and a
#: pattern built from the job name silently matched NEITHER -- a heartbeat
#: check quietly monitoring five of eight jobs, which is the failure it
#: exists to prevent.
#:
#: Anchored on "20" so cron_evosep_20260917.log matches and
#: cron_evosep_watch_20260917.log does not. Without that, a dead
#: cron_evosep would be masked by its sibling still writing.
#:
#: Three of them append to ONE undated file instead of rotating daily, which
#: is a third naming convention and the reason a date-anchored pattern found
#: none of them. Those are named exactly, so nothing can shadow them.
#:
#: db_backup is the one entry here whose absence would be silent in a way
#: that matters most: PG Farm keeps no backups of its own, so if that job
#: stops, nothing else in the system notices and the loss only surfaces when
#: a restore is needed. It is a daily 03:17 tick, so 30 h is one missed run.
CRON_LOGS = {
    "evosep":             ("cron_evosep_20*.log", 3.0),
    "ht_watch":           ("cron_ht_watch_20*.log", 3.0),
    "stan_alerts":        ("cron_stan_alerts_20*.log", 3.0),
    "flinders_dispatch":  ("cron_flinders_20*.log", 3.0),
    "count_acquisitions": ("count_acq_submit.log", 6.0),
    "ioncloud":           ("cron_ioncloud.log", 6.0),
    "community_sync":     ("cron_community_sync_20*.log", 14.0),
    "bruker_maint":       ("cron_bruker_maint_20*.log", 30.0),
    "db_backup":          ("db_backup_submit.log", 30.0),
}

#: Where the cron scripts write their per-day logs.
CRON_LOG_DIR = "/quobyte/proteomics-grp/STAN/logs"


def check_cron_heartbeat(now: datetime | None = None,
                         log_dir: str | None = None,
                         station: str = DEFAULT_STATION) -> list[Alert]:
    """Alert on a scheduled job that has stopped producing output.

    Deliberately measures the LOG FILE's mtime rather than asking whether
    the work succeeded. A job can fail loudly every tick and still be alive;
    what went unnoticed for eight days was a job that stopped saying
    anything. Silence is the signal. A job that fails while it still talks
    is :func:`check_job_failures`' question.

    Skips a job with no log at all rather than alerting: that is a job never
    installed on this host, not one that has died.
    """
    import glob
    import os

    now = now or datetime.now(timezone.utc)
    root = log_dir or CRON_LOG_DIR
    alerts: list[Alert] = []

    # If NOT ONE expected log exists, this is not Hive -- a single-lab
    # install runs none of these -- and the whole check no-ops. But once any
    # of them is present, a missing one is a real absence rather than a
    # different deployment, and saying nothing about it is how this check
    # reported five of eight jobs healthy while watching only five.
    if _cron_logs_present(root) == 0:
        return []

    for name, (pattern, max_h) in sorted(CRON_LOGS.items()):
        try:
            hits = glob.glob(os.path.join(root, pattern))
        except Exception:  # noqa: BLE001
            continue
        if not hits:
            alerts.append(Alert(
                key=f"cron_nolog:{name}",
                kind="cron_silent",
                instrument=station,
                severity="warning",
                headline=f"cron_{name} has never written a log",
                detail=[
                    f"*Expected:* {os.path.join(root, pattern)}",
                    "Either the job has never run here, or its log name "
                    "changed and this check has been skipping it silently.",
                ],
                signature="missing",
                cool_off_hours=24.0,
                extra={"cron": name, "missing_log": True},
            ))
            continue
        try:
            newest = max(hits, key=os.path.getmtime)
            seen = datetime.fromtimestamp(os.path.getmtime(newest), tz=timezone.utc)
        except OSError:
            continue
        age_h = (now - seen).total_seconds() / 3600.0
        if age_h <= max_h:
            continue
        hours = int(age_h)
        alerts.append(Alert(
            key=f"cron_silent:{name}",
            kind="cron_silent",
            instrument=station,
            severity="critical" if age_h >= max_h * 4 else "warning",
            headline=f"cron_{name}.sh has written nothing for {hours} h",
            detail=[
                f"*Last output:* {seen:%Y-%m-%d %H:%M} UTC "
                f"({hours}h ago, alerting past {max_h:g}h)",
                f"*Log:* {newest}",
                "The job is not running, or is dying before it can log. "
                "Check the execute bit first -- that is how this failed on "
                "2026-09-09.",
            ],
            signature=f"{hours // 24}d",
            cool_off_hours=24.0,
            at=seen.strftime("%Y-%m-%d %H:%M"),
            extra={"cron": name, "age_hours": round(age_h, 1)},
        ))
    return alerts


# ── jobs that keep failing ───────────────────────────────────────

#: STAN's scheduled SLURM jobs, by job name, and how many finished runs in a
#: row must fail before that is news. Anything not named here is ignored, so
#: FRAN's and everyone else's jobs under the same account never post to
#: STAN's channel. The database backup alerts on one: PG Farm keeps no backup
#: of its own (see CRON_LOGS).
SLURM_JOBS = {
    "stan-ioncloud":  3,    # hourly, 4-task array
    "stan_count_acq": 4,    # every 15 min
    "stan_db_backup": 1,    # daily
}

#: The cron (a :data:`CRON_LOGS` key) that submits each of SLURM_JOBS. Its
#: heartbeat limit is also how long the job may go without finishing a run,
#: and its log being here is what says the job is scheduled on this host.
SLURM_JOB_CRON = {
    "stan-ioncloud":  "ioncloud",
    "stan_count_acq": "count_acquisitions",
    "stan_db_backup": "db_backup",
}

#: The per-file jobs -- one per raw file: stan-mon-<run> and the
#: dispatcher's stan-<run> searches -- are not runs of one job, so "in a row"
#: does not fit them. An array (stan-peg-thermo, stan-4dffcloud) is a bulk
#: run somebody started by hand, not a file, and is not counted. This many
#: files whose latest attempt failed within the window is news (Brett,
#: 2026-10-21). Blanks and washes are not counted:
#: the monitor finds nothing to measure in them and records that as a
#: failure, so over 30 days they were 22 of the 24 files that failed, and
#: counting them posted on 6 days instead of 1.
QC_FAIL_FILES = 3
QC_FAIL_WINDOW_HOURS = 24.0

#: How far back sacct is asked: 48 ion-cloud runs, two backups.
JOB_LOOKBACK_HOURS = 48.0

#: Where sacct lives on all three UC Davis clusters, for a cron PATH without it.
SACCT_FALLBACK = ("/cvmfs/hpc.ucdavis.edu/sw/spack/environments/core/view/"
                  "generic/slurm/bin/sacct")

_FAILED_STATES = frozenset({"FAILED", "TIMEOUT", "OUT_OF_MEMORY", "NODE_FAIL",
                            "BOOT_FAIL", "DEADLINE"})
#: Finished, one way or another. CANCELLED and PREEMPTED say nothing about
#: whether the job works, so they neither extend a streak nor end one.
_ENDED_STATES = _FAILED_STATES | {"COMPLETED", "CANCELLED", "PREEMPTED"}
_SACCT_FIELDS = "JobID,JobIDRaw,JobName,State,Start,End,StdOut,StdErr"
_ERROR_LINE = re.compile(
    r"\[err\]|Traceback|\bERROR\b|Error\b|error:|FAILED:|\bABORT\b")
#: How much of a job's log is read, every tick, on a login node.
_LOG_HEAD_LINES = 2000
_LOG_HEAD_BYTES = 1024 * 1024
_LOG_LINE_BYTES = 64 * 1024
_LOG_TAIL_BYTES = 64 * 1024


def _cron_logs_present(root: str) -> int:
    """How many of :data:`CRON_LOGS` have a log under ``root``; 0 off Hive."""
    import glob
    import os

    present = 0
    for _n, (pat, _h) in CRON_LOGS.items():
        try:
            if glob.glob(os.path.join(root, pat)):
                present += 1
        except Exception:  # noqa: BLE001
            pass
    return present


def _sacct_text(lookback_hours: float) -> str | None:
    """This account's jobs from the last ``lookback_hours``; None without SLURM."""
    import os

    exe = shutil.which("sacct") or (
        SACCT_FALLBACK if os.path.exists(SACCT_FALLBACK) else None)
    if exe is None:
        return None
    proc = subprocess.run(
        [exe, "-X", "-n", "-P", "-S", f"now-{int(lookback_hours)}hours",
         "-E", "now", f"--format={_SACCT_FIELDS}"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    return proc.stdout


def _parse_sacct(text: str) -> list[dict]:
    """Rows of ``sacct -X -P`` output, with State cut to its first word."""
    rows = []
    for line in (text or "").splitlines():
        f = line.split("|")
        if len(f) != 8:
            continue
        jobid, raw, name, state, start, end, out, err = f
        head = jobid.split("_")[0]
        if not head.isdigit() or not name:
            continue
        rows.append({"jobid": jobid, "run": int(head), "raw": raw, "name": name,
                     "state": (state.split() or [""])[0], "start": start,
                     "end": end, "out": out, "err": err})
    return rows


def _expand_log(path: str, row: dict) -> str:
    """Fill in the SLURM filename patterns sacct reports unexpanded."""
    run, _, task = row["jobid"].partition("_")
    return (path.replace("%A", run).replace("%a", task)
            .replace("%j", row["raw"]).replace("%x", row["name"]))


def _first_error(row: dict) -> str | None:
    """The cause of a failed job, from its stdout, then stderr, if readable.

    The per-file jobs end with one JSON status line whose ``error`` field is
    the cause ("rawmeat extraction returned empty"); that wins, so it is
    looked for in the log's tail first. A crashed monitor logs "ERROR
    step_monitor failed" and a traceback above it, which say less.
    Otherwise the first line that looks like an error. Stops at the
    cluster's job-summary epilog, whose "State : FAILED" says nothing new,
    and skips WARNING lines: PG Farm prints a collation warning on every
    connection. Every read is capped, because this runs on a login node
    every tick.
    """
    import json
    import os

    def _status_error(line: str) -> str | None:
        if not (line.startswith("{") and '"status": "error"' in line):
            return None
        try:
            why = json.loads(line).get("error")
        except (ValueError, AttributeError):
            return None
        return str(why)[:200] if why else None

    seen: set[str] = set()
    for key in ("out", "err"):
        if not row.get(key):
            continue
        path = _expand_log(row[key], row)
        if path in seen:                   # no --error: sacct repeats StdOut
            continue
        seen.add(path)
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - _LOG_TAIL_BYTES))
                tail = fh.read(_LOG_TAIL_BYTES).decode(errors="replace")
                for line in reversed(tail.splitlines()):
                    why = _status_error(line)
                    if why:
                        return why
                fh.seek(0)
                budget = _LOG_HEAD_BYTES
                for _ in range(_LOG_HEAD_LINES):
                    raw = fh.readline(min(_LOG_LINE_BYTES, budget))
                    budget -= len(raw)
                    if not raw or budget <= 0:
                        break
                    line = raw.decode(errors="replace")
                    if line.startswith("#####"):
                        break
                    if "WARNING" not in line and _ERROR_LINE.search(line):
                        return line.strip()[:200]
        except (OSError, ValueError):
            continue
    return None


def _failing_streak(name: str, need: int, rows: list[dict], now: datetime,
                    station: str) -> Alert | None:
    """Alert when the newest ``need``+ finished runs of one job all failed."""
    runs: dict[int, list[dict]] = {}
    for r in rows:
        runs.setdefault(r["run"], []).append(r)

    streak: list[list[dict]] = []          # failed runs, newest first
    ended_ok = False
    for rid in sorted(runs, reverse=True):
        states = {t["state"] for t in runs[rid]}
        if not states <= _ENDED_STATES:
            # Still queued or running. Newer than any failure, it may be the
            # run that ends the streak, so wait for its verdict: the backup
            # that followed the 2026-10-18 failure took 14 minutes, and a
            # repeat posted meanwhile would have been wrong.
            if not streak:
                return None
            continue
        if states & _FAILED_STATES:
            streak.append(runs[rid])
        elif "COMPLETED" in states:
            ended_ok = True
            break
    if len(streak) < need:
        return None

    starts = [_parse_local(t["start"]) for t in streak[-1]]
    since = min((s for s in starts if s), default=now)
    span_h = max(0.0, (now - since).total_seconds() / 3600.0)
    bad = [t for t in streak[0] if t["state"] in _FAILED_STATES]
    tasks = sorted((t["jobid"].partition("_")[2] for t in bad if "_" in t["jobid"]),
                   key=lambda x: (len(x), x))
    err = _first_error(bad[0])
    log = _expand_log(bad[0]["out"], bad[0]) if bad[0]["out"] else None

    headline = (f"{name} failed its last run" if len(streak) == 1
                else f"{name} has failed {len(streak)} runs in a row")
    if tasks:
        headline += f" (task {', '.join(tasks)})"
    detail = [f"*Failing since:* {since.astimezone(INSTRUMENT_TZ):%a %d %b %H:%M}"
              + ("" if ended_ok else f", or before (sacct is asked for "
                 f"{JOB_LOOKBACK_HOURS:g} h)")]
    if err:
        detail.append(f"*Error:* `{err}`")
    if log:
        detail.append(f"*Log:* {log}")
    detail.append("It still writes its log every run, so the cron heartbeat "
                  "sees nothing wrong.")
    # The signature is the severity alone, and the 24 h cool-off is the
    # pace: once a day, however the failures fall. Severity only rises
    # within a streak, so it changes once, when the failures span a day.
    # Two things that were here posted far more often. A day count flipped
    # at the window's edge, where sacct reports a run overlapping its start
    # on one tick and not the next. The streak's opening run made every new
    # streak news, and nothing clears the store when one ends, so a job
    # that failed 3 runs, worked once and failed 3 more posted every 4 h.
    severity = "critical" if need == 1 or span_h >= 24 else "warning"
    return Alert(
        key=f"job_failing:{name}",
        kind="job_failing",
        instrument=station,
        severity=severity,
        headline=headline,
        detail=detail,
        signature=severity,
        cool_off_hours=24.0,
        at=bad[0]["end"] or None,
        extra={"job": name, "runs_failed": len(streak),
               "hours": round(span_h, 1)},
    )


def _failing_files(rows: list[dict], now: datetime, station: str) -> Alert | None:
    """Alert when QC_FAIL_FILES per-file jobs' latest attempts failed lately."""
    latest: dict[str, dict] = {}
    for r in rows:
        if r["state"] not in _ENDED_STATES:
            continue
        if r["name"] not in latest or r["run"] > latest[r["name"]]["run"]:
            latest[r["name"]] = r
    cutoff = now - timedelta(hours=QC_FAIL_WINDOW_HOURS)
    failing = []
    for name, r in latest.items():
        end = _parse_local(r["end"])
        if r["state"] in _FAILED_STATES and end and end >= cutoff:
            failing.append((end, name, r))
    if len(failing) < QC_FAIL_FILES:
        return None

    failing.sort(key=lambda x: x[0], reverse=True)
    detail = []
    for end, name, r in failing[:5]:
        err = _first_error(r)
        detail.append(f"• `{name}` {end.astimezone(INSTRUMENT_TZ):%a %H:%M}"
                      + (f" -- {err}" if err else ""))
    if len(failing) > 5:
        detail.append(f"…and {len(failing) - 5} more")
    detail.append("Counted once per raw file; one whose retry worked, and "
                  "blanks and washes, are not counted.")
    return Alert(
        key="job_failing:per_file",
        kind="job_failing",
        instrument=station,
        severity="warning",
        headline=(f"{len(failing)} per-file QC jobs failed in the last "
                  f"{QC_FAIL_WINDOW_HOURS:g} h"),
        detail=detail,
        # Constant, so the 24 h cool-off alone sets the pace: by the time it
        # has passed, every file in the last message is out of the 24 h
        # window, so a repeat is only ever about failures since. A date here
        # stepped back when a retry after midnight worked, and posted again.
        signature="3+",
        cool_off_hours=24.0,
        extra={"job": "per_file", "files": len(failing)},
    )


def _cron_log(root: str, cron: str) -> tuple[str | None, datetime | None]:
    """The newest log of one :data:`CRON_LOGS` cron, and when it was written.

    Both None when the cron has no log here: it is not scheduled on this
    host.
    """
    import glob
    import os

    try:
        hits = glob.glob(os.path.join(root, CRON_LOGS[cron][0]))
        if not hits:
            return None, None
        newest = max(hits, key=os.path.getmtime)
        return newest, datetime.fromtimestamp(os.path.getmtime(newest),
                                              tz=timezone.utc)
    except OSError:
        return None, None


def _last_line(path: str) -> str:
    """The last non-empty line of a log, from its last 4 KB."""
    import os

    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 4096))
            lines = fh.read(4096).decode(errors="replace").splitlines()
    except (OSError, ValueError):
        return ""
    return next((ln.strip() for ln in reversed(lines) if ln.strip()), "")[:200]


def _stalled(name: str, rows: list[dict], now: datetime, station: str,
             root: str) -> Alert | None:
    """Alert when no run of a scheduled job has finished for its cron's limit.

    The streak check judges runs that finish, and waits for an unfinished
    run's verdict. A run held in the queue never gives one, and its cron
    logs "already queued" every tick, so the heartbeat sees a fresh log too.
    On 2026-10-07 counter run 24684245 sat in the queue from 11:30 to 13:57
    while its cron wrote "skip: counter already queued/running" nine times,
    and on 2026-10-19 an ion-cloud task hung for 8 h the same way; nothing
    could say so. This asks when a run last finished, completed or failed,
    which catches that, a hung run, a cron that ABORTs before submitting
    every tick, and an sbatch that fails, alike. A cancelled or preempted
    run gives no verdict here either: a job preempted every time never does
    its work.

    A cron whose log has gone stale is the heartbeat's to report, and this
    stays out of it: the cron is not running, which says more, and two
    alerts a day about one cause is one too many.
    """
    cron = SLURM_JOB_CRON.get(name)
    if cron is None:
        return None
    log, written = _cron_log(root, cron)
    if log is None:
        return None                        # not scheduled on this host
    max_h = CRON_LOGS[cron][1]
    if (now - written).total_seconds() / 3600.0 > max_h:
        return None                        # check_cron_heartbeat's case

    runs: dict[int, list[dict]] = {}
    for r in rows:
        runs.setdefault(r["run"], []).append(r)
    last_end, last_how, good_end = None, "", None
    for tasks in runs.values():
        states = {t["state"] for t in tasks}
        if not states <= _ENDED_STATES:
            continue
        ends = [_parse_local(t["end"]) for t in tasks]
        if not all(ends):
            continue
        end = max(ends)
        if states & _FAILED_STATES:
            how = "failed"
        elif "COMPLETED" in states:
            how = "completed"
            if good_end is None or end > good_end:
                good_end = end
        else:
            continue                       # cancelled or preempted: no verdict
        if last_end is None or end > last_end:
            last_end, last_how = end, how
    if last_end is None:
        age_h = JOB_LOOKBACK_HOURS         # at least: sacct shows no more
    else:
        age_h = (now - last_end).total_seconds() / 3600.0
    if age_h <= max_h:
        return None

    def _when(t: datetime | None) -> str:
        return (f"{t.astimezone(INSTRUMENT_TZ):%a %d %b %H:%M}" if t
                else f"none in the last {JOB_LOOKBACK_HOURS:g} h")

    if last_end is None:
        headline = f"{name} has not finished a run in {JOB_LOOKBACK_HOURS:g} h"
    else:
        headline = f"{name} has not finished a run for {int(age_h)} h"
    newest = runs[max(runs)] if runs else []
    states = {t["state"] for t in newest}
    if not newest or states <= _ENDED_STATES:
        doing = "no run queued or running"
    elif "RUNNING" in states:
        start = min((s for s in (_parse_local(t["start"]) for t in newest) if s),
                    default=None)
        doing = f"run {newest[0]['jobid'].partition('_')[0]} running" + (
            f" since {start.astimezone(INSTRUMENT_TZ):%a %H:%M}" if start else "")
    else:
        doing = (f"run {newest[0]['jobid'].partition('_')[0]} "
                 + ("waiting in the queue" if states & {"PENDING", "REQUEUED"}
                    else "/".join(sorted(states)).lower()))
    detail = [f"*Last finished run:* {_when(last_end)}"
              + (f" ({last_how})" if last_how else "")
              + f", alerting past {max_h:g} h"]
    if good_end != last_end:
        detail.append(f"*Last good run:* {_when(good_end)}")
    detail.append(f"*Now:* {doing}")
    last_line = _last_line(log)
    if last_line:
        detail.append(f"*Its cron last wrote:* `{last_line}`")
    detail.append(f"*Cron log:* {log}")
    # PG Farm keeps no backup of its own, so a missed one is critical at once.
    severity = ("critical" if SLURM_JOBS[name] == 1 or age_h >= 24
                else "warning")
    return Alert(
        key=f"job_stalled:{name}",
        kind="job_stalled",
        instrument=station,
        severity=severity,
        headline=headline,
        detail=detail,
        # As for a failing streak: once a day, and once more if it turns
        # critical. Nothing that moves tick to tick belongs here.
        signature=severity,
        cool_off_hours=24.0,
        extra={"job": name, "hours": round(age_h, 1)},
    )


def _unwatched(station: str, why: str) -> Alert:
    """Failing jobs cannot be checked: said once a day, never swallowed."""
    return Alert(
        key="job_failing:unreadable",
        kind="job_failing",
        instrument=station,
        severity="warning",
        headline="Could not check for failing SLURM jobs -- they are not "
                 "being watched",
        detail=[f"*Error:* `{why[:200]}`"],
        signature="unreadable",
        cool_off_hours=24.0,
    )


def check_job_failures(now: datetime | None = None,
                       station: str = DEFAULT_STATION,
                       sacct_text: str | None = None,
                       log_dir: str | None = None) -> list[Alert]:
    """Alert on a STAN SLURM job that keeps FAILING, not just going quiet.

    The other half of :func:`check_cron_heartbeat`, which measures silence.
    A job that fails every run but still logs every run looks alive to it.
    From 2026-09-23 to 2026-10-21 shard 1 of stan-ioncloud failed every hour
    while cron_ioncloud.log was written every hour, so for four weeks nothing
    alerted; it was found by reading the job list. This asks sacct, the
    scheduler's own record of how each job ended.

    Args:
        now: The current time (tests).
        station: Instrument tag for the alert.
        sacct_text: ``sacct -X -n -P`` output in :data:`_SACCT_FIELDS` order;
            None runs sacct.
        log_dir: Where the cron logs are; :data:`CRON_LOG_DIR` by default.

    Returns:
        Alerts for :data:`SLURM_JOBS` streaks, for those that have not
        finished a run in too long, and for per-file failures. Empty on a
        host without SLURM.
    """
    now = now or datetime.now(timezone.utc)
    root = log_dir or CRON_LOG_DIR
    if sacct_text is None:
        try:
            sacct_text = _sacct_text(JOB_LOOKBACK_HOURS)
        except Exception as e:  # noqa: BLE001 -- reported, not swallowed
            why = getattr(e, "stderr", None) or ""
            if isinstance(why, bytes):     # TimeoutExpired keeps bytes
                why = why.decode(errors="replace")
            why = why.strip() or str(e)
            logger.warning("sacct failed; failing jobs are not being watched: %s", why)
            return [_unwatched(station, why)]
        if sacct_text is None:
            # No SLURM is a single-lab install -- unless STAN's cron logs are
            # here, in which case this is Hive and sacct has moved.
            if _cron_logs_present(root):
                return [_unwatched(station, "sacct is not on PATH or at "
                                            f"{SACCT_FALLBACK}")]
            return []
    rows = _parse_sacct(sacct_text)
    if not rows and _cron_logs_present(root):
        # On Hive the counter alone runs every 15 min, so an empty history
        # is sacct's output changing, not every job stopping at once --
        # which would read as three critical stalls about healthy jobs.
        return [_unwatched(station, "sacct listed no jobs this check can "
                                    f"read for the last {JOB_LOOKBACK_HOURS:g} h")]

    alerts = []
    for name, need in sorted(SLURM_JOBS.items()):
        mine = [r for r in rows if r["name"] == name]
        # Not finishing comes first: failed runs and then nothing at all is
        # a job that has stopped, and "has failed 3 runs in a row" would
        # hide that until the failures left sacct's window two days later.
        a = (_stalled(name, mine, now, station, root)
             or _failing_streak(name, need, mine, now, station))
        if a:
            alerts.append(a)
    per_file = [r for r in rows
                if r["name"].startswith("stan-") and r["name"] not in SLURM_JOBS
                and "_" not in r["jobid"]          # an array is not a file
                and not is_blank_or_wash(r["name"])]
    a = _failing_files(per_file, now, station)
    if a:
        alerts.append(a)
    return alerts

# ── entry point ──────────────────────────────────────────────────


def load_documents(evosep_path=None, bruker_path=None,
                   use_pg: bool | None = None) -> tuple[dict | None, dict | None]:
    """Fetch both analysis documents: explicit paths, then PG, then bundled.

    Same precedence the Maintenance endpoints use, for the same reason — the
    alerter and the dashboard must be looking at one document, or they will
    disagree about whether anything is wrong.
    """
    import json
    from pathlib import Path

    def _file(p):
        try:
            return json.loads(Path(p).read_text())
        except Exception:
            logger.warning("could not read %s", p, exc_info=True)
            return None

    evosep = _file(evosep_path) if evosep_path else None
    bruker = _file(bruker_path) if bruker_path else None

    if evosep is None or bruker is None:
        import os

        if use_pg is None:
            use_pg = os.environ.get("STAN_DB_BACKEND", "").lower() == "pg"
        if use_pg:
            # These readers cache on the row's xmin (and, with
            # $STAN_PG_DOC_CACHE_DIR, across cron processes), re-checked
            # against PG on every call. Still exactly the document PG
            # serves -- a cache of it, never a copy that can drift -- which
            # is what a watchdog needs; it just stops re-downloading 864 KB
            # every tick to find it unchanged.
            try:
                from stan.db_pg import (
                    get_bruker_maintenance_pg,
                    get_evosep_column_health_pg,
                )

                if evosep is None:
                    evosep = get_evosep_column_health_pg()
                if bruker is None:
                    bruker = get_bruker_maintenance_pg()
            except Exception:
                logger.warning("could not read telemetry from PG", exc_info=True)

    if evosep is None or bruker is None:
        try:
            from stan.config import resolve_config_path

            if evosep is None:
                evosep = _file(resolve_config_path("evosep_column_health.json"))
            if bruker is None:
                bruker = _file(resolve_config_path("bruker_maintenance.json"))
        except Exception:
            logger.debug("no bundled telemetry documents", exc_info=True)

    return evosep, bruker


#: The fleet name for the timsTOF/Evosep station, as `runs` and
#: `maintenance_events` spell it. Not the Evosep log's `TIMS-10878`, nor
#: Compass's `HPZ6` -- a maintenance event has to land on the instrument the
#: rest of STAN knows about or it will not show on that instrument's calendar.
EVENT_INSTRUMENT = "timsTOF HT"

#: Machine-written events are attributed so nobody later reads one as Brett's
#: own log entry and trusts it as a first-hand observation.
EVENT_OPERATOR = "STAN auto"


def log_clog_events(sent_alerts: list[Alert],
                    instrument: str = EVENT_INSTRUMENT) -> list[str]:
    """Put a fresh clog on the maintenance calendar. Never raises.

    Only `clog`, and only the first time an episode is announced.

    Why not over-pressure too: the clog alert's key is
    ``clog:<station>:<method>`` with no timestamp in it, so it is stable for
    the whole episode by construction -- one episode can only ever produce one
    event. The over-pressure alert is keyed on the ceiling-hit run, and which
    run holds the highest peak can change as the episode grows, so logging it
    would risk a second calendar row for the same incident. Over-pressure
    still reaches Slack immediately either way.

    Why only alerts that were actually sent: state is recorded only on a
    successful send, so an unsent alert re-fires as `new` next tick. Logging
    on anything looser would write a duplicate event every time Slack was
    briefly unreachable.

    Writes through `stan.db.log_event`, which routes to PG Farm when
    STAN_DB_BACKEND=pg -- deliberately NOT a new concurrent writer against the
    Quobyte SQLite file STAN has just finished moving off.
    """
    logged: list[str] = []
    for a in sent_alerts:
        if a.kind != "clog" or a.extra.get("why") != "new":
            continue
        try:
            from stan.db import log_event

            notes = (
                f"Automatically detected by STAN: {a.headline}. "
                f"{a.extra.get('n_critical')} consecutive runs at critical "
                f"back-pressure on {a.extra.get('method')} "
                f"(threshold {CLOG_CRITICAL_RUNS}); worst "
                f"{a.extra.get('worst_pct_over_baseline')}% over the running "
                f"baseline. Source: Evosep One procedure logs. "
                f"Not a human observation -- verify before acting on it."
            )
            logged.append(log_event(
                instrument=instrument,
                event_type="column_clog",
                notes=notes,
                operator=EVENT_OPERATOR,
                created_by=EVENT_OPERATOR,
            ))
        except Exception:  # noqa: BLE001 — the calendar is a bonus, not the job
            logger.warning("could not log a column_clog maintenance event",
                           exc_info=True)
    return logged


def run_watch(dry_run: bool = False, seed: bool = False,
              evosep_path=None, bruker_path=None,
              lookback_hours: float = LOOKBACK_HOURS,
              bruker_lookback_hours: float = BRUKER_LOOKBACK_HOURS,
              station: str = DEFAULT_STATION,
              now: datetime | None = None,
              store=None) -> dict:
    """Evaluate both documents and Slack anything new. Never raises.

    ``seed`` records everything currently visible as already-sent without
    sending it — run once at install so a fresh state file does not replay a
    fortnight of resolved history into the channel.
    """
    from stan.notify import AlertStore, notify

    now = now or datetime.now(timezone.utc)
    evosep, bruker = load_documents(evosep_path, bruker_path)

    alerts: list[Alert] = []
    if evosep:
        alerts += check_evosep_episodes(evosep, now=now, station=station,
                                        lookback_hours=lookback_hours)
        alerts += check_column_wear(evosep, station=station)

    # When the fast path was handed a short `--since` extract, column wear is
    # invisible to it (the install predates the window). The full-history
    # document published for the dashboard does see it, so consult that too --
    # it is one PG row, and duplicate keys collapse in `notify`.
    if evosep_path and not ((evosep or {}).get("column") or {}).get("known"):
        full, _ = load_documents(None, None)
        if full and full is not evosep:
            alerts += check_column_wear(full, station=station)

    if bruker:
        alerts += check_bruker(bruker, now=now, station=station,
                               lookback_hours=bruker_lookback_hours)

    # Evaluated on whatever loaded, including nothing: a feed that has gone
    # silent is exactly the case where the document is stale or missing.
    alerts += check_feed_freshness(evosep, bruker, now=now, station=station)

    # The other half: data can be arriving while the job that publishes it
    # has stopped. Reads PG, not the documents above, because a caller
    # holding a freshly generated one is exactly the case this catches.
    alerts += check_publish_freshness(now=now, station=station)

    # And the generic form: a scheduled job that has stopped saying
    # anything at all, which no per-feed check can see.
    alerts += check_cron_heartbeat(now=now, station=station)

    # And its opposite: a job that keeps running, and logging, and failing.
    # Contained: a bug in it must not cost this tick every other alert.
    try:
        alerts += check_job_failures(now=now, station=station)
    except Exception as e:  # noqa: BLE001 -- reported, not swallowed
        logger.warning("check_job_failures failed", exc_info=True)
        alerts.append(_unwatched(station, f"{type(e).__name__}: {e}"))

    store = store if store is not None else AlertStore()

    if seed:
        for a in alerts:
            store.record(a, now=now)
        store.flush()
        return {"seeded": len(alerts), "sent": False, "n_alerts": len(alerts),
                "evosep_loaded": bool(evosep), "bruker_loaded": bool(bruker)}

    # Acknowledgements are polled BEFORE deciding what to send: a reaction
    # left five minutes ago should suppress this tick's repetition, not the
    # next one's.
    acked: list[dict] = []
    if not dry_run:
        from stan.notify import poll_acks

        try:
            acked = poll_acks(store, now=now)
        except Exception:  # noqa: BLE001 — a notifier never breaks its caller
            logger.warning("acknowledgement poll failed", exc_info=True)

    result = notify(alerts, store=store, dry_run=dry_run, now=now)
    result["acknowledged"] = acked

    # Close off incidents that have stopped. Only when the Evosep document
    # actually loaded: if the extract failed every key would vanish, and
    # closing on that would announce that everything resolved at the exact
    # moment we stopped being able to see anything.
    if evosep and not dry_run:
        from stan.notify import close_finished_threads

        live = {a.thread_key or a.key for a in alerts}
        # A standing condition that is live but merely suppressed this tick
        # must not be closed either: it is quiet, not over. Any method still
        # producing flagged runs inside the lookback counts as live.
        live |= {f"clog:{station}:{f.get('method')}"
                 for f in (evosep.get("flags") or [])}
        try:
            result["closed_threads"] = close_finished_threads(store, live, now=now)
        except Exception:  # noqa: BLE001
            logger.warning("closing finished threads failed", exc_info=True)
            result["closed_threads"] = []

    # A clog that only ever existed as a Slack message scrolls away. Putting
    # it on the maintenance calendar gives the column-lifetime maths a real
    # fault to reason about. Gated on `sent` so it inherits the Slack dedup
    # exactly -- see log_clog_events.
    if result.get("sent"):
        result["maintenance_events"] = log_clog_events(
            [a for a in alerts if a.extra.get("why") == "new"])

    result["evosep_loaded"] = bool(evosep)
    result["bruker_loaded"] = bool(bruker)
    result["evosep_generated_at"] = (evosep or {}).get("generated_at")
    result["bruker_backup_date"] = (bruker or {}).get("backup_date")
    return result
