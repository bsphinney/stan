"""Submit QC metrics to the community HeLa benchmark via the STAN relay.

Submissions are posted to the HF Space relay which handles HF Dataset
uploads server-side. No HF token is required on the client. Raw files
are NEVER uploaded — metrics only.
"""

from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error

from stan import __version__
from stan.community.fingerprint_dedup import compute_submission_fingerprint
from stan.community.validate import validate_submission
from stan.config import load_community
from stan.db import mark_submitted
from stan.metrics.scoring import compute_cohort_id
from stan.search.community_params import check_diann_version_compatible

logger = logging.getLogger(__name__)

RELAY_URL = "https://brettsp-stan.hf.space"


def _detect_sample_type(run_name: str) -> str:
    """Detect QC standard from the run filename.

    Returns a short identifier for the cell line / digest used as the QC
    standard.  Default is "hela" (most common).  This drives cohort
    separation so that K562, yeast, etc. are never compared against HeLa.
    """
    name_lower = run_name.lower()
    if "k562" in name_lower:
        return "k562"
    if "yeast" in name_lower or "sc_" in name_lower:
        return "yeast"
    if "ecoli" in name_lower or "e.coli" in name_lower or "e_coli" in name_lower:
        return "ecoli"
    if "hek293" in name_lower or "hek-293" in name_lower or "hek_293" in name_lower:
        return "hek293"
    # Default: HeLa (most common QC standard)
    return "hela"


