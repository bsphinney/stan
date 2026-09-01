"""Hive-side single-shot QC pipeline.

Runs the entire STAN pipeline against ONE raw file on Quobyte:

    detect mode → run DIA-NN/Sage → extract metrics → resolve SPD/LC/MS2
    → IPS → evaluate gates → insert_run → PEG/drift → TIC/BPC → 4DFF
    → HOLD flag (on FAIL)

Designed to be the body of a single SLURM job. The Hive cron dispatcher
(`stan/community/scripts/dispatch_hive.py`) calls this for any raw not yet
in the global Hive-resident DB.

The instrument-PC watcher's `_store_run` is the canonical client-side
mirror of this orchestration. Both paths share leaf modules — extractor,
features, tic, db, gating — but orchestrate independently for now so the
production watcher stays untouched while the Hive path bakes.

Idempotent: re-running against a raw whose row already exists short-
circuits unless `force=True`. Search artifacts under `out_dir` are reused
when present, so re-running a processed raw is cheap.
"""

from __future__ import annotations

import logging
import sqlite3

from stan.db import connect as _db_connect
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _resolve_vendor(raw_path: Path) -> str:
    """Infer vendor from raw-file shape. .d directory → bruker, .raw → thermo."""
    if raw_path.is_dir() and raw_path.suffix.lower() == ".d":
        return "bruker"
    if raw_path.is_file() and raw_path.suffix.lower() == ".raw":
        return "thermo"
    raise ValueError(
        f"Cannot infer vendor for {raw_path} — expected a Bruker .d directory "
        f"or a Thermo .raw file."
    )


def _row_exists(db_path: Path, instrument: str, raw_path: Path) -> str | None:
    """Return existing run_id if any row for this (instrument, run_name,
    raw_path) is in the DB. Used for cheap short-circuit on cron re-runs.

    Pre-fix this gated on ``ips_score IS NOT NULL`` to mean "complete".
    Problem: ``insert_run`` writes ``ips_score`` before PEG/drift/TIC/4DFF
    run, so a crash mid-pipeline leaves a row that looks complete and the
    cron skips it forever. The simpler, correct contract: any row at all
    means "search ran, primary metrics in DB". Re-running orphaned
    children is a job for ``stan repair-incomplete`` (separate command);
    re-running a full pipeline is a job for ``--force``. Don't conflate.

    PG mode: writes go only to PG, so the SQLite ``runs`` table never sees
    completions — consult PG instead of the write-only-bypassed SQLite.
    """
    from stan.db_pg import use_pg, raw_run_id_pg
    if use_pg():
        return raw_run_id_pg(raw_path)
    try:
        with _db_connect(db_path) as con:
            row = con.execute(
                "SELECT id FROM runs WHERE instrument = ? AND run_name = ? "
                "AND raw_path = ? LIMIT 1",
                (instrument, raw_path.name, str(raw_path)),
            ).fetchone()
            return str(row[0]) if row else None
    except sqlite3.OperationalError:
        return None  # fresh DB, table not yet created


def _detect_mode_str(raw_path: Path, vendor: str, forced_mode: str) -> tuple[str, "object"]:
    """Resolve mode as ("dia"|"dda", AcquisitionMode).

    Honors `forced_mode` when set; otherwise reads from raw metadata via
    the existing detector. UNKNOWN falls back to DIA per-vendor (matches
    daemon.py:1037-1045 — DIA is the dominant QC mode and DIA-NN search
    will fail loudly on a real DDA file).
    """
    from stan.watcher.detector import (
        AcquisitionMode, detect_mode, is_dia,
    )

    if forced_mode:
        fm = forced_mode.lower()
        if fm == "dia":
            return "dia", (
                AcquisitionMode.DIA_PASEF if vendor == "bruker"
                else AcquisitionMode.DIA_ORBITRAP
            )
        if fm == "dda":
            return "dda", (
                AcquisitionMode.DDA_PASEF if vendor == "bruker"
                else AcquisitionMode.DDA_ORBITRAP
            )
        raise ValueError(f"forced_mode must be 'dia' or 'dda', got {forced_mode!r}")

    detected = detect_mode(raw_path, vendor)
    if detected == AcquisitionMode.UNKNOWN:
        # daemon.py default: DIA per-vendor.
        logger.warning(
            "Mode detection returned UNKNOWN for %s — defaulting to DIA",
            raw_path.name,
        )
        detected = (
            AcquisitionMode.DIA_PASEF if vendor == "bruker"
            else AcquisitionMode.DIA_ORBITRAP
        )
    return ("dia" if is_dia(detected) else "dda"), detected


