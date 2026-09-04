"""One-line QC summaries to Slack.

Brett asked for "a one line with the precursors and proteins id'd and
anything that flagged like high peg or mass out of calibration". These pin
the two things that made it non-trivial:

  * PEG is computed AFTER insert_run on the Hive path, so the summary has to
    be posted after `_run_peg_and_drift` or it reports every run PEG-free.
  * `_run_peg_and_drift` returned None on three of its four paths. The caller
    folds its result into `metrics`, so an unguarded update() would have
    raised inside the QC pipeline.
"""
from __future__ import annotations

import pytest

from stan.alerts import format_qc_summary, send_qc_summary, qc_summary_enabled
from stan.gating.evaluator import GateDecision, GateResult


def test_clean_run_is_one_line_with_ids():
    line = format_qc_summary(
        "timsTOF HT", "20260904_hela_60spd",
        {"n_precursors": 48231, "n_proteins": 5102, "ips_score": 92},
        GateDecision(result=GateResult.PASS))
    assert "\n" not in line, "the whole point is that it is one line"
    assert "48,231 precursors" in line
    assert "5,102 proteins" in line
    assert "IPS 92" in line
    assert ":warning:" not in line


def test_high_peg_and_mass_calibration_are_flagged():
    line = format_qc_summary(
        "timsTOF HT", "run",
        {"n_precursors": 31204, "n_proteins": 3880, "ips_score": 41,
         "peg_score": 78.2, "peg_n_ions_detected": 9,
         "pct_delta_mass_lt5ppm": 62.4},
        GateDecision(result=GateResult.FAIL,
                     failed_gates=["pct_delta_mass_lt5ppm"]))
    assert "PEG heavy (78)" in line
    assert "mass calibration (62% <5 ppm)" in line
    assert line.startswith(":red_circle:")


def test_trace_peg_is_not_reported():
    """20-50 is the normal background of shared plasticware; flagging it
    would fire on most runs and train everyone to ignore the channel."""
    line = format_qc_summary(
        "timsTOF HT", "run", {"n_precursors": 40000, "peg_score": 31.0},
        GateDecision(result=GateResult.PASS))
    assert "PEG" not in line


def test_dda_falls_back_to_psms():
    line = format_qc_summary("timsTOF HT", "run",
                             {"n_psms": 12044, "n_proteins": 2100},
                             GateDecision(result=GateResult.PASS))
    assert "12,044 PSMs" in line


def test_missing_metrics_do_not_raise():
    line = format_qc_summary("timsTOF HT", "run", {}, None)
    assert "run" in line


def test_no_webhook_means_no_send(monkeypatch):
    monkeypatch.setattr("stan.alerts._get_slack_webhook", lambda: None)
    assert send_qc_summary("i", "r", {"n_precursors": 1}) is False


def test_env_toggle_disables(monkeypatch):
    monkeypatch.setenv("STAN_SLACK_QC_SUMMARY", "0")
    assert qc_summary_enabled() is False
    monkeypatch.setattr("stan.alerts._get_slack_webhook",
                        lambda: "https://hooks.slack.com/services/X/Y/Z")
    assert send_qc_summary("i", "r", {"n_precursors": 1}) is False
    monkeypatch.setenv("STAN_SLACK_QC_SUMMARY", "1")
    assert qc_summary_enabled() is True


def test_send_posts_when_configured(monkeypatch):
    sent = {}
    monkeypatch.delenv("STAN_SLACK_QC_SUMMARY", raising=False)
    monkeypatch.setattr("stan.alerts._get_slack_webhook",
                        lambda: "https://hooks.slack.com/services/X/Y/Z")
    monkeypatch.setattr("stan.alerts._post_to_slack_async",
                        lambda hook, payload: sent.update(payload))
    assert send_qc_summary("timsTOF HT", "r",
                           {"n_precursors": 100, "n_proteins": 10},
                           GateDecision(result=GateResult.PASS)) is True
    assert "100 precursors" in sent["text"]
    assert sent["blocks"][0]["text"]["type"] == "mrkdwn"


