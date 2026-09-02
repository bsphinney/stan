-- Instrument telemetry caches: Bruker maintenance + Evosep column health
-- (STAN v1.0.52+)
--
-- WHY A TABLE AND NOT A FILE. Both of these are produced on Hive -- one from
-- the timsTOF's own nightly Compass BACKUP (never the live DB, no password),
-- the other from the Evosep's procedure logs. The hosted dashboard runs on
-- Azure and cannot read a Quobyte file, so the first cut shipped the Bruker
-- document bundled in the deploy at config/bruker_maintenance.json. That works
-- but freezes the data at deploy time: the nightly extractor refreshes the
-- file on Hive and the live site never sees it.
--
-- PG Farm is the way across. The extractor upserts here nightly, the endpoint
-- reads here first and falls back to the bundled file, so the hosted panel
-- goes from deploy-frozen to nightly-fresh with no code change.
--
-- ONE ROW PER SOURCE. These are whole documents, not row-per-observation: the
-- shape of the analysis changes as the extractors improve, and versioning a
-- jsonb blob costs nothing while a wide relational schema would need a
-- migration every time a new signal is added. `id = 1` with a CHECK keeps it
-- honestly single-row rather than relying on convention.
--
-- The service account (genome-proteomics-service-account) gets DML only --
-- it can refresh the document but not reshape the table, matching how it is
-- granted on every other STAN table.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no
-- DDL on schema public. See docs/PG_FARM_ACCESS.md.
--
--   export PGPASSWORD="$(pgfarm auth token)"
--   python scripts/apply_pg_migration.py migrations/2026-09-01_instrument_telemetry_cache.sql --user brettsp

BEGIN;

-- timsTOF acquisition health, from the nightly Compass backup.
CREATE TABLE IF NOT EXISTS bruker_maintenance (
    id          integer     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    doc         jsonb       NOT NULL
);

COMMENT ON TABLE bruker_maintenance IS
    'Single-row cache of timsTOF acquisition-health signals extracted on Hive '
    'from the instrument''s own Compass Server backup. Refreshed nightly by '
    'cron_bruker_maintenance.sh; read by /api/maintenance/bruker.';

-- Evosep column health (backpressure trend / clog early warning), from the
-- instrument's procedure logs. Bruker's DB records no pressure at all, so the
-- Evosep logs are the only source of a pressure trace.
CREATE TABLE IF NOT EXISTS evosep_column_health (
    id          integer     PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    doc         jsonb       NOT NULL
);

COMMENT ON TABLE evosep_column_health IS
    'Single-row cache of Evosep One column-health signals (per-run backpressure, '
    'per-method baselines, ageing trend since column change) extracted on Hive '
    'from the Evosep procedure logs. Read by the Maintenance tab.';

-- The extractor writes as the service account; owner keeps DDL.
GRANT SELECT, INSERT, UPDATE, DELETE ON bruker_maintenance
    TO "genome-proteomics-service-account";
GRANT SELECT, INSERT, UPDATE, DELETE ON evosep_column_health
    TO "genome-proteomics-service-account";

COMMIT;
