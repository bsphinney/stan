"""Submit arcade game high scores to the community leaderboard via the STAN relay.

Scores are posted to the HF Space relay which handles storage server-side.
No HF token is required on the client. Only the game name, score, pseudonym,
instrument family, and STAN version are transmitted — no raw files, no patient
metadata, no instrument serial numbers.

Opt-in: submission only happens when ``community_submit: true`` is set in
``~/.stan/community.yml``. The additional ``arcade_submit`` flag can be used
to opt in to arcade submissions independently; it defaults to the value of
``community_submit``.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from stan import __version__
from stan.config import load_community

logger = logging.getLogger(__name__)

RELAY_URL = "https://brettsp-stan.hf.space"
_SUBMIT_ENDPOINT = f"{RELAY_URL}/api/arcade/submit"
_LEADERBOARD_ENDPOINT = f"{RELAY_URL}/api/arcade/leaderboard"

# Instrument model → broad family (same mapping as submit._instrument_family,
# reproduced here so arcade_submit has no import dependency on submit.py).
_FAMILY_MAP = {
    "timstof": "timsTOF",
    "tims tof": "timsTOF",
    "astral": "Astral",
    "exploris": "Exploris",
    "lumos": "Lumos",
    "fusion": "Lumos",
    "eclipse": "Eclipse",
    "orbitrap": "Orbitrap",
}


def _instrument_family(model: str) -> str:
    """Map an instrument model string to a privacy-safe family label.

    Returns the broad instrument class rather than the full model name to
    reduce fingerprinting risk. Callers pass the ``instrument`` field from
    ``~/.stan/instruments.yml``; the returned string is what lands in the
    community dataset.

    Args:
        model: Raw instrument model string from instruments.yml.

    Returns:
        Broad family string, e.g. ``"timsTOF"``, ``"Exploris"``, ``"Lumos"``.
        Falls back to the original model string if no known pattern matches.
    """
    model_lower = model.lower()
    for key, family in _FAMILY_MAP.items():
        if key in model_lower:
            return family
    return model


def _is_arcade_submit_enabled(community_config: dict) -> bool:
    """Return True if arcade score submission is enabled.

    ``arcade_submit`` defaults to the value of ``community_submit`` so labs
    that have already opted in to the QC benchmark also get arcade scores
    submitted without extra config. Labs can explicitly set
    ``arcade_submit: false`` to suppress only arcade submissions.

    Args:
        community_config: Loaded dict from ``~/.stan/community.yml``.

    Returns:
        True when arcade submissions should proceed.
    """
    community_on = bool(community_config.get("community_submit", False))
    return bool(community_config.get("arcade_submit", community_on))


def submit_arcade_score(
    game: str,
    score: int | float,
    instrument: str = "",
    won: bool = False,
) -> dict:
    """Submit an arcade high score to the community leaderboard.

    Reads ``display_name`` and opt-in flags from ``~/.stan/community.yml``.
    Never raises — all errors are returned in the ``error`` key so a game-over
    event never crashes the dashboard.

    Privacy guarantees:
    - Only game name, score, won flag, pseudonym, instrument *family*
      (not model), STAN version, and timestamp are transmitted.
    - Raw files are never touched.
    - Patient / sample metadata is never collected.
    - Instrument serial numbers are never sent.

    Args:
        game: Short game identifier matching the relay's known games:
            ``"keratin_invaders"``, ``"angry_specs"``, ``"mzork"``.
        score: Numeric score achieved (integer points or float).
        instrument: Instrument model string from ``instruments.yml``.
            Coerced to a family label before transmission.
        won: Whether the player won (relevant for m/zork).

    Returns:
        ``{"status": "ok"}`` on success, or
        ``{"status": "failed", "error": "<reason>"}`` on any failure.
    """
    try:
        community_config = load_community()
    except Exception as exc:
        logger.debug("arcade_submit: could not load community.yml: %s", exc)
        community_config = {}

    if not _is_arcade_submit_enabled(community_config):
        logger.debug("arcade_submit: disabled (community_submit/arcade_submit false)")
        return {"status": "skipped", "reason": "arcade_submit not enabled"}

    display_name = community_config.get("display_name") or "anonymous"
    auth_token = community_config.get("auth_token", "")

    payload = {
        "game": game,
        "score": score,
        "won": won,
        "display_name": display_name,
        "instrument_family": _instrument_family(instrument),
        "stan_version": __version__,
        "achieved_at": datetime.now(timezone.utc).isoformat(),
    }

    data = json.dumps(payload).encode("utf-8")
    headers: dict[str, str] = {
        "Content-Type": "application/json",
        "User-Agent": f"STAN/{__version__}",
    }
    if auth_token:
        headers["X-STAN-Auth"] = auth_token

    req = urllib.request.Request(_SUBMIT_ENDPOINT, data=data, headers=headers)

    last_err: Exception | None = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result: dict = json.loads(resp.read())
            # Normalize relay "accepted" → client "ok" so callers
            # always see "ok" | "failed" | "skipped".
            return {"status": "ok", "rank": result.get("rank")}
        except urllib.error.HTTPError as exc:
            # HTTP 4xx/5xx: don't retry 4xx (client error), retry once on 5xx
            body = exc.read().decode("utf-8", errors="replace")
            logger.warning("arcade_submit: HTTP %s from relay: %s", exc.code, body[:200])
            if exc.code < 500:
                return {"status": "failed", "error": f"HTTP {exc.code}: {body[:120]}"}
            last_err = exc
        except urllib.error.URLError as exc:
            logger.warning("arcade_submit: network error (attempt %d/2): %s", attempt + 1, exc)
            last_err = exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("arcade_submit: unexpected error: %s", exc)
            return {"status": "failed", "error": str(exc)}

        if attempt == 0:
            time.sleep(2)

    return {"status": "failed", "error": str(last_err)}


def fetch_leaderboard(game: str, limit: int = 10) -> list[dict]:
    """Fetch the top scores for a game from the community relay.

    Args:
        game: Game identifier (e.g. ``"keratin_invaders"``).
        limit: Maximum number of rows to return (default 10).

    Returns:
        List of score dicts ``{display_name, score, instrument_family,
        achieved_at}``, sorted descending by score. Empty list on error.
    """
    url = f"{_LEADERBOARD_ENDPOINT}?game={game}&limit={limit}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data: dict = json.loads(resp.read())
        return data.get("scores") or data.get("leaderboard") or []
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_leaderboard(%s): %s", game, exc)
        return []
