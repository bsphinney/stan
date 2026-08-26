-- sample_health + health_tic_traces on PG Farm  (STAN v1.0.14)
--
-- WHY: these are the last tables the Hive pipeline still wrote to SQLite.
-- The global stan.db lives on Quobyte, and ~100 concurrent SLURM jobs writing
-- one SQLite file over a network filesystem has corrupted it THREE times
-- (2026-05, 2026-06-10, 2026-08-26). Retry/busy_timeout mitigates contention
-- but cannot prevent SQLITE_IOERR corruption on that storage. Moving the
-- monitor pipeline's writes to PG removes the last concurrent writer.
--
-- SQLite remains fully supported: a single-lab Windows install must stay
-- turnkey without PG Farm, so the routing is conditional on use_pg().
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no
-- CREATE on schema public. See docs/PG_FARM_ACCESS.md.
--
--   export PGPASSWORD="$(pgfarm auth token)"
--   python scripts/apply_pg_migration.py migrations/2026-08-26_sample_health.sql --user brettsp

BEGIN;

CREATE TABLE IF NOT EXISTS sample_health (
    id                       TEXT PRIMARY KEY,
    instrument               TEXT NOT NULL,
    run_name                 TEXT NOT NULL,
    run_date                 TEXT NOT NULL,   -- ISO 8601 from analysis.tdf
    raw_path                 TEXT,
    verdict                  TEXT NOT NULL,   -- pass | warn | fail
    reasons                  TEXT,            -- JSON array, as in SQLite

    -- rawmeat summary, flat for simple charting
    n_ms1_frames             INTEGER,
    n_ms2_frames             INTEGER,
    rt_duration_min          DOUBLE PRECISION,
    ms1_max_intensity        DOUBLE PRECISION,
    ms1_total_tic            DOUBLE PRECISION,
    dynamic_range_log10      DOUBLE PRECISION,
    dropout_rate_per_100_ms1 DOUBLE PRECISION,
    pressure_mean_mbar       DOUBLE PRECISION,
    pressure_range_mbar      DOUBLE PRECISION,
    median_ms1_acc_ms        DOUBLE PRECISION,

    -- PEG + drift for non-QC runs; same semantics as the runs columns so
    -- dashboard code can treat the two row types interchangeably.
    peg_score                DOUBLE PRECISION,
    peg_class                TEXT,
    peg_n_ions_detected      INTEGER,
    peg_intensity_pct        DOUBLE PRECISION,
    drift_coverage           DOUBLE PRECISION,
    drift_median_im          DOUBLE PRECISION,
    drift_p90_abs_im         DOUBLE PRECISION,
    drift_class              TEXT,

    -- Which host produced the row, matching runs.host_origin. A central
    -- store needs this to tell two instruments' rows apart.
    host_origin              TEXT
);

CREATE TABLE IF NOT EXISTS health_tic_traces (
    health_id    TEXT PRIMARY KEY REFERENCES sample_health(id) ON DELETE CASCADE,
    rt_min       TEXT NOT NULL,   -- JSON array of floats
    intensity    TEXT NOT NULL,   -- JSON array of floats
    n_frames     INTEGER,
    bp_intensity TEXT             -- JSON array; Bruker only
);

CREATE INDEX IF NOT EXISTS idx_sh_instrument ON sample_health (instrument);
CREATE INDEX IF NOT EXISTS idx_sh_date       ON sample_health (run_date);
CREATE INDEX IF NOT EXISTS idx_sh_verdict    ON sample_health (verdict);

GRANT SELECT, INSERT, UPDATE, DELETE ON sample_health      TO "genome-proteomics-service-account";
GRANT SELECT, INSERT, UPDATE, DELETE ON health_tic_traces  TO "genome-proteomics-service-account";

COMMIT;
