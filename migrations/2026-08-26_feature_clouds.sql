-- feature_clouds: charge-labelled 4DFF ion cloud, stored centrally (STAN v1.0.15)
--
-- WHY: /api/runs/{id}/features-by-charge opens the 4DFF `.features` sidecar off
-- the LOCAL filesystem. The sidecars exist on Hive next to the raw data, but the
-- dashboard runs on a Mac (and, soon, on Azure) where /nfs/lssc0/... does not
-- exist -- so the Ion cloud tab is coupled to a host that never has the files.
-- Extract a downsampled cloud on Hive where the data lives, store it centrally,
-- serve it from the DB.
--
-- Deliberately NOT reusing drift_peak_clouds: that table holds the MS1-histogram
-- cloud written by the PEG/drift backfill, and it has no charge/rt dimension.
-- Sharing the (run_id, source) primary key would mean whichever writer ran last
-- silently clobbered the other.
--
-- Arrays are JSON-encoded TEXT, matching drift_peak_clouds. The WRITER must
-- downsample (~5000 points/run, the existing convention -- see
-- stan/db.py::insert_drift_peak_cloud) so a 100 MB sidecar can't turn into a
-- 100 MB row. n_total records the pre-downsample count so the UI can say
-- "showing 5,000 of 184,000 features".
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no CREATE
-- on schema public. See docs/PG_FARM_ACCESS.md.

BEGIN;

CREATE TABLE IF NOT EXISTS feature_clouds (
    run_id        TEXT NOT NULL,
    source        TEXT NOT NULL,      -- 'runs' | 'sample_health'
    mz            TEXT NOT NULL,      -- JSON array of floats
    mobility      TEXT NOT NULL,      -- JSON array of floats (1/K0)
    rt            TEXT NOT NULL,      -- JSON array of floats (min)
    charge        TEXT NOT NULL,      -- JSON array of ints
    intensity     TEXT NOT NULL,      -- JSON array of floats
    n_points      INTEGER NOT NULL,   -- points stored (post-downsample)
    n_total       INTEGER NOT NULL,   -- features in the sidecar before downsampling
    features_path TEXT,               -- provenance: which .features it came from
    created_at    TEXT,
    PRIMARY KEY (run_id, source)
);

GRANT SELECT, INSERT, UPDATE, DELETE ON feature_clouds TO "genome-proteomics-service-account";

COMMIT;
