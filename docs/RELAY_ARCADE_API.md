# STAN Arcade — Community Leaderboard Relay Spec

The arcade games (`keratin-invaders`, `angry_specs`, `mzork`) submit
high scores to the public HF Space relay so other STAN users can see
which labs hold the top spot. The dashboard's Arcade tab fetches and
displays the leaderboard at the top of the menu.

This document describes the two endpoints the relay needs to add.
Apply on the Space at <https://huggingface.co/spaces/brettsp/stan>.

---

## 1. `POST /api/arcade/submit`

Receives a high-score submission from a game. Persists per-game best
score per `display_name` (i.e. only update if the new score beats the
existing one for that lab + game pair).

### Request body

```json
{
  "game": "keratin_invaders",       // one of: "keratin_invaders" | "angry_specs" | "mzork"
  "score": 1240,                    // integer — game-specific scoring
  "won": true,                      // bool — required for mzork, optional otherwise
  "level": 3,                       // integer — current level when game ended (optional)
  "display_name": "Clogged PeakTail",  // lab pseudonym from community.yml
  "ts": "2026-04-27T17:30:00Z"      // ISO8601
}
```

### Response

- **200 OK** `{"status": "recorded", "is_new_best": true|false}` — new score accepted (or ignored if not a personal best).
- **400** for invalid `game` or non-numeric `score`.
- **429** if the same `display_name` submits faster than once per 5 seconds (basic abuse guard).

### Storage

Suggested layout in the `brettsp/stan-benchmark` HF Dataset:

```
arcade_scores/
├── keratin_invaders.parquet
├── angry_specs.parquet
└── mzork.parquet
```

Each parquet has columns: `display_name`, `score`, `won`, `level`,
`ts`, `stan_version` (optional). Keep one row per `display_name`
(personal best) — overwrite when a higher score arrives.

---

## 2. `GET /api/arcade/leaderboard?game=<game>&limit=<n>`

Returns the top N scores for a given game, sorted descending.

### Query params

- `game` (required) — `keratin_invaders` | `angry_specs` | `mzork`
- `limit` (optional, default 10, max 50) — number of rows to return

### Response

```json
{
  "game": "keratin_invaders",
  "scores": [
    { "display_name": "Clogged PeakTail",   "score": 1240, "won": false, "level": 3, "ts": "..." },
    { "display_name": "Peptide Wizard",     "score":  920, "won": true,  "level": 2, "ts": "..." },
    ...
  ]
}
```

### Game-specific scoring conventions

| Game | Score meaning | Higher = better? |
|---|---|---|
| `keratin_invaders` | hairs filtered + per-level integrity bonus | yes |
| `angry_specs`      | parts broken × damage + unused-vial bonus | yes |
| `mzork`            | `100 - turns + (50 if grue defeated else 0)` | yes |

---

## Client side (already shipped)

- Games post to relay via `window.parent.postMessage({type:'arcade-score', game, score, won, level})`.
- Parent `arcade.html` relays via `fetch(RELAY + '/api/arcade/submit', {...})`.
- arcade.html calls `GET /api/arcade/leaderboard?game=X&limit=5` for each of the 3 games on tab open.
- Lab identity comes from STAN's local `/api/community/identity` endpoint, which reads `community.yml` `display_name`.

Until the relay endpoints are deployed, the client falls back gracefully:
- "Leaderboard endpoint not deployed yet (404)" message in each game cell on the menu.
- Score submissions silently swallow the network error.

So the arcade UI ships now; turning on real scoring is a one-time relay deployment.
