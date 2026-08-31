"""Read-only gate for a publicly-hosted STAN dashboard.

``stan dashboard`` was written for a listener bound to 127.0.0.1, and its
whole safety model rests on that. The Origin middleware in ``server.py``
deliberately *allows* state-changing requests that carry no ``Origin``
header, on the reasoning that only a local operator CLI can reach the socket
at all. That reasoning is sound on a laptop and false the moment the app is
published: a plain ``curl -X POST https://stan.example/api/fleet/command``
sends no Origin and sails straight through.

The exposure is not theoretical. ``/api/fleet/command`` enqueues commands
from ``stan/control.py``'s whitelist — ``update_stan``, ``apply_config``,
``restart_watcher`` — for any instrument host. That is remote code execution
on the instrument PCs. Ten other routes overwrite ``instruments.yml`` /
``thresholds.yml`` from the request body, publish QC data to the public
community relay, or append caller-controlled text to a log file.

So: when ``STAN_DASHBOARD_READONLY`` is set, refuse every mutating request
and hide the introspection surface. The Azure/App Service deployment sets it;
a local operator install never does, so ``stan dashboard`` on an instrument PC
keeps working exactly as before.

This is a *gate*, not authentication. It is the second layer behind platform
SSO (see ``docs/AZURE_HOSTING_PLAN.md``) and exists precisely because the
first layer is a portal setting that a future misconfiguration could flip.
"""

from __future__ import annotations

import logging
import re
import os

logger = logging.getLogger(__name__)

#: Methods that can change state. HEAD/GET/OPTIONS are always allowed.
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Where Easy Auth sends a caller to sign in. Handed back on a 403 so the UI
#: can offer the link rather than leaving a dead end.
LOGIN_URL = "/.auth/login/aad?post_login_redirect_uri=/"

#: Writes the PUBLIC dashboard accepts with no login at all.
#: Only the arcade score post. A leaderboard shared across the community and
#: every local STAN is pointless if only signed-in operators can add to it,
#: and the payload is a game score -- no lab data, no instrument control. The
#: fields are length-bounded and range-checked at the endpoint, so the worst
#: case is a junk high score, which a person can delete.
#: Nothing that touches QC data, instruments, or config belongs here.
_PUBLIC_WRITE_PATHS = frozenset({
    "/api/arcade/score",
})

#: The only writes a signed-in operator may perform on the hosted dashboard.
#: An explicit allow-list, not "everything except X": publishing this lab's
#: runs and recording maintenance are deliberate, attributable actions,
#: whereas the fleet-command and config-write routes are remote code
#: execution against instrument PCs and stay refused on a public host no
#: matter who is signed in. Widen this only with a reason.
_PRIVILEGED_PATHS = frozenset({
    "/api/community/sync",
})

#: Same rule, for routes whose path carries a variable segment. Anchored so
#: a pattern can never match more than the one route it names.
_PRIVILEGED_PATTERNS = (
    # Maintenance log: POST /api/instruments/{instrument}/events
    re.compile(r"^/api/instruments/[^/]+/events$"),
)


def _is_privileged_path(path: str) -> bool:
    return path in _PRIVILEGED_PATHS or any(
        p.match(path) for p in _PRIVILEGED_PATTERNS
    )

#: Reads that require a signed-in operator on the PUBLIC host.
#:
#: Almost everything in STAN is deliberately open -- Brett's position is that
#: aggregate QC data is not sensitive and the community benefits from it
#: being visible. High-throughput submission data is the exception: it is
#: keyed by a customer's submission number and carries their sample names
#: (`SI-48`, `DTTCAAoff`), which is a different kind of information from
#: "this instrument identified 30,000 precursors on a HeLa standard".
#:
#: Gated whole rather than per-field. A partial gate that shows the plate map
#: but hides the names still leaks the submission's shape and size, and the
#: failure mode of getting it subtly wrong is silent.
_PRIVILEGED_READ_PATTERNS = (
    re.compile(r"^/api/ht(/|$)"),
)


def _is_privileged_read(path: str) -> bool:
    return any(p.match(path) for p in _PRIVILEGED_READ_PATTERNS)


#: Introspection surfaces that leak route shapes or internals. Hidden rather
#: than 403'd so a scanner sees "not here" instead of "here but forbidden".
_HIDDEN_PATHS = frozenset({
    "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect",
    "/api/dashboard-errors",
})


def is_readonly() -> bool:
    """True when this process should refuse state-changing requests."""
    return (os.environ.get("STAN_DASHBOARD_READONLY") or "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def install_readonly_gate(app) -> bool:
    """Attach the gate to a FastAPI app. Returns True if it was enabled.

    Safe to call unconditionally; it is a no-op unless the env var is set.
    """
    if not is_readonly():
        return False

    from fastapi.responses import JSONResponse

    from stan.dashboard.auth import caller_email, is_privileged

    @app.middleware("http")
    async def _readonly_gate(request, call_next):
        path = request.url.path.rstrip("/") or "/"
        if path in _HIDDEN_PATHS:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        # A share link carries a token. Let it reach the endpoint, which is
        # the only place with enough context to check the token against the
        # submission actually being asked for -- the gate sees the path, not
        # which customer's plate this is.
        has_share_token = bool(request.query_params.get("token"))
        if (_is_privileged_read(path) and not has_share_token
                and not is_privileged(request)):
            logger.info("read-only: refused HT read %s from %s", path,
                        getattr(request.client, "host", "?"))
            return JSONResponse(
                {"detail": "High-throughput submission data requires an "
                           "authorized sign-in.",
                 "login_url": LOGIN_URL},
                status_code=403)
        if request.method.upper() in _MUTATING:
            # A signed-in, allow-listed operator gets write access back. This
            # is the only way through the gate, and it is decided per request
            # from the platform-verified Easy Auth principal — never from a
            # process-wide flag that a misconfiguration could leave on.
            if path in _PUBLIC_WRITE_PATHS:
                return await call_next(request)
            if _is_privileged_path(path) and is_privileged(request):
                logger.info(
                    "read-only: allowing %s %s for %s",
                    request.method, path, caller_email(request) or "authorized caller",
                )
                return await call_next(request)
            # Log enough to notice a probe, but never the body.
            logger.warning(
                "read-only: refused %s %s from %s",
                request.method, path,
                getattr(request.client, "host", "?"),
            )
            return JSONResponse(
                {"detail": "This STAN dashboard is read-only. "
                           "Sign in as an authorized operator to publish.",
                 "login_url": LOGIN_URL},
                status_code=403,
            )
        return await call_next(request)

    logger.warning(
        "STAN_DASHBOARD_READONLY is set — mutating routes return 403 and "
        "%d introspection paths are hidden", len(_HIDDEN_PATHS),
    )
    return True
