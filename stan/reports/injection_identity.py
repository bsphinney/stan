"""Who an injection was: vial, sample name and time, from a run name or PG."""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta, timezone

from stan.instrument_time import parse_local

logger = logging.getLogger(__name__)

#: HyStar appends ``_<vial>_<n>_<id>.d`` to the sample name.
_HYSTAR_SUFFIX = re.compile(r"_(S\d+-[A-H]\d+)_\d+_\d+\.d$")


def sample_from_run_name(run_name: str | None) -> tuple[str | None, str | None]:
    """Split a run name into ``(sample, vial)``.

    Unrecognised names keep the name (minus ``.d``) and have no vial.
    """
    if not run_name:
        return None, None
    m = _HYSTAR_SUFFIX.search(run_name)
    if m:
        return run_name[:m.start()], m.group(1)
    name = run_name[:-2] if run_name.endswith(".d") else run_name
    return name, None


def identity_line(vial: str | None, sample: str | None, at: datetime | None) -> str:
    """``"{vial} · {sample} · {HH:MM}"``, leaving out whatever is missing."""
    parts = [p for p in (vial, sample) if p]
    if at is not None:
        parts.append(f"{at:%H:%M}")
    return " · ".join(parts)


def find_run_near(vial: str | None, at_local: datetime,
                  window_s: int = 180) -> str | None:
    """The ``runs.run_name`` nearest ``at_local`` (instrument time), or None.

    Matches the vial too when one is given. Only asks PG when
    ``STAN_DB_BACKEND=pg``; any PG error is logged at debug and gives None.
    """
    if os.environ.get("STAN_DB_BACKEND", "").lower() != "pg":
        return None
    try:
        at = parse_local(at_local)
        if at is None:
            return None
        at_utc = at.astimezone(timezone.utc)
        lo, hi = at_utc - timedelta(seconds=window_s), at_utc + timedelta(seconds=window_s)
        sql = ("SELECT run_name FROM runs WHERE run_date BETWEEN %s AND %s")
        args: list = [lo, hi]
        if vial:
            sql += " AND run_name LIKE %s"
            args.append(f"%_{vial}_%")
        sql += " ORDER BY abs(extract(epoch from run_date - %s)) LIMIT 1"
        args.append(at_utc)

        from stan.db_pg import _connect

        with _connect() as pg, pg.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
        return row[0] if row else None
    except Exception:
        logger.debug("find_run_near failed", exc_info=True)
        return None
