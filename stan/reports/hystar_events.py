"""Hive's half of the HyStar watcher: relay, remember, and watch the watcher.

WHY THIS EXISTS
---------------
At 02:04 on Saturday 2026-10-24 HyStar stopped the timsTOF queue over a
missing Evotip in S1-A5. The only alert was the hourly Evosep path, at 03:00,
naming an Evosep log folder nobody could match to a sample. The watcher on
TIMS-10878 (`instrument/scripts/watch_hystar_errors.ps1`) now posts a stop to
Slack within a minute, and always drops an event file and a heartbeat on the
share Hive sees as ``/quobyte/proteomics-grp/STAN/hystar_events/TIMS-10878/``.
This module is what Hive does with them, once a minute, from
`scripts/cron_hystar_events.sh`:

  * **Relay.** An event the PC could not post (``posted_by_pc: false``) goes
    out from here: the PC's own payload, verbatim, plus one context line
    saying Hive relayed it. Only after Slack accepts it is it recorded and
    moved to ``processed/``, so a failed relay is retried next tick.
  * **Remember.** Every stop marks the keys the hourly paths would use for
    the same failure (`suppression_keys`) in alert_state, so the Evosep and
    Bruker alerts stay quiet about an injection the watcher already reported.
  * **Watch the watcher.** A dead watcher looks exactly like a healthy queue.
    No heartbeat for 10 min pages once (24 h cool-off), unless a
    ``pause_until.json`` says the downtime is planned; recovery says so once.
  * **Explain the QC line.** `find_stop_for_run` lets the QC summary say
    "injection aborted" instead of a red IPS 0 for the run a stop emptied.

The PC writes the files; Hive only moves them (``os.replace`` within one
filesystem). That keeps Quobyte's one-writer-per-file rule.

EVERY TIME HERE IS INSTRUMENT-LOCAL
-----------------------------------
Event and heartbeat times are naive local ISO, like the other documents,
read with `parse_local`. Keys are built the way the hourly path builds them
(`instrument_watch._parse`, naive read as UTC) so that they are equal
strings; a key built in real UTC would suppress the wrong hour.

The one exception: whether the watcher is alive is judged by when
``heartbeat.json`` *landed* (its mtime, on the Hive server's clock), not by
the time written inside it. That is the clock the cron's bash fast path
reads too, and the PC's clock cannot then fake an outage or a live watcher.
"""

from __future__ import annotations

import copy
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stan.instrument_time import INSTRUMENT_TZ, parse_local, parse_raw
from stan.notify import Alert, AlertStore, notify, post_slack
from stan.reports.injection_identity import sample_from_run_name
from stan.reports.instrument_watch import BUCKET_MINUTES, DEFAULT_STATION, _bucket, _parse

logger = logging.getLogger(__name__)

#: Where the watcher's share lands on Hive (``Y:\\STAN\\hystar_events``).
DEFAULT_EVENT_ROOT = Path("/quobyte/proteomics-grp/STAN/hystar_events")

#: The one instrument PC running the watcher, and its folder under the root.
HOST = "TIMS-10878"

HEARTBEAT = "heartbeat.json"
PAUSE = "pause_until.json"
#: Written when the watcher-down page has gone out; removed on recovery.
DOWN_MARKER = ".down_alerted"
_NOT_EVENTS = frozenset({HEARTBEAT, PAUSE})

#: Which hourly-path kinds a stop of each watcher category stands for.
#: ``aborted`` is the Evosep path's word for a run cut short, which every
#: stop on the Evosep side also is. A HyStar crash has no hourly twin.
SUPPRESS_KINDS: dict[str, tuple[str, ...]] = {
    "evotip": ("evotip", "aborted"),
    "overpressure": ("overpressure", "aborted"),
    "connection_lost": ("connection_lost",),
    "ms_error": ("ms_error",),
    "hystar_crash": (),
    "evosep_hw": ("aborted",),
    "other": ("aborted",),
}