def test_peg_helper_always_returns_a_dict(monkeypatch):
    """Its result is fed to metrics.update(); None there raises TypeError.

    Scoped to one sys.modules entry on purpose. Patching builtins.__import__
    to simulate the missing extra broke 54 unrelated tests in the full run:
    every import during the test went through the stub, and modules that were
    mid-import were left half-built for everyone after it.
    """
    import sys as _sys
    from pathlib import Path
    from stan.pipeline import hive_process

    # A module object with no `read_ms1_any` makes the `from ... import`
    # inside the helper raise ImportError, which is the path under test.
    import types
    monkeypatch.setitem(_sys.modules, "stan.metrics.peg_io",
                        types.ModuleType("stan.metrics.peg_io"))

    got = hive_process._run_peg_and_drift(
        Path("/nonexistent.d"), "rid", Path("/tmp/does-not-matter.db"))
    assert isinstance(got, dict), "None here would raise in metrics.update()"


def test_every_return_path_yields_a_value():
    """Static guard: a bare `return` in this function is the bug again."""
    import ast, inspect
    from stan.pipeline import hive_process
    src = inspect.getsource(hive_process._run_peg_and_drift)
    fn = ast.parse(src.lstrip()).body[0]
    bare = [n.lineno for n in ast.walk(fn)
            if isinstance(n, ast.Return) and n.value is None]
    assert not bare, f"bare return(s) at offset {bare} would give metrics.update(None)"
    assert isinstance(fn.body[-1], ast.Return), "falls off the end -> returns None"


# ── icon colour comes from IPS, not the gate ───────────────────────
#
# Live runs on 2026-09-04 exposed this: FL030926_HeL50_90m_3.raw posted a
# GREEN circle with "IPS 0" and no IDs at all. Every one of the 4,540 rows in
# the runs table is gate_result="pass" -- thresholds.yml does not exist on any
# deployment, so evaluate_gates short-circuits to PASS before it ever compares
# a metric. Colouring off the gate meant the icon was a constant.
#
# IPS is cohort-calibrated from the lab's own baselines and is what the
# front-page IpsBadge already uses, so these bands are copied from it.

_PASS = GateDecision(result=GateResult.PASS)


def test_dead_run_is_red_even_though_the_gate_says_pass():
    """The FL030926_HeL50_90m_3.raw case, verbatim."""
    line = format_qc_summary(
        "Orbitrap Fusion Lumos", "FL030926_HeL50_90m_3.raw",
        {"n_psms": 0, "n_proteins": 0, "ips_score": 0}, _PASS)
    assert line.startswith(":red_circle:")
    assert "no identifications" in line


def test_healthy_run_from_the_same_plate_stays_green():
    line = format_qc_summary(
        "Orbitrap Fusion Lumos", "FL030926_HeL50_120m_2.raw",
        {"n_psms": 53084, "n_proteins": 5430, "ips_score": 85}, _PASS)
    assert line.startswith(":large_green_circle:")
    assert "no identifications" not in line


def test_middling_ips_is_yellow():
    """IPS 74 -- the 35m run that posted alongside the dead one."""
    line = format_qc_summary(
        "Orbitrap Fusion Lumos", "FL030926_HeL50_35m_5.raw",
        {"n_psms": 32074, "n_proteins": 3949, "ips_score": 74}, _PASS)
    assert line.startswith(":large_yellow_circle:")


@pytest.mark.parametrize("ips,icon", [
    (80, ":large_green_circle:"), (79, ":large_yellow_circle:"),
    (60, ":large_yellow_circle:"), (59, ":red_circle:"),
])
def test_band_boundaries_match_the_dashboard(ips, icon):
    line = format_qc_summary("i", "r", {"n_psms": 1, "ips_score": ips}, _PASS)
    assert line.startswith(icon)


def test_without_ips_it_falls_back_to_the_gate():
    line = format_qc_summary("i", "r", {"n_psms": 5000},
                             GateDecision(result=GateResult.FAIL,
                                          failed_gates=["n_psms"]))
    assert line.startswith(":red_circle:")


def test_a_run_that_was_never_searched_is_not_called_dead():
    """No ID key at all != zero IDs. Monitor-pipeline runs must not be flagged."""
    line = format_qc_summary("i", "r", {"n_ms2_frames": 12000}, None)
    assert "no identifications" not in line
