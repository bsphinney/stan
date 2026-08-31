"""High-throughput submission analysis: matching, outliers, plate, queue.

The user-facing question is "which wells do I re-run?", so the bar is that a
flag be defensible: a real difference from the batch, with the numbers shown.
These tests pin the parts that were wrong against the real archive before
they were fixed.
"""

from __future__ import annotations

from stan.metrics.ht_outliers import (
    find_outliers,
    is_edge_well,
    matches_submission,
    parse_injection,
    parse_well,
    plate_map,
    queue_series,
)


def _sample(name, tic=1e10, ms1=1e8, ms2=5000, verdict="pass", **kw):
    row = {"run_name": name, "ms1_total_tic": tic, "ms1_max_intensity": ms1,
           "n_ms2_frames": ms2, "rt_duration_min": 21.0,
           "dropout_rate_per_100_ms1": 0.0, "dynamic_range_log10": 4.0,
           "verdict": verdict, "kind": "sample"}
    row.update(kw)
    return row


# ── submission matching ────────────────────────────────────────────

def test_zero_padded_submission_matches_bare_filename():
    """Operators type 0793; the filename carries 793."""
    assert matches_submission("20260827_793_100spd_Hel50_S6-A12_1_24121.d", "0793")
    assert matches_submission("20260827_793_100spd_Hel50_S6-A12_1_24121.d", "793")


def test_acquisition_counter_is_not_a_submission_match():
    """Both of these are real files from OTHER submissions.

    A substring search for "793" hits the trailing acquisition counter and
    would put another customer's samples on this plate map.
    """
    for name in ("20aug26_GallEV_60spd_med6_S4-F12_1_23793.d",
                 "07102026_HE50_60-spd-dia-_S1-A2_1_22793.d"):
        assert not matches_submission(name, "793"), name
        assert not matches_submission(name, "0793"), name


def test_non_numeric_codes_still_substring_match():
    """Project codes like SK- are how those submissions are findable."""
    assert matches_submission("08132026__60SPD_DIA-SK-10_S3-E7_1_23686.d", "SK-")


def test_empty_query_matches_nothing():
    assert not matches_submission("anything_793_x.d", "")


# ── well / injection parsing ───────────────────────────────────────

def test_parse_well_and_edges():
    assert parse_well("x_S6-E7_1_1.d") == {"plate": "S6", "row": "E", "col": 7}
    assert parse_well("no_well_here.d") is None
    assert is_edge_well("A", 5) and is_edge_well("D", 1) and is_edge_well("H", 12)
    assert not is_edge_well("D", 6)


def test_parse_injection_reads_the_trailing_counter():
    assert parse_injection("20260827_793_100spd_Hel50_S6-A12_1_24121.d") == 24121
    assert parse_injection("nope.d") is None


# ── outliers ───────────────────────────────────────────────────────

def test_low_signal_well_is_flagged_against_its_batch():
    rows = [_sample(f"s{i}_S6-D{i}_1_{100+i}.d", tic=1e10) for i in range(1, 11)]
    rows.append(_sample("dead_S6-D11_1_200.d", tic=1e6))
    out = find_outliers(rows)
    flagged = [r["run_name"] for r in out["needs_rerun"]]
    assert "dead_S6-D11_1_200.d" in flagged
    reasons = out["needs_rerun"][0]["outlier_reasons"]
    assert any(x["metric"] == "ms1_total_tic" for x in reasons)
    assert reasons[0]["median"] is not None, "a flag must show what normal was"


def test_unusually_high_signal_is_not_a_reason_to_rerun():
    """Flagging both tails would send good samples back to the queue."""
    rows = [_sample(f"s{i}_S6-D{i}_1_{100+i}.d", tic=1e10) for i in range(1, 11)]
    rows.append(_sample("bright_S6-D11_1_200.d", tic=9e10))
    out = find_outliers(rows)
    assert out["n_needs_rerun"] == 0


def test_small_cohort_skips_statistics():
    """MAD over four points is noise; one bad well would smear its neighbours."""
    rows = [_sample(f"s{i}_S6-D{i}_1_{100+i}.d", tic=1e10) for i in range(3)]
    rows.append(_sample("low_S6-D9_1_9.d", tic=1.0))
    out = find_outliers(rows)
    assert out["cohort_ok"] is False
    assert out["n_needs_rerun"] == 0


