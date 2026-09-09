#!/bin/bash -l
# Full-history Evosep extract + publish, as an sbatch job.
#
# A standalone script rather than an sbatch --wrap one-liner: the wrap version
# was written through two layers of ssh quoting on 2026-09-03 and silently lost
# every variable, so it ran `/bin/python /extract_evosep.py` and failed in
# under a second. A file has no quoting layers.
set -euo pipefail

EV=/quobyte/proteomics-grp/STAN/evosep
LOGS=/quobyte/proteomics-grp/STAN/evosep_logs
VENV=/quobyte/proteomics-grp/brett/stan_venv
FULL=/quobyte/proteomics-grp/STAN/evosep_column_health.json
export STAN_DB_BACKEND=pg

echo "start $(date '+%F %T')  host=$(hostname)"
tmp=$(mktemp "${FULL}.XXXX")
trap 'rm -f "$tmp"' EXIT

"$VENV/bin/python" "$EV/extract_evosep.py" \
    --root "$LOGS" --instrument 'timsTOF HT' --out "$tmp"

# Refuse to publish a document that lost its history: an empty `daily` or zero
# runs means the extract half-worked, and overwriting a good document with it
# would be worse than publishing nothing.
"$VENV/bin/python" - "$tmp" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
s = d.get("summary", {})
print("runs:", s.get("n_runs"), "window:", s.get("first_run"), "->", s.get("last_run"))
print("keys:", [k for k in ("wash_flow", "column_lifetimes", "pressure_reference",
                            "sample_impact", "expected_pressure") if k in d])
assert s.get("n_runs", 0) > 0 and d.get("daily"), "no runs or no daily aggregates"
PY

mv -f "$tmp" "$FULL"
trap - EXIT
"$VENV/bin/python" "$EV/publish_evosep_pg.py" "$FULL" evosep_column_health
echo "PUBLISHED $(date '+%F %T')"
