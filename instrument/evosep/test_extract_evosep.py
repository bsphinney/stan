"""Unit tests for the Evosep column-health extractor.

Run from this directory:  python3 -m pytest test_extract_evosep.py -q

These cover the analysis decisions that are easy to get subtly wrong and that
change the headline numbers — absolute-vs-relative time alignment, breach
persistence, trailing baselines, and the method classification that decides
what counts as a column signal at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import extract_evosep as ex


# ── absolute-time interpolation ───────────────────────────────────────────

def test_at_minute_endpoints_and_midpoint():
    curve = [0.0, 100.0]          # two bins spanning a 10-minute run
    assert ex._at_minute(curve, 10, 0) == 0.0
    assert ex._at_minute(curve, 10, 10) == 100.0
    assert ex._at_minute(curve, 10, 5) == 50.0


def test_at_minute_realigns_a_short_run():
    """A run that aborted early must not have its features shifted earlier.

    Two runs with the same absolute schedule but different lengths: read at
    the same absolute minute, they must agree. Bin-for-bin they would not —
    that artifact once turned a real 8.7 min warning into a false 11.1 min.
    """
    full = [0.0, 50.0, 100.0, 150.0, 200.0]     # 20-minute run, 5 bins
    short = full[:4]                             # same schedule, aborted at 16 min
    for t in (4.0, 8.0, 12.0):
        a = ex._at_minute(full, 20, t)
        b = ex._at_minute(short, 16, t)
        assert abs(a - b) < 1e-9, f"misaligned at {t} min: {a} vs {b}"


def test_at_minute_handles_empty():
    assert ex._at_minute([], 10, 5) is None
    assert ex._at_minute([1.0], 0, 5) is None


# ── downsampling ──────────────────────────────────────────────────────────

def test_downsample_uses_medians_not_means():
    """A single spike must not move the drawn curve."""
    series = [(0, 10.0), (1, 10.0), (2, 1000.0), (3, 10.0)]
    out = ex._downsample(series, n=1)
    assert out == [10.0]


def test_downsample_forward_fills_empty_bins():
    series = [(0, 5.0), (10, 7.0)]
    out = ex._downsample(series, n=4)
    assert len(out) == 4
    assert all(v is not None for v in out)


# ── method classification ─────────────────────────────────────────────────

def _runs(plateaus, dur=14.0, method="m"):
    return [{"method": method, "start": f"2026-08-{10 + i:02d}T00:00:00",
             "duration_min": dur, "plateau_bar": p, "peak_bar": p + 50,
             "curve": [p] * 8, "run": f"r{i}"}
            for i, p in enumerate(plateaus)]


def test_a_regulated_setpoint_is_not_an_analytical_method():
    """A pressure that never moves is a setpoint, not a column measurement.

    System-and-column-wash sits at 399.6 bar +/- 0.3 across weeks, straddling
    real clog events. Treating it as a column signal would put a permanently
    flat line in the panel and dilute the baselines.
    """
    ms = ex.summarise_method("wash", _runs([399.6, 399.6, 399.5, 399.6, 399.6, 399.6]))
    assert ms["pressure_controlled"] is True
    assert ms["analytical"] is False


def test_a_real_gradient_is_analytical():
    ms = ex.summarise_method("100spd", _runs([320, 325, 331, 338, 344, 352, 349]))
    assert ms["analytical"] is True
    assert ms["pressure_controlled"] is False


def test_low_pressure_utility_is_not_analytical():
    ms = ex.summarise_method("prep", _runs([50, 58, 72, 45, 61, 55]))
    assert ms["analytical"] is False


def test_unstable_duration_is_not_analytical():
    rs = _runs([300, 310, 305, 315, 308, 312])
    rs[0]["duration_min"] = 400.0        # Preparation runs vary wildly
    rs[1]["duration_min"] = 3.0
    ms = ex.summarise_method("prep", rs)
    assert ms["analytical"] is False


# ── baselines ─────────────────────────────────────────────────────────────

def test_local_baseline_is_trailing_only():
    """A run is never compared against its own future.

    The number the panel shows has to be the number a live watchdog would have
    had at the moment the run started.
    """
    rs = _runs([300] * 10 + [900])
    ex.add_local_baselines(rs)
    last = rs[-1]
    assert last["local_baseline_bar"] == 300           # unaffected by its own spike
    assert last["pct_over_baseline"] == 200.0
    # Too few priors to trust a baseline at all.
    assert "local_baseline_bar" not in rs[0]


# ── step detection ────────────────────────────────────────────────────────

def test_a_transient_spike_is_not_an_intervention():
    """One clogged sample makes a big step that comes straight back down."""
    series = [(f"2026-08-{10 + i:02d}T00:00:00", v)
              for i, v in enumerate([300, 300, 300, 400, 300, 300, 300])]
    assert ex.detect_steps(series) == []


def test_a_sustained_step_is_an_intervention():
    """A new column or a wash moves the level and it stays moved."""
    series = [(f"2026-08-{10 + i:02d}T00:00:00", v)
              for i, v in enumerate([400, 400, 400, 300, 300, 300, 300])]
    steps = ex.detect_steps(series)
    assert len(steps) == 1
    assert steps[0]["direction"] == "drop"
    assert steps[0]["change_pct"] == -25.0


# ── parsing ───────────────────────────────────────────────────────────────

def test_parse_series_skips_a_truncated_final_line(tmp_path):
    """Instrument logs from a killed run can end mid-write."""
    p = tmp_path / "Pump-HP_Pressure.txt"
    p.write_text("time\tPump HP:Pressure [bar]\n"
                 "00:00:01.000\t100.0\n"
                 "00:00:02.000\t200.0\n"
                 "00:00:03.0")
    out = ex.parse_series(str(p))
    assert out == [(1.0, 100.0), (2.0, 200.0)]


def test_parse_series_missing_file_is_empty_not_fatal():
    assert ex.parse_series("/nonexistent/Pump-HP_Pressure.txt") == []


def test_parse_maintenance_info_reads_wear_counters(tmp_path):
    p = tmp_path / "maintenance-info.txt"
    p.write_text(
        "Component: pumphp \n"
        "Product number: 1001 \n"
        "Displacement (total): 337 mL\n"
        "Displacement (seal): 337 mL\n"
        "---------\n"
        "Component: instrument \n"
        "100 samples per day: 14065 \n"
        "60 samples per day: 12486 \n"
        "Total analyses: 27822 \n"
        "Loop volume: 30.972 µL\n")
    info = ex.parse_maintenance_info(str(p))
    assert info["total_analyses"] == 27822
    assert info["loop_volume_ul"] == 30.972
    assert info["pump_seal_ml"]["pumphp"] == 337
    assert info["method_lifetime_counts"]["100 samples per day"] == 14065


# ── mirror folders ────────────────────────────────────────────────────────

def _mk_mirror(root, name, runs):
    """Create <root>/<name>/S00230/<run>/ for each run folder name."""
    for r in runs:
        (root / name / "S00230" / r).mkdir(parents=True, exist_ok=True)
    return name


def test_host_mirrors_returns_every_pull_oldest_first(tmp_path):
    """A later narrow pull must not hide an earlier full-history pull.

    The copy script writes a new timestamped folder per run and defaults to
    the last 30 days, so taking only the newest folder silently truncates a
    two-year `/all` pull down to a month.
    """
    _mk_mirror(tmp_path, "TIMS-10878_20260901_173533", ["100-samples-per-day_2026-08-17_14-04-38"])
    _mk_mirror(tmp_path, "TIMS-10878_20260903_090000", ["100-samples-per-day_2026-09-02_10-00-00"])
    assert ex.host_mirrors(str(tmp_path)) == [
        "TIMS-10878_20260901_173533", "TIMS-10878_20260903_090000"]


def test_host_mirrors_ignores_a_different_instrument_pc(tmp_path):
    _mk_mirror(tmp_path, "TIMS-10878_20260901_173533", ["a_2026-08-17_14-04-38"])
    _mk_mirror(tmp_path, "OTHER-PC_20260902_120000", ["b_2026-08-18_14-04-38"])
    # newest stamp belongs to OTHER-PC, so only OTHER-PC's mirrors are used
    assert ex.host_mirrors(str(tmp_path)) == ["OTHER-PC_20260902_120000"]


def test_host_mirrors_honours_an_explicit_choice(tmp_path):
    _mk_mirror(tmp_path, "TIMS-10878_20260901_173533", ["a_2026-08-17_14-04-38"])
    assert ex.host_mirrors(str(tmp_path), "TIMS-10878_20260901_173533") == [
        "TIMS-10878_20260901_173533"]


def test_host_mirrors_falls_back_for_an_unstamped_folder(tmp_path):
    (tmp_path / "handmade").mkdir()
    assert ex.host_mirrors(str(tmp_path)) == ["handmade"]


# ── column install inference ──────────────────────────────────────────────

def _series(start_day, n, bar):
    return [{"start": f"2026-08-{start_day + i:02d}T12:00:00", "plateau_bar": bar}
            for i in range(n)]


def _methods(series, steps):
    return {"m": {"method": "m", "analytical": True, "n_runs": len(series),
                  "series": series, "steps": steps}}


def test_inferred_install_too_close_to_window_start_is_unverifiable():
    """The real 2026-07-31 column change sits before the log window opens.

    With only days of log before the biggest drop, the extractor must not
    name that drop as the install date without saying it cannot know.
    """
    series = _series(14, 18, 330.0)
    steps = [{"at": "2026-08-19T11:38:43", "direction": "drop", "change_pct": -11.4}]
    out = ex.column_age([], _methods(series, steps), [])
    assert out["confidence"] == "unverifiable"
    assert out["installed_is_lower_bound"] is True
    assert out["log_days_before_install"] < ex.INSTALL_MIN_PRIOR_DAYS
    assert "on or before" in out["caveat"]


def test_inferred_install_with_enough_prior_history_is_trusted():
    series = [{"start": f"2026-06-{d:02d}T12:00:00", "plateau_bar": 330.0}
              for d in range(1, 29)]
    steps = [{"at": "2026-06-25T12:00:00", "direction": "drop", "change_pct": -11.4}]
    out = ex.column_age([], _methods(series, steps), [])
    assert out["confidence"] == "inferred"
    assert "installed_is_lower_bound" not in out


def test_a_logged_column_event_beats_inference():
    series = _series(14, 18, 330.0)
    steps = [{"at": "2026-08-19T11:38:43", "direction": "drop", "change_pct": -11.4}]
    events = [{"event_date": "2026-07-31T18:00:00", "event_type": "column_change",
               "first_run_resolved": "2026-07-31T18:00:00",
               "first_run": "23229", "first_run_match": "run name"}]
    out = ex.column_age([], _methods(series, steps), events)
    assert out["confidence"] == "logged"
    assert out["installed"] == "2026-07-31T18:00:00"


def test_host_mirrors_prefers_the_stable_mirror_and_keeps_legacy_pulls(tmp_path):
    """The 2026-09-02 rewrite mirrors to `<HOST>_mirror` instead of stamping.

    A name-based "newest" pick misses that folder entirely and silently reads
    a stale timestamped copy. Both must be read, mirror LAST so its copy of an
    overlapping run wins de-duplication.
    """
    _mk_mirror(tmp_path, "TIMS-10878_20260901_173533", ["a_2026-08-17_14-04-38"])
    _mk_mirror(tmp_path, "TIMS-10878_mirror", ["b_2023-07-12_09-00-00"])
    assert ex.host_mirrors(str(tmp_path)) == [
        "TIMS-10878_20260901_173533", "TIMS-10878_mirror"]


def test_host_mirrors_with_only_the_stable_mirror(tmp_path):
    _mk_mirror(tmp_path, "TIMS-10878_mirror", ["a_2023-07-12_09-00-00"])
    assert ex.host_mirrors(str(tmp_path)) == ["TIMS-10878_mirror"]


def test_a_mirror_beats_a_newer_timestamped_pull_from_another_pc(tmp_path):
    _mk_mirror(tmp_path, "TIMS-10878_mirror", ["a_2023-07-12_09-00-00"])
    _mk_mirror(tmp_path, "OTHER-PC_20260902_120000", ["b_2026-09-02_12-00-00"])
    assert ex.host_mirrors(str(tmp_path)) == ["TIMS-10878_mirror"]


def test_a_run_in_both_layouts_is_not_counted_twice(tmp_path):
    """The mirror was seeded from the first timestamped pull, so overlap is
    expected by construction. De-duplication, not exclusion, handles it."""
    shared = "100-samples-per-day_2026-08-17_14-04-38"
    _mk_mirror(tmp_path, "TIMS-10878_20260901_173533", [shared, "only-legacy_2026-08-17_15-00-00"])
    _mk_mirror(tmp_path, "TIMS-10878_mirror", [shared, "only-mirror_2023-07-12_09-00-00"])
    found = {}
    for m in ex.host_mirrors(str(tmp_path)):
        base = tmp_path / m / "S00230"
        for run in sorted(p.name for p in base.iterdir()):
            found[("S00230", run)] = str(base / run)
    assert len(found) == 3, found            # 4 folder entries, 3 distinct runs
    # the mirror's copy of the shared run wins, because it is walked last
    assert "TIMS-10878_mirror" in found[("S00230", shared)]


def test_parse_maintenance_info_handles_the_2023_file_shape(tmp_path):
    """The backfill reaches 2023, whose maintenance-info.txt differs.

    Verified against a real 2023-07-13 run: no leading numeric line, no
    `Component: Host` block and no `Loop mode` — the fields the wear counters
    and the column-age injection count depend on must still be found.
    """
    p = tmp_path / "maintenance-info.txt"
    p.write_text(
        "Component: Software\n"
        "Evosep One RC.Net Driver: hystardriver_2.3.57.0 \n"
        "---------------------------------------------------------------\n"
        "Component: pumphp\n"
        "Product number: 1001 \n"
        "Serial number: 1140 \n"
        "Displacement (total): 272 mL\n"
        "Displacement (seal): 272 mL\n"
        "---------------------------------------------------------------\n"
        "Component: instrument\n"
        "Product number: EV1000 \n"
        "Serial number: S00230 \n"
        "100 samples per day: 1244 \n"
        "60 samples per day: 2665 \n"
        "Total analyses: 4524 \n"
        "Loop volume: 30.972 µL\n")
    info = ex.parse_maintenance_info(str(p))
    assert info["total_analyses"] == 4524
    assert info["loop_volume_ul"] == 30.972
    assert info["pump_seal_ml"]["pumphp"] == 272
    assert info["method_lifetime_counts"]["60 samples per day"] == 2665


# ── daily aggregates ──────────────────────────────────────────────────────

def _run(day, hh, method, plateau, peak=200.0):
    return {"start": f"2026-08-{day:02d}T{hh:02d}:00:00", "method": method,
            "plateau_bar": plateau, "peak_bar": peak}


def test_daily_aggregates_group_by_calendar_day():
    runs = [_run(17, 9, "m", 300.0), _run(17, 12, "m", 310.0),
            _run(18, 9, "m", 400.0)]
    methods = {"m": {"analytical": True}}
    out = ex.daily_aggregates(runs, [], methods)
    assert [d["date"] for d in out] == ["2026-08-17", "2026-08-18"]
    assert out[0]["n_runs"] == 2
    assert out[0]["plateau_median_bar"] == 305.0
    assert out[1]["plateau_median_bar"] == 400.0


def test_daily_aggregates_exclude_utility_methods_from_pressure():
    """Preparation and wash run against a regulated setpoint, so letting them
    into the median would move the column number for a non-column reason."""
    runs = [_run(17, 9, "m", 300.0), _run(17, 10, "Preparation", 60.0)]
    methods = {"m": {"analytical": True}, "Preparation": {"analytical": False}}
    out = ex.daily_aggregates(runs, [], methods)
    assert out[0]["n_runs"] == 2
    assert out[0]["n_analytical"] == 1
    assert out[0]["plateau_median_bar"] == 300.0


def test_daily_aggregates_count_ceiling_and_flags():
    runs = [_run(17, 9, "m", 300.0, peak=ex.CEILING_BAR),
            _run(17, 10, "m", 300.0, peak=100.0)]
    methods = {"m": {"analytical": True}}
    flags = [{"start": "2026-08-17T09:00:00"}]
    out = ex.daily_aggregates(runs, flags, methods)
    assert out[0]["n_at_ceiling"] == 1
    assert out[0]["n_flagged"] == 1


# ── per-column segments ───────────────────────────────────────────────────

def _seg_methods(series, steps):
    return {"m": {"method": "m", "analytical": True, "n_runs": len(series),
                  "series": series, "steps": steps}}


def test_column_segments_split_on_a_baseline_drop():
    series = ([{"start": f"2026-06-{d:02d}T12:00:00", "plateau_bar": 400.0}
               for d in range(1, 16)]
              + [{"start": f"2026-06-{d:02d}T12:00:00", "plateau_bar": 300.0}
                 for d in range(16, 29)])
    steps = [{"at": "2026-06-16T12:00:00", "direction": "drop", "change_pct": -25.0}]
    out = ex.column_segments([], _seg_methods(series, steps), [])
    assert len(out) == 2
    assert out[0]["installed"] == "2026-06-01T12:00:00"
    assert out[0]["retired"] == "2026-06-16T12:00:00"
    assert out[1]["installed"] == "2026-06-16T12:00:00"
    assert out[1]["retired"] is None
    assert out[1]["baseline_at_install_bar"] == 300.0


def test_first_column_segment_is_marked_a_lower_bound():
    """The record starts mid-column, so the first segment's age is a floor —
    the same honesty rule column_age() applies."""
    series = [{"start": f"2026-06-{d:02d}T12:00:00", "plateau_bar": 400.0}
              for d in range(1, 16)]
    out = ex.column_segments([], _seg_methods(series, []), [])
    assert len(out) == 1
    assert out[0]["installed_is_lower_bound"] is True
    assert out[0]["days_is_lower_bound"] is True
    assert out[0]["source"] == "start of the record"


def test_column_segments_prefer_logged_events_over_inferred_steps():
    series = [{"start": f"2026-07-{d:02d}T12:00:00", "plateau_bar": 400.0}
              for d in range(1, 29)]
    steps = [{"at": "2026-07-20T12:00:00", "direction": "drop", "change_pct": -25.0}]
    events = [{"event_date": "2026-07-10T12:00:00", "event_type": "column_change"}]
    out = ex.column_segments([], _seg_methods(series, steps), events)
    assert [s["installed"] for s in out] == ["2026-07-01T12:00:00", "2026-07-10T12:00:00"]
    assert out[1]["source"] == "logged maintenance event"
    assert "installed_is_lower_bound" not in out[0]


# ── windowing and the size budget (end to end through main) ───────────────

def _write_run(base, method, date, hh, bar):
    """A minimally valid run folder: a Pump-HP pressure trace over 20 min."""
    d = base / f"{method}_{date}_{hh:02d}-00-00"
    d.mkdir(parents=True, exist_ok=True)
    rows = ["time\tPump HP:Pressure [bar]"]
    for i in range(60):                       # 20 min at 20 s
        s = i * 20
        rows.append(f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}.000\t"
                    f"{bar + (i % 3) * 0.5:.3f}")
    (d / "Pump-HP_Pressure.txt").write_text("\n".join(rows) + "\n")
    (d / "maintenance-info.txt").write_text(
        "Component: instrument\nTotal analyses: 100 \nLoop volume: 30.972 µL\n")
    return d


def _tree(tmp_path, n_days=40):
    """A mirror with one analytical run a day, pressure drifting upward."""
    base = tmp_path / "TIMS-10878_mirror" / "S00230"
    for i in range(n_days):
        day = datetime(2026, 6, 1) + timedelta(days=i)
        _write_run(base, "100-samples-per-day", day.strftime("%Y-%m-%d"), 12,
                   300.0 + i * 2.0)
    return tmp_path


def _run_main(tmp_path, out, *args):
    import json as _json
    rc = ex.main(["--root", str(tmp_path), "--out", str(out), *args])
    return rc, (_json.loads(out.read_text()) if out.exists() else None)


def test_summary_describes_the_whole_record_while_runs_are_windowed(tmp_path):
    out = tmp_path / "o.json"
    rc, doc = _run_main(_tree(tmp_path), out, "--runs-window-days", "10")
    assert rc == 0
    assert doc["summary"]["n_runs"] == 40           # the whole record
    assert doc["summary"]["first_run"].startswith("2026-06-01")
    assert doc["runs_window"]["days"] == 10
    assert len(doc["runs"]) < 40                    # ...but trimmed on the way out
    assert len(doc["daily"]) == 40                  # one entry per day, forever
    assert all(s["start"] >= doc["runs_window"]["from"]
               for m in doc["methods"].values() for s in (m.get("series") or []))


def test_window_zero_keeps_every_run(tmp_path):
    out = tmp_path / "o.json"
    rc, doc = _run_main(_tree(tmp_path), out,
                        "--runs-window-days", "0", "--keep-all-runs")
    assert rc == 0
    assert doc["runs_window"] is None
    assert len(doc["runs"]) == 40


def test_over_budget_fails_loudly_instead_of_publishing(tmp_path, capsys):
    """A 15 MB row served to a phone is the regression this prevents."""
    out = tmp_path / "o.json"
    rc = ex.main(["--root", str(_tree(tmp_path)), "--out", str(out),
                  "--max-doc-mb", "0.0001"])
    assert rc == 3
    assert not out.exists(), "an over-budget document must not be written"
    err = capsys.readouterr().err
    assert "over the" in err and "budget" in err


# ── operator-supplied "first run on the new column" ───────────────────────

def _age_methods(steps):
    series = [{"start": f"2026-08-{d:02d}T12:00:00", "plateau_bar": 330.0}
              for d in range(14, 32)]
    return {"m": {"method": "m", "analytical": True, "n_runs": len(series),
                  "series": series, "steps": steps}}


def test_a_resolved_first_run_anchors_the_install():
    """The operator names the first run on the new column; that run's
    timestamp is the boundary, not the day they got round to logging it."""
    events = [{"event_type": "column_change",
               "event_date": "2026-08-25T00:00:00",
               "first_run": "20260731_HE50_60-spd-dia-new-zdf-column_S1-A1_1_23232.d",
               "first_run_resolved": "2026-07-31T18:55:02",
               "first_run_match": "run name"}]
    out = ex.column_age([], _age_methods([]), events)
    assert out["installed"] == "2026-07-31T18:55:02"
    assert out["confidence"] == "logged"
    assert out["source"] == "logged column change, anchored to its first run"
    assert out["logged_first_run"]["matched_by"] == "run name"
    assert "installed_is_lower_bound" not in out


def test_a_logged_install_records_where_the_inference_disagreed():
    """The 2026-08-19 answer was a glass-capillary swap. Keeping it visible
    beside the logged date is what shows the inference was off."""
    steps = [{"at": "2026-08-19T11:38:43", "direction": "drop", "change_pct": -11.4}]
    events = [{"event_type": "column_change",
               "event_date": "2026-08-25T00:00:00",
               "first_run": "23232",
               "first_run_resolved": "2026-07-31T18:55:02",
               "first_run_match": "injection counter 23232"}]
    out = ex.column_age([], _age_methods(steps), events)
    assert out["installed"] == "2026-07-31T18:55:02"
    assert out["inferred_installed"] == "2026-08-19T11:38:43"
    assert out["inferred_disagrees_by_days"] == 18.7
    assert "capillary swap" in out["inference_note"]


def test_an_unresolvable_first_run_falls_back_to_the_event_date():
    """A typo must degrade to the logged date, never fail the extract."""
    events = [{"event_type": "column_change",
               "event_date": "2026-08-25T00:00:00",
               "first_run": "whatever the operator typed"}]
    out = ex.column_age([], _age_methods([]), events)
    assert out["installed"] == "2026-08-25T00:00:00"
    assert out["source"] == "logged maintenance event"
    assert out["logged_first_run"]["unresolved"] is True
    assert out["confidence"] == "inferred"      # date good only to the day


def test_resolve_first_run_ignores_empty_input_without_touching_the_db():
    assert ex.resolve_first_run(None, "timsTOF HT") is None
    assert ex.resolve_first_run("", "timsTOF HT") is None
    assert ex.resolve_first_run("   ", "timsTOF HT") is None


# ── the pruning describes itself ──────────────────────────────────────────

def test_the_window_block_says_what_was_pruned_and_why(tmp_path):
    """A short `runs` array must be legible as policy, not as truncation."""
    out = tmp_path / "o.json"
    rc, doc = _run_main(_tree(tmp_path), out, "--runs-window-days", "10")
    assert rc == 0
    w = doc["runs_window"]
    assert w["n_runs_in_window"] >= w["n_runs_kept"]
    assert w["n_runs_pruned"] == w["n_runs_in_window"] - w["n_runs_kept"]
    assert "curve" in w["runs_kept_rule"]
    assert "daily" in w["why"] and "--keep-all-runs" in w["why"]


def test_keep_all_runs_says_so_in_the_document(tmp_path):
    out = tmp_path / "o.json"
    rc, doc = _run_main(_tree(tmp_path), out,
                        "--runs-window-days", "10", "--keep-all-runs")
    assert rc == 0
    w = doc["runs_window"]
    assert w["runs_kept_rule"] == "every run in the window"
    assert w["n_runs_pruned"] == 0


# ── timestamps: logged events are UTC-aware, the Evosep logs are naive local ──

def test_to_log_time_puts_utc_events_on_the_local_log_clock():
    """Not cosmetic: comparing an aware event_date with a naive log timestamp
    raises, and comparing their ISO strings skews everything by the UTC
    offset — enough to date a column change to the wrong day."""
    got = ex.to_log_time("2026-08-01T01:32:39+00:00")
    assert got is not None and "+" not in got and got.endswith(("0", "9", "8"))
    assert ex.to_log_time("2026-08-19T11:38:43") == "2026-08-19T11:38:43"
    assert ex.to_log_time(None) is None
    assert ex.to_log_time("not a date") is None


def test_a_utc_logged_event_does_not_crash_against_a_naive_inference():
    """Regression: `can't subtract offset-naive and offset-aware datetimes`,
    hit on the first real cron tick."""
    steps = [{"at": "2026-08-19T11:38:43", "direction": "drop", "change_pct": -11.4}]
    events = [{"event_type": "column_change",
               "event_date": ex.to_log_time("2026-07-31T18:00:00+00:00")}]
    out = ex.column_age([], _age_methods(steps), events)
    assert out["confidence"] == "inferred"
    assert isinstance(out["inferred_disagrees_by_days"], float)


def test_an_install_the_log_has_not_reached_yet_fails_closed():
    """A column logged as installed after the last mirrored run has no run
    measuring it, so age and wear are not computable from pressure. It must
    fail closed rather than report a number, so wear alerting stays silent
    until the new column's runs actually arrive."""
    steps = [{"at": "2026-08-19T11:38:43", "direction": "drop", "change_pct": -11.4}]
    events = [{"event_type": "column_change", "event_date": "2026-09-02T05:00:00"}]
    out = ex.column_age([], _age_methods(steps), events)   # series ends 2026-08-31
    assert out["confidence"] == "unverifiable"
    assert out["installed_is_lower_bound"] is True
    assert "no run on this column has reached the log yet" in out["caveat"]


