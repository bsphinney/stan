-- Maintenance log: the first run acquired on a new column (STAN v1.0.56)
--
-- WHY. The Evosep column-health panel infers when a column went in by looking
-- for a step in the Pump-HP plateau pressure series. That inference is not
-- reliable: on 2026-09-02 three extracts of the SAME instrument on the SAME
-- day, differing only in how much history each covered, put the install at
--
--     2026-08-19   (full document)
--     2026-09-01   (3-day window)
--     2026-07-31   (the truth, per the operator and the run filenames)
--
-- 2026-08-19 was in fact the day a new glass CAPILLARY went in, not a column.
-- Everything downstream of the install date -- runs since change, wear
-- percentage, "column worn" alerting -- was therefore confidently wrong, which
-- is worse than being absent. Alerting on column wear now fails closed unless
-- confidence is real.
--
-- The fix is to stop guessing. An operator logging a column change names the
-- first run acquired on the new column, and the analysis anchors to that exact
-- run. A run number does not drift with the extraction window.
--
-- SHAPE. Free text on purpose: a bare injection counter ("24040") and a full
-- run name ("20260731_HE50_60-spd-dia-new-zdf-column") are both things an
-- operator will reasonably type, and both are resolvable against `runs`.
-- Nullable, so every existing row and every non-column event is unaffected.
--
-- stan/db.py writes this column only when it exists (see _event_column_exists),
-- so an unmigrated lab keeps logging events normally instead of erroring on
-- every save.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no
-- DDL on schema public. See docs/PG_FARM_ACCESS.md.
--
--   export PGPASSWORD="$(pgfarm auth token)"
--   python scripts/apply_pg_migration.py migrations/2026-09-02_maintenance_first_run.sql --user brettsp

BEGIN;

ALTER TABLE maintenance_events ADD COLUMN IF NOT EXISTS first_run TEXT;

COMMENT ON COLUMN maintenance_events.first_run IS
    'For column_change events: the first run acquired on the new column, as a '
    'run name or injection number. Ground truth for anchoring the swap in the '
    'Evosep pressure log, which cannot infer it reliably on its own.';

COMMIT;
