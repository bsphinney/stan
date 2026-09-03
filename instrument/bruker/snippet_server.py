# =============================================================================
# STAN integration snippet  --  backend endpoint for the Bruker maintenance view
#
# PASTE the function below into stan/dashboard/server.py, next to the other
# @app.get("/api/maintenance/...") routes (search for
# `api_maintenance_calendar` -- put this right after it).
#
# It serves the JSON produced by HT_work/bruker/extract_bruker.sh. The extractor
# runs on Hive against the newest Bruker Compass backup and writes
# `bruker_maintenance.json` into STAN's user config dir (~/STAN/), which is
# exactly where resolve_config_path() already looks for thresholds.yml and
# ui_prefs.yml -- so this follows a pattern that is already in the codebase.
#
# No new imports are required: `json`, `HTTPException` and `resolve_config_path`
# are already imported at the top of server.py.
# =============================================================================


@app.get("/api/maintenance/bruker")
async def api_maintenance_bruker() -> dict:
    """Bruker timsTOF acquisition-history maintenance signals.

    Read-only summary extracted from the instrument's Compass Server
    PostgreSQL backup by the Hive-side extractor
    (HT_work/bruker/extract_bruker.sh). The extractor writes
    ``bruker_maintenance.json`` into the STAN config dir on a schedule; this
    route just serves the cached document so the dashboard never touches the
    Bruker database directly.

    Responds 404 when the cache file is absent -- the frontend treats that as
    "the Bruker extractor hasn't run yet" and hides the panel, exactly like the
    ui_prefs.yml 404 path.
    """
    try:
        path = resolve_config_path("bruker_maintenance.json")
    except FileNotFoundError:
        raise HTTPException(
            status_code=404,
            detail="Bruker maintenance cache not found (extractor has not run)",
        )
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("Bruker maintenance cache unreadable: %s", exc)
        raise HTTPException(status_code=503, detail="Bruker maintenance cache unreadable")
