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
import os

logger = logging.getLogger(__name__)

#: Methods that can change state. HEAD/GET/OPTIONS are always allowed.
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})

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

    @app.middleware("http")
    async def _readonly_gate(request, call_next):
        path = request.url.path.rstrip("/") or "/"
        if path in _HIDDEN_PATHS:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        if request.method.upper() in _MUTATING:
            # Log enough to notice a probe, but never the body.
            logger.warning(
                "read-only: refused %s %s from %s",
                request.method, path,
                getattr(request.client, "host", "?"),
            )
            return JSONResponse(
                {"detail": "This STAN dashboard is read-only. "
                           "State-changing requests are disabled."},
                status_code=403,
            )
        return await call_next(request)

    logger.warning(
        "STAN_DASHBOARD_READONLY is set — mutating routes return 403 and "
        "%d introspection paths are hidden", len(_HIDDEN_PATHS),
    )
    return True
