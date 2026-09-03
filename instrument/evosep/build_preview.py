#!/usr/bin/env python3
"""Build the standalone prototype from the SAME component STAN will use.

Deliberately not a second implementation: the preview stubs `useFetch` to hand
back the real extracted JSON and renders `snippet_index_component.jsx`
verbatim, so what you see here is what the dashboard renders.
"""
import json, pathlib

here = pathlib.Path(__file__).parent
comp = (here / "snippet_index_component.jsx").read_text()
doc = json.loads((here / "evosep_column_health.json").read_text())

HEAD = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Evosep column health — STAN preview</title>
<script src="https://cdn.jsdelivr.net/npm/react@18/umd/react.production.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/react-dom@18/umd/react-dom.production.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/@babel/standalone/babel.min.js"></script>
<style>
  /* STAN's own theme variables, copied verbatim from index.html. */
  :root {
    --bg: #011a3a; --surface: #022851; --border: #1e3a5f;
    --text: #e2e8f0; --muted: #a0b4cc; --accent: #DAAA00;
    --pass: #22c55e; --warn: #eab308; --fail: #ef4444;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); color: var(--text); margin: 0; padding: 1.5rem;
         font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
  .wrap { max-width: 1180px; margin: 0 auto; }
  .card { background: var(--surface); border: 1px solid var(--border);
          border-radius: 0.75rem; padding: 1.25rem; margin-bottom: 1rem; }
  .card h3 { font-size: 1rem; margin: 0 0 0.75rem; color: var(--accent); }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 0.5rem; color: var(--muted);
       border-bottom: 2px solid var(--border); font-weight: 600; }
  td { padding: 0.5rem; border-bottom: 1px solid var(--border); vertical-align: top; }
  .banner { color: var(--muted); font-size: 0.8rem; margin-bottom: 1rem;
            border-left: 3px solid var(--accent); padding-left: 0.75rem; }
</style></head><body><div class="wrap">
<div class="banner">
  Standalone preview of the STAN Maintenance-tab panel. Renders
  <code>snippet_index_component.jsx</code> unmodified against the real
  extracted document — every number below comes from the Evosep One's own
  pressure logs.
</div>
<div id="root"></div></div>
<script type="text/babel">
const { useState, useEffect, useCallback } = React;
"""

TAIL = """
ReactDOM.createRoot(document.getElementById('root')).render(<EvosepColumnPanel />);
</script></body></html>
"""

stub = ("const EVOSEP_DOC = " + json.dumps(doc, separators=(",", ":")) + ";\n"
        "/* Stubbed for the preview; in STAN this is the real useFetch. */\n"
        "function useFetch(){ return { data: EVOSEP_DOC, loading:false, error:null }; }\n")

out = HEAD + stub + comp + TAIL
(here / "maintenance_preview_evosep.html").write_text(out)
print("wrote maintenance_preview_evosep.html  (%.1f KB)" % (len(out)/1024))
