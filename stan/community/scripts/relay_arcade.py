"""Reference implementation for the arcade leaderboard relay endpoints.

THIS FILE IS NOT DEPLOYED TO THE HF SPACE — it is documentation for Brett
to hand-port into the HF Space ``app.py``.  Read the API contract section
at the bottom and paste the relevant FastAPI routes into app.py.

=============================================================================
API CONTRACT — paste into brettsp/stan HF Space app.py
=============================================================================

Dependencies already present in the HF Space (no new installs needed):
  fastapi, pydantic, huggingface_hub, pyarrow, polars, pandas

## POST /api/arcade/submit

Submit a game score to the community leaderboard.

Request JSON (ArcadeScoreIn):
{
  "game":              str,          # "keratin_invaders" | "angry_specs" | "mzork"
  "score":             float,        # Points achieved (higher = better for all games)
  "won":               bool,         # Whether the player won (m/zork only; ignored otherwise)
  "display_name":      str,          # Lab pseudonym from stan setup (max 64 chars)
  "instrument_family": str,          # "timsTOF" | "Exploris" | "Lumos" | "Astral" | ...
  "stan_version":      str,          # e.g. "0.2.353"
  "achieved_at":       str           # ISO-8601 UTC timestamp
}

Optional header: X-STAN-Auth: <auth_token>  (soft rate-limit bypass when verified)

Response JSON on success (HTTP 200):
{
  "status":  "accepted",
  "rank":    int | null              # Current rank of this score (null if not yet computed)
}

Response JSON on error:
{
  "detail": str                      # Human-readable reason
}

HTTP error codes:
  400  Validation error (unknown game, score out of range, display_name too long)
  422  Pydantic parse error
  429  Rate-limited (>5 submissions per display_name per game per hour)
  500  Storage failure

## GET /api/arcade/leaderboard

Fetch the top scores for one game.

Query parameters:
  game   str   required — "keratin_invaders" | "angry_specs" | "mzork"
  limit  int   optional — default 10, max 50

Response JSON (HTTP 200):
{
  "game":   str,
  "scores": [
    {
      "display_name":      str,
      "score":             float,
      "won":               bool,
      "instrument_family": str,
      "achieved_at":       str,    # ISO-8601 UTC
      "stan_version":      str
    },
    ...
  ]
}

Response JSON on error:
{
  "detail": str
}

=============================================================================
HF DATASET SCHEMA — brettsp/stan-benchmark/arcade/<game>.parquet
=============================================================================

Columns:
  submission_id     str     UUID4 generated server-side
  display_name      str     Lab pseudonym (max 64 chars)
  score             float64 Numeric score
  won               bool    Win/loss flag (mzork); null for other games
  instrument_family str     Broad family — never full model name
  stan_version      str
  achieved_at       str     ISO-8601 UTC
  received_at       str     Server receipt time ISO-8601 UTC
  is_flagged        bool    Set true by the dedup/rate-limit logic

One parquet file per game under:
  brettsp/stan-benchmark/arcade/keratin_invaders.parquet
  brettsp/stan-benchmark/arcade/angry_specs.parquet
  brettsp/stan-benchmark/arcade/mzork.parquet

=============================================================================
PSEUDO-CODE — reference logic for Brett to port into app.py
=============================================================================
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KNOWN_GAMES = {"keratin_invaders", "angry_specs", "mzork"}
MAX_DISPLAY_NAME_LEN = 64
MAX_SCORE = 10_000_000          # Sanity ceiling — raise if a game breaks this
MIN_SCORE = 0
RATE_LIMIT_PER_HOUR = 5        # Submissions per (display_name, game) per hour
LEADERBOARD_MAX_LIMIT = 50
DEDUP_WINDOW_SECONDS = 60      # Same display_name + game + score within 60s = duplicate

# ---------------------------------------------------------------------------
# Pydantic models (FastAPI request/response)
# ---------------------------------------------------------------------------

# from pydantic import BaseModel, Field
# from typing import Optional
#
# class ArcadeScoreIn(BaseModel):
#     game: str
#     score: float
#     won: bool = False
#     display_name: str
#     instrument_family: str = ""
#     stan_version: str = ""
#     achieved_at: str = ""
#
# class ArcadeScoreRow(BaseModel):
#     display_name: str
#     score: float
#     won: bool
#     instrument_family: str
#     achieved_at: str
#     stan_version: str
#
# class LeaderboardResponse(BaseModel):
#     game: str
#     scores: list[ArcadeScoreRow]

# ---------------------------------------------------------------------------
# POST /api/arcade/submit  (pseudo-code)
# ---------------------------------------------------------------------------

def _submit_arcade_score_pseudocode(body_dict: dict) -> dict:
    """Pseudo-code for the relay submit handler.

    Replace with real FastAPI route in app.py:

        @app.post("/api/arcade/submit")
        async def arcade_submit(body: ArcadeScoreIn, request: Request):
            ...
    """
    # import uuid
    # from datetime import datetime, timezone

    game = body_dict.get("game", "")
    score = float(body_dict.get("score", 0))
    display_name = str(body_dict.get("display_name", ""))[:MAX_DISPLAY_NAME_LEN]
    _won = bool(body_dict.get("won", False))
    _instrument_family = str(body_dict.get("instrument_family", ""))
    _stan_version = str(body_dict.get("stan_version", ""))
    _achieved_at = str(body_dict.get("achieved_at", ""))

    # --- Validation ---
    if game not in KNOWN_GAMES:
        # HTTP 400
        return {"detail": f"Unknown game '{game}'. Known: {sorted(KNOWN_GAMES)}"}

    if not (MIN_SCORE <= score <= MAX_SCORE):
        return {"detail": f"Score {score} out of range [{MIN_SCORE}, {MAX_SCORE}]"}

    if len(display_name) == 0:
        display_name = "anonymous"

    # --- Rate limiting ---
    # Check an in-memory or Redis store keyed by (display_name, game)
    # Count submissions in the last 3600 seconds; reject if >= RATE_LIMIT_PER_HOUR
    # if _rate_exceeded(display_name, game):
    #     raise HTTPException(status_code=429, detail="Rate limit: 5 submissions/hour")

    # --- Dedup ---
    # Load the parquet for this game, filter rows where:
    #   display_name == display_name AND game == game AND score == score
    #   AND received_at >= now - DEDUP_WINDOW_SECONDS
    # If any such row exists, return accepted without writing a new row
    # (idempotent — same score within 60s = same session)
    # existing = _find_duplicate(game, display_name, score, DEDUP_WINDOW_SECONDS)
    # if existing:
    #     return {"status": "accepted", "rank": None}

    # --- Write to HF Dataset ---
    # new_row dict to append to HF Dataset parquet (pseudo-code):
    # submission_id = str(uuid.uuid4())
    # received_at = datetime.now(timezone.utc).isoformat()
    # new_row = {
    #     "submission_id": submission_id,
    #     "display_name": display_name,
    #     "score": score,
    #     "won": _won,
    #     "instrument_family": _instrument_family,
    #     "stan_version": _stan_version,
    #     "achieved_at": _achieved_at or received_at,
    #     "received_at": received_at,
    #     "is_flagged": False,
    # }
    # _append_to_hf_dataset("brettsp/stan-benchmark", parquet_path, new_row)

    # Load existing parquet, append new row, upload back to HF Dataset
    # parquet_path = f"arcade/{game}.parquet"
    # _append_to_hf_dataset("brettsp/stan-benchmark", parquet_path, new_row)

    # --- Compute rank ---
    # rank = _compute_rank(game, display_name, score)

    return {"status": "accepted", "rank": None}


# ---------------------------------------------------------------------------
# GET /api/arcade/leaderboard  (pseudo-code)
# ---------------------------------------------------------------------------

def _leaderboard_pseudocode(game: str, limit: int = 10) -> dict:
    """Pseudo-code for the relay leaderboard handler.

    Replace with real FastAPI route in app.py:

        @app.get("/api/arcade/leaderboard")
        async def arcade_leaderboard(game: str, limit: int = 10):
            ...
    """
    if game not in KNOWN_GAMES:
        # HTTP 400
        return {"detail": f"Unknown game '{game}'"}

    limit = min(max(1, limit), LEADERBOARD_MAX_LIMIT)

    # Load parquet for this game from HF Dataset
    # parquet_path = f"arcade/{game}.parquet"
    # df = _load_hf_parquet("brettsp/stan-benchmark", parquet_path)
    #
    # Filter out flagged rows, sort descending by score, deduplicate by
    # display_name (keep best score per lab), take top `limit` rows:
    #
    # df = (
    #     df.filter(~pl.col("is_flagged"))
    #       .sort("score", descending=True)
    #       .unique(subset=["display_name"], keep="first")
    #       .head(limit)
    # )
    #
    # Return:
    # scores = [
    #     {
    #         "display_name": row["display_name"],
    #         "score": row["score"],
    #         "won": row["won"],
    #         "instrument_family": row["instrument_family"],
    #         "achieved_at": row["achieved_at"],
    #         "stan_version": row["stan_version"],
    #     }
    #     for row in df.to_dicts()
    # ]

    scores: list[dict] = []  # placeholder
    return {"game": game, "scores": scores}
