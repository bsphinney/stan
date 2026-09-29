"""STAN Community Benchmark — Relay API + Public Dashboard.

This HF Space serves two purposes:
1. Relay API: accepts community benchmark submissions from STAN clients
   and writes them to the brettsp/stan-benchmark dataset. Users never
   need an HF token — this Space handles authentication.
2. Public dashboard: community reference ranges, instrument health explorer.

Hosted at: https://huggingface.co/spaces/brettsp/stan
"""

from __future__ import annotations

import hmac
import io
import io
import json
import logging
import math
import os
import re
import shutil
import tempfile
import threading
import time
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

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
SPACE_VERSION = "1.2.1"

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
#
# Each queued item carries its own path_in_repo (v1.2.0): the PEG channel
# writes peg/peg_latest.parquet and peg/submissions/*.parquet through the
# same worker, so they share the commit budget with benchmark submissions
# instead of competing with it.

import queue
# The public top-level export: huggingface_hub 2.0 dropped the
# huggingface_hub.hf_api re-export the relay imported until v1.2.0, and the
# Space's Docker build installs whatever release is current.
from huggingface_hub import CommitOperationAdd


@dataclass(frozen=True)
class _QueuedFile:
    """One file waiting for the next batched commit."""

    path_in_repo: str
    data: bytes
    # Set only for a file that is overwritten in place (peg/peg_latest.parquet).
    # A failed commit re-queues its items at the BACK of the queue, behind
    # any newer copy of the same file queued meanwhile, so plain FIFO would
    # let the stale copy land last. The version lets the drain drop it.
    version: int | None = None


_SUBMIT_QUEUE: queue.Queue[_QueuedFile] = queue.Queue()
FLUSH_INTERVAL_SEC = 60          # seconds between batch flushes
FLUSH_MAX_BATCH = 100            # max files per HF commit
_FLUSH_WORKER_STARTED = False
_FLUSH_LOCK = threading.Lock()
# Newest version queued per overwrite-in-place path. Only versioned paths
# are recorded, so the one-file-per-submission paths never accumulate here.
_LATEST_QUEUED_VERSION: dict[str, int] = {}
_QUEUE_VERSION_LOCK = threading.Lock()


def _queue_submission(submission_id: str, parquet_bytes: bytes) -> None:
    """Add a submission's parquet payload to the batch-commit queue.

    Called from /api/submit. Returns immediately — the actual HF Dataset
    write happens in the worker thread below.
    """
    _queue_file(f"submissions/{submission_id}.parquet", parquet_bytes)


def _queue_file(path_in_repo: str, data: bytes, version: int | None = None) -> None:
    """Queue any dataset file for the next batched commit.

    Args:
        path_in_repo: Destination path in the dataset repo.
        data: File contents.
        version: Pass a monotonically increasing number for a file that is
            rewritten in place; older queued copies of the same path are then
            dropped instead of committed. Leave None for write-once paths.
    """
    if version is not None:
        with _QUEUE_VERSION_LOCK:
            prev = _LATEST_QUEUED_VERSION.get(path_in_repo)
            if prev is None or version > prev:
                _LATEST_QUEUED_VERSION[path_in_repo] = version
    _SUBMIT_QUEUE.put(_QueuedFile(path_in_repo, data, version))
    _ensure_flush_worker_started()


def _is_superseded(item: _QueuedFile) -> bool:
    """True when a newer copy of this overwrite-in-place file was queued."""
    if item.version is None:
        return False
    with _QUEUE_VERSION_LOCK:
        latest = _LATEST_QUEUED_VERSION.get(item.path_in_repo, item.version)
    return item.version < latest


def _drain_batch(max_items: int = FLUSH_MAX_BATCH) -> list[_QueuedFile]:
    """Take up to ``max_items`` live items off the queue, skipping stale copies."""
    items: list[_QueuedFile] = []
    while len(items) < max_items:
        try:
            item = _SUBMIT_QUEUE.get_nowait()
        except queue.Empty:
            break
        if _is_superseded(item):
            continue
        items.append(item)
    return items


def _batch_commit_message(items: list[_QueuedFile]) -> str:
    """Commit title for one batch.

    A batch of benchmark submissions only keeps the exact pre-1.2.0 title,
    "Batch submit N runs", so the dataset history reads the same as before.
    """
    n_runs = sum(1 for it in items if it.path_in_repo.startswith("submissions/"))
    n_other = len(items) - n_runs
    if n_other == 0:
        return f"Batch submit {n_runs} runs"
    peg = f"PEG share update ({n_other} file{'' if n_other == 1 else 's'})"
    if n_runs == 0:
        return peg
    return f"Batch submit {n_runs} runs + {peg}"


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
        backoff = _flush_once(api, backoff)


def _flush_once(api: Any, backoff: int) -> int:
    """Commit one batch from the queue and return the next sleep in seconds.

    Split out of the worker loop so the commit path can be exercised
    without a thread or a sleep.
    """
    # Drain up to FLUSH_MAX_BATCH items from the queue.
    items = _drain_batch()
    if not items:
        return FLUSH_INTERVAL_SEC

    operations = [
        CommitOperationAdd(
            path_in_repo=it.path_in_repo,
            path_or_fileobj=io.BytesIO(it.data),
        )
        for it in items
    ]
    try:
        api.create_commit(
            repo_id=HF_DATASET_REPO,
            repo_type="dataset",
            operations=operations,
            commit_message=_batch_commit_message(items),
        )
        logger.info("HF batch commit OK: %d files", len(items))
        # A PEG-only commit does not touch the benchmark rows, so it has
        # no reason to throw away the submissions cache.
        if any(it.path_in_repo.startswith("submissions/") for it in items):
            _invalidate_submissions_cache()
        return FLUSH_INTERVAL_SEC   # reset backoff on success
    except Exception as e:
        # Re-enqueue the items so they get retried next cycle. Use
        # exponential backoff on rate-limit responses so we don't
        # hammer HF when it's already pushing back.
        for it in items:
            _SUBMIT_QUEUE.put(it)
        if "429" in str(e) or "Too Many Requests" in str(e):
            backoff = min(backoff * 2, 900)   # cap at 15 min
            logger.warning("HF batch commit rate-limited; backing off %ds", backoff)
            return backoff
        logger.exception("HF batch commit failed; will retry next cycle")
        return FLUSH_INTERVAL_SEC


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

# ── Claims privacy: peppered email hashes (v1.2.0, spec D3) ──
# claims.json sits in the PUBLIC dataset, and its email_hash was a bare,
# unsalted sha256(email)[:32]. Anyone holding a list of candidate emails
# (core-facility directors, say) could hash them and link a pseudonym on a
# public PEG ranking to a person. With the Space secret CLAIMS_PEPPER set,
# stored hashes become hmac_sha256(pepper, sha256(email)[:32])[:32] and are
# marked "v": 2. The HMAC input is the OLD hash, so existing entries are
# migrated from what is stored without anyone re-entering an email.
#
# The "v": 2 marker is what stops a second migration pass from HMAC-ing an
# already-peppered hash, which would lock the owner out of re-claiming.
# Never remove or rotate CLAIMS_PEPPER once set: v2 entries cannot be
# matched without it (the claim flow answers 503 rather than a false
# "different email"). Old unpeppered values remain in the dataset's git
# history; squashing that is a separate decision.
CLAIMS_HASH_VERSION = 2
# Serialises claims.json read-modify-write inside this process. Reentrant
# because verify-claim holds it across _load_claims, which may itself save
# a migration.
_CLAIMS_SAVE_LOCK = threading.RLock()


class ClaimsMisconfigured(RuntimeError):
    """A peppered (v2) claim exists but CLAIMS_PEPPER is not set."""


# ── Lab names: one canonical form for claims and the PEG channel ──
# A claim is only as good as the lookup that enforces it. claims.json keys
# and every submitted PEG display_name go through _clean_text, so a name
# that renders like a claimed one cannot pass as a different, unclaimed
# name, and a claim typed with a doubled space or pasted in NFD still
# matches its owner's submissions.
LAB_NAME_MAX = 60
# Claim limits are keyed by who is asking, never by the lab name alone: a
# budget any anonymous caller can spend on a name is a way to stop its
# owner re-claiming it, and re-claiming is how a lab rotates its token.
CLAIM_CALLS_PER_CLIENT_HOUR = 10    # POST /api/claim-name per caller address, refused calls included
CLAIM_CODES_PER_HOUR = 3            # codes issued per (lab name, email)
CLAIM_MAX_ATTEMPTS = 5              # wrong codes per claim_id; per caller without one
CLAIM_LEGACY_ATTEMPTS_PER_NAME = 20  # wrong codes without a claim_id, all callers together
CLAIM_RATE_WINDOW_SEC = 3600