def test_explicit_fail_verdict_is_flagged_even_without_spread():
    """If every well is bad, none is a statistical outlier and all need eyes."""
    rows = [_sample(f"s{i}_S6-D{i}_1_{100+i}.d") for i in range(1, 9)]
    rows.append(_sample("bad_S6-D9_1_9.d", verdict="fail", reasons=["no signal"]))
    out = find_outliers(rows)
    assert any(r["run_name"] == "bad_S6-D9_1_9.d" for r in out["needs_rerun"])


def test_identical_values_produce_no_outliers():
    """MAD of zero must not divide."""
    rows = [_sample(f"s{i}_S6-D{i}_1_{100+i}.d", tic=1e10) for i in range(1, 9)]
    assert find_outliers(rows)["n_needs_rerun"] == 0


# ── plate map ──────────────────────────────────────────────────────

def test_standards_use_precursors_and_are_excluded_from_edge_stats():
    """Precursor counts and TIC differ by orders of magnitude.

    Mixing them would wreck the colour scale and make the edge comparison
    meaningless.
    """
    rows = [_sample(f"s{i}_S6-D{i}_1_{100+i}.d", tic=1e10) for i in range(2, 12)]
    rows += [_sample(f"e{i}_S6-A{i}_1_{200+i}.d", tic=1e10) for i in range(1, 6)]
    std = {"run_name": "hela_S6-A12_1_300.d", "kind": "qc",
           "n_precursors": 30000, "ms1_total_tic": None}
    pm = plate_map(rows + [std])
    p = pm["plates"][0]
    assert p["wells"]["A12"]["value"] == 30000
    assert p["wells"]["A12"]["kind"] == "qc"
    assert p["max"] == 1e10, "sample scale must ignore the standard"
    assert p["edge_effect"]["n_edge"] >= 3


def test_edge_effect_none_when_too_few_wells_to_compare():
    pm = plate_map([_sample("a_S6-D6_1_1.d")])
    assert pm["plates"][0]["edge_effect"] is None


# ── queue trend ────────────────────────────────────────────────────

def test_declining_standards_are_reported_as_negative_trend():
    """A dirtying source shows as falling identifications across the queue."""
    pts = [{"run_name": f"h_S6-A12_1_{1000 + i * 10}.d", "kind": "qc",
            "n_precursors": 30000 - i * 1000, "ms1_total_tic": 1e10}
           for i in range(8)]
    q = queue_series(pts)
    t = q["standards_trend_precursors"]
    assert t is not None and t["n"] == 8
    assert t["pct_change_over_queue"] < 0
    assert q["points"][0]["injection"] < q["points"][-1]["injection"], "sorted by injection"


def test_trend_needs_enough_standards_to_mean_anything():
    pts = [{"run_name": f"h_S6-A12_1_{1000 + i}.d", "kind": "qc",
            "n_precursors": 30000} for i in range(3)]
    assert queue_series(pts)["standards_trend_precursors"] is None


def test_near_zero_quantised_metric_does_not_manufacture_flags():
    """Real regression from submission 0793.

    dropout_rate_per_100_ms1 was 0.0 for 69 of 88 samples, so median and MAD
    were both zero. A mean-absolute-deviation fallback gave z=4.8 to samples
    whose dropout rate was 0.32 per 100 scans -- three tenths of one percent
    -- and put seven of them on the re-run list. Statistically extreme,
    practically meaningless. With a zero median the metric must be skipped.
    """
    rows = []
    for i in range(69):
        rows.append(_sample(f"z{i}_S6-D{i % 12 + 1}_1_{100 + i}.d",
                            dropout_rate_per_100_ms1=0.0))
    for i in range(7):
        rows.append(_sample(f"d{i}_S6-E{i + 1}_1_{300 + i}.d",
                            dropout_rate_per_100_ms1=0.32))
    out = find_outliers(rows)
    assert out["n_needs_rerun"] == 0, (
        "a 0.32-per-100 dropout rate is not a reason to re-run a sample")
    assert out["stats"]["dropout_rate_per_100_ms1"]["rule"] == "skipped"


def test_uniform_batch_with_a_dead_well_still_flags_it():
    """The zero-MAD case that DOES matter: a real, large relative deficit."""
    rows = [_sample(f"s{i}_S6-D{i}_1_{100 + i}.d", tic=1e10) for i in range(1, 11)]
    rows.append(_sample("dead_S6-D11_1_200.d", tic=1e6))
    out = find_outliers(rows)
    flagged = [r["run_name"] for r in out["needs_rerun"]]
    assert "dead_S6-D11_1_200.d" in flagged
    reason = next(x for x in out["needs_rerun"][0]["outlier_reasons"]
                  if x["metric"] == "ms1_total_tic")
    assert reason["pct_of_median"] is not None, "show how far off the batch it is"