def _run_search(
    raw_path: Path,
    out_dir: Path,
    mode_str: str,
    vendor: str,
    family: str,
) -> Path | None:
    """Dispatch to DIA-NN or Sage via the production-tested run_one_v1
    runners. Both functions are bug-hardened for the `--`-in-path DIA-NN
    issue, FASTA download race, and Thermo→mzML routing.
    """
    from stan.community.scripts.run_one_v1 import run_diann, run_sage

    out_dir.mkdir(parents=True, exist_ok=True)
    if mode_str == "dia":
        return run_diann(raw_path, out_dir, vendor, family)
    return run_sage(raw_path, out_dir, vendor)


def _extract_metrics(
    report_path: Path,
    raw_path: Path,
    vendor: str,
    mode_str: str,
    family: str,
    spd: int | None,
    gradient_min: int | None,
    column_vendor: str,
    column_model: str,
) -> dict:
    """Run DIA or DDA extractor, then enrich with SPD, LC, MS2 analyzer,
    column metadata, and IPS score. Mirrors daemon._store_run:1395-1458.
    """
    from stan.metrics.extractor import extract_dda_metrics, extract_dia_metrics

    if mode_str == "dia":
        # IMPORTANT: pass Path, not str. extract_dia_metrics walks
        # report.parquet's sibling report.stats.tsv via
        # report_path.with_name(...) — that's a Path method. Passing
        # a str crashes the median_points_across_peak branch with
        # "AttributeError: 'str' object has no attribute 'with_name'"
        # (Exploris timing test 2026-05-07).
        metrics = extract_dia_metrics(
            report_path,
            raw_path=raw_path,
            vendor=vendor,
            gradient_min=gradient_min,
        )
        from stan.metrics.chromatography import compute_ips_dia
        metrics["instrument_family"] = family
        metrics["spd"] = spd
        metrics["gradient_length_min"] = gradient_min
        metrics["ips_score"] = compute_ips_dia(metrics)
    else:
        metrics = extract_dda_metrics(
            report_path,
            gradient_min=gradient_min or 60,
        )
        from stan.metrics.chromatography import compute_ips_dda
        metrics["instrument_family"] = family
        metrics["spd"] = spd
        metrics["gradient_length_min"] = gradient_min
        metrics["ips_score"] = compute_ips_dda(metrics)

    # LC system from raw metadata.
    try:
        from stan.metrics.scoring import detect_lc_system
        lc_sys = detect_lc_system(raw_path)
        if lc_sys:
            metrics["lc_system"] = lc_sys
    except Exception:
        logger.debug("LC system detect failed for %s", raw_path.name, exc_info=True)

    # MS2 analyzer (OT/IT for Thermo, "tof" for Bruker).
    try:
        if vendor == "thermo":
            from stan.tools.trfp import detect_ms2_analyzer
            metrics["ms2_analyzer"] = detect_ms2_analyzer(raw_path)
        elif vendor == "bruker":
            metrics["ms2_analyzer"] = "tof"
    except Exception:
        logger.debug("ms2_analyzer detect failed for %s", raw_path.name, exc_info=True)

    if column_vendor:
        metrics["column_vendor"] = column_vendor
    if column_model:
        metrics["column_model"] = column_model

    # Search-engine provenance.
    if mode_str == "dia":
        metrics["search_engine"] = "diann"
        metrics.setdefault("diann_version", "2.3.0")
    else:
        metrics["search_engine"] = "sage"

    return metrics


def _resolve_spd_chain(
    raw_path: Path,
    config_spd: int | None,
    report_path: Path | None,
) -> int | None:
    """Layered SPD resolve matching daemon._resolve_spd + run_one_v1
    fallbacks. Documented in CLAUDE.md.

    1. Bruker XML / TDF / Thermo metadata via validate_spd_from_metadata
    2. Cohort default from CLI arg (instruments.yml on the instrument PC)
    3. Filename regex
    4. RT span from DIA-NN report (if available)
    """
    try:
        from stan.metrics.scoring import validate_spd_from_metadata
        spd = validate_spd_from_metadata(raw_path)
        if spd is not None:
            return spd
    except Exception:
        logger.debug("validate_spd_from_metadata failed", exc_info=True)

    if config_spd:
        return config_spd

    # Filename regex (matches run_one_v1._resolve_spd_from_name).
    import re
    name = raw_path.name.lower()
    m = re.search(r"(\d+)\s*spd\b", name)
    if not m:
        m = re.search(r"_(\d+)spd_", name)
    if m:
        return int(m.group(1))

    # RT span from search report.
    if report_path is not None and report_path.exists():
        try:
            import polars as pl
            from stan.metrics.scoring import gradient_min_to_spd
            df = pl.read_parquet(report_path, columns=["RT"])
            if df.height > 0:
                grad = float(df["RT"].max()) - float(df["RT"].min())
                if grad > 0:
                    return gradient_min_to_spd(grad)
        except Exception:
            logger.debug("RT-span SPD fallback failed", exc_info=True)

    return None