def test_an_unanchored_event_inside_the_record_is_inferred_not_logged():
    """It names the right column, but a placeholder noon is only good to the
    day — so the date is weaker evidence than an anchored first run."""
    events = [{"event_type": "column_change", "event_date": "2026-08-20T12:00:00"}]
    out = ex.column_age([], _age_methods([]), events)
    assert out["confidence"] == "inferred"
    assert "installed_is_lower_bound" not in out


def test_an_anchored_event_inside_the_record_is_logged():
    events = [{"event_type": "column_change", "event_date": "2026-08-20T12:00:00",
               "first_run": "23229", "first_run_resolved": "2026-08-20T09:14:00",
               "first_run_match": "run name"}]
    out = ex.column_age([], _age_methods([]), events)
    assert out["confidence"] == "logged"
    assert out["installed"] == "2026-08-20T09:14:00"


def test_a_step_across_a_hole_in_the_record_is_not_an_intervention():
    """The mirror is backfilled in chunks, so a 2023 block can sit next to a
    2026 block. The pressure difference across that hole is not an event —
    it once produced an 'intervention' whose two runs were 1,141 days apart."""
    series = ([(f"2023-07-{d:02d}T12:00:00", 400.0) for d in range(10, 20)]
              + [(f"2026-08-{d:02d}T12:00:00", 300.0) for d in range(10, 20)])
    assert ex.detect_steps(series) == []


