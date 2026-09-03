"""Slack Web API transport — the things an Incoming Webhook cannot do.

WHY THIS EXISTS ALONGSIDE THE WEBHOOK
-------------------------------------
A webhook can only fire a new message into one channel. It cannot tell you the
message it just created, so it cannot thread, and it cannot read anything back.
That forces the alerter into a choice it should not have to make: repeat itself
on a timer, or go quiet and hope somebody noticed.

A bot token removes the choice. `chat.postMessage` returns the message `ts`, so
every later development in an incident can be a reply under the first one — the
channel gets one line, the thread gets the whole story. And `reactions.get`
means "did a human actually look at this" is an answerable question rather than
a 12-hour guess.

WHAT THIS MODULE IS AND IS NOT
------------------------------
This is transport only: credentials, HTTP, and turning Slack's `{"ok": false,
"error": ...}` into something a caller can branch on. Every policy decision --
what deserves a reply, what an acknowledgement suppresses, when a thread is
closed -- lives in `stan.notify`. The split matters because the policy is the
part with the interesting bugs, and it should be testable without a socket.

RULES CARRIED OVER FROM THE WEBHOOK PATH
----------------------------------------
* **Never break the caller.** Every public function returns a value and raises
  nothing. These run from a cron whose real work already succeeded.
* **The token never reaches a log.** A bot token is a bearer credential with
  more reach than the webhook -- it can post as the app anywhere it is invited.
  `_scrub` strips anything shaped like one from text on its way to a logger,
  because urllib and Slack error bodies will both happily echo what they were
  given.

CONFIGURATION
-------------
Resolved the same way as everything else in STAN: environment first, then
`community.yml`, then a 0600 file under `~/.stan`.

    slack_bot_token: "xoxb-..."     # chat:write, reactions:read
    slack_channel:   "C0123456789"  # or "#stan-alerts"

`slack_channel` has no equivalent in the webhook world -- a webhook URL *is* a
channel -- so it is genuinely new configuration, and without it there is
nothing to post to. That is not an error: the caller falls back to the webhook
and simply loses threading, which is exactly the degradation another lab
running STAN with only a webhook will experience permanently.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

BOT_TOKEN_ENV = "STAN_SLACK_BOT_TOKEN"
BOT_TOKEN_FILE = "slack_bot_token"
CHANNEL_ENV = "STAN_SLACK_CHANNEL"

API_ROOT = "https://slack.com/api/"

#: Same budget as the webhook path: long enough for a slow handshake from
#: Hive, short enough that a cron tick cannot wedge behind a hung Slack.
TIMEOUT_SECONDS = 10

#: Scopes this module needs. `chat:write` posts and threads; `reactions:read`
#: answers "did anyone look at it". Reported by `stan doctor` so a missing one
#: surfaces at configuration time rather than at 2am.
REQUIRED_SCOPES = ("chat:write", "reactions:read")

#: Bot tokens are `xoxb-`; the others are here because a mis-pasted user or
#: app token is just as much a credential and must not be logged either.
_TOKEN_PATTERN = re.compile(r"xox[abposr]-[A-Za-z0-9-]+")


def _scrub(text: object) -> str:
    """Strip anything shaped like a Slack token out of text bound for a log."""
    return _TOKEN_PATTERN.sub("<token>", str(text))


# ── configuration ────────────────────────────────────────────────


def _from_config(key: str, env: str, filename: str) -> str | None:
    val = (os.environ.get(env) or "").strip()
    if val:
        return val
    try:
        from stan.config import load_community

        val = str((load_community() or {}).get(key) or "").strip()
        if val:
            return val
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("could not read community.yml for %s", key, exc_info=True)
    try:
        path = Path.home() / ".stan" / filename
        if path.exists():
            val = path.read_text().strip()
            if val:
                return val
    except OSError:
        logger.debug("could not read ~/.stan/%s", filename, exc_info=True)
    return None


def bot_token() -> str | None:
    """The `xoxb-` bot token, or None when only a webhook is configured.

    A value that is not shaped like a bot token is treated as unconfigured
    rather than sent: pasting a webhook URL into this field should leave
    threading off, not POST a credential to an API that will reject it and
    echo it back in an error.
    """
    val = _from_config("slack_bot_token", BOT_TOKEN_ENV, BOT_TOKEN_FILE)
    if not val:
        return None
    if not val.startswith("xoxb-"):
        logger.warning("slack_bot_token is not a bot token (expected xoxb-); "
                       "threading stays off")
        return None
    return val


def channel() -> str | None:
    """Where `chat.postMessage` posts. No webhook equivalent — see module docs."""
    return _from_config("slack_channel", CHANNEL_ENV, "slack_channel")


def threading_available() -> bool:
    """True when we can post a message AND know its id afterwards."""
    return bool(bot_token()) and bool(channel())


# ── transport ────────────────────────────────────────────────────


def api_call(method: str, payload: dict | None = None) -> dict | None:
    """Call one Web API method. Returns the parsed body, or None on failure.

    Never raises. A returned dict always has `ok`; callers branch on that
    rather than on exceptions, because "Slack said no" and "the network was
    down" need the same treatment from a cron: log it, carry on.
    """
    token = bot_token()
    if not token:
        return None

    body = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(
        API_ROOT + method,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        logger.warning("Slack %s failed: HTTP %s", method, e.code)
        return None
    except Exception as e:  # noqa: BLE001 — transport never breaks its caller
        logger.warning("Slack %s failed: %s", method, _scrub(repr(e)))
        return None

    if not data.get("ok"):
        err = data.get("error", "unknown")
        if err == "missing_scope":
            # Worth spelling out: this is a configuration problem a human must
            # fix, not a transient one that will clear on the next tick.
            logger.warning(
                "Slack %s needs the %r scope, which this token does not have. "
                "Re-install the app with it; `stan doctor` lists what is "
                "missing.", method, data.get("needed"))
        else:
            logger.warning("Slack %s returned %s", method, _scrub(err))
        return data
    return data


def post_message(text: str, blocks: list[dict] | None = None,
                 thread_ts: str | None = None) -> dict | None:
    """Post a message, optionally as a reply. Returns {channel, ts} or None.

    `text` is what the phone notification shows, so it carries the headline
    even when `blocks` render the detail -- same contract as the webhook path.
    """
    ch = channel()
    if not ch:
        return None
    payload: dict = {"channel": ch, "text": text}
    if blocks:
        payload["blocks"] = blocks
    if thread_ts:
        payload["thread_ts"] = thread_ts
        # Replies stay in the thread. A clog update does not need to re-ping
        # the whole channel -- the channel already has the parent.
        payload["reply_broadcast"] = False
    data = api_call("chat.postMessage", payload)
    if not data or not data.get("ok"):
        return None
    return {"channel": data.get("channel") or ch, "ts": data.get("ts")}


def get_reactions(ch: str, ts: str) -> list[dict] | None:
    """Reactions on one message, or None if it could not be read.

    None means "could not tell" and must not be read as "nobody reacted" --
    treating a failed read as an absence of acknowledgement is the safe
    direction (we keep alerting), but treating it as a *presence* would
    silence a live alert on a network blip.
    """
    data = api_call("reactions.get", {"channel": ch, "timestamp": ts,
                                      "full": True})
    if not data or not data.get("ok"):
        return None
    msg = data.get("message") or {}
    return msg.get("reactions") or []


# ── diagnostics ──────────────────────────────────────────────────


def auth_check() -> dict:
    """What `stan doctor` needs to say about the bot token.

    Returns a plain dict rather than printing, so the CLI owns presentation:
    ``{configured, valid, team, user, channel, scopes, missing_scopes, error}``.
    Scopes come from the `x-oauth-scopes` response header, which is the only
    place Slack reports them on a successful call.
    """
    out = {"configured": bool(bot_token()), "channel": channel(),
           "valid": False, "team": None, "user": None,
           "scopes": [], "missing_scopes": [], "error": None}
    if not out["configured"]:
        out["error"] = "no slack_bot_token configured"
        return out

    token = bot_token()
    req = urllib.request.Request(
        API_ROOT + "auth.test", data=b"{}",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            granted = (resp.headers.get("x-oauth-scopes") or "").strip()
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        out["error"] = _scrub(repr(e))
        return out

    if not data.get("ok"):
        out["error"] = _scrub(data.get("error", "unknown"))
        return out

    out["valid"] = True
    out["team"] = data.get("team")
    out["user"] = data.get("user")
    out["scopes"] = [s for s in granted.split(",") if s] if granted else []
    if out["scopes"]:
        out["missing_scopes"] = [s for s in REQUIRED_SCOPES
                                 if s not in out["scopes"]]
    return out
