"""Injection amount: parse it from a file name, resolve it for a run, and
say when it cannot be trusted.

One module for the ingest paths (watcher, Hive pipeline, ``run_one_v1``)
and the submit path, so the rule that stamps ``amount_source`` at ingest
is the same rule that checks a row before it is sent.

Precedence (Brett, 2026-10-05):

1. a per-run declaration (``stan hive-process --amount-ng``) -> ``declared``
2. a unit-anchored amount in the file name                   -> ``parsed``
3. the instrument default (``hela_amount_ng`` / ``amount_ng``
   in instruments.yml or dispatch.yml)                       -> ``assumed``
4. 50 ng                                                     -> ``assumed``

An instrument default is reported as ``assumed``: it is the lab's standing
value, not something recorded for this run, and the three-value enum
(declared | parsed | assumed) is what the relay accepts.

A conflict between a declared amount and the file name, a file name that
names two different amounts, or an amount above 5,000 ng is never sent
silently: :func:`amount_problem` names it, and
``stan.community.submit.submission_readiness`` holds the run back with
that reason.
"""

from __future__ import annotations

import math
import re

#: The three values ``amount_source`` can take (relay enum, spec §A.5 B4).
AMOUNT_SOURCES: tuple[str, ...] = ("declared", "parsed", "assumed")

#: What a run with no declaration, no unit-anchored token and no
#: instrument default is stamped with (the post-2020 lab convention).
ASSUMED_AMOUNT_NG = 50.0

#: Above this an amount is implausible for a QC injection (D8). 50 µg
#: ("HeL50ug", 10 snapshot rows) is almost certainly a typo for 50 ng.
MAX_PLAUSIBLE_NG = 5000.0

_UNIT_TO_NG = {"ng": 1.0, "ug": 1000.0, "µg": 1000.0, "μg": 1000.0, "mcg": 1000.0}

# A number, an optional single "_", "-" or space, then a mass unit.
#
# * The unit is required. A bare number is never an amount: "HeL50" and
#   "FL20170223_Hela4-cntrl" (replicate 4) carry none. That rule dates from
#   2026-04-30, when an implicit ``hel\d+`` pattern stamped replicate
#   numbers as amounts.
# * µ is U+00B5 (MICRO SIGN, what a keyboard types) and μ is U+03BC (GREEK
#   SMALL LETTER MU, what Unicode NFKC turns it into). Both are accepted.
# * The unit must not be followed by a LOWER-case letter, so "2ugli" (a typo
#   of "ugly" in a real UC Davis name) is not 2 µg. An upper-case letter may
#   follow: "HeLa100ngDIASpc" and "HeL50ngDia" are 100 and 50 ng, and those
#   CamelCase names are common in this lab's archive. The lookahead is case
#   sensitive on purpose; the unit itself is matched case-insensitively.
# * The number must not continue a longer digit run ("(?<![\d.])"), so the
#   whole number is read. "K562100ng" still reads 562,100 ng; nothing in a
#   name can tell that apart, which is what MAX_PLAUSIBLE_NG is for.
_AMOUNT_RE = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?)[_\- ]?((?i:ng|ug|µg|μg|mcg))(?![a-z])"
)


def _amounts_in(name: str) -> list[float]:
    """Every unit-anchored amount in ``name``, in order, in nanograms."""
    out: list[float] = []
    for m in _AMOUNT_RE.finditer(name or ""):
        try:
            value = float(m.group(1))
        except ValueError:
            continue
        out.append(value * _UNIT_TO_NG[m.group(2).lower()])
    return out


def parse_amount_ng(name: str | None) -> float | None:
    """The injection amount a file name states, in nanograms, or None.

    Unit-anchored: ``ng``, ``ug``, ``µg`` (U+00B5), ``μg`` (U+03BC) and
    ``mcg``, case-insensitive, with an optional ``_``, ``-`` or space
    between number and unit. A number with no unit never parses.

    >>> parse_amount_ng("FL271022_FaimHe1ug_CV4680microDia-w6_120m_3.raw")
    1000.0
    >>> parse_amount_ng("Ex041123_HeLa50ng-DiaW45_4ian90m_2ugli.raw")
    50.0
    >>> parse_amount_ng("FL-1MaiMuncitoresc_HeL50_90m.raw") is None
    True

    When a name states more than one amount the first is returned;
    :func:`amount_problem` reports the disagreement.
    """
    found = _amounts_in(name or "")
    return found[0] if found else None


