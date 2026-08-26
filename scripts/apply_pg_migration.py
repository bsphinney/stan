"""Apply a .sql migration to PG Farm as a named user (no psql required).

The Mac dev box has psycopg2 but not the psql client, and STAN's schema
changes must run as the table OWNER (``brettsp``) -- the service account has
DML on ``runs`` but no CREATE on schema public and no CREATE on the database,
so it cannot create or alter tables itself.

Get an owner token first::

    npm install -g @ucd-lib/pgfarm     # once
    pgfarm auth login                  # UCD CAS, browser flow
    export PGPASSWORD="$(pgfarm auth token)"

Then::

    python scripts/apply_pg_migration.py migrations/<file>.sql --user brettsp

The whole file is sent as one statement batch so its own BEGIN/COMMIT
controls the transaction; nothing is auto-committed around it.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger("apply_migration")

PG_HOST = "pgfarm.library.ucdavis.edu"
PG_PORT = 5432
PG_DB = "uc-davis-genome-center-proteomics-core/stan"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sql_file", type=Path)
    ap.add_argument("--user", default="brettsp",
                    help="PG role to connect as (default: the table owner).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the SQL and connect, but roll back instead of committing.")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.sql_file.exists():
        logger.error("no such file: %s", args.sql_file)
        return 2
    sql = args.sql_file.read_text()

    pwd = os.environ.get("PGPASSWORD", "").strip()
    if not pwd:
        logger.error('PGPASSWORD not set — run: export PGPASSWORD="$(pgfarm auth token)"')
        return 2

    import psycopg2

    con = psycopg2.connect(
        host=PG_HOST, port=PG_PORT, database=PG_DB, sslmode="require",
        user=args.user, password=pwd, connect_timeout=30,
    )
    try:
        cur = con.cursor()
        cur.execute("SELECT current_user, has_schema_privilege(current_user,'public','CREATE')")
        who, can_create = cur.fetchone()
        logger.info("connected as %s (CREATE on public: %s)", who, can_create)
        if not can_create:
            logger.error("%s cannot CREATE in schema public — wrong role for a migration", who)
            return 3

        cur.execute(sql)
        if args.dry_run:
            con.rollback()
            logger.info("dry-run — rolled back")
        else:
            con.commit()
            logger.info("applied %s", args.sql_file.name)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