def _gradient_for_spd(spd: int | None, config_grad: int | None) -> int | None:
    """Snap SPD → typical Evosep gradient minutes. daemon._store_run table."""
    if config_grad:
        return config_grad
    if not spd:
        return None
    _SPD_TO_GRAD = {200: 6, 100: 11, 60: 21, 40: 30, 30: 44, 15: 88}
    for s, g in _SPD_TO_GRAD.items():
        if spd >= s:
            return g
    return None


def _run_peg_and_drift(
    raw_path: Path,
    run_id: str,
    db_path: Path,
) -> None:
    """Compute PEG + DIA window drift and persist to the runs row.

    Direct port of InstrumentWatcher._run_peg_and_drift — duplicated
    here so the production watcher stays untouched. Best-effort: any
    failure is logged at DEBUG and the pipeline continues.
    """
    try:
        from stan.metrics.peg import detect_peg_in_spectra
        from stan.metrics.peg_io import read_ms1_any, PegReaderUnavailable
        from stan.metrics.window_drift import detect_window_drift
        from stan.db import (
            update_peg_result, update_drift_result,
            insert_peg_ion_hits, insert_drift_window_centroids,
            insert_drift_peak_cloud,
        )
    except Exception:
        logger.debug("PEG/drift imports failed", exc_info=True)
        return

    is_bruker = raw_path.is_dir() and raw_path.suffix == ".d"

    peg_reader_available = True
    try:
        spectra = list(read_ms1_any(raw_path))
        peg = detect_peg_in_spectra(spectra)
        if not update_peg_result(
            run_id=run_id,
            peg_score=peg.peg_score,
            peg_n_ions_detected=peg.n_ions_detected,
            peg_intensity_pct=peg.intensity_pct,
            peg_class=peg.peg_class,
            db_path=db_path,
            table="runs",
        ):
            # Zero rows matched means the PEG compute was thrown away.
            # This failed silently for months once PG became the store of
            # record — never let it be quiet again.
            logger.warning(
                "PEG result NOT persisted (no runs row id=%s) for %s",
                run_id, raw_path.name,
            )
        try:
            insert_peg_ion_hits(
                run_id=run_id, matches=peg.matches,
                db_path=db_path, table="runs",
            )
        except Exception:
            logger.debug("PEG breakdown write failed", exc_info=True)
    except PegReaderUnavailable as e:
        # Don't name alphatims specifically — Thermo .raw reaches this via
        # a missing fisher_py, and the wrong library name sends debugging
        # after a Bruker dependency that was installed all along.
        logger.warning(
            "MS1 reader unavailable — PEG + drift skipped for %s (%s)",
            raw_path.name, e,
        )
        peg_reader_available = False
        # Deliberately leave peg_* NULL rather than stamping peg_score=0.0
        # / peg_class="unknown". A run we could not measure is not a clean
        # run: 0.0 reads as a real "no PEG detected" result downstream,
        # whereas NULL renders "—" and keeps coverage stats honest.
    except Exception as e:
        logger.warning("PEG detect failed for %s (%s)", raw_path.name, e)
        try:
            update_peg_result(
                run_id=run_id, peg_score=0.0,
                peg_n_ions_detected=0, peg_intensity_pct=0.0,
                peg_class="unknown", db_path=db_path, table="runs",
            )
        except Exception:
            pass

    if not peg_reader_available or not is_bruker:
        return

    try:
        drift = detect_window_drift(raw_path)
        if not update_drift_result(
            run_id=run_id,
            drift_coverage=drift.global_coverage,
            drift_median_im=drift.median_drift_im,
            drift_p90_abs_im=drift.p90_abs_drift_im,
            drift_class=drift.drift_class,
            db_path=db_path,
            table="runs",
        ):
            logger.warning(
                "drift result NOT persisted (no runs row id=%s) for %s",
                run_id, raw_path.name,
            )
        if drift.drift_class != "unknown":
            try:
                insert_drift_window_centroids(
                    run_id=run_id, per_window=drift.per_window,
                    db_path=db_path, table="runs",
                )
            except Exception:
                logger.debug("drift breakdown write failed", exc_info=True)
            try:
                if drift.cloud_mz:
                    insert_drift_peak_cloud(
                        run_id=run_id,
                        mz=drift.cloud_mz, im=drift.cloud_im,
                        log_intensity=drift.cloud_log_intensity,
                        db_path=db_path, table="runs",
                    )
            except Exception:
                logger.debug("drift cloud write failed", exc_info=True)
    except Exception:
        logger.debug("drift detection failed", exc_info=True)


