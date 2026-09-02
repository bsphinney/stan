-- Maintenance log: consumable size/spec (STAN v1.0.61)
--
-- WHY. On 2026-09-02 a column was replaced for over-pressure and the
-- restriction did not fully clear until the post-change washes, which left the
-- emitter, glass capillary and fittings as live candidates. STAN could record
-- WHICH parts were changed but nothing about them -- and for anything in the
-- flow path, the one property that drives backpressure is the bore.
--
-- A CaptiveSpray emitter comes in 10 um and 20 um; this lab has used 20 um for
-- several years. Without recording it, a change of emitter size is invisible
-- to the pressure analysis and would read as a column or clog event.
--
-- SHAPE. Deliberately generic rather than `emitter_um`: emitters, capillaries
-- and columns all have a bore, and one column per part type multiplies for no
-- gain. Free text holding a short machine-parseable spec ("20um"), so the
-- pressure analysis can regex a number out of it and the UI can render it.
-- Nullable; every existing row and every event with no consumable is
-- unaffected.
--
-- stan/db.py writes it only when the column exists (see _event_column_exists),
-- so an unmigrated lab keeps logging events normally.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp).
--
--   export PGPASSWORD="$(pgfarm auth token | tail -n1)"
--   python scripts/apply_pg_migration.py migrations/2026-09-02_maintenance_part_spec.sql --user brettsp

BEGIN;

ALTER TABLE maintenance_events ADD COLUMN IF NOT EXISTS part_spec TEXT;

COMMENT ON COLUMN maintenance_events.part_spec IS
    'Size/spec of the consumable this event concerns, e.g. "20um" for a '
    'CaptiveSpray emitter bore. Generic across emitters, capillaries and '
    'columns because bore is what drives backpressure in all three.';

COMMIT;
