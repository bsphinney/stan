-- runs: four community cohort attributes (STAN v1.2.16, P3a).
--
-- WHY. The community redesign keys nanoLC cohorts by gradient band, LC model
-- and flow regime (spec decision 12), and needs to know where a run's
-- injection amount came from and whether FAIMS was on (spec §A.3 B4, §A.5).
-- UC Davis submissions go out via the Hive cron `stan submit-all --backend pg`
-- (scripts/cron_community_sync.sh), which reads PG, so the values only reach
-- the relay if they are stored here at ingest.
--
--   lc_model      TEXT     canonical LC name from the raw file ("UltiMate
--                          3000", "Evosep One", "Vanquish Neo", ...);
--                          stan.metrics.scoring.detect_lc_model. NULL = not
--                          recognised / not recorded.
--   lc_flow       TEXT     nano | capillary | micro, from dispatch.yml /
--                          instruments.yml `lc_flow`. NULL = not set.
--   amount_source TEXT     declared | parsed | assumed
--                          (stan.community.amount.resolve_amount).
--   faims         INTEGER  1 / 0 / NULL (unknown). INTEGER, not BOOLEAN, to
--                          match SQLite; always compare `faims = 1`.
--
-- Existing rows stay NULL. Nothing is backfilled here: a backfill from the
-- raw files is P4 and needs Brett's go after a before/after.
--
-- SAFE TO APPLY LATE. stan/db_pg.py insert_run_pg reads the live column list
-- (information_schema) and drops keys PG does not have, logging once per
-- process, so Hive ingest keeps working before this runs. After it runs, a
-- process started later writes the four columns.
--
-- EGRESS. New columns change the runs table shape, so the Azure dashboard's
-- PG->SQLite mirror re-fetches runs once on its next tick (~73 MB).
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp); the service account has DML but no
-- CREATE/ALTER on schema public.
--
--   pgfarm auth login
--   export PGPASSWORD="$(pgfarm auth token | tail -n1)"
--   python scripts/apply_pg_migration.py migrations/2026-10-05_runs_lc_faims.sql --user brettsp
--
-- Rehearse first with --dry-run: it runs the ALTERs and rolls back. No
-- BEGIN/COMMIT in this file on purpose -- apply_pg_migration.py runs it in
-- psycopg2's own transaction and commits or rolls back itself; a COMMIT here
-- would commit a --dry-run.

ALTER TABLE runs ADD COLUMN IF NOT EXISTS lc_model      TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS lc_flow       TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS amount_source TEXT;
ALTER TABLE runs ADD COLUMN IF NOT EXISTS faims         INTEGER;