def _persist_tic(raw_path: Path, run_id: str, db_path: Path) -> None:
    """Extract TIC + BPC and write to tic_traces. Best-effort."""
    try:
        from stan.metrics.tic import (
            extract_tic_bruker, extract_tic_thermo, downsample_trace,
        )
        from stan.db import insert_tic_trace
        trace = None
        if raw_path.is_dir() and raw_path.suffix.lower() == ".d":
            trace = extract_tic_bruker(raw_path)
        elif raw_path.is_file() and raw_path.suffix.lower() == ".raw":
            trace = extract_tic_thermo(raw_path)
        if trace and trace.rt_min and trace.intensity:
            trace = downsample_trace(trace, n_bins=128)
            insert_tic_trace(
                run_id, trace.rt_min, trace.intensity,
                db_path=db_path, bp_intensity=trace.bp_intensity,
            )
    except Exception:
        logger.debug("TIC extract failed for %s", raw_path.name, exc_info=True)


def _run_4dff_inline(raw_path: Path, run_id: str = "", db_path: Path | None = None) -> None:
    """Generate the `.features` sidecar for a Bruker .d, then publish it.

    Two halves, and the second one matters as much as the first: writing
    the sidecar makes the ion cloud *possible*, storing it makes the
    cloud *visible*. The dashboard reads the DB, not this filesystem —
    it runs on a laptop while the raw data sits on the cluster — so a
    sidecar nobody extracted is a run that renders the empty legacy SVG.

    Skips Thermo, skips 4DFF when a sidecar already exists (but still
    publishes it), skips when the binary isn't installed.
    """
    if not (raw_path.is_dir() and raw_path.suffix.lower() == ".d"):
        return
    try:
        from stan.metrics.features import (
            find_features_file, is_4dff_installed, run_4dff,
        )
        if find_features_file(raw_path) is None:
            if not is_4dff_installed():
                logger.info(
                    "4DFF not installed; skipping features for %s", raw_path.name
                )
                return
            run_4dff(raw_path)
    except Exception:
        logger.warning("4DFF run failed for %s", raw_path.name, exc_info=True)
        return

    if run_id:
        from stan.metrics.feature_cloud import publish_feature_cloud
        n = publish_feature_cloud(raw_path, run_id, db_path=db_path)
        if n:
            logger.info("ion cloud stored: %d points for %s", n, raw_path.name)


QC_PATTERN = "(?i)(he(l[_\\-\\s]?[a5\\d]|[_\\-\\s]?\\d)|qc|std[_\\-\\s]?he)"


def _classify_raw(raw_name: str) -> str:
    """Decide whether a raw is QC ('qc') or sample/wash ('monitor').

    QC files match the same regex the watcher uses. Everything else
    (patient samples, blanks, washes) routes to the monitor pipeline
    so it lands in sample_health instead of polluting runs/.
    """
    import re as _re
    if _re.search(QC_PATTERN, raw_name):
        return "qc"
    return "monitor"


