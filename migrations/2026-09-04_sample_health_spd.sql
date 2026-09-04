-- sample_health.spd — samples per day for non-QC acquisitions.
--
-- The dashboard's TIC overlay filters Sample and Blank traces by SPD,
-- but the column only ever existed on `runs`; the API stubbed NULL for
-- every sample row. Selecting any gradient therefore dropped 100 % of
-- Sample and Blank traces and the panel read "Sample (0)" on a week
-- with several hundred of them.
--
-- Populated per-file at ingest by stan.db._resolve_sample_spd (raw-file
-- metadata, then a filename token). NULL means "could not resolve" and
-- renders as "SPD unknown" — deliberately not backfilled from the
-- instruments.yml cohort default, which would bucket-mix cohorts.
--
-- Backfill existing rows with:  stan fix-sample-spds
--
-- Requires the table OWNER (brettsp via CAS); the service account has
-- DML but no CREATE/ALTER on schema public. See docs/PG_FARM.md.
--   python scripts/apply_pg_migration.py migrations/2026-09-04_sample_health_spd.sql

ALTER TABLE sample_health ADD COLUMN IF NOT EXISTS spd INTEGER;