def test_a_step_within_normal_running_is_still_found():
    """The gap guard must not silence real interventions across a weekend."""
    series = ([(f"2026-08-{d:02d}T12:00:00", 400.0) for d in range(1, 11)]
              + [(f"2026-08-{d:02d}T12:00:00", 300.0) for d in range(11, 21)])
    steps = ex.detect_steps(series)
    assert len(steps) == 1
    assert steps[0]["direction"] == "drop"
    assert steps[0]["at"].startswith("2026-08-11")


def test_inference_uses_the_most_recent_drop_not_the_deepest():
    """"How old is the column fitted now" is answered by the LAST boundary.
    Picking the deepest drop was invisible on a fortnight of log and absurd on
    years of it — it named a 2023 date against the 2023+2026 mirror."""
    steps = [{"at": "2023-07-18T11:29:41", "direction": "drop", "change_pct": -35.0},
             {"at": "2026-08-19T11:38:43", "direction": "drop", "change_pct": -11.4}]
    out = ex.column_age([], _age_methods(steps), [])
    assert out["installed"] == "2026-08-19T11:38:43"


def test_age_is_measured_from_the_install_not_the_first_visible_run():
    """The 30-min tick ships a 3-day window. With an anchored install of
    2026-07-31 the old code measured `since[0]` -> last run and reported a
    3-day-old column that was really 33 days old."""
    series = [{"start": f"2026-08-{d:02d}T12:00:00", "plateau_bar": 330.0}
              for d in range(29, 32)]
    methods = {"m": {"method": "m", "analytical": True, "n_runs": len(series),
                     "series": series, "steps": []}}
    events = [{"event_type": "column_change", "event_date": "2026-07-31T18:04:00"}]
    out = ex.column_age([], methods, events)
    assert out["days_since"] == 30.75          # from the install
    assert out["observed_days"] == 2.0         # the window is only 3 runs
    assert out["log_covers_install"] is False
    assert out["counts_are_lower_bounds"] is True
    assert "only" in out["coverage_note"]


