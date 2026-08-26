-- PEG/drift detail tables for PG Farm  (STAN v1.0.11)
--
-- WHY: PG holds only `runs`, so the per-run drill-downs the dashboard modals
-- read had no central home. Summary scores (peg_score, drift_class) render
-- fine off the run row, but "DIA window drift breakdown" and the PEG ion
-- ladder came up empty for every Hive-processed run, and the ion cloud never
-- loaded. These three tables are a straight mirror of the SQLite schema.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp). The service account has
-- SELECT/INSERT/UPDATE/DELETE on `runs` but no CREATE on schema public, so it
-- cannot create these itself -- hence the explicit GRANTs at the end.
--
--   PGPASSWORD=... psql "host=pgfarm.library.ucdavis.edu port=5432 \
--     dbname=uc-davis-genome-center-proteomics-core/stan user=brettsp \
--     sslmode=require" -f migrations/2026-08-26_peg_drift_detail_tables.sql

BEGIN;

CREATE TABLE IF NOT EXISTS peg_ion_hits (
    run_id              TEXT NOT NULL,
    source              TEXT NOT NULL,          -- 'runs' | 'sample_health'
    mz                  DOUBLE PRECISION NOT NULL,
    observed_intensity  DOUBLE PRECISION NOT NULL,
    adduct              TEXT NOT NULL,          -- '+H' | '+Na' | '+NH4' | '+K'
    repeat_n            INTEGER NOT NULL,       -- PEG degree of polymerization
    charge              INTEGER NOT NULL DEFAULT 1,
    ppm_error           DOUBLE PRECISION,
    PRIMARY KEY (run_id, source, repeat_n, adduct, charge)
);

CREATE TABLE IF NOT EXISTS drift_window_centroids (
    run_id              TEXT NOT NULL,
    source              TEXT NOT NULL,
    window_idx          INTEGER NOT NULL,
    mz_low              DOUBLE PRECISION NOT NULL,
    mz_high             DOUBLE PRECISION NOT NULL,
    im_low              DOUBLE PRECISION NOT NULL,
    im_high             DOUBLE PRECISION NOT NULL,
    im_center           DOUBLE PRECISION NOT NULL,
    im_mode             DOUBLE PRECISION NOT NULL,
    drift_im            DOUBLE PRECISION NOT NULL,
    coverage            DOUBLE PRECISION NOT NULL,
    -- True iff the window was evaluated for drift (center in peptide zone +
    -- sufficient signal). Tail sentinel rows are stored for viz parity but
    -- excluded from classification aggregation.
    in_peptide_zone     INTEGER DEFAULT 1,
    PRIMARY KEY (run_id, source, window_idx)
);

CREATE TABLE IF NOT EXISTS drift_peak_clouds (
    run_id              TEXT NOT NULL,
    source              TEXT NOT NULL,
    mz                  TEXT NOT NULL,          -- JSON array of floats
    im                  TEXT NOT NULL,          -- JSON array of floats (1/K0)
    log_intensity       TEXT NOT NULL,          -- JSON array of floats
    n_points            INTEGER NOT NULL,
    PRIMARY KEY (run_id, source)
);

CREATE INDEX IF NOT EXISTS idx_peg_hits_run  ON peg_ion_hits (run_id);
CREATE INDEX IF NOT EXISTS idx_drift_win_run ON drift_window_centroids (run_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON peg_ion_hits            TO "genome-proteomics-service-account";
GRANT SELECT, INSERT, UPDATE, DELETE ON drift_window_centroids  TO "genome-proteomics-service-account";
GRANT SELECT, INSERT, UPDATE, DELETE ON drift_peak_clouds       TO "genome-proteomics-service-account";

COMMIT;
