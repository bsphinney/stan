"""STAN Community Benchmark — Relay API + Public Dashboard.

This HF Space serves two purposes:
1. Relay API: accepts community benchmark submissions from STAN clients
   and writes them to the brettsp/stan-benchmark dataset. Users never
   need an HF token — this Space handles authentication.
2. Public dashboard: community reference ranges, instrument health explorer.

Hosted at: https://huggingface.co/spaces/brettsp/stan
"""

from __future__ import annotations

import io
import io
import json
import logging
import os
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Version of THIS Space, shown in the page footer and served at
# /api/version. Distinct from PINNED_DIANN_VERSION (a DIA-NN pin) and
# from the STAN client version — the Space and the client release
# independently. Bump on every deploy.
SPACE_VERSION = "1.1.0"

app = FastAPI(title="STAN Community Benchmark", version=SPACE_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# gzip JSON responses (e.g. /api/leaderboard ~15MB -> ~3.5MB); browsers send Accept-Encoding
app.add_middleware(GZipMiddleware, minimum_size=1000)

HF_DATASET_REPO = "brettsp/stan-benchmark"
HF_TOKEN = os.environ.get("HF_TOKEN", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")

# ── Submissions cache ────────────────────────────────────────────
# The /api/leaderboard and /api/cohorts/{id}/tic endpoints used to call
# hf_hub_download once per submission parquet on every request. At 83+
# submissions this made the dashboard painfully slow to load. We cache
# the concatenated DataFrame in memory with a TTL and use
# snapshot_download to fetch all parquets in parallel on a cache miss.
_SUBMISSIONS_CACHE: dict = {"df": None, "ts": 0.0}
_SUBMISSIONS_CACHE_LOCK = threading.Lock()
SUBMISSIONS_CACHE_TTL_SEC = 300  # 5 minutes


def _load_all_submissions(force_refresh: bool = False) -> "pl.DataFrame | None":
    """Return a single concatenated DataFrame of all submissions, cached.

    Uses huggingface_hub.snapshot_download with allow_patterns to pull
    every submissions/*.parquet file in parallel (much faster than a
    per-file hf_hub_download loop). Results are memoised for
    SUBMISSIONS_CACHE_TTL_SEC seconds, protected by a lock.

    force_refresh=True bypasses the cache (used by ?refresh=1 and by
    /api/submit after a successful upload).
    """
    now = time.time()
    with _SUBMISSIONS_CACHE_LOCK:
        cached = _SUBMISSIONS_CACHE.get("df")
        cached_ts = _SUBMISSIONS_CACHE.get("ts", 0.0)
        if (
            not force_refresh
            and cached is not None
            and (now - cached_ts) < SUBMISSIONS_CACHE_TTL_SEC
        ):
            return cached

    # Cache miss — fetch outside the lock so concurrent readers aren't blocked.
    df = None

    # Fast path (default): read the single consolidated benchmark_latest.parquet
    # (~2.4MB, rebuilt nightly by consolidate_benchmark.yml) instead of
    # snapshot_download + parse of thousands of per-submission parquets. This is
    # what keeps a COLD load fast (~1s vs many seconds). Skipped on force_refresh
    # so a just-submitted run can still be surfaced via the per-submission path.
    if not force_refresh:
        try:
            from huggingface_hub import hf_hub_download
            latest = hf_hub_download(
                repo_id=HF_DATASET_REPO,
                filename="benchmark_latest.parquet",
                repo_type="dataset",
                token=HF_TOKEN,
            )
            cand = pl.read_parquet(latest)
            if cand is not None and not cand.is_empty():
                df = cand
        except Exception:
            logger.warning(
                "benchmark_latest.parquet unavailable; falling back to "
                "per-submission snapshot", exc_info=True,
            )

    if df is None:
        try:
            from huggingface_hub import snapshot_download
            snap_dir = snapshot_download(
                repo_id=HF_DATASET_REPO,
                repo_type="dataset",
                allow_patterns=["submissions/*.parquet"],
                token=HF_TOKEN,
            )
            sub_dir = Path(snap_dir) / "submissions"
            parquet_paths = sorted(sub_dir.glob("*.parquet")) if sub_dir.exists() else []
            if not parquet_paths:
                df = pl.DataFrame()
            else:
                dfs = []
                for fp in parquet_paths:
                    try:
                        dfs.append(pl.read_parquet(fp))
                    except Exception:
                        logger.warning("Failed to read %s", fp.name)
                if not dfs:
                    df = pl.DataFrame()
                else:
                    df = pl.concat(dfs, how="diagonal_relaxed")
        except Exception:
            logger.exception("snapshot_download of submissions failed")
            # Return the stale cache if we have one — better than nothing.
            with _SUBMISSIONS_CACHE_LOCK:
                return _SUBMISSIONS_CACHE.get("df")

    with _SUBMISSIONS_CACHE_LOCK:
        _SUBMISSIONS_CACHE["df"] = df
        _SUBMISSIONS_CACHE["ts"] = time.time()
    return df


def _invalidate_submissions_cache() -> None:
    """Drop the cached DataFrame so the next read refetches from HF."""
    with _SUBMISSIONS_CACHE_LOCK:
        _SUBMISSIONS_CACHE["df"] = None
        _SUBMISSIONS_CACHE["ts"] = 0.0


# ── Batched-commit submission worker ─────────────────────────────
# HF Hub rate-limits dataset commits at 256/hour per repo. Per-submission
# upload_file() = one commit each, which means a backfill of 3K+ rows
# (or a busy day with multiple labs submitting concurrently) breaks the
# limit and rejects valid submissions with 429s.
#
# The fix is to queue submissions in memory and flush them as a single
# multi-file commit every ~60 seconds via HfApi.create_commit(). One
# commit can hold 100+ files, so the effective throughput is ~10,000+
# submissions/hour with the same rate-limit budget.
#
# Trade-off: if the Space restarts before a flush, in-flight queued
# items are lost. STAN clients are idempotent so they re-attempt next
# submit-all. Queue size is bounded by FLUSH_INTERVAL and FLUSH_MAX_BATCH
# so memory pressure stays in check.

import queue
from huggingface_hub.hf_api import CommitOperationAdd

_SUBMIT_QUEUE: queue.Queue[tuple[str, bytes]] = queue.Queue()
FLUSH_INTERVAL_SEC = 60          # seconds between batch flushes
FLUSH_MAX_BATCH = 100            # max files per HF commit
_FLUSH_WORKER_STARTED = False
_FLUSH_LOCK = threading.Lock()


def _queue_submission(submission_id: str, parquet_bytes: bytes) -> None:
    """Add a submission's parquet payload to the batch-commit queue.

    Called from /api/submit. Returns immediately — the actual HF Dataset
    write happens in the worker thread below.
    """
    _SUBMIT_QUEUE.put((submission_id, parquet_bytes))
    _ensure_flush_worker_started()


def _ensure_flush_worker_started() -> None:
    """Lazy-start the background flush worker on first submission."""
    global _FLUSH_WORKER_STARTED
    with _FLUSH_LOCK:
        if _FLUSH_WORKER_STARTED:
            return
        t = threading.Thread(target=_flush_worker, daemon=True, name="hf-batch-commit")
        t.start()
        _FLUSH_WORKER_STARTED = True
        logger.info("Started HF batch-commit worker (interval=%ds, max_batch=%d)",
                    FLUSH_INTERVAL_SEC, FLUSH_MAX_BATCH)


def _flush_worker() -> None:
    """Drain the submission queue periodically, committing as one batch.

    Loops forever. Each cycle:
      1. Wait FLUSH_INTERVAL_SEC OR until queue has FLUSH_MAX_BATCH items
      2. Drain up to FLUSH_MAX_BATCH items
      3. create_commit() with all parquets in one HF commit
      4. On 429 (rate limit): re-enqueue items, double the sleep, retry
      5. On other error: re-enqueue items, log, retry next cycle
    """
    from huggingface_hub import HfApi

    api = HfApi(token=HF_TOKEN)
    backoff = FLUSH_INTERVAL_SEC

    while True:
        # Wait for either the interval to expire or the queue to fill up.
        time.sleep(backoff)

        # Drain up to FLUSH_MAX_BATCH items from the queue.
        items: list[tuple[str, bytes]] = []
        while len(items) < FLUSH_MAX_BATCH:
            try:
                items.append(_SUBMIT_QUEUE.get_nowait())
            except queue.Empty:
                break
        if not items:
            backoff = FLUSH_INTERVAL_SEC
            continue

        operations = [
            CommitOperationAdd(
                path_in_repo=f"submissions/{sid}.parquet",
                path_or_fileobj=io.BytesIO(buf),
            )
            for sid, buf in items
        ]
        try:
            api.create_commit(
                repo_id=HF_DATASET_REPO,
                repo_type="dataset",
                operations=operations,
                commit_message=f"Batch submit {len(items)} runs",
            )
            logger.info("HF batch commit OK: %d files", len(items))
            _invalidate_submissions_cache()
            backoff = FLUSH_INTERVAL_SEC   # reset backoff on success
        except Exception as e:
            # Re-enqueue the items so they get retried next cycle. Use
            # exponential backoff on rate-limit responses so we don't
            # hammer HF when it's already pushing back.
            for sid, buf in items:
                _SUBMIT_QUEUE.put((sid, buf))
            if "429" in str(e) or "Too Many Requests" in str(e):
                backoff = min(backoff * 2, 900)   # cap at 15 min
                logger.warning("HF batch commit rate-limited; backing off %ds", backoff)
            else:
                logger.exception("HF batch commit failed; will retry next cycle")
                backoff = FLUSH_INTERVAL_SEC


# ── Identity verification (email-based name claiming) ────────────
# Labs claim a pseudonym via email verification. The relay stores a
# JSON mapping {pseudonym → {email_hash, token_hash, claimed_at}} in
# the HF Dataset at identity/claims.json. On submission, the token
# is validated — no one can submit as "Clogged PeakTail" without the
# token that was emailed to the owner.
import hashlib
import json
import secrets
import time

IDENTITY_FILE = "identity/claims.json"
_pending_codes: dict[str, dict] = {}  # in-memory: pseudonym → {code, email_hash, expires}


def _hash(s: str) -> str:
    return hashlib.sha256(s.strip().lower().encode()).hexdigest()[:32]


def _load_claims() -> dict:
    """Load claimed names from HF Dataset."""
    try:
        from huggingface_hub import hf_hub_download
        p = hf_hub_download(HF_DATASET_REPO, IDENTITY_FILE, repo_type="dataset", token=HF_TOKEN)
        return json.loads(open(p).read())
    except Exception:
        return {}


def _save_claims(claims: dict) -> None:
    """Write claims back to HF Dataset."""
    import io
    from huggingface_hub import HfApi
    api = HfApi(token=HF_TOKEN)
    buf = io.BytesIO(json.dumps(claims, indent=2).encode())
    api.upload_file(
        path_or_fileobj=buf,
        path_in_repo=IDENTITY_FILE,
        repo_id=HF_DATASET_REPO,
        repo_type="dataset",
        commit_message="Update identity claims",
    )


def _send_verification_email(email: str, code: str, pseudonym: str) -> bool:
    """Send a 6-digit verification code via Resend."""
    if not RESEND_API_KEY:
        logger.error("RESEND_API_KEY not set — check HF Space secrets")
        return False
    try:
        # Use requests if available (more reliable from Docker containers),
        # fall back to urllib if not.
        payload = {
            "from": "STAN Community <noreply@stan-proteomics.org>",
            "to": [email],
            "subject": f"STAN verification code: {code}",
            "html": (
                f"<h2>STAN Community Benchmark</h2>"
                f"<p>Your verification code for <b>{pseudonym}</b> is:</p>"
                f"<h1 style='letter-spacing:0.3em;color:#38bdf8'>{code}</h1>"
                f"<p>Enter this code in your terminal to claim your lab name.</p>"
                f"<p style='color:#888;font-size:0.85em'>"
                f"Your email is NEVER stored — only a one-way hash for verification.<br>"
                f"If you didn't request this, ignore this email. "
                f"Code expires in 15 minutes.</p>"
            ),
        }
        try:
            import requests as _req
            r = _req.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
                timeout=15,
            )
            r.raise_for_status()
            result = r.json()
        except ImportError:
            # Fall back to urllib
            import urllib.request
            data = json.dumps(payload).encode()
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=data,
                headers={
                    "Authorization": f"Bearer {RESEND_API_KEY}",
                    "Content-Type": "application/json",
                    "User-Agent": "STAN/0.2.0",
                },
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read())

        logger.info("Verification email sent to %s for %s: %s", email[:5]+"...", pseudonym, result.get("id"))
        return True
    except Exception as e:
        logger.exception("Failed to send verification email: %s", str(e))
        return False


# DIA-NN version pinning — the community libraries were built with this version.
# Submissions from other major.minor versions are rejected because they produce
# non-comparable results. Commercial users on older versions can still use STAN
# locally; only community submission requires the pinned version.
PINNED_DIANN_VERSION = "2.3"  # major.minor only — allow patch differences


# ── IPS v2 (Instrument Performance Score) — cohort-calibrated ──────
# Computed server-side from submitted metrics so all submissions use the
# same formula regardless of STAN client version.
#
# IPS v2 is calibrated against 359 real UCD HeLa QC runs (April 2026).
# Formula: 50% precursors + 30% peptides + 20% proteins, each scored by
# position within the (instrument_family, spd_bucket) reference cohort:
#     value <= p10       → 0-30   (linear)
#     p10 < value <= p50 → 30-60  (linear)
#     p50 < value <= p90 → 60-90  (linear)
#     value > p90        → 90-100 (asymptotic, 1.5·p90 = 100)
# A run at its cohort median scores exactly 60; p90 scores exactly 90.
#
# Keep this table in sync with stan/metrics/chromatography.py:IPS_REFERENCES.

IPS_REFERENCES: dict[tuple, dict] = {
    ("Exploris 480", "deep"):   {"n": 8,   "precursors": (26974, 31698, 35425), "peptides": (23845, 28545, 32111), "proteins": (3373, 3993, 4195)},
    ("Exploris 480", "medium"): {"n": 46,  "precursors": (19159, 25259, 29874), "peptides": (17550, 23020, 27081), "proteins": (2539, 3104, 3478)},
    ("Lumos", "deep"):   {"n": 26,  "precursors": (31423, 39149, 56834), "peptides": (29066, 35762, 50551), "proteins": (3982, 4510, 5509)},
    ("Lumos", "medium"): {"n": 64,  "precursors": (16705, 27251, 37839), "peptides": (15452, 24847, 33727), "proteins": (2657, 3614, 4347)},
    ("timsTOF HT", "fast"):   {"n": 74,  "precursors": (32305, 42778, 48757), "peptides": (28864, 38195, 43578), "proteins": (4300, 4768, 5104)},
    ("timsTOF HT", "medium"): {"n": 30,  "precursors": (36153, 45262, 50142), "peptides": (32779, 40531, 44574), "proteins": (4730, 4972, 5160)},
    ("timsTOF HT", "ultra"):  {"n": 104, "precursors": (25203, 37051, 45731), "peptides": (23722, 33106, 40851), "proteins": (3940, 4509, 4945)},
    # family-wide fallbacks (any SPD)
    ("timsTOF HT", "*"):      {"n": 208, "precursors": (30003, 40364, 47857), "peptides": (26423, 36155, 42531), "proteins": (4141, 4703, 5068)},
    ("Exploris 480", "*"):      {"n": 54,  "precursors": (19159, 25908, 31036), "peptides": (17550, 23474, 28321), "proteins": (2539, 3166, 3775)},
    ("Lumos", "*"):      {"n": 90,  "precursors": (18519, 30522, 47340), "peptides": (16941, 27907, 43154), "proteins": (2965, 3917, 4906)},
}

_GLOBAL_REFERENCE = {
    "n": 352,
    "precursors": (19000, 35000, 48000),
    "peptides":   (17000, 31000, 42000),
    "proteins":   (2900,  4200,  5100),
}


# DDA cohort references (keyed only by instrument family — not enough DDA
# seed data to bucket by SPD yet). Calibrated from 26 healthy Exploris 480
# and 5 timsTOF HT DDA runs in the UCD seed. DDA distributions are
# fundamentally different from DIA (DDA picks top-N per cycle, DIA samples
# all) so DIA references cannot be reused. For DDA the "precursors" key
# stores n_psms anchors.
IPS_REFERENCES_DDA = {
    ("Exploris 480", "*"): {
        "n": 26,
        "precursors": (13705, 18565, 22165),  # n_psms anchors
        "peptides":   (11335, 16630, 19589),
        "proteins":   (2730,  3420,  3815),
    },
    ("timsTOF HT", "*"): {
        "n": 5,
        "precursors": (27005, 27310, 42141),
        "peptides":   (24596, 24950, 37620),
        "proteins":   (3917,  4074,  4853),
    },
}

_DDA_FALLBACK = {
    "n": 0,
    "precursors": (15000, 40000, 80000),
    "peptides":   (12000, 25000, 45000),
    "proteins":   (2500,  3800,  5000),
}


def _get_dda_reference(family):
    if family and (family, "*") in IPS_REFERENCES_DDA:
        return IPS_REFERENCES_DDA[(family, "*")]
    return _DDA_FALLBACK


def _spd_bucket(spd: int | None) -> str:
    if not spd or spd <= 0:
        return "medium"
    if spd <= 15:
        return "deep"
    if spd <= 40:
        return "medium"
    if spd <= 80:
        return "fast"
    return "ultra"


def _get_reference(family: str | None, spd: int | None) -> dict:
    if family:
        key = (family, _spd_bucket(spd))
        if key in IPS_REFERENCES:
            return IPS_REFERENCES[key]
        key2 = (family, "*")
        if key2 in IPS_REFERENCES:
            return IPS_REFERENCES[key2]
    return _GLOBAL_REFERENCE


def _component_score(value: float, p10: float, p50: float, p90: float) -> float:
    """Piecewise-linear 0-100. Anchors: 0→0, p10→30, p50→60, p90→90, 1.5·p90→100."""
    if value is None or value <= 0 or p10 <= 0 or p50 <= 0 or p90 <= 0:
        return 0.0
    if value <= p10:
        return 30.0 * (value / p10)
    if value <= p50:
        return 30.0 + 30.0 * (value - p10) / (p50 - p10)
    if value <= p90:
        return 60.0 + 30.0 * (value - p50) / (p90 - p50)
    excess = min((value - p90) / (0.5 * p90), 1.0)
    return 90.0 + 10.0 * excess


def compute_ips(sub: "BenchmarkSubmission") -> int:
    """Compute IPS v2 (0-100) from a community submission.

    Uses only metrics we reliably measure: n_precursors, n_peptides,
    n_proteins (and n_psms for DDA). Cohort-calibrated from 388 real
    UCD HeLa QC runs (April 2026 seed).

    Weights: 50% precursors/PSMs + 30% peptides + 20% proteins.

    DIA and DDA use separate cohort references because their distributions
    differ on the same instrument (DDA picks top-N per cycle, DIA samples
    all precursors every cycle).
    """
    is_dia = "dia" in sub.acquisition_mode.lower()

    if is_dia:
        ref = _get_reference(sub.instrument_family, sub.spd)
        s_depth = _component_score(sub.n_precursors or 0, *ref["precursors"])
        s_pep = _component_score(sub.n_peptides or 0, *ref["peptides"])
        s_pro = _component_score(sub.n_proteins or 0, *ref["proteins"])
    else:
        ref = _get_dda_reference(sub.instrument_family)
        s_depth = _component_score(sub.n_psms or 0, *ref["precursors"])
        s_pep = _component_score(sub.n_peptides or 0, *ref["peptides"])
        s_pro = _component_score(sub.n_proteins or 0, *ref["proteins"])

    ips = 0.5 * s_depth + 0.3 * s_pep + 0.2 * s_pro
    return int(round(max(0, min(100, ips))))


# ── Submission schema ───────────────────────────────────────────────

class BenchmarkSubmission(BaseModel):
    """A community benchmark submission from a STAN client."""

    stan_version: str
    # v1.0 schema fields — STAN v0.2.256+ stamps these at submit time so
    # the consolidator can route the row to benchmark_latest (v1) vs
    # benchmark_historical (pre-1.0). schema_version="v1.0.0" plus
    # populated fasta_md5 + (DIA only) speclib_md5 → assets_verified=True.
    schema_version: str = ""
    fasta_md5: str = ""
    speclib_md5: str = ""
    # n_precursors / community library precursor count (0-100). >90%
    # means the lab's data is library-limited rather than
    # instrument-limited and the community library should grow to
    # accommodate them.
    library_coverage_pct: float | None = None
    display_name: str = "Anonymous Lab"
    instrument_family: str
    instrument_model: str
    acquisition_mode: str
    spd: int = 0
    gradient_length_min: int = 0
    amount_ng: float = 50.0
    hela_source: str = "Pierce HeLa Protein Digest Standard"
    institution_type: str = "core_facility"
    cohort_id: str

    n_precursors: int = 0
    n_peptides: int = 0
    n_proteins: int = 0
    median_cv_precursor: float = 0.0
    median_fragments_per_precursor: float = 0.0
    ips_score: int = 0
    n_psms: int = 0
    missed_cleavage_rate: float = 0.0
    median_peak_width_sec: float | None = None
    median_points_across_peak: float | None = None
    column_vendor: str = ""
    column_model: str = ""
    lc_system: str = ""  # "evosep" | "custom" | "" (unknown); used to split TIC overlay
    sample_type: str = "hela"  # QC standard: "hela", "k562", "yeast", "ecoli", "hek293"
    fingerprint: str = ""  # for duplicate detection
    diann_version: str = ""  # pinned for reproducibility — must match PINNED_DIANN_VERSION
    # ID-free literature-standard metrics (added 2026-04 per QC survey):
    # NIST MSQC mass-accuracy drift, CPTAC peak capacity, QCloud2 dynamic
    # range, and raw TIC signal components. All free from DIA-NN outputs
    # (report.stats.tsv + report.parquet).
    median_mass_acc_ms1_ppm: float | None = None
    median_mass_acc_ms2_ppm: float | None = None
    ms1_signal: float | None = None
    ms2_signal: float | None = None
    peak_capacity: float | None = None
    dynamic_range_log10: float | None = None
    # Identified TIC trace (128 bins of RT vs signal)
    # For Evosep users: enables cross-lab gradient shape comparison
    # Raw TIC from instrument (Bruker .d) or identified TIC from DIA-NN report
    tic_rt_bins: list[float] | None = None
    tic_intensity: list[float] | None = None
    # Run identification and original acquisition date
    run_name: str = ""
    run_date: str = ""
    # Additional stats (from DIA-NN report.stats.tsv)
    fwhm_rt_min: float | None = None


# ── Hard gates ──────────────────────────────────────────────────────

HARD_GATES = {
    "dia": {"n_precursors_min": 1000, "median_cv_precursor_max": 60.0},
    "dda": {"n_psms_min": 5000},
}


# Fields the dashboard depends on for full panel population. Submissions
# missing any of these are rejected outright when schema_version is v1.x —
# we don't want partial rows polluting cohort plots.
# Fields the dashboard needs for full panel population. Only the
# string fields demand presence (empty string == "missing"). Numeric
# fields are checked for None only — 0 is a legitimate measurement
# (median_mass_acc_ms1_ppm could be 0.0 ppm on a perfectly-calibrated
# instrument; spd=0 is "unknown" but enforced separately by the
# hard gates).
V1_REQUIRED_DIA_STR = {
    "fasta_md5", "speclib_md5",
    "column_vendor", "column_model",
    "run_date", "run_name",
    "cohort_id",
}
V1_REQUIRED_DIA_NUM = {
    "n_precursors", "n_peptides", "n_proteins", "ips_score",
    "median_mass_acc_ms1_ppm", "median_mass_acc_ms2_ppm",
    "ms1_signal", "ms2_signal",
    "median_peak_width_sec", "median_points_across_peak",
    "dynamic_range_log10", "fwhm_rt_min",
    "peak_capacity",
    "library_coverage_pct",
    "spd",
}
V1_REQUIRED_DIA_LIST = {
    "tic_rt_bins", "tic_intensity",
}
V1_REQUIRED_DDA_STR = {
    "fasta_md5",
    "column_vendor", "column_model",
    "run_date", "run_name",
    "cohort_id",
}
V1_REQUIRED_DDA_NUM = {
    "n_psms", "n_peptides", "n_proteins", "ips_score",
    "spd",
    # library_coverage_pct intentionally omitted — DDA does not search
    # a spectral library, so the metric is not meaningful. DIA still
    # requires it (V1_REQUIRED_DIA_NUM).
}