#: How far back from the stop to mark keys. The hourly keys use the START of
#: the run (Evosep procedure start, Compass task start) and an MS error can
#: arrive 15-40 min in, so one run length, the longest method in use.
SUPPRESS_WINDOW_MIN = 60

#: No heartbeat for longer than this means the watcher is down.
WATCHER_DOWN_AFTER = timedelta(minutes=10)
WATCHER_COOLOFF_HOURS = 24.0

#: QC stop-match windows (spec §4.6): the .d lands ~1 s after the stop.
QC_MATCH_WITH_VIAL_S = 180
QC_MATCH_WITHOUT_VIAL_S = 90

RELAY_NOTE = "Relayed by Hive: the watcher could not reach Slack"
_RELAY_CONTEXT = {"type": "context",
                  "elements": [{"type": "mrkdwn", "text": RELAY_NOTE}]}

DOWN_HEADLINE = "No heartbeat from the HyStar watcher"
DOWN_DETAIL = (f"No heartbeat from the HyStar watcher on {HOST}: PC off or "
               "rebooted, nobody logged in, watcher stopped, or the PC can't "
               "reach the share. The queue is not being watched.")
BACK_HEADLINE = "HyStar watcher is reporting again"


def _down_key(station: str) -> str:
    return f"hystar_watcher_down:{station}"


def _own_key(event: dict, station: str) -> str:
    return f"hystar:{station}:{event['id']}"


def _utc(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _stamp(dt: datetime) -> str:
    return _utc(dt).strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path):
    # utf-8-sig: PowerShell 5.1's `-Encoding UTF8` writes a BOM.
    return json.loads(path.read_text(encoding="utf-8-sig"))


# ── event I/O ────────────────────────────────────────────────────


def _valid(ev) -> str | None:
    """Why ``ev`` is not an event, or None when it is one."""
    if not isinstance(ev, dict):
        return f"not a JSON object ({type(ev).__name__})"
    for field in ("id", "type"):
        if not isinstance(ev.get(field), str) or not ev[field].strip():
            return f"no {field!r}"
    return None


def _scan_events(host_dir: Path) -> tuple[list[dict], list[tuple[Path, str]]]:
    """(events sorted by ``at``, [(bad file, why)]). Never raises.

    A file that cannot be *read* right now (share hiccup) is neither: it is
    left where it is for the next tick. Only content that will never parse
    is bad.
    """
    try:
        paths = sorted(p for p in Path(host_dir).glob("*.json")
                       if p.name not in _NOT_EVENTS and p.is_file())
    except OSError as e:
        logger.warning("could not list %s (%s)", host_dir, type(e).__name__)
        return [], []
    good: list[dict] = []
    bad: list[tuple[Path, str]] = []
    for p in paths:
        try:
            ev = _read_json(p)
        except ValueError as e:              # bad JSON or bad UTF-8
            bad.append((p, f"unparseable ({type(e).__name__})"))
            continue
        except OSError as e:
            logger.warning("could not read %s (%s); trying next tick", p.name,
                           type(e).__name__)
            continue
        why = _valid(ev)
        if why:
            bad.append((p, why))
            continue
        ev["_file"] = p.name
        good.append(ev)
    good.sort(key=lambda e: (str(e.get("at") or ""), e["id"]))
    return good, bad


