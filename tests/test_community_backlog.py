"""The dashboard's Sync count uses the same readiness rule as submit-all."""

from __future__ import annotations

from stan.dashboard import server
from tests.test_submission_readiness import READY_DIA


def test_backlog_splits_ready_waiting_and_ineligible(monkeypatch):
    monkeypatch.setattr(server, "_pending_community_runs", lambda rows: rows)
    rows = [
        dict(READY_DIA, id="a"),
        dict(READY_DIA, id="b", tic_rt_bins=None),
        dict(READY_DIA, id="c", peak_capacity=None),
        dict(READY_DIA, id="d", n_precursors=900, n_peptides=500, n_proteins=200),
    ]
    got = server._community_backlog(rows)
    assert [r["id"] for r in got["ready"]] == ["a"]
    assert [r["id"] for r in got["needs_metrics"]] == ["b", "c"]
    assert [r["id"] for r in got["ineligible"]] == ["d"]
