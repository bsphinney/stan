-- Move dispatch_attempts to PG Farm (STAN v1.0.54)
--
-- WHY. This is the last table with live data that had no PG path. The code
-- said so plainly -- "Lives in SQLite in both backends because
-- dispatch_attempts was never migrated to PG" -- and it is precisely where
-- STAN's recurring SQLite corruption keeps landing:
--
--   2026-09-01 10:20  freelist corruption + missing index rows -> "database
--                     disk image is malformed". Every stan-mon job died at
--                     init_db(); 7,357 jobs failed in one day before anyone
--                     noticed, because the cron kept submitting into a DB
--                     that could not be opened.
--   2026-09-01 18:25  again, 8 hours after the repair: rows missing from
--                     idx_dispatch_attempts_status and
--                     sqlite_autoindex_dispatch_attempts_1.
--
-- Both hits are on this table's indexes, and the reason is the write pattern:
-- the dispatcher UPSERTs up to 50-60 rows every 5 minutes while SLURM jobs
-- write their own outcomes concurrently, all against one SQLite file on
-- Quobyte. SQLite's locking assumes a POSIX filesystem that a distributed
-- one does not faithfully provide, so the b-tree indexes drift out of step
-- with the table. Repairing it works, and it keeps coming back; the fix is to
-- stop writing it there.
--
-- Prior occurrences of the same class of failure: 2026-05-11, 2026-06-10,
-- 2026-08-26 (x2). See CLAUDE.md "stan.db corrupts repeatedly".
--
-- SHAPE. Mirrors the SQLite schema so the dedup predicate is unchanged --
-- keyed on raw_path, re-attempts UPDATE and bump attempt_count. attempted_at
-- becomes timestamptz (it was an ISO-8601 TEXT); the reader only orders and
-- compares, so nothing depends on the text form.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no
-- DDL on schema public. See docs/PG_FARM_ACCESS.md.
--
--   export PGPASSWORD="$(pgfarm auth token)"
--   python scripts/apply_pg_migration.py migrations/2026-09-01_dispatch_attempts_pg.sql --user brettsp

BEGIN;

CREATE TABLE IF NOT EXISTS dispatch_attempts (
    raw_path       text        PRIMARY KEY,
    attempted_at   timestamptz NOT NULL DEFAULT now(),
    status         text        NOT NULL,          -- 'ok' | 'failed' | 'skipped'
    error          text,
    error_type     text,
    attempt_count  integer     NOT NULL DEFAULT 1,
    last_run_id    text,
    host_origin    text                            -- which instrument/host recorded it
);

-- The dispatcher's only query: failed raws at or past the attempt cap.
CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_status
    ON dispatch_attempts (status, attempt_count);

COMMENT ON TABLE dispatch_attempts IS
    'Every search-dispatch outcome, keyed on raw_path so re-attempts update in '
    'place and bump attempt_count. Moved off the Quobyte SQLite file in v1.0.54 '
    'after repeated index corruption from concurrent SLURM writers.';

GRANT SELECT, INSERT, UPDATE, DELETE ON dispatch_attempts
    TO "genome-proteomics-service-account";

COMMIT;
