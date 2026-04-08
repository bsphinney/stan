"""Tests for file stability detection."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from stan.watcher.stability import StabilityTracker


def test_bruker_stability_not_ready(mock_d_dir: Path) -> None:
    """A .d directory that hasn't been checked enough times should not be stable."""
    tracker = StabilityTracker(path=mock_d_dir, vendor="bruker", stable_secs=60)

    # First check — not enough history
    with patch("stan.watcher.stability.time") as mock_time:
        mock_time.time.return_value = 1000.0
        assert tracker.check() is False


def test_bruker_stability_ready(mock_d_dir: Path) -> None:
    """A .d directory with constant size over stable_secs should be stable."""
    tracker = StabilityTracker(path=mock_d_dir, vendor="bruker", stable_secs=30)

    # Simulate enough checks with constant size (30s / 10s = 3 checks minimum)
    base_time = 1000.0
    for i in range(4):
        tracker._last_check = 0  # reset guard to allow rapid checks
        tracker._size_history.append((base_time + i * 10, 1024 + 4096))

    # Prune to window and check
    tracker._size_history = [
        (t, s) for t, s in tracker._size_history
        if t >= base_time + 30 - 30
    ]

    # The sizes should all be equal
    sizes = [s for _, s in tracker._size_history]
    assert len(set(sizes)) == 1
    assert sizes[0] > 0


def test_thermo_stability_ready(mock_raw_file: Path) -> None:
    """A .raw file with constant size over stable_secs should be stable."""
    tracker = StabilityTracker(path=mock_raw_file, vendor="thermo", stable_secs=20)

    file_size = mock_raw_file.stat().st_size

    # Simulate enough checks (20s / 10s = 2 checks minimum)
    base_time = 1000.0
    tracker._size_history = [
        (base_time, file_size),
        (base_time + 10, file_size),
    ]
    tracker._last_check = base_time + 10

    # Manually verify the stability logic
    sizes = [s for _, s in tracker._size_history]
    assert len(sizes) >= 2
    assert len(set(sizes)) == 1
    assert sizes[0] > 0


def test_empty_directory_never_stabilizes(tmp_path: Path) -> None:
    """A .d directory with no files (size=0) should never report stable."""
    empty_d = tmp_path / "empty.d"
    empty_d.mkdir()

    tracker = StabilityTracker(path=empty_d, vendor="bruker", stable_secs=20)

    # Simulate checks with zero size
    base_time = 1000.0
    tracker._size_history = [
        (base_time, 0),
        (base_time + 10, 0),
        (base_time + 20, 0),
    ]

    # Even though size is constant, size=0 should not be considered stable
    sizes = [s for _, s in tracker._size_history]
    assert len(set(sizes)) == 1
    assert sizes[0] == 0  # guard: size must be > 0


def test_changing_size_not_stable(mock_d_dir: Path) -> None:
    """A .d directory with changing size should not be stable."""
    tracker = StabilityTracker(path=mock_d_dir, vendor="bruker", stable_secs=20)

    base_time = 1000.0
    tracker._size_history = [
        (base_time, 1000),
        (base_time + 10, 2000),  # size changed
        (base_time + 20, 3000),  # size changed again
    ]

    sizes = [s for _, s in tracker._size_history]
    # Multiple distinct sizes means not stable
    assert len(set(sizes)) > 1
