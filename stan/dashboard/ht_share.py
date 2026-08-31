"""Per-submission share links, so a collaborator can watch their own plate.

An external collaborator has no UC Davis account, so a login cannot be the
answer for them — but HT data is customer-identifying and must not simply be
public either. A share link splits the difference: the holder sees exactly
one submission's progress, live, and nothing else in STAN.

The token is an HMAC of the submission number under a server-side secret,
not a stored row. That means:

  * no migration and no table to keep in step across SQLite and PG;
  * the same link works on the local dashboard and the hosted one, as long
    as they share the secret;
  * a token is only valid for the submission it was minted for, so a
    collaborator cannot edit the number in the URL and read someone else's
    plate — the usual failure of "secret link" schemes.

The tradeoff is deliberate and worth stating: revocation is all-or-nothing.
Rotating STAN_HT_SHARE_SECRET invalidates every outstanding link at once.
For handing a customer a link to their own run that is an acceptable trade;
if per-link revocation is ever needed, that is the point to add a table.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

logger = logging.getLogger(__name__)

_SECRET_ENV = "STAN_HT_SHARE_SECRET"
_SECRET_FILE = "ht_share_secret"
#: 32 hex chars = 128 bits. Not guessable, still short enough to paste in an
#: email without wrapping.
_TOKEN_CHARS = 32


def _secret() -> bytes | None:
    """The signing secret, from the environment or the user's STAN dir.

    Generated once on first use and kept 0600. Returns None only when it
    cannot be created, in which case sharing stays off rather than falling
    back to something predictable.
    """
    env = (os.environ.get(_SECRET_ENV) or "").strip()
    if env:
        return env.encode()

    path = Path.home() / ".stan" / _SECRET_FILE
    try:
        if path.exists():
            val = path.read_text().strip()
            if val:
                return val.encode()
        path.parent.mkdir(parents=True, exist_ok=True)
        val = secrets.token_hex(32)
        path.write_text(val)
        path.chmod(0o600)
        logger.info("minted a new HT share secret at %s", path)
        return val.encode()
    except Exception:
        logger.warning("no HT share secret available; sharing disabled",
                       exc_info=True)
        return None


def _canonical(submission: str) -> str:
    """Normalise so 0793 and 793 mint and verify the same token.

    Operators type the padded form and filenames carry it bare; a link that
    worked only for the spelling used when it was created would be a
    confusing failure.
    """
    s = str(submission or "").strip().lower()
    return s.lstrip("0") or s


def make_token(submission: str) -> str | None:
    """A share token for one submission, or None if sharing is unavailable."""
    key = _secret()
    if not key or not str(submission or "").strip():
        return None
    mac = hmac.new(key, _canonical(submission).encode(), hashlib.sha256)
    return mac.hexdigest()[:_TOKEN_CHARS]


def verify_token(submission: str, token: str) -> bool:
    """Is this token valid for THIS submission? Constant-time."""
    if not token or not str(submission or "").strip():
        return False
    expected = make_token(submission)
    if not expected:
        return False
    return hmac.compare_digest(expected, str(token).strip().lower())