def _check_v1_completeness(sub: BenchmarkSubmission) -> list[str]:
    """Reject v1.0 submissions that don't carry every field the
    dashboard renders. String fields must be non-empty; numeric
    fields must be non-None (0 is allowed — the hard gates handle
    illegal-zero counts separately); list fields must be non-empty.
    Pre-1.0 rows skip this check entirely."""
    sv = (sub.schema_version or "")
    if not (sv.startswith("v1") or sv.startswith("1.")):
        return []
    mode = (sub.acquisition_mode or "").lower()
    if "dda" in mode:
        str_req, num_req, list_req = V1_REQUIRED_DDA_STR, V1_REQUIRED_DDA_NUM, set()
    else:
        str_req, num_req, list_req = V1_REQUIRED_DIA_STR, V1_REQUIRED_DIA_NUM, V1_REQUIRED_DIA_LIST
    missing = []
    for field in str_req:
        v = getattr(sub, field, None)
        if not v:
            missing.append(field)
    for field in num_req:
        v = getattr(sub, field, None)
        if v is None:
            missing.append(field)
    for field in list_req:
        v = getattr(sub, field, None)
        if not v:
            missing.append(field)
    return missing


def _check_gates(sub: BenchmarkSubmission) -> list[str]:
    mode = sub.acquisition_mode.lower()
    gates = HARD_GATES.get(mode, {})
    failures = []
    if "n_precursors_min" in gates and sub.n_precursors < gates["n_precursors_min"]:
        failures.append(f"n_precursors={sub.n_precursors} below minimum {gates['n_precursors_min']}")
    if "median_cv_precursor_max" in gates and sub.median_cv_precursor > gates["median_cv_precursor_max"]:
        failures.append(f"median_cv={sub.median_cv_precursor} above maximum {gates['median_cv_precursor_max']}")
    if "n_psms_min" in gates and sub.n_psms < gates["n_psms_min"]:
        failures.append(f"n_psms={sub.n_psms} below minimum {gates['n_psms_min']}")
    return failures


# ── Routes ──────────────────────────────────────────────────────────

# ── Identity claim/verify endpoints ────────────────────────────────

class ClaimRequest(BaseModel):
    pseudonym: str
    email: str


class VerifyRequest(BaseModel):
    pseudonym: str
    code: str


@app.get("/api/names")
async def list_names() -> dict:
    """List all claimed pseudonyms for autocomplete in stan setup."""
    claims = _load_claims()
    return {"names": sorted(claims.keys())}


@app.post("/api/claim-name")
async def claim_name(req: ClaimRequest) -> dict:
    """Start the name-claim process. Sends a 6-digit code to the email.

    Privacy: the email is NEVER stored. Only a SHA256 hash is kept to
    verify re-claims on new machines. STAN cannot de-anonymize participants.
    The verification code is ephemeral (15 minutes, in-memory only).
    """
    pseudonym = req.pseudonym.strip()
    email = req.email.strip().lower()

    if not pseudonym or not email:
        raise HTTPException(status_code=400, detail="Pseudonym and email are required")

    # Check if already claimed by someone else
    claims = _load_claims()
    if pseudonym in claims:
        existing_hash = claims[pseudonym].get("email_hash", "")
        if existing_hash and existing_hash != _hash(email):
            raise HTTPException(
                status_code=409,
                detail=f"'{pseudonym}' is already claimed by a different email. "
                       "Pick a different name or use the email you originally registered with."
            )

    # Generate 6-digit code
    code = f"{secrets.randbelow(900000) + 100000}"

    # Store in memory (expires in 15 min)
    _pending_codes[pseudonym] = {
        "code": code,
        "email_hash": _hash(email),
        "email_raw": email,  # only held in memory for sending, never persisted
        "expires": time.time() + 900,
    }

    # Send the code
    ok = _send_verification_email(email, code, pseudonym)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send verification email. Try again.")

    return {
        "status": "code_sent",
        "message": f"Verification code sent to {email[:3]}...{email[email.index('@'):]}"
    }


@app.post("/api/verify-claim")
async def verify_claim(req: VerifyRequest) -> dict:
    """Complete the name-claim process. Returns an auth token.

    The token is stored locally at ~/.stan/community.yml and included in
    all future submissions. The relay validates the token on each submission.

    Privacy guarantee: only the SHA256 hash of the email is stored. The
    email itself and the verification code are discarded after verification.
    """
    pseudonym = req.pseudonym.strip()
    code = req.code.strip()

    pending = _pending_codes.get(pseudonym)
    if not pending:
        raise HTTPException(status_code=400, detail="No pending verification for this name. Call /api/claim-name first.")

    if time.time() > pending["expires"]:
        del _pending_codes[pseudonym]
        raise HTTPException(status_code=410, detail="Code expired. Request a new one.")

    if pending["code"] != code:
        raise HTTPException(status_code=403, detail="Incorrect code.")

    # Generate a permanent auth token for this pseudonym
    token = secrets.token_urlsafe(32)

    # Store the claim (email hash + token hash only — never the raw email)
    claims = _load_claims()
    claims[pseudonym] = {
        "email_hash": pending["email_hash"],
        "token_hash": _hash(token),
        "claimed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _save_claims(claims)

    # Clean up
    del _pending_codes[pseudonym]

    return {
        "status": "verified",
        "pseudonym": pseudonym,
        "token": token,  # returned once, stored locally by the client
        "message": f"'{pseudonym}' is now yours. Token saved — use it on all your instruments.",
    }


# ── Arcade leaderboard ───────────────────────────────────────────────
#
# The shared board lives in PG Farm (table `arcade_scores`), written by
# STAN installs. This Space deliberately has NO PG credentials: the
# service account holds DML on every STAN table, and handing that to a
# public Space so people can post arcade scores is wildly out of
# proportion to the thing being protected. Instead we proxy READS from
# the public UC Davis dashboard, which needs no credentials at all.
#
# Writes are therefore not accepted here. The client falls back to
# showing the board read-only, which is the honest state.
ARCADE_SOURCE = "https://ucd.stan-proteomics.org/api/arcade/leaderboard"


@app.get("/api/arcade/leaderboard")
async def api_arcade_leaderboard(game: str = "", limit: int = 5):
    """Proxy the shared leaderboard. Read-only; never touches PG directly."""
    import requests

    try:
        r = requests.get(
            ARCADE_SOURCE,
            params={"game": game, "limit": max(1, min(int(limit), 50))},
            timeout=8,
        )
        if r.status_code != 200:
            return {"scores": [], "read_only": True,
                    "reason": f"upstream {r.status_code}"}
        d = r.json()
        return {"scores": d.get("scores") or d.get("leaderboard") or [],
                "read_only": True}
    except Exception as e:  # noqa: BLE001 - an empty board beats a 500
        return {"scores": [], "read_only": True, "reason": str(e)[:120]}


@app.get("/api/version")
async def api_version():
    """Version of this Space. The dashboard and CLI report their own."""
    return {"version": SPACE_VERSION, "pinned_diann": PINNED_DIANN_VERSION}

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    # INDEX_HTML is a raw string (it contains JS braces), so the version
    # is substituted here rather than interpolated into the literal.
    return HTMLResponse(INDEX_HTML.replace("__SPACE_VERSION__", SPACE_VERSION))


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "dataset": HF_DATASET_REPO}


@app.post("/api/admin/refresh-cache")
async def admin_refresh_cache(request: Request) -> dict:
    """Drop the in-memory submissions cache so the next read refetches
    every parquet from the dataset.

    Required after wipe_v1.py runs — the relay caches the concatenated
    submissions DataFrame for 5 min, and within that window every
    re-submission of a previously-seen fingerprint is rejected as a
    duplicate even though the underlying parquets have been deleted.

    Auth: requires X-STAN-Admin header matching the ADMIN_SECRET env
    var. If ADMIN_SECRET is not set on the Space, the endpoint is
    open (development mode) — set the secret in production.
    """
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    provided = request.headers.get("X-STAN-Admin", "")
    if admin_secret and provided != admin_secret:
        raise HTTPException(status_code=403, detail="Invalid admin secret.")
    _invalidate_submissions_cache()
    return {"status": "cache cleared"}


@app.post("/api/submit")
async def submit(sub: BenchmarkSubmission, request: Request) -> dict:
    if not HF_TOKEN:
        raise HTTPException(status_code=500, detail="HF_TOKEN not configured on Space")

    # ── Auth check: only official STAN installations that completed
    # email verification via `stan setup` can submit. The auth token
    # is issued during /api/verify-claim and stored in community.yml.
    # Forks that skip setup or modify the submission code won't have
    # a valid token.
    auth_token = request.headers.get("X-STAN-Auth", "")
    if auth_token:
        # Verify the token matches the claimed display_name
        claimed = _load_claimed_names()
        name_record = claimed.get(sub.display_name)
        if name_record and name_record.get("token") != auth_token:
            raise HTTPException(
                status_code=403,
                detail="Auth token does not match the claimed display name. "
                       "Run `stan setup` to re-verify your lab identity.",
            )
    else:
        # No token — warn but still accept for now (grace period while
        # existing installations update to v0.2.73+). Once adoption is
        # widespread, change this to a hard reject.
        logger.warning(
            "Submission from '%s' has no X-STAN-Auth token — "
            "accepting during grace period. Will require auth in a future release.",
            sub.display_name,
        )

    # v1.0 completeness gate — refuse partial rows that would leave
    # dashboard panels empty.
    missing_v1 = _check_v1_completeness(sub)
    if missing_v1:
        raise HTTPException(
            status_code=422,
            detail=(
                "Submission rejected: incomplete v1.0 schema. "
                f"Missing: {', '.join(missing_v1)}. "
                "Update STAN to the latest version + re-run submit."
            ),
        )

    failures = _check_gates(sub)
    if failures:
        raise HTTPException(status_code=422, detail=f"Submission rejected: {'; '.join(failures)}")

    # DIA-NN version check — must match pinned community version
    if sub.diann_version:
        ver_parts = sub.diann_version.split(".")
        pinned_parts = PINNED_DIANN_VERSION.split(".")
        if len(ver_parts) < 2 or ver_parts[0] != pinned_parts[0] or ver_parts[1] != pinned_parts[1]:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"DIA-NN version mismatch: submission used {sub.diann_version}, "
                    f"community benchmark requires {PINNED_DIANN_VERSION}.x. "
                    f"The community libraries were built with DIA-NN {PINNED_DIANN_VERSION} "
                    f"and different versions produce non-comparable results. "
                    f"Use DIA-NN {PINNED_DIANN_VERSION}.x for community submissions, or use STAN "
                    f"locally with any version for private QC."
                ),
            )

    # Dedup check — reject if this fingerprint is already in the dataset.
    # Reuses the shared submissions cache (5 min TTL) so we don't
    # re-download every parquet on every submission.
    if sub.fingerprint:
        try:
            existing = _load_all_submissions()
            if (
                existing is not None
                and not existing.is_empty()
                and "fingerprint" in existing.columns
                and sub.fingerprint in existing["fingerprint"].to_list()
            ):
                raise HTTPException(
                    status_code=409,
                    detail=f"Duplicate submission: fingerprint {sub.fingerprint} already exists. "
                           f"This run appears to have been submitted before from the same lab.",
                )
        except HTTPException:
            raise
        except Exception:
            logger.warning("Dedup check failed, proceeding with submission", exc_info=True)

    # Compute IPS server-side if not provided
    if not sub.ips_score:
        sub.ips_score = compute_ips(sub)

    submission_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)

    row = {
        "submission_id": [submission_id],
        "submitted_at": [now],
        "stan_version": [sub.stan_version],
        "display_name": [sub.display_name],
        "instrument_family": [sub.instrument_family],
        "instrument_model": [sub.instrument_model],
        "acquisition_mode": [sub.acquisition_mode],
        "spd": [sub.spd],
        "gradient_length_min": [sub.gradient_length_min],
        "amount_ng": [sub.amount_ng],
        "n_precursors": [sub.n_precursors],
        "n_peptides": [sub.n_peptides],
        "n_proteins": [sub.n_proteins],
        "n_psms": [sub.n_psms],
        "median_cv_precursor": [sub.median_cv_precursor],
        "median_fragments_per_precursor": [sub.median_fragments_per_precursor],
        "ips_score": [sub.ips_score],
        "missed_cleavage_rate": [sub.missed_cleavage_rate],
        "median_peak_width_sec": [sub.median_peak_width_sec or 0.0],
        "median_points_across_peak": [sub.median_points_across_peak or 0.0],
        "column_vendor": [sub.column_vendor],
        "column_model": [sub.column_model],
        "lc_system": [sub.lc_system],
        "sample_type": [sub.sample_type],
        "community_score": [0.0],
        "cohort_id": [sub.cohort_id],
        "is_flagged": [False],
        "fingerprint": [sub.fingerprint],
        "diann_version": [sub.diann_version],
        "median_mass_acc_ms1_ppm": [sub.median_mass_acc_ms1_ppm],
        "median_mass_acc_ms2_ppm": [sub.median_mass_acc_ms2_ppm],
        "ms1_signal":  [sub.ms1_signal],
        "ms2_signal":  [sub.ms2_signal],
        "peak_capacity": [sub.peak_capacity],
        "dynamic_range_log10": [sub.dynamic_range_log10],
        "tic_rt_bins": [json.dumps(sub.tic_rt_bins) if sub.tic_rt_bins else None],
        "tic_intensity": [json.dumps(sub.tic_intensity) if sub.tic_intensity else None],
        "run_name": [sub.run_name],
        "run_date": [sub.run_date],
        "fwhm_rt_min": [sub.fwhm_rt_min],
        "schema_version": [sub.schema_version],
        "fasta_md5": [sub.fasta_md5],
        "speclib_md5": [sub.speclib_md5],
        "library_coverage_pct": [sub.library_coverage_pct],
    }

    schema = pa.schema([
        pa.field("submission_id", pa.string()),
        pa.field("submitted_at", pa.timestamp("us", tz="UTC")),
        pa.field("stan_version", pa.string()),
        pa.field("display_name", pa.string()),
        pa.field("instrument_family", pa.string()),
        pa.field("instrument_model", pa.string()),
        pa.field("acquisition_mode", pa.string()),
        pa.field("spd", pa.int32()),
        pa.field("gradient_length_min", pa.int32()),
        pa.field("amount_ng", pa.float32()),
        pa.field("n_precursors", pa.int32()),
        pa.field("n_peptides", pa.int32()),
        pa.field("n_proteins", pa.int32()),
        pa.field("n_psms", pa.int32()),
        pa.field("median_cv_precursor", pa.float32()),
        pa.field("median_fragments_per_precursor", pa.float32()),
        pa.field("ips_score", pa.int32()),
        pa.field("missed_cleavage_rate", pa.float32()),
        pa.field("median_peak_width_sec", pa.float32()),
        pa.field("median_points_across_peak", pa.float32()),
        pa.field("column_vendor", pa.string()),
        pa.field("column_model", pa.string()),
        pa.field("lc_system", pa.string()),
        pa.field("sample_type", pa.string()),
        pa.field("community_score", pa.float32()),
        pa.field("cohort_id", pa.string()),
        pa.field("is_flagged", pa.bool_()),
        pa.field("fingerprint", pa.string()),
        pa.field("diann_version", pa.string()),
        pa.field("median_mass_acc_ms1_ppm", pa.float32()),
        pa.field("median_mass_acc_ms2_ppm", pa.float32()),
        pa.field("ms1_signal", pa.float64()),
        pa.field("ms2_signal", pa.float64()),
        pa.field("peak_capacity", pa.float32()),
        pa.field("dynamic_range_log10", pa.float32()),
        pa.field("tic_rt_bins", pa.string()),      # JSON array of 128 RT bin centers
        pa.field("tic_intensity", pa.string()),    # JSON array of 128 signal values
        pa.field("run_name", pa.string()),         # original run filename (e.g. "Astral_HeLa_250ng_01.raw")
        pa.field("run_date", pa.string()),         # ISO date of acquisition (not submission)
        pa.field("fwhm_rt_min", pa.float32()),     # median FWHM in minutes from report.stats.tsv
        # v1.0 schema fields (STAN v0.2.256+):
        pa.field("schema_version", pa.string()),   # "v1.0.0" / "" if pre-1.0
        pa.field("fasta_md5", pa.string()),        # MD5 of frozen community FASTA
        pa.field("speclib_md5", pa.string()),      # MD5 of frozen vendor speclib (DIA only)
        pa.field("library_coverage_pct", pa.float32()),  # n_precursors / community library size
    ])

    table = pa.table(row, schema=schema)
    buf = io.BytesIO()
    pq.write_table(table, buf)
    buf.seek(0)

    # Queue the parquet bytes for the background batch-commit worker. The
    # relay accepts the submission immediately and returns success — the
    # actual HF Dataset upload happens in a batched commit (one HF commit
    # per ~50 submissions instead of one-per-submission) which dodges
    # HuggingFace's 256-commits/hour rate limit. The worker retries on
    # transient failures, so the client doesn't see Upload-failed errors
    # from S3/HF backpressure.
    _queue_submission(submission_id, buf.getvalue())

    logger.info("Submission queued: %s (%s, %s)", submission_id[:8], sub.instrument_family, sub.cohort_id)
    return {"submission_id": submission_id, "cohort_id": sub.cohort_id, "status": "accepted"}


# Fields the client is allowed to patch on an existing submission.
# These are all metadata that can be re-derived from the raw file without
# re-running a search (SPD, LC system, acquisition date, TIC traces,
# etc.). We do NOT allow patching of n_precursors / n_psms / cv / etc.
# because those would let a lab rewrite its search results after the fact.
_UPDATABLE_FIELDS = {
    "spd",
    "gradient_length_min",
    "lc_system",
    "sample_type",
    "run_date",
    "run_name",
    "display_name",
    "column_vendor",
    "column_model",
    "amount_ng",
    "cohort_id",
    "diann_version",
    "instrument_family",
    "instrument_model",
    # TIC traces are re-extracted from the raw file (Bruker analysis.tdf
    # or DIA-NN report.parquet) by `stan backfill-tic`, not from a
    # re-run of the search. Allowed via this endpoint.
    "tic_rt_bins",
    "tic_intensity",
    # Outlier / QC flagging — for marking failed injections, wrong
    # cell lines, amount mismatches, etc. after the fact.
    "is_flagged",
    # Metric backfill — for fixing rows where search completed
    # (precursors populated) but peptide/protein counting was buggy
    # in an older STAN version. NOT for inflating numbers post-hoc.
    "n_peptides",
    "n_proteins",
    # Stats from report.stats.tsv also qualify as derived metadata that
    # can be rebuilt from existing search output without a re-search.
    "fwhm_rt_min",
    "ms1_signal",
    "ms2_signal",
    "median_mass_acc_ms1_ppm",
    "median_mass_acc_ms2_ppm",
    "peak_capacity",
    "dynamic_range_log10",
    "median_points_across_peak",
    "median_peak_width_sec",
}


@app.post("/api/update/{submission_id}")
async def update_submission(submission_id: str, request: Request) -> dict:
    """Patch fields on an existing submission parquet in place.

    Used by `stan repair-metadata --push` to fix historical submissions
    that had wrong SPD / run_date / lc_system because of the client
    baseline bug. Metadata only — result fields (n_precursors, n_psms,
    median_cv_precursor, …) cannot be changed via this endpoint.

    Auth: requires X-STAN-Admin header matching the ADMIN_SECRET env var.
    This prevents forks from modifying existing community data.
    """
    # Only the admin (Brett) or authenticated STAN clients can patch
    # existing submissions. This prevents forks from corrupting data.
    admin_secret = os.environ.get("ADMIN_SECRET", "")
    provided = request.headers.get("X-STAN-Admin", "")
    auth_token = request.headers.get("X-STAN-Auth", "")

    if admin_secret and not provided and not auth_token:
        raise HTTPException(
            status_code=403,
            detail="Update requires authentication. "
                   "Use stan repair-metadata --push (sends X-STAN-Auth) "
                   "or provide X-STAN-Admin header.",
        )
    if admin_secret and provided and provided != admin_secret:
        raise HTTPException(status_code=403, detail="Invalid admin secret.")

    try:
        patch = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(patch, dict) or not patch:
        raise HTTPException(status_code=400, detail="Patch must be a non-empty object")

    # Whitelist
    bad = [k for k in patch if k not in _UPDATABLE_FIELDS]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"Fields not updatable: {bad}. Allowed: {sorted(_UPDATABLE_FIELDS)}",
        )

    try:
        from huggingface_hub import HfApi, hf_hub_download
    except ImportError:
        raise HTTPException(status_code=500, detail="huggingface_hub unavailable")

    parquet_name = f"submissions/{submission_id}.parquet"
    api = HfApi(token=HF_TOKEN)

    # Normalize the patch — TIC trace columns are stored as JSON-encoded
    # strings in the parquet (see the /api/submit write path), so list
    # values must be serialized before assignment.
    _JSON_ENCODED = {"tic_rt_bins", "tic_intensity"}
    normalized_patch = {}
    for k, v in patch.items():
        if k in _JSON_ENCODED and isinstance(v, list):
            normalized_patch[k] = json.dumps(v)
        else:
            normalized_patch[k] = v

    def _apply_patch_to_df(df: pl.DataFrame) -> pl.DataFrame:
        """Apply the normalized patch to every row in ``df``.

        Used for the single-row individual-file case. For the seed-file
        case we call this on a filtered one-row frame via _apply_patch_
        to_seed_row below.
        """
        expressions = []
        for k, v in normalized_patch.items():
            if k not in df.columns:
                if isinstance(v, int):
                    df = df.with_columns(pl.lit(None).cast(pl.Int32).alias(k))
                elif isinstance(v, float):
                    df = df.with_columns(pl.lit(None).cast(pl.Float32).alias(k))
                else:
                    df = df.with_columns(pl.lit(None).cast(pl.Utf8).alias(k))
            expressions.append(pl.lit(v).alias(k))
        return df.with_columns(expressions)

    cache = tempfile.mkdtemp(prefix="hf_update_")
    try:
        # ── Path A: individual per-submission parquet ──────────────
        individual_path = None
        try:
            individual_path = hf_hub_download(
                repo_id=HF_DATASET_REPO,
                filename=parquet_name,
                repo_type="dataset",
                token=HF_TOKEN,
                cache_dir=cache,
            )
        except Exception as e:
            logger.info(
                "Update: no individual file for %s (%s); will try seed fallback",
                submission_id[:8], type(e).__name__,
            )

        if individual_path is not None:
            df = pl.read_parquet(individual_path)
            if df.is_empty():
                raise HTTPException(status_code=404, detail="Individual parquet is empty")
            df = _apply_patch_to_df(df)
            buf = io.BytesIO()
            df.write_parquet(buf)
            buf.seek(0)
            try:
                api.upload_file(
                    path_or_fileobj=buf,
                    path_in_repo=parquet_name,
                    repo_id=HF_DATASET_REPO,
                    repo_type="dataset",
                    commit_message=f"Patch {submission_id[:8]}: {sorted(patch.keys())}",
                )
            except Exception as e:
                logger.exception("Failed to upload patched individual %s", submission_id[:8])
                raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

            _invalidate_submissions_cache()
            logger.info(
                "Submission %s patched (individual): %s",
                submission_id[:8], sorted(patch.keys()),
            )
            return {
                "submission_id": submission_id,
                "updated_fields": sorted(patch.keys()),
                "status": "updated",
                "source": "individual",
            }

        # ── Path B: seed-file fallback ─────────────────────────────
        # The row may live inside a multi-row seed parquet
        # (e.g. submissions/ucd_seed_2026-04-05.parquet). List every
        # parquet under submissions/ and scan each non-individual one
        # for a row whose submission_id matches.
        try:
            files = api.list_repo_files(repo_id=HF_DATASET_REPO, repo_type="dataset")
        except Exception as e:
            logger.exception("Failed to list dataset files")
            raise HTTPException(status_code=500, detail=f"list_repo_files failed: {e}")

        seed_candidates = [
            f for f in files
            if f.startswith("submissions/")
            and f.endswith(".parquet")
            and f != parquet_name
            and "seed" in f.lower()  # bulk-uploaded seed files
        ]

        target_seed = None
        target_df = None
        for seed_name in seed_candidates:
            try:
                seed_path = hf_hub_download(
                    repo_id=HF_DATASET_REPO,
                    filename=seed_name,
                    repo_type="dataset",
                    token=HF_TOKEN,
                    cache_dir=cache,
                )
                sdf = pl.read_parquet(seed_path)
            except Exception:
                logger.exception("Failed to read seed %s", seed_name)
                continue
            if "submission_id" not in sdf.columns:
                continue
            if sdf.filter(pl.col("submission_id") == submission_id).height == 0:
                continue
            target_seed = seed_name
            target_df = sdf
            break

        if target_seed is None or target_df is None:
            logger.warning(
                "Update: submission %s not found in any individual file or seed",
                submission_id[:8],
            )
            raise HTTPException(
                status_code=404,
                detail=f"Submission not found: {submission_id}",
            )

        # Split the seed: patched row + everything else.
        matching = target_df.filter(pl.col("submission_id") == submission_id)
        others = target_df.filter(pl.col("submission_id") != submission_id)
        patched_row = _apply_patch_to_df(matching)

        # Reconcile schemas — _apply_patch_to_df may have added columns
        # to `patched_row` that aren't in `others`, and vice versa.
        # diagonal_relaxed handles that.
        new_seed = pl.concat(
            [others, patched_row], how="diagonal_relaxed"
        )

        buf = io.BytesIO()
        new_seed.write_parquet(buf)
        buf.seek(0)
        try:
            api.upload_file(
                path_or_fileobj=buf,
                path_in_repo=target_seed,
                repo_id=HF_DATASET_REPO,
                repo_type="dataset",
                commit_message=(
                    f"Patch {submission_id[:8]} in seed "
                    f"{target_seed.split('/')[-1]}: {sorted(patch.keys())}"
                ),
            )
        except Exception as e:
            logger.exception(
                "Failed to upload patched seed %s for %s", target_seed, submission_id[:8]
            )
            raise HTTPException(status_code=500, detail=f"Seed upload failed: {e}")

        _invalidate_submissions_cache()
        logger.info(
            "Submission %s patched (seed %s): %s",
            submission_id[:8], target_seed.split("/")[-1], sorted(patch.keys()),
        )
        return {
            "submission_id": submission_id,
            "updated_fields": sorted(patch.keys()),
            "status": "updated",
            "source": f"seed:{target_seed.split('/')[-1]}",
        }
    finally:
        try:
            shutil.rmtree(cache, ignore_errors=True)
        except Exception:
            pass


