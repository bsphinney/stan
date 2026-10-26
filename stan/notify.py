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
the migration lands.

WHEN PG BLINKS
--------------
On 2026-10-25 21:30 one failed PG lookup switched the store to the file for
the rest of the tick. The file held a different, much smaller memory, so
Friday's two failures went out again as "new". Once alerts wake a phone, that
is being woken at night about two-day-old failures. So:

  * **PG is retried per call.** A store gives up on PG only after
    `_PG_MAX_CONSECUTIVE_FAILURES` failures in a row, for the rest of its
    life (one tick). The first failure is logged at WARNING with its type.
  * **The file is a real mirror.** Every successful PG write also writes the
    file, so the fallback remembers what was sent.
  * **It mirrors both ways.** A write that only reached the file is flagged
    `pg_pending` and written back by `sync_pending_to_pg` at the start of the
    next `notify`; until then `last()` prefers it over a PG answer that has
    not caught up. Otherwise the next normal tick re-sends it: the same bug
    in reverse.
  * **Old point events are held, not guessed.** When PG failed and the file
    has never heard of a point event older than an hour, `notify` holds it
    and posts one standing notice instead. Fresh events still go out; so do
    standing conditions, which are about now. A file-only store (a
    single-lab SQLite install) never holds anything: it has no PG to wait
    for.
  * **Every file read-modify-write is under a lock** on `<state>.lock`, so
    two crons sharing the file cannot drop each other's rows. The wait for
    it is bounded (10 s): every Hive alert cron shares one file on /quobyte,
    and a hung holder must not freeze all of them.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from stan.instrument_time import parse_local

try:  # POSIX only. A Windows single-lab install has one writer and no lock.
    import fcntl
except ImportError:  # pragma: no cover - exercised by monkeypatching to None
    fcntl = None  # type: ignore[assignment]

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


MENTION_ENV = "STAN_SLACK_MENTION"
MENTION_FILE = "slack_mention"
_MENTION_ID = re.compile(r"^[UW][A-Z0-9]{6,}$")


def slack_mention_id() -> str | None:
    """The Slack member ID to @mention on urgent alerts, or None.

    Order: ``$STAN_SLACK_MENTION`` -> ``slack_mention_user`` in community.yml
    -> ``~/.stan/slack_mention``. A value that is not a member ID (``U...`` or
    ``W...``) is ignored, so ``@channel`` or a typo never pages a whole channel.
    """
    env = (os.environ.get(MENTION_ENV) or "").strip()
    if env:
        return env if _MENTION_ID.match(env) else None

    try:
        from stan.config import load_community

        val = str((load_community() or {}).get("slack_mention_user") or "").strip()
        if val:
            return val if _MENTION_ID.match(val) else None
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("could not read community.yml for a Slack mention", exc_info=True)

    try:
        path = Path.home() / ".stan" / MENTION_FILE
        if path.exists():
            val = path.read_text().strip()
            if val:
                return val if _MENTION_ID.match(val) else None
    except OSError:
        logger.debug("could not read %s", MENTION_FILE, exc_info=True)

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

    ``thread_key`` names the *incident* this belongs to, which is not always
    its own condition: the over-pressure events and the named sample from one
    clog episode all carry the clog's key, so they land as replies under it
    instead of as three separate lines in the channel. None means "no
    conversation" — batched with the other unthreaded alerts, as before.
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
    thread_key: str | None = None      # incident this belongs to
    mention: bool = False              # @mention the configured member


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

    mention = ""
    if any(a.mention for a in ordered):
        member = slack_mention_id()
        if member:
            mention = f"<@{member}> "

    text = f"{mention}{icon} {worst.instrument} — {worst.headline}"
    if len(ordered) > 1:
        text += f"  (+{len(ordered) - 1} more)"

    blocks: list[dict] = [
        # header blocks are plain_text and truncate at 150 chars; keep the
        # emoji in the section body where mrkdwn works instead.
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"{mention}{icon} *{worst.instrument} — {worst.headline}*"}},
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


