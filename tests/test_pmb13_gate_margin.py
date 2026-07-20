import json
from decimal import Decimal

from kalshi_predictor.benchmarking.gate_margin import (
    EDGE_THRESHOLD,
    PROBABILITY_QUANTUM,
    _decision_margin,
    build_exact_gate_margin_report,
    write_exact_gate_margin_report,
)


def test_pmb13_is_deterministic_and_certifies_every_flippable_boundary(tmp_path):
    first = json.loads(write_exact_gate_margin_report(tmp_path / "a").read_text())
    second = json.loads(write_exact_gate_margin_report(tmp_path / "b").read_text())
    assert first == second
    flippable = [row for row in first["decisions"] if row["forecast_flippable"]]
    assert flippable
    assert all(row["boundary_certified"] for row in flippable)
    assert first["unchanged_thresholds"] == {
        "minimum_edge": str(EDGE_THRESHOLD),
        "probability_quantum": str(PROBABILITY_QUANTUM),
    }
    assert first["database_writes"] == 0
    assert first["execution_enabled"] is False


def test_pmb13_exact_quantized_margin_flips_pass_and_reject_decisions():
    allocated = _decision(forecast="0.60", ask="0.55", blocker=None, status="ALLOCATED")
    rejected = _decision(
        forecast="0.57", ask="0.55", blocker="EDGE_NOT_POSITIVE", status="REJECTED"
    )
    allocated_row = _decision_margin(allocated)
    rejected_row = _decision_margin(rejected)
    assert allocated_row["minimum_forecast_change_to_flip"] == "-0.0300"
    assert Decimal(allocated_row["flipped_forecast_probability"]) <= Decimal("0.57")
    assert rejected_row["minimum_forecast_change_to_flip"] == "0.0001"
    assert Decimal(rejected_row["flipped_forecast_probability"]) > Decimal("0.57")


def test_pmb13_classifies_non_edge_blockers_as_not_forecast_flippable():
    report = build_exact_gate_margin_report()
    assert all(row["active_blocker_type"] in {
        "NONE", "EDGE", "LIQUIDITY", "EXPOSURE", "OTHER"
    } for row in report["decisions"])
    liquidity = _decision(
        forecast="0.80", ask="0.55", blocker="INSUFFICIENT_LIQUIDITY", status="REJECTED"
    )
    row = _decision_margin(liquidity)
    assert row["active_blocker_type"] == "LIQUIDITY"
    assert row["forecast_flippable"] is False
    assert row["minimum_forecast_change_to_flip"] is None


def _decision(*, forecast, ask, blocker, status):
    return {
        "timestamp": "2026-07-17T00:00:00+00:00", "ticker": "SYN",
        "category": "crypto", "status": status, "blocker": blocker,
        "forecast_probability": forecast, "best_yes_ask": ask,
        "model_name": "crypto_v2", "model_version": "2.0.0",
        "feature_ref": {"id": 1}, "observation_ref": {"id": 2},
        "orderbook_ref": {"id": 3},
    }