@app.get("/api/leaderboard")
async def leaderboard(refresh: int = 0) -> dict:
    """Fetch all submissions and return as JSON for the dashboard.

    Results are cached in memory for SUBMISSIONS_CACHE_TTL_SEC seconds.
    Pass ?refresh=1 to bypass the cache and force a fresh snapshot_download.
    """
    try:
        df = _load_all_submissions(force_refresh=bool(refresh))
        if df is None or df.is_empty():
            return {"submissions": [], "count": 0}

        # Sort by primary metric descending — defensive, since n_precursors
        # may be missing from DDA submissions.
        if "n_precursors" in df.columns:
            df = df.sort("n_precursors", descending=True, nulls_last=True)
        # TIC traces (~10MB across all rows) are fetched lazily via
        # /api/tic-overlay so the initial leaderboard load stays light.
        slim = df.drop([c for c in ("tic_rt_bins", "tic_intensity") if c in df.columns])
        return {"submissions": slim.to_dicts(), "count": slim.height}
    except Exception:
        logger.exception("Failed to fetch leaderboard")
        return {"submissions": [], "count": 0, "error": "Failed to fetch data"}


@app.get("/api/tic-overlay")
async def tic_overlay(refresh: int = 0) -> dict:
    """Lazy companion to /api/leaderboard: per-submission TIC traces only.
    The Community-tab TIC overlay fetches this after the initial page
    render so the main leaderboard payload stays light."""
    try:
        df = _load_all_submissions(force_refresh=bool(refresh))
        if df is None or df.is_empty():
            return {"traces": [], "count": 0}
        if "tic_rt_bins" not in df.columns or "tic_intensity" not in df.columns:
            return {"traces": [], "count": 0}
        cols = [c for c in ("submission_id", "tic_rt_bins", "tic_intensity")
                if c in df.columns]
        sub = df.select(cols).filter(
            pl.col("tic_rt_bins").is_not_null()
            & pl.col("tic_intensity").is_not_null())
        return {"traces": sub.to_dicts(), "count": sub.height}
    except Exception:
        logger.exception("Failed to fetch tic-overlay")
        return {"traces": [], "count": 0, "error": "Failed to fetch data"}


@app.get("/api/cohorts")
async def cohorts() -> dict:
    try:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(
            repo_id=HF_DATASET_REPO,
            filename="cohort_stats/cohort_percentiles_latest.json",
            repo_type="dataset", token=HF_TOKEN,
        )
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {"cohorts": {}, "note": "No cohort data yet"}


@app.get("/api/cohorts/{cohort_id}/tic")
async def cohort_tic(cohort_id: str, refresh: int = 0) -> dict:
    """Community average TIC trace for a cohort (instrument family + SPD).

    Returns the median TIC across all submissions in the cohort that
    include TIC data. For Evosep users, this enables cross-lab gradient
    shape comparison.

    Reads from the shared submissions cache — no per-request downloads.
    """
    try:
        import numpy as np

        df = _load_all_submissions(force_refresh=bool(refresh))
        if df is None or df.is_empty():
            return {"cohort_id": cohort_id, "median_tic": None, "traces": [], "n_traces": 0}
        if "tic_rt_bins" not in df.columns or "tic_intensity" not in df.columns:
            return {"cohort_id": cohort_id, "median_tic": None, "traces": [], "n_traces": 0}
        if "cohort_id" not in df.columns:
            return {"cohort_id": cohort_id, "median_tic": None, "traces": [], "n_traces": 0}

        cohort_df = df.filter(pl.col("cohort_id") == cohort_id)
        all_tics = []
        for row in cohort_df.iter_rows(named=True):
            rt_json = row.get("tic_rt_bins")
            int_json = row.get("tic_intensity")
            if rt_json and int_json:
                try:
                    rt = json.loads(rt_json)
                    intensity = json.loads(int_json)
                except Exception:
                    continue
                if len(rt) > 10 and len(intensity) > 10:
                    all_tics.append({"rt": rt, "intensity": intensity,
                                    "display_name": row.get("display_name", "")})

        if not all_tics:
            return {"cohort_id": cohort_id, "median_tic": None, "traces": [], "n_traces": 0}

        # Compute median TIC on a common RT grid (use first trace's RT bins)
        common_rt = all_tics[0]["rt"]
        n_bins = len(common_rt)
        all_intensities = []
        for tic in all_tics:
            if len(tic["intensity"]) == n_bins:
                all_intensities.append(tic["intensity"])

        if all_intensities:
            intensity_matrix = np.array(all_intensities)
            median_intensity = np.median(intensity_matrix, axis=0).tolist()
        else:
            median_intensity = None

        return {
            "cohort_id": cohort_id,
            "median_tic": {"rt": common_rt, "intensity": median_intensity} if median_intensity else None,
            "traces": all_tics[:50],  # limit to 50 traces for display
            "n_traces": len(all_tics),
        }

    except Exception:
        logger.exception("Failed to compute cohort TIC")
        return {"cohort_id": cohort_id, "median_tic": None, "traces": [], "n_traces": 0, "error": "Failed"}


