-- irt_anchor_rts: cIRT anchor retention times, centrally  (STAN v1.0.21)
--
-- WHY: the "cIRT anchor RT drift" chart reads this table and it exists only
-- in SQLite, so it is empty on every host that didn't personally run the
-- extraction — including the hosted dashboard. Same class as peg_ion_hits
-- and feature_clouds: a per-run detail table the fleet needs centrally.
--
-- No FK to runs(id) here, unlike the SQLite schema. PG holds runs from every
-- host, and anchors may be backfilled for a run in a different order; a hard
-- FK would make the backfill order-dependent for no benefit. The join is
-- still on run_id.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp). See docs/PG_FARM_ACCESS.md.

BEGIN;

CREATE TABLE IF NOT EXISTS irt_anchor_rts (
    run_id           TEXT NOT NULL,
    peptide          TEXT NOT NULL,
    observed_rt_min  DOUBLE PRECISION NOT NULL,
    reference_rt_min DOUBLE PRECISION,
    PRIMARY KEY (run_id, peptide)
);

CREATE INDEX IF NOT EXISTS idx_irt_run ON irt_anchor_rts (run_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON irt_anchor_rts TO "genome-proteomics-service-account";

COMMIT;
