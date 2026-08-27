-- arcade_scores: one leaderboard shared by every STAN  (v1.0.22)
--
-- WHY: each STAN install kept scores to itself, and the community Space's
-- /api/arcade/leaderboard was never deployed (the arcade shows "endpoint not
-- deployed yet (404)" for all four existing games). Putting scores in PG Farm
-- means a local install, the hosted UC Davis dashboard and the community Space
-- all read and write the same board.
--
-- PRIVACY. This table is different from everything else STAN stores: it holds
-- a person's name and affiliation, typed in by them, and it is readable by
-- every lab running STAN. So:
--   * both fields are OPTIONAL — a blank name scores as 'anonymous'
--   * they are free text supplied by a player, therefore UNTRUSTED. The
--     dashboard already had an XSS incident from exactly this shape (see the
--     escapeHtml comment in public/arcade.html): another lab's display_name
--     was rendered into innerHTML. Escape at every interpolation site.
--   * no email, no institutional ID, nothing that identifies someone who did
--     not choose to be identified.
--
-- submitted_by_host records which install posted the row, for moderation and
-- de-duplication. It is NOT shown on the board.

BEGIN;

CREATE TABLE IF NOT EXISTS arcade_scores (
    id                TEXT PRIMARY KEY,          -- uuid hex, generated client-side
    game              TEXT NOT NULL,             -- 'mass_match' | 'core_defense' | ...
    score             BIGINT NOT NULL,
    level             INTEGER,
    won               BOOLEAN,
    player_name       TEXT,                      -- optional, free text, UNTRUSTED
    affiliation       TEXT,                      -- optional, free text, UNTRUSTED
    submitted_by_host TEXT,                      -- provenance; not displayed
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_arcade_game_score ON arcade_scores (game, score DESC);
CREATE INDEX IF NOT EXISTS idx_arcade_created    ON arcade_scores (created_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON arcade_scores TO "genome-proteomics-service-account";

COMMIT;
