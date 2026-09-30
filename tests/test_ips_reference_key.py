"""IPS finds each instrument's own references whatever name the caller uses.

The pipeline passes the family ("timsTOF", "Exploris", "Lumos"); the
references are keyed "timsTOF HT", "Exploris 480", "Lumos". Before 1.2.8 only
Lumos matched and the other two were scored against the pooled global
reference.
"""

from __future__ import annotations

import pytest

from stan.metrics import chromatography as ch


@pytest.mark.parametrize("name,key", [
    ("timsTOF", "timsTOF HT"), ("timsTOF HT", "timsTOF HT"),
    ("Exploris", "Exploris 480"), ("Orbitrap Exploris 480", "Exploris 480"),
    ("Lumos", "Lumos"), ("Orbitrap Fusion Lumos", "Lumos"),
])
def test_family_and_model_names_reach_their_references(name, key):
    ref = ch._get_reference(name, 60 if "tims" in name else 30)
    assert ref is not ch._GLOBAL_REFERENCE
    assert ref is ch._get_reference(key, 60 if "tims" in name else 30)


def test_unknown_instrument_still_falls_back():
    assert ch._get_reference("Astral", 60) is ch._GLOBAL_REFERENCE


def test_dda_references_by_family():
    assert ch._get_dda_reference("Exploris") is ch.IPS_REFERENCES_DDA[("Exploris 480", "*")]
    assert ch._get_dda_reference("timsTOF") is ch.IPS_REFERENCES_DDA[("timsTOF HT", "*")]


def test_exploris_family_scores_against_its_own_cohort():
    """A run at the Exploris reference median scores ~60, not the global-reference value."""
    ref = ch._get_reference("Exploris 480", 30)
    m = {"n_precursors": ref.precursors[1], "n_peptides": ref.peptides[1], "n_proteins": ref.proteins[1],
         "instrument_family": "Exploris", "spd": 30}
    assert ch.compute_ips_dia(m) == 60
