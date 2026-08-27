"""Consolidation must count each run once.

`fingerprint` is the relay's identity for an acquisition, and the relay
already refuses a duplicate at submit time. Consolidation did not honour
it, so a run that reached the dataset more than once — a `--force`
repopulate, a retry storm — was counted once per copy when cohort
percentiles were computed. On 2026-08-26 that was 1,024 of 4,095
published rows, with 749 runs duplicated up to 6x.
"""

from __future__ import annotations

import polars as pl

from stan.community.scripts.consolidate import _dedupe_by_fingerprint


def _row(fp, submitted_at, n_precursors):
    return {"fingerprint": fp, "submitted_at": submitted_at,
            "n_precursors": n_precursors, "run_name": f"{fp}.d"}


def test_collapses_repeat_submissions():
    df = pl.DataFrame([
        _row("aaa", "2026-05-29T17:02:21Z", 40000),
        _row("aaa", "2026-05-29T17:02:22Z", 40000),
        _row("aaa", "2026-05-29T17:03:13Z", 40000),
        _row("bbb", "2026-05-29T17:02:30Z", 51000),
    ])
    out = _dedupe_by_fingerprint(df)
    assert out.height == 2
    assert sorted(out["fingerprint"].to_list()) == ["aaa", "bbb"]


def test_keeps_the_newest_copy():
    """A re-search resubmitted later must win over the stale result."""
    df = pl.DataFrame([
        _row("aaa", "2026-05-01T00:00:00Z", 30000),
        _row("aaa", "2026-08-01T00:00:00Z", 44000),
        _row("aaa", "2026-06-01T00:00:00Z", 31000),
    ])
    out = _dedupe_by_fingerprint(df)
    assert out.height == 1
    assert out["n_precursors"].to_list() == [44000]


def test_null_fingerprints_are_not_collapsed_together():
    """A null is not evidence two rows are the same run."""
    df = pl.DataFrame([
        _row(None, "2026-05-01T00:00:00Z", 30000),
        _row(None, "2026-05-02T00:00:00Z", 31000),
        _row("aaa", "2026-05-03T00:00:00Z", 32000),
        _row("aaa", "2026-05-04T00:00:00Z", 33000),
    ])
    out = _dedupe_by_fingerprint(df)
    assert out.height == 3, "both null rows survive; only the aaa pair collapses"
    assert out["fingerprint"].null_count() == 2


def test_no_fingerprint_column_is_a_passthrough():
    df = pl.DataFrame({"run_name": ["a.d", "b.d"], "n_precursors": [1, 2]})
    assert _dedupe_by_fingerprint(df).height == 2


def test_already_unique_input_is_unchanged():
    df = pl.DataFrame([
        _row("aaa", "2026-05-01T00:00:00Z", 30000),
        _row("bbb", "2026-05-02T00:00:00Z", 31000),
    ])
    assert _dedupe_by_fingerprint(df).height == 2