def _same(a: float, b: float) -> bool:
    return math.isclose(float(a), float(b), rel_tol=1e-3, abs_tol=1e-6)


def resolve_amount(
    run_name: str | None,
    declared: float | None = None,
    instrument_default: float | None = None,
) -> tuple[float, str]:
    """The amount to stamp on a run and where it came from.

    Args:
        run_name: The raw file's name (only the name is read).
        declared: A per-run declaration, e.g. ``--amount-ng`` on
            ``stan hive-process``. Wins over everything.
        instrument_default: The instrument's configured amount
            (``hela_amount_ng`` in instruments.yml, ``amount_ng`` in
            dispatch.yml). Used only when the name states no amount.

    Returns:
        ``(amount_ng, amount_source)`` with the source one of
        :data:`AMOUNT_SOURCES`. A declared amount that the file name
        contradicts is still returned as declared; :func:`amount_problem`
        is what refuses to send it.
    """
    if declared is not None and _positive(declared):
        return float(declared), "declared"
    parsed = parse_amount_ng(run_name)
    if parsed is not None:
        return parsed, "parsed"
    if instrument_default is not None and _positive(instrument_default):
        return float(instrument_default), "assumed"
    return ASSUMED_AMOUNT_NG, "assumed"


def _positive(value: object) -> bool:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return math.isfinite(v) and v > 0


def derive_amount_source(run_name: str | None, amount_ng: float | None) -> str:
    """``amount_source`` for a row stored before 1.2.16 recorded one.

    ``parsed`` when the file name states the stored amount, else
    ``assumed`` (the stored value came from a default). A name that states
    a DIFFERENT amount is also ``assumed`` here; :func:`amount_problem`
    reports the conflict.
    """
    parsed = parse_amount_ng(run_name)
    if parsed is not None and amount_ng is not None and _same(parsed, amount_ng):
        return "parsed"
    return "assumed"


def amount_problem(
    run_name: str | None,
    amount_ng: float | None,
    amount_source: str | None = None,
) -> str | None:
    """Why this run's amount must not be sent as it stands, or None.

    * the stored amount is above :data:`MAX_PLAUSIBLE_NG`;
    * the file name states an amount above it;
    * the file name states two different amounts;
    * the file name states an amount that differs from the stored one
      (a declared amount the name contradicts, or an older row stamped
      with a default while its name said otherwise).
    """
    del amount_source  # the rule is the same whatever the source; kept for callers
    found = _amounts_in(run_name or "")
    if amount_ng is not None and _positive(amount_ng) and float(amount_ng) > MAX_PLAUSIBLE_NG:
        return f"amount {_fmt(amount_ng)} ng is above {_fmt(MAX_PLAUSIBLE_NG)} ng"
    if found and max(found) > MAX_PLAUSIBLE_NG:
        return (
            f"file name states {_fmt(max(found))} ng, above "
            f"{_fmt(MAX_PLAUSIBLE_NG)} ng (likely a unit typo)"
        )
    distinct: list[float] = []
    for v in found:
        if not any(_same(v, d) for d in distinct):
            distinct.append(v)
    if len(distinct) > 1:
        return "file name states more than one amount: " + ", ".join(
            f"{_fmt(v)} ng" for v in distinct
        )
    if distinct and amount_ng is not None and not _same(distinct[0], amount_ng):
        return (
            f"amount conflict: file name says {_fmt(distinct[0])} ng, "
            f"the run says {_fmt(amount_ng)} ng"
        )
    return None


def _fmt(v: float) -> str:
    v = float(v)
    return f"{v:,.0f}" if v.is_integer() else f"{v:,.6g}"
