"""Watch high-throughput plates and email when one goes wrong.

An HT plate runs unattended for many hours. The failures that matter are
the ones nobody is standing next to: a queue that stops mid-plate (an
overpressure trip took plate S5 down at 39 of 96 wells on 2026-08-28), a
run of consecutive dead wells that says something systemic broke rather
than one sample being poor, and a source getting dirty across the queue.

Each condition is written to be quiet by default. An alert that fires on
every ordinary batch gets filtered to a folder and then the real one is
missed too, so the thresholds are deliberately conservative and every alert
carries the numbers that triggered it.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: A plate is "stalled" only after this long with no new injection. Long
#: enough to survive a lunch break, a long gradient, or a queue paused to
#: swap solvent -- none of which are faults.
STALL_HOURS = 3.0

#: Consecutive flagged injections that mean "something systemic", not "one
#: bad sample". Three in a row is rare by chance and is the signature of a
#: spray failure or an empty plate region.
CONSECUTIVE_FAILURES = 3

#: Percent decline in HeLa precursor identifications across a queue that is
#: worth mentioning. Real submission 0793 showed -7%, which is normal wear
#: over a plate; -25% is a source that needs cleaning before the next batch.
STANDARDS_DECLINE_PCT = -25.0

#: Where the "already told you" state lives, so a stalled plate produces one
#: email rather than one per cron tick.
STATE_FILENAME = "ht_watch_state.json"


def _state_path(state_dir: Path | None = None) -> Path:
    base = state_dir or (Path.home() / ".stan")
    base.mkdir(parents=True, exist_ok=True)
    return base / STATE_FILENAME


def _load_state(state_dir: Path | None = None) -> dict:
    try:
        return json.loads(_state_path(state_dir).read_text())
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("unreadable ht_watch state; starting clean", exc_info=True)
        return {}


def _save_state(state: dict, state_dir: Path | None = None) -> None:
    try:
        _state_path(state_dir).write_text(json.dumps(state, indent=1))
    except Exception:
        # Failing to remember is not a reason to fail to alert; the cost is
        # a duplicate email next tick.
        logger.warning("could not persist ht_watch state", exc_info=True)


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        s = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def check_plate(analysis: dict, now: datetime | None = None) -> list[dict]:
    """Conditions worth emailing about for one submission's analysis.

    Takes the same structure `/api/ht/submission` returns, so the watcher
    and the dashboard can never disagree about what counts as a problem.
    """
    now = now or datetime.now(timezone.utc)
    alerts: list[dict] = []
    query = analysis.get("query") or "?"

    # 1. A plate that stopped part-way and has gone quiet.
    latest = None
    for p in analysis.get("queue", {}).get("points", []) or []:
        ts = _parse_ts(p.get("run_date"))
        if ts and (latest is None or ts > latest):
            latest = ts
    for plate in analysis.get("plate", {}).get("plates", []) or []:
        if plate.get("is_complete"):
            continue
        if plate.get("n_wells", 0) < 4:
            # A plate that has barely started is not stalled, it is starting.
            continue
        if latest is None:
            continue
        idle_h = (now - latest).total_seconds() / 3600.0
        if idle_h >= STALL_HOURS:
            alerts.append({
                "kind": "stalled_plate",
                "submission": query,
                "plate": plate.get("plate"),
                "acquired": plate.get("n_wells"),
                "expected": plate.get("n_expected"),
                "remaining": plate.get("n_missing"),
                "idle_hours": round(idle_h, 1),
                "missing_wells": (plate.get("missing_wells") or [])[:24],
                "summary": (
                    f"Plate {plate.get('plate')} of submission {query} stopped at "
                    f"{plate.get('n_wells')}/{plate.get('n_expected')} wells and has "
                    f"been idle {idle_h:.1f} h — {plate.get('n_missing')} wells "
                    f"still to run."),
            })

    # 2. Consecutive flagged injections — systemic, not one poor sample.
    pts = [p for p in analysis.get("queue", {}).get("points", []) or []
           if p.get("kind") != "qc"]
    run: list[dict] = []
    worst: list[dict] = []
    for p in pts:
        if p.get("is_outlier"):
            run.append(p)
            if len(run) > len(worst):
                worst = list(run)
        else:
            run = []
    if len(worst) >= CONSECUTIVE_FAILURES:
        alerts.append({
            "kind": "consecutive_failures",
            "submission": query,
            "count": len(worst),
            "runs": [p.get("run_name") for p in worst],
            "summary": (
                f"{len(worst)} consecutive injections flagged in submission "
                f"{query} — that pattern is usually the instrument or the "
                f"plate, not the samples."),
        })

    # 3. The standards losing ground across the queue.
    trend = (analysis.get("queue", {}) or {}).get("standards_trend_precursors")
    if trend and trend.get("pct_change_over_queue") is not None:
        pct = trend["pct_change_over_queue"]
        if pct <= STANDARDS_DECLINE_PCT:
            alerts.append({
                "kind": "standards_declining",
                "submission": query,
                "pct_change": pct,
                "n_standards": trend.get("n"),
                "summary": (
                    f"HeLa standards in submission {query} lost {abs(pct):.0f}% "
                    f"of their precursor identifications across the queue "
                    f"(n={trend.get('n')}) — the source is likely dirtying."),
            })

    return alerts


def alert_key(alert: dict) -> str:
    """Identity for de-duplication.

    Deliberately excludes the changing numbers: a stalled plate that has been
    idle four hours and then five is the same news, and re-sending it teaches
    the reader to ignore the alert.
    """
    return f"{alert.get('kind')}:{alert.get('submission')}:{alert.get('plate') or ''}"


def render_email(alerts: list[dict]) -> tuple[str, str]:
    """Subject and HTML body. Plain and skimmable — this arrives on a phone."""
    kinds = {a["kind"] for a in alerts}
    if "stalled_plate" in kinds:
        subject = f"STAN HT: plate stopped — {alerts[0].get('submission')}"
    elif "consecutive_failures" in kinds:
        subject = f"STAN HT: consecutive failures — {alerts[0].get('submission')}"
    else:
        subject = f"STAN HT: instrument trend — {alerts[0].get('submission')}"

    items = "".join(
        f"<li style='margin-bottom:10px'>{a['summary']}</li>" for a in alerts)
    html = f"""
    <div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
                max-width:640px;color:#111">
      <h2 style="margin-bottom:4px">STAN — high-throughput alert</h2>
      <p style="color:#555;margin-top:0">timsTOF HT</p>
      <ul style="padding-left:18px">{items}</ul>
      <p style="color:#777;font-size:12px">
        Sent once per condition per submission. Open the HT tab in STAN for
        the plate map, the queue trend and the wells still to run.
      </p>
    </div>"""
    return subject, html