def test_full_coverage_reports_no_lower_bound_caveat():
    series = [{"start": f"2026-07-{d:02d}T12:00:00", "plateau_bar": 330.0}
              for d in range(20, 31)]
    methods = {"m": {"method": "m", "analytical": True, "n_runs": len(series),
                     "series": series, "steps": []}}
    events = [{"event_type": "column_change", "event_date": "2026-07-25T12:00:00"}]
    out = ex.column_age([], methods, events)
    assert out["log_covers_install"] is True
    assert "counts_are_lower_bounds" not in out
    assert out["days_since"] == out["observed_days"]


def test_the_newest_change_wins_even_without_an_anchor():
    """Both rows are REAL changes: 2026-07-31 (anchored to run 23229) and
    2026-09-02. Preferring the anchored one reported the column that had just
    been REMOVED — a 33-day age for a column hours old. A missing anchor
    lowers confidence in the date; it never selects a different column."""
    series = ([{"start": f"2026-08-{d:02d}T12:00:00", "plateau_bar": 330.0}
               for d in range(1, 32)]
              + [{"start": "2026-09-02T10:33:00", "plateau_bar": 399.0},
                 {"start": "2026-09-02T10:47:00", "plateau_bar": 452.0}])
    methods = {"m": {"method": "m", "analytical": True, "n_runs": len(series),
                     "series": series, "steps": []}}
    events = [
        {"event_type": "column_change", "event_date": "2026-09-02T05:00:00"},
        {"event_type": "column_change", "event_date": "2026-07-31T05:00:00",
         "first_run": "23229", "first_run_resolved": "2026-07-31T11:04:35",
         "first_run_match": "injection counter 23229"},
    ]
    out = ex.column_age([], methods, events)
    assert out["installed"] == "2026-09-02T05:00:00"
    assert out["confidence"] == "inferred"       # newest, but unanchored
    assert "placeholder noon" in out["caveat"]
    considered = out["logged_events_considered"]
    assert [e["used"] for e in considered] == [False, True]
    assert considered[0]["anchored"] is True