def _free_name(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for n in range(1, 1000):
        alt = dest.with_name(f"{dest.stem}.{n}{dest.suffix}")
        if not alt.exists():
            return alt
    return dest


def _reject(host_dir: Path, path: Path, why: str) -> None:
    logger.warning("rejected HyStar event file %s: %s; moved to rejected/",
                   path.name, why)
    try:
        dest = Path(host_dir) / "rejected"
        dest.mkdir(exist_ok=True)
        os.replace(path, _free_name(dest / path.name))
    except OSError as e:
        logger.warning("could not move %s to rejected/ (%s)", path.name,
                       type(e).__name__)


def load_new_events(host_dir: Path) -> list[dict]:
    """The events waiting in ``host_dir``, oldest first. Never raises.

    Top-level ``*.json`` except the heartbeat and the pause file. A file that
    is not a valid event is logged at WARNING and moved to ``rejected/``.
    Each event carries ``_file``, the name it was read from.
    """
    good, bad = _scan_events(host_dir)
    for path, why in bad:
        _reject(host_dir, path, why)
    return good


def mark_processed(host_dir: Path, event: dict) -> None:
    """Move the event's file to ``processed/YYYY-MM/`` (the event's month).

    Raises OSError when the move fails; the tick catches it, and the event's
    own key stops the next tick from posting it twice.
    """
    name = event.get("_file") or f"{event['id']}.json"
    at = parse_raw(event.get("at"))
    month = (at or datetime.now(INSTRUMENT_TZ)).strftime("%Y-%m")
    dest = Path(host_dir) / "processed" / month
    dest.mkdir(parents=True, exist_ok=True)
    os.replace(Path(host_dir) / name, dest / name)


# ── keys ─────────────────────────────────────────────────────────


def suppression_keys(event: dict, station: str = DEFAULT_STATION,
                     window_min: int = SUPPRESS_WINDOW_MIN) -> list[str]:
    """The hourly paths' keys for the failure this stop reports.

    ``{kind}:{station}:{bucket}`` for each 10-min bucket from ``at -
    window_min`` to ``at``, built exactly as `instrument_watch` builds them.
    Only a ``stop`` suppresses anything: a reminder, restart or test is not
    a failure the hourly paths would also report.
    """
    if (event or {}).get("type") != "stop":
        return []
    kinds = SUPPRESS_KINDS.get(event.get("category"), SUPPRESS_KINDS["other"])
    at = _parse(event.get("at"))
    if at is None or not kinds:
        return []
    buckets: list[str] = []
    t = at - timedelta(minutes=window_min)
    while t <= at:
        b = _bucket(t)
        if b not in buckets:
            buckets.append(b)
        t += timedelta(minutes=BUCKET_MINUTES)
    return [f"{kind}:{station}:{b}" for kind in kinds for b in buckets]


# ── relay ────────────────────────────────────────────────────────


def _relay_payload(event: dict) -> dict:
    """The PC's payload (text and blocks only) plus the relay note.

    Without blocks, Slack would show the lone context block in place of the
    text, so the text is carried in a section first.
    """
    src = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    text = src.get("text")
    if not isinstance(text, str) or not text.strip():
        title = event.get("title") or event.get("type")
        text = f":red_circle: {event.get('host') or HOST} — {title}"
        if event.get("message"):
            text += f": {event['message']}"
    blocks = src.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
    return {"text": text, "blocks": copy.deepcopy(blocks) + [copy.deepcopy(_RELAY_CONTEXT)]}


def _record_keys(event: dict, store: AlertStore, now: datetime,
                 station: str) -> None:
    title = str(event.get("title") or event.get("type"))
    for key in suppression_keys(event, station) + [_own_key(event, station)]:
        store.record(Alert(key=key, kind=key.split(":", 1)[0], instrument=HOST,
                           headline=title), now=now)
    store.flush()


def _relay(event: dict, store: AlertStore, now: datetime,
           station: str = DEFAULT_STATION) -> str:
    """``"relayed"``, ``"recorded"``, ``"already"`` or ``"failed"``."""
    try:
        if store.last(_own_key(event, station)) is not None:
            return "already"
    except Exception:  # noqa: BLE001 — unknown is "not yet"; a repeat beats silence
        logger.debug("own-key lookup failed for %s", event.get("id"), exc_info=True)
    relayed = False
    if event.get("posted_by_pc") is not True:
        if not post_slack(_relay_payload(event)):
            logger.warning("could not relay HyStar event %s; retrying next tick",
                           event.get("id"))
            return "failed"
        relayed = True
    _record_keys(event, store, now, station)
    return "relayed" if relayed else "recorded"


def relay_event(event: dict, store: AlertStore, now: datetime) -> bool:
    """Post the event if the PC could not, and record its keys.

    ``posted_by_pc`` true: record only. Anything else: post ``payload``
    verbatim plus the relay note, and record only once Slack accepted it.
    Returns whether the event is done with (safe to move to processed/).
    """
    return _relay(event, store, now) != "failed"


# ── heartbeat ────────────────────────────────────────────────────


def _mtime(path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _heartbeat_times(path: Path) -> tuple[datetime | None, datetime | None]:
    """``(landed, reported)`` for the heartbeat file.

    ``landed`` decides freshness: the file's mtime, on the Hive server's
    clock -- the same clock the cron's bash fast path reads, so the two never
    disagree, and a PC clock running slow or fast can fake neither an outage
    nor a live watcher. (Review round 1: a PC 15 min slow made a live watcher
    page every minute.) Only if the mtime cannot be read does it fall back to
    the PC's ``utc``, then ``at``.

    ``reported`` is what the PC said (``at``, else ``utc``), for display only.
    """
    try:
        doc = _read_json(path)
    except (OSError, ValueError):
        doc = None
    doc = doc if isinstance(doc, dict) else {}
    reported = parse_local(doc.get("at")) or _parse(doc.get("utc"))
    landed = _mtime(path) or _parse(doc.get("utc")) or parse_local(doc.get("at"))
    return landed, reported


def _paused_until(host_dir: Path, now: datetime) -> datetime | None:
    path = Path(host_dir) / PAUSE
    if not path.exists():
        return None
    try:
        until = parse_local(_read_json(path).get("until"))
    except Exception as e:  # noqa: BLE001
        logger.warning("could not read %s (%s); not paused", path,
                       type(e).__name__)
        return None
    return until if until is not None and _utc(until) > _utc(now) else None


def _marker_doc(marker: Path) -> dict:
    try:
        doc = json.loads(marker.read_text())
        return doc if isinstance(doc, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _read_marker(marker: Path) -> str:
    sent = _marker_doc(marker).get("down_last_sent")
    if sent:
        return str(sent)
    stamp = _mtime(marker)
    return _stamp(stamp) if stamp else "unknown"


#: Tolerated gap between the PC's clock and the server's before the down page
#: mentions it. Display only.
_SKEW_WORTH_SAYING = timedelta(minutes=5)


def _watcher_state(host_dir: Path, now: datetime) -> dict:
    hb = Path(host_dir) / HEARTBEAT
    state: dict = {"installed": hb.exists(), "last_heartbeat": None,
                   "reported": None, "down": False, "paused_until": None,
                   "down_alerted": (Path(host_dir) / DOWN_MARKER).exists()}
    if not state["installed"]:
        return state
    landed, reported = _heartbeat_times(hb)
    state["last_heartbeat"] = landed
    state["reported"] = reported
    state["down"] = landed is not None and _utc(now) - _utc(landed) > WATCHER_DOWN_AFTER
    state["paused_until"] = _paused_until(host_dir, now)
    return state


def check_watcher_heartbeat(host_dir: Path, now: datetime,
                            station: str = DEFAULT_STATION) -> list[Alert]:
    """A page when the watcher has gone quiet; a note when it is back.

    Freshness is the heartbeat file's mtime on the Hive server's clock (see
    `_heartbeat_times`). No heartbeat file at all means the watcher was never
    installed, which is not an outage. A ``pause_until.json`` in the future
    silences the page.

    The page's signature names the episode and stays fixed for it: the
    ``since`` recorded in ``.down_alerted`` once the page has gone out, else
    the last heartbeat's arrival. A repeat inside the 24 h cool-off is
    therefore never "escalated", while a second outage the same day (marker
    cleared by the recovery note) is a new message.
    """
    st = _watcher_state(host_dir, now)
    if not st["installed"] or st["last_heartbeat"] is None:
        return []
    last = st["last_heartbeat"]
    last_local = _utc(last).astimezone(INSTRUMENT_TZ)
    marker = Path(host_dir) / DOWN_MARKER
    if st["down"]:
        if st["paused_until"] is not None:
            return []
        since = (str(_marker_doc(marker).get("since") or "") if st["down_alerted"]
                 else "") or f"{last_local:%Y%m%dT%H%M%S}"
        mins = int((_utc(now) - _utc(last)).total_seconds() // 60)
        ago = f"{mins} min" if mins < 120 else f"{mins // 60} h {mins % 60:02d} min"
        seen = f"*Last heartbeat:* {last_local:%Y-%m-%d %H:%M} ({ago} ago)"
        reported = st["reported"]
        if reported is not None and abs(_utc(reported) - _utc(last)) > _SKEW_WORTH_SAYING:
            seen += (f"; the PC's clock said "
                     f"{_utc(reported).astimezone(INSTRUMENT_TZ):%H:%M}")
        return [Alert(
            key=_down_key(station), kind="watcher_down", instrument=HOST,
            severity="critical", mention=True,
            cool_off_hours=WATCHER_COOLOFF_HOURS,
            signature=f"since:{since}",
            headline=DOWN_HEADLINE,
            detail=[DOWN_DETAIL,
                    f"{seen}. Planned downtime: `watch_hystar_errors.bat -Pause 8h` "
                    "silences this."],
            extra={"since": since,
                   "last_heartbeat": last_local.isoformat(timespec="seconds")},
        )]
    if st["down_alerted"]:
        sent = _read_marker(marker)
        return [Alert(
            key=f"hystar_watcher_back:{station}:{sent}", kind="watcher_back",
            instrument=HOST, severity="info",
            headline=BACK_HEADLINE,
            detail=[f"Heartbeat at {last_local:%Y-%m-%d %H:%M}. "
                    "The queue is being watched again."],
        )]
    return []


def _update_marker(host_dir: Path, alerts: list[Alert], res: dict,
                   now: datetime) -> None:
    """Write the marker once the page went out; remove it once 'back' did."""
    marker = Path(host_dir) / DOWN_MARKER
    went = {f["key"] for f in res.get("fresh") or []} if res.get("sent") else set()
    said = went | set(res.get("suppressed") or [])   # suppressed: sent earlier
    for a in alerts:
        if a.key not in said:
            continue
        try:
            if a.kind == "watcher_down" and not marker.exists():
                marker.write_text(json.dumps({"down_last_sent": _stamp(now),
                                              **a.extra}))
            elif a.kind == "watcher_back":
                marker.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("could not update %s (%s)", marker, type(e).__name__)


# ── QC lookup ────────────────────────────────────────────────────


def find_stop_for_run(run_name: str, run_date: str | datetime | None,
                      root: Path = DEFAULT_EVENT_ROOT) -> dict | None:
    """The recorded stop that emptied this run, or None. Never raises.

    Looks in the host folder and in ``processed/`` for the run's month and
    the month before. A stop matches within ±180 s of ``run_date`` on the
    same vial, or within ±90 s when the stop has no vial. The closest wins.
    """
    try:
        when = parse_local(run_date)
        if when is None:
            return None
        host_dir = Path(root) / HOST
        if not host_dir.is_dir():
            return None
        _sample, vial = sample_from_run_name(run_name)
        local = _utc(when).astimezone(INSTRUMENT_TZ)
        prev = local.replace(day=1) - timedelta(days=1)
        dirs = [host_dir, host_dir / "processed" / f"{local:%Y-%m}",
                host_dir / "processed" / f"{prev:%Y-%m}"]
        best: tuple[float, dict] | None = None
        for d in dirs:
            if not d.is_dir():
                continue
            for p in d.glob("*.json"):
                if p.name in _NOT_EVENTS:
                    continue
                try:
                    ev = _read_json(p)
                except (OSError, ValueError):
                    continue
                if not isinstance(ev, dict) or ev.get("type") != "stop":
                    continue
                at = parse_local(ev.get("at"))
                if at is None:
                    continue
                gap = abs((_utc(at) - _utc(when)).total_seconds())
                if ev.get("vial"):
                    if ev["vial"] != vial or gap > QC_MATCH_WITH_VIAL_S:
                        continue
                elif gap > QC_MATCH_WITHOUT_VIAL_S:
                    continue
                if best is None or gap < best[0]:
                    best = (gap, ev)
        return best[1] if best else None
    except Exception:  # noqa: BLE001 — a QC line never fails over this
        logger.debug("find_stop_for_run failed for %s", run_name, exc_info=True)
        return None


# ── tick ─────────────────────────────────────────────────────────


def run_hystar_tick(root: Path = DEFAULT_EVENT_ROOT, dry_run: bool = False,
                    now: datetime | None = None,
                    store: AlertStore | None = None) -> dict:
    """One cron tick: events first, then the heartbeat. Never raises.

    Returns a summary for the cron log. ``dry_run`` posts, records and moves
    nothing, and lists what it would have relayed in ``would_relay``.
    """
    now = now or datetime.now(timezone.utc)
    host_dir = Path(root) / HOST
    summary: dict = {
        "host": HOST, "root": str(root), "at": now.isoformat(timespec="seconds"),
        "dry_run": dry_run, "events": 0, "relayed": [], "recorded": [],
        "already": [], "pending": [], "rejected": [], "errors": [],
        "would_relay": [], "watcher": None, "alerts": None,
    }
    try:
        if not host_dir.is_dir():
            summary["watcher"] = {"installed": False}
            return summary
        store = store if store is not None else AlertStore()

        events, bad = _scan_events(host_dir)
        summary["events"] = len(events)
        for path, why in bad:
            summary["rejected"].append(path.name)
            if not dry_run:
                _reject(host_dir, path, why)

        for ev in events:
            if dry_run:
                if ev.get("posted_by_pc") is not True:
                    summary["would_relay"].append(ev["id"])
                continue
            try:
                outcome = _relay(ev, store, now)
                if outcome == "failed":
                    summary["pending"].append(ev["id"])
                    continue
                summary[outcome].append(ev["id"])     # relayed / recorded / already
                mark_processed(host_dir, ev)
            except Exception as e:  # noqa: BLE001 — one bad event never stops the rest
                logger.warning("HyStar event %s failed (%s: %s)", ev.get("id"),
                               type(e).__name__, e)
                summary["errors"].append(ev.get("id"))

        st = _watcher_state(host_dir, now)
        summary["watcher"] = {k: (v.isoformat(timespec="seconds")
                                  if isinstance(v, datetime) else v)
                              for k, v in st.items()}
        alerts = check_watcher_heartbeat(host_dir, now)
        if alerts:
            res = notify(alerts, store=store, dry_run=dry_run, now=now)
            summary["alerts"] = {k: res.get(k) for k in
                                 ("n_fresh", "sent", "fresh", "suppressed", "held")}
            if not dry_run:
                _update_marker(host_dir, alerts, res, now)
        elif not st["installed"] and st["down_alerted"] and not dry_run:
            # Uninstalled while down: nothing will ever say "back", so the
            # marker would only keep the bash fast path starting Python.
            (host_dir / DOWN_MARKER).unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001 — the cron must always log a summary
        logger.warning("HyStar tick failed (%s: %s)", type(e).__name__, e,
                       exc_info=True)
        summary["errors"].append(f"tick: {type(e).__name__}")
    return summary
