-- Maintenance log: attribution, downtime spans, community-sharing flag
-- (STAN v1.0.34)
--
-- THREE things, all on maintenance_events:
--
-- 1. ATTRIBUTION. The table records `operator` (who did the work, free text)
--    but nothing about who *recorded* the entry or when. On the hosted
--    dashboard the entry is made by a signed-in UC Davis identity, and a
--    maintenance log that cannot say who wrote a line is not much of a log --
--    these entries drive LC-column age, so a wrong one has consequences.
--    created_by holds the Easy Auth principal; created_at is server-side.
--
-- 2. DOWNTIME. An instrument being down is an interval, not an instant.
--    end_date makes any event able to span a period, so 'downtime' is just
--    an event type with an end. Existing rows keep end_date NULL and read
--    exactly as before.
--
-- 3. COMMUNITY SHARING. share_community marks an entry as publishable to the
--    community site (cross-lab "how often does this instrument family need a
--    source clean / how much downtime do people actually see"). It defaults
--    FALSE: maintenance notes can name people and customers, so sharing is
--    opt-in per entry and nothing leaves the lab until someone ticks it.
--    The relay side is not built yet -- this is the flag it will read.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no
-- DDL on schema public. See docs/PG_FARM_ACCESS.md.
--
--   export PGPASSWORD="$(pgfarm auth token)"
--   python scripts/apply_pg_migration.py migrations/2026-08-28_maintenance_downtime.sql --user brettsp

BEGIN;

ALTER TABLE maintenance_events ADD COLUMN IF NOT EXISTS created_by      TEXT;
ALTER TABLE maintenance_events ADD COLUMN IF NOT EXISTS created_at      TIMESTAMPTZ;
ALTER TABLE maintenance_events ADD COLUMN IF NOT EXISTS end_date        TEXT;
ALTER TABLE maintenance_events ADD COLUMN IF NOT EXISTS share_community BOOLEAN NOT NULL DEFAULT FALSE;

-- The calendar reads a date window per instrument.
CREATE INDEX IF NOT EXISTS idx_maintenance_events_window
    ON maintenance_events (instrument, event_date);

GRANT SELECT, INSERT, UPDATE, DELETE ON maintenance_events
    TO "genome-proteomics-service-account";

COMMIT;
