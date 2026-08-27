"""Per-request authorization for the hosted STAN dashboard.

The public site is deliberately open: anyone with the link reads the QC data,
because Brett's position is that there is nothing sensitive in it. What must
NOT be open is anything that acts on the lab's behalf — publishing runs to the
community benchmark under the lab's pseudonym, fleet commands, config edits.
An unauthenticated button for those would let any visitor publish as this lab.

So the hosted dashboard runs read-only for everyone, and a signed-in,
allow-listed operator gets the write actions back.

PRODUCTION — Azure App Service "Easy Auth" with Microsoft Entra ID in
*allow-unauthenticated* mode, pointed at the UC Davis tenant (which is what
puts CAS + Duo in front of the login). The platform performs the login and
injects the verified principal as a base64 JSON blob in
``X-MS-CLIENT-PRINCIPAL`` plus ``X-MS-CLIENT-PRINCIPAL-NAME``.

  Why the header can be trusted: with Easy Auth enabled, App Service strips
  any client-supplied ``X-MS-CLIENT-PRINCIPAL*`` headers and sets them itself
  only after validating the login, so the app never sees a spoofed one. That
  guarantee comes from Easy Auth being on — if this app is ever fronted some
  other way, auth has to be terminated there in the same fashion.

FAIL-CLOSED. No principal, or no allow-list configured, or the caller is not
on it → read-only. A misconfiguration must never hand write access to the
public internet.

This mirrors FRAN's ``corpus_browser/app/auth.py``, deliberately: same
platform, same tenant, same header contract, and one pattern to reason about
across both deployments.

LOCAL / DEV — a local `stan dashboard` is not read-only in the first place
(see readonly.py), so it needs no shortcut here and none is provided.
"""

from __future__ import annotations

import base64
import json
import logging
import os

logger = logging.getLogger(__name__)

# Comma-separated UC Davis emails / UPNs, e.g. "bsphinney@ucdavis.edu,ggrigorean@ucdavis.edu".
# Read per call rather than at import so rotating the app setting takes effect
# on restart without a code change, and so tests can monkeypatch the env.
_ALLOWED_ENV = "STAN_ALLOWED_USERS"
# Entra security-group object id; membership managed in Entra with no redeploy.
_GROUP_ENV = "STAN_REQUIRED_GROUP"

_UPN_CLAIMS = (
    "preferred_username",
    "email",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/upn",
)
_GROUP_CLAIMS = (
    "groups",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
)


def _allowed_users() -> frozenset[str]:
    return frozenset(
        u.strip().lower()
        for u in os.environ.get(_ALLOWED_ENV, "").split(",")
        if u.strip()
    )


def _decode_principal(request) -> dict | None:
    """Decode the Easy Auth principal header, or None if absent/malformed."""
    raw = request.headers.get("x-ms-client-principal")
    if not raw:
        return None
    try:
        return json.loads(base64.b64decode(raw).decode("utf-8"))
    except Exception:  # noqa: BLE001 - any decode failure is simply "no principal"
        logger.debug("undecodable client principal header", exc_info=True)
        return None


def _claims(principal: dict | None) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for c in (principal or {}).get("claims", []) or []:
        typ, val = c.get("typ"), c.get("val")
        if typ is not None:
            out.setdefault(typ, []).append(val)
    return out


def caller_email(request) -> str | None:
    """The signed-in user's email/UPN, lowercased. None when not signed in."""
    name_hdr = (request.headers.get("x-ms-client-principal-name") or "").strip().lower()
    if "@" in name_hdr:
        return name_hdr
    principal = _decode_principal(request)
    claims = _claims(principal)
    for ct in _UPN_CLAIMS:
        for v in claims.get(ct, []) or []:
            if v and "@" in v:
                return v.strip().lower()
    ud = ((principal or {}).get("userDetails") or "").strip().lower()
    return ud if "@" in ud else None


def is_privileged(request) -> bool:
    """May THIS caller perform write actions on the hosted dashboard?

    Fail-closed: every path that is not an affirmative match returns False.
    """
    principal = _decode_principal(request)
    if not principal:
        return False

    group = os.environ.get(_GROUP_ENV, "").strip()
    allowed = _allowed_users()
    if not group and not allowed:
        # Signed in, but no gate configured. Refuse rather than treat
        # "authenticated" as "authorized" — anyone with a Microsoft account
        # can authenticate.
        logger.warning(
            "%s signed in but neither %s nor %s is set — refusing",
            caller_email(request) or "caller", _ALLOWED_ENV, _GROUP_ENV,
        )
        return False

    claims = _claims(principal)

    if group:
        groups: list[str] = []
        for ct in _GROUP_CLAIMS:
            groups += claims.get(ct, []) or []
        if group in groups:
            return True

    if allowed:
        ids: set[str] = set()
        name_hdr = (request.headers.get("x-ms-client-principal-name") or "").strip().lower()
        if name_hdr:
            ids.add(name_hdr)
        for ct in _UPN_CLAIMS:
            for v in claims.get(ct, []) or []:
                if v:
                    ids.add(v.strip().lower())
        ud = (principal.get("userDetails") or "").strip().lower()
        if ud:
            ids.add(ud)
        if ids & allowed:
            return True

    return False