# ── Dashboard HTML ──────────────────────────────────────────────────
# NOTE: Community-focused dashboard with reference ranges and percentiles.

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>STAN Community Benchmark</title>
    <!-- PWA: makes the community site installable (Add to Home Screen -> standalone,
         chrome-free) just like the godmode dashboard. -->
    <link rel="manifest" href="/static/manifest.json">
    <meta name="apple-mobile-web-app-capable" content="yes">
    <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
    <meta name="apple-mobile-web-app-title" content="STAN Community">
    <meta name="theme-color" content="#011a3a">
    <link rel="apple-touch-icon" href="/static/icons/apple-touch-icon.png">
    <script src="https://cdn.plot.ly/plotly-2.35.0.min.js"></script>
    <style>
        :root {
            --ucd-blue: #022851;
            --ucd-blue-light: #03396c;
            --ucd-blue-dark: #011a3a;
            --ucd-gold: #FFBF00;
            --ucd-gold-dark: #DAAA00;
            --ucd-gold-glow: rgba(255, 191, 0, 0.15);
            --ucd-gold-border: rgba(255, 191, 0, 0.3);
            --text-primary: #e8eef5;
            --text-secondary: #a0b4cc;
            --text-muted: #6b82a0;
            --card-bg: rgba(2, 40, 81, 0.6);
            --card-border: rgba(255, 191, 0, 0.12);
            --table-header: #011a3a;
            --table-hover: rgba(255, 191, 0, 0.06);
            --table-border: rgba(255, 191, 0, 0.08);
            --green: #34d399; --yellow: #fbbf24; --red: #f87171;
        }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: var(--ucd-blue-dark); color: var(--text-primary);
            min-height: 100vh; padding: 2rem; overflow-x: hidden;
            background-image: linear-gradient(160deg, #011a3a 0%, #022851 50%, #03396c 100%);
        }

        /* Header */
        .header { text-align: center; margin-bottom: 2.5rem; }
        .header h1 {
            font-size: 4.5rem; letter-spacing: 0.25em; font-weight: 900;
            color: var(--ucd-gold);
            text-shadow: 0 0 20px rgba(255,191,0,0.4), 0 0 60px rgba(255,191,0,0.15), 0 4px 8px rgba(0,0,0,0.3);
            background: linear-gradient(180deg, #FFD54F 0%, #FFBF00 40%, #DAAA00 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            background-clip: text;
            filter: drop-shadow(0 2px 8px rgba(255,191,0,0.3));
        }
        .header .subtitle { color: var(--text-secondary); margin-top: 0.5rem; font-size: 1.05rem; }
        .header .tagline { color: var(--text-muted); margin-top: 0.25rem; font-size: 0.95rem; font-style: italic; }
        .header .ucd-badge { display: inline-block; margin-top: 0.75rem; padding: 0.3rem 1rem; border: 1px solid var(--ucd-gold-border); border-radius: 999px; font-size: 0.8rem; color: var(--ucd-gold-dark); background: var(--ucd-gold-glow); }
        .links { margin-top: 1rem; }
        .links a { color: var(--ucd-gold); text-decoration: none; margin: 0 0.75rem; font-weight: 500; }
        .links a:hover { text-decoration: underline; color: #ffe066; }

        /* Stats */
        .stats-row { display: flex; gap: 1.5rem; justify-content: center; flex-wrap: wrap; margin-bottom: 2.5rem; }
        .stat-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 1.5rem 2rem; text-align: center; min-width: 150px; backdrop-filter: blur(8px); }
        .stat-card .number { font-size: 2rem; font-weight: 700; color: var(--ucd-gold); }
        .stat-card .label { color: var(--text-secondary); font-size: 0.85rem; margin-top: 0.25rem; }

        /* Sections */
        .section { margin-bottom: 2.5rem; max-width: 1200px; margin-left: auto; margin-right: auto; }
        .section h2 { font-size: 1.3rem; margin-bottom: 0.5rem; color: var(--text-primary); border-bottom: 2px solid var(--ucd-gold-border); padding-bottom: 0.5rem; }
        .section .description { color: var(--text-secondary); font-size: 0.9rem; margin-bottom: 1rem; line-height: 1.5; }

        /* Charts */
        .chart-row { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; }
        .chart-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 1rem; overflow: hidden; }
        .chart-card h3 { font-size: 0.95rem; color: var(--ucd-gold-dark); margin-bottom: 0.25rem; padding-left: 0.5rem; }
        .chart-card .chart-desc { font-size: 0.8rem; color: var(--text-muted); padding-left: 0.5rem; margin-bottom: 0.5rem; }
        .chart-full { grid-column: 1 / -1; }

        /* Reference ranges */
        .ref-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; margin-top: 1rem; }
        .ref-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 10px; padding: 1.25rem; }
        .ref-card h4 { color: var(--ucd-gold-dark); font-size: 0.9rem; margin-bottom: 0.75rem; }
        .ref-row { display: flex; justify-content: space-between; padding: 0.3rem 0; border-bottom: 1px solid var(--table-border); font-size: 0.85rem; }
        .ref-row:last-child { border-bottom: none; }
        .ref-metric { color: var(--text-secondary); }
        .ref-range { color: var(--text-primary); font-weight: 500; }

        /* Info cards */
        .info-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; }
        .info-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 1.25rem; }
        .info-card h3 { font-size: 0.95rem; color: var(--ucd-gold-dark); margin-bottom: 0.75rem; }
        .info-card p { color: var(--text-secondary); font-size: 0.9rem; line-height: 1.6; }

        /* Tabs */
        .tabs { display: flex; gap: 0.5rem; margin-bottom: 1rem; }
        .tab { padding: 0.5rem 1.25rem; border-radius: 8px; cursor: pointer; background: var(--card-bg); color: var(--text-secondary); border: 1px solid transparent; font-size: 0.9rem; transition: all 0.15s; }
        .tab:hover { border-color: var(--ucd-gold-border); color: var(--text-primary); }
        .tab.active { background: var(--ucd-gold); color: var(--ucd-blue-dark); font-weight: 600; border-color: var(--ucd-gold); }

        /* Table */
        table { width: 100%; border-collapse: collapse; background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; overflow: hidden; }
        #table-container { overflow-x: auto; max-width: 100%; }
        th { background: var(--table-header); padding: 0.75rem 1rem; text-align: left; font-size: 0.8rem; color: var(--ucd-gold-dark); text-transform: uppercase; letter-spacing: 0.05em; border-bottom: 2px solid var(--ucd-gold-border); }
        td { padding: 0.75rem 1rem; border-top: 1px solid var(--table-border); }
        tr:hover td { background: var(--table-hover); }

        /* Badges */
        .badge { display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px; font-size: 0.75rem; font-weight: 600; }
        .badge-dia { background: rgba(2,40,81,0.8); color: #5cb8ff; border: 1px solid rgba(92,184,255,0.3); }
        .badge-dda { background: rgba(60,20,80,0.6); color: #c084fc; border: 1px solid rgba(192,132,252,0.3); }
        .badge-ips-high { background: rgba(6,78,59,0.6); color: var(--green); border: 1px solid rgba(52,211,153,0.3); }
        .badge-ips-mid { background: var(--ucd-gold-glow); color: var(--ucd-gold-dark); border: 1px solid var(--ucd-gold-border); }
        .badge-ips-low { background: rgba(69,10,10,0.6); color: var(--red); border: 1px solid rgba(248,113,113,0.3); }
        .pctile-badge { display: inline-block; padding: 0.2rem 0.65rem; border-radius: 6px; font-size: 0.8rem; font-weight: 600; }
        .pctile-top { background: rgba(6,78,59,0.5); color: var(--green); }
        .pctile-mid { background: rgba(2,40,81,0.5); color: #5cb8ff; }
        .pctile-low { background: rgba(69,10,10,0.4); color: var(--red); }

        .empty-state { text-align: center; padding: 3rem; color: var(--text-muted); background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; }
        .footer { text-align: center; margin-top: 3rem; color: var(--text-muted); font-size: 0.85rem; }
        .footer a { color: var(--text-secondary); }

        @media (max-width: 768px) {
            body { padding: 0.75rem; }
            .header h1 { font-size: 2.5rem; }
            .chart-row, .info-grid { grid-template-columns: 1fr; }
            table { font-size: 0.85rem; }
            th, td { padding: 0.5rem; }
            .chart-card { padding: 0.75rem; }
            .chart-desc { font-size: 0.8rem; line-height: 1.4; }
            .links { gap: 0.4rem; flex-wrap: wrap; justify-content: center; }
            select { max-width: 100%; }
        }
        /* Tap-to-fullscreen for charts (esp. phones) */
        .chart-card { position: relative; }
        .fs-btn { position: absolute; top: 10px; right: 10px; z-index: 6;
            background: rgba(255,191,0,0.15); border: 1px solid rgba(255,191,0,0.4);
            color: #ffbf00; border-radius: 6px; padding: 3px 9px; font-size: 1.05rem;
            line-height: 1; cursor: pointer; }
        .fs-btn:hover { background: rgba(255,191,0,0.28); }
        .chart-card.fs { position: fixed; inset: 0; width: 100vw; height: 100vh;
            margin: 0; border-radius: 0; z-index: 9999; overflow: auto; padding: 2.5rem 0.5rem 0.5rem;
            background: linear-gradient(160deg, #011a3a 0%, #022851 50%, #03396c 100%); }
        /* In fullscreen, show ONLY the figure — hide title, explainer, and controls.
           (Exit fullscreen to change filters.) The close button stays visible. */
        .chart-card.fs > *:not([id^="chart-"]):not(.fs-btn) { display: none !important; }
        .chart-card.fs > div[id^="chart-"], .chart-card.fs .js-plotly-plot { height: 92vh !important; }
        body.fs-open { overflow: hidden; }
    </style>
</head>
<body>

<div class="header">
    <h1>STAN</h1>
    <p class="subtitle">Standardized proteomic Throughput ANalyzer</p>
    <p class="tagline">Know your instrument. Community reference ranges for mass spectrometer QC.</p>
    <div class="ucd-badge">UC Davis Proteomics Core</div>
    <div style="margin-top:0.4rem;font-size:0.78rem;opacity:0.65">community site v__SPACE_VERSION__</div>
    <div style="margin-top:1rem;padding:0.5rem 1.25rem;background:rgba(255,191,0,0.1);border:1px solid var(--ucd-gold-border);border-radius:8px;display:inline-block;font-size:0.85rem;color:var(--ucd-gold-dark)">
        Seeded with 3,800+ longitudinal QC runs from UC Davis across timsTOF HT, Exploris 480, and Fusion Lumos.
        <a href="https://github.com/bsphinney/stan" style="color:var(--ucd-gold);margin-left:0.5rem">Install STAN to contribute your own.</a>
    </div>
    <div class="links">
        <a href="/museum">&#127963; Museum</a>
        <a href="/arcade">&#127918; Arcade</a>
        <a href="https://github.com/bsphinney/stan">GitHub</a>
        <a href="https://huggingface.co/datasets/brettsp/stan-benchmark">Dataset</a>
        <a href="/docs">API</a>
    </div>
</div>

<!-- Summary stats -->
<div class="stats-row">
    <div class="stat-card"><div class="number" id="stat-submissions">--</div><div class="label">Submissions</div></div>
    <div class="stat-card"><div class="number" id="stat-labs">--</div><div class="label">Contributing Labs</div></div>
    <div class="stat-card"><div class="number" id="stat-instruments">--</div><div class="label">Instrument Models</div></div>
    <div class="stat-card" style="display:flex;align-items:center;gap:0.5rem;cursor:pointer" onclick="toggleFailedFilter()">
        <input type="checkbox" id="hide-failed-cb" checked style="accent-color:var(--accent);cursor:pointer">
        <div>
            <div class="number" id="stat-failed" style="font-size:1.1rem">--</div>
            <div class="label">Hide failed runs</div>
        </div>
    </div>
</div>

<!-- Sample type filter -->
<div style="display:flex;align-items:center;gap:1rem;margin:0.5rem 2rem 0.5rem 2rem;flex-wrap:wrap">
    <label style="color:var(--text-secondary);font-size:0.85rem;font-weight:600">QC Standard:</label>
    <select id="sample-type-select" onchange="changeSampleType(this)"
            style="background:var(--card-bg);color:var(--text-primary);border:1px solid var(--card-border);border-radius:6px;padding:0.35rem 0.7rem;font-size:0.85rem">
        <option value="hela" selected>HeLa (default)</option>
        <option value="k562">K562</option>
        <option value="yeast">Yeast</option>
        <option value="ecoli">E. coli</option>
        <option value="hek293">HEK293</option>
        <option value="all">All standards</option>
    </select>
    <span id="sample-type-counts" style="color:var(--text-muted);font-size:0.8rem"></span>
</div>

<!-- Community Reference Ranges -->
<div class="section">
    <h2>Community Reference Ranges</h2>
    <p class="description">
        Expected performance ranges established by the community. Use the filters below to
        find your instrument and method. Ranges update automatically as more labs contribute.
    </p>
    <div id="ref-filters" style="margin-bottom:1rem; display:flex; flex-wrap:wrap; gap:1.5rem; align-items:flex-start;"></div>
    <div id="ref-ranges-container" class="ref-grid">
        <div class="empty-state">Loading community data...</div>
    </div>
</div>

<!-- Best Configurations leaderboard — answers the headline question
     "what instrument × SPD × amount loaded gives the best data?" in
     a single ranked table. -->
<div class="section">
    <h2>Best Configurations <span id="config-leaderboard-badge" style="font-size:0.75rem; padding:0.15rem 0.5rem; border-radius:4px; background:rgba(56,189,248,0.2); color:var(--accent); margin-left:0.5rem"></span></h2>
    <p class="description">
        Top instrument × throughput × amount-loaded combinations from community data.
        Each row is one cohort (≥3 submissions). Click any column header to re-rank — pick
        your priority (depth or accuracy) and read the row.
    </p>
    <div class="chart-card chart-full">
        <div id="config-leaderboard" style="overflow-x:auto"></div>
    </div>
</div>

<!-- Where Do You Stand -->
<div class="section">
    <h2>Instrument Health Explorer</h2>
    <p class="description">
        Each point represents an anonymous submission. Distributions show the community range
        for each instrument family. Hover for details.
    </p>
    <div class="chart-row">
        <div class="chart-card">
            <h3>Depth by Amount Loaded <span id="amount-mode-badge" style="font-size:0.75rem; padding:0.15rem 0.5rem; border-radius:4px; background:rgba(56,189,248,0.2); color:var(--accent); margin-left:0.5rem"></span></h3>
            <div class="chart-desc">How does sample load affect identification depth? Each violin is one amount bucket, colored by instrument model. Saturation typically appears between 100–250&nbsp;ng on Orbitrap; timsTOF often climbs further. Sample type from the page filter above; DIA/DDA from the tab below.</div>
            <div id="chart-amount-depth"></div>
        </div>
        <div class="chart-card">
            <h3>Identification Depth by Platform <span id="violin-mode-badge" style="font-size:0.75rem; padding:0.15rem 0.5rem; border-radius:4px; background:rgba(56,189,248,0.2); color:var(--accent); margin-left:0.5rem"></span></h3>
            <div class="chart-desc">How many precursors (DIA) or PSMs (DDA) does each platform typically identify? Apples-to-apples comparison — pick a load amount so 5&nbsp;ng K562 isn't pooled with 200&nbsp;ng HeLa. Each violin is one instrument <em>model</em> (Pro vs Pro 2 vs HT are separate). Sample type comes from the page-level filter above.</div>
            <div style="margin:0.5rem 0; display:flex; align-items:center; gap:0.75rem; flex-wrap:wrap">
                <label style="color:var(--text-muted); font-size:0.85rem">Amount loaded:
                    <select id="violin-amount-filter" onchange="renderViolin()" style="margin-left:0.4rem; padding:0.25rem; background:#0b1d33; color:var(--text); border:1px solid #1e3a5f; border-radius:4px; font-size:0.85rem">
                        <option value="50" selected>50 ng (standard)</option>
                        <option value="ultralow">&lt;10 ng (ultra-low)</option>
                        <option value="low">10–49 ng (low)</option>
                        <option value="high">100–249 ng (high)</option>
                        <option value="ultrahigh">≥250 ng (ultra-high)</option>
                        <option value="all">All amounts (⚠ mixes loads)</option>
                    </select>
                </label>
                <span style="color:var(--text-muted); font-size:0.75rem">Shape: ○ 50 ng · ◇ &lt;20 ng · □ &gt;100 ng</span>
            </div>
            <div id="chart-violin"></div>
        </div>
    </div>
    <div class="chart-row">
        <div class="chart-card chart-full">
            <h3>Depth by Throughput (which SPD gives me the best data?)</h3>
            <div class="chart-desc">Precursors/PSMs vs. samples-per-day, faceted by instrument family. Each box is one SPD bucket. Filter by amount loaded so you compare apples to apples — the optimal SPD may differ by loading.</div>
            <div style="margin:0.5rem 0; display:flex; align-items:center; gap:0.75rem">
                <label style="color:var(--text-muted); font-size:0.85rem">Amount loaded:
                    <select id="spd-amount-filter" onchange="renderSpdDepth()" style="margin-left:0.4rem; padding:0.25rem; background:#0b1d33; color:var(--text); border:1px solid #1e3a5f; border-radius:4px; font-size:0.85rem">
                        <option value="all">All amounts</option>
                        <option value="50" selected>50 ng (standard)</option>
                        <option value="low">&lt;20 ng (ultra-low)</option>
                        <option value="high">&gt;100 ng (high)</option>
                    </select>
                </label>
                <span style="color:var(--text-muted); font-size:0.75rem">Shape: ○ 50 ng · ◇ &lt;20 ng · □ &gt;100 ng</span>
            </div>
            <div id="chart-spd-depth"></div>
        </div>
    </div>
    <div class="chart-row">
        <div class="chart-card chart-full">
            <h3>Identification Depth vs. IPS</h3>
            <div class="chart-desc">Cohort-normalized depth score (IPS) vs. precursor count. IPS is a <b>depth</b> score — not an LC-health metric. Shape: ● timsTOF HT · ◆ Astral · ■ Exploris 480 · ▲ Lumos. Color = SPD. Size ∝ peptide count.</div>
            <div id="chart-ips"></div>
        </div>
    </div>
    <div class="chart-row" id="row-column-compare">
        <div class="chart-card chart-full">
            <h3>Column Comparison (same instrument, SPD, and amount)</h3>
            <div class="chart-desc">How does your LC column compare to others under identical conditions? Each group shows submissions matched by instrument family, SPD, and injection amount — only the column differs.</div>
            <div id="chart-column-compare"></div>
        </div>
    </div>
    <div class="chart-row">
        <div class="chart-card chart-full">
            <h3>Throughput vs. Quantitation Quality (Matthews &amp; Hayes 1976)</h3>
            <div class="chart-desc">SPD vs. data points across peak. Below 6 points, quantitation error exceeds 1%. Shape = LC column. Color = instrument family.</div>
            <div id="chart-points-peak"></div>
        </div>
    </div>
    <!-- ── Real LC-health metrics from the 2024 literature survey ── -->
    <div class="section-header" style="margin-top:2rem">
        <h2 style="color:var(--accent)">LC / Instrument Health (ID-free metrics)</h2>
        <p class="section-desc">These are the metrics the 2024 proteomics QC literature (NIST MSQC, QCloud2, CPTAC, PTXQC) considers the real LC-health signals — they catch failures BEFORE identifications collapse. Unlike IPS (which is a depth rank), these don't depend on how many peptides you identified.</p>
    </div>
    <div class="chart-row">
        <div class="chart-card">
            <h3>Mass Accuracy Drift (MS1)</h3>
            <div class="chart-desc">Median corrected MS1 mass error per run. Tracks Orbitrap / qTOF calibration stability. A rising trend signals lock-mass failure or thermal drift before IDs drop.</div>
            <div id="chart-mass-acc"></div>
        </div>
    </div>
    <div class="chart-row">
        <div class="chart-card">
            <h3>MS1 Signal (TIC proxy)</h3>
            <div class="chart-desc">Total MS1 ion current per run — a proxy for ion source health. Drops of &gt;2× signal dirty emitter, low flow, or sample prep issue before IDs fall.</div>
            <div id="chart-ms1-signal"></div>
        </div>
        <div class="chart-card">
            <h3>Dynamic Range</h3>
            <div class="chart-desc">log<sub>10</sub>(p99 / p01) of precursor intensity. Compresses when the ion source is dirty or the LC is losing pressure. <em>Populated going forward by the STAN watcher.</em></div>
            <div id="chart-dyn-range"></div>
        </div>
    </div>
    <div class="chart-row">
        <div class="chart-card">
            <h3>Points Across Peak</h3>
            <div class="chart-desc">Datapoints per chromatographic peak (per <a href="https://doi.org/10.1021/ac50005a009" style="color:var(--ucd-gold)">Matthews &amp; Hayes 1976</a>). A <strong>rising trend</strong> at constant SPD signals column degradation (peaks broadening). Validated against Spectronaut (median 9 on timsTOF 100 SPD).</div>
            <div id="chart-pts-peak"></div>
        </div>
    </div>
    <div class="chart-row">
        <div class="chart-card chart-full">
            <h3>Instrument Health Fingerprint</h3>
            <div class="chart-desc">Each polygon is one (instrument&nbsp;model × SPD&nbsp;tier) cohort with ≥3 DIA submissions, plotting the median across that cohort. Axes: 3 depth metrics + MS1 mass accuracy (lower&nbsp;ppm → better → outer ring). Normalization is relative to the cohort-median range, so the smallest cohort pins to the center and the largest to the edge.</div>
            <div id="chart-radar"></div>
        </div>
    </div>
    <div class="chart-row">
        <div class="chart-card chart-full">
            <h3>Community TIC Overlay by SPD</h3>
            <div class="chart-desc">Identified (DIA) or raw (DDA) TIC chromatograms grouped by throughput + LC system + acquisition mode. For Evosep users the gradient is standardized &mdash; shape differences reveal instrument-specific issues. Thick dashed line = community median. <strong>Always pick one acquisition mode</strong> &mdash; DIA and DDA have different cycle times and their shapes should not be averaged together. <em>First-of-its-kind cross-lab TIC comparison.</em></div>
            <div style="margin-bottom:0.5rem;">
                <select id="tic-spd-select" style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:0.3rem;padding:0.3rem 0.6rem;font-size:0.85rem;"></select>
                <select id="tic-lc-select" style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:0.3rem;padding:0.3rem 0.6rem;font-size:0.85rem;margin-left:0.4rem;">
                    <option value="all">All LC systems</option>
                    <option value="evosep">Evosep only</option>
                    <option value="custom">Custom / nanoLC only</option>
                </select>
                <select id="tic-mode-select" style="background:var(--card);color:var(--text);border:1px solid var(--border);border-radius:0.3rem;padding:0.3rem 0.6rem;font-size:0.85rem;margin-left:0.4rem;">
                    <option value="dia">DIA only</option>
                    <option value="dda">DDA only</option>
                    <option value="all">DIA + DDA (mixed)</option>
                </select>
                <span id="tic-count" style="color:var(--muted);margin-left:0.5rem;font-size:0.85rem;"></span>
                <label style="color:var(--muted);margin-left:0.6rem;font-size:0.82rem;cursor:pointer;white-space:nowrap;"><input type="checkbox" id="tic-show-all" style="vertical-align:middle;margin-right:0.25rem;" onchange="renderCommunityTIC()">show all traces</label>
            </div>
            <div id="chart-community-tic"></div>
        </div>
    </div>
</div>

<!-- Your Lab vs. Community — the community-appropriate version of Levey-Jennings.
     Instead of tracking "one instrument over time" (which doesn't work when "Lumos"
     aggregates all labs), this shows YOUR LAB's runs plotted over time with the
     COMMUNITY median ± SD as reference bands. You see two things at once:
       1. Your instrument's own trend (are you improving or degrading?)
       2. Where you sit vs. everyone else (above or below the community median?) -->
<div class="section">
    <h2>Your Lab vs. Community</h2>
    <p class="description">
        Your instrument's QC trend over time, with community reference bands (mean ± 1/2/3σ
        from all submissions of the same instrument family). Use this to answer:
        <em>"Is my instrument drifting, and am I above or below the community baseline?"</em>
    </p>
    <div style="margin:0.5rem 0 1rem 0; display:flex; gap:1rem; flex-wrap:wrap; align-items:center">
        <label style="color:var(--text-muted); font-size:0.85rem">Lab:
            <select id="lab-select" onchange="renderLabVsCommunity()" style="margin-left:0.4rem; padding:0.25rem 0.5rem; background:#0b1d33; color:var(--text); border:1px solid #1e3a5f; border-radius:4px; font-size:0.85rem; max-width:250px"></select>
        </label>
        <label style="color:var(--text-muted); font-size:0.85rem">Instrument:
            <select id="lab-instrument" onchange="renderLabVsCommunity()" style="margin-left:0.4rem; padding:0.25rem 0.5rem; background:#0b1d33; color:var(--text); border:1px solid #1e3a5f; border-radius:4px; font-size:0.85rem"></select>
        </label>
        <label style="color:var(--text-muted); font-size:0.85rem">Metric:
            <select id="lab-metric" onchange="renderLabVsCommunity()" style="margin-left:0.4rem; padding:0.25rem 0.5rem; background:#0b1d33; color:var(--text); border:1px solid #1e3a5f; border-radius:4px; font-size:0.85rem">
                <option value="n_precursors">Precursors (DIA)</option>
                <option value="n_proteins">Proteins</option>
                <option value="median_mass_acc_ms1_ppm">MS1 mass accuracy (ppm)</option>
                <option value="ms1_signal">MS1 signal (TIC)</option>
                <option value="ips_score">IPS score</option>
            </select>
        </label>
        <label style="color:var(--text-muted); font-size:0.85rem">Amount:
            <select id="lab-amount" onchange="renderLabVsCommunity()" style="margin-left:0.4rem; padding:0.25rem 0.5rem; background:#0b1d33; color:var(--text); border:1px solid #1e3a5f; border-radius:4px; font-size:0.85rem">
                <option value="all">All amounts</option>
                <option value="50" selected>50 ng (standard)</option>
                <option value="low">&lt;20 ng (ultra-low)</option>
                <option value="high">&gt;100 ng (high)</option>
            </select>
        </label>
    </div>
    <div class="chart-row">
        <div class="chart-card chart-full">
            <h3>Your Trend vs. Community Reference</h3>
            <div class="chart-desc">Blue dots = your lab's runs over time. Shaded bands = community mean ± 1σ (green), ± 2σ (amber), ± 3σ (red). Runs outside ±2σ deserve a look; outside ±3σ = something is wrong or exceptional.</div>
            <div id="chart-lab-trend"></div>
        </div>
    </div>
</div>

<!-- Understanding the metrics -->
<div class="section">
    <h2>Understanding the Metrics</h2>
    <div class="info-grid">
        <div class="info-card">
            <h3>The HeLa Standard</h3>
            <p>
                All STAN benchmarking uses the
                <a href="https://www.thermofisher.com/order/catalog/product/88328" style="color:var(--ucd-gold)">Pierce HeLa Protein Digest Standard</a>
                (Thermo Scientific, cat# 88328 / 88329). This is a tryptic digest of the
                HeLa S3 cell line containing &gt;15,000 proteins with &lt;10% missed cleavages.<br><br>
                Using a single, commercially available standard ensures every lab starts
                from the same sample. Differences in metrics reflect instrument and LC
                performance, not sample preparation variability.<br><br>
                <a href="https://www.thermofisher.com/order/catalog/product/88328" style="color:var(--ucd-gold)">Buy 20 &mu;g (88328)</a> &middot;
                <a href="https://www.thermofisher.com/order/catalog/product/88329" style="color:var(--ucd-gold)">Buy 5 x 20 &mu;g (88329)</a>
            </p>
        </div>
        <div class="info-card">
            <h3>IPS: Instrument Performance Score (0-100)</h3>
            <p>
                A composite score computed entirely from search output — no reference
                run, no blanks, works from the very first QC injection.<br><br>
                <strong>DIA:</strong>
                <strong style="color: var(--ucd-gold)">30%</strong> precursor depth &middot;
                <strong style="color: var(--ucd-gold)">25%</strong> spectral quality (frags/precursor) &middot;
                <strong style="color: var(--ucd-gold)">20%</strong> sampling (pts/peak) &middot;
                <strong style="color: var(--ucd-gold)">15%</strong> quant coverage &middot;
                <strong style="color: var(--ucd-gold)">10%</strong> digestion<br>
                <strong>DDA:</strong>
                <strong style="color: var(--ucd-gold)">30%</strong> PSM depth &middot;
                <strong style="color: var(--ucd-gold)">25%</strong> mass accuracy &middot;
                <strong style="color: var(--ucd-gold)">20%</strong> sampling (pts/peak) &middot;
                <strong style="color: var(--ucd-gold)">15%</strong> hyperscore &middot;
                <strong style="color: var(--ucd-gold)">10%</strong> digestion<br><br>
                <span class="badge badge-ips-high">90-100 Excellent</span>
                <span class="badge" style="background:rgba(6,78,59,0.4);color:#6ee7b7;border:1px solid rgba(110,231,183,0.3)">80-89 Good</span>
                <span class="badge badge-ips-mid">60-79 Marginal</span>
                <span class="badge badge-ips-low">&lt;60 Investigate</span>
            </p>
        </div>
        <div class="info-card">
            <h3>Why This Benchmark Works</h3>
            <p>
                Every community submission searches the exact same frozen, hash-verified
                human UniProt FASTA and predicted spectral library. Identical upstream
                parameters mean differences in output reflect instrument performance,
                not search configuration.<br><br>
                <strong style="color: var(--ucd-gold)">Primary:</strong> Precursors (DIA) / PSMs (DDA) &mdash; purest instrument signal<br>
                <strong style="color: var(--ucd-gold)">Secondary:</strong> Peptides &mdash; slight sensitivity to search settings<br>
                <strong style="color: var(--ucd-gold)">Context:</strong> Proteins &mdash; shown for reference, not used for ranking (still affected by inference algorithm)<br>
                <strong style="color: var(--ucd-gold)">Health:</strong> IPS, missed cleavages, charge distribution
            </p>
        </div>
        <div class="info-card">
            <h3>Points Across Peak</h3>
            <p>
                The number of MS2 scans sampling each precursor's elution profile directly
                determines quantitation accuracy
                (<a href="https://doi.org/10.1021/ac50012a005" style="color:var(--ucd-gold)">Matthews &amp; Hayes, 1976</a>).<br><br>
                <span style="color:var(--green)">12+ points:</span> reliable quantitation<br>
                <span style="color:var(--yellow)">6-12 points:</span> minimum for acceptable accuracy (depends on peak shape)<br>
                <span style="color:var(--red)">&lt;6 points:</span> systematic quantitation error increases rapidly<br><br>
                At high SPD with short columns, cycle time can exceed peak width.
                These are guidelines — actual error depends on peak symmetry and
                integration method. Track this metric to find the throughput limit
                for your setup.
            </p>
        </div>
    </div>
</div>

<!-- Community Submissions -->
<div class="section">
    <h2>Community Submissions</h2>
    <p class="description">
        All submissions are anonymous by default. Click column headers to sort.
        Use the export button to download as CSV.
    </p>
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:0.5rem;margin-bottom:1rem">
        <div class="tabs" style="margin-bottom:0">
            <button class="tab active" onclick="showTab('dia')">DIA</button>
            <button class="tab" onclick="showTab('dda')">DDA</button>
            <button class="tab" onclick="showTab('all')">All</button>
        </div>
        <div style="display:flex;gap:0.5rem;align-items:center">
            <input id="table-search" type="text" placeholder="Filter by instrument, column..."
                   oninput="tablePage=0;renderTable()"
                   style="background:var(--card-bg);border:1px solid var(--card-border);border-radius:6px;
                          color:var(--text-primary);padding:0.4rem 0.75rem;font-size:0.85rem;width:200px;outline:none">
            <button onclick="exportCSV()" style="background:var(--ucd-gold);color:var(--ucd-blue-dark);
                    border:none;border-radius:6px;padding:0.4rem 0.75rem;font-size:0.85rem;font-weight:600;cursor:pointer">
                Export CSV
            </button>
        </div>
    </div>
    <div id="table-container">
        <div class="empty-state" id="loading-state">
            <div id="loading-msg" style="margin-bottom:0.75rem">Loading submissions...</div>
            <div style="max-width:360px;margin:0 auto;background:rgba(255,191,0,0.08);border-radius:4px;overflow:hidden;height:6px">
                <div id="loading-bar" style="width:5%;height:100%;background:linear-gradient(90deg,#FFBF00,#ff8c00);transition:width 0.3s ease"></div>
            </div>
        </div>
    </div>
</div>

<div class="footer">
    <p>STAN Community Benchmark &mdash; <a href="https://github.com/bsphinney/stan">open source</a>, built at the <a href="https://proteomics.ucdavis.edu">UC Davis Proteomics Core</a></p>
    <p style="margin-top: 0.25rem;">Data: <a href="https://creativecommons.org/licenses/by/4.0/">CC BY 4.0</a> &middot; Code: <a href="https://opensource.org/licenses/MIT">MIT</a> &middot; Raw files are never uploaded &middot; Anonymous by default &middot; Emails are NEVER stored (only one-way hashes for verification)</p>
</div>

<script>
const PL = {
    paper_bgcolor: 'rgba(0,0,0,0)',
    plot_bgcolor: 'rgba(2,40,81,0.3)',
    font: { color: '#a0b4cc', family: '-apple-system, sans-serif', size: 12 },
    margin: { t: 30, r: 30, b: 50, l: 60 },
    xaxis: { gridcolor: 'rgba(255,191,0,0.08)', zerolinecolor: 'rgba(255,191,0,0.15)' },
    yaxis: { gridcolor: 'rgba(255,191,0,0.08)', zerolinecolor: 'rgba(255,191,0,0.15)' },
};
const PC = { responsive: true, displayModeBar: false };
// FC maps both broad family names ("timsTOF") and full model names
// ("timsTOF HT") to a unique hue. Without the broad-family entries
// every dashboard render that calls fc(s.instrument_family) on a
// non-Lumos row fell through to the gray default, making timsTOF
// and Exploris indistinguishable on the Matthews & Hayes plot etc.
const FC = {
    'timsTOF':       '#5cb8ff',  'timsTOF HT': '#5cb8ff',
    'timsTOF Pro':   '#7dd3fc',  'timsTOF Pro 2': '#38bdf8',
    'Astral':        '#FFBF00',  'Orbitrap Astral': '#FFBF00',
    'Exploris':      '#c084fc',  'Exploris 480': '#c084fc',
    'Orbitrap Exploris 480': '#c084fc',
    'Lumos':         '#f87171',  'Orbitrap Fusion Lumos': '#f87171',
};
function fc(f) { return FC[f] || '#6b82a0'; }

let allDataRaw = [];  // unfiltered — includes flagged/failed runs
let allData = [];     // filtered — what charts and stats use
let currentTab = 'dia';
let hideFailed = true; // default: exclude flagged runs from charts + averages
let currentSampleType = 'hela'; // default: show HeLa only

function applyFilters() {
    let data = allDataRaw;
    if (hideFailed) {
        data = data.filter(s => !s.is_flagged);
    }
    if (currentSampleType !== 'all') {
        data = data.filter(s => (s.sample_type || 'hela') === currentSampleType);
    }
    allData = data;
}

// Backward-compat alias used by toggleFailedFilter
function applyFailedFilter() { applyFilters(); }

function changeSampleType(sel) {
    currentSampleType = sel.value;
    applyFilters();
    updateStats();
    try { renderCharts(); } catch(e) { console.error(e); }
    try { renderTable(); }  catch(e) { console.error(e); }
    try { renderRefRanges(); } catch(e) { console.error(e); }
}

// Tap-to-fullscreen: add an expand (\u26F6) button to every chart card. Figures
// are small on phones; tapping blows the chart up to the full viewport (rotate to
// landscape for the most detail), tapping again restores it.
function _stanInjectExpand() {
    if (!window._stanFsSync) {
        window._stanFsSync = true;
        const _sync = () => {
            if (!(document.fullscreenElement || document.webkitFullscreenElement)) {
                document.querySelectorAll('.chart-card.fs').forEach(c => {
                    c.classList.remove('fs');
                    const pd = c.querySelector('[id^="chart-"]');
                    setTimeout(() => { try { if (window.Plotly && pd) Plotly.Plots.resize(pd); } catch(e){} }, 80);
                });
                document.body.classList.remove('fs-open');
            }
        };
        document.addEventListener('fullscreenchange', _sync);
        document.addEventListener('webkitfullscreenchange', _sync);
    }
    document.querySelectorAll('.chart-card').forEach(card => {
        if (card.querySelector('.fs-btn')) return;
        const plot = card.querySelector('[id^="chart-"]');
        if (!plot) return;
        const btn = document.createElement('button');
        btn.className = 'fs-btn';
        btn.textContent = '\u26F6';
        btn.setAttribute('aria-label', 'Toggle full screen');
        btn.onclick = () => {
            const goingFs = !card.classList.contains('fs');
            if (goingFs) {
                card.classList.add('fs'); document.body.classList.add('fs-open');
                // True fullscreen (hides browser chrome) where the platform allows it
                // — Android Chrome, desktop, iPad. iPhone Safari blocks element
                // fullscreen, so it gracefully stays the CSS overlay. (For a fully
                // chrome-free iPhone view, Add STAN to the Home Screen — PWA standalone.)
                const req = card.requestFullscreen || card.webkitRequestFullscreen;
                if (req) { try { req.call(card); } catch(e){} }
            } else {
                const exit = document.exitFullscreen || document.webkitExitFullscreen;
                if (document.fullscreenElement || document.webkitFullscreenElement) { try { exit.call(document); } catch(e){} }
                card.classList.remove('fs'); document.body.classList.remove('fs-open');
            }
            setTimeout(() => { try { if (window.Plotly) Plotly.Plots.resize(plot); } catch(e){} }, 80);
        };
        card.appendChild(btn);
    });
}
window.addEventListener('load', () => { try { _stanInjectExpand(); } catch(e){} });

async function loadData() {
    // Fetch is isolated from render so a downstream JS exception in one
    // chart doesn't make the whole page look "failed to load". Previously
    // a thrown chart-render error ended up in this try/catch and wrote the
    // error message to the table container, even though allData was fine —
    // which is why switching tabs "fixed" it (tab-switch re-ran renderTable
    // against already-loaded data).
    //
    // Fake-progress the loading bar while we wait on the server. The server
    // now caches submissions in-memory (5 min TTL + snapshot_download on
    // cache miss), so warm requests return in ~100ms and cold requests take
    // a few seconds. The bar just reassures users something is happening.
    const bar = document.getElementById('loading-bar');
    const msg = document.getElementById('loading-msg');
    let tick = 0;
    const barTimer = setInterval(() => {
        tick++;
        if (bar) {
            // Asymptote toward 90% while waiting; snap to 100% on success.
            const pct = Math.min(90, 5 + tick * 4);
            bar.style.width = pct + '%';
        }
        if (msg && tick === 5) msg.textContent = 'Loading submissions... (first load may take a few seconds)';
    }, 200);
    let d;
    try {
        const r = await fetch('/api/leaderboard');
        if (!r.ok) throw new Error(`HTTP ${r.status} ${r.statusText}`);
        d = await r.json();
    } catch (e) {
        clearInterval(barTimer);
        console.error('[loadData] fetch failed:', e);
        document.getElementById('table-container').innerHTML =
            `<div class="empty-state">Failed to load data: ${e.message}. Try refreshing.</div>`;
        return;
    }
    clearInterval(barTimer);
    if (bar) bar.style.width = '100%';
    if (msg) msg.textContent = `Loaded ${(d.submissions || []).length} submissions`;

    allDataRaw = d.submissions || [];
    applyFailedFilter();

    // Render table FIRST so the primary content is always visible even if
    // a chart renderer throws later.
    try { updateStats(); }    catch (e) { console.error('[updateStats]', e); }
    try { renderTable(); }    catch (e) { console.error('[renderTable]', e); }
    try { renderRefRanges(); } catch (e) { console.error('[renderRefRanges]', e); }
    try { renderCharts(); }   catch (e) { console.error('[renderCharts]', e); }

    // Lazy-load heavy TIC traces AFTER the initial render, then merge into
    // allDataRaw by submission_id and re-render the community TIC overlay.
    fetch('/api/tic-overlay').then(r => r.ok ? r.json() : null).then(t => {
        if (!t || !t.traces) return;
        const byId = {};
        t.traces.forEach(x => { byId[x.submission_id] = x; });
        allDataRaw.forEach(s => {
            const x = byId[s.submission_id];
            if (x) { s.tic_rt_bins = x.tic_rt_bins; s.tic_intensity = x.tic_intensity; }
        });
        applyFilters();
        try { renderCommunityTIC(); } catch(e) { console.error('[tic-overlay merge]', e); }
    }).catch(e => console.error('[tic-overlay fetch]', e));
}

function updateStats() {
    document.getElementById('stat-submissions').textContent = allData.length;
    document.getElementById('stat-labs').textContent = new Set(allData.map(s=>s.display_name)).size;
    document.getElementById('stat-instruments').textContent = new Set(allData.map(s=>s.instrument_model)).size;
    const nFailed = allDataRaw.filter(s => s.is_flagged).length;
    const failedEl = document.getElementById('stat-failed');
    if (failedEl) failedEl.textContent = `${nFailed} flagged`;
    const cb = document.getElementById('hide-failed-cb');
    if (cb) cb.checked = hideFailed;

    // Update sample-type count badges
    const stBadge = document.getElementById('sample-type-counts');
    if (stBadge) {
        const counts = {};
        const base = hideFailed ? allDataRaw.filter(s => !s.is_flagged) : allDataRaw;
        base.forEach(s => {
            const st = s.sample_type || 'hela';
            counts[st] = (counts[st] || 0) + 1;
        });
        const parts = Object.entries(counts).sort((a,b) => b[1] - a[1])
            .map(([k,v]) => `${k.toUpperCase()}: ${v}`);
        stBadge.textContent = parts.join(' | ');
    }
}

function toggleFailedFilter() {
    hideFailed = !hideFailed;
    applyFailedFilter();
    updateStats();
    try { renderCharts(); } catch(e) { console.error(e); }
    try { renderTable(); }  catch(e) { console.error(e); }
    try { renderRefRanges(); } catch(e) { console.error(e); }
}

// ── Reference ranges ────────────────────────────────────────────

// Global filter state
let refFilters = { families: new Set(), modes: new Set() };

function buildRefFilters() {
    const families = [...new Set(allData.map(s=>s.instrument_family))].sort();
    const modes = [...new Set(allData.map(s=>s.acquisition_mode.toLowerCase().includes('dia')?'DIA':'DDA'))].sort();

    // Initialize: all selected
    if (refFilters.families.size === 0) families.forEach(f => refFilters.families.add(f));
    if (refFilters.modes.size === 0) modes.forEach(m => refFilters.modes.add(m));

    function checkbox(group, value, label) {
        const checked = refFilters[group].has(value) ? 'checked' : '';
        return `<label style="cursor:pointer;color:var(--text-secondary);font-size:0.85rem;display:flex;align-items:center;gap:0.3rem">
            <input type="checkbox" ${checked} onchange="toggleRefFilter('${group}','${value}')"> ${label}
        </label>`;
    }

    let h = '<div style="display:flex;flex-direction:column;gap:0.3rem"><span style="font-size:0.75rem;color:var(--text-muted);text-transform:uppercase">Instrument</span>';
    families.forEach(f => { h += checkbox('families', f, f); });
    h += '</div>';

    h += '<div style="display:flex;flex-direction:column;gap:0.3rem"><span style="font-size:0.75rem;color:var(--text-muted);text-transform:uppercase">Mode</span>';
    modes.forEach(m => { h += checkbox('modes', m, m); });
    h += '</div>';

    document.getElementById('ref-filters').innerHTML = h;
}

function toggleRefFilter(group, value) {
    if (refFilters[group].has(value)) refFilters[group].delete(value);
    else refFilters[group].add(value);
    renderRefRanges();
}

function renderRefRanges() {
    buildRefFilters();

    // Filter data by selected filters
    const filtered = allData.filter(s => {
        const mode = s.acquisition_mode.toLowerCase().includes('dia') ? 'DIA' : 'DDA';
        return refFilters.families.has(s.instrument_family) && refFilters.modes.has(mode);
    });

    // Hierarchical cohorts: column-specific when available, broad fallback
    const MIN_FOR_COLUMN = 3;

    function broadId(s) {
        // Reference cards must split timsTOF HT / Pro 2 / Pro / SCP / Ultra
        // — they're meaningfully different instruments. cohort_id was
        // built from instrument_family ("timsTOF") so we substitute the
        // model here. Falls back to family when instrument_model is
        // empty (legacy submissions).
        const parts = (s.cohort_id || '').split('_');
        const tail = parts.slice(1, 3).join('_');  // spd_amount
        const model = (s.instrument_model || s.instrument_family || 'Unknown').trim();
        return `${model}_${tail}`;
    }
    function colKey(s) {
        return (s.column_model || '').trim().toLowerCase() || '';
    }

    // Translate bucket names to readable labels
    const AMOUNT_LABELS = {
        'ultra-low': '≤25 ng', 'low': '26-75 ng', 'mid': '76-150 ng',
        'standard': '151-300 ng', 'high': '301-600 ng', 'very-high': '>600 ng',
    };
    const SPD_LABELS = {
        '200+spd': '200+ SPD', '100spd': '100 SPD', '60spd': '60 SPD',
        '30spd': '30 SPD', '15spd': '15 SPD', 'deep': 'Deep (>2h)',
    };
    function readableCohort(bid) {
        const parts = bid.split('_');
        const fam = parts[0] || '';
        const spd = SPD_LABELS[parts[1]] || parts[1] || '';
        const amt = AMOUNT_LABELS[parts[2]] || parts[2] || '';
        return `${fam} · ${spd} · ${amt}`;
    }

    // Build broad cohorts from filtered data
    const broadCohorts = {};
    filtered.forEach(s => {
        const bid = broadId(s);
        if (!broadCohorts[bid]) broadCohorts[bid] = [];
        broadCohorts[bid].push(s);
    });

    function iqr(arr) {
        const s = arr.slice().sort((a,b)=>a-b);
        const q1 = s[Math.floor(s.length*0.25)] || s[0];
        const q3 = s[Math.floor(s.length*0.75)] || s[s.length-1];
        return `${q1.toLocaleString()} - ${q3.toLocaleString()}`;
    }
    function med(arr) {
        const s = arr.slice().sort((a,b)=>a-b);
        return s[Math.floor(s.length/2)] || 0;
    }
    function refCardHtml(title, subtitle, subs, highlight) {
        const isDIA = subs[0].acquisition_mode.toLowerCase().includes('dia');
        const primary = isDIA ? subs.map(s=>s.n_precursors) : subs.map(s=>s.n_psms);
        const peps = subs.map(s=>s.n_peptides);
        const prots = subs.map(s=>s.n_proteins);
        const ips = subs.map(s=>s.ips_score);
        const pts = subs.map(s=>s.median_points_across_peak||0).filter(v=>v>0);

        // Build readable description from actual submission data
        const amounts = [...new Set(subs.map(s=>s.amount_ng||50))];
        const amtStr = amounts.length === 1 ? `${amounts[0]} ng` : `${Math.min(...amounts)}-${Math.max(...amounts)} ng`;
        const spds = [...new Set(subs.map(s=>s.spd||0).filter(v=>v>0))];
        const spdStr = spds.length === 1 ? `${spds[0]} SPD` : spds.length > 0 ? `${Math.min(...spds)}-${Math.max(...spds)} SPD` : '';
        const mode = isDIA ? 'DIA' : 'DDA';

        let h = `<div class="ref-card" style="${highlight?'border-color:var(--ucd-gold-border);':''}">`;
        h += `<h4>${title} <span style="color:var(--text-muted);font-weight:400">(n=${subs.length})</span></h4>`;
        h += `<div style="font-size:0.8rem;color:var(--text-secondary);margin-bottom:0.5rem">${mode} &middot; ${spdStr} &middot; ${amtStr}</div>`;
        if (subtitle) h += `<div style="font-size:0.8rem;color:var(--text-muted);margin-bottom:0.5rem">${subtitle}</div>`;
        h += `<div class="ref-row"><span class="ref-metric">${isDIA?'Precursors':'PSMs'} (IQR)</span><span class="ref-range">${iqr(primary)}</span></div>`;
        h += `<div class="ref-row"><span class="ref-metric">Peptides (IQR)</span><span class="ref-range">${iqr(peps)}</span></div>`;
        h += `<div class="ref-row"><span class="ref-metric">Proteins (IQR)</span><span class="ref-range">${iqr(prots)}</span></div>`;
        h += `<div class="ref-row"><span class="ref-metric">IPS median</span><span class="ref-range">${med(ips)}</span></div>`;
        if (pts.length > 0) {
            h += `<div class="ref-row"><span class="ref-metric">Points/peak median</span><span class="ref-range">${med(pts).toFixed(1)}</span></div>`;
        }
        h += `</div>`;
        return h;
    }

    let html = '';

    // Sort broad cohorts by submission count descending
    const sortedBroad = Object.entries(broadCohorts).sort((a,b) => b[1].length - a[1].length);

    for (const [bid, broadSubs] of sortedBroad) {
        // Group by column within this broad cohort
        const byColumn = {};
        broadSubs.forEach(s => {
            const ck = colKey(s);
            if (!byColumn[ck]) byColumn[ck] = [];
            byColumn[ck].push(s);
        });

        const columnGroups = Object.entries(byColumn)
            .filter(([ck, subs]) => ck && subs.length >= MIN_FOR_COLUMN)
            .sort((a,b) => b[1].length - a[1].length);

        if (columnGroups.length > 0) {
            // Show column-specific cards
            for (const [ck, colSubs] of columnGroups) {
                const colDisplay = colSubs[0].column_vendor
                    ? `${colSubs[0].column_vendor} ${colSubs[0].column_model}`
                    : colSubs[0].column_model;
                html += refCardHtml(
                    readableCohort(bid),
                    colDisplay,
                    colSubs,
                    true,
                );
            }
            // Also show broad cohort as fallback context
            if (broadSubs.length > columnGroups.reduce((a,[,s])=>a+s.length, 0)) {
                html += refCardHtml(
                    readableCohort(bid),
                    'All columns combined',
                    broadSubs,
                    false,
                );
            }
        } else {
            // No column-specific groups — show broad cohort
            const hasColumns = broadSubs.some(s => s.column_model);
            const subtitle = hasColumns
                ? 'Not enough data per column yet — contribute to build column-specific ranges!'
                : '';
            html += refCardHtml(readableCohort(bid), subtitle, broadSubs, false);
        }
    }

    document.getElementById('ref-ranges-container').innerHTML = html || '<div class="empty-state">No data yet</div>';
}

// ── Charts ──────────────────────────────────────────────────────

function renderCharts() {
    // Each chart wrapped so one broken renderer (typically a Plotly version
    // mismatch or an edge case on empty data) doesn't take down the rest
    // of the dashboard.
    const charts = [
        ['config-leaderboard', renderConfigLeaderboard],
        ['amount-depth',   renderAmountDepth],
        ['violin',         renderViolin],
        ['spd-depth',      renderSpdDepth],
        ['grs',            renderGrs],
        ['mass-acc',       renderMassAccuracy],
        ['ms1-signal',     renderMs1Signal],
        ['dyn-range',      renderDynamicRange],
        ['pts-peak',       renderPtsPerPeak],
        ['column-compare', renderColumnComparison],
        ['points-peak',    renderPointsAcrossPeak],
        ['radar',          renderRadar],
        ['community-tic',  renderCommunityTIC],
        ['lab-trend',       renderLabVsCommunity],
    ];
    for (const [name, fn] of charts) {
        try { fn(); }
        catch (e) { console.error(`[chart:${name}]`, e); }
    }
}

// ── Literature-survey LC-health charts ──────────────────────────────
// Each chart renders a single ID-free metric across the cohort, grouped
// by instrument family. Empty-state message if the field is unpopulated.

function _lcScatterByFamily(divId, field, yTitle, transform, layoutOverrides) {
    // Generic helper: scatter of submitted_at vs `field`, colored by family.
    const el = document.getElementById(divId);
    if (!el) return;
    // Drop only null/undefined — 0 is a legitimate measurement for
    // some metrics (median_mass_acc_ms1_ppm = 0.0 ppm = perfectly
    // calibrated). For metrics where 0 means "failed run" the
    // hard-gate validator already prevents those rows from
    // landing.
    const data = allData.filter(s => s[field] != null);
    if (data.length < 3) {
        el.innerHTML = `<div class="empty-state" style="padding:2rem;text-align:center">
            Not yet populated. This metric will appear once the STAN watcher has
            processed runs that include this field in DIA-NN's output.</div>`;
        return;
    }
    const families = [...new Set(data.map(s => s.instrument_family))].sort();
    const traces = families.map(fam => {
        const sub = data.filter(s => s.instrument_family === fam);
        return {
            type: 'scatter', mode: 'markers',
            name: fam,
            x: sub.map(s => runDate(s).toISOString().slice(0,10)),
            y: sub.map(s => transform ? transform(s[field]) : s[field]),
            marker: { color: fc(fam), size: 7, opacity: 0.8, line: {color:'#fff',width:0.3} },
            text: sub.map(s => `${s.instrument_model}<br>${s.run_name || ''}<br>${s.spd||'?'} SPD`),
            hovertemplate: `%{text}<br>${yTitle}: %{y:.2f}<extra></extra>`,
        };
    });
    const layout = {
        ...PL,
        xaxis: { ...PL.xaxis, title: 'Acquisition date', type: 'date', showticklabels: true,
                 tickformat: '%b %Y', tickfont: { size: 10, color: '#8aa4c0' } },
        yaxis: { ...PL.yaxis, title: yTitle },
        height: 320,
        showlegend: true,
        legend: { font: { color: '#a0b4cc' }, orientation: 'h', y: -0.15 },
    };
    if (layoutOverrides) {
        if (layoutOverrides.yaxis) layout.yaxis = { ...layout.yaxis, ...layoutOverrides.yaxis };
        if (layoutOverrides.xaxis) layout.xaxis = { ...layout.xaxis, ...layoutOverrides.xaxis };
    }
    Plotly.newPlot(divId, traces, layout, PC);
}

function renderMassAccuracy() {
    // Mass accuracy values in the Excel backfill are stored as corrected
    // ppm. Absolute value for the plot (direction doesn't matter).
    _lcScatterByFamily('chart-mass-acc', 'median_mass_acc_ms1_ppm', 'MS1 mass error (ppm)',
        v => Math.abs(v), { yaxis: { dtick: 5, range: [0, 25] } });
}

function renderMs1Signal() {
    // MS1.Signal is raw ion current in arbitrary units. Log10 for readability.
    _lcScatterByFamily('chart-ms1-signal', 'ms1_signal', 'log10(MS1 TIC signal)',
        v => (v > 0 ? Math.log10(v) : null));
}

function renderPtsPerPeak() {
    _lcScatterByFamily('chart-pts-peak', 'median_points_across_peak', 'Datapoints per peak');
}

function renderDynamicRange() {
    _lcScatterByFamily('chart-dyn-range', 'dynamic_range_log10', 'log10 dynamic range',
        null, { yaxis: { rangemode: 'tozero' } });
}

// ── Community TIC Overlay ─────────────────────────────────────────

// Infer LC system from column metadata or SPD fallback.
// Evosep's only standard SPDs are 100, 60, 30 — if column info is missing
// but SPD is exactly one of those, assume Evosep. Old submissions without
// an explicit `lc_system` field fall through to this inference.
function inferLcSystem(s) {
    const v = (s.column_vendor || '').toLowerCase();
    const m = (s.column_model || '').toLowerCase();
    if (v.includes('evo') || m.includes('evo') || m.match(/^ev\d/)) return 'evosep';
    if ([100, 60, 30].includes(s.spd)) return 'evosep';
    return 'custom';
}

function renderCommunityTIC() {
    const el = document.getElementById('chart-community-tic');
    const sel = document.getElementById('tic-spd-select');
    const lcSel = document.getElementById('tic-lc-select');
    const modeSel = document.getElementById('tic-mode-select');
    const countEl = document.getElementById('tic-count');
    if (!el || !sel) return;

    // Group submissions by SPD that have TIC data
    const withTIC = allData.filter(s => s.tic_rt_bins && s.tic_intensity);
    if (withTIC.length === 0) {
        el.innerHTML = '<p style="color:var(--muted)">No TIC data available yet. Run STAN v0.2.40+ to submit TIC traces.</p>';
        return;
    }

    // Apply LC system filter before computing SPD options so the dropdown
    // only lists SPDs that actually have matching traces for that LC.
    const lcFilter = (lcSel && lcSel.value) || 'all';
    const lcOf = (s) => s.lc_system || inferLcSystem(s);
    let lcFiltered = lcFilter === 'all'
        ? withTIC
        : withTIC.filter(s => lcOf(s) === lcFilter);

    // Apply DIA/DDA acquisition-mode filter. DIA and DDA have very
    // different scan rates and cycle times, so mixing them in a single
    // community median produces meaningless shapes. Default to DIA.
    const modeFilter = (modeSel && modeSel.value) || 'dia';
    const modeOf = (s) => (s.acquisition_mode || '').toLowerCase();
    if (modeFilter !== 'all') {
        lcFiltered = lcFiltered.filter(s => modeOf(s).includes(modeFilter));
    }

    // Get unique SPDs (from the LC-filtered set)
    const spds = [...new Set(lcFiltered.map(s => s.spd || 0))].filter(s => s > 0).sort((a,b) => a-b);
    if (spds.length === 0) {
        el.innerHTML = '<p style="color:var(--muted)">No TIC traces for this LC system.</p>';
        if (countEl) countEl.textContent = '';
        return;
    }

    // Repopulate SPD dropdown whenever the set of available SPDs changes
    // (e.g. after switching LC filter). Preserve selection if still valid.
    const prevSPD = sel.value;
    const newOpts = spds.map(spd => {
        const n = lcFiltered.filter(s => s.spd === spd).length;
        return spd + '|' + n;
    }).join(',');
    if (sel.dataset.opts !== newOpts) {
        sel.innerHTML = '';
        spds.forEach(spd => {
            const n = lcFiltered.filter(s => s.spd === spd).length;
            const opt = document.createElement('option');
            opt.value = spd;
            opt.textContent = spd + ' SPD (' + n + ' runs)';
            sel.appendChild(opt);
        });
        sel.dataset.opts = newOpts;
        if (prevSPD && spds.includes(parseInt(prevSPD))) sel.value = prevSPD;
        sel.onchange = () => renderCommunityTIC();
    }
    if (lcSel && !lcSel.onchange) {
        lcSel.onchange = () => renderCommunityTIC();
    }
    if (modeSel && !modeSel.onchange) {
        modeSel.onchange = () => renderCommunityTIC();
    }

    const selectedSPD = parseInt(sel.value) || spds[0];
    const traces = lcFiltered.filter(s => s.spd === selectedSPD);
    const lcLabel = lcFilter === 'evosep' ? ', Evosep'
                  : lcFilter === 'custom' ? ', Custom LC'
                  : '';
    const modeLabel = modeFilter === 'dia' ? ', DIA'
                    : modeFilter === 'dda' ? ', DDA'
                    : ', DIA+DDA';
    countEl.textContent = traces.length + ' traces (' + selectedSPD + ' SPD'
                        + lcLabel + modeLabel + ')';

    // Parse TIC JSON
    const parsed = traces.map(s => {
        try {
            const rt = typeof s.tic_rt_bins === 'string' ? JSON.parse(s.tic_rt_bins) : s.tic_rt_bins;
            const int_ = typeof s.tic_intensity === 'string' ? JSON.parse(s.tic_intensity) : s.tic_intensity;
            return { rt, intensity: int_, name: s.display_name || 'Anonymous' };
        } catch(e) { return null; }
    }).filter(Boolean);

    if (parsed.length === 0) {
        el.innerHTML = '<p style="color:var(--muted)">Could not parse TIC data.</p>';
        return;
    }

    // Median + shaded percentile bands instead of a 600+ trace hairball.
    // The median shape + IQR/10-90 spread carry the real signal and read
    // cleanly at any size. Raw traces are available via the "show all" toggle.
    const nBins = parsed[0].rt.length;
    const rtAxis = parsed[0].rt;
    const norms = parsed
        .filter(t => t.intensity.length === nBins)
        .map(t => { const mx = Math.max(...t.intensity); return mx > 0 ? t.intensity.map(v => v / mx) : t.intensity; });

    const pct = (sorted, q) => {
        if (!sorted.length) return 0;
        const idx = Math.min(sorted.length - 1, Math.max(0, Math.round((q / 100) * (sorted.length - 1))));
        return sorted[idx];
    };
    const p10 = [], p25 = [], p50 = [], p75 = [], p90 = [];
    for (let j = 0; j < nBins; j++) {
        const col = norms.map(n => n[j] || 0).sort((a, b) => a - b);
        p10.push(pct(col, 10)); p25.push(pct(col, 25)); p50.push(pct(col, 50));
        p75.push(pct(col, 75)); p90.push(pct(col, 90));
    }

    const band = (y, fill, fillcolor, name) => ({
        x: rtAxis, y, type: 'scatter', mode: 'lines',
        line: { width: 0, color: 'rgba(0,0,0,0)' },
        fill: fill || undefined, fillcolor,
        name: name || '', showlegend: !!name, hoverinfo: 'skip',
    });

    const plotTraces = [];
    const showAll = !!(document.getElementById('tic-show-all') || {}).checked;
    if (showAll) {
        const colors = ['rgba(100,180,255,0.12)', 'rgba(100,255,180,0.12)', 'rgba(255,180,100,0.12)', 'rgba(180,100,255,0.12)'];
        parsed.forEach((t, i) => {
            const mx = Math.max(...t.intensity);
            const norm = mx > 0 ? t.intensity.map(v => v / mx) : t.intensity;
            plotTraces.push({ x: t.rt, y: norm, type: 'scatter', mode: 'lines',
                line: { width: 0.7, color: colors[i % colors.length] },
                name: t.name, showlegend: false, hoverinfo: 'skip' });
        });
    }
    // Order matters for 'tonexty' fills: each lower edge is pushed immediately
    // before its upper edge so the fill anchors to the correct band floor.
    plotTraces.push(band(p10, null, 'rgba(255,107,53,0)'));
    plotTraces.push(band(p90, 'tonexty', 'rgba(255,107,53,0.12)', '10\u201390th pct'));
    plotTraces.push(band(p25, null, 'rgba(255,107,53,0)'));
    plotTraces.push(band(p75, 'tonexty', 'rgba(255,107,53,0.28)', '25\u201375th pct (IQR)'));
    plotTraces.push({
        x: rtAxis, y: p50, type: 'scatter', mode: 'lines',
        line: { width: 3, color: '#ff6b35', dash: 'dash' },
        name: 'Community median (' + norms.length + ' runs)', showlegend: true, hoverinfo: 'skip',
    });

    el.innerHTML = '';  // clear any stale 'No TIC data' message before plotting
    Plotly.newPlot(el, plotTraces, {
        xaxis: { title: 'Retention Time (min)', color: '#94a3b8', gridcolor: '#1e293b' },
        yaxis: { title: 'Normalized Signal', color: '#94a3b8', gridcolor: '#1e293b', range: [0, 1.05] },
        paper_bgcolor: 'transparent', plot_bgcolor: 'transparent',
        margin: { t: 10, b: 50, l: 60, r: 20 },
        height: 350,
        legend: { x: 0.7, y: 0.95, bgcolor: 'rgba(0,0,0,0.5)', font: { color: '#e2e8f0' } },
    }, {responsive: true});
}

// ── Your Lab vs. Community ─────────────────────────────────────────
// Levey-Jennings reimagined for community scale: one lab's time series
// overlaid on the community's reference bands (mean ± σ from ALL
// submissions of the same instrument family).

function renderLabVsCommunity() {
    const el = document.getElementById('chart-lab-trend');
    if (!el) return;

    // Amount filter
    const amtFilter = (document.getElementById('lab-amount') || {}).value || 'all';
    const amtFilterFn = (s) => {
        const a = s.amount_ng || 50;
        if (amtFilter === '50')  return a >= 20 && a <= 100;
        if (amtFilter === 'low') return a < 20;
        if (amtFilter === 'high') return a > 100;
        return true;  // 'all'
    };

    // Populate lab dropdown on first call
    const labSel = document.getElementById('lab-select');
    const instSel = document.getElementById('lab-instrument');
    const metricKey = (document.getElementById('lab-metric') || {}).value || 'n_precursors';

    if (labSel && labSel.options.length === 0) {
        const labs = [...new Set(allData.map(s => s.display_name).filter(Boolean))].sort();
        labSel.innerHTML = labs.map(l => `<option value="${l}">${l}</option>`).join('');
        // Default to UCD if present
        const ucd = labs.find(l => l.includes('UC Davis'));
        if (ucd) labSel.value = ucd;
    }

    const selectedLab = labSel ? labSel.value : '';
    if (!selectedLab) {
        el.innerHTML = '<div class="empty-state" style="padding:2rem">Select a lab to see their trend.</div>';
        return;
    }

    // Filter to this lab's runs + amount filter
    const labRuns = allData.filter(s => s.display_name === selectedLab && amtFilterFn(s));

    // Populate instrument dropdown from this lab's instruments
    if (instSel) {
        const models = [...new Set(labRuns.map(s => s.instrument_model).filter(Boolean))].sort();
        const prev = instSel.value;
        instSel.innerHTML = models.map(m => `<option value="${m}">${m}</option>`).join('');
        if (models.includes(prev)) instSel.value = prev;
    }

    const selectedInst = instSel ? instSel.value : '';
    const labFiltered = selectedInst
        ? labRuns.filter(s => s.instrument_model === selectedInst)
        : labRuns;

    // Get the instrument FAMILY for community reference
    const family = labFiltered.length > 0 ? labFiltered[0].instrument_family : null;

    // Community reference: all runs from ANY lab with the same instrument family + amount filter
    const communityRuns = family
        ? allData.filter(s => s.instrument_family === family && s[metricKey] != null && s[metricKey] !== 0 && amtFilterFn(s))
        : [];
    const communityVals = communityRuns.map(s => s[metricKey]).filter(v => v != null && v !== 0);

    if (communityVals.length < 3) {
        el.innerHTML = `<div class="empty-state" style="padding:2rem">Not enough community data for ${family || 'this instrument'} on this metric.</div>`;
        return;
    }

    const cMean = communityVals.reduce((a, b) => a + b, 0) / communityVals.length;
    const cSd = Math.sqrt(communityVals.reduce((a, v) => a + (v - cMean) ** 2, 0) / (communityVals.length - 1));

    // Lab's data sorted by date (parse from filename)
    const labWithMetric = labFiltered.filter(s => s[metricKey] != null && s[metricKey] !== 0);
    if (labWithMetric.length === 0) {
        el.innerHTML = `<div class="empty-state" style="padding:2rem">${selectedLab} has no data for this metric on ${selectedInst || 'this instrument'}.</div>`;
        return;
    }

    const withDate = labWithMetric.map(s => ({ s, d: runDate(s) })).sort((a, b) => a.d - b.d);
    const xs = withDate.map(r => r.d);
    const ys = withDate.map(r => r.s[metricKey]);
    const xRange = [xs[0], xs[xs.length - 1]];

    // Westgard coloring for each point
    const colors = ys.map(v => {
        const z = Math.abs(v - cMean) / (cSd || 1);
        if (z >= 3) return '#f87171';
        if (z >= 2) return '#fbbf24';
        return '#5cb8ff';
    });

    const hoverText = withDate.map(({ s, d }) =>
        `${s.run_name || ''}<br>${d.toISOString().slice(0,10)}<br>${metricKey}: ${typeof s[metricKey] === 'number' ? s[metricKey].toLocaleString() : s[metricKey]}<br>${s.spd||'?'} SPD`
    );

    // Community reference bands as filled areas
    const bandTrace = (lo, hi, color, name) => ({
        type: 'scatter', mode: 'lines',
        x: [...xRange, ...xRange.slice().reverse()],
        y: [hi, hi, lo, lo],
        fill: 'toself', fillcolor: color,
        line: { width: 0 }, hoverinfo: 'skip',
        showlegend: true, name,
    });

    const traces = [
        bandTrace(cMean - 3*cSd, cMean + 3*cSd, 'rgba(248,113,113,0.08)', '±3σ'),
        bandTrace(cMean - 2*cSd, cMean + 2*cSd, 'rgba(234,179,8,0.08)',   '±2σ'),
        bandTrace(cMean - 1*cSd, cMean + 1*cSd, 'rgba(34,197,94,0.10)',   '±1σ'),
        // Community mean line
        {
            type: 'scatter', mode: 'lines',
            x: xRange, y: [cMean, cMean],
            line: { color: 'rgba(160,180,204,0.6)', width: 1.5, dash: 'dash' },
            hoverinfo: 'skip', showlegend: true, name: `Community mean (${Math.round(cMean).toLocaleString()})`,
        },
        // Lab's data points + connecting line
        {
            type: 'scatter', mode: 'markers+lines',
            x: xs, y: ys,
            marker: { color: colors, size: 8, line: { color: '#fff', width: 0.5 } },
            line: { color: 'rgba(56,189,248,0.5)', width: 1.5 },
            text: hoverText,
            hovertemplate: '%{text}<extra></extra>',
            showlegend: true, name: selectedLab.length > 25 ? selectedLab.slice(0,25)+'…' : selectedLab,
        },
    ];

    Plotly.newPlot('chart-lab-trend', traces, {
        ...PL,
        xaxis: { ...PL.xaxis, title: 'Run date', type: 'date' },
        yaxis: { ...PL.yaxis, title: metricKey },
        height: 400,
        showlegend: true,
        legend: { font: { color: '#a0b4cc', size: 10 }, orientation: 'h', y: -0.18, yanchor: 'top' },
        annotations: [{
            x: 0.01, y: 0.98, xref: 'paper', yref: 'paper', showarrow: false, align: 'left',
            text: `Your lab: n=${labWithMetric.length} &nbsp;·&nbsp; Community ${family}: n=${communityVals.length}, mean=${Math.round(cMean).toLocaleString()}, σ=${Math.round(cSd).toLocaleString()}`,
            font: { color: 'var(--text-muted)', size: 10 },
        }],
    }, PC);
}

// ── Longitudinal trends (Levey-Jennings / Moving Range / Pareto) ────
// UCD run names follow "FLDDMMYY_..." or "ExDDMMYY_..." (European day-month-year,
// e.g. FL030123 = 3 January 2023). Parse the first 6 digits after the 2-char
// prefix and turn them into a JS Date. Returns null for filenames that don't
// match (newer runs that only have submitted_at).
function runDateFromName(name) {
    // Parse real acquisition dates from UCD filename conventions:
    //   Lumos/Exploris: FLDDMMYY_... or ExDDMMYY_...  (European day-month-year)
    //   timsTOF:        DDmmmYYYY_... (e.g. 03jun2024_HeLa50ng_DIA...)
    //   timsTOF alt:    DDMMYY_...    (e.g. 040823_HeLa50ng...)
    if (!name) return null;

    // Pattern 1: FL/Ex + 6 digits (DDMMYY)
    let m = name.match(/^[A-Za-z]{2}(\d{2})(\d{2})(\d{2})_/);
    if (m) {
        const dd = parseInt(m[1], 10), mm = parseInt(m[2], 10);
        let yy = parseInt(m[3], 10);
        if (dd <= 31 && mm <= 12) {
            yy = yy < 50 ? 2000 + yy : 1900 + yy;
            const d = new Date(yy, mm - 1, dd);
            if (!isNaN(d.getTime())) return d;
        }
    }

    // Pattern 2: DDmmmYYYY (e.g. 03jun2024, 22mai25, 11iun25)
    // Month names: English + Brett's French/Romanian abbreviations
    const MONTHS = {
        jan:1, feb:2, mar:3, apr:4, may:5, jun:6, jul:7, aug:8, sep:9, oct:10, nov:11, dec:12,
        mai:5, iun:6, iul:7, noi:11, ian:1,  // Romanian/French variants Brett uses
    };
    m = name.match(/^(\d{1,2})([a-z]{3})(\d{2,4})_/i);
    if (m) {
        const dd = parseInt(m[1], 10);
        const mon = MONTHS[m[2].toLowerCase()];
        let yr = parseInt(m[3], 10);
        if (mon && dd <= 31) {
            if (yr < 100) yr = yr < 50 ? 2000 + yr : 1900 + yr;
            const d = new Date(yr, mon - 1, dd);
            if (!isNaN(d.getTime())) return d;
        }
    }

    // Pattern 3: bare DDMMYY_ (e.g. 040823_HeLa...)
    m = name.match(/^(\d{2})(\d{2})(\d{2})_/);
    if (m) {
        const dd = parseInt(m[1], 10), mm = parseInt(m[2], 10);
        let yy = parseInt(m[3], 10);
        if (dd <= 31 && mm <= 12) {
            yy = yy < 50 ? 2000 + yy : 1900 + yy;
            const d = new Date(yy, mm - 1, dd);
            if (!isNaN(d.getTime())) return d;
        }
    }

    return null;
}

function runDate(s) {
    // Prefer explicit run_date from the client (acquisition date), then parse
    // from run_name, then fall back to the submission timestamp.
    if (s.run_date) {
        const d = new Date(s.run_date);
        if (!isNaN(d.getTime())) return d;
    }
    return runDateFromName(s.run_name) || new Date(s.submitted_at || Date.now());
}

function _stats(values) {
    if (!values.length) return { mean: 0, sd: 0 };
    const mean = values.reduce((a, b) => a + b, 0) / values.length;
    const sd = Math.sqrt(
        values.reduce((a, v) => a + (v - mean) ** 2, 0) / Math.max(1, values.length - 1)
    );
    return { mean, sd };
}

function populateInstrumentSelect() {
    const sel = document.getElementById('lj-instrument');
    if (!sel) return;
    const seen = sel.options.length > 0;
    const models = [...new Set(allData.map(s => s.instrument_model).filter(Boolean))].sort();
    if (!seen) {
        sel.innerHTML = models.map(m => `<option value="${m}">${m}</option>`).join('');
        // Default to the model with the most rows
        const counts = {};
        allData.forEach(s => { counts[s.instrument_model] = (counts[s.instrument_model] || 0) + 1; });
        const best = models.sort((a, b) => counts[b] - counts[a])[0];
        if (best) sel.value = best;
    }
}

function renderLongitudinal() {
    populateInstrumentSelect();
    const instrument = document.getElementById('lj-instrument')?.value;
    const metric = document.getElementById('lj-metric')?.value || 'n_precursors';
    if (!instrument) return;

    // Filter: same instrument, metric present, and for depth metrics also
    // match the current DIA/DDA tab so we don't mix precursors with PSMs.
    let runs = allData.filter(s => s.instrument_model === instrument && s[metric] != null && s[metric] !== 0);
    if (metric === 'n_precursors') runs = runs.filter(s => (s.acquisition_mode || '').toLowerCase().includes('dia'));
    if (metric === 'n_psms')       runs = runs.filter(s => (s.acquisition_mode || '').toLowerCase().includes('dda'));

    renderLeveyJennings(runs, metric, instrument);
    renderMovingRange(runs, metric, instrument);
    renderParetoVariability(instrument);
}

function renderLeveyJennings(runs, metric, instrument) {
    const el = document.getElementById('chart-lj');
    if (!el) return;
    if (runs.length < 3) {
        el.innerHTML = `<div class="empty-state" style="padding:2rem;text-align:center">Need ≥3 runs with this metric on ${instrument} to draw control limits. Have ${runs.length}.</div>`;
        return;
    }

    // Sort by real run date (parsed from filename), fall back to submitted_at
    const withDate = runs.map(s => ({ s, d: runDate(s) })).sort((a, b) => a.d - b.d);
    const xs = withDate.map(r => r.d);
    const ys = withDate.map(r => r.s[metric]);
    const { mean, sd } = _stats(ys);

    const xDomain = [xs[0], xs[xs.length - 1]];
    const band = (offset, color, dash) => ({
        type: 'scatter', mode: 'lines',
        x: xDomain, y: [mean + offset * sd, mean + offset * sd],
        line: { color, width: 1, dash }, hoverinfo: 'skip', showlegend: false,
    });

    // Flag Westgard-style outliers for the scatter color
    const colors = ys.map(v => {
        const z = Math.abs(v - mean) / (sd || 1);
        if (z >= 3) return '#f87171';  // red — 1-3s violation
        if (z >= 2) return '#fbbf24';  // amber
        return '#5cb8ff';              // in control
    });

    const hoverText = withDate.map(({ s, d }) => {
        const run = s.run_name || '';
        return `${run}<br>${d.toISOString().slice(0,10)}<br>${metric}: ${(s[metric]||0).toLocaleString()}<br>SPD: ${s.spd || '?'}`;
    });

    const traces = [
        band( 0, '#6b82a0', 'solid'),
        band(+1, 'rgba(90,130,160,0.4)', 'dot'),
        band(-1, 'rgba(90,130,160,0.4)', 'dot'),
        band(+2, 'rgba(251,191,36,0.6)', 'dash'),
        band(-2, 'rgba(251,191,36,0.6)', 'dash'),
        band(+3, 'rgba(248,113,113,0.8)', 'dash'),
        band(-3, 'rgba(248,113,113,0.8)', 'dash'),
        {
            type: 'scatter', mode: 'markers+lines',
            x: xs, y: ys,
            marker: { color: colors, size: 7, line: { color: '#fff', width: 0.3 } },
            line: { color: 'rgba(160,180,204,0.4)', width: 1 },
            text: hoverText,
            hovertemplate: '%{text}<extra></extra>',
            showlegend: false,
        },
    ];

    Plotly.newPlot('chart-lj', traces, {
        ...PL,
        xaxis: { ...PL.xaxis, title: 'Run date', type: 'date' },
        yaxis: { ...PL.yaxis, title: metric },
        height: 360,
        showlegend: false,
        annotations: [{
            x: 0.01, y: 0.98, xref: 'paper', yref: 'paper', showarrow: false, align: 'left',
            text: `n=${ys.length}   mean=${mean.toFixed(1)}   σ=${sd.toFixed(1)}   CV=${(100*sd/mean).toFixed(1)}%`,
            font: { color: 'var(--text-muted)', size: 11 },
        }],
    }, PC);
}

function renderMovingRange(runs, metric, instrument) {
    const el = document.getElementById('chart-mr');
    if (!el) return;
    if (runs.length < 3) {
        el.innerHTML = '<div class="empty-state" style="padding:2rem;text-align:center">Need ≥3 runs</div>';
        return;
    }
    const withDate = runs.map(s => ({ s, d: runDate(s) })).sort((a, b) => a.d - b.d);
    const xs = withDate.slice(1).map(r => r.d);
    const ys = [];
    for (let i = 1; i < withDate.length; i++) {
        ys.push(Math.abs(withDate[i].s[metric] - withDate[i - 1].s[metric]));
    }
    const meanMR = ys.reduce((a, b) => a + b, 0) / ys.length;
    const ucl = 3.267 * meanMR;  // Shewhart constant for individual moving range

    const colors = ys.map(v => (v > ucl ? '#f87171' : '#5cb8ff'));
    Plotly.newPlot('chart-mr', [
        {
            type: 'scatter', mode: 'lines',
            x: [xs[0], xs[xs.length - 1]], y: [meanMR, meanMR],
            line: { color: '#6b82a0', width: 1 }, showlegend: false, hoverinfo: 'skip',
        },
        {
            type: 'scatter', mode: 'lines',
            x: [xs[0], xs[xs.length - 1]], y: [ucl, ucl],
            line: { color: 'rgba(248,113,113,0.7)', width: 1, dash: 'dash' }, showlegend: false, hoverinfo: 'skip',
        },
        {
            type: 'scatter', mode: 'markers+lines',
            x: xs, y: ys,
            marker: { color: colors, size: 6 },
            line: { color: 'rgba(160,180,204,0.4)', width: 1 },
            hovertemplate: '%{x|%Y-%m-%d}<br>|Δ|: %{y:.1f}<extra></extra>',
            showlegend: false,
        },
    ], {
        ...PL,
        xaxis: { ...PL.xaxis, title: 'Run date', type: 'date' },
        yaxis: { ...PL.yaxis, title: `|Δ ${metric}|` },
        height: 260,
        annotations: [{
            x: 0.01, y: 0.98, xref: 'paper', yref: 'paper', showarrow: false, align: 'left',
            text: `mean MR=${meanMR.toFixed(1)}   UCL=${ucl.toFixed(1)}`,
            font: { color: 'var(--text-muted)', size: 11 },
        }],
    }, PC);
}

function renderParetoVariability(instrument) {
    const el = document.getElementById('chart-pareto-var');
    if (!el) return;

    // Candidate metrics with their human-readable labels
    const METRICS = [
        ['n_precursors',             'Precursors'],
        ['n_peptides',               'Peptides'],
        ['n_proteins',               'Proteins'],
        ['ips_score',                'IPS'],
        ['median_peak_width_sec',    'Peak width'],
        ['median_mass_acc_ms1_ppm',  'MS1 mass acc'],
        ['median_mass_acc_ms2_ppm',  'MS2 mass acc'],
        ['ms1_signal',               'MS1 signal'],
        ['ms2_signal',               'MS2 signal'],
    ];

    const runs = allData.filter(s => s.instrument_model === instrument);
    const cvs = [];
    for (const [key, label] of METRICS) {
        const vals = runs.map(s => s[key]).filter(v => v != null && v !== 0);
        if (vals.length < 3) continue;
        const { mean, sd } = _stats(vals);
        if (mean === 0) continue;
        const cv = 100 * sd / Math.abs(mean);
        cvs.push({ key, label, cv, n: vals.length });
    }

    if (!cvs.length) {
        el.innerHTML = `<div class="empty-state" style="padding:2rem;text-align:center">No variability data for ${instrument} yet.</div>`;
        return;
    }

    cvs.sort((a, b) => b.cv - a.cv);

    Plotly.newPlot('chart-pareto-var', [{
        type: 'bar',
        orientation: 'h',
        x: cvs.map(c => c.cv).reverse(),
        y: cvs.map(c => c.label).reverse(),
        text: cvs.map(c => `${c.cv.toFixed(1)}% (n=${c.n})`).reverse(),
        textposition: 'outside',
        marker: {
            color: cvs.map(c => c.cv).reverse(),
            colorscale: [[0, '#34d399'], [0.5, '#FFBF00'], [1, '#f87171']],
        },
        hovertemplate: '%{y}: %{x:.1f}%<extra></extra>',
    }], {
        ...PL,
        xaxis: { ...PL.xaxis, title: 'Coefficient of variation (%)' },
        yaxis: { ...PL.yaxis, automargin: true },
        height: Math.max(260, 30 * cvs.length + 80),
        margin: { l: 120, r: 80, t: 20, b: 40 },
    }, PC);
}

// Depth-by-SPD: which throughput produces the best data on each instrument?
// One subplot per instrument family. X = SPD bucket, Y = precursors/PSMs.
// Amount-filtered: dropdown selects which loading tier to show so you compare
// apples-to-apples across SPDs. Shape encodes amount bucket for the "All"
// view so off-standard amounts pop visually.
function renderSpdDepth() {
    const el = document.getElementById('chart-spd-depth');
    if (!el) return;

    // Amount filter from dropdown
    const amtFilter = (document.getElementById('spd-amount-filter') || {}).value || '50';
    const amountShape = s => {
        const a = s.amount_ng || 50;
        if (a < 20)  return 'diamond';
        if (a > 100) return 'square';
        return 'circle';
    };

    let data = allData;
    if (currentTab === 'dia') data = data.filter(s => s.acquisition_mode.toLowerCase().includes('dia'));
    else if (currentTab === 'dda') data = data.filter(s => s.acquisition_mode.toLowerCase().includes('dda'));
    data = data.filter(s => (s.n_precursors || s.n_psms) && s.spd);

    // Apply amount filter
    if (amtFilter === '50')   data = data.filter(s => (s.amount_ng || 50) >= 20 && (s.amount_ng || 50) <= 100);
    if (amtFilter === 'low')  data = data.filter(s => (s.amount_ng || 50) < 20);
    if (amtFilter === 'high') data = data.filter(s => (s.amount_ng || 50) > 100);
    // 'all' = no filter

    if (data.length === 0) {
        el.innerHTML = '<div class="empty-state" style="padding:2rem">No runs at this amount tier. Try "All amounts".</div>';
        return;
    }

    const BUCKET_ORDER = ['deep','medium','fast','ultra'];
    const BUCKET_LABEL = {
        deep:   'deep (≤15 SPD)',
        medium: 'medium (16–40)',
        fast:   'fast (41–80)',
        ultra:  'ultra (>80)',
    };
    const spdBucket = (spd) => {
        if (!spd || spd <= 0) return 'medium';
        if (spd <= 15) return 'deep';
        if (spd <= 40) return 'medium';
        if (spd <= 80) return 'fast';
        return 'ultra';
    };
    const depth = (s) => Math.max(s.n_precursors || 0, s.n_psms || 0);

    const families = [...new Set(data.map(s => s.instrument_family))].sort();
    const traces = [];
    const annotations = [];

    families.forEach((fam, fi) => {
        const famRuns = data.filter(s => s.instrument_family === fam);
        const axisSuffix = fi === 0 ? '' : (fi + 1);
        const xaxisRef = 'x' + axisSuffix;
        const yaxisRef = 'y' + axisSuffix;

        // One box per bucket that actually has runs
        const bucketsPresent = BUCKET_ORDER.filter(b => famRuns.some(s => spdBucket(s.spd) === b));
        bucketsPresent.forEach(b => {
            const runs = famRuns.filter(s => spdBucket(s.spd) === b);
            traces.push({
                type: 'box',
                y: runs.map(depth),
                x: runs.map(() => BUCKET_LABEL[b]),
                name: BUCKET_LABEL[b],
                boxpoints: false,
                line: { color: fc(fam) },
                fillcolor: fc(fam) + '20',
                showlegend: false,
                xaxis: xaxisRef, yaxis: yaxisRef,
                hoverinfo: 'skip',
            });
        });

        // Scatter overlay — color by SPD (consistent with violin), shape by amount
        traces.push({
            type: 'scatter', mode: 'markers',
            x: famRuns.map(s => BUCKET_LABEL[spdBucket(s.spd)]),
            y: famRuns.map(depth),
            xaxis: xaxisRef, yaxis: yaxisRef,
            showlegend: false,
            marker: {
                color: famRuns.map(s => s.spd || 30),
                colorscale: [[0,'#5cb8ff'],[0.3,'#34d399'],[0.6,'#FFBF00'],[1,'#f87171']],
                cmin: 5, cmax: 200,
                size: 6, opacity: 0.8,
                symbol: famRuns.map(amountShape),
                line: { color: '#fff', width: 0.3 },
                showscale: fi === families.length - 1,
                colorbar: fi === families.length - 1 ? {
                    title: 'SPD', tickfont:{color:'#a0b4cc'},
                    titlefont:{color:'#DAAA00'}, len: 0.5, thickness: 10, x: 1.02,
                } : undefined,
            },
            text: famRuns.map(s => `${s.instrument_model}<br>${s.spd} SPD, ${s.amount_ng||50} ng<br>${s.acquisition_mode.toUpperCase()}`),
            hovertemplate: '%{text}<br>IDs: %{y:,}<extra></extra>',
        });

        const amtLabel = amtFilter === 'all' ? '' : `, ${amtFilter === '50' ? '50 ng' : amtFilter === 'low' ? '<20 ng' : '>100 ng'}`;
        annotations.push({
            text: `<b>${fam}</b> (n=${famRuns.length}${amtLabel})`,
            xref: 'paper', yref: 'paper',
            x: (fi + 0.5) / families.length, y: 1.02,
            xanchor: 'center', yanchor: 'bottom',
            showarrow: false,
            font: { color: fc(fam), size: 13 },
        });
    });

    // Build subplot layout
    const layout = { ...PL, height: 420, showlegend: false, annotations };
    families.forEach((fam, fi) => {
        const xKey = 'xaxis' + (fi === 0 ? '' : (fi + 1));
        const yKey = 'yaxis' + (fi === 0 ? '' : (fi + 1));
        const left = fi / families.length + 0.02;
        const right = (fi + 1) / families.length - 0.02;
        layout[xKey] = {
            ...PL.xaxis,
            domain: [left, Math.min(right, 1)],
            categoryorder: 'array',
            categoryarray: BUCKET_ORDER.map(b => BUCKET_LABEL[b]),
            title: '',
        };
        layout[yKey] = {
            ...PL.yaxis,
            anchor: 'x' + (fi === 0 ? '' : (fi + 1)),
            title: fi === 0 ? 'Precursors / PSMs' : '',
            matches: fi === 0 ? undefined : 'y',
        };
    });

    Plotly.newPlot('chart-spd-depth', traces, layout, PC);
}

// Best Configurations leaderboard — single ranked table answering
// "what instrument × SPD × amount loaded gives the best data?". Cohort
// key is (instrument_model, SPD_tier, amount_bucket). Each row is one
// cohort with n>=3 submissions. Click headers to re-sort.
let configSortCol = 'precursors';
let configSortAsc = false;

function _configSpdTier(spd) {
    if (!spd || spd <= 0) return '?';
    if (spd >= 100)       return '100+';
    if (spd >= 60)        return '60-100';
    if (spd >= 30)        return '30-60';
    return '<30';
}
function _configAmountBucket(a) {
    a = (a == null) ? 50 : a;
    if (a < 10)   return '<10 ng';
    if (a < 50)   return '10-49 ng';
    if (a < 100)  return '50 ng';
    if (a < 250)  return '100-249 ng';
    return '≥250 ng';
}
function _median(arr) {
    if (!arr.length) return 0;
    const s = [...arr].sort((a,b) => a - b);
    const m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m-1] + s[m]) / 2;
}

function sortConfigLeaderboard(col) {
    if (configSortCol === col) configSortAsc = !configSortAsc;
    else { configSortCol = col; configSortAsc = false; }
    renderConfigLeaderboard();
}

function renderConfigLeaderboard() {
    const container = document.getElementById('config-leaderboard');
    if (!container) return;

    let data = allData;
    if (currentTab === 'dia') data = data.filter(s => (s.acquisition_mode||'').toLowerCase().includes('dia'));
    else if (currentTab === 'dda') data = data.filter(s => (s.acquisition_mode||'').toLowerCase().includes('dda'));

    const depthMet = currentTab === 'dda' ? 'n_psms' : 'n_precursors';
    const depthLabel = currentTab === 'dda' ? 'PSMs' : 'Precursors';

    const modelOf = s => s.instrument_model || s.instrument_family || 'Unknown';
    const cohorts = {};
    data.forEach(s => {
        const key = [modelOf(s), _configSpdTier(s.spd), _configAmountBucket(s.amount_ng)].join('|');
        if (!cohorts[key]) {
            cohorts[key] = {
                model: modelOf(s),
                spd: _configSpdTier(s.spd),
                amount: _configAmountBucket(s.amount_ng),
                vals: { precursors:[], peptides:[], proteins:[], psms:[], ms1ppm:[] },
            };
        }
        const c = cohorts[key];
        if (s.n_precursors > 0) c.vals.precursors.push(s.n_precursors);
        if (s.n_peptides   > 0) c.vals.peptides.push(s.n_peptides);
        if (s.n_proteins   > 0) c.vals.proteins.push(s.n_proteins);
        if (s.n_psms       > 0) c.vals.psms.push(s.n_psms);
        if (s.median_mass_acc_ms1_ppm > 0) c.vals.ms1ppm.push(s.median_mass_acc_ms1_ppm);
    });

    const MIN_N = 3;
    let rows = Object.values(cohorts)
        .filter(c => c.vals[currentTab === 'dda' ? 'psms' : 'precursors'].length >= MIN_N)
        .map(c => ({
            model: c.model, spd: c.spd, amount: c.amount,
            n: c.vals[currentTab === 'dda' ? 'psms' : 'precursors'].length,
            precursors: _median(c.vals.precursors),
            peptides:   _median(c.vals.peptides),
            proteins:   _median(c.vals.proteins),
            psms:       _median(c.vals.psms),
            ms1ppm:     _median(c.vals.ms1ppm),
        }));

    if (!rows.length) {
        container.innerHTML = '<div class="empty-state" style="padding:1.5rem; text-align:center; color:var(--text-muted)">No cohorts with ≥3 submissions for this filter yet.</div>';
        return;
    }

    // Sort. ms1ppm is "lower is better"; everything else "higher is better".
    const lowerBetter = configSortCol === 'ms1ppm';
    rows.sort((a, b) => {
        const av = a[configSortCol] ?? 0, bv = b[configSortCol] ?? 0;
        if (av === bv) return 0;
        const ascending = configSortAsc !== lowerBetter;  // toggling on header re-flips
        return ascending ? (av - bv) : (bv - av);
    });

    // Identify "best" cells for badge highlighting.
    const bestDepth   = rows[0]?.[depthMet === 'n_psms' ? 'psms' : 'precursors'] ?? 0;
    const bestMs1     = Math.min(...rows.map(r => r.ms1ppm).filter(v => v > 0));

    // Update the section badge with current filter context
    const badge = document.getElementById('config-leaderboard-badge');
    if (badge) {
        const sampleLabel = (typeof currentSampleType !== 'undefined' && currentSampleType !== 'all')
                            ? currentSampleType.toUpperCase() : 'all samples';
        const modeLabel = currentTab === 'dia' ? 'DIA' : currentTab === 'dda' ? 'DDA' : 'all modes';
        badge.textContent = `${sampleLabel} · ${modeLabel} · sorted by ${configSortCol}`;
    }

    const fmt = (v) => v ? Math.round(v).toLocaleString() : '—';
    const fmtPpm = (v) => v ? v.toFixed(2) : '—';
    const arrow = (col) => col === configSortCol ? (configSortAsc ? ' ▲' : ' ▼') : '';
    const th = (col, label) => `<th onclick="sortConfigLeaderboard('${col}')" style="cursor:pointer; user-select:none; padding:0.5rem 0.75rem; border-bottom:1px solid #1e3a5f; font-weight:600; color:#a0b4cc; text-align:right; white-space:nowrap">${label}${arrow(col)}</th>`;
    const thLeft = (col, label) => `<th onclick="sortConfigLeaderboard('${col}')" style="cursor:pointer; user-select:none; padding:0.5rem 0.75rem; border-bottom:1px solid #1e3a5f; font-weight:600; color:#a0b4cc; text-align:left; white-space:nowrap">${label}${arrow(col)}</th>`;

    let html = '<table style="width:100%; border-collapse:collapse; font-size:0.9rem">';
    html += '<thead><tr>';
    html += '<th style="padding:0.5rem 0.75rem; text-align:right; color:#6b82a0">#</th>';
    html += thLeft('model',     'Instrument');
    html += thLeft('spd',       'SPD');
    html += thLeft('amount',    'Amount');
    html += th('precursors', 'Precursors');
    html += th('peptides',   'Peptides');
    html += th('proteins',   'Proteins');
    if (currentTab !== 'dia') html += th('psms', 'PSMs');
    html += th('ms1ppm',     'MS1 ppm');
    html += th('n',          'n');
    html += '</tr></thead><tbody>';

    rows.forEach((r, i) => {
        const isBestDepth = (currentTab === 'dda' ? r.psms : r.precursors) === bestDepth;
        const isBestMs1   = r.ms1ppm > 0 && r.ms1ppm === bestMs1;
        const rowBg = i % 2 ? 'rgba(11,29,51,0.4)' : 'transparent';
        const badgeDepth = isBestDepth ? ' <span style="font-size:0.7rem; padding:0.1rem 0.35rem; border-radius:3px; background:rgba(56,189,248,0.25); color:var(--accent)">best depth</span>' : '';
        const badgeMs1   = isBestMs1   ? ' <span style="font-size:0.7rem; padding:0.1rem 0.35rem; border-radius:3px; background:rgba(16,185,129,0.25); color:#10b981">best accuracy</span>' : '';
        const cell = (v, right=true) => `<td style="padding:0.45rem 0.75rem; text-align:${right?'right':'left'}; border-bottom:1px solid rgba(30,58,95,0.4)">${v}</td>`;
        html += `<tr style="background:${rowBg}">`;
        html += cell(i+1);
        html += cell(`<span style="color:${typeof fc==='function' ? fc(r.model) : '#a0b4cc'}; font-weight:600">${r.model}</span>${badgeDepth}${badgeMs1}`, false);
        html += cell(r.spd, false);
        html += cell(r.amount, false);
        html += cell(fmt(r.precursors));
        html += cell(fmt(r.peptides));
        html += cell(fmt(r.proteins));
        if (currentTab !== 'dia') html += cell(fmt(r.psms));
        html += cell(fmtPpm(r.ms1ppm));
        html += cell(r.n);
        html += '</tr>';
    });
    html += '</tbody></table>';
    container.innerHTML = html;
}

// Depth by Amount Loaded — companion to the platform violin. One violin
// per amount bucket × instrument_model. Apples-to-apples: sample_type
// comes from the page filter, mode from the DIA/DDA tab. Bucketing
// matches the design call (ultra-low <10 ng / low 10–49 / standard 50–99
// / high 100–249 / ultra-high ≥250).
const AMOUNT_BUCKETS = [
    { key: 'ultralow',  label: '<10 ng',     test: a => a < 10 },
    { key: 'low',       label: '10–49 ng',   test: a => a >= 10 && a < 50 },
    { key: 'standard',  label: '50 ng',      test: a => a >= 50 && a < 100 },
    { key: 'high',      label: '100–249 ng', test: a => a >= 100 && a < 250 },
    { key: 'ultrahigh', label: '≥250 ng',    test: a => a >= 250 },
];
function _amountBucket(a) {
    a = (a == null) ? 50 : a;
    for (const b of AMOUNT_BUCKETS) if (b.test(a)) return b.key;
    return 'standard';
}

function renderAmountDepth() {
    let plotData = allData;
    if (currentTab === 'dia') plotData = plotData.filter(s=>s.acquisition_mode.toLowerCase().includes('dia'));
    else if (currentTab === 'dda') plotData = plotData.filter(s=>s.acquisition_mode.toLowerCase().includes('dda'));

    const badge = document.getElementById('amount-mode-badge');
    if (badge) {
        const modeLabel = currentTab === 'dia' ? 'DIA (precursors)'
                        : currentTab === 'dda' ? 'DDA (PSMs)'
                        : 'ALL modes (⚠ precursors + PSMs)';
        const sampleLabel = (typeof currentSampleType !== 'undefined' && currentSampleType !== 'all')
                            ? currentSampleType.toUpperCase()
                            : 'all samples';
        const warn = currentTab === 'all' || !currentTab;
        badge.textContent = `${sampleLabel} · ${modeLabel}`;
        badge.style.background = warn ? 'rgba(234,179,8,0.2)' : 'rgba(56,189,248,0.2)';
        badge.style.color = warn ? 'var(--warn)' : 'var(--accent)';
    }

    if (plotData.length === 0) {
        const el = document.getElementById('chart-amount-depth');
        if (el) el.innerHTML = '<div class="empty-state" style="padding:2rem; text-align:center; color:var(--text-muted)">No submissions match these filters yet.</div>';
        return;
    }

    const depth = s => Math.max(s.n_precursors || 0, s.n_psms || 0);
    const modelKey = s => s.instrument_model || s.instrument_family || 'unknown';
    const models = [...new Set(plotData.map(modelKey))].sort();

    // One violin trace per (model, bucket). Plotly groups same-x violins
    // side-by-side when violinmode='group'. Bucket is the categorical x;
    // model is the legend dimension.
    const traces = [];
    for (const model of models) {
        const xs = [], ys = [];
        const subs = plotData.filter(s => modelKey(s) === model);
        for (const s of subs) {
            xs.push(AMOUNT_BUCKETS.find(b => b.key === _amountBucket(s.amount_ng))?.label || '50 ng');
            ys.push(depth(s));
        }
        if (!xs.length) continue;
        traces.push({
            type: 'violin',
            x: xs, y: ys, name: model,
            box: { visible: true },
            meanline: { visible: true },
            line: { color: fc(model) },
            fillcolor: fc(model) + '30',
            points: subs.length <= 12 ? 'all' : false,
            pointpos: 0,
            jitter: 0.2,
            marker: { size: 4, color: fc(model), opacity: 0.6 },
            spanmode: 'hard',
            scalemode: 'count',
            hovertemplate: `${model}<br>%{x}<br>IDs: %{y:,}<extra></extra>`,
        });
    }

    Plotly.newPlot('chart-amount-depth', traces, {
        ...PL,
        violinmode: 'group',
        xaxis: {
            ...PL.xaxis,
            title: 'Amount loaded',
            type: 'category',
            categoryorder: 'array',
            categoryarray: AMOUNT_BUCKETS.map(b => b.label),
        },
        yaxis: { ...PL.yaxis, title: 'Precursors / PSMs' },
        legend: {
            orientation: 'h', x: 0, y: -0.18,
            font: { color: '#a0b4cc', size: 11 },
        },
        height: 380,
    }, PC);
}

function renderViolin() {
    // Only show data matching the active tab to avoid mixing precursors and PSMs
    let plotData = allData;
    if (currentTab === 'dia') plotData = plotData.filter(s=>s.acquisition_mode.toLowerCase().includes('dia'));
    else if (currentTab === 'dda') plotData = plotData.filter(s=>s.acquisition_mode.toLowerCase().includes('dda'));

    // Amount-bucket filter — pooling 5 ng K562 with 200 ng HeLa makes
    // apples-to-oranges comparisons. Default 50 ng (the canonical UCD
    // standard); user can switch via the dropdown.
    const amountSel = document.getElementById('violin-amount-filter');
    const amountVal = amountSel ? amountSel.value : '50';
    const inAmtBucket = (a) => {
        a = (a == null) ? 50 : a;
        switch (amountVal) {
            case 'ultralow':  return a < 10;
            case 'low':       return a >= 10 && a < 50;
            case '50':        return a >= 50 && a < 100;
            case 'high':      return a >= 100 && a < 250;
            case 'ultrahigh': return a >= 250;
            default:          return true;  // 'all'
        }
    };
    if (amountVal !== 'all') {
        plotData = plotData.filter(s => inAmtBucket(s.amount_ng));
    }

    const amountLabel = {
        ultralow:  '<10 ng',
        low:       '10–49 ng',
        '50':      '50 ng',
        high:      '100–249 ng',
        ultrahigh: '≥250 ng',
        all:       'ALL amounts',
    }[amountVal] || '50 ng';

    // Update the mode badge so users always know what they're looking at.
    // Concatenates DIA/DDA, sample_type (page-level), and amount bucket
    // so the chart can never silently mix incomparable cohorts.
    const badge = document.getElementById('violin-mode-badge');
    if (badge) {
        const modeLabel = currentTab === 'dia' ? 'DIA (precursors)'
                        : currentTab === 'dda' ? 'DDA (PSMs)'
                        : 'ALL modes (⚠ precursors + PSMs)';
        const sampleLabel = (typeof currentSampleType !== 'undefined' && currentSampleType !== 'all')
                            ? currentSampleType.toUpperCase()
                            : 'all samples';
        const modeWarn = currentTab === 'all' || !currentTab;
        const amtWarn  = amountVal === 'all';
        const warn = modeWarn || amtWarn;
        badge.textContent = `${sampleLabel} · ${amountLabel} · ${modeLabel}`;
        badge.style.background = warn ? 'rgba(234,179,8,0.2)' : 'rgba(56,189,248,0.2)';
        badge.style.color = warn ? 'var(--warn)' : 'var(--accent)';
    }

    if (plotData.length === 0) {
        const el = document.getElementById('chart-violin');
        if (el) el.innerHTML = '<div class="empty-state" style="padding:2rem; text-align:center; color:var(--text-muted)">No submissions match these filters yet. Try a wider amount range or different sample type.</div>';
        return;
    }

    // Group by instrument MODEL, not family — keeps timsTOF HT / Pro / Pro 2
    // visually distinct. Falls back to family for legacy rows missing model.
    const modelKey = s => s.instrument_model || s.instrument_family || 'unknown';
    const groups = [...new Set(plotData.map(modelKey))].sort();
    const families = groups;  // alias for downstream code expecting `families`
    // Color channel is SPD (throughput). Different load amounts/sample
    // types are now guaranteed-comparable by the filter above, so SPD is
    // the meaningful per-point dimension to visualize.
    const SPD_COLORSCALE = [[0,'#5cb8ff'],[0.3,'#34d399'],[0.6,'#FFBF00'],[1,'#f87171']];
    const famIndex = Object.fromEntries(groups.map((f,i) => [f, i]));

    // Use NUMERIC x on both traces so they share the same linear axis.
    // Mixing categorical (violin) + numeric (scatter) on the same axis made
    // Plotly fall back to categorical, which dropped the scatter points at
    // the wrong categorical positions and broke the per-point color map.
    const depth = s => Math.max(s.n_precursors || 0, s.n_psms || 0);

    // 1. Violin trace per family — shape only (no points), centered on the
    //    family's numeric index
    const violinTraces = families.map((fam, i) => {
        const sub = plotData.filter(s => modelKey(s) === fam);
        return {
            type: 'violin',
            y: sub.map(depth),
            x: sub.map(() => i),                // numeric, not the family name
            name: fam,
            box: { visible: true },
            meanline: { visible: true },
            line: { color: fc(fam) },
            fillcolor: fc(fam) + '20',
            points: false,
            hoverinfo: 'skip',
            showlegend: false,
            spanmode: 'hard',
            width: 0.8,
        };
    });

    // 2. Deterministic jitter (so points don't jump on re-render)
    const jitter = s => {
        let h = 0;
        const id = s.submission_id || s.run_name || '';
        for (let k = 0; k < id.length; k++) h = (((h << 5) - h) + id.charCodeAt(k)) | 0;
        return ((h % 1000) / 1000 - 0.5) * 0.5;  // [-0.25, 0.25]
    };

    // 3. Scatter overlay — only shown when the family has ≤100 points.
    //    At 669 Lumos rows (post-backfill), individual points are noise —
    //    the violin shape already tells the distribution story. For sparse
    //    families (new lab just joining, <100 submissions) individual points
    //    are still valuable, so we cap per family and subsample the rest.
    const MAX_POINTS_PER_FAMILY = 80;
    const amountShape = s => {
        const a = s.amount_ng || 50;
        if (a < 20)  return 'diamond';
        if (a > 100) return 'square';
        return 'circle';
    };

    // Subsample: keep all points for small families, random subset for large ones
    let scatterData = [];
    for (const fam of families) {
        const sub = plotData.filter(s => modelKey(s) === fam);
        if (sub.length <= MAX_POINTS_PER_FAMILY) {
            scatterData.push(...sub);
        } else {
            // Deterministic subsample: pick every N-th point sorted by depth
            const sorted = [...sub].sort((a, b) => depth(a) - depth(b));
            const step = sorted.length / MAX_POINTS_PER_FAMILY;
            for (let i = 0; i < MAX_POINTS_PER_FAMILY; i++) {
                scatterData.push(sorted[Math.floor(i * step)]);
            }
        }
    }

    const scatterTrace = {
        type: 'scatter', mode: 'markers',
        x: scatterData.map(s => famIndex[modelKey(s)] + jitter(s)),
        y: scatterData.map(depth),
        showlegend: false,
        marker: {
            color: scatterData.map(s => s.spd || 30),
            colorscale: SPD_COLORSCALE,
            cmin: 5, cmax: 200,
            size: 5, opacity: 0.55,
            symbol: scatterData.map(amountShape),
            line: { color: '#fff', width: 0.3 },
            colorbar: {
                title: 'SPD',
                tickfont: { color: '#a0b4cc' },
                titlefont: { color: '#DAAA00' },
                len: 0.5, thickness: 10, x: 1.02,
            },
        },
        text: scatterData.map(s => {
            const col = s.column_model ? `<br>${s.column_vendor} ${s.column_model}` : '';
            return `${s.instrument_model}<br>${s.acquisition_mode.toUpperCase()}<br>${s.spd} SPD, ${s.amount_ng||50} ng${col}`;
        }),
        hovertemplate: '%{text}<br>IDs: %{y:,}<extra></extra>',
    };

    Plotly.newPlot('chart-violin', [...violinTraces, scatterTrace], {
        ...PL,
        xaxis: {
            ...PL.xaxis,
            type: 'linear',
            autorange: false,
            range: [-0.6, families.length - 0.4],
            tickmode: 'array',
            tickvals: families.map((_, i) => i),
            ticktext: families,
        },
        yaxis: { ...PL.yaxis, title: 'Precursors / PSMs' },
        showlegend: false,
        height: 380,
        annotations: [{
            x: 0.02, y: 0.98, xref: 'paper', yref: 'paper', showarrow: false,
            align: 'left',
            text: `Color: SPD &nbsp;·&nbsp; n=${plotData.length} (${scatterData.length} shown)`,
            font: { color: 'var(--text-muted)', size: 10 },
        }],
    }, PC);
}

function renderGrs() {
    const dia = allData.filter(s=>s.acquisition_mode.toLowerCase().includes('dia')&&s.n_precursors>0);
    const FAMILY_SYMBOLS = {'timsTOF HT':'circle','Astral':'diamond','Exploris 480':'square','Lumos':'triangle-up'};
    // Force numeric x. If ips_score arrives as a string from the relay JSON,
    // Plotly treats the whole axis as categorical and renders ticks in
    // data-insertion order (which looked like the axis was reversed).
    const traces = [{
        x: dia.map(s => Number(s.ips_score) || 0),
        y: dia.map(s => Number(s.n_precursors) || 0),
        text: dia.map(s => {
            const col = s.column_model ? `<br>${s.column_vendor} ${s.column_model}` : '';
            return `${s.instrument_model}<br>${s.spd} SPD, ${s.amount_ng||50} ng${col}`;
        }),
        mode:'markers', type:'scatter',
        marker: {
            // Color by SPD instead of amount_ng (amount is nearly constant
            // across the seed cohort so color-by-amount was a dead channel)
            color: dia.map(s=>s.spd||30),
            colorscale: [[0,'#5cb8ff'],[0.3,'#34d399'],[0.6,'#FFBF00'],[1,'#f87171']],
            cmin: 5, cmax: 200,
            size: dia.map(s=>Math.max(9, Math.min(18, (s.n_peptides||10000)/1000))),
            symbol: dia.map(s=>FAMILY_SYMBOLS[s.instrument_family]||'circle'),
            opacity: 0.85,
            colorbar: { title:'SPD', tickfont:{color:'#a0b4cc'}, titlefont:{color:'#DAAA00'}, len:0.6, thickness:10 },
            line: {color:'#fff', width:1},
        },
        hovertemplate: '%{text}<br>IPS: %{x}<br>Precursors: %{y:,}<extra></extra>',
        showlegend: false,
    }];
    Plotly.newPlot('chart-ips', traces, {
        ...PL,
        xaxis: {
            ...PL.xaxis,
            title: 'IPS Score (cohort depth rank) — higher = more IDs than median',
            type: 'linear',
            range: [0, 105],
            autorange: false,
            tickmode: 'linear', tick0: 0, dtick: 20,
        },
        yaxis:{...PL.yaxis,title:'Precursor Count'},
        legend:{font:{color:'#a0b4cc'}}, height:380,
        shapes: [
            {type:'line',x0:70,x1:70,y0:0,y1:1,yref:'paper',line:{color:'rgba(255,191,0,0.2)',width:1,dash:'dot'}},
        ],
        annotations: [
            // Left = bottom of cohort by depth. Right = top.
            // NOT a signal for LC vs source vs calibration — use the
            // dedicated LC charts for that. This axis is just "where do
            // my IDs sit in the community distribution".
            {x: 15, y: 0.95, xref:'x', yref:'paper', text:'← below cohort', showarrow:false,
             font:{color:'rgba(248,113,113,0.75)', size:11}},
            {x: 92, y: 0.95, xref:'x', yref:'paper', text:'above cohort →', showarrow:false,
             font:{color:'rgba(52,211,153,0.75)', size:11}},
        ],
    }, PC);
}

function renderColumnComparison() {
    // The Column Comparison panel only makes sense when at least one
    // (instrument, SPD, amount) cohort has 2+ distinct columns. Until
    // a second lab joins or one of Brett's instruments swaps columns,
    // there's nothing to compare — hide the whole row instead of
    // rendering an empty-state placeholder.
    const row = document.getElementById('row-column-compare');
    const setVisible = on => { if (row) row.style.display = on ? '' : 'none'; };

    // Group by broad cohort, then compare columns within each group
    const withCol = allData.filter(s => s.column_model && s.column_model.trim());
    if (withCol.length < 3) {
        setVisible(false);
        return;
    }

    // Build broad cohorts
    function broadId(s) {
        // Reference cards must split timsTOF HT / Pro 2 / Pro / SCP / Ultra
        // — they're meaningfully different instruments. cohort_id was
        // built from instrument_family ("timsTOF") so we substitute the
        // model here. Falls back to family when instrument_model is
        // empty (legacy submissions).
        const parts = (s.cohort_id || '').split('_');
        const tail = parts.slice(1, 3).join('_');  // spd_amount
        const model = (s.instrument_model || s.instrument_family || 'Unknown').trim();
        return `${model}_${tail}`;
    }

    const groups = {};
    withCol.forEach(s => {
        const bid = broadId(s);
        if (!groups[bid]) groups[bid] = {};
        const col = `${s.column_vendor||''} ${s.column_model}`.trim();
        if (!groups[bid][col]) groups[bid][col] = [];
        groups[bid][col].push(s);
    });

    // Find groups with 2+ different columns
    const validGroups = Object.entries(groups)
        .filter(([bid, cols]) => Object.keys(cols).length >= 2)
        .sort((a,b) => {
            const nA = Object.values(a[1]).reduce((s,arr)=>s+arr.length, 0);
            const nB = Object.values(b[1]).reduce((s,arr)=>s+arr.length, 0);
            return nB - nA;
        });

    if (!validGroups.length) {
        setVisible(false);
        return;
        // Below kept for the case where we re-enable the explainer.
        // eslint-disable-next-line no-unreachable
        const cohortCounts = Object.entries(groups).map(([bid, cols]) => ({
            bid, n_cols: Object.keys(cols).length,
            n_runs: Object.values(cols).reduce((a, arr) => a + arr.length, 0),
        }));
        const cohortList = cohortCounts
            .sort((a, b) => b.n_runs - a.n_runs)
            .slice(0, 5)
            .map(c => `<li>${readableCohort(c.bid)}: ${c.n_runs} runs on ${c.n_cols} column${c.n_cols===1?'':'s'}</li>`)
            .join('');
        document.getElementById('chart-column-compare').innerHTML =
            '<div class="empty-state" style="padding:2rem;text-align:left">' +
            '<p>Column comparison needs <b>≥2 different columns</b> sharing the same ' +
            '(instrument, SPD, amount) combination. The current cohort has one column per instrument:</p>' +
            '<ul style="margin:0.75rem 0;padding-left:1.25rem">' + cohortList + '</ul>' +
            '<p style="margin-top:0.75rem">This panel will populate once external labs with different columns submit. ' +
            'Set your LC column in <code>stan setup</code> to contribute.</p>' +
            '</div>';
        return;
    }

    // Build grouped bar chart: one group per broad cohort, bars for each column
    // Use the primary metric (precursors for DIA, PSMs for DDA)
    const traces = [];
    const COL_COLORS = ['#FFBF00', '#5cb8ff', '#c084fc', '#34d399', '#f87171', '#fb923c'];
    const allColumns = new Set();

    setVisible(true);

    validGroups.forEach(([bid]) => {
        Object.keys(groups[bid]).forEach(col => allColumns.add(col));
    });

    const colList = [...allColumns];

    colList.forEach((col, ci) => {
        const x = [];
        const y = [];
        const texts = [];

        validGroups.forEach(([bid, cols]) => {
            const subs = cols[col] || [];
            if (subs.length > 0) {
                const isDIA = subs[0].acquisition_mode.toLowerCase().includes('dia');
                const vals = isDIA ? subs.map(s=>s.n_precursors) : subs.map(s=>s.n_psms);
                const avg = vals.reduce((a,b)=>a+b, 0) / vals.length;
                const amt = subs[0].amount_ng || 50;
                const spd = subs[0].spd || '?';
                x.push(`${readableCohort(bid)}`);
                y.push(Math.round(avg));
                texts.push(`${col}<br>n=${subs.length}, avg=${Math.round(avg).toLocaleString()}`);
            } else {
                x.push(`${readableCohort(bid)}`);
                y.push(0);
                texts.push('');
            }
        });

        // Shorten column name for legend
        const shortCol = col.length > 35 ? col.substring(0, 32) + '...' : col;

        traces.push({
            x, y, text: texts,
            name: shortCol,
            type: 'bar',
            marker: { color: COL_COLORS[ci % COL_COLORS.length], opacity: 0.85 },
            hovertemplate: '%{text}<extra></extra>',
        });
    });

    Plotly.newPlot('chart-column-compare', traces, {
        ...PL,
        barmode: 'group',
        yaxis: { ...PL.yaxis, title: 'Avg Precursors (DIA) / PSMs (DDA)' },
        xaxis: { ...PL.xaxis, tickangle: -20 },
        legend: { font: { color: '#a0b4cc', size: 10 }, orientation: 'h', y: -0.25 },
        height: 420,
        margin: { ...PL.margin, b: 100 },
    }, PC);
}

function renderPointsAcrossPeak() {
    // SPD vs points across peak — the quantitation quality cliff
    // Shape by column vendor, color by instrument family
    const withPts = allData.filter(s => (s.median_points_across_peak || 0) > 0);

    if (!withPts.length) {
        document.getElementById('chart-points-peak').innerHTML =
            '<div class="empty-state" style="padding:2rem;text-align:center">' +
            '<p>Points-across-peak data will appear as labs submit new QC runs via <code>stan watch</code>.</p>' +
            '<p style="margin-top:0.5rem;color:var(--text-muted)">This metric measures how many MS2 scans sample each chromatographic peak — ' +
            'below 6 points, quantitation error exceeds 1% (Matthews &amp; Hayes, 1976).</p>' +
            '</div>';
        return;
    }

    // Group by column vendor for different marker symbols
    const SYMBOLS = {'Evosep':'circle','IonOpticks':'diamond','PepSep':'square','Thermo':'triangle-up','':'circle','other':'cross'};
    const families = [...new Set(withPts.map(s=>s.instrument_family))];

    const traces = families.map(fam => {
        const sub = withPts.filter(s=>s.instrument_family===fam);
        return {
            x: sub.map(s=>s.spd||30),
            y: sub.map(s=>s.median_points_across_peak),
            text: sub.map(s=> {
                const col = s.column_model ? `${s.column_vendor} ${s.column_model}` : 'Column not specified';
                return `${s.instrument_model}<br>${col}<br>Peak width: ${(s.median_peak_width_sec||0).toFixed(1)}s`;
            }),
            mode:'markers', type:'scatter', name:fam,
            marker: {
                color: fc(fam), size: 14, opacity: 0.85,
                symbol: sub.map(s => SYMBOLS[s.column_vendor] || 'circle'),
                line: {color:'#fff', width:1.5},
            },
            hovertemplate: '%{text}<br>SPD: %{x}<br>Points/peak: %{y:.1f}<extra></extra>',
        };
    });

    // Add the Matthews & Hayes threshold line at 6 points
    traces.push({
        x: [1, 500], y: [6, 6],
        mode: 'lines', type: 'scatter', name: 'Min for <1% error',
        line: {color: 'rgba(248,113,113,0.5)', width: 2, dash: 'dash'},
        hoverinfo: 'skip', showlegend: true,
    });

    // Add a "good" zone at 12 points
    traces.push({
        x: [1, 500], y: [12, 12],
        mode: 'lines', type: 'scatter', name: 'Recommended (12+)',
        line: {color: 'rgba(52,211,153,0.4)', width: 1.5, dash: 'dot'},
        hoverinfo: 'skip', showlegend: true,
    });

    // Cap the x-axis at 1.5x the highest observed SPD so the data
    // doesn't get crushed against the left edge when nobody runs at
    // 500 SPD.
    const maxSpd = Math.max(...withPts.map(s => s.spd || 0));
    const xMax = Math.max(60, maxSpd * 1.5);

    Plotly.newPlot('chart-points-peak', traces, {
        ...PL,
        xaxis: {...PL.xaxis, title:'Samples per Day (SPD)', type:'linear',
                range: [0, xMax]},
        yaxis: {...PL.yaxis, title:'Data Points Across Peak'},
        legend: {font:{color:'#a0b4cc', size:11}},
        height: 400,
        annotations: [
            {x:0.02, y:0.08, xref:'paper', yref:'paper', text:'<1% quant error above dashed line (Matthews & Hayes 1976)',
             showarrow:false, font:{color:'rgba(248,113,113,0.7)',size:10}},
        ],
    }, PC);
}

function renderRadar() {
    const dia = allData.filter(s=>s.acquisition_mode.toLowerCase().includes('dia')&&s.n_precursors>0);
    if (!dia.length) { document.getElementById('chart-radar').innerHTML='<div class="empty-state">No DIA data</div>'; return; }

    // Axes: 3 depth metrics + 1 LC-health metric.
    // IPS dropped — it's derived from the same depth stack so it duplicates
    // the precursor/peptide/protein axes (circular). Replaced with median
    // MS1 mass accuracy, INVERTED so "good calibration" pushes the polygon
    // outward like the depth axes do.
    const mets = ['n_precursors','n_peptides','n_proteins','median_mass_acc_ms1_ppm'];
    const labs = ['Precursors','Peptides','Proteins','MS1 Mass Acc'];
    // For each metric: true if higher is better, false if lower is better
    const HIGHER_BETTER = [true, true, true, false];

    const spdTier = spd => {
        if (!spd || spd <= 0)   return '?';
        if (spd >= 100)         return '100+ SPD';
        if (spd >= 60)          return '60-100 SPD';
        if (spd >= 30)          return '30-60 SPD';
        return '<30 SPD';
    };

    // Cohort key now includes instrument_model (HT / Pro / Pro 2 / etc.)
    // so the new Bruker hardware doesn't get pooled with timsTOF HT just
    // because they share a family. Falls back to family for legacy rows.
    const modelKey = s => s.instrument_model || s.instrument_family || 'Unknown';
    const cohorts = {};
    dia.forEach(s => {
        const key = modelKey(s) + ' ' + spdTier(s.spd);
        if (!cohorts[key]) cohorts[key] = { model: modelKey(s), vals: {} };
        mets.forEach(m => {
            const v = s[m];
            if (v == null || v <= 0) return;
            if (!cohorts[key].vals[m]) cohorts[key].vals[m] = [];
            cohorts[key].vals[m].push(v);
        });
    });

    const COHORT_MIN_N = 3;
    Object.keys(cohorts).forEach(k => {
        if ((cohorts[k].vals[mets[0]] || []).length < COHORT_MIN_N) {
            delete cohorts[k];
        }
    });

    function median(arr) {
        if (!arr.length) return 0;
        const sorted = [...arr].sort((a,b) => a - b);
        const mid = Math.floor(sorted.length / 2);
        return sorted.length % 2 ? sorted[mid] : (sorted[mid-1] + sorted[mid]) / 2;
    }

    // Normalize against the COHORT-MEDIAN range, not the all-submission range.
    // Otherwise cohort medians always sit in the middle 20-50% of the radar
    // (where most individual values cluster) and every polygon collapses
    // into the same tiny diamond. With cohort-median range, the smallest
    // cohort pins to 0 and the largest to 100 — full radial spread.
    const cohortMedians = mets.map(m =>
        Object.values(cohorts).map(c => median(c.vals[m] || [])).filter(v => v > 0)
    );
    const mins = cohortMedians.map(arr => arr.length ? Math.min(...arr) : 0);
    const maxs = cohortMedians.map(arr => arr.length ? Math.max(...arr) : 1);
    function norm(v, i) {
        if (maxs[i] === mins[i]) return 50;
        if (v == null || v <= 0) return 0;
        const pct = ((v - mins[i]) / (maxs[i] - mins[i])) * 100;
        // Invert axes where lower-is-better (mass acc, peak width)
        return HIGHER_BETTER[i] ? pct : 100 - pct;
    }

    // Each cohort key (family + SPD bucket) gets its own color.
    // Coloring by family alone makes a single-vendor lab's traces
    // all look identical even when their SPD cohorts are clearly
    // different.
    const COHORT_COLORS = [
        '#ffbf00', '#4ecdc4', '#ff6b6b', '#a78bfa',
        '#45b7d1', '#fb923c', '#10b981', '#f43f5e',
    ];
    const traces = Object.entries(cohorts).map(([key, coh], i) => {
        const medians = mets.map(m => median(coh.vals[m]));
        const n = coh.vals[mets[0]].length;
        const color = COHORT_COLORS[i % COHORT_COLORS.length];
        // Hover shows the actual median value, not the normalized one —
        // "MS1 Mass Acc 2.1 ppm" is meaningful, "47.3" is not.
        const fmt = (v, j) => {
            if (v == null) return 'n/a';
            if (j === 3) return v.toFixed(2) + ' ppm';
            return Math.round(v).toLocaleString();
        };
        const customdata = medians.map((v, j) => fmt(v, j));
        return {
            type: 'scatterpolar',
            r: [...medians.map((v, j) => norm(v, j)), norm(medians[0], 0)],
            theta: [...labs, labs[0]],
            name: key + ' (n=' + n + ')',
            fill: 'toself',
            fillcolor: color + '20',
            line: { color, width: 2.5 },
            marker: { size: 5 },
            customdata: [...customdata, customdata[0]],
            hovertemplate:
                `<b>${key}</b><br>%{theta}: %{customdata}<br>` +
                `<i>radial position: %{r:.0f}/100 within cohort range</i>` +
                `<extra></extra>`,
        };
    });

    Plotly.newPlot('chart-radar', traces, {
        ...PL,
        polar: {
            bgcolor: 'rgba(2,40,81,0.3)',
            radialaxis: { visible: true, range: [0, 100], gridcolor: 'rgba(255,191,0,0.1)', tickfont: { color: '#6b82a0' } },
            angularaxis: { gridcolor: 'rgba(255,191,0,0.15)', tickfont: { color: '#a0b4cc' } },
        },
        legend: (window.innerWidth < 768)
            ? { font: { color: '#a0b4cc', size: 9 }, orientation: 'h', x: 0, y: -0.18, yanchor: 'top' }
            : { font: { color: '#a0b4cc', size: 11 } },
        height: (window.innerWidth < 768) ? 560 : 420,
        margin: (window.innerWidth < 768) ? { t: 24, r: 12, b: 220, l: 12 } : { t: 40, r: 80, b: 40, l: 80 },
    }, PC);
}

// ── Submissions table (sortable, filterable, exportable) ────────

let tableSortCol = null;
let tableSortAsc = false;
let tablePage = 0;
const TABLE_PAGE_SIZE = 25;

function showTab(tab) {
    currentTab = tab;
    tableSortCol = null;
    tablePage = 0;
    document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    event.target.classList.add('active');
    // Re-render every chart that splits by DIA/DDA. Previously only the
    // violin + table re-ran here, which left the leaderboard / amount-depth
    // showing stale numbers after a tab switch.
    try { renderConfigLeaderboard(); } catch(e) { console.error(e); }
    try { renderAmountDepth(); }       catch(e) { console.error(e); }
    try { renderViolin(); }            catch(e) { console.error(e); }
    renderTable();
}

function sortTable(col) {
    if (tableSortCol === col) { tableSortAsc = !tableSortAsc; }
    else { tableSortCol = col; tableSortAsc = false; }
    tablePage = 0;
    renderTable();
}
function prevPage() { if (tablePage > 0) { tablePage--; renderTable(); } }
function nextPage(maxPage) { if (tablePage < maxPage) { tablePage++; renderTable(); } }

function pctile(val, arr) {
    if (!arr.length) return 50;
    return Math.round(arr.filter(v=>v<val).length/arr.length*100);
}

function pctileBadge(p) {
    if (p >= 75) return `<span class="pctile-badge pctile-top">${p}th</span>`;
    if (p >= 25) return `<span class="pctile-badge pctile-mid">${p}th</span>`;
    return `<span class="pctile-badge pctile-low">${p}th</span>`;
}

function ipsBadge(s) {
    if (s>=90) return `<span class="badge badge-ips-high">IPS ${s}</span>`;
    if (s>=80) return `<span class="badge" style="background:rgba(6,78,59,0.4);color:#6ee7b7;border:1px solid rgba(110,231,183,0.3)">IPS ${s}</span>`;
    if (s>=60) return `<span class="badge badge-ips-mid">IPS ${s}</span>`;
    return `<span class="badge badge-ips-low">IPS ${s}</span>`;
}

function modeBadge(m) {
    return m.toLowerCase().includes('dia') ? '<span class="badge badge-dia">DIA</span>' : '<span class="badge badge-dda">DDA</span>';
}

function getTableData() {
    let data = [...allData];

    // Tab filter
    if (currentTab==='dia') data = data.filter(s=>s.acquisition_mode.toLowerCase().includes('dia'));
    else if (currentTab==='dda') data = data.filter(s=>s.acquisition_mode.toLowerCase().includes('dda'));

    // Search filter
    const search = (document.getElementById('table-search')?.value || '').toLowerCase().trim();
    if (search) {
        data = data.filter(s => {
            const hay = `${s.instrument_model} ${s.instrument_family} ${s.column_vendor||''} ${s.column_model||''} ${s.acquisition_mode} ${s.cohort_id}`.toLowerCase();
            return hay.includes(search);
        });
    }

    return data;
}

function renderTable() {
    const filtered = getTableData();
    if (!filtered.length) {
        document.getElementById('table-container').innerHTML='<div class="empty-state">No matching submissions.</div>';
        return;
    }

    const isDDA = currentTab==='dda';
    const pKey = isDDA ? 'n_psms' : 'n_precursors';
    const pLabel = isDDA ? 'PSMs' : 'Precursors';

    // The percentile badge is computed against whichever depth metric the
    // user is currently sorting by, so sort and pctile agree. If the sort
    // column isn't a rankable metric (e.g. instrument, date), fall back to
    // the primary depth metric (precursors for DIA, PSMs for DDA).
    const RANKABLE = new Set([pKey, 'n_peptides', 'n_proteins', 'ips_score']);
    const pctileKey = (tableSortCol && RANKABLE.has(tableSortCol)) ? tableSortCol : pKey;
    const pctileLabel = {
        n_precursors: 'Precursors',
        n_psms:       'PSMs',
        n_peptides:   'Peptides',
        n_proteins:   'Proteins',
        ips_score:    'IPS',
    }[pctileKey] || pLabel;

    // Cohort percentiles against the active rank metric
    const cohortVals = {};
    filtered.forEach(s => {
        if (!cohortVals[s.cohort_id]) cohortVals[s.cohort_id] = [];
        cohortVals[s.cohort_id].push(s[pctileKey]||0);
    });

    // Sort
    const sortKey = tableSortCol;
    if (sortKey) {
        filtered.sort((a,b) => {
            let va, vb;
            if (sortKey === 'run_date') {
                // Sort by parsed Date epoch — nulls/invalid to bottom
                const da = runDate(a), db = runDate(b);
                va = (da && !isNaN(da.getTime())) ? da.getTime() : (tableSortAsc ? Infinity : -Infinity);
                vb = (db && !isNaN(db.getTime())) ? db.getTime() : (tableSortAsc ? Infinity : -Infinity);
            } else {
                va = a[sortKey]||0;
                vb = b[sortKey]||0;
                if (typeof va === 'string') { va = va.toLowerCase(); vb = (vb||'').toLowerCase(); }
            }
            if (va < vb) return tableSortAsc ? -1 : 1;
            if (va > vb) return tableSortAsc ? 1 : -1;
            return 0;
        });
    } else {
        filtered.sort((a,b) => (b[pKey]||0) - (a[pKey]||0));
    }

    // Pagination
    const totalRows = filtered.length;
    const maxPage = Math.max(0, Math.ceil(totalRows / TABLE_PAGE_SIZE) - 1);
    if (tablePage > maxPage) tablePage = maxPage;
    const pageStart = tablePage * TABLE_PAGE_SIZE;
    const pageRows = filtered.slice(pageStart, pageStart + TABLE_PAGE_SIZE);

    // Column definitions
    // The Pctile header label updates to show which metric drives the
    // percentile right now — so users know "PEPTIDES %" means this badge
    // is the cohort rank by peptides, not by precursors.
    const cols = [
        {key:'_pctile', label: `${pctileLabel} %`, sortKey: pKey},
        {key:'instrument_model', label:'Instrument'},
        {key:'acquisition_mode', label:'Mode'},
        {key: pKey, label: pLabel},
        {key:'n_peptides', label:'Peptides'},
        {key:'n_proteins', label:'Proteins'},
        {key:'median_points_across_peak', label:'Pts/Peak'},
        {key:'ips_score', label:'IPS'},
        {key:'column_model', label:'Column'},
        {key:'spd', label:'SPD'},
        {key:'amount_ng', label:'Amount'},
        {key:'run_date', label:'Date'},
    ];

    function sortArrow(key) {
        if (tableSortCol !== key) return '';
        return tableSortAsc ? ' &#9650;' : ' &#9660;';
    }

    let h = '<table><thead><tr>';
    cols.forEach(c => {
        const sk = c.sortKey || c.key;
        const clickable = sk !== '_pctile';
        if (clickable) {
            h += `<th style="cursor:pointer" onclick="sortTable('${sk}')">${c.label}${sortArrow(sk)}</th>`;
        } else {
            h += `<th>${c.label}</th>`;
        }
    });
    h += '</tr></thead><tbody>';

    pageRows.forEach(s => {
        const p = pctile(s[pctileKey]||0, cohortVals[s.cohort_id]||[]);
        h += '<tr>';
        h += `<td>${pctileBadge(p)}</td>`;
        h += `<td>${s.instrument_model}</td>`;
        h += `<td>${modeBadge(s.acquisition_mode)}</td>`;
        h += `<td><strong>${(s[pKey]||0).toLocaleString()}</strong></td>`;
        h += `<td>${(s.n_peptides||0).toLocaleString()}</td>`;
        h += `<td>${(s.n_proteins||0).toLocaleString()}</td>`;
        const pts = s.median_points_across_peak;
        if (pts && pts > 0) {
            const ptColor = pts >= 12 ? 'var(--green)' : pts >= 6 ? 'var(--yellow)' : 'var(--red)';
            h += `<td style="color:${ptColor};font-weight:600">${pts.toFixed(1)}</td>`;
        } else {
            h += `<td style="color:var(--text-muted)">--</td>`;
        }
        h += `<td>${ipsBadge(s.ips_score||0)}</td>`;
        const col = s.column_model ? `${s.column_vendor||''} ${s.column_model}`.trim() : '';
        h += `<td style="font-size:0.8rem;color:var(--text-muted)">${col||'--'}</td>`;
        h += `<td>${s.spd||'-'}</td>`;
        h += `<td>${s.amount_ng||50}ng</td>`;
        // Show acquisition date (run_date) not submission date
        const rd = runDate(s);
        let dt = '--';
        if (rd && !isNaN(rd.getTime()) && rd.getFullYear() >= 2000 && rd.getFullYear() <= 2100) {
            dt = rd.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
        }
        h += `<td style="font-size:0.8rem;color:var(--text-muted)" data-epoch="${rd?rd.getTime():0}">${dt}</td>`;
        h += '</tr>';
    });
    h += '</tbody></table>';
    h += `<div style="display:flex;justify-content:space-between;align-items:center;margin-top:0.75rem;font-size:0.85rem;color:var(--text-muted)">`;
    h += `<span>${totalRows} submissions</span>`;
    if (totalRows > TABLE_PAGE_SIZE) {
        h += `<span>`;
        h += `<button onclick="prevPage()" style="background:rgba(255,191,0,0.15);color:var(--ucd-gold);border:1px solid rgba(255,191,0,0.3);border-radius:4px;padding:4px 12px;cursor:pointer;margin-right:8px;font-size:0.8rem" ${tablePage===0?'disabled':''}>Prev</button>`;
        h += `Page ${tablePage+1} of ${maxPage+1}`;
        h += `<button onclick="nextPage(${maxPage})" style="background:rgba(255,191,0,0.15);color:var(--ucd-gold);border:1px solid rgba(255,191,0,0.3);border-radius:4px;padding:4px 12px;cursor:pointer;margin-left:8px;font-size:0.8rem" ${tablePage>=maxPage?'disabled':''}>Next</button>`;
        h += `</span>`;
    }
    h += `</div>`;
    document.getElementById('table-container').innerHTML = h;
}

function exportCSV() {
    const data = getTableData();
    if (!data.length) return;

    const isDDA = currentTab==='dda';
    const pKey = isDDA ? 'n_psms' : 'n_precursors';

    const headers = ['instrument_model','instrument_family','acquisition_mode',
        pKey,'n_peptides','n_proteins','median_points_across_peak',
        'ips_score','column_vendor','column_model','spd','amount_ng',
        'median_cv_precursor','missed_cleavage_rate','median_peak_width_sec','cohort_id'];

    let csv = headers.join(',') + '\n';
    data.forEach(s => {
        csv += headers.map(h => {
            const v = s[h];
            if (v === null || v === undefined) return '';
            if (typeof v === 'string' && v.includes(',')) return `"${v}"`;
            return v;
        }).join(',') + '\n';
    });

    const blob = new Blob([csv], {type: 'text/csv'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `stan_benchmark_${currentTab}_${new Date().toISOString().slice(0,10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

loadData();
</script>
</body>
</html>
"""


# ── Error telemetry endpoint ─────────────────────────────────────────

_ERROR_REPORTS_PATH = Path("error_reports.json")
_ERROR_REPORTS_MAX = 1000
_ERROR_REPORTS_LOCK = threading.Lock()
_ERROR_RATE_LIMIT = 100
_ERROR_RATE_WINDOW = 3600
_error_rate_counters: dict[str, list[float]] = {}


def _check_error_rate_limit(ip: str) -> bool:
    now = time.time()
    cutoff = now - _ERROR_RATE_WINDOW
    if ip not in _error_rate_counters:
        _error_rate_counters[ip] = []
    _error_rate_counters[ip] = [t for t in _error_rate_counters[ip] if t > cutoff]
    if len(_error_rate_counters[ip]) >= _ERROR_RATE_LIMIT:
        return False
    _error_rate_counters[ip].append(now)
    return True


def _append_error_report(report: dict) -> None:
    with _ERROR_REPORTS_LOCK:
        entries: list[dict] = []
        if _ERROR_REPORTS_PATH.exists():
            try:
                entries = json.loads(_ERROR_REPORTS_PATH.read_text())
                if not isinstance(entries, list):
                    entries = []
            except (json.JSONDecodeError, OSError):
                entries = []
        entries.append(report)
        if len(entries) > _ERROR_REPORTS_MAX:
            entries = entries[-_ERROR_REPORTS_MAX:]
        _ERROR_REPORTS_PATH.write_text(json.dumps(entries, indent=2))


class ErrorReport(BaseModel):
    timestamp: str = ""
    stan_version: str = "unknown"
    python_version: str = ""
    os: str = ""
    os_version: str = ""
    arch: str = ""
    error_type: str = ""
    error_message: str = ""
    traceback: str = ""
    search_engine: str = ""
    raw_file_name: str = ""
    vendor: str = ""
    acquisition_mode: str = ""
    instrument_model: str = ""


@app.post("/api/error-report")
async def error_report(body: ErrorReport, request: Request) -> dict:
    client_ip = request.client.host if request.client else "unknown"
    if not _check_error_rate_limit(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    record = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "client_ip_hash": hashlib.sha256(client_ip.encode()).hexdigest()[:16],
        **body.model_dump(),
    }
    if len(record.get("traceback", "")) > 5000:
        record["traceback"] = record["traceback"][:5000] + "\n... (truncated)"
    if len(record.get("error_message", "")) > 1000:
        record["error_message"] = record["error_message"][:1000] + "... (truncated)"
    try:
        _append_error_report(record)
    except Exception:
        logger.exception("Failed to store error report")
        raise HTTPException(status_code=500, detail="Failed to store report")
    logger.info("Error report: %s %s (STAN %s)", record.get("error_type"), record.get("error_message", "")[:80], record.get("stan_version"))
    return {"status": "ok"}


@app.get("/api/error-reports")
async def get_error_reports(limit: int = 50) -> dict:
    if not _ERROR_REPORTS_PATH.exists():
        return {"reports": [], "count": 0}
    try:
        entries = json.loads(_ERROR_REPORTS_PATH.read_text())
        entries = list(reversed(entries[-limit:]))
        return {"reports": entries, "count": len(entries)}
    except Exception:
        return {"reports": [], "count": 0}


# ───────────────────────────── Museum + Arcade ─────────────────────────────
# Static assets (museum.html, arcade.html, JSON/PNG data) live under
# /app/static/. Added 2026-05-15 to ship the historical-QC museum and arcade
# games alongside the relay + community dashboard.
import os as _os
from fastapi.staticfiles import StaticFiles as _StaticFiles
_STATIC_DIR = _os.path.join(_os.path.dirname(__file__), "static")
if _os.path.isdir(_STATIC_DIR):
    app.mount("/static", _StaticFiles(directory=_STATIC_DIR), name="static")

def _serve(name: str):
    """Serve static HTML with <base href="/static/"> injected so all the
    relative URLs (JSON, PNG, JS imports) resolve against the static mount."""
    path = _os.path.join(_STATIC_DIR, name)
    if not _os.path.exists(path):
        return HTMLResponse(f"<h1>Not found: {name}</h1>", status_code=404)
    html = open(path).read()
    if "<base " not in html.lower():
        html = html.replace("<head>", '<head>\n<base href="/static/">', 1)
    return HTMLResponse(html)

@app.get("/museum", response_class=HTMLResponse)
async def museum_page():
    return _serve("museum.html")

@app.get("/arcade", response_class=HTMLResponse)
async def arcade_page():
    return _serve("arcade.html")

@app.get("/angry-specs", response_class=HTMLResponse)
async def angry_specs_page():
    return _serve("angry-specs.html")

@app.get("/keratin-invaders", response_class=HTMLResponse)
async def keratin_invaders_page():
    return _serve("keratin-invaders.html")

@app.get("/mzork", response_class=HTMLResponse)
async def mzork_page():
    return _serve("mzork.html")


@app.get("/karatemass", response_class=HTMLResponse)
async def karatemass_page():
    return _serve("karatemass.html")
