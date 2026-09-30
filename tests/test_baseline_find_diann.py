"""Which DIA-NN binary `stan baseline` searches with.

The Windows installer now installs the pinned 2.3.x beside whatever DIA-NN
is already there, often 2.7.0. The old picker took the highest version on
disk, so baseline searched with 2.7.0 and the relay rejected every row.
"""

from __future__ import annotations

import pytest

from stan import baseline


@pytest.fixture(autouse=True)
def _no_binary_probe(monkeypatch):
    """Folder names carry the version here; never spawn a real binary."""
    import stan.search.version_detect as vd

    monkeypatch.setattr(vd, "detect_diann_version", lambda exe="diann": None)


def test_pinned_minor_beats_newer_major():
    got = baseline._pick_diann(
        ["C:/DIA-NN/2.7.0/DiaNN.exe", "C:/DIA-NN/2.3.2/DiaNN.exe"], "2.3.0"
    )
    assert got == ("C:/DIA-NN/2.3.2/DiaNN.exe", "2.3.2", True)


def test_exact_pin_preferred_within_the_minor():
    got = baseline._pick_diann(
        [
            "C:/DIA-NN/2.3.2/DiaNN.exe",
            "C:/DIA-NN/2.3.0/DiaNN.exe",
            "C:/DIA-NN/2.7.0/DiaNN.exe",
        ],
        "2.3.0",
    )
    assert got == ("C:/DIA-NN/2.3.0/DiaNN.exe", "2.3.0", True)


def test_highest_patch_when_exact_pin_absent():
    got = baseline._pick_diann(
        ["C:/DIA-NN/2.3.1/DiaNN.exe", "C:/DIA-NN/2.3.2/DiaNN.exe"], "2.3.0"
    )
    assert got == ("C:/DIA-NN/2.3.2/DiaNN.exe", "2.3.2", True)


def test_falls_back_to_highest_and_flags_incompatible():
    got = baseline._pick_diann(
        ["C:/DIA-NN/1.8.1/DiaNN.exe", "C:/DIA-NN/2.7.0/DiaNN.exe"], "2.3.0"
    )
    assert got == ("C:/DIA-NN/2.7.0/DiaNN.exe", "2.7.0", False)


def test_unversioned_path_uses_the_binary_header(monkeypatch):
    import stan.search.version_detect as vd

    monkeypatch.setattr(
        vd,
        "detect_diann_version",
        lambda exe="diann": "2.3.2" if exe == "/usr/local/bin/diann" else None,
    )
    got = baseline._pick_diann(
        ["/usr/local/bin/diann", "/opt/DIA-NN/2.7.0/diann"], "2.3.0"
    )
    assert got == ("/usr/local/bin/diann", "2.3.2", True)


def test_no_candidates():
    assert baseline._pick_diann([], "2.3.0") is None


def test_last_version_in_the_folder_wins():
    """A version-like token earlier in the path must not be read as the version."""
    got = baseline._pick_diann(
        ["D:/Tools v1.0/DIA-NN/2.3.2/DiaNN.exe", "C:/Program Files/DIA-NN/2.7.0/DiaNN.exe"],
        "2.3.0",
    )
    assert got == ("D:/Tools v1.0/DIA-NN/2.3.2/DiaNN.exe", "2.3.2", True)


def test_windows_backslash_paths():
    got = baseline._pick_diann(
        ["C:\\DIA-NN\\2.7.0\\DiaNN.exe", "C:\\DIA-NN\\2.3.2\\DiaNN.exe"], "2.3.0"
    )
    assert got == ("C:\\DIA-NN\\2.3.2\\DiaNN.exe", "2.3.2", True)