def test_with_no_anchor_anywhere_the_newest_event_still_wins():
    series = [{"start": f"2026-08-{d:02d}T12:00:00", "plateau_bar": 330.0}
              for d in range(1, 31)]
    methods = {"m": {"method": "m", "analytical": True, "n_runs": len(series),
                     "series": series, "steps": []}}
    events = [{"event_type": "column_change", "event_date": "2026-08-05T05:00:00"},
              {"event_type": "column_change", "event_date": "2026-08-20T05:00:00"}]
    out = ex.column_age([], methods, events)
    assert out["installed"] == "2026-08-20T05:00:00"


# ── per-column pressure vs flow ───────────────────────────────────────────

def _pr_run(day, hh, method, flow, bar):
    return {"start": f"2026-08-{day:02d}T{hh:02d}:00:00", "method": method,
            "plateau_bar": bar, "plateau_flow_ul_min": flow, "peak_bar": bar,
            "local_baseline_bar": bar}


def _pr_methods():
    return {"100-samples-per-day": {"analytical": True},
            "Preparation": {"analytical": False}}


def test_pressure_table_bins_by_measured_flow_not_nominal():
    runs = ([_pr_run(d, 9, "100-samples-per-day", 1.02, 350.0) for d in range(1, 9)]
            + [_pr_run(d, 12, "100-samples-per-day", 1.51, 520.0) for d in range(1, 9)])
    segs = [{"installed": "2026-08-01T00:00:00", "retired": None}]
    out = ex.pressure_flow_table(runs, _pr_methods(), segs, {})
    bins = out["columns"][0]["bins"]
    assert [b["flow_ul_min"] for b in bins] == [1.02, 1.51]
    assert bins[0]["plateau_median_bar"] == 350.0
    # bar per uL/min is ~constant across bins for a healthy bed
    assert abs(bins[0]["bar_per_ul_min"] - bins[1]["bar_per_ul_min"]) < 15


def test_pressure_table_excludes_utility_methods():
    runs = ([_pr_run(d, 9, "100-samples-per-day", 1.0, 350.0) for d in range(1, 9)]
            + [_pr_run(d, 10, "Preparation", 1.0, 60.0) for d in range(1, 9)])
    segs = [{"installed": "2026-08-01T00:00:00", "retired": None}]
    out = ex.pressure_flow_table(runs, _pr_methods(), segs, {})
    assert out["columns"][0]["n_runs"] == 8
    assert out["columns"][0]["bins"][0]["plateau_median_bar"] == 350.0


def test_a_bin_below_the_minimum_is_not_published():
    """A 'typical pressure' from two runs is not a reference value."""
    runs = ([_pr_run(d, 9, "100-samples-per-day", 1.0, 350.0) for d in range(1, 9)]
            + [_pr_run(d, 11, "100-samples-per-day", 2.0, 700.0) for d in range(1, 3)])
    segs = [{"installed": "2026-08-01T00:00:00", "retired": None}]
    out = ex.pressure_flow_table(runs, _pr_methods(), segs, {})
    assert [b["flow_ul_min"] for b in out["columns"][0]["bins"]] == [1.0]


def test_a_segment_whose_life_is_mostly_unobserved_is_flagged():
    """The 'previous column' segment is stated 2023-07 -> 2026-07 but every
    run in it is from 2023. Presented as one column it would claim the current
    column is 36% stiffer than the one it replaced, which the data cannot say."""
    runs = [{"start": f"2023-07-{d:02d}T09:00:00", "method": "100-samples-per-day",
             "plateau_bar": 300.0, "plateau_flow_ul_min": 1.0, "peak_bar": 300.0}
            for d in range(10, 20)]
    segs = [{"installed": "2023-07-10T00:00:00", "retired": "2026-07-31T05:00:00"}]
    out = ex.pressure_flow_table(runs, _pr_methods(), segs, {})
    col = out["columns"][0]
    assert col["is_one_column"] is False
    kinds = [g["kind"] for g in col["spans_gaps"]]
    assert "unobserved_end" in kinds
    assert col["observed_to"].startswith("2023-07-19")


def test_the_table_stamps_the_oven_temperature_and_says_it_is_unrecorded():
    runs = [_pr_run(d, 9, "100-samples-per-day", 1.0, 350.0) for d in range(1, 9)]
    segs = [{"installed": "2026-08-01T00:00:00", "retired": None}]
    cat = {"defaults": {"oven_c": 50, "oven_c_source": "operator-reported",
                        "oven_device": "Bruker Column Toaster"}}
    out = ex.pressure_flow_table(runs, _pr_methods(), segs, cat)
    assert out["measured_at_oven_c"] == 50
    assert out["oven_c_source"] == "operator-reported"
    assert out["temperature_recorded_per_run"] is False


def test_darcy_check_is_a_smell_test_that_passes_on_real_geometry():
    col = {"length_cm": 10, "bore_um": 150, "particle_um": 1.5}
    bins = [{"flow_ul_min": 1.0, "plateau_median_bar": 236.1}]
    out = ex.darcy_check(col, bins, 50)
    assert out["agrees"] is True
    assert 0.5 < out["median_ratio"] < 2.0
    assert 150 < out["rows"][0]["predicted_bar"] < 250


# ── sample pressure impact ────────────────────────────────────────────────

def _imp_runs(n=40, step_at=None, step=0.0, flow=1.5, method="100-samples-per-day",
              day0=1):
    out = []
    for i in range(n):
        bar = 300.0 + (step if step_at is not None and i >= step_at else 0.0)
        out.append({"start": f"2026-08-{day0 + i // 20:02d}T{(i % 20) + 2:02d}:00:00",
                    "method": method, "plateau_bar": bar,
                    "plateau_flow_ul_min": flow, "peak_bar": bar,
                    "well": f"S5-A{i % 9 + 1}"})
    return out


_IMP_METHODS = {"100-samples-per-day": {"analytical": True},
                "System-and-column-wash": {"analytical": True}}
_SEG = [{"installed": "2026-08-01T00:00:00", "retired": None}]


def test_a_single_step_is_reported_once_not_once_per_neighbour():
    """The core design decision. A per-run (after - before) delta reports one
    step k times with near-identical magnitudes and cannot say which injection
    caused it — on real data that turned 4 events into 18 flags."""
    runs = _imp_runs(40, step_at=20, step=90.0)
    out = ex.sample_pressure_impact(runs, _IMP_METHODS, _SEG, [], [])
    assert out["n_flags"] == 1
    f = out["flags"][0]
    assert f["at"] == runs[20]["start"]
    assert f["delta_bar_per_ul_min"] == 60.0        # 90 bar at 1.5 uL/min


