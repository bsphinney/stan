"""Slack notifications, and the memory that stops them repeating.

WHY THIS EXISTS
---------------
A column clogged on the night of 2026-08-31 and nobody found out until the
morning. Nothing was watching: the Evosep column-health extractor had only
ever been run by hand, and the one thing that *was* scheduled — the nightly
Bruker maintenance cron — emails, at 20:00, from a backup taken hours
earlier. Email is also the wrong channel for "the instrument is failing right
now"; it is read when someone next opens their inbox.

So this module is the transport half of the fix: one place that knows how to
reach Slack, and one place that remembers what it already said.

TWO RULES THAT MATTER MORE THAN THE FEATURE
-------------------------------------------
1. **A notifier must never take down its caller.** These functions are
   invoked from cron jobs whose real work (extract, publish) has already
   succeeded by the time they run. A dead webhook, a DNS blip or a Slack
   outage must cost a log line, not a failed job. Every public function here
   returns a value and raises nothing.

2. **A webhook URL is a bearer credential.** Anyone holding it can post into
   the lab's channel. It is never logged, never included in an exception
   message, and never echoed back by a CLI command — `_scrub` exists because
   urllib is perfectly willing to put the URL it was given into the text of
   an error.

DEDUPLICATION
-------------
The clog above spanned seven consecutive runs; an earlier one on 2026-08-28
spanned fourteen hours. Sending one Slack message per flagged run would have
produced dozens of pings for one event, which is how an alert channel becomes
a channel nobody reads. So every alert carries a *key* that identifies the
condition rather than the observation, and this module keeps the last time it
sent each key.

Re-sending happens on exactly two triggers:

  * **the state changed** — the alert's `signature` differs from the one last
    sent. Signatures are deliberately coarse (a severity plus a 10-point
    pressure band), so a clog getting worse re-alerts while pressure jittering
    by 2 bar does not; and
  * **the cool-off elapsed** — for a *standing* condition only. A clog still
    present twelve hours later has survived a whole shift unnoticed and is
    worth saying again.

A *point* event (one aborted run, one missing Evotip) sets `cool_off_hours`
to None and is therefore said once, ever. Its key carries a time bucket, so
the same failure tomorrow is a different key.

WHERE THE STATE LIVES
---------------------
PG Farm, in `alert_state` (migrations/2026-09-02_alert_state.sql). Not SQLite:
STAN has just finished moving off the Quobyte SQLite file after five separate
corruptions from concurrent writers, and an alerter whose memory is corrupt
either goes silent or spams — both worse than no alerter.

The PG path is DDL-free, the same contract as `get_evosep_column_health_pg`:
the table is owned by `brettsp` and the service account has DML only. When the
table is missing (migration not yet applied) or PG is unreachable, this falls
back to a JSON file. That fallback is deliberate — alerting must work before
the migration lands, and the cost of the fallback is only that two hosts could
each send the same alert once. A single flock'd cron is the only writer, so in
practice it is exact.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

#: Env var holding the Incoming Webhook URL. Checked first so a cron or an
#: Azure app setting can supply it without a config file on disk.
WEBHOOK_ENV = "STAN_SLACK_WEBHOOK"

#: Fallback file, 0600, for a machine that has no env var. Mirrors how
#: `stan.dashboard.ht_share` resolves its signing secret.
WEBHOOK_FILE = "slack_webhook"

#: Slack's own host. Anything else is refused rather than posted to: a typo'd
#: or attacker-supplied "webhook" would otherwise become an exfiltration
#: channel for whatever the alert body happens to contain.
_SLACK_HOST_PREFIX = "https://hooks.slack.com/"

#: Long enough for a slow TLS handshake from Hive, short enough that a cron
#: tick cannot wedge behind an unresponsive Slack.
POST_TIMEOUT_SECONDS = 10

_WEBHOOK_PATTERN = re.compile(r"https://hooks\.slack\.com/\S+")

DASHBOARD_URL = "https://ucd.stan-proteomics.org"


# ── webhook resolution ───────────────────────────────────────────


def _webhook_file() -> Path:
    return Path.home() / ".stan" / WEBHOOK_FILE


def slack_webhook() -> str | None:
    """The Incoming Webhook URL, or None if Slack is not configured.

    Order: ``$STAN_SLACK_WEBHOOK`` → ``slack_webhook_url`` in community.yml →
    ``~/.stan/slack_webhook``. Env first because that is how every deployed
    STAN host (Hive cron, Azure app service) supplies a secret; the file is
    for a workstation where exporting a variable does not survive a reboot.

    A value that is not a hooks.slack.com URL is treated as unconfigured, not
    as an error — a half-finished config line should leave alerts off rather
    than POST the lab's instrument status somewhere unexpected.
    """
    env = (os.environ.get(WEBHOOK_ENV) or "").strip()
    if env:
        return env if _looks_like_webhook(env) else _reject(WEBHOOK_ENV)

    try:
        from stan.config import load_community

        url = str((load_community() or {}).get("slack_webhook_url") or "").strip()
        if url:
            return url if _looks_like_webhook(url) else _reject("community.yml")
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("could not read community.yml for a Slack webhook", exc_info=True)

    try:
        path = _webhook_file()
        if path.exists():
            url = path.read_text().strip()
            if url:
                return url if _looks_like_webhook(url) else _reject(str(path))
    except OSError:
        logger.debug("could not read %s", WEBHOOK_FILE, exc_info=True)

    return None


def _looks_like_webhook(url: str) -> bool:
    return url.startswith(_SLACK_HOST_PREFIX)


def _reject(source: str) -> None:
    # Says where the bad value came from, never what it was.
    logger.warning(
        "%s holds something that is not a https://hooks.slack.com/ URL; "
        "Slack alerts stay off", source,
    )
    return None


def slack_configured() -> bool:
    """True when an alert would actually reach somebody."""
    return slack_webhook() is not None


def _scrub(text: str, webhook: str | None) -> str:
    """Remove any webhook URL from text destined for a log.

    urllib puts the URL it was handed into some of its error strings, so
    logging an exception verbatim is how a webhook ends up in a cron log that
    is world-readable on a group share. Scrubs both the exact URL we used and
    anything else shaped like one.
    """
    out = str(text)
    if webhook:
        out = out.replace(webhook, "<webhook>")
    return _WEBHOOK_PATTERN.sub("<webhook>", out)


# ── posting ──────────────────────────────────────────────────────


def post_slack(payload: dict, webhook: str | None = None) -> bool:
    """POST one message. Returns whether it landed; never raises.

    Synchronous on purpose. The caller needs the answer: state is only
    recorded as "told them" when the message actually went, so a Slack outage
    means the alert is retried on the next tick rather than silently lost.
    (`stan.alerts` keeps its own fire-and-forget wrapper for the QC-gate path,
    which runs inline with acquisition and must not wait on the network.)
    """
    hook = webhook or slack_webhook()
    if not hook:
        logger.debug("no Slack webhook configured; message dropped")
        return False

    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            hook, data=data, headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT_SECONDS) as resp:
            if 200 <= resp.status < 300:
                return True
            logger.warning("Slack returned HTTP %s", resp.status)
            return False
    except urllib.error.HTTPError as e:
        # Slack answers a revoked or mistyped webhook with 403/404 and a
        # one-word body ("no_service", "invalid_token"). Worth having; the URL
        # in e.url is not.
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:120]
        except Exception:
            pass
        logger.warning("Slack rejected the message: HTTP %s %s",
                       e.code, _scrub(body, hook))
        return False
    except Exception as e:  # noqa: BLE001 — a notifier never breaks its caller
        logger.warning("Slack post failed: %s", _scrub(repr(e), hook))
        return False


def send_message(text: str, blocks: list[dict] | None = None) -> bool:
    """Send a message. ``text`` is what the phone push notification shows.

    Slack uses the top-level ``text`` for the notification banner and only
    renders ``blocks`` once the message is opened, so ``text`` has to carry
    the whole headline — which instrument, and what is wrong — for the 2am
    case this was built for.
    """
    payload: dict = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    return post_slack(payload)


def send_test(message: str = "STAN Slack alerts are configured") -> bool:
    """Prove the webhook works. Used by ``stan test-alert``."""
    return send_message(
        f":white_check_mark: {message}",
        [{"type": "section",
          "text": {"type": "mrkdwn",
                   "text": f":white_check_mark: *{message}*\n"
                           f"Instrument alerts will arrive here. "
                           f"<{DASHBOARD_URL}|Open STAN>"}}],
    )


# ── alert identity ───────────────────────────────────────────────


@dataclass
class Alert:
    """One thing worth telling a human about.

    ``key`` identifies the *condition*, not the observation — 40 flagged runs
    of one clog share a key and produce one message. ``signature`` is the
    coarse state of that condition; a change re-alerts even inside the
    cool-off. ``cool_off_hours`` of None means a one-shot point event.
    """

    key: str
    kind: str
    instrument: str
    headline: str
    detail: list[str] = field(default_factory=list)
    severity: str = "warning"          # "critical" | "warning" | "info"
    signature: str = ""
    cool_off_hours: float | None = None
    at: str | None = None              # ISO timestamp of the event, if any
    extra: dict = field(default_factory=dict)


_SEVERITY_ICON = {"critical": ":red_circle:", "warning": ":large_orange_diamond:",
                  "info": ":information_source:"}
#: Ordered worst-first, so a batch is headlined by its worst member.
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}


def render_alerts(alerts: list[Alert]) -> tuple[str, list[dict]]:
    """Turn alerts into (notification text, Slack blocks).

    Written to be legible on a phone at 2am: the notification line names the
    instrument and the fault with a number in it, so the decision "do I get
    up" can be made from the lock screen without opening anything.
    """
    ordered = sorted(alerts, key=lambda a: (_SEVERITY_RANK.get(a.severity, 3), a.key))
    worst = ordered[0]
    icon = _SEVERITY_ICON.get(worst.severity, ":large_orange_diamond:")

    text = f"{icon} {worst.instrument} — {worst.headline}"
    if len(ordered) > 1:
        text += f"  (+{len(ordered) - 1} more)"

    blocks: list[dict] = [
        # header blocks are plain_text and truncate at 150 chars; keep the
        # emoji in the section body where mrkdwn works instead.
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"{icon} *{worst.instrument} — {worst.headline}*"}},
    ]
    for a in ordered:
        lines = []
        if a is not worst:
            lines.append(f"{_SEVERITY_ICON.get(a.severity, '')} *{a.headline}*")
        lines.extend(a.detail)
        if a.at:
            lines.append(f"_at {a.at}_")
        if lines:
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": "\n".join(lines)}})
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn",
                      "text": f"<{DASHBOARD_URL}|STAN dashboard> · Maintenance tab"}],
    })
    return text, blocks


# ── dedup state ──────────────────────────────────────────────────


def _state_path() -> Path:
    override = (os.environ.get("STAN_ALERT_STATE") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".stan" / "alert_state.json"


class AlertStore:
    """What has already been said, and when.

    PG Farm first, a JSON file when PG has no table or is unreachable. The
    fallback is not a degraded mode to be fixed later: it is what makes the
    alerter useful on a host that has never had the migration applied.
    """

    def __init__(self, use_pg: bool | None = None, path: Path | None = None):
        self._path = path or _state_path()
        self._file_cache: dict | None = None
        if use_pg is None:
            use_pg = os.environ.get("STAN_DB_BACKEND", "").lower() == "pg"
        self._use_pg = bool(use_pg)

    # -- backends ------------------------------------------------

    def _pg_get(self, key: str) -> dict | None:
        from stan.db_pg import _connect

        with _connect() as pg, pg.cursor() as cur:
            try:
                cur.execute(
                    "SELECT last_sent, signature FROM alert_state WHERE alert_key = %s",
                    (key,))
            except Exception:  # noqa: BLE001 — undefined_table -> not migrated yet
                pg.rollback()
                raise
            row = cur.fetchone()
        if not row:
            return None
        return {"last_sent": row[0].isoformat() if row[0] else None,
                "signature": row[1] or ""}

    def _pg_put(self, alert: Alert, now: datetime) -> None:
        from stan.db_pg import _connect

        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "INSERT INTO alert_state"
                " (alert_key, first_seen, last_sent, n_sent, signature, kind,"
                "  instrument, detail)"
                " VALUES (%s, %s, %s, 1, %s, %s, %s, %s)"
                " ON CONFLICT (alert_key) DO UPDATE SET"
                "  last_sent = excluded.last_sent,"
                "  n_sent = alert_state.n_sent + 1,"
                "  signature = excluded.signature,"
                "  detail = excluded.detail",
                (alert.key, now, now, alert.signature, alert.kind,
                 alert.instrument, json.dumps(alert.extra)))
            pg.commit()

    def _file(self) -> dict:
        if self._file_cache is None:
            try:
                self._file_cache = json.loads(self._path.read_text())
            except FileNotFoundError:
                self._file_cache = {}
            except Exception:
                logger.warning("unreadable alert state at %s; starting clean",
                               self._path, exc_info=True)
                self._file_cache = {}
        return self._file_cache

    def _file_flush(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._file_cache or {}, indent=1, sort_keys=True))
            os.replace(tmp, self._path)
        except Exception:
            # Failing to remember costs a duplicate message next tick, which
            # is strictly better than failing to alert.
            logger.warning("could not persist alert state", exc_info=True)

    # -- api -----------------------------------------------------

    def last(self, key: str) -> dict | None:
        """The record for ``key``, or None if it has never been sent."""
        if self._use_pg:
            try:
                return self._pg_get(key)
            except Exception:
                logger.info("alert_state unavailable in PG; using the file "
                            "fallback at %s", self._path)
                self._use_pg = False
        return (self._file().get(key) or None)

    def record(self, alert: Alert, now: datetime | None = None) -> None:
        """Remember that ``alert`` was just sent."""
        now = now or datetime.now(timezone.utc)
        if self._use_pg:
            try:
                self._pg_put(alert, now)
                return
            except Exception:
                logger.info("could not record alert_state in PG; using the file "
                            "fallback", exc_info=True)
                self._use_pg = False
        self._file()[alert.key] = {
            "last_sent": now.isoformat(timespec="seconds"),
            "signature": alert.signature,
            "kind": alert.kind,
        }
        self._file_flush()

    def flush(self) -> None:
        if not self._use_pg:
            self._file_flush()


def _parse(ts) -> datetime | None:
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def should_send(alert: Alert, previous: dict | None,
                now: datetime | None = None) -> tuple[bool, str]:
    """Decide whether ``alert`` is news. Returns (send, why).

    The three ways something is news:

      * it has never been sent (``new``);
      * its signature changed since last time (``escalated``) — a clog that
        went from 20% over baseline to 35% is a different message even though
        it is the same clog; or
      * it is a standing condition whose cool-off has elapsed (``cooled_off``).

    Everything else is ``suppressed``. A point event — one aborted run, one
    missing Evotip — has no cool-off and so is only ever sent once.
    """
    if previous is None:
        return True, "new"

    if (previous.get("signature") or "") != (alert.signature or ""):
        return True, "escalated"

    if alert.cool_off_hours is None:
        return False, "suppressed"

    now = now or datetime.now(timezone.utc)
    last = _parse(previous.get("last_sent"))
    if last is None:
        return True, "new"
    if now - last >= timedelta(hours=alert.cool_off_hours):
        return True, "cooled_off"
    return False, "suppressed"


def collapse(alerts: list[Alert]) -> list[Alert]:
    """One alert per key within a single batch, keeping the worst.

    Two sources describe the same physical failure: an aborted injection is
    both an Evosep `tip` flag and a Compass "No Evotip was present". They are
    keyed to collapse — but only the *store* was consulted for that, so
    without this both lines still went out in the same message, which is the
    duplicate the shared key existed to prevent.
    """
    best: dict[str, Alert] = {}
    for a in alerts:
        prior = best.get(a.key)
        if prior is None or (_SEVERITY_RANK.get(a.severity, 3)
                             < _SEVERITY_RANK.get(prior.severity, 3)):
            best[a.key] = a
    # Preserve the caller's ordering; render_alerts does the real sorting.
    seen: set[str] = set()
    out: list[Alert] = []
    for a in alerts:
        if a.key in seen:
            continue
        seen.add(a.key)
        out.append(best[a.key])
    return out


def notify(alerts: list[Alert], store: AlertStore | None = None,
           dry_run: bool = False, now: datetime | None = None) -> dict:
    """Send whatever is news out of ``alerts``, as ONE Slack message.

    Batching is the point: a plate that goes wrong tends to go wrong in
    several ways at once, and three pings for one incident trains the reader
    to swipe them away.

    Never raises. Returns a summary suitable for printing from a cron.
    """
    now = now or datetime.now(timezone.utc)
    store = store if store is not None else AlertStore()
    alerts = collapse(alerts)

    fresh: list[Alert] = []
    suppressed: list[str] = []
    for a in alerts:
        try:
            send, why = should_send(a, store.last(a.key), now=now)
        except Exception:  # noqa: BLE001 — a broken store must not lose the alert
            logger.warning("alert state lookup failed for %s; sending", a.key,
                           exc_info=True)
            send, why = True, "state_unavailable"
        if send:
            a.extra = {**a.extra, "why": why}
            fresh.append(a)
        else:
            suppressed.append(a.key)

    result = {
        "n_alerts": len(alerts),
        "n_fresh": len(fresh),
        "n_suppressed": len(suppressed),
        "suppressed": suppressed,
        "fresh": [{"key": a.key, "severity": a.severity, "headline": a.headline,
                   "why": a.extra.get("why")} for a in fresh],
        "slack_configured": slack_configured(),
        "sent": False,
        "dry_run": dry_run,
    }
    if not fresh or dry_run:
        return result

    text, blocks = render_alerts(fresh)
    if not send_message(text, blocks):
        # Deliberately do NOT record: an unsent alert must be retried next
        # tick, not quietly forgotten. Same rule as the HT email watcher.
        logger.warning("Slack send failed; %d alert(s) will retry", len(fresh))
        return result

    result["sent"] = True
    for a in fresh:
        store.record(a, now=now)
    store.flush()
    return result
