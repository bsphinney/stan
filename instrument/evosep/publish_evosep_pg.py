#!/usr/bin/env python3
"""Upsert an Evosep column-health JSON doc into PG Farm for the hosted dashboard.

DDL-free by design: the table is created by
migrations/2026-09-01_instrument_telemetry_cache.sql as the owner (brettsp).
The service account this runs as has DML only, and a bare
`CREATE TABLE IF NOT EXISTS` is refused on schema public even when the table
already exists -- so it must not be attempted here.

Uses STAN's own PG connection, so it works with whatever stan is installed on
Hive without needing a newer package version.

    python3 publish_evosep_pg.py evosep_column_health.json [table]
"""
import json
import os
import sys

from stan.db_pg import _connect

#: Hard ceiling on the published document, in megabytes. This row is fetched
#: on every Maintenance-tab load, including on a phone, so its size is a user-
#: facing latency budget rather than a storage concern.
#:
#: Asserted HERE as well as in the extractor because this is the last gate
#: before the row is written: an old or hand-made JSON can reach this script
#: without ever passing through `extract_evosep.py --max-doc-mb`. Measured
#: sizes that motivate it: 568 runs = 0.33 MB, and the full 2023-onward mirror
#: unwindowed is ~13 MB, of which `runs` alone is ~10.9 MB.
MAX_DOC_MB = float(os.environ.get("EVOSEP_MAX_DOC_MB", "1.0"))

TABLE = sys.argv[2] if len(sys.argv) > 2 else "evosep_column_health"
doc = json.load(open(sys.argv[1]))

# Cheap sanity gate: never publish a document that lost its runs, or the panel
# would render an empty shell over a perfectly good previous version.
if not doc.get("summary") or not doc.get("runs"):
    sys.exit("refusing to publish: document has no summary/runs")

payload = json.dumps(doc)
mb = len(payload.encode()) / 1024 / 1024
if MAX_DOC_MB and mb > MAX_DOC_MB:
    big = sorted(((len(json.dumps(v)), k) for k, v in doc.items()), reverse=True)[:4]
    sys.exit(
        f"refusing to publish: document is {mb:.2f} MB, over the "
        f"{MAX_DOC_MB:.2f} MB budget. Largest keys: "
        + ", ".join(f"{k} {b / 1024:.0f} KB" for b, k in big)
        + ". Re-extract with a smaller --runs-window-days, or set "
          "EVOSEP_MAX_DOC_MB deliberately.")

with _connect() as pg, pg.cursor() as cur:
    cur.execute(
        f"INSERT INTO {TABLE} (id, updated_at, doc) VALUES (1, now(), %s)"
        f" ON CONFLICT (id) DO UPDATE SET"
        f" updated_at = excluded.updated_at, doc = excluded.doc",
        (payload,))
    pg.commit()
    cur.execute(f"SELECT updated_at, pg_column_size(doc) FROM {TABLE} WHERE id=1")
    ts, sz = cur.fetchone()
    print(f"{TABLE}: updated_at={ts}, doc={sz} bytes, runs={len(doc['runs'])}")
