"""Share per-run PEG measurements with the community PEG board.

This is the client half of the dedicated PEG channel on the HF relay
(``POST /api/peg/submit``, design spec §4.4). It is deliberately separate
from :mod:`stan.community.submit`: PEG is read from raw MS1 and needs no
search, so a lab can share it without the frozen DIA-NN community search,
and a whole history goes up in a handful of requests instead of one
``/api/update`` commit per row against HF's 256 commits/hour.

What leaves the lab, per run: instrument model and family, LC group
(``evosep`` / ``other``), SPD, acquisition mode, sample type, amount, the
four PEG fields and a ``run_key``. The run name is used only to derive
``run_key`` (a truncated sha256) and is never sent. Raw files, sample
metadata and serial numbers are never read here.

The client is stateless and idempotent: every sync resends every
shareable run and the relay skips records identical to what it holds,
so no PG column (and no owner DDL) is needed to remember what was sent.

Sharing is opt-in: ``peg_share: true`` in ``~/.stan/community.yml`` or
``STAN_PEG_SHARE=1``. With neither set, :func:`sync_peg` returns before
touching the database or the network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import stat
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

import httpx
import yaml

from stan import __version__
from stan.community.submit import RELAY_URL, _detect_sample_type, _instrument_family

# run_key hashes basename(run_name), and the PEG readers de-duplicate on the
# same basename (peg_trends.acquisition_key); ranking copies uses the readers'
# own canonical_rank. One definition of each, so the tab and the board cannot
# disagree about which rows are one acquisition or which copy of it counts.
from stan.metrics.peg_trends import canonical_rank, run_basename  # noqa: F401 - re-exported

logger = logging.getLogger(__name__)

PEG_METHOD = "stan-peg-1"  # 60-ion PEG1-20 x H/NH4/Na panel, 5 ppm, 1e4 floor, 80 MS1 scans
BATCH_SIZE = 2000  # the relay's per-request cap (spec §4.5)
REAL_PEG_CLASSES = frozenset({"clean", "trace", "moderate", "heavy"})

# runs.lc_system holds "evosep", "custom" or NULL/"" (detect_lc_system in
# stan/metrics/scoring.py). The relay knows only "evosep" and "other". A run
# whose LC was never detected is NOT guessed into either group: the Evosep vs
# other-LC panel is the whole reason other LCs are shared, and a mislabelled
# run would land on the wrong side of it.
LC_MAP = {"evosep": "evosep", "custom": "other"}

# runs holds at least one 1980 row (a dead instrument-PC clock); the relay
# refuses dates more than a day in the future. Dropping both here keeps them
# out of the relay's rejected list, which is reserved for surprises.
MIN_RUN_DATE = datetime(2015, 1, 1, tzinfo=timezone.utc)
FUTURE_SLACK = timedelta(days=1)

REQUEST_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
RETRY_BACKOFF_S = 5.0
MAX_RETRY_WAIT_S = 60.0

# Identity-level refusals: every batch in the sync would get the same answer,
# so the first one stops the run rather than repeating it N times.
_FATAL_STATUSES = frozenset({400, 401, 403, 404, 405})

CLAIM_HINT = (
    "The lab name is claimed and the relay did not accept this install's "
    "auth_token. Run `stan community-claim` to re-verify by email and store "
    "a fresh token."
)

# Sharing under a name with no auth_token works -- the relay accepts
# unclaimed names -- but whoever claims the name first owns it: its rows then
# need their token, and this lab's unverified rows drop off the board. The
# relay's join card says so; a lab that only ever runs the cron sees this.
UNCLAIMED_HINT = (
    "'{name}' is unclaimed: anyone can claim it and take its place on the "
    "board. Run `stan community-claim` first."
)

_TRUTHY = frozenset({"1", "true", "yes", "on"})

# Seam for tests: retry backoff must not sleep in the suite.
_sleep = time.sleep

_DATE_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d+)?)?)?"
    r"\s*(Z|z|[+-]\d{2}(?::?\d{2})?)?$"
)


# ── Record building ─────────────────────────────────────────────────────

def utc_iso(value: Any) -> str | None:
    """Normalise a ``runs.run_date`` value to ``YYYY-MM-DDTHH:MM:SSZ``.

    PG hands back an aware ``datetime`` (``timestamptz``); SQLite hands back
    TEXT, and the same instant is stored both as ``...T11:47:26-08:00`` and
    ``...T19:47:26+00:00``. Both must produce the same string, or the same
    run gets two ``run_key`` values and is counted twice on the board.
    Naive values are taken as UTC, which is how PG Farm stores them.

    Args:
        value: ``datetime`` or ISO-8601-like string.

    Returns:
        The UTC timestamp truncated to seconds, or None if unparseable.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        m = _DATE_RE.match(value.strip())
        if not m:
            return None
        y, mo, d, hh, mi, ss, tz = m.groups()
        try:
            dt = datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0))
        except ValueError:
            return None
        if tz and tz not in ("Z", "z"):
            sign = 1 if tz[0] == "+" else -1
            digits = tz[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:4] or 0))
            dt = dt.replace(tzinfo=timezone(sign * offset))
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def compute_run_key(instrument_model: str, run_name: str, run_date_utc_iso: str) -> str:
    """Anonymous, stable id of one acquisition (spec §4.4).

    Args:
        instrument_model: ``runs.instrument``, e.g. ``"timsTOF HT"``.
        run_name: Run name or path; only its basename is hashed.
        run_date_utc_iso: Output of :func:`utc_iso`.

    Returns:
        First 24 hex characters of
        ``sha256(f"{instrument_model}|{basename(run_name)}|{run_date_utc_iso}")``.
    """
    raw = f"{instrument_model.strip()}|{run_basename(run_name)}|{run_date_utc_iso}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _finite(value: Any) -> float | None:
    """Float value, or None for NULL, NaN, inf and non-numbers."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _is_blank_or_wash(run_name: str) -> bool:
    """The blank/wash name filter shared with the PEG tab (spec §4.1)."""
    from stan.metrics.peg_trends import is_blank_or_wash

    return bool(is_blank_or_wash(run_name))


def _record_or_reason(row: Mapping[str, Any], now: datetime) -> tuple[dict | None, str]:
    """Build one share record, or name the first reason the row is not shared."""
    run_name = str(row.get("run_name") or "")
    # The reader already filters hidden = 0; this is the belt to its braces.
    # PG gives an integer, SQLite may give "0" -- which is truthy as a string.
    hidden = row.get("hidden")
    if hidden is True or (_finite(hidden) or 0.0) != 0.0:
        return None, "hidden"
    if not run_basename(run_name):
        return None, "no_run_name"
    if _is_blank_or_wash(run_name):
        return None, "blank_or_wash"

    # Real-PEG filter (spec §4.1). 'unknown' is the reader-failure sentinel,
    # written with peg_score=0.0 -- indistinguishable from a perfectly clean
    # run by score alone, so the class is the only thing that tells them apart.
    peg_class = str(row.get("peg_class") or "").strip().lower()
    if peg_class == "unknown":
        return None, "peg_unknown"
    score = _finite(row.get("peg_score"))
    pct = _finite(row.get("peg_intensity_pct"))
    ions = _finite(row.get("peg_n_ions_detected"))
    if peg_class not in REAL_PEG_CLASSES or score is None or pct is None or ions is None:
        return None, "no_peg"
    if not (0.0 <= pct <= 100.0 and 0.0 <= score <= 100.0 and 0 <= ions <= 500):
        return None, "peg_out_of_range"

    lc = LC_MAP.get(str(row.get("lc_system") or "").strip().lower())
    if lc is None:
        return None, "lc_unknown"

    instrument = str(row.get("instrument") or "").strip()
    if not instrument:
        return None, "no_instrument"

    run_date = utc_iso(row.get("run_date"))
    if run_date is None:
        return None, "bad_date"
    when = datetime.strptime(run_date, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if when < MIN_RUN_DATE or when > now + FUTURE_SLACK:
        return None, "bad_date"

    spd = _finite(row.get("spd"))
    if spd is None or not (1 <= spd <= 2000):
        return None, "no_spd"

    amount = _finite(row.get("amount_ng"))
    sample_type = str(row.get("sample_type") or "").strip().lower() or _detect_sample_type(run_name)

    return {
        "run_key": compute_run_key(instrument, run_name, run_date),
        "run_date": run_date,
        "instrument_family": _instrument_family(instrument),
        "instrument_model": instrument,
        "lc_system": lc,
        # Not stored in the DB today (spec §2 out of scope); the field exists
        # so a lab that knows its LC model can send it later.
        "lc_model": None,
        "spd": int(round(spd)),
        "acquisition_mode": str(row.get("mode") or "").strip().lower(),
        "sample_type": sample_type,
        # submit-all's default for a NULL amount (cli.py submit_all), so the
        # two community channels describe the same run the same way.
        "amount_ng": amount if amount is not None else 50.0,
        # PG stores both as float4, so 34.6 arrives as 34.59999847. Rounding
        # keeps the payload small and, more importantly, keeps a re-sent
        # record byte-identical so the relay's "unchanged" check holds.
        "peg_intensity_pct": round(pct, 4),
        "peg_score": round(score, 3),
        "peg_n_ions_detected": int(ions),
        "peg_class": peg_class,
        "peg_method": PEG_METHOD,
    }, ""


def _processing_rank(row: Mapping[str, Any], rec: Mapping[str, Any]) -> tuple:
    """Order duplicate rows of one acquisition; the highest is the one shared.

    The PEG readers already return one row per acquisition, chosen by
    ``peg_trends.canonical_rank`` (stored ion hits, then the newest
    ``stan_version`` by number, then the highest ``id``). This is the second
    line of defence and ranks by exactly those keys: any other order would
    share a different copy from the one the PEG tab counts the moment two
    got through. The record itself is the last key, so the choice is still
    independent of row order when a caller supplies none of those columns.
    """
    return (*canonical_rank(dict(row)), json.dumps(rec, sort_keys=True, default=str))


def build_peg_records(
    rows: Iterable[Mapping[str, Any]],
    stan_version: str | None = None,
) -> tuple[list[dict], dict[str, int]]:
    """Turn ``get_peg_share_rows()`` output into relay share records.

    Every row either becomes exactly one record or is counted under exactly
    one skip reason: ``hidden``, ``no_run_name``, ``blank_or_wash``,
    ``peg_unknown``, ``no_peg``, ``peg_out_of_range``, ``lc_unknown``,
    ``no_instrument``, ``bad_date``, ``no_spd`` or ``duplicate_run_key``.
    Unmeasured PEG is dropped, never sent as 0.

    When several rows are the same acquisition (one ``run_key``), the one
    shared is chosen by :func:`_processing_rank` -- the readers' own rank --
    never by row order.

    Args:
        rows: Dicts with ``run_name, instrument, run_date, spd, mode,
            amount_ng, lc_system, peg_score, peg_intensity_pct,
            peg_n_ions_detected, peg_class`` and optionally ``sample_type``,
            ``hidden``, and ``has_hits``, ``stan_version`` and ``id`` to rank
            duplicates the way the readers do. None of those three is sent.
        stan_version: Accepted for symmetry with the batch payload, which
            carries the version once per request; records do not repeat it
            (the spec §4.4 record has no version field).

    Returns:
        ``(records, skipped)``: records sorted by ``(run_date, run_key)`` so
        batches are stable from one sync to the next, and a count of
        skipped rows per reason. No record contains the run name.
    """
    del stan_version  # the version travels in the batch envelope, not per record
    now = datetime.now(timezone.utc)
    best: dict[str, tuple[tuple, dict]] = {}
    skipped: Counter[str] = Counter()
    for row in rows:
        rec, reason = _record_or_reason(row, now)
        if rec is None:
            skipped[reason] += 1
            continue
        # The same acquisition sits in runs more than once: re-ingested under
        # a second path, or re-processed (live PG, 2026-09-28: 241 run_keys
        # with 2-5 rows, the PEG values differing in 168 and the class in 78).
        # Neither backend orders the tied rows, so "first one read" was heap
        # order -- and the public value could flip clean <-> heavy after any
        # UPDATE moved a row. One key, one record, picked by what it is.
        rank = _processing_rank(row, rec)
        held = best.get(rec["run_key"])
        if held is not None:
            skipped["duplicate_run_key"] += 1
            if held[0] >= rank:
                continue
        best[rec["run_key"]] = (rank, rec)
    records = [rec for _, rec in best.values()]
    records.sort(key=lambda r: (r["run_date"], r["run_key"]))
    return records, dict(skipped)


# ── Configuration ───────────────────────────────────────────────────────

def load_community_cfg() -> dict:
    """Read community.yml, or ``{}`` when there is none (a fresh install)."""
    from stan.config import load_community

    try:
        return load_community() or {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("could not read community.yml", exc_info=True)
        return {}


def peg_share_enabled(cfg: Mapping[str, Any] | None = None) -> tuple[bool, str]:
    """Whether this install has opted in to PEG sharing, and why.

    Args:
        cfg: Parsed community.yml; read from disk when None.

    Returns:
        ``(enabled, reason)``, the reason in words fit for a log line.
    """
    if cfg is None:
        cfg = load_community_cfg()
    if str(os.environ.get("STAN_PEG_SHARE", "")).strip().lower() in _TRUTHY:
        return True, "STAN_PEG_SHARE is set"
    flag = cfg.get("peg_share")
    if flag is True or str(flag).strip().lower() in _TRUTHY:
        return True, "peg_share: true in community.yml"
    return False, "PEG sharing is off (set peg_share: true in community.yml or STAN_PEG_SHARE=1)"


def resolve_display_name(cfg: Mapping[str, Any] | None = None) -> str:
    """The lab's community name: community.yml first, then STAN_DISPLAY_NAME.

    Same order as the dashboard's community sync (``_load_community_cfg`` in
    stan/dashboard/server.py): the hosted deployment has no community.yml,
    and without the env fallback it would publish under no name at all.

    Args:
        cfg: Parsed community.yml; read from disk when None.

    Returns:
        The stripped name, or ``""``.
    """
    if cfg is None:
        cfg = load_community_cfg()
    name = str(cfg.get("display_name") or "").strip()
    return name or str(os.environ.get("STAN_DISPLAY_NAME") or "").strip()


def _display_name_problem(name: str) -> str:
    """Why a name cannot be shared under, or ``""`` when it can."""
    if not name:
        return "no display_name in community.yml or STAN_DISPLAY_NAME"
    if name.lower() == "anonymous lab":
        # A public ranking needs a lab to rank; the relay refuses it too (400).
        return "'Anonymous Lab' cannot be ranked; run `stan setup` to choose a lab name"
    if len(name) > 60:
        return "display_name is longer than the relay's 60-character limit"
    return ""


def _community_yml_path() -> Path:
    """The community.yml this module writes (the user config dir's)."""
    from stan.config import get_user_config_dir

    return get_user_config_dir() / "community.yml"


def _read_community_mapping(path: Path) -> dict:
    """community.yml as a dict, ``{}`` when absent; raises when it is not one."""
    if not path.exists():
        return {}
    from stan.config import read_config_text

    # BOM-aware: PowerShell 5.1 writes community.yml with a UTF-8 BOM.
    data = yaml.safe_load(read_config_text(path)) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a YAML mapping; refusing to overwrite it")
    return data


def community_yml_problem() -> str:
    """Why :func:`write_community_keys` would fail here, or ``""`` when it would not.

    ``stan community-claim`` asks this *before* the email round trip. The
    relay retires a name's old token the moment it issues the new one, so a
    community.yml found unwritable only afterwards leaves the lab with no
    working token on any machine. A full disk can still fail the write
    itself; this catches what can be known in advance.
    """
    path = _community_yml_path()
    try:
        _read_community_mapping(path)
    except yaml.YAMLError as e:
        first = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return f"{path} is not valid YAML ({first})"
    except (OSError, ValueError) as e:
        return str(e)
    probe = path.parent
    while not probe.exists() and probe != probe.parent:  # mkdir() will create the rest
        probe = probe.parent
    if not os.access(probe, os.W_OK | os.X_OK):
        return f"{probe} is not writable, so {path.name} cannot be updated"
    return ""


def write_community_keys(updates: Mapping[str, Any], set_if_missing: Mapping[str, Any] | None = None) -> Path:
    """Merge keys into the user's community.yml without dropping the others.

    The file holds the auth token (and on Hive the Slack secrets), so it is
    written through a temp file that is owner-only from the moment it exists
    and removed if anything fails. YAML comments do not survive the round
    trip, exactly as with ``stan setup``.

    Args:
        updates: Keys to set unconditionally (e.g. ``auth_token``).
        set_if_missing: Keys to set only when absent or empty.

    Returns:
        Path of the written file.
    """
    path = _community_yml_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_community_mapping(path)
    if path.exists():
        try:
            # Windows installs have been seen with the read-only flag set.
            path.chmod(stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
    for key, value in (set_if_missing or {}).items():
        if not data.get(key):
            data[key] = value
    data.update(updates)
    text = yaml.safe_dump(data, default_flow_style=False, sort_keys=False)

    # tmp.write_text() created the file under the umask -- 0664 on Hive, in a
    # group-writable ~/.stan -- with every secret in it until the chmod, and a
    # write that died on the home quota left it there like that. So the tmp is
    # 0600 from creation, O_EXCL so a stale or planted one (a symlink) is never
    # followed, and it is removed on any failure.
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.unlink()
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path


# ── Sync ────────────────────────────────────────────────────────────────

@contextmanager
def _backend_env(backend: str | None) -> Iterator[None]:
    """Point STAN_DB_BACKEND at ``backend`` for the duration of the read.

    ``use_pg()`` keys off the env var, so this is how ``--backend`` reaches
    ``get_peg_share_rows()``. None leaves whatever the environment says.
    """
    if backend is None:
        yield
        return
    choice = backend.strip().lower()
    if choice not in ("pg", "sqlite"):
        raise ValueError(f"backend must be 'pg' or 'sqlite', got {backend!r}")
    previous = os.environ.get("STAN_DB_BACKEND")
    os.environ["STAN_DB_BACKEND"] = choice
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("STAN_DB_BACKEND", None)
        else:
            os.environ["STAN_DB_BACKEND"] = previous


def _fetch_share_rows() -> list[dict]:
    """Rows from the store of record (backend chosen by STAN_DB_BACKEND)."""
    from stan.db import get_peg_share_rows

    return list(get_peg_share_rows())


def _logs_dir() -> Path:
    """Where CLI jobs write their logs (``~/.stan/logs``, ``~/STAN/logs`` on Windows)."""
    from stan.config import get_user_config_dir

    return get_user_config_dir() / "logs"


def _retry_wait(resp: httpx.Response | None) -> float:
    """Backoff before the single retry, honouring a short Retry-After."""
    if resp is not None:
        try:
            wait = float(resp.headers.get("Retry-After", ""))
            if math.isfinite(wait):
                return max(0.0, min(wait, MAX_RETRY_WAIT_S))
        except ValueError:
            pass
    return RETRY_BACKOFF_S


def _relay_detail(resp: httpx.Response) -> str:
    """The relay's error text: FastAPI's ``detail``, else the body's start."""
    try:
        body = resp.json()
        if isinstance(body, dict) and body.get("detail"):
            return str(body["detail"])
    except ValueError:
        pass
    return resp.text[:200].strip()


def _post_batch(url: str, body: bytes, headers: dict[str, str]) -> dict:
    """POST one batch, retrying once on 429, 5xx or a network error.

    Returns:
        ``{"ok", "http_status", "attempts", "response", "error", "fatal"}``.
        ``fatal`` means every other batch would get the same answer.
    """
    out: dict[str, Any] = {"ok": False, "http_status": None, "attempts": 0,
                           "response": None, "error": "", "fatal": False}
    for attempt in (1, 2):
        out["attempts"] = attempt
        resp: httpx.Response | None = None
        retryable = False
        try:
            resp = httpx.post(url, content=body, headers=headers, timeout=REQUEST_TIMEOUT)
        except httpx.HTTPError as e:
            out["error"] = f"network error: {type(e).__name__}: {e}"
            retryable = True
        except Exception as e:  # a malformed --relay URL, mostly
            out["error"] = f"request failed: {type(e).__name__}: {e}"
            out["fatal"] = True
            return out
        if resp is not None:
            out["http_status"] = resp.status_code
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, dict) and data.get("status") == "ok":
                    out.update(ok=True, response=data, error="")
                    return out
                # A sleeping HF Space answers 200 with an HTML loading page.
                # backfill-tic counted those as successes for a whole summer
                # (cli.py _backfill_tic_impl); only the JSON body counts.
                out["error"] = "relay answered 200 without the expected JSON (Space asleep?)"
                retryable = True
            elif resp.status_code == 429 or resp.status_code >= 500:
                out["error"] = f"HTTP {resp.status_code}: {_relay_detail(resp)}"
                retryable = True
            elif resp.status_code == 403:
                out["error"] = f"HTTP 403: {CLAIM_HINT} (relay: {_relay_detail(resp)})"
                out["fatal"] = True
                return out
            else:
                out["error"] = f"HTTP {resp.status_code}: {_relay_detail(resp)}"
                out["fatal"] = resp.status_code in _FATAL_STATUSES
                return out
        if retryable and attempt == 1:
            wait = _retry_wait(resp)
            logger.warning("PEG sync: %s; retrying once in %.0f s", out["error"], wait)
            _sleep(wait)
    return out


def _exit_code(status: str) -> int:
    """CLI exit code for a sync status (spec §4.4)."""
    return 1 if status in ("failed", "no_display_name", "db_error") else 0


def sync_peg(
    backend: str | None = None,
    dry_run: bool = False,
    relay_url: str | None = None,
) -> dict:
    """Send every shareable PEG run to the relay, in batches of 2000.

    Order of refusal, cheapest first: sharing off returns before any DB read
    or network call; a missing or anonymous name refuses before the read.
    ``dry_run`` builds the records (even with sharing off, so a lab can see
    what it would share before opting in) and sends nothing.

    Writes ``<logs>/peg_sync_<UTC ts>.jsonl``: one line per batch and a
    summary line. Nothing is written when sharing is simply off.

    Args:
        backend: ``"pg"``, ``"sqlite"`` or None to follow STAN_DB_BACKEND.
        dry_run: Build and count, but POST nothing.
        relay_url: Relay base URL; defaults to the public Space.

    Returns:
        Summary dict. ``status`` is one of ``ok``, ``partial``, ``failed``,
        ``dry_run``, ``nothing_to_share``, ``sharing_off``,
        ``no_display_name`` or ``db_error``; ``exit_code`` is what the CLI
        should exit with.
    """
    relay = (relay_url or RELAY_URL).rstrip("/")
    cfg = load_community_cfg()
    enabled, why = peg_share_enabled(cfg)
    display_name = resolve_display_name(cfg)
    result: dict[str, Any] = {
        "status": "", "reason": "", "sharing_enabled": enabled,
        "display_name": display_name, "relay_url": relay, "backend": backend,
        "dry_run": dry_run, "stan_version": __version__,
        "rows_read": 0, "n_records": 0, "skipped": {},
        "batches": 0, "batches_ok": 0, "batches_failed": 0,
        "accepted": 0, "unchanged": 0, "rejected": 0, "rejected_reasons": {},
        "verified": None, "unclaimed": False, "warnings": [],
        "errors": [], "log_path": None, "exit_code": 0,
    }

    def _finish(status: str, reason: str = "") -> dict:
        result["status"] = status
        result["reason"] = reason
        result["exit_code"] = _exit_code(status)
        return result

    if not enabled and not dry_run:
        logger.info("PEG sync skipped: %s", why)
        return _finish("sharing_off", why)

    started = time.monotonic()
    log_dir = _logs_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"peg_sync_{stamp}.jsonl"
    result["log_path"] = str(log_path)

    with open(log_path, "a", encoding="utf-8") as log_fh:
        def _log(record: dict) -> None:
            record["ts"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            log_fh.write(json.dumps(record, default=str) + "\n")
            log_fh.flush()

        def _summary(status: str, reason: str = "") -> dict:
            _finish(status, reason)
            result["elapsed_s"] = round(time.monotonic() - started, 2)
            _log({"event": "summary", **{k: v for k, v in result.items() if k != "log_path"}})
            return result

        name_problem = _display_name_problem(display_name)
        if name_problem and not dry_run:
            logger.warning("PEG sync refused: %s", name_problem)
            result["errors"].append(name_problem)
            return _summary("no_display_name", name_problem)

        # Always sent when present (unlike /api/submit, whose relay auth path
        # was broken until the PEG relay work): a claimed name without its
        # token is refused with 403 on this channel.
        token = str(cfg.get("auth_token") or "").strip()
        if enabled and not token and not name_problem:
            hint = UNCLAIMED_HINT.format(name=display_name)
            result["unclaimed"] = True
            result["warnings"].append(hint)
            logger.warning("PEG sync: %s", hint)

        try:
            with _backend_env(backend):
                rows = _fetch_share_rows()
        except Exception as e:
            msg = f"could not read PEG rows: {type(e).__name__}: {e}"
            logger.warning("PEG sync: %s", msg)
            result["errors"].append(msg)
            return _summary("db_error", msg)

        try:
            records, skipped = build_peg_records(rows, __version__)
        except Exception as e:
            msg = f"could not build PEG records: {type(e).__name__}: {e}"
            logger.warning("PEG sync: %s", msg, exc_info=True)
            result["errors"].append(msg)
            return _summary("failed", msg)
        result.update(rows_read=len(rows), n_records=len(records), skipped=skipped)
        batches = [records[i:i + BATCH_SIZE] for i in range(0, len(records), BATCH_SIZE)]
        result["batches"] = len(batches)

        if dry_run:
            for i, batch in enumerate(batches, 1):
                _log({"event": "batch", "batch": i, "n": len(batch), "dry_run": True})
            reason = "" if enabled else why
            if name_problem:
                result["errors"].append(name_problem)
            return _summary("dry_run", reason)
        if not records:
            return _summary("nothing_to_share", "no run has a real PEG measurement to share")

        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"STAN/{__version__}",
        }
        if token:
            headers["X-STAN-Auth"] = token

        url = f"{relay}/api/peg/submit"
        rejected_reasons: Counter[str] = Counter()
        stop_error = ""
        for i, batch in enumerate(batches, 1):
            if stop_error:
                result["batches_failed"] += 1
                _log({"event": "batch", "batch": i, "n": len(batch), "ok": False,
                      "error": f"not sent: {stop_error}"})
                continue
            body = json.dumps(
                {"display_name": display_name, "stan_version": __version__, "records": batch},
                allow_nan=False,
            ).encode("utf-8")
            outcome = _post_batch(url, body, headers)
            line: dict[str, Any] = {"event": "batch", "batch": i, "n": len(batch),
                                    "ok": outcome["ok"], "http_status": outcome["http_status"],
                                    "attempts": outcome["attempts"]}
            if outcome["ok"]:
                data = outcome["response"]
                rejected = data.get("rejected") or []
                result["batches_ok"] += 1
                result["accepted"] += int(data.get("accepted") or 0)
                result["unchanged"] += int(data.get("unchanged") or 0)
                result["rejected"] += len(rejected)
                if "verified" in data:
                    result["verified"] = bool(data["verified"])
                sample = []
                for rej in rejected:
                    reason = str((rej or {}).get("reason") or "unspecified")
                    rejected_reasons[reason] += 1
                    idx = (rej or {}).get("index")
                    if len(sample) < 50 and isinstance(idx, int) and 0 <= idx < len(batch):
                        sample.append({"index": idx, "run_key": batch[idx]["run_key"],
                                       "run_date": batch[idx]["run_date"], "reason": reason})
                line.update(accepted=data.get("accepted"), unchanged=data.get("unchanged"),
                            rejected=len(rejected), rejected_sample=sample,
                            verified=data.get("verified"))
                if rejected:
                    logger.warning("PEG sync batch %d: relay rejected %d of %d records",
                                   i, len(rejected), len(batch))
            else:
                result["batches_failed"] += 1
                result["errors"].append(f"batch {i}: {outcome['error']}")
                line["error"] = outcome["error"]
                logger.warning("PEG sync batch %d/%d failed: %s", i, len(batches), outcome["error"])
                if outcome["fatal"]:
                    stop_error = outcome["error"]
            _log(line)

        result["rejected_reasons"] = dict(rejected_reasons)
        if result["batches_ok"] == 0:
            return _summary("failed", result["errors"][0] if result["errors"] else "")
        if result["batches_failed"]:
            return _summary("partial", f"{result['batches_failed']} of {len(batches)} batches failed")
        return _summary("ok")
