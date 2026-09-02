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
from datetime import datetime, timedelta, timezone

from stan.notify import Alert

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
#: is a lag, not a preference. The Bruker document is rebuilt nightly at 20:00
#: from a backup the instrument wrote at 18:00, so a failure at 14:38 is not
#: visible to anything until ~29 h later. Under the 24 h window it would be
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
    lookback window is a day wide, which absorbs the offset.
    """
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    s = str(ts).strip().replace("Z", "+00:00")
    for parse in (
        lambda v: datetime.fromisoformat(v),
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M"),
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            dt = parse(s)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


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
        last = episode[-1]
        last_at = _parse(last.get("start"))
        at_str = last_at.strftime("%Y-%m-%d %H:%M") if last_at else None

        pressure = [f for f in episode if "high_pressure" in (f.get("kinds") or [])]
        criticals = [f for f in pressure if f.get("severity") == "critical"]
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
                    instrument=label,
                    severity="warning",
                    headline="Evotip not seated — injection aborted",
                    detail=[f"*Run:* `{f.get('run')}`   *Method:* {f.get('method')}",
                            f"*Low-pressure-side peak:* "
                            f"{f.get('tip_pressure_max_bar')} bar"],
                    at=f_at.strftime("%Y-%m-%d %H:%M") if f_at else None,
                    extra={"run": f.get("run")},
                ))
            elif "aborted" in kinds:
                alerts.append(Alert(
                    key=f"aborted:{station}:{_bucket(f_at)}",
                    kind="aborted",
                    instrument=label,
                    severity="warning",
                    headline="run aborted part-way",
                    detail=[f"*Run:* `{f.get('run')}`   *Method:* {f.get('method')}",
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
    # 2. The BASELINE ORIGIN must be trustworthy, which is a separate axis and
    #    the one that bites quietly. Once Brett's column change was logged as
    #    an anchored event, `confidence` became "logged" on BOTH documents --
    #    but the 30-minute tick only carries a 3-day window, so its
    #    `baseline_at_install_bar` is the baseline at the start of that
    #    window, not at the install. Real numbers from 2026-09-02:
    #    days_since 32.99 (correct) against observed_days 1.94. Drift measured
    #    from that origin describes three days and calls it the column's life,
    #    reads far too low, and the 25% threshold would then never fire.
    #    That is a SILENT failure -- the worst kind here, and exactly the
    #    shape of the problem this whole change exists to end. So wear is only
    #    judged on the daily full document, and `log_covers_install` is how we
    #    tell which one we were handed.
    #
    # Fails CLOSED on both. A wear alert is a nudge to order a column; a nudge
    # built on a number nobody can stand behind is worse than no nudge at all.
    trustworthy_date = (str(col.get("confidence") or "") in ("logged", "inferred")
                        and not col.get("installed_is_lower_bound"))
    trustworthy_baseline = (bool(col.get("log_covers_install"))
                            and not col.get("counts_are_lower_bounds"))
    if not (trustworthy_date and trustworthy_baseline):
        logger.info(
            "column wear not judgeable from this document (confidence=%s, "
            "installed_is_lower_bound=%s, log_covers_install=%s, "
            "counts_are_lower_bounds=%s); skipping",
            col.get("confidence"), col.get("installed_is_lower_bound"),
            col.get("log_covers_install"), col.get("counts_are_lower_bounds"))
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
        alerts.append(Alert(
            key=f"{kind}:{station}:{_bucket(at)}",
            kind=kind,
            instrument=label,
            severity=severity,
            headline=cat,
            detail=[
                f"*Well:* {f.get('well')}   *File:* `{f.get('fname')}`",
                f"> {msg[:220]}" if msg else "",
            ],
            at=at.strftime("%Y-%m-%d %H:%M"),
            extra={"fname": f.get("fname"), "category": cat},
        ))
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

    store = store if store is not None else AlertStore()

    if seed:
        for a in alerts:
            store.record(a, now=now)
        store.flush()
        return {"seeded": len(alerts), "sent": False, "n_alerts": len(alerts),
                "evosep_loaded": bool(evosep), "bruker_loaded": bool(bruker)}

    result = notify(alerts, store=store, dry_run=dry_run, now=now)

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
