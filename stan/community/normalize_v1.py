"""v1.0 schema normalization for community benchmark submissions.

Shared between the one-shot migration (`scripts/migrate_v1.py`) and the
nightly consolidator (`scripts/consolidate.py`) so historical and new
submissions are normalized identically.

The original submission parquets in `submissions/<id>.parquet` are NEVER
modified — we treat them as the immutable audit trail. Normalization is
applied to the concatenated DataFrame before publishing
`benchmark_latest.parquet`.

v1.0 invariants enforced here:
  - acquisition_mode is lowercase
  - schema_version is stamped on every row ("1.0" or "pre-1.0")
  - cohort_id is the broad form (family_spd_amount, no column suffix)
  - sample_type is populated (default "hela")
  - run_date is populated (inferred from submitted_at if missing,
    flagged with run_date_inferred=True)
  - assets_verified flag reflects whether fasta/speclib hashes are present
  - hard-fail rows (ips_score=0 AND spd=0) are split into quarantine
"""

from __future__ import annotations

import logging

import polars as pl

from stan.metrics.scoring import compute_broad_cohort_id

logger = logging.getLogger(__name__)

_SAMPLE_TYPE_PATTERNS: list[tuple[str, str]] = [
    ("k562", "k562"),
    ("hek293", "hek293"),
    ("hek-293", "hek293"),
    ("hek_293", "hek293"),
    ("jurkat", "jurkat"),
    ("a549", "a549"),
    ("u2os", "u2os"),
    ("mcf7", "mcf7"),
    ("nih3t3", "nih3t3"),
    ("yeast", "yeast"),
    ("sc_", "yeast"),
    ("ecoli", "ecoli"),
    ("e.coli", "ecoli"),
    ("e_coli", "ecoli"),
]


def detect_sample_type(run_name: str | None) -> str:
    """Infer sample type from run filename. Matches submit.py:_detect_sample_type."""
    if not run_name:
        return "hela"
    name = run_name.lower()
    for pattern, st in _SAMPLE_TYPE_PATTERNS:
        if pattern in name:
            return st
    return "hela"


def _is_v1_compliant_row(row: dict) -> bool:
    """A row is v1.0 compliant when it carries BOTH:
      - a schema_version stamp starting with "v1" / "1."
      - assets_verified=True (computed earlier in normalize())

    The forward path: submit.py stamps schema_version=SEARCH_PARAMS_VERSION
    ("v1.0.0") AND wires fasta_md5/speclib_md5 into the payload at the
    same time. Both must land for a row to count as v1.

    Historical submissions stamped neither — they fall through to
    "pre-1.0" regardless of how high their stan_version is.
    """
    sv = row.get("schema_version") or ""
    if not (sv.startswith("v1") or sv.startswith("1.")):
        return False
    return bool(row.get("assets_verified"))


