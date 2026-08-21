"""Shared yield ranking for crypto sibling-vector families."""

from __future__ import annotations


def family_yield_score(
    *,
    average_coverage: float,
    bounds_failure_rate: float,
    coherence_failure_rate: float,
    bucket_count: int,
    observed_events: int,
) -> float:
    """Higher scores favor families likely to clear the unchanged 80% gate."""
    coverage_shortfall = max(0.0, 0.80 - average_coverage)
    uncertainty_penalty = 10.0 if observed_events == 0 else 0.0
    width_penalty = max(0, bucket_count - 25) * 0.35
    return round(
        100.0 * average_coverage
        - 120.0 * coverage_shortfall
        - 25.0 * bounds_failure_rate
        - 15.0 * coherence_failure_rate
        - uncertainty_penalty
        - width_penalty,
        4,
    )


def bucket_band(count: int) -> str:
    if count <= 25:
        return "LE25"
    if count <= 50:
        return "26_50"
    if count <= 100:
        return "51_100"
    return "GT100"


def family_key(series_ticker: str, bucket_count: int) -> str:
    return f"{series_ticker}|buckets:{bucket_band(bucket_count)}"