def process_raw(
    raw_path: Path,
    *,
    instrument: str,
    family: str,
    db_path: Path,
    out_dir: Path,
    vendor: str = "",
    forced_mode: str = "",
    column_vendor: str = "",
    column_model: str = "",
    hela_amount_ng: float = 50.0,
    spd: int | None = None,
    gradient_length_min: int | None = None,
    force: bool = False,
    classification: str = "auto",
) -> dict:
    """Run the full STAN QC pipeline against `raw_path` and write to
    the global Hive-resident DB at `db_path`.

    Args:
        raw_path: Bruker .d directory or Thermo .raw file. Must be
            already on Quobyte (or any path readable from this host).
        instrument: Canonical instrument model name, e.g. "timsTOF HT",
            "Orbitrap Fusion Lumos", "Orbitrap Exploris 480". Used as
            the runs.instrument key + threshold lookup.
        family: Instrument family for IPS cohort key — "timsTOF" |
            "Lumos" | "Exploris".
        db_path: Path to the global Hive-resident stan.db.
        out_dir: Directory where DIA-NN/Sage outputs land.
        vendor: "bruker" | "thermo". Auto-inferred from raw_path shape
            when empty.
        forced_mode: "dia" | "dda" to override mode detection.
        column_vendor, column_model: Stamped onto runs row for the
            dashboard Column Comparison panel.
        hela_amount_ng: Injection amount stamp (default 50 ng matches
            post-2020 lab convention).
        spd: Cohort default if metadata-resolve fails.
        gradient_length_min: Forced gradient length, otherwise snapped
            from SPD.
        force: Re-run search even when a completed row already exists.

    Returns a status dict with keys:
        status: "ok" | "skipped" | "search_failed" | "error"
        run_id: str | None
        mode: "dia" | "dda" | None
        gate_result: "pass" | "warn" | "fail" | None
        instrument, family, raw, error
    """
    from stan.db import init_db, insert_run, record_dispatch_attempt
    from stan.gating.evaluator import evaluate_gates
    from stan.watcher.acquisition_date import get_acquisition_date

    record: dict = {
        "status": "error",
        "run_id": None,
        "mode": None,
        "gate_result": None,
        "instrument": instrument,
        "family": family,
        "raw": str(raw_path),
    }

    try:
        if not raw_path.exists():
            raise FileNotFoundError(f"raw_path does not exist: {raw_path}")

        # Reject placeholder instrument names. The watcher has a
        # `_model_from_raw` fallback at daemon.py:1491-1495 — but on the
        # Hive path the dispatcher is the source of truth for cohort
        # keying; a placeholder here means a dispatcher bug we want to
        # surface, not paper over. Pre-fix would silently mis-cohort
        # rows under "auto" / "unknown" / "" and produce the dashboard's
        # "two cards per instrument" bug v0.2.229 fixed for the watcher.
        normalized = instrument.strip().lower()
        if not normalized or normalized in ("auto", "unknown"):
            raise ValueError(
                f"instrument must be a real model name, got {instrument!r}. "
                f"Pass e.g. 'timsTOF HT' / 'Orbitrap Fusion Lumos' / "
                f"'Orbitrap Exploris 480'."
            )

        if not vendor:
            vendor = _resolve_vendor(raw_path)

        # Ensure schema is current on whatever DB we were handed. Cheap
        # and idempotent — `init_db` only ALTERs missing columns.
        init_db(db_path)

        # Classification: QC files run the search/extract/IPS/gates
        # pipeline (this function's main body). Sample/wash files run
        # the monitor pipeline (step_monitor) — rawmeat metrics into
        # sample_health, no DIA-NN. Auto-classify by filename pattern
        # unless caller forces a mode.
        if classification == "auto":
            classification = _classify_raw(raw_path.name)
        record["classification"] = classification

        if classification == "monitor":
            from stan.pipeline.hive_steps import step_monitor
            mon = step_monitor(
                raw_path=raw_path,
                instrument=instrument,
                vendor=vendor,
                db_path=db_path,
            )
            record.update(
                status=mon["status"],
                run_id=mon.get("health_id"),  # health row id
                error=mon.get("error"),
                verdict=mon.get("verdict"),
                reasons=mon.get("reasons"),
                mode="monitor",
            )
            return record

        if not force:
            existing = _row_exists(db_path, instrument, raw_path)
            if existing:
                logger.info(
                    "Run %s already processed (id=%s); skipping",
                    raw_path.name, existing[:8],
                )
                # Bump the audit trail so a stuck-vs-healthy raw is
                # visible in dispatch_attempts. "skipped" is a documented
                # status (db.py:533).
                try:
                    record_dispatch_attempt(
                        raw_path=str(raw_path), status="skipped",
                        last_run_id=existing, db_path=db_path,
                    )
                except Exception:
                    logger.debug("dispatch_attempts(skipped) failed", exc_info=True)
                record.update(status="skipped", run_id=existing)
                return record

        mode_str, mode_enum = _detect_mode_str(raw_path, vendor, forced_mode)
        record["mode"] = mode_str

        report_path = _run_search(raw_path, out_dir, mode_str, vendor, family)
        if report_path is None:
            record_dispatch_attempt(
                raw_path=str(raw_path), status="failed",
                error="search returned None", db_path=db_path,
            )
            record.update(status="search_failed")
            return record

        resolved_spd = _resolve_spd_chain(raw_path, spd, report_path)
        resolved_grad = _gradient_for_spd(resolved_spd, gradient_length_min)

        metrics = _extract_metrics(
            report_path=report_path,
            raw_path=raw_path,
            vendor=vendor,
            mode_str=mode_str,
            family=family,
            spd=resolved_spd,
            gradient_min=resolved_grad,
            column_vendor=column_vendor,
            column_model=column_model,
        )

        # Surface "search ran but produced zero IDs" as a distinct status
        # so the dispatcher can route these to manual review instead of
        # retrying. Mirrors run_one_v1.extract_and_submit's empty-search
        # short-circuit (run_one_v1.py:534-563). Row still gets written
        # so the dashboard surfaces the degraded state, but the JSONL
        # status flags it for the operator.
        primary_count = (
            metrics.get("n_precursors") if mode_str == "dia"
            else metrics.get("n_psms")
        ) or 0
        record["primary_count"] = primary_count

        decision = evaluate_gates(
            metrics=metrics,
            instrument_model=instrument,
            acquisition_mode=mode_str,
        )

        acq_date = get_acquisition_date(raw_path)
        if not acq_date:
            try:
                acq_date = datetime.fromtimestamp(
                    raw_path.stat().st_mtime, tz=timezone.utc
                ).isoformat()
            except OSError:
                acq_date = None

        # Extract TIC inline so the PG runs row carries tic_rt_bins /
        # tic_intensity (v1 community submission requires both). The
        # SQLite-only _persist_tic call below writes the child table
        # for local dashboards; PG mode reads from the row itself.
        try:
            from stan.metrics.tic import (
                extract_tic_bruker, extract_tic_thermo, downsample_trace,
            )
            trace = None
            if raw_path.is_dir() and raw_path.suffix.lower() == ".d":
                trace = extract_tic_bruker(raw_path)
            elif raw_path.is_file() and raw_path.suffix.lower() == ".raw":
                trace = extract_tic_thermo(raw_path)
            if trace and trace.rt_min and trace.intensity:
                trace = downsample_trace(trace, n_bins=128)
                metrics["tic_rt_bins"] = [round(r, 3) for r in trace.rt_min]
                metrics["tic_intensity"] = [round(v, 0) for v in trace.intensity]
        except Exception:
            logger.debug("inline TIC extract failed for %s", raw_path.name, exc_info=True)

        run_id = insert_run(
            instrument=instrument,
            run_name=raw_path.name,
            raw_path=str(raw_path),
            mode=mode_enum.value,
            metrics=metrics,
            gate_result=decision.result.value,
            failed_gates=decision.failed_gates,
            diagnosis=decision.diagnosis,
            amount_ng=hela_amount_ng,
            spd=resolved_spd,
            gradient_length_min=resolved_grad,
            db_path=db_path,
            run_date=acq_date,
        )
        record["run_id"] = run_id
        record["gate_result"] = decision.result.value

        _run_peg_and_drift(raw_path, run_id, db_path)
        _persist_tic(raw_path, run_id, db_path)
        _run_4dff_inline(raw_path, run_id=run_id, db_path=db_path)

        # HOLD flag deliberately NOT written on the Hive path. The
        # contract is "pause the instrument's next acquisition on FAIL"
        # — that needs the file on the instrument PC, not on Quobyte
        # where no acquisition watcher will ever see it. Pause-on-fail
        # for the Hive-side architecture is a future story for the
        # uploader (instrument PC writes the HOLD flag locally based on
        # the gate_result it gets back from the global DB). Don't ship
        # a no-op gate that gives a false sense of safety.

        record_dispatch_attempt(
            raw_path=str(raw_path), status="ok",
            last_run_id=run_id, db_path=db_path,
        )
        # If the search ran fine but produced zero IDs, surface that
        # as a distinct status so the dispatcher can flag for review
        # instead of treating it as a healthy completion.
        record["status"] = "search_empty" if primary_count == 0 else "ok"
        return record

    except Exception as e:
        logger.exception("hive-process failed for %s", raw_path)
        try:
            record_dispatch_attempt(
                raw_path=str(raw_path), status="failed",
                error=f"{type(e).__name__}: {e}",
                error_type=type(e).__name__, db_path=db_path,
            )
        except Exception:
            pass
        record["error"] = f"{type(e).__name__}: {e}"
        return record
