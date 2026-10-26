"""The instruments' clock, and reading naive local timestamps against it."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

#: The instruments' clock. Both documents write naive local times, which
#: `_parse` calls UTC -- harmless when they are compared with each other, but
#: an AGE measured against the real `now` came out 7-8 h too old.
#:
#: Windows has no tz database of its own; pyproject installs ``tzdata`` there.
#: Without it the clock falls back to UTC, quietly at import (stan.notify
#: imports this module, so a WARNING here would print on every command), and
#: says so once, at WARNING, when an instrument-local time is first read.
try:
    INSTRUMENT_TZ = ZoneInfo("America/Los_Angeles")
    _TZ_FALLBACK = False
except ZoneInfoNotFoundError:   # no tz database on this host
    logger.debug("no tz database; instrument times will be read as UTC")
    INSTRUMENT_TZ = timezone.utc
    _TZ_FALLBACK = True
_fallback_warned = False


def _warn_fallback_once() -> None:
    global _fallback_warned
    if _TZ_FALLBACK and not _fallback_warned:
        _fallback_warned = True
        logger.warning("no tz database (pip install tzdata); reading instrument "
                       "times as UTC, which ages both feeds by 7-8 h")


def parse_raw(ts) -> datetime | None:
    """Parse a timestamp the documents use, keeping its zone -- usually none."""
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    s = str(ts).strip().replace("Z", "+00:00")
    for parse in (
        lambda v: datetime.fromisoformat(v),
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M"),
        lambda v: datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),
    ):
        try:
            return parse(s)
        except (ValueError, TypeError):
            continue
    return None


def parse_local(ts: str | datetime | None) -> datetime | None:
    """Parse ``ts``; a naive time is instrument-local, an aware one is kept.

    Only for measuring an age against the real clock. The failure checks keep
    the UTC reading, because their alert keys are built from the parsed time
    and a shifted key would re-send every event already reported.

    An ambiguous local time (DST fall-back, the repeated hour) takes the
    later offset (``fold=1``), so a fresh event is never aged by an extra hour.
    """
    dt = parse_raw(ts)
    if dt is None:
        return None
    if dt.tzinfo:
        return dt
    _warn_fallback_once()
    return dt.replace(tzinfo=INSTRUMENT_TZ, fold=1)
