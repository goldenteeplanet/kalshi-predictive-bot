import json
from decimal import Decimal

import pytest

from kalshi_predictor.benchmarking.sensitivity import (
    build_sensitivity_grid,
    write_sensitivity_grid,
)


def test_pmb12_builds_deterministic_27_variant_frontier(tmp_path):
    first = json.loads(write_sensitivity_grid(tmp_path / "a").read_text())
    second = json.loads(write_sensitivity_grid(tmp_path / "b").read_text())
    assert first == second
    assert first["grid"]["variant_count"] == 27
    assert first["summary"]["frontier_variants"] > 0
    assert first["summary"]["all_attribution_complete"] is True
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False


def test_pmb12_identifies_stable_and_fragile_ticker_decisions():
    report = build_sensitivity_grid()
    rows = {row["ticker"]: row for row in report["decision_stability"]}
    assert set(rows) == {"SYN-BTC", "SYN-NYC-WEATHER", "SYN-SPORTS"}
    assert report["summary"]["stable_tickers"] + report["summary"]["fragile_tickers"] == 3
    assert all(row["total_variants"] == 27 for row in rows.values())


def test_pmb12_rejects_unbounded_or_empty_grid():
    with pytest.raises(ValueError, match="bounded"):
        build_sensitivity_grid(perturbations=(Decimal("0.11"),))
    with pytest.raises(ValueError, match="non-empty"):
        build_sensitivity_grid(perturbations=())