#: Reserved keys inside the `detail` jsonb. Thread and acknowledgement state
#: live there rather than in new columns, because `detail` exists precisely so
#: this can evolve without DDL -- the original migration says so -- and because
#: the JSON file fallback then needs no separate shape. The leading underscore
#: keeps them clear of anything an alert puts in `extra`.
_THREAD = "_thread"
_ACK = "_ack"
#: The same two, spelled without the underscore in the file backend, where
#: they are ordinary nested dicts rather than jsonb paths.
_FILE_FIELD = {_THREAD: "thread", _ACK: "ack"}

#: Reactions that count as "a human has seen this". Deliberately a short list:
#: an ack silences repetition, so a reaction people use casually would silence
#: alerts by accident.
ACK_EMOJI = ("white_check_mark", "heavy_check_mark", "eyes")


def _state_path() -> Path:
    override = (os.environ.get("STAN_ALERT_STATE") or "").strip()
    if override:
        return Path(override)
    return Path.home() / ".stan" / "alert_state.json"


#: Longest a write waits for another cron's lock on the state file before it
#: writes without it. Holders keep it for milliseconds; anything past this is
#: a hung holder, not a busy one.
_LOCK_WAIT_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.1


def _flock_bounded(fh, wait_seconds: float) -> bool:
    """``LOCK_EX`` on ``fh``, retried non-blocking for up to ``wait_seconds``.

    True once held, False when the wait ran out. Any other OSError (a share
    that does not support locks) propagates to the caller's handler.
    """
    deadline = time.monotonic() + max(0.0, wait_seconds)
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                raise
        if time.monotonic() >= deadline:
            return False
        time.sleep(_LOCK_POLL_SECONDS)


#: Failures in a row after which a store stops asking PG for the rest of its
#: life (one tick). One failure is a blip and worth retrying -- treating it as
#: an outage is the 2026-10-25 bug. Two in a row is an outage, and asking again
#: for every alert would only add a connect timeout per alert to the tick.
_PG_MAX_CONSECUTIVE_FAILURES = 2

#: Credential-shaped text that can appear in a driver error. Scrubbed before a
#: PG failure reaches a cron log on a group share.
_SECRET_ASSIGNMENT = re.compile(r"(?i)\b(password|pgpassword|token|secret)(\s*[=:]\s*)\S+")
_JWT = re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+")


def _scrub_error(e: BaseException) -> str:
    """One log line for ``e``, with anything credential-shaped removed."""
    text = _scrub(str(e), None)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2<redacted>", text)
    text = _JWT.sub("<jwt>", text)
    return " ".join(text.split())[:300]


