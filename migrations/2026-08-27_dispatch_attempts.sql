-- dispatch_attempts on PG Farm  (STAN v1.0.33)
--
-- WHY: this table was missed by 2026-08-26_sample_health.sql, which moved
-- the monitor pipeline's writes to PG precisely because ~100 concurrent
-- SLURM jobs writing one SQLite file on Quobyte had corrupted it three
-- times. dispatch_attempts has the same writers and the same storage, so
-- it kept the problem alive.
--
-- The symptom it caused: dispatch_hive._failed_too_many() reads this table
-- to stop re-dispatching a permanently broken raw. Writes from the compute
-- nodes were being lost, so the cap could not engage. Observed 2026-08-27:
-- five raws re-submitted on every 5-minute tick (~1,440 wasted SLURM jobs a
-- day), two of them with NO row at all despite failing every time, and two
-- others stuck at attempt_count 13 and 16 -- far past the cap of 3. The
-- write failure was swallowed at DEBUG in db.record_dispatch_attempt(),
-- so none of it was visible in a log.
--
-- SQLite remains fully supported: a single-lab Windows install must stay
-- turnkey without PG Farm, so the routing is conditional on use_pg().
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no
-- CREATE on schema public. See docs/PG_FARM_ACCESS.md.
--
--   export PGPASSWORD="$(pgfarm auth token)"
--   python scripts/apply_pg_migration.py migrations/2026-08-27_dispatch_attempts.sql --user brettsp

BEGIN;

CREATE TABLE IF NOT EXISTS dispatch_attempts (
    raw_path        TEXT PRIMARY KEY,
    attempted_at    TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL,          -- 'ok' | 'failed' | 'skipped'
    error           TEXT,
    error_type      TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 1,
    last_run_id     TEXT
);

-- Mirrors the SQLite index. The hot read is the dispatcher's capped-set
-- preload: status='failed' AND attempt_count >= max_attempts.
CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_status
    ON dispatch_attempts (status, attempted_at);
CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_capped
    ON dispatch_attempts (status, attempt_count);

-- The service account does the DML; only the owner can create.
GRANT SELECT, INSERT, UPDATE, DELETE ON dispatch_attempts
    TO "genome-proteomics-service-account";

COMMIT;