def test_partial_plate_reports_what_is_left_to_run():
    """A queue can stop mid-plate — an overpressure trip, an aborted batch.

    The question then is which wells still need injecting, so the map has to
    distinguish "not acquired" from "acquired and empty". Real case: plate S5
    stopped at 39 of 96 wells on 2026-08-28.
    """
    rows = [_sample(f"s_S5-{r}{c}_1_{100 + c}.d")
            for c in (1, 2, 3) for r in "ABCDEFGH"]
    p = plate_map(rows)["plates"][0]
    assert p["n_wells"] == 24
    assert p["n_expected"] == 96
    assert p["n_missing"] == 72
    assert p["is_complete"] is False
    assert p["pct_complete"] == 25.0
    assert "A4" in p["missing_wells"] and "A1" not in p["missing_wells"]


def test_full_plate_reports_complete():
    rows = [_sample(f"s_S6-{r}{c}_1_{100 + c}.d")
            for c in range(1, 13) for r in "ABCDEFGH"]
    p = plate_map(rows)["plates"][0]
    assert p["is_complete"] is True
    assert p["n_missing"] == 0 and p["pct_complete"] == 100.0


def test_tiny_difference_is_not_flagged_however_large_its_z():
    """Real false positive from submission 0793.

    MS2 frame count is near-constant across the plate, so its MAD is tiny and
    a well 5.4% below the median scored z = -249.6 -- while the four samples
    genuinely worth re-running, at 73-80% below median TIC, scored only -3.5
    to -3.9. The largest z on the plate marked the smallest real problem.
    """
    rows = [_sample(f"s{i}_S6-D{i}_1_{100 + i}.d", ms2=6894 + (i % 3))
            for i in range(1, 20)]
    rows.append(_sample("near_S6-E1_1_500.d", ms2=6524))   # 5.4% below
    out = find_outliers(rows)
    assert out["n_needs_rerun"] == 0, (
        "a 5% difference is not a reason to re-run a customer's sample")


def test_large_deficit_is_still_flagged():
    """The signal the gate must not suppress: 80% below the batch."""
    rows = [_sample(f"s{i}_S6-D{i}_1_{100 + i}.d", tic=3.69e10 + i * 1e8)
            for i in range(1, 20)]
    rows.append(_sample("weak_S6-E1_1_500.d", tic=7.47e9))  # ~80% below
    out = find_outliers(rows)
    names = [r["run_name"] for r in out["needs_rerun"]]
    assert "weak_S6-E1_1_500.d" in names
    reason = out["needs_rerun"][0]["outlier_reasons"][0]
    assert reason["pct_diff"] < -70, "lead with the deficit, not the z-score"


def test_letter_code_submissions_are_discovered():
    """Not every plate carries a number.

    Plate S5 on 2026-08-28 was named `20260828_100spd_COH-6_S5-F1_...` --
    no numeric submission, the customer identified only by the sample code.
    Looking for digits alone made that plate invisible to the watcher, which
    is exactly the plate that had stopped and needed watching.
    """
    from stan.metrics.ht_outliers import discover_submissions, submission_key
    assert submission_key("20260828_100spd_COH-6_S5-F1_1_24164.d") == "COH"
    rows = [{"run_name": f"20260828_100spd_COH-{i}_S5-A{i}_1_{100 + i}.d"}
            for i in range(1, 7)]
    assert "COH" in discover_submissions(rows)


def test_numeric_submission_wins_over_the_sample_code():
    """A plate with both must be one group, not two."""
    from stan.metrics.ht_outliers import submission_key
    assert submission_key("20260827_793_100spd_SI-1_S6-A1_1_24046.d") == "793"


def test_a_name_with_neither_is_skipped():
    from stan.metrics.ht_outliers import submission_key
    assert submission_key("blankDia_S1-H8_1_23992.d") is None


# ── a submission spanning several trays ─────────────────────────────

def _run(name):
    return {"run_name": name, "ms1_total_tic": 1e10, "ms1_max_intensity": 1e8,
            "n_ms2_frames": 5000, "rt_duration_min": 21.0, "verdict": "pass"}