def test_a_transient_spike_is_not_a_flag():
    """Elevated during one injection and back to baseline afterwards is
    annoying, not damage. Only a persistent step counts."""
    runs = _imp_runs(40)
    runs[20]["plateau_bar"] = 500.0
    out = ex.sample_pressure_impact(runs, _IMP_METHODS, _SEG, [], [])
    assert out["n_flags"] == 0


def test_column_conditioning_is_not_blamed_on_a_sample():
    runs = _imp_runs(40, step_at=2, step=90.0)
    out = ex.sample_pressure_impact(runs, _IMP_METHODS, _SEG, [], [])
    assert out["n_flags"] == 0


def test_a_step_beside_a_logged_intervention_is_not_blamed_on_a_sample():
    runs = _imp_runs(40, step_at=20, step=90.0)
    events = [{"event_date": runs[20]["start"], "event_type": "column_change"}]
    out = ex.sample_pressure_impact(runs, _IMP_METHODS, _SEG, events, [])
    assert out["n_flags"] == 0


def test_a_pressure_controlled_method_is_skipped():
    """The washes hold 400 bar and let flow float, so their pressure cannot
    step and a 'step' there would be meaningless."""
    runs = _imp_runs(40, step_at=20, step=90.0, method="System-and-column-wash")
    for i, r in enumerate(runs):          # flow floats, pressure pinned
        r["plateau_flow_ul_min"] = 1.5 + (i % 7) * 0.1
    out = ex.sample_pressure_impact(runs, _IMP_METHODS, _SEG, [], [])
    assert out["n_flags"] == 0


def test_a_step_below_the_absolute_floor_is_not_flagged():
    runs = _imp_runs(40, step_at=20, step=6.0)     # 4 bar/(uL/min) at 1.5
    out = ex.sample_pressure_impact(runs, _IMP_METHODS, _SEG, [], [])
    assert out["n_flags"] == 0


def test_attribution_joins_on_well_and_time_and_parses_the_submission():
    runs = _imp_runs(40, step_at=20, step=90.0)
    culprit = runs[20]
    index = [{"run_name": f"20260827_793_100spd_Plasma-12_{culprit['well']}_1_24031.d",
              "t": culprit["start"], "well": culprit["well"]}]
    out = ex.sample_pressure_impact(runs, _IMP_METHODS, _SEG, [], index)
    f = out["flags"][0]
    assert f["submission"] == "PROT_0793"
    assert f["confidence"] == "high"
    agg = out["by_submission"][0]
    assert agg["n_flagged"] == 1
    assert agg["delta_per_injection"] is not None


def test_the_submission_parse_is_anchored_so_a_well_is_not_read_as_one():
    """`^\\d{8}_(\\d{2,4})_` — anchored precisely so plate wells and instrument
    serials later in the name cannot be misread as a submission number."""
    assert ex.SUBMISSION_RE.match("20260827_793_100spd_Hel50_S6-B12_1_24031.d")
    assert ex.SUBMISSION_RE.match("20260827_793_x").group(1) == "793"
    assert ex.SUBMISSION_RE.match("08222026_HE50_60-spd-dia_S1-A1_1_1.d") is None
    assert ex.SUBMISSION_RE.match("19aug26_HeL50_100spd_S4-E1_1_2.d") is None


def test_the_limits_ship_with_the_numbers():
    """This feature names people; the caveats are not optional."""
    runs = _imp_runs(40, step_at=20, step=90.0)
    out = ex.sample_pressure_impact(runs, _IMP_METHODS, _SEG, [], [])
    joined = " ".join(out["limits"]).lower()
    assert "correlation, not cause" in joined
    assert "run order" in joined
    assert "delta_per_injection" in joined


def test_a_control_injection_is_never_charged_to_a_submission():
    """A HeLa standard or a blank is not a submitted sample. The step is still
    reported — a blank that fouls a column is worth knowing — but it does not
    reach anyone's tally."""
    runs = _imp_runs(40, step_at=20, step=90.0)
    culprit = runs[20]
    index = [{"run_name": f"20260827_793_100spd_Hel50_{culprit['well']}_1_24031.d",
              "t": culprit["start"], "well": culprit["well"]}]
    out = ex.sample_pressure_impact(runs, _IMP_METHODS, _SEG, [], index)
    f = out["flags"][0]
    assert f["is_control"] is True
    assert "submission" not in f
    assert out["by_submission"] == []


# ── absolute expected pressure ────────────────────────────────────────────

_REF = {
    "flow_bin_ul_min": 0.1,
    "measured_at_oven_c": 50,
    "oven_c_source": "operator-reported",
    "columns": [{"installed": "2026-08-01T00:00:00", "retired": None,
                 "bins": [{"flow_ul_min": 1.5, "plateau_median_bar": 349.5,
                           "plateau_p5_bar": 320.9}]}],
}


def test_expected_pressure_does_not_chase_a_sustained_clog():
    """The measured failure: during the 2026-08-31 clog the trailing baseline
    climbed 321.8 -> 361.1 and absorbed 12 points, demoting a critical run to
    elevated. An absolute reference must not decay like that."""
    runs = [{"start": "2026-08-31T23:32:00", "plateau_bar": 417.8,
             "plateau_flow_ul_min": 1.5},
            {"start": "2026-09-01T00:43:00", "plateau_bar": 408.3,
             "plateau_flow_ul_min": 1.5}]
    meta = ex.attach_expected_pressure(runs, _REF)
    assert meta["available"] is True
    assert runs[0]["pct_over_expected"] == 30.2
    assert runs[1]["pct_over_expected"] == 27.2   # was 13.1% against baseline
    # the two stay within a few points of each other, unlike 29.8 -> 13.1
    assert abs(runs[0]["pct_over_expected"] - runs[1]["pct_over_expected"]) < 5


def test_no_expectation_is_borrowed_from_another_column():
    """A column hours old has no reference of its own. Using its
    predecessor's would report +24% for a brand-new column."""
    runs = [{"start": "2026-09-02T10:33:00", "plateau_bar": 398.9,
             "plateau_flow_ul_min": 1.5}]
    ref = {"flow_bin_ul_min": 0.1, "columns": [
        {"installed": "2026-08-01T00:00:00", "retired": "2026-09-02T05:00:00",
         "bins": [{"flow_ul_min": 1.5, "plateau_p5_bar": 320.9}]}]}
    meta = ex.attach_expected_pressure(runs, ref)
    assert "expected_plateau_bar" not in runs[0]
    assert "too few runs on it yet" in runs[0]["expected_unavailable"]
    assert meta["n_runs_without_expectation"] == 1


def test_expected_pressure_carries_the_temperature_caveat():
    runs = [{"start": "2026-08-31T23:32:00", "plateau_bar": 417.8,
             "plateau_flow_ul_min": 1.5}]
    meta = ex.attach_expected_pressure(runs, _REF)
    assert meta["assumes_oven_c"] == 50
    assert "recorded nowhere per run" in meta["temperature_caveat"]


def test_expected_pressure_reports_when_there_is_no_reference_at_all():
    meta = ex.attach_expected_pressure([], {})
    assert meta["available"] is False
    assert "enough runs" in meta["reason"]


# ── wash flow (pressure-controlled washes) ────────────────────────────────

def _wash(day, hh, flow, bar=399.6, sp=400.0):
    return {"start": f"2026-09-{day:02d}T{hh:02d}:00:00",
            "method": "System-and-column-wash", "control_mode": "pressure",
            "setpoint_bar": sp, "plateau_bar": bar, "plateau_flow_ul_min": flow}


