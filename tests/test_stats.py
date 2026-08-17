"""Unit tests for the latency-distribution helpers (capybench.stats)."""

import pytest

from capybench.stats import percentile, summarize_ms


def test_percentile_empty_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        percentile([], 50)


def test_percentile_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match=r"\[0, 100\]"):
        percentile([1.0], 101)


def test_percentile_single_value() -> None:
    assert percentile([42.0], 99) == 42.0


def test_percentile_endpoints() -> None:
    values = [5.0, 1.0, 3.0]
    assert percentile(values, 0) == 1.0
    assert percentile(values, 100) == 5.0


def test_percentile_interpolates() -> None:
    # rank = 0.95 * 9 = 8.55 over [1..10] -> 9.55
    values = [float(v) for v in range(1, 11)]
    assert percentile(values, 95) == pytest.approx(9.55)
    assert percentile(values, 50) == pytest.approx(5.5)


def test_percentile_unsorted_input() -> None:
    assert percentile([9.0, 1.0, 5.0], 50) == 5.0


def test_summarize_ms_keys_and_ordering() -> None:
    summary = summarize_ms([float(v) for v in range(1, 101)])
    assert set(summary) == {"min_ms", "avg_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms"}
    assert summary["min_ms"] == 1.0
    assert summary["max_ms"] == 100.0
    assert summary["min_ms"] <= summary["p50_ms"] <= summary["p95_ms"] <= summary["p99_ms"]


def test_summarize_ms_empty_rejected() -> None:
    with pytest.raises(ValueError, match="empty"):
        summarize_ms([])
