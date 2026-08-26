-- utilization_snapshot: the acquisition-counter output, centrally  (v1.0.19)
--
-- WHY: /api/utilization reads utilization.json off the Quobyte mirror. A
-- hosted dashboard has no Quobyte mount, so the Samples tab's Throughput &
-- utilisation panel reads "not found on the Hive mirror" forever. Same class
-- of bug as the ion cloud reading .features off the local filesystem: an
-- artifact produced on Hive that the serving host can never see.
--
-- A single-row snapshot rather than a normalised table: the payload is the
-- counter's own JSON (per-day and per-hour counts per instrument), it is
-- rewritten wholesale on every run, and nothing queries inside it in SQL.
-- Normalising it would buy nothing and add a migration every time the
-- counter's shape changes.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp). See docs/PG_FARM_ACCESS.md.

BEGIN;

CREATE TABLE IF NOT EXISTS utilization_snapshot (
    id           TEXT PRIMARY KEY,   -- always 'current'; one live snapshot
    generated_at TEXT,
    payload      TEXT NOT NULL       -- the counter's JSON, verbatim
);

GRANT SELECT, INSERT, UPDATE, DELETE ON utilization_snapshot TO "genome-proteomics-service-account";

COMMIT;