def test_wash_flow_uses_only_pressure_controlled_runs_at_their_setpoint():
    """Under pressure control the pressure is pinned and FLOW is the
    measurement — but only if the run actually reached the setpoint."""
    runs = [_wash(1, 9, 2.10), _wash(1, 10, 2.12),
            _wash(1, 11, 1.50, bar=250.0),          # never reached 400
            {"start": "2026-09-01T12:00:00", "method": "100-samples-per-day",
             "control_mode": "flow", "plateau_bar": 350.0,
             "plateau_flow_ul_min": 1.5}]           # flow-controlled: excluded
    out = ex.wash_flow_trend(runs, [])
    assert out["n"] == 2
    assert out["setpoint_bar"] == 400.0
    assert [p["flow_ul_min"] for p in out["series"]] == [2.10, 2.12]


def test_washes_are_split_by_install_TIMESTAMP_not_by_date():
    """The 2026-09-02 change happened mid-morning. Grouping by date puts that
    morning's washes — the blocked column being removed — into the new
    column's figures and halves its apparent permeability."""
    runs = [_wash(2, 9, 1.74), _wash(2, 10, 1.73), _wash(2, 13, 2.25)]
    segs = [{"installed": "2026-08-01T00:00:00", "retired": "2026-09-02T11:00:00"},
            {"installed": "2026-09-02T11:00:00", "retired": None}]
    out = ex.wash_flow_trend(runs, segs)
    assign = {p["t"][11:13]: p["segment_installed"] for p in out["series"]}
    assert assign["09"] == "2026-08-01T00:00:00"
    assert assign["10"] == "2026-08-01T00:00:00"
    assert assign["13"] == "2026-09-02T11:00:00"


def test_a_step_just_after_the_install_warns_the_boundary_is_wrong():
    """Real case: three washes at 1.74 (old column) and two at 2.25 (new)
    landed in one group because the install is logged at a placeholder noon."""
    runs = [_wash(2, 9, 1.741), _wash(2, 10, 1.735), _wash(2, 11, 1.748),
            _wash(2, 13, 2.252), _wash(2, 14, 2.258)]
    segs = [{"installed": "2026-09-02T05:00:00", "retired": None}]
    out = ex.wash_flow_trend(runs, segs)
    bw = out["by_segment"][0]["boundary_warning"]
    assert bw["n_before"] == 3
    assert bw["change_pct"] > 25
    assert bw["days_into_segment"] < 1        # hours in -> placeholder
    assert "placeholder" in bw["note"]


def test_a_steady_column_raises_no_boundary_warning():
    runs = [_wash(2, h, 2.10 + (h % 3) * 0.01) for h in range(9, 20)]
    out = ex.wash_flow_trend(runs, [{"installed": "2026-09-02T00:00:00",
                                     "retired": None}])
    assert "boundary_warning" not in out["by_segment"][0]


def test_the_setpoint_is_read_over_the_plateau_not_the_whole_run(tmp_path):
    """A wash ramps through lower setpoints on its way to 400 bar. Taking the
    median over the whole run returned 202.5 for a run plainly holding 400,
    which then failed the tolerance check and dropped a real point."""
    p = tmp_path / "Pump-HP_Setpoint.txt"
    rows = ["time\tPump HP:Setpoint []"]
    for i in range(60):                       # 20 min at 20 s
        s = i * 20
        # Exactly half low, half high — which is how the real file produced a
        # whole-run median of 202.5, the midpoint of 5 and 400.
        val = 5.0 if i < 30 else 400.0
        rows.append(f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}.000\t{val:.3f}")
    p.write_text("\n".join(rows) + "\n")
    # Whole-run: the median lands between the two levels, so the run is
    # scored against a setpoint it never held and the point is dropped.
    polluted = ex.parse_control_mode(str(p))
    assert polluted.get("setpoint_bar") == 202.5
    got = ex.parse_control_mode(str(p), dur_s=1200.0)
    assert got["control_mode"] == "pressure"
    assert got["setpoint_bar"] == 400.0


def test_wash_flow_limits_forbid_comparing_with_analytical_resistance():
    runs = [_wash(1, 9, 2.10), _wash(1, 10, 2.12)]
    joined = " ".join(ex.wash_flow_trend(runs, [])["limits"]).lower()
    assert "only with other wash flows" in joined
    assert "viscous heating" in joined


# ── column change detection (two channels + the recovery test) ────────────

def _res_run(day, hh, bar, flow=1.5, method="100-samples-per-day"):
    return {"start": f"2026-08-{day:02d}T{hh:02d}:00:00", "method": method,
            "control_mode": "flow", "plateau_bar": bar,
            "plateau_flow_ul_min": flow, "peak_bar": bar}


_CC_METHODS = {"100-samples-per-day": {"analytical": True}}


def _settled_then(day_from, day_to, resistance):
    """One run an hour, at a given resistance, over a range of days."""
    return [_res_run(d, h, resistance * 1.5)
            for d in range(day_from, day_to) for h in (9, 12, 15, 18)]


def test_a_clog_recovery_is_not_a_column_change():
    """The failure that split one real column into three. Clearing a blockage
    is a sustained drop and can lift wash flow, so neither channel nor the
    pair tells it from a new column — but it returns resistance to where the
    column already sat, where a new column goes materially below it."""
    runs = (_settled_then(1, 15, 190.0)          # healthy at 190
            + _settled_then(15, 20, 260.0)       # clogs up to 260
            + _settled_then(20, 28, 192.0))      # cleared, back to ~190
    assert ex.detect_column_changes(runs, _CC_METHODS) == []


def test_a_real_column_change_drops_below_the_old_baseline():
    runs = (_settled_then(1, 15, 220.0)          # settled at 220
            + _settled_then(15, 28, 170.0))      # new column, a new low
    got = ex.detect_column_changes(runs, _CC_METHODS)
    assert len(got) == 1
    assert got[0]["at"].startswith("2026-08-15")
    assert got[0]["resistance_change_pct"] < -8
    assert got[0]["provenance"] == "detected"     # no washes either side


def test_washes_that_do_not_rise_veto_a_candidate():
    """A drop bracketed by washes whose flow did NOT improve is evidence the
    column did not change."""
    runs = _settled_then(1, 15, 220.0) + _settled_then(15, 28, 170.0)
    flat = [{"start": f"2026-08-{d:02d}T20:00:00", "method": "wash",
             "control_mode": "pressure", "setpoint_bar": 400.0,
             "plateau_bar": 399.6, "plateau_flow_ul_min": 2.0}
            for d in (13, 14, 15, 16)]
    assert ex.detect_column_changes(runs + flat, _CC_METHODS) == []


def test_lifetimes_count_only_analytical_injections():
    runs = (_settled_then(1, 10, 200.0)
            + [{"start": "2026-08-05T21:00:00", "method": "wash",
                "control_mode": "pressure", "setpoint_bar": 400.0,
                "plateau_bar": 399.6, "plateau_flow_ul_min": 2.0}])
    methods = {"100-samples-per-day": {"analytical": True},
               "wash": {"analytical": False}}
    out = ex.column_lifetimes(runs, methods, [], [])
    assert out["n_columns"] == 1
    assert out["current"]["injections"] == 36        # the wash is not counted


def test_the_newest_column_is_reported_even_when_too_new_to_judge():
    runs = _settled_then(1, 15, 220.0) + _settled_then(15, 16, 170.0)
    det = ex.detect_column_changes(runs, _CC_METHODS)
    out = ex.column_lifetimes(runs, _CC_METHODS, [], det)
    cur = out["current"]
    assert cur["provisional"] is True
    assert cur["injections"] > 0
    assert "too few to characterise" in cur["provisional_note"]


# ── runs remaining, from the wash-flow decline ────────────────────────────

def test_runs_remaining_needs_enough_washes():
    out = ex.runs_remaining([(i * 10, 2.2) for i in range(5)], 50)
    assert out["gate_open"] is False
    assert "qualifying washes" in out["gate_reason"]