def normalize(df: pl.DataFrame) -> dict[str, pl.DataFrame]:
    """Normalize a concatenated submissions DataFrame into v1.0 schema.

    Returns three DataFrames:
      - "v1": passes v1.0 schema (publish to benchmark_latest.parquet)
      - "historical": valid but pre-1.0 schema
        (publish to benchmark_historical.parquet, used as reference but
        excluded from the official v1.0 leaderboard)
      - "quarantine": hard-fails (ips=0 AND spd=0, missing cohort_id, etc.)
        (publish to benchmark_quarantine.parquet for audit only)
    """
    if df.height == 0:
        return {"v1": df, "historical": df, "quarantine": df}

    work = df.clone()

    # ── Ensure expected columns exist (NULL-fill if absent) ───────────
    for col, dtype in [
        ("schema_version", pl.Utf8),
        ("sample_type", pl.Utf8),
        ("run_date", pl.Utf8),
        ("run_date_inferred", pl.Boolean),
        ("assets_verified", pl.Boolean),
        ("fasta_md5", pl.Utf8),
        ("speclib_md5", pl.Utf8),
        ("diann_version", pl.Utf8),
        ("acquisition_mode", pl.Utf8),
        ("cohort_id", pl.Utf8),
        ("run_name", pl.Utf8),
        ("submitted_at", pl.Utf8),
        ("ips_score", pl.Int64),
        ("spd", pl.Int64),
    ]:
        if col not in work.columns:
            work = work.with_columns(pl.lit(None, dtype=dtype).alias(col))

    # ── 1. Lowercase acquisition_mode ─────────────────────────────────
    work = work.with_columns(
        pl.col("acquisition_mode").str.to_lowercase().alias("acquisition_mode")
    )

    # ── 2. Collapse cohort_id to broad form (drop column suffix) ──────
    cohort_ids = work["cohort_id"].to_list()
    broad = [compute_broad_cohort_id(c) if c else c for c in cohort_ids]
    work = work.with_columns(pl.Series("cohort_id", broad, dtype=pl.Utf8))

    # ── 3. Backfill sample_type from run_name (default hela) ──────────
    sample_types = work["sample_type"].to_list()
    run_names = work["run_name"].to_list()
    filled = [
        st if st else detect_sample_type(rn)
        for st, rn in zip(sample_types, run_names)
    ]
    work = work.with_columns(pl.Series("sample_type", filled, dtype=pl.Utf8))

    # ── 4. Backfill run_date from submitted_at; mark inferred ─────────
    # submitted_at varies across historical parquets (Utf8 in newer ones,
    # Datetime('us', 'UTC') in older ones). Cast everything to ISO-8601
    # string before merging.
    run_dates = work["run_date"].cast(pl.Utf8, strict=False).to_list()
    submitted_at = work["submitted_at"].cast(pl.Utf8, strict=False).to_list()
    inferred_flags = work["run_date_inferred"].to_list()
    new_dates = []
    new_flags = []
    for rd, sa, flag in zip(run_dates, submitted_at, inferred_flags):
        if rd:
            new_dates.append(rd)
            new_flags.append(bool(flag))
        else:
            new_dates.append(sa or "")
            new_flags.append(True)
    work = work.with_columns(
        pl.Series("run_date", new_dates, dtype=pl.Utf8),
        pl.Series("run_date_inferred", new_flags, dtype=pl.Boolean),
    )

    # ── 5. Compute assets_verified flag ───────────────────────────────
    fasta_hashes = work["fasta_md5"].to_list()
    speclib_hashes = work["speclib_md5"].to_list()
    modes = work["acquisition_mode"].to_list()
    verified = []
    for fasta, speclib, mode in zip(fasta_hashes, speclib_hashes, modes):
        ok = bool(fasta)
        if mode and "dia" in mode:
            ok = ok and bool(speclib)
        verified.append(ok)
    work = work.with_columns(
        pl.Series("assets_verified", verified, dtype=pl.Boolean)
    )

    # ── 6. Stamp schema_version per row ───────────────────────────────
    # Preserve the original stamp ("v1.0.0", future "v1.0.1", etc.) when
    # the row is v1.0 compliant. Replace with "pre-1.0" otherwise.
    rows = work.to_dicts()
    schema_versions = []
    for row in rows:
        if _is_v1_compliant_row(row):
            schema_versions.append(row.get("schema_version") or "v1.0.0")
        else:
            schema_versions.append("pre-1.0")
    work = work.with_columns(
        pl.Series("schema_version", schema_versions, dtype=pl.Utf8)
    )

    # ── 7. Split: v1 / historical / quarantine ────────────────────────
    quarantine = work.filter(
        (
            (pl.col("ips_score").is_null() | (pl.col("ips_score") == 0))
            & (pl.col("spd").is_null() | (pl.col("spd") == 0))
        )
        | pl.col("cohort_id").is_null()
        | (pl.col("cohort_id") == "")
    )
    keep = work.filter(
        ~(
            (
                (pl.col("ips_score").is_null() | (pl.col("ips_score") == 0))
                & (pl.col("spd").is_null() | (pl.col("spd") == 0))
            )
            | pl.col("cohort_id").is_null()
            | (pl.col("cohort_id") == "")
        )
    )
    v1 = keep.filter(pl.col("schema_version") != "pre-1.0")
    historical = keep.filter(pl.col("schema_version") == "pre-1.0")

    logger.info(
        "Normalized %d rows: v1=%d, historical=%d, quarantine=%d",
        df.height, v1.height, historical.height, quarantine.height,
    )

    return {"v1": v1, "historical": historical, "quarantine": quarantine}
