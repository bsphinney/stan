#!/usr/bin/env python3
"""Upsert a Bruker maintenance JSON doc into PG Farm for the hosted dashboard.

DDL-free by design: the table is created by
migrations/2026-09-01_instrument_telemetry_cache.sql as the owner (brettsp).
The service account this runs as has DML only, and a bare
`CREATE TABLE IF NOT EXISTS` is refused on schema public even when the table
already exists -- so it must not be attempted here.

Uses STAN's own PG connection, so it works with whatever stan is installed on
Hive without needing a newer package version.
"""
import json, sys
from stan.db_pg import _connect

TABLE = sys.argv[2] if len(sys.argv) > 2 else "bruker_maintenance"
doc = json.load(open(sys.argv[1]))

with _connect() as pg, pg.cursor() as cur:
    cur.execute(
        f"INSERT INTO {TABLE} (id, updated_at, doc) VALUES (1, now(), %s)"
        f" ON CONFLICT (id) DO UPDATE SET"
        f" updated_at = excluded.updated_at, doc = excluded.doc",
        (json.dumps(doc),))
    pg.commit()
    cur.execute(f"SELECT updated_at, pg_column_size(doc) FROM {TABLE} WHERE id=1")
    ts, sz = cur.fetchone()
    print(f"{TABLE}: updated_at={ts}, doc={sz} bytes")