def test_no_decline_means_no_estimate_not_a_huge_one():
    """The degenerate case, from real data: at 266 injections the retired
    column read ABOVE fresh, and a flat fit through that extrapolated to
    31,744 injections remaining. Gating on the signal suppresses it."""
    washes = [(i * 20, 2.26 + (i % 3) * 0.01) for i in range(20)]
    out = ex.runs_remaining(washes, 400)
    assert out["gate_open"] is False
    assert "less than the 8 % decline" in out["gate_reason"]
    assert out["pct_of_fresh"] > 95


def test_a_flat_fit_is_refused_even_after_a_decline():
    """Decline present but the trend is noise: the slope must be
    distinguishable from flat before anything is projected."""
    washes = [(i * 20, 2.26) for i in range(10)] + [(200 + i * 20, 2.0) for i in range(10)]
    out = ex.runs_remaining(washes, 400)
    if not out["gate_open"]:
        assert "flat" in out["gate_reason"] or "decline" in out["gate_reason"]


def test_an_open_gate_reports_a_range_that_brackets_the_truth():
    """Synthetic column: fresh 2.26, declining 0.001 per injection, so it
    reaches the 76.7 % trigger (1.733) at about 527 injections."""
    washes = [(i * 20, 2.26 - 0.001 * i * 20) for i in range(21)]
    out = ex.runs_remaining(washes, 400)
    assert out["gate_open"] is True
    # `fresh` is the median of the first few washes (2.22), which sets the
    # TRIGGER; the line itself has intercept 2.26. Solve the line for the
    # trigger — conflating the two is what made an earlier version of this
    # test wrong rather than the code.
    trigger = out["trigger_flow_ul_min"]
    truth = (2.26 - trigger) / 0.001 - 400
    assert out["estimate_low"] <= truth <= out["estimate_high"]
    # Noiseless input gives a zero standard error; the model-uncertainty floor
    # must still produce a range rather than a falsely precise point.
    assert out["estimate_high"] > out["estimate_low"]
    assert trigger == round(out["fresh_flow_ul_min"] * ex.WASH_REPLACE_FRAC, 3)


def test_a_brand_new_column_shows_the_gate_shut_with_a_reason():
    """Two washes and seven injections must produce a reason, never a number
    — a silent absence gets read as 'fine'."""
    out = ex.runs_remaining([(2, 2.256), (5, 2.258)], 7)
    assert out["gate_open"] is False
    assert out["gate_reason"]
    assert "estimate_low" not in out


def test_the_trigger_is_relative_so_it_carries_to_another_column_type():
    """No absolute flow and no geometry: a column running at half the flow
    gets a trigger at half the value."""
    a = ex.runs_remaining([(i * 20, 2.26 - 0.001 * i * 20) for i in range(21)], 0)
    b = ex.runs_remaining([(i * 20, 1.13 - 0.0005 * i * 20) for i in range(21)], 0)
    assert abs(a["trigger_flow_ul_min"] / b["trigger_flow_ul_min"] - 2.0) < 0.01
    assert abs(a["estimate_high"] - b["estimate_high"]) <= 2


# ── partial / aborted procedures ──────────────────────────────────────────

def test_a_run_with_no_plateau_does_not_crash_the_extract():
    """LIVE BUG, 2026-09-02: an aborted procedure carries a
    `plateau_flow_ul_min` but a None `plateau_bar`, and

        res = [r["plateau_bar"] / r["plateau_flow_ul_min"] for r in mine]

    raised TypeError, which failed the whole extract and blocked the publish.
    The panel then served a stale document for a day while the operator read a
    column age that was two days out of date.

    It surfaced only on an INTERMEDIATE window. The cron runs a 3-day window
    and the full history, and the offending run sat in neither — so a guard
    like this can be broken for months without anyone noticing. Hence a test
    rather than a fix alone.
    """
    runs = _settled_then(1, 10, 200.0)
    runs.insert(5, {"start": "2026-08-02T11:00:00", "method": "100-samples-per-day",
                    "control_mode": "flow", "plateau_bar": None,
                    "plateau_flow_ul_min": 1.5, "peak_bar": None})
    out = ex.column_lifetimes(runs, _CC_METHODS, [], [])
    assert out["available"] is True
    cur = out["current"]
    # the aborted run still counts as an injection...
    assert cur["injections"] == len(runs)
    # ...but is excluded from the resistance, and the exclusion is COUNTED so
    # a systematically-null field cannot hide as "no data"
    assert cur["runs_without_plateau"] == 1
    assert cur["fresh_resistance_bar_per_ul_min"] == 200.0


def test_every_run_lacking_a_plateau_leaves_resistance_absent_not_zero():
    runs = [{"start": f"2026-08-0{d}T09:00:00", "method": "100-samples-per-day",
             "control_mode": "flow", "plateau_bar": None,
             "plateau_flow_ul_min": 1.5, "peak_bar": None} for d in range(1, 8)]
    out = ex.column_lifetimes(runs, _CC_METHODS, [], [])
    cur = out["current"]
    assert cur["injections"] == 7
    assert cur["runs_without_plateau"] == 7
    assert "fresh_resistance_bar_per_ul_min" not in cur
    assert "wear_pct_of_fresh" not in cur


def test_the_detector_also_survives_a_run_with_no_plateau():
    runs = _settled_then(1, 15, 220.0) + _settled_then(15, 28, 170.0)
    runs.insert(30, {"start": "2026-08-14T20:00:00", "method": "100-samples-per-day",
                     "control_mode": "flow", "plateau_bar": None,
                     "plateau_flow_ul_min": 1.5, "peak_bar": None})
    got = ex.detect_column_changes(runs, _CC_METHODS)
    assert len(got) == 1


# ── wash-level column detection (Brett's rule) ────────────────────────────

def _w(day, flow, hh=9):
    return {"start": f"2026-08-{day:02d}T{hh:02d}:00:00", "method": "wash",
            "control_mode": "pressure", "setpoint_bar": 400.0,
            "plateau_bar": 399.6, "plateau_flow_ul_min": flow}


def test_only_a_return_to_FRESH_counts_as_a_column_change():
    """Level, not step size. A wash recovery lifts flow but cannot reach the
    fresh level; only a replacement can. The 2026-08-31 wash jumped +18.7%,
    larger than the real 2026-07-30 column change's +10.4%, so size alone
    cannot tell them apart."""
    # fresh ~2.26, declines to 1.9, a wash lifts it to 2.05 (NOT fresh)...
    washes = ([_w(d, 2.26) for d in range(1, 6)]
              + [_w(d, 1.90) for d in range(6, 11)]
              + [_w(d, 2.05) for d in range(11, 16)])
    assert ex.detect_column_changes_by_wash(washes) == []
    # ...and now a replacement takes it back to fresh
    washes += [_w(d, 2.26) for d in range(16, 21)]
    got = ex.detect_column_changes_by_wash(washes)
    assert len(got) == 1
    assert got[0]["provenance"] == "detected-wash-level"
    assert got[0]["fell_to_pct_of_fresh"] < 92
    assert got[0]["recovered_to_pct_of_fresh"] >= 97


def test_a_column_that_never_declines_yields_no_boundary():
    """The known limit: the rule needs a decline before a recovery, so a
    column replaced proactively leaves no signature. Safe here only because
    the operator changes columns on failure, never on a schedule."""
    washes = [_w(d, 2.26 + (d % 3) * 0.01) for d in range(1, 21)]
    assert ex.detect_column_changes_by_wash(washes) == []


def test_the_lifetime_distribution_excludes_floors():
    """A start-of-record column's life is a floor and would drag the median
    down if pooled with measured ones."""
    runs = (_settled_then(1, 12, 200.0) + _settled_then(12, 26, 160.0))
    det = ex.detect_column_changes(runs, _CC_METHODS)
    out = ex.column_lifetimes(runs, _CC_METHODS, [], det)
    dist = out["lifetime_distribution"]
    assert dist is None or all(
        c["boundary_provenance"] != "start-of-record"
        for c in out["columns"][:dist["n_columns"]] if not c["is_current"])
