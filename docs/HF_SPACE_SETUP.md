# HF Space Error Report Endpoint

## What it does

The `/api/error-report` endpoint on the STAN HF Space (`brettsp/stan`) receives
anonymous error reports from STAN clients. When a user opts in via
`error_telemetry: true` in `~/.stan/community.yml`, unhandled errors are sent
to this endpoint as fire-and-forget POST requests.

No authentication is required. No PII is collected. File paths are stripped
to filenames only before the client sends them (see `stan/telemetry.py`).

Reports are stored in `error_reports.json` on the Space filesystem, capped
at the most recent 1000 entries. This gives Brett visibility into common
failure modes across the community without requiring users to file issues.

## How to add it to the Space

1. Open the Space editor: https://huggingface.co/spaces/brettsp/stan/blob/main/app.py
2. Add any missing imports at the top of `app.py`:

```python
import hashlib
import threading
import time
from pathlib import Path
from fastapi import Request
```

(`json`, `datetime`, `logging`, `HTTPException`, `BaseModel` should already be imported.)

3. Copy the contents of `docs/hf_space_error_endpoint.py` (everything below the
   "Paste everything below" comment) into `app.py`, anywhere after the
   `app = FastAPI(...)` line. A good spot is right before the `INDEX_HTML` string
   or right after the `/api/health` route.

4. Commit and the Space will rebuild automatically.

## Payload format

The STAN client (`stan/telemetry.py`) sends POST requests to
`https://brettsp-stan.hf.space/api/error-report` with this JSON body:

```json
{
    "timestamp": "2026-04-07T18:30:00+00:00",
    "stan_version": "0.4.0",
    "python_version": "3.11.8",
    "os": "Linux",
    "os_version": "5.15.0",
    "arch": "x86_64",
    "error_type": "FileNotFoundError",
    "error_message": "No such file or directory: 'report.parquet'",
    "traceback": "File \"extractor.py\", line 42, in extract ...",
    "search_engine": "diann",
    "raw_file_name": "HeLa_QC_001",
    "vendor": "bruker",
    "acquisition_mode": "dia",
    "instrument_model": "timsTOF HT"
}
```

All fields are optional (default to empty string). The `traceback` field
has full paths stripped to filenames only by the client before sending.

## What gets stored

Each entry in `error_reports.json` includes the payload above plus:

- `received_at` -- server-side UTC timestamp
- `client_ip_hash` -- first 16 hex chars of SHA256(IP), for rate-limit forensics only

The IP address itself is never stored.

## Rate limiting

Simple in-memory counter: max 100 reports per hour per IP address. Returns
HTTP 429 if exceeded. The counter resets when the Space restarts (which is
fine -- this just prevents runaway loops from flooding the store).

## Viewing reports

Error reports are stored at `error_reports.json` in the Space's working
directory. To view them:

- SSH into the Space or use the file browser in the HF Space settings
- Or uncomment the `/api/error-reports` GET endpoint in the code (see the
  bottom of `hf_space_error_endpoint.py`) to expose them via API

## Related files

| File | Purpose |
|------|---------|
| `stan/telemetry.py` | Client-side: builds payload, sends to relay |
| `docs/hf_space_error_endpoint.py` | Server-side: endpoint code to paste into Space |
| `~/.stan/community.yml` | User opt-in: `error_telemetry: true` |
| `~/.stan/error_log.json` | Local error log (always written, last 100 entries) |
