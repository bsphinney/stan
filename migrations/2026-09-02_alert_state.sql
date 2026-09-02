-- Alert de-duplication state (STAN Slack instrument alerts)
--
-- WHY. A column clogged on the night of 2026-08-31 and nobody was told. The
-- fix has two halves: something must run on a schedule, and it must reach
-- Slack. This table is what stops the second half from becoming its own
-- problem.
--
-- The 2026-08-31 clog spanned seven consecutive runs; the 2026-08-28 one ran
-- fourteen hours. One Slack message per flagged run would have meant dozens
-- of pings for one event, and a channel that gets dozens of pings for one
-- event is a channel nobody reads -- at which point the real alert is missed
-- for the same reason it was missed before, just noisier.
--
-- So the alerter keys on the CONDITION rather than the observation, and this
-- table remembers when each key was last sent. Re-sending happens only on a
-- signature change (the condition got worse) or after a cool-off (it is still
-- true half a day later). See stan/notify.py::should_send.
--
-- WHY PG AND NOT SQLITE. STAN has just finished moving off the SQLite file on
-- Quobyte after five corruptions from concurrent writers (2026-05-11,
-- 2026-06-10, 2026-08-26 x2, 2026-09-01). An alerter whose memory is corrupt
-- either goes silent or spams; both are worse than no alerter. The code falls
-- back to a JSON file when this table is missing, so alerting works before
-- this migration is applied -- but PG is where it belongs.
--
-- SHAPE. One row per alert key. `signature` is deliberately coarse (a severity
-- plus a 10-point pressure band) so a clog getting worse re-alerts while
-- pressure jittering by 2 bar does not. `detail` is jsonb because the shape of
-- what an alert carries changes as the extractors improve, and a wide
-- relational schema would need a migration every time a new signal is added --
-- the same reasoning as instrument_telemetry_cache.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no
-- DDL on schema public. See docs/PG_FARM_ACCESS.md.
--
--   export PGPASSWORD="$(pgfarm auth token)"
--   python scripts/apply_pg_migration.py migrations/2026-09-02_alert_state.sql --user brettsp

BEGIN;

CREATE TABLE IF NOT EXISTS alert_state (
    alert_key   text        PRIMARY KEY,   -- the condition, not the observation
    first_seen  timestamptz NOT NULL DEFAULT now(),
    last_sent   timestamptz NOT NULL DEFAULT now(),
    n_sent      integer     NOT NULL DEFAULT 1,
    signature   text        NOT NULL DEFAULT '',  -- coarse state; a change re-alerts
    kind        text,                             -- clog | overpressure | evotip | ...
    instrument  text,
    detail      jsonb
);

-- Only used for housekeeping ("what has fired lately"); the hot path is the
-- primary key lookup.
CREATE INDEX IF NOT EXISTS idx_alert_state_last_sent
    ON alert_state (last_sent DESC);

COMMENT ON TABLE alert_state IS
    'Last-sent state per alert key, so an instrument fault that spans dozens '
    'of runs produces one Slack message rather than dozens. Written by '
    'stan.notify.AlertStore; falls back to a JSON file when unavailable.';

GRANT SELECT, INSERT, UPDATE, DELETE ON alert_state
    TO "genome-proteomics-service-account";

COMMIT;
