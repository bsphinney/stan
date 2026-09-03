"""`/stan` — instrument status from a phone, over a public HTTP route.

WHAT THIS IS
------------
A Slack slash command. Somebody types `/stan` and gets back the state of the
instrument: how old the column is, what the pressure is doing against what it
should be doing, what was flagged in the last day, and whether an incident is
currently open. It answers the question the alerts cannot — "is it *still*
bad?" — without anyone opening a laptop.

THE SECURITY IS THE FEATURE
---------------------------
Everything else here is a formatting exercise. What matters is that this adds
a **public, unauthenticated POST route to a production app**, and the only
thing standing between it and the internet is the signature check below.
Slack's request signing is what makes that safe, so it is implemented to the
letter:

* **HMAC-SHA256 over ``v0:{timestamp}:{raw body}``**, compared with
  `hmac.compare_digest`. Never `==`: a timing-variable comparison on a
  public endpoint leaks the expected digest a byte at a time.
* **The RAW body bytes**, read once, before any parsing. FastAPI will happily
  hand you a parsed form and let you re-encode it, and the re-encoding will
  differ from what Slack signed (parameter order, escaping) — so the digest
  would be computed over something Slack never sent. Read the bytes, verify
  those bytes, parse afterwards.
* **The timestamp string exactly as sent** goes into the base string. Parsing
  it to an int and formatting it back would silently alter an unusual-but-
  valid value and break verification for no reason; the int is only used for
  the age check.
* **A five-minute replay window.** A captured request is otherwise valid
  forever, and this route is reachable by anyone.
* **A pinned `team_id`.** A signature only proves the request came from an app
  holding the secret. Pinning the workspace means a leaked secret installed
  elsewhere still cannot read this lab's instrument.
* **Fail closed.** No signing secret configured → 404, not 500 and not an
  explanatory error. An install without the feature should be indistinguishable
  from one that has no such endpoint, because an error message that says
  "signing secret not configured" tells an attacker exactly what to look for.
* **Nothing sensitive in the reply.** A Slack channel is a wider and more
  permanent audience than the dashboard: messages are searchable by everyone
  in the workspace forever, and get forwarded. So the reply carries no sample
  names, no submission identifiers and no file paths — in particular NOT
  `sample_impact`, which exists precisely to name whose sample was in the
  column. Counts, pressures, ages and severities only.
* **Nothing sensitive in the logs.** The body, the signature and the secret
  are never logged, at any level. The body is form-encoded user text and the
  signature is a credential-equivalent.

WHY A SEPARATE MODULE
---------------------
So the verification can be tested without a server, and so the security-
critical code is not interleaved with three thousand lines of dashboard
endpoints. `server.py` does one thing: include the router.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

#: The route. Also has to be exempted in `stan.dashboard.readonly`, which
#: refuses every unauthenticated POST — see the note there.
ROUTE = "/api/slack/command"

SIGNING_SECRET_ENV = "STAN_SLACK_SIGNING_SECRET"
TEAM_ID_ENV = "STAN_SLACK_TEAM_ID"

#: Slack's own recommendation. A captured request is replayable until it
#: expires, and this endpoint is reachable by anyone who finds the URL.
MAX_AGE_SECONDS = 300

#: Requests per team per minute. Generous for humans typing a command,
#: tight enough that a leaked secret cannot be used to hammer PG Farm.
#: In-memory and therefore per-worker: this is a courtesy limit against
#: accidents and casual abuse, not a defence against a determined attacker,
#: who is stopped by the signature instead.
RATE_LIMIT_PER_MINUTE = 20

_rate: dict[str, deque] = {}


# ── configuration ────────────────────────────────────────────────


def _from_config(key: str, env: str) -> str | None:
    """Env, then community.yml, then ~/.stan/<key> — as everywhere in STAN."""
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
        path = Path.home() / ".stan" / key
        if path.exists():
            val = path.read_text().strip()
            if val:
                return val
    except OSError:
        pass
    return None


def signing_secret() -> str | None:
    return _from_config("slack_signing_secret", SIGNING_SECRET_ENV)


def allowed_team_id() -> str | None:
    return _from_config("slack_team_id", TEAM_ID_ENV)


# ── verification ─────────────────────────────────────────────────


def verify_signature(secret: str, timestamp: str, raw_body: bytes,
                     signature: str, now: float | None = None) -> bool:
    """Is this genuinely Slack, recently, and unmodified?

    Pure and side-effect free so it can be tested against Slack's own
    published example, which pins the base-string construction rather than
    merely checking that this module agrees with itself.
    """
    if not secret or not timestamp or not signature:
        return False

    now = time.time() if now is None else now
    try:
        age = abs(now - int(timestamp))
    except (TypeError, ValueError):
        return False
    if age > MAX_AGE_SECONDS:
        return False

    # The timestamp goes in as the exact string received; int() above is only
    # for the age comparison.
    base = b"v0:" + timestamp.encode() + b":" + raw_body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _rate_ok(team: str, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    window = _rate.setdefault(team, deque())
    while window and now - window[0] > 60:
        window.popleft()
    if len(window) >= RATE_LIMIT_PER_MINUTE:
        return False
    window.append(now)
    return True


# ── the answer ───────────────────────────────────────────────────


def _load_document() -> dict | None:
    """The published Evosep document. PG first, bundled file second.

    Deliberately the SAME document `/api/maintenance/evosep` already serves
    anonymously, so this route exposes nothing new — it reformats what a
    browser can already fetch. Reading one cached PG row keeps the whole
    request inside Slack's 3-second budget without needing the deferred
    `response_url` dance.
    """
    try:
        from stan.db_pg import get_evosep_column_health_pg, use_pg

        if use_pg():
            doc = get_evosep_column_health_pg()
            if doc:
                return doc
    except Exception:  # noqa: BLE001 — fall through to the file cache
        logger.warning("Slack command: PG read failed", exc_info=True)
    try:
        import json

        from stan.config import resolve_config_path

        with open(resolve_config_path("evosep_column_health.json")) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


def _fmt(v, unit: str = "", nd: int = 0) -> str:
    if v is None:
        return "unknown"
    try:
        return f"{float(v):.{nd}f}{unit}"
    except (TypeError, ValueError):
        return str(v)


def build_status(doc: dict | None, now: float | None = None) -> str:
    """The reply text. Carries no sample, submission or path — see module docs.

    Written to be read on a phone: the first line answers "is it bad", and
    everything after it is why.
    """
    if not doc:
        return (":grey_question: No column-health document has been published "
                "yet. Check the Maintenance tab on the dashboard.")

    import datetime as _dt

    now = time.time() if now is None else now
    col = doc.get("column") or {}
    summary = doc.get("summary") or {}
    flags = doc.get("flags") or []
    host = str(doc.get("instrument_host") or "the instrument")

    # Last 24 h of flagged runs, counted only. Run names are method+timestamp
    # rather than sample names, but they are still identifiers and add nothing
    # a count does not.
    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=24)
    recent, worst_pct, at_ceiling, criticals = [], None, 0, 0
    for f in flags:
        try:
            t = _dt.datetime.fromisoformat(str(f.get("start")).replace("Z", "+00:00"))
            if t.tzinfo is None:
                t = t.replace(tzinfo=_dt.timezone.utc)
        except (TypeError, ValueError):
            continue
        if t < cutoff:
            continue
        recent.append(f)
        if f.get("severity") == "critical":
            criticals += 1
        if "ceiling" in (f.get("kinds") or []):
            at_ceiling += 1
        pct = f.get("pct_over_expected")
        if pct is None:
            pct = f.get("pct_over_baseline")
        if pct is not None and (worst_pct is None or pct > worst_pct):
            worst_pct = pct

    if criticals:
        head = f":red_circle: *{host}* — {criticals} critical run(s) in the last 24 h"
    elif recent:
        head = f":large_orange_diamond: *{host}* — {len(recent)} flagged run(s) in the last 24 h"
    else:
        head = f":white_check_mark: *{host}* — nothing flagged in the last 24 h"

    lines = [head, ""]

    # Column age. Only stated when the document vouches for it: the install
    # date is inferred when it is not logged, and a confidently wrong age is
    # worse than none. Same gate the wear alert uses.
    conf = str(col.get("confidence") or "")
    if col.get("known") and conf in ("logged", "inferred") and not col.get("installed_is_lower_bound"):
        age = f"{_fmt(col.get('days_since'), ' days', 1)}, {col.get('injections_since', '?')} injections"
        if not col.get("log_covers_install") or col.get("counts_are_lower_bounds"):
            age += " (at least — the log does not reach the install)"
        lines.append(f"*Column:* {age}  _({conf})_")
    else:
        lines.append("*Column:* age not verifiable from the logs")

    # Pressure now, against the column's own expectation where there is one.
    latest = None
    for r in (doc.get("runs") or []):
        if r.get("plateau_bar") is not None:
            latest = r
    if latest:
        line = f"*Plateau:* {_fmt(latest.get('plateau_bar'), ' bar')}"
        exp = latest.get("expected_plateau_bar")
        pct = latest.get("pct_over_expected")
        if exp is not None and pct is not None:
            line += f" vs {_fmt(exp, ' bar')} expected ({pct:+.0f}%)"
        elif latest.get("expected_unavailable"):
            line += " (no expectation yet for this column)"
        ceiling = doc.get("ceiling_bar")
        if ceiling:
            line += f", pump limit {_fmt(ceiling, ' bar')}"
        lines.append(line)

    if recent:
        bits = [f"{len(recent)} flagged"]
        if criticals:
            bits.append(f"{criticals} critical")
        if at_ceiling:
            bits.append(f"{at_ceiling} at the pump ceiling")
        if worst_pct is not None:
            bits.append(f"worst {worst_pct:+.0f}% over")
        lines.append("*Last 24 h:* " + ", ".join(bits))
        newest = max(recent, key=lambda f: str(f.get("start")))
        lines.append(f"*Open episode:* yes — {newest.get('method', '?')}, "
                     f"most recent flag {str(newest.get('start'))[:16].replace('T', ' ')}")
    else:
        lines.append("*Open episode:* none")

    lines.append("")
    lines.append(f"_Published {str(doc.get('generated_at') or 'unknown')[:19].replace('T', ' ')} · "
                 f"{summary.get('n_runs', '?')} runs analysed_")
    return "\n".join(lines)


# ── the route ────────────────────────────────────────────────────


@router.post(ROUTE)
async def slack_command(request: Request):
    """Handle `/stan`. Unauthenticated in the platform sense; see module docs."""
    secret = signing_secret()
    if not secret:
        # Fail closed and look like nothing is here. Never explain.
        raise HTTPException(status_code=404, detail="Not Found")

    # RAW bytes, once, before anything parses them.
    raw = await request.body()
    ts = request.headers.get("X-Slack-Request-Timestamp") or ""
    sig = request.headers.get("X-Slack-Signature") or ""

    if not verify_signature(secret, ts, raw, sig):
        # No detail: a caller who cannot sign gets nothing to calibrate
        # against, and the reason is in our logs, not theirs.
        logger.warning("Slack command: signature rejected from %s",
                       getattr(request.client, "host", "?"))
        raise HTTPException(status_code=401, detail="Unauthorized")

    # Parse only after the bytes are proven authentic.
    from urllib.parse import parse_qs

    form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
    team = form.get("team_id") or ""

    expected_team = allowed_team_id()
    if expected_team and not hmac.compare_digest(team, expected_team):
        logger.warning("Slack command: refused a valid signature from team %r",
                       team[:16])
        raise HTTPException(status_code=403, detail="Forbidden")

    if not _rate_ok(team or "unknown"):
        logger.warning("Slack command: rate limited team %r", team[:16])
        return JSONResponse({"response_type": "ephemeral",
                             "text": "Slow down a moment — try again shortly."})

    text = build_status(_load_document())
    # ephemeral: the reply goes to whoever typed it, not the whole channel.
    return JSONResponse({"response_type": "ephemeral", "text": text})
