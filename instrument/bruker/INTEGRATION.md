# Integrating the Bruker maintenance view into STAN

Everything here is drop-in code matched to STAN's existing patterns (learned from
the Hive checkout at `/quobyte/proteomics-grp/brett/stan`). Because
`~/Documents/STAN` is locked, apply these by hand once you have Full Disk Access —
it is a ~5-minute paste job in two files plus one cron line.

```
Bruker Compass backup ──▶ extract_bruker.sh (Hive, read-only) ──▶ bruker_maintenance.json
                                                                        │
                                       cron every 30 min publishes it   │
                                                                        ▼
                     GET /api/maintenance/bruker  ◀── reads the cached JSON (server.py)
                                                                        │
                                                                        ▼
                     <BrukerAcquisitionPanel/>  inside the Maintenance tab (index.html)
```

The dashboard never touches the Bruker database — it only reads a small JSON cache,
exactly like STAN already reads `acq_date_cache.json` / `thresholds.yml`.

---

## Step 1 — Backend endpoint  (`stan/dashboard/server.py`)

Open `snippet_server.py` and paste the `api_maintenance_bruker` function next to
the other maintenance routes — right **after** `api_maintenance_calendar`
(search for `@app.get("/api/maintenance/calendar")`).

- **No new imports.** `json`, `HTTPException`, `logger` and `resolve_config_path`
  are already imported at the top of `server.py`.
- It resolves `bruker_maintenance.json` via `resolve_config_path(...)` — the same
  helper `thresholds.yml` and `ui_prefs.yml` use — and returns it, or `404` when
  the extractor hasn't produced a cache yet.

## Step 2 — React panel  (`stan/dashboard/public/index.html`)

1. Paste the **entire** contents of `snippet_index_component.jsx` into
   `index.html` just **above** `function MaintenanceTab()` (~line 4575). It defines
   `BrukerAcquisitionPanel` plus its private sub-components (`BThroughput`,
   `BDuty`, `BPlate`, `BFailBars`, `BMethodBars`, `BFailTable`) and helpers.
   - It uses STAN's own `useFetch(url, deps)` helper and `className="card"`.
   - It uses STAN's theme CSS vars (`--surface`, `--border`, `--accent`,
     `--muted`, `--text`, `--pass`, `--warn`, `--fail`). The three chart data
     hues (blue `#3987e5`, orange `#d95926`, aqua `#199e70`) are the
     dataviz-validated dark categorical slots 1–3 — they clear CVD + contrast on
     STAN's navy surface. Plate status additionally carries a **glyph + legend**,
     so state never rides on colour alone.
   - Charts are inline SVG with native `<title>` hover tooltips — no new library
     (STAN's Plotly is left untouched).

2. Wire it into the tab. Inside `MaintenanceTab()`'s `return (...)`, add the panel
   as the **last child**, right before the final closing `</div>` — see
   `snippet_maintenance_wiring.jsx`:

   ```jsx
             <BrukerAcquisitionPanel />
         </div>   {/* end of MaintenanceTab return */}
   ```

No change to the `TABS` array is needed — the Maintenance tab already exists; this
adds a section inside it.

## Step 3 — Feed the cache  (Hive)

The endpoint serves whatever `bruker_maintenance.json` the extractor writes.

- **Deploy** `extract_bruker.sh` + `extract.sql` somewhere on Hive (they currently
  live in `/quobyte/proteomics-grp/brett/HT_bruker_scratch/`; move them beside the
  other STAN tooling if you like).
- **Schedule** with the suggested, `flock`-guarded `cron_bruker_maintenance.sh`
  (mirrors `cron_count_acquisitions.sh`). It extracts to a temp file, sanity-checks
  it, then atomically publishes to `/quobyte/proteomics-grp/STAN/bruker_maintenance.json`:

  ```
  */30 * * * * flock -n /tmp/stan_bruker_maint.lock /quobyte/proteomics-grp/STAN/cron_bruker_maintenance.sh
  ```

- **Delivery.** If the dashboard runs **on Hive**, `resolve_config_path` finds the
  file there and you're done. If it runs elsewhere (hosted PG-Farm dashboard),
  add an `rsync` of that JSON to the dashboard host's `~/STAN/` dir inside the
  cron script (a placeholder line is already there), **or** use the PG-Farm option
  below.

### Optional — serve from PG Farm instead of a file

If you'd rather the hosted dashboard read from PG Farm (STAN's canonical DB when
`STAN_DB_BACKEND=pg`, via `stan/db_pg.py`) than from a synced file:

1. One table: `CREATE TABLE bruker_maintenance (id int PRIMARY KEY DEFAULT 1,
   updated_at timestamptz, doc jsonb);`
2. In the cron script, after producing the JSON, upsert it:
   `INSERT INTO bruker_maintenance (id, updated_at, doc) VALUES (1, now(), $doc)
   ON CONFLICT (id) DO UPDATE SET updated_at = excluded.updated_at, doc = excluded.doc;`
   (psql to `pgfarm.library.ucdavis.edu`, same creds STAN already uses.)
3. Change the endpoint body to `return get_bruker_maintenance()` — a one-liner in
   `stan/db_pg.py`: `SELECT doc FROM bruker_maintenance WHERE id = 1`.

The file-cache path (Steps 1–3) is the recommended default: fewer moving parts,
and identical to how STAN already ships `acq_date_cache.json`.

---

## Verification

Both deliverables were rendered and screenshotted with the **real** Aug-31 backup
data before hand-off:

- `maintenance_preview.html` — the standalone prototype (open it directly).
- `snippet_index_component.jsx` — rendered in a React 18 + Babel harness
  (`test_harness.html`, same CDNs as `index.html`) with a stubbed `useFetch`
  returning the real JSON. It rendered identically, with **zero** console errors
  or React warnings.

After pasting, hit `GET /api/maintenance/bruker` (should return the JSON, or 404
before the first cron run) and open the Maintenance tab — the panel appears below
the Maintenance Log.
