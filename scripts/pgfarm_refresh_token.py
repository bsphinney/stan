"""Mint a fresh PG Farm 7-day token from the service-account secret.

PG Farm service accounts authenticate in two steps:
  1. A long-lived *secret* (downloaded as ``service-account.json`` when you
     rotate it in the PG Farm UI) — keep this safe, it never appears in any
     log and the admin cannot read it.
  2. A short-lived *token* minted from the secret. The token (a JWT) is what
     Postgres accepts as the password. It is valid 7 days; refresh well
     before that (default policy: every 5 days).

This script does step 2: POST {username, secret} to the login endpoint, write
the returned ``access_token`` to the token file STAN's PG backend reads
(``stan/db_pg.py:_resolve_pgpassword`` → ``STAN_PGFARM_TOKEN_FILE`` or
``/quobyte/proteomics-grp/brett/.pgfarm_token``). Both the secret file and the
token file live on the shared Quobyte FS, so a refresh from either the Mac
(``/Volumes/proteomics-grp/...``) or Hive (``/quobyte/proteomics-grp/...``)
updates the token every dispatch job reads.

Run standalone, or from the dispatch cron with ``--max-age-days 5`` so it only
re-mints when the current token is stale. Never prints the secret or token.

    python scripts/pgfarm_refresh_token.py \\
        --secret-file /quobyte/proteomics-grp/brett/.pgfarm_secret.json \\
        --token-file  /quobyte/proteomics-grp/brett/.pgfarm_token \\
        --max-age-days 5
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

LOGIN_URL = "https://pgfarm.library.ucdavis.edu/auth/service-account/login"


def mint_token(secret_file: Path) -> tuple[str, int]:
    """Exchange the service-account secret for a fresh access token.

    Returns ``(access_token, expires_in_seconds)``. Raises on HTTP error or
    a malformed response. The secret never leaves this process.
    """
    sa = json.loads(secret_file.read_text())
    body = json.dumps(
        {"username": sa["username"], "secret": sa["secret"]}
    ).encode()
    req = urllib.request.Request(
        LOGIN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = json.loads(resp.read())
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"no access_token in response (keys={list(data)})")
    return token, int(data.get("expires_in", 0))


def token_age_days(token_file: Path) -> float:
    """Age of the current token file in days, or +inf if missing."""
    if not token_file.exists():
        return float("inf")
    return (time.time() - token_file.stat().st_mtime) / 86400.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--secret-file", type=Path, required=True,
                    help="Path to service-account.json (the rotated secret).")
    ap.add_argument("--token-file", type=Path, required=True,
                    help="Where to write the minted 7-day token.")
    ap.add_argument("--max-age-days", type=float, default=0.0,
                    help="Only refresh if the current token is older than "
                         "this (0 = always refresh).")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if not args.secret_file.exists():
        logger.error("secret file missing: %s", args.secret_file)
        return 2

    age = token_age_days(args.token_file)
    if args.max_age_days and age < args.max_age_days:
        logger.info("token is %.1fd old (< %.1fd) — no refresh needed",
                    age, args.max_age_days)
        return 0

    try:
        token, expires_in = mint_token(args.secret_file)
    except urllib.error.HTTPError as e:
        logger.error("token endpoint HTTP %s: %s", e.code, e.read()[:200])
        return 1
    except Exception as e:  # noqa: BLE001
        logger.error("token mint failed: %s: %s", type(e).__name__, e)
        return 1

    # Write atomically with tight perms; never log the token value.
    args.token_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.token_file.with_suffix(args.token_file.suffix + ".tmp")
    tmp.write_text(token)
    os.chmod(tmp, 0o600)
    os.replace(tmp, args.token_file)
    logger.info("minted fresh token (len=%d, expires_in=%ds) -> %s",
                len(token), expires_in, args.token_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
