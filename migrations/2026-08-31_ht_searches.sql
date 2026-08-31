-- ht_searches: what was searched, as what organism, by whom  (STAN v1.0.42)
--
-- WHY: STAN can say which files a submission is, and the pipeline skill can
-- search them, but nothing recorded that the search happened. Six months on,
-- "was 0793 ever searched, and against which organism?" had no answer short
-- of digging through Hive directories and FRAN.
--
-- The organism matters most. It is the one search parameter STAN cannot
-- derive and the operator must supply, so it is also the one most worth
-- writing down -- a submission searched against the wrong FASTA looks
-- perfectly healthy in every QC metric STAN collects.
--
-- Deliberately NOT a copy of FRAN's search records. FRAN owns the corpus and
-- the provenance of what it ingested; this is only the link from a STAN
-- submission to that work, so the HT tab can say "searched as Homo sapiens,
-- in FRAN" instead of nothing.
--
-- MUST BE RUN AS THE TABLE OWNER (brettsp) -- the service account has no
-- DDL on schema public. See docs/PG_FARM_ACCESS.md.
--
--   export PGPASSWORD="$(pgfarm auth token)"
--   python scripts/apply_pg_migration.py migrations/2026-08-31_ht_searches.sql --user brettsp

BEGIN;

CREATE TABLE IF NOT EXISTS ht_searches (
    id              TEXT PRIMARY KEY,
    submission      TEXT NOT NULL,
    organism        TEXT,
    fasta_path      TEXT,
    engine          TEXT,
    engine_version  TEXT,
    n_files         INTEGER,
    output_dir      TEXT,
    fran_status     TEXT,          -- 'staged' | 'ingested' | 'not_sent'
    -- Stored, not derived. FRAN's UI is hash-routed (#/submission/<id>,
    -- #/run/<id>), and whoever deposits the search is the only one who knows
    -- which id FRAN gave it. Guessing the URL here would produce links that
    -- look right and 404 quietly.
    fran_search_id  TEXT,
    fran_url        TEXT,
    searched_at     TIMESTAMPTZ NOT NULL,
    searched_by     TEXT,
    notes           TEXT
);

-- The hot read is "everything for this submission, newest first".
CREATE INDEX IF NOT EXISTS idx_ht_searches_submission
    ON ht_searches (submission, searched_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON ht_searches
    TO "genome-proteomics-service-account";

COMMIT;
