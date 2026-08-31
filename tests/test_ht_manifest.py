"""The manifest is a contract with an external search tool.

STAN's job here is narrow: say which files a submission is. The searching
belongs to the Core's proteomics-pipeline skill, which already derives
parameters, pins the engine, batches SLURM and deposits into FRAN. What
matters is that the list is right — a short list would be searched happily
and reported as a success.
"""

from __future__ import annotations

import pytest

from stan.metrics.ht_manifest import build_manifest, classify_run


def _r(name, path="/nfs/x/{}.d", **kw):
    row = {"run_name": name, "raw_path": path.format(name),
           "ms1_total_tic": 1e10, "ms1_max_intensity": 1e8,
           "n_ms2_frames": 5000, "rt_duration_min": 21.0, "verdict": "pass"}
    row.update(kw)
    return row


def test_classification():
    assert classify_run("20260828_100spd_Blank_S5-A11_1_1.d") == "blank"
    assert classify_run("x_wash_S1-A1_1_2.d") == "blank"
    assert classify_run("20260827_793_100spd_Hel50_S6-A12_1_3.d") == "standard"
    assert classify_run("20260827_793_100spd_SI-1_S6-A1_1_4.d") == "sample"
    assert classify_run("anything.d", kind="qc") == "standard", "table wins"


def test_samples_exclude_blanks_and_standards():
    """FRAN's corpus counts searches as customer work; washes would inflate it."""
    rows = [_r(f"20260827_793_100spd_SI-{i}_S6-A{i % 12 + 1}_1_{24026 + i}.d")
            for i in range(10)]
    rows += [_r(f"20260827_793_100spd_Blank_S6-B{i}_1_{24040 + i}.d")
             for i in range(3)]
    rows += [_r(f"20260827_793_100spd_Hel50_S6-C{i}_1_{24050 + i}.d")
             for i in range(2)]
    m = build_manifest("0793", rows)
    assert m["n_files"] == 10
    assert m["counts"] == {"sample": 10, "blank": 3, "standard": 2}
    assert all("Blank" not in f and "Hel50" not in f for f in m["files"])


def test_include_all_and_standards():
    rows = [_r(f"20260827_793_100spd_SI-{i}_S6-A{i % 12 + 1}_1_{24026 + i}.d")
            for i in range(6)]
    rows += [_r(f"20260827_793_100spd_Hel50_S6-C{i}_1_{24050 + i}.d")
             for i in range(2)]
    assert build_manifest("0793", rows, include="all")["n_files"] == 8
    assert build_manifest("0793", rows, include="standards")["n_files"] == 2


def test_manifest_spans_trays_like_the_dashboard():
    """0793 runs S6 then continues onto S5, which never names it."""
    rows = [_r(f"20260827_793_100spd_SI-{i}_S6-A{i % 12 + 1}_1_{24026 + i}.d")
            for i in range(40)]
    rows += [_r(f"20260828_100spd_COH-{i}_S5-B{i % 12 + 1}_1_{24070 + i}.d")
             for i in range(20)]
    m = build_manifest("0793", rows)
    assert set(m["plates"]) == {"S5", "S6"}
    assert m["n_files"] == 60


def test_runs_without_a_raw_path_are_surfaced_not_dropped():
    """A silently short list gets searched and reported as a success."""
    rows = [_r(f"20260827_793_100spd_SI-{i}_S6-A{i % 12 + 1}_1_{24026 + i}.d")
            for i in range(5)]
    rows.append({"run_name": "20260827_793_100spd_SI-99_S6-H1_1_24099.d",
                 "raw_path": None, "verdict": "pass"})
    m = build_manifest("0793", rows)
    assert m["n_files"] == 5
    assert m["missing_paths"] == ["20260827_793_100spd_SI-99_S6-H1_1_24099.d"]


def test_rerun_filter_matches_the_dashboard_flags():
    rows = [_r(f"20260827_793_100spd_SI-{i}_S6-A{i % 12 + 1}_1_{24026 + i}.d")
            for i in range(20)]
    rows.append(_r("20260827_793_100spd_SI-99_S6-H1_1_24099.d", ms1_total_tic=1e8))
    m = build_manifest("0793", rows, include="rerun")
    assert m["n_files"] == 1
    assert "SI-99" in m["files"][0]


def test_unknown_include_is_rejected():
    with pytest.raises(ValueError):
        build_manifest("0793", [], include="everything")