class AlertStore:
    """What has already been said, and when.

    PG Farm first, a JSON file when PG has no table or is unreachable. The
    fallback is not a degraded mode to be fixed later: it is what makes the
    alerter useful on a host that has never had the migration applied.

    ``_use_pg`` is what the store was configured for and never changes; a
    failure does not flip it (see the module docstring, WHEN PG BLINKS).
    ``last_source`` says where the latest ``last()`` got its answer, and
    ``degraded`` whether any PG call has failed in this instance.
    """

    def __init__(self, use_pg: bool | None = None, path: Path | None = None):
        self._path = path or _state_path()
        self._file_cache: dict | None = None
        #: Keys changed in memory whose write to the file has not landed yet.
        self._unsaved: set[str] = set()
        if use_pg is None:
            use_pg = os.environ.get("STAN_DB_BACKEND", "").lower() == "pg"
        self._use_pg = bool(use_pg)
        self._pg_failures = 0              # consecutive
        self._pg_warned = False
        self.degraded = False
        self.last_source = "pg" if self._use_pg else "file"

    # -- backends ------------------------------------------------

    def _pg_get(self, key: str) -> dict | None:
        from stan.db_pg import _connect

        with _connect() as pg, pg.cursor() as cur:
            try:
                cur.execute(
                    "SELECT last_sent, signature, kind, detail"
                    " FROM alert_state WHERE alert_key = %s",
                    (key,))
            except Exception:  # noqa: BLE001 — undefined_table -> not migrated yet
                pg.rollback()
                raise
            row = cur.fetchone()
        if not row:
            return None
        detail = row[3] or {}
        return {"last_sent": row[0].isoformat() if row[0] else None,
                "signature": row[1] or "",
                "kind": row[2],
                "thread": detail.get(_THREAD) or None,
                "ack": detail.get(_ACK) or None}

    def _pg_put(self, alert: Alert, now: datetime) -> None:
        from stan.db_pg import _connect

        # `detail` is MERGED, not replaced. Thread and acknowledgement live in
        # the same jsonb (see _THREAD/_ACK), and a plain overwrite would drop
        # the thread id on the next update -- turning every reply back into a
        # new channel message, silently.
        #
        # `last_sent` keeps the later of the two, because `sync_pending_to_pg`
        # writes sends from an outage after the fact and must not wind back a
        # row that has been sent since. For a live send `now` is the later
        # anyway. `signature` is still overwritten: the file row being synced
        # was mirrored from every send this host made, so it is never older
        # than what this host put in PG.
        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "INSERT INTO alert_state"
                " (alert_key, first_seen, last_sent, n_sent, signature, kind,"
                "  instrument, detail)"
                " VALUES (%s, %s, %s, 1, %s, %s, %s, %s)"
                " ON CONFLICT (alert_key) DO UPDATE SET"
                "  last_sent = GREATEST(alert_state.last_sent, excluded.last_sent),"
                "  n_sent = alert_state.n_sent + 1,"
                "  signature = excluded.signature,"
                "  detail = COALESCE(alert_state.detail, '{}'::jsonb)"
                "           || excluded.detail",
                (alert.key, now, now, alert.signature, alert.kind,
                 alert.instrument, json.dumps(alert.extra)))
            pg.commit()

    def _pg_merge_detail(self, key: str, patch: dict, kind: str | None) -> None:
        """Merge a patch into one row's `detail`, creating the row if needed."""
        from stan.db_pg import _connect

        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "INSERT INTO alert_state (alert_key, kind, detail)"
                " VALUES (%s, %s, %s)"
                " ON CONFLICT (alert_key) DO UPDATE SET"
                "  detail = COALESCE(alert_state.detail, '{}'::jsonb)"
                "           || excluded.detail",
                (key, kind, json.dumps(patch)))
            pg.commit()

    def _pg_open_threads(self) -> list[dict]:
        from stan.db_pg import _connect

        with _connect() as pg, pg.cursor() as cur:
            cur.execute(
                "SELECT alert_key, kind, detail FROM alert_state"
                " WHERE detail -> %s ->> 'ts' IS NOT NULL"
                "   AND detail -> %s ->> 'closed_at' IS NULL",
                (_THREAD, _THREAD))
            rows = cur.fetchall()
        return [{"key": r[0], "kind": (r[2] or {}).get(_THREAD, {}).get("kind") or r[1],
                 "thread": (r[2] or {}).get(_THREAD) or {}} for r in rows]

    # -- PG health -----------------------------------------------

    def _pg_active(self) -> bool:
        """Configured for PG, and not yet given up on in this instance."""
        return self._use_pg and self._pg_failures < _PG_MAX_CONSECUTIVE_FAILURES

    def _pg_ok(self) -> None:
        self._pg_failures = 0

    def _pg_failed(self, e: Exception, what: str) -> None:
        self._pg_failures += 1
        self.degraded = True
        if not self._pg_warned:
            self._pg_warned = True
            logger.warning("alert_state %s failed in PG (%s: %s); answering from "
                           "the file at %s", what, type(e).__name__,
                           _scrub_error(e), self._path)
        else:
            logger.debug("alert_state %s failed in PG again (%s: %s)", what,
                         type(e).__name__, _scrub_error(e))
        if self._pg_failures == _PG_MAX_CONSECUTIVE_FAILURES:
            logger.info("alert_state: %d PG failures in a row; using only the "
                        "file at %s for the rest of this run",
                        self._pg_failures, self._path)

    # -- file ----------------------------------------------------

    @contextlib.contextmanager
    def _file_lock(self):
        """Hold an exclusive lock on ``<state>.lock`` around a read-modify-write.

        Several crons fall back to the same file, so two can write it in the
        same minute; without the lock the later write drops the earlier one's
        rows. A no-op where fcntl does not exist (Windows single-lab installs
        have one writer), and when the lock cannot be taken: writing without
        it risks a duplicate message, never a lost alert.

        The wait is bounded (`_LOCK_WAIT_SECONDS`). Every alerting cron on
        Hive shares one file on /quobyte, so a holder that hangs there would
        otherwise freeze all of them at once: a pager that stops without a
        word. After the bound this writes without the lock and says so.
        """
        lock_path = self._path.with_name(self._path.name + ".lock")
        fh = None
        if fcntl is not None:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                fh = open(lock_path, "a")  # held open across the yield
                if not _flock_bounded(fh, _LOCK_WAIT_SECONDS):
                    logger.warning("could not lock %s within %g s (another writer "
                                   "holds it); writing without the lock",
                                   lock_path, _LOCK_WAIT_SECONDS)
                    fh.close()
                    fh = None
            except OSError as e:
                logger.warning("could not lock %s (%s); writing without the lock",
                               lock_path, type(e).__name__)
                if fh is not None:
                    fh.close()
                    fh = None
        try:
            yield
        finally:
            if fh is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                fh.close()

    def _file_load(self) -> dict | None:
        """The file as it is on disk now.

        ``{}`` when it is absent or corrupt (start clean, as always). None when
        it exists but cannot be read right now, so a writer knows not to
        replace everything in it with one row.
        """
        try:
            data = json.loads(self._path.read_text())
        except FileNotFoundError:
            return {}
        except ValueError:                 # JSONDecodeError, bad UTF-8
            logger.warning("unreadable alert state at %s; starting clean",
                           self._path)
            return {}
        except OSError as e:
            logger.warning("could not read alert state at %s (%s)", self._path,
                           type(e).__name__)
            return None
        if not isinstance(data, dict):
            logger.warning("alert state at %s is not a JSON object; starting clean",
                           self._path)
            return {}
        return data

    def _file(self) -> dict:
        if self._file_cache is None:
            self._file_cache = self._file_load() or {}
        return self._file_cache

    def _file_write(self, data: dict) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            os.replace(tmp, self._path)
            return True
        except Exception:
            # Failing to remember costs a duplicate message next tick, which
            # is strictly better than failing to alert.
            logger.warning("could not persist alert state", exc_info=True)
            return False

    def _file_update(self, key: str, mutate) -> None:
        """Read-modify-write one row under the lock. Never raises.

        Re-reads the file inside the lock rather than writing this instance's
        copy, which may be minutes old and miss another cron's rows.
        """
        with self._file_lock():
            disk = self._file_load()
            data = disk if disk is not None else dict(self._file())
            row = dict(data.get(key) or {})
            mutate(row)
            data[key] = row
            self._file_cache = data
            if disk is not None and self._file_write(data):
                self._unsaved.discard(key)
            else:
                self._unsaved.add(key)

    def _owed_to_pg(self, key: str, pg_row: dict | None) -> dict | None:
        """The file's row for ``key`` if it records a send PG has not caught up on.

        `sync_pending_to_pg` normally clears these first, but it can fail on a
        blip while the lookups after it succeed. PG would then say "never
        sent" about something that went out during the outage.
        """
        row = self._file().get(key)
        if not row or not row.get("pg_pending"):
            return None
        mine = _parse(row.get("last_sent"))
        if mine is None:
            return None
        theirs = _parse((pg_row or {}).get("last_sent"))
        return row if theirs is None or mine > theirs else None

    # -- api -----------------------------------------------------

    def last(self, key: str) -> dict | None:
        """The record for ``key``, or None if it has never been sent."""
        if self._pg_active():
            try:
                row = self._pg_get(key)
            except Exception as e:  # noqa: BLE001 — retried next call, see _pg_failed
                self._pg_failed(e, "lookup")
            else:
                self._pg_ok()
                owed = self._owed_to_pg(key, row)
                if owed is not None:
                    self.last_source = "file"
                    return owed
                self.last_source = "pg"
                return row
        self.last_source = "file"
        return (self._file().get(key) or None)

    def record(self, alert: Alert, now: datetime | None = None) -> None:
        """Remember that ``alert`` was just sent: PG, and always the file."""
        now = now or datetime.now(timezone.utc)
        owed = self._use_pg
        if self._pg_active():
            try:
                self._pg_put(alert, now)
            except Exception as e:  # noqa: BLE001
                self._pg_failed(e, "write")
            else:
                self._pg_ok()
                owed = False

        def apply(row: dict) -> None:
            row.update({
                "last_sent": now.isoformat(timespec="seconds"),
                "signature": alert.signature,
                "kind": alert.kind,
                "instrument": alert.instrument,
            })                             # keeps `thread` / `ack`
            if owed:
                row["pg_pending"] = True

        self._file_update(alert.key, apply)

    def _merge(self, key: str, field: str, patch: dict,
               kind: str | None = None) -> None:
        """Attach thread or acknowledgement state to a key. Never raises."""
        owed = self._use_pg
        if self._pg_active():
            try:
                self._pg_merge_detail(key, {field: patch}, kind)
            except Exception as e:  # noqa: BLE001
                self._pg_failed(e, f"{field} merge")
            else:
                self._pg_ok()
                owed = False

        def apply(row: dict) -> None:
            existing = dict(row.get(_FILE_FIELD[field]) or {})
            existing.update(patch)
            row[_FILE_FIELD[field]] = existing
            if kind and not row.get("kind"):
                row["kind"] = kind
            if owed:
                row["pg_pending"] = True

        self._file_update(key, apply)

    def sync_pending_to_pg(self) -> int:
        """Write rows that only reached the file back to PG. Never raises.

        Without this the tick after an outage asks PG, PG has never heard of
        what went out during it, and it all goes out again. Oldest first;
        stops at the first failure and leaves the rest flagged for next time.
        A row changed in the file while this ran keeps its flag. Returns how
        many rows reached PG.
        """
        if not self._pg_active():
            return 0
        disk = self._file_load()
        if not disk:
            return 0
        owed = sorted(((k, dict(r)) for k, r in disk.items()
                       if isinstance(r, dict) and r.get("pg_pending")),
                      key=lambda kr: str(kr[1].get("last_sent") or ""))
        done: list[tuple[str, dict]] = []
        for key, row in owed:
            patch = {f: row[ff] for f, ff in _FILE_FIELD.items() if row.get(ff)}
            sent_at = _parse(row.get("last_sent"))
            try:
                if sent_at is not None:
                    self._pg_put(Alert(key=key, kind=row.get("kind") or "",
                                       instrument=row.get("instrument") or "",
                                       headline="", signature=row.get("signature") or "",
                                       extra=patch), sent_at)
                elif patch:
                    self._pg_merge_detail(key, patch, row.get("kind"))
            except Exception as e:  # noqa: BLE001
                self._pg_failed(e, "sync")
                break
            self._pg_ok()
            done.append((key, row))
        if not done:
            return 0

        with self._file_lock():
            now_disk = self._file_load()
            if now_disk is not None:
                for key, row in done:
                    if now_disk.get(key) == row:
                        cleared = dict(row)
                        cleared.pop("pg_pending", None)
                        now_disk[key] = cleared
                if self._file_write(now_disk):
                    self._file_cache = now_disk
        logger.info("alert_state: wrote %d send(s) recorded during a PG outage "
                    "back to PG", len(done))
        return len(done)

    def set_thread(self, key: str, channel: str, ts: str, kind: str,
                   now: datetime | None = None) -> None:
        """Remember the message that opened this incident's thread."""
        now = now or datetime.now(timezone.utc)
        self._merge(key, _THREAD, {"channel": channel, "ts": ts, "kind": kind,
                                   "opened_at": now.isoformat(timespec="seconds")},
                    kind=kind)

    def close_thread(self, key: str, now: datetime | None = None) -> None:
        """Mark an incident finished, so its thread is not re-closed."""
        now = now or datetime.now(timezone.utc)
        self._merge(key, _THREAD,
                    {"closed_at": now.isoformat(timespec="seconds")})

    def set_ack(self, key: str, by: str | None, emoji: str,
                now: datetime | None = None) -> None:
        """Record that a human reacted to this incident's parent message."""
        now = now or datetime.now(timezone.utc)
        self._merge(key, _ACK, {"by": by, "emoji": emoji,
                                "at": now.isoformat(timespec="seconds")})

    def open_threads(self) -> list[dict]:
        """Incidents with a thread that has not been closed off."""
        if self._pg_active():
            try:
                rows = self._pg_open_threads()
            except Exception as e:  # noqa: BLE001
                self._pg_failed(e, "thread list")
            else:
                self._pg_ok()
                return rows
        out = []
        for key, row in (self._file() or {}).items():
            th = (row or {}).get("thread") or {}
            if th.get("ts") and not th.get("closed_at"):
                out.append({"key": key, "kind": th.get("kind") or row.get("kind"),
                            "thread": th})
        return out

    def flush(self) -> None:
        """Persist any change an earlier write could not. Never raises.

        Every write already goes to the file as it happens (PG or not), so
        this only retries the ones that failed, merged onto the file as it is
        now rather than over it.
        """
        if not self._unsaved:
            return
        with self._file_lock():
            disk = self._file_load()
            if disk is None:
                return
            cache = self._file_cache or {}
            for key in self._unsaved:
                if key in cache:
                    disk[key] = cache[key]
            if self._file_write(disk):
                self._file_cache = disk
                self._unsaved.clear()


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

    An **acknowledgement suppresses repetition, never escalation.** Somebody
    reacting to the alert means they have seen it, which is not the same as it
    being fixed — so the cool-off stops nagging them, while a genuine change
    of state still speaks. That ordering is the whole point: the escalation
    check runs first and returns before the ack is ever consulted.
    """
    if previous is None:
        return True, "new"

    if (previous.get("signature") or "") != (alert.signature or ""):
        return True, "escalated"

    if alert.cool_off_hours is None:
        return False, "suppressed"

    # Reached only for an unchanged standing condition, i.e. exactly the
    # repetition an acknowledgement is meant to stop. Escalation returned two
    # branches ago and never gets here.
    if (previous.get("ack") or {}).get("at"):
        return False, "acknowledged"

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


def poll_acks(store: AlertStore, now: datetime | None = None) -> list[dict]:
    """Look for a human reacting to any open incident. Never raises.

    This is the whole reason the ack is a *poll* rather than an events
    endpoint: it rides the 30-minute tick that already exists, needs no public
    route into the lab, and its worst failure is a late acknowledgement.

    A reactions read that fails returns None from the transport and is skipped,
    NOT treated as "no reactions" -- the distinction matters only in one
    direction, but it is the safe one: a blip must never look like an ack and
    silence a live alert.
    """
    from stan import slack_api

    now = now or datetime.now(timezone.utc)
    found: list[dict] = []
    if not slack_api.threading_available():
        return found
    try:
        open_threads = store.open_threads()
    except Exception:  # noqa: BLE001
        logger.warning("could not list open threads for the ack poll",
                       exc_info=True)
        return found

    for t in open_threads:
        th = t.get("thread") or {}
        ch, ts = th.get("channel"), th.get("ts")
        if not ch or not ts:
            continue
        reactions = slack_api.get_reactions(ch, ts)
        if reactions is None:
            continue                      # could not tell; try again next tick
        for r in reactions:
            if r.get("name") not in ACK_EMOJI:
                continue
            who = (r.get("users") or [None])[0]
            store.set_ack(t["key"], who, r.get("name"), now=now)
            found.append({"key": t["key"], "emoji": r.get("name"), "by": who})
            break
    return found


def close_finished_threads(store: AlertStore, live_keys: set[str],
                           now: datetime | None = None) -> list[str]:
    """Reply "this appears to be over" under incidents that have stopped.

    An episode that simply stops is exactly what leaves somebody wondering
    whether it was fixed or whether the watcher died, so silence is the wrong
    ending. Only standing conditions are closed: a point event was never an
    ongoing situation to begin with.

    The caller must only pass ``live_keys`` from a document that actually
    loaded. If an extract fails, every key vanishes and closing on that would
    announce that everything resolved at the moment we stopped being able to
    see anything.
    """
    from stan import slack_api

    now = now or datetime.now(timezone.utc)
    closed: list[str] = []
    if not slack_api.threading_available():
        return closed
    try:
        open_threads = store.open_threads()
    except Exception:  # noqa: BLE001
        logger.warning("could not list open threads to close", exc_info=True)
        return closed

    for t in open_threads:
        if t["key"] in live_keys or t.get("kind") not in _STANDING_KINDS:
            continue
        th = t.get("thread") or {}
        if not th.get("ts"):
            continue
        posted = slack_api.post_message(
            f":white_check_mark: Resolved — no further flagged runs "
            f"({t['key'].split(':')[0]}).",
            [{"type": "section", "text": {"type": "mrkdwn",
              "text": ":white_check_mark: *Resolved* — the instrument has "
                      "stopped producing flagged runs for this condition.\n"
                      "_Closed automatically because the condition went quiet, "
                      "not because anyone confirmed a fix._"}}],
            thread_ts=th["ts"])
        if posted:
            store.close_thread(t["key"], now=now)
            closed.append(t["key"])
    store.flush()
    return closed


#: Conditions that can meaningfully "end" and so deserve a closing reply.
#: A point event -- one aborted run -- was never ongoing.
_STANDING_KINDS = ("clog", "pressure_rising", "column_worn")


def _thread_group(a: Alert) -> str | None:
    return a.thread_key or None


#: Key of the standing notice posted while point events are being held.
HELD_NOTICE_KEY = "alert_memory_unavailable"

#: How many held keys the notice lists before saying "and N more".
_HELD_LISTED = 10


def _hold(alert: Alert, previous: dict | None, store, now: datetime,
          older_than: timedelta) -> bool:
    """True for a point event PG could not vouch for that is too old to be news.

    Only when the store is configured for PG and this lookup fell back to the
    file because PG failed (``last_source == "file"``). A file-only store
    -- a single-lab SQLite install -- has no PG to wait for, and holding there
    would drop every point alert older than ``older_than`` for good. An event
    with no readable time is never held: its age is unknown, and an unknown
    age is not evidence that it is old.

    Age is measured in instrument-local time (`parse_local`); `_parse` would
    call a naive local time UTC and age a fresh event by 7-8 h.
    """
    if alert.cool_off_hours is not None or previous is not None:
        return False
    if not getattr(store, "_use_pg", False):
        return False
    if getattr(store, "last_source", None) != "file":
        return False
    try:
        at = parse_local(alert.at)
        if at is None:
            return False
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now - at > older_than
    except Exception:  # noqa: BLE001 — never hold on a time we could not read
        return False


def _held_notice(held: list[str]) -> Alert:
    shown = ", ".join(held[:_HELD_LISTED])
    if len(held) > _HELD_LISTED:
        shown += f" and {len(held) - _HELD_LISTED} more"
    return Alert(
        key=HELD_NOTICE_KEY, kind="alert_memory", instrument="STAN",
        severity="warning",
        headline=f"STAN's alert memory (PG) is unavailable; {len(held)} older events held",
        detail=[f"Held: {shown}.",
                "Each goes out when PG answers, if it was never sent; "
                "past its lookback it is dropped, and this notice is the record."],
        signature="held", cool_off_hours=12.0)


def notify(alerts: list[Alert], store: AlertStore | None = None,
           dry_run: bool = False, now: datetime | None = None,
           hold_older_than: timedelta = timedelta(hours=1)) -> dict:
    """Send whatever is news, threaded by incident where possible.

    With a bot token, the first alert of an incident opens a thread and every
    later development replies under it: the channel gets one line, the thread
    gets the story. Without one, everything falls back to the webhook and a
    single batched message — which is what another lab running STAN with only
    a webhook gets permanently, so that path stays first-class.

    While PG cannot answer, a point event the file has never heard of and that
    is older than ``hold_older_than`` is held rather than sent (see `_hold`),
    and one standing notice says how many. ``result["held"]`` lists them.

    Never raises. Returns a summary suitable for printing from a cron.
    """
    from stan import slack_api

    now = now or datetime.now(timezone.utc)
    store = store if store is not None else AlertStore()
    alerts = collapse(alerts)

    # Sends that only reached the file during an earlier outage go back to PG
    # first, so this tick's lookups see them. Not on a dry run: it writes.
    if getattr(store, "_use_pg", False) and not dry_run:
        try:
            store.sync_pending_to_pg()
        except Exception as e:  # noqa: BLE001 — it never raises; belt and braces
            logger.warning("could not write pending alert state back to PG (%s)",
                           type(e).__name__)

    fresh: list[Alert] = []
    suppressed: list[str] = []
    held: list[str] = []
    for a in alerts:
        try:
            previous = store.last(a.key)
            send, why = should_send(a, previous, now=now)
        except Exception:  # noqa: BLE001 — a broken store must not lose the alert
            logger.warning("alert state lookup failed for %s; sending", a.key,
                           exc_info=True)
            send, why = True, "state_unavailable"
        else:
            if send and _hold(a, previous, store, now, hold_older_than):
                held.append(a.key)
                continue
        if send:
            a.extra = {**a.extra, "why": why}
            fresh.append(a)
        else:
            suppressed.append(a.key)

    if held:
        logger.warning("alert memory (PG) unavailable; held %d point event(s) "
                       "older than %s: %s", len(held), hold_older_than,
                       ", ".join(held))
        notice = _held_notice(held)
        try:
            send, why = should_send(notice, store.last(notice.key), now=now)
        except Exception:  # noqa: BLE001
            send, why = True, "state_unavailable"
        if send:
            notice.extra = {"why": why}
            fresh.append(notice)
        else:
            suppressed.append(notice.key)

    threaded = slack_api.threading_available()
    result = {
        "n_alerts": len(alerts),
        "n_fresh": len(fresh),
        "n_suppressed": len(suppressed),
        "suppressed": suppressed,
        "held": held,
        "fresh": [{"key": a.key, "severity": a.severity, "headline": a.headline,
                   "why": a.extra.get("why")} for a in fresh],
        "slack_configured": slack_configured() or threaded,
        "threaded": threaded,
        "threads": [],
        "sent": False,
        "dry_run": dry_run,
    }
    if not fresh or dry_run:
        return result

    if not threaded:
        # Webhook path, unchanged: one batched message, no thread, no ack.
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

    # Threaded path. Group by incident; alerts with no incident (a Compass
    # software error, say) are batched together as before under a None group.
    groups: dict[str | None, list[Alert]] = {}
    for a in fresh:
        groups.setdefault(_thread_group(a), []).append(a)

    any_sent = False
    for gkey, members in groups.items():
        text, blocks = render_alerts(members)
        parent_ts = None
        if gkey:
            prev = store.last(gkey) or {}
            parent_ts = ((prev.get("thread") or {}).get("ts")
                         if not (prev.get("thread") or {}).get("closed_at")
                         else None)
        posted = slack_api.post_message(text, blocks, thread_ts=parent_ts)
        if not posted:
            logger.warning("Slack send failed for %s; %d alert(s) will retry",
                           gkey or "(unthreaded)", len(members))
            continue
        any_sent = True
        # The first thing said about an incident opens its thread -- which may
        # be an over-pressure rather than the clog itself, and that is right:
        # the thread is the incident, not one condition within it.
        if gkey and not parent_ts:
            kind = next((m.kind for m in members if m.kind in _STANDING_KINDS),
                        members[0].kind)
            store.set_thread(gkey, posted["channel"], posted["ts"], kind, now=now)
        result["threads"].append(
            {"key": gkey, "ts": posted["ts"], "reply": bool(parent_ts),
             "n": len(members)})
        for a in members:
            store.record(a, now=now)

    result["sent"] = any_sent
    store.flush()
    return result
