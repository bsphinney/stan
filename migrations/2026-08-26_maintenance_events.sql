-- maintenance_events on PG Farm  (STAN v1.0.17)
--
-- WHY: the maintenance log is the operator's record of what was physically
-- done to an instrument -- column changes, source cleans, PMs, LC service --
-- and it is what turns "IPS dropped on the 24th" into "because we swapped the
-- column on the 24th". It was SQLite-only and therefore per-machine: the
-- events live on whichever PC logged them. A fleet dashboard (and the
-- forthcoming Azure deployment, which has no Quobyte mount at all) cannot see
-- them. Brett: "the maintenance logs are very important."
--
-- host_origin is deliberately NOT added here. Unlike runs/sample_health, an
-- event is already keyed to a named instrument, and the same instrument can be
-- logged from more than one host (an operator PC and the fleet dashboard),
-- so origin would fragment the very history we are trying to unify.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no
-- CREATE on schema public. See docs/PG_FARM_ACCESS.md.

BEGIN;

CREATE TABLE IF NOT EXISTS maintenance_events (
    id            TEXT PRIMARY KEY,
    instrument    TEXT NOT NULL,
    -- column_change | source_clean | calibration | pm | lc_service | other
    event_type    TEXT NOT NULL,
    event_date    TEXT NOT NULL,
    notes         TEXT DEFAULT '',
    operator      TEXT DEFAULT '',
    -- Column tracking: what was installed at a column_change.
    column_vendor TEXT,
    column_model  TEXT,
    column_serial TEXT
);

CREATE INDEX IF NOT EXISTS idx_me_instrument ON maintenance_events (instrument);
CREATE INDEX IF NOT EXISTS idx_me_date       ON maintenance_events (event_date);

GRANT SELECT, INSERT, UPDATE, DELETE ON maintenance_events TO "genome-proteomics-service-account";

COMMIT;
