"""The dashboard's Sync count uses the same readiness rule as submit-all."""

from __future__ import annotations

from stan.dashboard import server
from tests.test_submission_readiness import READY_DIA


def test_backlog_splits_ready_waiting_and_ineligible(monkeypatch):
    monkeypatch.setattr(server, "_pending_community_runs", lambda rows: rows)
    import stan.db as db
    monkeypatch.setattr(db, "run_ids_with_tic", lambda ids, db_path=None: {"e"})
    rows = [
        dict(READY_DIA, id="a"),
        dict(READY_DIA, id="b", tic_rt_bins=None),
        dict(READY_DIA, id="c", peak_capacity=None),
        dict(READY_DIA, id="d", n_precursors=900, n_peptides=500, n_proteins=200),
        # TIC not on the row but stored (SQLite side table / PG-direct): ready
        dict(READY_DIA, id="e", tic_rt_bins=None, tic_intensity=None),
    ]
    got = server._community_backlog(rows)
    assert [r["id"] for r in got["ready"]] == ["a", "e"]
    assert [r["id"] for r in got["needs_metrics"]] == ["b", "c"]
    assert [r["id"] for r in got["ineligible"]] == ["d"]
