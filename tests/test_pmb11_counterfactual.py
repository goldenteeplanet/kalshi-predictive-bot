import json
from decimal import Decimal

import pytest

from kalshi_predictor.benchmarking.counterfactual import (
    build_counterfactual_model_comparison,
    write_counterfactual_model_comparison,
)


def test_pmb11_is_deterministic_and_attributes_every_changed_decision(tmp_path):
    first = json.loads(write_counterfactual_model_comparison(tmp_path / "a").read_text())
    second = json.loads(write_counterfactual_model_comparison(tmp_path / "b").read_text())
    assert first == second
    assert first["comparison"]["changed_decision_count"] > 0
    assert first["comparison"]["all_changes_attributed"] is True
    assert {row["category"] for row in first["baseline"]["decisions"]} == {
        "crypto", "weather", "sports"
    }
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False


def test_pmb11_holds_books_constant_and_changes_only_model_inputs():
    report = build_counterfactual_model_comparison()
    baseline = {(row["ticker"], row["timestamp"]): row for row in report["baseline"]["decisions"]}
    candidate = {(row["ticker"], row["timestamp"]): row for row in report["candidate"]["decisions"]}
    for key in set(baseline) & set(candidate):
        assert baseline[key]["best_yes_ask"] == candidate[key]["best_yes_ask"]
        assert baseline[key]["orderbook_ref"] == candidate[key]["orderbook_ref"]
    assert report["baseline"]["model_versions"] != report["candidate"]["model_versions"]


def test_pmb11_rejects_incomplete_counterfactual_inputs():
    with pytest.raises(ValueError, match="exact synthetic ticker set"):
        build_counterfactual_model_comparison(baseline_forecasts={"SYN-BTC": Decimal("0.5")})
