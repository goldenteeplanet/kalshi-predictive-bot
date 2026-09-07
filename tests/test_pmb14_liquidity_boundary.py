import json
from decimal import Decimal

import pytest

from kalshi_predictor.benchmarking.liquidity_boundary import (
    build_liquidity_boundary_sweep,
    write_liquidity_boundary_sweep,
)


def test_pmb14_is_deterministic_and_attributes_all_grid_rows(tmp_path):
    first = json.loads(write_liquidity_boundary_sweep(tmp_path / "a").read_text())
    second = json.loads(write_liquidity_boundary_sweep(tmp_path / "b").read_text())
    assert first == second
    assert first["summary"]["rows"] == 27
    assert first["summary"]["allocated"] > 0
    assert first["summary"]["edge_blocked"] > 0
    assert first["summary"]["liquidity_blocked"] > 0
    assert first["summary"]["all_attribution_complete"] is True
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False


def test_pmb14_identifies_exact_depth_and_spread_boundaries():
    report = build_liquidity_boundary_sweep()
    boundaries = {row["ticker"]: row for row in report["ticker_boundaries"]}
    assert set(boundaries) == {"SYN-BTC", "SYN-NYC-WEATHER", "SYN-SPORTS"}
    assert all(
        row["minimum_top_five_depth_for_allocation_by_spread"]["0.02"] == "5"
        for row in boundaries.values()
    )
    assert report["summary"]["partial_fills"] > 0
    assert report["summary"]["full_fills"] > 0


def test_pmb14_rejects_invalid_grids():
    with pytest.raises(ValueError, match="non-empty"):
        build_liquidity_boundary_sweep(spreads=())
    with pytest.raises(ValueError, match="between 0 and 1"):
        build_liquidity_boundary_sweep(spreads=(Decimal("1.0"),))
    with pytest.raises(ValueError, match="non-negative"):
        build_liquidity_boundary_sweep(top_five_depths=(Decimal("-1"),))
