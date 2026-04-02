"""Tests for column health trend analysis."""

from __future__ import annotations

from stan.metrics.column_health import _linear_trend, _classify_health


def test_linear_trend_flat():
    """Flat values should produce near-zero slope."""
    slope, r2 = _linear_trend([100, 100, 100, 100, 100])
    assert abs(slope) < 0.001


def test_linear_trend_increasing():
    """Increasing values should produce positive slope."""
    slope, r2 = _linear_trend([10, 20, 30, 40, 50])
    assert slope > 0
    assert r2 > 0.99


def test_linear_trend_decreasing():
    """Decreasing values should produce negative slope."""
    slope, r2 = _linear_trend([50, 40, 30, 20, 10])
    assert slope < 0
    assert r2 > 0.99


def test_linear_trend_insufficient():
    """Single value should return zero."""
    slope, r2 = _linear_trend([100])
    assert slope == 0.0
    assert r2 == 0.0


def test_classify_healthy():
    """Stable TIC should be classified as healthy."""
    status, msg = _classify_health(0.1, 0.05, 0.0, 0.0, [1000] * 20)
    assert status == "healthy"


def test_classify_degraded():
    """Strong declining TIC should be classified as degraded."""
    # Slope = -20 per run out of mean 500 = -4%/run with high R²
    status, msg = _classify_health(-20, 0.8, 0.0, 0.0, list(range(600, 200, -20)))
    assert status == "degraded"
    assert "replacement" in msg.lower() or "declining" in msg.lower()