def submit_to_benchmark(
    run: dict,
    spd: int | None = None,
    gradient_length_min: int | None = None,
    amount_ng: float = 50.0,
    hela_source: str = "Pierce HeLa Protein Digest Standard",
    asset_hashes: dict[str, str] | None = None,
    diann_version: str | None = None,
) -> dict:
    """Submit a QC run to the community benchmark.

    Args:
        run: Run dict from the local SQLite database.
        spd: Samples per day (primary throughput — e.g. 60 for Evosep 60 SPD).
            If not provided, falls back to gradient_length_min.
        gradient_length_min: Gradient length in minutes (fallback for custom LC).
        amount_ng: Amount of HeLa digest injected (ng).
        hela_source: HeLa digest source/vendor.
        asset_hashes: MD5 hashes of the speclib and FASTA used in the search.
            Keys: "fasta_md5", "speclib_md5". Used to verify the correct
            community library was used.

    Returns:
        Dict with submission_id and status.

    Raises:
        ValueError: If submission fails validation.
    """
    try:
        community_config = load_community()
    except Exception:
        community_config = {}

    display_name = community_config.get("display_name", "") or "Anonymous Lab"
    mode = run.get("mode", "").lower()

    # Validate (including library hash verification)
    from stan.search.community_params import SEARCH_PARAMS_VERSION

    validation = validate_submission(
        run, mode,
        search_params_version=SEARCH_PARAMS_VERSION,
        asset_hashes=asset_hashes,
    )
    if not validation.is_valid:
        # Include BOTH rejected_gates and errors in the message.
        # rejected_gates = threshold failures (n_proteins<1500, etc.)
        # errors = "Missing required metric: foo" from extractor gaps
        # Previously only gates were shown — runs with missing metrics
        # produced empty "Submission rejected: " messages and were
        # impossible to diagnose from the submit-all log.
        parts = list(validation.rejected_gates) + list(validation.errors)
        reason = "; ".join(parts) if parts else "no reason reported"
        raise ValueError(f"Submission rejected: {reason}")

    # Version check — community benchmark requires the pinned DIA-NN version
    # because different versions produce non-comparable results
    if diann_version is None:
        # Try to detect from run metadata or system
        from stan.search.version_detect import detect_diann_version
        diann_version = run.get("diann_version") or detect_diann_version() or "unknown"

    is_compat, msg = check_diann_version_compatible(diann_version)
    if not is_compat:
        raise ValueError(f"Submission rejected: {msg}")

    # Build submission
    # Coerce None → "" for every string field the relay expects. SQLite
    # NULLs come back as None, and `.get(key, default)` only returns the
    # default when the key is absent, not when the value is None. Without
    # this, runs with a NULL column_vendor/column_model (pre-parser runs)
    # are rejected by the relay Pydantic validator with 422 errors.
    instrument = run.get("instrument") or ""
    instrument_family = _instrument_family(instrument)

    column_model = run.get("column_model") or ""
    sample_type = run.get("sample_type") or _detect_sample_type(run.get("run_name") or "")
    # NOTE: sample_type is sent to the relay as its own payload field (see
    # below) but is intentionally NOT passed to compute_cohort_id() — the
    # installed scoring.py on in-field instrument PCs may be stale (v<=0.2.109
    # signature, no sample_type kwarg). Hotfix: keep the call compatible with
    # both old and new bytecode. Re-enable once all PCs have updated.
    # v1.0: column_model is preserved as its own payload field but is NOT
    # folded into cohort_id anymore. Free-form column strings ("10cm x 150um,
    # 1.5um c18 (evosep)") fragmented historical cohorts — see migrate_v1.py.
    # Drilldown by column happens client-side via the column_model column.
    cohort_id = compute_cohort_id(
        instrument_family, amount_ng, spd=spd, gradient_min=gradient_length_min,
    )

    # Compute fingerprint for dedup — same (lab, instrument, run_name, amount, spd)
    # will always produce the same fingerprint so resubmissions are detectable
    fingerprint = compute_submission_fingerprint(
        display_name=display_name,
        instrument_model=instrument,
        run_name=run.get("run_name", ""),
        amount_ng=amount_ng,
        spd=spd,
    )

    # Build payload matching the relay's BenchmarkSubmission schema
    # The relay generates submission_id, submitted_at, community_score, is_flagged
    submit_payload = {
        "stan_version": __version__,
        "schema_version": SEARCH_PARAMS_VERSION,
        "display_name": display_name,
        "instrument_family": instrument_family,
        "instrument_model": instrument,
        # Always lowercase — historical mix of "DIA" / "dia" fragmented cohorts
        "acquisition_mode": (run.get("mode") or "").lower(),
        "spd": spd or 0,
        "gradient_length_min": gradient_length_min or 0,
        "amount_ng": amount_ng,
        "n_precursors": run.get("n_precursors") or 0,
        # n_peptides is the relay's single peptide count for both
        # modes. DIA fills it via extract_dia_metrics. DDA fills it
        # via extract_dda_metrics' n_peptides_dda key — fall back so
        # DDA submissions don't ship 0 peptides.
        "n_peptides": run.get("n_peptides") or run.get("n_peptides_dda") or 0,
        "n_proteins": run.get("n_proteins") or 0,
        "n_psms": run.get("n_psms") or 0,
        "median_cv_precursor": run.get("median_cv_precursor") or 0.0,
        "median_fragments_per_precursor": run.get("median_fragments_per_precursor") or 0.0,
        "ips_score": run.get("ips_score") or 0,
        "missed_cleavage_rate": run.get("missed_cleavage_rate") or 0.0,
        "cohort_id": cohort_id,
        "sample_type": sample_type,
        "fingerprint": fingerprint,
        "diann_version": diann_version or "",
        "column_vendor": run.get("column_vendor") or "",
        "column_model": column_model,
        "lc_system": run.get("lc_system") or "",
        # Original acquisition date (not submission date)
        "run_name": run.get("run_name") or "",
        "run_date": run.get("run_date") or "",
        # Stats from DIA-NN report.stats.tsv
        "ms1_signal": run.get("ms1_signal"),
        "ms2_signal": run.get("ms2_signal"),
        "fwhm_rt_min": run.get("fwhm_rt_min"),
        "median_mass_acc_ms1_ppm": run.get("median_mass_acc_ms1_ppm"),
        "median_mass_acc_ms2_ppm": run.get("median_mass_acc_ms2_ppm"),
        "peak_capacity": run.get("peak_capacity"),
        "dynamic_range_log10": run.get("dynamic_range_log10"),
        # Matthews & Hayes 1976 quantitation-quality metric — was in
        # the relay schema but never shipped by the client, leaving
        # the dashboard's Throughput vs Quantitation Quality plot
        # empty even after every other field landed.
        "median_points_across_peak": run.get("median_points_across_peak"),
        "median_peak_width_sec": run.get("median_peak_width_sec"),
    }

    # Add identified TIC trace if available (128 bins, ~500 bytes)
    tic_rt = run.get("tic_rt_bins")
    tic_int = run.get("tic_intensity")
    if tic_rt and tic_int:
        submit_payload["tic_rt_bins"] = tic_rt
        submit_payload["tic_intensity"] = tic_int

    # Library saturation warning — if n_precursors approaches the
    # community library size, the library is the bottleneck, not the
    # instrument. Flag the row with library_coverage_pct so the
    # dashboard + post-1.0 expansion logic can surface saturation
    # patterns. Warn on the client too so the operator sees it.
    from stan.search.community_params import (
        COMMUNITY_LIBRARY_PRECURSOR_COUNT,
        SATURATION_THRESHOLD_PCT,
    )

    vendor_for_lib = (run.get("vendor") or "").lower()
    if "bruker" in vendor_for_lib or instrument_family == "timsTOF":
        lib_size = COMMUNITY_LIBRARY_PRECURSOR_COUNT.get("bruker", 0)
    else:
        lib_size = COMMUNITY_LIBRARY_PRECURSOR_COUNT.get("thermo", 0)
    n_prec = run.get("n_precursors") or 0
    if lib_size and n_prec:
        coverage_pct = round(n_prec / lib_size * 100, 1)
        submit_payload["library_coverage_pct"] = coverage_pct
        if coverage_pct >= SATURATION_THRESHOLD_PCT * 100:
            logger.warning(
                "Library saturation: run %s identified %d/%d precursors "
                "(%.1f%% of community library). Consider expanding the "
                "community library — see post-1.0 brainstorm.",
                run.get("run_name", "?"), n_prec, lib_size, coverage_pct,
            )

    # Library/FASTA hashes — the validator computed these but the original
    # payload silently dropped them, leaving 100% of historical rows
    # unverifiable. Wire them through so the v1.0 normalizer can stamp
    # assets_verified=True on every new submission.
    #
    # If the caller did not provide explicit hashes, fall back to the
    # canonical EXPECTED_ASSET_HASHES from validate.py. This is safe
    # because the version-pinned DIA-NN check above (≥2.3.x) is
    # already the gating condition: a row that reaches this point is
    # known to have been searched with the frozen tool version, so
    # the corresponding frozen FASTA + speclib hashes apply
    # deterministically. submit-all flows that pull rows from a local
    # stan.db produced by the community-search pipeline don't need to
    # recompute hashes — they're constants.
    from stan.community.validate import EXPECTED_ASSET_HASHES

    fasta_md5 = (asset_hashes or {}).get("fasta_md5") or EXPECTED_ASSET_HASHES.get(
        "human_hela_202604.fasta", ""
    )
    if fasta_md5:
        submit_payload["fasta_md5"] = fasta_md5

    speclib_md5 = (asset_hashes or {}).get("speclib_md5")
    if not speclib_md5:
        # Vendor-aware fallback to the right frozen library hash.
        vendor = (run.get("vendor") or "").lower()
        if "bruker" in vendor or instrument_family == "timsTOF":
            speclib_md5 = EXPECTED_ASSET_HASHES.get(
                "hela_timstof_202604.parquet", ""
            )
        else:
            speclib_md5 = EXPECTED_ASSET_HASHES.get(
                "hela_orbitrap_202604.parquet", ""
            )
    # DDA submissions don't use a speclib, so leave the field unset
    # for non-DIA runs.
    if speclib_md5 and "dia" in (run.get("mode") or "").lower():
        submit_payload["speclib_md5"] = speclib_md5

    # Send the auth_token from community.yml so the relay can verify
    # this is an official STAN installation that went through email
    # verification. Forks that skip `stan setup` won't have a token.
    auth_token = community_config.get("auth_token", "")

    try:
        data = json.dumps(submit_payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"STAN/{__version__}",
        }
        if auth_token:
            headers["X-STAN-Auth"] = auth_token
        req = urllib.request.Request(
            f"{RELAY_URL}/api/submit",
            data=data,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            if result.get("status") != "accepted":
                raise RuntimeError(
                    f"Relay rejected submission: {result.get('detail', result.get('error', 'unknown'))}"
                )
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error("Relay HTTP %s: %s", e.code, body)
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        raise RuntimeError(f"Community relay rejected submission: {detail}") from e
    except urllib.error.URLError as e:
        logger.error("Failed to reach relay: %s", e)
        raise RuntimeError(f"Could not reach community relay: {e}") from e

    # Get submission_id from relay response
    submission_id = result.get("submission_id", "")

    # Mark as submitted in local DB
    run_id = run.get("id", "")
    if run_id and submission_id:
        try:
            mark_submitted(run_id, submission_id)
        except Exception:
            logger.exception("Failed to mark run as submitted locally")

    logger.info(
        "Submitted to community benchmark: %s (cohort: %s)", submission_id[:8], cohort_id
    )

    return {
        "submission_id": submission_id,
        "cohort_id": result.get("cohort_id", cohort_id),
        "is_flagged": len(validation.flags) > 0,
        "flags": validation.flags,
        "status": "submitted",
    }


def _instrument_family(model: str) -> str:
    """Map instrument model name to family for cohort bucketing.

    Returns the broad instrument class, not the full model variant.
    This is the single authoritative source for the family string —
    both baseline.py and submit_to_benchmark() call through here so
    cohort_ids, dashboard scatter colors, and community TIC overlay
    groupings stay consistent.
    """
    model_lower = model.lower()
    if "timstof" in model_lower or "tims tof" in model_lower:
        return "timsTOF"
    if "astral" in model_lower:
        return "Astral"
    if "exploris" in model_lower:
        return "Exploris"
    if "lumos" in model_lower or "fusion" in model_lower:
        return "Lumos"
    if "eclipse" in model_lower:
        return "Eclipse"
    if "orbitrap" in model_lower:
        return "Orbitrap"
    return model