# Unicode Default_Ignorable_Code_Point (DerivedCoreProperties.txt). These
# render as nothing, and several are not category C, so dropping C* alone
# kept them: combining grapheme joiner (Mn), variation selectors (Mn), the
# Hangul fillers (Lo).
_DEFAULT_IGNORABLE = (
    (0x00AD, 0x00AD), (0x034F, 0x034F), (0x061C, 0x061C), (0x115F, 0x1160),
    (0x17B4, 0x17B5), (0x180B, 0x180F), (0x200B, 0x200F), (0x202A, 0x202E),
    (0x2060, 0x206F), (0x3164, 0x3164), (0xFE00, 0xFE0F), (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0), (0xFFF0, 0xFFF8), (0x1BCA0, 0x1BCA3), (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
# Visible as blank but not whitespace to str.isspace(): BRAILLE PATTERN BLANK.
_BLANK_AS_SPACE = frozenset({0x2800})


def _default_ignorable(cp: int) -> bool:
    return any(lo <= cp <= hi for lo, hi in _DEFAULT_IGNORABLE)


def _clean_text(value: Any) -> str:
    """Canonical form of a lab name or other submitted label.

    NFKC folds compatibility forms (fullwidth letters, ligatures) onto their
    plain spelling; control, format and default-ignorable characters are
    dropped; any blank is a space; whitespace is collapsed and stripped.
    Zero-width spaces, bidi overrides, variation selectors, the combining
    grapheme joiner and the Hangul fillers all render as nothing, so left in
    they would let a name that looks exactly like a claimed one pass as a
    different, unclaimed name.

    Case is kept (live claims hold both "Clogged PeakTail" and "Clogged
    Peaktail"). Cross-script confusables, such as a Cyrillic "С" for a Latin
    "C", are NOT handled: such a name is a different, unverified lab, and the
    missing check mark is what tells them apart.
    """
    if not isinstance(value, str):
        return ""
    s = unicodedata.normalize("NFKC", value)
    kept = []
    for ch in s:
        cp = ord(ch)
        if ch.isspace() or cp in _BLANK_AS_SPACE:
            kept.append(" ")
        elif not (_default_ignorable(cp) or unicodedata.category(ch).startswith("C")):
            kept.append(ch)
    # Dropping a joiner can leave a base letter and its combining mark
    # adjacent again ("e" CGJ U+0301); recompose so that equals "é".
    s = unicodedata.normalize("NFC", "".join(kept))
    return " ".join(s.split())


def _claim_name(raw: str) -> str:
    """Canonical lab name for claim-name / verify-claim, or 400."""
    if len(raw) > LAB_NAME_MAX * 4:
        raise HTTPException(status_code=400, detail=f"Lab name must be 1-{LAB_NAME_MAX} characters.")
    name = _clean_text(raw)
    if not name or len(name) > LAB_NAME_MAX:
        raise HTTPException(status_code=400, detail=f"Lab name must be 1-{LAB_NAME_MAX} characters.")
    if name.lower() == "anonymous lab":
        raise HTTPException(status_code=400, detail="'Anonymous Lab' is the default name and cannot be claimed.")
    return name


def _claims_for(claims: dict, name: str) -> list[dict]:
    """Every claims.json entry whose key is ``name`` in canonical form.

    Keys written before claims were canonicalised may differ from their
    canonical form ("Double  Space"). Looking them up raw would let such a
    claimed name read as unclaimed; more than one entry per canonical name
    means all of them bind it (callers fail closed).
    """
    return [
        entry if isinstance(entry, dict) else {}
        for key, entry in claims.items()
        if _clean_text(key) == name
    ]


def _claim_clock() -> float:
    """Monotonic seconds for the claim-code rate window."""
    return time.monotonic()


def _window_hit(buckets: dict[str, list[float]], lock: threading.Lock, key: str,
                limit: int, window_sec: float, now: float) -> bool:
    """Count one hit for ``key`` in a sliding window; False once it already has ``limit``."""
    cutoff = now - window_sec
    with lock:
        hits = [t for t in buckets.get(key, ()) if t > cutoff]
        allowed = len(hits) < limit
        if allowed:
            hits.append(now)
        buckets[key] = hits
        if len(buckets) > 1000:
            for k in [k for k, v in buckets.items() if not v or v[-1] <= cutoff]:
                del buckets[k]
    return allowed


def _window_full(buckets: dict[str, list[float]], lock: threading.Lock, key: str,
                 limit: int, window_sec: float, now: float) -> bool:
    """Has ``key`` already had ``limit`` hits in the window? Counts nothing."""
    cutoff = now - window_sec
    with lock:
        return sum(1 for t in buckets.get(key, ()) if t > cutoff) >= limit


_CLAIM_RATE: dict[str, list[float]] = {}
_CLAIM_RATE_LOCK = threading.Lock()


def _hash(s: str) -> str:
    return hashlib.sha256(s.strip().lower().encode()).hexdigest()[:32]


def _claims_pepper() -> str:
    """Server-side pepper for claim email hashes; '' when the secret is unset."""
    return os.environ.get("CLAIMS_PEPPER", "")


def _pepper_email_hash(legacy_hash: str, pepper: str) -> str:
    """v2 email hash: HMAC-SHA256 keyed by the pepper over the legacy hash."""
    return hmac.new(pepper.encode(), legacy_hash.encode(), hashlib.sha256).hexdigest()[:32]


def _stored_email_hash(email: str) -> tuple[str, int | None]:
    """Email hash to persist for a new claim, plus its "v" marker (None = legacy)."""
    legacy = _hash(email)
    pepper = _claims_pepper()
    if pepper:
        return _pepper_email_hash(legacy, pepper), CLAIMS_HASH_VERSION
    return legacy, None


def _claim_email_matches(entry: dict, email: str) -> bool:
    """Does ``email`` match the email a claim was registered with?

    Compares in whichever form the entry is stored: v2 entries against the
    peppered hash, legacy entries against the bare hash.

    Raises:
        ClaimsMisconfigured: the entry is v2 but CLAIMS_PEPPER is unset.
    """
    stored = str(entry.get("email_hash") or "")
    candidate = _hash(email)
    if entry.get("v") == CLAIMS_HASH_VERSION:
        pepper = _claims_pepper()
        if not pepper:
            raise ClaimsMisconfigured("claim is peppered (v2) but CLAIMS_PEPPER is not set")
        candidate = _pepper_email_hash(candidate, pepper)
    return hmac.compare_digest(candidate.encode(), stored.encode())


def _hf_missing_file(exc: BaseException) -> bool:
    """True when an hf_hub_download error means "that file is not in the repo".

    Only that case may be treated as an empty store. LocalEntryNotFoundError
    subclasses EntryNotFoundError but means the Hub could not be REACHED;
    reading it as "no file" would let the next write replace the real file
    with a near-empty one.
    """
    try:
        from huggingface_hub import errors as hf_errors
    except ImportError:  # pragma: no cover - very old huggingface_hub
        return False
    local = getattr(hf_errors, "LocalEntryNotFoundError", None)
    if local is not None and isinstance(exc, local):
        return False
    missing = tuple(
        c for c in (
            getattr(hf_errors, "RemoteEntryNotFoundError", None),
            getattr(hf_errors, "EntryNotFoundError", None),
        ) if c is not None
    )
    return bool(missing) and isinstance(exc, missing)


def _fetch_claims() -> dict:
    """Download claims.json. A missing file is {}; any other failure raises.

    force_download: without it, a HEAD that fails (timeout, 5xx, 429) makes
    hf_hub_download quietly return the copy cached at refs/main, which can
    predate this process's own last save. A strict caller would then save a
    new claim over that stale copy and erase every claim made since. The
    file is under 1 KB, so re-downloading it every time costs nothing.
    """
    from huggingface_hub import hf_hub_download
    try:
        p = hf_hub_download(
            HF_DATASET_REPO, IDENTITY_FILE, repo_type="dataset", token=HF_TOKEN, force_download=True,
        )
    except Exception as e:
        if _hf_missing_file(e):
            return {}
        raise
    claims = json.loads(Path(p).read_text())
    if not isinstance(claims, dict):
        raise ValueError(f"{IDENTITY_FILE} is not a JSON object")
    return claims


def _pepper_legacy(claims: dict, pepper: str) -> tuple[dict, int]:
    """(copy of ``claims`` with legacy email hashes peppered, how many were)."""
    legacy = [
        name for name, entry in claims.items()
        if isinstance(entry, dict) and entry.get("v") != CLAIMS_HASH_VERSION
    ]
    if not legacy:
        return claims, 0
    migrated = dict(claims)
    for name in legacy:
        entry = dict(claims[name])
        old = str(entry.get("email_hash") or "")
        entry["email_hash"] = _pepper_email_hash(old, pepper) if old else ""
        entry["v"] = CLAIMS_HASH_VERSION
        migrated[name] = entry
    return migrated, len(legacy)


def _migrate_claims(claims: dict) -> dict:
    """Pepper any legacy email hashes and persist the result (no-op without the secret)."""
    pepper = _claims_pepper()
    if not pepper:
        return claims
    view, n = _pepper_legacy(claims, pepper)
    if not n:
        return claims
    # ``claims`` was read outside the lock. Saving it as-is could write over
    # a claim that verify-claim saved in between, freeing that name again,
    # so re-read and migrate what is there now, all under the lock.
    with _CLAIMS_SAVE_LOCK:
        try:
            view, n = _pepper_legacy(_fetch_claims(), pepper)
            if n:
                _save_claims(view)
                logger.info("Peppered %d claim email hash(es) in %s", n, IDENTITY_FILE)
        except Exception:
            # Callers still get the migrated view, and _claim_email_matches
            # reads either form, so a failed save only delays the rewrite.
            logger.exception("Could not save peppered claims; will retry on next load")
    return view


def _load_claims(strict: bool = False) -> dict:
    """Load claimed names from HF Dataset.

    Args:
        strict: Raise when claims.json cannot be fetched, instead of
            returning {}. Anything that WRITES claims, or decides whether a
            name is claimed, must be strict: an outage read as "no claims"
            either wipes every claim on the next save or waves a spoofer
            through as the owner.
    """
    try:
        claims = _fetch_claims()
    except Exception:
        if strict:
            raise
        logger.warning("%s unavailable; treating as empty", IDENTITY_FILE, exc_info=True)
        return {}
    return _migrate_claims(claims)


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
    # Echoed from the claim-name answer; proves the caller is the one who
    # asked for the code. Empty from STAN versions that predate it, which
    # still verify, under the per-caller and per-name guess caps instead.
    claim_id: str = ""


@app.get("/api/names")
async def list_names() -> dict:
    """List all claimed pseudonyms for autocomplete in stan setup."""
    claims = _load_claims()
    return {"names": sorted(claims.keys())}


@app.post("/api/claim-name")
async def claim_name(req: ClaimRequest, request: Request) -> dict:
    """Start the name-claim process. Sends a 6-digit code to the email.

    Privacy: the email is NEVER stored. Only a SHA256 hash is kept to
    verify re-claims on new machines. STAN cannot de-anonymize participants.
    The verification code is ephemeral (15 minutes, in-memory only).

    The answer carries a ``claim_id`` for verify-claim to echo: it binds
    the code's wrong-guess budget to this caller.
    """
    email = req.email.strip().lower()
    if not req.pseudonym.strip() or not email:
        raise HTTPException(status_code=400, detail="Pseudonym and email are required")
    # Canonical form: the key stored in claims.json is the exact string the
    # PEG channel looks a submitted display_name up by.
    pseudonym = _claim_name(req.pseudonym)

    # Every call counts, refused ones included, so the "different email"
    # answer below cannot be used to test candidate emails freely. It counts
    # against the CALLER: counted against the name, three strangers' refused
    # calls an hour kept the owner from ever being sent a code.
    # (_peg_client_key explains which address that is behind the proxy.)
    client_key, _ = _peg_client_key(request)
    if not _window_hit(_CLAIM_RATE, _CLAIM_RATE_LOCK, f"calls\n{client_key}",
                       CLAIM_CALLS_PER_CLIENT_HOUR, CLAIM_RATE_WINDOW_SEC, _claim_clock()):
        raise HTTPException(
            status_code=429,
            detail="Too many verification requests from this address. Try again in an hour.",
        )

    # Check if already claimed by someone else. Strict: if claims.json
    # cannot be read, a claimed name must not look free.
    try:
        claims = _load_claims(strict=True)
    except Exception:
        logger.exception("claim-name: %s unavailable", IDENTITY_FILE)
        raise HTTPException(status_code=503, detail="Lab-name registry unavailable. Try again shortly.")
    for entry in _claims_for(claims, pseudonym):
        existing_hash = entry.get("email_hash", "")
        try:
            same_email = _claim_email_matches(entry, email)
        except ClaimsMisconfigured:
            logger.error("claim-name: '%s' is peppered but CLAIMS_PEPPER is unset", pseudonym)
            raise HTTPException(
                status_code=503,
                detail="Lab-name registry is misconfigured on the server. Try again later.",
            )
        if existing_hash and not same_email:
            raise HTTPException(
                status_code=409,
                detail=f"'{pseudonym}' is already claimed by a different email. "
                       "Pick a different name or use the email you originally registered with."
            )

    # Codes actually sent are capped per (name, email), which also caps how
    # many codes can be guessed at. For a claimed name only the owner's email
    # gets this far, so only someone who knows it can spend this budget, and
    # every code it costs lands in the owner's inbox.
    if not _window_hit(_CLAIM_RATE, _CLAIM_RATE_LOCK, f"codes\n{pseudonym}\n{_hash(email)}",
                       CLAIM_CODES_PER_HOUR, CLAIM_RATE_WINDOW_SEC, _claim_clock()):
        raise HTTPException(
            status_code=429,
            detail=f"Too many verification codes for '{pseudonym}'. Try again in an hour.",
        )

    # Generate 6-digit code
    code = f"{secrets.randbelow(900000) + 100000}"
    claim_id = secrets.token_urlsafe(16)

    # Store in memory (expires in 15 min)
    email_hash, hash_version = _stored_email_hash(email)
    _pending_codes[pseudonym] = {
        "code": code,
        "claim_id": claim_id,
        "email_hash": email_hash,
        "email_raw": email,  # only held in memory for sending, never persisted
        "expires": time.time() + 900,
    }
    if hash_version is not None:
        _pending_codes[pseudonym]["v"] = hash_version

    # Send the code
    ok = _send_verification_email(email, code, pseudonym)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send verification email. Try again.")

    return {
        "status": "code_sent",
        "message": f"Verification code sent to {email[:3]}...{email[email.index('@'):]}",
        "claim_id": claim_id,
    }


@app.post("/api/verify-claim")
async def verify_claim(req: VerifyRequest, request: Request) -> dict:
    """Complete the name-claim process. Returns an auth token.

    The token is stored locally at ~/.stan/community.yml and included in
    all future submissions. The relay validates the token on each submission.

    Privacy guarantee: only the SHA256 hash of the email is stored. The
    email itself and the verification code are discarded after verification.
    """
    pseudonym = _claim_name(req.pseudonym)
    code = req.code.strip()
    claim_id = req.claim_id.strip()

    pending = _pending_codes.get(pseudonym)
    # A claim_id that is not the one issued is not a guess at this code: it
    # must neither spend the code's attempts nor fall back to the path below.
    if not pending or (claim_id and not hmac.compare_digest(
            str(pending.get("claim_id", "")).encode(), claim_id.encode())):
        raise HTTPException(status_code=400, detail="No pending verification for this name. Call /api/claim-name first.")

    if time.time() > pending["expires"]:
        del _pending_codes[pseudonym]
        raise HTTPException(status_code=410, detail="Code expired. Request a new one.")

    # A 6-digit code is only safe against guessing if guesses are few.
    if claim_id:
        # Only the caller that asked for this code holds its claim_id, so
        # only that caller can spend its CLAIM_MAX_ATTEMPTS; claim-name
        # sends at most CLAIM_CODES_PER_HOUR codes per name and email.
        if not hmac.compare_digest(pending["code"].encode(), code.encode()):
            pending["attempts"] = pending.get("attempts", 0) + 1
            if pending["attempts"] >= CLAIM_MAX_ATTEMPTS:
                _pending_codes.pop(pseudonym, None)
                raise HTTPException(
                    status_code=429,
                    detail="Too many incorrect codes. Request a new one with /api/claim-name.",
                )
            raise HTTPException(status_code=403, detail="Incorrect code.")
    else:
        # No claim_id: a STAN that predates it, or anyone else. Such a guess
        # cannot be tied to whoever asked for the code, so it never spends
        # the code, which would let anyone throw away the one the owner was
        # just emailed. Guesses are capped per caller instead, plus a per-name
        # ceiling for callers that rotate addresses. That ceiling can hold up
        # only claim_id-less verification; a caller with the claim_id is
        # never affected by it.
        client_key, _ = _peg_client_key(request)
        by_caller = f"guesses\n{pseudonym}\n{client_key}"
        by_name = f"guesses\n{pseudonym}"
        now = _claim_clock()
        if (_window_full(_CLAIM_RATE, _CLAIM_RATE_LOCK, by_caller, CLAIM_MAX_ATTEMPTS,
                         CLAIM_RATE_WINDOW_SEC, now)
                or _window_full(_CLAIM_RATE, _CLAIM_RATE_LOCK, by_name, CLAIM_LEGACY_ATTEMPTS_PER_NAME,
                                CLAIM_RATE_WINDOW_SEC, now)):
            raise HTTPException(
                status_code=429,
                detail=f"Too many incorrect codes for '{pseudonym}'. Try again in an hour, "
                       "or update STAN, whose verification this limit does not apply to.",
            )
        if not hmac.compare_digest(pending["code"].encode(), code.encode()):
            for key, limit in ((by_caller, CLAIM_MAX_ATTEMPTS), (by_name, CLAIM_LEGACY_ATTEMPTS_PER_NAME)):
                _window_hit(_CLAIM_RATE, _CLAIM_RATE_LOCK, key, limit, CLAIM_RATE_WINDOW_SEC, now)
            raise HTTPException(status_code=403, detail="Incorrect code.")

    # Generate a permanent auth token for this pseudonym
    token = secrets.token_urlsafe(32)

    # Store the claim (email hash + token hash only — never the raw email).
    # Strict load: a read failure taken as {} would save a claims file
    # holding only this one name, erasing every other lab's claim.
    record = {
        "email_hash": pending["email_hash"],
        "token_hash": _hash(token),
        "claimed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    if pending.get("v") is not None:
        record["v"] = pending["v"]
    with _CLAIMS_SAVE_LOCK:
        try:
            claims = _load_claims(strict=True)
        except Exception:
            logger.exception("verify-claim: %s unavailable", IDENTITY_FILE)
            raise HTTPException(status_code=503, detail="Lab-name registry unavailable. Try again shortly.")
        # A key stored before claims were canonicalised ("Double  Space") is
        # the same name. claim-name already matched this email against it,
        # so the re-claim replaces it, retiring its token as a re-claim does.
        for alias in [k for k in claims if k != pseudonym and _clean_text(k) == pseudonym]:
            del claims[alias]
        claims[pseudonym] = record
        _save_claims(claims)

    # Clean up
    _pending_codes.pop(pseudonym, None)

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


# ── PEG Watch: community PEG share channel (v1.2.0) ─────────────────
#
# Contract: docs/superpowers/specs/2026-09-28-peg-watch-design.md in the
# STAN repo (§4.4, §4.5; decisions D1-D4).
#
# PEG is read from raw MS1 and needs no search, so it has its own channel
# rather than columns on the benchmark submission (D1): any Evosep lab can
# join without the frozen DIA-NN 2.3 search, a lab's whole history lands in
# one commit instead of one /api/update commit per row against HF's
# 256 commits/h, and the frozen benchmark schema is untouched.
#
# Storage, in dataset brettsp/stan-benchmark:
#   peg/peg_latest.parquet             whole table, one row per
#                                      (display_name, run_key), newest wins
#   peg/submissions/<ts>_<id8>.parquet the rows each accepted batch changed
# Both go through the batched commit worker above. The table lives in
# memory; if the Space restarts before a flush, the next 6-hourly client
# sync resends everything, so nothing is lost for good.
#
# Stdlib + pyarrow only. This image does not install numpy (Dockerfile),
# and /api/cohorts/{id}/tic answers "Failed" live, most likely on its
# `import numpy`. Medians and percentiles here are plain Python: at a few
# thousand rows per lab that is milliseconds, and results are cached.

PEG_LATEST_PATH = "peg/peg_latest.parquet"
PEG_SUBMISSIONS_DIR = "peg/submissions"
PEG_CLASSES = ("clean", "trace", "moderate", "heavy")
PEG_LC_SYSTEMS = ("evosep", "other")
# Evosep methods an operator can actually select. Keep in sync with
# stan/metrics/scoring.py:KNOWN_METHOD_SPD. On Bruker + Evosep an SPD is a
# method identity, so a derived 36 or 128 is not a cohort anyone ran.
EVOSEP_METHOD_SPD = frozenset({500, 300, 200, 100, 60, 40, 30, 15})
PEG_WINDOWS = (30, 90, 365)
PEG_DEFAULT_FAMILY = "timsTOF"
PEG_MIN_RUNS = 5               # to be ranked, and for a previous window to yield change_pct
PEG_MOST_IMPROVED_MAX = -15    # change_pct must be at least this negative for the badge
# A percent change needs a previous median worth dividing by. From 0.004 %
# to 3.2 % reads as "+80150 %", and 0.02 -> 0.01 % would out-improve a lab
# that went from 12 % to 7 %; both are noise at the detection floor.
PEG_CHANGE_FLOOR_PCT = 0.1     # previous median below this: change_pct is None
PEG_MOST_IMPROVED_MIN_DROP = 0.5  # the badge also needs a fall of this many percentage points
PEG_LEADERBOARD_WEEKS = 12
PEG_LC_COMPARE_WEEKS = 26
PEG_TREND_MAX_WEEKS = 260
PEG_MAX_RECORDS = 2000
PEG_RATE_LIMIT = 30            # POST /api/peg/submit per client per hour
PEG_RATE_WINDOW_SEC = 3600
PEG_CACHE_TTL_SEC = 300
PEG_LOAD_RETRY_SEC = 30
PEG_NAME_MAX = LAB_NAME_MAX
# The whole table lives in this process's memory and is re-serialised on
# every accepted batch, and the relay behind CORS "*" cannot tell a lab from
# a script. These bound what anyone can make it hold. UC Davis, the largest
# sharer, has ~4.6k runs after the Thermo backfill.
PEG_MAX_ROWS_PER_NAME = 20_000     # new runs beyond this are rejected; updates still land
PEG_MAX_UNVERIFIED_NAMES = 200     # distinct unclaimed names before a new one gets 429
PEG_MAX_UNVERIFIED_ROWS = 200_000  # all rows under unclaimed names, together
# Instrument families as STAN clients send them
# (stan.community.submit._instrument_family). Stored in this spelling
# whatever case a record used, so no client can respell a family on the
# board or split it in two.
_PEG_FAMILY_SPELLING = {f.lower(): f for f in ("timsTOF", "Astral", "Exploris", "Lumos", "Eclipse", "Orbitrap")}
_STAN_VERSION_RE = re.compile(r"[0-9A-Za-z.+-]{1,32}")
_PEG_RUN_KEY_RE = re.compile(r"^[0-9a-f]{24}$")
_PEG_DATE_FLOOR = datetime(2000, 1, 1, tzinfo=timezone.utc)
_PEG_FAR_FUTURE = datetime(9999, 1, 1, tzinfo=timezone.utc)

# Fields a client sends per run, in stored-column order. Anything else in a
# record is ignored and never stored, so a client that mistakenly sends a
# run name cannot publish it.
_PEG_RECORD_FIELDS = (
    "run_key", "run_date", "instrument_family", "instrument_model",
    "lc_system", "lc_model", "spd", "acquisition_mode", "sample_type",
    "amount_ng", "peg_intensity_pct", "peg_score", "peg_n_ions_detected",
    "peg_class", "peg_method",
)
# Explicit schema: float64 so a value read back compares equal to the one a
# client resends (float32 would make every resync look like a change).
_PEG_SCHEMA = pa.schema([
    pa.field("display_name", pa.string()),
    pa.field("run_key", pa.string()),                       # 24 hex, sha256 prefix; no run name
    pa.field("run_date", pa.timestamp("us", tz="UTC")),     # acquisition time
    pa.field("instrument_family", pa.string()),
    pa.field("instrument_model", pa.string()),
    pa.field("lc_system", pa.string()),                     # "evosep" | "other"
    pa.field("lc_model", pa.string()),
    pa.field("spd", pa.int32()),
    pa.field("acquisition_mode", pa.string()),
    pa.field("sample_type", pa.string()),
    pa.field("amount_ng", pa.float64()),
    pa.field("peg_intensity_pct", pa.float64()),            # PEG share of MS1, percent
    pa.field("peg_score", pa.float64()),                    # 0-100
    pa.field("peg_n_ions_detected", pa.int32()),
    pa.field("peg_class", pa.string()),                     # clean | trace | moderate | heavy
    pa.field("peg_method", pa.string()),                    # e.g. "stan-peg-1"
    pa.field("verified", pa.bool_()),                       # name was claimed and the token matched
    pa.field("submitted_at", pa.timestamp("us", tz="UTC")),  # last time this row changed
    pa.field("first_seen_at", pa.timestamp("us", tz="UTC")),
])
_PEG_COLUMNS = tuple(f.name for f in _PEG_SCHEMA)
_PEG_TIMESTAMP_COLUMNS = ("run_date", "submitted_at", "first_seen_at")


class PegStoreUnavailable(RuntimeError):
    """peg_latest.parquet could not be read, so nothing may be written over it."""


# rows: {(display_name, run_key): row}. Rows are replaced, never mutated,
# so a snapshot list of them can be read outside the lock.
# names: {display_name: [rows, verified rows]}, kept in step with ``rows``
# so the size limits in peg_submit cost O(names), not O(rows).
_PEG_STORE: dict[str, Any] = {"loaded": False, "rows": {}, "names": {}, "version": 0, "failed_at": None}
_PEG_LOCK = threading.RLock()
_PEG_CACHE: dict[tuple, tuple[float, dict]] = {}
_PEG_CACHE_LOCK = threading.Lock()
_PEG_RATE: dict[str, list[float]] = {}
_PEG_RATE_LOCK = threading.Lock()


def _peg_now() -> datetime:
    """Current UTC time: the date every PEG window is anchored at."""
    return datetime.now(timezone.utc)


def _peg_clock() -> float:
    """Monotonic seconds for cache ages, the rate window and load retries."""
    return time.monotonic()


def _as_utc(dt: datetime) -> datetime:
    """Aware UTC datetime; a naive value is taken to be UTC already."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)


def _iso_z(dt: datetime) -> str:
    return _as_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── PEG store: load, serialise ──

def _peg_rows_from_parquet(path: Path) -> dict[tuple[str, str], dict]:
    """Read peg_latest.parquet into the in-memory {(name, run_key): row} map."""
    table = pq.read_table(path)
    rows: dict[tuple[str, str], dict] = {}
    skipped = 0
    for rec in table.to_pylist():
        row = {c: rec.get(c) for c in _PEG_COLUMNS}
        if not row["display_name"] or not row["run_key"] or row["run_date"] is None:
            skipped += 1
            continue
        for c in _PEG_TIMESTAMP_COLUMNS:
            if row[c] is not None:
                row[c] = _as_utc(row[c])
        row["verified"] = bool(row["verified"])
        rows[(row["display_name"], row["run_key"])] = row
    if skipped:
        logger.warning("%s: skipped %d rows without name/run_key/run_date", PEG_LATEST_PATH, skipped)
    return rows


def _peg_rows_to_parquet(rows: list[dict]) -> bytes:
    """Serialise rows with the explicit PEG schema, sorted for stable output."""
    ordered = sorted(rows, key=lambda r: (r["display_name"], r["run_date"], r["run_key"]))
    table = pa.Table.from_pylist(
        [{c: r.get(c) for c in _PEG_COLUMNS} for r in ordered], schema=_PEG_SCHEMA,
    )
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def _peg_ensure_loaded() -> None:
    """Load peg_latest.parquet into memory once. Caller holds _PEG_LOCK.

    A file that is not in the dataset yet is an empty store. Any other
    failure raises and leaves the store unloaded: a table rebuilt from one
    batch after a failed read would overwrite every other lab's history on
    the next commit.

    Raises:
        PegStoreUnavailable: the file could not be read (retried after
            PEG_LOAD_RETRY_SEC, so an outage does not stall every request
            on a fresh download attempt).
    """
    if _PEG_STORE["loaded"]:
        return
    failed_at = _PEG_STORE["failed_at"]
    if failed_at is not None and _peg_clock() - failed_at < PEG_LOAD_RETRY_SEC:
        raise PegStoreUnavailable(f"{PEG_LATEST_PATH} failed to load moments ago")
    from huggingface_hub import hf_hub_download
    try:
        try:
            path = hf_hub_download(
                HF_DATASET_REPO, PEG_LATEST_PATH, repo_type="dataset", token=HF_TOKEN,
            )
        except Exception as e:
            if not _hf_missing_file(e):
                raise
            rows: dict[tuple[str, str], dict] = {}
            logger.info("%s not in the dataset yet; starting an empty PEG store", PEG_LATEST_PATH)
        else:
            rows = _peg_rows_from_parquet(Path(path))
    except Exception as e:
        _PEG_STORE["failed_at"] = _peg_clock()
        logger.exception("Could not load %s", PEG_LATEST_PATH)
        raise PegStoreUnavailable(str(e)) from e
    names: dict[str, list[int]] = {}
    for row in rows.values():
        _peg_tally(names, None, row)
    _PEG_STORE.update(loaded=True, rows=rows, names=names, failed_at=None)
    logger.info("PEG store loaded: %d rows", len(rows))


def _peg_tally(names: dict[str, list[int]], old: dict | None, new: dict) -> None:
    """Keep the per-name [rows, verified rows] tally in step with one store write."""
    tally = names.setdefault(new["display_name"], [0, 0])
    if old is None:
        tally[0] += 1
    else:
        tally[1] -= bool(old.get("verified"))
    tally[1] += bool(new.get("verified"))


# ── PEG submit: identity, rate limit, validation ──

def _peg_client_key(request: Request) -> tuple[str, int]:
    """Rate-limit key for the caller, and how many X-Forwarded-For entries came in.

    Behind the Space's proxy request.client.host is the proxy itself, which
    would make a per-IP limit global. Proxies append the address they saw
    to X-Forwarded-For, so the right-most entry is the one a client cannot
    forge (the left-most is whatever the client chose to send). Which hop
    the HF proxy chain leaves there is not verified yet; the submit log
    records the entry count so that can be checked after deploy.
    """
    parts = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if parts:
        return parts[-1], len(parts)
    return (request.client.host if request.client else "unknown"), 0


def _peg_rate_ok(key: str) -> bool:
    """Count one request against ``key``; False once it has PEG_RATE_LIMIT this hour."""
    return _window_hit(_PEG_RATE, _PEG_RATE_LOCK, key, PEG_RATE_LIMIT, PEG_RATE_WINDOW_SEC, _peg_clock())


def _peg_identity(name: str, token: str) -> bool:
    """Decide ``verified`` for a submitting lab name (spec §4.5, D3).

    A claimed name must present the token issued by /api/verify-claim;
    claims store only its hash, compared as _hash(token). An unclaimed name
    is accepted but unverified. ``name`` is already canonical, and claims
    are matched by the canonical form of their key, so a claim stored as
    "Double  Space" still binds "Double Space".

    Raises:
        HTTPException: 403 for a claimed name without its token; 503 when
            claims.json cannot be read (an unreadable registry must not
            make a claimed name look free).
    """
    try:
        claims = _load_claims(strict=True)
    except Exception:
        logger.exception("PEG submit: %s unavailable", IDENTITY_FILE)
        raise HTTPException(status_code=503, detail="Lab-name registry unavailable. Try again shortly.")
    entries = _claims_for(claims, name)
    if not entries:
        return False
    token = (token or "").strip()
    for entry in entries:
        token_hash = str(entry.get("token_hash") or "")
        if token and token_hash and hmac.compare_digest(_hash(token).encode(), token_hash.encode()):
            return True
    raise HTTPException(
        status_code=403,
        detail="This lab name is claimed. Run `stan community-claim` to get a token.",
    )


def _peg_number(value: Any) -> float | None:
    """A finite JSON number as float; None for anything else (bools included)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def _peg_int(value: Any) -> int | None:
    """A JSON number with no fractional part as int; else None."""
    f = _peg_number(value)
    if f is None or f != int(f):
        return None
    return int(f)


def _peg_parse_run_date(value: Any) -> datetime | None:
    """ISO-8601 string to aware UTC datetime; a missing offset means UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s[-1] in "Zz":
        s = s[:-1] + "+00:00"
    try:
        return _as_utc(datetime.fromisoformat(s))
    except ValueError:
        return None


def _peg_optional_text(rec: dict, key: str, max_len: int, lower: bool = False) -> tuple[str | None, str | None]:
    """(value, error) for an optional free-text field; empty becomes None."""
    raw = rec.get(key)
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, f"{key} must be a string"
    text = _clean_text(raw)
    if len(text) > max_len:
        return None, f"{key} is longer than {max_len} characters"
    if not text:
        return None, None
    return (text.lower() if lower else text), None


def _peg_validate_record(rec: Any, now: datetime) -> tuple[dict | None, str | None]:
    """Validate and normalise one share record (spec §4.5).

    Only measured PEG is accepted: peg_class must be one of the four real
    classes and the numbers must be present. 'unknown' is the reader's
    failure sentinel (score 0.0), and a null sent as 0 would rank a lab
    that never measured anything as the cleanest on the board.

    Returns:
        (record, None) when valid, else (None, reason).
    """
    if not isinstance(rec, dict):
        return None, "record must be a JSON object"
    run_key = rec.get("run_key")
    run_key = run_key.strip().lower() if isinstance(run_key, str) else ""
    if not _PEG_RUN_KEY_RE.match(run_key):
        return None, "run_key must be 24 hex characters"
    run_date = _peg_parse_run_date(rec.get("run_date"))
    if run_date is None:
        return None, "run_date must be an ISO-8601 timestamp"
    if run_date > now + timedelta(days=1):
        return None, "run_date is in the future"
    if run_date < _PEG_DATE_FLOOR:
        return None, "run_date is before 2000"
    lc = rec.get("lc_system")
    lc = lc.strip().lower() if isinstance(lc, str) else ""
    if lc not in PEG_LC_SYSTEMS:
        return None, "lc_system must be 'evosep' or 'other'"
    cls = rec.get("peg_class")
    cls = cls.strip().lower() if isinstance(cls, str) else ""
    if cls not in PEG_CLASSES:
        return None, "peg_class must be clean, trace, moderate or heavy"
    pct = _peg_number(rec.get("peg_intensity_pct"))
    if pct is None or not 0 <= pct <= 100:
        return None, "peg_intensity_pct must be a number from 0 to 100"
    score = _peg_number(rec.get("peg_score"))
    if score is None or not 0 <= score <= 100:
        return None, "peg_score must be a number from 0 to 100"
    n_ions = None
    if rec.get("peg_n_ions_detected") is not None:
        n_ions = _peg_int(rec.get("peg_n_ions_detected"))
        if n_ions is None or not 0 <= n_ions <= 500:
            return None, "peg_n_ions_detected must be an integer from 0 to 500"
    spd = _peg_int(rec.get("spd"))
    if spd is None or not 1 <= spd <= 2000:
        return None, "spd must be an integer from 1 to 2000"
    family = _clean_text(rec.get("instrument_family"))
    if not family or len(family) > 60:
        return None, "instrument_family is required (at most 60 characters)"
    family = _PEG_FAMILY_SPELLING.get(family.lower(), family)
    model = _clean_text(rec.get("instrument_model"))
    if not model or len(model) > 80:
        return None, "instrument_model is required (at most 80 characters)"
    amount = None
    if rec.get("amount_ng") is not None:
        amount = _peg_number(rec.get("amount_ng"))
        if amount is None or not 0 <= amount <= 1_000_000:
            return None, "amount_ng must be a number from 0 to 1e6"
    texts: dict[str, str | None] = {}
    for key, max_len, lower in (
        ("lc_model", 80, False), ("acquisition_mode", 40, True),
        ("sample_type", 40, True), ("peg_method", 40, False),
    ):
        texts[key], err = _peg_optional_text(rec, key, max_len, lower)
        if err:
            return None, err
    return {
        "run_key": run_key,
        "run_date": run_date,
        "instrument_family": family,
        "instrument_model": model,
        "lc_system": lc,
        "lc_model": texts["lc_model"],
        "spd": spd,
        "acquisition_mode": texts["acquisition_mode"],
        "sample_type": texts["sample_type"],
        "amount_ng": amount,
        "peg_intensity_pct": pct,
        "peg_score": score,
        "peg_n_ions_detected": n_ions,
        "peg_class": cls,
        "peg_method": texts["peg_method"],
    }, None


def _peg_same(old: dict, rec: dict, verified: bool) -> bool:
    """True when a stored row already says exactly what ``rec`` says."""
    if bool(old.get("verified")) != verified:
        return False
    return all(old.get(f) == rec.get(f) for f in _PEG_RECORD_FIELDS)


class PegSubmission(BaseModel):
    """POST /api/peg/submit body (spec §4.4).

    ``records`` is untyped on purpose: each record is validated on its own
    so one bad row is reported in ``rejected`` instead of failing the batch.
    """

    display_name: str = ""
    stan_version: str = ""
    records: list[Any] = []


@app.post("/api/peg/submit")
def peg_submit(body: PegSubmission, request: Request) -> dict:
    """Accept a batch of per-run PEG records from a STAN client.

    The client is stateless and resends every shareable run on each sync,
    so records identical to what is stored count as ``unchanged`` and a
    batch that changes nothing makes no commit at all.
    """
    client_key, xff_entries = _peg_client_key(request)
    if not _peg_rate_ok(client_key):
        raise HTTPException(
            status_code=429,
            detail=f"Rate limit: {PEG_RATE_LIMIT} PEG submissions per hour from one address.",
        )
    # Bound the raw length before normalising it (NFKC is linear, but not free).
    name = _clean_text(body.display_name) if len(body.display_name) <= PEG_NAME_MAX * 4 else ""
    if not name or len(name) > PEG_NAME_MAX:
        raise HTTPException(status_code=400, detail="display_name is required (1-60 characters).")
    if name.lower() == "anonymous lab":
        raise HTTPException(
            status_code=400,
            detail="'Anonymous Lab' cannot share PEG. Set display_name in ~/.stan/community.yml.",
        )
    if len(body.records) > PEG_MAX_RECORDS:
        raise HTTPException(
            status_code=413, detail=f"At most {PEG_MAX_RECORDS} records per request; send batches.",
        )
    verified = _peg_identity(name, request.headers.get("X-STAN-Auth", ""))

    now = _peg_now()
    rejected: list[dict] = []
    valid: list[tuple[int, dict]] = []
    for i, raw in enumerate(body.records):
        rec, reason = _peg_validate_record(raw, now)
        if rec is None:
            rejected.append({"index": i, "reason": reason})
        else:
            valid.append((i, rec))
    # The same run twice in one batch: the later record wins. Keeping only
    # one matters beyond tidiness -- merging both would rewrite the row
    # twice, so a client with a duplicated run would commit on every sync
    # even though nothing ever changes.
    last_index = {rec["run_key"]: i for i, rec in valid}
    batch: list[tuple[int, dict]] = []
    for i, rec in valid:
        if last_index[rec["run_key"]] == i:
            batch.append((i, rec))
        else:
            rejected.append({
                "index": i,
                "reason": f"duplicate run_key; record {last_index[rec['run_key']]} of this batch was used",
            })

    changed: list[dict] = []
    unchanged = 0
    with _PEG_LOCK:
        try:
            _peg_ensure_loaded()
        except PegStoreUnavailable:
            raise HTTPException(
                status_code=503, detail="PEG store unavailable; nothing was written. Retry later.",
            )
        store = _PEG_STORE["rows"]
        names = _PEG_STORE["names"]
        # Size limits. A claimed name is bounded by its email-verified claim;
        # an unclaimed one only by these, so unclaimed names are also capped
        # in number and in rows all together.
        if not verified and name not in names and (
            sum(1 for n_rows, n_verified in names.values() if n_verified == 0) >= PEG_MAX_UNVERIFIED_NAMES
        ):
            raise HTTPException(
                status_code=429,
                detail="The PEG board is not taking new unclaimed lab names. Claim yours with "
                       "`stan community-claim`, then run `stan peg-sync` again.",
            )
        n_rows = names.get(name, (0, 0))[0]
        unverified_rows = 0 if verified else sum(n - v for n, v in names.values())
        for i, rec in batch:
            old = store.get((name, rec["run_key"]))
            if old is not None and _peg_same(old, rec, verified):
                unchanged += 1
                continue
            if old is None:
                # New runs only: an update to a stored run always lands.
                if n_rows >= PEG_MAX_ROWS_PER_NAME:
                    rejected.append({"index": i, "reason": (
                        f"lab row limit reached ({PEG_MAX_ROWS_PER_NAME} runs per lab name)")})
                    continue
                if not verified and unverified_rows >= PEG_MAX_UNVERIFIED_ROWS:
                    rejected.append({"index": i, "reason": (
                        "row limit for unclaimed lab names reached; claim yours with `stan community-claim`")})
                    continue
                n_rows += 1
                if not verified:
                    unverified_rows += 1
            changed.append({
                **rec,
                "display_name": name,
                "verified": verified,
                "submitted_at": now,
                "first_seen_at": (old or {}).get("first_seen_at") or now,
            })
        if changed:
            # Serialise the changed rows BEFORE touching the store, so a row
            # that cannot be written never gets into the table.
            audit_bytes = _peg_rows_to_parquet(changed)
            for row in changed:
                key = (name, row["run_key"])
                _peg_tally(names, store.get(key), row)
                store[key] = row
            _PEG_STORE["version"] += 1
            version = _PEG_STORE["version"]
            snapshot = list(store.values())

    rejected.sort(key=lambda r: r["index"])
    if changed:
        latest_bytes = _peg_rows_to_parquet(snapshot)
        stamp = _as_utc(now).strftime("%Y%m%dT%H%M%SZ")
        _queue_file(f"{PEG_SUBMISSIONS_DIR}/{stamp}_{uuid.uuid4().hex[:8]}.parquet", audit_bytes)
        _queue_file(PEG_LATEST_PATH, latest_bytes, version=version)

    # stan_version is free text from the client and goes into the one log
    # line that is read to check the proxy chain; a newline in it could
    # forge a whole second line. Anything but a plain version string is "?".
    stan_version = body.stan_version if _STAN_VERSION_RE.fullmatch(body.stan_version or "") else "?"
    logger.info(
        "PEG submit %r (verified=%s, stan %s): %d accepted, %d unchanged, %d rejected "
        "[client %s, %d X-Forwarded-For entries]",
        name, verified, stan_version, len(changed), unchanged, len(rejected),
        hashlib.sha256(client_key.encode()).hexdigest()[:12], xff_entries,
    )
    return {
        "status": "ok",
        "display_name": name,
        "verified": verified,
        "accepted": len(changed),
        "unchanged": unchanged,
        "rejected": rejected,
    }


# ── PEG aggregates (pure functions of the stored rows + "now") ──

def _peg_quantile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolation quantile (PG percentile_cont); None when empty."""
    n = len(sorted_vals)
    if n == 0:
        return None
    pos = q * (n - 1)
    lo = math.floor(pos)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _r3(x: float | None) -> float | None:
    return None if x is None else round(x, 3)


def _peg_share(rows: list[dict], cls: str) -> int | None:
    """Percent of rows in class ``cls``, rounded; None when there are no rows."""
    if not rows:
        return None
    return round(100 * sum(1 for r in rows if r["peg_class"] == cls) / len(rows))


def _peg_countable(rows: list[dict]) -> tuple[list[dict], set[str]]:
    """Rows that count toward public aggregates, and the verified lab names.

    Once a name has any verified row, its unverified rows are left out. A
    lab that claims its name resends everything with its token and so
    re-marks all of its own runs verified; what stays unverified under that
    name is either a run the lab no longer shares or a row someone else sent
    under the name before it was claimed. Neither speaks for the lab.
    """
    verified_names = {r["display_name"] for r in rows if r.get("verified")}
    keep = [r for r in rows if r.get("verified") or r["display_name"] not in verified_names]
    return keep, verified_names


def _peg_family_names(rows: list[dict]) -> dict[str, str]:
    """Lower-cased instrument family -> the spelling to show for it.

    A family STAN knows keeps its fixed spelling. Any other takes the
    spelling that arrived first, verified rows before unverified ones. A
    majority vote would let one unverified client respell a family for
    every reader by sending 2000 rows.
    """
    names: dict[str, str] = {}
    best: dict[str, tuple] = {}
    for r in rows:
        fam = r["instrument_family"]
        key = fam.lower()
        if key in _PEG_FAMILY_SPELLING:
            names[key] = _PEG_FAMILY_SPELLING[key]
            continue
        rank = (not r.get("verified"), r.get("first_seen_at") or _PEG_FAR_FUTURE, fam)
        if key not in best or rank < best[key]:
            best[key] = rank
            names[key] = fam
    return names


def _peg_week_edges(end: datetime, n_weeks: int) -> list[datetime]:
    """Edges of ``n_weeks`` trailing 7-day buckets ending at ``end``, oldest first.

    Buckets trail the window end (the end of the as-of day) rather than
    following calendar weeks, so the newest point is always a full week and
    lines up with the 30/90/365-day windows, as in the approved mockup.
    """
    return [end - timedelta(days=7 * (n_weeks - i)) for i in range(n_weeks + 1)]


def _peg_weekly_buckets(rows: list[dict], edges: list[datetime]) -> list[list[dict]]:
    buckets: list[list[dict]] = [[] for _ in range(len(edges) - 1)]
    for r in rows:
        t = r["run_date"]
        if edges[0] <= t < edges[-1]:
            buckets[int((t - edges[0]) // timedelta(days=7))].append(r)
    return buckets


def _peg_weekly_medians(rows: list[dict], edges: list[datetime]) -> list[float | None]:
    return [
        _r3(_peg_quantile(sorted(r["peg_intensity_pct"] for r in b), 0.5))
        for b in _peg_weekly_buckets(rows, edges)
    ]


def _peg_window_bounds(now: datetime, window: int) -> tuple[date, datetime, datetime]:
    """(as_of, window start, window end): the ``window`` UTC days ending today."""
    as_of = _as_utc(now).date()
    end = _day_start(as_of) + timedelta(days=1)
    return as_of, end - timedelta(days=window), end


def _peg_prev_median(prev_rows: list[dict]) -> float | None:
    """Median of the previous window; None when it had fewer than 5 runs."""
    if len(prev_rows) < PEG_MIN_RUNS:
        return None
    return _peg_quantile(sorted(r["peg_intensity_pct"] for r in prev_rows), 0.5)


def _peg_change_pct(current: float, prev: float | None) -> int | None:
    """Percent change of the median vs the previous window's median.

    None without a previous median, or when it is below
    PEG_CHANGE_FLOOR_PCT: relative change from next to nothing is
    unbounded noise, not an improvement or a regression.
    """
    if prev is None or prev < PEG_CHANGE_FLOOR_PCT:
        return None
    return round(100 * (current - prev) / prev)


def _peg_leaderboard(rows: list[dict], family: str, spd: int, window: int, now: datetime) -> dict:
    """GET /api/peg/leaderboard payload (spec §4.5). Ranks Evosep runs only."""
    as_of, start, end = _peg_window_bounds(now, window)
    prev_start = start - timedelta(days=window)
    year_start = end - timedelta(days=365)
    edges = _peg_week_edges(end, PEG_LEADERBOARD_WEEKS)

    countable, verified_names = _peg_countable(rows)
    names = _peg_family_names(countable)
    evosep = [
        r for r in countable
        if r["lc_system"] == "evosep" and r["spd"] in EVOSEP_METHOD_SPD
    ]

    by_cohort: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for r in evosep:
        if year_start <= r["run_date"] < end:
            by_cohort[(r["instrument_family"].lower(), r["spd"])].append(r)
    cohorts = [
        {
            "family": names[fam],
            "spd": s,
            "n_labs": len({r["display_name"] for r in rs}),
            "n_runs_365d": len(rs),
        }
        for (fam, s), rs in sorted(by_cohort.items(), key=lambda kv: (names[kv[0][0]].lower(), -kv[0][1]))
    ]

    fam_key = family.lower()
    cur: dict[str, list[dict]] = defaultdict(list)
    prev: dict[str, list[dict]] = defaultdict(list)
    recent: dict[str, list[dict]] = defaultdict(list)
    for r in evosep:
        if r["spd"] != spd or r["instrument_family"].lower() != fam_key:
            continue
        t, lab = r["run_date"], r["display_name"]
        if start <= t < end:
            cur[lab].append(r)
        elif prev_start <= t < start:
            prev[lab].append(r)
        if edges[0] <= t < end:
            recent[lab].append(r)

    labs = []
    for lab, rs in cur.items():
        n = len(rs)
        labs.append({
            "name": lab,
            "rows": rs,
            "n": n,
            "median": _peg_quantile(sorted(r["peg_intensity_pct"] for r in rs), 0.5),
            "prev_median": _peg_prev_median(prev.get(lab, [])),
            "clean": sum(1 for r in rs if r["peg_class"] == "clean") / n,
        })
    ranked_labs = sorted(
        (lab for lab in labs if lab["n"] >= PEG_MIN_RUNS),
        key=lambda lab: (lab["median"], -lab["clean"], -lab["n"], lab["name"].lower(), lab["name"]),
    )
    ranked = [
        {
            "rank": i,
            "display_name": lab["name"],
            "verified": lab["name"] in verified_names,
            "instrument_models": sorted({r["instrument_model"] for r in lab["rows"]}),
            "n_runs": lab["n"],
            "median_pct": _r3(lab["median"]),
            "clean_pct": _peg_share(lab["rows"], "clean"),
            "heavy_pct": _peg_share(lab["rows"], "heavy"),
            "change_pct": _peg_change_pct(lab["median"], lab["prev_median"]),
            "weekly": _peg_weekly_medians(recent.get(lab["name"], []), edges),
            "badges": [],
        }
        for i, lab in enumerate(ranked_labs, start=1)
    ]
    if len(ranked) >= 2:
        ranked[0]["badges"].append("cleanest")
    improvers = [
        r for r, lab in zip(ranked, ranked_labs)
        if r["change_pct"] is not None and r["change_pct"] <= PEG_MOST_IMPROVED_MAX
        and lab["prev_median"] - lab["median"] >= PEG_MOST_IMPROVED_MIN_DROP
    ]
    # Like "cleanest", a badge on a one-lab board ranks the lab against
    # nobody; the E2E run showed a lone lab collecting "Most improved".
    if improvers and len(ranked) >= 2:
        min(improvers, key=lambda r: (r["change_pct"], r["rank"]))["badges"].append("most_improved")
    unranked = [
        {"display_name": lab["name"], "verified": lab["name"] in verified_names, "n_runs": lab["n"]}
        for lab in sorted(labs, key=lambda lab: (-lab["n"], lab["name"].lower(), lab["name"]))
        if lab["n"] < PEG_MIN_RUNS
    ]
    pooled = sorted(r["peg_intensity_pct"] for rs in cur.values() for r in rs)
    return {
        "generated_at": _iso_z(now),
        "as_of": as_of.isoformat(),
        "window_days": window,
        "family": names.get(fam_key) or _PEG_FAMILY_SPELLING.get(fam_key, family),
        "spd": spd,
        "cohorts": cohorts,
        "ranked": ranked,
        "unranked": unranked,
        "community": {
            "n_labs": len(cur),
            "n_runs": len(pooled),
            "p25_pct": _r3(_peg_quantile(pooled, 0.25)),
            "median_pct": _r3(_peg_quantile(pooled, 0.5)),
            "p75_pct": _r3(_peg_quantile(pooled, 0.75)),
        },
    }


def _peg_trend(rows: list[dict], family: str, spd: int, weeks: int, now: datetime) -> dict:
    """GET /api/peg/trend payload: weekly community PEG band for one Evosep cohort."""
    _, _, end = _peg_window_bounds(now, 1)
    edges = _peg_week_edges(end, weeks)
    countable, _ = _peg_countable(rows)
    fam_key = family.lower()
    selected = [
        r for r in countable
        if r["lc_system"] == "evosep" and spd in EVOSEP_METHOD_SPD
        and r["spd"] == spd and r["instrument_family"].lower() == fam_key
    ]
    out = []
    for i, bucket in enumerate(_peg_weekly_buckets(selected, edges)):
        pcts = sorted(r["peg_intensity_pct"] for r in bucket)
        out.append({
            "week_start": edges[i].date().isoformat(),
            "n_labs": len({r["display_name"] for r in bucket}),
            "n_runs": len(bucket),
            "p25": _r3(_peg_quantile(pcts, 0.25)),
            "p50": _r3(_peg_quantile(pcts, 0.5)),
            "p75": _r3(_peg_quantile(pcts, 0.75)),
        })
    return {"weeks": out}


def _peg_lc_compare(rows: list[dict], family: str, window: int, now: datetime) -> dict:
    """GET /api/peg/lc-compare payload: Evosep vs other LC within one family (D4).

    Never ranks anything. PEG share depends on the detector (absolute 1e4
    floor), so only a within-family comparison is like-for-like.
    """
    as_of, start, end = _peg_window_bounds(now, window)
    edges = _peg_week_edges(end, PEG_LC_COMPARE_WEEKS)
    countable, _ = _peg_countable(rows)
    names = _peg_family_names(countable)
    fam_key = family.lower()

    groups = []
    for lc in PEG_LC_SYSTEMS:
        all_rows = [
            r for r in countable
            if r["lc_system"] == lc and r["instrument_family"].lower() == fam_key
        ]
        win = [r for r in all_rows if start <= r["run_date"] < end]
        pcts = sorted(r["peg_intensity_pct"] for r in win)
        groups.append({
            "lc": lc,
            "n_labs": len({r["display_name"] for r in win}),
            "n_runs": len(win),
            "p25_pct": _r3(_peg_quantile(pcts, 0.25)),
            "median_pct": _r3(_peg_quantile(pcts, 0.5)),
            "p75_pct": _r3(_peg_quantile(pcts, 0.75)),
            "clean_pct": _peg_share(win, "clean"),
            "heavy_pct": _peg_share(win, "heavy"),
            "weekly": _peg_weekly_medians(all_rows, edges),
        })

    per_family: dict[str, dict[str, list]] = defaultdict(
        lambda: {lc: [0, set()] for lc in PEG_LC_SYSTEMS}
    )
    for r in countable:
        if start <= r["run_date"] < end:
            slot = per_family[r["instrument_family"].lower()][r["lc_system"]]
            slot[0] += 1
            slot[1].add(r["display_name"])
    families = [
        {
            "family": names[fam],
            "evosep_runs": c["evosep"][0],
            "other_runs": c["other"][0],
            "evosep_labs": len(c["evosep"][1]),
            "other_labs": len(c["other"][1]),
        }
        for fam, c in sorted(
            per_family.items(),
            key=lambda kv: (-(kv[1]["evosep"][0] + kv[1]["other"][0]), names[kv[0]].lower()),
        )
    ]
    return {
        "family": names.get(fam_key) or _PEG_FAMILY_SPELLING.get(fam_key, family),
        "window_days": window,
        "as_of": as_of.isoformat(),
        "groups": groups,
        "families": families,
    }


def _peg_cached(key: tuple, build: Callable[[list[dict], datetime], dict]) -> dict:
    """Serve an aggregate from the 5-minute cache, computing it on a miss.

    The key also carries the UTC date (windows are anchored there) and the
    store version, so an accepted change is visible on the next read
    instead of up to five minutes later.

    Raises:
        HTTPException: 503 when the store cannot be loaded.
    """
    now = _as_utc(_peg_now())
    with _PEG_LOCK:
        try:
            _peg_ensure_loaded()
        except PegStoreUnavailable:
            raise HTTPException(status_code=503, detail="PEG store unavailable. Try again shortly.")
        full_key = key + (now.date().isoformat(), _PEG_STORE["version"])
        with _PEG_CACHE_LOCK:
            hit = _PEG_CACHE.get(full_key)
        if hit is not None and _peg_clock() - hit[0] < PEG_CACHE_TTL_SEC:
            return hit[1]
        rows = list(_PEG_STORE["rows"].values())
    payload = build(rows, now)
    stamp = _peg_clock()
    with _PEG_CACHE_LOCK:
        for k in [k for k, (t, _) in _PEG_CACHE.items() if stamp - t >= PEG_CACHE_TTL_SEC]:
            del _PEG_CACHE[k]
        if len(_PEG_CACHE) >= 256:
            _PEG_CACHE.clear()
        _PEG_CACHE[full_key] = (stamp, payload)
    return payload


def _peg_window_param(window: int) -> int:
    if window not in PEG_WINDOWS:
        raise HTTPException(status_code=400, detail="window must be 30, 90 or 365 (days)")
    return window


def _peg_family_param(family: str) -> str:
    return _clean_text(family) or PEG_DEFAULT_FAMILY


@app.get("/api/peg/leaderboard")
def peg_leaderboard(family: str = PEG_DEFAULT_FAMILY, spd: int = 100, window: int = 30) -> dict:
    """Evosep PEG leaderboard for one cohort (instrument family x Evosep SPD method).

    Labs rank by median PEG share of MS1 over the last ``window`` days,
    lower is better; fewer than 5 runs in the window leaves a lab unranked.
    """
    window = _peg_window_param(window)
    fam = _peg_family_param(family)
    return _peg_cached(
        ("leaderboard", fam, spd, window),
        lambda rows, now: _peg_leaderboard(rows, fam, spd, window, now),
    )


@app.get("/api/peg/trend")
def peg_trend(family: str = PEG_DEFAULT_FAMILY, spd: int = 100, weeks: int = 52) -> dict:
    """Weekly community PEG band (p25/p50/p75 of runs) for one Evosep cohort, oldest first."""
    weeks = max(1, min(weeks, PEG_TREND_MAX_WEEKS))
    fam = _peg_family_param(family)
    return _peg_cached(
        ("trend", fam, spd, weeks),
        lambda rows, now: _peg_trend(rows, fam, spd, weeks, now),
    )


@app.get("/api/peg/lc-compare")
def peg_lc_compare(family: str = PEG_DEFAULT_FAMILY, window: int = 90) -> dict:
    """Evosep vs other LC PEG within one instrument family. Shown, never ranked."""
    window = _peg_window_param(window)
    fam = _peg_family_param(family)
    return _peg_cached(
        ("lc-compare", fam, window),
        lambda rows, now: _peg_lc_compare(rows, fam, window, now),
    )


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

        /* ── Evosep PEG Watch (v1.2.0) ──
           Everything is scoped to #peg with peg- prefixes: the page already has
           global .tab, .badge and table rules, and showTab() strips .active from
           every .tab on the page, so the PEG toggles must not reuse them.
           Class colours follow the STAN dashboard: clean green, trace yellow,
           moderate orange, heavy red. */
        #peg { scroll-margin-top: 1rem; --peg-clean: #34d399; --peg-heavy: #f87171; --peg-other: #60a5fa; }
        #peg a { color: var(--ucd-gold); }
        #peg button:focus-visible, #peg a:focus-visible { outline: 2px solid var(--ucd-gold); outline-offset: 2px; }
        #peg code { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace; font-size: 0.88em; color: var(--text-primary); background: rgba(1,26,58,0.7); padding: 0.05rem 0.35rem; border-radius: 4px; white-space: nowrap; }
        #peg .peg-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 12px; padding: 1rem 1.1rem; margin-bottom: 1.5rem; min-width: 0; }
        #peg .peg-card h3 { font-size: 0.95rem; color: var(--ucd-gold-dark); margin-bottom: 0.25rem; }
        #peg .peg-sub { font-size: 0.82rem; color: var(--text-muted); line-height: 1.5; max-width: 72ch; }
        #peg .peg-muted { color: var(--text-muted); }
        #peg .peg-fine { font-size: 0.8rem; color: var(--text-muted); line-height: 1.5; margin-top: 0.6rem; }
        #peg .peg-bar { display: flex; justify-content: space-between; align-items: flex-end; gap: 0.75rem 1.25rem; flex-wrap: wrap; margin-bottom: 0.85rem; }
        #peg .peg-ctrls { display: flex; flex-direction: column; gap: 0.5rem; align-items: flex-end; }
        #peg .peg-chips { display: flex; flex-wrap: wrap; gap: 0.35rem; justify-content: flex-end; }
        #peg .peg-chip { font: inherit; font-size: 0.78rem; padding: 0.3rem 0.8rem; border-radius: 999px; border: 1px solid var(--card-border); background: rgba(1,26,58,0.6); color: var(--text-secondary); cursor: pointer; white-space: nowrap; }
        #peg .peg-chip:hover { border-color: var(--ucd-gold-border); color: var(--text-primary); }
        #peg .peg-chip[aria-pressed="true"] { background: var(--ucd-gold); border-color: var(--ucd-gold); color: var(--ucd-blue-dark); font-weight: 600; }
        #peg .peg-seg { display: inline-flex; border: 1px solid var(--card-border); border-radius: 8px; overflow: hidden; background: rgba(1,26,58,0.6); }
        #peg .peg-seg button { font: inherit; font-size: 0.78rem; color: var(--text-secondary); background: none; border: 0; border-right: 1px solid var(--card-border); padding: 0.3rem 0.8rem; cursor: pointer; }
        #peg .peg-seg button:last-child { border-right: 0; }
        #peg .peg-seg button:hover:not([aria-pressed="true"]) { color: var(--text-primary); }
        #peg .peg-seg button[aria-pressed="true"] { background: var(--ucd-gold); color: var(--ucd-blue-dark); font-weight: 600; }
        #peg .peg-empty { border: 1px dashed var(--ucd-gold-border); border-radius: 10px; padding: 1.1rem 1.25rem; color: var(--text-secondary); font-size: 0.88rem; line-height: 1.6; }
        #peg .peg-empty b { color: var(--text-primary); }
        #peg .peg-scrollx { overflow-x: auto; }
        #peg table.peg-board { min-width: 840px; font-size: 0.88rem; }
        #peg .peg-board th { white-space: nowrap; padding: 0.6rem 0.7rem; font-size: 0.72rem; }
        #peg .peg-board td { padding: 0.6rem 0.7rem; vertical-align: middle; }
        #peg .peg-board .r { text-align: right; font-variant-numeric: tabular-nums; }
        #peg .peg-rank { font-weight: 800; font-size: 1.05rem; width: 2.5rem; font-variant-numeric: tabular-nums; color: var(--text-secondary); }
        #peg .peg-rank.r1 { color: var(--ucd-gold); }
        #peg .peg-lab { font-weight: 650; color: var(--text-primary); }
        #peg .peg-ok { display: inline-grid; place-items: center; width: 16px; height: 16px; border-radius: 50%; background: rgba(52,211,153,0.18); color: var(--peg-clean); font-size: 0.66rem; font-weight: 800; margin-left: 0.35rem; vertical-align: 1px; cursor: help; }
        #peg .peg-unv { font-size: 0.62rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase; padding: 1px 6px; border-radius: 4px; border: 1px dashed var(--text-muted); color: var(--text-muted); margin-left: 0.4rem; vertical-align: 1px; cursor: help; white-space: nowrap; }
        #peg .peg-badge { display: inline-block; font-size: 0.66rem; font-weight: 700; padding: 1px 7px; border-radius: 999px; border: 1px solid; margin-left: 0.4rem; vertical-align: 1px; white-space: nowrap; }
        #peg .peg-b-clean { color: var(--peg-clean); border-color: rgba(52,211,153,0.5); }
        #peg .peg-b-impr { color: #7dd3fc; border-color: rgba(125,211,252,0.5); }
        #peg .peg-meter { display: flex; align-items: center; gap: 0.5rem; min-width: 180px; }
        #peg .peg-track { flex: 1; height: 8px; border-radius: 4px; background: rgba(160,180,204,0.12); position: relative; overflow: hidden; }
        #peg .peg-fill { position: absolute; inset: 0 auto 0 0; border-radius: 4px; background: linear-gradient(90deg, #DAAA00, #FFBF00); }
        #peg .peg-val { width: 3.4rem; text-align: right; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text-primary); }
        #peg .peg-down { color: var(--peg-clean); font-weight: 650; }
        #peg .peg-up { color: var(--peg-heavy); font-weight: 650; }
        #peg svg.peg-spark { display: block; overflow: visible; }
        #peg .peg-spark path { fill: none; stroke: #a0b4cc; stroke-width: 1.6; stroke-linejoin: round; stroke-linecap: round; }
        #peg .peg-spark circle { fill: var(--text-primary); }
        #peg .peg-unranked { color: var(--text-muted); font-size: 0.82rem; margin-top: 0.75rem; line-height: 1.6; }
        #peg .peg-unranked .peg-lab { font-weight: 600; color: var(--text-secondary); }
        #peg .peg-foot { display: flex; justify-content: space-between; gap: 0.4rem 1rem; flex-wrap: wrap; color: var(--text-muted); font-size: 0.8rem; margin-top: 0.75rem; }
        #peg .peg-foot b { color: var(--text-primary); }
        #peg .peg-h4 { font-size: 0.72rem; letter-spacing: 0.08em; text-transform: uppercase; color: var(--text-muted); font-weight: 650; margin-bottom: 0.6rem; }
        #peg .peg-lcgroups { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr)); gap: 0.75rem; }
        #peg .peg-lcg { border: 1px solid var(--card-border); border-radius: 10px; padding: 0.8rem 0.9rem; background: rgba(1,26,58,0.45); display: grid; gap: 0.45rem; min-width: 0; }
        #peg .peg-lcg-top { display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; font-size: 0.78rem; flex-wrap: wrap; }
        #peg .peg-lcchip { font-size: 0.66rem; font-weight: 750; letter-spacing: 0.05em; text-transform: uppercase; padding: 2px 7px; border-radius: 4px; white-space: nowrap; }
        #peg .peg-lc-evosep { background: rgba(255,191,0,0.16); color: var(--ucd-gold); }
        #peg .peg-lc-other { background: rgba(96,165,250,0.16); color: var(--peg-other); }
        #peg .peg-big { font-size: 1.5rem; font-weight: 750; font-variant-numeric: tabular-nums; color: var(--text-primary); }
        #peg .peg-big small { font-size: 0.75rem; font-weight: 500; color: var(--text-muted); margin-left: 0.35rem; }
        #peg .peg-iqr { position: relative; height: 10px; border-radius: 5px; background: rgba(160,180,204,0.1); }
        #peg .peg-iqr .b { position: absolute; top: 0; bottom: 0; border-radius: 5px; opacity: 0.55; }
        #peg .peg-iqr .m { position: absolute; top: -3px; bottom: -3px; width: 3px; border-radius: 2px; background: var(--text-primary); }
        #peg .peg-iqr-axis { display: flex; justify-content: space-between; color: var(--text-muted); font-size: 0.66rem; margin-top: -0.2rem; }
        #peg .peg-lcg-meta { display: flex; justify-content: space-between; gap: 0.5rem; color: var(--text-muted); font-size: 0.78rem; }
        #peg .peg-lcg-meta b { color: var(--text-primary); font-variant-numeric: tabular-nums; }
        #peg .peg-lcg-wk { display: grid; gap: 0.2rem; font-size: 0.7rem; color: var(--text-muted); }
        #peg .peg-lcg-wk svg.peg-spark { width: 100%; height: auto; }
        /* An LC slot with no runs yet: same card box, dashed, holding how to fill it. */
        #peg .peg-lcg-empty { border: 1px dashed var(--ucd-gold-border); background: transparent; grid-template-rows: auto 1fr; }
        #peg .peg-lcg-join { display: grid; align-content: center; justify-items: start; gap: 0.45rem; padding: 0.6rem 0; color: var(--text-secondary); font-size: 0.85rem; line-height: 1.55; }
        #peg .peg-lcg-join a { font-size: 0.8rem; }
        #peg .peg-note { margin-top: 0.85rem; font-size: 0.8rem; color: var(--yellow); display: flex; gap: 0.5rem; align-items: flex-start; line-height: 1.5; }
        #peg .peg-note::before { content: '!'; flex: none; width: 16px; height: 16px; border-radius: 50%; border: 1px solid currentColor; display: grid; place-items: center; font-size: 0.66rem; font-weight: 800; margin-top: 1px; }
        #peg .chart-card > h3 { padding-right: 2.75rem; } /* clear the injected .fs-btn (top-right) at phone width */
        #peg .peg-trend-badge { font-size: 0.75rem; padding: 0.15rem 0.5rem; border-radius: 4px; background: var(--ucd-gold-glow); color: var(--ucd-gold); margin-left: 0.5rem; font-weight: 600; }
        #peg ol.peg-steps { list-style: none; counter-reset: pegstep; display: grid; gap: 0.6rem; margin: 0.7rem 0 0.2rem; }
        #peg ol.peg-steps li { display: grid; grid-template-columns: 26px minmax(0,1fr); gap: 0.6rem; counter-increment: pegstep; color: var(--text-secondary); font-size: 0.88rem; line-height: 1.55; }
        #peg ol.peg-steps li::before { content: counter(pegstep); width: 24px; height: 24px; border-radius: 50%; border: 1px solid var(--ucd-gold-dark); color: var(--ucd-gold); display: grid; place-items: center; font-weight: 750; font-size: 0.8rem; }
        #peg ol.peg-steps b, #peg .peg-method b { color: var(--text-primary); }
        #peg .peg-method p + p { margin-top: 0.65rem; }
        @media (max-width: 768px) {
            #peg .peg-ctrls { align-items: flex-start; }
            #peg .peg-chips { justify-content: flex-start; }
        }
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
        <a href="#peg">PEG Watch</a>
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

<!-- Evosep PEG Watch (v1.2.0). A separate relay channel from the benchmark
     above: labs share per-run PEG read from raw MS1 through /api/peg/submit,
     with no community search. Everything in this section comes from the
     pre-aggregated GET /api/peg/* endpoints, never from the benchmark rows,
     so the QC Standard and failed-run filters above do not apply to it.
     Spec: docs/superpowers/specs/2026-09-28-peg-watch-design.md (4.5, D4). -->
<div class="section" id="peg">
    <h2>Evosep PEG Watch</h2>
    <p class="description">
        Polyethylene glycol (PEG) from tips, solvents and plastics shows up in MS1 as a ladder of ions
        44.026&nbsp;Da apart, and it can cost identifications. STAN measures it in QC runs. Evosep
        labs that opt in share their per-run PEG here, so you can see whether your level is normal and
        whether a fix worked. PEG needs no database search, so this board is separate from the benchmark
        above and the filters at the top of the page do not apply to it.
    </p>

    <div class="peg-card">
        <div class="peg-bar">
            <div>
                <h3>Community PEG leaderboard</h3>
                <p class="peg-sub">Evosep labs ranked by median PEG share of MS1 across their QC runs. Lower is cleaner. Labs are compared only within one instrument family and Evosep method.</p>
            </div>
            <div class="peg-ctrls">
                <div class="peg-chips" id="peg-coh" role="group" aria-label="Cohort: instrument family and Evosep method"></div>
                <div class="peg-seg" id="peg-win" role="group" aria-label="Window">
                    <button type="button" data-win="30" aria-pressed="true">30 days</button>
                    <button type="button" data-win="90" aria-pressed="false">90 days</button>
                    <button type="button" data-win="365" aria-pressed="false">1 year</button>
                </div>
            </div>
        </div>
        <div id="peg-board-wrap"><div class="peg-empty">Loading the PEG leaderboard...</div></div>
        <div class="peg-unranked" id="peg-unranked"></div>
        <div class="peg-foot"><span id="peg-community"></span><span>A lab needs 5 QC runs in the window to be ranked.</span></div>
    </div>

    <div class="chart-row">
        <div class="chart-card chart-full">
            <h3>Community PEG, week by week <span class="peg-trend-badge" id="peg-trend-badge"></span></h3>
            <div class="chart-desc">Every shared QC run in the selected cohort, pooled across labs into 7-day buckets over the last year. Gold line = median; shaded band = the middle half of runs (25th to 75th percentile). Gaps are weeks with no runs.</div>
            <div id="chart-peg-trend"><div class="empty-state" style="padding:2rem">Loading...</div></div>
            <div class="chart-desc" id="peg-trend-note" style="margin:0.4rem 0 0"></div>
        </div>
    </div>

    <!-- The one addition to the approved PEG Watch design (D4): Evosep vs other
         LC, compared only within an instrument family. Shown, never ranked. -->
    <div class="peg-card">
        <div class="peg-bar">
            <div>
                <h3>Evosep vs other LC</h3>
                <p class="peg-sub">Does the LC front end change how much PEG reaches the MS? Shared QC runs from Evosep and non-Evosep labs on the same instrument family over the last 90 days, all gradients pooled. Shown for comparison, never ranked.</p>
            </div>
            <div class="peg-ctrls"><div class="peg-chips" id="peg-lcfam" role="group" aria-label="Instrument family"></div></div>
        </div>
        <div id="peg-lc-body"><div class="peg-empty">Loading...</div></div>
        <p class="peg-note"><span>Compare LC systems within one instrument family only. PEG share depends on the detector (STAN counts MS1 peaks above an absolute 10<sup>4</sup> intensity floor), so comparisons across instrument families, such as an Evosep timsTOF against a nanoLC Orbitrap, are not like-for-like.</span></p>
    </div>

    <div class="info-grid">
        <div class="info-card" id="peg-join">
            <h3>Put your lab on the board</h3>
            <p>Any lab running STAN 1.2 or later can join. PEG is read straight from raw MS1, so it needs no community search.</p>
            <ol class="peg-steps">
                <li><div><b>Claim your lab name.</b> Run <code>stan community-claim</code> to prove it with an emailed code. Do this first: an unclaimed name can be claimed by anyone, who then takes over its place on the board. Unclaimed names show as <span class="peg-unv" title="This name is not claimed, so anyone could submit under it">unverified</span>.</div></li>
                <li><div><b>Opt in.</b> Add <code>peg_share: true</code> to <code>~/.stan/community.yml</code>. Sharing stays off until you do.</div></li>
                <li><div><b>Sync.</b> Run <code>stan peg-sync</code>. It sends every QC run that has a PEG measurement, and the relay keeps only what changed, so it is safe to run again at any time.</div></li>
            </ol>
            <p class="peg-fine">Lab names are pseudonyms. Shared per QC run: date, instrument model, LC system, Evosep method (SPD), acquisition mode, sample type and amount, and the PEG share, score, ion count and class. File and sample names, raw data, spectra and serial numbers never leave your lab.</p>
            <p class="peg-fine">Found PEG? <a href="https://github.com/bsphinney/stan/blob/main/docs/PEG_EVOSEP_DIAGNOSTIC.md">Isolate the source in one night</a> with STAN's Evosep PEG diagnostic.</p>
        </div>
        <div class="info-card peg-method">
            <h3>How the ranking works</h3>
            <p><b>PEG share of MS1.</b> STAN reads 80 MS1 scans spread across the gradient and matches peaks within 5&nbsp;ppm to the PEG ladder: PEG1&ndash;20 as [M+H]<sup>+</sup>, [M+NH<sub>4</sub>]<sup>+</sup> and [M+Na]<sup>+</sup>, spaced 44.026&nbsp;Da (C<sub>2</sub>H<sub>4</sub>O). The share is the matched intensity over all MS1 peaks above 10<sup>4</sup> counts. Unlike the 0&ndash;100 PEG score, it keeps rising with contamination, so it still separates labs that all score 100.</p>
            <p><b>Cohorts.</b> Labs are ranked only against the same instrument family and Evosep method, because detector response and gradient length change the number. Only Evosep runs are ranked.</p>
            <p><b>Rank.</b> Median over the window's QC runs, lowest first, with at least 5 runs; ties go to more clean runs, then more runs. <b>Clean</b> means a PEG score below 20. Runs with no PEG measurement are left out; they never count as clean.</p>
            <p><b>Badges.</b> <span class="peg-badge peg-b-clean" style="margin-left:0">Cleanest</span> is rank 1 once two or more labs are ranked. <span class="peg-badge peg-b-impr" style="margin-left:0">Most improved</span> is the biggest fall in median against the previous window, if it fell by 15% or more and by at least 0.5 percentage points, also once two or more labs are ranked. <b>Change</b> stays blank when the previous window had fewer than 5 runs or a median below 0.1%, where a percent change is noise.</p>
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

<script id="stan-esc">
// HTML-escape a string for innerHTML and attribute values. Lab names,
// instrument models and families all come from submitters, and a pseudonym
// is only authenticated on the PEG channel, so every one of them passes
// through here. Its own block so tests/test_relay_peg.py can load it with
// the PEG Watch script and nothing else.
function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
</script>
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

// Translate cohort bucket names to readable labels. Shared: the reference
// cards and the column comparison chart both label cohorts with it (it once
// lived inside renderRefRanges, and renderColumnComparison threw a
// ReferenceError the moment two columns shared a cohort).
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
        // Pseudonyms are submitter-chosen and unauthenticated on /api/submit: escape them.
        labSel.innerHTML = labs.map(l => `<option value="${esc(l)}">${esc(l)}</option>`).join('');
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
        instSel.innerHTML = models.map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join('');
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
        el.innerHTML = `<div class="empty-state" style="padding:2rem">Not enough community data for ${esc(family || 'this instrument')} on this metric.</div>`;
        return;
    }

    const cMean = communityVals.reduce((a, b) => a + b, 0) / communityVals.length;
    const cSd = Math.sqrt(communityVals.reduce((a, v) => a + (v - cMean) ** 2, 0) / (communityVals.length - 1));

    // Lab's data sorted by date (parse from filename)
    const labWithMetric = labFiltered.filter(s => s[metricKey] != null && s[metricKey] !== 0);
    if (labWithMetric.length === 0) {
        el.innerHTML = `<div class="empty-state" style="padding:2rem">${esc(selectedLab)} has no data for this metric on ${esc(selectedInst || 'this instrument')}.</div>`;
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

// Kept on window so the PEG Watch script below can re-align a #peg deep
// link once these charts have pushed its section down the page.
window.stanMainLoad = loadData();
</script>
<script id="peg-watch-js">
// ── Evosep PEG Watch (community site v1.2.0) ────────────────────────
// Draws only the relay's pre-aggregated GET /api/peg/{leaderboard,trend,
// lc-compare} payloads (spec 2026-09-28-peg-watch-design.md, 4.5). It
// loads on its own: a PEG relay error never blanks the benchmark above,
// and a slow benchmark load never holds this section up.
//
// Lab names, instrument models and families are all submitter-supplied,
// and an unclaimed name is unauthenticated, so every one of them goes
// through esc() before it reaches innerHTML. The *Html / *Parts / *Traces
// builders are plain functions of a payload so tests/test_relay_peg.py can
// run them in node against hostile strings.

const PEG_LC_WINDOW = 90;             // the LC panel's fixed window, as in the approved mockup
const PEG_TREND_WEEKS = 52;
const PEG_CACHE_MS = 5 * 60 * 1000;   // the relay caches aggregates for 5 minutes too
const PEG_BAR_HI = 30;                // % at the right end of the leaderboard share bars
const PEG_IQR_HI = 20;                // % at the right end of the LC range bars
const pegState = {
    family: 'timsTOF', spd: 100, window: 30, lcFamily: null,
    cohorts: [], families: [], picked: false,
    boardSeq: 0, trendSeq: 0, lcSeq: 0,
};
const _pegCache = new Map();

function pegNum(v) { return (typeof v === 'number' && isFinite(v)) ? v : null; }

function pegPct(p) {
    p = pegNum(p);
    if (p == null) return '—';
    if (p === 0) return '0%';
    if (p < 0.1) return p.toFixed(2) + '%';
    if (p < 10) return p.toFixed(1) + '%';
    return Math.round(p) + '%';
}

function pegCount(n) { n = pegNum(n); return n == null ? '—' : n.toLocaleString('en-US'); }

function pegPlural(n, word) { return `${pegCount(n)} ${word}${pegNum(n) === 1 ? '' : 's'}`; }

function pegSameFamily(a, b) { return String(a == null ? '' : a).toLowerCase() === String(b == null ? '' : b).toLowerCase(); }

// Position of a PEG share on a log10(p + 0.05) axis from 0 to `hi` %, as a
// percentage of the track. PEG share runs from 0 to 30 % and more; on a
// linear axis every clean lab would sit on top of zero.
function pegLogPos(p, hi) {
    const lg = v => Math.log10(Math.max(0, v) + 0.05);
    return Math.max(0, Math.min(100, (lg(p) - lg(0)) / (lg(hi) - lg(0)) * 100));
}

// Sparkline of weekly medians on the same log scale, oldest first. By
// default each line fills its own box, which shows shape rather than
// level; pass lo/hi to put several on one scale (the LC panel does).
function pegSpark(vals, opt) {
    opt = opt || {};
    const w = opt.w || 96, h = opt.h || 26;
    const v = (Array.isArray(vals) ? vals : []).map(pegNum);
    const have = v.filter(x => x != null);
    if (have.length < 2) return '<span class="peg-muted">—</span>';
    const lg = x => Math.log10(Math.max(0, x) + 0.05);
    const mn = opt.lo != null ? lg(opt.lo) : Math.min(...have.map(lg));
    const mx = opt.hi != null ? lg(opt.hi) : Math.max(...have.map(lg));
    const span = (mx - mn) || 1;
    const n = v.length;
    const X = i => 2 + (n > 1 ? i / (n - 1) : 0) * (w - 4);
    const Y = x => h - 3 - (lg(x) - mn) / span * (h - 6);
    let d = '', dots = '', last = -1;
    v.forEach((x, i) => {
        if (x == null) return;
        const joined = i > 0 && v[i - 1] != null;
        d += (joined ? 'L' : 'M') + X(i).toFixed(1) + ' ' + Y(x).toFixed(1);
        // A week with no neighbour on either side draws no segment; mark it.
        if (!joined && (i === n - 1 || v[i + 1] == null)) {
            dots += `<circle cx="${X(i).toFixed(1)}" cy="${Y(x).toFixed(1)}" r="1.5"/>`;
        }
        last = i;
    });
    const label = esc('Weekly median PEG share, oldest first: ' + v.map(pegPct).join(', '));
    const size = opt.fluid ? '' : ` width="${w}" height="${h}"`;
    return `<svg class="peg-spark"${size} viewBox="0 0 ${w} ${h}" role="img" aria-label="${label}"><title>${label}</title>`
        + `<path d="${d}"/>${dots}<circle cx="${X(last).toFixed(1)}" cy="${Y(v[last]).toFixed(1)}" r="2.4"/></svg>`;
}

function pegIdentityHtml(verified) {
    return verified === true
        ? '<span class="peg-ok" title="Verified: sent with the token for this claimed lab name" aria-label="verified">&#10003;</span>'
        : '<span class="peg-unv" title="This name is not claimed, so anyone could submit under it">unverified</span>';
}

function pegCohortChipsHtml(cohorts, family, spd) {
    return (Array.isArray(cohorts) ? cohorts : []).map((c, i) => {
        const on = pegSameFamily(c.family, family) && pegNum(c.spd) === spd;
        const tip = `${pegPlural(c.n_labs, 'lab')} · ${pegPlural(c.n_runs_365d, 'QC run')} in the last year`;
        return `<button type="button" class="peg-chip" data-i="${i}" aria-pressed="${on}" title="${esc(tip)}">`
            + `${esc(c.family)} · ${esc(pegCount(c.spd))} SPD</button>`;
    }).join('');
}

// The leaderboard payload as three HTML fragments: the table (or an empty
// state), the unranked list, and the one-line community summary.
function pegBoardParts(board) {
    board = board || {};
    const ranked = Array.isArray(board.ranked) ? board.ranked : [];
    const unranked = Array.isArray(board.unranked) ? board.unranked : [];
    const cohorts = Array.isArray(board.cohorts) ? board.cohorts : [];
    const days = pegCount(board.window_days);
    const cohort = `${esc(board.family)} · ${esc(pegCount(board.spd))} SPD`;
    const out = { main: '', unranked: '', community: '' };
    if (!cohorts.length) {
        out.main = '<div class="peg-empty"><b>No Evosep lab is sharing PEG yet.</b> Be the first: run '
            + '<code>stan community-claim</code>, add '
            + '<code>peg_share: true</code> to <code>~/.stan/community.yml</code> and run <code>stan peg-sync</code>. '
            + '<a href="#peg-join">How to join</a></div>';
        return out;
    }
    if (!ranked.length) {
        out.main = `<div class="peg-empty">No lab in ${cohort} has 5 or more QC runs in the last ${days} days`
            + `${unranked.length ? ' (labs with fewer are listed below)' : ''}. Try a longer window or another cohort.</div>`;
    } else {
        let h = '<div class="peg-scrollx"><table class="peg-board"><thead><tr>'
            + '<th>#</th><th>Lab</th><th>Instrument</th><th class="r">QC runs</th>'
            + '<th>PEG share of MS1 · median</th><th class="r">Clean</th><th>Last 12 weeks</th>'
            + '<th class="r">Change</th></tr></thead><tbody>';
        ranked.forEach(r => {
            const rank = pegNum(r.rank);
            const badges = Array.isArray(r.badges) ? r.badges : [];
            const models = Array.isArray(r.instrument_models) ? r.instrument_models : [];
            const med = pegNum(r.median_pct), clean = pegNum(r.clean_pct), heavy = pegNum(r.heavy_pct);
            const chg = pegNum(r.change_pct);
            const bar = med == null ? 0 : Math.max(2, pegLogPos(med, PEG_BAR_HI));
            const chgHtml = chg == null ? '<span class="peg-muted">—</span>'
                : chg < 0 ? `<span class="peg-down">▼ ${Math.abs(chg)}%</span>`
                : chg > 0 ? `<span class="peg-up">▲ ${chg}%</span>` : '0%';
            h += '<tr>'
                + `<td class="peg-rank${rank === 1 ? ' r1' : ''}">${pegCount(rank)}</td>`
                + `<td><span class="peg-lab">${esc(r.display_name)}</span>${pegIdentityHtml(r.verified)}`
                + (badges.includes('cleanest') ? '<span class="peg-badge peg-b-clean">Cleanest</span>' : '')
                + (badges.includes('most_improved') ? '<span class="peg-badge peg-b-impr">Most improved</span>' : '')
                + '</td>'
                + `<td>${esc(models.join(', ')) || '—'}</td>`
                + `<td class="r">${pegCount(r.n_runs)}</td>`
                + `<td><div class="peg-meter"><div class="peg-track"><div class="peg-fill" style="width:${bar.toFixed(1)}%"></div></div>`
                + `<span class="peg-val">${pegPct(med)}</span></div></td>`
                + `<td class="r" title="Heavy PEG in ${heavy == null ? '—' : heavy + '%'} of runs">${clean == null ? '—' : clean + '%'}</td>`
                + `<td>${pegSpark(r.weekly)}</td>`
                + `<td class="r" title="Median vs the previous ${days} days">${chgHtml}</td>`
                + '</tr>';
        });
        out.main = h + '</tbody></table></div>';
    }
    if (unranked.length) {
        out.unranked = 'Not ranked yet, fewer than 5 QC runs in this window: ' + unranked.map(u =>
            `<span class="peg-lab">${esc(u.display_name)}</span>${pegIdentityHtml(u.verified)} · ${pegPlural(u.n_runs, 'run')}`
        ).join(', ');
    }
    const c = board.community || {};
    if (pegNum(c.n_runs)) {
        out.community = `${cohort}, last ${days} days: ${pegPlural(c.n_labs, 'lab')} · ${pegPlural(c.n_runs, 'QC run')}`
            + ` · community median <b>${pegPct(c.median_pct)}</b> (middle half ${pegPct(c.p25_pct)} to ${pegPct(c.p75_pct)})`;
    }
    return out;
}

function pegLcFamilyChipsHtml(families, current) {
    return (Array.isArray(families) ? families : []).map((f, i) => {
        const tip = `Last ${PEG_LC_WINDOW} days: Evosep ${pegPlural(f.evosep_runs, 'run')} from ${pegPlural(f.evosep_labs, 'lab')}, `
            + `other LC ${pegPlural(f.other_runs, 'run')} from ${pegPlural(f.other_labs, 'lab')}`;
        return `<button type="button" class="peg-chip" data-i="${i}" aria-pressed="${pegSameFamily(f.family, current)}" `
            + `title="${esc(tip)}">${esc(f.family)}</button>`;
    }).join('');
}

// One family's Evosep vs other-LC comparison: always two slots side by
// side, one per LC. A slot with runs draws its full card; a slot without
// is a placeholder card that says so and how to fill it, so a family with
// one LC so far shows its data instead of an empty box. Families are never
// set side by side here: PEG share is not comparable across detectors.
function pegLcHtml(d) {
    d = d || {};
    const groups = Array.isArray(d.groups) ? d.groups : [];
    const group = lc => groups.find(g => g && g.lc === lc) || { lc: lc, n_runs: 0, n_labs: 0, weekly: [] };
    const evo = group('evosep'), oth = group('other');
    const fam = esc(d.family), days = pegCount(d.window_days);
    const nE = pegNum(evo.n_runs) || 0, nO = pegNum(oth.n_runs) || 0;
    const head = `<h4 class="peg-h4">Community · ${fam} labs · last ${days} days</h4>`;
    if (!nE && !nO) {
        return head + `<div class="peg-empty">No ${fam} lab has shared PEG in the last ${days} days.</div>`;
    }
    // The empty slot. Its weekly line spans 26 weeks and the window only
    // `days`, so a group can have older runs and none in the window: that
    // is "none lately", not "no lab yet".
    const placeholder = (g, label, cls, joiners) => {
        const older = (Array.isArray(g.weekly) ? g.weekly : []).some(v => pegNum(v) != null);
        const status = older ? `none in the last ${days} days` : 'no lab yet';
        const lead = older ? `No ${fam} lab ${label === 'Evosep' ? 'on an Evosep' : 'on a non-Evosep LC'} `
            + `has shared PEG in the last ${days} days. ` : '';
        return '<div class="peg-lcg peg-lcg-empty">'
            + `<div class="peg-lcg-top"><span class="peg-lcchip ${cls}">${label}</span>`
            + `<span class="peg-muted">${status}</span></div>`
            + `<div class="peg-lcg-join"><p>${lead}Labs running ${fam} ${joiners} can join: <code>stan peg-sync</code></p>`
            + '<a href="#peg-join">How to put your lab on the board</a></div>'
            + '</div>';
    };
    const otherJoiners = pegSameFamily(d.family, 'timsTOF') ? 'with a nanoElute or other LC' : 'with a non-Evosep LC';
    // Both weekly lines on one scale, so their heights can be compared.
    const all = [].concat(evo.weekly || [], oth.weekly || []).map(pegNum).filter(x => x != null);
    const lo = all.length ? Math.min(...all) : 0, hi = all.length ? Math.max(...all) : 1;
    const card = (g, label, cls, colour) => {
        const p25 = pegNum(g.p25_pct), med = pegNum(g.median_pct), p75 = pegNum(g.p75_pct);
        const a = pegLogPos(p25 == null ? 0 : p25, PEG_IQR_HI), b = pegLogPos(p75 == null ? 0 : p75, PEG_IQR_HI);
        const m = pegLogPos(med == null ? 0 : med, PEG_IQR_HI);
        const clean = pegNum(g.clean_pct), heavy = pegNum(g.heavy_pct);
        return '<div class="peg-lcg">'
            + `<div class="peg-lcg-top"><span class="peg-lcchip ${cls}">${label}</span>`
            + `<span class="peg-muted">${pegPlural(g.n_labs, 'lab')} · ${pegPlural(g.n_runs, 'run')}</span></div>`
            + `<div class="peg-big">${pegPct(med)}<small>median PEG share</small></div>`
            + `<div class="peg-iqr" title="25th percentile ${pegPct(p25)} · median ${pegPct(med)} · 75th percentile ${pegPct(p75)}">`
            + `<span class="b" style="left:${a.toFixed(1)}%;width:${Math.max(1, b - a).toFixed(1)}%;background:${colour}"></span>`
            + `<span class="m" style="left:calc(${m.toFixed(1)}% - 1px)"></span></div>`
            + `<div class="peg-iqr-axis"><span>0</span><span>1%</span><span>${PEG_IQR_HI}%</span></div>`
            + `<div class="peg-lcg-meta"><span>clean <b>${clean == null ? '—' : clean + '%'}</b></span>`
            + `<span>heavy <b>${heavy == null ? '—' : heavy + '%'}</b></span></div>`
            + `<div class="peg-lcg-wk"><span>weekly median, last 26 weeks</span>${pegSpark(g.weekly, { w: 480, h: 44, lo: lo, hi: hi, fluid: true })}</div>`
            + '</div>';
    };
    const bar = 'Bar: the middle half of runs (25th to 75th percentile) on a log scale, tick = median.';
    return head + '<div class="peg-lcgroups">'
        + (nE ? card(evo, 'Evosep', 'peg-lc-evosep', '#FFBF00')
              : placeholder(evo, 'Evosep', 'peg-lc-evosep', 'on an Evosep'))
        + (nO ? card(oth, 'Other LC', 'peg-lc-other', '#60a5fa')
              : placeholder(oth, 'Other LC', 'peg-lc-other', otherJoiners))
        + '</div><p class="peg-fine">'
        + (nE && nO ? `${bar} The two weekly lines share one scale.`
            : `Only ${fam} labs are compared here; the ${nE ? 'Other LC' : 'Evosep'} side fills in as `
              + `${fam} labs ${nE ? 'on a non-Evosep LC' : 'on an Evosep'} join. ${bar}`)
        + '</p>';
}

// Plotly traces for the weekly community band. The band is one closed
// polygon per run of consecutive weeks with data: fill:'tonexty' would
// bridge empty weeks with a band that no run supports.
function pegTrendTraces(weeks) {
    weeks = Array.isArray(weeks) ? weeks : [];
    const x = [], p25 = [], p50 = [], p75 = [], cd = [];
    weeks.forEach(w => {
        x.push(String(w.week_start));
        p25.push(pegNum(w.p25)); p50.push(pegNum(w.p50)); p75.push(pegNum(w.p75));
        cd.push([pegNum(w.n_labs) || 0, pegNum(w.n_runs) || 0, pegNum(w.p25), pegNum(w.p75)]);
    });
    const traces = [];
    let seg = [];
    const flush = () => {
        if (seg.length >= 2) {
            const back = seg.slice().reverse();
            traces.push({
                type: 'scatter', mode: 'lines', fill: 'toself', fillcolor: 'rgba(255,191,0,0.16)',
                line: { width: 0 }, hoverinfo: 'skip', legendgroup: 'band', showlegend: traces.length === 0,
                name: 'middle half of runs (25th–75th pct)',
                x: seg.map(i => x[i]).concat(back.map(i => x[i])),
                y: seg.map(i => p75[i]).concat(back.map(i => p25[i])),
            });
        }
        seg = [];
    };
    for (let i = 0; i < weeks.length; i++) {
        if (p25[i] != null && p75[i] != null) seg.push(i); else flush();
    }
    flush();
    traces.push({
        type: 'scatter', mode: 'lines+markers', name: 'median', x: x, y: p50, customdata: cd, connectgaps: false,
        line: { color: '#FFBF00', width: 2.4 }, marker: { color: '#FFBF00', size: 5 },
        hovertemplate: '7 days from %{x|%b %d, %Y}<br>median %{y:.2f}%'
            + '<br>middle half %{customdata[2]:.2f} to %{customdata[3]:.2f}%'
            + '<br>%{customdata[1]} runs from %{customdata[0]} lab(s)<extra></extra>',
    });
    const withRuns = cd.filter(c => c[1] > 0);
    return { traces: traces, nWeeks: withRuns.length, maxLabs: withRuns.reduce((m, c) => Math.max(m, c[0]), 0) };
}

// ── wiring ──

async function pegFetch(path, params) {
    const url = path + '?' + new URLSearchParams(params).toString();
    const hit = _pegCache.get(url);
    if (hit && Date.now() - hit[0] < PEG_CACHE_MS) return hit[1];
    const r = await fetch(url);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    _pegCache.set(url, [Date.now(), d]);
    return d;
}

function _pegSet(id, html) {
    const el = document.getElementById(id);
    if (el) el.innerHTML = html;
}

async function loadPegBoard() {
    const seq = ++pegState.boardSeq;
    let board;
    try {
        board = await pegFetch('/api/peg/leaderboard',
            { family: pegState.family, spd: pegState.spd, window: pegState.window });
    } catch (e) {
        if (seq !== pegState.boardSeq) return;
        console.error('[peg leaderboard]', e);
        _pegSet('peg-board-wrap', `<div class="peg-empty">The PEG leaderboard is unavailable right now (${esc(e.message)}). `
            + 'The rest of this page is unaffected; try again in a minute.</div>');
        _pegSet('peg-unranked', ''); _pegSet('peg-community', '');
        return;
    }
    if (seq !== pegState.boardSeq) return;   // a newer toggle already asked for something else
    const cohorts = Array.isArray(board.cohorts) ? board.cohorts : [];
    // The default cohort (timsTOF, 100 SPD) may have no runs while others
    // do. Open on the busiest cohort rather than on an empty board next to
    // chips that have data. Only on first load: after that the reader picks.
    if (!pegState.picked) {
        pegState.picked = true;
        const here = cohorts.some(c => pegSameFamily(c.family, pegState.family) && pegNum(c.spd) === pegState.spd);
        if (cohorts.length && !here) {
            const best = cohorts.reduce((a, c) => ((pegNum(c.n_runs_365d) || 0) > (pegNum(a.n_runs_365d) || 0) ? c : a));
            pegState.family = String(best.family);
            pegState.spd = pegNum(best.spd) || 100;
            return loadPegBoard();
        }
    }
    pegState.cohorts = cohorts;
    try { _pegSet('peg-coh', pegCohortChipsHtml(cohorts, pegState.family, pegState.spd)); }
    catch (e) { console.error('[peg cohorts]', e); }
    try {
        const parts = pegBoardParts(board);
        _pegSet('peg-board-wrap', parts.main);
        _pegSet('peg-unranked', parts.unranked);
        _pegSet('peg-community', parts.community);
    } catch (e) {
        console.error('[peg board]', e);
        _pegSet('peg-board-wrap', '<div class="peg-empty">Could not draw the PEG leaderboard.</div>');
    }
}

async function loadPegTrend() {
    const seq = ++pegState.trendSeq;
    const el = document.getElementById('chart-peg-trend');
    const note = document.getElementById('peg-trend-note');
    const badge = document.getElementById('peg-trend-badge');
    if (!el) return;
    if (badge) badge.textContent = `${pegState.family} · ${pegState.spd} SPD`;
    const empty = msg => {
        if (window.Plotly) { try { Plotly.purge(el); } catch (e) {} }
        el.innerHTML = `<div class="empty-state" style="padding:2rem">${msg}</div>`;
        if (note) note.textContent = '';
    };
    let t;
    try {
        t = await pegFetch('/api/peg/trend', { family: pegState.family, spd: pegState.spd, weeks: PEG_TREND_WEEKS });
    } catch (e) {
        if (seq !== pegState.trendSeq) return;
        console.error('[peg trend]', e);
        empty(`The weekly band is unavailable right now (${esc(e.message)}).`);
        return;
    }
    if (seq !== pegState.trendSeq) return;
    try {
        const tr = pegTrendTraces(t && t.weeks);
        if (!tr.nWeeks) { empty(`No shared QC runs in this cohort over the last ${PEG_TREND_WEEKS} weeks.`); return; }
        if (!window.Plotly) { empty('The chart library did not load. Reload the page to see this chart.'); return; }
        Plotly.purge(el);
        el.innerHTML = '';
        Plotly.newPlot(el, tr.traces, {
            ...PL,
            height: 320,
            margin: { t: 16, r: 24, b: 40, l: 60 },
            xaxis: { ...PL.xaxis, type: 'date', tickformat: '%b %Y' },
            yaxis: { ...PL.yaxis, title: 'PEG share of MS1 (%)', rangemode: 'tozero' },
            showlegend: true,
            legend: { orientation: 'h', x: 0, y: 1.12, font: { color: '#a0b4cc' } },
            hovermode: 'closest',
        }, PC);
        if (note) {
            note.textContent = tr.maxLabs <= 1
                ? 'One lab shares in this cohort so far, so the band is that lab’s own week-to-week spread. It becomes a community range as more labs join.'
                : '';
        }
    } catch (e) {
        console.error('[peg trend render]', e);
        empty('Could not draw the weekly band.');
    }
}

async function loadPegLc() {
    const seq = ++pegState.lcSeq;
    let d;
    try {
        d = await pegFetch('/api/peg/lc-compare',
            { family: pegState.lcFamily || pegState.family, window: PEG_LC_WINDOW });
    } catch (e) {
        if (seq !== pegState.lcSeq) return;
        console.error('[peg lc-compare]', e);
        _pegSet('peg-lc-body', `<div class="peg-empty">The LC comparison is unavailable right now (${esc(e.message)}).</div>`);
        return;
    }
    if (seq !== pegState.lcSeq) return;
    const fams = Array.isArray(d.families) ? d.families : [];
    if (pegState.lcFamily == null) {
        // Open on a family that has both LC groups; failing that, the busiest.
        const pick = fams.find(f => (pegNum(f.evosep_runs) || 0) > 0 && (pegNum(f.other_runs) || 0) > 0) || fams[0];
        pegState.lcFamily = pick ? String(pick.family) : String(d.family || pegState.family);
        if (pick && !pegSameFamily(pick.family, d.family)) return loadPegLc();
    }
    pegState.families = fams;
    try { _pegSet('peg-lcfam', pegLcFamilyChipsHtml(fams, pegState.lcFamily)); }
    catch (e) { console.error('[peg lc families]', e); }
    try {
        _pegSet('peg-lc-body', fams.length ? pegLcHtml(d)
            : `<div class="peg-empty">No lab has shared PEG in the last ${PEG_LC_WINDOW} days yet.</div>`);
    } catch (e) {
        console.error('[peg lc]', e);
        _pegSet('peg-lc-body', '<div class="peg-empty">Could not draw the LC comparison.</div>');
    }
}

function _pegPress(group, button) {
    for (const b of group.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b === button));
}

// Deep link #peg: the benchmark charts above render after their own fetch
// and push this section a few thousand pixels down. Browsers with scroll
// anchoring keep the reader in place; the rest would land mid-page. So
// re-align once each loader settles, unless the reader has moved already.
let _pegReaderMoved = false;
if (typeof window !== 'undefined' && window.addEventListener) {
    ['wheel', 'touchmove', 'keydown', 'mousedown'].forEach(ev =>
        window.addEventListener(ev, () => { _pegReaderMoved = true; }, { passive: true, once: true }));
}
function _pegHonourHash() {
    if (_pegReaderMoved || location.hash !== '#peg') return;
    const el = document.getElementById('peg');
    if (el) el.scrollIntoView({ block: 'start' });
}

function loadPegWatch() {
    const coh = document.getElementById('peg-coh');
    const win = document.getElementById('peg-win');
    const lcf = document.getElementById('peg-lcfam');
    if (coh) coh.addEventListener('click', e => {
        const b = e.target.closest('button[data-i]');
        const c = b && pegState.cohorts[+b.dataset.i];
        if (!c) return;
        pegState.family = String(c.family);
        pegState.spd = pegNum(c.spd) || 100;
        _pegPress(coh, b);
        loadPegBoard().catch(err => console.error('[peg]', err));
        loadPegTrend().catch(err => console.error('[peg]', err));
    });
    if (win) win.addEventListener('click', e => {
        const b = e.target.closest('button[data-win]');
        if (!b) return;
        pegState.window = +b.dataset.win;
        _pegPress(win, b);
        loadPegBoard().catch(err => console.error('[peg]', err));
    });
    if (lcf) lcf.addEventListener('click', e => {
        const b = e.target.closest('button[data-i]');
        const f = b && pegState.families[+b.dataset.i];
        if (!f) return;
        pegState.lcFamily = String(f.family);
        _pegPress(lcf, b);
        loadPegLc().catch(err => console.error('[peg]', err));
    });
    // The trend follows whichever cohort the board settles on.
    const board = loadPegBoard().then(loadPegTrend).catch(err => console.error('[peg]', err));
    const lc = loadPegLc().catch(err => console.error('[peg]', err));
    Promise.all([board, lc]).then(_pegHonourHash);
    if (window.stanMainLoad) window.stanMainLoad.then(_pegHonourHash, _pegHonourHash);
}

if (typeof document !== 'undefined' && document.getElementById && document.getElementById('peg')) {
    try { loadPegWatch(); } catch (e) { console.error('[peg]', e); }
}
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
