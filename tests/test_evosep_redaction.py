"""The public column-health document must not name a customer's samples.

On 2026-09-03 `/api/maintenance/evosep` was serving `PROT_0793`, 45 full
acquisition names (`20260828_100spd_COH-48_S5-H6_1_24180.d`) and 100 well
positions to anyone with curl, attached to an assertion that those samples
fouled a column. readonly.py already gated `/api/ht` for exactly this reason;
the column-health document grew the same content later and reached an ungated
route.

readonly.py warns that per-field gating fails silently. This is that warning's
countermeasure: the same pattern audit that found the leak, run as a test, so a
new field carrying an identifier fails loudly instead.
"""
from __future__ import annotations

import re

# Acquisition names, submission ids, plate wells.
IDENTIFIER = re.compile(
    r"(?i)(PROT_\d{3,4}"          # submission id
    r"|[A-Za-z0-9\-]+_\d+\.d\b"   # acquisition directory name
    r"|\bS\d-[A-H]\d{1,2}\b)"     # plate well
)


def _strings(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v, f"{path}[]")
    elif isinstance(node, str):
        yield path, node


def _document():
    """A document shaped like the real one, carrying every known leak."""
    return {
        "summary": {"n_runs": 27222},
        "ceiling_bar": 520.0,
        "runs": [{"plateau_bar": 312.0, "well": "S1-H3"}],
        "validation": {"matched": [
            {"file": "20260828_100spd_COH-48_S5-H6_1_24180.d", "well": "S5-H6"},
        ]},
        "sample_impact": {
            "n_flags": 2,
            "flags": [{"run_name": "20260828_100spd_COH-21_S5-E3_1_24214.d",
                       "well": "S5-E3", "submission": "PROT_0793",
                       "delta_bar_per_ul_min": 64.6}],
            "by_submission": [{"submission": "PROT_0793", "n_flagged": 2}],
        },
        "column_events_logged": [{"notes": "swapped after PROT_0793 clogged it"}],
    }


def test_evosep_public_payload_carries_no_identifiers():
    from stan.dashboard.server import _redact_evosep_document

    out = _redact_evosep_document(_document())
    hits = [(p, v) for p, v in _strings(out) if IDENTIFIER.search(v)]
    assert not hits, f"identifiers survived redaction: {hits}"


def test_redaction_keeps_the_measurements():
    """Redaction must not gut the panel -- the pressures are the point."""
    from stan.dashboard.server import _redact_evosep_document

    out = _redact_evosep_document(_document())
    assert out["summary"]["n_runs"] == 27222
    assert out["ceiling_bar"] == 520.0
    assert out["runs"][0]["plateau_bar"] == 312.0
    assert out["sample_impact"]["n_flags"] == 2
    assert out["sample_impact"]["flags"][0]["delta_bar_per_ul_min"] == 64.6


def test_by_submission_is_dropped_entirely():
    """It exists only to attribute fouling to a named customer."""
    from stan.dashboard.server import _redact_evosep_document

    out = _redact_evosep_document(_document())
    assert "by_submission" not in out["sample_impact"]


def test_signed_in_callers_see_the_whole_document():
    from stan.dashboard.server import _maybe_redact_evosep

    class Req:
        headers = {"x-ms-client-principal-name": "bsphinney@ucdavis.edu"}

    doc = _document()
    assert _maybe_redact_evosep(doc, Req()) == doc


def test_anonymous_callers_are_redacted():
    from stan.dashboard.server import _maybe_redact_evosep

    class Req:
        headers = {}

    out = _maybe_redact_evosep(_document(), Req())
    assert "_redacted" in out
    assert "by_submission" not in out["sample_impact"]
