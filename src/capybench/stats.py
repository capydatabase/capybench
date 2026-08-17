"""Latency-distribution helpers shared by runners and scenarios."""

from __future__ import annotations

import math
from collections.abc import Sequence


def percentile(values: Sequence[float], pct: float) -> float:
    """Linear-interpolated percentile (same method as numpy's default).

    ``pct`` is in [0, 100]. Raises ``ValueError`` on an empty input so a silent
    0.0 never masquerades as a measurement.
    """
    if not values:
        raise ValueError("percentile() of empty sequence")
    if not 0.0 <= pct <= 100.0:
        raise ValueError(f"pct must be in [0, 100], got {pct}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def summarize_ms(values: Sequence[float]) -> dict[str, float]:
    """Standard latency summary: min/avg/p50/p95/p99/max, keyed by metric suffix."""
    if not values:
        raise ValueError("summarize_ms() of empty sequence")
    return {
        "min_ms": min(values),
        "avg_ms": sum(values) / len(values),
        "p50_ms": percentile(values, 50),
        "p95_ms": percentile(values, 95),
        "p99_ms": percentile(values, 99),
        "max_ms": max(values),
    }
