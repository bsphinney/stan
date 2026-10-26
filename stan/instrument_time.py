"""The instruments' clock, and reading naive local timestamps against it."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

#: The instruments' clock. Both documents write naive local times, which
#: `_parse` calls UTC -- harmless when they are compared with each other, but
#: an AGE measured against the real `now` came out 7-8 h too old.
try:
    INSTRUMENT_TZ = ZoneInfo("America/Los_Angeles")
except ZoneInfoNotFoundError:   # no tz database on this host
    logger.warning("no tz database; reading instrument times as UTC, "
                   "which ages both feeds by 7-8 h")
    INSTRUMENT_TZ = timezone.utc


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
    return dt if dt.tzinfo else dt.replace(tzinfo=INSTRUMENT_TZ, fold=1)