def test_submission_follows_its_queue_onto_the_next_tray():
    """Real case: 0793 filled tray S6, then continued onto S5 the next day.

    Only the first tray carries the number -- S5 is named
    `20260828_100spd_COH-12_S5-D2_1_24157.d`. What links them is the
    acquisition counter: S6 ends at 24125, S5 begins at 24126.
    """
    from stan.metrics.ht_outliers import expand_submission_runs
    rows = [_run(f"20260827_793_100spd_SI-{i}_S6-A{i % 12 + 1}_1_{24026 + i}.d")
            for i in range(60)]
    rows += [_run(f"20260828_100spd_COH-{i}_S5-B{i % 12 + 1}_1_{24090 + i}.d")
             for i in range(20)]
    got = {r["run_name"] for r in expand_submission_runs("0793", rows)}
    assert len(got) == 80, "both trays belong to the submission"


def test_expansion_does_not_reach_backwards():
    """The tray that ran BEFORE belongs to whatever came before.

    On 2026-08-27 tray S4 finished at injection 24024 and 793 began at
    24026. Adjacent, but somebody else's work -- expanding backwards
    swallowed all 85 of its runs.
    """
    from stan.metrics.ht_outliers import expand_submission_runs
    rows = [_run(f"27aug26_HeL50_100spd_S4-A{i % 12 + 1}_1_{23990 + i}.d")
            for i in range(30)]                                    # ends 24019
    rows += [_run(f"20260827_793_100spd_SI-{i}_S6-B{i % 12 + 1}_1_{24026 + i}.d")
             for i in range(40)]
    got = {r["run_name"] for r in expand_submission_runs("0793", rows)}
    assert all("_793_" in n for n in got), "must not absorb the earlier tray"


def test_a_tray_naming_another_submission_never_joins():
    from stan.metrics.ht_outliers import expand_submission_runs
    rows = [_run(f"20260827_793_100spd_SI-{i}_S6-A{i % 12 + 1}_1_{24026 + i}.d")
            for i in range(40)]
    rows += [_run(f"20260828_812_100spd_X-{i}_S5-C{i % 12 + 1}_1_{24070 + i}.d")
             for i in range(20)]
    got = {r["run_name"] for r in expand_submission_runs("0793", rows)}
    assert all("_812_" not in n for n in got), "another customer's plate"


def test_reruns_are_picked_up_by_the_submission_search():
    """Re-runs are labelled `0793_rerun` with the sample name."""
    from stan.metrics.ht_outliers import matches_submission
    assert matches_submission("20260901_0793_rerun_SI-48_S1-A1_1_24200.d", "0793")
    assert matches_submission("20260901_793_rerun_Si-70_S1-A2_1_24201.d", "0793")
    assert not matches_submission("20260901_0794_rerun_X-1_S1-A3_1_24202.d", "0793")


def test_a_distant_reuse_of_the_same_tray_label_is_not_joined():
    """Tray labels repeat monthly; S6 in April is not S6 in August."""
    from stan.metrics.ht_outliers import expand_submission_runs
    rows = [_run(f"20260827_793_100spd_SI-{i}_S6-A{i % 12 + 1}_1_{24026 + i}.d")
            for i in range(40)]
    rows += [_run(f"14April2026_x_S6-D{i % 12 + 1}_1_{18450 + i}.d")
             for i in range(30)]
    got = {r["run_name"] for r in expand_submission_runs("0793", rows)}
    assert all("April" not in n for n in got)


def test_hela_standards_are_not_discovered_as_a_submission():
    """Real false alert: '28 consecutive injections flagged in submission HEL'.

    The sample-code fallback exists for plates named without a number, but
    HeLa standards share a prefix too. Grouping them as a submission compares
    the QC standards against each other and alerts on a thing that is not a
    submission — which is how an alert channel gets ignored.
    """
    from stan.metrics.ht_outliers import discover_submissions, submission_key
    assert submission_key("20260827_100spd_HEL-1_S6-A12_1_24121.d") is None
    rows = [{"run_name": f"20260827_100spd_HEL-{i}_S6-A{i}_1_{100 + i}.d"}
            for i in range(1, 9)]
    assert discover_submissions(rows) == []


def test_a_real_code_submission_is_still_discovered():
    from stan.metrics.ht_outliers import discover_submissions
    rows = [{"run_name": f"20260828_100spd_COH-{i}_S5-A{i}_1_{100 + i}.d"}
            for i in range(1, 9)]
    assert discover_submissions(rows) == ["COH"]
